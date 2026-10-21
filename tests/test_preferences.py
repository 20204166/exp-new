"""Focused tests for the central preferences model and its JSON store."""

import json
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from maintenance.preferences import (
    INTERVAL_POLICIES,
    AppPreferences,
    PreferencesSaveError,
    PreferencesStore,
    RefreshIntervals,
    default_preferences_path,
    validate_interval_ms,
)


class AppPreferencesModelTests(unittest.TestCase):
    def test_defaults_derive_from_refresh_intervals(self) -> None:
        defaults = AppPreferences.defaults()

        self.assertEqual(
            defaults.refresh_intervals.as_dict(),
            RefreshIntervals().as_dict(),
        )
        self.assertEqual(
            defaults.visible_cards,
            frozenset(RefreshIntervals().as_dict()),
        )
        self.assertFalse(defaults.hide_unavailable_cards)

    def test_defaults_cover_every_catalog_card(self) -> None:
        from maintenance.components.catalog import ResourceFeatureCatalog

        defaults = AppPreferences.defaults()
        catalog_keys = {feature.key for feature in ResourceFeatureCatalog().all()}
        self.assertEqual(defaults.visible_cards, catalog_keys)

    def test_with_interval_changes_only_named_component(self) -> None:
        preferences = AppPreferences.defaults()
        updated = preferences.with_interval("cpu", 5000)

        self.assertEqual(updated.refresh_intervals.cpu, 5000)
        self.assertEqual(updated.refresh_intervals.network, 1000)
        self.assertEqual(updated.refresh_intervals.memory, 5000)

    def test_with_interval_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            AppPreferences.defaults().with_interval("nope", 1000)

    def test_with_interval_rejects_out_of_range_and_step_mismatch(self) -> None:
        defaults = AppPreferences.defaults()
        with self.assertRaises(ValueError):
            defaults.with_interval("cpu", 0)
        with self.assertRaises(ValueError):
            defaults.with_interval("cpu", 999)
        with self.assertRaises(ValueError):
            defaults.with_interval("storage", 30001)

    def test_with_card_visibility_toggles_one_card(self) -> None:
        preferences = AppPreferences.defaults()
        hidden = preferences.with_card_visibility("battery", False)
        self.assertNotIn("battery", hidden.visible_cards)
        restored = hidden.with_card_visibility("battery", True)
        self.assertIn("battery", restored.visible_cards)

    def test_with_card_visibility_rejects_unknown_key(self) -> None:
        with self.assertRaises(ValueError):
            AppPreferences.defaults().with_card_visibility("nope", True)

    def test_with_hide_unavailable_cards_changes_flag(self) -> None:
        preferences = AppPreferences.defaults()
        self.assertTrue(
            preferences.with_hide_unavailable_cards(True).hide_unavailable_cards
        )

    def test_interval_policies_cover_every_component(self) -> None:
        self.assertEqual(
            set(INTERVAL_POLICIES),
            set(RefreshIntervals().as_dict()),
        )

    def test_validate_interval_rejects_booleans_and_negatives(self) -> None:
        self.assertFalse(validate_interval_ms("cpu", True))
        self.assertFalse(validate_interval_ms("cpu", False))
        self.assertFalse(validate_interval_ms("cpu", -1000))
        self.assertTrue(validate_interval_ms("cpu", 1000))


