"""Synthetic coverage for the Connect-native identity transition host adapter."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, PropertyMock, patch

from expra_connect import NodeId, RelationshipState, RuntimeState
from expra_connect.cluster import ClusterRole, InviteRecord, RoleAssignment
from expra_connect.surfaces.registry import SurfaceAccess, SurfaceGrant

from maintenance.connect_runtime import (
    IDENTITY_TRANSITION_NOTICE,
    ConnectActivationRecordError,
    ConnectProfilePathError,
    ConnectRuntimeAdapter,
    default_connect_profile_dir,
)


class ConnectRuntimeAdapterTests(unittest.TestCase):
    def test_exp_host_provider_is_installed_in_connect_service_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_provider = object()
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
                host_provider=host_provider,
            )

            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            try:
                self.assertIs(runtime.config.provider, host_provider)
                self.assertEqual(runtime.config.app_id, "expra-connect-harness")
                self.assertEqual(runtime.config.display_name, "Expra Connect")
            finally:
                adapter.shutdown()

    def test_cluster_projection_comes_from_connect_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            try:
                self.assertEqual(adapter.cluster_status(), runtime.cluster_status())
            finally:
                adapter.shutdown()

    def test_lease_renewal_uses_fresh_coordinator_fence_and_safe_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            identity = runtime.identity
            self.assertIsNotNone(identity)
            assert identity is not None
            try:
                with (
                    patch.object(
                        adapter,
                        "current_coordinator_fence",
                        return_value=("cluster-1", 7, "synthetic-only"),
                    ) as current_fence,
                    patch.object(runtime, "heartbeat", return_value=True) as heartbeat,
                    patch("time.time", return_value=123.0),
                ):
                    result = adapter.renew_coordinator_lease()

                self.assertTrue(result)
                current_fence.assert_called_once_with()
                heartbeat.assert_called_once()
                kwargs = heartbeat.call_args.kwargs
                self.assertEqual(kwargs["coordinator_id"], identity.node_id)
                self.assertIn("fencing_token", kwargs)
                self.assertIsInstance(kwargs["fencing_token"], str)
                self.assertEqual(kwargs["now"], 123.0)
            finally:
                adapter.shutdown()

    def test_cluster_surface_grant_requires_supported_mode_and_bounded_expiry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            source = NodeId("source-node")
            handler = lambda _peer, _params: None
            definition = adapter.register_surface(
                "exp.cluster-grant-test", read=handler
            )
            grant = SurfaceGrant(
                source,
                definition.surface_id,
                SurfaceAccess.READ,
                time.time() + 60,
                True,
            )
            try:
                with patch.object(
                    runtime, "grant_cluster_surface_access", return_value=grant
                ) as grant_cluster:
                    result = adapter.grant_cluster_surface_access(
                        source,
                        definition.surface_id,
                        access=SurfaceAccess.READ,
                        expires_at=grant.expires_at,
                    )
                    self.assertIs(result, grant)
                    grant_cluster.assert_called_once()

                    with self.assertRaises(ValueError):
                        adapter.grant_cluster_surface_access(
                            source,
                            definition.surface_id,
                            access=SurfaceAccess.READ,
                            expires_at=None,
                        )
                    with self.assertRaises(ValueError):
                        adapter.grant_cluster_surface_access(
                            source,
                            definition.surface_id,
                            access=SurfaceAccess.READ,
                            expires_at=time.time() + 3_601,
                        )
                    with self.assertRaises(PermissionError):
                        adapter.grant_cluster_surface_access(
                            source,
                            definition.surface_id,
                            access=SurfaceAccess.ACTION,
                            expires_at=time.time() + 60,
                        )
                    self.assertEqual(grant_cluster.call_count, 1)
            finally:
                adapter.shutdown()

    def test_cluster_provider_operations_require_authenticated_peer_and_current_fence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            remote = Mock()
            remote.pause_worker.return_value = {"ok": True}
            remote.renew_coordinator_lease.return_value = {"ok": True}
            peer_id = NodeId("worker-1")
            try:
                with (
                    patch.object(adapter, "_remote_provider", return_value=remote),
                    patch.object(
                        adapter,
                        "current_coordinator_fence",
                        return_value=("cluster-1", 4, "synthetic-only"),
                    ) as current_fence,
                ):
                    self.assertEqual(
                        adapter.cluster_member_operation("pause_worker", peer_id),
                        {"ok": True},
                    )
                    self.assertEqual(
                        adapter.renew_remote_coordinator_lease(peer_id), {"ok": True}
                    )
                    with self.assertRaises(ValueError):
                        adapter.cluster_member_operation("unknown", peer_id)

                remote.pause_worker.assert_called_once()
                remote.renew_coordinator_lease.assert_called_once()
                self.assertEqual(current_fence.call_count, 2)
            finally:
                adapter.shutdown()

    def test_peer_diagnostics_and_transport_rotation_delegate_to_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("synthetic-peer")
            diagnostics = {"runtime": "started"}
            peer_diagnostics = {"relationship": "unpaired"}
            rotation_status = runtime.status
            try:
                with (
                    patch.object(runtime, "diagnostics", return_value=diagnostics),
                    patch.object(
                        runtime,
                        "pairing_diagnostics",
                        return_value=peer_diagnostics,
                    ) as pairing_diagnostics,
                    patch.object(
                        runtime, "rotate_transport", return_value=rotation_status
                    ) as rotate_transport,
                ):
                    self.assertIs(adapter.diagnostics(), diagnostics)
                    self.assertIs(
                        adapter.pairing_diagnostics(peer_id), peer_diagnostics
                    )
                    self.assertIs(adapter.rotate_transport(), rotation_status)

                pairing_diagnostics.assert_called_once_with(peer_id)
                rotate_transport.assert_called_once_with()
            finally:
                adapter.shutdown()

    def test_node_records_are_exposed_from_connect_runtime_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            self.assertTrue(adapter.start())
            try:
                runtime = adapter.runtime
                self.assertIsNotNone(runtime)
                assert runtime is not None
                registry = runtime.registry
                identity = runtime.identity
                self.assertIsNotNone(registry)
                self.assertIsNotNone(identity)
                assert registry is not None
                assert identity is not None
                records = adapter.node_records
                self.assertEqual(records, registry.records)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].node_id, identity.node_id)
                self.assertEqual(records[0].trust.value, "authorized")
            finally:
                adapter.shutdown()

    def test_cluster_fence_is_available_only_from_active_connect_coordinator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            try:
                cluster_id, epoch, fence = adapter.current_coordinator_fence()
                self.assertTrue(cluster_id)
                self.assertEqual(epoch, 1)
                self.assertTrue(fence)
            finally:
                adapter.shutdown()

    def test_peer_trust_revocation_is_delegated_to_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("synthetic-peer")
            try:
                with patch.object(runtime, "revoke_peer") as revoke_peer:
                    adapter.revoke_peer(peer_id)
                revoke_peer.assert_called_once_with(peer_id)
            finally:
                adapter.shutdown()

    def test_peer_repair_is_delegated_to_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("synthetic-peer")
            repaired = object()
            try:
                with patch.object(
                    runtime, "repair_peer", return_value=repaired
                ) as repair_peer:
                    self.assertIs(adapter.repair_peer(peer_id), repaired)
                repair_peer.assert_called_once_with(
                    peer_id,
                    permissions=frozenset({"read_state"}),
                    cancel_event=None,
                )
            finally:
                adapter.shutdown()

    def test_app_surface_registration_is_owned_by_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None

            def read_status(
                peer_id: NodeId, _params: dict[str, object]
            ) -> dict[str, object]:
                return {"requester": peer_id.value}

            try:
                definition = adapter.register_surface("exp.status", read=read_status)
                self.assertEqual(definition.surface_id, "exp.status")
                self.assertEqual(
                    adapter.surface_definitions, {"exp.status": definition}
                )
                with self.assertRaises(TypeError):
                    cast(dict[str, object], adapter.surface_definitions)["other"] = (
                        definition
                    )
                grant = adapter.grant_surface_access(
                    NodeId("synthetic-peer"), "exp.status", access="read"
                )
                self.assertEqual(grant.surface_id, "exp.status")
                self.assertEqual(grant.source, NodeId("synthetic-peer"))
                adapter.revoke_surface_access(NodeId("synthetic-peer"), "exp.status")
            finally:
                adapter.shutdown()
            self.assertEqual(adapter.surface_definitions, {})

    def test_peer_lifecycle_facade_projects_and_delegates_to_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            try:
                self.assertEqual(adapter.status, runtime.status)
                self.assertEqual(adapter.peers, runtime.peers)
                self.assertEqual(adapter.connections, runtime.connections)

                peer_id = NodeId("synthetic-peer")
                trusted_peer = object()
                connected = object()
                with (
                    patch.object(
                        runtime, "pair_peer", return_value=trusted_peer
                    ) as pair_peer,
                    patch.object(
                        runtime, "connect_peer", return_value=connected
                    ) as connect_peer,
                ):
                    self.assertIs(adapter.pair_peer(peer_id), trusted_peer)
                    self.assertIs(adapter.connect_peer(peer_id), connected)

                pair_peer.assert_called_once_with(peer_id)
                connect_peer.assert_called_once_with(peer_id)
            finally:
                adapter.shutdown()

    def test_remote_read_facade_requires_authorized_live_peer_and_delegates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("remote-peer")
            provider = Mock()
            provider.dashboard_snapshot.return_value = {"ok": True}
            try:
                with (
                    patch.object(
                        type(runtime),
                        "connections",
                        new_callable=PropertyMock,
                        return_value=(peer_id,),
                    ),
                    patch.object(
                        runtime,
                        "peer_relationship",
                        return_value=RelationshipState.OUTBOUND_TRUSTED,
                    ),
                    patch.object(
                        runtime, "connect_peer", return_value=provider
                    ) as connect_peer,
                ):
                    self.assertEqual(
                        adapter.remote_dashboard_snapshot(peer_id), {"ok": True}
                    )
                connect_peer.assert_called_once_with(peer_id)
                provider.dashboard_snapshot.assert_called_once_with(None)

                with (
                    patch.object(
                        type(runtime),
                        "connections",
                        new_callable=PropertyMock,
                        return_value=(),
                    ),
                    patch.object(
                        runtime,
                        "peer_relationship",
                        return_value=RelationshipState.OUTBOUND_TRUSTED,
                    ),
                    patch.object(runtime, "connect_peer") as connect_peer,
                    self.assertRaises(ConnectionError),
                ):
                    adapter.remote_dashboard_snapshot(peer_id)
                connect_peer.assert_not_called()
            finally:
                adapter.shutdown()

    def test_shared_capability_facade_delegates_to_connect_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("capability-peer")

            def handler(_peer: NodeId, _params: dict[str, object]) -> object:
                return {"available": True}

            try:
                with patch.object(
                    runtime,
                    "peer_relationship",
                    return_value=RelationshipState.INBOUND_GRANTED,
                ):
                    adapter.register_shared_capability("exp.health-summary", handler)
                    adapter.allow_shared_capability(peer_id, "exp.health-summary")

                self.assertEqual(
                    runtime.sharing.request(peer_id, "exp.health-summary"),
                    {"available": True},
                )
                adapter.revoke_shared_capability(peer_id, "exp.health-summary")
                with self.assertRaises(PermissionError):
                    runtime.sharing.request(peer_id, "exp.health-summary")

                with patch.object(
                    runtime,
                    "peer_relationship",
                    return_value=RelationshipState.INBOUND_GRANTED,
                ):
                    adapter.allow_shared_capability(peer_id, "exp.health-summary")
                adapter.revoke_peer_shared_capabilities(peer_id)
                with self.assertRaises(PermissionError):
                    runtime.sharing.request(peer_id, "exp.health-summary")
            finally:
                adapter.shutdown()

    def test_remote_process_actions_use_typed_authenticated_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("remote-peer")
            provider = Mock()
            provider.invoke_surface_action.return_value = {
                "requested": 1,
                "stopped": [321],
                "force_required": [],
                "errors": [],
            }
            try:
                with (
                    patch.object(
                        type(runtime),
                        "connections",
                        new_callable=PropertyMock,
                        return_value=(peer_id,),
                    ),
                    patch.object(
                        runtime,
                        "peer_relationship",
                        return_value=RelationshipState.OUTBOUND_TRUSTED,
                    ),
                    patch.object(runtime, "connect_peer", return_value=provider),
                ):
                    result = adapter.remote_request_quit(
                        peer_id, [321], expected_create_times={321: 123.5}
                    )

                self.assertEqual(result.stopped, (321,))
                provider.invoke_surface_action.assert_called_once_with(
                    "exp.process-termination",
                    "request_quit",
                    params={"pids": [321], "expected_create_times": {"321": 123.5}},
                )
            finally:
                adapter.shutdown()

    def test_remote_surface_reads_are_limited_to_registered_host_definitions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("remote-peer")
            provider = Mock()
            provider.read_surface.return_value = {"items": []}
            provider.review_surface.return_value = {"review": "ok"}

            def handler(_peer: NodeId, _params: dict[str, object]) -> dict[str, object]:
                return {}

            try:
                adapter.register_surface("exp.status", read=handler, review=handler)
                with (
                    patch.object(
                        type(runtime),
                        "connections",
                        new_callable=PropertyMock,
                        return_value=(peer_id,),
                    ),
                    patch.object(
                        runtime,
                        "peer_relationship",
                        return_value=RelationshipState.OUTBOUND_TRUSTED,
                    ),
                    patch.object(runtime, "connect_peer", return_value=provider),
                ):
                    self.assertEqual(
                        adapter.remote_read_surface(peer_id, "exp.status"),
                        {"items": []},
                    )
                    self.assertEqual(
                        adapter.remote_review_surface(peer_id, "exp.status"),
                        {"review": "ok"},
                    )
                    with self.assertRaises(ValueError):
                        adapter.remote_read_surface(peer_id, "unregistered")

                provider.read_surface.assert_called_once_with("exp.status", params={})
                provider.review_surface.assert_called_once_with("exp.status", params={})
            finally:
                adapter.shutdown()

    def test_package_cluster_and_reconnect_operations_are_delegated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ConnectRuntimeAdapter(
                profile_dir=root / "connect" / "profile",
                activation_path=root / "connect" / "activation.json",
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            peer_id = NodeId("synthetic-peer")
            provider = object()
            invite = InviteRecord("hash", peer_id.value, 200.0)
            assignment = RoleAssignment(frozenset({ClusterRole.WORKER}), peer_id)
            fence = "synthetic-fence-not-for-logging"
            try:
                with (
                    patch.object(
                        runtime, "create_invite", return_value=invite
                    ) as create,
                    patch.object(
                        runtime, "join_cluster", return_value={"joined": True}
                    ) as join,
                    patch.object(runtime, "heartbeat", return_value=True) as heartbeat,
                    patch.object(
                        runtime, "promote_if_due", return_value=False
                    ) as promote,
                    patch.object(runtime, "rejoin_as_worker") as rejoin,
                    patch.object(
                        runtime, "reconnect_peer", return_value=provider
                    ) as reconnect,
                    patch.object(runtime, "disconnect_peer") as disconnect,
                    patch.object(
                        runtime, "assign_role", return_value=assignment
                    ) as assign,
                ):
                    self.assertIs(adapter.create_invite(peer_id), invite)
                    self.assertEqual(
                        adapter.join_cluster(provider, invite), {"joined": True}
                    )
                    self.assertTrue(adapter.heartbeat(peer_id, fence, now=123.0))
                    self.assertFalse(adapter.promote_if_due(now=124.0))
                    adapter.rejoin_as_worker(peer_id, current_epoch=4)
                    self.assertIs(adapter.reconnect_peer(peer_id), provider)
                    adapter.disconnect_peer(peer_id)
                    self.assertIs(
                        adapter.assign_role(peer_id, ClusterRole.WORKER), assignment
                    )

                create.assert_called_once_with(peer_id, ttl_seconds=300.0)
                join.assert_called_once_with(provider, invite)
                heartbeat.assert_called_once_with(
                    coordinator_id=peer_id, fencing_token=fence, now=123.0
                )
                promote.assert_called_once_with(now=124.0)
                rejoin.assert_called_once_with(node_id=peer_id, current_epoch=4)
                reconnect.assert_called_once_with(peer_id)
                disconnect.assert_called_once_with(peer_id, reason="disconnected")
                assign.assert_called_once_with(peer_id, ClusterRole.WORKER)
            finally:
                adapter.shutdown()

    def test_cluster_role_changes_use_connect_and_survive_adapter_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "connect" / "profile"
            activation = root / "connect" / "activation.json"
            adapter = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=activation,
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: True,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            self.assertTrue(adapter.start())
            assignment = adapter.assign_role(
                NodeId("synthetic-worker"), ClusterRole.WORKER
            )
            self.assertEqual(assignment.node_id, NodeId("synthetic-worker"))
            adapter.revoke_member(NodeId("synthetic-worker"))
            adapter.shutdown()

            resumed = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=activation,
                legacy_state_paths=(root / "legacy-cluster.json",),
                confirm_identity_transition=lambda _notice: self.fail(
                    "an accepted profile must not repeat the identity transition"
                ),
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(resumed.start())
            runtime = resumed.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            cluster = runtime.cluster
            self.assertIsNotNone(cluster)
            assert cluster is not None
            assignment = cluster.assignments[NodeId("synthetic-worker")]
            self.assertTrue(assignment.revoked)
            resumed.shutdown()

    def test_default_profile_is_separate_from_legacy_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preferences = (
                Path(directory) / "config" / "system-analyzer" / "preferences.json"
            )

            profile = default_connect_profile_dir(preferences)

            self.assertNotEqual(profile, preferences.parent)
            self.assertNotEqual(profile, preferences.parent / "cluster.json")
            self.assertNotEqual(profile.parent, preferences.parent)

    def test_declining_transition_does_not_create_profile_or_touch_legacy_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy" / "cluster.json"
            legacy.parent.mkdir()
            original = b'{"identity":"legacy-node","trust":["old-peer"]}\n'
            legacy.write_bytes(original)
            profile = root / "connect" / "profile"
            notices: list[str] = []

            def decline(notice: str) -> bool:
                notices.append(notice)
                return False

            adapter = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=profile.parent / "activation.json",
                legacy_state_paths=(legacy,),
                confirm_identity_transition=decline,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            started = adapter.start()

            self.assertFalse(started)
            self.assertEqual(notices, [IDENTITY_TRANSITION_NOTICE])
            self.assertFalse(profile.exists())
            self.assertFalse((profile.parent / "activation.json").exists())
            self.assertEqual(legacy.read_bytes(), original)

    def test_accepting_creates_native_identity_without_importing_legacy_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy" / "cluster.json"
            legacy.parent.mkdir()
            original = json.dumps(
                {
                    "local_node_id": "legacy-node",
                    "trusted_nodes": [{"node_id": "legacy-peer", "secret": "legacy"}],
                    "peer_grants": [
                        {"caller_node_id": "legacy-peer", "secret": "legacy"}
                    ],
                    "cluster_id": "legacy-cluster",
                }
            ).encode()
            legacy.write_bytes(original)
            profile = root / "connect" / "profile"
            notices: list[str] = []

            def approve(notice: str) -> bool:
                notices.append(notice)
                return True

            adapter = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=profile.parent / "activation.json",
                legacy_state_paths=(legacy,),
                confirm_identity_transition=approve,
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            self.assertTrue(adapter.start())
            runtime = adapter.runtime
            self.assertIsNotNone(runtime)
            assert runtime is not None
            identity = runtime.device_identity
            self.assertIsNotNone(identity)
            assert identity is not None
            self.assertIs(runtime.status.state, RuntimeState.STARTED)
            self.assertNotEqual(identity.node_id.value, "legacy-node")
            self.assertIs(
                runtime.peer_relationship(NodeId("legacy-peer")),
                RelationshipState.UNPAIRED,
            )
            cluster = runtime.cluster_status()
            self.assertNotEqual(cluster["cluster_id"], "legacy-cluster")
            self.assertEqual(
                {member["node_id"] for member in cluster["members"]},
                {identity.node_id.value},
            )
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual(notices, [IDENTITY_TRANSITION_NOTICE])
            identity_id = identity.node_id.value
            adapter.shutdown()

            resumed = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=profile.parent / "activation.json",
                legacy_state_paths=(legacy,),
                confirm_identity_transition=lambda _notice: self.fail(
                    "an accepted Connect identity must not ask to transition again"
                ),
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )
            self.assertTrue(resumed.start())
            resumed_runtime = resumed.runtime
            self.assertIsNotNone(resumed_runtime)
            assert resumed_runtime is not None
            resumed_identity = resumed_runtime.device_identity
            self.assertIsNotNone(resumed_identity)
            assert resumed_identity is not None
            self.assertEqual(resumed_identity.node_id.value, identity_id)
            self.assertEqual(legacy.read_bytes(), original)
            resumed.shutdown()

    def test_profile_path_must_not_overlap_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory) / "legacy"
            legacy_file = legacy_root / "cluster.json"
            legacy_root.mkdir()

            with self.assertRaises(ConnectProfilePathError):
                ConnectRuntimeAdapter(
                    profile_dir=legacy_root,
                    activation_path=Path(directory) / "activation.json",
                    legacy_state_paths=(legacy_file,),
                    confirm_identity_transition=lambda _notice: True,
                    bind_host="127.0.0.1",
                    preferred_port=0,
                    discovery_enabled=False,
                )

    def test_activation_record_must_not_overlap_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_file = root / "legacy" / "cluster.json"

            with self.assertRaises(ConnectProfilePathError):
                ConnectRuntimeAdapter(
                    profile_dir=root / "connect" / "profile",
                    activation_path=legacy_file,
                    legacy_state_paths=(legacy_file,),
                    confirm_identity_transition=lambda _notice: True,
                    bind_host="127.0.0.1",
                    preferred_port=0,
                    discovery_enabled=False,
                )

    def test_profile_symlink_is_rejected_before_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            linked_profile = root / "connect-profile"
            linked_profile.symlink_to(legacy_root, target_is_directory=True)

            with self.assertRaises(ConnectProfilePathError):
                ConnectRuntimeAdapter(
                    profile_dir=linked_profile,
                    activation_path=root / "activation.json",
                    legacy_state_paths=(legacy_root / "cluster.json",),
                    confirm_identity_transition=lambda _notice: True,
                    bind_host="127.0.0.1",
                    preferred_port=0,
                    discovery_enabled=False,
                )

    def test_malformed_activation_record_fails_closed_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "connect" / "profile"
            profile.mkdir(parents=True)
            activation = root / "connect" / "activation.json"
            activation.write_text("not-json", encoding="utf-8")
            legacy = root / "legacy.json"
            legacy.write_bytes(b"legacy recovery bytes")
            adapter = ConnectRuntimeAdapter(
                profile_dir=profile,
                activation_path=activation,
                legacy_state_paths=(legacy,),
                confirm_identity_transition=lambda _notice: self.fail(
                    "must not ask to replace an unverified identity"
                ),
                bind_host="127.0.0.1",
                preferred_port=0,
                discovery_enabled=False,
            )

            with self.assertRaises(ConnectActivationRecordError):
                adapter.start()

            self.assertEqual(activation.read_text(encoding="utf-8"), "not-json")
            self.assertEqual(legacy.read_bytes(), b"legacy recovery bytes")


if __name__ == "__main__":
    unittest.main()
