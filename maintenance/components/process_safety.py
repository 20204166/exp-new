"""Process safety decisions shared by the scanner and the action managers.

The protected-name list, the username matching rule, and the lenient
protected-PID snapshot each exist exactly once here. `ProcessSafetyPolicy`
answers whether a process may be managed without performing the action; the
lenient `protected_process_pids` view is for scan-time display, while the
policy itself fails closed.
"""

import os
from collections.abc import Callable, Iterable
from typing import Any

from .scan_support import PSUTIL_INSTALL_HINT

PROTECTED_PROCESS_NAMES: frozenset[str] = frozenset(
    {
        "csrss.exe",
        "controlcenter",
        "dwm.exe",
        "dock",
        "explorer.exe",
        "finder",
        "init",
        "kernel_task",
        "kthreadd",
        "launchd",
        "lsass.exe",
        "loginwindow",
        "registry",
        "services.exe",
        "smss.exe",
        "system",
        "systemd",
        "systemuiserver",
        "terminal",
        "wininit.exe",
        "winlogon.exe",
        "windowserver",
    }
)


def normalize_username(value: Any) -> str:
    """Normalise a local username, dropping Windows domain prefixes."""

    if not isinstance(value, str):
        return ""
    return value.replace("/", "\\").rsplit("\\", 1)[-1].casefold()


def is_protected_process_name(
    name: Any,
    protected_names: Iterable[str],
) -> bool:
    """Return whether a display name matches a protected-name rule.

    Shared by the scanner (lenient scan-time display), the action managers
    (name and executable-basename checks), and ``ProcessSafetyPolicy``
    (fail-closed management policy), so the one fold-and-match rule can
    never drift between the three consumers. Non-string names never match.
    """

    if not isinstance(name, str):
        return False
    folded = name.casefold()
    return folded in protected_names


def usernames_match(username: Any, current_user: Any) -> bool:
    """Compare local usernames while ignoring Windows domain prefixes."""

    normalized_username = normalize_username(username)
    normalized_current_user = normalize_username(current_user)
    return bool(normalized_username) and normalized_username == normalized_current_user


def protected_process_pids(psutil_module: Any) -> set[int]:
    """Return base protected PIDs plus this process's parent chain.

    This is the lenient scan-time view: missing or inaccessible process
    information only limits the snapshot; it does not fail the scan. The
    stricter fail-closed view lives in `ProcessSafetyPolicy`.
    """

    protected, _complete = protected_process_pid_snapshot(psutil_module)
    return protected


def protected_process_pid_snapshot(psutil_module: Any) -> tuple[set[int], bool]:
    """Return protected PIDs and whether the current ancestry was readable."""

    protected = {0, 1, os.getpid()}
    if psutil_module is None:
        return protected, False
    try:
        process = psutil_module.Process(os.getpid())
        protected.update(parent.pid for parent in process.parents())
    except Exception:  # noqa: BLE001 - action safety must fail closed.
        return protected, False
    return protected, True


class ProcessSafetyPolicy:
    """Answer whether a process may be managed without performing the action.

    The policy keeps the scanner and process-action protection rules together,
    but it never terminates or otherwise changes a process. Missing or
    inaccessible process information is treated as protected.
    """

    PROTECTED_NAMES: frozenset[str] = PROTECTED_PROCESS_NAMES

    def __init__(
        self,
        *,
        current_pid_loader: Callable[[], int] = os.getpid,
        process_loader: Callable[[int], Any] | None = None,
        protected_names: Iterable[str] | None = None,
    ) -> None:
        self._current_pid_loader = current_pid_loader
        self._process_loader = process_loader or self._default_process_loader
        names = self.PROTECTED_NAMES if protected_names is None else protected_names
        self._protected_names = frozenset(
            name.casefold() for name in names if isinstance(name, str) and name
        )

    def protected_pids(self) -> set[int]:
        """Return PID 0, PID 1, this process, and its known parent chain."""

        protected, _complete = self._protected_pid_snapshot()
        return protected

    def same_user(self, username: str, current_user: str) -> bool:
        """Compare local usernames while ignoring Windows domain prefixes."""

        return usernames_match(username, current_user)

    def can_manage(
        self,
        *,
        pid: int,
        name: str,
        username: str,
        current_user: str,
    ) -> bool:
        """Return whether a process with the supplied facts may be managed."""

        protected_pids, complete = self._protected_pid_snapshot()
        if not complete:
            return False
        return self._can_manage_with_pids(
            pid=pid,
            name=name,
            username=username,
            current_user=current_user,
            protected_pids=protected_pids,
        )

    def can_manage_process(self, *, pid: int, current_user: str) -> bool:
        """Load one process and apply the policy, failing closed on lookup errors."""

        protected_pids, complete = self._protected_pid_snapshot()
        if not complete or not isinstance(pid, int) or pid in protected_pids:
            return False

        try:
            process = self._process_loader(pid)
            name = process.name()
            username = process.username()
        except Exception:  # noqa: BLE001 - fail closed on any loader failure.
            # A disappearing or inaccessible process cannot be safely offered.
            return False

        return self._can_manage_with_pids(
            pid=pid,
            name=name,
            username=username,
            current_user=current_user,
            protected_pids=protected_pids,
        )

    def _protected_pid_snapshot(self) -> tuple[set[int], bool]:
        protected = {0, 1}
        try:
            current_pid = self._current_pid_loader()
            if not isinstance(current_pid, int) or current_pid < 0:
                return protected, False
            protected.add(current_pid)

            process = self._process_loader(current_pid)
            for parent in process.parents():
                parent_pid = getattr(parent, "pid", None)
                if not isinstance(parent_pid, int) or parent_pid < 0:
                    return protected, False
                protected.add(parent_pid)
        except Exception:  # noqa: BLE001 - fail closed on any ancestry failure.
            # Keep the base protected PIDs visible, but do not permit management
            # when the current process ancestry cannot be established.
            return protected, False
        return protected, True

    def _can_manage_with_pids(
        self,
        *,
        pid: int,
        name: str,
        username: str,
        current_user: str,
        protected_pids: set[int],
    ) -> bool:
        if not isinstance(pid, int) or pid < 0 or pid in protected_pids:
            return False
        if not isinstance(name, str) or not name:
            return False
        if is_protected_process_name(name, self._protected_names):
            return False
        return self.same_user(username, current_user)

    @staticmethod
    def _normalize_username(value: str) -> str:
        return normalize_username(value)

    @staticmethod
    def _default_process_loader(pid: int) -> Any:
        try:
            import psutil  # type: ignore[import-untyped]
        except ImportError as error:
            raise RuntimeError(PSUTIL_INSTALL_HINT) from error
        return psutil.Process(pid)
