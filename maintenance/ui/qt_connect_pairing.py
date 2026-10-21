"""Qt-thread approval bridge for Connect's inbound pairing callback."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(eq=False)
class _PendingApproval:
    """One worker waiting for a fail-closed UI decision."""

    completed: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    approved: bool = False


class QtPairingApprovalBridge:
    """Marshal target-side Pair approval onto Qt without blocking its event loop.

    The Connect listener callback runs on a package-owned worker thread. The
    host submits the actual prompt to Qt and waits only for a bounded interval.
    Closing, failed delivery, timeout, and prompt exceptions all deny Pair.
    """

    def __init__(
        self,
        *,
        submit: Callable[[Callable[[], None]], None],
        approve: Callable[[object], bool],
        is_ui_thread: Callable[[], bool],
        timeout_seconds: float = 55.0,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("pairing approval timeout must be finite and positive")
        self._submit = submit
        self._approve = approve
        self._is_ui_thread = is_ui_thread
        self._timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[_PendingApproval] = set()

    def __call__(self, request: object) -> bool:
        """Return the explicit UI decision, or deny if it cannot be obtained."""

        with self._lock:
            if self._closed:
                return False
        if self._is_ui_thread():
            try:
                return self._approve(request) is True
            except Exception:  # noqa: BLE001 -- Approval errors deny Pair.
                return False

        pending = _PendingApproval()
        with self._lock:
            if self._closed:
                return False
            self._pending.add(pending)
        try:
            self._submit(lambda: self._deliver_decision(pending, request))
        except Exception:  # noqa: BLE001 -- Failed Qt delivery denies Pair.
            self._cancel(pending)
            return False

        if not pending.completed.wait(self._timeout_seconds):
            self._cancel(pending)
            return False
        return pending.approved

    def close(self) -> None:
        """Reject pending approvals and deny all future callback invocations."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending)
            self._pending.clear()
            for item in pending:
                item.cancelled = True
                item.approved = False
                item.completed.set()

    def _deliver_decision(self, pending: _PendingApproval, request: object) -> None:
        with self._lock:
            if self._closed or pending.cancelled:
                return
        try:
            approved = self._approve(request) is True
        except Exception:  # noqa: BLE001 -- Prompt failures deny Pair.
            approved = False
        with self._lock:
            if self._closed or pending.cancelled:
                pending.approved = False
            else:
                pending.approved = approved
            self._pending.discard(pending)
            pending.completed.set()

    def _cancel(self, pending: _PendingApproval) -> None:
        with self._lock:
            pending.cancelled = True
            pending.approved = False
            self._pending.discard(pending)
