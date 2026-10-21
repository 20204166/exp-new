"""Secure cluster data contract, trusted-node persistence, and lifecycle glue.

This module owns the versioned ``NodeSnapshot`` envelope (node identity +
schema version + payload), the codecs for the read models that cross the
cluster boundary, and the durable ``ClusterStore`` that persists the
discovery toggle and trusted-node records (credentials, display overrides,
and optional manual host/port) atomically.

It deliberately imports no Tkinter, transport, scanner, or network code, so
the contract is safe to load in any interpreter and inside tests without a
display. The authenticated transport that consumes these envelopes lives in
``maintenance.remote``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import os
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from maintenance.components.cluster_roles import (
    CapabilityGrant,
    ClusterRole,
    CoordinatorEpoch,
    RoleAssignment,
    hash_invite,
    new_fencing_token,
)
from maintenance.components.temperature import (
    temperature_sample_from_dict,
    temperature_sample_to_dict,
)
from maintenance.models import (
    CapabilityState,
    DashboardSnapshot,
    FileCandidate,
    ProcessActionResult,
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
    generate_node_secret,
    generate_stable_node_id,
)
from maintenance.persistence import atomic_write_text, read_text_or_none
from maintenance.preferences import default_preferences_path

LOGGER = logging.getLogger(__name__)

CLUSTER_SCHEMA_VERSION = 2
CONFIG_FILE_NAME = "cluster.json"


class ClusterDataError(ValueError):
    """Raised when a serialized cluster value cannot be decoded safely."""


class ClusterSaveError(RuntimeError):
    """Raised when cluster settings could not be committed to disk."""


class InviteExpiredError(ValueError):
    """Raised when a pairing-only invite is no longer valid."""


@dataclass(frozen=True, slots=True)
class InviteRecord:
    """A one-time pairing invite, bound to the issuing cluster's live fence.

    ``cluster_id``/``coordinator_id``/``epoch``/``fencing_token`` are
    snapshotted from the issuing ``ClusterState`` at creation time. They exist
    so the joining node can learn the target's cluster fence from the invite
    itself (see ``encode_invite_blob``) -- ``RemoteService._verify_role_fence``
    requires every role operation, ``consume_invite`` included, to already
    carry the target's current cluster fence, so the invite must be what
    teaches the caller those values. Old persisted invites predating this
    field set decode with empty/zero defaults, which can never satisfy that
    fence check, so they simply keep failing closed rather than crashing.
    """

    token_hash: str
    target_node_id: str
    expires_at: float
    token: str = field(default="", repr=False, compare=False)
    cluster_id: str = ""
    coordinator_id: str = ""
    epoch: int = 0
    fencing_token: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at):
            raise ValueError("invite expiry must be finite")


def encode_invite_blob(invite: InviteRecord) -> str:
    """Serialize one invite (token + issuing cluster fence) to a copyable string."""

    payload = {
        "token": invite.token,
        "cluster_id": invite.cluster_id,
        "coordinator_id": invite.coordinator_id,
        "epoch": invite.epoch,
        "fencing_token": invite.fencing_token,
        "target_node_id": invite.target_node_id,
        "expires_at": invite.expires_at,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_invite_blob(blob: str) -> InviteRecord:
    """Parse a pasted invite blob, never trusting its contents implicitly."""

    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        raise ClusterDataError("invite is not a valid pairing invite") from error
    if not isinstance(data, dict):
        raise ClusterDataError("invite must decode to an object")
    token = data.get("token")
    cluster_id = data.get("cluster_id")
    coordinator_id = data.get("coordinator_id")
    epoch = data.get("epoch")
    fencing_token = data.get("fencing_token")
    target_node_id = data.get("target_node_id", "")
    expires_at = data.get("expires_at")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(cluster_id, str)
        or not cluster_id
        or not isinstance(coordinator_id, str)
        or not coordinator_id
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
        or not isinstance(fencing_token, str)
        or not fencing_token
        or not isinstance(target_node_id, str)
        or not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(float(expires_at))
    ):
        raise ClusterDataError("invite has malformed or missing fields")
    try:
        return InviteRecord(
            hash_invite(token),
            target_node_id,
            float(expires_at),
            token=token,
            cluster_id=cluster_id,
            coordinator_id=coordinator_id,
            epoch=epoch,
            fencing_token=fencing_token,
        )
    except ValueError as error:
        raise ClusterDataError("invite has an invalid expiry") from error


def _initial_roles(local_node_id: str) -> tuple[RoleAssignment, ...]:
    return (
        RoleAssignment(
            frozenset({ClusterRole.COORDINATOR, ClusterRole.WORKER}),
            node_id=NodeId(local_node_id),
        ),
    )


def _initial_epoch(local_node_id: str, now: float | None = None) -> CoordinatorEpoch:
    current = time.time() if now is None else now
    return CoordinatorEpoch(
        epoch=1,
        coordinator_id=NodeId(local_node_id),
        fencing_token=new_fencing_token(),
        issued_at=current,
        lease_expires_at=current + 120.0,
    )


def default_cluster_path(
    *,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the standard per-user cluster settings file.

    Reuses the platform-aware preferences directory and keeps cluster data in
    its own ``cluster.json`` beside ``preferences.json``.
    """

    return default_preferences_path(
        environment=environment,
        home=home,
        platform_name=platform_name,
    ).with_name(CONFIG_FILE_NAME)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ClusterDataError(f"{field} must be an ISO timestamp string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ClusterDataError(f"{field} is not a valid timestamp") from error


def resource_summary_to_dict(summary: ResourceSummary) -> dict[str, Any]:
    data = {
        "key": summary.key,
        "title": summary.title,
        "value": summary.value,
        "subtitle": summary.subtitle,
        "percent": summary.percent,
        "details": list(summary.details),
        "actionable": summary.actionable,
        "failed": summary.failed,
        # Keep the v1 field readable by older peers. New peers recover the
        # precise state from the additive field below.
        "capability": (
            summary.capability.value
            if summary.capability
            in (
                CapabilityState.SUPPORTED,
                CapabilityState.UNSUPPORTED,
                CapabilityState.UNKNOWN,
            )
            else CapabilityState.UNKNOWN.value
        ),
        "temperatures": [
            temperature_sample_to_dict(sample) for sample in summary.temperatures
        ],
    }
    if summary.capability not in (
        CapabilityState.SUPPORTED,
        CapabilityState.UNSUPPORTED,
        CapabilityState.UNKNOWN,
    ):
        data["capability_state"] = summary.capability.value
    return data


def resource_summary_from_dict(data: Any) -> ResourceSummary:
    if not isinstance(data, dict):
        raise ClusterDataError("resource summary must be an object")
    key = data.get("key")
    if not isinstance(key, str):
        raise ClusterDataError("resource summary key must be a string")
    capability = data.get(
        "capability_state", data.get("capability", CapabilityState.UNKNOWN.value)
    )
    try:
        state = CapabilityState(capability)
    except ValueError as error:
        raise ClusterDataError("resource summary has an unknown capability") from error
    details = data.get("details")
    if not isinstance(details, list) or not all(
        isinstance(item, str) for item in details
    ):
        raise ClusterDataError("resource summary details must be a string list")
    temperatures = data.get("temperatures", [])
    if not isinstance(temperatures, list):
        raise ClusterDataError("resource summary temperatures must be a list")
    percent = data.get("percent")
    if percent is not None and not isinstance(percent, (int, float)):
        raise ClusterDataError("resource summary percent must be a number")
    for attribute in ("title", "value", "subtitle"):
        if not isinstance(data.get(attribute), str):
            raise ClusterDataError(f"resource summary {attribute} must be a string")
    for attribute in ("actionable", "failed"):
        if not isinstance(data.get(attribute), bool):
            raise ClusterDataError(f"resource summary {attribute} must be a boolean")
    decoded_temperatures = []
    for item in temperatures:
        try:
            decoded_temperatures.append(temperature_sample_from_dict(item))
        except (TypeError, ValueError, OverflowError):
            continue
    return ResourceSummary(
        key=key,
        title=data["title"],
        value=data["value"],
        subtitle=data["subtitle"],
        percent=cast(float | None, percent),
        details=tuple(details),
        actionable=bool(data["actionable"]),
        failed=bool(data["failed"]),
        capability=state,
        temperatures=tuple(decoded_temperatures),
    )


def process_action_result_to_dict(result: ProcessActionResult) -> dict[str, Any]:
    """Encode one process-action result for the authenticated wire contract."""

    return {
        "requested": result.requested,
        "stopped": list(result.stopped),
        "force_required": list(result.force_required),
        "errors": list(result.errors),
    }


def process_action_result_from_dict(data: Any) -> ProcessActionResult:
    """Decode one process-action result without applying action policy."""

    if not isinstance(data, dict) or set(data) != {
        "requested",
        "stopped",
        "force_required",
        "errors",
    }:
        raise ClusterDataError("process action result must be an object")
    requested = data["requested"]
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
        raise ClusterDataError("process action result requested is invalid")

    def pids(value: Any, field: str) -> tuple[int, ...]:
        if not isinstance(value, list):
            raise ClusterDataError(f"process action result {field} must be a list")
        if any(
            not isinstance(pid, int) or isinstance(pid, bool) or pid < 0
            for pid in value
        ):
            raise ClusterDataError(f"process action result {field} has invalid PIDs")
        result = tuple(value)
        if len(set(result)) != len(result):
            raise ClusterDataError(f"process action result {field} has duplicate PIDs")
        return result

    stopped = pids(data["stopped"], "stopped")
    force_required = pids(data["force_required"], "force_required")
    if set(stopped).intersection(force_required):
        raise ClusterDataError("process action result has overlapping PIDs")
    if len(stopped) + len(force_required) > requested:
        raise ClusterDataError("process action result exceeds requested count")
    errors = data["errors"]
    if not isinstance(errors, list) or any(
        not isinstance(error, str) for error in errors
    ):
        raise ClusterDataError("process action result errors must be a string list")
    return ProcessActionResult(
        requested=requested,
        stopped=stopped,
        force_required=force_required,
        errors=tuple(errors),
    )


def dashboard_snapshot_to_dict(snapshot: DashboardSnapshot) -> dict[str, Any]:
    return {
        "system_label": snapshot.system_label,
        "scanned_at": _iso(snapshot.scanned_at),
        "resources": [
            resource_summary_to_dict(resource) for resource in snapshot.resources
        ],
    }


def dashboard_snapshot_from_dict(data: Any) -> DashboardSnapshot:
    if not isinstance(data, dict):
        raise ClusterDataError("dashboard snapshot must be an object")
    system_label = data.get("system_label")
    if not isinstance(system_label, str):
        raise ClusterDataError("dashboard system_label must be a string")
    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ClusterDataError("dashboard resources must be a list")
    return DashboardSnapshot(
        system_label=system_label,
        scanned_at=_parse_datetime(data.get("scanned_at"), "dashboard scanned_at"),
        resources=tuple(resource_summary_from_dict(item) for item in resources),
    )


def node_snapshot_to_dict(snapshot: NodeSnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": snapshot.schema_version,
        "node_id": snapshot.node_id.value,
        "display_name": snapshot.display_name,
        "hostname": snapshot.hostname,
        "platform": snapshot.platform,
        "status": snapshot.status.value,
        "capabilities": sorted(
            capability.value for capability in snapshot.capabilities
        ),
        "scanned_at": _iso(snapshot.scanned_at),
    }
    if snapshot.dashboard is not None:
        payload["dashboard"] = dashboard_snapshot_to_dict(snapshot.dashboard)
    return payload


def node_snapshot_from_dict(data: Any) -> NodeSnapshot:
    if not isinstance(data, dict):
        raise ClusterDataError("node snapshot must be an object")
    if data.get("schema_version") != NODE_SNAPSHOT_SCHEMA_VERSION:
        raise ClusterDataError(
            f"unsupported node snapshot schema {data.get('schema_version')!r}"
        )
    node_id = data.get("node_id")
    if not isinstance(node_id, str):
        raise ClusterDataError("node snapshot node_id must be a string")
    for attribute in ("display_name", "hostname"):
        if not isinstance(data.get(attribute), str):
            raise ClusterDataError(f"node snapshot {attribute} must be a string")
    platform = data.get("platform")
    if platform is not None and not isinstance(platform, str):
        raise ClusterDataError("node snapshot platform must be a string or null")
    try:
        status = NodeStatus(data["status"])
    except (KeyError, ValueError) as error:
        raise ClusterDataError("node snapshot status is invalid") from error
    capabilities_data = data.get("capabilities")
    if not isinstance(capabilities_data, list):
        raise ClusterDataError("node snapshot capabilities must be a list")
    capabilities: set[NodeCapability] = set()
    for raw in capabilities_data:
        if not isinstance(raw, str):
            raise ClusterDataError("node snapshot capability must be a string")
        try:
            capabilities.add(NodeCapability(raw))
        except ValueError as error:
            raise ClusterDataError("node snapshot has an unknown capability") from error
    dashboard = data.get("dashboard")
    if dashboard is not None:
        dashboard = dashboard_snapshot_from_dict(dashboard)
    return NodeSnapshot(
        node_id=NodeId(node_id),
        display_name=data["display_name"],
        hostname=data["hostname"],
        platform=platform,
        status=status,
        capabilities=frozenset(capabilities),
        scanned_at=_parse_datetime(data.get("scanned_at"), "node snapshot scanned_at"),
        dashboard=dashboard,
        schema_version=NODE_SNAPSHOT_SCHEMA_VERSION,
    )


def node_snapshot_envelope(snapshot: NodeSnapshot) -> str:
    """Serialize one node snapshot to its wire envelope text."""
    return json.dumps(node_snapshot_to_dict(snapshot), sort_keys=True) + "\n"


def parse_node_snapshot_envelope(text: str) -> NodeSnapshot:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ClusterDataError("node snapshot envelope is not valid JSON") from error
    return node_snapshot_from_dict(data)


def is_node_snapshot_compatible(version: Any) -> bool:
    return version == NODE_SNAPSHOT_SCHEMA_VERSION


def process_candidate_to_dict(process: ProcessCandidate) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "name": process.name,
        "memory_bytes": process.memory_bytes,
        "memory_percent": process.memory_percent,
        "cpu_percent": process.cpu_percent,
        "activity": process.activity,
        "username": process.username,
        "action_allowed": process.action_allowed,
        "create_time": process.create_time,
        "protected": process.protected,
        "action_state": process.action_state.value,
    }


