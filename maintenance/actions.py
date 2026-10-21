import getpass
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from maintenance.components import (
    PROTECTED_PROCESS_NAMES,
    require_psutil,
    usernames_match,
)
from maintenance.components.process_safety import (
    is_protected_process_name,
    protected_process_pid_snapshot,
)
from maintenance.models import FileActionResult, ProcessActionResult
from maintenance.nodes import ProcessActionKind, ProcessTerminationRequest
from maintenance.scanner import SystemScanner

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    from send2trash import send2trash  # type: ignore[import-untyped]
except ImportError:
    send2trash = None


class ProcessManager:
    """Quit selected user processes with explicit safety checks."""

    PROTECTED_NAMES: frozenset[str] = PROTECTED_PROCESS_NAMES

    @staticmethod
    def _require_psutil() -> Any:
        return require_psutil(psutil)

    def request_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> ProcessActionResult:
        return self._run_process_action(
            pids,
            lambda process: process.terminate(),
            expected_create_times=expected_create_times,
        )

    def terminate(self, request: ProcessTerminationRequest) -> ProcessActionResult:
        """Execute only a typed, allowlisted request through local safety checks."""

        pids = [process.pid for process in request.processes]
        expected_create_times = {
            process.pid: process.create_time
            for process in request.processes
            if process.create_time is not None
        }
        if request.action is ProcessActionKind.REQUEST_QUIT:
            return self.request_quit(pids, expected_create_times)
        if request.action is ProcessActionKind.FORCE_QUIT:
            return self.force_quit(pids, expected_create_times)
        raise ValueError("unsupported process action")

    def force_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> ProcessActionResult:
        return self._run_process_action(
            pids,
            lambda process: process.kill(),
            alive_message="did not stop.",
            expected_create_times=expected_create_times,
            include_children=True,
        )

    def _run_process_action(
        self,
        pids: list[int],
        action: Callable[[Any], None],
        alive_message: str | None = None,
        expected_create_times: dict[int, float] | None = None,
        include_children: bool = False,
    ) -> ProcessActionResult:
        psutil_module = self._require_psutil()
        processes, errors = self._allowed_processes(
            pids,
            psutil_module,
            expected_create_times,
        )
        if not processes:
            return self._build_process_action_result(pids, (), (), errors)

        process_errors = (
            psutil_module.NoSuchProcess,
            psutil_module.AccessDenied,
        )
        target_create_times = dict(expected_create_times or {})
        targets: list[Any] = []

        if include_children:
            current_user = getpass.getuser()
            protected_pids = self._protected_pids(psutil_module)
            if protected_pids is None:
                errors.append("Process ancestry unavailable; refusing process actions.")
                return self._build_process_action_result(pids, (), (), errors)
            for process in processes:
                try:
                    children = process.children(recursive=True)
                except process_errors:
                    children = []
                for child in children:
                    if self._is_allowed_target(child, current_user, protected_pids):
                        try:
                            target_create_times[child.pid] = float(child.create_time())
                        except process_errors as error:
                            errors.append(f"PID {child.pid}: {error}")
                        else:
                            targets.append(child)
                    else:
                        errors.append(f"PID {child.pid} is protected.")
                targets.append(process)
        else:
            targets.extend(processes)

        for target in targets:
            try:
                expected_create_time = target_create_times.get(target.pid)
                if expected_create_time is not None and float(
                    target.create_time()
                ) != float(expected_create_time):
                    errors.append(f"PID {target.pid} changed since it was scanned.")
                    continue
                action(target)
            except process_errors as error:
                errors.append(f"PID {target.pid}: {error}")

        gone, alive = psutil_module.wait_procs(processes, timeout=3)
        if alive_message is not None:
            errors.extend(f"PID {process.pid} {alive_message}" for process in alive)
        return self._build_process_action_result(pids, gone, alive, errors)

    @staticmethod
    def _build_process_action_result(
        pids: list[int],
        gone: Iterable[Any],
        alive: Iterable[Any],
        errors: list[str],
    ) -> ProcessActionResult:
        return ProcessActionResult(
            requested=len(pids),
            stopped=tuple(process.pid for process in gone),
            force_required=tuple(process.pid for process in alive),
            errors=tuple(errors),
        )

    def _allowed_processes(
        self,
        pids: list[int],
        psutil_module: Any,
        expected_create_times: dict[int, float] | None = None,
    ) -> tuple[list[Any], list[str]]:
        current_user = getpass.getuser()
        protected_pids = self._protected_pids(psutil_module)
        if protected_pids is None:
            return [], ["Process ancestry unavailable; refusing process actions."]
        process_factory = psutil_module.Process
        same_user = self._same_user
        processes: list[Any] = []
        errors: list[str] = []

        for pid in dict.fromkeys(pids):
            if pid in protected_pids:
                errors.append(f"PID {pid} is protected.")
                continue

            try:
                process = process_factory(pid)
                name = process.name()
                username = process.username()
                if expected_create_times and pid in expected_create_times:
                    actual_create_time = process.create_time()
                    if float(actual_create_time) != float(expected_create_times[pid]):
                        errors.append(f"PID {pid} changed since it was scanned.")
                        continue
            except (psutil_module.NoSuchProcess, psutil_module.AccessDenied) as error:
                errors.append(f"PID {pid}: {error}")
                continue

            if not same_user(username, current_user) or self._is_protected_process(
                name, process
            ):
                errors.append(f"{name} (PID {pid}) is protected.")
                continue
            processes.append(process)

        return processes, errors

    def _is_allowed_target(
        self,
        process: Any,
        current_user: str,
        protected_pids: set[int],
    ) -> bool:
        """Return whether a force-quit child may be acted on.

        Recursively discovered children must pass the same safety checks as a
        directly requested process: not a protected PID, owned by the current
        user, and not a protected name/executable. A child that cannot be read
        is treated as protected (fail closed) so a force quit never kills an
        unsafe descendant merely because its parent was approved.
        """

        if process.pid in protected_pids:
            return False
        try:
            name = process.name()
            username = process.username()
        except Exception:  # noqa: BLE001 - unreadable children are protected (fail closed).
            return False
        if not self._same_user(username, current_user):
            return False
        return not self._is_protected_process(name, process)

    @staticmethod
    def _is_protected_process(name: str, process: Any) -> bool:
        """Return whether a process is protected by name or executable path.

        The executable path is a more reliable identifier than the display
        name on some platforms, so it is consulted as a second, fail-closed
        check whenever psutil can read it.
        """

        if is_protected_process_name(name, ProcessManager.PROTECTED_NAMES):
            return True
        try:
            executable = process.exe()
        except Exception:  # noqa: BLE001 - executable path is best-effort.
            return False
        if executable:
            return is_protected_process_name(
                Path(executable).name,
                ProcessManager.PROTECTED_NAMES,
            )
        return False

    @staticmethod
    def _protected_pids(psutil_module: Any) -> set[int] | None:
        protected, complete = protected_process_pid_snapshot(psutil_module)
        return protected if complete else None

    @staticmethod
    def _same_user(username: str, current_user: str) -> bool:
        return usernames_match(username, current_user)


