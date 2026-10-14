"""Core 10.5/10.6a/10.9：取消后的原 source 与冷开权威。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import tm_activation_journal as journal
import tm_sqlite_store as storage
import tm_migration as migration
from platform_fs_contracts import BoundDirectoryAuthority, LockLease, PendingPublication, PlatformFileError, PlatformFileErrorCode
from tm_contracts import TMRecordDraft, ExportReport
from tm_contracts import AssetPreservationState, MigrationFailure, SourceBindingState
from tests.test_tm_snapshot_apply_owner_lifecycle import _SnapshotApplyFixture


@unittest.skipIf(sys.platform == "win32", "POSIX snapshot source handoff")
class SnapshotCancelledSourceTests(_SnapshotApplyFixture):
    def cold(self, *, append: bool) -> dict[str, object]:
        code = '''
import json,sys
from pathlib import Path
from tm_engine import TMEngine
from tm_contracts import TMRecordDraft, ExportReport
engine=TMEngine(str(Path(sys.argv[1])))
store=engine.canonical_store
assert store is not None
with store.query_lease() as view:
    prior=[r.target_raw for r in view.exact_records("prior")]
    incoming=[r.target_raw for r in view.exact_records("incoming")]
if sys.argv[2]=="yes":
    store.append(TMRecordDraft(source_raw="after failure",target_raw="保留",speaker_raw=None,context_prev_raw=None,context_next_raw=None,file_source=None,provenance=()))
with store.query_lease() as view:
    appended=[r.target_raw for r in view.exact_records("after failure")]
print(json.dumps(dict(prior=prior,incoming=incoming,appended=appended,generation=store.canonical_revision().generation)))
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.source), "yes" if append else "no"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def check_cancelled(self, method: str, *, baseline: str = "completed") -> None:
        store = self.activate()
        if baseline == "appended":
            store.append(TMRecordDraft(source_raw="legal append", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
        elif baseline == "diverged":
            self.source.write_text('{"source":"external","target":"发生分歧"}\n', encoding="utf-8")
        elif baseline == "refreshed":
            store.append(TMRecordDraft(source_raw="before refresh", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
            self.assertIs(type(self.service.refresh_configured_snapshot(store)), ExportReport)
        elif baseline in {"cancelled", "cancelled-refreshed"}:
            with patch.object(storage.ResourceStoreCoordinator, "publish_activation", side_effect=OSError("prior cancellation")):
                result = self.service.import_snapshot(self.source, self.identity.resource_id)
            self.assertIs(type(result), MigrationFailure)
            if baseline == "cancelled-refreshed":
                store.append(TMRecordDraft(source_raw="before legal refresh", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
                self.assertIs(type(self.service.refresh_configured_snapshot(store)), ExportReport)
        elif baseline in {"no-record", "no-record-diverged"}:
            journal._activation_journal_path(self.identity).unlink()
            if baseline == "no-record-diverged":
                self.source.write_text('{"source":"external","target":"外部正常编辑"}\n', encoding="utf-8")
                self.assertIs(store.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
        if baseline in {"cancelled-refreshed", "no-record-diverged"}:
            with store.query_lease() as view:
                self.assertEqual([r.target_raw for r in view.exact_records("prior")], ["旧译文"])
            store.append(TMRecordDraft(source_raw="after prior change", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
        before = self.source.read_bytes()
        source_identity = (self.source.stat().st_dev, self.source.stat().st_ino)
        database = self.identity.canonical_sidecar_path
        database_before = database.read_bytes()
        cancelled_original = []
        real_cancel = storage.ResourceStoreCoordinator.cancel_prepared_activation
        def cancel(owner, preparation, **kwargs):
            real_cancel(owner, preparation, **kwargs)
            terminal_path = journal._activation_terminal_path(self.identity)
            stat = terminal_path.stat()
            cancelled_original.append((stat.st_dev, stat.st_ino, terminal_path.read_bytes()))
        with patch.object(storage.ResourceStoreCoordinator, method, side_effect=OSError("ordinary publication failure")) as injected, patch.object(storage.ResourceStoreCoordinator, "cancel_prepared_activation", cancel):
            outcome = self.apply(initial=False)
        self.assertEqual(injected.call_count, 1)
        assert type(outcome) is MigrationFailure
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual((self.source.stat().st_dev, self.source.stat().st_ino), source_identity)
        self.assertEqual(database.read_bytes(), database_before)
        self.assertEqual((outcome.stage, outcome.error_code, outcome.retryable,
                          outcome.active_generation, outcome.diagnostics, outcome.recovery_locators),
                         ("ACTIVATION", "IMPORT.FAILED", False, 0, (), ()))
        self.assertEqual(outcome.original_source_preservation.state, AssetPreservationState.VERIFIED_UNCHANGED)
        self.assertEqual(outcome.original_source_preservation.before_digest, hashlib.sha256(before).hexdigest())
        self.assertEqual(outcome.original_source_preservation.observed_digest, hashlib.sha256(before).hexdigest())
        self.assertEqual(outcome.active_store_preservation.state, AssetPreservationState.VERIFIED_UNCHANGED)
        self.assertEqual(outcome.active_store_preservation.before_digest, hashlib.sha256(database_before).hexdigest())
        self.assertEqual(outcome.active_store_preservation.observed_digest, hashlib.sha256(database_before).hexdigest())
        self.assertFalse(outcome.canonical_authority_published)
        self.assertFalse(outcome.canonical_authority_ambiguous)
        self.assertFalse(self.family._recovery_required)
        if baseline == "no-record-diverged":
            self.assertIs(store.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
        terminal = journal._activation_terminal_path(self.identity)
        terminal_stat = terminal.stat()
        self.assertEqual(cancelled_original, [(terminal_stat.st_dev, terminal_stat.st_ino, terminal.read_bytes())])
        self.assertFalse(journal._activation_journal_path(self.identity).exists())
        cancelled = journal._parse_activation_journal_bytes(terminal.read_bytes(), expected_journal_path=journal._activation_journal_path(self.identity))
        for _ in range(2):
            replay = self.owner.recover_durable_activation()
            assert replay is not None
            self.assertEqual((replay.action, replay.generation), ("CANCELLED", 0))
            self.assertEqual(cancelled_original, [(terminal.stat().st_dev, terminal.stat().st_ino, terminal.read_bytes())])
        self.assertEqual(cancelled.source_jsonl_digest, self.digest)
        self.assertEqual(cancelled.phase, journal._ActivationJournalPhase.PREPARED)
        for append in (True, False):
            self.assertEqual(self.cold(append=append), dict(prior=["旧译文"], incoming=[], appended=["保留"], generation=0))

    def test_prepared_publication_oserror_restores_source_and_cold_authority(self):
        self.check_cancelled("publish_prepared_activation")

    def test_activation_publication_oserror_restores_source_and_cold_authority(self):
        self.check_cancelled("publish_activation")

    def test_legally_appended_prior_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="appended")

    def test_source_diverged_prior_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="diverged")

    def test_cancelled_baseline_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="cancelled")

    def test_no_record_baseline_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="no-record")

    def test_cancelled_then_legal_refresh_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="cancelled-refreshed")

    def test_no_record_source_diverged_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="no-record-diverged")

    def test_pre_owner_candidate_failure_returns_same_source(self):
        self.activate()
        before = (self.source.stat().st_ino, self.source.read_bytes())
        with patch.object(migration, "_bound_candidate_from_opened", side_effect=OSError("source candidate failure")):
            with self.assertRaises(OSError):
                self.apply(initial=False)
        self.assertTrue(self.source.exists())
        self.assertEqual((self.source.stat().st_ino, self.source.read_bytes()), before)

    def test_source_publication_failure_returns_same_source(self):
        self.activate()
        before = (self.source.stat().st_ino, self.source.read_bytes())
        real = BoundDirectoryAuthority.begin_publish
        fired = []
        def publish(parent, candidate, destination, **kwargs):
            if destination == self.source.name and not fired:
                fired.append(True)
                raise OSError("source publication failure")
            return real(parent, candidate, destination, **kwargs)
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish):
            with self.assertRaises(OSError):
                self.apply(initial=False)
        self.assertEqual((self.source.stat().st_ino, self.source.read_bytes()), before)

    def test_source_forward_and_return_platform_errors_keep_recovery_facts(self):
        from platform_fs_posix import _PosixRootedDirectory

        store = self.activate()
        before = (self.source.stat().st_dev, self.source.stat().st_ino, self.source.read_bytes())
        database_before = self.identity.canonical_sidecar_path.read_bytes()
        real_rename = _PosixRootedDirectory._rename_candidate
        failures = []

        def fail_source(parent, candidate, destination, **kwargs):
            if destination == self.source.name:
                reservation = self.service._active_owner_reservation
                assert reservation is not None
                reservation.reprove()
                lease = reservation._lease
                assert lease is not None
                self.assertFalse(lease.closed)
                error = PlatformFileError(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True)
                failures.append(error)
                raise error
            return real_rename(parent, candidate, destination, **kwargs)

        with patch.object(_PosixRootedDirectory, "_rename_candidate", fail_source):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=False)
        self.assertEqual(len(failures), 2)
        failure = caught.exception
        self.assertEqual(failure.error_code, "IMPORT.RECOVERY_REQUIRED")
        carrier = failure.__cause__
        self.assertIs(type(carrier), journal.ActivationPreparationError)
        assert isinstance(carrier, journal.ActivationPreparationError)
        self.assertEqual(carrier.code, "ACTIVATION.RECOVERY_REQUIRED")
        self.assertEqual(carrier.reason_code, failures[0].code)
        self.assertIs(carrier.__cause__, failures[0])
        self.assertFalse(getattr(failures[0], "__notes__", ()))
        self.assertIn("Snapshot source/authority return incomplete: PlatformFileError: " + failures[1].code, getattr(carrier, "__notes__", ()))
        facts = getattr(failure, "_snapshot_source_return")
        self.assertIs(facts.primary, failures[0])
        self.assertFalse(facts.source_restored)
        self.assertFalse(facts.current_authority_verified)
        locators = getattr(failure, "recovery_locators")
        self.assertEqual(len(locators), 1)
        locator = locators[0]
        self.assertNotEqual(locator.path, self.source)
        self.assertEqual(locator.asset_kind.value, "ORIGINAL_SOURCE")
        self.assertEqual(locator.expected_digest, hashlib.sha256(before[2]).hexdigest())
        self.assertEqual((locator.path.stat().st_dev, locator.path.stat().st_ino, locator.path.read_bytes()), before)
        self.assertEqual(self.identity.canonical_sidecar_path.read_bytes(), database_before)
        self.assertTrue(self.family._recovery_required)
        self.assertEqual(self.owner.state, "ACTIVATING")
        with self.assertRaises(storage.SQLiteStoreLifecycleError):
            store.health()

    def test_interrupted_finalization_stops_live_family_but_cold_proves_completion(self):
        self.activate()
        before = (self.source.stat().st_ino, self.source.read_bytes())
        with patch.object(storage.ResourceStoreCoordinator, "publish_activation", side_effect=OSError("ordinary publication failure")), patch.object(storage.ResourceStoreCoordinator, "_finish_snapshot_source_return", side_effect=OSError("authority return interrupted")):
            with self.assertRaises(migration.MigrationPreflightError):
                self.apply(initial=False)
        self.assertEqual((self.source.stat().st_ino, self.source.read_bytes()), before)
        self.assertTrue(self.family._recovery_required)
        self.assertEqual(self.owner.state, "ACTIVATING")
        for append in (True, False):
            self.assertEqual(self.cold(append=append), dict(prior=["旧译文"], incoming=[], appended=["保留"], generation=0))

    def test_refreshed_prior_keeps_current_authority(self):
        self.check_cancelled("publish_activation", baseline="refreshed")

    def test_outer_close_failure_keeps_restored_facts_and_original_error(self):
        self.activate()
        before = (self.source.stat().st_ino, self.source.read_bytes())
        original = OSError("ordinary publication failure")
        real_close = LockLease.close
        def close(lease):
            real_close(lease)
            raise OSError("outer close failed")
        with patch.object(storage.ResourceStoreCoordinator,"publish_activation",side_effect=original), patch.object(LockLease,"close",close):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=False)
        self.assertEqual(caught.exception.error_code, "IMPORT.RECOVERY_REQUIRED")
        self.assertIs(caught.exception.__cause__, original)
        facts = getattr(caught.exception, "_snapshot_source_return")
        self.assertTrue(facts.source_restored)
        self.assertTrue(facts.current_authority_verified)
        self.assertEqual(getattr(caught.exception, "_verified_snapshot_outcome").original_source_preservation.before_digest, hashlib.sha256(before[1]).hexdigest())
        self.assertEqual((self.source.stat().st_ino, self.source.read_bytes()), before)
        self.assertTrue(self.family._recovery_required)
        self.assertEqual(self.cold(append=True)["generation"], 0)
        self.assertEqual(self.cold(append=False)["incoming"], [])

    def test_source_pending_close_failure_retains_original_recovery_object(self):
        self.activate()
        before = (self.source.stat().st_ino, self.source.read_bytes())
        names = {}
        real_publish = BoundDirectoryAuthority.begin_publish
        real_close = PendingPublication.close
        fired = []
        def publish(parent, candidate, destination, **kwargs):
            pending = real_publish(parent, candidate, destination, **kwargs)
            names[pending] = destination
            return pending
        def close(pending):
            real_close(pending)
            if names.get(pending) == self.source.name and not fired:
                fired.append(pending)
                raise OSError("source pending close failed")
        with patch.object(storage.ResourceStoreCoordinator,"publish_activation",side_effect=OSError("ordinary publication failure")), patch.object(BoundDirectoryAuthority,"begin_publish",publish), patch.object(PendingPublication,"close",close):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=False)
        locator = getattr(caught.exception, "recovery_locators")[0]
        self.assertEqual((locator.path.stat().st_ino, locator.path.read_bytes()), before)
        self.assertTrue(self.family._pending_activation_closes)
        self.assertTrue(self.family._recovery_required)
        self.assertFalse(getattr(caught.exception, "_snapshot_source_return").current_authority_verified)

    def test_returned_source_close_failure_keeps_exact_locator_and_owned_retry(self):
        store = self.activate()
        before = (self.source.stat().st_dev, self.source.stat().st_ino, self.source.read_bytes())
        original = OSError("ordinary publication failure")
        real_publish = BoundDirectoryAuthority.begin_publish
        real_close = PendingPublication.close
        source_pendings = []
        return_close_attempts = []

        def publish(parent, candidate, destination, **kwargs):
            pending = real_publish(parent, candidate, destination, **kwargs)
            if destination == self.source.name:
                source_pendings.append(pending)
            return pending

        def close(pending):
            real_close(pending)
            if len(source_pendings) == 2 and pending is source_pendings[1]:
                return_close_attempts.append(pending)
                if len(return_close_attempts) == 1:
                    raise OSError("returned source pending close failed")

        with patch.object(storage.ResourceStoreCoordinator, "publish_activation", side_effect=original), patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(PendingPublication, "close", close):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.apply(initial=False)
            self.assertEqual(caught.exception.error_code, "IMPORT.RECOVERY_REQUIRED")
            self.assertIs(caught.exception.__cause__, original)
            self.assertEqual(len(source_pendings), 2)
            self.assertEqual(return_close_attempts, [source_pendings[1]])
            self.assertEqual((self.source.stat().st_dev, self.source.stat().st_ino, self.source.read_bytes()), before)
            self.assertEqual(len(self.family._pending_activation_closes), 1)
            self.assertTrue(self.family._recovery_required)
            self.assertEqual(self.owner.state, "ACTIVATING")
            facts = getattr(caught.exception, "_snapshot_source_return")
            self.assertFalse(facts.current_authority_verified)
            locators = getattr(caught.exception, "recovery_locators")
            self.assertEqual(len(locators), 1)
            locator = locators[0]
            self.assertEqual(locator.path, self.source)
            self.assertEqual(locator.asset_kind.value, "ORIGINAL_SOURCE")
            self.assertEqual(locator.expected_digest, hashlib.sha256(before[2]).hexdigest())
            self.assertEqual((locator.path.stat().st_dev, locator.path.stat().st_ino, locator.path.read_bytes()), before)
            self.assertTrue(facts.source_restored)
            with self.assertRaises(storage.SQLiteStoreLifecycleError):
                store.health()
            replay = self.owner.recover_durable_activation()
            assert replay is not None
            self.assertEqual((replay.action, replay.generation), ("CANCELLED", 0))
            self.assertEqual(return_close_attempts, [source_pendings[1], source_pendings[1]])
            self.assertFalse(self.family._pending_activation_closes)
            self.assertFalse(self.family._recovery_required)
            with store.query_lease() as view:
                self.assertEqual([r.target_raw for r in view.exact_records("prior")], ["旧译文"])

    def test_real_journal_temporary_keeps_cold_recovery_stopped(self):
        self.activate()
        with patch.object(storage.ResourceStoreCoordinator, "publish_activation", side_effect=OSError("ordinary publication failure")):
            self.assertIs(type(self.apply(initial=False)), MigrationFailure)
        before = (self.source.stat().st_ino, self.source.read_bytes())
        terminal = journal._activation_terminal_path(self.identity)
        cancelled = (terminal.stat().st_ino, terminal.read_bytes())
        temporary = journal._activation_journal_temp_path(journal._activation_journal_path(self.identity))
        temporary.write_bytes(b"unfinished journal publication")
        for _ in range(2):
            result = subprocess.run([sys.executable, "-B", "-c", "from tm_engine import TMEngine; import sys; TMEngine(sys.argv[1])", str(self.source)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(temporary.read_bytes(), b"unfinished journal publication")
            self.assertEqual((terminal.stat().st_ino, terminal.read_bytes()), cancelled)
            self.assertEqual((self.source.stat().st_ino, self.source.read_bytes()), before)

    def test_cancelled_then_atomic_source_save_keeps_current_authority(self):
        store = self.activate()
        with patch.object(storage.ResourceStoreCoordinator, "publish_activation", side_effect=OSError("ordinary publication failure")):
            self.assertIs(type(self.apply(initial=False)), MigrationFailure)
        terminal = journal._activation_terminal_path(self.identity)
        cancelled = (terminal.stat().st_ino, terminal.read_bytes())
        original_inode = self.source.stat().st_ino
        replacement = self.source.with_suffix(".atomic-save")
        replacement.write_text('{"source":"external","target":"安全替换保存"}\n', encoding="utf-8")
        os.replace(replacement, self.source)
        self.assertNotEqual(self.source.stat().st_ino, original_inode)
        self.assertIs(store.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
        with store.query_lease() as view:
            self.assertEqual([r.target_raw for r in view.exact_records("prior")], ["旧译文"])
        store.append(TMRecordDraft(source_raw="after atomic save", target_raw="保留", speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))
        for _ in range(2):
            replay = self.owner.recover_durable_activation()
            assert replay is not None
            self.assertEqual((replay.action, replay.generation), ("CANCELLED", 0))
        for append in (True, False):
            self.assertEqual(self.cold(append=append), dict(prior=["旧译文"], incoming=[], appended=["保留"], generation=0))
        self.assertEqual((terminal.stat().st_ino, terminal.read_bytes()), cancelled)