def process_candidate_from_dict(data: Any) -> ProcessCandidate:
    if not isinstance(data, dict):
        raise ClusterDataError("process candidate must be an object")
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ClusterDataError("process candidate pid must be an integer")
    for attribute in ("name", "activity", "username"):
        if not isinstance(data.get(attribute), str):
            raise ClusterDataError(f"process candidate {attribute} must be a string")
    for attribute in ("memory_bytes", "memory_percent", "cpu_percent"):
        value = data.get(attribute)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ClusterDataError(f"process candidate {attribute} must be a number")
    create_time = data.get("create_time")
    if create_time is not None and (
        not isinstance(create_time, (int, float))
        or isinstance(create_time, bool)
        or not math.isfinite(float(create_time))
        or create_time < 0
    ):
        raise ClusterDataError("process candidate create_time must be a number")
    action_allowed = data.get("action_allowed")
    if not isinstance(action_allowed, bool):
        raise ClusterDataError("process candidate action_allowed must be a boolean")
    protected = data.get("protected", not action_allowed)
    if not isinstance(protected, bool):
        raise ClusterDataError("process candidate protected must be a boolean")
    action_state_value = data.get(
        "action_state",
        ProcessActionState.ALLOWED.value
        if action_allowed and not protected
        else ProcessActionState.PROTECTED.value,
    )
    try:
        action_state = ProcessActionState(action_state_value)
    except ValueError as error:
        raise ClusterDataError("process candidate action_state is invalid") from error
    return ProcessCandidate(
        pid=pid,
        name=data["name"],
        memory_bytes=int(data["memory_bytes"]),
        memory_percent=float(data["memory_percent"]),
        cpu_percent=float(data["cpu_percent"]),
        activity=data["activity"],
        username=data["username"],
        action_allowed=action_allowed,
        create_time=cast(float | None, create_time),
        protected=protected,
        action_state=action_state,
    )


