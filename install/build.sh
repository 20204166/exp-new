#!/usr/bin/env bash
# Build the universal System Analyzer wheel and one paired release bundle.
# Use --variant native with --native-connect-wheel for the native bundle.
#
# Build-backend strategy: the offline fast path uses the setuptools already
# present in the resolved interpreter (--no-build-isolation, no network). When
# setuptools>=68 is missing there, build.sh falls back to pip build isolation,
# which installs the declared requirement from the configured package source.
# Override with SA_BUILD_ISOLATION=auto|never|always. A failed build
# never leaves the package version bumped without a matching wheel.
#
# Before building, maintenance._release auto-bumps the package version when the
# source inputs differ from the newest built wheel, and syncs dist/SHA256SUMS
# afterwards. Override the bump with SA_VERSION_BUMP=none|patch|feature|minor.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$here/install/_common.sh"
py="$(resolve_python)"
bump="${SA_VERSION_BUMP:-auto}"
isolation="${SA_BUILD_ISOLATION:-auto}"
variant="universal"
native_connect_wheel=""
variant_set=0
while (($#)); do
  case "$1" in
    --variant)
      if [ "$variant_set" -eq 1 ] || [ "$#" -lt 2 ]; then
        echo "ERROR: --variant requires one value and may only be specified once." >&2
        exit 2
      fi
      case "$2" in
        universal|native) variant="$2" ;;
        *) echo "ERROR: variant must be universal or native." >&2; exit 2 ;;
      esac
      variant_set=1
      shift
      ;;
    --native-connect-wheel)
      if [ -n "$native_connect_wheel" ] || [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
        echo "ERROR: --native-connect-wheel requires one path and may only be used once." >&2
        exit 2
      fi
      native_connect_wheel="$2"
      shift
      ;;
    *)
      echo "usage: $0 [--variant universal|native] [--native-connect-wheel PATH]" >&2
      exit 2
      ;;
  esac
  shift
done
if [ "$variant" = "native" ] && [ -z "$native_connect_wheel" ]; then
  echo "ERROR: native variant requires --native-connect-wheel PATH." >&2
  exit 2
fi
if [ "$variant" = "universal" ] && [ -n "$native_connect_wheel" ]; then
  echo "ERROR: --native-connect-wheel requires --variant native." >&2
  exit 2
fi

if [ -n "$native_connect_wheel" ] && [[ "$native_connect_wheel" != /* ]]; then
  native_connect_wheel="$PWD/$native_connect_wheel"
fi
cd "$here"

require_python "$py" || exit 1

if [ "$variant" = "universal" ]; then
  "$py" -m maintenance._release verify-companion-wheel \
    "$here/dist/expra_connect-2.2.1.1-py3-none-any.whl"
else
  "$py" -m maintenance._release verify-companion-wheel \
    "$native_connect_wheel" --native
fi

# Detect the build backend BEFORE touching the version so a missing backend
# fails fast with a clear message instead of a pip traceback.
has_backend=0
if require_build_backend "$py" 2>/dev/null; then has_backend=1; fi

wheel_extra=()
case "$isolation" in
  never)
    if [ "$has_backend" -ne 1 ]; then
      echo "ERROR: build backend 'setuptools>=68' is not installed for $py." >&2
      echo "       Install it or run with SA_BUILD_ISOLATION=auto|always to let pip fetch it." >&2
      exit 1
    fi
    wheel_extra=(--no-build-isolation)
    ;;
  always)
    wheel_extra=()
    ;;
  auto)
    if [ "$has_backend" -eq 1 ]; then
      wheel_extra=(--no-build-isolation)
    else
      echo "note: setuptools not found in $py; using pip build isolation to fetch the declared build requirement."
    fi
    ;;
  *)
    echo "ERROR: unknown SA_BUILD_ISOLATION='$isolation' (expected auto|never|always)" >&2
    exit 1
    ;;
esac

version_backup="$(mktemp)"
cp "$here/maintenance/_version.py" "$version_backup"
build_root="$(mktemp -d "${TMPDIR:-/tmp}/system-analyzer-release.XXXXXX")"
built_ok=0
restore_on_failure() {
  if [ "$built_ok" -ne 1 ]; then
    cp "$version_backup" "$here/maintenance/_version.py"
    echo "note: restored maintenance/_version.py after failed build" >&2
  fi
  rm -f "$version_backup"
  rm -rf "$build_root"
}
trap restore_on_failure EXIT

echo "Preparing build inputs and version..."
"$py" -m maintenance._release prepare-build \
  --package-dir "$here" \
  --bump "$bump"

mkdir -p "$build_root/source" "$build_root/artifacts"
cp "$here/pyproject.toml" "$here/README.md" \
  "$here/main.py" "$here/algo.py" "$build_root/source/"
cp -a "$here/maintenance" "$build_root/source/"

app_version="$("$py" -c 'from maintenance._version import __version__; print(__version__)')"
app_wheel="$here/dist/system_analyzer-${app_version}-py3-none-any.whl"
wheel_is_current=0
if [ -f "$app_wheel" ] && "$py" -c 'import sys; from pathlib import Path; from maintenance._release import _collect_package_inputs, _read_manifest_from_wheel, diff_manifests; raise SystemExit(0 if not diff_manifests(_read_manifest_from_wheel(Path(sys.argv[1])), _collect_package_inputs(Path(sys.argv[2]))).has_changes else 1)' "$app_wheel" "$here"; then
  wheel_is_current=1
fi
if [ "$wheel_is_current" -eq 0 ]; then
  echo "Building universal system-analyzer wheel..."
  cp -a "$build_root/source" "$build_root/app-source"
  "$py" -m pip wheel "$build_root/app-source" --no-deps "${wheel_extra[@]}" \
    -w "$build_root/artifacts"
  app_wheel="$build_root/artifacts/system_analyzer-${app_version}-py3-none-any.whl"
fi
"$py" -m maintenance._release verify-wheel "$app_wheel"
if [ "$variant" = "universal" ]; then
  companion_wheel="$here/dist/expra_connect-2.2.1.1-py3-none-any.whl"
else
  companion_wheel="$native_connect_wheel"
fi
"$py" -m maintenance._release create-bundle \
  "$app_wheel" "$companion_wheel" \
  "$here/dist/$variant/$app_version" --variant "$variant"
if [ "$app_wheel" != "$here/dist/system_analyzer-${app_version}-py3-none-any.whl" ]; then
  cp "$app_wheel" "$here/dist/"
fi
if [ "$variant" = "native" ]; then
  cp "$native_connect_wheel" "$here/dist/"
fi
"$py" -m maintenance._release sync-artifacts --package-dir "$here"
built_ok=1
echo "Built $variant bundle: $here/dist/$variant/$app_version"
echo "Install from a bundle with pip --find-links and the bundled harness version."
