import unittest

from maintenance.components.cluster_roles import (
    ClusterRole,
    CoordinatorEpoch,
    FencingError,
    RoleAssignment,
    RoleAuthorizationError,
    RoleState,
    promote_subcoordinator,
    rejoin_as_worker,
    renew_lease,
)
from maintenance.nodes import NodeId, NodePermission


class ClusterRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = RoleAssignment(
            frozenset({ClusterRole.COORDINATOR}), NodeId("coord")
        )
        self.worker = RoleAssignment(frozenset({ClusterRole.WORKER}), NodeId("worker"))
        self.sub = RoleAssignment(
            frozenset({ClusterRole.SUBCOORDINATOR}), NodeId("sub")
        )

    def test_coordinator_always_has_worker_role(self) -> None:
        self.assertIn(ClusterRole.WORKER, self.coordinator.roles)

    def test_worker_cannot_assign_roles(self) -> None:
        with self.assertRaises(RoleAuthorizationError):
            RoleState().assign(
                actor=self.worker,
                target=NodeId("peer"),
                roles=frozenset({ClusterRole.WORKER}),
            )

    def test_only_one_subcoordinator(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub))
        with self.assertRaises(RoleAuthorizationError):
            state.assign(
                actor=self.coordinator,
                target=NodeId("other"),
                roles=frozenset({ClusterRole.SUBCOORDINATOR}),
            )

    def test_two_active_coordinators_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RoleState(
                assignments=(
                    self.coordinator,
                    RoleAssignment(
                        frozenset({ClusterRole.COORDINATOR}), NodeId("other")
                    ),
                )
            )

    def test_promotion_increments_epoch_after_timeout(self) -> None:
        state = RoleState(
            assignments=(self.coordinator, self.sub),
            epoch=CoordinatorEpoch(7, NodeId("coord"), "token", 0.0, 100.0),
        )
        decision = promote_subcoordinator(state, now=120.1)
        self.assertEqual(decision.epoch.epoch, 8)
        self.assertEqual(decision.epoch.coordinator_id, NodeId("sub"))

    def test_promotion_before_timeout_is_rejected(self) -> None:
        state = RoleState(
            assignments=(self.coordinator, self.sub),
            epoch=CoordinatorEpoch(7, NodeId("coord"), "token", 0.0, 100.0),
        )
        with self.assertRaises(FencingError):
            promote_subcoordinator(state, now=99.0)

    def test_stale_lease_token_is_rejected(self) -> None:
        epoch = CoordinatorEpoch(1, NodeId("coord"), "token", 0.0, 100.0)
        with self.assertRaises(FencingError):
            renew_lease(
                epoch,
                coordinator_id=NodeId("coord"),
                fencing_token="old",
                now=10.0,
            )

    def test_non_finite_epoch_times_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CoordinatorEpoch(1, NodeId("coord"), "token", float("nan"), 100.0)

    def test_returning_coordinator_is_fenced_to_worker(self) -> None:
        state = RoleState(
            assignments=(self.coordinator, self.sub),
            epoch=CoordinatorEpoch(8, NodeId("sub"), "new", 0.0, 100.0),
        )
        returned = rejoin_as_worker(state, node_id=NodeId("coord"), current_epoch=8)
        assignment = returned.assignment_for(NodeId("coord"))
        assert assignment is not None
        self.assertEqual(assignment.roles, frozenset({ClusterRole.WORKER}))

    def test_remove_job_requires_active_coordinator(self) -> None:
        with self.assertRaises(RoleAuthorizationError):
            RoleState(assignments=(self.worker,)).remove_job(
                actor=self.worker,
                target=NodeId("worker"),
            )

    def test_remove_job_clears_active_assignment(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))
        updated = state.remove_job(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)

    def test_remove_job_revoked_target_is_rejected(self) -> None:
        revoked = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            revoked=True,
        )
        state = RoleState(assignments=(self.coordinator, revoked))
        with self.assertRaises(RoleAuthorizationError):
            state.remove_job(actor=self.coordinator, target=NodeId("worker"))

    def test_remove_job_non_worker_target_is_rejected(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub))
        with self.assertRaises(RoleAuthorizationError):
            state.remove_job(actor=self.coordinator, target=NodeId("sub"))

    def test_assign_job_restores_participation(self) -> None:
        idle = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            has_active_job=False,
        )
        state = RoleState(assignments=(self.coordinator, idle))
        updated = state.assign_job(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertTrue(assignment.has_active_job)

    def test_default_assignment_is_idle(self) -> None:
        self.assertFalse(self.worker.has_active_job)

    def test_clear_revocation_drops_stale_revoked_assignment(self) -> None:
        revoked = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            revoked=True,
        )
        state = RoleState(assignments=(self.coordinator, revoked))

        cleared = state.clear_revocation(NodeId("worker"))

        self.assertIsNone(cleared.assignment_for(NodeId("worker")))

    def test_clear_revocation_allows_reassignment_after_fresh_pairing(self) -> None:
        revoked = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            revoked=True,
        )
        state = RoleState(assignments=(self.coordinator, revoked)).clear_revocation(
            NodeId("worker")
        )

        updated, _change = state.assign(
            actor=self.coordinator,
            target=NodeId("worker"),
            roles=frozenset({ClusterRole.WORKER}),
        )

        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertFalse(assignment.revoked)

    def test_clear_revocation_is_a_no_op_for_active_assignment(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))

        cleared = state.clear_revocation(NodeId("worker"))

        self.assertEqual(cleared.assignment_for(NodeId("worker")), self.worker)

    def test_assign_job_revoked_target_is_rejected(self) -> None:
        revoked = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            revoked=True,
        )
        state = RoleState(assignments=(self.coordinator, revoked))
        with self.assertRaises(RoleAuthorizationError):
            state.assign_job(actor=self.coordinator, target=NodeId("worker"))

    def test_assign_job_non_worker_target_is_rejected(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub))
        with self.assertRaises(RoleAuthorizationError):
            state.assign_job(actor=self.coordinator, target=NodeId("sub"))

    def test_remove_job_unknown_target_is_rejected(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))
        with self.assertRaises(RoleAuthorizationError):
            state.remove_job(actor=self.coordinator, target=NodeId("ghost"))

    def test_assign_job_unknown_target_is_rejected(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))
        with self.assertRaises(RoleAuthorizationError):
            state.assign_job(actor=self.coordinator, target=NodeId("ghost"))

    def test_coordinator_can_grant_scoped_subcoordinator_permissions(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub, self.worker))

        updated = state.grant_capabilities(
            actor=self.coordinator,
            subject=NodeId("sub"),
            target=NodeId("worker"),
            permissions=frozenset(
                {NodePermission.COMPONENT_READ, NodePermission.STORAGE_REVIEW}
            ),
            now=10.0,
            expires_at=100.0,
        )

        grant = updated.capability_grant(NodeId("sub"), NodeId("worker"))
        self.assertIsNotNone(grant)
        assert grant is not None
        self.assertEqual(
            grant.permissions,
            frozenset({NodePermission.COMPONENT_READ, NodePermission.STORAGE_REVIEW}),
        )

    def test_subcoordinator_cannot_grant_or_target_outside_cluster(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub, self.worker))
        with self.assertRaises(RoleAuthorizationError):
            state.grant_capabilities(
                actor=self.sub,
                subject=NodeId("sub"),
                target=NodeId("worker"),
                permissions=frozenset({NodePermission.COMPONENT_READ}),
                now=10.0,
                expires_at=100.0,
            )

    def test_revoking_scoped_capabilities_is_immediate(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub, self.worker))
        state = state.grant_capabilities(
            actor=self.coordinator,
            subject=NodeId("sub"),
            target=NodeId("worker"),
            permissions=frozenset({NodePermission.COMPONENT_READ}),
            now=10.0,
            expires_at=100.0,
        )

        updated = state.revoke_capabilities(
            actor=self.coordinator,
            subject=NodeId("sub"),
            target=NodeId("worker"),
        )

        self.assertIsNone(updated.capability_grant(NodeId("sub"), NodeId("worker")))

    def test_revoking_a_node_removes_related_capability_grants(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub, self.worker))
        state = state.grant_capabilities(
            actor=self.coordinator,
            subject=NodeId("sub"),
            target=NodeId("worker"),
            permissions=frozenset({NodePermission.COMPONENT_READ}),
            now=10.0,
            expires_at=100.0,
        )

        updated = state.revoke(actor=self.coordinator, target=NodeId("worker"))

        self.assertEqual(updated.capability_grants, ())

    def test_authority_is_full_for_coordinator_and_scoped_for_subcoordinator(
        self,
    ) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub, self.worker))
        self.assertTrue(
            state.allows(
                subject=NodeId("coord"),
                target=NodeId("worker"),
                permission=NodePermission.CLEANUP,
                now=10.0,
            )
        )
        self.assertFalse(
            state.allows(
                subject=NodeId("sub"),
                target=NodeId("worker"),
                permission=NodePermission.CLEANUP,
                now=10.0,
            )
        )

    def test_remove_job_paused_worker_is_allowed(self) -> None:
        paused = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            paused=True,
        )
        state = RoleState(assignments=(self.coordinator, paused))
        updated = state.remove_job(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertTrue(assignment.paused)
        self.assertFalse(assignment.has_active_job)

    def test_pause_resume_preserves_active_job_state(self) -> None:
        idle = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            has_active_job=False,
        )
        state = RoleState(assignments=(self.coordinator, idle))
        resumed = state.resume(actor=self.coordinator, target=NodeId("worker"))
        assignment = resumed.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)

    def test_remove_job_is_idempotent(self) -> None:
        idle = RoleAssignment(
            frozenset({ClusterRole.WORKER}),
            NodeId("worker"),
            has_active_job=False,
        )
        state = RoleState(assignments=(self.coordinator, idle))
        updated = state.remove_job(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)

    def test_assign_job_is_idempotent(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))
        updated = state.assign_job(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertTrue(assignment.has_active_job)

    def test_subcoordinator_is_not_treated_as_job_holder(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.sub))
        with self.assertRaises(RoleAuthorizationError):
            state.remove_job(actor=self.coordinator, target=NodeId("sub"))

    def test_role_assign_preserves_idle_occupancy(self) -> None:
        state = RoleState(assignments=(self.coordinator, self.worker))
        updated, _ = state.assign(
            actor=self.coordinator,
            target=NodeId("worker"),
            roles=frozenset({ClusterRole.SUBCOORDINATOR}),
        )
        assignment = updated.assignment_for(NodeId("worker"))
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)

    def test_role_assign_preserves_active_occupancy(self) -> None:
        active_worker = RoleAssignment(
            frozenset({ClusterRole.WORKER}), NodeId("worker"), has_active_job=True
        )
        state = RoleState(assignments=(self.coordinator, active_worker))
        updated, _ = state.assign(
            actor=self.coordinator,
            target=NodeId("worker"),
            roles=frozenset({ClusterRole.SUBCOORDINATOR}),
        )
        assignment = updated.assignment_for(NodeId("worker"))
        assert assignment is not None
        self.assertTrue(assignment.has_active_job)

    def test_new_assignment_via_assign_is_idle(self) -> None:
        state = RoleState(assignments=(self.coordinator,))
        updated, _ = state.assign(
            actor=self.coordinator,
            target=NodeId("new-worker"),
            roles=frozenset({ClusterRole.WORKER}),
        )
        assignment = updated.assignment_for(NodeId("new-worker"))
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)

    def test_revoke_clears_active_job(self) -> None:
        active_worker = RoleAssignment(
            frozenset({ClusterRole.WORKER}), NodeId("worker"), has_active_job=True
        )
        state = RoleState(assignments=(self.coordinator, active_worker))
        updated = state.revoke(actor=self.coordinator, target=NodeId("worker"))
        assignment = updated.assignment_for(NodeId("worker"))
        assert assignment is not None
        self.assertTrue(assignment.revoked)
        self.assertFalse(assignment.has_active_job)

    def test_rejoin_as_worker_clears_active_job(self) -> None:
        active_coord = RoleAssignment(
            frozenset({ClusterRole.COORDINATOR, ClusterRole.WORKER}),
            NodeId("coord"),
            has_active_job=True,
        )
        state = RoleState(
            assignments=(active_coord, self.sub),
            epoch=CoordinatorEpoch(8, NodeId("sub"), "fence", 0.0, 100.0),
        )
        returned = rejoin_as_worker(state, node_id=NodeId("coord"), current_epoch=8)
        assignment = returned.assignment_for(NodeId("coord"))
        assert assignment is not None
        self.assertFalse(assignment.has_active_job)