def file_candidate_to_dict(candidate: FileCandidate) -> dict[str, Any]:
    return {
        "path": str(candidate.path),
        "size_bytes": candidate.size_bytes,
        "modified_at": _iso(candidate.modified_at),
        "reason": candidate.reason,
    }


def file_candidate_from_dict(data: Any) -> FileCandidate:
    if not isinstance(data, dict):
        raise ClusterDataError("file candidate must be an object")
    path = data.get("path")
    if not isinstance(path, str):
        raise ClusterDataError("file candidate path must be a string")
    size = data.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool):
        raise ClusterDataError("file candidate size_bytes must be an integer")
    reason = data.get("reason")
    if not isinstance(reason, str):
        raise ClusterDataError("file candidate reason must be a string")
    return FileCandidate(
        path=Path(path),
        size_bytes=size,
        modified_at=_parse_datetime(
            data.get("modified_at"), "file candidate modified_at"
        ),
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class TrustedNodeRecord:
    """One persisted trusted/authorised node plus its connection metadata.

    ``host``/``port`` record where the node's authenticated service may be
    reached (a discovered peer or a manually configured host); ``secret`` is
    the 256-bit pairing credential that authenticates every request.
    """

    node_id: str
    display_name: str
    hostname: str
    platform: str | None
    color: str | None
    host: str
    port: int | None
    capabilities: frozenset[NodeCapability]
    secret: str
    trusted_at: float
    identity_fingerprint: str | None = None
    transport_fingerprint: str | None = None
    permissions: frozenset[NodePermission] = frozenset()


@dataclass(frozen=True, slots=True)
class PeerGrantRecord:
    """Target-owned credential and permission grant for one caller."""

    caller_node_id: str
    secret: str
    permissions: frozenset[NodePermission]
    expires_at: float | None = None
    transaction_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingPairing:
    """Target-owned pairing approval awaiting initiator confirmation."""

    transaction_id: str
    caller_node_id: str
    identity_fingerprint: str
    transport_fingerprint: str
    secret: str
    permissions: frozenset[NodePermission]
    expires_at: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at):
            raise ValueError("pending pairing expiry must be finite")


