"""Bounded, thread-safe runtime observability primitives."""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal["success", "failure", "cancelled"]
EventKind = Literal[
    "request",
    "commit",
    "failure",
    "coalesced",
    "stale",
    "rejected",
    "cache_hit",
]
_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "request",
        "commit",
        "failure",
        "coalesced",
        "stale",
        "rejected",
        "cache_hit",
    }
)
_COUNTED_EVENT_KINDS: frozenset[str] = frozenset({"coalesced", "stale", "rejected"})


@dataclass(frozen=True, slots=True)
class ObservationToken:
    target: str
    started: float
    identifier: int


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    target: str
    count: int
    successes: int
    failures: int
    cancellations: int
    coalesced: int
    stale: int
    rejected: int
    events: tuple[tuple[str, int], ...]
    in_flight: int
    peak_in_flight: int
    samples: tuple[float, ...]
    distribution: dict[str, float | int]
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ObservabilitySnapshot:
    session_started_at: float
    captured_at: float
    duration_seconds: float
    metrics: tuple[MetricSnapshot, ...]


@dataclass(slots=True)
class _Metric:
    samples: deque[float]
    count: int = 0
    successes: int = 0
    failures: int = 0
    cancellations: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    last_error: str | None = None
    coalesced: int = 0
    stale: int = 0
    rejected: int = 0
    events: dict[str, int] = field(default_factory=dict)


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize_samples(samples: tuple[float, ...]) -> dict[str, float | int]:
    """Return distribution statistics for a non-empty bounded sample set."""

    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    p25 = _percentile(ordered, 0.25)
    p75 = _percentile(ordered, 0.75)
    median = statistics.median(ordered)
    return {
        "minimum": ordered[0],
        "p25": p25,
        "p50": median,
        "median": median,
        "p75": p75,
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "maximum": ordered[-1],
        "spread": ordered[-1] - ordered[0],
        "outliers": sum(value > p75 + 1.5 * (p75 - p25) for value in ordered),
    }


class ObservabilityWatcher:
    """Collect bounded runtime metrics without owning application behavior."""

    def __init__(
        self,
        *,
        sample_limit: int = 128,
        clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if sample_limit <= 0:
            raise ValueError("sample_limit must be positive")
        self._sample_limit = sample_limit
        self._clock = clock
        self._wall_clock = wall_clock
        self._session_started_at = wall_clock()
        self._lock = threading.RLock()
        self._metrics: dict[str, _Metric] = {}
        self._active_tokens: set[int] = set()
        self._next_token = 0

    def begin(self, target: str) -> ObservationToken:
        self._validate_target(target)
        with self._lock:
            metric = self._metric(target)
            metric.in_flight += 1
            metric.peak_in_flight = max(metric.peak_in_flight, metric.in_flight)
            self._next_token += 1
            token = ObservationToken(target, self._clock(), self._next_token)
            self._active_tokens.add(token.identifier)
        return token

    def finish(
        self,
        token: ObservationToken,
        *,
        outcome: Outcome = "success",
        duration_seconds: float | None = None,
        detail: str | None = None,
    ) -> None:
        duration = (
            self._clock() - token.started
            if duration_seconds is None
            else duration_seconds
        )
        with self._lock:
            if token.identifier not in self._active_tokens:
                raise ValueError("observation token was already finished")
            if duration < 0:
                raise ValueError("duration_seconds cannot be negative")
            self._active_tokens.remove(token.identifier)
            metric = self._metric(token.target)
            metric.in_flight = max(metric.in_flight - 1, 0)
        self.record(token.target, duration, outcome=outcome, detail=detail)

    def record(
        self,
        target: str,
        duration_seconds: float,
        *,
        outcome: Outcome = "success",
        detail: str | None = None,
    ) -> None:
        self._validate_target(target)
        if duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        if outcome not in ("success", "failure", "cancelled"):
            raise ValueError(f"invalid outcome: {outcome}")
        with self._lock:
            metric = self._metric(target)
            metric.count += 1
            metric.samples.append(float(duration_seconds))
            if outcome == "success":
                metric.successes += 1
            elif outcome == "failure":
                metric.failures += 1
                metric.last_error = (detail or "failure")[:160]
            else:
                metric.cancellations += 1

    def record_event(self, target: str, event: EventKind) -> None:
        self._validate_target(target)
        if event not in _EVENT_KINDS:
            raise ValueError(f"invalid event: {event}")
        with self._lock:
            metric = self._metric(target)
            metric.events[event] = metric.events.get(event, 0) + 1
            if event in _COUNTED_EVENT_KINDS:
                setattr(metric, event, getattr(metric, event) + 1)

    def event_count(self, target: str, event: str) -> int:
        with self._lock:
            return self._metrics.get(target, _Metric(deque(maxlen=1))).events.get(
                event, 0
            )

    def event_total(self, prefix: str, event: str) -> int:
        with self._lock:
            return sum(
                metric.events.get(event, 0)
                for target, metric in self._metrics.items()
                if target.startswith(prefix)
            )

    def snapshot(self) -> ObservabilitySnapshot:
        with self._lock:
            metrics = tuple(
                self._snapshot_metric(target, metric)
                for target, metric in sorted(self._metrics.items())
            )
        captured_at = self._wall_clock()
        return ObservabilitySnapshot(
            self._session_started_at,
            captured_at,
            max(captured_at - self._session_started_at, 0.0),
            metrics,
        )

    def reset(self) -> None:
        """Start a fresh in-memory observation session."""

        with self._lock:
            self._metrics.clear()
            self._active_tokens.clear()
            self._session_started_at = self._wall_clock()

    def _metric(self, target: str) -> _Metric:
        return self._metrics.setdefault(
            target, _Metric(deque(maxlen=self._sample_limit))
        )

    @staticmethod
    def _validate_target(target: str) -> None:
        if not target or len(target) > 160:
            raise ValueError("target must be non-empty and at most 160 characters")

    @staticmethod
    def _snapshot_metric(target: str, metric: _Metric) -> MetricSnapshot:
        samples = tuple(metric.samples)
        return MetricSnapshot(
            target,
            metric.count,
            metric.successes,
            metric.failures,
            metric.cancellations,
            metric.coalesced,
            metric.stale,
            metric.rejected,
            tuple(sorted(metric.events.items())),
            metric.in_flight,
            metric.peak_in_flight,
            samples,
            summarize_samples(samples) if samples else {},
            metric.last_error,
        )
