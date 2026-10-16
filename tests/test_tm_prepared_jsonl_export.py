"""Core 10.10a：真实 canonical 的私有 JSONL pair 生命周期。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import copy
import hashlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_migration as migration
from platform_fs_contracts import BoundDirectoryAuthority, LockLease
from tests.test_tm_initial_activation_publication import _fixture
from tm_contracts import CanonicalResourceIdentity, ExportFailure, ExportReport, MigrationReport
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


class PreparedJSONLExportTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.base: Path
        self.source: Path
        self.output: Path
        self.destination: Path
        self.manifest: Path
        self.identity: CanonicalResourceIdentity
        self.owner: ResourceStoreCoordinator
        self.service: migration.TMMigrationService
        self.store: SQLiteTMStore

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "output"
        self.output.mkdir()
        self.destination = self.output / "private.jsonl"
        self.manifest = migration._export_artifact_paths(self.destination).manifest
        self.identity, self.owner, self.service = _fixture(self.source)
        activated = self.service.activate_initial(self.identity.configured_jsonl_path, self.identity.resource_id)
        self.assertIs(type(activated), MigrationReport, activated)
        self.addCleanup(self.owner.close_family_session)
        self.store = SQLiteTMStore.from_coordinator(self.owner)

    def prepare(self):
        prepared = self.service.prepare_jsonl_export(self.store, self.destination)
        assert isinstance(prepared, migration.PreparedJSONLExport), prepared
        self.assertIsInstance(prepared.report, ExportReport)
        return prepared

    def test_prepare_consumes_same_bytes_and_discard_releases_pair(self):
        prepared = self.prepare()
        self.assertEqual(prepared.report.exported_count, 3)
        self.assertEqual(prepared.report.destination_digest, hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertTrue(self.manifest.is_file())
        if self.owner._family is not None:
            self.assertFalse(self.owner._family._operation_tokens)
        # No source operation remains held while the private pair is consumed.
        self.assertEqual(self.store.canonical_revision().record_count, 3)
        result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertTrue(result.complete, result)
        self.assertEqual(result.removed_names, (self.destination.name, self.manifest.name))
        self.assertEqual(result.retained_names, ())
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.manifest.exists())

    def test_direct_export_pair_remains_and_same_encoder_is_used(self):
        direct = self.output / "direct.jsonl"
        exported = self.service.export_jsonl(self.store, direct)
        self.assertIsInstance(exported, ExportReport)
        prepared = self.prepare()
        self.assertEqual(self.destination.read_bytes(), direct.read_bytes())
        self.assertTrue(self.service.discard_prepared_jsonl_export(prepared).complete)
        self.assertTrue(direct.is_file())
        self.assertTrue(migration._export_artifact_paths(direct).manifest.is_file())

    def test_existing_pair_is_rejected_without_replacement(self):
        original = self.service.export_jsonl(self.store, self.destination)
        self.assertIsInstance(original, ExportReport)
        before = (self.destination.read_bytes(), self.manifest.read_bytes())
        result = self.service.prepare_jsonl_export(self.store, self.destination)
        self.assertIsInstance(result, ExportFailure)
        self.assertEqual((self.destination.read_bytes(), self.manifest.read_bytes()), before)

    def test_existing_body_alone_is_rejected(self):
        self.destination.write_bytes(b"user-owned\n")
        result = self.service.prepare_jsonl_export(self.store, self.destination)
        self.assertIsInstance(result, ExportFailure)
        self.assertEqual(self.destination.read_bytes(), b"user-owned\n")
        self.assertFalse(self.manifest.exists())

    def test_discard_reproves_both_before_any_unlink(self):
        prepared = self.prepare()
        self.manifest.unlink()
        self.manifest.write_bytes(b"foreign companion")
        with patch.object(BoundDirectoryAuthority, "unlink_owned", side_effect=AssertionError("must not unlink")):
            result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertFalse(result.complete)
        self.assertEqual(result.removed_names, ())
        self.assertEqual(result.retained_names, (self.destination.name, self.manifest.name))
        self.assertTrue(self.destination.exists())
        self.assertEqual(self.manifest.read_bytes(), b"foreign companion")

    def test_changed_body_in_same_file_is_not_deleted(self):
        prepared = self.prepare()
        self.destination.write_bytes(b"changed own inode")
        result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertFalse(result.complete)
        self.assertEqual(result.removed_names, ())
        self.assertEqual(self.destination.read_bytes(), b"changed own inode")
        self.assertTrue(self.manifest.exists())

    def test_successful_publication_identity_is_not_replaced_by_same_byte_copy(self):
        publish = self.service._publish_bound_export_snapshot
        moved = self.output / "published-original.jsonl"
        def swap(*args, **kwargs):
            outcome = publish(*args, **kwargs)
            self.assertIsInstance(outcome, ExportReport)
            if sys.platform == "win32":
                parent = kwargs["parent"]
                pair = (self.destination, self.manifest)
                before_bytes = tuple(path.read_bytes() for path in pair)
                before_entries = tuple(parent.inspect_entry(path.name) for path in pair)
                self.assertTrue(all(entry is not None for entry in before_entries))
                # The live Windows directory binding rejects this external rename.
                with self.assertRaises(PermissionError) as denied:
                    self.destination.rename(moved)
                self.assertEqual(denied.exception.winerror, 32)
                self.assertEqual(tuple(path.read_bytes() for path in pair), before_bytes)
                self.assertEqual(tuple(parent.inspect_entry(path.name) for path in pair), before_entries)
                self.assertFalse(moved.exists())
                return outcome
            self.destination.rename(moved)
            shutil.copyfile(moved, self.destination)
            return outcome
        with patch.object(self.service, "_publish_bound_export_snapshot", swap):
            result = self.service.prepare_jsonl_export(self.store, self.destination)
        if sys.platform == "win32":
            self.assertIsInstance(result, migration.PreparedJSONLExport)
            assert isinstance(result, migration.PreparedJSONLExport)
            state = result._state
            assert state is not None
            discarded = self.service.discard_prepared_jsonl_export(result)
            self.assertTrue(discarded.complete, discarded)
            self.assertEqual(discarded.removed_names, (self.destination.name, self.manifest.name))
            self.assertEqual(discarded.retained_names, ())
            self.assertFalse(self.destination.exists())
            self.assertFalse(self.manifest.exists())
            self.assertFalse(moved.exists())
            self.assertTrue(all(authority.closed for authority in (state.lease, state.parent, state.root)))
            return
        self.assertIsInstance(result, ExportFailure)
        assert isinstance(result, ExportFailure)
        self.assertTrue(result.publication_committed)
        self.assertEqual(moved.read_bytes(), self.destination.read_bytes())
        self.assertTrue(self.manifest.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX 外部目录替换的可观察拒绝")
    def test_discard_observed_directory_drift_preserves_both_locations(self):
        prepared = self.prepare()
        old = self.base / "output-old"
        self.output.rename(old)
        shutil.copytree(old, self.output)
        result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertFalse(result.complete)
        for directory in (old, self.output):
            self.assertTrue((directory / self.destination.name).exists())
            self.assertTrue((directory / self.manifest.name).exists())

    def test_second_unlink_failure_reports_partial_and_repeat_does_not_retry(self):
        prepared = self.prepare()
        real_unlink = BoundDirectoryAuthority.unlink_owned
        calls = []
        def unlink(parent, name, identity):
            calls.append(name)
            if name == self.manifest.name:
                raise OSError("second file busy")
            return real_unlink(parent, name, identity)
        with patch.object(BoundDirectoryAuthority, "unlink_owned", unlink):
            result = self.service.discard_prepared_jsonl_export(prepared)
            repeated = self.service.discard_prepared_jsonl_export(prepared)
        self.assertIs(repeated, result)
        self.assertFalse(result.complete)
        self.assertEqual(result.removed_names, (self.destination.name,))
        self.assertEqual(result.retained_names, (self.manifest.name,))
        self.assertEqual(calls, [self.destination.name, self.manifest.name])
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.manifest.exists())

    def test_repeat_success_does_not_reopen_or_unlink(self):
        prepared = self.prepare()
        result = self.service.discard_prepared_jsonl_export(prepared)
        with patch.object(BoundDirectoryAuthority, "unlink_owned", side_effect=AssertionError("repeat mutation")):
            self.assertIs(self.service.discard_prepared_jsonl_export(prepared), result)

    def test_unlink_then_sync_failure_reports_observed_absence(self):
        prepared = self.prepare()
        state = prepared._state
        assert state is not None
        real_unlink = BoundDirectoryAuthority.unlink_owned
        def unlink(parent, name, identity):
            real_unlink(parent, name, identity)
            raise OSError("post-unlink synchronization failed")
        with patch.object(BoundDirectoryAuthority, "unlink_owned", unlink):
            result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertFalse(result.complete)
        self.assertEqual(result.removed_names, (self.destination.name,))
        self.assertEqual(result.retained_names, (self.manifest.name,))
        self.assertTrue(all(authority.closed for authority in (state.lease, state.parent, state.root)))

    def test_unknown_and_foreign_owner_cannot_discard(self):
        prepared = self.prepare()
        other = migration.TMMigrationService(resource_identity=self.identity, canonical_store_id=self.service.canonical_store_id)
        with self.assertRaises(ValueError):
            other.discard_prepared_jsonl_export(prepared)
        with self.assertRaises(TypeError):
            getattr(self.service, "discard_prepared_jsonl_export")(prepared.report)
        with self.assertRaises(TypeError):
            copy.copy(prepared)
        with self.assertRaises(AttributeError):
            setattr(prepared, "report", prepared.report)
        self.assertTrue(self.destination.exists())
        self.assertTrue(self.service.discard_prepared_jsonl_export(prepared).complete)

    def test_close_failure_does_not_skip_other_authority_closes(self):
        prepared = self.prepare()
        state = prepared._state
        assert state is not None
        real_close = LockLease.close
        closed = []
        def close(lease):
            real_close(lease)
            closed.append(lease)
            raise OSError("lease close failed")
        with patch.object(LockLease, "close", close):
            result = self.service.discard_prepared_jsonl_export(prepared)
        self.assertFalse(result.complete)
        self.assertEqual(result.retained_names, ())
        self.assertIn("EXPORT.STAGE.AUTHORITY_CLOSE_FAILED", result.diagnostic_codes)
        self.assertEqual(len(closed), 1)
        self.assertTrue(all(authority.closed for authority in (state.lease, state.parent, state.root)))
        # Reacquisition and a new export prove the old destination lease ended.
        self.assertIsInstance(self.service.export_jsonl(self.store, self.destination), ExportReport)

    @unittest.skipIf(sys.platform == "win32", "POSIX 来源 operation terminal")
    def test_source_terminal_failure_returns_failure_and_preserves_committed_pair(self):
        family = self.owner._family
        assert family is not None
        real_operation = family.operation
        real_close = LockLease.close
        source_leases = []
        retained = []
        real_export = self.service._run_bound_arbitrary_export
        def export(*args, **kwargs):
            result = real_export(*args, **kwargs)
            retained.extend(kwargs["retained_private"])
            return result
        @contextmanager
        def operation(*args, **kwargs):
            with real_operation(*args, **kwargs) as borrow:
                source_leases.append(borrow._lease)
                yield borrow
        def close(lease):
            real_close(lease)
            if source_leases and lease is source_leases[0]:
                raise OSError("source terminal failed")
        with patch.object(family, "operation", operation), patch.object(LockLease, "close", close), patch.object(self.service, "_run_bound_arbitrary_export", export):
            result = self.service.prepare_jsonl_export(self.store, self.destination)
        self.assertIsInstance(result, ExportFailure)
        assert isinstance(result, ExportFailure)
        self.assertTrue(result.publication_committed)
        self.assertTrue(self.destination.exists())
        self.assertTrue(self.manifest.exists())
        self.assertFalse(family._operation_tokens)
        self.assertEqual(len(retained), 1)
        state = retained[0]
        self.assertTrue(all(authority.closed for authority in (state.lease, state.parent, state.root)))


if __name__ == "__main__":
    unittest.main()
