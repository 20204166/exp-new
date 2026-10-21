from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock

from expra_connect import NodeId, RelationshipState
from expra_connect.surfaces.registry import SurfaceAccess, SurfaceDefinition

from maintenance.connect_grants import (
    ConnectSurfaceGrantPolicy,
    ConnectSurfaceGrantStore,
    SurfaceGrantApproval,
)


class ConnectSurfaceGrantStoreTests(unittest.TestCase):
    def test_round_trips_v2_and_writes_exact_sorted_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            store = ConnectSurfaceGrantStore(path)
            approvals = frozenset(
                {
                    SurfaceGrantApproval(
                        NodeId("peer-b"), "exp.cluster-history", SurfaceAccess.REVIEW
                    ),
                    SurfaceGrantApproval(
                        NodeId("peer-a"), "exp.process-history", SurfaceAccess.READ
                    ),
                    SurfaceGrantApproval(
                        NodeId("peer-a"), "exp.process-actions", SurfaceAccess.ACTION
                    ),
                }
            )

            store.save(approvals)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "schema_version": 2,
                    "grants": [
                        {
                            "peer_id": "peer-a",
                            "surface_id": "exp.process-actions",
                            "access": "action",
                        },
                        {
                            "peer_id": "peer-a",
                            "surface_id": "exp.process-history",
                            "access": "read",
                        },
                        {
                            "peer_id": "peer-b",
                            "surface_id": "exp.cluster-history",
                            "access": "review",
                        },
                    ],
                },
            )
            first_serialization = path.read_bytes()
            store.save(approvals)
            self.assertEqual(path.read_bytes(), first_serialization)
            self.assertEqual(store.load(), approvals)

    def test_missing_policy_file_is_an_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConnectSurfaceGrantStore(Path(directory) / "missing.json")

            self.assertEqual(store.load(), frozenset())

    def test_v1_permissions_translate_without_rewriting_until_explicit_save(
        self,
    ) -> None:
        legacy_grants = {
            "peer-a": ["process_termination", "process_force_termination"],
            "peer-b": ["cluster_snapshot_receive", "cluster_standby_receive"],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            original = json.dumps(
                {
                    "schema_version": 1,
                    "grants": [
                        {"peer_id": peer_id, "permissions": permissions}
                        for peer_id, permissions in legacy_grants.items()
                    ],
                }
            )
            path.write_text(original, encoding="utf-8")
            store = ConnectSurfaceGrantStore(path)

            approvals = store.load()

            self.assertEqual(
                approvals,
                frozenset(
                    {
                        SurfaceGrantApproval(
                            NodeId("peer-a"),
                            "exp.process-termination",
                            SurfaceAccess.ACTION,
                        ),
                        SurfaceGrantApproval(
                            NodeId("peer-a"),
                            "exp.process-force-termination",
                            SurfaceAccess.ACTION,
                        ),
                        SurfaceGrantApproval(
                            NodeId("peer-b"),
                            "exp.cluster-history",
                            SurfaceAccess.ACTION,
                        ),
                        SurfaceGrantApproval(
                            NodeId("peer-b"),
                            "exp.cluster-standby-history",
                            SurfaceAccess.ACTION,
                        ),
                    }
                ),
            )
            self.assertEqual(path.read_text(encoding="utf-8"), original)

            store.save(approvals)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["schema_version"], 2
            )

    def test_malformed_policy_fails_closed_without_replacing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            original = '{"schema_version":2,"grants":[{"peer_id":"peer"}]}'
            path.write_text(original, encoding="utf-8")
            store = ConnectSurfaceGrantStore(path)

            with self.assertRaises(ValueError):
                store.load()

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_unsupported_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            path.write_text('{"schema_version":3,"grants":[]}', encoding="utf-8")

            with self.assertRaises(ValueError):
                ConnectSurfaceGrantStore(path).load()

    def test_unreadable_policy_fails_closed_instead_of_becoming_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            path.mkdir()

            with self.assertRaises((OSError, RuntimeError)):
                ConnectSurfaceGrantStore(path).load()

    def test_invalid_replacement_does_not_overwrite_malformed_existing_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            original = '{"schema_version":2,"grants":[{"peer_id":"bad"}]}'
            path.write_text(original, encoding="utf-8")
            store = ConnectSurfaceGrantStore(path)

            with self.assertRaises(ValueError):
                store.save(
                    [
                        SurfaceGrantApproval(
                            NodeId("peer"), "exp.surface", SurfaceAccess.READ
                        )
                    ]
                )

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_duplicate_and_invalid_approvals_are_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface-grants.json"
            store = ConnectSurfaceGrantStore(path)
            approval = SurfaceGrantApproval(
                NodeId("peer"), "exp.surface", SurfaceAccess.READ
            )

            with self.assertRaises(ValueError):
                store.save([approval, approval])
            self.assertFalse(path.exists())

            with self.assertRaises(ValueError):
                SurfaceGrantApproval(NodeId("local"), "exp.surface", SurfaceAccess.READ)
            with self.assertRaises(ValueError):
                SurfaceGrantApproval(NodeId("peer"), " " * 129, SurfaceAccess.READ)
            with self.assertRaises(TypeError):
                SurfaceGrantApproval(
                    NodeId("peer"),
                    "exp.surface",
                    cast(SurfaceAccess, "invoke"),
                )


