"""Scanner-specific responsibility support.

The mixin preserves SystemScanner method names while keeping dependency
lookups on maintenance.scanner for existing monkeypatch seams.
"""

# The mixin is intentionally completed by SystemScanner; the concrete state
# and class constants live on that owner rather than being duplicated here.
# pyright: reportAttributeAccessIssue=false, reportUndefinedVariable=false, reportGeneralTypeIssues=false, reportOptionalMemberAccess=false, reportArgumentType=false
# mypy: disable-error-code="attr-defined,misc,has-type,assignment,valid-type,name-defined"

from __future__ import annotations

import threading
from typing import Any

from maintenance.components import ScanCancelled, protected_process_pids
from maintenance.components.process_safety import is_protected_process_name
from maintenance.components.scan_support import call_legacy_compatible
from maintenance.models import ProcessActionState, ProcessCandidate

from ._compat import scanner_module


class ProcessesMixin:
    """Own one cohesive scanner implementation responsibility."""

    def scan_processes(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[ProcessCandidate]:
        self._check_cancelled(cancel_event)
        psutil_module = self._require_psutil()
        current_user = scanner_module.getpass.getuser()
        protected_pids = self._protected_pids()
        processes = list(self._process_iter(attrs=["pid", "create_time"]))
        process_creation_times: dict[int, float] = {}
        for process in processes:
            self._check_cancelled(cancel_event)
            try:
                create_time = process.info.get("create_time")
                if isinstance(create_time, (int, float)):
                    process_creation_times[process.pid] = float(create_time)
            except (AttributeError, KeyError, TypeError):
                continue

        for process in processes:
            self._check_cancelled(cancel_event)
            try:
                process.cpu_percent(None)
            except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
                continue

        if cancel_event is not None:
            if cancel_event.wait(self.PROCESS_SAMPLE_SECONDS):
                raise ScanCancelled("Process scan cancelled")
        else:
            scanner_module.time.sleep(self.PROCESS_SAMPLE_SECONDS)

        candidates: list[ProcessCandidate] = []

        for process in processes:
            self._check_cancelled(cancel_event)
            try:
                info = process.as_dict(
                    attrs=[
                        "pid",
                        "name",
                        "username",
                        "memory_info",
                        "memory_percent",
                        "create_time",
                    ]
                )
                cpu_percent = process.cpu_percent(None)
                memory_info = info["memory_info"]
                memory_bytes = memory_info.rss
                memory_percent = info["memory_percent"]
                expected_create_time = process_creation_times.get(info["pid"])
                current_create_time = info.get("create_time")
                if expected_create_time is None or not isinstance(
                    current_create_time,
                    (int, float),
                ):
                    continue
                if float(current_create_time) != expected_create_time:
                    continue
                self._check_cancelled(cancel_event)
            except (
                psutil_module.NoSuchProcess,
                psutil_module.AccessDenied,
                AttributeError,
                KeyError,
                TypeError,
            ):
                continue

            if (
                memory_bytes < self.PROCESS_MIN_BYTES
                and cpu_percent < self.PROCESS_ACTIVITY_MIN_CPU_PERCENT
            ):
                continue

            name = info["name"] or f"Process {info['pid']}"
            username = info["username"] or "Unknown"
            protected_name = is_protected_process_name(
                name, self.PROTECTED_PROCESS_NAMES
            )
            action_allowed = (
                self._same_user(username, current_user)
                and info["pid"] not in protected_pids
                and not protected_name
            )
            activity = self._activity_label(cpu_percent)

            candidates.append(
                ProcessCandidate(
                    pid=info["pid"],
                    name=name,
                    memory_bytes=memory_bytes,
                    memory_percent=memory_percent,
                    cpu_percent=cpu_percent,
                    activity=activity,
                    username=username,
                    action_allowed=action_allowed,
                    create_time=float(current_create_time),
                    protected=not action_allowed,
                    action_state=(
                        ProcessActionState.ALLOWED
                        if action_allowed
                        else ProcessActionState.PROTECTED
                    ),
                )
            )

        return candidates

    @staticmethod
    def _activity_label(cpu_percent: float) -> str:
        """Classify a process as Active or Low activity from its delta sample.

        The classification threshold is deliberately small so near-idle
        processes are not shown as busy, and the value passed in is already a
        delta over `PROCESS_SAMPLE_SECONDS` (never a single instantaneous
        reading).
        """

        if cpu_percent < scanner_module.SystemScanner.PROCESS_ACTIVITY_MIN_CPU_PERCENT:
            return "Low activity"
        return "Active"

    @staticmethod
    def _process_iter(*, attrs: list[str]) -> Any:
        psutil_module = scanner_module.SystemScanner._require_psutil()
        return call_legacy_compatible(
            lambda: psutil_module.process_iter(attrs=attrs),
            psutil_module.process_iter,
        )

    def _protected_pids(self) -> set[int]:
        return protected_process_pids(scanner_module.psutil)
