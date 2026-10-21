"""Node/target model and registry for the System Analyzer cluster boundary.

This module owns the domain concepts behind "selected node": stable identity,
display metadata, trust/capability state, per-node runtime context, and the
central registry that both the local machine and future discovered/paired
peers are registered in. It deliberately imports no Tkinter, scanner, network,
or transport code, so the model is safe to load in any interpreter and inside
tests without a display.

Invariants that every caller must preserve:

- ``DISCOVERED != TRUSTED != AUTHORISED``. A discovered candidate never gains
  read or action capabilities by virtue of being seen on the network.
- The local node is always registered and always selectable.
- Coordinator/cache keys are node-qualified through ``node_operation_key`` so
  two nodes can never overwrite or coalesce each other's work.
- A node without a capability (or whose state is unknown) is refused for that
  action; capability is never inferred from a hostname or from ``is_local``.
"""

import math
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from maintenance.components.temperature import TemperatureTelemetry
from maintenance.models import DashboardSnapshot, ProcessCandidate, ResourceSummary

LOCAL_NODE_ID = "local"
LOCAL_NODE_HOSTNAME = "localhost"
LOCAL_DISPLAY_NAME = "This System"
NODE_SNAPSHOT_SCHEMA_VERSION = 1


def generate_node_secret() -> str:
    """Return a fresh 256-bit node credential as hex text.

    The secret authenticates every request this machine sends for one remote
    node; it is generated at pairing time and never travels on the wire.
    """

    return secrets.token_hex(32)


def generate_stable_node_id() -> str:
    """Return a fresh installation identity for local discovery."""

    return f"node-{secrets.token_hex(16)}"


class NodeStatus(str, Enum):
    """Connectivity state of a registered node."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class NodeConnectionStatus(str, Enum):
    """Runtime connectivity state, independent of trust and authorization."""

    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    ONLINE = "online"
    OFFLINE = "offline"
    AUTHENTICATION_FAILED = "authentication_failed"
    IDENTITY_CHANGED = "identity_changed"


class PeerFailure(str, Enum):
    """Classified peer failures used by the bounded reconnect policy."""

    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    ROUTE_FAILURE = "route_failure"
    DISAPPEARED = "disappeared"
    AUTHENTICATION_FAILED = "authentication_failed"
    IDENTITY_CHANGED = "identity_changed"


def classify_peer_failure(error: BaseException | str) -> PeerFailure:
    """Classify coordinator-delivered peer errors without retrying trust failures."""

    error_name = type(error).__name__ if isinstance(error, BaseException) else ""
    if error_name in {"RemoteAuthError", "RemoteProtocolError"}:
        return PeerFailure.AUTHENTICATION_FAILED
    message = str(error).lower()
    if any(
        marker in message
        for marker in (
            "auth",
            "signature",
            "identity",
            "wrong node",
            "caller",
            "target identity",
            "certificate",
            "fingerprint",
        )
    ):
        return PeerFailure.AUTHENTICATION_FAILED
    if "timeout" in message or "timed out" in message:
        return PeerFailure.TIMEOUT
    if "refused" in message:
        return PeerFailure.CONNECTION_REFUSED
    if any(marker in message for marker in ("route", "network is unreachable")):
        return PeerFailure.ROUTE_FAILURE
    return PeerFailure.DISAPPEARED


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """A peer's runtime connection state and the time it was observed."""

    status: NodeConnectionStatus
    reason: str | None = None
    changed_at: float | None = None

    @classmethod
    def unknown(cls, *, now: float | None = None) -> "ConnectionState":
        return cls(NodeConnectionStatus.UNKNOWN, changed_at=now)

    @classmethod
    def connecting(cls, *, now: float | None = None) -> "ConnectionState":
        return cls(NodeConnectionStatus.CONNECTING, changed_at=now)

    @classmethod
    def online(cls, *, now: float | None = None) -> "ConnectionState":
        return cls(NodeConnectionStatus.ONLINE, changed_at=now)

    @classmethod
    def offline(
        cls, reason: str | None = None, *, now: float | None = None
    ) -> "ConnectionState":
        return cls(NodeConnectionStatus.OFFLINE, reason=reason, changed_at=now)


