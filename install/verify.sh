#!/usr/bin/env bash
# Verify a built wheel before installing it: no forbidden content, expected
# members, entry points present, and (when present) the SHA256SUMS entry.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
py="$(resolve_python)"; wheel="$(wheel_path "${1:-}")"
require_python "$py" || exit 1
echo "Verifying: $wheel"
validate_release_artifacts "$py" "$wheel"
