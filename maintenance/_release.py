"""Release helpers for the packaged System Analyzer wheel.

The build scripts use this module to:

1. compare the current source inputs with the newest built wheel,
2. auto-bump ``maintenance._version.__version__`` using the four-segment
   decimal-style carry convention,
3. refresh ``dist/SHA256SUMS`` after a build, and
4. verify a built wheel before install (no forbidden content, expected
   members, entry points present).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Literal

VersionBump = Literal["none", "patch", "feature", "minor"]

_VERSION_RE = re.compile(r'(__version__\s*=\s*")(?P<value>\d+\.\d+\.\d+\.\d+)(")')
_WHEEL_NAME_RE = re.compile(r"^system_analyzer-(\d+\.\d+\.\d+\.\d+)-py3-none-any\.whl$")
_CONNECT_NATIVE_WHEEL_RE = re.compile(
    r"^expra_connect-(?P<version>\d+\.\d+\.\d+\.\d+)-"
    r"cp(?P<interpreter>\d+)-cp(?P<abi>\d+)-"
    r"(?P<platform>linux_[a-z0-9_]+|manylinux[a-z0-9_]+|"
    r"macosx_[a-z0-9_]+|win_(?:amd64|arm64)|win32)\.whl$"
)

_VERSION_MODULE = Path("maintenance") / "_version.py"
_PACKAGE_ROOT = "maintenance"
_TOP_LEVEL_MODULES = ("main.py", "algo.py")
_COMPANION_VERSION = "2.2.1.1"
_COMPANION_WHEEL_NAME = f"expra_connect-{_COMPANION_VERSION}-py3-none-any.whl"
_COMPANION_WHEEL_SHA256 = (
    "da6349f9878574d65bf4a90a62e247074a1f4b567679e952ca211c3430faf2b0"
)
_COMPANION_UPDATE_FILES = (
    Path("AGENTS.md"),
    Path("README.md"),
    Path("pyproject.toml"),
    Path("requirements.txt"),
    Path("maintenance/_release.py"),
    Path("install/_common.sh"),
    Path("install/_common.ps1"),
    Path("install/build.sh"),
    Path("install/build.ps1"),
    Path("install/install-user.sh"),
    Path("install/install-user.ps1"),
    Path("install/install-online.sh"),
    Path("install/install-online.ps1"),
)
_COMPANION_WHEEL_RE = re.compile(
    r"^expra_connect-(?P<version>\d+\.\d+\.\d+\.\d+)-py3-none-any\.whl$"
)


@dataclass(frozen=True)
class DiffSummary:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def _parse_version(raw: str) -> tuple[int, int, int, int]:
    pieces = raw.split(".")
    if len(pieces) != 4:
        raise ValueError(f"expected four version segments, got {raw!r}")
    return tuple(int(piece) for piece in pieces)  # type: ignore[return-value]


def _format_version(parts: tuple[int, int, int, int]) -> str:
    return ".".join(str(part) for part in parts)


def _bump_segment(
    parts: tuple[int, int, int, int], index: int
) -> tuple[int, int, int, int]:
    values = list(parts)
    values[index] += 1
    for position in range(index, 0, -1):
        if values[position] <= 9:
            break
        values[position] = 0
        values[position - 1] += 1
    return tuple(values)  # type: ignore[return-value]


def _compare_versions(left: str, right: str) -> int:
    l_parts = _parse_version(left)
    r_parts = _parse_version(right)
    if l_parts < r_parts:
        return -1
    if l_parts > r_parts:
        return 1
    return 0


def _pick_base_version(current: str, latest_wheel: str | None) -> str:
    if latest_wheel is None:
        return current
    return latest_wheel if _compare_versions(latest_wheel, current) > 0 else current


def _next_version(base: str, bump: VersionBump) -> str:
    parts = _parse_version(base)
    if bump == "none":
        return base
    if bump == "minor":
        return _format_version(_bump_segment((parts[0], parts[1], 0, 0), 1))
    if bump == "feature":
        return _format_version(_bump_segment((parts[0], parts[1], parts[2], 0), 2))
    return _format_version(_bump_segment(parts, 3))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _version_normalized(payload: bytes) -> bytes:
    text = payload.decode("utf-8")
    text = _VERSION_RE.sub(r"\1__VERSION__\3", text)
    return text.encode("utf-8")


def _skip_source_path(path: Path) -> bool:
    parts = path.parts
    return "__pycache__" in parts or path.suffix == ".pyc"


def _collect_package_inputs(package_dir: Path) -> dict[str, str]:
    """Hash every importable build input exactly as the wheel stores it.

    Keys match the wheel member names so ``_read_manifest_from_wheel`` can
    compare like-for-like. The ``_version.py`` value is version-normalized so
    an automatic bump never counts as a content change.
    """

    manifest: dict[str, str] = {}
    source_root = package_dir / _PACKAGE_ROOT
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or _skip_source_path(path):
            continue
        relative = path.relative_to(source_root).as_posix()
        payload = path.read_bytes()
        if relative == _VERSION_MODULE.name:
            payload = _version_normalized(payload)
        manifest[f"{_PACKAGE_ROOT}/{relative}"] = _sha256_bytes(payload)
    for name in _TOP_LEVEL_MODULES:
        path = package_dir / name
        manifest[name] = _sha256_bytes(path.read_bytes())
    return manifest


def _read_manifest_from_wheel(wheel_path: Path) -> dict[str, str]:
    """Derive build-input hashes from a wheel's stored members."""

    build_inputs: dict[str, str] = {}
    with zipfile.ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.endswith("/") or ".dist-info/" in name:
                continue
            payload = archive.read(name)
            if name == f"{_PACKAGE_ROOT}/{_VERSION_MODULE.name}":
                payload = _version_normalized(payload)
            build_inputs[name] = _sha256_bytes(payload)
    return build_inputs


