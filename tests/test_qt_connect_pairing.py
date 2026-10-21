from __future__ import annotations

import threading
import unittest
from queue import Queue
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from maintenance.ui.qt_connect_pairing import QtPairingApprovalBridge


def pairing_request() -> SimpleNamespace:
    return SimpleNamespace(
        caller_node_id=SimpleNamespace(value="synthetic-peer"),
        identity_fingerprint="identity-fingerprint",
        transport_fingerprint="transport-fingerprint",
        proposed_secret="synthetic-secret-must-not-be-displayed",
        permissions=frozenset({"read_state"}),
        intent="pair",
    )


class QtPairingApprovalBridgeTests(unittest.TestCase):
    def test_ui_thread_approval_runs_directly(self) -> None:
        received: list[object] = []

        def approve(request: object) -> bool:
            received.append(request)
            return True

        bridge = QtPairingApprovalBridge(
            submit=lambda callback: self.fail("UI-thread approval must not queue"),
            approve=approve,
            is_ui_thread=lambda: True,
        )
        request = pairing_request()

        self.assertTrue(bridge(request))
        self.assertEqual(received, [request])
        bridge.close()

    def test_worker_approval_is_marshaled_and_returns_the_ui_decision(self) -> None:
        deliveries: Queue[Any] = Queue()
        request = pairing_request()
        results: list[bool] = []
        bridge = QtPairingApprovalBridge(
            submit=deliveries.put,
            approve=lambda received: received is request,
            is_ui_thread=lambda: False,
            timeout_seconds=1.0,
        )
        worker = threading.Thread(target=lambda: results.append(bridge(request)))
        worker.start()

        callback = deliveries.get(timeout=1.0)
        callback()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        bridge.close()

    def test_closed_bridge_denies_pending_request_and_skips_late_dialog(self) -> None:
        deliveries: Queue[Any] = Queue()
        approve = Mock(return_value=True)
        results: list[bool] = []
        bridge = QtPairingApprovalBridge(
            submit=deliveries.put,
            approve=approve,
            is_ui_thread=lambda: False,
            timeout_seconds=1.0,
        )
        worker = threading.Thread(
            target=lambda: results.append(bridge(pairing_request()))
        )
        worker.start()
        callback = deliveries.get(timeout=1.0)

        bridge.close()
        callback()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        approve.assert_not_called()

    def test_timed_out_request_cannot_be_approved_by_late_ui_callback(self) -> None:
        deliveries: Queue[Any] = Queue()
        approve = Mock(return_value=True)
        results: list[bool] = []
        bridge = QtPairingApprovalBridge(
            submit=deliveries.put,
            approve=approve,
            is_ui_thread=lambda: False,
            timeout_seconds=0.01,
        )
        worker = threading.Thread(
            target=lambda: results.append(bridge(pairing_request()))
        )
        worker.start()
        callback = deliveries.get(timeout=1.0)
        worker.join(timeout=1.0)
        callback()

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        approve.assert_not_called()
        bridge.close()

    def test_delivery_failure_denies_without_invoking_approval(self) -> None:
        approve = Mock(return_value=True)

        def fail_delivery(_callback: Any) -> None:
            raise RuntimeError("delivery is closed")

        bridge = QtPairingApprovalBridge(
            submit=fail_delivery,
            approve=approve,
            is_ui_thread=lambda: False,
            timeout_seconds=1.0,
        )

        self.assertFalse(bridge(pairing_request()))
        approve.assert_not_called()
        bridge.close()


if __name__ == "__main__":
    unittest.main()
