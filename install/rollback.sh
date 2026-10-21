#!/usr/bin/env bash
# Roll back to a previous wheel kept in dist/. Usage: rollback.sh <version>
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
[ -n "${1:-}" ] || { echo "usage: rollback.sh <version>" >&2; exit 2; }
py="$(resolve_python)"; wheel="$(wheel_path "$1")"
echo "Rolling back to: $(basename "$wheel")"
validate_release_artifacts "$py" "$wheel"
clean_installed_package "$py"
install_release_wheels "$py" "$wheel"
verify_installed "$py" "$(wheel_version "$wheel")"
echo "Restored:"; show_version "$py"
