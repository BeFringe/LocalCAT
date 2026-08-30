"""Windows bound-family acceptance for WA-06 Task 5.12a."""

from __future__ import annotations

import os
from pathlib import Path, PurePath
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    CandidateFile,
    LockPolicy,
    LockWait,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
)
from platform_fs_windows import WindowsPlatformAdapter
from tm_contracts import ExportFailure, ExportReport, MigrationReport
from tm_engine import TMEngine
from tm_migration import TMMigrationService, _export_artifact_paths
import tm_migration
from tm_sqlite_store import (
    ReceiptCompletionProbe,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
)
from tests.test_tm_initial_activation_windows import (
    _identity as _activation_identity,
    _remove_long_quarantine,
    _service as _activation_service,
    _tree_file_bytes,
)
from tests.test_tm_export import (
    _destination,
    _ledger_receipt,
    _ledger_rows,
    _prepared_store,
)


@unittest.skipUnless(os.name == "nt", "Windows bound publication")
class WindowsTMExportFamilyTests(unittest.TestCase):
    def _service(
        self,
        identity: object,
        backend: WindowsPlatformAdapter,
    ) -> TMMigrationService:
        return TMMigrationService(
            resource_identity=identity,
            canonical_store_id="store.primary",
            platform_backend=backend,
        )

    def test_bound_family_success_keeps_active_authority_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            backend = WindowsPlatformAdapter()
            destination = _destination(root)
            before = store.capture_export_snapshot()

            result = self._service(
                stage.resource_identity,
                backend,
            ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportReport)
            self.assertEqual(store.capture_export_snapshot(), before)
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "completed")
            paths = _export_artifact_paths(destination)
            self.assertTrue(paths.destination.is_file())
            self.assertTrue(paths.manifest.is_file())
            for residue in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(residue.exists())

    def test_existing_pair_keeps_all_four_pending_through_owner_phase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            backend = WindowsPlatformAdapter()
            service = self._service(stage.resource_identity, backend)
            destination = _destination(root)
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            events: list[str] = []
            original_complete = SQLiteTMStore.complete_bound_issued_export_receipt
            original_terminal = PendingPublication.terminal_reproof
            original_close = PendingPublication.close

            def complete(authority: SQLiteTMStore, *args: object, **kwargs: object) -> None:
                events.append("owner")
                original_complete(authority, *args, **kwargs)

            def terminal(authority: PendingPublication) -> object:
                events.append("terminal")
                return original_terminal(authority)

            def close(authority: PendingPublication) -> None:
                events.append("close")
                original_close(authority)

            with patch.object(
                SQLiteTMStore,
                "complete_bound_issued_export_receipt",
                new=complete,
            ), patch.object(
                PendingPublication,
                "terminal_reproof",
                new=terminal,
            ), patch.object(PendingPublication, "close", new=close):
                second = service.export_jsonl(store, destination)

            self.assertIs(type(second), ExportReport)
            self.assertEqual(events[0], "owner")
            self.assertEqual(events.count("terminal"), 4)
            self.assertEqual(events.count("close"), 4)
            self.assertLess(
                max(index for index, value in enumerate(events) if value == "terminal"),
                min(index for index, value in enumerate(events) if value == "close"),
            )
            paths = _export_artifact_paths(destination)
            self.assertFalse(paths.jsonl_recovery.exists())
            self.assertFalse(paths.manifest_recovery.exists())

    def test_manifest_post_rename_fault_restores_exact_prior_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            first_service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(first_service.export_jsonl(store, destination)), ExportReport)
            paths = _export_artifact_paths(destination)
            prior_jsonl = destination.read_bytes()
            prior_manifest = paths.manifest.read_bytes()
            rename_count = 0

            def fault(phase: str) -> None:
                nonlocal rename_count
                if phase == "publish_after_rename":
                    rename_count += 1
                    if rename_count == 4:
                        raise RuntimeError("manifest post-rename")

            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(_fault_injector=fault),
            ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertFalse(result.publication_committed)
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual([row[9] for row in rows], ["completed", "cancelled"])
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), str(artifact))

    def test_owner_precommit_failure_restores_and_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            with patch.object(
                SQLiteTMStore,
                "complete_bound_issued_export_receipt",
                side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)
            self.assertIs(type(result), ExportFailure)
            self.assertFalse(destination.exists())
            self.assertFalse(_export_artifact_paths(destination).manifest.exists())
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "cancelled")

    def test_owner_commit_then_raise_requires_four_terminal_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            original = SQLiteTMStore.complete_bound_issued_export_receipt
            terminal_count = 0
            original_terminal = PendingPublication.terminal_reproof

            def commit_then_raise(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                original(authority, *args, **kwargs)
                raise sqlite3.DatabaseError("after commit")

            def terminal(authority: PendingPublication) -> object:
                nonlocal terminal_count
                terminal_count += 1
                return original_terminal(authority)

            with patch.object(
                SQLiteTMStore,
                "complete_bound_issued_export_receipt",
                new=commit_then_raise,
            ), patch.object(
                PendingPublication,
                "terminal_reproof",
                new=terminal,
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportReport)
            self.assertEqual(terminal_count, 2)
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "completed")

    def test_existing_pair_commit_then_raise_keeps_four_pending_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            events: list[str] = []
            original_complete = SQLiteTMStore.complete_bound_issued_export_receipt
            original_probe = SQLiteTMStore.probe_issued_receipt_completed
            original_terminal = PendingPublication.terminal_reproof
            original_close = PendingPublication.close

            def complete_then_raise(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                original_complete(authority, *args, **kwargs)
                events.append("owner-raised")
                raise sqlite3.DatabaseError("after commit")

            def probe(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> ReceiptCompletionProbe:
                events.append("probe")
                return original_probe(authority, *args, **kwargs)

            def terminal(authority: PendingPublication) -> object:
                events.append("terminal")
                return original_terminal(authority)

            def close(authority: PendingPublication) -> None:
                events.append("close")
                original_close(authority)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    new=complete_then_raise,
                ),
                patch.object(
                    SQLiteTMStore,
                    "probe_issued_receipt_completed",
                    new=probe,
                ),
                patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    new=terminal,
                ),
                patch.object(PendingPublication, "close", new=close),
            ):
                result = service.export_jsonl(store, destination)

            self.assertIs(type(result), ExportReport)
            self.assertEqual(events[:2], ["owner-raised", "probe"])
            self.assertEqual(events.count("terminal"), 4)
            self.assertEqual(events.count("close"), 4)

    def test_committed_unclean_never_restores_or_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            prior_manifest = _export_artifact_paths(destination).manifest.read_bytes()
            original_complete = SQLiteTMStore.complete_bound_issued_export_receipt

            def complete_then_raise(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                original_complete(authority, *args, **kwargs)
                raise sqlite3.DatabaseError("committed but cleanup unknown")

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    new=complete_then_raise,
                ),
                patch.object(
                    SQLiteTMStore,
                    "probe_issued_receipt_completed",
                    return_value=ReceiptCompletionProbe.COMMITTED_UNCLEAN,
                ),
                patch.object(
                    store,
                    "cancel_issued_export_receipt",
                    side_effect=AssertionError("committed receipt was cancelled"),
                ),
                patch.object(
                    tm_migration,
                    "_restore_bound_export_family",
                    side_effect=AssertionError("committed family was restored"),
                ),
            ):
                result = service.export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_committed)
            self.assertNotEqual(
                _export_artifact_paths(destination).manifest.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "completed"],
            )

    def test_existing_pair_rollback_orders_original_restore_and_recovery_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            events: list[str] = []
            original_terminal = PendingPublication.terminal_reproof
            original_close = PendingPublication.close
            original_cancel = store.cancel_issued_export_receipt

            def terminal(authority: PendingPublication) -> object:
                events.append("terminal")
                return original_terminal(authority)

            def close(authority: PendingPublication) -> None:
                events.append("close")
                original_close(authority)

            def cancel(*args: object, **kwargs: object) -> None:
                events.append("cancel")
                original_cancel(*args, **kwargs)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(PendingPublication, "terminal_reproof", new=terminal),
                patch.object(PendingPublication, "close", new=close),
                patch.object(store, "cancel_issued_export_receipt", new=cancel),
            ):
                result = service.export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(
                events,
                [
                    "terminal",
                    "terminal",
                    "close",
                    "close",
                    "cancel",
                    "terminal",
                    "terminal",
                    "terminal",
                    "terminal",
                    "close",
                    "close",
                    "close",
                    "close",
                ],
            )

    def test_rollback_create_if_absent_never_overwrites_external_insertion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            inserted = False

            def insert_foreign(name: str) -> None:
                nonlocal inserted
                if not inserted and name == destination.name:
                    destination.write_bytes(b"foreign rollback competitor")
                    inserted = True

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    tm_migration,
                    "_after_bound_export_rollback_unlink",
                    side_effect=insert_foreign,
                ),
                patch.object(
                    store,
                    "cancel_issued_export_receipt",
                    side_effect=AssertionError("foreign insertion was cancelled"),
                ),
            ):
                result = service.export_jsonl(store, destination)

            self.assertTrue(inserted)
            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(result.error_code, "EXPORT.RECOVERY_REQUIRED")
            self.assertEqual(destination.read_bytes(), b"foreign rollback competitor")
            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "issued"],
            )

    def test_rollback_create_if_absent_operational_failure_is_recovery_required(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            armed = False
            fired = False

            def arm_after_unlink(_name: str) -> None:
                nonlocal armed
                armed = True

            def fault(phase: str) -> None:
                nonlocal fired
                if armed and phase == "publish_before_rename" and not fired:
                    fired = True
                    raise RuntimeError("rollback create-if-absent fault")

            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(_fault_injector=fault),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    tm_migration,
                    "_after_bound_export_rollback_unlink",
                    side_effect=arm_after_unlink,
                ),
                patch.object(
                    store,
                    "cancel_issued_export_receipt",
                    side_effect=AssertionError("failed restore was cancelled"),
                ),
            ):
                result = service.export_jsonl(store, destination)

            self.assertTrue(fired)
            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(result.error_code, "EXPORT.RECOVERY_REQUIRED")
            self.assertFalse(destination.exists())
            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "issued"],
            )

    def test_rollback_programmer_error_crosses_public_seam(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    tm_migration,
                    "_after_bound_export_rollback_unlink",
                    side_effect=AssertionError("rollback programmer fault"),
                ),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "rollback programmer fault",
                ):
                    service.export_jsonl(store, destination)

    def test_original_pending_terminal_operational_failure_requires_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    side_effect=PlatformFileError(
                        PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                        retryable=False,
                    ),
                ),
                patch.object(
                    store,
                    "cancel_issued_export_receipt",
                    side_effect=AssertionError("unclosed restore was cancelled"),
                ),
            ):
                result = service.export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(result.stage, "EXPORT.RECOVERY")
            self.assertEqual(result.error_code, "EXPORT.RECOVERY_REQUIRED")
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "issued"],
            )

    def test_original_pending_terminal_programmer_error_crosses_public_seam(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_export_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    side_effect=AssertionError("terminal programmer fault"),
                ),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "terminal programmer fault",
                ):
                    service.export_jsonl(store, destination)

            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "issued"],
            )

    def test_post_rename_fault_rolls_back_absence_and_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            fired = False

            def fault(phase: str) -> None:
                nonlocal fired
                if phase == "publish_after_rename" and not fired:
                    fired = True
                    raise RuntimeError("injected")

            backend = WindowsPlatformAdapter(_fault_injector=fault)
            destination = _destination(root)
            result = self._service(
                stage.resource_identity,
                backend,
            ).export_jsonl(store, destination)

            self.assertTrue(fired)
            self.assertIs(type(result), ExportFailure)
            paths = _export_artifact_paths(destination)
            for artifact in (
                paths.destination,
                paths.manifest,
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), str(artifact))
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "cancelled")

    def test_preissued_second_candidate_failure_cleans_exact_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            original = tm_migration._bound_candidate_from_chunks
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PlatformFileError(
                        PlatformFileErrorCode.PUBLISH_FAILED,
                        retryable=True,
                    )
                return original(*args, **kwargs)

            with patch.object(
                tm_migration,
                "_bound_candidate_from_chunks",
                side_effect=fail_second,
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertFalse(result.publication_commit_ambiguous)
            self.assertEqual(_ledger_rows(stage.staged_db_path, destination), ())
            paths = _export_artifact_paths(destination)
            for artifact in (
                paths.destination,
                paths.manifest,
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), str(artifact))

    def test_preissued_candidate_faults_preserve_exact_cleanup_state(self) -> None:
        fault = PlatformFileError(
            PlatformFileErrorCode.PUBLISH_FAILED,
            retryable=True,
        )
        for phase in ("identity", "write", "flush", "close", "unlink"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store = _prepared_store(root)
                destination = _destination(root)
                paths = _export_artifact_paths(destination)
                stack = []
                if phase == "identity":
                    stack.append(patch.object(CandidateFile, "identity", side_effect=fault))
                elif phase in {"write", "close", "unlink"}:
                    stack.append(
                        patch.object(CandidateFile, "write_chunks", side_effect=fault)
                    )
                else:
                    stack.append(
                        patch.object(
                            CandidateFile,
                            "flush_content",
                            side_effect=PlatformFileError(
                                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                                retryable=False,
                            ),
                        )
                    )
                if phase == "close":
                    original_close = CandidateFile.close

                    def close_then_raise(authority: CandidateFile) -> None:
                        original_close(authority)
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )

                    stack.append(patch.object(CandidateFile, "close", new=close_then_raise))
                if phase == "unlink":
                    stack.append(
                        patch.object(
                            BoundDirectoryAuthority,
                            "unlink_owned",
                            side_effect=PlatformFileError(
                                PlatformFileErrorCode.RECOVERY_REQUIRED,
                                retryable=True,
                            ),
                        )
                    )
                entered = []
                try:
                    for manager in stack:
                        entered.append(manager)
                        manager.__enter__()
                    result = self._service(
                        stage.resource_identity,
                        WindowsPlatformAdapter(),
                    ).export_jsonl(store, destination)
                finally:
                    for manager in reversed(entered):
                        manager.__exit__(None, None, None)

                self.assertIs(type(result), ExportFailure)
                self.assertEqual(_ledger_rows(stage.staged_db_path, destination), ())
                if phase in {"identity", "unlink"}:
                    self.assertEqual(result.error_code, "EXPORT.RECOVERY_REQUIRED")
                    self.assertTrue(paths.jsonl_temp.exists())
                    paths.jsonl_temp.unlink()
                else:
                    self.assertFalse(paths.jsonl_temp.exists())
                    expected = (
                        "EXPORT.DURABILITY_UNAVAILABLE"
                        if phase == "flush"
                        else "EXPORT.CAPABILITY_UNAVAILABLE"
                    )
                    self.assertEqual(result.error_code, expected)

    def test_recovery_copy_post_rename_failure_is_recovery_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            service = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            )
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            before_jsonl = destination.read_bytes()
            before_manifest = _export_artifact_paths(destination).manifest.read_bytes()
            fired = False

            def fault_after_recovery_rename(phase: str) -> None:
                nonlocal fired
                if phase == "publish_after_rename" and not fired:
                    fired = True
                    raise RuntimeError("recovery post-arm")

            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(_fault_injector=fault_after_recovery_rename),
            ).export_jsonl(store, destination)

            self.assertTrue(fired)
            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(destination.read_bytes(), before_jsonl)
            self.assertEqual(
                _export_artifact_paths(destination).manifest.read_bytes(),
                before_manifest,
            )
            self.assertEqual(
                [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                ["completed", "issued"],
            )

    def test_registration_commit_then_exception_preserves_family_as_ambiguous(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            original = store.register_issued_export_receipt

            def commit_then_raise(*args: object, **kwargs: object) -> None:
                original(*args, **kwargs)
                raise sqlite3.DatabaseError("injected")

            with patch.object(
                store,
                "register_issued_export_receipt",
                side_effect=commit_then_raise,
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_commit_ambiguous)
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "issued")
            paths = _export_artifact_paths(destination)
            self.assertTrue(paths.jsonl_temp.is_file())
            self.assertTrue(paths.manifest_temp.is_file())
            self.assertFalse(paths.destination.exists())
            self.assertFalse(paths.manifest.exists())

    def test_hardlinked_destination_is_foreign_and_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            foreign = destination.with_name("foreign.jsonl")
            foreign.write_bytes(b"foreign\n")
            os.link(foreign, destination)
            before = foreign.read_bytes()

            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(foreign.read_bytes(), before)
            self.assertEqual(destination.read_bytes(), before)
            self.assertEqual(_ledger_rows(stage.staged_db_path, destination), ())

    def test_target_open_blocks_replace_without_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            backend = WindowsPlatformAdapter()
            service = self._service(stage.resource_identity, backend)
            destination = _destination(root)
            first = service.export_jsonl(store, destination)
            self.assertIs(type(first), ExportReport)
            before_jsonl = destination.read_bytes()
            before_manifest = _export_artifact_paths(destination).manifest.read_bytes()

            authority = backend.bind_root(destination.parent)
            reader = backend.open_regular(authority, PurePath(destination.name))
            authority.close()
            try:
                result = service.export_jsonl(store, destination)
            finally:
                reader.close()

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(destination.read_bytes(), before_jsonl)
            self.assertEqual(
                _export_artifact_paths(destination).manifest.read_bytes(),
                before_manifest,
            )
            self.assertFalse(result.publication_committed)

    def test_lock_contention_is_preflight_and_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            error = PlatformFileError(
                PlatformFileErrorCode.LOCK_CONTENDED,
                retryable=True,
            )
            with patch.object(
                WindowsPlatformAdapter,
                "acquire",
                side_effect=error,
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.LOCK_CONTENDED")
            self.assertEqual(_ledger_rows(stage.staged_db_path, destination), ())
            self.assertFalse(destination.exists())

    def test_case_aliases_share_one_real_parent_family_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            upper = _destination(root, "CaseAlias.jsonl")
            lower = upper.with_name("casealias.jsonl")
            holder_backend = WindowsPlatformAdapter()
            contender_backend = WindowsPlatformAdapter()
            holder_root = holder_backend.bind_root(upper.parent)
            holder_parent = holder_backend.bind_parent(
                holder_root,
                PurePath(upper.name),
            )
            lease = holder_backend.acquire(
                holder_parent,
                tm_migration._bound_export_lock_name(upper.name),
                tm_migration._bound_export_lock_payload(upper.name),
                LockPolicy(LockWait.BLOCK),
            )
            started = threading.Event()
            finished = threading.Event()
            outcome: list[object] = []

            def contend() -> None:
                started.set()
                outcome.append(
                    self._service(
                        stage.resource_identity,
                        contender_backend,
                    ).export_jsonl(store, lower)
                )
                finished.set()

            worker = threading.Thread(target=contend, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2.0))
            time.sleep(0.2)
            self.assertFalse(finished.is_set())
            lease.close()
            holder_parent.close()
            holder_root.close()
            worker.join(10.0)

            self.assertFalse(worker.is_alive())
            self.assertTrue(finished.is_set())
            self.assertEqual(len(outcome), 1)
            self.assertIs(type(outcome[0]), ExportReport)

    def test_embedded_nul_preserves_destination_unsafe_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = Path(f"{root.resolve()}/bad\0name.jsonl")

            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
            self.assertEqual(result.error_code, "EXPORT.DESTINATION_UNSAFE")
            self.assertEqual(_ledger_rows(stage.staged_db_path), ())

    def test_manifest_hardlink_is_foreign_and_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            foreign = paths.manifest.with_name("foreign-manifest.json")
            foreign.write_bytes(b"foreign manifest")
            os.link(foreign, paths.manifest)

            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.MANIFEST_UNSAFE")
            self.assertEqual(foreign.read_bytes(), b"foreign manifest")
            self.assertEqual(paths.manifest.read_bytes(), b"foreign manifest")
            self.assertFalse(destination.exists())
            self.assertEqual(_ledger_rows(stage.staged_db_path), ())

    def test_open_manifest_blocks_replace_without_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            backend = WindowsPlatformAdapter()
            service = self._service(stage.resource_identity, backend)
            self.assertIs(type(service.export_jsonl(store, destination)), ExportReport)
            paths = _export_artifact_paths(destination)
            before_jsonl = destination.read_bytes()
            before_manifest = paths.manifest.read_bytes()
            authority = backend.bind_root(destination.parent)
            reader = backend.open_regular(authority, PurePath(paths.manifest.name))
            authority.close()
            try:
                result = service.export_jsonl(store, destination)
            finally:
                reader.close()

            self.assertIs(type(result), ExportFailure)
            self.assertFalse(result.publication_committed)
            self.assertEqual(destination.read_bytes(), before_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), before_manifest)

    def test_orphan_manifest_and_foreign_temp_recovery_are_never_mutated(
        self,
    ) -> None:
        for artifact_kind in (
            "manifest",
            "jsonl_temp",
            "manifest_temp",
            "jsonl_recovery",
            "manifest_recovery",
        ):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store = _prepared_store(root)
                destination = _destination(root)
                paths = _export_artifact_paths(destination)
                artifact = getattr(paths, artifact_kind)
                artifact.write_bytes(b"foreign artifact")

                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

                self.assertIs(type(result), ExportFailure)
                self.assertEqual(artifact.read_bytes(), b"foreign artifact")
                self.assertFalse(destination.exists())
                self.assertEqual(_ledger_rows(stage.staged_db_path), ())

    def test_missing_parent_uses_existing_parent_unsafe_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = root / "missing" / "snapshot.jsonl"
            result = self._service(
                stage.resource_identity,
                WindowsPlatformAdapter(),
            ).export_jsonl(store, destination)
            self.assertIs(type(result), ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.PARENT_UNSAFE")
            self.assertEqual(_ledger_rows(stage.staged_db_path, destination), ())

    def test_terminal_failure_after_owner_commit_is_committed_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            error = PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
            with patch.object(
                PendingPublication,
                "terminal_reproof",
                side_effect=error,
            ):
                result = self._service(
                    stage.resource_identity,
                    WindowsPlatformAdapter(),
                ).export_jsonl(store, destination)

            self.assertIs(type(result), ExportFailure)
            self.assertTrue(result.publication_committed)
            self.assertTrue(destination.is_file())
            self.assertTrue(_export_artifact_paths(destination).manifest.is_file())
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][9], "completed")

    def test_each_existing_family_terminal_failure_is_never_success(self) -> None:
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store = _prepared_store(root)
                destination = _destination(root)
                self.assertIs(
                    type(
                        self._service(
                            stage.resource_identity,
                            WindowsPlatformAdapter(),
                        ).export_jsonl(store, destination)
                    ),
                    ExportReport,
                )
                calls = 0
                original_terminal = PendingPublication.terminal_reproof

                def terminal(authority: PendingPublication) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )
                    return original_terminal(authority)

                with patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    new=terminal,
                ):
                    result = self._service(
                        stage.resource_identity,
                        WindowsPlatformAdapter(),
                    ).export_jsonl(store, destination)

                self.assertIs(type(result), ExportFailure)
                self.assertTrue(result.publication_committed)
                self.assertEqual(
                    [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                    ["completed", "completed"],
                )

    def test_each_existing_family_close_failure_is_never_success(self) -> None:
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store = _prepared_store(root)
                destination = _destination(root)
                self.assertIs(
                    type(
                        self._service(
                            stage.resource_identity,
                            WindowsPlatformAdapter(),
                        ).export_jsonl(store, destination)
                    ),
                    ExportReport,
                )
                calls = 0
                original_close = PendingPublication.close

                def close(authority: PendingPublication) -> None:
                    nonlocal calls
                    calls += 1
                    original_close(authority)
                    if calls == fail_at:
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )

                with patch.object(PendingPublication, "close", new=close):
                    result = self._service(
                        stage.resource_identity,
                        WindowsPlatformAdapter(),
                    ).export_jsonl(store, destination)

                self.assertIs(type(result), ExportFailure)
                self.assertTrue(result.publication_committed)
                self.assertEqual(
                    [row[9] for row in _ledger_rows(stage.staged_db_path, destination)],
                    ["completed", "completed"],
                )

    def test_bound_owner_rejects_ledger_destination_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            receipt = _ledger_receipt(
                store,
                snapshot_id="snapshot.export.destination-mismatch",
            )
            generation = store.capture_export_snapshot().revision.generation
            store.register_issued_export_receipt(
                receipt,
                destination_jsonl_path=destination,
                destination_manifest_path=paths.manifest,
                expected_generation=generation,
            )
            callback_called = False

            def reprove(_receipt: object) -> None:
                nonlocal callback_called
                callback_called = True

            wrong = destination.with_name("other.jsonl")
            with self.assertRaises(SQLiteStoreSchemaError) as caught:
                store.complete_bound_issued_export_receipt(
                    receipt.snapshot_id,
                    expected_generation=generation,
                    destination_jsonl_path=wrong,
                    destination_manifest_path=wrong.with_name(
                        f"{wrong.name}.localcat-snapshot.json"
                    ),
                    family_reprove=reprove,
                )
            self.assertEqual(
                str(caught.exception),
                "STORE.RECEIPT_DESTINATION_MISMATCH",
            )
            self.assertFalse(callback_called)
            self.assertEqual(
                _ledger_rows(stage.staged_db_path, destination)[0][9],
                "issued",
            )

    def test_completed_export_receipt_coexists_with_activation_cold_open(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.addCleanup(_remove_long_quarantine, root)
        identity = _activation_identity(root)
        activation = _activation_service(identity)
        activated = activation.activate_initial(
            identity.configured_jsonl_path,
            identity.resource_id,
        )
        self.assertIs(type(activated), MigrationReport, repr(activated))
        engine = TMEngine(
            str(identity.configured_jsonl_path),
            update=False,
            expected_resource_id=identity.resource_id,
        )
        store = engine.canonical_store
        self.assertIsNotNone(store)
        assert store is not None
        destination = _destination(root)

        exported = activation.export_jsonl(store, destination)

        self.assertIs(type(exported), ExportReport)
        rows = _ledger_rows(identity.canonical_sidecar_path)
        self.assertEqual(
            sorted(str(row[9]) for row in rows),
            ["completed", "completed"],
        )
        before = _tree_file_bytes(root)
        reopened = TMEngine(
            str(identity.configured_jsonl_path),
            update=False,
            expected_resource_id=identity.resource_id,
        )
        self.assertTrue(reopened.canonical_active)
        self.assertEqual(_tree_file_bytes(root), before)

    def test_activation_receipt_never_falls_back_to_arbitrary_export_history(
        self,
    ) -> None:
        for mutation in ("missing", "wrong-status"):
            with self.subTest(mutation=mutation):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name).resolve()
                self.addCleanup(_remove_long_quarantine, root)
                identity = _activation_identity(root)
                activation = _activation_service(identity)
                activated = activation.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport, repr(activated))
                engine = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                    expected_resource_id=identity.resource_id,
                )
                store = engine.canonical_store
                self.assertIsNotNone(store)
                assert store is not None
                destination = _destination(root)
                self.assertIs(
                    type(activation.export_jsonl(store, destination)),
                    ExportReport,
                )
                connection = sqlite3.connect(identity.canonical_sidecar_path)
                try:
                    activation_receipt_id = connection.execute(
                        "SELECT snapshot_id FROM tm_snapshot_binding "
                        "WHERE binding_id = 1"
                    ).fetchone()[0]
                    if mutation == "missing":
                        connection.execute(
                            "DELETE FROM tm_snapshot_receipt "
                            "WHERE snapshot_id = ?",
                            (activation_receipt_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE tm_snapshot_receipt SET status = 'cancelled' "
                            "WHERE snapshot_id = ?",
                            (activation_receipt_id,),
                        )
                    connection.commit()
                finally:
                    connection.close()
                before = _tree_file_bytes(root)

                with self.assertRaisesRegex(
                    ValueError,
                    "^TM.CANONICAL_UNHEALTHY$",
                ):
                    TMEngine(
                        str(identity.configured_jsonl_path),
                        update=False,
                        expected_resource_id=identity.resource_id,
                    )
                self.assertEqual(_tree_file_bytes(root), before)


if __name__ == "__main__":
    unittest.main()
