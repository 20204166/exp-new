import base64
import json
import math
import tempfile
import unittest
from pathlib import Path

from expra_connect import NodeId, NodePermission
from expra_connect.cluster import (
    CapabilityGrant,
    Cluster,
    ClusterDataError,
    ClusterRole,
    ClusterState,
    ClusterStore,
    CoordinatorEpoch,
    RoleAssignment,
    decode_invite_blob,
    encode_invite_blob,
)


class ClusterRolePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cluster.yaml"
        self.store = ClusterStore(self.path)

    def test_local_state_round_trips_role_and_epoch(self) -> None:
        state = ClusterState.create_local(local_node_id="coord")
        self.store.save(state)
        restored = self.store.load()
        self.assertEqual(restored.local_node_id, "coord")
        self.assertIn(ClusterRole.COORDINATOR, restored.local_assignment.roles)
        assert restored.coordinator_epoch is not None
        self.assertEqual(restored.coordinator_epoch.epoch, 1)

    def test_connect_rejects_legacy_v1_state_without_importing_authority(self) -> None:
        self.path.write_text(
            '{"schema_version": 1, "local_node_id": "legacy", '
            '"trusted_nodes": [], "peer_grants": []}',
            encoding="utf-8",
        )
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_has_active_job_round_trip(self) -> None:
        state = ClusterState.create_local(local_node_id="coord")
        assignment = state.local_assignment
        idle = type(assignment)(
            assignment.roles,
            node_id=assignment.node_id,
            paused=assignment.paused,
            revoked=assignment.revoked,
            has_active_job=False,
        )
        state.role_assignments = (idle,)
        self.store.save(state)
        loaded = self.store.load()
        self.assertFalse(loaded.local_assignment.has_active_job)

    def test_persisted_active_job_normalized_to_false_at_startup(self) -> None:
        # Old documents (no has_active_job field) and documents with has_active_job=true
        # both start as idle on load — no recoverable job runtime exists, so persisted
        # True would create ghost-busy workers.
        payload = {
            "schema_version": 2,
            "local_node_id": "worker",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [{"node_id": "worker", "roles": ["worker"]}],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.store.load()
        self.assertFalse(loaded.local_assignment.has_active_job)

    def test_has_active_job_false_and_paused_round_trip(self) -> None:
        state = ClusterState.create_local(local_node_id="worker")
        idle = type(state.local_assignment)(
            frozenset({ClusterRole.WORKER}),
            node_id=NodeId("worker"),
            paused=True,
            has_active_job=False,
        )
        state.role_assignments = (idle,)
        self.store.save(state)
        loaded = self.store.load()
        assignment = loaded.local_assignment
        self.assertTrue(assignment.paused)
        self.assertFalse(assignment.has_active_job)

    def test_malformed_has_active_job_field_fails_closed(self) -> None:
        # Connect rejects malformed persisted role booleans instead of repairing them.
        payload = {
            "schema_version": 2,
            "local_node_id": "worker",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [
                {"node_id": "worker", "roles": ["worker"], "has_active_job": "yes"}
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_revoked_with_no_active_job_round_trip(self) -> None:
        state = ClusterState.create_local(local_node_id="coord")
        revoked_idle = type(state.local_assignment)(
            frozenset({ClusterRole.WORKER}),
            node_id=NodeId("peer"),
            revoked=True,
            has_active_job=False,
        )
        state.role_assignments = (
            state.local_assignment,
            revoked_idle,
        )
        self.store.save(state)
        loaded = self.store.load()
        peer = next(
            item for item in loaded.role_assignments if item.node_id == NodeId("peer")
        )
        self.assertTrue(peer.revoked)
        self.assertFalse(peer.has_active_job)

    def test_coordinator_assignment_starts_idle_after_restart(self) -> None:
        # Active-job state is not preserved across restart; no recoverable job runtime exists.
        state = ClusterState.create_local(local_node_id="coord")
        self.store.save(state)
        loaded = self.store.load()
        self.assertFalse(loaded.local_assignment.has_active_job)

    def test_explicit_true_in_json_normalized_to_false_at_startup(self) -> None:
        # A persisted has_active_job=true (from a pre-fix or old session) must never
        # create a ghost-busy worker on restart since there is no recoverable job runtime.
        payload = {
            "schema_version": 2,
            "local_node_id": "worker",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [
                {"node_id": "worker", "roles": ["worker"], "has_active_job": True}
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.store.load()
        self.assertFalse(loaded.local_assignment.has_active_job)

    def test_expired_invite_is_consumed_and_rejected(self) -> None:
        state = ClusterState.create_local(local_node_id="coord", now=100.0)
        invite = state.create_invite(now=100.0, ttl_seconds=60.0)
        with self.assertRaises(ValueError):
            state.consume_invite(invite.token, now=160.1)

    def test_create_invite_binds_the_issuing_cluster_fence(self) -> None:
        state = ClusterState.create_local(local_node_id="coord")
        invite = state.create_invite(target_node_id="worker", now=100.0)
        assert state.coordinator_epoch is not None
        self.assertEqual(invite.cluster_id, state.cluster_id)
        self.assertEqual(
            invite.coordinator_id, state.coordinator_epoch.coordinator_id.value
        )
        self.assertEqual(invite.epoch, state.coordinator_epoch.epoch)
        self.assertEqual(invite.fencing_token, state.coordinator_epoch.fencing_token)

    def test_create_invite_requires_a_coordinator_epoch(self) -> None:
        state = ClusterState(local_node_id="coord")
        with self.assertRaises(PermissionError):
            state.create_invite(now=100.0)

    def test_invite_blob_round_trips(self) -> None:
        state = ClusterState.create_local(local_node_id="coord")
        invite = state.create_invite(target_node_id="worker", now=100.0)
        blob = encode_invite_blob(invite)
        decoded = decode_invite_blob(blob)
        self.assertEqual(decoded.token, invite.token)
        self.assertEqual(decoded.cluster_id, invite.cluster_id)
        self.assertEqual(decoded.coordinator_id, invite.coordinator_id)
        self.assertEqual(decoded.epoch, invite.epoch)
        self.assertEqual(decoded.fencing_token, invite.fencing_token)
        self.assertEqual(decoded.expires_at, invite.expires_at)
        self.assertEqual(decoded.target_node_id, invite.target_node_id)

    def test_decode_invite_blob_rejects_garbage(self) -> None:
        with self.assertRaises(ClusterDataError):
            decode_invite_blob("not-base64-json")

    def test_decode_invite_blob_rejects_missing_fence_fields(self) -> None:
        blob = base64.urlsafe_b64encode(
            json.dumps({"token": "abc", "expires_at": 1.0}).encode("utf-8")
        ).decode("ascii")
        with self.assertRaises(ClusterDataError):
            decode_invite_blob(blob)

    def test_invite_without_persisted_fence_fields_fails_closed(self) -> None:
        payload = {
            "schema_version": 2,
            "local_node_id": "coord",
            "trusted_nodes": [],
            "peer_grants": [],
            "active_invites": [
                {
                    "token_hash": "ab" * 32,
                    "target_node_id": "worker",
                    "expires_at": 9999999999.0,
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_legacy_invite_shape_is_rejected_on_load(self) -> None:
        payload = {
            "schema_version": 2,
            "local_node_id": "coord",
            "trusted_nodes": [],
            "peer_grants": [],
            "active_invites": [
                {
                    "token_hash": "ab" * 32,
                    "target_node_id": "worker",
                    "expires_at": float("nan"),
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_duplicate_active_coordinators_collapse_to_one_on_load(self) -> None:
        payload = {
            "schema_version": 2,
            "local_node_id": "coord",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [
                {"node_id": "coord", "roles": ["coordinator", "worker"]},
                {"node_id": "other", "roles": ["coordinator", "worker"]},
                {"node_id": "peer", "roles": ["worker"]},
            ],
            "coordinator_epoch": {
                "epoch": 1,
                "coordinator_id": "coord",
                "fencing_token": "t",
                "issued_at": 100.0,
                "lease_expires_at": 220.0,
            },
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_duplicate_active_subcoordinators_collapse_to_one_on_load(self) -> None:
        payload = {
            "schema_version": 2,
            "local_node_id": "coord",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [
                {"node_id": "first", "roles": ["subcoordinator"]},
                {"node_id": "second", "roles": ["subcoordinator"]},
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ClusterDataError):
            self.store.load()

    def test_non_finite_epoch_lease_clears_unusable_epoch(self) -> None:
        payload = {
            "schema_version": 2,
            "local_node_id": "coord",
            "trusted_nodes": [],
            "peer_grants": [],
            "role_assignments": [
                {"node_id": "coord", "roles": ["coordinator", "worker"]}
            ],
            "coordinator_epoch": {
                "epoch": 1,
                "coordinator_id": "coord",
                "fencing_token": "t",
                "issued_at": 100.0,
                "lease_expires_at": math.inf,
            },
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.store.load()
        self.assertIsNone(loaded.coordinator_epoch)

    def test_capability_grant_round_trips(self) -> None:
        grant = CapabilityGrant(
            NodeId("sub"),
            NodeId("worker"),
            frozenset({NodePermission.READ_STATE, NodePermission.CLEANUP}),
            10.0,
            100.0,
        )
        self.store.save(ClusterState(capability_grants=(grant,)))
        loaded = self.store.load()
        self.assertEqual(loaded.capability_grants, (grant,))

    def test_malformed_capability_grants_fail_closed(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "local_node_id": "coord",
                    "capability_grants": [
                        {
                            "subject": "sub",
                            "target": "worker",
                            "permissions": [],
                            "issued_at": 10.0,
                            "expires_at": 100.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ClusterDataError):
            self.store.load()


class CoordinatorAuthorityTests(unittest.TestCase):
    """Connect's live cluster predicate gates coordinator-only work."""

    def _cluster(
        self,
        local_id: str,
        *,
        role: ClusterRole = ClusterRole.COORDINATOR,
        coordinator_id: str = "coord",
        paused: bool = False,
        revoked: bool = False,
    ) -> Cluster:
        cluster = Cluster(NodeId(local_id))
        cluster.assignments = {
            NodeId(local_id): RoleAssignment(
                frozenset({role}),
                NodeId(local_id),
                paused=paused,
                revoked=revoked,
            )
        }
        cluster.epoch = CoordinatorEpoch(
            1, NodeId(coordinator_id), "synthetic-fence", 0.0, 9999.0
        )
        return cluster

    def test_active_coordinator_returns_true(self) -> None:
        self.assertTrue(self._cluster("coord").is_active_coordinator)

    def test_worker_only_returns_false(self) -> None:
        self.assertFalse(
            self._cluster("worker", role=ClusterRole.WORKER).is_active_coordinator
        )

    def test_subcoordinator_returns_false(self) -> None:
        self.assertFalse(
            self._cluster("sub", role=ClusterRole.SUBCOORDINATOR).is_active_coordinator
        )

    def test_stale_former_coordinator_returns_false(self) -> None:
        # Epoch belongs to a different (newer) coordinator — stale former node.
        self.assertFalse(
            self._cluster("old-coord", coordinator_id="new-coord").is_active_coordinator
        )

    def test_paused_coordinator_returns_false(self) -> None:
        self.assertFalse(self._cluster("coord", paused=True).is_active_coordinator)

    def test_revoked_coordinator_returns_false(self) -> None:
        self.assertFalse(self._cluster("coord", revoked=True).is_active_coordinator)

    def test_promoted_new_coordinator_returns_true(self) -> None:
        # After Phase-6 failover: new epoch with local node as coordinator_id.
        self.assertTrue(
            self._cluster("new-coord", coordinator_id="new-coord").is_active_coordinator
        )
