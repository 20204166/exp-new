#!/usr/bin/env bash
# Uninstall System Analyzer from the target venv.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
py="$(resolve_python)"; echo "Before:"; show_version "$py"
clean_installed_package "$py"
echo "Uninstalled. If a stale __editable__.*.pth lingers, remove it from site-packages."
