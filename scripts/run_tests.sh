#!/usr/bin/env bash
# Run the test suite isolated from the developer's real desktop session.
#
# tests/support/live_tk.py-guarded tests (e.g. tests/test_live_tk_resize.py,
# tests/test_page_wiring_consistency.py::LiveWindowWiringTests) build a real
# Tk root and open real windows on whatever DISPLAY is active. Run against a
# real, in-use desktop, those windows compete with the person actually using
# the machine for window-manager focus/redraw events, which can make an
# otherwise-passing test hang or flake nondeterministically -- this is not a
# code bug, it's contention for the display (see docs/AUDIT_FOLLOWUP_2026-09-13.md).
#
# This script runs the suite inside an isolated virtual X framebuffer (Xvfb)
# via xvfb-run when available, so live-Tk tests get a real display to render
# into without ever touching the developer's actual screen. Falls back to
# running against the current DISPLAY (with a warning) when xvfb-run isn't
# installed -- Xvfb is a Linux/X11 tool, so this fallback is expected on
# other platforms; live-Tk tests still skip cleanly there if no display
# exists at all (DISPLAY_AVAILABLE in tests/support/live_tk.py).
#
# Usage:
#   scripts/run_tests.sh                  # full suite (python -m unittest discover -s tests)
#   scripts/run_tests.sh tests.test_window -v   # forwarded straight to `python -m unittest`

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

if [ "$#" -eq 0 ]; then
  set -- discover -s tests -p "test_*.py"
fi

if command -v xvfb-run >/dev/null 2>&1; then
  export SA_TEST_XVFB=1
  exec xvfb-run --auto-servernum --server-args="-screen 0 1280x1024x24" \
    "$PYTHON" -m unittest "$@"
fi

echo "xvfb-run not found; running tests against the current display (\$DISPLAY=${DISPLAY:-unset})." >&2
echo "Live-Tk tests may open real windows and can flake if the machine is in interactive use." >&2
echo "Install xvfb for isolated runs, e.g.: sudo apt install xvfb" >&2
exec "$PYTHON" -m unittest "$@"
