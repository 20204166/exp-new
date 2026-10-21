"""Pure cluster role, lease, and fencing decisions.

This module has no persistence, transport, Tk, or executor ownership.  Callers
persist and publish the returned immutable values through the existing cluster
and connection owners.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass, replace
from enum import Enum

from maintenance.nodes import NodeId, NodePermission

HEARTBEAT_TIMEOUT_SECONDS = 120.0


class ClusterRole(str, Enum):
    WORKER = "worker"
    COORDINATOR = "coordinator"
    SUBCOORDINATOR = "subcoordinator"


class RoleAuthorizationError(ValueError):
    """Raised when an actor cannot make a role transition."""


class FencingError(ValueError):
    """Raised when a lease or epoch is stale."""


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    roles: frozenset[ClusterRole]
    node_id: NodeId | None = None
    paused: bool = False
    revoked: bool = False
    has_active_job: bool = False

    def __post_init__(self) -> None:
        roles = frozenset(self.roles)
        if ClusterRole.COORDINATOR in roles:
            roles = roles | {ClusterRole.WORKER}
        if not roles:
            roles = frozenset({ClusterRole.WORKER})
        if self.revoked and self.paused:
            raise ValueError("a revoked node cannot also be paused")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True, slots=True)
class CoordinatorLease:
    coordinator_id: NodeId
    epoch: int
    fencing_token: str
    issued_at: float
    expires_at: float
    last_heartbeat_at: float

    def __post_init__(self) -> None:
        if self.epoch < 0 or not self.fencing_token:
            raise ValueError("invalid coordinator lease")
        if self.expires_at < self.issued_at:
            raise ValueError("lease expiry precedes issuance")


@dataclass(frozen=True, slots=True)
class CoordinatorEpoch:
    epoch: int
    coordinator_id: NodeId
    fencing_token: str
    issued_at: float
    lease_expires_at: float

    def __post_init__(self) -> None:
        if (
            self.epoch < 0
            or not self.fencing_token
            or not math.isfinite(self.issued_at)
            or not math.isfinite(self.lease_expires_at)
        ):
            raise ValueError("invalid coordinator epoch")
        if self.lease_expires_at < self.issued_at:
            raise ValueError("epoch lease expiry precedes issuance")


@dataclass(frozen=True, slots=True)
class RoleChange:
    node_id: NodeId
    assignment: RoleAssignment
    changed_at: float
    actor_id: NodeId


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """Scoped authority delegated to one Subcoordinator for one target."""

    subject: NodeId
    target: NodeId
    permissions: frozenset[NodePermission]
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if not self.permissions:
            raise ValueError("a capability grant must contain a permission")
        if not math.isfinite(self.issued_at) or not math.isfinite(self.expires_at):
            raise ValueError("capability grant times must be finite")
        if self.expires_at <= self.issued_at:
            raise ValueError("capability grant must expire after issuance")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    role_assignment: RoleAssignment
    epoch: CoordinatorEpoch
    assignments: tuple[RoleAssignment, ...] = ()

    @property
    def role(self) -> ClusterRole:
        return ClusterRole.COORDINATOR


@dataclass(frozen=True, slots=True)
class RoleState:
    assignments: tuple[RoleAssignment, ...] = ()
    epoch: CoordinatorEpoch | None = None
    promotion_epochs: frozenset[int] = frozenset()
    capability_grants: tuple[CapabilityGrant, ...] = ()

    def __post_init__(self) -> None:
        assignments = [item for item in self.assignments if item.node_id is not None]
        if len({item.node_id for item in assignments}) != len(assignments):
            raise ValueError("duplicate node role assignment")
        active_coordinators = [
            item
            for item in assignments
            if ClusterRole.COORDINATOR in item.roles and not item.revoked
        ]
        active_subcoordinators = [
            item
            for item in assignments
            if ClusterRole.SUBCOORDINATOR in item.roles and not item.revoked
        ]
        if len(active_coordinators) > 1:
            raise ValueError("only one active Coordinator is allowed")
        if len(active_subcoordinators) > 1:
            raise ValueError("only one active Subcoordinator is allowed")
        grant_keys = [(grant.subject, grant.target) for grant in self.capability_grants]
        if len(set(grant_keys)) != len(grant_keys):
            raise ValueError("duplicate capability grant")

    def assignment_for(self, node_id: NodeId) -> RoleAssignment | None:
        return next(
            (item for item in self.assignments if item.node_id == node_id), None
        )

    def capability_grant(
        self, subject: NodeId, target: NodeId, *, now: float | None = None
    ) -> CapabilityGrant | None:
        grant = next(
            (
                item
                for item in self.capability_grants
                if item.subject == subject and item.target == target
            ),
            None,
        )
        if grant is not None and now is not None and now >= grant.expires_at:
            return None
        return grant

    def grant_capabilities(
        self,
        *,
        actor: RoleAssignment,
        subject: NodeId,
        target: NodeId,
        permissions: frozenset[NodePermission],
        now: float,
        expires_at: float,
    ) -> RoleState:
        self._assert_control(actor)
        subject_assignment = self.assignment_for(subject)
        target_assignment = self.assignment_for(target)
        if (
            subject_assignment is None
            or subject_assignment.revoked
            or ClusterRole.SUBCOORDINATOR not in subject_assignment.roles
        ):
            raise RoleAuthorizationError(
                "capability subject is not an active Subcoordinator"
            )
        if (
            target_assignment is None
            or target_assignment.revoked
            or ClusterRole.WORKER not in target_assignment.roles
            or subject == target
        ):
            raise RoleAuthorizationError("capability target is not an active Worker")
        grant = CapabilityGrant(subject, target, permissions, now, expires_at)
        return replace(
            self,
            capability_grants=tuple(
                item
                for item in self.capability_grants
                if (item.subject, item.target) != (subject, target)
            )
            + (grant,),
        )

    def revoke_capabilities(
        self, *, actor: RoleAssignment, subject: NodeId, target: NodeId
    ) -> RoleState:
        self._assert_control(actor)
        return replace(
            self,
            capability_grants=tuple(
                item
                for item in self.capability_grants
                if (item.subject, item.target) != (subject, target)
            ),
        )

    def allows(
        self,
        *,
        subject: NodeId,
        target: NodeId,
        permission: NodePermission,
        now: float,
    ) -> bool:
        """Return whether a current role may use one permission on one node."""

        subject_assignment = self.assignment_for(subject)
        target_assignment = self.assignment_for(target)
        if (
            subject_assignment is None
            or subject_assignment.revoked
            or subject_assignment.paused
            or target_assignment is None
            or target_assignment.revoked
        ):
            return False
        if subject == target:
            return True
        if ClusterRole.COORDINATOR in subject_assignment.roles:
            return ClusterRole.WORKER in target_assignment.roles
        if ClusterRole.SUBCOORDINATOR not in subject_assignment.roles:
            return False
        grant = self.capability_grant(subject, target, now=now)
        return grant is not None and permission in grant.permissions

    def active_coordinator(self) -> RoleAssignment | None:
        return next(
            (
                item
                for item in self.assignments
                if ClusterRole.COORDINATOR in item.roles and not item.revoked
            ),
            None,
        )

    def assign(
        self,
        *,
        actor: RoleAssignment,
        target: NodeId,
        roles: frozenset[ClusterRole],
        now: float = 0.0,
    ) -> tuple[RoleState, RoleChange]:
        state = self
        if ClusterRole.COORDINATOR not in actor.roles or actor.paused or actor.revoked:
            raise RoleAuthorizationError("only an active Coordinator may assign roles")
        assignment = RoleAssignment(roles, node_id=target)
        current = state.assignment_for(target)
        if current is not None and current.revoked:
            raise RoleAuthorizationError("revoked node requires a new pairing invite")
        if current is not None:
            # Preserve existing occupancy; role reassignment does not start or stop a job.
            assignment = replace(assignment, has_active_job=current.has_active_job)
        if ClusterRole.COORDINATOR in assignment.roles:
            raise RoleAuthorizationError("Coordinator ownership cannot be delegated")
        other_sub = next(
            (
                item
                for item in state.assignments
                if item.node_id != target
                and ClusterRole.SUBCOORDINATOR in item.roles
                and not item.revoked
            ),
            None,
        )
        if ClusterRole.SUBCOORDINATOR in assignment.roles and other_sub is not None:
            raise RoleAuthorizationError("only one active Subcoordinator is allowed")
        updated = tuple(
            assignment if item.node_id == target else item for item in state.assignments
        )
        if not any(item.node_id == target for item in state.assignments):
            updated += (assignment,)
        change = RoleChange(target, assignment, now, actor.node_id or NodeId("local"))
        return replace(state, assignments=updated), change

    def pause(self, *, actor: RoleAssignment, target: NodeId) -> RoleState:
        self._assert_control(actor)
        current = self.assignment_for(target)
        if current is None or current.revoked:
            raise RoleAuthorizationError("unknown or revoked node")
        updated = replace(current, paused=True)
        return self._replace_assignment(updated)

    def resume(self, *, actor: RoleAssignment, target: NodeId) -> RoleState:
        self._assert_control(actor)
        current = self.assignment_for(target)
        if current is None or current.revoked:
            raise RoleAuthorizationError("unknown or revoked node")
        return self._replace_assignment(replace(current, paused=False))

    def clear_revocation(self, target: NodeId) -> RoleState:
        """Drop a stale revoked assignment so a freshly paired node is unblocked.

        Trust and cluster role are separate owners: revoking trust for a node
        also revokes its role (see revoke_node), but re-establishing trust
        through a fresh pairing invite does not by itself touch role state.
        Without this, a revoked-then-re-paired node's role assignment stays
        permanently revoked and every role operation against it keeps failing
        with "revoked node requires a new pairing invite" even after that
        invite has happened. Callers apply this once trust is re-established.
        """

        current = self.assignment_for(target)
        if current is None or not current.revoked:
            return self
        return replace(
            self,
            assignments=tuple(
                item for item in self.assignments if item.node_id != target
            ),
        )

    def revoke(self, *, actor: RoleAssignment, target: NodeId) -> RoleState:
        self._assert_control(actor)
        current = self.assignment_for(target)
        if current is None or current.revoked:
            raise RoleAuthorizationError("unknown or revoked node")
        updated = self._replace_assignment(
            replace(current, revoked=True, has_active_job=False)
        )
        return replace(
            updated,
            capability_grants=tuple(
                item
                for item in updated.capability_grants
                if item.subject != target and item.target != target
            ),
        )

    def remove_job(self, *, actor: RoleAssignment, target: NodeId) -> RoleState:
        self._assert_control(actor)
        current = self.assignment_for(target)
        if current is None or current.revoked:
            raise RoleAuthorizationError("unknown or revoked node")
        if ClusterRole.WORKER not in current.roles:
            raise RoleAuthorizationError("target has no worker role")
        return self._replace_assignment(replace(current, has_active_job=False))

    def assign_job(self, *, actor: RoleAssignment, target: NodeId) -> RoleState:
        self._assert_control(actor)
        current = self.assignment_for(target)
        if current is None or current.revoked:
            raise RoleAuthorizationError("unknown or revoked node")
        if ClusterRole.WORKER not in current.roles:
            raise RoleAuthorizationError("target has no worker role")
        return self._replace_assignment(replace(current, has_active_job=True))

    def _assert_control(self, actor: RoleAssignment) -> None:
        if ClusterRole.COORDINATOR not in actor.roles or actor.paused or actor.revoked:
            raise RoleAuthorizationError("only an active Coordinator may control peers")

    def _replace_assignment(self, assignment: RoleAssignment) -> RoleState:
        return replace(
            self,
            assignments=tuple(
                assignment if item.node_id == assignment.node_id else item
                for item in self.assignments
            ),
        )


def new_fencing_token() -> str:
    return secrets.token_hex(32)


def renew_lease(
    epoch: CoordinatorEpoch,
    *,
    coordinator_id: NodeId,
    fencing_token: str,
    now: float,
    lease_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
) -> CoordinatorEpoch:
    if epoch.coordinator_id != coordinator_id or not hmac.compare_digest(
        epoch.fencing_token, fencing_token
    ):
        raise FencingError("heartbeat fencing token is stale")
    if now > epoch.lease_expires_at:
        raise FencingError("coordinator lease has expired")
    return replace(
        epoch,
        issued_at=now,
        lease_expires_at=max(epoch.lease_expires_at, now + lease_seconds),
    )


def can_promote(
    state: RoleState,
    *,
    subcoordinator_id: NodeId,
    now: float,
    authenticated: bool = True,
) -> bool:
    if not authenticated or state.epoch is None or now < state.epoch.lease_expires_at:
        return False
    assignment = state.assignment_for(subcoordinator_id)
    return bool(
        assignment
        and ClusterRole.SUBCOORDINATOR in assignment.roles
        and not assignment.revoked
        and not assignment.paused
        and state.epoch.epoch not in state.promotion_epochs
    )


def promote_subcoordinator(
    state: RoleState,
    *,
    now: float,
    authenticated: bool = True,
) -> PromotionDecision:
    if state.epoch is None:
        raise FencingError("no coordinator epoch exists")
    sub = next(
        (
            item
            for item in state.assignments
            if ClusterRole.SUBCOORDINATOR in item.roles
            and not item.revoked
            and not item.paused
        ),
        None,
    )
    if (
        sub is None
        or sub.node_id is None
        or not can_promote(
            state, subcoordinator_id=sub.node_id, now=now, authenticated=authenticated
        )
    ):
        raise FencingError("subcoordinator cannot promote")
    next_epoch = CoordinatorEpoch(
        epoch=state.epoch.epoch + 1,
        coordinator_id=sub.node_id,
        fencing_token=new_fencing_token(),
        issued_at=now,
        lease_expires_at=now + HEARTBEAT_TIMEOUT_SECONDS,
    )
    promoted = RoleAssignment(
        frozenset({ClusterRole.COORDINATOR, ClusterRole.WORKER}), node_id=sub.node_id
    )
    assignments = tuple(
        promoted
        if item.node_id == sub.node_id
        else replace(item, roles=frozenset({ClusterRole.WORKER}))
        for item in state.assignments
    )
    return PromotionDecision(promoted, next_epoch, assignments)


def rejoin_as_worker(
    state: RoleState, *, node_id: NodeId, current_epoch: int
) -> RoleState:
    """Fence a returning former Coordinator into the current Worker epoch."""

    if state.epoch is not None and current_epoch < state.epoch.epoch:
        raise FencingError("returning node presented a stale epoch")
    assignment = state.assignment_for(node_id)
    if assignment is None or ClusterRole.COORDINATOR not in assignment.roles:
        return state
    return state._replace_assignment(
        replace(
            assignment,
            roles=frozenset({ClusterRole.WORKER}),
            paused=False,
            # A returning node cannot resume a prior job; no recoverable job runtime exists.
            has_active_job=False,
        )
    )


def hash_invite(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "HEARTBEAT_TIMEOUT_SECONDS",
    "ClusterRole",
    "CoordinatorEpoch",
    "CoordinatorLease",
    "FencingError",
    "PromotionDecision",
    "RoleAssignment",
    "RoleAuthorizationError",
    "RoleChange",
    "RoleState",
    "can_promote",
    "hash_invite",
    "new_fencing_token",
    "promote_subcoordinator",
    "rejoin_as_worker",
    "renew_lease",
]
