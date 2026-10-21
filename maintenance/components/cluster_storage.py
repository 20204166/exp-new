"""Bounded local SQLite storage for Coordinator history and standby batches."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maintenance.nodes import NodeId

LOGGER = logging.getLogger(__name__)
COORDINATOR_MAX_BYTES = 2 * 1024 * 1024 * 1024
STANDBY_MAX_BYTES = 256 * 1024 * 1024
STANDBY_MAX_AGE_SECONDS = 24 * 60 * 60
MAX_BATCH_PAYLOAD_BYTES = 4 * 1024 * 1024


def _wire_integer(value: object, field: str) -> int:
    """Accept JSON integers without silently truncating other numeric types."""

    if type(value) is not int:  # bool is intentionally excluded as well.
        raise ValueError(f"snapshot batch {field} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    node_id: NodeId
    metric: str
    display_value: str
    numeric_value: float | None
    percent: float | None
    observed_at: float


@dataclass(frozen=True, slots=True)
class SnapshotBatch:
    batch_id: str
    source_node_id: NodeId
    source_epoch: int
    sequence: int
    observed_at: float
    payload: tuple[ResourceSnapshot, ...]
    encoded_size: int
    cluster_id: str = ""

    def __post_init__(self) -> None:
        if not self.batch_id or self.source_epoch < 0 or self.sequence < 0:
            raise ValueError("invalid snapshot batch identity")
        if self.encoded_size < 0:
            raise ValueError("snapshot batch size must not be negative")
        if not math.isfinite(float(self.observed_at)):
            raise ValueError("snapshot batch timestamp must be finite")
        if not isinstance(self.cluster_id, str):
            raise TypeError("snapshot batch cluster id must be text")


@dataclass(frozen=True, slots=True)
class StorageStatus:
    bytes_used: int
    max_bytes: int
    row_count: int
    oldest_at: float | None
    newest_at: float | None
    history_writes_paused: bool = False
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class DataGap:
    source_node_id: NodeId
    source_epoch: int
    first_missing_sequence: int
    last_missing_sequence: int
    detected_at: float

    def __post_init__(self) -> None:
        if (
            self.source_epoch < 0
            or self.first_missing_sequence < 0
            or self.last_missing_sequence < self.first_missing_sequence
            or not math.isfinite(self.detected_at)
        ):
            raise ValueError("invalid data gap")


def snapshot_batch_to_dict(batch: SnapshotBatch) -> dict[str, object]:
    """Encode only the normalized, bounded data allowed on the role wire."""

    return {
        "batch_id": batch.batch_id,
        "source_node_id": batch.source_node_id.value,
        "source_epoch": batch.source_epoch,
        "sequence": batch.sequence,
        "observed_at": batch.observed_at,
        "encoded_size": batch.encoded_size,
        "cluster_id": batch.cluster_id,
        "payload": [
            {
                "node_id": item.node_id.value,
                "metric": item.metric,
                "display_value": item.display_value,
                "numeric_value": item.numeric_value,
                "percent": item.percent,
                "observed_at": item.observed_at,
            }
            for item in batch.payload
        ],
    }


def snapshot_batch_from_resources(
    *,
    source_node_id: NodeId,
    cluster_id: str,
    epoch: int,
    sequence: int,
    observed_at: float,
    resources: Iterable[object],
) -> SnapshotBatch:
    """Build the bounded cluster data-plane batch for one dashboard sample."""

    payload = tuple(
        ResourceSnapshot(
            source_node_id,
            str(getattr(resource, "key", "unknown")),
            str(getattr(resource, "value", "Unavailable")),
            None,
            getattr(resource, "percent", None),
            observed_at,
        )
        for resource in resources
    )
    batch = SnapshotBatch(
        f"{source_node_id.value}:{epoch}:{sequence}",
        source_node_id,
        epoch,
        sequence,
        observed_at,
        payload,
        0,
        cluster_id,
    )
    encoded_size = len(
        json.dumps(snapshot_batch_to_dict(batch), separators=(",", ":")).encode()
    )
    if encoded_size > MAX_BATCH_PAYLOAD_BYTES:
        raise ValueError("snapshot batch is too large")
    return SnapshotBatch(
        batch.batch_id,
        batch.source_node_id,
        batch.source_epoch,
        batch.sequence,
        batch.observed_at,
        batch.payload,
        encoded_size,
        batch.cluster_id,
    )


def snapshot_batch_from_dict(value: object) -> SnapshotBatch:
    if not isinstance(value, dict):
        raise TypeError("snapshot batch must be an object")
    required = {
        "batch_id",
        "source_node_id",
        "source_epoch",
        "sequence",
        "observed_at",
        "encoded_size",
        "payload",
    }
    if not required <= set(value):
        raise ValueError("snapshot batch is incomplete")
    payload = value["payload"]
    if not isinstance(payload, list) or len(payload) > 10000:
        raise ValueError("snapshot batch payload is invalid")
    records: list[ResourceSnapshot] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TypeError("snapshot record is invalid")
        try:
            record = ResourceSnapshot(
                NodeId(str(item["node_id"])),
                str(item["metric"]),
                str(item["display_value"]),
                item.get("numeric_value"),
                item.get("percent"),
                float(item["observed_at"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("snapshot record is invalid") from error
        if not math.isfinite(record.observed_at):
            raise ValueError("snapshot record timestamp is not finite")
        records.append(record)
    batch = SnapshotBatch(
        str(value["batch_id"]),
        NodeId(str(value["source_node_id"])),
        _wire_integer(value["source_epoch"], "source epoch"),
        _wire_integer(value["sequence"], "sequence"),
        float(value["observed_at"]),
        tuple(records),
        _wire_integer(value["encoded_size"], "encoded size"),
        str(value.get("cluster_id", "")),
    )
    if (
        len(json.dumps(snapshot_batch_to_dict(batch), separators=(",", ":")).encode())
        > MAX_BATCH_PAYLOAD_BYTES
    ):
        raise ValueError("snapshot batch is too large")
    _BatchStore._validate_batch(batch)
    return batch


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2.0)
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class _BatchStore:
    def __init__(self, path: Path, *, max_bytes: int, max_age: float | None) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_age = max_age
        self._paused = False
        self._warning: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS snapshot_batches (
                    batch_id TEXT PRIMARY KEY,
                    source_node_id TEXT NOT NULL,
                    source_epoch INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at REAL NOT NULL,
                    encoded_size INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(snapshot_batches)")
            }
            if "cluster_id" not in columns:
                db.execute(
                    "ALTER TABLE snapshot_batches ADD COLUMN cluster_id TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS batches_observed ON snapshot_batches(observed_at)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS batches_source_sequence "
                "ON snapshot_batches(source_node_id, source_epoch, sequence)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS sequence_counters (
                    source_node_id TEXT NOT NULL,
                    source_epoch INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    PRIMARY KEY (source_node_id, source_epoch)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS data_gaps (
                    source_node_id TEXT NOT NULL,
                    source_epoch INTEGER NOT NULL,
                    first_missing_sequence INTEGER NOT NULL,
                    last_missing_sequence INTEGER NOT NULL,
                    detected_at REAL NOT NULL,
                    PRIMARY KEY (source_node_id, source_epoch,
                                 first_missing_sequence, last_missing_sequence)
                )"""
            )
            db.commit()

    @property
    def capacity_basis(self) -> str:
        """Describe the bounded accounting used by this store."""

        return "logical encoded payload bytes"

    def append(self, batch: SnapshotBatch, *, now: float | None = None) -> bool:
        self._validate_batch(batch)
        current = time.time() if now is None else now
        if self.max_age is not None and batch.observed_at < current - self.max_age:
            return False
        if batch.encoded_size > self.max_bytes:
            self._paused = True
            self._warning = "history storage limit reached"
            return False
        if self._paused:
            return False
        payload = json.dumps(
            [
                {
                    "node_id": item.node_id.value,
                    "metric": item.metric,
                    "display_value": item.display_value,
                    "numeric_value": item.numeric_value,
                    "percent": item.percent,
                    "observed_at": item.observed_at,
                }
                for item in batch.payload
            ],
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_BATCH_PAYLOAD_BYTES:
            self._paused = True
            self._warning = "snapshot batch is too large"
            return False
        try:
            with _connect(self.path) as db:
                existing = db.execute(
                    "SELECT 1 FROM snapshot_batches WHERE batch_id = ?",
                    (batch.batch_id,),
                ).fetchone()
                if existing is not None:
                    return False
                db.execute(
                    "INSERT INTO snapshot_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch.batch_id,
                        batch.source_node_id.value,
                        batch.source_epoch,
                        batch.sequence,
                        batch.observed_at,
                        batch.encoded_size,
                        payload,
                        batch.cluster_id,
                    ),
                )
                self._purge_locked(db, current)
                db.commit()
        except sqlite3.IntegrityError:
            return False
        except sqlite3.OperationalError as error:
            self._record_sqlite_failure(error)
            return False
        return True

    def allocate_sequence(self, source_node_id: NodeId, source_epoch: int) -> int:
        """Atomically allocate a persistent sequence for one source and epoch."""

        if type(source_epoch) is not int or source_epoch < 0:
            raise ValueError("source epoch must be a non-negative integer")
        with _connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence FROM sequence_counters "
                "WHERE source_node_id = ? AND source_epoch = ?",
                (source_node_id.value, source_epoch),
            ).fetchone()
            sequence = 1 if row is None else int(row[0]) + 1
            if sequence > 2**63 - 1:
                raise OverflowError("cluster history sequence is exhausted")
            db.execute(
                "INSERT INTO sequence_counters VALUES (?, ?, ?) "
                "ON CONFLICT(source_node_id, source_epoch) "
                "DO UPDATE SET sequence = excluded.sequence",
                (source_node_id.value, source_epoch, sequence),
            )
            db.commit()
        return sequence

    def import_batch(
        self,
        batch: SnapshotBatch,
        *,
        cluster_id: str | None = None,
        expected_source_node_id: NodeId | None = None,
        expected_epoch: int | None = None,
        now: float | None = None,
    ) -> bool:
        if cluster_id is not None and batch.cluster_id not in ("", cluster_id):
            raise ValueError("snapshot batch belongs to another cluster")
        if (
            expected_source_node_id is not None
            and batch.source_node_id != expected_source_node_id
        ):
            raise ValueError("snapshot batch source identity is invalid")
        if expected_epoch is not None and batch.source_epoch != expected_epoch:
            raise ValueError("snapshot batch epoch is stale")
        with _connect(self.path) as db:
            prior = db.execute(
                "SELECT source_epoch, sequence FROM snapshot_batches "
                "WHERE source_node_id = ? ORDER BY sequence DESC LIMIT 1",
                (batch.source_node_id.value,),
            ).fetchone()
        if (
            prior is not None
            and batch.source_epoch == int(prior[0])
            and batch.sequence <= int(prior[1])
        ):
            if batch.batch_id in self.batch_ids():
                return False
            raise ValueError("snapshot batch sequence is stale")
        if (
            prior is not None
            and batch.source_epoch == int(prior[0])
            and batch.sequence > int(prior[1]) + 1
        ):
            gap = DataGap(
                batch.source_node_id,
                batch.source_epoch,
                int(prior[1]) + 1,
                batch.sequence - 1,
                time.time() if now is None else now,
            )
            with _connect(self.path) as db:
                db.execute(
                    "INSERT OR IGNORE INTO data_gaps VALUES (?, ?, ?, ?, ?)",
                    (
                        gap.source_node_id.value,
                        gap.source_epoch,
                        gap.first_missing_sequence,
                        gap.last_missing_sequence,
                        gap.detected_at,
                    ),
                )
                db.commit()
        return self.append(batch, now=now)

    def data_gaps(self) -> tuple[DataGap, ...]:
        with _connect(self.path) as db:
            rows = db.execute(
                "SELECT source_node_id, source_epoch, first_missing_sequence, "
                "last_missing_sequence, detected_at FROM data_gaps "
                "ORDER BY detected_at, rowid"
            ).fetchall()
        return tuple(
            DataGap(
                NodeId(str(row[0])),
                int(row[1]),
                int(row[2]),
                int(row[3]),
                float(row[4]),
            )
            for row in rows
        )

    def batches(self) -> tuple[SnapshotBatch, ...]:
        with _connect(self.path) as db:
            rows = db.execute(
                "SELECT batch_id, source_node_id, source_epoch, sequence, observed_at, encoded_size, payload, cluster_id "
                "FROM snapshot_batches ORDER BY observed_at, rowid"
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def batch_ids(self) -> tuple[str, ...]:
        return tuple(batch.batch_id for batch in self.batches())

    def status(self) -> StorageStatus:
        with _connect(self.path) as db:
            row = db.execute(
                "SELECT COALESCE(SUM(encoded_size), 0), COUNT(*), MIN(observed_at), MAX(observed_at) "
                "FROM snapshot_batches"
            ).fetchone()
        return StorageStatus(
            bytes_used=int(row[0]),
            max_bytes=self.max_bytes,
            row_count=int(row[1]),
            oldest_at=row[2],
            newest_at=row[3],
            history_writes_paused=self._paused,
            warning=self._warning,
        )

    def _purge_locked(self, db: sqlite3.Connection, now: float) -> None:
        cutoff = None if self.max_age is None else now - self.max_age
        if cutoff is not None:
            db.execute("DELETE FROM snapshot_batches WHERE observed_at < ?", (cutoff,))
        while True:
            row = db.execute(
                "SELECT COALESCE(SUM(encoded_size), 0) FROM snapshot_batches"
            ).fetchone()
            if int(row[0]) <= self.max_bytes:
                self._paused = False
                return
            oldest = db.execute(
                "SELECT batch_id FROM snapshot_batches ORDER BY observed_at, rowid LIMIT 1"
            ).fetchone()
            if oldest is None:
                self._paused = True
                self._warning = "history storage limit reached"
                return
            db.execute("DELETE FROM snapshot_batches WHERE batch_id = ?", (oldest[0],))

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> SnapshotBatch:
        payload = json.loads(str(row[6]))
        records = tuple(
            ResourceSnapshot(
                node_id=NodeId(str(item["node_id"])),
                metric=str(item["metric"]),
                display_value=str(item["display_value"]),
                numeric_value=item.get("numeric_value"),
                percent=item.get("percent"),
                observed_at=float(item["observed_at"]),
            )
            for item in payload
        )
        return SnapshotBatch(
            batch_id=str(row[0]),
            source_node_id=NodeId(str(row[1])),
            source_epoch=int(row[2]),
            sequence=int(row[3]),
            observed_at=float(row[4]),
            encoded_size=int(row[5]),
            payload=records,
            cluster_id=str(row[7]),
        )

    @staticmethod
    def _validate_batch(batch: SnapshotBatch) -> None:
        if any(not item.metric or not item.display_value for item in batch.payload):
            raise ValueError("snapshot contains an empty field")

    def _record_sqlite_failure(self, error: sqlite3.OperationalError) -> None:
        message = str(error).lower()
        if any(marker in message for marker in ("full", "i/o", "locked", "readonly")):
            self._paused = True
            self._warning = "history writes paused: storage unavailable"
            LOGGER.warning("Cluster history write paused: %s", error)
            return
        raise error


class CoordinatorTimeline(_BatchStore):
    """Coordinator-owned bounded timeline storage."""

    def __init__(self, path: Path, *, max_bytes: int = COORDINATOR_MAX_BYTES) -> None:
        super().__init__(path, max_bytes=max_bytes, max_age=None)

    def snapshots(self) -> tuple[ResourceSnapshot, ...]:
        return tuple(item for batch in self.batches() for item in batch.payload)


class StandbyBuffer(_BatchStore):
    """Subcoordinator-owned rolling authenticated batch buffer."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = STANDBY_MAX_BYTES,
        max_age_seconds: float = STANDBY_MAX_AGE_SECONDS,
    ) -> None:
        super().__init__(path, max_bytes=max_bytes, max_age=max_age_seconds)


__all__ = [
    "COORDINATOR_MAX_BYTES",
    "MAX_BATCH_PAYLOAD_BYTES",
    "STANDBY_MAX_AGE_SECONDS",
    "STANDBY_MAX_BYTES",
    "CoordinatorTimeline",
    "DataGap",
    "ResourceSnapshot",
    "SnapshotBatch",
    "StandbyBuffer",
    "StorageStatus",
    "snapshot_batch_from_dict",
    "snapshot_batch_to_dict",
]
