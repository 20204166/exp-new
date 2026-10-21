import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from maintenance.components.cluster_storage import (
    CoordinatorTimeline,
    ResourceSnapshot,
    SnapshotBatch,
    StandbyBuffer,
    snapshot_batch_from_dict,
    snapshot_batch_from_resources,
)
from maintenance.nodes import NodeId


def make_batch(batch_id: str, size_bytes: int) -> SnapshotBatch:
    return SnapshotBatch(
        batch_id=batch_id,
        source_node_id=NodeId("coordinator"),
        source_epoch=3,
        sequence=size_bytes,
        observed_at=100.0,
        payload=(ResourceSnapshot(NodeId("worker"), "cpu", "10%", 10.0, 10.0, 100.0),),
        encoded_size=size_bytes,
    )


class ClusterStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def test_standby_purges_oldest_batch_at_cap(self) -> None:
        store = StandbyBuffer(self.path / "standby.db", max_bytes=256)
        self.assertTrue(store.append(make_batch("old", 200), now=100.0))
        self.assertTrue(store.append(make_batch("new", 100), now=100.0))
        self.assertEqual(store.batch_ids(), ("new",))

    def test_duplicate_batch_is_idempotent(self) -> None:
        store = CoordinatorTimeline(self.path / "history.db", max_bytes=256)
        batch = make_batch("same", 20)
        self.assertTrue(store.append(batch, now=100.0))
        self.assertFalse(store.append(batch, now=100.0))
        self.assertEqual(store.status().row_count, 1)

    def test_oversized_batch_pauses_writes_without_inserting(self) -> None:
        store = CoordinatorTimeline(self.path / "history.db", max_bytes=20)
        self.assertFalse(store.append(make_batch("large", 21), now=100.0))
        self.assertEqual(store.batch_ids(), ())
        self.assertTrue(store.status().history_writes_paused)

    def test_storage_status_reports_logical_payload_budget(self) -> None:
        store = CoordinatorTimeline(self.path / "history.db", max_bytes=20)
        self.assertEqual(store.max_bytes, 20)
        self.assertEqual(store.capacity_basis, "logical encoded payload bytes")

    def test_standby_drops_old_batches(self) -> None:
        store = StandbyBuffer(self.path / "standby.db", max_bytes=1024)
        self.assertFalse(store.append(make_batch("old", 20), now=100.0 + 86400.1))

    def test_snapshots_are_normalized_and_read_back(self) -> None:
        store = CoordinatorTimeline(self.path / "history.db", max_bytes=1024)
        store.append(make_batch("batch", 20), now=100.0)
        self.assertEqual(store.snapshots()[0].node_id, NodeId("worker"))

    def test_import_records_missing_sequence_gap(self) -> None:
        store = CoordinatorTimeline(self.path / "history.db", max_bytes=1024)
        first = SnapshotBatch(
            "first",
            NodeId("coordinator"),
            3,
            1,
            100.0,
            (ResourceSnapshot(NodeId("worker"), "cpu", "10%", 10.0, 10.0, 100.0),),
            20,
        )
        second = SnapshotBatch(
            "third", NodeId("coordinator"), 3, 3, 102.0, first.payload, 20
        )
        self.assertTrue(store.import_batch(first, expected_epoch=3, now=100.0))
        self.assertTrue(store.import_batch(second, expected_epoch=3, now=102.0))
        self.assertEqual(store.data_gaps()[0].first_missing_sequence, 2)

    def test_wire_batch_rejects_non_integer_identity_fields(self) -> None:
        value = {
            "batch_id": "batch",
            "source_node_id": "coordinator",
            "source_epoch": 1.5,
            "sequence": True,
            "observed_at": 100.0,
            "encoded_size": 20,
            "payload": [],
        }

        with self.assertRaises(ValueError):
            snapshot_batch_from_dict(value)

    def test_snapshot_batch_builder_binds_cluster_epoch_and_bounds_payload(
        self,
    ) -> None:
        batch = snapshot_batch_from_resources(
            source_node_id=NodeId("worker"),
            cluster_id="cluster",
            epoch=4,
            sequence=7,
            observed_at=120.0,
            resources=(
                SimpleNamespace(
                    key="cpu", value="42%", percent=42.0, numeric_value=42.0
                ),
            ),
        )

        self.assertEqual(batch.source_node_id, NodeId("worker"))
        self.assertEqual(batch.cluster_id, "cluster")
        self.assertEqual(batch.source_epoch, 4)
        self.assertEqual(batch.sequence, 7)
        self.assertEqual(batch.payload[0].metric, "cpu")
        self.assertGreater(batch.encoded_size, 0)

    def test_local_sequence_allocation_survives_store_restart(self) -> None:
        path = self.path / "history.sqlite3"
        first = CoordinatorTimeline(path)

        self.assertEqual(first.allocate_sequence(NodeId("worker"), 4), 1)
        self.assertEqual(first.allocate_sequence(NodeId("worker"), 4), 2)
        self.assertEqual(first.allocate_sequence(NodeId("worker"), 5), 1)

        restarted = CoordinatorTimeline(path)
        self.assertEqual(restarted.allocate_sequence(NodeId("worker"), 4), 3)
