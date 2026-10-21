"""Focused tests for the System Analyzer release/build helpers."""

import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from maintenance import _release


def _make_package(root: Path, version: str) -> None:
    maintenance = root / "maintenance"
    maintenance.mkdir(parents=True)
    (maintenance / "__init__.py").write_text(
        "from maintenance._version import __version__\n"
    )
    (maintenance / "_version.py").write_text(f'__version__ = "{version}"\n')
    (maintenance / "py.typed").write_text("")
    for name in ("main.py", "algo.py"):
        (root / name).write_text(f"# {name}\n")


def _wheel_from_package(root: Path, version: str) -> Path:
    """Zip the exact source bytes into a wheel so manifests compare equal."""

    wheel = root / "dist" / f"system_analyzer-{version}-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in sorted((root / "maintenance").rglob("*")):
            if path.is_file():
                relative = path.relative_to(root / "maintenance").as_posix()
                archive.writestr(f"maintenance/{relative}", path.read_bytes())
        for name in ("main.py", "algo.py"):
            archive.writestr(name, (root / name).read_bytes())
        archive.writestr(
            f"system_analyzer-{version}.dist-info/entry_points.txt",
            "[console_scripts]\nsystem-analyzer = main:main\n",
        )
        archive.writestr(
            f"system_analyzer-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
    return wheel


def _connect_wheel(
    path: Path,
    *,
    name: str = "expra-connect",
    version: str = _release._COMPANION_VERSION,
    tag: str = "py3-none-any",
    purelib: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {str(purelib).lower()}\nTag: {tag}\n",
        )
        if tag != "py3-none-any":
            archive.writestr(
                "expra_connect/cluster/models.cpython-312-x86_64-linux-gnu.so",
                b"native extension",
            )
    return path


class VersionHelpersTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self) -> None:
        self.assertEqual(
            _release._format_version(_release._parse_version("1.2.3.4")), "1.2.3.4"
        )

    def test_parse_rejects_non_four_segment_versions(self) -> None:
        with self.assertRaises(ValueError):
            _release._parse_version("1.0.0")

    def test_bump_carries_into_earlier_segments(self) -> None:
        self.assertEqual(
            _release._format_version(_release._bump_segment((1, 2, 3, 4), 3)), "1.2.3.5"
        )
        self.assertEqual(
            _release._format_version(_release._bump_segment((1, 2, 3, 9), 3)), "1.2.4.0"
        )
        self.assertEqual(
            _release._format_version(_release._bump_segment((1, 2, 9, 9), 2)), "1.3.0.9"
        )
        self.assertEqual(
            _release._format_version(_release._bump_segment((1, 9, 9, 9), 1)), "2.0.9.9"
        )

    def test_next_version_levels(self) -> None:
        self.assertEqual(_release._next_version("1.2.3.4", "none"), "1.2.3.4")
        self.assertEqual(_release._next_version("1.2.3.4", "patch"), "1.2.3.5")
        self.assertEqual(_release._next_version("1.2.3.4", "feature"), "1.2.4.0")
        self.assertEqual(_release._next_version("1.2.3.4", "minor"), "1.3.0.0")

    def test_pick_base_version_prefers_newer(self) -> None:
        self.assertEqual(_release._pick_base_version("1.0.0.0", None), "1.0.0.0")
        self.assertEqual(_release._pick_base_version("1.0.0.0", "1.2.0.0"), "1.2.0.0")
        self.assertEqual(_release._pick_base_version("1.5.0.0", "1.2.0.0"), "1.5.0.0")