@dataclass(slots=True)
class RetryState:
    """Bounded retry timing for one peer, without owning a timer."""

    attempt: int = 0
    next_attempt_at: float | None = None
    automatic_retry: bool = True
    last_failure: PeerFailure | None = None

    def record_failure(
        self,
        failure: PeerFailure,
        *,
        now: float,
        jitter: Callable[[int], float] | None = None,
        base_seconds: float = 1.0,
        max_seconds: float = 300.0,
        max_backoff_exponent: int = 8,
    ) -> None:
        self.last_failure = failure
        self.attempt += 1
        if failure in (
            PeerFailure.AUTHENTICATION_FAILED,
            PeerFailure.IDENTITY_CHANGED,
        ):
            self.next_attempt_at = None
            self.automatic_retry = False
            return
        delay = min(
            max_seconds,
            base_seconds * (2 ** min(self.attempt - 1, max_backoff_exponent)),
        )
        extra = 0.0 if jitter is None else max(0.0, float(jitter(self.attempt)))
        self.next_attempt_at = now + delay + extra
        self.automatic_retry = True

    def reset(self) -> None:
        self.attempt = 0
        self.next_attempt_at = None
        self.automatic_retry = True
        self.last_failure = None


class NodeIdentityStatus(str, Enum):
    """Continuity of the stable node identity presented by a peer."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    MISMATCH = "mismatch"


class NodePairingState(str, Enum):
    """Explicit lifecycle state for one discovered peer."""

    DISCOVERED = "discovered"
    PAIRING = "pairing"
    TRUSTED = "trusted"
    PAIRING_FAILED = "pairing_failed"
    IDENTITY_CHANGED = "identity_changed"


class NodeTrustState(str, Enum):
    """How a node entered the registry and what may be done with it.

    ``LOCAL`` is the machine this process runs on. ``DISCOVERED``/``UNTRUSTED``
    are network observations that must be explicitly promoted by a future
    pairing flow before they become ``TRUSTED`` (and later ``AUTHORISED``).
    """

    LOCAL = "local"
    DISCOVERED = "discovered"
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    AUTHORISED = "authorised"


class NodeCapability(str, Enum):
    """Capabilities a node advertises or is authorised for.

    Read and destructive capabilities are deliberately separate: a node may
    support process review while explicitly refusing termination, and a remote
    node defaults to read-only regardless of what its discovery metadata says.
    """

    DASHBOARD_READ = "dashboard_read"
    COMPONENT_READ = "component_read"
    PROCESS_REVIEW = "process_review"
    PROCESS_TERMINATION = "process_termination"
    PROCESS_FORCE_TERMINATION = "process_force_termination"
    STORAGE_REVIEW = "storage_review"
    CLEANUP = "cleanup"
    REMOTE_MANAGEMENT = "remote_management"


class NodePermission(str, Enum):
    """Explicit operations a trusted caller may request from a node."""

    DASHBOARD_READ = "dashboard_read"
    COMPONENT_READ = "component_read"
    PROCESS_REVIEW = "process_review"
    PROCESS_TERMINATION = "process_termination"
    PROCESS_FORCE_TERMINATION = "process_force_termination"
    STORAGE_REVIEW = "storage_review"
    CLEANUP = "cleanup"
    REMOTE_MANAGEMENT = "remote_management"


class ProcessActionKind(str, Enum):
    """The only process actions that may cross a node boundary."""

    REQUEST_QUIT = "request_quit"
    FORCE_QUIT = "force_quit"


READ_PERMISSIONS = frozenset(
    {
        NodePermission.DASHBOARD_READ,
        NodePermission.COMPONENT_READ,
        NodePermission.PROCESS_REVIEW,
        NodePermission.STORAGE_REVIEW,
    }
)

READ_CAPABILITIES = frozenset(
    {
        NodeCapability.DASHBOARD_READ,
        NodeCapability.COMPONENT_READ,
        NodeCapability.PROCESS_REVIEW,
        NodeCapability.STORAGE_REVIEW,
    }
)


@dataclass(frozen=True, slots=True)
class NodeId:
    """Stable opaque identity for one node.

    Never derived from a display name, hostname, address, or selector position
    so a node's identity survives renames, DHCP churn, and multi-homing.
    """

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class NodeDescriptor:
    """Immutable metadata describing one registered node."""

    id: NodeId
    display_name: str
    hostname: str
    is_local: bool
    trust: NodeTrustState
    status: NodeStatus
    capabilities: frozenset[NodeCapability]
    platform: str | None = None
    color: str | None = None
    identity_fingerprint: str | None = None
    identity_status: NodeIdentityStatus = NodeIdentityStatus.UNVERIFIED
    permissions: frozenset[NodePermission] = frozenset()
    pairing_state: NodePairingState = NodePairingState.TRUSTED
    role: str = "worker"

    def has(self, capability: NodeCapability) -> bool:
        return capability in self.capabilities


def is_trusted_descriptor(descriptor: NodeDescriptor) -> bool:
    """Return whether a descriptor is trusted or authorised."""

    return descriptor.trust in (NodeTrustState.TRUSTED, NodeTrustState.AUTHORISED)


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """Versioned, node-bound read snapshot for one machine.

    Carries the node identity and trust/capability state alongside the
    dashboard payload so consumers never have to guess which node produced a
    result. ``is_stale`` expresses freshness against a caller-supplied age
    bound so offline/stale presentation stays in one place.
    """

    node_id: NodeId
    display_name: str
    hostname: str
    platform: str | None
    status: NodeStatus
    capabilities: frozenset[NodeCapability]
    scanned_at: datetime
    dashboard: DashboardSnapshot | None = None
    schema_version: int = NODE_SNAPSHOT_SCHEMA_VERSION

    @property
    def resources(self) -> tuple[ResourceSummary, ...]:
        """Return the dashboard resources without exposing the wire envelope."""

        return () if self.dashboard is None else self.dashboard.resources

    def resource(self, key: str) -> ResourceSummary:
        """Return one normalized resource from this node's snapshot."""

        if self.dashboard is None:
            raise KeyError(f"Snapshot has no resources: {key}")
        return self.dashboard.get(key)

    def is_stale(
        self,
        *,
        now: datetime | None = None,
        max_age: timedelta,
    ) -> bool:
        current = datetime.now(timezone.utc).astimezone() if now is None else now
        if current.tzinfo is None and self.scanned_at.tzinfo is not None:
            current = current.replace(tzinfo=self.scanned_at.tzinfo)
        elif current.tzinfo is not None and self.scanned_at.tzinfo is None:
            current = current.replace(tzinfo=None)
        return current - self.scanned_at > max_age


