#!/bin/sh
# Install a Linux desktop/menu entry for System Analyzer into the CURRENT
# user's applications directory. Uses the user's own XDG_DATA_HOME / $HOME,
# so no one user's home directory is hard-coded anywhere.
#
# Usage: ./install-desktop.sh
# Remove: rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/system-analyzer.desktop"
set -e

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SOURCE_DESKTOP="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/packaging/system-analyzer.desktop"

if [ ! -f "$SOURCE_DESKTOP" ]; then
    echo "error: desktop entry template not found: $SOURCE_DESKTOP" >&2
    exit 1
fi

mkdir -p "$APPS_DIR"
cp "$SOURCE_DESKTOP" "$APPS_DIR/system-analyzer.desktop"
echo "Installed menu entry: $APPS_DIR/system-analyzer.desktop"
echo "The 'system-analyzer' command must be on PATH (run: pip install -e .)"