"""Shared process/psutil fakes for ``ProcessManager`` action tests.

The action-oriented fake models the exact ``psutil.Process``/``psutil``
surface ``maintenance.actions.ProcessManager`` consumes: identity fields,
create-time revalidation, children, termination/kill state, exception types
and ``wait_procs``. It is deliberately separate from the scanner-oriented and
safety-policy process fakes because those model different contracts.
"""

import getpass
import os
from typing import Any


class ActionProcess:
    """Configurable fake process for action-manager tests."""

    def __init__(
        self,
        pid: int,
        name: str,
        username: str,
        *,
        exe: str | None = None,
        create_time: float = 1000.0,
        children: list["ActionProcess"] | None = None,
        ignores_sigterm: bool = False,
        deny_name: bool = False,
        deny_username: bool = False,
    ) -> None:
        self.pid = pid
        self._name = name
        self._username = username
        self._exe = exe
        self._create_time = create_time
        self._children = children or []
        self.ignores_sigterm = ignores_sigterm
        self.deny_name = deny_name
        self.deny_username = deny_username
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        if self.deny_name:
            raise ActionPsutil.AccessDenied("permission denied")
        return self._name

    def username(self) -> str:
        if self.deny_username:
            raise ActionPsutil.AccessDenied("permission denied")
        return self._username

    def exe(self) -> str | None:
        if self._exe is None:
            raise OSError("no executable path")
        return self._exe

    def create_time(self) -> float:
        return self._create_time

    def parents(self) -> list[Any]:
        return []

    def children(self, recursive: bool = False) -> list["ActionProcess"]:
        del recursive
        return self._children

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class ActionPsutil:
    """Configurable fake ``psutil`` module for action-manager tests."""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, processes: dict[int, ActionProcess]) -> None:
        self.processes = dict(processes)
        self.processes.setdefault(
            os.getpid(), ActionProcess(os.getpid(), "python", getpass.getuser())
        )
        self.wait_procs_calls = 0

    def Process(self, pid: int) -> ActionProcess:
        if pid not in self.processes:
            raise ActionPsutil.NoSuchProcess(f"no process {pid}")
        return self.processes[pid]

    def wait_procs(
        self,
        processes: list[ActionProcess],
        timeout: int,
    ) -> tuple[list[ActionProcess], list[ActionProcess]]:
        del timeout
        self.wait_procs_calls += 1
        alive = [process for process in processes if process.ignores_sigterm]
        gone = [process for process in processes if not process.ignores_sigterm]
        return gone, alive
