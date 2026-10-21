"""Shared deterministic builders for node model and registry tests.

Each factory returns fresh instances with explicit overrides so a test can
target one trust/status/capability condition without repeating the full
``NodeContext``/``DiscoveredNodeCandidate`` structure.
"""

from collections.abc import Iterable
from typing import Any

from maintenance.nodes import (
    DiscoveredNodeCandidate,
    NodeCapability,
    NodeContext,
    NodeDescriptor,
    NodeId,
    NodePermission,
    NodeStatus,
    NodeTrustState,
    local_node_descriptor,
)

_SENTINEL = object()


def make_local_context(
    *,
    provider: Any = _SENTINEL,
    process_manager: Any = _SENTINEL,
    file_manager: Any = _SENTINEL,
    scheduler: Any = _SENTINEL,
    coordinator: Any = _SENTINEL,
    **overrides: Any,
) -> NodeContext:
    """Build a local node context with explicit dependency overrides."""

    return NodeContext(
        descriptor=local_node_descriptor(),
        provider=provider,
        process_manager=process_manager,
        file_manager=file_manager,
        scheduler=scheduler,
        coordinator=coordinator,
        **overrides,
    )


def make_remote_context(
    node_id: str,
    *,
    trust: NodeTrustState = NodeTrustState.TRUSTED,
    status: NodeStatus = NodeStatus.UNKNOWN,
    display_name: str | None = None,
    hostname: str | None = None,
    capabilities: Iterable[NodeCapability] = (),
    platform: str | None = None,
    permissions: Iterable[NodePermission] = (),
    provider: Any = None,
    process_manager: Any = None,
    file_manager: Any = None,
    scheduler: Any = None,
    coordinator: Any = None,
    **overrides: Any,
) -> NodeContext:
    """Build a remote/trusted node context for one peer.

    Defaults describe a trusted, unverified peer (``UNKNOWN`` status, no
    dependencies); callers override trust/status/dependencies for the specific
    scenario they exercise.
    """

    return NodeContext(
        descriptor=NodeDescriptor(
            id=NodeId(node_id),
            display_name=display_name if display_name is not None else node_id,
            hostname=hostname if hostname is not None else f"{node_id}.example",
            is_local=False,
            trust=trust,
            status=status,
            capabilities=frozenset(capabilities),
            platform=platform,
            permissions=frozenset(permissions),
        ),
        provider=provider,
        process_manager=process_manager,
        file_manager=file_manager,
        scheduler=scheduler,
        coordinator=coordinator,
        **overrides,
    )


def make_candidate(
    stable_id: str = "peer-a",
    *,
    hostname: str = "peer-a-host",
    protocol_version: str = "1",
    port: int | None = 5000,
    connectable: bool = False,
    compatible: bool = True,
    last_seen: float = 100.0,
    identity_fingerprint: str | None = None,
) -> DiscoveredNodeCandidate:
    """Build a normalized discovered candidate for registry pairing tests.

    ``identity_fingerprint`` stays ``None`` unless the caller supplies one;
    a verified pairing flow requires an explicit fingerprint.
    """

    return DiscoveredNodeCandidate(
        stable_id=stable_id,
        hostname=hostname,
        addresses=("192.168.1.10",),
        port=port,
        service_name=f"{stable_id}._system-analyzer._tcp.local.",
        app_version="1.2.2.0",
        protocol_version=protocol_version,
        platform="Linux",
        connectable=connectable,
        compatible=compatible,
        last_seen=last_seen,
        identity_fingerprint=identity_fingerprint,
    )