class ManifestDiffTests(unittest.TestCase):
    def test_diff_categorises_added_removed_changed(self) -> None:
        diff = _release.diff_manifests(
            {"a": "1", "b": "1", "c": "1"},
            {"b": "2", "c": "1", "d": "1"},
        )
        self.assertEqual(diff.added, ("d",))
        self.assertEqual(diff.removed, ("a",))
        self.assertEqual(diff.changed, ("b",))

    def test_classify_bump_levels(self) -> None:
        no_change = _release.DiffSummary((), (), ())
        self.assertEqual(_release.classify_bump(no_change), "none")

        minor = _release.DiffSummary(("main.py",), (), ())
        self.assertEqual(_release.classify_bump(minor), "minor")

        feature_add = _release.DiffSummary(("maintenance/health.py",), (), ())
        self.assertEqual(_release.classify_bump(feature_add), "feature")

        feature_change = _release.DiffSummary((), (), ("maintenance/scanner.py",))
        self.assertEqual(_release.classify_bump(feature_change), "feature")

        patch_change = _release.DiffSummary((), (), ("maintenance/components/gpu.py",))
        self.assertEqual(_release.classify_bump(patch_change), "patch")

    def test_classify_bump_override(self) -> None:
        diff = _release.DiffSummary((), (), ("maintenance/components/gpu.py",))
        self.assertEqual(_release.classify_bump(diff, override="minor"), "minor")
        with self.assertRaises(ValueError):
            _release.classify_bump(diff, override="bogus")


class VersionFileTests(unittest.TestCase):
    def test_read_and_write_version_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            version_path = package / "maintenance"
            version_path.mkdir()
            (version_path / "_version.py").write_text(
                '__version__ = "1.0.0.0"\n', encoding="utf-8"
            )
            self.assertEqual(_release.read_current_version(package), "1.0.0.0")
            _release.write_current_version(package, "1.1.0.0")
            self.assertEqual(_release.read_current_version(package), "1.1.0.0")


