"""Deterministic scheduling fakes shared by coordinator, timer and concurrency tests.

The deferred runner models the exact worker queue ``AppCoordinator`` consumes
without real threads; the timer master models the Tk ``after`` protocol
``AppWindow`` depends on. Both are deliberately minimal: they record the
scheduling contract and let tests step time explicitly.
"""

from collections.abc import Callable


class DeferredRunner:
    """Queued worker runner that executes workers only when the test steps it.

    One fresh instance must be created per test: the queue is mutable state
    that must never leak between tests. ``run(index)`` steps one queued worker;
    the default ``index=0`` is FIFO, and an explicit index lets a test run a
    later worker first without real threads.
    """

    def __init__(self) -> None:
        self.workers: list[Callable[[], None]] = []

    def __call__(self, worker: Callable[[], None]) -> None:
        self.workers.append(worker)

    def run(self, index: int = 0) -> None:
        self.workers.pop(index)()

    def run_next(self) -> None:
        self.run(0)

    @property
    def pending(self) -> int:
        return len(self.workers)


class TimerMaster:
    """Recording fake for the Tk ``after``/``after_cancel`` timer protocol."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[object, ...]] = []
        self.cancelled: list[str] = []
        self.destroyed = False
        self.next_timer_id = 0

    def after(self, delay: int, callback: object, *args: object) -> str:
        self.scheduled.append((delay, callback, *args))
        self.next_timer_id += 1
        return f"after#{self.next_timer_id}"

    def after_cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)

    def destroy(self) -> None:
        self.destroyed = True


class FailingMaster(TimerMaster):
    """Timer master whose ``after`` call raises on a dead event loop."""

    def after(self, delay: int, callback: object, *args: object) -> str:
        del delay, callback, args
        raise RuntimeError("event loop is not running")


class FailingCancelMaster(TimerMaster):
    """Timer master whose ``after_cancel`` call raises on a dead event loop."""

    def after_cancel(self, identifier: str) -> None:
        del identifier
        raise RuntimeError("event loop is not running")
