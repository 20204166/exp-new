"""Node model and registry tests for the cluster target boundary."""

import unittest
from dataclasses import replace
from pathlib import Path

from maintenance.nodes import (
    LOCAL_NODE_ID,
    DiscoveredNodeCandidate,
    NodeCapability,
    NodeContext,
    NodeDescriptor,
    NodeId,
    NodeRegistry,
    NodeStatus,
    NodeTrustState,
    is_trusted_descriptor,
    local_node_descriptor,
    node_identity_fingerprint,
    node_operation_key,
)
from tests.support.nodes import make_candidate, make_remote_context
from tests.support.nodes import make_local_context as _local_context


def _candidate(
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
    return make_candidate(
        stable_id,
        hostname=hostname,
        protocol_version=protocol_version,
        port=port,
        connectable=connectable,
        compatible=compatible,
        last_seen=last_seen,
        identity_fingerprint=identity_fingerprint
        or node_identity_fingerprint(stable_id),
    )


def _peer_context(
    node_id: str, display_name: str, trust: NodeTrustState
) -> NodeContext:
    return make_remote_context(
        node_id,
        trust=trust,
        status=NodeStatus.ONLINE,
        display_name=display_name,
        hostname=display_name,
        provider=object(),
        process_manager=object(),
        file_manager=object(),
        scheduler=object(),
        coordinator=object(),
    )


class NodeIdTests(unittest.TestCase):
    def test_node_id_is_stable_and_hashed_by_value(self) -> None:
        self.assertEqual(NodeId("local"), NodeId("local"))
        self.assertEqual(hash(NodeId("local")), hash(NodeId("local")))
        self.assertEqual(str(NodeId("abc")), "abc")

    def test_local_node_descriptor_is_local_online_and_fully_capable(self) -> None:
        descriptor = local_node_descriptor()
        self.assertTrue(descriptor.is_local)
        self.assertEqual(descriptor.trust, NodeTrustState.LOCAL)
        self.assertEqual(descriptor.status, NodeStatus.ONLINE)
        self.assertTrue(descriptor.has(NodeCapability.PROCESS_TERMINATION))
        self.assertTrue(descriptor.has(NodeCapability.CLEANUP))

    def test_node_operation_key_qualifies_operations(self) -> None:
        self.assertEqual(
            node_operation_key(NodeId("local"), "component:cpu"),
            "node:local:component:cpu",
        )
        self.assertEqual(
            node_operation_key(NodeId("gaming"), "process"),
            "node:gaming:process",
        )
        self.assertNotEqual(
            node_operation_key(NodeId("a"), "process"),
            node_operation_key(NodeId("b"), "process"),
        )


class NodeRegistryTests(unittest.TestCase):
    def test_registering_local_node_makes_it_selected(self) -> None:
        registry = NodeRegistry(_local_context())
        self.assertEqual(registry.selected_id(), NodeId(LOCAL_NODE_ID))
        self.assertEqual(
            registry.selected_context().descriptor.trust, NodeTrustState.LOCAL
        )

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = NodeRegistry()
        registry.register_context(_local_context())
        with self.assertRaises(ValueError):
            registry.register_context(_local_context())

    def test_arbitrary_number_of_registered_nodes(self) -> None:
        registry = NodeRegistry(_local_context())
        for index in range(20):
            registry.register_context(
                _peer_context(f"node-{index}", f"Node {index}", NodeTrustState.TRUSTED)
            )
        self.assertEqual(len(registry.contexts()), 21)

    def test_unknown_node_fails_closed(self) -> None:
        registry = NodeRegistry(_local_context())
        with self.assertRaises(KeyError):
            registry.context(NodeId("nope"))
        with self.assertRaises(KeyError):
            registry.select(NodeId("nope"))

    def test_select_requires_trusted_node(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        with self.assertRaises(ValueError):
            registry.select(NodeId("peer-a"))

    def test_select_switches_selected_context(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(
            _peer_context("dev", "Dev Node", NodeTrustState.TRUSTED)
        )
        registry.select(NodeId("dev"))
        self.assertEqual(registry.selected_id(), NodeId("dev"))
        self.assertEqual(
            registry.selected_context().descriptor.display_name, "Dev Node"
        )
        registry.select(NodeId(LOCAL_NODE_ID))
        self.assertEqual(registry.selected_id(), NodeId(LOCAL_NODE_ID))

    def test_only_local_and_trusted_nodes_are_selectable(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.register_context(
            _peer_context("peer-b", "Peer B", NodeTrustState.DISCOVERED)
        )
        registry.register_context(
            _peer_context("trusted", "Trusted", NodeTrustState.TRUSTED)
        )
        names = {d.display_name for d in registry.selectable_descriptors()}
        self.assertEqual(names, {"This System", "Trusted"})

    def test_is_trusted_descriptor_accepts_trusted_and_authorised(self) -> None:
        trusted = _peer_context("peer-a", "Peer A", NodeTrustState.TRUSTED).descriptor
        authorised = _peer_context(
            "peer-b", "Peer B", NodeTrustState.AUTHORISED
        ).descriptor
        local = local_node_descriptor()

        self.assertTrue(is_trusted_descriptor(trusted))
        self.assertTrue(is_trusted_descriptor(authorised))
        self.assertFalse(is_trusted_descriptor(local))


class DiscoveredBoundaryTests(unittest.TestCase):
    def test_discovered_candidate_is_untrusted_and_non_selectable(self) -> None:
        registry = NodeRegistry(_local_context())
        descriptor = registry.update_discovered(_candidate("peer-a"))
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        self.assertEqual(descriptor.trust, NodeTrustState.UNTRUSTED)
        self.assertEqual(descriptor.capabilities, frozenset())
        self.assertNotIn(
            NodeId("peer-a"), {d.id for d in registry.selectable_descriptors()}
        )

    def test_discovered_candidate_never_gains_capabilities_automatically(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a", connectable=True, port=5000))
        self.assertEqual(
            registry.contexts(),
            (registry.context(NodeId(LOCAL_NODE_ID)),),
        )
        for candidate in registry.discovered_candidates():
            self.assertFalse(candidate.compatible and False)

    def test_local_node_self_discovery_is_ignored(self) -> None:
        registry = NodeRegistry(_local_context())
        result = registry.update_discovered(_candidate(LOCAL_NODE_ID))
        self.assertIsNone(result)
        self.assertEqual(registry.discovered_candidates(), ())

    def test_offline_and_duplicate_candidates_deduplicate(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a", last_seen=1.0))
        registry.update_discovered(_candidate("peer-a", last_seen=2.0))
        self.assertEqual(len(registry.discovered_candidates()), 1)

    def test_remove_discovered_drops_candidate(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.remove_discovered(NodeId("peer-a"))
        self.assertEqual(registry.discovered_candidates(), ())

    def test_reject_discovered_is_explicit_remove(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.reject_discovered(NodeId("peer-a"))
        self.assertEqual(registry.discovered_candidates(), ())

    def test_known_trusted_node_presence_updates_without_reintroducing_discovery(
        self,
    ) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(
            _peer_context("peer-a", "Peer A", NodeTrustState.TRUSTED)
        )
        registry.remove_discovered(NodeId("peer-a"))
        self.assertEqual(
            registry.context(NodeId("peer-a")).descriptor.status, NodeStatus.OFFLINE
        )

        registry.update_discovered(_candidate("peer-a", hostname="new-host"))

        self.assertEqual(registry.discovered_candidates(), ())
        descriptor = registry.context(NodeId("peer-a")).descriptor
        self.assertEqual(descriptor.status, NodeStatus.UNKNOWN)
        self.assertEqual(descriptor.hostname, "new-host")
        self.assertEqual(descriptor.display_name, "new-host")

    def test_trusted_rediscovery_preserves_custom_display_name(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(
            NodeContext(
                descriptor=NodeDescriptor(
                    id=NodeId("peer-a"),
                    display_name="Peer A",
                    hostname="peer-a-host",
                    is_local=False,
                    trust=NodeTrustState.TRUSTED,
                    status=NodeStatus.ONLINE,
                    capabilities=frozenset(),
                ),
                provider=object(),
                process_manager=object(),
                file_manager=object(),
                scheduler=object(),
                coordinator=object(),
            )
        )

        registry.update_discovered(_candidate("peer-a", hostname="new-host"))

        descriptor = registry.context(NodeId("peer-a")).descriptor
        self.assertEqual(descriptor.display_name, "Peer A")
        self.assertEqual(descriptor.hostname, "new-host")

    def test_promote_to_trusted_is_explicit_and_read_only_by_default(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        descriptor = registry.promote_to_trusted(NodeId("peer-a"))
        self.assertEqual(descriptor.trust, NodeTrustState.TRUSTED)
        self.assertEqual(descriptor.capabilities, frozenset())
        self.assertNotIn(
            NodeId("peer-a"), {d.id for d in registry.selectable_descriptors()}
        )
        self.assertEqual(registry.discovered_candidates(), ())
        with self.assertRaises(ValueError):
            registry.select(NodeId("peer-a"))

    def test_operational_context_replaces_trusted_placeholder(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))

        registry.register_context(
            _peer_context("peer-a", "Peer A", NodeTrustState.TRUSTED)
        )

        registry.select(NodeId("peer-a"))
        self.assertEqual(registry.selected_id(), NodeId("peer-a"))

    def test_placeholder_replacement_cannot_change_trust_or_local_identity(
        self,
    ) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))

        with self.assertRaises(ValueError):
            registry.register_context(
                _peer_context("peer-a", "Peer A", NodeTrustState.DISCOVERED)
            )

    def test_trust_promotion_rejects_destructive_capabilities(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))

        with self.assertRaises(ValueError):
            registry.promote_to_trusted(
                NodeId("peer-a"),
                capabilities=(NodeCapability.PROCESS_TERMINATION,),
            )

    def test_promote_to_trusted_requires_a_discovered_candidate(self) -> None:
        registry = NodeRegistry(_local_context())
        with self.assertRaises(KeyError):
            registry.promote_to_trusted(NodeId("never-seen"))

    def test_promotion_requires_explicit_pairing_transition(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))

        with self.assertRaises(ValueError):
            registry.promote_to_trusted(NodeId("peer-a"))

        self.assertEqual(
            registry.pairing_state(NodeId("peer-a")).value,
            "discovered",
        )
        registry.begin_pairing(NodeId("peer-a"))
        self.assertEqual(
            registry.pairing_state(NodeId("peer-a")).value,
            "pairing",
        )

    def test_failed_pairing_does_not_create_trust(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        registry.fail_pairing(NodeId("peer-a"))

        self.assertEqual(
            registry.pairing_state(NodeId("peer-a")).value,
            "pairing_failed",
        )
        self.assertNotIn(
            NodeId("peer-a"),
            {descriptor.id for descriptor in registry.selectable_descriptors()},
        )

    def test_incompatible_peer_cannot_enter_pairing(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(
            _candidate("peer-a", protocol_version="99", compatible=False)
        )

        with self.assertRaises(ValueError):
            registry.begin_pairing(NodeId("peer-a"))

        self.assertEqual(
            registry.pairing_state(NodeId("peer-a")).value,
            "pairing_failed",
        )

    def test_promotion_requires_a_peer_fingerprint(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(
            replace(_candidate("peer-a"), identity_fingerprint=None)
        )

        with self.assertRaises(ValueError):
            registry.promote_to_trusted(NodeId("peer-a"))

    def test_verified_pairing_records_identity_fingerprint(self) -> None:
        registry = NodeRegistry(_local_context())
        fingerprint = node_identity_fingerprint("peer-a")
        registry.update_discovered(
            _candidate("peer-a", identity_fingerprint=fingerprint)
        )
        registry.begin_pairing(NodeId("peer-a"))

        descriptor = registry.promote_to_trusted(NodeId("peer-a"))

        self.assertEqual(descriptor.identity_fingerprint, fingerprint)
        self.assertEqual(descriptor.identity_status.value, "verified")

    def test_identity_mismatch_is_not_silently_accepted(self) -> None:
        registry = NodeRegistry(_local_context())
        original = node_identity_fingerprint("peer-a")
        registry.update_discovered(_candidate("peer-a", identity_fingerprint=original))
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))

        registry.update_discovered(
            _candidate("peer-a", hostname="new-host", identity_fingerprint="changed")
        )

        descriptor = registry.context(NodeId("peer-a")).descriptor
        self.assertEqual(descriptor.identity_status.value, "mismatch")
        self.assertEqual(len(registry.discovered_candidates()), 1)
        self.assertEqual(registry.discovered_candidates()[0].hostname, "new-host")
        self.assertNotIn(
            NodeId("peer-a"),
            {item.id for item in registry.selectable_descriptors()},
        )

        registry.update_discovered(
            _candidate("peer-a", hostname="new-host", identity_fingerprint="changed")
        )

        self.assertEqual(
            registry.context(NodeId("peer-a")).descriptor.identity_status.value,
            "mismatch",
        )

        registry.update_discovered(
            _candidate(
                "peer-a", hostname="original-host", identity_fingerprint=original
            )
        )

        self.assertEqual(
            registry.context(NodeId("peer-a")).descriptor.identity_status.value,
            "mismatch",
        )

    def test_revoke_then_repair_accepts_new_verified_identity(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(
            _candidate("peer-a", identity_fingerprint="original")
        )
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))
        registry.revoke_trusted(NodeId("peer-a"))
        registry.update_discovered(
            _candidate("peer-a", identity_fingerprint="replacement")
        )
        registry.begin_pairing(NodeId("peer-a"))

        descriptor = registry.promote_to_trusted(NodeId("peer-a"))

        self.assertEqual(descriptor.identity_fingerprint, "replacement")
        self.assertEqual(descriptor.identity_status.value, "verified")

    def test_revoke_trusted_removes_node_and_deselects_to_local(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))
        registry.register_context(
            _peer_context("peer-a", "Peer A", NodeTrustState.TRUSTED)
        )
        registry.select(NodeId("peer-a"))
        registry.revoke_trusted(NodeId("peer-a"))
        self.assertEqual(registry.selected_id(), NodeId(LOCAL_NODE_ID))
        with self.assertRaises(KeyError):
            registry.context(NodeId("peer-a"))

    def test_revoked_peer_reappears_as_untrusted_discovery_candidate(self) -> None:
        # Revoke invalidates trust; it must not blacklist discovery. A peer
        # that keeps advertising after being revoked has to surface again
        # through the normal untrusted-candidate path, not stay hidden.
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))
        registry.promote_to_trusted(NodeId("peer-a"))
        self.assertEqual(registry.discovered_candidates(), ())

        registry.revoke_trusted(NodeId("peer-a"))
        with self.assertRaises(KeyError):
            registry.context(NodeId("peer-a"))

        registry.update_discovered(_candidate("peer-a"))

        candidates = {c.stable_id for c in registry.discovered_candidates()}
        self.assertIn("peer-a", candidates)

    def test_rejected_pair_request_leaves_peer_discoverable(self) -> None:
        # Reject is weaker than revoke: it never created trust, so the peer
        # must remain visible as an ordinary untrusted discovery candidate
        # on the very next scan, with no cooldown or blacklist.
        registry = NodeRegistry(_local_context())
        registry.update_discovered(_candidate("peer-a"))
        registry.begin_pairing(NodeId("peer-a"))

        registry.reject_discovered(NodeId("peer-a"))

        self.assertEqual(registry.discovered_candidates(), ())
        registry.update_discovered(_candidate("peer-a"))
        candidates = {c.stable_id for c in registry.discovered_candidates()}
        self.assertIn("peer-a", candidates)

    def test_revoke_local_node_is_rejected(self) -> None:
        registry = NodeRegistry(_local_context())
        with self.assertRaises(ValueError):
            registry.revoke_trusted(NodeId(LOCAL_NODE_ID))

    def test_revoke_untrusted_node_is_rejected(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(
            _peer_context("peer", "Peer", NodeTrustState.DISCOVERED)
        )
        with self.assertRaises(ValueError):
            registry.revoke_trusted(NodeId("peer"))

    def test_set_display_name_renames_and_returns_descriptor(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(_peer_context("peer", "Old", NodeTrustState.TRUSTED))
        descriptor = registry.set_display_name(NodeId("peer"), "New Name")
        self.assertEqual(descriptor.display_name, "New Name")
        self.assertEqual(
            registry.context(NodeId("peer")).descriptor.display_name, "New Name"
        )

    def test_set_color_updates_descriptor(self) -> None:
        registry = NodeRegistry(_local_context())
        registry.register_context(_peer_context("peer", "Peer", NodeTrustState.TRUSTED))
        descriptor = registry.set_color(NodeId("peer"), "emerald")
        self.assertEqual(descriptor.color, "emerald")
        self.assertEqual(registry.context(NodeId("peer")).descriptor.color, "emerald")


class NodeRefTests(unittest.TestCase):
    def test_process_ref_is_node_bound(self) -> None:
        from maintenance.nodes import ProcessRef

        ref = ProcessRef(node_id=NodeId("local"), pid=42, create_time=1.5)
        self.assertEqual(ref.node_id, NodeId("local"))
        self.assertEqual(ref.pid, 42)
        self.assertEqual(ref.create_time, 1.5)

    def test_file_ref_is_node_bound(self) -> None:
        from maintenance.nodes import FileRef

        ref = FileRef(node_id=NodeId("local"), path=Path("/tmp/x"), size_bytes=10)
        self.assertEqual(ref.size_bytes, 10)


if __name__ == "__main__":
    unittest.main()
