"""Tests for the small Qt shell around the Expra Connect package."""

from __future__ import annotations

import time
import unittest
from collections.abc import Callable
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from expra_connect import NodeId, RelationshipState
from expra_connect.surfaces.registry import (
    SurfaceAccess,
    SurfaceDefinition,
    SurfaceGrant,
)
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from maintenance.ui.qt_connect_harness import QtConnectHarnessWindow
from tests.support.qt import get_qapplication


class QtConnectHarnessTests(unittest.TestCase):
    app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = get_qapplication()

    @staticmethod
    def make_adapter(peers: tuple[object, ...] = ()) -> Mock:
        adapter = Mock()
        adapter.started = False

        def start_adapter() -> bool:
            adapter.started = True
            return True

        adapter.start.side_effect = start_adapter
        adapter.runtime = SimpleNamespace(
            identity=SimpleNamespace(node_id=SimpleNamespace(value="local-node"))
        )
        adapter.status = SimpleNamespace(discovery_started=True)
        adapter.peers = peers
        adapter.node_records = ()
        adapter.connections = ()
        adapter.peer_relationship.return_value = RelationshipState.UNPAIRED
        adapter.cluster_status.return_value = {
            "cluster_id": "",
            "coordinator_id": None,
            "epoch": None,
            "members": [],
        }
        return adapter

    def wait_for_operation(self, window: QtConnectHarnessWindow) -> None:
        deadline = time.monotonic() + 2
        while window._operation_active and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(window._operation_active, "Connect operation timed out")

    def test_starts_runtime_and_shows_identity_and_discovery_state(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertTrue(adapter.started)
            self.assertIn("local-node", window.identity_label.text())
            self.assertIn("Discovery active", window.discovery_label.text())
            self.assertEqual("Searching for peers…", window.peer_status_label.text())
        finally:
            window.close()

        adapter.shutdown.assert_called_once_with()

    def test_harness_uses_flat_control_styles(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            stylesheet = window.styleSheet()
            self.assertIn("QPushButton { border: 1px solid", stylesheet)
            self.assertIn("QGroupBox { border: 1px solid", stylesheet)
            self.assertNotIn("qlineargradient", stylesheet.lower())
        finally:
            window.close()

    def test_harness_applies_shared_typographic_roles(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertEqual("title", window.title_label.property("role"))
            self.assertEqual("status", window.status_label.property("role"))
            self.assertIn("QLabel[role='status']", window.styleSheet())
            self.assertIn("QGroupBox::title", window.styleSheet())
        finally:
            window.close()

    def test_capability_sections_use_keyboard_accessible_tabs(self) -> None:
        window = QtConnectHarnessWindow(lambda _pairing_callback: self.make_adapter())
        try:
            window.show()
            self.app.processEvents()
            self.assertEqual(window.section_tabs.count(), 3)
            self.assertEqual(
                [window.section_tabs.tabText(index) for index in range(3)],
                ["Runtime and peers", "Remote capabilities", "Cluster"],
            )
            self.assertEqual(
                "Connect capability sections", window.section_tabs.accessibleName()
            )
            self.assertFalse(window.remote_dashboard_button.isEnabled())
            self.assertFalse(window.remote_processes_button.isEnabled())
            self.assertFalse(window.remote_request_shared_button.isEnabled())
            self.assertFalse(window.remote_grant_surface_button.isEnabled())
            window.section_tabs.tabBar().setFocus()
            QTest.keyClick(window.section_tabs.tabBar(), Qt.Key.Key_Right)
            self.assertEqual(window.section_tabs.currentIndex(), 1)
        finally:
            window.close()

    def test_remote_process_action_requires_explicit_confirmation(self) -> None:
        peer = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((peer,))
        adapter.peer_relationship.return_value = RelationshipState.OUTBOUND_TRUSTED
        adapter.connections = (NodeId("peer-1"),)
        adapter.remote_process_candidates.return_value = [
            SimpleNamespace(
                pid=412,
                name="worker\nignore confirmation",
                create_time=123.25,
                action_allowed=True,
            )
        ]
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.remote_processes_button.isEnabled())
            window.remote_processes_button.click()
            self.wait_for_operation(window)
            self.assertEqual(window.remote_process_list.count(), 1)
            window.remote_process_list.setCurrentRow(0)
            selected_process = window.remote_process_list.currentItem().data(
                Qt.ItemDataRole.UserRole
            )
            self.assertNotIn("\n", selected_process["name"])

            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ):
                window._terminate_remote_process()

            adapter.remote_request_quit.assert_not_called()
        finally:
            window.close()

    def test_surface_action_approval_lists_actions_and_defaults_to_no(self) -> None:
        peer = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((peer,))
        adapter.peer_relationship.return_value = RelationshipState.INBOUND_GRANTED

        def handler(_peer: NodeId, _params: dict[str, object]) -> object:
            return None

        adapter.surface_definitions = {
            "exp.test-actions": SurfaceDefinition(
                "exp.test-actions",
                handler,
                handler,
                {"stop": handler, "review": handler},
            )
        }
        policy = Mock()
        policy.desired_approvals = frozenset()
        policy.applied_approvals = frozenset()
        window = QtConnectHarnessWindow(
            lambda _pairing_callback: adapter,
            on_runtime_started=lambda _adapter: policy,
        )
        try:
            window.peer_list.setCurrentRow(0)
            window.surface_selector.setCurrentIndex(
                window.surface_selector.findData("exp.test-actions")
            )
            window.surface_access_selector.setCurrentIndex(
                window.surface_access_selector.findData(SurfaceAccess.ACTION)
            )
            self.assertTrue(window.remote_grant_surface_button.isEnabled())
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as confirmation:
                window._grant_surface()

            self.assertIn("stop", confirmation.call_args.args[2])
            self.assertIn("review", confirmation.call_args.args[2])
            self.assertIs(
                confirmation.call_args.args[-1], QMessageBox.StandardButton.No
            )
            policy.grant.assert_not_called()
        finally:
            window.close()

    def test_confirmed_process_action_uses_selected_pid_and_creation_time(self) -> None:
        peer = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((peer,))
        adapter.peer_relationship.return_value = RelationshipState.OUTBOUND_TRUSTED
        adapter.connections = (NodeId("peer-1"),)
        adapter.remote_process_candidates.return_value = [
            SimpleNamespace(
                pid=412,
                name="worker",
                create_time=123.25,
                action_allowed=True,
            )
        ]
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            window.remote_processes_button.click()
            self.wait_for_operation(window)
            window.remote_process_list.setCurrentRow(0)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window._terminate_remote_process()
                self.wait_for_operation(window)

            adapter.remote_request_quit.assert_called_once_with(
                NodeId("peer-1"), [412], expected_create_times={412: 123.25}
            )
        finally:
            window.close()

    def test_unavailable_saved_policy_does_not_block_runtime_or_remote_reads(
        self,
    ) -> None:
        adapter = self.make_adapter()
        policy = Mock()
        policy.reapply.side_effect = RuntimeError("malformed policy")

        window = QtConnectHarnessWindow(
            lambda _pairing_callback: adapter,
            on_runtime_started=lambda _adapter: policy,
        )
        try:
            self.assertTrue(adapter.started)
            self.assertIn("unavailable", window.surface_approval_state_label.text())
            self.assertTrue(window.remote_status_label.text())
        finally:
            window.close()

    def test_policy_clear_failure_does_not_prevent_runtime_shutdown(self) -> None:
        adapter = self.make_adapter()
        policy = Mock()
        policy.clear_runtime.side_effect = RuntimeError("runtime revoke failed")
        window = QtConnectHarnessWindow(
            lambda _pairing_callback: adapter,
            on_runtime_started=lambda _adapter: policy,
        )

        window.close()

        adapter.shutdown.assert_called_once_with()

    def test_remote_availability_distinguishes_direction_and_connection(self) -> None:
        peer = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((peer,))
        adapter.peer_relationship.return_value = RelationshipState.INBOUND_GRANTED
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertIn("outbound trust", window.remote_status_label.text())
            self.assertFalse(window.remote_dashboard_button.isEnabled())

            adapter.peer_relationship.return_value = RelationshipState.OUTBOUND_TRUSTED
            adapter.connections = ()
            window.refresh_status()
            window.peer_list.setCurrentRow(0)

            self.assertIn("not connected", window.remote_status_label.text())
            self.assertFalse(window.remote_dashboard_button.isEnabled())
        finally:
            window.close()

    def test_selected_peer_details_wrap_on_narrow_window(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        window = QtConnectHarnessWindow(
            lambda _pairing_callback: self.make_adapter((candidate,))
        )
        try:
            window.resize(620, 760)
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.peer_detail_label.wordWrap())
        finally:
            window.close()

    def test_malformed_membership_projects_as_unknown(self) -> None:
        record = SimpleNamespace(membership=SimpleNamespace(value=[]))
        self.assertEqual(QtConnectHarnessWindow._membership_value(record), "unknown")

    def test_cluster_projection_rejects_malformed_payload_without_stringifying_it(
        self,
    ) -> None:
        class UnsafeValue:
            def __str__(self) -> str:
                raise AssertionError("untrusted status values must not be stringified")

        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": UnsafeValue(),
            "coordinator_id": None,
            "epoch": 1,
            "members": [],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.refresh_status()

            self.assertIn("unavailable", window.cluster_status_label.text().lower())
        finally:
            window.close()

    def test_empty_cluster_projection_is_presented_as_not_joined(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertEqual(
                "Not joined to a cluster", window.cluster_status_label.text()
            )
            self.assertEqual(0, window.cluster_member_list.count())
        finally:
            window.close()

    def test_disabled_cluster_is_distinct_from_unjoined_state(self) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {"enabled": False, "members": []}
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertIn("disabled", window.cluster_status_label.text().lower())
        finally:
            window.close()

    def test_cluster_projection_allows_only_typed_public_fields(self) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": "node-1",
            "epoch": 7,
            "members": [
                {
                    "node_id": "node-1",
                    "roles": ["coordinator"],
                    "paused": True,
                    "revoked": True,
                    "fencing_token": "never-render-this",
                }
            ],
            "fencing_token": "never-render-this-either",
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.refresh_status()

            rendered = " ".join(widget.text() for widget in window.findChildren(QLabel))
            self.assertIn("cluster-1", rendered)
            self.assertIn("Coordinator: node-1", rendered)
            self.assertIn("Epoch: 7", rendered)
            self.assertIn("coordinator", rendered)
            self.assertIn("paused", rendered)
            self.assertIn("revoked", rendered)
            self.assertNotIn("never-render-this", rendered)
        finally:
            window.close()

    def test_cluster_projection_displays_missing_coordinator_and_epoch_safely(
        self,
    ) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "members": [],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.refresh_status()

            self.assertIn(
                "Coordinator: Unavailable", window.cluster_status_label.text()
            )
            self.assertIn("Epoch: Unavailable", window.cluster_status_label.text())
        finally:
            window.close()

    def test_cluster_projection_rejects_invalid_assignment_fields(self) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": None,
            "epoch": 0,
            "members": [
                {
                    "node_id": "node-1",
                    "roles": ["unknown-role"],
                    "paused": False,
                    "revoked": False,
                }
            ],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.refresh_status()

            self.assertIn("unavailable", window.cluster_status_label.text().lower())
        finally:
            window.close()

    def test_operation_failure_includes_safe_recovery_guidance(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window._finish_operation(error=True)
            self.assertIn("refresh status", window.status_label.text().lower())
        finally:
            window.close()

    def test_cluster_controls_stay_at_top_of_tab_content(self) -> None:
        window = QtConnectHarnessWindow(lambda _pairing_callback: self.make_adapter())
        try:
            window.section_tabs.setCurrentIndex(2)
            window.resize(620, 760)
            window.show()
            self.app.processEvents()
            cluster_group = window.cluster_status_label.parentWidget()
            self.assertIsNotNone(cluster_group)
            assert cluster_group is not None
            self.assertLessEqual(
                cluster_group.height(), cluster_group.sizeHint().height() + 16
            )
        finally:
            window.close()

    def test_first_identity_creation_uses_explicit_confirmation(self) -> None:
        adapter = self.make_adapter()

        def factory(pairing_callback: Callable[[object], bool]) -> Mock:
            def start_adapter() -> bool:
                if not pairing_callback("first identity"):
                    return False
                adapter.started = True
                return True

            adapter.start.side_effect = start_adapter
            return adapter

        with patch(
            "maintenance.ui.qt_connect_harness.confirm_connect_identity_transition",
            return_value=False,
        ) as confirm:
            window = QtConnectHarnessWindow(factory)
        try:
            confirm.assert_called_once_with(window)
            self.assertFalse(adapter.started)
            self.assertIn("declined", window.status_label.text())
        finally:
            window.close()

    def test_accepted_first_identity_confirmation_allows_connect_start(self) -> None:
        adapter = self.make_adapter()

        def factory(pairing_callback: Callable[[object], bool]) -> Mock:
            def start_adapter() -> bool:
                if not pairing_callback("first identity"):
                    return False
                adapter.started = True
                return True

            adapter.start.side_effect = start_adapter
            return adapter

        with patch(
            "maintenance.ui.qt_connect_harness.confirm_connect_identity_transition",
            return_value=True,
        ) as confirm:
            window = QtConnectHarnessWindow(factory)
        try:
            confirm.assert_called_once_with(window)
            self.assertTrue(adapter.started)
            self.assertIn("running", window.status_label.text())
        finally:
            window.close()

    def test_pair_requires_explicit_fingerprint_review(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as prompt:
                window._pair_selected_peer()
            self.assertIn("identity-fp", prompt.call_args.args[2])
            self.assertIn("transport-fp", prompt.call_args.args[2])
            adapter.pair_peer.assert_not_called()
        finally:
            window.close()

    def test_peer_controls_follow_trust_and_connection_separately(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        adapter.peer_relationship.return_value = RelationshipState.UNPAIRED
        adapter.connections = ()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.pair_button.isEnabled())
            self.assertFalse(window.repair_button.isEnabled())
            self.assertFalse(window.connect_button.isEnabled())
            self.assertFalse(window.disconnect_button.isEnabled())
            self.assertFalse(window.revoke_peer_button.isEnabled())

            adapter.peer_relationship.return_value = RelationshipState.INBOUND_GRANTED
            window.refresh_status()
            self.assertFalse(window.connect_button.isEnabled())
            self.assertFalse(window.pair_button.isEnabled())
            self.assertTrue(window.revoke_peer_button.isEnabled())

            adapter.peer_relationship.return_value = RelationshipState.OUTBOUND_TRUSTED
            window.refresh_status()
            self.assertTrue(window.connect_button.isEnabled())
            self.assertTrue(window.revoke_peer_button.isEnabled())

            adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
            adapter.connections = (NodeId("peer-1"),)
            window.refresh_status()
            window.peer_list.setCurrentRow(0)
            self.assertFalse(window.pair_button.isEnabled())
            self.assertFalse(window.repair_button.isEnabled())
            self.assertFalse(window.connect_button.isEnabled())
            self.assertTrue(window.disconnect_button.isEnabled())
            self.assertTrue(window.revoke_peer_button.isEnabled())
        finally:
            window.close()

    def test_repair_and_trust_revocation_decline_without_connect_calls(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        adapter.peer_relationship.return_value = RelationshipState.REPAIR_REQUIRED
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.repair_button.isEnabled())
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as prompt:
                window._repair_selected_peer()
            self.assertIn("identity-fp", prompt.call_args.args[2])
            self.assertIn("transport-fp", prompt.call_args.args[2])
            adapter.repair_peer.assert_not_called()

            adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
            window.refresh_status()
            window.peer_list.setCurrentRow(0)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as prompt:
                window._revoke_selected_peer()
            self.assertIn("peer-1", prompt.call_args.args[2])
            adapter.revoke_peer.assert_not_called()
        finally:
            window.close()

    def test_diagnostics_projection_never_renders_unknown_secret_fields(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
        adapter.connections = (NodeId("peer-1"),)
        adapter.node_records = (
            SimpleNamespace(
                node_id=NodeId("peer-1"),
                trust=SimpleNamespace(value="trusted"),
                membership=SimpleNamespace(value="worker"),
            ),
        )
        adapter.diagnostics.return_value = {
            "routes": [],
            "sessions": [],
            "private_key": "diagnostic-private-marker",
            "fencing_token": "diagnostic-fence-marker",
        }
        adapter.pairing_diagnostics.return_value = {
            "identity": {
                "transport_continuity": "same_generation",
                "private_key": "peer-private-marker",
            },
            "pending": {"intent": None, "direction": None, "count": 0},
            "fencing_token": "peer-fence-marker",
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            rendered = " ".join(
                (
                    window.diagnostics_label.text(),
                    window.peer_detail_label.text(),
                    window.peer_list.item(0).text(),
                )
            )
            for marker in (
                "diagnostic-private-marker",
                "diagnostic-fence-marker",
                "peer-private-marker",
                "peer-fence-marker",
            ):
                self.assertNotIn(marker, rendered)
            self.assertIn("same_generation", rendered)
            self.assertIn("Trust relationship: bidirectional", rendered)
            self.assertIn("Connection: online", rendered)
            self.assertIn("Cluster membership: worker", rendered)
        finally:
            window.close()

    def test_connection_actions_delegate_to_connect_runtime(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.connect_button.isEnabled())
            window._connect_selected_peer()
            self.wait_for_operation(window)
            adapter.connect_peer.assert_called_once_with(NodeId("peer-1"))

            adapter.connections = (NodeId("peer-1"),)
            window.refresh_status()
            window.peer_list.setCurrentRow(0)
            self.assertTrue(window.reconnect_button.isEnabled())
            self.assertTrue(window.disconnect_button.isEnabled())
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window._reconnect_selected_peer()
            self.wait_for_operation(window)
            adapter.reconnect_peer.assert_called_once_with(NodeId("peer-1"))

            window._disconnect_selected_peer()
            self.wait_for_operation(window)
            adapter.disconnect_peer.assert_called_once_with(NodeId("peer-1"))
        finally:
            window.close()

    def test_transport_rotation_requires_confirmation(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertEqual("danger", window.rotate_transport_button.property("role"))
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as prompt:
                window._rotate_transport()
            self.assertIn("TLS transport identity", prompt.call_args.args[2])
            adapter.rotate_transport.assert_not_called()
        finally:
            window.close()

    def test_trusted_peer_is_not_offered_pairing_again(self) -> None:
        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        record = SimpleNamespace(
            node_id=SimpleNamespace(value="peer-1"),
            trust=SimpleNamespace(value="trusted"),
            connection=SimpleNamespace(status=SimpleNamespace(value="online")),
        )
        adapter = self.make_adapter((candidate,))
        adapter.node_records = (record,)
        adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
        adapter.connections = (NodeId("peer-1"),)
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            self.assertFalse(window.pair_button.isEnabled())
            self.assertIn("trusted, online", window.peer_list.item(0).text())
            self.assertEqual(window.peer_list.count(), 1)
        finally:
            window.close()

    def test_approved_pair_shows_both_fingerprints_before_calling_connect(self) -> None:
        from expra_connect import NodeId as ConnectNodeId

        candidate = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((candidate,))
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ) as prompt:
                window._pair_selected_peer()

            self.assertIn("identity-fp", prompt.call_args.args[2])
            self.assertIn("transport-fp", prompt.call_args.args[2])
            self.wait_for_operation(window)
            adapter.pair_peer.assert_called_once_with(ConnectNodeId("peer-1"))
        finally:
            window.close()

    def test_stop_connect_shuts_down_runtime_once(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        window.stop_connect()
        window.close()

        adapter.shutdown.assert_called_once_with()
        self.assertIn("stopped", window.status_label.text().lower())

    def test_close_does_not_race_runtime_shutdown_with_active_operation(self) -> None:
        adapter = self.make_adapter()
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        operation_started = Event()
        release_operation = Event()

        def slow_operation() -> None:
            operation_started.set()
            release_operation.wait(1)

        try:
            window._run_operation(
                "Working…", slow_operation, "Done", lambda _result: None
            )
            self.assertTrue(operation_started.wait(1))
            self.assertFalse(window.close())
            adapter.shutdown.assert_not_called()
            release_operation.set()
            self.wait_for_operation(window)
        finally:
            release_operation.set()
            window.close()

        adapter.shutdown.assert_called_once_with()

    def test_join_uses_targeted_invite_and_connect_provider(self) -> None:
        from expra_connect import NodeId as ConnectNodeId
        from expra_connect.cluster import (
            InviteRecord,
            decode_invite_blob,
            encode_invite_blob,
        )

        adapter = self.make_adapter()
        invite = InviteRecord(
            "invite-hash",
            "local-node",
            1_900_000_000.0,
            "opaque-token",
            "cluster-id",
            "coordinator",
            1,
            "fence-value",
        )
        adapter.connect_peer.return_value = object()
        provider = adapter.connect_peer.return_value
        adapter.join_cluster.return_value = {"joined": True}
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            with patch(
                "maintenance.ui.qt_connect_harness.QInputDialog.getText",
                return_value=(encode_invite_blob(invite), True),
            ):
                window._join_with_invite()
                self.wait_for_operation(window)

            self.wait_for_operation(window)
            adapter.connect_peer.assert_called_once_with(ConnectNodeId("coordinator"))
            adapter.join_cluster.assert_called_once_with(
                provider, decode_invite_blob(encode_invite_blob(invite))
            )
            self.assertIn("Joined", window.status_label.text())
        finally:
            window.close()

    def test_expired_invite_is_refused_before_connecting_to_coordinator(self) -> None:
        from expra_connect.cluster import InviteRecord, encode_invite_blob

        adapter = self.make_adapter()
        invite = InviteRecord(
            "invite-hash",
            "local-node",
            time.time() - 1,
            "opaque-token",
            "cluster-id",
            "coordinator",
            1,
            "fence-value",
        )
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            with patch(
                "maintenance.ui.qt_connect_harness.QInputDialog.getText",
                return_value=(encode_invite_blob(invite), True),
            ):
                window._join_with_invite()
                self.wait_for_operation(window)

            adapter.connect_peer.assert_not_called()
            adapter.join_cluster.assert_not_called()
            self.assertIn("expired", window.status_label.text().lower())
        finally:
            window.close()

    def test_join_refuses_to_replace_any_existing_membership(self) -> None:
        from expra_connect.cluster import InviteRecord, encode_invite_blob

        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "existing-cluster",
            "coordinator_id": "local-node",
            "epoch": 2,
            "members": [
                {
                    "node_id": "local-node",
                    "roles": ["coordinator"],
                    "paused": False,
                    "revoked": False,
                }
            ],
        }
        invite = InviteRecord(
            "invite-hash",
            "local-node",
            time.time() + 60,
            "opaque-token",
            "other-cluster",
            "coordinator",
            1,
            "fence-value",
        )
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            with patch(
                "maintenance.ui.qt_connect_harness.QInputDialog.getText",
                return_value=(encode_invite_blob(invite), True),
            ):
                window._join_with_invite()
                self.wait_for_operation(window)

            adapter.connect_peer.assert_not_called()
            adapter.join_cluster.assert_not_called()
            self.assertIn("replace", window.status_label.text().lower())
        finally:
            window.close()

    def test_join_refuses_when_cluster_support_is_disabled(self) -> None:
        from expra_connect.cluster import InviteRecord, encode_invite_blob

        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {"enabled": False, "members": []}
        invite = InviteRecord(
            "invite-hash",
            "local-node",
            time.time() + 60,
            "opaque-token",
            "other-cluster",
            "coordinator",
            1,
            "fence-value",
        )
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            with patch(
                "maintenance.ui.qt_connect_harness.QInputDialog.getText",
                return_value=(encode_invite_blob(invite), True),
            ):
                window._join_with_invite()

            adapter.connect_peer.assert_not_called()
            adapter.join_cluster.assert_not_called()
            self.assertIn("disabled", window.status_label.text().lower())
        finally:
            window.close()

    def test_create_invite_shows_expiry_only_with_the_one_time_invite_dialog(
        self,
    ) -> None:
        from expra_connect.cluster import InviteRecord

        peer = SimpleNamespace(
            stable_id="peer-1",
            hostname="peer.example",
            identity_fingerprint="identity-fp",
            transport_fingerprint="transport-fp",
        )
        adapter = self.make_adapter((peer,))
        invite = InviteRecord(
            "invite-hash",
            "peer-1",
            1_900_000_000.0,
            "one-time-secret",
            "cluster-id",
            "coordinator",
            1,
            "fence-value",
        )
        adapter.create_invite.return_value = invite
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.peer_list.setCurrentRow(0)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.information"
            ) as information:
                window._create_invite_for_selected_peer()
                self.wait_for_operation(window)

            adapter.create_invite.assert_called_once_with(NodeId("peer-1"))
            message = information.call_args.args[2]
            self.assertIn("expires at", message.lower())
            self.assertIn("UTC", message)
            self.assertNotIn("one-time-secret", message)
            self.assertNotIn("fence-value", message)
        finally:
            window.close()

    def test_cluster_role_and_member_actions_confirm_target_and_default_to_no(
        self,
    ) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": "local-node",
            "epoch": 2,
            "members": [
                {
                    "node_id": "local-node",
                    "roles": ["coordinator"],
                    "paused": False,
                    "revoked": False,
                },
                {
                    "node_id": "worker-1",
                    "roles": ["worker"],
                    "paused": False,
                    "revoked": False,
                },
            ],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.cluster_member_list.setCurrentRow(1)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as confirmation:
                window._assign_selected_cluster_role()

            self.assertIn("worker-1", confirmation.call_args.args[2])
            self.assertIs(
                confirmation.call_args.args[-1], QMessageBox.StandardButton.No
            )
            adapter.assign_role.assert_not_called()

            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as confirmation:
                window._revoke_selected_cluster_member()

            self.assertIn("worker-1", confirmation.call_args.args[2])
            self.assertIs(
                confirmation.call_args.args[-1], QMessageBox.StandardButton.No
            )
            adapter.revoke_member.assert_not_called()
        finally:
            window.close()

    def test_cluster_worker_controls_follow_paused_and_revoked_membership(self) -> None:
        adapter = self.make_adapter()
        adapter.connections = (NodeId("worker-1"),)
        adapter.peer_relationship.return_value = RelationshipState.BIDIRECTIONAL
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": "local-node",
            "epoch": 2,
            "members": [
                {
                    "node_id": "local-node",
                    "roles": ["coordinator"],
                    "paused": False,
                    "revoked": False,
                },
                {
                    "node_id": "worker-1",
                    "roles": ["worker"],
                    "paused": True,
                    "revoked": False,
                },
            ],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.cluster_member_list.setCurrentRow(1)
            self.assertFalse(window.pause_worker_button.isEnabled())
            self.assertTrue(window.resume_worker_button.isEnabled())
            self.assertTrue(window.revoke_worker_button.isEnabled())

            adapter.cluster_status.return_value["members"][1]["revoked"] = True
            window.refresh_status()
            window.cluster_member_list.setCurrentRow(1)
            self.assertFalse(window.pause_worker_button.isEnabled())
            self.assertFalse(window.resume_worker_button.isEnabled())
            self.assertFalse(window.revoke_worker_button.isEnabled())
            self.assertFalse(window.remove_job_button.isEnabled())
        finally:
            window.close()

    def test_lease_renewal_ui_uses_only_the_token_free_adapter_wrapper(self) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": "local-node",
            "epoch": 2,
            "members": [
                {
                    "node_id": "local-node",
                    "roles": ["coordinator"],
                    "paused": False,
                    "revoked": False,
                }
            ],
        }
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            self.assertFalse(window.promote_if_due_button.isEnabled())
            self.assertFalse(window.rejoin_as_worker_button.isEnabled())
            window.renew_cluster_lease_button.click()
            self.wait_for_operation(window)

            adapter.renew_coordinator_lease.assert_called_once_with()
            adapter.heartbeat.assert_not_called()
        finally:
            window.close()

    def test_cluster_surface_grant_is_short_lived_and_shows_countdown(self) -> None:
        adapter = self.make_adapter()
        adapter.cluster_status.return_value = {
            "enabled": True,
            "cluster_id": "cluster-1",
            "coordinator_id": "local-node",
            "epoch": 2,
            "members": [
                {
                    "node_id": "local-node",
                    "roles": ["coordinator"],
                    "paused": False,
                    "revoked": False,
                },
                {
                    "node_id": "worker-1",
                    "roles": ["worker"],
                    "paused": False,
                    "revoked": False,
                },
            ],
        }
        handler = lambda _peer, _params: None
        definition = SurfaceDefinition("exp.cluster-share", handler, None, {})
        adapter.surface_definitions = {definition.surface_id: definition}
        expiry = time.time() + 300
        adapter.grant_cluster_surface_access.return_value = SurfaceGrant(
            NodeId("worker-1"),
            definition.surface_id,
            SurfaceAccess.READ,
            expiry,
            True,
        )
        window = QtConnectHarnessWindow(lambda _pairing_callback: adapter)
        try:
            window.cluster_member_list.setCurrentRow(1)
            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ) as confirmation:
                window._grant_cluster_surface_access()

            self.assertIn("worker-1", confirmation.call_args.args[2])
            self.assertIn(definition.surface_id, confirmation.call_args.args[2])
            self.assertIs(
                confirmation.call_args.args[-1], QMessageBox.StandardButton.No
            )
            adapter.grant_cluster_surface_access.assert_not_called()

            with patch(
                "maintenance.ui.qt_connect_harness.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                window._grant_cluster_surface_access()
                self.wait_for_operation(window)

            adapter.grant_cluster_surface_access.assert_called_once()
            args, kwargs = adapter.grant_cluster_surface_access.call_args
            self.assertEqual(args, (NodeId("worker-1"), definition.surface_id))
            self.assertEqual(kwargs["access"], SurfaceAccess.READ)
            self.assertGreater(kwargs["expires_at"], time.time())
            self.assertLessEqual(kwargs["expires_at"] - time.time(), 300)
            self.assertIn(
                "expires in", window.cluster_share_expiry_label.text().lower()
            )
            self.assertIn("early revocation", window.cluster_share_policy_label.text())
        finally:
            window.close()

    def test_main_entrypoint_runs_the_connect_harness(self) -> None:
        import main

        lock = Mock()
        with (
            patch("main.instance_lock.acquire", return_value=lock),
            patch("main.setup_logging"),
            patch("main.run_application") as run_application,
            patch("sys.argv", ["expra-connect-harness"]),
        ):
            main.main()

        lock.release.assert_called_once_with()
        run_application.assert_called_once()
        self.assertEqual(run_application.call_args.args[0], ["expra-connect-harness"])


if __name__ == "__main__":
    unittest.main()
