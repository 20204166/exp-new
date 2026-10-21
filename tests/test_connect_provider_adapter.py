from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import Mock, call

from expra_connect import NodeId as ConnectNodeId
from expra_connect.remote.models import (
    DashboardSnapshot as ConnectDashboardSnapshot,
)
from expra_connect.remote.models import (
    FileCandidate as ConnectFileCandidate,
)
from expra_connect.remote.models import (
    NodeSnapshot as ConnectNodeSnapshot,
)
from expra_connect.remote.models import (
    NodeStatus as ConnectNodeStatus,
)
from expra_connect.remote.models import (
    ProcessActionResult as ConnectProcessActionResult,
)
from expra_connect.remote.models import (
    ProcessCandidate as ConnectProcessCandidate,
)
from expra_connect.remote.models import (
    ResourceSummary as ConnectResourceSummary,
)
from expra_connect.remote.provider import AuthenticatedNodeProvider

from maintenance import connect_provider
from maintenance.components.cluster_storage import SnapshotBatch, snapshot_batch_to_dict
from maintenance.connect_provider import (
    ConnectExpClusterDataPlane,
    ConnectExpClusterOperations,
    ConnectExpHostProvider,
    ConnectExpNodeProvider,
    ConnectExpProcessActionBackend,
    ConnectProviderIdentityError,
)
from maintenance.models import (
    CapabilityState,
    DashboardSnapshot,
    FileCandidate,
    ProcessCandidate,
    ResourceSummary,
)
from maintenance.nodes import NodeId, NodeStatus