@dataclass(frozen=True, slots=True)
class PendingTrustRevocation:
    """Durable outbox record for remote grant cleanup after offline revoke.

    Created when trust is revoked but the target is unreachable.  The
    ``secret`` is HIGH-SENSITIVITY: used only for the narrow ``revoke_self``
    RPC and deleted immediately on confirmed cleanup.  It must never appear
    in logs, diagnostics, or UI.
    """

    target_node_id: str
    target_host: str
    target_port: int
    target_transport_fingerprint: str
    caller_node_id: str
    secret: str
    created_at: float


@dataclass(slots=True)
class ClusterState:
    """The persisted cluster settings and trusted-node records."""

    discovery_enabled: bool = True
    trusted_nodes: tuple[TrustedNodeRecord, ...] = ()
    local_node_id: str = "local"
    local_identity_persisted: bool = True
    peer_grants: tuple[PeerGrantRecord, ...] = ()
    cluster_id: str = "local-cluster"
    role_assignments: tuple[RoleAssignment, ...] = ()
    coordinator_epoch: CoordinatorEpoch | None = None
    active_invites: tuple[InviteRecord, ...] = ()
    promotion_epochs: frozenset[int] = frozenset()
    capability_grants: tuple[CapabilityGrant, ...] = ()
    pending_pairings: tuple[PendingPairing, ...] = ()
    pending_trust_revocations: tuple[PendingTrustRevocation, ...] = ()

    @classmethod
    def create_local(cls, *, local_node_id: str | None = None) -> ClusterState:
        node_id = local_node_id or generate_stable_node_id()
        return cls(
            local_node_id=node_id,
            cluster_id=secrets.token_urlsafe(18),
            role_assignments=_initial_roles(node_id),
            coordinator_epoch=_initial_epoch(node_id),
        )

    @property
    def local_assignment(self) -> RoleAssignment:
        assignment = next(
            (
                item
                for item in self.role_assignments
                if item.node_id is not None and item.node_id.value == self.local_node_id
            ),
            None,
        )
        return assignment or RoleAssignment(
            frozenset({ClusterRole.WORKER}), node_id=NodeId(self.local_node_id)
        )

    @property
    def is_active_coordinator(self) -> bool:
        """True when the local node currently holds active Coordinator authority.

        Requires: a live coordinator_epoch whose coordinator_id matches the
        local node, a COORDINATOR role assignment that is neither paused nor
        revoked.  Any gap — no epoch, epoch belonging to a different node, or
        paused/revoked role — returns False.

        This is the gating predicate for orchestration work that only the
        current Coordinator may initiate, such as MOVABLE job placement.
        """
        if self.coordinator_epoch is None:
            return False
        assignment = self.local_assignment
        return (
            not assignment.revoked
            and not assignment.paused
            and ClusterRole.COORDINATOR in assignment.roles
            and self.coordinator_epoch.coordinator_id.value == self.local_node_id
        )

    def create_invite(
        self,
        *,
        target_node_id: str = "",
        now: float | None = None,
        ttl_seconds: float = 300.0,
    ) -> InviteRecord:
        if ttl_seconds <= 0:
            raise ValueError("invite TTL must be positive")
        if self.coordinator_epoch is None:
            raise ValueError("cluster has no coordinator epoch to bind an invite to")
        token = secrets.token_urlsafe(32)
        record = InviteRecord(
            hash_invite(token),
            target_node_id,
            (time.time() if now is None else now) + ttl_seconds,
            cluster_id=self.cluster_id,
            coordinator_id=self.coordinator_epoch.coordinator_id.value,
            epoch=self.coordinator_epoch.epoch,
            fencing_token=self.coordinator_epoch.fencing_token,
        )
        self.active_invites = self.active_invites + (record,)
        # The token is returned to the caller but is never persisted.
        return replace(record, token=token)

    def consume_invite(self, token: str, *, now: float | None = None) -> InviteRecord:
        current = time.time() if now is None else now
        token_hash = hash_invite(token)
        for record in self.active_invites:
            if record.token_hash == token_hash:
                self.active_invites = tuple(
                    item for item in self.active_invites if item != record
                )
                if current > record.expires_at:
                    raise InviteExpiredError("pairing invite has expired")
                return record
        raise InviteExpiredError("pairing invite is unknown or expired")

    def grant(self, caller_node_id: str) -> PeerGrantRecord | None:
        for grant in self.peer_grants:
            if grant.caller_node_id == caller_node_id:
                return grant
        return None

    def record(self, node_id: str) -> TrustedNodeRecord | None:
        for record in self.trusted_nodes:
            if record.node_id == node_id:
                return record
        return None

    def prune_pending_pairings(self, *, now: float | None = None) -> None:
        """Remove pending pairings that are no longer valid."""
        current = time.time() if now is None else now
        self.pending_pairings = tuple(
            pairing for pairing in self.pending_pairings if current < pairing.expires_at
        )

    def pending_pairing(
        self, transaction_id: str, *, now: float | None = None
    ) -> PendingPairing | None:
        """Return a live pending pairing, pruning expired records first."""
        self.prune_pending_pairings(now=now)
        return next(
            (
                pairing
                for pairing in self.pending_pairings
                if pairing.transaction_id == transaction_id
            ),
            None,
        )