class WheelManifestTests(unittest.TestCase):
    def test_prepare_uses_newer_versioned_bundle_as_previous_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            previous = _wheel_from_package(root, "1.2.0.0")
            bundled = root / "dist" / "native" / "1.2.0.0" / previous.name
            bundled.parent.mkdir(parents=True)
            shutil.move(previous, bundled)

            self.assertEqual(_release._latest_build_wheel(root), bundled)

    def test_version_change_does_not_count_as_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.2.3.4")
            wheel = _wheel_from_package(root, "1.2.3.4")
            wheel_manifest = _release._read_manifest_from_wheel(wheel)

            _release.write_current_version(root, "1.2.3.5")
            source_manifest = _release._collect_package_inputs(root)

            diff = _release.diff_manifests(wheel_manifest, source_manifest)
            self.assertFalse(diff.has_changes, diff)

    def test_real_content_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            wheel = _wheel_from_package(root, "1.0.0.0")
            wheel_manifest = _release._read_manifest_from_wheel(wheel)

            (root / "algo.py").write_text("# changed algo.py\n")
            source_manifest = _release._collect_package_inputs(root)

            diff = _release.diff_manifests(wheel_manifest, source_manifest)
            self.assertIn("algo.py", diff.changed)

    def test_prepare_build_bumps_once_then_stays_put_with_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")

            first = _release.prepare_build(root)
            version_after_first = _release.read_current_version(root)
            self.assertEqual(version_after_first, first)

            _wheel_from_package(root, version_after_first)
            second = _release.prepare_build(root)
            self.assertEqual(second, version_after_first)
            self.assertEqual(_release.read_current_version(root), version_after_first)

    def test_rewrite_sha256sums_contains_only_newest_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            _wheel_from_package(root, "1.0.0.0")
            newer = _wheel_from_package(root, "1.1.0.0")
            _release.rewrite_sha256sums(root / "dist")
            lines = (root / "dist" / "SHA256SUMS").read_text().splitlines()

            self.assertEqual(len(lines), 1)
            self.assertTrue(
                lines[0].endswith("system_analyzer-1.1.0.0-py3-none-any.whl"), lines[0]
            )
            expected = hashlib.sha256(newer.read_bytes()).hexdigest()
            self.assertEqual(lines[0].split()[0], expected)

    def test_rewrite_sha256sums_retains_universal_companion_history_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            dist = root / "dist"
            old_companion = dist / "expra_connect-2.2.0.4-py3-none-any.whl"
            new_companion = dist / "expra_connect-2.2.1.0-py3-none-any.whl"
            native_companion = (
                dist / "expra_connect-2.2.1.0-cp312-cp312-linux_x86_64.whl"
            )
            old_companion.write_bytes(b"old universal")
            new_companion.write_bytes(b"new universal")
            native_companion.write_bytes(b"native")

            _release.rewrite_sha256sums(dist)
            lines = (dist / "SHA256SUMS").read_text().splitlines()
            entries = {line.split(maxsplit=1)[1]: line.split()[0] for line in lines}

            self.assertEqual(
                set(entries),
                {
                    app_wheel.name,
                    old_companion.name,
                    new_companion.name,
                },
            )
            self.assertEqual(
                entries[old_companion.name],
                hashlib.sha256(old_companion.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                entries[new_companion.name],
                hashlib.sha256(new_companion.read_bytes()).hexdigest(),
            )

    def test_rewrite_sha256sums_includes_native_companion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            universal = _connect_wheel(root / "dist" / _release._COMPANION_WHEEL_NAME)
            native = _connect_wheel(
                root / "dist" / "expra_connect-2.2.1.1-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
                purelib=False,
            )

            _release.rewrite_sha256sums(root / "dist")

            entries = {
                line.split(maxsplit=1)[1]: line.split()[0]
                for line in (root / "dist" / "SHA256SUMS").read_text().splitlines()
            }
            self.assertEqual(
                set(entries), {app_wheel.name, universal.name, native.name}
            )
            self.assertEqual(
                entries[native.name], hashlib.sha256(native.read_bytes()).hexdigest()
            )


class WheelVerifyTests(unittest.TestCase):
    def test_verify_accepts_valid_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            wheel = _wheel_from_package(root, "1.0.0.0")
            _release.verify_wheel(wheel)  # must not raise

    def test_verify_rejects_forbidden_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            wheel = _wheel_from_package(root, "1.0.0.0")
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("secrets/.env", "TOKEN=x")
            with self.assertRaises(ValueError):
                _release.verify_wheel(wheel)

    def test_verify_rejects_missing_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            wheel = _wheel_from_package(root, "1.0.0.0")
            rebuild = root / "dist" / "stripped.whl"
            with zipfile.ZipFile(wheel) as source:
                keep = [name for name in source.namelist() if name != "algo.py"]
                with zipfile.ZipFile(rebuild, "w") as archive:
                    for name in keep:
                        archive.writestr(name, source.read(name))
            with self.assertRaises(ValueError):
                _release.verify_wheel(rebuild)

    def test_verify_rejects_native_metadata_in_universal_app_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            wheel = _wheel_from_package(root, "1.0.0.0")
            rebuilt = root / "mutated" / wheel.name
            rebuilt.parent.mkdir()
            with (
                zipfile.ZipFile(wheel) as source,
                zipfile.ZipFile(rebuilt, "w") as target,
            ):
                for name in source.namelist():
                    content = source.read(name)
                    if name.endswith(".dist-info/WHEEL"):
                        content = content.replace(
                            b"Tag: py3-none-any", b"Tag: cp312-cp312-linux_x86_64"
                        )
                    target.writestr(name, content)

            with self.assertRaisesRegex(ValueError, "universal metadata"):
                _release.verify_wheel(rebuilt)


class CompanionWheelTests(unittest.TestCase):
    def test_update_companion_release_updates_pins_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            _make_package(root, "1.0.0.0")
            _wheel_from_package(root, "1.0.0.0")
            current_name = _release._COMPANION_WHEEL_NAME
            current_wheel = dist / current_name
            source_wheel = Path(__file__).resolve().parents[1] / "dist" / current_name
            current_wheel.write_bytes(source_wheel.read_bytes())
            (dist / "SHA256SUMS").write_text("old manifest\n", encoding="utf-8")

            for relative in _release._COMPANION_UPDATE_FILES:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(__file__).resolve().parents[1] / relative, target)

            new_version = _release._next_version(_release._COMPANION_VERSION, "patch")
            candidate = _connect_wheel(
                root / "candidate" / f"expra_connect-{new_version}-py3-none-any.whl",
                version=new_version,
            )

            self.assertEqual(
                _release.main(
                    ["update-companion", str(candidate), "--package-dir", str(root)]
                ),
                0,
            )

            new_name = f"expra_connect-{new_version}-py3-none-any.whl"
            self.assertTrue((dist / new_name).is_file())
            self.assertTrue(current_wheel.is_file())
            self.assertEqual(current_wheel.read_bytes(), source_wheel.read_bytes())
            self.assertIn(new_version, (root / "pyproject.toml").read_text())
            self.assertIn(
                f"expra-connect>={new_version},<3",
                (root / "pyproject.toml").read_text(),
            )
            self.assertIn(
                f"expra-connect>={new_version},<3",
                (root / "requirements.txt").read_text(),
            )
            self.assertIn(new_name, (root / "install/_common.sh").read_text())
            self.assertIn(new_version, (root / "install/install-online.sh").read_text())
            self.assertIn(
                new_version, (root / "install/install-online.ps1").read_text()
            )
            for relative in _release._COMPANION_UPDATE_FILES:
                self.assertNotIn(
                    _release._COMPANION_VERSION,
                    (root / relative).read_text(encoding="utf-8"),
                    relative.as_posix(),
                )
            manifest = (dist / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertIn(
                hashlib.sha256(current_wheel.read_bytes()).hexdigest(), manifest
            )
            self.assertIn(
                hashlib.sha256((dist / new_name).read_bytes()).hexdigest(), manifest
            )

    def test_update_companion_release_rejects_candidate_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            current_name = _release._COMPANION_WHEEL_NAME
            current_wheel = dist / current_name
            source_wheel = Path(__file__).resolve().parents[1] / "dist" / current_name
            current_wheel.write_bytes(source_wheel.read_bytes())
            before = current_wheel.read_bytes()
            new_version = _release._next_version(_release._COMPANION_VERSION, "patch")
            candidate = _connect_wheel(
                root
                / "candidate"
                / f"expra_connect-{new_version}-cp312-cp312-linux_x86_64.whl",
                version=new_version,
                tag="cp312-cp312-linux_x86_64",
            )

            with self.assertRaises(ValueError):
                _release.update_companion_release(candidate, root)

            self.assertEqual(current_wheel.read_bytes(), before)
            self.assertEqual([path.name for path in dist.iterdir()], [current_name])

    def test_checked_in_companion_matches_pinned_artifact(self) -> None:
        wheel = (
            Path(__file__).resolve().parents[1]
            / "dist"
            / _release._COMPANION_WHEEL_NAME
        )
        _release.validate_companion_wheel(wheel)

    def test_release_cli_validates_companion_artifact(self) -> None:
        wheel = (
            Path(__file__).resolve().parents[1]
            / "dist"
            / _release._COMPANION_WHEEL_NAME
        )
        self.assertEqual(
            _release.main(["verify-companion-wheel", str(wheel)]),
            0,
        )

    def test_release_validation_rejects_app_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            companion = root / "dist" / _release._COMPANION_WHEEL_NAME
            companion.write_bytes(
                (
                    Path(__file__).resolve().parents[1] / "dist" / companion.name
                ).read_bytes()
            )
            _release.rewrite_sha256sums(root / "dist")
            with zipfile.ZipFile(app_wheel, "a") as archive:
                archive.writestr("extra-member.txt", "changed after manifest")

            with self.assertRaisesRegex(ValueError, "release checksum mismatch"):
                _release.verify_release_artifacts(app_wheel, root / "dist")

    def test_accepts_pinned_universal_companion_with_matching_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(Path(directory) / _release._COMPANION_WHEEL_NAME)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            _release.validate_companion_wheel(wheel, expected_sha256=digest)

    def test_accepts_native_companion_for_release_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(
                Path(directory)
                / f"expra_connect-{_release._COMPANION_VERSION}-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
                purelib=False,
            )

            digest = _release.validate_companion_wheel(
                wheel, expected_sha256=None, allow_native=True
            )

            self.assertEqual(digest, hashlib.sha256(wheel.read_bytes()).hexdigest())

    def test_release_cli_accepts_explicit_native_companion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(
                Path(directory)
                / f"expra_connect-{_release._COMPANION_VERSION}-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
                purelib=False,
            )

            self.assertEqual(
                _release.main(["verify-companion-wheel", str(wheel), "--native"]),
                0,
            )

    def test_native_bundle_pairs_universal_harness_with_connect_native_wheel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            connect_wheel = _connect_wheel(
                root
                / "input"
                / f"expra_connect-{_release._COMPANION_VERSION}-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
                purelib=False,
            )
            bundle = root / "dist" / "native" / "1.0.0.0"

            _release.create_release_bundle(
                app_wheel, connect_wheel, bundle, variant="native"
            )
            _release.verify_release_bundle(bundle, variant="native")

            self.assertTrue((bundle / app_wheel.name).is_file())
            self.assertTrue((bundle / connect_wheel.name).is_file())
            self.assertTrue((bundle / "SHA256SUMS").is_file())

    def test_release_bundle_refuses_to_replace_existing_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            connect_wheel = _connect_wheel(
                root
                / "input"
                / f"expra_connect-{_release._COMPANION_VERSION}-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
                purelib=False,
            )
            bundle = root / "dist" / "native" / "1.0.0.0"
            bundle.mkdir(parents=True)
            (bundle / "user-file.txt").write_text("keep", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _release.create_release_bundle(
                    app_wheel, connect_wheel, bundle, variant="native"
                )

            self.assertEqual((bundle / "user-file.txt").read_text(), "keep")

    def test_rejects_wrong_companion_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(
                Path(directory)
                / f"expra_connect-{_release._next_version(_release._COMPANION_VERSION, 'patch')}-py3-none-any.whl",
                version=_release._next_version(_release._COMPANION_VERSION, "patch"),
            )
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                _release.validate_companion_wheel(wheel, expected_sha256=digest)

    def test_rejects_non_universal_companion_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(
                Path(directory)
                / f"expra_connect-{_release._COMPANION_VERSION}-cp312-cp312-linux_x86_64.whl",
                tag="cp312-cp312-linux_x86_64",
            )
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                _release.validate_companion_wheel(wheel, expected_sha256=digest)

    def test_rejects_wrong_companion_distribution_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(
                Path(directory)
                / f"other_package-{_release._COMPANION_VERSION}-py3-none-any.whl",
                name="other-package",
            )
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                _release.validate_companion_wheel(wheel, expected_sha256=digest)

    def test_rejects_companion_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = _connect_wheel(Path(directory) / _release._COMPANION_WHEEL_NAME)
            with self.assertRaises(ValueError):
                _release.validate_companion_wheel(wheel, expected_sha256="0" * 64)

    def test_checksum_manifest_includes_both_app_and_companion_wheels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            app_wheel = _wheel_from_package(root, "1.0.0.0")
            companion = _connect_wheel(root / "dist" / _release._COMPANION_WHEEL_NAME)

            _release.rewrite_sha256sums(root / "dist")

            lines = (root / "dist" / "SHA256SUMS").read_text().splitlines()
            self.assertEqual(
                {line.split(maxsplit=1)[1] for line in lines},
                {app_wheel.name, companion.name},
            )

    def test_sync_artifacts_rejects_missing_companion_before_manifest_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_package(root, "1.0.0.0")
            _wheel_from_package(root, "1.0.0.0")
            manifest = root / "dist" / "SHA256SUMS"
            manifest.write_text("preserve prior manifest\n", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                _release.sync_artifacts(root)

            self.assertEqual(
                manifest.read_text(encoding="utf-8"), "preserve prior manifest\n"
            )


if __name__ == "__main__":
    unittest.main()
