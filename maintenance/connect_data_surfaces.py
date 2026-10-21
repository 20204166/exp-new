"""Exp-owned, Connect-authorized cluster history receive surfaces."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from expra_connect import NodeId as ConnectNodeId
from expra_connect.surfaces.registry import SurfaceDefinition, SurfaceHandler

from maintenance.components.cluster_storage import (
    CoordinatorTimeline,
    StandbyBuffer,
    snapshot_batch_from_dict,
)
from maintenance.connect_provider import (
    CLUSTER_SNAPSHOT_SURFACE_ID,
    CLUSTER_STANDBY_SURFACE_ID,
)
from maintenance.connect_runtime import ConnectRuntimeAdapter
from maintenance.nodes import NodeId


def register_cluster_data_surfaces(
    runtime: ConnectRuntimeAdapter,
    *,
    timeline: CoordinatorTimeline,
    standby: StandbyBuffer,
    clock: Callable[[], float] | None = None,
) -> tuple[SurfaceDefinition, SurfaceDefinition]:
    """Register bounded history ingestion; these surfaces grant no authority.

    Connect authenticates the caller and enforces the explicit action grant.
    Exp additionally checks current cluster membership, role, cluster ID and
    epoch before appending the caller-bound batch to its local bounded store.
    This is an application history policy, not an atomic cluster operation.
    """

    def read(_peer_id: ConnectNodeId, _params: dict[str, object]) -> dict[str, object]:
        return {}

    snapshot = runtime.register_surface(
        CLUSTER_SNAPSHOT_SURFACE_ID,
        read=read,
        actions={
            "append_snapshot": _append_handler(
                runtime,
                timeline,
                local_role="coordinator",
                peer_role="worker",
                clock=clock,
            )
        },
    )
    standby_batch = runtime.register_surface(
        CLUSTER_STANDBY_SURFACE_ID,
        read=read,
        actions={
            "append_standby_batch": _append_handler(
                runtime,
                standby,
                local_role="subcoordinator",
                peer_role="coordinator",
                clock=clock,
            )
        },
    )
    return snapshot, standby_batch


def _append_handler(
    runtime: ConnectRuntimeAdapter,
    store: CoordinatorTimeline | StandbyBuffer,
    *,
    local_role: str,
    peer_role: str,
    clock: Callable[[], float] | None,
) -> SurfaceHandler:
    def append(peer_id: ConnectNodeId, params: dict[str, object]) -> dict[str, object]:
        if set(params) != {"batch"}:
            raise ValueError("cluster history parameters are invalid")
        batch = snapshot_batch_from_dict(params["batch"])
        if batch.source_node_id.value != peer_id.value:
            raise PermissionError("cluster history source does not match sender")

        status = runtime.cluster_status()
        cluster_id = status.get("cluster_id")
        epoch = status.get("epoch")
        if (
            not isinstance(cluster_id, str)
            or not cluster_id
            or not isinstance(epoch, int)
            or isinstance(epoch, bool)
        ):
            raise PermissionError(
                "current Connect cluster history scope is unavailable"
            )
        if batch.cluster_id != cluster_id or batch.source_epoch != epoch:
            raise PermissionError("cluster history belongs to another cluster epoch")

        members = status.get("members")
        identity = runtime.runtime.identity if runtime.runtime is not None else None
        local_id = identity.node_id.value if identity is not None else None
        if not isinstance(members, list) or not isinstance(local_id, str):
            raise PermissionError("current Connect cluster membership is unavailable")
        local_member = _active_member(members, local_id)
        peer_member = _active_member(members, peer_id.value)
        if (
            local_member is None
            or local_role not in local_member
            or peer_member is None
            or peer_role not in peer_member
        ):
            raise PermissionError("cluster history sender or receiver role is invalid")

        stored = store.import_batch(
            batch,
            cluster_id=cluster_id,
            expected_source_node_id=NodeId(peer_id.value),
            expected_epoch=epoch,
            now=clock() if clock is not None else None,
        )
        return {"accepted": stored}

    return append


def _active_member(members: list[Any], node_id: str) -> frozenset[str] | None:
    for member in members:
        if (
            not isinstance(member, dict)
            or member.get("node_id") != node_id
            or member.get("paused") is True
            or member.get("revoked") is True
        ):
            continue
        roles = member.get("roles")
        if not isinstance(roles, list) or any(
            not isinstance(role, str) for role in roles
        ):
            return None
        return frozenset(roles)
    return None