@dataclass(frozen=True, slots=True)
class NodeCredentials:
    """The shared secret authenticating one remote node's requests.

    ``secret`` is 256-bit hex text generated at pairing time; the replay and
    freshness protections live in the authenticated transport, not here.
    """

    node_id: NodeId
    secret: str
    created_at: float


def local_capabilities() -> frozenset[NodeCapability]:
    """Return the capabilities the local machine is authorised for.

    These mirror what the current local ``ProcessManager``/``FileManager``
    support; the local safety checks inside those managers remain the final
    authority for any destructive operation.
    """

    return frozenset(
        {
            NodeCapability.DASHBOARD_READ,
            NodeCapability.COMPONENT_READ,
            NodeCapability.PROCESS_REVIEW,
            NodeCapability.PROCESS_TERMINATION,
            NodeCapability.PROCESS_FORCE_TERMINATION,
            NodeCapability.STORAGE_REVIEW,
            NodeCapability.CLEANUP,
            NodeCapability.REMOTE_MANAGEMENT,
        }
    )


def local_node_descriptor(
    *,
    hostname: str = LOCAL_NODE_HOSTNAME,
    display_name: str = LOCAL_DISPLAY_NAME,
    platform_name: str | None = None,
) -> NodeDescriptor:
    """Return the canonical descriptor for the local machine."""

    return NodeDescriptor(
        id=NodeId(LOCAL_NODE_ID),
        display_name=display_name,
        hostname=hostname,
        is_local=True,
        trust=NodeTrustState.LOCAL,
        status=NodeStatus.ONLINE,
        capabilities=local_capabilities(),
        platform=platform_name,
        identity_fingerprint=node_identity_fingerprint(LOCAL_NODE_ID),
        identity_status=NodeIdentityStatus.VERIFIED,
        permissions=frozenset(NodePermission),
    )