def _newest_wheel(dist_dir: Path) -> Path | None:
    wheels: list[tuple[tuple[int, int, int, int], Path]] = []
    for path in dist_dir.glob("system_analyzer-*.whl"):
        match = _WHEEL_NAME_RE.match(path.name)
        if match is None:
            continue
        wheels.append((_parse_version(match.group(1)), path))
    if not wheels:
        return None
    wheels.sort()
    return wheels[-1][1]


def _latest_build_wheel(package_dir: Path) -> Path | None:
    """Find the newest harness wheel in dist or a versioned variant bundle."""

    dist_dir = package_dir / "dist"
    candidates: list[tuple[tuple[int, int, int, int], Path]] = []
    for path in dist_dir.rglob("system_analyzer-*.whl"):
        match = _WHEEL_NAME_RE.fullmatch(path.name)
        if match is None:
            continue
        relative = path.relative_to(dist_dir)
        if len(relative.parts) not in {1, 3}:
            continue
        if len(relative.parts) == 3:
            variant, bundle_version, _ = relative.parts
            if variant not in {"native", "universal"} or bundle_version != match.group(
                1
            ):
                continue
        candidates.append((_parse_version(match.group(1)), path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _wheel_version(path: Path) -> str:
    match = _WHEEL_NAME_RE.match(path.name)
    if match is None:
        raise ValueError(f"unexpected wheel filename: {path.name}")
    return match.group(1)


def diff_manifests(
    previous: Mapping[str, str], current: Mapping[str, str]
) -> DiffSummary:
    before_keys = set(previous)
    after_keys = set(current)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))
    changed = tuple(
        sorted(key for key in before_keys & after_keys if previous[key] != current[key])
    )
    return DiffSummary(added=added, removed=removed, changed=changed)


_MINOR_SURFACES = frozenset(
    {
        "main.py",
        "algo.py",
        "maintenance/connect_runtime.py",
        "maintenance/ui/qt_connect_harness.py",
        "maintenance/actions.py",
    }
)

_FEATURE_SURFACES = frozenset(
    {
        "maintenance/scanner.py",
        "maintenance/health.py",
        "maintenance/preferences.py",
        "maintenance/ui/layout.py",
        "maintenance/ui/preferences_page.py",
        "maintenance/ui/settings_home.py",
        "maintenance/components/coordinator.py",
    }
)


def classify_bump(diff: DiffSummary, *, override: str | None = None) -> VersionBump:
    """Choose the semantic bump level from the source diff.

    ``patch``: routine internal changes. ``feature``: user-visible behaviour
    or a new/changed non-core surface. ``minor``: structural additions or
    removals at the app's top level. ``none``: no build-input change.
    """

    if override:
        if override not in {"none", "patch", "feature", "minor", "auto"}:
            raise ValueError(f"unsupported bump override: {override}")
        if override != "auto":
            return override  # type: ignore[return-value]

    if not diff.has_changes:
        return "none"

    if diff.added or diff.removed:
        if any(area in _MINOR_SURFACES for area in (*diff.added, *diff.removed)):
            return "minor"
        return "feature"

    if any(key in _FEATURE_SURFACES for key in diff.changed):
        return "feature"
    return "patch"


