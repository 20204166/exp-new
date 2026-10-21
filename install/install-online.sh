#!/usr/bin/env bash
# Standalone online installer. It can be downloaded and run from any folder:
#   curl -fsSL https://raw.githubusercontent.com/20204166/exp-new/main/install/install-online.sh | bash
#   curl -fsSL .../install-online.sh | bash -s -- --force-reinstall
set -euo pipefail

base="https://raw.githubusercontent.com/20204166/exp-new/main/dist"
connect_version="2.2.1.1"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

download() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else echo "curl or wget is required" >&2; exit 1; fi
}

# Return whether the interpreter is inside a virtual environment. A venv
# interpreter must never be used for a --user install: pip refuses --user
# inside venvs ("User site-packages are not visible in this virtualenv"),
# which is exactly what happens when a venv (e.g. the repo .venv) is active.
py_in_venv() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' >/dev/null 2>&1
}

force_reinstall=0
for arg in "$@"; do
  case "$arg" in
    --force-reinstall) force_reinstall=1 ;;
    -*) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

reinstall_flag=()
reinstall_flag=(--force-reinstall)

choose_python() {
  local candidate version major minor base
  base=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    version="$($candidate -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [ -n "$version" ] || continue
    major="${version%%.*}"; minor="${version#*.}"
    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
      if ! py_in_venv "$candidate"; then
        echo "$candidate"; return 0
      fi
      if [ -z "$base" ]; then base="$candidate"; fi
    fi
  done
  # Every 3.10+ candidate was a venv: fall back to its base interpreter (the
  # real system Python the venv was created from), so a per-user install works
  # even while a venv is active (the common macOS case where /usr/bin/python3
  # is still 3.9).
  if [ -n "$base" ]; then
    local base_py
    base_py="$("$base" -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
    if [ -n "$base_py" ] && [ -x "$base_py" ] && ! py_in_venv "$base_py"; then
      echo "$base_py"; return 0
    fi
  fi
  echo "System Analyzer requires Python 3.10 or newer. Deactivate any active" >&2
  echo "virtual environment (or run outside it) and retry." >&2
  exit 1
}

py="$(choose_python)"
download "$base/SHA256SUMS" "$tmp/SHA256SUMS"
app_entry="$(awk '$2 ~ /^system_analyzer-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl$/ { if (++count > 1) exit 1; print $1, $2 } END { if (count != 1) exit 1 }' "$tmp/SHA256SUMS")" \
  || { echo "Could not identify exactly one application wheel" >&2; exit 1; }
read -r app_expected wheel <<<"$app_entry"
companion_entries="$(awk -v prefix="expra_connect-${connect_version}-" 'index($2, prefix) == 1 && $2 ~ /\.whl$/ { print $1, $2 }' "$tmp/SHA256SUMS")"
[ -n "$companion_entries" ] || { echo "Could not identify compatible expra-connect wheels" >&2; exit 1; }
download "$base/$wheel" "$tmp/$wheel"

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else "$py" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; fi
}
[ "$app_expected" = "$(sha256_file "$tmp/$wheel")" ] || { echo "Application wheel checksum mismatch" >&2; exit 1; }
while read -r companion_expected companion; do
  download "$base/$companion" "$tmp/$companion"
  [ "$companion_expected" = "$(sha256_file "$tmp/$companion")" ] || {
    echo "expra-connect wheel checksum mismatch: $companion" >&2; exit 1;
  }
done <<<"$companion_entries"

install_pip() {
  local err; err="$(mktemp)"
  if "$@" --break-system-packages 2>"$err"; then rm -f "$err"; return 0; fi
  if grep -qi "no such option" "$err"; then rm -f "$err"; "$@"; return $?; fi
  cat "$err" >&2; rm -f "$err"; return 1
}

clean_installed_package() {
  local attempt
  for attempt in 1 2 3; do
    if ! "$py" -m pip show system-analyzer >/dev/null 2>&1; then return 0; fi
    echo "Removing existing system-analyzer distribution (pass $attempt)..."
    uninstall_pip "$py" -m pip uninstall -y system-analyzer
  done
  "$py" -m pip show system-analyzer >/dev/null 2>&1 && {
    echo "ERROR: could not remove every system-analyzer distribution" >&2
    exit 1
  }
}

clean_user_installed_package() {
  local attempt
  for attempt in 1 2 3; do
    if ! "$py" - <<'PY' >/dev/null 2>&1
import importlib.metadata as metadata
import pathlib
import site

try:
    root = pathlib.Path(metadata.distribution("system-analyzer").locate_file(""))
except metadata.PackageNotFoundError:
    raise SystemExit(1)
user_root = pathlib.Path(site.getusersitepackages())
raise SystemExit(0 if root == user_root or user_root in root.parents else 1)
PY
    then return 0; fi
    echo "Removing existing user system-analyzer distribution (pass $attempt)..."
    uninstall_pip "$py" -m pip uninstall -y system-analyzer
  done
  if "$py" - <<'PY' >/dev/null 2>&1
import importlib.metadata as metadata
import pathlib
import site

try:
    root = pathlib.Path(metadata.distribution("system-analyzer").locate_file(""))
except metadata.PackageNotFoundError:
    raise SystemExit(1)
user_root = pathlib.Path(site.getusersitepackages())
raise SystemExit(0 if root == user_root or user_root in root.parents else 1)
PY
  then
    echo "ERROR: could not remove every user system-analyzer distribution" >&2
    return 1
  fi
}

uninstall_pip() {
  local err; err="$(mktemp)"
  if "$@" --break-system-packages 2>"$err"; then rm -f "$err"; return 0; fi
  if grep -qi "no such option" "$err"; then rm -f "$err"; "$@"; return $?; fi
  cat "$err" >&2; rm -f "$err"; return 1
}

clean_user_installed_package
install_pip "$py" -m pip install --user --find-links "$tmp" \
  "${reinstall_flag[@]}" "$tmp/$wheel" "expra-connect==$connect_version"
expected_version="${wheel#system_analyzer-}"
expected_version="${expected_version%-py3-none-any.whl}"
(cd / && "$py" - "$expected_version" <<'PY'
import importlib.metadata as metadata
import sys

expected = sys.argv[1]
actual = metadata.version("system-analyzer")
if actual != expected:
    raise SystemExit(f"installed version {actual} does not match wheel version {expected}")
import maintenance
import main
print(f"Installed system-analyzer {actual}")
print(f"  maintenance: {maintenance.__file__}")
print(f"  main: {main.__file__}")
PY
)
bin_dir="$($py -c 'import sysconfig;print(sysconfig.get_path("scripts", scheme="posix_user"))')"
launcher="$bin_dir/system-analyzer"
[ -x "$launcher" ] || { echo "Install completed but launcher was not found at $launcher" >&2; exit 1; }
echo "Installed $wheel. Console scripts: $bin_dir"
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
  rc_file="$HOME/.profile"; case "$(basename "${SHELL:-bash}")" in
    zsh) rc_file="$HOME/.zshrc" ;; bash) rc_file="$HOME/.bashrc" ;; esac
  echo "Add it to PATH: echo 'export PATH=\"$bin_dir:\$PATH\"' >> $rc_file"
  echo "Then run: source $rc_file"
fi
if echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
  echo "Run from anywhere: system-analyzer"
else
  echo "Launch directly now: $launcher"
fi