def trusted_node_record(
    *,
    node_id: str,
    display_name: str,
    hostname: str,
    host: str,
    platform: str | None = None,
    color: str | None = None,
    port: int | None = None,
    capabilities: Iterable[NodeCapability] = (),
    secret: str | None = None,
    trusted_at: float | None = None,
    identity_fingerprint: str | None = None,
    transport_fingerprint: str | None = None,
    permissions: Iterable[NodePermission] = (),
) -> TrustedNodeRecord:
    """Build one trusted-node record, generating a fresh secret when absent."""

    return TrustedNodeRecord(
        node_id=node_id,
        display_name=display_name,
        hostname=hostname,
        platform=platform,
        color=color,
        host=host,
        port=port,
        capabilities=frozenset(capabilities),
        secret=secret or generate_node_secret(),
        trusted_at=trusted_at if trusted_at is not None else time.time(),
        identity_fingerprint=identity_fingerprint,
        transport_fingerprint=transport_fingerprint,
        permissions=frozenset(permissions),
    )


class ClusterStore:
    """Load and atomically save a ``ClusterState`` document.

    ``load()`` always returns valid state; malformed documents fall back to
    defaults (logged) and are never allowed to block startup, exactly like the
    preferences store. A later successful edit replaces them.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> ClusterState:
        text = read_text_or_none(
            self.path,
            logger=LOGGER,
            warning_template="Failed to read cluster settings: %s",
        )
        if text is None:
            state = ClusterState.create_local()
            return replace(
                state,
                local_identity_persisted=self._save_identity_migration(state),
            )
        state = self._parse(text)
        if state.local_node_id == "local":
            local_node_id = generate_stable_node_id()
            state = replace(
                state,
                local_node_id=local_node_id,
                role_assignments=_initial_roles(local_node_id),
                coordinator_epoch=_initial_epoch(local_node_id),
            )
            state = replace(
                state,
                local_identity_persisted=self._save_identity_migration(state),
            )
        elif not state.role_assignments:
            state = replace(
                state,
                role_assignments=_initial_roles(state.local_node_id),
                coordinator_epoch=_initial_epoch(state.local_node_id),
            )
        elif state.coordinator_epoch is None:
            state = replace(
                state, coordinator_epoch=_initial_epoch(state.local_node_id)
            )
        return state

    def _save_identity_migration(self, state: ClusterState) -> bool:
        try:
            self.save(state)
        except ClusterSaveError as error:
            LOGGER.warning("Could not persist local node identity: %s", error)
            return False
        return True

    def save(self, state: ClusterState) -> None:
        """Validate and persist ``state`` atomically.

        A uniquely named temporary file is written in the destination
        directory, flushed and fsynced, then committed with ``os.replace``.
        Runtime state must only be published by the caller after this returns.

        This document holds plaintext pairing/grant secrets
        (``TrustedNodeRecord.secret`` / ``PeerGrantRecord.secret``), so the
        file is explicitly restricted to the owning user after every write,
        matching the same ``chmod 0o600`` treatment already applied to the
        peer TLS private key -- not relied upon implicitly via
        ``tempfile.mkstemp``'s default mode.
        """

        payload = self._serialize(state)
        atomic_write_text(
            self.path,
            payload,
            temp_prefix=".cluster-",
            create_directory_message="Cannot create cluster settings directory",
            save_message="Failed to save cluster settings",
            save_error_factory=ClusterSaveError,
            fsync_warning_template=(
                "Cluster settings committed but directory fsync failed: %s"
            ),
            logger=LOGGER,
        )
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def _parse(self, text: str) -> ClusterState:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            LOGGER.warning("Malformed cluster settings JSON; using defaults")
            return ClusterState()
        if not isinstance(data, dict):
            LOGGER.warning("Cluster settings root must be an object; using defaults")
            return ClusterState()
        schema_version = data.get("schema_version")
        if schema_version not in (1, CLUSTER_SCHEMA_VERSION):
            LOGGER.warning("Unsupported cluster settings schema; using defaults")
            return ClusterState()
        discovery = data.get("discovery_enabled", True)
        if not isinstance(discovery, bool):
            LOGGER.warning("Cluster discovery flag is malformed; using defaults")
            return ClusterState()
        records_data = data.get("trusted_nodes", [])
        if not isinstance(records_data, list):
            LOGGER.warning("Cluster trusted nodes are malformed; using defaults")
            return ClusterState()
        records = tuple(
            record
            for item in records_data
            if (record := self._parse_record(item)) is not None
        )
        grants_data = data.get("peer_grants", [])
        grants = (
            tuple(
                grant
                for item in grants_data
                if (grant := self._parse_grant(item)) is not None
            )
            if isinstance(grants_data, list)
            else ()
        )
        grant_counts: dict[str, int] = {}
        for grant in grants:
            grant_counts[grant.caller_node_id] = (
                grant_counts.get(grant.caller_node_id, 0) + 1
            )
        grants = tuple(
            grant for grant in grants if grant_counts[grant.caller_node_id] == 1
        )
        local_node_id = data.get("local_node_id", "local")
        if not isinstance(local_node_id, str) or not local_node_id:
            LOGGER.warning("Cluster local node identity is malformed; using a new id")
            local_node_id = "local"
        role_assignments = self._parse_roles(
            data.get("role_assignments"), local_node_id
        )
        epoch = self._parse_epoch(data.get("coordinator_epoch"))
        invites = self._parse_invites(data.get("active_invites"))
        raw_promotion_epochs = data.get("promotion_epochs", [])
        promotion_epochs = (
            frozenset(
                item
                for item in raw_promotion_epochs
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0
            )
            if isinstance(raw_promotion_epochs, list)
            else frozenset()
        )
        capability_grants_data = data.get("capability_grants", [])
        capability_grants = (
            tuple(
                capability_grant
                for item in capability_grants_data
                if (capability_grant := self._parse_capability_grant(item)) is not None
            )
            if isinstance(capability_grants_data, list)
            else ()
        )
        grant_keys = [
            (item.subject.value, item.target.value) for item in capability_grants
        ]
        if len(set(grant_keys)) != len(grant_keys):
            capability_grants = ()
        pending_pairings = self._parse_pending_pairings(data.get("pending_pairings"))
        pending_trust_revocations = self._parse_pending_trust_revocations(
            data.get("pending_trust_revocations")
        )
        state = ClusterState(
            discovery_enabled=discovery,
            trusted_nodes=records,
            local_node_id=local_node_id,
            peer_grants=grants,
            cluster_id=(
                data.get("cluster_id", "local-cluster")
                if isinstance(data.get("cluster_id", "local-cluster"), str)
                else "local-cluster"
            ),
            role_assignments=role_assignments,
            coordinator_epoch=epoch,
            active_invites=invites,
            promotion_epochs=promotion_epochs,
            capability_grants=capability_grants,
            pending_pairings=pending_pairings,
            pending_trust_revocations=pending_trust_revocations,
        )
        state.prune_pending_pairings()
        return state

    @staticmethod
    def _parse_roles(value: Any, local_node_id: str) -> tuple[RoleAssignment, ...]:
        if value is None:
            return _initial_roles(local_node_id)
        if not isinstance(value, list):
            return ()
        assignments: list[RoleAssignment] = []
        exclusive_roles = {ClusterRole.COORDINATOR, ClusterRole.SUBCOORDINATOR}
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("node_id"), str):
                continue
            raw_roles = item.get("roles", [])
            if not isinstance(raw_roles, list):
                continue
            try:
                roles = frozenset(ClusterRole(raw) for raw in raw_roles)
                assignment = RoleAssignment(
                    roles,
                    node_id=NodeId(item["node_id"]),
                    paused=bool(item.get("paused", False)),
                    revoked=bool(item.get("revoked", False)),
                    has_active_job=False,  # normalized at startup; no recoverable job runtime
                )
            except (TypeError, ValueError):
                continue
            if any(
                current.roles & assignment.roles & exclusive_roles
                for current in assignments
            ):
                continue
            assignments.append(assignment)
        return tuple(assignments) or _initial_roles(local_node_id)

    @staticmethod
    def _parse_epoch(value: Any) -> CoordinatorEpoch | None:
        if not isinstance(value, dict):
            return None
        try:
            epoch = int(value["epoch"])
            issued_at = float(value["issued_at"])
            expiry = float(value["lease_expires_at"])
            coordinator = value["coordinator_id"]
            token = value["fencing_token"]
            if not isinstance(coordinator, str) or not isinstance(token, str):
                return None
            return CoordinatorEpoch(
                epoch, NodeId(coordinator), token, issued_at, expiry
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_invites(value: Any) -> tuple[InviteRecord, ...]:
        if not isinstance(value, list):
            return ()
        records: list[InviteRecord] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            token_hash = item.get("token_hash")
            target = item.get("target_node_id", "")
            expiry = item.get("expires_at")
            cluster_id = item.get("cluster_id", "")
            coordinator_id = item.get("coordinator_id", "")
            epoch = item.get("epoch", 0)
            fencing_token = item.get("fencing_token", "")
            if (
                isinstance(token_hash, str)
                and isinstance(target, str)
                and isinstance(expiry, (int, float))
                and isinstance(cluster_id, str)
                and isinstance(coordinator_id, str)
                and isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and isinstance(fencing_token, str)
            ):
                try:
                    records.append(
                        InviteRecord(
                            token_hash,
                            target,
                            float(expiry),
                            cluster_id=cluster_id,
                            coordinator_id=coordinator_id,
                            epoch=epoch,
                            fencing_token=fencing_token,
                        )
                    )
                except ValueError:
                    continue
        return tuple(records)

    @staticmethod
    def _parse_record(item: Any) -> TrustedNodeRecord | None:
        if not isinstance(item, dict):
            LOGGER.warning("Ignoring malformed trusted-node record")
            return None
        node_id = item.get("node_id")
        if not isinstance(node_id, str):
            LOGGER.warning("Ignoring trusted-node record without an id")
            return None
        for attribute in ("display_name", "hostname"):
            if not isinstance(item.get(attribute), str):
                LOGGER.warning("Ignoring malformed trusted-node record %s", node_id)
                return None
        host = item.get("host", "")
        if not isinstance(host, str):
            LOGGER.warning("Ignoring malformed trusted-node record %s", node_id)
            return None
        port = item.get("port")
        if port is not None and (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 <= port <= 65535
        ):
            LOGGER.warning("Ignoring malformed trusted-node port for %s", node_id)
            return None
        secret = item.get("secret")
        if not isinstance(secret, str) or not secret:
            LOGGER.warning("Ignoring trusted-node record without a secret: %s", node_id)
            return None
        capabilities_data = item.get("capabilities", [])
        capabilities: set[NodeCapability] = set()
        if isinstance(capabilities_data, list):
            for raw in capabilities_data:
                if isinstance(raw, str):
                    try:
                        capabilities.add(NodeCapability(raw))
                    except ValueError:
                        LOGGER.warning(
                            "Ignoring unknown capability %r for %s", raw, node_id
                        )
        platform = item.get("platform")
        if platform is not None and not isinstance(platform, str):
            platform = None
        color = item.get("color")
        if color is not None and not isinstance(color, str):
            color = None
        trusted_at = item.get("trusted_at")
        if not isinstance(trusted_at, (int, float)):
            trusted_at = time.time()
        return TrustedNodeRecord(
            node_id=node_id,
            display_name=item["display_name"],
            hostname=item["hostname"],
            platform=platform,
            color=color,
            host=host,
            port=port,
            capabilities=frozenset(capabilities),
            secret=secret,
            trusted_at=float(trusted_at),
            identity_fingerprint=(
                item.get("identity_fingerprint")
                if isinstance(item.get("identity_fingerprint"), str)
                else None
            ),
            transport_fingerprint=(
                item.get("transport_fingerprint")
                if isinstance(item.get("transport_fingerprint"), str)
                else None
            ),
            permissions=ClusterStore._parse_permissions(item.get("permissions")),
        )

    @staticmethod
    def _parse_permissions(value: Any) -> frozenset[NodePermission]:
        if value is None:
            return frozenset()
        if not isinstance(value, list):
            return frozenset()
        permissions: set[NodePermission] = set()
        for raw in value:
            if not isinstance(raw, str):
                return frozenset()
            try:
                permissions.add(NodePermission(raw))
            except ValueError:
                LOGGER.warning("Ignoring unknown node permission %r", raw)
                return frozenset()
        return frozenset(permissions)

    @staticmethod
    def _parse_grant(item: Any) -> PeerGrantRecord | None:
        if not isinstance(item, dict):
            LOGGER.warning("Ignoring malformed peer grant")
            return None
        caller = item.get("caller_node_id")
        secret = item.get("secret")
        if not isinstance(caller, str) or not caller:
            return None
        if not isinstance(secret, str) or len(secret) != 64:
            LOGGER.warning("Ignoring malformed peer grant for %s", caller)
            return None
        try:
            bytes.fromhex(secret)
        except ValueError:
            LOGGER.warning("Ignoring non-hex peer grant for %s", caller)
            return None
        raw_permissions = item.get("permissions")
        known_permissions = {permission.value for permission in NodePermission}
        if not isinstance(raw_permissions, list) or any(
            not isinstance(raw, str) or raw not in known_permissions
            for raw in raw_permissions
        ):
            LOGGER.warning("Ignoring malformed permissions for peer grant %s", caller)
            return None
        raw_expiry = item.get("expires_at")
        if raw_expiry is not None and (
            not isinstance(raw_expiry, (int, float))
            or isinstance(raw_expiry, bool)
            or not math.isfinite(float(raw_expiry))
        ):
            LOGGER.warning("Ignoring malformed expiry for peer grant %s", caller)
            return None
        transaction_id = item.get("transaction_id")
        if transaction_id is not None and (
            not isinstance(transaction_id, str) or not transaction_id
        ):
            LOGGER.warning(
                "Ignoring malformed transaction id for peer grant %s", caller
            )
            return None
        return PeerGrantRecord(
            caller_node_id=caller,
            secret=secret,
            permissions=frozenset(NodePermission(raw) for raw in raw_permissions),
            expires_at=float(raw_expiry) if raw_expiry is not None else None,
            transaction_id=transaction_id,
        )

    @staticmethod
    def _parse_pending_pairings(value: Any) -> tuple[PendingPairing, ...]:
        if not isinstance(value, list):
            return ()
        records: list[PendingPairing] = []
        for item in value:
            if not isinstance(item, dict):
                LOGGER.warning("Ignoring malformed pending pairing")
                continue
            transaction_id = item.get("transaction_id")
            caller_node_id = item.get("caller_node_id")
            identity_fingerprint = item.get("identity_fingerprint")
            transport_fingerprint = item.get("transport_fingerprint")
            secret = item.get("secret")
            permissions = item.get("permissions")
            expires_at = item.get("expires_at")
            if (
                not isinstance(transaction_id, str)
                or not transaction_id
                or not isinstance(caller_node_id, str)
                or not caller_node_id
                or not isinstance(identity_fingerprint, str)
                or not identity_fingerprint
                or not isinstance(transport_fingerprint, str)
                or not transport_fingerprint
                or not isinstance(secret, str)
                or not secret
            ):
                LOGGER.warning("Ignoring malformed pending pairing")
                continue
            if len(secret) != 64:
                LOGGER.warning("Ignoring malformed pending pairing secret")
                continue
            try:
                bytes.fromhex(secret)
            except ValueError:
                LOGGER.warning("Ignoring non-hex pending pairing secret")
                continue
            known_permissions = {permission.value for permission in NodePermission}
            if not isinstance(permissions, list) or any(
                not isinstance(permission, str) or permission not in known_permissions
                for permission in permissions
            ):
                LOGGER.warning("Ignoring malformed pending pairing permissions")
                continue
            if (
                not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not math.isfinite(float(expires_at))
            ):
                LOGGER.warning("Ignoring malformed pending pairing expiry")
                continue
            try:
                records.append(
                    PendingPairing(
                        transaction_id,
                        caller_node_id,
                        identity_fingerprint,
                        transport_fingerprint,
                        secret,
                        frozenset(
                            NodePermission(permission) for permission in permissions
                        ),
                        float(expires_at),
                    )
                )
            except (TypeError, ValueError):
                LOGGER.warning("Ignoring malformed pending pairing")
        return tuple(records)

    @staticmethod
    def _parse_pending_trust_revocations(
        value: Any,
    ) -> tuple[PendingTrustRevocation, ...]:
        if not isinstance(value, list):
            return ()
        records: list[PendingTrustRevocation] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            target_node_id = item.get("target_node_id")
            target_host = item.get("target_host")
            target_port = item.get("target_port")
            target_transport_fingerprint = item.get("target_transport_fingerprint")
            caller_node_id = item.get("caller_node_id")
            secret = item.get("secret")
            created_at = item.get("created_at")
            if (
                not isinstance(target_node_id, str)
                or not target_node_id
                or not isinstance(target_host, str)
                or not target_host
                or not isinstance(target_port, int)
                or isinstance(target_port, bool)
                or not 0 <= target_port <= 65535
                or not isinstance(target_transport_fingerprint, str)
                or not target_transport_fingerprint
                or not isinstance(caller_node_id, str)
                or not caller_node_id
                or not isinstance(secret, str)
                or len(secret) != 64
                or not isinstance(created_at, (int, float))
                or isinstance(created_at, bool)
            ):
                LOGGER.warning("Ignoring malformed pending trust revocation")
                continue
            try:
                bytes.fromhex(secret)
            except ValueError:
                LOGGER.warning("Ignoring non-hex pending trust revocation secret")
                continue
            records.append(
                PendingTrustRevocation(
                    target_node_id=target_node_id,
                    target_host=target_host,
                    target_port=target_port,
                    target_transport_fingerprint=target_transport_fingerprint,
                    caller_node_id=caller_node_id,
                    secret=secret,
                    created_at=float(created_at),
                )
            )
        return tuple(records)

    @staticmethod
    def _parse_capability_grant(item: Any) -> CapabilityGrant | None:
        if not isinstance(item, dict):
            return None
        subject = item.get("subject")
        target = item.get("target")
        permissions = item.get("permissions")
        issued_at = item.get("issued_at")
        expires_at = item.get("expires_at")
        known = {permission.value for permission in NodePermission}
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(target, str)
            or not target
            or not isinstance(permissions, list)
            or any(
                not isinstance(value, str) or value not in known
                for value in permissions
            )
            or not isinstance(issued_at, (int, float))
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
        ):
            return None
        try:
            return CapabilityGrant(
                NodeId(subject),
                NodeId(target),
                frozenset(NodePermission(value) for value in permissions),
                float(issued_at),
                float(expires_at),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _serialize(state: ClusterState) -> str:
        payload: dict[str, Any] = {
            "schema_version": CLUSTER_SCHEMA_VERSION,
            "discovery_enabled": state.discovery_enabled,
            "local_node_id": state.local_node_id,
            "cluster_id": state.cluster_id,
            "role_assignments": [
                {
                    "node_id": item.node_id.value if item.node_id is not None else "",
                    "roles": sorted(role.value for role in item.roles),
                    "paused": item.paused,
                    "revoked": item.revoked,
                    "has_active_job": item.has_active_job,
                }
                for item in state.role_assignments
                if item.node_id is not None
            ],
            "coordinator_epoch": (
                {
                    "epoch": state.coordinator_epoch.epoch,
                    "coordinator_id": state.coordinator_epoch.coordinator_id.value,
                    "fencing_token": state.coordinator_epoch.fencing_token,
                    "issued_at": state.coordinator_epoch.issued_at,
                    "lease_expires_at": state.coordinator_epoch.lease_expires_at,
                }
                if state.coordinator_epoch is not None
                else None
            ),
            "capability_grants": [
                {
                    "subject": item.subject.value,
                    "target": item.target.value,
                    "permissions": sorted(
                        permission.value for permission in item.permissions
                    ),
                    "issued_at": item.issued_at,
                    "expires_at": item.expires_at,
                }
                for item in state.capability_grants
            ],
            "active_invites": [
                {
                    "token_hash": invite.token_hash,
                    "target_node_id": invite.target_node_id,
                    "expires_at": invite.expires_at,
                    "cluster_id": invite.cluster_id,
                    "coordinator_id": invite.coordinator_id,
                    "epoch": invite.epoch,
                    "fencing_token": invite.fencing_token,
                }
                for invite in state.active_invites
            ],
            "promotion_epochs": sorted(state.promotion_epochs),
            "trusted_nodes": [
                {
                    "node_id": record.node_id,
                    "display_name": record.display_name,
                    "hostname": record.hostname,
                    "platform": record.platform,
                    "color": record.color,
                    "host": record.host,
                    "port": record.port,
                    "capabilities": sorted(
                        capability.value for capability in record.capabilities
                    ),
                    "secret": record.secret,
                    "trusted_at": record.trusted_at,
                    "identity_fingerprint": record.identity_fingerprint,
                    "transport_fingerprint": record.transport_fingerprint,
                    "permissions": sorted(
                        permission.value for permission in record.permissions
                    ),
                }
                for record in state.trusted_nodes
            ],
            "peer_grants": [
                {
                    "caller_node_id": grant.caller_node_id,
                    "secret": grant.secret,
                    "permissions": sorted(
                        permission.value for permission in grant.permissions
                    ),
                    **(
                        {"expires_at": grant.expires_at}
                        if grant.expires_at is not None
                        else {}
                    ),
                    **(
                        {"transaction_id": grant.transaction_id}
                        if grant.transaction_id is not None
                        else {}
                    ),
                }
                for grant in state.peer_grants
            ],
            "pending_pairings": [
                {
                    "transaction_id": pairing.transaction_id,
                    "caller_node_id": pairing.caller_node_id,
                    "identity_fingerprint": pairing.identity_fingerprint,
                    "transport_fingerprint": pairing.transport_fingerprint,
                    "secret": pairing.secret,
                    "permissions": sorted(
                        permission.value for permission in pairing.permissions
                    ),
                    "expires_at": pairing.expires_at,
                }
                for pairing in state.pending_pairings
            ],
            "pending_trust_revocations": [
                {
                    "target_node_id": r.target_node_id,
                    "target_host": r.target_host,
                    "target_port": r.target_port,
                    "target_transport_fingerprint": r.target_transport_fingerprint,
                    "caller_node_id": r.caller_node_id,
                    "secret": r.secret,
                    "created_at": r.created_at,
                }
                for r in state.pending_trust_revocations
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
