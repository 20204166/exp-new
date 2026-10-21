"""Cluster data-contract and trusted-node store tests."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from maintenance.cluster import (
    ClusterDataError,
    ClusterSaveError,
    ClusterState,
    ClusterStore,
    PeerGrantRecord,
    PendingPairing,
    dashboard_snapshot_from_dict,
    dashboard_snapshot_to_dict,
    file_candidate_from_dict,
    file_candidate_to_dict,
    is_node_snapshot_compatible,
    node_snapshot_envelope,
    node_snapshot_from_dict,
    node_snapshot_to_dict,
    parse_node_snapshot_envelope,
    process_candidate_from_dict,
    process_candidate_to_dict,
    resource_summary_from_dict,
    resource_summary_to_dict,
    trusted_node_record,
)
from maintenance.components.temperature import (
    temperature_sample_from_dict,
    temperature_sample_to_dict,
)
from maintenance.models import (
    CapabilityState,
    DashboardSnapshot,
    ProcessActionState,
    ProcessCandidate,
    ResourceSummary,
)
from maintenance.nodes import (
    NODE_SNAPSHOT_SCHEMA_VERSION,
    NodeCapability,
    NodeId,
    NodePermission,
    NodeSnapshot,
    NodeStatus,
)
from tests.support.models import make_file_candidate, make_snapshot, make_summary
from tests.support.temperature import make_temperature_sample

NOW = datetime.now(timezone.utc).astimezone()


def _summary(key: str = "cpu") -> ResourceSummary:
    return make_summary(
        key,
        key.upper(),
        value="10%",
        subtitle="running",
        percent=10.0,
        details=(f"{key.upper()}: 10%",),
        capability=CapabilityState.SUPPORTED,
    )


def _dashboard() -> DashboardSnapshot:
    return make_snapshot(
        _summary("cpu"),
        _summary("memory"),
        system_label="peer-host",
        scanned_at=NOW,
    )


def _snapshot() -> NodeSnapshot:
    return NodeSnapshot(
        node_id=NodeId("peer"),
        display_name="Peer",
        hostname="peer-host",
        platform="Linux",
        status=NodeStatus.ONLINE,
        capabilities=frozenset({NodeCapability.DASHBOARD_READ}),
        scanned_at=NOW,
        dashboard=_dashboard(),
    )


class ResourceSummaryCodecTests(unittest.TestCase):
    def test_temperature_sample_decoder_accepts_valid_boundaries(self) -> None:
        payload = temperature_sample_to_dict(make_temperature_sample("cpu", 45.0))

        for value in (45.0, 0.1, 249.9):
            with self.subTest(value=value):
                decoded = temperature_sample_from_dict(
                    {**payload, "value_celsius": value}
                )
                self.assertEqual(decoded.value_celsius, value)

    def test_temperature_sample_decoder_rejects_invalid_values(self) -> None:
        payload = temperature_sample_to_dict(make_temperature_sample("cpu", 45.0))
        values = (
            0,
            250,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            10**1000,
            "45",
        )

        for value in values:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                temperature_sample_from_dict({**payload, "value_celsius": value})

    def test_temperature_sample_decoder_rejects_bad_metadata_and_time(self) -> None:
        payload = temperature_sample_to_dict(make_temperature_sample("cpu", 45.0))

        for field in ("sensor_id", "sensor_name"):
            with self.subTest(field=field):
                invalid = dict(payload)
                invalid.pop(field)
                with self.assertRaises(TypeError):
                    temperature_sample_from_dict(invalid)

        for field, value in (
            ("component", ""),
            ("sensor_id", ""),
            ("sensor_name", ""),
            ("sampled_at", "not a timestamp"),
            ("sampled_monotonic", float("nan")),
            ("sampled_monotonic", float("inf")),
            ("sampled_monotonic", float("-inf")),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises((TypeError, ValueError)),
            ):
                temperature_sample_from_dict({**payload, field: value})

    def test_resource_summary_drops_only_invalid_temperature_samples(self) -> None:
        valid = temperature_sample_to_dict(make_temperature_sample("cpu", 45.0))
        invalid = {**valid, "value_celsius": float("nan")}
        payload = resource_summary_to_dict(_summary("cpu"))
        payload["temperatures"] = [invalid, valid]

        decoded = resource_summary_from_dict(payload)

        self.assertEqual(decoded.key, "cpu")
        self.assertEqual(decoded.title, "CPU")
        self.assertEqual(decoded.value, "10%")
        self.assertEqual(decoded.temperatures, (make_temperature_sample("cpu", 45.0),))

    def test_round_trip_preserves_summary(self) -> None:
        original = _summary("cpu")
        decoded = resource_summary_from_dict(resource_summary_to_dict(original))
        self.assertEqual(decoded, original)

    def test_round_trip_preserves_temperatures(self) -> None:
        original = ResourceSummary(
            key="cpu",
            title="CPU",
            value="10%",
            subtitle="running",
            percent=10.0,
            details=("CPU: 45°C",),
            actionable=False,
            failed=False,
            capability=CapabilityState.SUPPORTED,
            temperatures=(make_temperature_sample("cpu", 45.0),),
        )
        decoded = resource_summary_from_dict(resource_summary_to_dict(original))
        self.assertEqual(decoded, original)

    def test_missing_key_is_rejected(self) -> None:
        with self.assertRaises(ClusterDataError):
            resource_summary_from_dict({"title": "x"})

    def test_invalid_boolean_names_are_reported(self) -> None:
        payload = resource_summary_to_dict(_summary())
        for field in ("actionable", "failed"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ClusterDataError, rf"resource summary {field} must be a boolean"
                ),
            ):
                resource_summary_from_dict({**payload, field: "yes"})


class DashboardCodecTests(unittest.TestCase):
    def test_round_trip_preserves_dashboard(self) -> None:
        original = _dashboard()
        decoded = dashboard_snapshot_from_dict(dashboard_snapshot_to_dict(original))
        self.assertEqual(decoded, original)

    def test_invalid_resources_are_rejected(self) -> None:
        payload = dashboard_snapshot_to_dict(_dashboard())
        payload["resources"] = "nope"
        with self.assertRaises(ClusterDataError):
            dashboard_snapshot_from_dict(payload)


class NodeSnapshotCodecTests(unittest.TestCase):
    def test_resources_and_capability_states_are_preserved_for_all_categories(
        self,
    ) -> None:
        resources = tuple(
            ResourceSummary(
                key=key,
                title=key,
                value=f"{key}-value",
                subtitle="state",
                percent=None,
                details=(key,),
                capability=(
                    CapabilityState.UNSUPPORTED
                    if key in {"gpu", "battery"}
                    else CapabilityState.SUPPORTED
                ),
            )
            for key in ("cpu", "memory", "storage", "gpu", "network", "battery")
        )
        original = NodeSnapshot(
            node_id=NodeId("peer"),
            display_name="Peer",
            hostname="peer-host",
            platform="Linux",
            status=NodeStatus.ONLINE,
            capabilities=frozenset({NodeCapability.DASHBOARD_READ}),
            scanned_at=NOW,
            dashboard=DashboardSnapshot("Peer", NOW, resources),
        )

        decoded = node_snapshot_from_dict(node_snapshot_to_dict(original))

        self.assertEqual(decoded.resources, resources)
        self.assertEqual(
            decoded.resource("battery").capability, CapabilityState.UNSUPPORTED
        )

    def test_round_trip_preserves_snapshot(self) -> None:
        original = _snapshot()
        decoded = node_snapshot_from_dict(node_snapshot_to_dict(original))
        self.assertEqual(decoded, original)

    def test_envelope_round_trip(self) -> None:
        original = _snapshot()
        decoded = parse_node_snapshot_envelope(node_snapshot_envelope(original))
        self.assertEqual(decoded, original)

    def test_unsupported_schema_is_rejected(self) -> None:
        payload = node_snapshot_to_dict(_snapshot())
        payload["schema_version"] = NODE_SNAPSHOT_SCHEMA_VERSION + 1
        with self.assertRaises(ClusterDataError):
            node_snapshot_from_dict(payload)

    def test_tampered_payload_is_rejected(self) -> None:
        payload = node_snapshot_to_dict(_snapshot())
        payload["status"] = "bogus"
        with self.assertRaises(ClusterDataError):
            node_snapshot_from_dict(payload)

    def test_compatibility_matches_schema_version(self) -> None:
        self.assertTrue(is_node_snapshot_compatible(NODE_SNAPSHOT_SCHEMA_VERSION))
        self.assertFalse(is_node_snapshot_compatible("2"))

    def test_is_stale_detects_age(self) -> None:
        fresh = _snapshot()
        self.assertFalse(fresh.is_stale(now=NOW, max_age=timedelta(seconds=60)))
        self.assertTrue(
            fresh.is_stale(
                now=NOW + timedelta(seconds=120), max_age=timedelta(seconds=60)
            )
        )


class ProcessAndFileCodecTests(unittest.TestCase):
    def test_process_candidate_round_trip(self) -> None:
        original = ProcessCandidate(
            1, "app", 100, 1.0, 2.0, "Active", "user", True, 1.5
        )
        decoded = process_candidate_from_dict(process_candidate_to_dict(original))
        self.assertEqual(decoded, original)

    def test_process_candidate_round_trip_preserves_target_identity_state(self) -> None:
        original = ProcessCandidate(
            42,
            "protected-app",
            100,
            1.0,
            2.0,
            "Active",
            "user",
            False,
            12.5,
            True,
            ProcessActionState.PROTECTED,
        )
        decoded = process_candidate_from_dict(process_candidate_to_dict(original))
        self.assertEqual(decoded, original)

    def test_invalid_numeric_name_is_reported(self) -> None:
        payload = process_candidate_to_dict(
            ProcessCandidate(1, "app", 100, 1.0, 2.0, "Active", "user", True)
        )
        for field in ("memory_bytes", "memory_percent", "cpu_percent"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ClusterDataError, rf"process candidate {field} must be a number"
                ),
            ):
                process_candidate_from_dict({**payload, field: "not-a-number"})

    def test_file_candidate_round_trip(self) -> None:
        original = make_file_candidate(Path("/tmp/x"), modified_at=NOW, reason="reason")
        decoded = file_candidate_from_dict(file_candidate_to_dict(original))
        self.assertEqual(decoded, original)


class ClusterStoreTests(unittest.TestCase):
    def test_invalid_utf8_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            path.write_bytes(b"\xff")
            state = ClusterStore(path).load()
            self.assertEqual(state.trusted_nodes, ())

    def test_out_of_range_port_record_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.json"
            payload = {
                "schema_version": 1,
                "discovery_enabled": True,
                "local_node_id": "local",
                "trusted_nodes": [
                    {
                        "node_id": "peer",
                        "display_name": "Peer",
                        "hostname": "peer",
                        "host": "peer",
                        "port": 70000,
                        "secret": "a" * 64,
                    }
                ],
            }
            path.write_text(json.dumps(payload))

            state = ClusterStore(path).load()

            self.assertEqual(state.trusted_nodes, ())

    def _store(self) -> tuple[ClusterStore, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "cluster.json"
        return ClusterStore(path), path

    def test_missing_file_loads_defaults(self) -> None:
        store, _path = self._store()
        state = store.load()
        self.assertTrue(state.discovery_enabled)
        self.assertEqual(state.trusted_nodes, ())
        self.assertTrue(state.local_node_id.startswith("node-"))

    def test_local_node_identity_survives_reload(self) -> None:
        store, _path = self._store()

        first = store.load()
        second = store.load()

        self.assertEqual(first.local_node_id, second.local_node_id)

    def test_failed_identity_migration_is_marked_not_persisted(self) -> None:
        store, _path = self._store()
        with patch.object(
            store, "save", Mock(side_effect=ClusterSaveError("disk full"))
        ):
            state = store.load()

        self.assertFalse(state.local_identity_persisted)

    def test_save_restricts_file_to_owner_only(self) -> None:
        store, path = self._store()
        store.save(ClusterState.create_local())

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_and_load_round_trip(self) -> None:
        store, path = self._store()
        record = trusted_node_record(
            node_id="peer",
            display_name="Peer",
            hostname="peer-host",
            host="192.168.1.10",
            port=5000,
            capabilities=frozenset({NodeCapability.DASHBOARD_READ}),
            permissions=frozenset({NodePermission.DASHBOARD_READ}),
            color="emerald",
            identity_fingerprint="aaaa:bbbb",
        )
        state = ClusterState(discovery_enabled=False, trusted_nodes=(record,))
        store.save(state)
        loaded = ClusterStore(path).load()
        self.assertFalse(loaded.discovery_enabled)
        self.assertEqual(len(loaded.trusted_nodes), 1)
        restored = loaded.trusted_nodes[0]
        self.assertEqual(restored.node_id, "peer")
        self.assertEqual(restored.display_name, "Peer")
        self.assertEqual(restored.host, "192.168.1.10")
        self.assertEqual(restored.port, 5000)
        self.assertEqual(restored.color, "emerald")
        self.assertEqual(restored.capabilities, {NodeCapability.DASHBOARD_READ})
        self.assertEqual(restored.secret, record.secret)
        self.assertEqual(restored.identity_fingerprint, "aaaa:bbbb")
        self.assertEqual(
            restored.permissions,
            {NodePermission.DASHBOARD_READ},
        )
        self.assertTrue(loaded.local_node_id.startswith("node-"))

    def test_legacy_trusted_record_gets_no_permissions(self) -> None:
        store, path = self._store()
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "trusted_nodes": [
                        {
                            "node_id": "peer",
                            "display_name": "Peer",
                            "hostname": "peer-host",
                            "platform": "Linux",
                            "color": None,
                            "host": "127.0.0.1",
                            "port": 5000,
                            "capabilities": ["process_review"],
                            "secret": "a" * 64,
                            "trusted_at": 1.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        record = store.load().trusted_nodes[0]

        self.assertEqual(record.permissions, frozenset())

    def test_malformed_file_falls_back_to_defaults(self) -> None:
        store, path = self._store()
        path.write_text("not json", encoding="utf-8")
        state = store.load()
        self.assertTrue(state.discovery_enabled)
        self.assertEqual(state.trusted_nodes, ())

    def test_unsupported_schema_falls_back_to_defaults(self) -> None:
        store, path = self._store()
        path.write_text('{"schema_version": 99}', encoding="utf-8")
        state = store.load()
        self.assertTrue(state.discovery_enabled)

    def test_record_without_secret_is_dropped(self) -> None:
        store, path = self._store()
        path.write_text(
            '{"schema_version": 1, "discovery_enabled": true, "trusted_nodes": '
            '[{"node_id": "peer", "display_name": "P", "hostname": "h", "host": "h"}]}',
            encoding="utf-8",
        )
        state = store.load()
        self.assertEqual(state.trusted_nodes, ())

    def test_save_failure_raises(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        store = ClusterStore(base / "cluster.json")
        store.save(ClusterState())
        # Turn the parent directory into a file so a later save cannot mkdir it.
        parent = base / "cluster.json"
        parent.unlink()
        parent.write_text("x", encoding="utf-8")
        store = ClusterStore(parent / "nested.json")
        with self.assertRaises(ClusterSaveError):
            store.save(ClusterState(discovery_enabled=False))

    def test_trusted_node_record_generates_a_secret(self) -> None:
        record = trusted_node_record(
            node_id="a", display_name="A", hostname="a", host="a"
        )
        self.assertEqual(len(record.secret), 64)

    def test_cluster_state_record_lookup(self) -> None:
        record = trusted_node_record(
            node_id="a", display_name="A", hostname="a", host="a"
        )
        state = ClusterState(trusted_nodes=(record,))
        self.assertIs(state.record("a"), record)
        self.assertIsNone(state.record("missing"))

    def test_target_owned_peer_grants_round_trip(self) -> None:
        store, path = self._store()
        grant = PeerGrantRecord(
            caller_node_id="caller",
            secret="a" * 64,
            permissions=frozenset({NodePermission.DASHBOARD_READ}),
            transaction_id="tx-1",
        )
        store.save(ClusterState(peer_grants=(grant,)))
        loaded = ClusterStore(path).load()
        self.assertEqual(loaded.grant("caller"), grant)

    def test_old_peer_grant_json_defaults_transaction_binding_to_none(self) -> None:
        _store, path = self._store()
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "peer_grants": [
                        {
                            "caller_node_id": "caller",
                            "secret": "a" * 64,
                            "permissions": ["dashboard_read"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        grant = ClusterStore(path).load().grant("caller")

        self.assertIsNotNone(grant)
        assert grant is not None
        self.assertIsNone(grant.transaction_id)

    def test_malformed_peer_grant_is_denied(self) -> None:
        _store, path = self._store()
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "peer_grants": [
                        {
                            "caller_node_id": "caller",
                            "secret": "not-a-secret",
                            "permissions": ["dashboard_read"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(ClusterStore(path).load().peer_grants, ())

    def test_duplicate_peer_grants_are_denied_together(self) -> None:
        _store, path = self._store()
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "peer_grants": [
                        {
                            "caller_node_id": "caller",
                            "secret": "a" * 64,
                            "permissions": ["dashboard_read"],
                        },
                        {
                            "caller_node_id": "caller",
                            "secret": "b" * 64,
                            "permissions": ["dashboard_read"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(ClusterStore(path).load().peer_grants, ())

    def test_pending_pairing_round_trip(self) -> None:
        store, path = self._store()
        pending = PendingPairing(
            transaction_id="tx-1",
            caller_node_id="caller",
            identity_fingerprint="identity",
            transport_fingerprint="transport",
            secret="a" * 64,
            permissions=frozenset({NodePermission.DASHBOARD_READ}),
            expires_at=200.0,
        )

        store.save(ClusterState(pending_pairings=(pending,)))

        with patch("maintenance.cluster.time.time", return_value=100.0):
            loaded = ClusterStore(path).load()

        self.assertEqual(loaded.pending_pairings, (pending,))

    def test_old_cluster_json_defaults_pending_pairings_to_empty(self) -> None:
        _store, path = self._store()
        path.write_text(
            json.dumps({"schema_version": 1, "local_node_id": "peer"}),
            encoding="utf-8",
        )

        self.assertEqual(ClusterStore(path).load().pending_pairings, ())

    def test_expired_pending_pairings_are_pruned_on_load(self) -> None:
        _store, path = self._store()
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "pending_pairings": [
                        {
                            "transaction_id": "expired",
                            "caller_node_id": "caller",
                            "identity_fingerprint": "identity",
                            "transport_fingerprint": "transport",
                            "secret": "a" * 64,
                            "permissions": ["dashboard_read"],
                            "expires_at": 100.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch("maintenance.cluster.time.time", return_value=100.0):
            state = ClusterStore(path).load()

        self.assertEqual(state.pending_pairings, ())

    def test_malformed_pending_pairings_are_ignored_individually(self) -> None:
        _store, path = self._store()
        valid = {
            "transaction_id": "valid",
            "caller_node_id": "caller",
            "identity_fingerprint": "identity",
            "transport_fingerprint": "transport",
            "secret": "a" * 64,
            "permissions": ["dashboard_read"],
            "expires_at": 200.0,
        }
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "pending_pairings": [
                        {**valid, "secret": "not-a-secret"},
                        valid,
                        "not-an-object",
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch("maintenance.cluster.time.time", return_value=100.0):
            state = ClusterStore(path).load()

        self.assertEqual(
            tuple(item.transaction_id for item in state.pending_pairings), ("valid",)
        )

    def test_pending_lookup_rejects_expired_record(self) -> None:
        pending = PendingPairing(
            "tx-1", "caller", "identity", "transport", "a" * 64, frozenset(), 100.0
        )
        state = ClusterState(pending_pairings=(pending,))

        self.assertIsNone(state.pending_pairing("tx-1", now=100.0))
        self.assertEqual(state.pending_pairings, ())

    def test_save_failure_does_not_change_active_or_pending_state(self) -> None:
        store, _path = self._store()
        pending = PendingPairing(
            "tx-1", "caller", "identity", "transport", "a" * 64, frozenset(), 200.0
        )
        grant = PeerGrantRecord("caller", "b" * 64, frozenset())
        state = ClusterState(peer_grants=(grant,), pending_pairings=(pending,))
        with (
            patch.object(
                store, "_serialize", side_effect=ClusterSaveError("disk full")
            ),
            self.assertRaises(ClusterSaveError),
        ):
            store.save(state)

        self.assertEqual(state.peer_grants, (grant,))
        self.assertEqual(state.pending_pairings, (pending,))


if __name__ == "__main__":
    unittest.main()
