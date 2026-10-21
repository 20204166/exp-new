"""Tests for the cross-platform single-instance lock.

Evidence labels:
  UNIT / EMULATED WINDOWS BRANCH — WindowsLockBranchTests
    (sys.platform patched to "win32", msvcrt injected as a stub)
  REAL-SOCKET ON LINUX — InstanceLockSubprocessTests
    (actual OS file locks across two real processes)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from maintenance import instance_lock
from maintenance.instance_lock import InstanceLock

_HELPER = Path(__file__).parent / "support" / "lock_subprocess_helper.py"


class InstanceLockAcquireTests(unittest.TestCase):
    """Fresh-lock and error-path unit tests; no subprocesses required."""

    def test_acquire_fresh_lock_returns_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lock = instance_lock.acquire(Path(d) / "test.lock")
            self.assertIsNotNone(lock)
            self.assertIsInstance(lock, InstanceLock)
            if lock:
                lock.release()

    def test_release_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lock = instance_lock.acquire(Path(d) / "test.lock")
            self.assertIsNotNone(lock)
            if lock:
                lock.release()
                lock.release()  # must not raise

    def test_acquire_creates_parent_directories_if_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            nested = Path(d) / "a" / "b" / "c" / "test.lock"
            lock = instance_lock.acquire(nested)
            self.assertIsNotNone(lock)
            if lock:
                lock.release()

    def test_acquire_returns_none_when_parent_cannot_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_bytes(b"x")  # regular file where a directory is expected
            result = instance_lock.acquire(blocker / "test.lock")
        self.assertIsNone(result)

    @unittest.skipIf(sys.platform == "win32", "flock is POSIX-only")
    def test_acquire_returns_none_when_flock_raises(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch("fcntl.flock", side_effect=BlockingIOError("already locked")),
        ):
            result = instance_lock.acquire(Path(d) / "test.lock")
        self.assertIsNone(result)


class InstanceLockSubprocessTests(unittest.TestCase):
    """Real process-boundary tests.  REAL-SOCKET ON LINUX — actual OS file locks.

    These tests spawn real child processes so the OS-level lock semantics
    (cross-PID exclusion, crash recovery) are exercised.
    """

    def test_second_process_blocked_while_first_holds_lock(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / "test.lock"

            proc_a = subprocess.Popen(
                [sys.executable, str(_HELPER), str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            status_a = (proc_a.stdout.readline() if proc_a.stdout else "").strip()
            self.assertEqual(status_a, "ACQUIRED", "Process A must acquire the lock")

            # Same process (this test) cannot acquire while A holds.
            result_b = instance_lock.acquire(lock_path)
            self.assertIsNone(result_b, "Same-profile second acquire must fail")

            # Terminate A → OS releases the lock.
            if proc_a.stdin:
                proc_a.stdin.close()
            proc_a.wait(timeout=5)

            # This process must now succeed.
            result_c = instance_lock.acquire(lock_path)
            self.assertIsNotNone(result_c, "Acquire after owner exits must succeed")
            if result_c:
                result_c.release()

    def test_crash_recovery_second_launch_succeeds(self) -> None:
        """SIGKILL the lock holder; the next acquire must succeed (OS releases on crash)."""
        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / "test.lock"

            proc = subprocess.Popen(
                [sys.executable, str(_HELPER), str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            status = (proc.stdout.readline() if proc.stdout else "").strip()
            self.assertEqual(status, "ACQUIRED")

            proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
            proc.wait(timeout=5)

            lock = instance_lock.acquire(lock_path)
            self.assertIsNotNone(
                lock, "Lock must be acquirable after crash-killed owner"
            )
            if lock:
                lock.release()


class WindowsLockBranchTests(unittest.TestCase):
    """UNIT / EMULATED WINDOWS BRANCH: msvcrt code path exercised on Linux.

    sys.platform is patched to "win32" and a stub msvcrt module is injected
    into sys.modules.  The real fcntl path is not taken in these tests.
    """

    _LK_NBLCK = 2  # msvcrt.LK_NBLCK constant value

    def _msvcrt_stub(self, locking_raises: BaseException | None = None) -> MagicMock:
        stub = MagicMock()
        stub.LK_NBLCK = self._LK_NBLCK
        if locking_raises is not None:
            stub.locking.side_effect = locking_raises
        return stub

    def test_windows_branch_calls_msvcrt_locking(self) -> None:
        stub = self._msvcrt_stub()
        with (
            tempfile.TemporaryDirectory() as d,
            patch("sys.platform", "win32"),
            patch.dict("sys.modules", {"msvcrt": stub}),
        ):
            lock = instance_lock.acquire(Path(d) / "test.lock")

        self.assertIsNotNone(lock)
        stub.locking.assert_called_once()
        _, args, _ = stub.locking.mock_calls[0]
        self.assertEqual(args[1], self._LK_NBLCK)  # mode = LK_NBLCK
        self.assertEqual(args[2], 1)  # nbytes = 1
        if lock:
            lock.release()

    def test_windows_branch_returns_none_when_msvcrt_locking_raises(self) -> None:
        stub = self._msvcrt_stub(locking_raises=OSError("already locked"))
        with (
            tempfile.TemporaryDirectory() as d,
            patch("sys.platform", "win32"),
            patch.dict("sys.modules", {"msvcrt": stub}),
        ):
            result = instance_lock.acquire(Path(d) / "test.lock")

        self.assertIsNone(result)

    def test_windows_branch_does_not_call_fcntl_flock(self) -> None:
        stub = self._msvcrt_stub()
        fcntl_stub = MagicMock()
        with (
            tempfile.TemporaryDirectory() as d,
            patch("sys.platform", "win32"),
            patch.dict("sys.modules", {"msvcrt": stub, "fcntl": fcntl_stub}),
        ):
            lock = instance_lock.acquire(Path(d) / "test.lock")

        fcntl_stub.flock.assert_not_called()
        if lock:
            lock.release()


if __name__ == "__main__":
    unittest.main()
