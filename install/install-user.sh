#!/usr/bin/env bash
# Install System Analyzer into the CURRENT USER's Python WITHOUT a virtual
# environment, so `system-analyzer` runs directly from any directory.
#
# You do NOT need to be inside the repo to run this: give the full path, e.g.
#   /path/to/exp/install/install-user.sh
# It locates the repo (and the committed wheel in dist/) from its own location.
#
#   install-user.sh              # per-user install  -> ~/.local/bin
#   install-user.sh --system     # machine-wide      -> /usr/local/bin (uses sudo)
#   install-user.sh --force-reinstall  # accepted for compatibility; reinstall is automatic
#   install-user.sh 1.2.2.0      # install a specific wheel from dist/
#
#   - uses the system interpreter (SA_SYSTEM_PYTHON to override; default
#     /usr/bin/python3 then python3)
#   - installs the built wheel from dist/ together with its dependencies
#   - handles PEP 668 externally-managed systems automatically
#     (reacts to the actual pip error, so it works on any distro)
#   - after install, run `system-analyzer` from anywhere (it is on PATH)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$here/install/_common.sh"

mode="user"
version=""
force_reinstall=0
for arg in "$@"; do
  case "$arg" in
    --system) mode="system" ;;
  --force-reinstall) force_reinstall=1 ;;
    -*) echo "Unknown option: $arg" >&2; exit 1 ;;
    *) version="$arg" ;;
  esac
done

if [ -n "${SA_SYSTEM_PYTHON:-}" ]; then py="$SA_SYSTEM_PYTHON"
elif found="$(first_system_python_ge_310)"; then py="$found"
else py=/usr/bin/python3; fi

require_python "$py" || exit 1

if [ "$mode" = "user" ] && py_in_venv "$py"; then
  echo "ERROR: '$py' is inside a virtual environment, so a per-user install is" >&2
  echo "       impossible (pip disables '--user' inside venvs). Deactivate the" >&2
  echo "       venv, or set SA_SYSTEM_PYTHON to a system interpreter." >&2
  exit 1
fi

wheel="$(wheel_path "$version")"
[ -n "$wheel" ] && [ -f "$wheel" ] || { echo "no wheel found; run install/build.sh first" >&2; exit 1; }
validate_release_artifacts "$py" "$wheel"

echo "System interpreter: $py ($("$py" --version 2>&1))"
echo "Verifying wheel: $(basename "$wheel")"
(cd "$here" && "$py" -m maintenance._release verify-wheel "$wheel")

# Try with --break-system-packages first (the flag is a no-op where PEP 668
# does not apply). If this pip predates the flag, retry without it.
install_pip() {
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

if [ "$mode" = "system" ]; then
  command -v sudo >/dev/null 2>&1 || { echo "sudo is required for --system" >&2; exit 1; }
  echo "Installing machine-wide (system Python, no venv)..."
  # A user-site copy shadows the machine-wide package on the normal Python
  # import path, so remove it before verifying the system installation.
  clean_user_installed_package "$py"
  clean_installed_package "$py" sudo -H
  # The existing application is removed above. Do not force-reinstall
  # dependencies: distro-managed packages such as Debian's psutil may satisfy
  # the requirement but have no RECORD file for pip to uninstall.
  install_pip sudo -H "$py" -m pip install --find-links "$here/dist" \
    "$wheel" "expra-connect==2.2.1.1"
  verify_installed "$py" "$(wheel_version "$wheel")"
  launcher="$(command -v system-analyzer || true)"
  if [ -n "$launcher" ]; then
    bin_dir="$(dirname "$launcher")"
  else
    bin_dir="$($py -c 'import sysconfig;print(sysconfig.get_path("scripts", scheme="posix_prefix"))' 2>/dev/null || true)"
    [ -n "$bin_dir" ] || bin_dir="/usr/local/bin"
    launcher="$bin_dir/system-analyzer"
  fi
  [ -x "$launcher" ] || { echo "Install completed but launcher was not found at $launcher" >&2; exit 1; }
  echo "Installed. Console scripts are in: $bin_dir"
  user_bin_dir="$($py -c 'import sysconfig;print(sysconfig.get_path("scripts", scheme="posix_user"))' 2>/dev/null || true)"
  case ":${PATH}:" in
    *":${user_bin_dir}:"*)
      # Keep an already-cached user-bin command path valid after replacing a
      # per-user install with the machine-wide one.
      if [ -n "$user_bin_dir" ] && [ "$user_bin_dir" != "$bin_dir" ]; then
        mkdir -p "$user_bin_dir"
        ln -sfn "$launcher" "$user_bin_dir/system-analyzer"
      fi
      ;;
  esac
  if echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
    echo "Launcher found on PATH. Run: system-analyzer"
  else
    login_shell="$(basename "${SHELL:-bash}")"
    case "$login_shell" in
      zsh) rc_file="$HOME/.zshrc" ;;
      bash) rc_file="$HOME/.bashrc" ;;
      *) rc_file="$HOME/.profile" ;;
    esac
    echo "Launcher exists at: $launcher"
    echo "Add it to PATH (shell: $login_shell):"
    echo "  echo 'export PATH=\"$bin_dir:\$PATH\"' >> $rc_file"
    echo "  source $rc_file"
    echo "Or launch directly now: $launcher"
  fi
else
  echo "Installing into the user environment (no venv)..."
  clean_user_installed_package "$py"
  install_pip "$py" -m pip install --user --find-links "$here/dist" \
    "$wheel" "expra-connect==2.2.1.1"
  verify_installed "$py" "$(wheel_version "$wheel")"
  bin_dir="$("$py" -c 'import sysconfig;print(sysconfig.get_path("scripts", scheme="posix_user"))' 2>/dev/null || echo "$HOME/.local/bin")"
  echo "Installed. Console scripts are in: $bin_dir"
  if echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
    echo "That directory is already on PATH: run 'system-analyzer'"
  else
    # Match the user's actual login shell (zsh ignores ~/.bashrc; macOS uses
    # ~/.zshrc by default), so the instruction actually works.
    login_shell="$(basename "${SHELL:-bash}")"
    case "$login_shell" in
      zsh) rc_file="$HOME/.zshrc" ;;
      bash) rc_file="$HOME/.bashrc" ;;
      *) rc_file="$HOME/.profile" ;;
    esac
    echo "Add it to PATH once (shell: $login_shell), e.g.:"
    echo "  echo 'export PATH=\"$bin_dir:\$PATH\"' >> $rc_file"
    echo "  source $rc_file    # or open a new terminal"
    echo "Then run: system-analyzer"
  fi
fi
echo "Verify: system-analyzer-snapshot --help"
if [ "$(uname)" = "Linux" ]; then
  echo ""
  echo "Firewall note: to allow inbound LAN peer connections on this machine, run:"
  echo "  sudo cp \"$here/packaging/system-analyzer\" /etc/ufw/applications.d/"
  echo "  sudo ufw app update 'System Analyzer'"
  echo "  sudo ufw allow 'System Analyzer'"
  echo "This opens TCP port 27321 only."
  echo "To remove: sudo ufw delete allow 'System Analyzer'"
fi