def node_identity_fingerprint(node_id: NodeId | str) -> str:
    """Return a display fingerprint for a stable node identity.

    This is a verification aid, not a credential. Authentication remains the
    existing HMAC transport using the separately paired secret.
    """

    value = node_id.value if isinstance(node_id, NodeId) else node_id
    digest = sha256(f"system-analyzer-node:{value}".encode()).hexdigest()
    return ":".join(digest[index : index + 4] for index in range(0, len(digest), 4))


def node_operation_key(node_id: NodeId, operation: str) -> str:
    """Return a node-qualified coordinator/cache key for one operation.

    Every shared operation key must flow through here so two nodes can never
    coalesce, overwrite, or subscribe to each other's work on one coordinator.
    """

    return f"node:{node_id.value}:{operation}"


def operation_key(node_id: NodeId | None, operation: str) -> str:
    """Return the local or node-qualified operation key for one action."""

    if node_id is None:
        return operation
    return node_operation_key(node_id, operation)


@dataclass(frozen=True, slots=True)
class ProcessRef:
    """A process reference bound to one node.

    The PID is only meaningful inside its own node; the create-time token (when
    available) protects against PID reuse exactly as the local manager does.
    """

    node_id: NodeId
    pid: int
    create_time: float | None = None


@dataclass(frozen=True, slots=True)
class ProcessTerminationRequest:
    """A target-bound, explicitly allowlisted process action request."""

    target_node_id: NodeId
    processes: tuple[ProcessRef, ...]
    action: ProcessActionKind

    def __post_init__(self) -> None:
        if not self.processes:
            raise ValueError("at least one process is required")
        if not isinstance(self.action, ProcessActionKind):
            raise TypeError("unsupported process action")
        for process in self.processes:
            if (
                not isinstance(process.pid, int)
                or isinstance(process.pid, bool)
                or process.pid < 0
            ):
                raise ValueError("process reference pid is invalid")
            if process.create_time is None:
                raise ValueError("process reference create_time is required")
            if (
                not isinstance(process.create_time, (int, float))
                or isinstance(process.create_time, bool)
                or not math.isfinite(float(process.create_time))
                or float(process.create_time) < 0
            ):
                raise ValueError("process reference create_time is invalid")
        if any(process.node_id != self.target_node_id for process in self.processes):
            raise ValueError("process references must match the target node")