class ConnectSurfaceGrantPolicyTests(unittest.TestCase):
    @staticmethod
    def make_adapter(
        relationship: RelationshipState = RelationshipState.INBOUND_GRANTED,
    ) -> Mock:
        adapter = Mock()
        adapter.peer_relationship.return_value = relationship
        adapter.surface_definitions = {
            "exp.read-only": SurfaceDefinition(
                "exp.read-only", lambda _peer, _params: {}, None, {}
            ),
            "exp.actions": SurfaceDefinition(
                "exp.actions",
                lambda _peer, _params: {},
                None,
                {"do": lambda _peer, _params: None},
            ),
        }
        return adapter

    def test_policy_requires_inbound_authority_not_outbound_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConnectSurfaceGrantStore(Path(directory) / "policy.json")
            policy = ConnectSurfaceGrantPolicy(store)
            adapter = self.make_adapter(RelationshipState.OUTBOUND_TRUSTED)
            approval = SurfaceGrantApproval(
                NodeId("peer"), "exp.actions", SurfaceAccess.ACTION
            )

            with self.assertRaises(PermissionError):
                policy.grant(adapter, approval)

            adapter.grant_surface_access.assert_not_called()

    def test_policy_rejects_unregistered_or_unsupported_access_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ConnectSurfaceGrantPolicy(
                ConnectSurfaceGrantStore(Path(directory) / "policy.json")
            )
            adapter = self.make_adapter()

            for approval in (
                SurfaceGrantApproval(NodeId("peer"), "missing", SurfaceAccess.READ),
                SurfaceGrantApproval(
                    NodeId("peer"), "exp.read-only", SurfaceAccess.ACTION
                ),
            ):
                with (
                    self.subTest(approval=approval),
                    self.assertRaises(ValueError),
                ):
                    policy.grant(adapter, approval)

            adapter.grant_surface_access.assert_not_called()

    def test_policy_grants_and_persists_only_eligible_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ConnectSurfaceGrantPolicy(
                ConnectSurfaceGrantStore(Path(directory) / "policy.json")
            )
            adapter = self.make_adapter(RelationshipState.BIDIRECTIONAL)
            approval = SurfaceGrantApproval(
                NodeId("peer"), "exp.actions", SurfaceAccess.ACTION
            )

            policy.grant(adapter, approval)

            adapter.grant_surface_access.assert_called_once_with(
                NodeId("peer"), "exp.actions", access=SurfaceAccess.ACTION
            )
            self.assertEqual(policy.applied_approvals, frozenset({approval}))
            self.assertEqual(policy.desired_approvals, frozenset({approval}))
            self.assertEqual(policy.store.load(), frozenset({approval}))

    def test_reapply_filters_direction_registration_and_surface_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConnectSurfaceGrantStore(Path(directory) / "policy.json")
            allowed = SurfaceGrantApproval(
                NodeId("inbound"), "exp.actions", SurfaceAccess.ACTION
            )
            outbound = SurfaceGrantApproval(
                NodeId("outbound"), "exp.actions", SurfaceAccess.ACTION
            )
            missing = SurfaceGrantApproval(
                NodeId("missing"), "not.registered", SurfaceAccess.READ
            )
            unsupported = SurfaceGrantApproval(
                NodeId("unsupported"), "exp.read-only", SurfaceAccess.ACTION
            )
            store.save([allowed, outbound, missing, unsupported])
            policy = ConnectSurfaceGrantPolicy(store)
            adapter = self.make_adapter()
            adapter.peer_relationship.side_effect = lambda peer: {
                "inbound": RelationshipState.INBOUND_GRANTED,
                "outbound": RelationshipState.OUTBOUND_TRUSTED,
                "missing": RelationshipState.INBOUND_GRANTED,
                "unsupported": RelationshipState.INBOUND_GRANTED,
            }[peer.value]

            applied = policy.reapply(adapter)

            self.assertEqual(applied, frozenset({allowed}))
            adapter.grant_surface_access.assert_called_once_with(
                allowed.peer_id, allowed.surface_id, access=allowed.access
            )

    def test_policy_compensates_runtime_grant_when_save_fails(self) -> None:
        store = Mock()
        store.load.return_value = frozenset()
        store.save.side_effect = RuntimeError("disk unavailable")
        policy = ConnectSurfaceGrantPolicy(store)
        adapter = self.make_adapter()
        approval = SurfaceGrantApproval(
            NodeId("peer"), "exp.actions", SurfaceAccess.ACTION
        )

        with self.assertRaises(RuntimeError):
            policy.grant(adapter, approval)

        adapter.revoke_surface_access.assert_called_once_with(
            NodeId("peer"), "exp.actions", access=SurfaceAccess.ACTION
        )
        self.assertEqual(policy.applied_approvals, frozenset())

    def test_unreadable_policy_clears_tracked_runtime_grants(self) -> None:
        store = Mock()
        store.load.return_value = frozenset()
        policy = ConnectSurfaceGrantPolicy(store)
        adapter = self.make_adapter()
        approval = SurfaceGrantApproval(
            NodeId("peer"), "exp.actions", SurfaceAccess.ACTION
        )
        policy.grant(adapter, approval)
        adapter.revoke_surface_access.reset_mock()
        store.load.side_effect = ValueError("malformed")

        with self.assertRaises(RuntimeError):
            policy.reapply(adapter)

        adapter.revoke_surface_access.assert_called_once_with(
            NodeId("peer"), "exp.actions", access=SurfaceAccess.ACTION
        )
        self.assertEqual(policy.desired_approvals, frozenset())
        self.assertEqual(policy.applied_approvals, frozenset())

    def test_revoke_persists_removal_before_reporting_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = ConnectSurfaceGrantPolicy(
                ConnectSurfaceGrantStore(Path(directory) / "policy.json")
            )
            adapter = self.make_adapter()
            approval = SurfaceGrantApproval(
                NodeId("peer"), "exp.actions", SurfaceAccess.ACTION
            )
            policy.grant(adapter, approval)
            adapter.revoke_surface_access.side_effect = RuntimeError("runtime failure")

            with self.assertRaises(RuntimeError):
                policy.revoke(adapter, approval)

            self.assertEqual(policy.store.load(), frozenset())
            self.assertEqual(policy.desired_approvals, frozenset())
            self.assertEqual(policy.applied_approvals, frozenset({approval}))


if __name__ == "__main__":
    unittest.main()
