"""Compatibility views from Connect's authenticated provider to Exp models."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from expra_connect.remote.models import (
    DashboardSnapshot as ConnectDashboardSnapshot,
)
from expra_connect.remote.models import FileCandidate as ConnectFileCandidate
from expra_connect.remote.models import ProcessCandidate as ConnectProcessCandidate
from expra_connect.remote.models import ResourceSummary as ConnectResourceSummary
from expra_connect.remote.models import (
    file_candidate_from_dict as connect_file_candidate_from_dict,
)
from expra_connect.remote.models import (
    file_candidate_to_dict as connect_file_candidate_to_dict,
)
from expra_connect.remote.models import (
    node_snapshot_to_dict as connect_node_snapshot_to_dict,
)
from expra_connect.remote.models import (
    process_candidate_from_dict as connect_process_candidate_from_dict,
)
from expra_connect.remote.models import (
    process_candidate_to_dict as connect_process_candidate_to_dict,
)
from expra_connect.remote.models import (
    resource_summary_from_dict as connect_resource_summary_from_dict,
)
from expra_connect.remote.models import (
    resource_summary_to_dict as connect_resource_summary_to_dict,
)
from expra_connect.remote.provider import AuthenticatedNodeProvider

from maintenance.cluster import (
    dashboard_snapshot_from_dict,
    file_candidate_from_dict,
    file_candidate_to_dict,
    node_snapshot_from_dict,
    process_action_result_from_dict,
    process_candidate_from_dict,
    process_candidate_to_dict,
    resource_summary_from_dict,
    resource_summary_to_dict,
)
from maintenance.components.cluster_storage import (
    snapshot_batch_from_dict,
    snapshot_batch_to_dict,
)
from maintenance.models import (
    DashboardSnapshot,
    FileCandidate,
    ProcessActionResult,
    ProcessCandidate,
    ResourceSummary,
)
from maintenance.nodes import NodeId, NodeSnapshot

PROCESS_TERMINATION_SURFACE_ID = "exp.process-termination"
PROCESS_FORCE_TERMINATION_SURFACE_ID = "exp.process-force-termination"
CLUSTER_SNAPSHOT_SURFACE_ID = "exp.cluster-history"
CLUSTER_STANDBY_SURFACE_ID = "exp.cluster-standby-history"
PROCESS_ACTION_BATCH_LIMIT = 256
ClusterFenceProvider = Callable[[], tuple[str, int, str]]


def _exp_resource_summary_to_connect(
    summary: ResourceSummary,
) -> ConnectResourceSummary:
    payload = resource_summary_to_dict(summary)
    if "capability_state" in payload:
        payload["capability"] = payload.pop("capability_state")
    return connect_resource_summary_from_dict(payload)


class ConnectProviderIdentityError(RuntimeError):
    """Raised when Connect returns a snapshot for a different bound peer."""


class ConnectExpHostProvider:
    """Expose Exp scanner results through Connect's host-provider contract.

    Connect owns wire schemas, authenticated dispatch, and transport. Exp owns
    scanner acquisition and its domain models; this adapter translates those
    results without introducing a second network path.
    """

    def __init__(self, provider: Any) -> None:
        if provider is None:
            raise TypeError("Exp host provider is required")
        self._provider = provider

    def dashboard_snapshot(self) -> ConnectDashboardSnapshot:
        snapshot = self._provider.dashboard_snapshot()
        resources = tuple(
            _exp_resource_summary_to_connect(resource)
            for resource in snapshot.resources
        )
        return ConnectDashboardSnapshot(
            system_label=snapshot.system_label,
            scanned_at=snapshot.scanned_at,
            resources=resources,
        )

    def component_summary(self, key: str) -> ConnectResourceSummary:
        summary = self._provider.component_summary(key)
        return _exp_resource_summary_to_connect(summary)

    def process_candidates(self) -> list[ConnectProcessCandidate]:
        return [
            connect_process_candidate_from_dict(process_candidate_to_dict(item))
            for item in self._provider.process_candidates()
        ]

    def storage_candidates(self) -> list[ConnectFileCandidate]:
        return [
            connect_file_candidate_from_dict(file_candidate_to_dict(item))
            for item in self._provider.storage_candidates()
        ]


class ConnectExpClusterDataPlane:
    """Send Exp history batches through Connect-authorized Exp surfaces.

    The callback derives the current scope from the Connect runtime. The
    receiver independently checks its current membership projection and stores
    only bounded, epoch-tagged history; this is not an atomic cluster operation.
    Fence material is never transmitted, logged, or stored by this data plane.
    """

    def __init__(
        self,
        provider: AuthenticatedNodeProvider,
        current_fence: ClusterFenceProvider,
    ) -> None:
        self._provider = provider
        self._current_fence = current_fence

    def _checked_fence(self) -> tuple[str, int, str]:
        return _checked_connect_fence(self._current_fence)

    def upload_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch = snapshot_batch_from_dict(payload)
        cluster_id, epoch, _fencing_token = self._checked_fence()
        if batch.cluster_id != cluster_id:
            raise ValueError("snapshot batch belongs to a different Connect cluster")
        if batch.source_epoch != epoch:
            raise ValueError("snapshot batch belongs to a stale Connect epoch")
        response = self._provider.invoke_surface_action(
            CLUSTER_SNAPSHOT_SURFACE_ID,
            "append_snapshot",
            params={"batch": snapshot_batch_to_dict(batch)},
        )
        return _checked_upload_response(response, "snapshot")

    def upload_standby_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch = snapshot_batch_from_dict(payload)
        cluster_id, epoch, _fencing_token = self._checked_fence()
        if batch.cluster_id != cluster_id:
            raise ValueError("standby batch belongs to a different Connect cluster")
        if batch.source_epoch != epoch:
            raise ValueError("standby batch belongs to a stale Connect epoch")
        response = self._provider.invoke_surface_action(
            CLUSTER_STANDBY_SURFACE_ID,
            "append_standby_batch",
            params={"batch": snapshot_batch_to_dict(batch)},
        )
        return _checked_upload_response(response, "standby batch")


class ConnectExpClusterOperations:
    """Adapt Exp cluster UI actions to Connect's authenticated role RPCs."""

    def __init__(
        self,
        provider: AuthenticatedNodeProvider,
        current_fence: ClusterFenceProvider,
    ) -> None:
        self._provider = provider
        self._current_fence = current_fence

    def pause_worker(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("pause_worker", target_node_id)

    def resume_worker(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("resume_worker", target_node_id)

    def revoke_member(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("revoke_member", target_node_id)

    def revoke_worker(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("revoke_worker", target_node_id)

    def remove_connection(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("remove_connection", target_node_id)

    def remove_job(self, target_node_id: str) -> dict[str, Any]:
        return self._invoke("remove_job", target_node_id)

    def renew_coordinator_lease(self) -> dict[str, Any]:
        return self._invoke_fenced("renew_coordinator_lease")

    def grant_capabilities(
        self,
        subject_node_id: str,
        target_node_id: str,
        permissions: list[str],
        *,
        expires_at: float,
    ) -> dict[str, Any]:
        self._validate_grant_nodes(subject_node_id, target_node_id)
        return self._invoke_fenced(
            "grant_capabilities",
            subject_node_id,
            target_node_id,
            permissions,
            expires_at=expires_at,
        )

    def sync_capability_grant(
        self,
        subject_node_id: str,
        target_node_id: str,
        permissions: list[str],
        *,
        expires_at: float,
    ) -> dict[str, Any]:
        self._validate_grant_nodes(subject_node_id, target_node_id)
        return self._invoke_fenced(
            "sync_capability_grant",
            subject_node_id,
            target_node_id,
            permissions,
            expires_at=expires_at,
        )

    def revoke_capabilities(
        self, subject_node_id: str, target_node_id: str
    ) -> dict[str, Any]:
        self._validate_grant_nodes(subject_node_id, target_node_id)
        return self._invoke_fenced(
            "revoke_capabilities", subject_node_id, target_node_id
        )

    @staticmethod
    def _validate_grant_nodes(subject_node_id: str, target_node_id: str) -> None:
        if (
            not isinstance(subject_node_id, str)
            or not subject_node_id
            or not isinstance(target_node_id, str)
            or not target_node_id
        ):
            raise ValueError("cluster capability grant target is invalid")

    def _invoke(self, operation: str, target_node_id: str) -> dict[str, Any]:
        if not target_node_id or not isinstance(target_node_id, str):
            raise ValueError("cluster action target is invalid")
        return self._invoke_fenced(operation, target_node_id)

    def _invoke_fenced(
        self, operation: str, *args: object, **kwargs: object
    ) -> dict[str, Any]:
        cluster_id, epoch, token = _checked_connect_fence(self._current_fence)
        method = getattr(self._provider, operation)
        result = method(
            *args,
            **kwargs,
            cluster_id=cluster_id,
            epoch=epoch,
            fencing_token=token,
        )
        if not isinstance(result, dict):
            raise TypeError("Connect cluster action response must be an object")
        return result


def _checked_connect_fence(
    current_fence: ClusterFenceProvider,
) -> tuple[str, int, str]:
    cluster_id, epoch, token = current_fence()
    if (
        not cluster_id
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
        or not token
    ):
        raise PermissionError("current Connect cluster fence is unavailable")
    return cluster_id, epoch, token


def _checked_upload_response(response: object, kind: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise TypeError("Connect cluster upload response must be an object")
    if response.get("ok") is False or response.get("supported") is False:
        raise RuntimeError(f"Connect does not accept Exp {kind} uploads")
    if response.get("accepted") is not True:
        raise RuntimeError(f"Exp {kind} was not accepted by the peer")
    return response


class ConnectExpNodeProvider:
    """Expose Connect read results through Exp's existing ``NodeProvider``.

    Connect owns transport, authentication, freshness, and wire validation.
    This class only translates the package's public result models into Exp's
    established app models; destructive actions deliberately remain separate
    from the read provider.
    """

    def __init__(
        self,
        provider: AuthenticatedNodeProvider,
        node_id: NodeId,
        *,
        current_fence: ClusterFenceProvider | None = None,
    ) -> None:
        if provider is None:
            raise TypeError("Connect provider is required")
        self._provider = provider
        self._node_id = node_id
        self._cluster_data_plane = (
            ConnectExpClusterDataPlane(provider, current_fence)
            if current_fence is not None
            else None
        )

    def upload_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._cluster_data_plane is None:
            raise PermissionError("Connect cluster history upload is unavailable")
        return self._cluster_data_plane.upload_snapshot(payload)

    def upload_standby_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._cluster_data_plane is None:
            raise PermissionError("Connect standby history upload is unavailable")
        return self._cluster_data_plane.upload_standby_batch(payload)

    def dashboard_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DashboardSnapshot:
        snapshot = self._provider.dashboard_snapshot(
            cancel_event=cancel_event, progress_callback=progress_callback
        )
        return dashboard_snapshot_from_dict(
            {
                "system_label": snapshot.system_label,
                "scanned_at": snapshot.scanned_at.isoformat(),
                "resources": [
                    connect_resource_summary_to_dict(resource)
                    for resource in snapshot.resources
                ],
            }
        )

    def node_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> NodeSnapshot:
        snapshot = self._provider.node_snapshot(
            cancel_event=cancel_event, progress_callback=progress_callback
        )
        if snapshot.node_id.value != self._node_id.value:
            raise ConnectProviderIdentityError(
                "Connect provider identity does not match its bound node"
            )
        payload = connect_node_snapshot_to_dict(snapshot)
        # Exp's versioned app model includes a schema marker that is not part of
        # Connect's package model. The package itself has already validated its
        # wire schema and source identity.
        payload["schema_version"] = 1
        return node_snapshot_from_dict(payload)

    def component_summary(
        self,
        key: str,
        cancel_event: Any | None = None,
    ) -> ResourceSummary:
        summary = self._provider.component_summary(key, cancel_event=cancel_event)
        return resource_summary_from_dict(connect_resource_summary_to_dict(summary))

    def process_candidates(
        self, cancel_event: Any | None = None
    ) -> list[ProcessCandidate]:
        return [
            process_candidate_from_dict(connect_process_candidate_to_dict(item))
            for item in self._provider.process_candidates(cancel_event=cancel_event)
        ]

    def storage_candidates(
        self,
        progress_callback: Callable[[str], None] | None = None,
        cancel_event: Any | None = None,
    ) -> list[FileCandidate]:
        return [
            file_candidate_from_dict(connect_file_candidate_to_dict(item))
            for item in self._provider.storage_candidates(
                progress_callback=progress_callback, cancel_event=cancel_event
            )
        ]

    def reset_component_sample(self, key: str) -> None:
        del key

    def stop_background_workers(self) -> None:
        """Connect owns and shuts down its provider lifecycle."""

        return


class ConnectExpProcessActionBackend:
    """Route approved process actions over Connect's authorized surface API."""

    def __init__(self, provider: AuthenticatedNodeProvider) -> None:
        self._provider = provider

    def request_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> ProcessActionResult:
        return self._invoke("request_quit", pids, expected_create_times)

    def force_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> ProcessActionResult:
        return self._invoke("force_quit", pids, expected_create_times)

    def _invoke(
        self,
        action: str,
        pids: list[int],
        expected_create_times: dict[int, float] | None,
    ) -> ProcessActionResult:
        if (
            not pids
            or len(pids) > PROCESS_ACTION_BATCH_LIMIT
            or any(
                not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                for pid in pids
            )
            or len(set(pids)) != len(pids)
        ):
            raise ValueError("process action contains invalid PIDs")
        create_times = expected_create_times or {}
        if any(
            pid not in pids
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
            for pid, value in create_times.items()
        ):
            raise ValueError("process action contains invalid creation times")
        surface_id = (
            PROCESS_FORCE_TERMINATION_SURFACE_ID
            if action == "force_quit"
            else PROCESS_TERMINATION_SURFACE_ID
        )
        response = self._provider.invoke_surface_action(
            surface_id,
            action,
            params={
                "pids": pids,
                "expected_create_times": {
                    str(pid): value for pid, value in create_times.items()
                },
            },
        )
        result = process_action_result_from_dict(response)
        if result.requested != len(pids):
            raise ValueError("process action response does not match its request")
        return result
