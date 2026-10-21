from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock

from expra_connect import NodeId as ConnectNodeId

from maintenance import connect_provider, connect_surfaces
from maintenance.components.cluster_storage import CoordinatorTimeline, StandbyBuffer
from maintenance.connect_data_surfaces import (
    CLUSTER_SNAPSHOT_SURFACE_ID,
    register_cluster_data_surfaces,
)
from maintenance.connect_provider import (
    PROCESS_FORCE_TERMINATION_SURFACE_ID,
    PROCESS_TERMINATION_SURFACE_ID,
)
from maintenance.connect_surfaces import (
    register_demo_shared_capability,
    register_process_action_surface,
)
from maintenance.models import ProcessActionResult


class ConnectProcessSurfaceTests(unittest.TestCase):
    def test_registers_parameter_free_demo_capability(self) -> None:
        runtime = Mock()

        register_demo_shared_capability(cast(Any, runtime))

        runtime.register_shared_capability.assert_called_once()
        capability, handler = runtime.register_shared_capability.call_args.args
        self.assertEqual(capability, "exp.health-summary")
        self.assertEqual(
            handler(ConnectNodeId("requester"), {}),
            {"capability": "exp.health-summary", "available": True},
        )
        with self.assertRaises(ValueError):
            handler(ConnectNodeId("requester"), {"unexpected": True})

    def test_process_action_batch_limit_has_one_shared_owner(self) -> None:
        self.assertEqual(
            getattr(connect_provider, "PROCESS_ACTION_BATCH_LIMIT", None), 256
        )
        self.assertEqual(
            getattr(connect_surfaces, "PROCESS_ACTION_BATCH_LIMIT", None),
            connect_provider.PROCESS_ACTION_BATCH_LIMIT,
        )

    def make_runtime(self) -> tuple[Mock, dict[str, Any]]:
        runtime = Mock()
        registrations: dict[str, dict[str, Any]] = {}

        def register_surface(
            surface_id: str,
            *,
            read: Any,
            actions: Any,
        ) -> object:
            definition = object()
            registration = {
                "surface_id": surface_id,
                "read": read,
                "actions": dict(actions),
                "definition": definition,
            }
            registrations[surface_id] = registration
            return definition

        runtime.register_surface.side_effect = register_surface
        return runtime, registrations

    def test_cluster_data_surfaces_validate_authenticated_sender_and_epoch(
        self,
    ) -> None:
        runtime = Mock()
        runtime.cluster_status.return_value = {
            "cluster_id": "cluster-1",
            "epoch": 4,
            "members": [
                {"node_id": "coordinator", "roles": ["coordinator"]},
                {"node_id": "worker", "roles": ["worker"]},
            ],
        }
        runtime.runtime.identity.node_id = ConnectNodeId("coordinator")
        registrations: dict[str, dict[str, Any]] = {}

        def register_surface(surface_id: str, *, read: Any, actions: Any) -> object:
            definition = object()
            registrations[surface_id] = {"actions": dict(actions)}
            return definition

        runtime.register_surface.side_effect = register_surface
        with TemporaryDirectory() as directory:
            timeline = CoordinatorTimeline(Path(directory) / "history.sqlite3")
            standby = StandbyBuffer(Path(directory) / "standby.sqlite3")
            register_cluster_data_surfaces(
                cast(Any, runtime), timeline=timeline, standby=standby
            )
            payload = {
                "batch_id": "worker:4:1",
                "source_node_id": "worker",
                "source_epoch": 4,
                "sequence": 1,
                "observed_at": 100.0,
                "encoded_size": 1,
                "cluster_id": "cluster-1",
                "payload": [],
            }

            result = registrations[CLUSTER_SNAPSHOT_SURFACE_ID]["actions"][
                "append_snapshot"
            ](ConnectNodeId("worker"), {"batch": payload})

            self.assertEqual(result, {"accepted": True})
            self.assertEqual(len(timeline.batches()), 1)
            self.assertEqual(timeline.batches()[0].source_node_id.value, "worker")
            self.assertEqual(standby.batches(), ())

    def test_cluster_data_surface_rejects_payload_claiming_another_sender(self) -> None:
        runtime = Mock()
        runtime.cluster_status.return_value = {
            "cluster_id": "cluster-1",
            "epoch": 4,
            "members": [
                {"node_id": "coordinator", "roles": ["coordinator"]},
                {"node_id": "worker", "roles": ["worker"]},
            ],
        }
        runtime.runtime.identity.node_id = ConnectNodeId("coordinator")
        registrations: dict[str, dict[str, Any]] = {}
        runtime.register_surface.side_effect = lambda surface_id, **kwargs: (
            registrations.update({surface_id: {"actions": dict(kwargs["actions"])}})
            or object()
        )
        timeline = Mock()

        register_cluster_data_surfaces(
            cast(Any, runtime), timeline=timeline, standby=Mock()
        )
        payload = {
            "batch_id": "other:4:1",
            "source_node_id": "other",
            "source_epoch": 4,
            "sequence": 1,
            "observed_at": 100.0,
            "encoded_size": 1,
            "cluster_id": "cluster-1",
            "payload": [],
        }

        with self.assertRaisesRegex(PermissionError, "sender"):
            registrations[CLUSTER_SNAPSHOT_SURFACE_ID]["actions"]["append_snapshot"](
                ConnectNodeId("worker"), {"batch": payload}
            )

        timeline.append.assert_not_called()

    def test_registers_action_surface_and_delegates_to_safe_host_manager(self) -> None:
        runtime, registrations = self.make_runtime()
        manager = Mock()
        result = ProcessActionResult(1, (41,), (), ())
        manager.request_quit.return_value = result

        definitions = register_process_action_surface(
            cast(Any, runtime), cast(Any, manager)
        )
        termination = registrations[PROCESS_TERMINATION_SURFACE_ID]
        force_termination = registrations[PROCESS_FORCE_TERMINATION_SURFACE_ID]
        self.assertEqual(
            definitions,
            (termination["definition"], force_termination["definition"]),
        )
        self.assertEqual(termination["read"](ConnectNodeId("peer"), {}), {})
        response = termination["actions"]["request_quit"](
            ConnectNodeId("peer"),
            {"pids": [41], "expected_create_times": {"41": 12.5}},
        )

        self.assertEqual(
            response,
            {"requested": 1, "stopped": [41], "force_required": [], "errors": []},
        )
        manager.request_quit.assert_called_once_with([41], {41: 12.5})

    def test_rejects_malformed_or_duplicate_process_ids_before_action(self) -> None:
        runtime, registrations = self.make_runtime()
        manager = Mock()
        register_process_action_surface(cast(Any, runtime), cast(Any, manager))
        handler = registrations[PROCESS_TERMINATION_SURFACE_ID]["actions"][
            "request_quit"
        ]

        for params in (
            {"pids": [True], "expected_create_times": {}},
            {"pids": [3, 3], "expected_create_times": {}},
            {"pids": [3], "expected_create_times": {"4": 1.0}},
            {"pids": [3], "expected_create_times": {}},
            {"pids": [3, 4], "expected_create_times": {"3": 1.0}},
            {"pids": [3], "expected_create_times": {"3": float("nan")}},
            {"pids": [3], "unexpected": True},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                handler(ConnectNodeId("peer"), params)

        manager.request_quit.assert_not_called()

    def test_process_action_batch_bound_is_preserved_at_surface(self) -> None:
        runtime, registrations = self.make_runtime()
        manager = Mock()
        manager.request_quit.return_value = ProcessActionResult(256, (), (), ())
        register_process_action_surface(cast(Any, runtime), cast(Any, manager))
        handler = registrations[PROCESS_TERMINATION_SURFACE_ID]["actions"][
            "request_quit"
        ]
        pids = list(range(1, 257))

        handler(
            ConnectNodeId("peer"),
            {
                "pids": pids,
                "expected_create_times": {str(pid): float(pid) for pid in pids},
            },
        )
        self.assertEqual(manager.request_quit.call_count, 1)

        with self.assertRaisesRegex(ValueError, "PIDs"):
            handler(
                ConnectNodeId("peer"),
                {
                    "pids": [*pids, 257],
                    "expected_create_times": {
                        str(pid): float(pid) for pid in (*pids, 257)
                    },
                },
            )
        self.assertEqual(manager.request_quit.call_count, 1)

    def test_force_action_uses_existing_process_manager_safety_path(self) -> None:
        runtime, registrations = self.make_runtime()
        manager = Mock()
        manager.force_quit.return_value = ProcessActionResult(1, (), (9,), ())
        register_process_action_surface(cast(Any, runtime), cast(Any, manager))

        response = registrations[PROCESS_FORCE_TERMINATION_SURFACE_ID]["actions"][
            "force_quit"
        ](
            ConnectNodeId("peer"),
            {"pids": [9], "expected_create_times": {"9": 9.0}},
        )

        self.assertEqual(response["force_required"], [9])
        manager.force_quit.assert_called_once_with([9], {9: 9.0})


if __name__ == "__main__":
    unittest.main()
