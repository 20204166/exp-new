import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from maintenance.components.scan_support import call_cancellable
from maintenance.models import (
    DashboardSnapshot,
    FileCandidate,
    ProcessCandidate,
    ResourceSummary,
)
from maintenance.scanner import ProgressCallback, SystemScanner


class Analyzer:
    """Collect system information for the interactive dashboard."""

    def __init__(self) -> None:
        """Build the analyzer facade around the structured scanner."""
        self.scanner = SystemScanner()

    @staticmethod
    def _call_with_cancel(
        func: Callable[..., Any],
        cancel_event: threading.Event | None,
    ) -> Any:
        """Call `func`, passing `cancel_event` only when one is provided.

        Reuses the legacy-signature adapter shared by the scanner and
        dialogs: when an event is provided and an older one-argument hook
        rejects the keyword, the fallback runs under the same cancellation
        check (`check_cancelled`) used everywhere else, so a cancelled
        legacy hook never starts a fresh uncancellable run.
        """

        if cancel_event is None:
            return func()
        return call_cancellable(
            lambda: func(cancel_event=cancel_event),
            func,
            cancel_event,
        )

    def dashboard_snapshot(
        self,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DashboardSnapshot:
        """Return structured data for the interactive dashboard."""
        return self.scanner.scan_dashboard(
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def stop_background_workers(self) -> None:
        """Stop the persistent CPU sampler and abandon any in-flight GPU query.

        Called when the window closes so no scanner thread keeps sampling
        or publishing after the UI is gone.
        """

        self.scanner._stop_cpu_sampler()
        self.scanner._stop_gpu_query()

    def component_summary(
        self,
        key: str,
        cancel_event: threading.Event | None = None,
    ) -> ResourceSummary:
        """Scan one dashboard component independently and return its card."""
        return self._call_with_cancel(
            lambda event=None: self.scanner.scan_component(key, cancel_event=event),
            cancel_event,
        )

    def reset_component_sample(self, key: str) -> None:
        """Reset one component's persistent sample state (e.g. network rates).

        Called when a component resumes periodic polling after a pause so the
        first resumed sample is a fresh reading rather than a stale average.
        Components without persistent sample state are unaffected.
        """

        if key != "network":
            return
        self.scanner._reset_network_sample()

    def process_candidates(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[ProcessCandidate]:
        """Return reviewable processes for the CPU and memory views."""
        return self._call_with_cancel(self.scanner.scan_processes, cancel_event)

    def storage_candidates(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        scan_root: Path | None = None,
    ) -> list[FileCandidate]:
        """Return large and duplicate files discovered in Downloads.

        ``scan_root`` overrides the scanned directory for this call only
        (e.g. the opt-in full-system scan); ``None`` keeps the existing
        Downloads-only behaviour and call signature for legacy callers.
        """
        if progress_callback is None and cancel_event is None and scan_root is None:
            return self.scanner.scan_downloads()
        return self.scanner.scan_downloads(
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            scan_root=scan_root,
        )