class PreferencesStoreRoundTripTests(unittest.TestCase):
    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            source = AppPreferences.defaults().with_interval("cpu", 5000)
            source = source.with_card_visibility("gpu", False)

            store.save(source)
            loaded = store.load()

            self.assertEqual(loaded, source)

    def test_load_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PreferencesStore(Path(directory) / "nope.json")
            self.assertEqual(store.load(), AppPreferences.defaults())

    def test_load_invalid_utf8_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_bytes(b"\xff")
            self.assertEqual(PreferencesStore(path).load(), AppPreferences.defaults())

    def test_load_malformed_payloads_return_defaults(self) -> None:
        valid = RefreshIntervals().as_dict()
        cases: list[tuple[str, object]] = [
            ("empty file", ""),
            ("malformed json", "{not json"),
            ("non-object root", "[1, 2]"),
            (
                "unsupported schema",
                {
                    "schema_version": 99,
                    "refresh_intervals_ms": valid,
                    "visible_cards": list(valid),
                    "hide_unavailable_cards": False,
                },
            ),
            (
                "invalid interval",
                {
                    "schema_version": 1,
                    "refresh_intervals_ms": {**valid, "cpu": 1},
                    "visible_cards": list(valid),
                    "hide_unavailable_cards": False,
                },
            ),
            (
                "unknown interval key",
                {
                    "schema_version": 1,
                    "refresh_intervals_ms": {**valid, "ram": 1000},
                    "visible_cards": list(valid),
                    "hide_unavailable_cards": False,
                },
            ),
            (
                "unknown card",
                {
                    "schema_version": 1,
                    "refresh_intervals_ms": valid,
                    "visible_cards": ["cpu", "wat"],
                    "hide_unavailable_cards": False,
                },
            ),
            (
                "boolean as int",
                {
                    "schema_version": 1,
                    "refresh_intervals_ms": valid,
                    "visible_cards": list(valid),
                    "hide_unavailable_cards": 1,
                },
            ),
        ]
        for name, payload in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "preferences.json"
                if isinstance(payload, str):
                    path.write_text(payload)
                else:
                    path.write_text(json.dumps(payload))
                self.assertEqual(
                    PreferencesStore(path).load(),
                    AppPreferences.defaults(),
                )

    def test_load_ignores_unknown_future_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            payload = {
                "schema_version": 1,
                "refresh_intervals_ms": RefreshIntervals().as_dict(),
                "visible_cards": list(RefreshIntervals().as_dict()),
                "hide_unavailable_cards": True,
                "future_field": {"anything": [1, 2]},
            }
            path.write_text(json.dumps(payload))
            loaded = PreferencesStore(path).load()
            self.assertTrue(loaded.hide_unavailable_cards)

    def test_load_honours_valid_custom_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            payload = {
                "schema_version": 1,
                "refresh_intervals_ms": {**RefreshIntervals().as_dict(), "gpu": 10000},
                "visible_cards": ["cpu", "memory", "storage", "network", "battery"],
                "hide_unavailable_cards": True,
            }
            path.write_text(json.dumps(payload))
            loaded = PreferencesStore(path).load()
            self.assertEqual(loaded.refresh_intervals.gpu, 10000)
            self.assertNotIn("gpu", loaded.visible_cards)
            self.assertTrue(loaded.hide_unavailable_cards)

    def test_reset_to_defaults_writes_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            store.save(AppPreferences.defaults().with_interval("cpu", 5000))

            result = store.reset_to_defaults()

            self.assertEqual(result, AppPreferences.defaults())
            self.assertEqual(store.load(), AppPreferences.defaults())


