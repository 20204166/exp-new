"""Subprocess helper for instance-lock tests.

Invoked as:
    python lock_subprocess_helper.py <lock_path>

Prints "ACQUIRED" and blocks on stdin when the lock is obtained.
Prints "BUSY" and exits 1 when another process holds the lock.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from maintenance import instance_lock

if len(sys.argv) != 2:
    print("usage: lock_subprocess_helper.py <lock_path>", file=sys.stderr)
    sys.exit(2)

lock = instance_lock.acquire(Path(sys.argv[1]))
if lock is None:
    print("BUSY", flush=True)
    sys.exit(1)

print("ACQUIRED", flush=True)
try:
    sys.stdin.read()
except OSError:
    pass
lock.release()
sys.exit(0)
