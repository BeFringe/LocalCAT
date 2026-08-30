"""Windows WA-06 5.9a replacement and recovery-owner acceptance."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

from tests.test_tm_portable_recovery_windows import (
    _assert_complete,
    _assert_no_worker_exception,
    _assert_unavailable,
    _remove_long_quarantine,
    _run_worker,
)


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableRecoveryAttackTests(unittest.TestCase):
    def test_live_same_byte_swap_is_blocked_during_sqlite_owner_window(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_MUTATION_GUARD")

            _assert_no_worker_exception(self, creator)
            self.assertGreater(creator["boundary_calls"], 0)
            _assert_complete(self, creator)
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_exact_manifest_physical_ahead_completes_one_generation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="MANIFEST_PHYSICAL")
            recover = _run_worker(root, "recover-bounded")
            replay = _run_worker(root, "replay")

            for result in (creator, recover, replay):
                _assert_no_worker_exception(self, result)
            self.assertEqual(creator["boundary_calls"], 1)
            self.assertEqual(creator["journal"]["phases"], ["DB_REPLACED"])
            self.assertIsNotNone(creator["runtime"]["manifest"])
            self.assertEqual(
                creator["runtime"]["database_sql"]["meta"]["activation_status"],
                "ACTIVE",
            )
            self.assertEqual(
                creator["runtime"]["database_sql"]["receipt_statuses"],
                ["completed"],
            )
            self.assertGreater(
                creator["runtime"]["database"]["size"],
                64 * 1024,
                "bounded recovery fixture must exceed one stream chunk",
            )
            _assert_complete(self, recover)
            _assert_complete(self, replay)
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_fresh_same_byte_replacements_use_current_exact_authority(self) -> None:
        for mutation_name in (
            "same-byte-database",
            "same-byte-stage-database",
            "same-byte-source",
        ):
            with self.subTest(mutation=mutation_name):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase="DB_REPLACED")
                    mutation = _run_worker(root, "mutate", mutation=mutation_name)
                    recover = _run_worker(root, "recover-bounded")

                    for result in (creator, mutation, recover):
                        _assert_no_worker_exception(self, result)
                    self.assertEqual(mutation["journal"]["phases"], ["DB_REPLACED"])
                    _assert_complete(self, recover)
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_fresh_recovery_uses_live_database_mutation_guard(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_REPLACED")
            recover = _run_worker(root, "recover-guard")
            replay = _run_worker(root, "replay")

            for result in (creator, recover, replay):
                _assert_no_worker_exception(self, result)
            self.assertGreater(recover["boundary_calls"], 0)
            _assert_complete(self, recover)
            _assert_complete(self, replay)
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_active_set_blocks_post_business_swaps_until_ready(self) -> None:
        for mode in (
            "recover-ready-manifest-same",
            "recover-ready-manifest-different",
            "recover-ready-source-different",
        ):
            with self.subTest(mode=mode):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase="DB_REPLACED")
                    recover = _run_worker(root, mode)

                    for result in (creator, recover):
                        _assert_no_worker_exception(self, result)
                    self.assertEqual(recover["boundary_calls"], 1)
                    _assert_complete(self, recover)
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_physical_manifest_ahead_rejects_sealed_database_unchanged(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_REPLACED")
            mutation = _run_worker(
                root,
                "mutate",
                mutation="physical-manifest-ahead-sealed",
            )
            recover = _run_worker(root, "recover")

            for result in (creator, mutation, recover):
                _assert_no_worker_exception(self, result)
            _assert_unavailable(self, recover)
            self.assertEqual(
                mutation["disk"]["database_sql"]["meta"]["activation_status"],
                "SEALED",
            )
            self.assertEqual(
                mutation["disk"]["database_sql"]["receipt_statuses"],
                ["issued"],
            )
            self.assertEqual(recover["disk"], mutation["disk"])
            self.assertEqual(recover["journal"], mutation["journal"])
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_active_set_close_failure_withholds_ready_initial_and_fresh(self) -> None:
        for creator_phase, failure_mode in (
            ("ACTIVE_SET_CLOSE", None),
            ("DB_REPLACED", "recover-close"),
        ):
            with self.subTest(
                creator_phase=creator_phase,
                failure_mode=failure_mode,
            ):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase=creator_phase)
                    if failure_mode is None:
                        failed = creator
                    else:
                        failed = _run_worker(root, failure_mode)
                    replay = _run_worker(root, "replay")

                    for result in (creator, failed, replay):
                        _assert_no_worker_exception(self, result)
                    self.assertEqual(failed["boundary_calls"], 1)
                    _assert_unavailable(self, failed)
                    self.assertEqual(failed["runtime"]["state"], "ACTIVATING")
                    self.assertFalse(failed["runtime"]["view_visible"])
                    _assert_complete(self, replay)
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_store_cleanup_faults_preserve_primary_and_release_all_handles(self) -> None:
        for phase, expected_released in (
            (
                "APPLY_PROGRAMMER_CLOSE",
                ("tm.primary.jsonl.sqlite3", "tm.primary.jsonl"),
            ),
            (
                "ACTIVE_REPROVE_PROGRAMMER_CLOSE",
                (
                    "tm.primary.jsonl.sqlite3",
                    "tm.primary.jsonl.localcat-snapshot.json",
                    "tm.primary.jsonl",
                ),
            ),
        ):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    result = _run_worker(root, "create", phase=phase)

                    _assert_no_worker_exception(self, result)
                    self.assertTrue(result["caught_is_primary"], result)
                    self.assertEqual(result["caught_exception_type"], "AssertionError")
                    self.assertGreater(result["cleanup_faults"], 0)
                    for name in expected_released:
                        self.assertTrue(result["released"][name], result)
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_synchronized_close_fault_releases_untransferred_guard(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            result = _run_worker(root, "create", phase="SYNCHRONIZED_CLOSE")

            _assert_no_worker_exception(self, result)
            _assert_unavailable(self, result)
            self.assertEqual(result["boundary_calls"], 1)
            self.assertEqual(result["cleanup_faults"], 1)
            self.assertTrue(result["released"]["tm.primary.jsonl.sqlite3"], result)
            self.assertTrue(result["released"]["tm.primary.jsonl"], result)
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_fresh_first_guard_reproof_failure_closes_without_masking(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_REPLACED")
            result = _run_worker(root, "recover-guard-reprove-close")

            for item in (creator, result):
                _assert_no_worker_exception(self, item)
            self.assertTrue(result["caught_is_primary"], result)
            self.assertEqual(result["caught_exception_type"], "AssertionError")
            self.assertTrue(result["fault_guard_closed"], result)
            self.assertTrue(result["released"]["tm.primary.jsonl.sqlite3"], result)
            self.assertEqual(result["runtime"]["state"], "ACTIVATING")
            self.assertFalse(result["runtime"]["view_visible"])
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_foreign_database_and_multilink_fail_without_cleanup(self) -> None:
        for mutation_name, phase in (
            ("foreign-database-replacement", "DB_REPLACED"),
            ("foreign-manifest-replacement", "MANIFEST_PUBLISHED"),
            ("stage-database-multilink", "DB_REPLACED"),
            ("stage-database-junction", "DB_REPLACED"),
        ):
            with self.subTest(mutation=mutation_name):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                mutation: dict = {}
                try:
                    creator = _run_worker(root, "create", phase=phase)
                    mutation = _run_worker(root, "mutate", mutation=mutation_name)
                    before = mutation["disk"]
                    journal_before = mutation["journal"]
                    recover = _run_worker(root, "recover")

                    for result in (creator, mutation, recover):
                        _assert_no_worker_exception(self, result)
                    _assert_unavailable(self, recover)
                    self.assertEqual(recover["disk"], before)
                    self.assertEqual(recover["journal"], journal_before)
                    if mutation_name == "stage-database-multilink":
                        self.assertTrue((root / "foreign-stage-hardlink").is_file())
                finally:
                    _remove_long_quarantine(root)
                    if (root / "foreign-stage-hardlink").exists():
                        os.unlink(root / "foreign-stage-hardlink")
                    if mutation_name == "stage-database-junction":
                        for name in mutation.get("journal", {}).get(
                            "stage_assets", {}
                        ):
                            candidate = root / name
                            if candidate.is_dir():
                                os.rmdir(candidate)
                        target = root / "foreign-junction-target"
                        if target.exists():
                            os.rmdir(target)
                    temporary.cleanup()

    def test_private_same_byte_foreign_acl_fails_before_business_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_REPLACED")
            mutation = _run_worker(
                root,
                "mutate",
                mutation="private-journal-foreign-acl",
            )
            before = mutation["disk"]
            recover = _run_worker(root, "recover")

            for result in (creator, mutation, recover):
                _assert_no_worker_exception(self, result)
            _assert_unavailable(self, recover)
            self.assertEqual(recover["disk"], before)
            self.assertEqual(recover["journal"], mutation["journal"])
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_portable_journal_persists_no_historical_file_identity(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_REPLACED")
            _assert_no_worker_exception(self, creator)
            private_roots = [
                path
                for path in root.iterdir()
                if path.is_dir() and path.name.startswith(".localcat-activation-private-")
            ]
            self.assertEqual(len(private_roots), 1)
            serialized = "\n".join(
                path.read_text(encoding="utf-8")
                for path in private_roots[0].iterdir()
                if path.suffix == ".json"
            )
            self.assertNotIn('"file_id"', serialized)
            self.assertNotIn('"volume_id"', serialized)
            self.assertNotIn('"inode"', serialized)
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
