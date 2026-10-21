"""Exp-owned lifecycle adapter for one Connect-native profile and identity."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import Any

from expra_connect import (
    DiscoveredNodeCandidate,
    NodeId,
    RelationshipState,
    RuntimeStatus,
    TrustedPeer,
)
from expra_connect.cluster import ClusterRole, InviteRecord, RoleAssignment
from expra_connect.runtime import ConnectConfig, ConnectRuntime, RuntimeState
from expra_connect.surfaces.registry import (
    SurfaceAccess,
    SurfaceDefinition,
    SurfaceGrant,
    SurfaceHandler,
)

from maintenance.connect_provider import (
    ConnectExpClusterOperations,
    ConnectExpProcessActionBackend,
)
from maintenance.persistence import atomic_write_text
from maintenance.preferences import default_preferences_path

LOGGER = logging.getLogger(__name__)

IDENTITY_TRANSITION_NOTICE = (
    "Expra Connect will create a new identity for this installation. Existing "
    "peers will not be trusted or transferred and must explicitly pair again. "
    "Legacy identity, trust, and cluster files will be kept unchanged for "
    "recovery; existing cluster membership must be re-established."
)
MAX_CLUSTER_SURFACE_GRANT_SECONDS = 3_600.0
_ACTIVATION_SCHEMA_VERSION = 1
_PROFILE_DIRECTORY_NAME = "profile"
_PROFILE_IDENTITY_FILES = ("identity.yaml", "device_identity.yaml")


class ConnectProfilePathError(ValueError):
    """Raised when the Connect profile could overlap legacy Exp state."""


class ConnectActivationRecordError(RuntimeError):
    """Raised when durable approval cannot safely identify the active profile."""


class ConnectRuntimeStartError(RuntimeError):
    """Raised when Connect does not reach its started state."""


def default_connect_profile_dir(preferences_path: Path | None = None) -> Path:
    """Return a new Connect-owned profile path beside, not inside, legacy state."""

    preferences = (
        default_preferences_path() if preferences_path is None else preferences_path
    )
    return (
        preferences.parent.parent / "system-analyzer-connect" / _PROFILE_DIRECTORY_NAME
    )


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _activation_payload(profile_dir: Path, node_id: str) -> str:
    return (
        json.dumps(
            {
                "schema_version": _ACTIVATION_SCHEMA_VERSION,
                "transition": "connect-native-identity",
                "profile_dir": str(profile_dir),
                "node_id": node_id,
            },
            sort_keys=True,
        )
        + "\n"
    )


class ConnectRuntimeAdapter:
    """Start/shut down Connect only after explicit new-identity approval.

    Exp contributes the Qt approval callback and host configuration. It never
    copies legacy identity, trust, grants, invitations, or cluster state into
    the Connect profile.
    """

    def __init__(
        self,
        *,
        profile_dir: Path,
        activation_path: Path,
        legacy_state_paths: Sequence[Path],
        confirm_identity_transition: Callable[[str], bool],
        bind_host: str = "0.0.0.0",
        preferred_port: int | None = None,
        discovery_enabled: bool = True,
        service_type: str = "_expra-peer._tcp.local.",
        on_pairing_request: Callable[[Any], bool] | None = None,
        host_provider: Any | None = None,
        runtime_factory: Callable[[ConnectConfig], ConnectRuntime] = ConnectRuntime,
    ) -> None:
        if profile_dir.is_symlink() or activation_path.is_symlink():
            raise ConnectProfilePathError(
                "Connect activation paths must not be symlinks"
            )
        self._profile_dir = _resolved(profile_dir)
        self._activation_path = _resolved(activation_path)
        self._confirm_identity_transition = confirm_identity_transition
        self._bind_host = bind_host
        self._preferred_port = preferred_port
        self._discovery_enabled = discovery_enabled
        self._service_type = service_type
        self._on_pairing_request = on_pairing_request
        self._host_provider = host_provider
        self._runtime_factory = runtime_factory
        self._runtime: ConnectRuntime | None = None
        self._surface_definitions: dict[str, SurfaceDefinition] = {}
        self._legacy_paths = tuple(_resolved(path) for path in legacy_state_paths)
        self._validate_paths()

    @property
    def runtime(self) -> ConnectRuntime | None:
        """Return the package runtime while active; Connect remains its owner."""

        return self._runtime

    @property
    def started(self) -> bool:
        return self._runtime is not None and self._runtime.started

    @property
    def surface_definitions(self) -> Mapping[str, SurfaceDefinition]:
        """Return a read-only snapshot of Exp surfaces registered this runtime."""

        return MappingProxyType(dict(self._surface_definitions))

    def _validate_paths(self) -> None:
        if _paths_overlap(self._profile_dir, self._activation_path):
            raise ConnectProfilePathError(
                "Connect profile and Exp activation record must be separate"
            )
        if self._profile_dir == self._activation_path.parent:
            raise ConnectProfilePathError(
                "Exp activation record must not be inside the Connect profile"
            )
        for legacy_path in self._legacy_paths:
            if _paths_overlap(self._profile_dir, legacy_path) or _paths_overlap(
                self._activation_path, legacy_path
            ):
                raise ConnectProfilePathError(
                    "Connect profile and activation record must not overlap legacy Exp state"
                )

    def _read_accepted_node_id(self) -> str | None:
        if not self._activation_path.exists():
            if self._profile_dir.exists() and any(self._profile_dir.iterdir()):
                raise ConnectActivationRecordError(
                    "unrecorded Connect profile exists; refusing implicit identity adoption"
                )
            return None
        try:
            value: object = json.loads(
                self._activation_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConnectActivationRecordError(
                "Connect activation record is unreadable; refusing profile startup"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "transition",
            "profile_dir",
            "node_id",
        }:
            raise ConnectActivationRecordError(
                "Connect activation record has an unsupported shape"
            )
        if (
            value["schema_version"] != _ACTIVATION_SCHEMA_VERSION
            or value["transition"] != "connect-native-identity"
            or value["profile_dir"] != str(self._profile_dir)
            or not isinstance(value["node_id"], str)
            or not value["node_id"]
        ):
            raise ConnectActivationRecordError(
                "Connect activation record does not match this profile"
            )
        if not self._profile_dir.is_dir() or any(
            not (self._profile_dir / name).is_file()
            or (self._profile_dir / name).is_symlink()
            for name in _PROFILE_IDENTITY_FILES
        ):
            raise ConnectActivationRecordError(
                "Connect profile identity files are incomplete; refusing replacement"
            )
        return value["node_id"]

    def start(self) -> bool:
        """Start Connect, asking before the first new identity is created.

        Return ``False`` when the user declines. All profile writes are directed
        to ``profile_dir``; legacy paths are validated but never read or changed.
        """

        if self.started:
            return True
        previous_node_id = self._read_accepted_node_id()
        if (
            previous_node_id is None
            and self._confirm_identity_transition(IDENTITY_TRANSITION_NOTICE)
            is not True
        ):
            return False

        self._profile_dir.parent.mkdir(parents=True, exist_ok=True)
        config_options: dict[str, Any] = {
            "app_id": "expra-connect-harness",
            "display_name": "Expra Connect",
            "bind_host": self._bind_host,
            "discovery_enabled": self._discovery_enabled,
            "service_type": self._service_type,
            "cluster_enabled": True,
            "provider": self._host_provider,
            "on_pairing_request": self._on_pairing_request,
        }
        if self._preferred_port is not None:
            config_options["preferred_port"] = self._preferred_port
        config = ConnectConfig(self._profile_dir, **config_options)
        runtime = self._runtime_factory(config)
        try:
            status = runtime.start()
            if status.state is not RuntimeState.STARTED:
                raise ConnectRuntimeStartError(
                    "Expra Connect did not reach the started state"
                )
            identity = runtime.device_identity
            if identity is None:
                raise ConnectRuntimeStartError(
                    "Expra Connect started without a device identity"
                )
            node_id = identity.node_id.value
            if previous_node_id is not None and node_id != previous_node_id:
                raise ConnectActivationRecordError(
                    "Connect identity changed unexpectedly; profile startup was stopped"
                )
            if previous_node_id is None:
                atomic_write_text(
                    self._activation_path,
                    _activation_payload(self._profile_dir, node_id),
                    temp_prefix=".connect-activation-",
                    create_directory_message="Could not create Connect activation directory",
                    save_message="Could not save Connect activation record",
                    save_error_factory=ConnectActivationRecordError,
                    fsync_warning_template="Could not sync Connect activation directory: %s",
                    logger=LOGGER,
                )
        except Exception:
            # Preserve the startup error; a secondary shutdown error must not
            # hide the original failure while the adapter stays unstarted.
            with suppress(Exception):
                runtime.shutdown()
            raise
        self._runtime = runtime
        return True

    def peer_relationship(self, peer_id: NodeId) -> RelationshipState:
        """Report Connect's package-owned relationship state for a peer."""

        return self._require_runtime().peer_relationship(peer_id)

    def diagnostics(self) -> dict[str, Any]:
        """Return Connect's secret-free runtime diagnostics projection."""

        return self._require_runtime().diagnostics()

    def pairing_diagnostics(self, peer_id: NodeId) -> dict[str, Any]:
        """Return Connect's redacted directional diagnostics for one peer."""

        return self._require_runtime().pairing_diagnostics(peer_id)

    def rotate_transport(self) -> RuntimeStatus:
        """Rotate Connect's transport identity through its public runtime API."""

        return self._require_runtime().rotate_transport()

    @property
    def status(self) -> RuntimeStatus:
        """Return the Connect runtime status without maintaining a host copy."""

        return self._require_runtime().status

    @property
    def peers(self) -> tuple[DiscoveredNodeCandidate, ...]:
        """Return Connect's current untrusted discovery candidates."""

        return self._require_runtime().peers

    @property
    def connections(self) -> tuple[NodeId, ...]:
        """Return Connect's current live peer connections."""

        return self._require_runtime().connections

    @property
    def node_records(self) -> tuple[Any, ...]:
        """Return Connect's immutable trust/connection/membership record view."""

        registry = self._require_runtime().registry
        if registry is None:
            raise ConnectRuntimeStartError(
                "Connect node registry is unavailable while runtime is started"
            )
        return registry.records

    def pair_peer(
        self,
        peer_id: NodeId,
        *,
        permissions: frozenset[str] = frozenset({"read_state"}),
        cancel_event: Event | None = None,
    ) -> TrustedPeer:
        """Pair through Connect; discovery itself never creates trust."""

        kwargs: dict[str, Any] = {}
        if permissions != frozenset({"read_state"}):
            kwargs["permissions"] = permissions
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        return self._require_runtime().pair_peer(peer_id, **kwargs)

    def repair_peer(
        self,
        peer_id: NodeId,
        *,
        permissions: frozenset[str] = frozenset({"read_state"}),
        cancel_event: Event | None = None,
    ) -> TrustedPeer:
        """Repair trust only through Connect's explicit repair protocol."""

        return self._require_runtime().repair_peer(
            peer_id, permissions=permissions, cancel_event=cancel_event
        )

    def revoke_peer(self, peer_id: NodeId) -> None:
        """Revoke directional Pair trust through Connect."""

        self._require_runtime().revoke_peer(peer_id)

    def connect_peer(self, peer_id: NodeId) -> Any:
        """Open a trusted peer connection using Connect's transport."""

        return self._require_runtime().connect_peer(peer_id)

    def reconnect_peer(self, peer_id: NodeId) -> Any:
        """Reconnect through Connect without changing trust or membership."""

        return self._require_runtime().reconnect_peer(peer_id)

    def disconnect_peer(self, peer_id: NodeId, *, reason: str = "disconnected") -> None:
        """Close the package-owned connection while retaining directional Pair."""

        self._require_runtime().disconnect_peer(peer_id, reason=reason)

    def register_surface(
        self,
        surface_id: str,
        *,
        read: SurfaceHandler,
        review: SurfaceHandler | None = None,
        actions: Mapping[str, SurfaceHandler]
        | Iterable[tuple[str, SurfaceHandler]]
        | None = None,
    ) -> SurfaceDefinition:
        """Register Exp-owned handlers in Connect's authenticated surface API."""

        definition = self._require_runtime().register_surface(
            surface_id, read=read, review=review, actions=actions
        )
        self._surface_definitions[definition.surface_id] = definition
        return definition

    def grant_surface_access(
        self,
        peer_id: NodeId,
        surface_id: str,
        *,
        access: SurfaceAccess | str,
        expires_at: float | None = None,
    ) -> SurfaceGrant:
        """Grant one explicitly approved peer access through Connect."""

        return self._require_runtime().grant_surface_access(
            peer_id, surface_id, access=access, expires_at=expires_at
        )

    def grant_cluster_surface_access(
        self,
        source: NodeId,
        surface_id: str,
        *,
        access: SurfaceAccess | str,
        expires_at: float | None,
    ) -> SurfaceGrant:
        """Create a bounded cluster-source grant for one registered surface."""

        if not isinstance(source, NodeId):
            raise TypeError("cluster surface source must be a Connect node")
        definition = self._registered_remote_surface(surface_id)
        if expires_at is None:
            raise ValueError("cluster surface grants must expire")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise TypeError("cluster surface expiry must be numeric")
        expiry = float(expires_at)
        now = time.time()
        if (
            not math.isfinite(expiry)
            or expiry <= now
            or expiry > now + MAX_CLUSTER_SURFACE_GRANT_SECONDS
        ):
            raise ValueError(
                "cluster surface grant expiry is outside the allowed window"
            )
        try:
            normalized_access = SurfaceAccess(access)
        except (TypeError, ValueError) as error:
            raise ValueError("cluster surface access mode is invalid") from error
        if normalized_access is SurfaceAccess.REVIEW and definition.review is None:
            raise PermissionError("surface review is not available")
        if normalized_access is SurfaceAccess.ACTION and not definition.actions:
            raise PermissionError("surface actions are not available")
        return self._require_runtime().grant_cluster_surface_access(
            source,
            surface_id,
            access=normalized_access,
            expires_at=expiry,
        )

    def revoke_surface_access(
        self,
        peer_id: NodeId,
        surface_id: str,
        *,
        access: SurfaceAccess | str | None = None,
    ) -> None:
        """Revoke one peer's runtime-local access to an Exp surface."""

        self._require_runtime().revoke_surface_access(
            peer_id, surface_id, access=access
        )

    def remote_hello(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> dict[str, Any]:
        """Read authenticated peer identity data on a connected outbound route."""

        return self._remote_provider(peer_id).hello(cancel_event)

    def remote_node_snapshot(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> Any:
        """Read a target node snapshot through Connect's authenticated provider."""

        return self._remote_provider(peer_id).node_snapshot(cancel_event)

    def remote_dashboard_snapshot(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> Any:
        """Read the target dashboard through Connect's authenticated provider."""

        return self._remote_provider(peer_id).dashboard_snapshot(cancel_event)

    def remote_component_summary(
        self, peer_id: NodeId, key: str, cancel_event: Event | None = None
    ) -> Any:
        """Read one target component summary through Connect."""

        return self._remote_provider(peer_id).component_summary(key, cancel_event)

    def remote_process_candidates(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> Any:
        """Read process candidates without exposing a provider to the UI."""

        return self._remote_provider(peer_id).process_candidates(cancel_event)

    def remote_storage_candidates(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> Any:
        """Read storage candidates without exposing a provider to the UI."""

        return self._remote_provider(peer_id).storage_candidates(
            cancel_event=cancel_event
        )

    def remote_read_surface(self, peer_id: NodeId, surface_id: str) -> Any:
        """Read a known host surface with no caller-supplied parameter form."""

        definition = self._registered_remote_surface(surface_id)
        if not callable(definition.read):
            raise ValueError(  # noqa: TRY004 -- Unsupported mode is a value error.
                "registered surface does not support read access"
            )
        return self._remote_provider(peer_id).read_surface(surface_id, params={})

    def remote_review_surface(self, peer_id: NodeId, surface_id: str) -> Any:
        """Review a known host surface without exposing arbitrary parameters."""

        definition = self._registered_remote_surface(surface_id)
        if not callable(definition.review):
            raise ValueError(  # noqa: TRY004 -- Unsupported mode is a value error.
                "registered surface does not support review access"
            )
        return self._remote_provider(peer_id).review_surface(surface_id, params={})

    def register_shared_capability(
        self,
        capability: str,
        handler: Callable[[NodeId, dict[str, object]], object],
    ) -> None:
        """Register an Exp-owned handler in Connect's runtime-local share table."""

        self._require_runtime().sharing.register(capability, handler)

    def allow_shared_capability(self, peer_id: NodeId, capability: str) -> None:
        """Allow one inbound-authorized peer to use one runtime-local capability."""

        relationship = self.peer_relationship(peer_id)
        if relationship not in {
            RelationshipState.INBOUND_GRANTED,
            RelationshipState.BIDIRECTIONAL,
        }:
            raise PermissionError("peer has not granted inbound access")
        self._require_runtime().sharing.allow(peer_id, capability)

    def revoke_shared_capability(self, peer_id: NodeId, capability: str) -> None:
        """Revoke one target-owned runtime-local capability grant."""

        self._require_runtime().sharing.revoke(peer_id, capability)

    def revoke_peer_shared_capabilities(self, peer_id: NodeId) -> None:
        """Revoke every generic runtime-local capability for one peer."""

        self._require_runtime().sharing.revoke_peer(peer_id)

    def clear_shared_capability_grants(self) -> None:
        """Clear all generic runtime-local grants without unregistering handlers."""

        self._require_runtime().sharing.clear_grants()

    def remote_request_shared(
        self,
        peer_id: NodeId,
        capability: str,
        params: dict[str, object] | None = None,
        cancel_event: Event | None = None,
    ) -> Any:
        """Request one explicitly shared target capability over Connect."""

        return self._remote_provider(peer_id).request_shared(
            capability, params, cancel_event
        )

    def remote_revoke_self(
        self, peer_id: NodeId, cancel_event: Event | None = None
    ) -> dict[str, Any]:
        """Ask a connected peer to revoke this caller's inbound grant."""

        return self._remote_provider(peer_id).revoke_self(cancel_event)

    def remote_request_quit(
        self,
        peer_id: NodeId,
        pids: list[int],
        *,
        expected_create_times: dict[int, float],
    ) -> Any:
        """Request typed graceful termination through the target's Exp surface."""

        backend = ConnectExpProcessActionBackend(self._remote_provider(peer_id))
        return backend.request_quit(pids, expected_create_times)

    def remote_force_quit(
        self,
        peer_id: NodeId,
        pids: list[int],
        *,
        expected_create_times: dict[int, float],
    ) -> Any:
        """Request typed force termination through the target's Exp surface."""

        backend = ConnectExpProcessActionBackend(self._remote_provider(peer_id))
        return backend.force_quit(pids, expected_create_times)

    def _remote_provider(self, peer_id: NodeId) -> Any:
        runtime = self._require_runtime()
        relationship = runtime.peer_relationship(peer_id)
        if relationship not in {
            RelationshipState.OUTBOUND_TRUSTED,
            RelationshipState.BIDIRECTIONAL,
        }:
            raise PermissionError("peer is not authorized for outbound requests")
        if peer_id not in runtime.connections:
            raise ConnectionError("peer has no live Connect connection")
        provider = runtime.connect_peer(peer_id)
        if provider is None:
            raise ConnectionError("Connect did not provide a live peer route")
        return provider

    def _registered_remote_surface(self, surface_id: str) -> SurfaceDefinition:
        if not isinstance(surface_id, str):
            raise TypeError("surface id must be a string")
        definition = self.surface_definitions.get(surface_id)
        if definition is None:
            raise ValueError("surface is not registered by this host")
        return definition

    def assign_role(self, target_node_id: NodeId, role: ClusterRole) -> RoleAssignment:
        """Assign a role through Connect's durable cluster authority."""

        return self._require_runtime().assign_role(target_node_id, role)

    def create_invite(
        self, target_node_id: NodeId, *, ttl_seconds: float = 300.0
    ) -> InviteRecord:
        """Create a targeted invite through Connect's cluster authority."""

        return self._require_runtime().create_invite(
            target_node_id, ttl_seconds=ttl_seconds
        )

    def join_cluster(self, provider: Any, invite: InviteRecord) -> dict[str, Any]:
        """Join only through a Connect-authenticated, already-paired provider."""

        return self._require_runtime().join_cluster(provider, invite)

    def heartbeat(
        self, coordinator_id: NodeId, fencing_token: str, *, now: float
    ) -> bool:
        """Delegate lease renewal; never retain or log the fencing token."""

        return self._require_runtime().heartbeat(
            coordinator_id=coordinator_id, fencing_token=fencing_token, now=now
        )

    def renew_coordinator_lease(self, *, now: float | None = None) -> bool:
        """Renew the local Coordinator lease without exposing its fencing token."""

        runtime = self._require_runtime()
        identity = runtime.identity
        if identity is None:
            raise PermissionError(
                "current Connect Coordinator authority is unavailable"
            )
        renewal_time = time.time() if now is None else now
        if (
            isinstance(renewal_time, bool)
            or not isinstance(renewal_time, (int, float))
            or not math.isfinite(float(renewal_time))
        ):
            raise ValueError("lease renewal time must be finite")
        _cluster_id, _epoch, fencing_token = self.current_coordinator_fence()
        return runtime.heartbeat(
            coordinator_id=identity.node_id,
            fencing_token=fencing_token,
            now=float(renewal_time),
        )

    def promote_if_due(self, *, now: float) -> bool:
        """Ask Connect's failover owner to evaluate promotion at the supplied time."""

        return self._require_runtime().promote_if_due(now=now)

    def rejoin_as_worker(self, node_id: NodeId, *, current_epoch: int) -> None:
        """Rejoin via Connect's canonical current cluster epoch."""

        self._require_runtime().rejoin_as_worker(
            node_id=node_id, current_epoch=current_epoch
        )

    def revoke_member(self, target_node_id: NodeId) -> None:
        """Revoke membership through Connect's durable cluster authority."""

        self._require_runtime().revoke_member(target_node_id)

    def cluster_member_operation(
        self, operation: str, target_node_id: NodeId
    ) -> dict[str, Any]:
        """Run one allowlisted worker operation through an authenticated peer."""

        if operation not in {
            "pause_worker",
            "resume_worker",
            "revoke_member",
            "revoke_worker",
            "remove_connection",
            "remove_job",
        }:
            raise ValueError("unsupported Connect cluster operation")
        actions = self._cluster_provider_operations(target_node_id)
        if operation == "pause_worker":
            return actions.pause_worker(target_node_id.value)
        if operation == "resume_worker":
            return actions.resume_worker(target_node_id.value)
        if operation == "revoke_member":
            return actions.revoke_member(target_node_id.value)
        if operation == "revoke_worker":
            return actions.revoke_worker(target_node_id.value)
        if operation == "remove_connection":
            return actions.remove_connection(target_node_id.value)
        return actions.remove_job(target_node_id.value)

    def renew_remote_coordinator_lease(self, peer_id: NodeId) -> dict[str, Any]:
        """Request a fenced coordinator lease renewal through one paired peer."""

        return self._cluster_provider_operations(peer_id).renew_coordinator_lease()

    def sync_remote_cluster_capability_grant(
        self,
        peer_id: NodeId,
        subject_node_id: NodeId,
        target_node_id: NodeId,
        permissions: list[str],
        *,
        expires_at: float,
    ) -> dict[str, Any]:
        """Synchronize one package-owned cluster capability grant to a peer."""

        return self._cluster_provider_operations(peer_id).sync_capability_grant(
            subject_node_id.value,
            target_node_id.value,
            permissions,
            expires_at=expires_at,
        )

    def revoke_remote_cluster_capabilities(
        self, peer_id: NodeId, subject_node_id: NodeId, target_node_id: NodeId
    ) -> dict[str, Any]:
        """Revoke package-owned cluster capabilities through an authenticated peer."""

        return self._cluster_provider_operations(peer_id).revoke_capabilities(
            subject_node_id.value, target_node_id.value
        )

    def _cluster_provider_operations(
        self, peer_id: NodeId
    ) -> ConnectExpClusterOperations:
        return ConnectExpClusterOperations(
            self._remote_provider(peer_id), self.current_coordinator_fence
        )

    def cluster_status(self) -> dict[str, Any]:
        """Return Connect's redacted cluster status projection."""

        return self._require_runtime().cluster_status()

    def current_cluster_fence(self) -> tuple[str, int, str]:
        """Return the live Connect fence for an active local member.

        This private-use integration value is intended for immediate fenced
        package RPCs. Callers must not persist, display, or log the token.
        """

        runtime = self._require_runtime()
        identity = runtime.identity
        cluster = runtime.cluster
        epoch = cluster.epoch if cluster is not None else None
        if identity is None or cluster is None or epoch is None:
            raise PermissionError(
                "current Connect Coordinator authority is unavailable"
            )
        assignment = cluster.local_assignment
        if not cluster.is_member(identity.node_id) or assignment.paused:
            raise PermissionError("current Connect cluster membership is unavailable")
        return cluster.cluster_id, epoch.epoch, epoch.fencing_token

    def current_coordinator_fence(self) -> tuple[str, int, str]:
        """Return the current fence only when this node is Connect's Coordinator."""

        runtime = self._require_runtime()
        identity = runtime.identity
        cluster = runtime.cluster
        if identity is None or cluster is None:
            raise PermissionError(
                "current Connect Coordinator authority is unavailable"
            )
        cluster_id, epoch, token = self.current_cluster_fence()
        assignment = cluster.local_assignment
        if (
            cluster.epoch is None
            or cluster.epoch.coordinator_id != identity.node_id
            or ClusterRole.COORDINATOR not in assignment.roles
        ):
            raise PermissionError(
                "current Connect Coordinator authority is unavailable"
            )
        return cluster_id, epoch, token

    def _require_runtime(self) -> ConnectRuntime:
        runtime = self._runtime
        if runtime is None or not runtime.started:
            raise RuntimeError("Connect runtime is not started")
        return runtime

    def shutdown(self) -> None:
        """Shut down the single package runtime; safe to call repeatedly."""

        runtime, self._runtime = self._runtime, None
        try:
            if runtime is not None:
                runtime.shutdown()
        finally:
            self._surface_definitions.clear()
