"""原配置路径的 POSIX v2 导出与独立冷恢复。"""
from __future__ import annotations

from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import shutil
import stat
import tm_migration as migration
import platform_fs_posix
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_tm_initial_activation_publication import SOURCE_BYTES
from tm_contracts import CanonicalResourceIdentity
from tm_migration import TMMigrationService
from tm_contracts import ExportFailure, ExportReport, MigrationReport
from tm_engine import TMEngine
import tm_sqlite_store as storage


@unittest.skipIf(sys.platform == 'win32', 'POSIX v2 导出')
class PosixExportRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)  # 保留平台默认原始配置，Mac 为 /var
        self.root = self.base / 'resource'
        self.root.mkdir()
        self.output = self.base / 'output'
        self.output.mkdir()
        self.destination = self.output / 'copy.jsonl'
        source = self.root / 'primary.jsonl'
        source.write_bytes(SOURCE_BYTES)
        self.identity = CanonicalResourceIdentity.from_configured_jsonl('tm.primary', source)
        self.owner = storage.ResourceStoreCoordinator(resource_identity=self.identity, canonical_store_id='store.primary')
        self.service = TMMigrationService(resource_identity=self.identity, canonical_store_id='store.primary', coordinator=self.owner)
        self.addCleanup(self.owner.close_family_session)
        result = self.service.activate_initial(self.identity.configured_jsonl_path, self.identity.resource_id)
        self.assertIs(type(result), MigrationReport, result)
        self.store = storage.SQLiteTMStore.from_coordinator(self.owner)

    def rows(self):
        operation = (nullcontext() if self.owner._family_closed else self.store._snapshot_operation())
        with operation, closing(sqlite3.connect(self.identity.canonical_sidecar_path)) as db:
            receipts = db.execute("SELECT snapshot_id,status FROM tm_snapshot_receipt WHERE destination_jsonl_path=?", (str(self.destination),)).fetchall()
            handoffs = db.execute("SELECT key,value FROM tm_meta WHERE key LIKE 'artifact_handoff.%'").fetchall()
        return receipts, handoffs

    def cold(self, *, require_sync=False):
        self.owner.close_family_session()
        script = '''
import json,sys
from pathlib import Path
from tm_engine import open_canonical_tm_store
from platform_fs_posix import PosixPlatformAdapter
from tm_sqlite_store import SQLiteTMStore
syncs=[]
original_sync=PosixPlatformAdapter.synchronize_bound_directory
original_clear=SQLiteTMStore.clear_issued_receipt_handoff
def sync(self, parent, lease):
    result=original_sync(self,parent,lease)
    facts=self.observe_bound_directory(parent)
    syncs.append((facts.device,facts.inode))
    return result
def clear(self,*args,**kwargs):
    if len(sys.argv)>2:
        parent=kwargs.get('artifact_parent')
        assert parent is not None and parent.identity in syncs, ('先同步后清除', syncs)
    return original_clear(self,*args,**kwargs)
PosixPlatformAdapter.synchronize_bound_directory=sync
SQLiteTMStore.clear_issued_receipt_handoff=clear
store=open_canonical_tm_store(Path(sys.argv[1]))
assert store is not None
try:
    print(json.dumps({'target':store.exact_records('same')[0].target_raw,'healthy':store.health().healthy}))
finally:
    store.coordinator.close_family_session()
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.identity.configured_jsonl_path), *(['sync'] if require_sync else [])], capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertTrue(observed['healthy'])
        self.assertEqual(observed['target'], 'winner')

    def test_raw_public_query_export_and_independent_cold_open(self):
        engine = TMEngine(str(self.identity.configured_jsonl_path))
        self.assertTrue(engine.canonical_active)
        self.addCleanup(engine.canonical_store.coordinator.close_family_session)
        self.assertEqual(engine.query_exact('same').target, 'winner')
        engine.canonical_store.coordinator.close_family_session()
        outcome = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(outcome), ExportReport, outcome)
        receipts, handoffs = self.rows()
        self.assertEqual([row[1] for row in receipts], ['completed'])
        self.assertFalse(handoffs)
        self.cold()

    def test_issued_after_recovery_record_cold_cancels_original_absence(self):
        original = self.store.record_export_recovery_handoff
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('模拟 issued 后进程中断')
        with patch.object(self.store, 'record_export_recovery_handoff', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.export_jsonl(self.store, self.destination)
        receipts, handoffs = self.rows()
        self.assertEqual([row[1] for row in receipts], ['issued'])
        self.assertEqual(len(handoffs), 1)
        self.cold()
        receipts, handoffs = self.rows()
        self.assertEqual([row[1] for row in receipts], ['cancelled'])
        self.assertFalse(handoffs)
        self.assertFalse(self.destination.exists())

    def test_completed_all_artifacts_absent_cold_syncs_before_clear(self):
        self.destination.write_bytes(b'old output\n')
        complete = self.store.complete_issued_export_receipt
        actual_fsync = platform_fs_posix.os.fsync
        parent_stat = self.output.stat()
        armed, fired = [], []
        def completed(*args, **kwargs):
            result = complete(*args, **kwargs)
            armed.append(True)
            return result
        def failed_sync(descriptor):
            observed = os.fstat(descriptor)
            if (armed and not fired and stat.S_ISDIR(observed.st_mode)
                    and (observed.st_dev, observed.st_ino) == (parent_stat.st_dev, parent_stat.st_ino)
                    and not tuple(self.output.glob('*.bak')) and not tuple(self.output.glob('*.tmp'))):
                fired.append(True)
                raise OSError('unlink 已成功后的真实父目录 fsync 失败')
            return actual_fsync(descriptor)
        with patch.object(self.store, 'complete_issued_export_receipt', completed), patch.object(platform_fs_posix.os, 'fsync', failed_sync):
            outcome = self.service.export_jsonl(self.store, self.destination)
        self.assertEqual(fired, [True])
        self.assertIs(type(outcome), ExportFailure, outcome)
        self.assertTrue(outcome.publication_committed)
        receipts, handoffs = self.rows()
        self.assertEqual([row[1] for row in receipts], ['completed'])
        self.assertEqual(len(handoffs), 1)
        self.assertFalse(tuple(self.output.glob('*.tmp')))
        self.assertFalse(tuple(self.output.glob('*.bak')))
        self.cold(require_sync=True)
        self.assertFalse(self.rows()[1])

    def test_jsonl_named_cold_reconstructs_original_manifest_temp(self):
        original = migration._bound_publish_candidate
        def interrupt(parent, lease, publication, prior, **kwargs):
            result = original(parent, lease, publication, prior, **kwargs)
            if publication.name == self.destination.name:
                raise KeyboardInterrupt('JSONL 命名后，manifest 命名前中断')
            return result
        with patch.object(migration, '_bound_publish_candidate', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.export_jsonl(self.store, self.destination)
        receipt, handoff = self.rows()
        self.assertEqual([row[1] for row in receipt], ['issued'])
        facts = json.loads(handoff[0][1])
        original_inode = (facts['manifest_temp_device'], facts['manifest_temp_inode'])
        self.cold(require_sync=True)
        self.assertEqual([row[1] for row in self.rows()[0]], ['completed'])
        manifest = self.destination.with_name(self.destination.name + '.localcat-snapshot.json').stat()
        self.assertEqual((manifest.st_dev, manifest.st_ino), original_inode)
        self.assertFalse(self.rows()[1])

    def test_export_replaces_existing_pair_then_cold_opens(self):
        for _ in range(2):
            result = self.service.export_jsonl(self.store, self.destination)
            self.assertIs(type(result), ExportReport, result)
        self.assertEqual([row[1] for row in self.rows()[0]], ['completed', 'completed'])
        self.assertFalse(self.rows()[1])
        self.cold()

    def test_raw_configured_refresh_and_cold_manifest_reconstruction(self):
        self.assertIs(type(self.service.refresh_configured_snapshot(self.store)), ExportReport)
        original = migration._bound_publish_candidate
        def interrupt(parent, lease, publication, prior, **kwargs):
            result = original(parent, lease, publication, prior, **kwargs)
            if publication.name == self.identity.configured_jsonl_path.name:
                raise KeyboardInterrupt('原配置 JSONL 已命名')
            return result
        with patch.object(migration, '_bound_publish_candidate', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.refresh_configured_snapshot(self.store)
        self.assertTrue(self.rows()[1])
        self.cold(require_sync=True)
        self.assertFalse(self.rows()[1])

    def test_arbitrary_symlink_output_parent_is_rejected(self):
        alias = self.base / 'linked-output'
        alias.symlink_to(self.output, target_is_directory=True)
        result = self.service.export_jsonl(self.store, alias / self.destination.name)
        self.assertIs(type(result), ExportFailure, result)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.rows()[0])
        self.assertTrue(self.store.health().healthy)

    def test_foreign_manifest_temp_is_preserved_by_cold_recovery(self):
        original = migration._bound_publish_candidate
        def interrupt(parent, lease, publication, prior, **kwargs):
            result = original(parent, lease, publication, prior, **kwargs)
            if publication.name == self.destination.name:
                raise KeyboardInterrupt('JSONL 命名后中断')
            return result
        with patch.object(migration, '_bound_publish_candidate', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.export_jsonl(self.store, self.destination)
        self.owner.close_family_session()
        temporary = self.destination.with_name('.' + self.destination.name + '.localcat-export.manifest.tmp')
        foreign = self.output / 'foreign'
        foreign.write_bytes(temporary.read_bytes())
        foreign.replace(temporary)
        before = {p.name:p.read_bytes() for p in self.output.iterdir() if p.is_file()}
        result = subprocess.run([sys.executable, '-B', '-c',
            'from tm_engine import open_canonical_tm_store; from pathlib import Path; import sys; open_canonical_tm_store(Path(sys.argv[1]))',
            str(self.identity.configured_jsonl_path)], capture_output=True, text=True, timeout=45)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TM.CANONICAL_RECOVERY_FAILED', result.stderr)
        self.assertEqual({p.name:p.read_bytes() for p in self.output.iterdir() if p.is_file()}, before)
        self.assertEqual([row[1] for row in self.rows()[0]], ['issued'])
        self.assertEqual(len(self.rows()[1]), 1)

    def _assert_unpublished_candidate_closed_after_owner_drift(self, boundary):
        real_publish = migration._bound_publish_candidate
        captured = []
        moved = self.base / 'original-source'
        wanted = self.destination.name + ('.localcat-snapshot.json' if boundary == 'manifest' else '')
        self.destination.write_bytes(b'old valid output\n')

        def publish(parent, lease, publication, prior, **kwargs):
            if publication.name == wanted:
                candidate = publication.candidate
                descriptors = (candidate._descriptor, *candidate._directory_fds)
                self.assertFalse(candidate.closed)
                self.assertGreater(len(descriptors), 1)
                for descriptor in descriptors:
                    os.fstat(descriptor)
                captured.append((candidate, descriptors))
                # Runs after every assertion below; never hides a leaked owner.
                self.addCleanup(candidate.close)
                self.root.rename(moved)
                shutil.copytree(moved, self.root)
            return real_publish(parent, lease, publication, prior, **kwargs)

        with patch.object(migration, '_bound_publish_candidate', publish):
            outcome = self.service.export_jsonl(self.store, self.destination)
        self.owner.close_family_session()
        self.assertIs(type(outcome), ExportFailure, outcome)
        self.assertEqual(outcome.error_code, 'EXPORT.RECOVERY_REQUIRED')
        self.assertFalse(outcome.publication_committed)
        self.assertEqual(self.owner.state, 'FAILED')
        self.assertEqual(len(captured), 1)
        candidate, descriptors = captured[0]
        live_descriptors = []
        for descriptor in descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                pass
            else:
                live_descriptors.append(descriptor)
        self.assertEqual((candidate.closed, live_descriptors), (True, []),
                         '未交接 candidate 及其文件/目录链须由原 caller 关闭')
        if boundary == 'jsonl':
            self.assertEqual(self.destination.read_bytes(), b'old valid output\n')
        self.assertFalse(self.destination.with_name(self.destination.name + '.localcat-snapshot.json').exists())
        recovery = self.destination.with_name('.' + self.destination.name + '.localcat-export-recovery.jsonl.bak')
        self.assertEqual(recovery.read_bytes(), b'old valid output\n')
        # The original owner is now closed; inspect the retained old directory,
        # never the same-path copy, and retain the issued recovery facts.
        with closing(sqlite3.connect(moved / self.identity.canonical_sidecar_path.name)) as db:
            self.assertEqual(db.execute('SELECT status FROM tm_snapshot_receipt WHERE destination_jsonl_path=?',
                (str(self.destination),)).fetchall(), [('issued',)])
            self.assertEqual(db.execute("SELECT count(*) FROM tm_meta WHERE key LIKE 'artifact_handoff.%'").fetchone(), (1,))

    def test_jsonl_candidate_closes_when_owner_reproof_fails_before_publish(self):
        self._assert_unpublished_candidate_closed_after_owner_drift('jsonl')

    def test_manifest_candidate_closes_when_owner_reproof_fails_before_publish(self):
        self._assert_unpublished_candidate_closed_after_owner_drift('manifest')