class PreferencesStoreFailureTests(unittest.TestCase):
    def test_save_failure_leaves_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "config"
            blocker.write_text("not a directory")
            store = PreferencesStore(blocker / "preferences.json")

            with self.assertRaises(PreferencesSaveError):
                store.save(AppPreferences.defaults())

            self.assertEqual(list(Path(directory).glob(".preferences-*")), [])

    def test_save_to_unwritable_parent_raises_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "config"
            blocker.write_text("not a directory")
            store = PreferencesStore(blocker / "preferences.json")

            with self.assertRaises(PreferencesSaveError):
                store.save(AppPreferences.defaults())

            self.assertEqual(list(Path(directory).glob(".preferences-*")), [])

    def test_previous_file_survives_commit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            store.save(AppPreferences.defaults())

            with (
                patch(
                    "maintenance.preferences.os.replace",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(PreferencesSaveError),
            ):
                store.save(AppPreferences.defaults().with_interval("cpu", 5000))

            self.assertEqual(store.load(), AppPreferences.defaults())


class DefaultPreferencesPathTests(unittest.TestCase):
    def test_linux_uses_xdg_config_home(self) -> None:
        path = default_preferences_path(
            environment={"XDG_CONFIG_HOME": "/xdg"},
            home=Path("/home/alice"),
            platform_name="Linux",
        )
        self.assertEqual(
            path,
            Path("/xdg/system-analyzer/preferences.json"),
        )

    def test_linux_falls_back_to_home_config(self) -> None:
        path = default_preferences_path(
            environment={},
            home=Path("/home/alice"),
            platform_name="Linux",
        )
        self.assertEqual(
            path,
            Path("/home/alice/.config/system-analyzer/preferences.json"),
        )

    def test_macos_uses_application_support(self) -> None:
        path = default_preferences_path(
            environment={},
            home=Path("/Users/alice"),
            platform_name="Darwin",
        )
        self.assertEqual(
            path,
            Path(
                "/Users/alice/Library/Application Support/system-analyzer/preferences.json"
            ),
        )

    def test_windows_uses_appdata(self) -> None:
        path = default_preferences_path(
            environment={"APPDATA": r"C:\Users\Alice\AppData\Roaming"},
            home=Path("C:/Users/Alice"),
            platform_name="Windows",
        )
        self.assertEqual(
            PureWindowsPath(path),
            PureWindowsPath(
                r"C:\Users\Alice\AppData\Roaming\system-analyzer\preferences.json"
            ),
        )

    def test_windows_falls_back_to_roaming(self) -> None:
        path = default_preferences_path(
            environment={},
            home=Path("C:/Users/Alice"),
            platform_name="Windows",
        )
        self.assertEqual(
            PureWindowsPath(path),
            PureWindowsPath(
                "C:/Users/Alice/AppData/Roaming/system-analyzer/preferences.json"
            ),
        )


class AppearancePreferenceTests(unittest.TestCase):
    def test_defaults_use_the_default_appearance(self) -> None:
        self.assertEqual(AppPreferences.defaults().appearance, "indigo")

    def test_with_appearance_validates_theme(self) -> None:
        preferences = AppPreferences.defaults()
        changed = preferences.with_appearance("emerald")
        self.assertEqual(changed.appearance, "emerald")
        self.assertEqual(preferences.appearance, "indigo")
        with self.assertRaises(ValueError):
            preferences.with_appearance("not-a-theme")

    def test_appearance_persists_through_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            saved = store.load().with_appearance("rose")
            store.save(saved)
            self.assertEqual(PreferencesStore(path).load().appearance, "rose")

    def test_invalid_appearance_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            store.save(AppPreferences.defaults())
            data = json.loads(path.read_text(encoding="utf-8"))
            data["appearance"] = "bogus"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(store.load().appearance, "indigo")


class FullSystemScanPreferenceTests(unittest.TestCase):
    def test_defaults_disable_full_system_scan(self) -> None:
        self.assertFalse(AppPreferences.defaults().full_system_scan_enabled)

    def test_with_full_system_scan_enabled_toggles_flag(self) -> None:
        preferences = AppPreferences.defaults()
        enabled = preferences.with_full_system_scan_enabled(True)
        self.assertTrue(enabled.full_system_scan_enabled)
        self.assertFalse(preferences.full_system_scan_enabled)
        self.assertFalse(
            enabled.with_full_system_scan_enabled(False).full_system_scan_enabled
        )

    def test_with_full_system_scan_enabled_rejects_non_boolean(self) -> None:
        with self.assertRaises(TypeError):
            AppPreferences.defaults().with_full_system_scan_enabled("yes")  # type: ignore[arg-type]

    def test_full_system_scan_preference_persists_through_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = PreferencesStore(path)
            saved = store.load().with_full_system_scan_enabled(True)
            store.save(saved)
            self.assertTrue(PreferencesStore(path).load().full_system_scan_enabled)

    def test_load_missing_full_system_scan_field_defaults_to_disabled(self) -> None:
        """A schema-version-1 document written before this field existed."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            legacy_payload = {
                "schema_version": 1,
                "refresh_intervals_ms": RefreshIntervals().as_dict(),
                "visible_cards": list(RefreshIntervals().as_dict()),
                "hide_unavailable_cards": False,
            }
            path.write_text(json.dumps(legacy_payload))
            loaded = PreferencesStore(path).load()
            self.assertFalse(loaded.full_system_scan_enabled)

    def test_load_malformed_full_system_scan_field_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            payload = {
                "schema_version": 1,
                "refresh_intervals_ms": RefreshIntervals().as_dict(),
                "visible_cards": list(RefreshIntervals().as_dict()),
                "hide_unavailable_cards": False,
                "full_system_scan_enabled": "yes",
            }
            path.write_text(json.dumps(payload))
            self.assertEqual(
                PreferencesStore(path).load(),
                AppPreferences.defaults(),
            )


if __name__ == "__main__":
    unittest.main()
