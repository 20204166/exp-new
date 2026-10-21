#!/usr/bin/env bash
# Install the stable wheel into the target venv (explicit, non-editable).
# REPLACES any existing install. Cut over only when intended.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
py="$(resolve_python)"; wheel="$(wheel_path "${1:-}")"
echo "Target python: $py"; echo "Before:"; show_version "$py"
validate_release_artifacts "$py" "$wheel"
clean_installed_package "$py"
install_release_wheels "$py" "$wheel"
verify_installed "$py" "$(wheel_version "$wheel")"
echo "After:"; show_version "$py"; echo "Verify: system-analyzer-snapshot --help"
