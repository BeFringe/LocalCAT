"""Core 10.5：包应用准入、终态与原资源 owner 的真实 SQLite 接缝。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from unittest.mock import patch

import tm_migration as migration
import tm_sqlite_store as storage
from platform_fs_contracts import BoundDirectoryAuthority, BoundRegularFile, LockLease, PendingPublication, PlatformFileError, PlatformFileErrorCode
from tm_contracts import CanonicalResourceIdentity, MigrationReport, SourceBindingState, TMRecordDraft
from tests.benchmark_worker_test_support import worker_temporary_directory


class _SnapshotApplyFixture(unittest.TestCase):
    def temporary_directory(self) -> AbstractContextManager[str]:
        return tempfile.TemporaryDirectory()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.base: Path
        self.root: Path
        self.source: Path
        self.identity: CanonicalResourceIdentity
        self.owner: storage.ResourceStoreCoordinator
        self.service: migration.TMMigrationService
        self.payload: Path
        self.digest: str

    @property
    def family(self) -> storage._ResourceFamilySession:
        family = self.owner._family
        assert family is not None
        return family

    def setUp(self) -> None:
        self.base = Path(self.enterContext(self.temporary_directory())).resolve()
        self.root = self.base / "resource"
        self.root.mkdir()
        self.source = self.root / "memory.jsonl"
        self.source.write_text('{"source":"prior","target":"旧译文"}\n', encoding="utf-8")
        self.identity = CanonicalResourceIdentity.from_configured_jsonl("tm.apply", self.source)
        self.owner = storage.ResourceStoreCoordinator(resource_identity=self.identity, canonical_store_id="store.apply")
        self.addCleanup(self.owner.close_family_session)
        self.service = migration.TMMigrationService(resource_identity=self.identity, canonical_store_id="store.apply", coordinator=self.owner)
        self.payload = self.base / "payload.jsonl"
        self.payload.write_text('{"source":"incoming","target":"新译文","speaker":"A","provenance":[["test","payload"]]}\n', encoding="utf-8")
        self.digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()

    def activate(self) -> storage.SQLiteTMStore:
        result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), MigrationReport, result)
        return storage.SQLiteTMStore.from_coordinator(self.owner)

    def baseline(self, store: storage.SQLiteTMStore) -> tuple[str, int, int, int]:
        value = store.canonical_revision()
        return (value.canonical_store_id, value.generation, value.head_revision, value.record_count)

    def apply(self, **kwargs):
        return self.service.apply_snapshot_payload(self.payload, self.identity.resource_id, expected_digest=self.digest, expected_record_count=1, **kwargs)

    def tree(self):
        return {p.relative_to(self.root).as_posix(): (p.stat().st_ino, p.read_bytes())
                for p in self.root.rglob("*") if p.is_file()}

@unittest.skipIf(sys.platform == "win32", "POSIX 原家族 apply")
class SnapshotApplyOwnerLifecycleTests(_SnapshotApplyFixture):
    def test_late_legal_append_is_stale_before_prepare_or_source_mutation(self):
        store = self.activate()
        expected = self.baseline(store)
        store.append(TMRecordDraft(source_raw="late save", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
        before = self.tree()
        prepared = []
        with self.assertRaises(migration.MigrationPreflightError) as caught:
            self.apply(initial=False, expected_destination_baseline=expected, owner_prepare=lambda: prepared.append(True))
        self.assertEqual(caught.exception.error_code, "IMPORT.DESTINATION_STALE")
        self.assertEqual(prepared, [])
        self.assertEqual(self.tree(), before)
        with store.query_lease() as view:
            self.assertEqual(view.exact_records("late save")[0].target_raw, "保留")

    def test_prepare_exception_preserves_entire_existing_family(self):
        store = self.activate()
        before = self.tree()
        primary = RuntimeError("sealed plan stale")
        def prepare():
            self.assertEqual(len(self.family._operation_tokens), 1)
            raise primary
        with self.assertRaises(RuntimeError) as caught:
            self.apply(initial=False, expected_destination_baseline=self.baseline(store), owner_prepare=prepare)
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.tree(), before)

    def test_create_finalizes_fresh_semantics_inside_original_owner(self):
        self.source.unlink()
        events = []
        def prepare():
            self.assertFalse(self.source.exists())
            events.append("prepare")
        def commit(report):
            self.assertEqual(report.migrated_count, 1)
            events.append("commit")
        def finalize(report, proof):
            self.assertIs(type(proof), migration.AppliedSnapshotProof)
            self.assertEqual(len(self.family._operation_tokens), 1)
            self.assertEqual(proof.resource_id, self.identity.resource_id)
            self.assertEqual(proof.canonical_store_id, self.owner.canonical_store_id)
            self.assertEqual((proof.generation, proof.head_revision, proof.record_count), (0, 1, 1))
            self.assertEqual(proof.payload_digest, self.digest)
            self.assertEqual(proof.payload_byte_count, len(self.payload.read_bytes()))
            self.assertEqual(len(proof.semantic_digest), 64)
            self.assertNotEqual(proof.semantic_digest, proof.payload_digest)
            events.append("finalize")
        outcome = self.apply(initial=True, owner_prepare=prepare, owner_commit=commit, owner_finalize=finalize)
        self.assertIs(type(outcome), MigrationReport, outcome)
        self.assertEqual(events, ["prepare", "commit", "finalize"])
        self.assertFalse(self.family._operation_tokens)
        with storage.SQLiteTMStore.from_coordinator(self.owner).query_lease() as view:
            self.assertEqual(view.exact_records("incoming")[0].target_raw, "新译文")

    def test_final_callback_failure_keeps_new_authority_and_exact_error(self):
        store = self.activate()
        primary = RuntimeError("receipt unavailable")
        def finalize(report, proof):
            self.assertEqual(proof.generation, 1)
            raise primary
        with self.assertRaises(RuntimeError) as caught:
            self.apply(initial=False, expected_destination_baseline=self.baseline(store), owner_finalize=finalize)
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.source.read_bytes(), self.payload.read_bytes())
        with store.query_lease() as view:
            self.assertEqual(view.exact_records("incoming")[0].target_raw, "新译文")

    def test_waiting_apply_checks_generation_after_prior_transition_finishes(self):
        store = self.activate()
        baseline = self.baseline(store)
        held = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        real_operation = self.family.operation
        @contextmanager
        def operation(*args, **kwargs):
            if held.is_set():
                second_started.set()
            with real_operation(*args, **kwargs) as borrow:
                yield borrow
        def first_commit(report):
            held.set()
            if not release.wait(5):
                raise AssertionError("second apply did not enter")
        prepared = []
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.family, "operation", operation):
            first = pool.submit(self.apply, initial=False, expected_destination_baseline=baseline, owner_commit=first_commit)
            self.assertTrue(held.wait(5))
            second = pool.submit(self.apply, initial=False, expected_destination_baseline=baseline, owner_prepare=lambda: prepared.append(True))
            self.assertTrue(second_started.wait(3))
            self.assertFalse(second.done())
            release.set()
            self.assertIs(type(first.result(timeout=8)), MigrationReport)
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                second.result(timeout=8)
        self.assertEqual(caught.exception.error_code, "IMPORT.DESTINATION_STALE")
        self.assertEqual(prepared, [])
        self.assertEqual(store.canonical_revision().generation, 1)

    def test_waiter_timeout_does_not_clear_active_calls_owner_projection(self):
        store = self.activate()
        baseline = self.baseline(store)
        held = threading.Event()
        release = threading.Event()
        self.owner._drain_timeout_seconds = 0.1
        def commit(report):
            held.set()
            if not release.wait(5):
                raise AssertionError("waiter did not finish")
            self.service.project_applied_owner(report, expected_digest=self.digest, expected_record_count=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.apply, initial=False, owner_commit=commit)
            self.assertTrue(held.wait(5))
            try:
                with self.assertRaises(migration.MigrationPreflightError) as caught:
                    self.apply(initial=False, expected_destination_baseline=baseline)
                self.assertEqual(caught.exception.error_code, "IMPORT.RESOURCE_LOCK_UNAVAILABLE")
            finally:
                release.set()
            self.assertIs(type(first.result(timeout=8)), MigrationReport)
        followup = self.apply(initial=False)
        self.assertIs(type(followup), MigrationReport)

    def test_final_callback_source_change_cannot_return_success(self):
        self.source.unlink()
        receipt = self.base / "committed-marker"
        def finalize(report, proof):
            receipt.write_text(proof.payload_digest)
            self.source.write_text('{"source":"changed","target":"external"}\n')
        with self.assertRaises((migration.MigrationPreflightError, storage.ActivationPreparationError)):
            self.apply(initial=True, owner_finalize=finalize)
        self.assertEqual(receipt.read_text(), self.digest)
        self.assertIn("external", self.source.read_text())

    def test_callbacks_follow_owned_closes_and_all_sql_is_closed(self):
        store = self.activate()
        baseline = self.baseline(store)
        names = {}
        events = []
        connections = []
        real_publish = BoundDirectoryAuthority.begin_publish
        real_terminal = PendingPublication.terminal_reproof
        real_close = PendingPublication.close
        real_cleanup = migration._cleanup_bound_export_artifacts
        real_connect = sqlite3.connect
        real_file_close = BoundRegularFile.close
        real_open = type(self.family._backend).open_regular
        payload_handles = []
        def opened(backend, parent, relative, *args, **kwargs):
            value = real_open(backend, parent, relative, *args, **kwargs)
            if str(relative) == self.payload.name:
                payload_handles.append(value)
            return value
        def file_close(value):
            result = real_file_close(value)
            if value in payload_handles:
                events.append("payload-close")
            return result
        def connect(*args, **kwargs):
            value = real_connect(*args, **kwargs)
            connections.append(value)
            return value
        def publish(parent, candidate, destination, **kwargs):
            value = real_publish(parent, candidate, destination, **kwargs)
            names[value] = destination
            return value
        def terminal(value):
            result = real_terminal(value)
            if names.get(value) == self.source.name:
                events.append("source-terminal")
            return result
        def close(value):
            result = real_close(value)
            if names.get(value) == self.source.name:
                events.append("source-close")
            return result
        def cleanup(parent, values):
            result = real_cleanup(parent, values)
            if any("localcat-import-recovery-" in name for name, _ in values):
                events.append("prior-cleanup")
            return result
        def finalize(report, proof):
            self.assertEqual(events, ["prepare", "commit", "source-terminal", "source-close", "prior-cleanup", "payload-close"])
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            self.assertEqual(proof.generation, 1)
            events.append("finalize")
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(PendingPublication, "terminal_reproof", terminal), patch.object(PendingPublication, "close", close), patch.object(migration, "_cleanup_bound_export_artifacts", cleanup), patch.object(sqlite3, "connect", connect), patch.object(type(self.family._backend), "open_regular", opened), patch.object(BoundRegularFile, "close", file_close):
            result = self.apply(initial=False, expected_destination_baseline=baseline, owner_prepare=lambda: events.append("prepare"), owner_commit=lambda report: events.append("commit"), owner_finalize=finalize)
        self.assertIs(type(result), MigrationReport, result)
        self.assertEqual(events[-1], "finalize")
        self.assertFalse(any("localcat-import-recovery-" in p.name for p in self.root.iterdir()))

    def _source_fault(self, method):
        self.source.unlink()
        pending_names = {}
        final = []
        real_publish = BoundDirectoryAuthority.begin_publish
        real_action = getattr(PendingPublication, method)
        def publish(parent, candidate, destination, **kwargs):
            value = real_publish(parent, candidate, destination, **kwargs)
            pending_names[value] = destination
            return value
        def action(value):
            result = real_action(value)
            if pending_names.get(value) == self.source.name:
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return result
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(PendingPublication, method, action):
            with self.assertRaises((migration.MigrationPreflightError, PlatformFileError)):
                self.apply(initial=True, owner_finalize=lambda *_: final.append(True))
        self.assertEqual(final, [])
        self.assertEqual(self.source.read_bytes(), self.payload.read_bytes())
        self.assertEqual(storage.SQLiteTMStore.from_coordinator(self.owner).canonical_revision().record_count, 1)

    def test_source_terminal_failure_does_not_finalize(self):
        self._source_fault("terminal_reproof")

    def test_source_close_failure_does_not_finalize(self):
        self._source_fault("close")

    def test_prior_cleanup_failure_does_not_finalize_or_restore_old_source(self):
        store = self.activate()
        final = []
        real_cleanup = migration._cleanup_bound_export_artifacts
        def cleanup(parent, values):
            if any("localcat-import-recovery-" in name for name, _ in values):
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return real_cleanup(parent, values)
        with patch.object(migration, "_cleanup_bound_export_artifacts", cleanup):
            with self.assertRaises(PlatformFileError):
                self.apply(initial=False, expected_destination_baseline=self.baseline(store), owner_finalize=lambda *_: final.append(True))
        self.assertEqual(final, [])
        self.assertEqual(self.source.read_bytes(), self.payload.read_bytes())
        self.assertTrue(any("localcat-import-recovery-" in p.name for p in self.root.iterdir()))

    def test_payload_close_failure_does_not_finalize(self):
        self.source.unlink()
        final = []
        real_close = BoundRegularFile.close
        real_open = type(self.family._backend).open_regular
        payload_handles = []
        def opened(backend, parent, relative, *args, **kwargs):
            value = real_open(backend, parent, relative, *args, **kwargs)
            if str(relative) == self.payload.name:
                payload_handles.append(value)
            return value
        def close(value):
            result = real_close(value)
            if value in payload_handles:
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return result
        with patch.object(type(self.family._backend), "open_regular", opened), patch.object(BoundRegularFile, "close", close):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=True, owner_finalize=lambda *_: final.append(True))
        self.assertEqual(caught.exception.error_code, "IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(final, [])

    def test_snapshot_reader_rejects_wrong_thread_and_expired_borrow(self):
        self.source.unlink()
        captured = []
        def finalize(report, proof):
            store = storage.SQLiteTMStore.from_coordinator(self.owner)
            reservation = self.service._active_owner_reservation
            assert reservation is not None
            borrow = reservation._family_borrow
            captured.append(borrow)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(store.capture_export_snapshot, _borrow=borrow)
                with self.assertRaises(storage.SQLiteStoreLifecycleError):
                    future.result(timeout=3)
            snapshot = store.capture_export_snapshot(_borrow=borrow)
            self.assertEqual(snapshot.records[0].record.target_raw, "新译文")
        result = self.apply(initial=True, owner_finalize=finalize)
        self.assertIs(type(result), MigrationReport)
        with self.assertRaises(storage.SQLiteStoreLifecycleError):
            storage.SQLiteTMStore.from_coordinator(self.owner).capture_export_snapshot(_borrow=captured[0])

    def test_semantic_reader_rejects_same_count_wrong_record_projection(self):
        self.source.unlink()
        final = []
        original = storage.SQLiteTMStore.capture_export_snapshot
        def snapshot(store, **kwargs):
            value = original(store, **kwargs)
            item = value.records[0]
            changed = replace(item, record=replace(item.record, target_raw="wrong"))
            return replace(value, records=(changed,))
        with patch.object(storage.SQLiteTMStore, "capture_export_snapshot", snapshot):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=True, owner_finalize=lambda *_: final.append(True))
        self.assertEqual(caught.exception.error_code, "IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(final, [])

    def test_outer_close_failure_keeps_durable_callback_fact_and_cold_new_resource(self):
        self.source.unlink()
        receipt = self.base / "consumer-receipt.json"
        real_close = LockLease.close
        def finalize(report, proof):
            receipt.write_text(json.dumps({"digest": proof.payload_digest, "generation": proof.generation}))
        def close(lease):
            result = real_close(lease)
            if receipt.exists():
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return result
        with patch.object(LockLease, "close", close):
            with self.assertRaises(PlatformFileError):
                self.apply(initial=True, owner_finalize=finalize)
        self.assertEqual(json.loads(receipt.read_text())["digest"], self.digest)
        self.assertEqual(self.family._state, "FAILED")
        code = "from tm_engine import TMEngine; import sys; s=TMEngine(sys.argv[1], expected_resource_id='tm.apply').canonical_store; assert s is not None; assert s.canonical_revision().record_count == 1; print(s.canonical_revision().generation)"
        cold = subprocess.run([sys.executable, "-B", "-c", code, str(self.source)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertEqual(cold.stdout.strip(), "0")

    def test_diverged_prior_can_explicitly_replace_and_legacy_call_still_works(self):
        store = self.activate()
        self.source.write_text('{"source":"external","target":"changed"}\n')
        self.assertIs(store.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
        final = []
        first = self.apply(initial=False, expected_destination_baseline=self.baseline(store), owner_finalize=lambda report, proof: final.append(proof))
        self.assertIs(type(first), MigrationReport)
        self.assertEqual(final[0].generation, 1)
        self.assertIsNot(store.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
        second = self.apply(initial=False)
        self.assertIs(type(second), MigrationReport)
        assert isinstance(second, MigrationReport)
        self.assertEqual(second.activated_generation, 2)

    def test_invalid_expectations_reject_before_admission(self):
        before = self.tree()
        invalid = [("store", True, 1, 1), ("store", -1, 1, 1), ("", 0, 1, 1), ["store", 0, 1, 1], ("store", 0, 1)]
        for expectation in invalid:
            with self.subTest(expectation=expectation), self.assertRaises(TypeError):
                self.apply(initial=False, expected_destination_baseline=expectation)
        with self.assertRaises(ValueError):
            self.apply(initial=True, expected_destination_baseline=("store", 0, 1, 1))
        for name in ("owner_prepare", "owner_finalize"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                self.apply(initial=True, **{name: 1})
        self.assertEqual(self.tree(), before)

    def test_multiple_variants_keep_order_context_and_provenance_in_final_proof(self):
        self.source.unlink()
        rows = [
            {"source": "same", "target": "first", "speaker": "A", "context_prev": "before", "context_next": "after", "file_source": "chapter", "provenance": [["origin", "one"]]},
            {"source": "same", "target": "second", "speaker": "B", "provenance": [["origin", "two"]]},
            {"source": "other", "target": "third"},
        ]
        self.payload.write_text("".join(json.dumps(row) + "\n" for row in rows))
        digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        final = []
        result = self.service.apply_snapshot_payload(self.payload, self.identity.resource_id, expected_digest=digest, expected_record_count=3, initial=True, owner_finalize=lambda report, proof: final.append(proof))
        self.assertIs(type(result), MigrationReport, result)
        self.assertEqual(final[0].record_count, 3)
        snapshot = storage.SQLiteTMStore.from_coordinator(self.owner).capture_export_snapshot()
        self.assertEqual([item.record.target_raw for item in snapshot.records], ["first", "second", "third"])
        self.assertEqual(snapshot.records[0].record.context_prev_raw, "before")
        self.assertEqual(snapshot.records[1].record.provenance, (("origin", "two"),))

    def test_final_reader_failure_keeps_publication_and_suppresses_finalize(self):
        self.source.unlink()
        final = []
        with patch.object(storage.SQLiteTMStore, "capture_export_snapshot", side_effect=sqlite3.OperationalError("reader fault")):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=True, owner_finalize=lambda *_: final.append(True))
        self.assertEqual(caught.exception.error_code, "IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(final, [])
        self.assertEqual(self.source.read_bytes(), self.payload.read_bytes())
        self.assertTrue(storage.SQLiteTMStore.from_coordinator(self.owner).health().healthy)

    def test_commit_error_finishes_source_but_never_runs_finalize(self):
        self.source.unlink()
        primary = RuntimeError("registry error")
        terminals = []
        final = []
        original = PendingPublication.terminal_reproof
        def terminal(pending):
            if pending.preliminary_facts().content_sha256.hex() == self.digest:
                terminals.append(True)
            return original(pending)
        def commit(report):
            raise primary
        with patch.object(PendingPublication, "terminal_reproof", terminal):
            with self.assertRaises(RuntimeError) as caught:
                self.apply(initial=True, owner_commit=commit, owner_finalize=lambda *_: final.append(True))
        self.assertIs(caught.exception, primary)
        self.assertEqual(terminals, [True])
        self.assertEqual(final, [])


@unittest.skipUnless(sys.platform == "win32", "Windows 原生 owner/final reader")
class WindowsSnapshotApplyOwnerLifecycleTests(_SnapshotApplyFixture):
    def temporary_directory(self) -> AbstractContextManager[str]:
        return worker_temporary_directory()

    def test_native_create_replace_final_reader_keeps_single_w1(self):
        from platform_fs_windows import WindowsPlatformAdapter
        self.source.unlink()
        acquisitions = []
        proofs = []
        real_acquire = WindowsPlatformAdapter.acquire
        def acquire(adapter, root, *args, **kwargs):
            acquisitions.append(root)
            return real_acquire(adapter, root, *args, **kwargs)
        def finalize(report, proof):
            reservation = self.service._active_owner_reservation
            assert reservation is not None
            self.assertIsNone(reservation._family_borrow)
            self.assertIsNone(self.owner._family)
            reservation.reprove()
            self.assertEqual(proof.payload_digest, self.digest)
            self.assertEqual(proof.record_count, 1)
            proofs.append(proof)
        with patch.object(WindowsPlatformAdapter, "acquire", acquire):
            first = self.apply(initial=True, owner_finalize=finalize)
        self.assertIs(type(first), MigrationReport, first)
        self.assertEqual(len(acquisitions), 1)
        acquisitions.clear()
        store = storage.SQLiteTMStore.from_coordinator(self.owner)
        with patch.object(WindowsPlatformAdapter, "acquire", acquire):
            second = self.apply(initial=False, expected_destination_baseline=self.baseline(store), owner_finalize=finalize)
        self.assertIs(type(second), MigrationReport, second)
        self.assertEqual(len(acquisitions), 1)
        self.assertEqual([proof.generation for proof in proofs], [0, 1])
        self.assertEqual(proofs[0].semantic_digest, proofs[1].semantic_digest)


if __name__ == "__main__":
    unittest.main()