def read_current_version(package_dir: Path) -> str:
    version_path = package_dir / _VERSION_MODULE
    match = _VERSION_RE.search(version_path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"could not find __version__ in {version_path}")
    return match.group("value")


def write_current_version(package_dir: Path, version: str) -> None:
    version_path = package_dir / _VERSION_MODULE
    text = version_path.read_text(encoding="utf-8")
    updated, count = _VERSION_RE.subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count != 1:
        raise ValueError(f"could not update __version__ in {version_path}")
    version_path.write_text(updated, encoding="utf-8")


def rewrite_sha256sums(dist_dir: Path, *, companion_wheel: Path | None = None) -> None:
    wheel = _newest_wheel(dist_dir)
    artifacts = [wheel] if wheel is not None else []
    companions = {
        path
        for path in dist_dir.glob("expra_connect-*-py3-none-any.whl")
        if _COMPANION_WHEEL_RE.fullmatch(path.name) is not None
    }
    companions.update(
        path
        for path in dist_dir.glob("expra_connect-*.whl")
        if (match := _CONNECT_NATIVE_WHEEL_RE.fullmatch(path.name)) is not None
        and match.group("version") == _COMPANION_VERSION
    )
    if (
        companion_wheel is not None
        and companion_wheel.is_file()
        and _COMPANION_WHEEL_RE.fullmatch(companion_wheel.name) is not None
    ):
        companions.add(companion_wheel)
    artifacts.extend(sorted(companions, key=lambda path: path.name))
    lines = [
        f"{_sha256_bytes(path.read_bytes())}  {path.name}"
        for path in artifacts
        if path is not None
    ]
    (dist_dir / "SHA256SUMS").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


def validate_companion_wheel(
    wheel_path: Path,
    *,
    expected_version: str | None = _COMPANION_VERSION,
    expected_sha256: str | None = _COMPANION_WHEEL_SHA256,
    allow_native: bool = False,
) -> str:
    """Validate a pinned universal or explicitly allowed native companion wheel."""

    universal_match = _COMPANION_WHEEL_RE.fullmatch(wheel_path.name)
    native_match = (
        _CONNECT_NATIVE_WHEEL_RE.fullmatch(wheel_path.name) if allow_native else None
    )
    if universal_match is None and native_match is None:
        raise ValueError(f"unexpected expra-connect wheel filename: {wheel_path.name}")
    is_native = native_match is not None
    if native_match is not None:
        filename_version = native_match.group("version")
        expected_tag = (
            f"cp{native_match.group('interpreter')}-cp{native_match.group('abi')}-"
            f"{native_match.group('platform')}"
        )
        if native_match.group("interpreter") != native_match.group("abi"):
            raise ValueError(
                "expra-connect native wheel has inconsistent interpreter and ABI tags"
            )
    else:
        if universal_match is None:
            raise ValueError(
                f"unexpected expra-connect wheel filename: {wheel_path.name}"
            )
        filename_version = universal_match.group("version")
        expected_tag = "py3-none-any"
    if expected_version is not None and filename_version != expected_version:
        raise ValueError("expra-connect wheel has unexpected filename version")
    actual_sha256 = _sha256_bytes(wheel_path.read_bytes())
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ValueError("expra-connect wheel checksum mismatch")

    try:
        with zipfile.ZipFile(wheel_path) as archive:
            metadata_paths = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            wheel_paths = [
                name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
            ]
            if len(metadata_paths) != 1 or len(wheel_paths) != 1:
                raise ValueError("expra-connect wheel has ambiguous metadata")
            metadata = Parser().parsestr(
                archive.read(metadata_paths[0]).decode("utf-8")
            )
            wheel_metadata = Parser().parsestr(
                archive.read(wheel_paths[0]).decode("utf-8")
            )
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ValueError("invalid expra-connect wheel archive") from error

    if metadata.get("Name", "").lower().replace("_", "-") != "expra-connect":
        raise ValueError("expra-connect wheel has unexpected distribution metadata")
    if metadata.get("Version") != filename_version:
        raise ValueError("expra-connect wheel filename and metadata versions differ")
    if expected_version is not None and metadata.get("Version") != expected_version:
        raise ValueError("expra-connect wheel has unexpected version metadata")
    tags = wheel_metadata.get_all("Tag", [])
    purelib = wheel_metadata.get("Root-Is-Purelib", "").lower() == "true"
    if tags != [expected_tag] or purelib == is_native:
        raise ValueError("expra-connect wheel tag or purelib metadata is invalid")
    with zipfile.ZipFile(wheel_path) as archive:
        has_extensions = any(
            name.endswith((".so", ".pyd")) for name in archive.namelist()
        )
    if is_native and not has_extensions:
        raise ValueError("expra-connect native wheel contains no native extensions")
    if not is_native and has_extensions:
        raise ValueError("expra-connect universal wheel contains native extensions")
    return actual_sha256


