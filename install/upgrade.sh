#!/usr/bin/env bash
# Upgrade to a given wheel, or build the latest wheel then install it.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
py="$(resolve_python)"; echo "Before:"; show_version "$py"
if [ -n "${1:-}" ]; then
  wheel="$(wheel_path "$1")"
else
  bash "$(dirname "${BASH_SOURCE[0]}")/build.sh"
  wheel="$(wheel_path)"
fi
echo "Upgrading to: $(basename "$wheel")"
# Verify the wheel before installing so a corrupt or partial artifact is never
# force-installed over a working copy.
name="$(basename "$wheel")"
version="${name#system_analyzer-}"
version="${version%-py3-none-any.whl}"
bash "$(dirname "${BASH_SOURCE[0]}")/verify.sh" "$version"
validate_release_artifacts "$py" "$wheel"
clean_installed_package "$py"
install_release_wheels "$py" "$wheel"
verify_installed "$py" "$version"
echo "After:"; show_version "$py"; echo "Rollback: rollback.sh <previous>"