class ConnectExpNodeProviderTests(unittest.TestCase):
    def test_exp_resource_summary_mapper_preserves_fields_and_capability(self) -> None:
        resource = ResourceSummary(
            key="memory",
            title="Memory",
            value="4 GiB",
            subtitle="used",
            percent=25.0,
            details=("4 GiB used",),
            capability=CapabilityState.TEMPORARILY_UNAVAILABLE,
        )

        converted = connect_provider._exp_resource_summary_to_connect(resource)

        self.assertIsInstance(converted, ConnectResourceSummary)
        self.assertEqual(converted.key, "memory")
        self.assertEqual(converted.title, "Memory")
        self.assertEqual(converted.value, "4 GiB")
        self.assertEqual(converted.subtitle, "used")
        self.assertEqual(converted.percent, 25.0)
        self.assertEqual(converted.details, ("4 GiB used",))
        self.assertEqual(converted.capability, "temporarily_unavailable")

    def test_cluster_member_actions_use_current_connect_fence(self) -> None:
        remote = Mock()
        for name in (
            "pause_worker",
            "resume_worker",
            "revoke_member",
            "revoke_worker",
            "remove_connection",
            "remove_job",
        ):
            getattr(remote, name).return_value = {"accepted": True}
        fence = Mock(return_value=("cluster-1", 8, "synthetic-fence"))
        actions = ConnectExpClusterOperations(remote, fence)

        actions.pause_worker("worker-1")
        actions.resume_worker("worker-1")
        actions.revoke_member("worker-1")
        actions.revoke_worker("worker-1")
        actions.remove_connection("worker-1")
        actions.remove_job("worker-1")

        for name in (
            "pause_worker",
            "resume_worker",
            "revoke_member",
            "revoke_worker",
            "remove_connection",
            "remove_job",
        ):
            getattr(remote, name).assert_called_once_with(
                "worker-1",
                cluster_id="cluster-1",
                epoch=8,
                fencing_token="synthetic-fence",
            )
        self.assertEqual(fence.call_count, 6)

    def test_cluster_member_action_fails_closed_without_current_connect_fence(
        self,
    ) -> None:
        remote = Mock()
        actions = ConnectExpClusterOperations(
            remote,
            Mock(side_effect=PermissionError("Connect Coordinator unavailable")),
        )

        with self.assertRaises(PermissionError):
            actions.pause_worker("worker-1")
        remote.pause_worker.assert_not_called()

    def test_cluster_member_action_rejects_malformed_connect_response(self) -> None:
        remote = Mock()
        remote.pause_worker.return_value = None
        actions = ConnectExpClusterOperations(
            remote, Mock(return_value=("cluster-1", 8, "synthetic-fence"))
        )

        with self.assertRaises(TypeError):
            actions.pause_worker("worker-1")

    def test_cluster_provider_lease_and_capability_actions_use_fresh_fence(
        self,
    ) -> None:
        remote = Mock()
        remote.renew_coordinator_lease.return_value = {"ok": True}
        remote.grant_capabilities.return_value = {"ok": True}
        remote.sync_capability_grant.return_value = {"ok": True}
        remote.revoke_capabilities.return_value = {"ok": True}
        fence = Mock(return_value=("cluster-1", 8, "synthetic-fence"))
        actions = ConnectExpClusterOperations(remote, fence)

        actions.renew_coordinator_lease()
        actions.grant_capabilities("subject-1", "target-1", ["read"], expires_at=200.0)
        actions.sync_capability_grant(
            "subject-1", "target-1", ["read"], expires_at=200.0
        )
        actions.revoke_capabilities("subject-1", "target-1")

        remote.renew_coordinator_lease.assert_called_once_with(
            cluster_id="cluster-1", epoch=8, fencing_token="synthetic-fence"
        )
        remote.grant_capabilities.assert_called_once_with(
            "subject-1",
            "target-1",
            ["read"],
            expires_at=200.0,
            cluster_id="cluster-1",
            epoch=8,
            fencing_token="synthetic-fence",
        )
        remote.sync_capability_grant.assert_called_once_with(
            "subject-1",
            "target-1",
            ["read"],
            expires_at=200.0,
            cluster_id="cluster-1",
            epoch=8,
            fencing_token="synthetic-fence",
        )
        remote.revoke_capabilities.assert_called_once_with(
            "subject-1",
            "target-1",
            cluster_id="cluster-1",
            epoch=8,
            fencing_token="synthetic-fence",
        )
        self.assertEqual(fence.call_count, 4)

    def test_exp_cluster_data_uses_connect_authenticated_fence(self) -> None:
        remote = Mock()
        remote.invoke_surface_action.return_value = {"accepted": True}
        fence = Mock(return_value=("cluster-1", 8, "synthetic-fence"))
        data_plane = ConnectExpClusterDataPlane(remote, fence)
        snapshot = snapshot_batch_to_dict(
            SnapshotBatch("snapshot-1", NodeId("local"), 8, 1, 10.0, (), 0, "cluster-1")
        )
        standby = snapshot_batch_to_dict(
            SnapshotBatch("standby-1", NodeId("local"), 8, 2, 11.0, (), 0, "cluster-1")
        )

        self.assertEqual(data_plane.upload_snapshot(snapshot), {"accepted": True})
        self.assertEqual(data_plane.upload_standby_batch(standby), {"accepted": True})

        remote.invoke_surface_action.assert_has_calls(
            [
                call(
                    "exp.cluster-history",
                    "append_snapshot",
                    params={"batch": snapshot},
                ),
                call(
                    "exp.cluster-standby-history",
                    "append_standby_batch",
                    params={"batch": standby},
                ),
            ]
        )
        self.assertEqual(fence.call_count, 2)

    def test_exp_cluster_data_fails_closed_without_connect_fence(self) -> None:
        remote = Mock()
        fence = Mock(
            side_effect=PermissionError("current Connect Coordinator required")
        )
        data_plane = ConnectExpClusterDataPlane(remote, fence)

        with self.assertRaises(PermissionError):
            data_plane.upload_snapshot(
                snapshot_batch_to_dict(
                    SnapshotBatch(
                        "snapshot-1", NodeId("local"), 8, 1, 10.0, (), 0, "cluster-1"
                    )
                )
            )
        remote.invoke_surface_action.assert_not_called()

    def test_exp_cluster_data_rejects_batch_from_another_cluster(self) -> None:
        remote = Mock()
        data_plane = ConnectExpClusterDataPlane(
            remote, lambda: ("current-cluster", 8, "synthetic-fence")
        )
        batch = snapshot_batch_to_dict(
            SnapshotBatch("snapshot-1", NodeId("local"), 8, 1, 10.0, (), 0, "other")
        )

        with self.assertRaisesRegex(ValueError, "cluster"):
            data_plane.upload_snapshot(batch)
        remote.invoke_surface_action.assert_not_called()

    def test_exp_cluster_data_rejects_peer_surface_unsupported_response(
        self,
    ) -> None:
        remote = Mock()
        remote.invoke_surface_action.return_value = {
            "ok": False,
            "supported": False,
            "reason": "host_supplied",
        }
        data_plane = ConnectExpClusterDataPlane(
            remote, lambda: ("cluster-1", 8, "synthetic-fence")
        )
        batch = snapshot_batch_to_dict(
            SnapshotBatch("snapshot-1", NodeId("local"), 8, 1, 10.0, (), 0, "cluster-1")
        )

        with self.assertRaisesRegex(
            RuntimeError, "does not accept Exp snapshot uploads"
        ):
            data_plane.upload_snapshot(batch)

    def test_exp_cluster_data_rejects_batch_from_stale_epoch(self) -> None:
        remote = Mock()
        remote.invoke_surface_action.return_value = {"accepted": True}
        data_plane = ConnectExpClusterDataPlane(
            remote, lambda: ("cluster-1", 8, "synthetic-fence")
        )
        batch = snapshot_batch_to_dict(
            SnapshotBatch(
                "snapshot-old", NodeId("local"), 7, 1, 10.0, (), 0, "cluster-1"
            )
        )

        with self.assertRaisesRegex(ValueError, "epoch"):
            data_plane.upload_snapshot(batch)

        remote.invoke_surface_action.assert_not_called()

    def make_provider(self) -> tuple[Mock, ConnectExpNodeProvider]:
        node_id = ConnectNodeId("connect-peer")
        resource = ConnectResourceSummary(
            key="cpu",
            title="CPU",
            value="12%",
            subtitle="",
            percent=12.0,
            details=("12%",),
            actionable=False,
            failed=False,
            capability="supported",
        )
        dashboard = ConnectDashboardSnapshot(
            system_label="Peer",
            scanned_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            resources=(resource,),
        )
        source = Mock()
        source.dashboard_snapshot.return_value = dashboard
        source.node_snapshot.return_value = ConnectNodeSnapshot(
            node_id=node_id,
            display_name="Peer",
            hostname="peer-host",
            platform="Linux",
            status=ConnectNodeStatus.ONLINE,
            capabilities=frozenset(),
            scanned_at=dashboard.scanned_at,
            dashboard=dashboard,
        )
        source.component_summary.return_value = resource
        source.process_candidates.return_value = [
            ConnectProcessCandidate(
                pid=42,
                name="worker",
                memory_bytes=2048,
                memory_percent=0.2,
                cpu_percent=3.0,
                activity="active",
                username="user",
                action_allowed=True,
                create_time=100.0,
            )
        ]
        source.storage_candidates.return_value = [
            ConnectFileCandidate(
                path=Path("/tmp/cache"),
                size_bytes=50,
                modified_at=dashboard.scanned_at,
                reason="temporary",
            )
        ]
        return source, ConnectExpNodeProvider(
            cast(AuthenticatedNodeProvider, source), NodeId(node_id.value)
        )

    def test_maps_connect_snapshots_and_resource_contracts_to_exp_models(self) -> None:
        source, provider = self.make_provider()

        snapshot = provider.node_snapshot()
        dashboard = provider.dashboard_snapshot()
        summary = provider.component_summary("cpu")

        self.assertEqual(snapshot.node_id, NodeId("connect-peer"))
        self.assertEqual(snapshot.status, NodeStatus.ONLINE)
        self.assertEqual(snapshot.resources[0].capability, CapabilityState.SUPPORTED)
        self.assertEqual(dashboard.resources[0].key, "cpu")
        self.assertEqual(summary.value, "12%")
        self.assertEqual(source.component_summary.call_args.args[0], "cpu")

    def test_host_provider_maps_exp_scanner_data_to_connect_wire_models(self) -> None:
        scanned_at = datetime(2026, 10, 20, tzinfo=timezone.utc)
        resource = ResourceSummary(
            key="memory",
            title="Memory",
            value="4 GiB",
            subtitle="used",
            percent=25.0,
            details=("4 GiB used",),
            capability=CapabilityState.TEMPORARILY_UNAVAILABLE,
        )
        source = Mock()
        source.dashboard_snapshot.return_value = DashboardSnapshot(
            "Local host", scanned_at, (resource,)
        )
        source.component_summary.return_value = resource
        source.process_candidates.return_value = [
            ProcessCandidate(
                51, "worker", 1024, 1.0, 2.0, "active", "user", False, 123.0
            )
        ]
        source.storage_candidates.return_value = [
            FileCandidate(Path("/tmp/cache"), 12, scanned_at, "temporary")
        ]
        provider = ConnectExpHostProvider(source)

        dashboard = provider.dashboard_snapshot()
        summary = provider.component_summary("memory")
        processes = provider.process_candidates()
        files = provider.storage_candidates()

        self.assertEqual(dashboard.system_label, "Local host")
        self.assertEqual(dashboard.resources[0].capability, "temporarily_unavailable")
        self.assertEqual(summary.capability, "temporarily_unavailable")
        self.assertEqual(processes[0].pid, 51)
        self.assertFalse(processes[0].action_allowed)
        self.assertEqual(files[0].path, Path("/tmp/cache"))
        source.dashboard_snapshot.assert_called_once_with()
        source.component_summary.assert_called_once_with("memory")
        source.process_candidates.assert_called_once_with()
        source.storage_candidates.assert_called_once_with()

    def test_maps_process_and_storage_candidates_without_owning_actions(self) -> None:
        _source, provider = self.make_provider()

        processes = provider.process_candidates()
        files = provider.storage_candidates()

        self.assertEqual((processes[0].pid, processes[0].create_time), (42, 100.0))
        self.assertEqual(files[0].path, Path("/tmp/cache"))
        self.assertEqual(files[0].size_bytes, 50)
        self.assertFalse(hasattr(provider, "request_quit"))
        self.assertFalse(hasattr(provider, "move_to_trash"))

    def test_forwards_cancellation_and_preserves_connect_identity_check(self) -> None:
        source, provider = self.make_provider()
        cancel_event = Mock()
        source.node_snapshot.return_value = ConnectNodeSnapshot(
            node_id=ConnectNodeId("different-peer"),
            display_name="Wrong",
            hostname="wrong-host",
            platform=None,
            status=ConnectNodeStatus.ONLINE,
            capabilities=frozenset(),
            scanned_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            dashboard=None,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(ConnectProviderIdentityError, "provider identity"):
            provider.node_snapshot(cancel_event=cancel_event)
        source.node_snapshot.assert_called_once_with(
            cancel_event=cancel_event, progress_callback=None
        )

    def test_process_actions_cross_only_the_granted_connect_surface(self) -> None:
        source, _provider = self.make_provider()
        source.invoke_surface_action.return_value = {
            "requested": 1,
            "stopped": [42],
            "force_required": [],
            "errors": [],
        }
        actions = ConnectExpProcessActionBackend(
            cast(AuthenticatedNodeProvider, source)
        )

        result = actions.request_quit([42], {42: 100.0})

        self.assertEqual(result.stopped, (42,))
        source.invoke_surface_action.assert_called_once_with(
            "exp.process-termination",
            "request_quit",
            params={"pids": [42], "expected_create_times": {"42": 100.0}},
        )

    def test_process_action_batch_bound_is_preserved_at_provider(self) -> None:
        source, _provider = self.make_provider()
        source.invoke_surface_action.return_value = {
            "requested": 256,
            "stopped": [],
            "force_required": [],
            "errors": [],
        }
        actions = ConnectExpProcessActionBackend(
            cast(AuthenticatedNodeProvider, source)
        )
        pids = list(range(1, 257))

        self.assertEqual(actions.request_quit(pids).requested, 256)
        source.invoke_surface_action.assert_called_once()

        with self.assertRaisesRegex(ValueError, "invalid PIDs"):
            actions.request_quit([*pids, 257])
        source.invoke_surface_action.assert_called_once()

    def test_force_termination_uses_its_separately_grantable_surface(self) -> None:
        source, _provider = self.make_provider()
        source.invoke_surface_action.return_value = {
            "requested": 1,
            "stopped": [42],
            "force_required": [],
            "errors": [],
        }
        actions = ConnectExpProcessActionBackend(
            cast(AuthenticatedNodeProvider, source)
        )

        result = actions.force_quit([42], {42: 100.0})

        self.assertEqual(result.stopped, (42,))
        source.invoke_surface_action.assert_called_once_with(
            "exp.process-force-termination",
            "force_quit",
            params={"pids": [42], "expected_create_times": {"42": 100.0}},
        )

    def test_process_action_response_is_validated_before_returning_to_exp(self) -> None:
        source, _provider = self.make_provider()
        source.invoke_surface_action.return_value = ConnectProcessActionResult(
            requested=99,
            stopped=tuple(range(99)),
            force_required=(),
            errors=(),
        )
        actions = ConnectExpProcessActionBackend(
            cast(AuthenticatedNodeProvider, source)
        )

        with self.assertRaises(ValueError):
            actions.request_quit([42])


if __name__ == "__main__":
    unittest.main()
