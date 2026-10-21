from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from maintenance.connect_grants import ConnectSurfaceGrantPolicy


class MainConnectPolicyTests(unittest.TestCase):
    def test_policy_path_is_outside_connect_profile_and_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_dir = root / "connect" / "profile"
            legacy_state = root / "legacy-cluster.json"
            adapter = Mock()
            with (
                patch("main.default_connect_profile_dir", return_value=profile_dir),
                patch("main.register_process_action_surface"),
                patch("main.register_demo_shared_capability"),
                patch("main.register_cluster_data_surfaces"),
            ):
                policy = main._register_connect_surfaces(adapter)

            self.assertIsInstance(policy, ConnectSurfaceGrantPolicy)
            self.assertEqual(
                policy.store.path, root / "connect" / "surface-approvals.json"
            )
            self.assertFalse(
                policy.store.path == profile_dir
                or profile_dir in policy.store.path.parents
                or policy.store.path == legacy_state
                or legacy_state in policy.store.path.parents
            )


if __name__ == "__main__":
    unittest.main()
