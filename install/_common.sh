#!/usr/bin/env bash
# Shared helpers for the System Analyzer install/release scripts (Unix-like:
# Linux, macOS, and WSL). Windows users use the equivalent *.ps1 scripts.
# Safety: no sudo by default; explicit venv target (repo .venv by default,
# override SA_PYTHON); no network for the committed-wheel path; clear version
# display.
set -euo pipefail

# The repo root IS the package root (single project layout).
pkg_dir() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }

# Ordered candidate interpreters: named 3.10+ first (python.org/Homebrew
# installs commonly register python3.10..python3.13), then Homebrew and
# python.org framework locations even when not on PATH, then the system
# /usr/bin/python3, then bare python3/python.
python_candidates() {
  local list=() name
  for name in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$name" >/dev/null 2>&1; then list+=("$name"); fi
  done
  for path in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
              /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
              /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
              /usr/local/bin/python3.11 /usr/local/bin/python3.10; do
    [ -x "$path" ] && list+=("$path")
  done
  [ -x /usr/bin/python3 ] && list+=(/usr/bin/python3)
  list+=(python3 python)
  printf '%s\n' "${list[@]}"
}

# Return the first candidate that is Python 3.10+ (probed quietly).
first_python_ge_310() {
  local candidate
  while IFS= read -r candidate; do
    if require_python "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done < <(python_candidates)
  return 1
}

# Return whether the interpreter is inside a virtual environment. A venv
# interpreter must never be used for a --user install: pip refuses --user
# inside venvs ("User site-packages are not visible in this virtualenv"),
# which is exactly what happens when the repo .venv is active on PATH.
py_in_venv() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' >/dev/null 2>&1
}

# Return the first 3.10+ interpreter that is NOT inside a virtual environment.
# Used by install-user.sh so a per-user/system install always lands in a real
# interpreter regardless of whether a venv (e.g. the repo .venv) is active.
# When every 3.10+ candidate is a venv, falls back to that venv's base
# interpreter (sys._base_executable), which is the real system Python the venv
# was created from.
first_system_python_ge_310() {
  local candidate base
  base=""
  while IFS= read -r candidate; do
    if ! require_python "$candidate" >/dev/null 2>&1; then continue; fi
    if ! py_in_venv "$candidate"; then echo "$candidate"; return 0; fi
    if [ -z "$base" ]; then base="$candidate"; fi
  done < <(python_candidates)
  if [ -n "$base" ]; then
    local base_py
    base_py="$("$base" -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
    if [ -n "$base_py" ] && [ -x "$base_py" ] \
       && require_python "$base_py" >/dev/null 2>&1 && ! py_in_venv "$base_py"; then
      echo "$base_py"
      return 0
    fi
  fi
  return 1
}

resolve_python() {
  if [ -n "${SA_PYTHON:-}" ]; then echo "$SA_PYTHON"; return; fi
  local r found; r="$(pkg_dir)"
  if [ -x "$r/.venv/Scripts/python.exe" ]; then echo "$r/.venv/Scripts/python.exe"; return; fi
  if [ -x "$r/.venv/bin/python" ]; then echo "$r/.venv/bin/python"; return; fi
  if found="$(first_python_ge_310)"; then echo "$found"; return; fi
  echo "python"
}

wheel_path() {
  local version="${1:-}" dist; dist="$(pkg_dir)/dist"
  if [ -n "$version" ]; then
    local w="$dist/system_analyzer-$version-py3-none-any.whl"
    [ -f "$w" ] || { echo "wheel not found for $version: $w" >&2; exit 1; }
    echo "$w"; return
  fi
  ls -t "$dist"/system_analyzer-*.whl 2>/dev/null | head -1
}

show_version() { "$1" -m pip show system-analyzer 2>/dev/null | grep '^Version:' || true; }

wheel_version() {
  local name
  name="$(basename "$1")"
  name="${name#system_analyzer-}"
  echo "${name%-py3-none-any.whl}"
}

companion_wheel_path() {
  printf '%s\n' "$(pkg_dir)/dist/expra_connect-2.2.1.1-py3-none-any.whl"
}

validate_release_artifacts() {
  local py="$1" app_wheel="$2" root
  root="$(pkg_dir)"
  (cd "$root" && "$py" -m maintenance._release verify-release-artifacts \
    "$app_wheel" --dist-dir "$root/dist")
}

install_release_wheels() {
  local py="$1" app_wheel="$2" dist
  dist="$(pkg_dir)/dist"
  "$py" -m pip install --find-links "$dist" \
    "$app_wheel" "expra-connect==2.2.1.1"
}

clean_installed_package() {
  local py="$1" attempt
  shift
  local prefix=("$@")
  for attempt in 1 2 3; do
    if ! "${prefix[@]}" "$py" -m pip show system-analyzer >/dev/null 2>&1; then
      return 0
    fi
    echo "Removing existing system-analyzer distribution (pass $attempt)..."
    uninstall_pip "${prefix[@]}" "$py" -m pip uninstall -y system-analyzer
  done
  if "${prefix[@]}" "$py" -m pip show system-analyzer >/dev/null 2>&1; then
    echo "ERROR: could not remove every system-analyzer distribution from $py" >&2
    return 1
  fi
}

clean_user_installed_package() {
  local py="$1" attempt
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
    then
      return 0
    fi
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
  local err
  err="$(mktemp)"
  if "$@" --break-system-packages 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  if grep -qi "no such option" "$err"; then
    rm -f "$err"
    "$@"
    return $?
  fi
  cat "$err" >&2
  rm -f "$err"
  return 1
}

verify_installed() {
  local py="$1" expected="$2"
  (cd / && "$py" - "$expected" <<'PY'
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
}

# System Analyzer uses PEP 604 union types (e.g. `threading.Event | None`) so
# it requires Python 3.10+. Fail fast with a clear message instead of a
# confusing import-time TypeError on older interpreters (e.g. the macOS 3.9
# system Python).
require_python() {
  local py="$1" v major minor
  v="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 1
  major="${v%%.*}"; minor="${v#*.}"; minor="${minor%%.*}"
  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    echo "System Analyzer requires Python 3.10 or newer; this interpreter is" \
         "$v ($py)." >&2
    echo "Install a newer Python (e.g. 'brew install python@3.12' on macOS) or" \
         "use a 3.10+ virtual environment, then retry." >&2
    return 1
  fi
  return 0
}

# Return 0 when the interpreter has the declared setuptools build backend
# (>= 68). Used to choose the offline no-isolation path or pip build isolation.
require_build_backend() {
  local py="$1"
  "$py" -c 'import re, setuptools
m = re.match(r"^(\d+)\.(\d+)", setuptools.__version__)
raise SystemExit(0 if m and (int(m.group(1)), int(m.group(2))) >= (68, 0) else 1)'
}
