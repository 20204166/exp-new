"""Small Qt presentation for the Expra Connect package-adoption harness."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from expra_connect import NodeId, RelationshipState
from expra_connect.cluster import (
    ClusterRole,
    InviteRecord,
    decode_invite_blob,
    encode_invite_blob,
)
from expra_connect.surfaces.registry import SurfaceAccess
from PySide6.QtCore import QRunnable, Qt, QThread, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from maintenance.connect_grants import SurfaceGrantApproval
from maintenance.ui import styles as ui_styles
from maintenance.ui.qt_connect_pairing import QtPairingApprovalBridge
from maintenance.ui.qt_connect_transition import confirm_connect_identity_transition
from maintenance.ui.window_supports.qt_timer_delivery import QtDeliveryQueue


@dataclass(frozen=True, slots=True)
class _ClusterMemberProjection:
    node_id: str
    roles: tuple[str, ...]
    paused: bool
    revoked: bool


@dataclass(frozen=True, slots=True)
class _ClusterProjection:
    enabled: bool
    cluster_id: str
    coordinator_id: str | None
    epoch: int | None
    members: tuple[_ClusterMemberProjection, ...]


class QtConnectHarnessWindow(QMainWindow):
    """Show Connect-owned runtime, peer, and cluster state with explicit actions."""

    def __init__(
        self,
        adapter_factory: Callable[[Callable[[object], bool]], Any],
        *,
        on_runtime_started: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__()
        self._closing = False
        self._runtime_stopped = False
        self._operation_active = False
        self._delivery = QtDeliveryQueue(parent=self)
        self._pairing_approval = QtPairingApprovalBridge(
            submit=self._delivery.submit,
            approve=self._approve_connect_request,
            is_ui_thread=lambda: QThread.currentThread() is self.thread(),
        )
        self.adapter = adapter_factory(self._pairing_approval)
        self._on_runtime_started = on_runtime_started
        self._surface_policy: Any | None = None
        self._surface_policy_unavailable = False
        self._cluster_projection: _ClusterProjection | None = None
        self._cluster_share_expires_at: float | None = None
        self._cluster_share_timer = QTimer(self)
        self._cluster_share_timer.setInterval(1_000)
        self._cluster_share_timer.timeout.connect(self._update_cluster_share_countdown)
        self._build_widgets()
        self._start_connect()

    def _approve_connect_request(self, request: object) -> bool:
        if isinstance(request, str):
            return confirm_connect_identity_transition(self)
        caller_id = getattr(getattr(request, "caller_node_id", None), "value", "")
        identity_fingerprint = getattr(request, "identity_fingerprint", "")
        transport_fingerprint = getattr(request, "transport_fingerprint", "")
        if not caller_id or not identity_fingerprint or not transport_fingerprint:
            return False
        response = QMessageBox.question(
            self,
            "Approve Expra Connect pairing?",
            "A peer requests a read-only connection. Verify both fingerprints "
            "with the other machine before approving.\n\n"
            f"Node: {caller_id}\n"
            f"Identity fingerprint: {identity_fingerprint}\n"
            f"Transport fingerprint: {transport_fingerprint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return response is QMessageBox.StandardButton.Yes

    def _build_widgets(self) -> None:
        self.setWindowTitle("Expra Connect")
        self.setMinimumSize(620, 560)
        self.resize(840, 680)
        ui_styles.configure_qt_styles(self)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(
            ui_styles.SPACING["page_x"],
            ui_styles.SPACING["page_y"],
            ui_styles.SPACING["page_x"],
            ui_styles.SPACING["page_y"],
        )
        layout.setSpacing(ui_styles.SPACING["section_gap"])

        self.title_label = QLabel("Expra Connect", central)
        self.title_label.setProperty("role", "title")
        self.status_label = QLabel("Starting Expra Connect…", central)
        self.status_label.setProperty("role", "status")
        self.status_label.setAccessibleName("Connect runtime status")
        self.identity_label = QLabel("Identity: waiting", central)
        self.identity_label.setProperty("role", "description")
        self.identity_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.discovery_label = QLabel("Discovery: starting", central)
        self.discovery_label.setProperty("role", "description")
        self.discovery_label.setAccessibleName("Discovery status")
        self.diagnostics_label = QLabel("Runtime diagnostics: waiting", central)
        self.diagnostics_label.setProperty("role", "description")
        self.diagnostics_label.setAccessibleName("Runtime diagnostics summary")
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.identity_label)
        layout.addWidget(self.discovery_label)
        layout.addWidget(self.diagnostics_label)

        self.section_tabs = QTabWidget(central)
        self.section_tabs.setAccessibleName("Connect capability sections")

        peer_page = QWidget(self.section_tabs)
        peer_page_layout = QVBoxLayout(peer_page)
        peers = QGroupBox("Peers", peer_page)
        peer_layout = QVBoxLayout(peers)
        self.peer_list = QListWidget(peers)
        self.peer_list.setAccessibleName("Discovered and trusted peers")
        self.peer_status_label = QLabel("Waiting for peer discovery…", peers)
        self.peer_status_label.setAccessibleName("Peer list status")
        self.peer_detail_label = QLabel("Select a peer to view its state.", peers)
        self.peer_detail_label.setProperty("role", "description")
        self.peer_detail_label.setAccessibleName("Selected peer details")
        self.peer_detail_label.setWordWrap(True)
        peer_layout.addWidget(self.peer_status_label)
        peer_layout.addWidget(self.peer_list)
        peer_layout.addWidget(self.peer_detail_label)
        peer_actions = QGridLayout()
        self.refresh_button = QPushButton("Refresh status", peers)
        self.refresh_button.setAccessibleName("Refresh Connect status projection")
        self.pair_button = QPushButton("Pair selected peer", peers)
        self.pair_button.setProperty("role", "primary")
        self.pair_button.setAccessibleName("Pair with selected peer")
        self.repair_button = QPushButton("Repair trust", peers)
        self.repair_button.setAccessibleName("Repair trust with selected peer")
        self.connect_button = QPushButton("Connect", peers)
        self.connect_button.setAccessibleName("Connect to selected peer")
        self.reconnect_button = QPushButton("Reconnect", peers)
        self.reconnect_button.setAccessibleName("Reconnect selected peer")
        self.disconnect_button = QPushButton("Disconnect", peers)
        self.disconnect_button.setAccessibleName("Disconnect selected peer")
        self.revoke_peer_button = QPushButton("Revoke trust", peers)
        self.revoke_peer_button.setProperty("role", "danger")
        self.revoke_peer_button.setAccessibleName("Revoke trust with selected peer")
        self.rotate_transport_button = QPushButton("Rotate transport", peers)
        self.rotate_transport_button.setProperty("role", "danger")
        self.rotate_transport_button.setAccessibleName(
            "Rotate Connect transport identity"
        )
        for index, button in enumerate(
            (
                self.refresh_button,
                self.pair_button,
                self.repair_button,
                self.connect_button,
                self.reconnect_button,
                self.disconnect_button,
                self.revoke_peer_button,
                self.rotate_transport_button,
            )
        ):
            peer_actions.addWidget(button, index // 3, index % 3)
        peer_layout.addLayout(peer_actions)
        peer_page_layout.addWidget(peers, 1)

        cluster_page = QWidget(self.section_tabs)
        cluster_page_layout = QVBoxLayout(cluster_page)
        cluster_scroll = QScrollArea(cluster_page)
        cluster_scroll.setWidgetResizable(True)
        cluster_scroll.setAccessibleName("Cluster topology and operations")
        cluster_content = QWidget(cluster_scroll)
        cluster_content_layout = QVBoxLayout(cluster_content)

        observe = QGroupBox("Observe", cluster_content)
        observe_layout = QVBoxLayout(observe)
        self.cluster_status_label = QLabel("Cluster status: unavailable", observe)
        self.cluster_status_label.setProperty("role", "description")
        self.cluster_status_label.setAccessibleName("Connect cluster status")
        self.cluster_status_label.setWordWrap(True)
        self.cluster_member_list = QListWidget(observe)
        self.cluster_member_list.setAccessibleName("Connect cluster members")
        self.cluster_member_list.setMaximumHeight(150)
        observe_layout.addWidget(self.cluster_status_label)
        observe_layout.addWidget(self.cluster_member_list)
        cluster_content_layout.addWidget(observe)

        invite_group = QGroupBox("Invite / Join", cluster_content)
        invite_layout = QVBoxLayout(invite_group)
        self.invite_target_label = QLabel(
            "Invite target: select a peer in Runtime and peers.", invite_group
        )
        self.invite_target_label.setProperty("role", "description")
        self.invite_target_label.setAccessibleName("Cluster invite target")
        self.invite_target_label.setWordWrap(True)
        self.invite_button = QPushButton("Create targeted invite", invite_group)
        self.invite_button.setAccessibleName("Create invite for selected peer")
        self.join_button = QPushButton("Join with invite…", invite_group)
        self.join_button.setAccessibleName("Join cluster with invite")
        invite_layout.addWidget(self.invite_target_label)
        invite_layout.addWidget(self.invite_button)
        invite_layout.addWidget(self.join_button)
        cluster_content_layout.addWidget(invite_group)

        manage = QGroupBox("Manage / Recover", cluster_content)
        manage_layout = QGridLayout(manage)
        self.cluster_role_selector = QComboBox(manage)
        self.cluster_role_selector.setAccessibleName("Role to assign")
        for role in ("worker", "subcoordinator"):
            self.cluster_role_selector.addItem(role.title(), role)
        self.assign_role_button = QPushButton("Assign role to selected member", manage)
        self.revoke_member_button = QPushButton("Revoke selected member", manage)
        self.pause_worker_button = QPushButton("Pause selected worker", manage)
        self.resume_worker_button = QPushButton("Resume selected worker", manage)
        self.revoke_worker_button = QPushButton("Revoke selected worker", manage)
        self.remove_connection_button = QPushButton("Remove worker connection", manage)
        self.remove_job_button = QPushButton("Remove worker job", manage)
        self.renew_cluster_lease_button = QPushButton("Renew coordinator lease", manage)
        self.promote_if_due_button = QPushButton("Evaluate failover promotion", manage)
        self.rejoin_as_worker_button = QPushButton("Rejoin this node as worker", manage)
        for button in (
            self.revoke_member_button,
            self.revoke_worker_button,
            self.remove_connection_button,
            self.remove_job_button,
        ):
            button.setProperty("role", "danger")
        self.assign_role_button.setAccessibleName("Assign selected cluster role")
        self.revoke_member_button.setAccessibleName("Revoke selected cluster member")
        self.pause_worker_button.setAccessibleName("Pause selected cluster worker")
        self.resume_worker_button.setAccessibleName("Resume selected cluster worker")
        self.revoke_worker_button.setAccessibleName("Revoke selected cluster worker")
        self.remove_connection_button.setAccessibleName(
            "Remove selected worker connection"
        )
        self.remove_job_button.setAccessibleName("Remove selected worker job")
        manage_layout.addWidget(self.cluster_role_selector, 0, 0)
        manage_layout.addWidget(self.assign_role_button, 0, 1)
        manage_layout.addWidget(self.renew_cluster_lease_button, 1, 0, 1, 2)
        manage_layout.addWidget(self.promote_if_due_button, 2, 0, 1, 2)
        manage_layout.addWidget(self.rejoin_as_worker_button, 3, 0, 1, 2)
        for row, button in enumerate(
            (
                self.revoke_member_button,
                self.pause_worker_button,
                self.resume_worker_button,
                self.revoke_worker_button,
                self.remove_connection_button,
                self.remove_job_button,
            ),
            start=4,
        ):
            manage_layout.addWidget(button, row, 0, 1, 2)
        cluster_content_layout.addWidget(manage)

        share = QGroupBox("Share a host surface with a cluster member", cluster_content)
        share_layout = QGridLayout(share)
        self.cluster_surface_selector = QComboBox(share)
        self.cluster_surface_selector.setAccessibleName("Cluster-shared host surface")
        self.cluster_surface_access_selector = QComboBox(share)
        self.cluster_surface_access_selector.setAccessibleName(
            "Cluster surface access mode"
        )
        self.cluster_share_duration = QSpinBox(share)
        self.cluster_share_duration.setRange(1, 60)
        self.cluster_share_duration.setValue(5)
        self.cluster_share_duration.setSuffix(" min")
        self.cluster_share_duration.setAccessibleName("Cluster share duration")
        self.cluster_share_button = QPushButton("Grant short-lived access", share)
        self.cluster_share_button.setAccessibleName(
            "Grant selected cluster member temporary surface access"
        )
        self.cluster_share_expiry_label = QLabel("No active cluster share.", share)
        self.cluster_share_expiry_label.setProperty("role", "description")
        self.cluster_share_expiry_label.setAccessibleName("Cluster share expiry")
        self.cluster_share_expiry_label.setWordWrap(True)
        self.cluster_share_policy_label = QLabel(
            "Access expires automatically (up to 60 minutes); early revocation is "
            "not available for cluster-source grants in this Connect version.",
            share,
        )
        self.cluster_share_policy_label.setProperty("role", "description")
        self.cluster_share_policy_label.setWordWrap(True)
        share_layout.addWidget(self.cluster_surface_selector, 0, 0)
        share_layout.addWidget(self.cluster_surface_access_selector, 0, 1)
        share_layout.addWidget(self.cluster_share_duration, 1, 0)
        share_layout.addWidget(self.cluster_share_button, 1, 1)
        share_layout.addWidget(self.cluster_share_expiry_label, 2, 0, 1, 2)
        share_layout.addWidget(self.cluster_share_policy_label, 3, 0, 1, 2)
        cluster_content_layout.addWidget(share)

        history = QGroupBox("Cluster history · Exp data plane", cluster_content)
        history_layout = QVBoxLayout(history)
        history_label = QLabel(
            "Snapshot and standby history are bounded Exp-owned application data, "
            "separate from Connect membership and control-plane state.",
            history,
        )
        history_label.setProperty("role", "description")
        history_label.setWordWrap(True)
        history_layout.addWidget(history_label)
        cluster_content_layout.addWidget(history)
        cluster_content_layout.addStretch(1)
        cluster_scroll.setWidget(cluster_content)
        cluster_page_layout.addWidget(cluster_scroll, 1)

        remote_page = QWidget(self.section_tabs)
        remote_page_layout = QVBoxLayout(remote_page)
        self.remote_status_label = QLabel(
            "Select an authorized, connected peer to use remote capabilities.",
            remote_page,
        )
        self.remote_status_label.setProperty("role", "description")
        self.remote_status_label.setAccessibleName("Remote capabilities status")
        self.remote_status_label.setWordWrap(True)
        remote_content = QWidget(remote_page)
        remote_content_layout = QVBoxLayout(remote_content)
        remote_content_layout.setContentsMargins(0, 0, 0, 0)
        remote_content_layout.setSpacing(ui_styles.SPACING["section_gap"])

        remote_reads = QGroupBox("Authenticated peer reads", remote_content)
        remote_reads_layout = QGridLayout(remote_reads)
        self.remote_hello_button = QPushButton("Check peer identity", remote_reads)
        self.remote_node_button = QPushButton("Read node snapshot", remote_reads)
        self.remote_dashboard_button = QPushButton("Read dashboard", remote_reads)
        self.remote_component_selector = QComboBox(remote_reads)
        self.remote_component_selector.setAccessibleName("Remote component")
        for key in ("cpu", "memory", "storage", "gpu", "network", "battery"):
            self.remote_component_selector.addItem(key.upper(), key)
        self.remote_component_button = QPushButton("Read component", remote_reads)
        self.remote_processes_button = QPushButton(
            "Read process candidates", remote_reads
        )
        self.remote_storage_button = QPushButton(
            "Read storage candidates", remote_reads
        )
        for row, (left, right) in enumerate(
            (
                (self.remote_hello_button, self.remote_node_button),
                (self.remote_dashboard_button, self.remote_storage_button),
                (self.remote_processes_button, self.remote_component_selector),
            )
        ):
            remote_reads_layout.addWidget(left, row, 0)
            remote_reads_layout.addWidget(right, row, 1)
        remote_reads_layout.addWidget(self.remote_component_button, 3, 0)
        remote_content_layout.addWidget(remote_reads)

        remote_process_actions = QGroupBox(
            "Target-host process actions", remote_content
        )
        remote_process_layout = QVBoxLayout(remote_process_actions)
        self.remote_process_list = QListWidget(remote_process_actions)
        self.remote_process_list.setAccessibleName("Remote process candidates")
        self.remote_terminate_button = QPushButton(
            "Request graceful termination", remote_process_actions
        )
        self.remote_force_terminate_button = QPushButton(
            "Force terminate", remote_process_actions
        )
        self.remote_force_terminate_button.setProperty("role", "danger")
        remote_process_layout.addWidget(self.remote_process_list)
        remote_process_buttons = QHBoxLayout()
        remote_process_buttons.addWidget(self.remote_terminate_button)
        remote_process_buttons.addWidget(self.remote_force_terminate_button)
        remote_process_layout.addLayout(remote_process_buttons)
        remote_content_layout.addWidget(remote_process_actions)

        shared = QGroupBox("Shared capability demonstration", remote_content)
        shared_layout = QGridLayout(shared)
        shared_layout.addWidget(
            QLabel("exp.health-summary · runtime-local grant", shared), 0, 0, 1, 2
        )
        self.remote_request_shared_button = QPushButton(
            "Request shared summary", shared
        )
        self.remote_revoke_self_button = QPushButton("Revoke my access", shared)
        self.allow_demo_button = QPushButton("Allow selected peer", shared)
        self.revoke_demo_button = QPushButton("Revoke selected peer", shared)
        for index, button in enumerate(
            (
                self.remote_request_shared_button,
                self.remote_revoke_self_button,
                self.allow_demo_button,
                self.revoke_demo_button,
            )
        ):
            shared_layout.addWidget(button, 1 + index // 2, index % 2)
        remote_content_layout.addWidget(shared)

        surfaces = QGroupBox("Host surface approvals", remote_content)
        surfaces_layout = QGridLayout(surfaces)
        self.surface_selector = QComboBox(surfaces)
        self.surface_selector.setAccessibleName("Registered host surface")
        self.surface_access_selector = QComboBox(surfaces)
        self.surface_access_selector.setAccessibleName("Surface access mode")
        self.remote_grant_surface_button = QPushButton(
            "Save and grant access", surfaces
        )
        self.remote_revoke_surface_button = QPushButton("Revoke saved access", surfaces)
        self.remote_read_surface_button = QPushButton("Read peer surface", surfaces)
        self.remote_review_surface_button = QPushButton("Review peer surface", surfaces)
        self.surface_approval_state_label = QLabel(
            "Saved approvals and runtime grants are tracked separately.", surfaces
        )
        self.surface_approval_state_label.setWordWrap(True)
        self.surface_approval_state_label.setAccessibleName("Surface approval state")
        surfaces_layout.addWidget(self.surface_selector, 0, 0, 1, 2)
        surfaces_layout.addWidget(self.surface_access_selector, 0, 2)
        surfaces_layout.addWidget(self.remote_grant_surface_button, 1, 0)
        surfaces_layout.addWidget(self.remote_revoke_surface_button, 1, 1)
        surfaces_layout.addWidget(self.surface_approval_state_label, 2, 0, 1, 3)
        surfaces_layout.addWidget(self.remote_read_surface_button, 3, 0)
        surfaces_layout.addWidget(self.remote_review_surface_button, 3, 1)
        remote_content_layout.addWidget(surfaces)
        remote_content_layout.addStretch(1)

        remote_scroll = QScrollArea(remote_page)
        remote_scroll.setWidgetResizable(True)
        remote_scroll.setAccessibleName("Remote capability controls")
        remote_scroll.setWidget(remote_content)
        remote_page_layout.addWidget(self.remote_status_label)
        remote_page_layout.addWidget(remote_scroll, 1)

        self.section_tabs.addTab(peer_page, "Runtime and peers")
        self.section_tabs.addTab(remote_page, "Remote capabilities")
        self.section_tabs.addTab(cluster_page, "Cluster")
        layout.addWidget(self.section_tabs, 1)

        footer = QHBoxLayout()
        self.stop_button = QPushButton("Stop Connect", central)
        self.stop_button.setProperty("role", "danger")
        self.stop_button.setAccessibleName("Stop Expra Connect runtime")
        footer.addStretch(1)
        footer.addWidget(self.stop_button)
        layout.addLayout(footer)

        self.setCentralWidget(central)
        self.refresh_button.clicked.connect(self.refresh_status)
        self.stop_button.clicked.connect(self.stop_connect)
        self.pair_button.clicked.connect(self._pair_selected_peer)
        self.repair_button.clicked.connect(self._repair_selected_peer)
        self.connect_button.clicked.connect(self._connect_selected_peer)
        self.reconnect_button.clicked.connect(self._reconnect_selected_peer)
        self.disconnect_button.clicked.connect(self._disconnect_selected_peer)
        self.revoke_peer_button.clicked.connect(self._revoke_selected_peer)
        self.rotate_transport_button.clicked.connect(self._rotate_transport)
        self.invite_button.clicked.connect(self._create_invite_for_selected_peer)
        self.join_button.clicked.connect(self._join_with_invite)
        self.assign_role_button.clicked.connect(self._assign_selected_cluster_role)
        self.revoke_member_button.clicked.connect(self._revoke_selected_cluster_member)
        self.pause_worker_button.clicked.connect(
            lambda: self._cluster_worker_operation("pause_worker")
        )
        self.resume_worker_button.clicked.connect(
            lambda: self._cluster_worker_operation("resume_worker")
        )
        self.revoke_worker_button.clicked.connect(
            lambda: self._cluster_worker_operation("revoke_worker")
        )
        self.remove_connection_button.clicked.connect(
            lambda: self._cluster_worker_operation("remove_connection")
        )
        self.remove_job_button.clicked.connect(
            lambda: self._cluster_worker_operation("remove_job")
        )
        self.renew_cluster_lease_button.clicked.connect(self._renew_cluster_lease)
        self.promote_if_due_button.clicked.connect(self._promote_cluster_if_due)
        self.rejoin_as_worker_button.clicked.connect(self._rejoin_cluster_as_worker)
        self.cluster_member_list.currentItemChanged.connect(
            self._update_cluster_action_availability
        )
        self.cluster_surface_selector.currentIndexChanged.connect(
            lambda *_args: self._update_surface_access_options(
                surface_selector=self.cluster_surface_selector,
                access_selector=self.cluster_surface_access_selector,
            )
        )
        self.cluster_surface_access_selector.currentIndexChanged.connect(
            self._update_cluster_action_availability
        )
        self.cluster_share_button.clicked.connect(self._grant_cluster_surface_access)
        self.peer_list.currentItemChanged.connect(self._update_action_availability)
        self.surface_selector.currentIndexChanged.connect(
            self._update_surface_access_options
        )
        self.remote_hello_button.clicked.connect(self._read_remote_hello)
        self.remote_node_button.clicked.connect(self._read_remote_node)
        self.remote_dashboard_button.clicked.connect(self._read_remote_dashboard)
        self.remote_component_button.clicked.connect(self._read_remote_component)
        self.remote_processes_button.clicked.connect(self._read_remote_processes)
        self.remote_storage_button.clicked.connect(self._read_remote_storage)
        self.remote_terminate_button.clicked.connect(
            lambda: self._terminate_remote_process()
        )
        self.remote_force_terminate_button.clicked.connect(
            lambda: self._terminate_remote_process(force=True)
        )
        self.remote_process_list.currentItemChanged.connect(
            self._update_action_availability
        )
        self.remote_request_shared_button.clicked.connect(self._request_shared_demo)
        self.remote_revoke_self_button.clicked.connect(self._revoke_self)
        self.allow_demo_button.clicked.connect(self._allow_demo)
        self.revoke_demo_button.clicked.connect(self._revoke_demo)
        self.remote_grant_surface_button.clicked.connect(self._grant_surface)
        self.remote_revoke_surface_button.clicked.connect(self._revoke_surface)
        self.remote_read_surface_button.clicked.connect(self._read_remote_surface)
        self.remote_review_surface_button.clicked.connect(self._review_remote_surface)
        self._set_action_buttons_enabled(False)

    def _start_connect(self) -> None:
        try:
            if self.adapter.start() is not True:
                self.status_label.setText("Connect setup was declined")
                self.stop_button.setEnabled(False)
                return
        except Exception:  # noqa: BLE001 -- Keep package details out of the UI.
            self.status_label.setText("Could not start Connect")
            self.stop_button.setEnabled(False)
            return
        if self._on_runtime_started is not None:
            try:
                self._surface_policy = self._on_runtime_started(self.adapter)
            except Exception:  # noqa: BLE001 -- Surface setup failure is fatal.
                self.adapter.shutdown()
                self._runtime_stopped = True
                self.status_label.setText("Could not register Connect host surfaces")
                self.stop_button.setEnabled(False)
                return
        self._reconcile_surface_policy()
        self.status_label.setText("Connect runtime is running")
        self._set_action_buttons_enabled(True)
        self.refresh_status()

    def refresh_status(self) -> None:
        if self._closing or not self.adapter.started:
            return
        try:
            identity = self.adapter.runtime.identity
            local_node_id = identity.node_id.value
            self.identity_label.setText(f"Identity: {local_node_id}")
            discovery_started = self.adapter.status.discovery_started
            self.discovery_label.setText(
                "Discovery active" if discovery_started else "Discovery not active"
            )
            try:
                diagnostics = self.adapter.diagnostics()
            except Exception:  # noqa: BLE001 -- Diagnostics are best effort.
                diagnostics = None
            if isinstance(diagnostics, dict):
                route_count = diagnostics.get("routes")
                session_count = diagnostics.get("sessions")
                routes = (
                    len(route_count) if isinstance(route_count, list) else "unknown"
                )
                sessions = (
                    len(session_count) if isinstance(session_count, list) else "unknown"
                )
                self.diagnostics_label.setText(
                    f"Runtime diagnostics: {routes} routes · {sessions} sessions"
                )
            else:
                self.diagnostics_label.setText("Runtime diagnostics unavailable")

            selected_payload = self._selected_peer_payload()
            selected_peer_id = (
                selected_payload.get("node_id")
                if selected_payload is not None
                else None
            )
            self.peer_list.clear()
            peers = tuple(self.adapter.peers)
            records = tuple(self.adapter.node_records)
            records_by_id = {
                record.node_id.value: record
                for record in records
                if getattr(getattr(record, "node_id", None), "value", None)
            }
            connected_ids = {
                node_id.value
                for node_id in self.adapter.connections
                if isinstance(node_id, NodeId)
            }
            displayed_ids: set[str] = set()
            for candidate in peers:
                node_id = getattr(candidate, "stable_id", "")
                if not isinstance(node_id, str) or not node_id:
                    continue
                hostname = getattr(candidate, "hostname", "Unknown peer")
                identity_fingerprint = getattr(candidate, "identity_fingerprint", "")
                transport_fingerprint = getattr(candidate, "transport_fingerprint", "")
                displayed_ids.add(node_id)
                record = records_by_id.get(node_id)
                trust = (
                    getattr(getattr(record, "trust", None), "value", "unknown")
                    if record is not None
                    else "discovered"
                )
                relationship = self._relationship_value(node_id)
                connection = "online" if node_id in connected_ids else "offline"
                membership = self._membership_value(record)
                item = QListWidgetItem(
                    f"{hostname} — {trust}, {connection} · {relationship}"
                )
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {
                        "node_id": node_id,
                        "identity_fingerprint": identity_fingerprint,
                        "transport_fingerprint": transport_fingerprint,
                        "discovered": True,
                        "trusted": trust in {"trusted", "authorized"},
                        "relationship": relationship,
                        "connected": node_id in connected_ids,
                        "membership": membership,
                    },
                )
                self.peer_list.addItem(item)

            for record in records:
                node_id = getattr(getattr(record, "node_id", None), "value", "")
                if not node_id or node_id == local_node_id or node_id in displayed_ids:
                    continue
                trust = getattr(getattr(record, "trust", None), "value", "unknown")
                relationship = self._relationship_value(node_id)
                connection = "online" if node_id in connected_ids else "offline"
                membership = self._membership_value(record)
                item = QListWidgetItem(
                    f"{node_id} — {trust}, {connection} · {relationship}"
                )
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {
                        "node_id": node_id,
                        "discovered": False,
                        "trusted": trust in {"trusted", "authorized"},
                        "relationship": relationship,
                        "connected": node_id in connected_ids,
                        "membership": membership,
                    },
                )
                self.peer_list.addItem(item)

            if isinstance(selected_peer_id, str):
                for row in range(self.peer_list.count()):
                    item = self.peer_list.item(row)
                    payload = item.data(Qt.ItemDataRole.UserRole)
                    if (
                        isinstance(payload, dict)
                        and payload.get("node_id") == selected_peer_id
                    ):
                        self.peer_list.setCurrentRow(row)
                        break

            self.peer_status_label.setText(
                f"{self.peer_list.count()} peer(s) available"
                if self.peer_list.count()
                else (
                    "Searching for peers…"
                    if discovery_started
                    else "No peers currently available"
                )
            )
            self._update_selected_peer_details()
            self._reconcile_surface_policy()
            self._refresh_surface_options()
            self._refresh_surface_options(
                surface_selector=self.cluster_surface_selector,
                access_selector=self.cluster_surface_access_selector,
            )
            self._update_surface_approval_state()
            self._update_remote_availability()
            cluster = self._project_cluster_status(self.adapter.cluster_status())
            self._cluster_projection = cluster
            self.cluster_member_list.clear()
            if cluster is None:
                self.cluster_status_label.setText("Cluster status unavailable")
            elif not cluster.enabled:
                self.cluster_status_label.setText("Cluster participation is disabled")
            elif not cluster.cluster_id:
                self.cluster_status_label.setText("Not joined to a cluster")
            else:
                coordinator = cluster.coordinator_id or "Unavailable"
                epoch = (
                    str(cluster.epoch) if cluster.epoch is not None else "Unavailable"
                )
                lines = [
                    f"Cluster {cluster.cluster_id} · {len(cluster.members)} member(s)",
                    f"Coordinator: {coordinator} · Epoch: {epoch}",
                ]
                lines.extend(
                    f"{member.node_id} · {', '.join(member.roles)}"
                    f"{' · paused' if member.paused else ''}"
                    f"{' · revoked' if member.revoked else ''}"
                    for member in cluster.members
                )
                self.cluster_status_label.setText("\n".join(lines))
                for member in cluster.members:
                    item = QListWidgetItem(
                        f"{member.node_id} · {', '.join(member.roles)}"
                        f"{' · paused' if member.paused else ''}"
                        f"{' · revoked' if member.revoked else ''}",
                        self.cluster_member_list,
                    )
                    item.setData(Qt.ItemDataRole.UserRole, member.node_id)
            self._update_cluster_action_availability()
            self._set_action_buttons_enabled(True)
        except Exception:  # noqa: BLE001 -- Status failure is presented, not raised.
            self.status_label.setText("Could not refresh Connect status")

    def stop_connect(self) -> None:
        self._shutdown_runtime()

    def _shutdown_runtime(self) -> None:
        if self._runtime_stopped:
            return
        self._runtime_stopped = True
        self._closing = True
        self._cluster_share_timer.stop()
        self._cluster_share_expires_at = None
        self._pairing_approval.close()
        shutdown_error = False
        try:
            if self._surface_policy is not None:
                self._surface_policy.clear_runtime(self.adapter)
        except Exception:  # noqa: BLE001 -- Continue to package shutdown.
            shutdown_error = True
        try:
            self.adapter.shutdown()
        except Exception:  # noqa: BLE001 -- Shutdown must not escape closeEvent.
            shutdown_error = True
        if shutdown_error:
            self.status_label.setText("Connect stopped with a shutdown error")
        else:
            self.status_label.setText("Connect runtime stopped")
        self.discovery_label.setText("Discovery stopped")
        self.diagnostics_label.setText("Runtime diagnostics unavailable while stopped")
        self.identity_label.setText("Identity: not running")
        self.peer_list.clear()
        self.peer_status_label.setText("Connect is stopped")
        self.cluster_status_label.setText("Cluster status unavailable while stopped")
        self._set_action_buttons_enabled(False)

    def _reconcile_surface_policy(self) -> None:
        if self._surface_policy is None or not self.adapter.started:
            return
        try:
            self._surface_policy.reapply(self.adapter)
        except Exception:  # noqa: BLE001 -- Keep policy diagnostics sanitized.
            self._surface_policy_unavailable = True
        else:
            self._surface_policy_unavailable = False

    def _pair_selected_peer(self) -> None:
        item = self.peer_list.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(payload, dict):
            return
        node_id = payload.get("node_id")
        identity_fingerprint = payload.get("identity_fingerprint")
        transport_fingerprint = payload.get("transport_fingerprint")
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(identity_fingerprint, str)
            or not identity_fingerprint
            or not isinstance(transport_fingerprint, str)
            or not transport_fingerprint
        ):
            self.status_label.setText("Select a discovered peer to pair")
            return
        response = QMessageBox.question(
            self,
            "Review Expra Connect peer",
            "Pair only after verifying both fingerprints with the other machine.\n\n"
            f"Peer: {node_id}\n"
            f"Identity fingerprint: {identity_fingerprint}\n"
            f"Transport fingerprint: {transport_fingerprint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Pairing with peer…",
            lambda: self.adapter.pair_peer(NodeId(node_id)),
            "Peer paired through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _repair_selected_peer(self) -> None:
        payload = self._selected_peer_payload()
        if payload is None:
            return
        node_id = payload.get("node_id")
        identity_fingerprint = payload.get("identity_fingerprint")
        transport_fingerprint = payload.get("transport_fingerprint")
        if not isinstance(node_id, str) or not node_id:
            return
        if (
            not isinstance(identity_fingerprint, str)
            or not identity_fingerprint
            or not isinstance(transport_fingerprint, str)
            or not transport_fingerprint
        ):
            self.status_label.setText("Repair is unavailable without both fingerprints")
            return
        response = QMessageBox.question(
            self,
            "Review peer trust repair",
            "Repair trust only after verifying both current fingerprints with the peer.\n\n"
            f"Peer: {node_id}\nIdentity fingerprint: {identity_fingerprint}\n"
            f"Transport fingerprint: {transport_fingerprint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Repairing peer trust…",
            lambda: self.adapter.repair_peer(NodeId(node_id)),
            "Peer trust repaired through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _connect_selected_peer(self) -> None:
        payload = self._selected_peer_payload()
        node_id = payload.get("node_id") if payload is not None else None
        if not isinstance(node_id, str) or not node_id:
            return
        self._run_operation(
            "Connecting to peer…",
            lambda: self.adapter.connect_peer(NodeId(node_id)),
            "Connected through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _reconnect_selected_peer(self) -> None:
        payload = self._selected_peer_payload()
        node_id = payload.get("node_id") if payload is not None else None
        if not isinstance(node_id, str) or not node_id:
            return
        response = QMessageBox.question(
            self,
            "Reconnect peer",
            f"Reconnect to {node_id}? An active remote operation may be interrupted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Reconnecting to peer…",
            lambda: self.adapter.reconnect_peer(NodeId(node_id)),
            "Reconnected through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _disconnect_selected_peer(self) -> None:
        payload = self._selected_peer_payload()
        node_id = payload.get("node_id") if payload is not None else None
        if not isinstance(node_id, str) or not node_id:
            return
        self._run_operation(
            "Disconnecting from peer…",
            lambda: self.adapter.disconnect_peer(NodeId(node_id)),
            "Disconnected; peer trust and membership are unchanged",
            lambda _result: self.refresh_status(),
        )

    def _revoke_selected_peer(self) -> None:
        payload = self._selected_peer_payload()
        node_id = payload.get("node_id") if payload is not None else None
        if not isinstance(node_id, str) or not node_id:
            return
        response = QMessageBox.question(
            self,
            "Revoke peer trust",
            f"Revoke Connect trust with {node_id}? This does not remove separate cluster membership.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Revoking peer trust…",
            lambda: self.adapter.revoke_peer(NodeId(node_id)),
            "Peer trust revoked through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _rotate_transport(self) -> None:
        response = QMessageBox.question(
            self,
            "Rotate Connect transport identity",
            "Rotate this installation's TLS transport identity? Peers may need to "
            "reconnect and verify transport continuity.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Rotating transport identity…",
            self.adapter.rotate_transport,
            "Transport identity rotated through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _create_invite_for_selected_peer(self) -> None:
        item = self.peer_list.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        node_id = payload.get("node_id") if isinstance(payload, dict) else None
        if not isinstance(node_id, str) or not node_id:
            self.status_label.setText("Select a peer for the targeted invite")
            return

        def show_invite(invite: InviteRecord) -> None:
            try:
                if (
                    isinstance(invite.expires_at, bool)
                    or not isinstance(invite.expires_at, (int, float))
                    or not math.isfinite(float(invite.expires_at))
                ):
                    raise ValueError("invite expiry is invalid")
                blob = encode_invite_blob(invite)
                expires_at = datetime.fromtimestamp(
                    float(invite.expires_at), timezone.utc
                )
            except (OverflowError, OSError, TypeError, ValueError):
                self.status_label.setText("Connect returned an invalid invite")
                return
            QMessageBox.information(
                self,
                "Expra Connect invite",
                "This targeted invite expires at "
                f"{expires_at:%Y-%m-%d %H:%M:%S UTC}. Share it only with the intended peer.\n\n"
                + blob,
            )

        self._run_operation(
            "Creating targeted invite…",
            lambda: self.adapter.create_invite(NodeId(node_id)),
            "Targeted invite created",
            show_invite,
        )

    def _join_with_invite(self) -> None:
        blob, accepted = QInputDialog.getText(
            self, "Join a cluster", "Paste the targeted Expra Connect invite:"
        )
        if not accepted or not blob.strip():
            return
        try:
            invite = decode_invite_blob(blob.strip())
            identity = self.adapter.runtime.identity
            local_node_id = identity.node_id.value
            if invite.target_node_id != local_node_id:
                self.status_label.setText(
                    "That invite was issued for a different Connect identity"
                )
                return
            if invite.expires_at <= time.time():
                self.status_label.setText("That targeted invite has expired")
                return
            if not isinstance(invite.coordinator_id, str) or not invite.coordinator_id:
                self.status_label.setText("That invite has no valid coordinator")
                return
            status = self._project_cluster_status(self.adapter.cluster_status())
            if status is None:
                self.status_label.setText(
                    "Current cluster status is unavailable; join was refused"
                )
                return
            if not status.enabled:
                self.status_label.setText(
                    "Cluster support is disabled; join was refused"
                )
                return
            if status.members:
                self.status_label.setText(
                    "Joining would replace an existing cluster; operation refused"
                )
                return
            coordinator_id = NodeId(invite.coordinator_id)
        except (TypeError, ValueError):
            self.status_label.setText("That Expra Connect invite could not be read")
            return
        except Exception:  # noqa: BLE001 -- Package errors are user-facing.
            self.status_label.setText("Expra Connect could not join the cluster")
            return
        self._run_operation(
            "Joining cluster…",
            lambda: self.adapter.join_cluster(
                self.adapter.connect_peer(coordinator_id), invite
            ),
            "Joined the cluster through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _selected_cluster_member_id(self) -> str | None:
        item = self.cluster_member_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, str) and value else None

    def _selected_cluster_member(self) -> _ClusterMemberProjection | None:
        target = self._selected_cluster_member_id()
        projection = self._cluster_projection
        if target is None or projection is None:
            return None
        return next(
            (member for member in projection.members if member.node_id == target), None
        )

    def _local_is_cluster_coordinator(self) -> bool:
        projection = self._cluster_projection
        if projection is None or not projection.coordinator_id:
            return False
        try:
            identity = self.adapter.runtime.identity.node_id.value
        except (AttributeError, TypeError):
            return False
        return isinstance(identity, str) and identity == projection.coordinator_id

    def _local_cluster_member(self) -> _ClusterMemberProjection | None:
        projection = self._cluster_projection
        if projection is None:
            return None
        try:
            identity = self.adapter.runtime.identity.node_id.value
        except (AttributeError, TypeError):
            return None
        if not isinstance(identity, str):
            return None
        return next(
            (member for member in projection.members if member.node_id == identity),
            None,
        )

    def _cluster_promotion_is_available(self) -> bool:
        projection = self._cluster_projection
        return projection is not None and any(
            "subcoordinator" in member.roles
            and not member.paused
            and not member.revoked
            for member in projection.members
        )

    def _local_worker_rejoin_is_available(self) -> bool:
        projection = self._cluster_projection
        member = self._local_cluster_member()
        return (
            projection is not None
            and bool(projection.cluster_id)
            and projection.epoch is not None
            and projection.coordinator_id is not None
            and member is not None
            and "coordinator" in member.roles
            and not member.revoked
            and member.node_id != projection.coordinator_id
        )

    def _update_cluster_action_availability(self, *_args: object) -> None:
        active = (
            self.adapter.started and not self._closing and not self._operation_active
        )
        is_coordinator = active and self._local_is_cluster_coordinator()
        target = self._selected_cluster_member_id()
        member = self._selected_cluster_member()
        projection = self._cluster_projection
        has_cluster = projection is not None and bool(projection.cluster_id)
        coordinator_id = projection.coordinator_id if projection is not None else None
        self.renew_cluster_lease_button.setEnabled(is_coordinator)
        self.promote_if_due_button.setEnabled(
            active and has_cluster and self._cluster_promotion_is_available()
        )
        self.rejoin_as_worker_button.setEnabled(
            active and self._local_worker_rejoin_is_available()
        )
        can_manage_member = (
            is_coordinator
            and target is not None
            and member is not None
            and not member.revoked
            and target != coordinator_id
        )
        self.assign_role_button.setEnabled(can_manage_member)
        self.revoke_member_button.setEnabled(can_manage_member)
        remote_target = False
        relationship = "unknown"
        connected = False
        if is_coordinator and target is not None:
            relationship = self._relationship_value(target)
            connections = getattr(self.adapter, "connections", ())
            try:
                connected = NodeId(target) in connections
            except ValueError:
                connected = False
            remote_target = (
                connected
                and member is not None
                and not member.revoked
                and "worker" in member.roles
                and relationship
                in {
                    RelationshipState.OUTBOUND_TRUSTED.value,
                    RelationshipState.BIDIRECTIONAL.value,
                }
            )
        self.pause_worker_button.setEnabled(
            remote_target and member is not None and not member.paused
        )
        self.resume_worker_button.setEnabled(
            remote_target and member is not None and member.paused
        )
        self.revoke_worker_button.setEnabled(remote_target)
        self.remove_connection_button.setEnabled(
            is_coordinator
            and connected
            and member is not None
            and relationship
            in {
                RelationshipState.OUTBOUND_TRUSTED.value,
                RelationshipState.BIDIRECTIONAL.value,
            }
        )
        self.remove_job_button.setEnabled(remote_target)
        selected_surface_id = self.cluster_surface_selector.currentData()
        surface_access = self._selected_surface_access(
            self.cluster_surface_access_selector
        )
        surface_definitions = getattr(self.adapter, "surface_definitions", {})
        surface_available = (
            isinstance(surface_definitions, Mapping)
            and isinstance(selected_surface_id, str)
            and selected_surface_id in surface_definitions
        )
        self.cluster_share_button.setEnabled(
            active
            and target is not None
            and surface_access is not None
            and surface_available
        )

    def _assign_selected_cluster_role(self) -> None:
        target = self._selected_cluster_member_id()
        role = self.cluster_role_selector.currentData()
        if target is None or not isinstance(role, str):
            return
        try:
            cluster_role = ClusterRole(role)
            node_id = NodeId(target)
        except ValueError:
            self.status_label.setText("Selected cluster role or member is invalid")
            return
        response = QMessageBox.question(
            self,
            "Assign cluster role",
            f"Assign {cluster_role.value} to {target}? Connect will enforce current authority.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Assigning cluster role…",
            lambda: self.adapter.assign_role(node_id, cluster_role),
            "Cluster role updated through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _revoke_selected_cluster_member(self) -> None:
        target = self._selected_cluster_member_id()
        if target is None:
            return
        response = QMessageBox.question(
            self,
            "Revoke cluster member",
            f"Revoke {target} from this cluster? The member will lose cluster authority.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        try:
            node_id = NodeId(target)
        except ValueError:
            self.status_label.setText("Selected cluster member is invalid")
            return
        self._run_operation(
            "Revoking cluster membership…",
            lambda: self.adapter.revoke_member(node_id),
            "Cluster membership revoked through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _cluster_worker_operation(self, operation: str) -> None:
        target = self._selected_cluster_member_id()
        descriptions = {
            "pause_worker": ("Pause worker", "pause worker activity"),
            "resume_worker": ("Resume worker", "resume worker activity"),
            "revoke_member": ("Revoke worker", "remove cluster authority"),
            "remove_connection": ("Remove worker connection", "remove its connection"),
            "remove_job": ("Remove worker job", "clear its active job assignment"),
        }
        description = descriptions.get(operation)
        if target is None or description is None:
            return
        title, consequence = description
        response = QMessageBox.question(
            self,
            title,
            f"Apply this operation to {target}: {consequence}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        try:
            node_id = NodeId(target)
        except ValueError:
            self.status_label.setText("Selected cluster member is invalid")
            return
        self._run_operation(
            f"{title}…",
            lambda: self.adapter.cluster_member_operation(operation, node_id),
            f"{title} completed through Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _renew_cluster_lease(self) -> None:
        if not self._local_is_cluster_coordinator():
            self.status_label.setText("Coordinator lease renewal is unavailable")
            return
        self._run_operation(
            "Renewing coordinator lease…",
            self.adapter.renew_coordinator_lease,
            "Coordinator lease renewal evaluated by Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _promote_cluster_if_due(self) -> None:
        if not self._cluster_promotion_is_available():
            return
        response = QMessageBox.question(
            self,
            "Evaluate failover promotion",
            "Ask Connect to evaluate whether an eligible subcoordinator should be promoted?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Evaluating failover promotion…",
            lambda: self.adapter.promote_if_due(now=time.time()),
            "Failover promotion evaluated by Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _rejoin_cluster_as_worker(self) -> None:
        projection = self._cluster_projection
        if projection is None or not self._local_worker_rejoin_is_available():
            self.status_label.setText(
                "Worker rejoin is unavailable for the current Connect role/epoch state"
            )
            return
        try:
            local_node_id = self.adapter.runtime.identity.node_id
        except (AttributeError, TypeError):
            self.status_label.setText("Local Connect identity is unavailable")
            return
        response = QMessageBox.question(
            self,
            "Rejoin as worker",
            "Rejoin this Connect identity as a worker at the current cluster epoch?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Rejoining cluster as worker…",
            lambda: self.adapter.rejoin_as_worker(
                local_node_id, current_epoch=projection.epoch
            ),
            "Worker rejoin evaluated by Expra Connect",
            lambda _result: self.refresh_status(),
        )

    def _grant_cluster_surface_access(self) -> None:
        target = self._selected_cluster_member_id()
        surface_id = self.cluster_surface_selector.currentData()
        access = self._selected_surface_access(self.cluster_surface_access_selector)
        if (
            target is None
            or not isinstance(surface_id, str)
            or access is None
            or self._cluster_projection is None
            or target
            not in {member.node_id for member in self._cluster_projection.members}
        ):
            self.status_label.setText("Select a current cluster member and surface")
            return
        try:
            source = NodeId(target)
        except ValueError:
            self.status_label.setText("Selected cluster member is invalid")
            return
        expires_at = time.time() + self.cluster_share_duration.value() * 60
        expiry_label = datetime.fromtimestamp(expires_at, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        response = QMessageBox.question(
            self,
            "Grant temporary cluster surface access",
            f"Grant {access.value} access to {surface_id} for {target} until "
            f"{expiry_label}? This grant expires automatically and cannot be "
            "revoked early in this Connect version.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Granting temporary cluster surface access…",
            lambda: self.adapter.grant_cluster_surface_access(
                source, surface_id, access=access, expires_at=expires_at
            ),
            "Temporary cluster surface access granted",
            lambda _result: self._record_cluster_share_expiry(expires_at),
        )

    def _record_cluster_share_expiry(self, expires_at: float) -> None:
        self._cluster_share_expires_at = expires_at
        self._cluster_share_timer.start()
        self._update_cluster_share_countdown()

    def _update_cluster_share_countdown(self) -> None:
        expires_at = self._cluster_share_expires_at
        if expires_at is None:
            self.cluster_share_expiry_label.setText("No active cluster share.")
            return
        remaining = max(0, math.ceil(expires_at - time.time()))
        if remaining == 0:
            self._cluster_share_timer.stop()
            self._cluster_share_expires_at = None
            self.cluster_share_expiry_label.setText(
                "Cluster share expired automatically; no early-revocation control is available."
            )
            return
        minutes, seconds = divmod(remaining, 60)
        self.cluster_share_expiry_label.setText(
            f"Cluster share expires in {minutes:02d}:{seconds:02d}."
        )

    def _run_operation(
        self,
        running_text: str,
        operation: Callable[[], Any],
        success_text: str,
        after_success: Callable[[Any], None],
    ) -> None:
        if self._closing or self._operation_active:
            return
        self._operation_active = True
        self.status_label.setText(running_text)
        self._set_action_buttons_enabled(False)

        def run() -> None:
            try:
                result = operation()
            except Exception:  # noqa: BLE001 -- Worker errors return to UI status.
                self._delivery.submit(lambda: self._finish_operation(error=True))
                return
            self._delivery.submit(
                lambda: self._finish_operation(
                    result=result,
                    success_text=success_text,
                    after_success=after_success,
                )
            )

        QThreadPool.globalInstance().start(QRunnable.create(run))

    def _finish_operation(
        self,
        *,
        error: bool = False,
        result: Any = None,
        success_text: str = "",
        after_success: Callable[[Any], None] | None = None,
    ) -> None:
        if self._closing:
            return
        self._operation_active = False
        self.status_label.setText(
            (
                "Expra Connect operation failed. Refresh status and review the "
                "operation's prerequisites before retrying."
            )
            if error
            else success_text
        )
        if error and self.section_tabs.currentIndex() == 1:
            self.remote_status_label.setText(
                "Remote operation failed or was denied. Check the peer's "
                "connection and current authorization before retrying."
            )
        self._set_action_buttons_enabled(self.adapter.started)
        if not error and after_success is not None:
            after_success(result)

    def _read_remote(self, running_text: str, method: Callable[[NodeId], Any]) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        self._run_operation(
            running_text,
            lambda: method(peer_id),
            "Authenticated peer response received",
            lambda result: self._show_remote_result("Peer response", result),
        )

    def _read_remote_hello(self) -> None:
        self._read_remote("Checking peer identity…", self.adapter.remote_hello)

    def _read_remote_node(self) -> None:
        self._read_remote("Reading node snapshot…", self.adapter.remote_node_snapshot)

    def _read_remote_dashboard(self) -> None:
        self._read_remote("Reading dashboard…", self.adapter.remote_dashboard_snapshot)

    def _read_remote_component(self) -> None:
        peer_id = self._selected_peer_node_id()
        key = self.remote_component_selector.currentData()
        if peer_id is None or not isinstance(key, str):
            return
        self._run_operation(
            "Reading component summary…",
            lambda: self.adapter.remote_component_summary(peer_id, key),
            "Remote component summary received",
            lambda result: self._show_remote_result("Component summary", result),
        )

    def _read_remote_processes(self) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        self._run_operation(
            "Reading process candidates…",
            lambda: self.adapter.remote_process_candidates(peer_id),
            "Remote process candidates received",
            self._populate_remote_processes,
        )

    def _populate_remote_processes(self, result: object) -> None:
        self.remote_process_list.clear()
        if not isinstance(result, list):
            self.remote_status_label.setText(
                "Remote process candidates: response unavailable"
            )
            return
        for candidate in result:
            pid = getattr(candidate, "pid", None)
            name = getattr(candidate, "name", None)
            create_time = getattr(candidate, "create_time", None)
            allowed = getattr(candidate, "action_allowed", False)
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(name, str)
                or not name
                or isinstance(create_time, bool)
                or not isinstance(create_time, (int, float))
                or not math.isfinite(float(create_time))
                or create_time < 0
            ):
                continue
            display_name = "".join(
                character if character.isprintable() else " " for character in name
            ).strip()[:120]
            if not display_name:
                continue
            item = QListWidgetItem(
                f"{display_name} · PID {pid} · started {float(create_time):.3f}"
            )
            item.setData(
                Qt.ItemDataRole.UserRole,
                {
                    "pid": pid,
                    "name": display_name,
                    "create_time": float(create_time),
                    "action_allowed": allowed is True,
                },
            )
            self.remote_process_list.addItem(item)
        self.remote_status_label.setText(
            f"Remote process candidates: {self.remote_process_list.count()} available"
        )
        self._set_action_buttons_enabled(
            not self._operation_active and self.adapter.started
        )

    def _terminate_remote_process(self, *, force: bool = False) -> None:
        peer_id = self._selected_peer_node_id()
        item = self.remote_process_list.currentItem()
        process = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if (
            peer_id is None
            or not isinstance(process, dict)
            or process.get("action_allowed") is not True
        ):
            return
        pid = process.get("pid")
        name = process.get("name")
        create_time = process.get("create_time")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or not isinstance(name, str)
            or not isinstance(create_time, float)
        ):
            return
        action = "force termination" if force else "graceful termination"
        consequences = (
            "The target host will immediately request force termination."
            if force
            else "The target host will request graceful shutdown first."
        )
        response = QMessageBox.question(
            self,
            f"Confirm {action}?",
            f"Peer: {peer_id.value}\nProcess: {name}\nPID: {pid}\n"
            f"Creation time: {create_time:.3f}\n\n{consequences}\n"
            "The target host performs the final safety checks.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        method = (
            self.adapter.remote_force_quit
            if force
            else self.adapter.remote_request_quit
        )
        running_text = (
            "Requesting force termination…"
            if force
            else "Requesting graceful termination…"
        )
        self._run_operation(
            running_text,
            lambda: method(peer_id, [pid], expected_create_times={pid: create_time}),
            "Target-host process action completed",
            lambda _result: self.remote_status_label.setText(
                "The target host returned a process-action result."
            ),
        )

    def _read_remote_storage(self) -> None:
        self._read_remote(
            "Reading storage candidates…", self.adapter.remote_storage_candidates
        )

    def _read_remote_surface(self) -> None:
        peer_id = self._selected_peer_node_id()
        surface_id = self.surface_selector.currentData()
        if peer_id is None or not isinstance(surface_id, str):
            return
        self._run_operation(
            "Reading peer surface…",
            lambda: self.adapter.remote_read_surface(peer_id, surface_id),
            "Peer surface read completed",
            lambda result: self._show_remote_result("Peer surface", result),
        )

    def _review_remote_surface(self) -> None:
        peer_id = self._selected_peer_node_id()
        surface_id = self.surface_selector.currentData()
        if peer_id is None or not isinstance(surface_id, str):
            return
        self._run_operation(
            "Reviewing peer surface…",
            lambda: self.adapter.remote_review_surface(peer_id, surface_id),
            "Peer surface review completed",
            lambda result: self._show_remote_result("Peer surface review", result),
        )

    def _show_remote_result(self, label: str, result: object) -> None:
        count: int | None = None
        if isinstance(result, (list, tuple)):
            count = len(result)
        elif isinstance(result, dict) and isinstance(result.get("items"), list):
            count = len(result["items"])
        elif isinstance(result, dict) and not result:
            count = 0
        else:
            resources = getattr(result, "resources", None)
            if isinstance(resources, (list, tuple)):
                count = len(resources)
            else:
                dashboard = getattr(result, "dashboard", None)
                dashboard_resources = getattr(dashboard, "resources", None)
                if isinstance(dashboard_resources, (list, tuple)):
                    count = len(dashboard_resources)
        detail = (
            f"{count} item(s)"
            if count is not None
            else "response received"
            if result is not None
            else "empty response"
        )
        self.remote_status_label.setText(f"{label}: {detail}")

    def _request_shared_demo(self) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        self._run_operation(
            "Requesting shared summary…",
            lambda: self.adapter.remote_request_shared(
                peer_id, "exp.health-summary", {}
            ),
            "Shared capability response received",
            lambda result: self._show_remote_result("Shared summary", result),
        )

    def _revoke_self(self) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        response = QMessageBox.question(
            self,
            "Revoke this peer's access?",
            "Ask the selected peer to remove this installation's inbound grant. "
            "Future authenticated requests may fail until access is approved again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response is not QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            "Requesting access revocation…",
            lambda: self.adapter.remote_revoke_self(peer_id),
            "Peer access revocation requested",
            lambda _result: self.remote_status_label.setText(
                "The selected peer processed the access revocation request."
            ),
        )

    def _allow_demo(self) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        self._run_operation(
            "Allowing shared summary…",
            lambda: self.adapter.allow_shared_capability(peer_id, "exp.health-summary"),
            "Runtime-local shared summary allowed for this peer",
            lambda _result: None,
        )

    def _revoke_demo(self) -> None:
        peer_id = self._selected_peer_node_id()
        if peer_id is None:
            return
        self._run_operation(
            "Revoking shared summary…",
            lambda: self.adapter.revoke_shared_capability(
                peer_id, "exp.health-summary"
            ),
            "Runtime-local shared summary revoked for this peer",
            lambda _result: None,
        )

    def _grant_surface(self) -> None:
        peer_id = self._selected_peer_node_id()
        surface_id = self.surface_selector.currentData()
        access = self._selected_surface_access()
        if (
            peer_id is None
            or not isinstance(surface_id, str)
            or not isinstance(access, SurfaceAccess)
            or self._surface_policy is None
        ):
            return
        if access is SurfaceAccess.ACTION:
            definition = self.adapter.surface_definitions.get(surface_id)
            action_names = sorted(definition.actions) if definition is not None else []
            if not action_names:
                self.surface_approval_state_label.setText(
                    "This surface has no registered actions."
                )
                return
            response = QMessageBox.question(
                self,
                "Approve all actions on this surface?",
                "This grants the selected peer access to every action on "
                f"{surface_id}:\n\n" + "\n".join(action_names),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response is not QMessageBox.StandardButton.Yes:
                return
        approval = SurfaceGrantApproval(peer_id, surface_id, access)
        policy = self._surface_policy
        if policy is None:
            return
        self._run_operation(
            "Saving surface approval…",
            lambda: policy.grant(self.adapter, approval),
            "Surface approval saved and applied",
            lambda _result: self.refresh_status(),
        )

    def _revoke_surface(self) -> None:
        peer_id = self._selected_peer_node_id()
        surface_id = self.surface_selector.currentData()
        access = self._selected_surface_access()
        if (
            peer_id is None
            or not isinstance(surface_id, str)
            or not isinstance(access, SurfaceAccess)
            or self._surface_policy is None
        ):
            return
        approval = SurfaceGrantApproval(peer_id, surface_id, access)
        policy = self._surface_policy
        if policy is None:
            return
        self._run_operation(
            "Revoking surface approval…",
            lambda: policy.revoke(self.adapter, approval),
            "Surface approval revoked",
            lambda _result: self.refresh_status(),
        )

    def _refresh_surface_options(
        self,
        *,
        surface_selector: QComboBox | None = None,
        access_selector: QComboBox | None = None,
    ) -> None:
        surface_selector = (
            self.surface_selector if surface_selector is None else surface_selector
        )
        access_selector = (
            self.surface_access_selector if access_selector is None else access_selector
        )
        definitions = getattr(self.adapter, "surface_definitions", {})
        if not isinstance(definitions, Mapping):
            definitions = {}
        selected_id = surface_selector.currentData()
        surface_selector.blockSignals(True)
        surface_selector.clear()
        for surface_id, definition in definitions.items():
            if isinstance(surface_id, str) and definition is not None:
                surface_selector.addItem(surface_id, surface_id)
        if selected_id is not None:
            index = surface_selector.findData(selected_id)
            if index >= 0:
                surface_selector.setCurrentIndex(index)
        surface_selector.blockSignals(False)
        self._update_surface_access_options(
            surface_selector=surface_selector, access_selector=access_selector
        )

    def _update_surface_access_options(
        self,
        *_args: object,
        surface_selector: QComboBox | None = None,
        access_selector: QComboBox | None = None,
    ) -> None:
        surface_selector = (
            self.surface_selector if surface_selector is None else surface_selector
        )
        access_selector = (
            self.surface_access_selector if access_selector is None else access_selector
        )
        surface_id = surface_selector.currentData()
        definitions = getattr(self.adapter, "surface_definitions", {})
        definition = (
            definitions.get(surface_id)
            if isinstance(surface_id, str) and isinstance(definitions, Mapping)
            else None
        )
        modes: list[SurfaceAccess] = []
        if definition is not None and callable(getattr(definition, "read", None)):
            modes.append(SurfaceAccess.READ)
        if definition is not None and callable(getattr(definition, "review", None)):
            modes.append(SurfaceAccess.REVIEW)
        if definition is not None and getattr(definition, "actions", {}):
            modes.append(SurfaceAccess.ACTION)
        selected_access = self._selected_surface_access(access_selector)
        access_selector.clear()
        for mode in modes:
            access_selector.addItem(mode.value.title(), mode.value)
        if selected_access is not None and selected_access in modes:
            access_selector.setCurrentIndex(modes.index(selected_access))
        if surface_selector is self.surface_selector:
            self._update_surface_approval_state()
            self._set_action_buttons_enabled(
                not self._operation_active and self.adapter.started
            )
        else:
            self._update_cluster_action_availability()

    def _update_surface_approval_state(self) -> None:
        peer_id = self._selected_peer_node_id()
        surface_id = self.surface_selector.currentData()
        access = self._selected_surface_access()
        policy = self._surface_policy
        if self._surface_policy_unavailable:
            self.surface_approval_state_label.setText(
                "Saved surface approvals are unavailable; no grants are reapplied."
            )
            return
        if (
            peer_id is None
            or not isinstance(surface_id, str)
            or not isinstance(access, SurfaceAccess)
            or policy is None
        ):
            self.surface_approval_state_label.setText(
                "Saved approvals and runtime grants are tracked separately."
            )
            return
        approval = SurfaceGrantApproval(peer_id, surface_id, access)
        saved = approval in policy.desired_approvals
        applied = approval in policy.applied_approvals
        self.surface_approval_state_label.setText(
            f"Saved approval: {'yes' if saved else 'no'} · "
            f"Runtime grant applied: {'yes' if applied else 'no'}"
        )

    def _selected_peer_node_id(self) -> NodeId | None:
        payload = self._selected_peer_payload()
        node_id = payload.get("node_id") if payload is not None else None
        if not isinstance(node_id, str) or not node_id:
            return None
        try:
            return NodeId(node_id)
        except ValueError:
            return None

    def _selected_surface_access(
        self, selector: QComboBox | None = None
    ) -> SurfaceAccess | None:
        selector = self.surface_access_selector if selector is None else selector
        value = selector.currentData()
        if isinstance(value, SurfaceAccess):
            return value
        if isinstance(value, str):
            try:
                return SurfaceAccess(value)
            except ValueError:
                return None
        return None

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        self.refresh_button.setEnabled(enabled)
        payload = self._selected_peer_payload()
        relationship = payload.get("relationship") if payload is not None else None
        has_fingerprints = payload is not None and all(
            isinstance(payload.get(key), str) and payload.get(key)
            for key in ("identity_fingerprint", "transport_fingerprint")
        )
        connected = payload is not None and payload.get("connected") is True
        outbound_authorized = relationship in {
            RelationshipState.OUTBOUND_TRUSTED.value,
            RelationshipState.BIDIRECTIONAL.value,
        }
        inbound_authorized = relationship in {
            RelationshipState.INBOUND_GRANTED.value,
            RelationshipState.BIDIRECTIONAL.value,
        }
        can_pair = (
            enabled
            and payload is not None
            and payload.get("discovered") is True
            and relationship == RelationshipState.UNPAIRED.value
            and has_fingerprints
        )
        self.pair_button.setEnabled(can_pair)
        self.pair_button.setProperty("role", "primary" if can_pair else "neutral")
        self.pair_button.style().unpolish(self.pair_button)
        self.pair_button.style().polish(self.pair_button)
        self.invite_button.setEnabled(enabled and payload is not None)
        self.repair_button.setEnabled(
            enabled
            and relationship == RelationshipState.REPAIR_REQUIRED.value
            and has_fingerprints
        )
        self.connect_button.setEnabled(
            enabled and outbound_authorized and not connected
        )
        self.reconnect_button.setEnabled(enabled and outbound_authorized and connected)
        self.disconnect_button.setEnabled(enabled and connected)
        self.revoke_peer_button.setEnabled(
            enabled
            and relationship
            in {
                RelationshipState.OUTBOUND_TRUSTED.value,
                RelationshipState.INBOUND_GRANTED.value,
                RelationshipState.BIDIRECTIONAL.value,
                RelationshipState.REPAIR_REQUIRED.value,
            }
        )
        self.rotate_transport_button.setEnabled(enabled)
        self.join_button.setEnabled(enabled)
        self.cluster_role_selector.setEnabled(enabled)
        self.assign_role_button.setEnabled(False)
        self.revoke_member_button.setEnabled(False)
        self.pause_worker_button.setEnabled(False)
        self.resume_worker_button.setEnabled(False)
        self.revoke_worker_button.setEnabled(False)
        self.remove_connection_button.setEnabled(False)
        self.remove_job_button.setEnabled(False)
        self.renew_cluster_lease_button.setEnabled(False)
        self.promote_if_due_button.setEnabled(False)
        self.rejoin_as_worker_button.setEnabled(False)
        self.cluster_share_button.setEnabled(False)
        self.stop_button.setEnabled(enabled)
        can_remote_read = enabled and outbound_authorized and connected
        for button in (
            self.remote_hello_button,
            self.remote_node_button,
            self.remote_dashboard_button,
            self.remote_component_button,
            self.remote_processes_button,
            self.remote_storage_button,
            self.remote_request_shared_button,
            self.remote_revoke_self_button,
        ):
            button.setEnabled(can_remote_read)
        process_item = self.remote_process_list.currentItem()
        process = (
            process_item.data(Qt.ItemDataRole.UserRole)
            if process_item is not None
            else None
        )
        can_act_on_process = (
            can_remote_read
            and isinstance(process, dict)
            and process.get("action_allowed") is True
            and isinstance(process.get("pid"), int)
            and not isinstance(process.get("pid"), bool)
            and isinstance(process.get("create_time"), float)
        )
        self.remote_terminate_button.setEnabled(can_act_on_process)
        self.remote_force_terminate_button.setEnabled(can_act_on_process)
        self.allow_demo_button.setEnabled(enabled and inbound_authorized)
        self.revoke_demo_button.setEnabled(enabled and payload is not None)
        has_surface_policy = (
            self._surface_policy is not None and not self._surface_policy_unavailable
        )
        has_surface_mode = self._selected_surface_access() is not None
        self.remote_grant_surface_button.setEnabled(
            enabled and inbound_authorized and has_surface_policy and has_surface_mode
        )
        self.remote_revoke_surface_button.setEnabled(
            enabled and payload is not None and has_surface_policy and has_surface_mode
        )
        selected_surface_id = self.surface_selector.currentData()
        definitions = getattr(self.adapter, "surface_definitions", {})
        selected_surface = (
            definitions.get(selected_surface_id)
            if isinstance(selected_surface_id, str) and isinstance(definitions, Mapping)
            else None
        )
        self.remote_read_surface_button.setEnabled(
            can_remote_read and selected_surface is not None
        )
        self.remote_review_surface_button.setEnabled(
            can_remote_read
            and selected_surface is not None
            and callable(getattr(selected_surface, "review", None))
        )
        self._update_selected_peer_details()
        self._update_surface_approval_state()
        self._update_cluster_action_availability()

    def _selected_peer_payload(self) -> dict[str, Any] | None:
        item = self.peer_list.currentItem()
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return payload if isinstance(payload, dict) else None

    def _update_action_availability(self, *_args: object) -> None:
        self._set_action_buttons_enabled(
            not self._operation_active and self.adapter.started
        )
        self._update_remote_availability()

    def _update_remote_availability(self) -> None:
        payload = self._selected_peer_payload()
        if payload is None:
            self.remote_status_label.setText(
                "Select a peer to use remote capabilities."
            )
            return
        relationship = payload.get("relationship")
        if relationship not in {
            RelationshipState.OUTBOUND_TRUSTED.value,
            RelationshipState.BIDIRECTIONAL.value,
        }:
            self.remote_status_label.setText(
                "Remote reads require outbound trust; inbound sharing is separate."
            )
            return
        if payload.get("connected") is not True:
            self.remote_status_label.setText(
                "Peer is authorized for outbound requests but not connected."
            )
            return
        self.remote_status_label.setText(
            "Connected peer: authenticated reads and shared requests are available."
        )

    def _relationship_value(self, peer_id: str) -> str:
        try:
            relationship = self.adapter.peer_relationship(NodeId(peer_id))
        except Exception:  # noqa: BLE001 -- Do not expose package details in UI.
            return "unknown"
        value = getattr(relationship, "value", None)
        allowed = {state.value for state in RelationshipState}
        return value if isinstance(value, str) and value in allowed else "unknown"

    @staticmethod
    def _membership_value(record: object | None) -> str:
        membership = getattr(record, "membership", None)
        value = getattr(membership, "value", None)
        if isinstance(value, str) and value in {
            "not_joined",
            "worker",
            "coordinator",
            "subcoordinator",
        }:
            return value
        return "unknown"

    @staticmethod
    def _project_cluster_status(status: object) -> _ClusterProjection | None:
        """Validate and copy only the public cluster fields shown by the UI."""

        if not isinstance(status, dict):
            return None
        enabled = status.get("enabled", True)
        if type(enabled) is not bool:
            return None
        if not enabled:
            members = status.get("members", [])
            if not isinstance(members, list) or members:
                return None
            return _ClusterProjection(False, "", None, None, ())
        cluster_id = status.get("cluster_id")
        coordinator_id = status.get("coordinator_id")
        epoch = status.get("epoch")
        members = status.get("members")
        if (
            not isinstance(cluster_id, str)
            or (coordinator_id is not None and not isinstance(coordinator_id, str))
            or (epoch is not None and (type(epoch) is not int or epoch < 0))
            or not isinstance(members, list)
            or (
                not cluster_id
                and (coordinator_id is not None or epoch is not None or members)
            )
        ):
            return None

        allowed_roles = {"worker", "coordinator", "subcoordinator"}
        projected_members: list[_ClusterMemberProjection] = []
        for member in members:
            if not isinstance(member, dict):
                return None
            node_id = member.get("node_id")
            roles = member.get("roles")
            paused = member.get("paused")
            revoked = member.get("revoked")
            if (
                not isinstance(node_id, str)
                or not node_id
                or not isinstance(roles, list)
                or any(
                    not isinstance(role, str) or role not in allowed_roles
                    for role in roles
                )
                or type(paused) is not bool
                or type(revoked) is not bool
            ):
                return None
            projected_members.append(
                _ClusterMemberProjection(node_id, tuple(roles), paused, revoked)
            )
        return _ClusterProjection(
            True, cluster_id, coordinator_id, epoch, tuple(projected_members)
        )

    def _update_selected_peer_details(self) -> None:
        payload = self._selected_peer_payload()
        if payload is None:
            self.peer_detail_label.setText("Select a peer to view its state.")
            self.invite_target_label.setText(
                "Invite target: select a peer in Runtime and peers."
            )
            return
        peer_id = payload.get("node_id")
        if isinstance(peer_id, str):
            self.invite_target_label.setText(f"Invite target: {peer_id}")
        relationship = payload.get("relationship", "unknown")
        connection = "online" if payload.get("connected") is True else "offline"
        membership = payload.get("membership", "unknown")
        continuity = "unknown"
        pending_text = "none"
        if isinstance(peer_id, str):
            try:
                diagnostics = self.adapter.pairing_diagnostics(NodeId(peer_id))
            except Exception:  # noqa: BLE001 -- Diagnostics are best effort.
                diagnostics = None
            if isinstance(diagnostics, dict):
                identity = diagnostics.get("identity")
                if isinstance(identity, dict):
                    candidate = identity.get("transport_continuity")
                    if isinstance(candidate, str) and candidate in {
                        "no_stored_trust",
                        "no_candidate",
                        "same_generation",
                        "rotation_proven",
                        "unproven",
                    }:
                        continuity = candidate
                pending = diagnostics.get("pending")
                if isinstance(pending, dict):
                    intent = pending.get("intent")
                    direction = pending.get("direction")
                    count = pending.get("count")
                    if (
                        intent in {"pair", "repair", "elevation"}
                        and direction in {"inbound", "outbound"}
                        and isinstance(count, int)
                        and not isinstance(count, bool)
                        and count > 0
                    ):
                        pending_text = f"{direction} {intent} ({count})"
        self.peer_detail_label.setText(
            f"Trust relationship: {relationship} · Connection: {connection} · "
            f"Cluster membership: {membership} · Transport continuity: {continuity} · "
            f"Pending pairing: {pending_text}"
        )

    def closeEvent(self, event: Any) -> None:
        if self._operation_active:
            self.status_label.setText(
                "Wait for the Connect operation to finish before closing"
            )
            event.ignore()
            return
        self._shutdown_runtime()
        self._delivery.close()
        event.accept()