class FileManager:
    """Move selected files to Trash after validating their location."""

    def __init__(self, allowed_root: Path | None = None) -> None:
        self.allowed_root = (
            allowed_root or SystemScanner._default_downloads_path()
        ).resolve()

    @staticmethod
    def _require_send2trash() -> Any:
        if send2trash is None:
            raise RuntimeError(
                "send2trash is not installed. Run: python -m pip install send2trash"
            )
        return send2trash

    def move_to_trash(self, paths: list[Path]) -> FileActionResult:
        send2trash_fn = self._require_send2trash()
        allowed_root = self.allowed_root
        root_is_dir = allowed_root.is_dir()
        moved: list[Path] = []
        errors: list[str] = []

        for original_path in dict.fromkeys(paths):
            path = original_path.expanduser()

            if path.is_symlink():
                errors.append(f"{path}: symbolic links are not allowed.")
                continue

            try:
                resolved_path = path.resolve(strict=True)
                initial_stat = resolved_path.stat()
            except (OSError, RuntimeError) as error:
                errors.append(f"{path}: {error}")
                continue

            if (
                not root_is_dir
                or not resolved_path.is_relative_to(allowed_root)
                or not resolved_path.is_file()
            ):
                errors.append(f"{path}: not an allowed Downloads file.")
                continue

            try:
                current_stat = resolved_path.stat()
                if (
                    current_stat.st_dev != initial_stat.st_dev
                    or current_stat.st_ino != initial_stat.st_ino
                    or not resolved_path.is_file()
                ):
                    errors.append(f"{path}: file changed during validation.")
                    continue
                send2trash_fn(str(resolved_path))
            except OSError as error:
                errors.append(f"{path}: {error}")
            else:
                moved.append(resolved_path)

        return FileActionResult(
            requested=len(paths),
            moved=tuple(moved),
            errors=tuple(errors),
        )
