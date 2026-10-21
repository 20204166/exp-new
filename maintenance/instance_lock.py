"""Cross-platform OS-held single-instance lock.

The lock is scoped to the profile state directory so different profiles and
different users may run simultaneously.  The OS releases the lock
automatically when the process terminates, including crashes and
force-kills, so no stale-lock recovery is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path


class InstanceLock:
    """OS-held exclusive lock on a state directory.

    Keep this object alive for the duration of the runtime.  The lock is
    released when release() is called or the process terminates (including
    unclean termination and crashes).
    """

    def __init__(self, _file: object) -> None:
        self._file = _file

    def release(self) -> None:
        """Release the lock; safe to call more than once."""
        f = self._file
        if f is not None:
            self._file = None
            try:
                f.close()  # type: ignore[attr-defined]
            except OSError:
                pass


def acquire(lock_path: Path) -> InstanceLock | None:
    """Try to acquire an exclusive OS-held lock on lock_path.

    Returns an InstanceLock that MUST be kept alive for the lock duration.
    Returns None if another process already owns the profile, or if the
    lock file cannot be created.

    The lock is automatically released when the process terminates
    (including unclean termination and crashes).
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "ab")  # noqa: SIM115 — must stay open past this scope
    except OSError:
        return None

    try:
        if sys.platform == "win32":
            import msvcrt  # type: ignore[import]

            lock_file.write(b"\x00")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None

    return InstanceLock(lock_file)
