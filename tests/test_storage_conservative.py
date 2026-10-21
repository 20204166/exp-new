"""Focused tests for conservative and reliable Storage Cleanup behaviour."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from maintenance.actions import FileManager
from maintenance.components import DownloadScanner, ScanCancelled


def _is_effective_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


class DuplicateCorrectnessTests(unittest.TestCase):
    def _scanner(self, root: Path) -> DownloadScanner:
        scanner = DownloadScanner(root)
        scanner.DUPLICATE_MIN_BYTES = 1
        scanner.LARGE_FILE_BYTES = 1
        return scanner

    def test_same_content_different_names_marks_one_verified_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bin").write_bytes(b"identical bytes")
            (root / "b.bin").write_bytes(b"identical bytes")

            candidates = self._scanner(root).scan_downloads()

        by_path = {candidate.path: candidate for candidate in candidates}
        self.assertEqual(len(by_path), 2)
        duplicate = next(
            candidate
            for candidate in candidates
            if "Verified duplicate" in candidate.reason
        )
        self.assertIn("Verified duplicate", duplicate.reason)
        self.assertIn("Large file", duplicate.reason)

    def test_same_size_different_content_is_not_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bin").write_bytes(b"AAAA")
            (root / "b.bin").write_bytes(b"BBBB")

            candidates = self._scanner(root).scan_downloads()

        for candidate in candidates:
            self.assertNotIn("Verified duplicate", candidate.reason)

    def test_same_name_different_content_is_not_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one" / "same.bin").write_bytes(b"AAAA")
            (root / "two" / "same.bin").write_bytes(b"BBBB")

            candidates = self._scanner(root).scan_downloads()

        for candidate in candidates:
            self.assertNotIn("Verified duplicate", candidate.reason)

    def test_staged_detection_never_hashes_size_unique_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.bin").write_bytes(b"1")
            (root / "two.bin").write_bytes(b"22")
            scanner = self._scanner(root)

            with patch.object(
                DownloadScanner,
                "_cached_file_hash",
                wraps=DownloadScanner._cached_file_hash,
            ) as hash_mock:
                scanner.scan_downloads()

            hash_mock.assert_not_called()


class NonDestructiveBehaviourTests(unittest.TestCase):
    def test_scan_only_reports_candidates_and_never_touches_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.bin"
            target.write_bytes(b"x" * (2 * 1024 * 1024))
            scanner = DownloadScanner(root)
            scanner.LARGE_FILE_BYTES = 1

            candidates = scanner.scan_downloads()

            self.assertEqual(len(candidates), 1)
            self.assertTrue(target.exists())
            self.assertEqual(target.stat().st_size, 2 * 1024 * 1024)

    def test_move_to_trash_is_recoverable_and_uses_trash_api_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            target = downloads / "review.bin"
            target.write_bytes(b"content")
            moved: list[str] = []

            manager = FileManager(downloads)
            with patch("maintenance.actions.send2trash", moved.append):
                result = manager.move_to_trash([target])

            self.assertEqual(result.moved, (target.resolve(),))
            self.assertEqual(moved, [str(target.resolve())])
            self.assertTrue(target.exists())


class StorageEdgeCaseTests(unittest.TestCase):
    def test_empty_downloads_returns_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(DownloadScanner(Path(directory)).scan_downloads(), [])

    def test_symlinks_are_never_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.bin"
            target.write_bytes(b"content")
            (root / "link.bin").symlink_to(target)
            scanner = DownloadScanner(root)
            scanner.LARGE_FILE_BYTES = 1
            scanner.DUPLICATE_MIN_BYTES = 1

            candidates = scanner.scan_downloads()

            self.assertEqual([candidate.path for candidate in candidates], [target])

    @unittest.skipIf(_is_effective_root(), "permission test requires a non-root user")
    @unittest.skipIf(sys.platform == "win32", "permission bits are POSIX-only")
    def test_inaccessible_files_are_never_marked_as_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.bin"
            second = root / "b.bin"
            first.write_bytes(b"AAAA")
            second.write_bytes(b"BBBB")
            first.chmod(0o000)
            second.chmod(0o000)
            try:
                scanner = DownloadScanner(root)
                scanner.DUPLICATE_MIN_BYTES = 1
                scanner.LARGE_FILE_BYTES = 10**9
                candidates = scanner.scan_downloads()
            finally:
                first.chmod(0o644)
                second.chmod(0o644)

            for candidate in candidates:
                self.assertNotIn("Verified duplicate", candidate.reason)

    def test_explicit_scan_root_still_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.bin"
            target.write_bytes(b"content")
            (root / "link.bin").symlink_to(target)
            scanner = DownloadScanner(root / "unused-downloads")
            scanner.LARGE_FILE_BYTES = 1
            scanner.DUPLICATE_MIN_BYTES = 1

            candidates = scanner.scan_downloads(scan_root=root)

            self.assertEqual([candidate.path for candidate in candidates], [target])

    def test_cancelled_scan_raises_scan_cancelled(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bin").write_bytes(b"content")
            cancel_event = threading.Event()
            cancel_event.set()

            with self.assertRaises(ScanCancelled):
                DownloadScanner(root).scan_downloads(cancel_event=cancel_event)


class FullSystemScanCleanupBoundaryTests(unittest.TestCase):
    """The opt-in full-system scan only ever broadens what is *reviewed*.

    Cleanup stays scoped to the existing FileManager.allowed_root (Downloads
    by default): a candidate the broad scan discovers outside that root is
    review-only and must be rejected here, never moved.
    """

    def test_candidates_outside_downloads_are_review_only(self) -> None:
        with (
            tempfile.TemporaryDirectory() as downloads_dir,
            tempfile.TemporaryDirectory() as broad_dir,
        ):
            downloads = Path(downloads_dir)
            broad_root = Path(broad_dir)
            inside = downloads / "inside.bin"
            outside = broad_root / "outside.bin"
            inside.write_bytes(b"content")
            outside.write_bytes(b"content")

            scanner = DownloadScanner(downloads)
            scanner.LARGE_FILE_BYTES = 1
            candidates = scanner.scan_downloads(scan_root=broad_root)
            self.assertEqual({candidate.path for candidate in candidates}, {outside})

            manager = FileManager(downloads)
            moved: list[str] = []
            with patch("maintenance.actions.send2trash", moved.append):
                result = manager.move_to_trash(
                    [candidate.path for candidate in candidates]
                )

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])
            self.assertEqual(len(result.errors), 1)


class DuplicateExactCountTests(unittest.TestCase):
    def _scanner(self, root: Path) -> DownloadScanner:
        scanner = DownloadScanner(root)
        scanner.DUPLICATE_MIN_BYTES = 1
        scanner.LARGE_FILE_BYTES = 1
        return scanner

    def test_group_of_three_marks_exactly_two_verified_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.bin", "b.bin", "c.bin"):
                (root / name).write_bytes(b"identical bytes")

            candidates = self._scanner(root).scan_downloads()

        duplicates = [c for c in candidates if "Verified duplicate" in c.reason]
        # All-but-one stable path is kept; the other two are marked.
        self.assertEqual(len(duplicates), 2)
        kept = [c for c in candidates if c.path.name == "a.bin"]
        self.assertTrue(kept)
        self.assertNotIn("Verified duplicate", kept[0].reason)

    def test_two_independent_duplicate_groups_are_each_marked_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a1.bin").write_bytes(b"AAAA")
            (root / "a2.bin").write_bytes(b"AAAA")
            (root / "b1.bin").write_bytes(b"BBBB")
            (root / "b2.bin").write_bytes(b"BBBB")

            candidates = self._scanner(root).scan_downloads()

        duplicates = [c for c in candidates if "Verified duplicate" in c.reason]
        self.assertEqual(len(duplicates), 2)


class FileManagerSafetyTests(unittest.TestCase):
    def _manager(self, root: Path) -> FileManager:
        return FileManager(root)

    def test_direct_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            target = downloads / "real.bin"
            target.write_bytes(b"content")
            link = downloads / "link.bin"
            link.symlink_to(target)
            moved: list[str] = []

            with patch("maintenance.actions.send2trash", moved.append):
                result = self._manager(downloads).move_to_trash([link])

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])
            self.assertEqual(len(result.errors), 1)

    def test_file_changed_between_stat_checks_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            target = downloads / "race.bin"
            replacement = downloads / "replacement.bin"
            target.write_bytes(b"original")
            replacement.write_bytes(b"replacement")
            target_stat = target.stat()
            replacement_stat = replacement.stat()
            resolved_target = target.resolve()
            original_stat = Path.stat
            target_reads = 0

            def changing_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
                nonlocal target_reads
                if path == resolved_target:
                    target_reads += 1
                    if target_reads <= 2:
                        return target_stat
                    return replacement_stat
                return original_stat(path, *args, **kwargs)

            moved: list[str] = []
            with (
                patch.object(Path, "stat", changing_stat),
                patch("maintenance.actions.send2trash", moved.append),
            ):
                result = self._manager(downloads).move_to_trash([target])

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])
            self.assertTrue(
                any("changed during validation" in error for error in result.errors)
            )

    def test_symlinked_ancestor_escaping_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            downloads = outside / "Downloads"
            downloads.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("outside")
            link_dir = downloads / "linkdir"
            link_dir.symlink_to(outside, target_is_directory=True)
            moved: list[str] = []

            with patch("maintenance.actions.send2trash", moved.append):
                result = self._manager(downloads).move_to_trash(
                    [link_dir / "secret.txt"]
                )

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])

    def test_missing_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            moved: list[str] = []

            with patch("maintenance.actions.send2trash", moved.append):
                result = self._manager(downloads).move_to_trash(
                    [downloads / "ghost.bin"]
                )

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])
            self.assertEqual(len(result.errors), 1)

    def test_directory_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            subdir = downloads / "folder"
            subdir.mkdir()
            moved: list[str] = []

            with patch("maintenance.actions.send2trash", moved.append):
                result = self._manager(downloads).move_to_trash([subdir])

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])

    def test_send2trash_failure_reports_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            first = downloads / "first.bin"
            second = downloads / "second.bin"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            errors: list[str] = []

            def fail_first(path: str) -> None:
                if path == str(first.resolve()):
                    raise OSError("trash denied")
                errors.append(path)

            with patch("maintenance.actions.send2trash", fail_first):
                result = self._manager(downloads).move_to_trash([first, second])

            self.assertEqual(result.moved, (second.resolve(),))
            self.assertEqual(len(result.errors), 1)

    def test_duplicate_input_paths_are_trashed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            downloads.mkdir()
            target = downloads / "file.bin"
            target.write_bytes(b"content")
            moved: list[str] = []

            with patch("maintenance.actions.send2trash", moved.append):
                result = self._manager(downloads).move_to_trash([target, target])

            self.assertEqual(result.requested, 2)
            self.assertEqual(result.moved, (target.resolve(),))
            self.assertEqual(len(moved), 1)


if __name__ == "__main__":
    unittest.main()
