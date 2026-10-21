"""Qt-owned callback and timer delivery, independent of Tk."""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from queue import Empty, Queue

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal, Slot


class QtDeliveryQueue(QObject):
    """Thread-safe worker submission with bounded GUI-thread delivery."""

    _wake = Signal()

    def __init__(self, *, batch_size: int = 128, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._batch_size = batch_size
        self._callbacks: Queue[Callable[[], None]] = Queue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._logger = logging.getLogger(__name__)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._drain)
        self._wake.connect(self._schedule_drain, Qt.ConnectionType.QueuedConnection)

    def submit(self, callback: Callable[[], None]) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._callbacks.put(callback)
        self._wake.emit()

    @Slot()
    def _schedule_drain(self) -> None:
        if not self._closed and not self._timer.isActive():
            self._timer.start(0)

    @Slot()
    def _drain(self) -> None:
        if self._closed:
            return
        for _ in range(self._batch_size):
            try:
                callback = self._callbacks.get_nowait()
            except Empty:
                break
            try:
                callback()
            except Exception as error:  # noqa: BLE001 - isolate UI delivery failures.
                self._logger.warning("Dropped UI delivery callback: %s", error)
        if not self._callbacks.empty():
            self._timer.start(0)

    def close(self) -> None:
        """Cancel scheduled delivery and discard callbacks not yet run."""
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("QtDeliveryQueue.close must run on its Qt thread")
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._timer.stop()
            QCoreApplication.removePostedEvents(self)
            while True:
                try:
                    self._callbacks.get_nowait()
                except Empty:
                    break


class QtTimerDelivery(QObject):
    """GUI-thread timer registry with cancellable opaque identifiers."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ids = itertools.count(1)
        self._timers: dict[str, QTimer] = {}
        self._retired_timers: set[QTimer] = set()
        self._closed = False
        self._logger = logging.getLogger(__name__)

    def schedule(
        self, delay: int, callback: Callable[..., None], *args: object
    ) -> str | None:
        self._assert_gui_thread()
        if self._closed:
            return None
        identifier = f"qtimer:{next(self._ids)}"
        timer = QTimer(self)
        timer.setSingleShot(True)

        def deliver() -> None:
            self._timers.pop(identifier, None)
            self._retire(timer)
            if self._closed:
                return
            try:
                callback(*args)
            except Exception as error:  # noqa: BLE001 - one callback cannot stop timers.
                self._logger.warning("Dropped UI timer callback: %s", error)

        timer.timeout.connect(deliver)
        self._timers[identifier] = timer
        timer.start(max(0, delay))
        return identifier

    def cancel(self, identifier: str | None) -> bool:
        self._assert_gui_thread()
        if identifier is None:
            return True
        timer = self._timers.pop(identifier, None)
        if timer is None:
            return False
        timer.stop()
        self._retire(timer)
        return True

    def cancel_all(self) -> None:
        """Cancel every pending timer while keeping this owner usable."""
        self._assert_gui_thread()
        for identifier in tuple(self._timers):
            self.cancel(identifier)

    def close(self) -> None:
        self._assert_gui_thread()
        if self._closed:
            return
        self._closed = True
        for timer in tuple(self._timers.values()):
            timer.stop()
        self._timers.clear()

    def _assert_gui_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise RuntimeError("QtTimerDelivery must be used on its Qt thread")

    def _retire(self, timer: QTimer) -> None:
        """Delete an inactive child timer later while retaining its wrapper."""
        self._retired_timers.add(timer)
        timer.setParent(None)
        timer.destroyed.connect(
            lambda _object=None, retired=timer: self._retired_timers.discard(retired)
        )
        timer.deleteLater()
