"""Host-owned Exp action surfaces registered with Expra Connect."""

from __future__ import annotations

import math
from collections.abc import Callable

from expra_connect import NodeId as ConnectNodeId
from expra_connect.surfaces.registry import SurfaceDefinition, SurfaceHandler

from maintenance.actions import ProcessManager
from maintenance.cluster import process_action_result_to_dict
from maintenance.connect_provider import (
    PROCESS_ACTION_BATCH_LIMIT,
    PROCESS_FORCE_TERMINATION_SURFACE_ID,
    PROCESS_TERMINATION_SURFACE_ID,
)
from maintenance.connect_runtime import ConnectRuntimeAdapter
from maintenance.models import ProcessActionResult

DEMO_SHARED_CAPABILITY_ID = "exp.health-summary"


def register_demo_shared_capability(runtime: ConnectRuntimeAdapter) -> None:
    """Register a harmless, parameter-free example in Connect's share table."""

    def report_availability(
        _peer_id: ConnectNodeId, params: dict[str, object]
    ) -> dict[str, object]:
        if params:
            raise ValueError("health summary does not accept parameters")
        return {"capability": DEMO_SHARED_CAPABILITY_ID, "available": True}

    runtime.register_shared_capability(DEMO_SHARED_CAPABILITY_ID, report_availability)


def register_process_action_surface(
    runtime: ConnectRuntimeAdapter, process_manager: ProcessManager
) -> tuple[SurfaceDefinition, SurfaceDefinition]:
    """Expose existing safe process actions behind Connect's action grant.

    No peer is granted access here. The Qt host must separately ask for
    explicit action access through ``grant_surface_access``. The existing
    process manager remains the final authority for protected/foreign PIDs.
    """

    def describe(
        _peer_id: ConnectNodeId, _params: dict[str, object]
    ) -> dict[str, object]:
        return {}

    termination = runtime.register_surface(
        PROCESS_TERMINATION_SURFACE_ID,
        read=describe,
        actions={"request_quit": _process_action_handler(process_manager.request_quit)},
    )
    force_termination = runtime.register_surface(
        PROCESS_FORCE_TERMINATION_SURFACE_ID,
        read=describe,
        actions={"force_quit": _process_action_handler(process_manager.force_quit)},
    )
    return termination, force_termination


def _process_action_handler(
    operation: Callable[[list[int], dict[int, float]], ProcessActionResult],
) -> SurfaceHandler:
    def handle(_peer_id: ConnectNodeId, params: dict[str, object]) -> dict[str, object]:
        pids, expected_create_times = _validated_process_request(params)
        result = operation(pids, expected_create_times)
        return process_action_result_to_dict(result)

    return handle


def _validated_process_request(
    params: dict[str, object],
) -> tuple[list[int], dict[int, float]]:
    if set(params) != {"pids", "expected_create_times"}:
        raise ValueError("process action parameters are invalid")
    raw_pids = params["pids"]
    if (
        not isinstance(raw_pids, list)
        or not raw_pids
        or len(raw_pids) > PROCESS_ACTION_BATCH_LIMIT
        or any(
            not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            for pid in raw_pids
        )
    ):
        raise ValueError("process action PIDs are invalid")
    pids = list(raw_pids)
    if len(set(pids)) != len(pids):
        raise ValueError("process action PIDs must be unique")

    raw_times = params["expected_create_times"]
    if not isinstance(raw_times, dict):
        raise TypeError("process action creation times must be an object")
    if set(raw_times) != {str(pid) for pid in pids}:
        raise ValueError("process action creation times must match every PID")
    expected_create_times: dict[int, float] = {}
    for raw_pid, raw_time in raw_times.items():
        if (
            not isinstance(raw_pid, str)
            or not raw_pid.isdecimal()
            or str(int(raw_pid)) != raw_pid
            or int(raw_pid) not in pids
            or isinstance(raw_time, bool)
            or not isinstance(raw_time, (int, float))
            or not math.isfinite(float(raw_time))
            or raw_time < 0
        ):
            raise ValueError("process action creation times are invalid")
        expected_create_times[int(raw_pid)] = float(raw_time)
    return pids, expected_create_times
