import unittest

from maintenance.cluster import resource_summary_from_dict, resource_summary_to_dict
from maintenance.components import ResourceFeatureCatalog
from maintenance.models import (
    CapabilityState,
    capability_label,
    resource_status,
)
from tests.support.models import make_summary


class CapabilityTransparencyTests(unittest.TestCase):
    def test_catalog_declares_platform_specific_metric_states(self) -> None:
        catalog = ResourceFeatureCatalog()

        gpu_usage = next(
            item
            for item in catalog.CAPABILITY_DECLARATIONS
            if item.component == "gpu" and item.metric == "utilization"
        )

        self.assertEqual(
            gpu_usage.state_for("Linux"), CapabilityState.TEMPORARILY_UNAVAILABLE
        )
        self.assertEqual(gpu_usage.state_for("Darwin"), CapabilityState.UNSUPPORTED)
        self.assertEqual(
            gpu_usage.state_for("Windows"), CapabilityState.TEMPORARILY_UNAVAILABLE
        )
        self.assertEqual(
            gpu_usage.state_for("Plan 9"),
            CapabilityState.NOT_VERIFIED_ON_NATIVE_PLATFORM,
        )
        self.assertIn(gpu_usage, catalog.declarations_for("Linux"))
        self.assertNotIn(gpu_usage, catalog.declarations_for("Plan 9"))

    def test_status_labels_keep_failure_and_capability_distinct(self) -> None:
        for state, expected in (
            (CapabilityState.UNSUPPORTED, "Unsupported"),
            (CapabilityState.TEMPORARILY_UNAVAILABLE, "Temporarily unavailable"),
            (CapabilityState.NO_DATA, "No data yet"),
            (CapabilityState.PERMISSION_LIMITED, "Permission required"),
            (
                CapabilityState.NOT_VERIFIED_ON_NATIVE_PLATFORM,
                "Not verified on this platform",
            ),
        ):
            summary = make_summary(
                "gpu",
                "GPU",
                value="Unavailable",
                subtitle="Information unavailable",
                percent=None,
                capability=state,
            )
            self.assertEqual(resource_status(summary), expected)
            self.assertEqual(capability_label(state), expected)

        failed = make_summary(
            "gpu",
            "GPU",
            value="Unavailable",
            subtitle="Information unavailable",
            percent=None,
            failed=True,
            capability=CapabilityState.PERMISSION_LIMITED,
        )
        self.assertEqual(resource_status(failed), "Permission required")

        worker_failure = make_summary(
            "gpu",
            "GPU",
            value="Unavailable",
            subtitle="Information unavailable",
            percent=None,
            failed=True,
        )
        self.assertEqual(resource_status(worker_failure), "Failed")

    def test_new_states_are_additive_for_v1_remote_decoders(self) -> None:
        summary = make_summary(
            "gpu",
            "GPU",
            value="Unavailable",
            subtitle="Information unavailable",
            percent=None,
            capability=CapabilityState.TEMPORARILY_UNAVAILABLE,
        )

        encoded = resource_summary_to_dict(summary)
        self.assertEqual(encoded["capability"], CapabilityState.UNKNOWN.value)
        self.assertEqual(
            encoded["capability_state"],
            CapabilityState.TEMPORARILY_UNAVAILABLE.value,
        )
        self.assertEqual(
            resource_summary_from_dict(encoded).capability,
            CapabilityState.TEMPORARILY_UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()
