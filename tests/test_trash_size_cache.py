"""Focused tests for the TTL-cached trash-size walk and its shared engine."""

import unittest
from typing import Any
from unittest.mock import Mock

from maintenance.scanner import SystemScanner
from tests.support.scanner import make_scanner, scanner_environment


class TrashSizeCacheTests(unittest.TestCase):
    def _scanner(self, trash_walk: Any) -> SystemScanner:
        scanner = make_scanner()
        self._walk = Mock(side_effect=trash_walk)
        return scanner

    def _storage_scan(self, scanner: SystemScanner) -> None:
        with scanner_environment(
            scanner,
            None,
            trash_size=self._walk,
            swap_devices=[],
        ):
            scanner.scan_component("storage")

    def _dashboard_scan(self, scanner: SystemScanner) -> None:
        with scanner_environment(scanner, None, trash_size=self._walk):
            scanner.scan_dashboard()

    def test_trash_walk_runs_once_within_ttl(self) -> None:
        scanner = self._scanner(lambda: 4096)

        self._storage_scan(scanner)
        self._storage_scan(scanner)

        self.assertEqual(self._walk.call_count, 1)

    def test_ttl_default_is_sixty_seconds(self) -> None:
        self.assertEqual(SystemScanner.TRASH_SIZE_REFRESH_SECONDS, 60.0)

    def test_cleared_cache_re_walks_on_next_component_scan(self) -> None:
        scanner = self._scanner(lambda: 4096)

        self._storage_scan(scanner)
        scanner._trash_size_cache = None
        self._storage_scan(scanner)

        self.assertEqual(self._walk.call_count, 2)

    def test_full_dashboard_scan_forces_a_fresh_walk(self) -> None:
        scanner = self._scanner(lambda: 4096)

        self._storage_scan(scanner)
        self._dashboard_scan(scanner)

        self.assertEqual(self._walk.call_count, 2)

    def test_fresh_value_after_cleanup_flow(self) -> None:
        values = iter((4096, 8192))
        scanner = self._scanner(lambda: next(values))

        self._storage_scan(scanner)
        self._dashboard_scan(scanner)

        self.assertEqual(self._walk.call_count, 2)
        cached = scanner._trash_size_cache
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached[1], 8192)

    def test_failed_walk_is_not_cached_and_retries(self) -> None:
        attempts = {"count": 0}

        def failing_walk() -> int:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError("denied")
            return 2048

        scanner = self._scanner(failing_walk)

        self._storage_scan(scanner)
        self.assertEqual(self._walk.call_count, 1)
        self.assertIsNone(scanner._trash_size_cache)

        self._storage_scan(scanner)
        self.assertEqual(self._walk.call_count, 2)

    def test_component_scan_serves_cached_value(self) -> None:
        scanner = self._scanner(lambda: 4096)

        self._storage_scan(scanner)
        self._storage_scan(scanner)

        cached = scanner._trash_size_cache
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached[1], 4096)


class TtlCacheEngineTests(unittest.TestCase):
    def _scanner(self) -> SystemScanner:
        return make_scanner()

    def test_engine_reuses_value_within_ttl(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> int:
            calls.append("load")
            return 42

        for _attempt in range(2):
            value = scanner._ttl_cached_value(
                lock=scanner._trash_size_cache_lock,
                cache_name="_trash_size_cache",
                ttl_seconds=60.0,
                loader=loader,
            )
            self.assertEqual(value, 42)

        self.assertEqual(calls, ["load"])

    def test_engine_reloads_after_ttl_expiry(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> int:
            calls.append("load")
            return 7

        scanner._ttl_cached_value(
            lock=scanner._trash_size_cache_lock,
            cache_name="_trash_size_cache",
            ttl_seconds=60.0,
            loader=loader,
        )
        cached = scanner._trash_size_cache
        self.assertIsNotNone(cached)
        assert cached is not None
        scanner._trash_size_cache = (cached[0] - 120.0, 7)
        scanner._ttl_cached_value(
            lock=scanner._trash_size_cache_lock,
            cache_name="_trash_size_cache",
            ttl_seconds=60.0,
            loader=loader,
        )

        self.assertEqual(calls, ["load", "load"])

    def test_engine_failed_loader_is_never_cached(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> int:
            calls.append("load")
            if len(calls) == 1:
                raise RuntimeError("walk failed")
            return 9

        with self.assertRaisesRegex(RuntimeError, "walk failed"):
            scanner._ttl_cached_value(
                lock=scanner._trash_size_cache_lock,
                cache_name="_trash_size_cache",
                ttl_seconds=60.0,
                loader=loader,
            )

        self.assertIsNone(scanner._trash_size_cache)
        self.assertEqual(
            scanner._ttl_cached_value(
                lock=scanner._trash_size_cache_lock,
                cache_name="_trash_size_cache",
                ttl_seconds=60.0,
                loader=loader,
            ),
            9,
        )
        self.assertEqual(calls, ["load", "load"])

    def test_engine_keeps_independent_slots_isolated(self) -> None:
        scanner = self._scanner()
        trash_calls: list[str] = []
        temp_calls: list[str] = []

        def trash_loader() -> int:
            trash_calls.append("load")
            return 1

        def temp_loader() -> list[str]:
            temp_calls.append("load")
            return ["CPU: 45°C"]

        scanner._ttl_cached_value(
            lock=scanner._trash_size_cache_lock,
            cache_name="_trash_size_cache",
            ttl_seconds=60.0,
            loader=trash_loader,
        )
        scanner._ttl_cached_value(
            lock=scanner._temperature_cache_lock,
            cache_name="_temperature_cache",
            ttl_seconds=5.0,
            loader=temp_loader,
        )
        scanner._ttl_cached_value(
            lock=scanner._trash_size_cache_lock,
            cache_name="_trash_size_cache",
            ttl_seconds=60.0,
            loader=trash_loader,
        )
        scanner._ttl_cached_value(
            lock=scanner._temperature_cache_lock,
            cache_name="_temperature_cache",
            ttl_seconds=5.0,
            loader=temp_loader,
        )

        self.assertEqual(trash_calls, ["load"])
        self.assertEqual(temp_calls, ["load"])


if __name__ == "__main__":
    unittest.main()