@dataclass(frozen=True, slots=True)
class FileRef:
    """A file reference bound to one node."""

    node_id: NodeId
    path: Path
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredNodeCandidate:
    """Normalized network presence observation, never an authorisation.

    Produced by the discovery component and stored by the registry as an
    untrusted candidate. ``connectable`` and ``compatible`` are separate from
    any trust decision: a connectable-looking peer is still not usable until a
    future pairing flow authorises it.
    """

    stable_id: str
    hostname: str
    addresses: tuple[str, ...]
    port: int | None
    service_name: str
    app_version: str
    protocol_version: str
    platform: str | None
    connectable: bool
    compatible: bool
    last_seen: float
    identity_fingerprint: str | None = None
    transport_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessActionRefusal:
    """Structured refusal for a destructive process action on a node.

    Returned instead of raising so a dialog can present a read-only/refused
    target without pretending the action ran or failing the whole surface.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class FileActionRefusal:
    reason: str


class NodeProvider(Protocol):
    """Read-side contract every node implementation must satisfy.

    ``Analyzer`` is the initial local implementation. A future authenticated
    remote provider implements the same protocol so cards, dialogs, and the
    dashboard never need to know the transport. All methods are read-only;
    mutation lives behind the action backends instead.
    """

    def dashboard_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DashboardSnapshot: ...

    def node_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> NodeSnapshot: ...

    def component_summary(
        self,
        key: str,
        cancel_event: Any | None = None,
    ) -> ResourceSummary: ...

    def process_candidates(
        self,
        cancel_event: Any | None = None,
    ) -> list[ProcessCandidate]: ...

    def storage_candidates(
        self,
        progress_callback: Callable[[str], None] | None = None,
        cancel_event: Any | None = None,
    ) -> list[Any]: ...

    def reset_component_sample(self, key: str) -> None: ...

    def stop_background_workers(self) -> None: ...


class LocalNodeProvider:
    """Adapt the existing local analyzer to the node-bound read contract.

    The wrapped analyzer remains available to existing callers; this adapter
    only adds the local descriptor to a dashboard result.
    """

    def __init__(self, provider: Any, descriptor: NodeDescriptor) -> None:
        self._provider = provider
        self._descriptor = descriptor

    def dashboard_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DashboardSnapshot:
        return self._provider.dashboard_snapshot(
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def node_snapshot(
        self,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> NodeSnapshot:
        dashboard = self.dashboard_snapshot(cancel_event, progress_callback)
        return self.snapshot_from_dashboard(dashboard)

    def snapshot_from_dashboard(self, dashboard: DashboardSnapshot) -> NodeSnapshot:
        """Bind an already-collected local dashboard result to its node."""

        return NodeSnapshot(
            node_id=self._descriptor.id,
            display_name=self._descriptor.display_name,
            hostname=self._descriptor.hostname,
            platform=self._descriptor.platform,
            status=self._descriptor.status,
            capabilities=self._descriptor.capabilities,
            scanned_at=dashboard.scanned_at,
            dashboard=dashboard,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


class ProcessActionBackend(Protocol):
    """Target-bound interface for process termination.

    Local implementations reuse the existing safety checks unchanged; remote or
    unauthorised targets return a refusal instead of touching local psutil.
    """

    def request_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> Any: ...

    def force_quit(
        self,
        pids: list[int],
        expected_create_times: dict[int, float] | None = None,
    ) -> Any: ...


class FileActionBackend(Protocol):
    def move_to_trash(
        self,
        refs: list[FileRef],
    ) -> Any: ...


@dataclass
class NodeContext:
    """Runtime state owned by one registered node.

    The window keeps its historical attributes (``analyzer``,
    ``process_manager``, ``file_manager``, ``snapshot``, ``_capabilities``,
    ``_component_scheduler``) mirroring the selected context so existing test
    seams and callers keep working; switching nodes re-syncs the mirrors from
    the newly selected context.
    """

    descriptor: NodeDescriptor
    provider: Any
    process_manager: Any
    file_manager: Any
    scheduler: Any
    coordinator: Any
    snapshot: Any | None = None
    node_snapshot: NodeSnapshot | None = None
    telemetry: TemperatureTelemetry = field(default_factory=TemperatureTelemetry)
    capabilities: dict[str, Any] = field(default_factory=dict)
    capability_counts: dict[str, int] = field(default_factory=dict)
    failed_card_counts: dict[str, int] = field(default_factory=dict)
    full_snapshot_applied_at: float | None = None
    connection: ConnectionState = field(default_factory=ConnectionState.unknown)
    retry: RetryState = field(default_factory=RetryState)
    connection_generation: int = 0
    last_heartbeat_at: float | None = None
    last_snapshot_at: float | None = None

    @property
    def node_id(self) -> NodeId:
        return self.descriptor.id


class NodeRegistry:
    """Central owner of known nodes, discovered candidates, and the selection.

    Instance-local (never process-global) so different windows can never
    mutate each other's registries. The registry owns descriptors and runtime
    contexts; ``AppWindow`` owns only the UI reactions to selection changes.
    """

    def __init__(self, local_context: NodeContext | None = None) -> None:
        self._contexts: dict[NodeId, NodeContext] = {}
        self._discovered: dict[NodeId, DiscoveredNodeCandidate] = {}
        self._pairing_states: dict[NodeId, NodePairingState] = {}
        self._selected_id: NodeId | None = None
        self._local_id: NodeId | None = None
        if local_context is not None:
            self.register_context(local_context)
            self.select(local_context.node_id)

    def register_context(self, context: NodeContext) -> None:
        node_id = context.node_id
        existing = self._contexts.get(node_id)
        if existing is not None:
            if self._is_placeholder(existing) and self._is_operational(context):
                if context.descriptor.is_local or not is_trusted_descriptor(
                    context.descriptor
                ):
                    raise ValueError(
                        "A trusted placeholder must be replaced by a trusted "
                        "or authorised remote context"
                    )
                self._contexts[node_id] = context
                return
            raise ValueError(f"Node already registered: {node_id}")
        if context.descriptor.is_local:
            if self._local_id is not None:
                raise ValueError("A local node is already registered")
            self._local_id = node_id
        self._contexts[node_id] = context

    def local_id(self) -> NodeId | None:
        return self._local_id

    def context(self, node_id: NodeId) -> NodeContext:
        try:
            return self._contexts[node_id]
        except KeyError as error:
            raise KeyError(f"Unknown node: {node_id}") from error

    def contexts(self) -> tuple[NodeContext, ...]:
        return tuple(self._contexts.values())

    def descriptors(self) -> tuple[NodeDescriptor, ...]:
        return tuple(context.descriptor for context in self._contexts.values())

    def selectable_descriptors(self) -> tuple[NodeDescriptor, ...]:
        """Return descriptors the UI may offer as dashboard targets.

        Only the local node and explicitly trusted/authorised nodes are
        selectable; discovered/untrusted candidates are never selectable so
        discovery can never silently widen the actionable surface.
        """

        return tuple(
            context.descriptor
            for context in self._contexts.values()
            if context.descriptor.trust
            in (
                NodeTrustState.LOCAL,
                NodeTrustState.TRUSTED,
                NodeTrustState.AUTHORISED,
            )
            and context.descriptor.identity_status is not NodeIdentityStatus.MISMATCH
            and (context.descriptor.is_local or self._is_operational(context))
        )

    def select(self, node_id: NodeId) -> NodeId:
        if node_id in self._discovered:
            raise ValueError(f"Node is not selectable (not trusted): {node_id}")
        if node_id not in self._contexts:
            raise KeyError(f"Unknown node: {node_id}")
        if node_id not in {item.id for item in self.selectable_descriptors()}:
            raise ValueError(f"Node is not selectable (not trusted): {node_id}")
        self._selected_id = node_id
        return node_id

    @staticmethod
    def _is_operational(context: NodeContext) -> bool:
        """Return whether a context can safely supply dashboard work.

        Trust records may exist before pairing creates a provider. They remain
        registry metadata, not dashboard targets, until a read provider and a
        per-node scheduler have both been attached.
        """

        return context.provider is not None and context.scheduler is not None

    @classmethod
    def _is_placeholder(cls, context: NodeContext) -> bool:
        return not context.descriptor.is_local and not cls._is_operational(context)

    def selected_id(self) -> NodeId:
        if self._selected_id is None:
            raise RuntimeError("No node is selected")
        return self._selected_id

    def selected_context(self) -> NodeContext:
        return self.context(self.selected_id())

    def update_discovered(
        self, candidate: DiscoveredNodeCandidate
    ) -> NodeDescriptor | None:
        """Record one discovered candidate as untrusted, non-selectable.

        Returns the stored descriptor, or ``None`` when the candidate is the
        local node or otherwise rejected. The candidate never gains read or
        action capabilities here; a future pairing flow promotes it.
        """

        if not candidate.stable_id or candidate.stable_id == LOCAL_NODE_ID:
            return None
        if candidate.port is not None and (
            candidate.port <= 0 or candidate.port > 65535
        ):
            return None
        if candidate.stable_id == (
            self._local_id.value if self._local_id else LOCAL_NODE_ID
        ):
            return None
        node_id = NodeId(candidate.stable_id)
        known_context = self._contexts.get(node_id)
        if known_context is not None:
            descriptor = known_context.descriptor
            identity_status = descriptor.identity_status
            if (
                descriptor.identity_fingerprint is not None
                and identity_status is not NodeIdentityStatus.MISMATCH
            ):
                identity_status = (
                    NodeIdentityStatus.VERIFIED
                    if candidate.identity_fingerprint == descriptor.identity_fingerprint
                    else NodeIdentityStatus.MISMATCH
                )
                if identity_status is NodeIdentityStatus.MISMATCH:
                    self._pairing_states[node_id] = NodePairingState.IDENTITY_CHANGED
            display_name = descriptor.display_name
            if display_name == descriptor.hostname:
                display_name = candidate.hostname
            connection_status = known_context.connection.status
            if connection_status is NodeConnectionStatus.ONLINE:
                runtime_status = NodeStatus.ONLINE
            elif connection_status in {
                NodeConnectionStatus.OFFLINE,
                NodeConnectionStatus.AUTHENTICATION_FAILED,
                NodeConnectionStatus.IDENTITY_CHANGED,
            }:
                runtime_status = NodeStatus.OFFLINE
            else:
                runtime_status = NodeStatus.UNKNOWN
            known_context.descriptor = replace(
                descriptor,
                display_name=display_name,
                hostname=candidate.hostname,
                status=runtime_status,
                platform=candidate.platform,
                identity_fingerprint=descriptor.identity_fingerprint,
                identity_status=identity_status,
                pairing_state=(
                    NodePairingState.IDENTITY_CHANGED
                    if identity_status is NodeIdentityStatus.MISMATCH
                    else descriptor.pairing_state
                ),
            )
            if identity_status is NodeIdentityStatus.MISMATCH:
                self._discovered[node_id] = candidate
                if self._selected_id == node_id:
                    self._selected_id = self._local_id
            return known_context.descriptor
        self._discovered[node_id] = candidate
        self._pairing_states.setdefault(node_id, NodePairingState.DISCOVERED)
        descriptor = NodeDescriptor(
            id=node_id,
            display_name=candidate.hostname,
            hostname=candidate.hostname,
            is_local=False,
            trust=NodeTrustState.UNTRUSTED,
            status=NodeStatus.ONLINE if candidate.last_seen else NodeStatus.UNKNOWN,
            capabilities=frozenset(),
            platform=candidate.platform,
            identity_fingerprint=candidate.identity_fingerprint,
            identity_status=NodeIdentityStatus.UNVERIFIED,
            pairing_state=NodePairingState.DISCOVERED,
        )
        return descriptor

    def confirm_identity(
        self, node_id: NodeId, identity_fingerprint: str
    ) -> NodeDescriptor:
        """Mark a trusted node verified after authenticated rediscovery."""

        context = self.context(node_id)
        descriptor = context.descriptor
        if (
            descriptor.identity_fingerprint is not None
            and descriptor.identity_fingerprint != identity_fingerprint
        ):
            raise ValueError(f"Identity fingerprint mismatch: {node_id}")
        context.descriptor = replace(
            descriptor,
            identity_fingerprint=identity_fingerprint,
            identity_status=NodeIdentityStatus.VERIFIED,
        )
        return context.descriptor

    def discovered_candidates(self) -> tuple[DiscoveredNodeCandidate, ...]:
        return tuple(self._discovered.values())

    def pairing_state(self, node_id: NodeId) -> NodePairingState:
        """Return the explicit pairing state, defaulting safely to discovery."""

        context = self._contexts.get(node_id)
        if context is not None:
            return context.descriptor.pairing_state
        return self._pairing_states.get(node_id, NodePairingState.DISCOVERED)

    def begin_pairing(self, node_id: NodeId) -> NodePairingState:
        """Enter pairing only after a caller deliberately starts the flow."""

        if node_id not in self._discovered:
            raise KeyError(f"No discovered node: {node_id}")
        candidate = self._discovered[node_id]
        if not candidate.identity_fingerprint:
            self._pairing_states[node_id] = NodePairingState.PAIRING_FAILED
            raise ValueError("Peer identity fingerprint is unavailable")
        if not candidate.compatible:
            self._pairing_states[node_id] = NodePairingState.PAIRING_FAILED
            raise ValueError("Peer protocol version is incompatible")
        # An IDENTITY_CHANGED state recovers only through explicit re-pairing,
        # never silently — the transition is the same regardless of prior state.
        self._pairing_states[node_id] = NodePairingState.PAIRING
        return NodePairingState.PAIRING

    def fail_pairing(self, node_id: NodeId) -> NodePairingState:
        if node_id in self._discovered:
            self._pairing_states[node_id] = NodePairingState.PAIRING_FAILED
        return self.pairing_state(node_id)

    def remove_discovered(self, node_id: NodeId) -> None:
        self._discovered.pop(node_id, None)
        context = self._contexts.get(node_id)
        if context is not None and not context.descriptor.is_local:
            context.descriptor = replace(context.descriptor, status=NodeStatus.OFFLINE)

    def reject_discovered(self, node_id: NodeId) -> None:
        """Explicitly reject one discovered peer candidate."""

        self.remove_discovered(node_id)
        self._pairing_states.pop(node_id, None)

    def promote_to_trusted(
        self,
        node_id: NodeId,
        *,
        capabilities: Iterable[NodeCapability] = (),
    ) -> NodeDescriptor:
        """Explicitly promote a discovered node to trusted, read-only.

        A future Nodes & Connections pairing flow calls this after the user
        approves a peer. Defaults to no action capabilities: authorisation is a
        separate, later step and is never granted by discovery.
        """

        candidate = self._discovered.get(node_id)
        if candidate is None:
            raise KeyError(f"No discovered node: {node_id}")
        requested_capabilities = frozenset(capabilities)
        if not candidate.identity_fingerprint:
            raise ValueError(
                "Peer identity fingerprint is unavailable; re-pair manually"
            )
        if self._pairing_states.get(node_id) is not NodePairingState.PAIRING:
            raise ValueError("Pairing must be deliberately initiated first")
        identity_fingerprint = candidate.identity_fingerprint
        if not requested_capabilities <= READ_CAPABILITIES:
            raise ValueError(
                "Trusted nodes may only receive read capabilities; "
                "authorisation is required for destructive capabilities"
            )
        descriptor = NodeDescriptor(
            id=node_id,
            display_name=candidate.hostname,
            hostname=candidate.hostname,
            is_local=False,
            trust=NodeTrustState.TRUSTED,
            status=NodeStatus.ONLINE,
            capabilities=frozenset(capabilities),
            platform=candidate.platform,
            identity_fingerprint=identity_fingerprint,
            identity_status=NodeIdentityStatus.VERIFIED,
            pairing_state=NodePairingState.TRUSTED,
        )
        context = NodeContext(
            descriptor=descriptor,
            provider=None,
            process_manager=None,
            file_manager=None,
            scheduler=None,
            coordinator=None,
        )
        self._discovered.pop(node_id, None)
        self._pairing_states.pop(node_id, None)
        self._contexts[node_id] = context
        return descriptor

    def revoke_trusted(self, node_id: NodeId) -> None:
        """Remove one trusted/authorised node from the registry.

        The local node can never be revoked; discovered candidates and
        placeholders that were never trusted are rejected. When the revoked
        node was selected, selection returns to the local node.
        """

        context = self.context(node_id)
        if context.descriptor.is_local:
            raise ValueError("The local node cannot be revoked")
        if context.descriptor.trust not in (
            NodeTrustState.TRUSTED,
            NodeTrustState.AUTHORISED,
        ):
            raise ValueError(f"Node is not trusted: {node_id}")
        del self._contexts[node_id]
        self._pairing_states.pop(node_id, None)
        if self._selected_id == node_id:
            self._selected_id = self._local_id

    def set_display_name(self, node_id: NodeId, display_name: str) -> NodeDescriptor:
        """Rename one registered node, returning its updated descriptor."""

        context = self.context(node_id)
        context.descriptor = replace(context.descriptor, display_name=display_name)
        return context.descriptor

    def set_color(self, node_id: NodeId, color: str | None) -> NodeDescriptor:
        """Set one registered node's display colour, returning its descriptor."""

        context = self.context(node_id)
        context.descriptor = replace(context.descriptor, color=color)
        return context.descriptor