def create_release_bundle(
    app_wheel: Path,
    companion_wheel: Path,
    bundle_dir: Path,
    *,
    variant: Literal["native", "universal"],
) -> Path:
    """Create an immutable two-wheel bundle for one Connect wheel flavor."""

    verify_wheel(app_wheel)
    app_version = _wheel_version(app_wheel)
    if variant == "native":
        validate_companion_wheel(
            companion_wheel, expected_sha256=None, allow_native=True
        )
        if _CONNECT_NATIVE_WHEEL_RE.fullmatch(companion_wheel.name) is None:
            raise ValueError("native bundle requires a native expra-connect wheel")
    else:
        validate_companion_wheel(companion_wheel)
        if _COMPANION_WHEEL_RE.fullmatch(companion_wheel.name) is None:
            raise ValueError(
                "universal bundle requires a universal expra-connect wheel"
            )

    files = (app_wheel, companion_wheel)
    if bundle_dir.exists():
        expected_names = {path.name for path in files} | {"SHA256SUMS"}
        actual_names = {path.name for path in bundle_dir.iterdir()}
        if actual_names != expected_names:
            raise FileExistsError(f"refusing to replace existing bundle: {bundle_dir}")
        verify_release_bundle(bundle_dir, variant=variant)
        for source in files:
            if (bundle_dir / source.name).read_bytes() != source.read_bytes():
                raise FileExistsError(
                    f"refusing to replace bundle member: {source.name}"
                )
        return bundle_dir

    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="system-analyzer-bundle-", dir=bundle_dir.parent
    ) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for source in files:
            shutil.copyfile(source, staging / source.name)
        lines = [
            f"{_sha256_bytes(source.read_bytes())}  {source.name}" for source in files
        ]
        (staging / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(staging, bundle_dir)
    print(
        f"Created {variant} release bundle for system-analyzer {app_version}: {bundle_dir}"
    )
    return bundle_dir


def verify_release_bundle(
    bundle_dir: Path, *, variant: Literal["native", "universal"]
) -> None:
    """Verify exactly one harness wheel and its matching Connect companion."""

    app_wheels = [
        path
        for path in bundle_dir.glob("system_analyzer-*.whl")
        if _WHEEL_NAME_RE.fullmatch(path.name) is not None
    ]
    companion_wheels = list(bundle_dir.glob("expra_connect-*.whl"))
    if len(app_wheels) != 1 or len(companion_wheels) != 1:
        raise ValueError("bundle must contain one harness wheel and one Connect wheel")
    app_wheel = app_wheels[0]
    companion = companion_wheels[0]
    verify_wheel(app_wheel)
    if variant == "native":
        validate_companion_wheel(companion, expected_sha256=None, allow_native=True)
        if _CONNECT_NATIVE_WHEEL_RE.fullmatch(companion.name) is None:
            raise ValueError("native bundle does not contain a native Connect wheel")
    else:
        validate_companion_wheel(companion)
        if _COMPANION_WHEEL_RE.fullmatch(companion.name) is None:
            raise ValueError(
                "universal bundle does not contain a universal Connect wheel"
            )

    expected = {app_wheel.name, companion.name}
    if {path.name for path in bundle_dir.iterdir()} != expected | {"SHA256SUMS"}:
        raise ValueError("bundle contains unexpected files")
    checksum_path = bundle_dir / "SHA256SUMS"
    if not checksum_path.is_file():
        raise FileNotFoundError(f"missing bundle checksum file: {checksum_path}")
    checksums: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+([A-Za-z0-9_.-]+)", line.strip())
        if match is None:
            raise ValueError("invalid bundle checksum entry")
        digest, name = match.groups()
        if name in checksums:
            raise ValueError(f"duplicate bundle checksum entry for {name}")
        checksums[name] = digest.lower()
    if set(checksums) != expected:
        raise ValueError("bundle checksum entries do not match its wheel members")
    for path in (app_wheel, companion):
        if checksums[path.name] != _sha256_bytes(path.read_bytes()):
            raise ValueError(f"bundle checksum mismatch for {path.name}")
    print(f"release bundle OK: {bundle_dir}")


def update_companion_release(wheel_path: Path, package_dir: Path) -> None:
    """Install a validated universal wheel and update maintained release pins."""

    if not wheel_path.is_file():
        raise FileNotFoundError(f"missing candidate expra-connect wheel: {wheel_path}")
    candidate_digest = validate_companion_wheel(
        wheel_path, expected_version=None, expected_sha256=None
    )
    version = _COMPANION_WHEEL_RE.fullmatch(wheel_path.name)
    if version is None:
        raise ValueError("candidate wheel filename is invalid")
    new_version = version.group("version")
    new_name = f"expra_connect-{new_version}-py3-none-any.whl"
    if _compare_versions(new_version, _COMPANION_VERSION) <= 0:
        raise ValueError(
            f"candidate version {new_version} is not newer than {_COMPANION_VERSION}"
        )

    staged_text: dict[Path, str] = {}
    old_name = _COMPANION_WHEEL_NAME
    for relative in _COMPANION_UPDATE_FILES:
        path = package_dir / relative
        if not path.is_file():
            raise FileNotFoundError(f"missing companion update target: {path}")
        text = path.read_text(encoding="utf-8")
        if _COMPANION_VERSION not in text:
            raise ValueError(f"expected current companion version in {path}")
        updated = text.replace(_COMPANION_VERSION, new_version)
        updated = updated.replace(old_name, new_name)
        updated = updated.replace(_COMPANION_WHEEL_SHA256, candidate_digest)
        staged_text[path] = updated

    dist_dir = package_dir / "dist"
    old_wheel = dist_dir / old_name
    if not old_wheel.is_file():
        raise FileNotFoundError(f"missing current companion wheel: {old_wheel}")
    new_wheel = dist_dir / new_name
    if new_wheel.exists() and new_wheel != old_wheel:
        raise FileExistsError(f"target companion wheel already exists: {new_wheel}")

    original_files = {path: path.read_bytes() for path in staged_text}
    original_companion = old_wheel.read_bytes()
    original_manifest = (
        (dist_dir / "SHA256SUMS").read_bytes()
        if (dist_dir / "SHA256SUMS").exists()
        else None
    )
    with tempfile.TemporaryDirectory(
        prefix="expra-connect-update-", dir=package_dir
    ) as temp:
        staged_dir = Path(temp)
        staged_wheel = staged_dir / new_name
        shutil.copyfile(wheel_path, staged_wheel)
        staged_files: dict[Path, Path] = {}
        for index, (path, text) in enumerate(staged_text.items()):
            staged_path = staged_dir / f"file-{index}"
            staged_path.write_text(text, encoding="utf-8")
            staged_files[path] = staged_path
        try:
            os.replace(staged_wheel, new_wheel)
            for path, staged_path in staged_files.items():
                os.replace(staged_path, path)
            rewrite_sha256sums(dist_dir, companion_wheel=new_wheel)
        except OSError:
            for path, payload in original_files.items():
                path.write_bytes(payload)
            old_wheel.write_bytes(original_companion)
            if original_manifest is None:
                (dist_dir / "SHA256SUMS").unlink(missing_ok=True)
            else:
                (dist_dir / "SHA256SUMS").write_bytes(original_manifest)
            new_wheel.unlink(missing_ok=True)
            raise
    print(f"Updated expra-connect to {new_version} ({new_name})")


def verify_release_artifacts(app_wheel: Path, dist_dir: Path) -> None:
    """Validate the selected app wheel and its pinned companion release artifact."""

    if app_wheel.parent.resolve() != dist_dir.resolve():
        raise ValueError("application wheel must be in the release dist directory")
    verify_wheel(app_wheel)

    manifest_path = dist_dir / "SHA256SUMS"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing release checksum manifest: {manifest_path}")
    checksums: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+([A-Za-z0-9_.-]+)", line.strip())
        if match is None:
            raise ValueError("invalid release checksum manifest entry")
        digest, name = match.groups()
        if name in checksums:
            raise ValueError(f"duplicate release checksum entry for {name}")
        checksums[name] = digest.lower()

    companion = dist_dir / _COMPANION_WHEEL_NAME
    validate_companion_wheel(companion)
    companion_digest = checksums.get(companion.name)
    if companion_digest != _COMPANION_WHEEL_SHA256:
        raise ValueError(
            "release manifest does not match the pinned expra-connect wheel"
        )

    for native_wheel in dist_dir.glob("expra_connect-*.whl"):
        native_match = _CONNECT_NATIVE_WHEEL_RE.fullmatch(native_wheel.name)
        if native_match is None or native_match.group("version") != _COMPANION_VERSION:
            continue
        validate_companion_wheel(
            native_wheel,
            expected_version=_COMPANION_VERSION,
            expected_sha256=None,
            allow_native=True,
        )
        if checksums.get(native_wheel.name) != _sha256_bytes(native_wheel.read_bytes()):
            raise ValueError(f"release checksum mismatch for {native_wheel.name}")

    app_digest = checksums.get(app_wheel.name)
    if app_digest is not None and app_digest != _sha256_bytes(app_wheel.read_bytes()):
        raise ValueError(f"release checksum mismatch for {app_wheel.name}")


def prepare_build(package_dir: Path, *, bump_override: str | None = None) -> str:
    """Auto-bump the version to the next build when source inputs changed."""

    current_manifest = _collect_package_inputs(package_dir)
    latest_wheel = _latest_build_wheel(package_dir)
    previous_manifest = (
        _read_manifest_from_wheel(latest_wheel) if latest_wheel is not None else {}
    )
    diff = diff_manifests(previous_manifest, current_manifest)
    current_version = read_current_version(package_dir)
    latest_version = _wheel_version(latest_wheel) if latest_wheel is not None else None
    base_version = _pick_base_version(current_version, latest_version)
    bump = classify_bump(diff, override=bump_override)
    next_version = _next_version(base_version, bump)
    if current_version != next_version:
        write_current_version(package_dir, next_version)
    print(f"current source version: {current_version}")
    print(f"latest built wheel: {latest_version or 'none'}")
    print(
        "diff against newest wheel: "
        f"{len(diff.changed)} changed, {len(diff.added)} added, {len(diff.removed)} removed"
    )
    print(f"selected bump: {bump}")
    print(f"next build version: {next_version}")
    return next_version


def sync_artifacts(package_dir: Path) -> Path:
    dist_dir = package_dir / "dist"
    wheel = _newest_wheel(dist_dir)
    if wheel is None:
        raise FileNotFoundError(f"no system_analyzer wheel found in {dist_dir}")
    companion = dist_dir / _COMPANION_WHEEL_NAME
    if not companion.is_file():
        raise FileNotFoundError(f"missing companion wheel: {companion}")
    validate_companion_wheel(companion)
    for native_wheel in dist_dir.glob("expra_connect-*.whl"):
        match = _CONNECT_NATIVE_WHEEL_RE.fullmatch(native_wheel.name)
        if match is None or match.group("version") != _COMPANION_VERSION:
            continue
        validate_companion_wheel(
            native_wheel,
            expected_version=_COMPANION_VERSION,
            expected_sha256=None,
            allow_native=True,
        )
    rewrite_sha256sums(dist_dir)
    print(f"SHA256SUMS refreshed for {wheel.name}")
    return wheel


def verify_wheel(wheel_path: Path) -> None:
    """Verify a built wheel: no forbidden content and expected members."""

    forbidden = (".env", ".venv", "tests/", "node_modules", "__pycache__", ".pyc")
    required = (
        "maintenance/__init__.py",
        "maintenance/_version.py",
        "main.py",
        "algo.py",
    )
    if _WHEEL_NAME_RE.fullmatch(wheel_path.name) is None:
        raise ValueError(f"unexpected application wheel filename: {wheel_path.name}")
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        bad = [
            name for name in names if any(token in name.lower() for token in forbidden)
        ]
        missing = [name for name in required if name not in names]
        entry_points = [name for name in names if name.endswith("entry_points.txt")]
        wheel_metadata_paths = [
            name for name in names if name.endswith(".dist-info/WHEEL")
        ]
    if bad:
        raise ValueError(f"forbidden wheel content: {bad}")
    if missing:
        raise ValueError(f"missing wheel members: {missing}")
    if not entry_points:
        raise ValueError("wheel has no entry_points.txt")
    if len(wheel_metadata_paths) != 1:
        raise ValueError("wheel has ambiguous or missing WHEEL metadata")
    with zipfile.ZipFile(wheel_path) as archive:
        wheel_metadata = Parser().parsestr(
            archive.read(wheel_metadata_paths[0]).decode("utf-8")
        )
    if (
        wheel_metadata.get_all("Tag", []) != ["py3-none-any"]
        or wheel_metadata.get("Root-Is-Purelib", "").lower() != "true"
        or any(name.endswith((".so", ".pyd")) for name in names)
    ):
        raise ValueError("application wheel does not have universal metadata")
    print(f"sha256: {_sha256_bytes(wheel_path.read_bytes())}")
    print(f"contents OK: {len(names)} members; no forbidden paths")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release helpers for System Analyzer")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare-build", help="diff sources and auto-bump version")
    prepare.add_argument("--package-dir", required=True)
    prepare.add_argument(
        "--bump", default="auto", choices=["auto", "none", "patch", "feature", "minor"]
    )

    sync = sub.add_parser("sync-artifacts", help="refresh dist/SHA256SUMS")
    sync.add_argument("--package-dir", required=True)

    verify = sub.add_parser("verify-wheel", help="verify one built wheel")
    verify.add_argument("wheel", nargs="?", help="path to the wheel to verify")

    release = sub.add_parser(
        "verify-release-artifacts",
        help="verify an app wheel and its companion artifact",
    )
    release.add_argument("wheel", help="selected app wheel path")
    release.add_argument("--dist-dir", help="release artifact directory")

    companion = sub.add_parser(
        "verify-companion-wheel", help="verify the pinned expra-connect wheel"
    )
    companion.add_argument("wheel", help="expra-connect wheel path")
    companion.add_argument(
        "--native", action="store_true", help="allow a native CPython wheel"
    )

    bundle = sub.add_parser("create-bundle", help="pair app and Connect wheels")
    bundle.add_argument("app_wheel")
    bundle.add_argument("companion_wheel")
    bundle.add_argument("output_dir")
    bundle.add_argument("--variant", required=True, choices=["native", "universal"])

    verify_bundle = sub.add_parser("verify-bundle", help="verify one release bundle")
    verify_bundle.add_argument("bundle_dir")
    verify_bundle.add_argument(
        "--variant", required=True, choices=["native", "universal"]
    )

    update_companion = sub.add_parser(
        "update-companion", help="update to a newer universal expra-connect wheel"
    )
    update_companion.add_argument("wheel", help="new universal expra-connect wheel")
    update_companion.add_argument("--package-dir", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "prepare-build":
        prepare_build(Path(args.package_dir).resolve(), bump_override=args.bump)
        return 0
    if args.command == "sync-artifacts":
        sync_artifacts(Path(args.package_dir).resolve())
        return 0
    if args.command == "verify-wheel":
        if not args.wheel:
            parser.error("verify-wheel requires a wheel path")
        verify_wheel(Path(args.wheel).resolve())
        return 0
    if args.command == "verify-release-artifacts":
        wheel = Path(args.wheel).resolve()
        dist_dir = Path(args.dist_dir).resolve() if args.dist_dir else wheel.parent
        verify_release_artifacts(wheel, dist_dir)
        return 0
    if args.command == "verify-companion-wheel":
        wheel = Path(args.wheel).resolve()
        validate_companion_wheel(
            wheel,
            expected_sha256=None if args.native else _COMPANION_WHEEL_SHA256,
            allow_native=args.native,
        )
        if args.native and _CONNECT_NATIVE_WHEEL_RE.fullmatch(wheel.name) is None:
            raise ValueError("--native requires a native expra-connect wheel")
        print(f"expra-connect wheel OK: {wheel.name}")
        return 0
    if args.command == "create-bundle":
        create_release_bundle(
            Path(args.app_wheel).resolve(),
            Path(args.companion_wheel).resolve(),
            Path(args.output_dir).resolve(),
            variant=args.variant,
        )
        return 0
    if args.command == "verify-bundle":
        verify_release_bundle(Path(args.bundle_dir).resolve(), variant=args.variant)
        return 0
    if args.command == "update-companion":
        update_companion_release(
            Path(args.wheel).resolve(), Path(args.package_dir).resolve()
        )
        return 0
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
