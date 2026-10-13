"""Core 10.10：真实 canonical 的 snapshot、receipt 与发布生命周期。"""
from __future__ import annotations

from contextlib import contextmanager, closing
from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_migration as migration
import tm_sqlite_store as storage
from tests.test_tm_initial_activation_publication import _fixture
from tm_contracts import CanonicalResourceIdentity, ExportFailure, ExportReport, MigrationReport, TMRecordDraft


@unittest.skipIf(sys.platform == "win32", "POSIX 家族生命周期")
class PosixSnapshotLifecycleTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.base: Path
        self.root: Path
        self.output: Path
        self.destination: Path
        self.identity: CanonicalResourceIdentity
        self.owner: storage.ResourceStoreCoordinator
        self.service: migration.TMMigrationService
        self.store: storage.SQLiteTMStore
        self.family: storage._ResourceFamilySession

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "resource"
        self.root.mkdir()
        self.output = self.base / "output"
        self.output.mkdir()
        self.destination = self.output / "snapshot.jsonl"
        self.identity, self.owner, self.service = _fixture(self.root)
        result = self.service.activate_initial(self.identity.configured_jsonl_path, self.identity.resource_id)
        self.assertIs(type(result), MigrationReport, result)
        self.store = storage.SQLiteTMStore.from_coordinator(self.owner)
        self.addCleanup(self.owner.close_family_session)
        assert self.owner._family is not None
        self.family = self.owner._family

    def test_real_export_and_refresh_keep_one_family_operation_through_publication(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                family = self.family
                original_root = family._root
                real_operation = family.operation
                seam_name = '_bound_publish_candidate'
                real_replace = getattr(migration, seam_name)
                tokens, publications = [], []
                @contextmanager
                def operation(*args, **kwargs):
                    with real_operation(*args, **kwargs) as borrow:
                        tokens.append(borrow._token)
                        yield borrow
                def replace(*args, **kwargs):
                    publications.append(tuple(family._operation_tokens.values()))
                    self.assertIs(family._root, original_root)
                    return real_replace(*args, **kwargs)
                with patch.object(family, "operation", operation), patch.object(migration, seam_name, replace):
                    result = (self.service.refresh_configured_snapshot(self.store) if configured
                              else self.service.export_jsonl(self.store, self.destination))
                self.assertIs(type(result), ExportReport, result)
                self.assertEqual(len(tokens), 1, "快照、receipt、发布和收尾应共享一次家族 operation")
                self.assertEqual(publications, [(tokens[0],), (tokens[0],)])
                path = self.identity.configured_jsonl_path if configured else self.destination
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([row['target'] for row in rows], ['first', 'winner', 'value'])
                self.assertFalse(family._operation_tokens)

    def test_source_directory_drift_before_destination_mutation_preserves_old_output(self):
        self.destination.write_bytes(b'old valid output\n')
        real_seam = migration._bound_publish_candidate
        moved = []
        def seam(*args, **kwargs):
            if not moved:
                original = self.base / 'original'
                self.root.rename(original)
                shutil.copytree(original, self.root)
                moved.append(original)
            return real_seam(*args, **kwargs)
        with patch.object(migration, '_bound_publish_candidate', seam):
            result = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(result), ExportFailure, result)
        assert isinstance(result, ExportFailure)
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.destination.read_bytes(), b'old valid output\n')
        self.assertEqual(self.owner.state, 'FAILED')
        with self.assertRaises(storage.SQLiteStoreLifecycleError):
            self.store.append(TMRecordDraft(source_raw='stop', target_raw='停止', speaker_raw=None, context_prev_raw=None, context_next_raw=None, file_source=None, provenance=()))

    def test_recovery_noop_uses_one_original_family_operation(self):
        family = self.family
        real_operation = family.operation
        tokens = []
        @contextmanager
        def operation(*args, **kwargs):
            with real_operation(*args, **kwargs) as borrow:
                tokens.append(borrow._token)
                yield borrow
        with patch.object(family, 'operation', operation):
            result = self.service.recover_configured_refresh(self.store)
        self.assertEqual(result.state.value, 'NOOP', result)
        self.assertEqual(len(tokens), 1)

    def test_diverged_canonical_can_export_without_rebinding_or_source_mutation(self):
        from tm_contracts import SourceBindingState
        before = self.store.source_binding_monitor.observe()
        changed = b'{"source":"outside","target":"untrusted"}\n'
        self.identity.configured_jsonl_path.write_bytes(changed)
        diverged = self.store.source_binding_monitor.observe()
        self.assertIs(diverged.state, SourceBindingState.SOURCE_DIVERGED)
        self.store.append(_draft('new canonical', '新译文'))
        result = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(result), ExportReport, result)
        after = self.store.source_binding_monitor.observe()
        self.assertIs(after.state, SourceBindingState.SOURCE_DIVERGED)
        self.assertEqual(after.binding_digest, before.binding_digest)
        self.assertEqual(self.identity.configured_jsonl_path.read_bytes(), changed)
        self.assertEqual(len(self.destination.read_text().splitlines()), 4)

    def test_destination_directory_drift_does_not_replace_source_authority(self):
        self.destination.write_bytes(b'old valid output\n')
        real_seam = migration._bound_publish_candidate
        moved = []
        def seam(*args, **kwargs):
            if not moved:
                original = self.base / 'old-output'
                self.output.rename(original)
                shutil.copytree(original, self.output)
                moved.append(original)
            return real_seam(*args, **kwargs)
        with patch.object(migration, '_bound_publish_candidate', seam):
            result = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(result), ExportFailure, result)
        assert isinstance(result, ExportFailure)
        self.assertEqual(self.destination.read_bytes(), b'old valid output\n')
        self.assertEqual((moved[0] / self.destination.name).read_bytes(), b'old valid output\n')
        self.assertEqual(self.owner.current_generation, 0)
        self.assertEqual([r.target_raw for r in self.store.exact_records('same')], ['winner', 'first'])

    def test_outer_lease_close_failure_keeps_committed_publication_fact(self):
        from platform_fs_contracts import LockLease
        import hashlib
        self.destination.write_bytes(b'old valid output\n')
        before = hashlib.sha256(self.destination.read_bytes()).hexdigest()
        real_close = LockLease.close
        fired = []
        selected_lease = []
        real_operation = self.family.operation
        @contextmanager
        def operation(*args, **kwargs):
            with real_operation(*args, **kwargs) as borrow:
                selected_lease.append(borrow._lease)
                yield borrow
        def close(lease):
            real_close(lease)
            if not fired and self.family._operation_tokens and lease is selected_lease[0]:
                fired.append(True)
                raise OSError('outer W1 close failed after publication')
        with patch.object(LockLease, 'close', close), patch.object(self.family, 'operation', operation):
            result = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(result), ExportFailure, result)
        assert isinstance(result, ExportFailure)
        self.assertTrue(result.publication_committed)
        self.assertFalse(result.publication_commit_ambiguous)
        self.assertFalse(result.recovery_locators)
        self.assertEqual(result.previous_destination_preservation.before_digest, before)
        self.assertEqual(result.previous_destination_preservation.observed_digest,
                         hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertEqual(self.owner.state, 'FAILED')

    def test_real_process_death_and_fresh_cold_open_complete_snapshot_protocol(self):
        import subprocess
        import sqlite3
        from tm_engine import open_canonical_tm_store
        # Create a real old configured receipt, then move the canonical head.
        self.store.append(_draft('new canonical', '新译文'))
        self.owner.close_family_session()
        for configured in (False, True):
            for boundary in ('issued', 'jsonl', 'manifest', 'completed'):
                with self.subTest(configured=configured, boundary=boundary):
                    result = subprocess.run([
                        sys.executable, '-B', '-c',
                        'from tests.test_tm_posix_snapshot_lifecycle import _crash_snapshot_worker; '
                        f'_crash_snapshot_worker({str(self.identity.configured_jsonl_path)!r}, '
                        f'{str(self.destination)!r}, {configured!r}, {boundary!r})',
                    ], capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
                    for _ in range(2):
                        store = open_canonical_tm_store(self.identity.configured_jsonl_path)
                        self.assertIs(type(store), storage.SQLiteTMStore)
                        assert isinstance(store, storage.SQLiteTMStore)
                        try:
                            self.assertEqual(len(store.capture_export_snapshot().records), 4)
                            self.assertEqual(store.coordinator.current_generation, 0)
                        finally:
                            store.coordinator.close_family_session()
                    with closing(sqlite3.connect(self.identity.canonical_sidecar_path)) as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM tm_snapshot_receipt WHERE status='issued'").fetchone(), (0,))
                        self.assertEqual(connection.execute("SELECT count(*) FROM tm_meta WHERE key LIKE 'artifact_handoff.%'").fetchone(), (0,))
                    for path in migration._export_artifact_paths(self.identity.configured_jsonl_path if configured else self.destination).__dict__.values():
                        if isinstance(path, Path) and (path.name.endswith('.tmp') or path.name.endswith('.bak')):
                            self.assertFalse(path.exists(), path)

    def test_completed_sql_close_retains_exact_connection_until_owner_recovery(self):
        import sqlite3
        actual_connect = sqlite3.connect
        selected, events = [], []
        fail_close = [True]
        class Connection(sqlite3.Connection):
            completing = False
            def execute(self, sql, *args, **kwargs):
                if "UPDATE tm_snapshot_receipt" in sql and "'completed'" in sql:
                    self.completing = True
                return super().execute(sql, *args, **kwargs)
            def close(self):
                if self.completing:
                    if self not in selected:
                        selected.append(self)
                    events.append(('close', self))
                    if fail_close[0]:
                        raise OSError('exact completion connection close failed')
                return super().close()
        def connect(*args, **kwargs):
            events.append(('connect', None))
            if fail_close[0]:
                kwargs['factory'] = Connection
            return actual_connect(*args, **kwargs)
        with patch.object(sqlite3, 'connect', connect):
            result = self.service.export_jsonl(self.store, self.destination)
            self.assertIs(type(result), ExportFailure, result)
            assert isinstance(result, ExportFailure)
            self.assertTrue(result.publication_committed)
            self.assertFalse(result.publication_commit_ambiguous)
            self.assertFalse(result.recovery_locators)
            self.assertEqual(len(selected), 1)
            self.assertEqual(events[-1], ('close', selected[0]))
            self.assertEqual(len(self.family._pending_activation_closes), 1)
            with self.assertRaises(storage.SQLiteStoreLifecycleError):
                self.store.exact_records('same')
            fail_close[0] = False
            before = len(events)
            self.owner.rehydrate_runtime_authority()
            self.assertEqual(events[before], ('close', selected[0]))
            self.assertFalse(self.family._pending_activation_closes)
            recovered = self.service.recover_configured_refresh(self.store)
            self.assertNotEqual(recovered.state.value, 'BLOCKED', recovered)
            self.assertEqual([r.target_raw for r in self.store.exact_records('same')], ['winner', 'first'])
        with self.assertRaises(sqlite3.ProgrammingError):
            selected[0].execute('SELECT 1')
        with closing(actual_connect(self.identity.canonical_sidecar_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tm_meta WHERE key LIKE 'artifact_handoff.%'").fetchone(), (0,))

    def test_failed_commit_probe_does_not_claim_known_commit(self):
        import sqlite3
        actual_connect = sqlite3.connect
        ambiguous = [False]
        class Connection(sqlite3.Connection):
            completing = False
            def execute(self, sql, *args, **kwargs):
                if "UPDATE tm_snapshot_receipt" in sql and "'completed'" in sql:
                    self.completing = True
                return super().execute(sql, *args, **kwargs)
            def commit(self):
                super().commit()
                if self.completing:
                    ambiguous[0] = True
                    raise sqlite3.OperationalError('commit response lost')
        def connect(*args, **kwargs):
            if ambiguous[0]:
                raise sqlite3.OperationalError('commit probe unavailable')
            kwargs['factory'] = Connection
            return actual_connect(*args, **kwargs)
        with patch.object(sqlite3, 'connect', connect):
            result = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(result), ExportFailure, result)
        assert isinstance(result, ExportFailure)
        self.assertFalse(result.publication_committed)
        self.assertTrue(result.publication_commit_ambiguous)
        self.assertEqual(len(self.destination.read_text().splitlines()), 3)
        recovered = self.service.recover_configured_refresh(self.store)
        self.assertNotEqual(recovered.state.value, 'BLOCKED', recovered)

    def test_completed_refresh_rejects_original_anchor_and_current_binding_tampering(self):
        import sqlite3
        from tm_engine import open_canonical_tm_store
        changes = {
            'activation anchor': "UPDATE tm_snapshot_receipt SET jsonl_digest='" + '1'*64 + "' WHERE snapshot_id=?",
            'current resource': "UPDATE tm_snapshot_receipt SET resource_id='foreign' WHERE snapshot_id=?",
            'current canonical': "UPDATE tm_snapshot_receipt SET canonical_store_id='foreign' WHERE snapshot_id=?",
            'current path': "UPDATE tm_snapshot_receipt SET destination_jsonl_path='/foreign.jsonl' WHERE snapshot_id=?",
            'current revision': "UPDATE tm_snapshot_receipt SET exported_revision=999 WHERE snapshot_id=?",
            'current count': "UPDATE tm_snapshot_receipt SET record_count=999 WHERE snapshot_id=?",
            'foreign export binding': 'foreign export binding',
        }
        self.owner.close_family_session()
        for label, sql in changes.items():
            with self.subTest(label=label):
                root = self.base / label.replace(' ', '-')
                root.mkdir()
                identity, owner, service = _fixture(root)
                result = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
                self.assertIs(type(result), MigrationReport, result)
                store = storage.SQLiteTMStore.from_coordinator(owner)
                self.addCleanup(owner.close_family_session)
                binding = store._read_refresh_recovery_facts().binding
                assert binding is not None
                original = binding.receipt.snapshot_id
                store.append(_draft('additional', 'new'))
                self.assertIs(type(service.refresh_configured_snapshot(store)), ExportReport)
                binding = store._read_refresh_recovery_facts().binding
                assert binding is not None
                current = binding.receipt.snapshot_id
                self.assertNotEqual(original, current)
                if label == 'foreign export binding':
                    self.assertIs(type(service.export_jsonl(store, self.destination)), ExportReport)
                owner.close_family_session()
                with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
                    if label == 'foreign export binding':
                        foreign = connection.execute('SELECT snapshot_id FROM tm_snapshot_receipt WHERE destination_jsonl_path=?', (str(self.destination),)).fetchone()[0]
                        connection.execute('UPDATE tm_snapshot_binding SET snapshot_id=?', (foreign,))
                    else:
                        connection.execute(sql, (original if label == 'activation anchor' else current,))
                    connection.commit()
                before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
                for _ in range(2):
                    with self.assertRaisesRegex((ValueError, storage.SQLiteStoreSchemaError), 'ACTIVE_BINDING_INVALID|RECOVERY_FAILED'):
                        open_canonical_tm_store(identity.configured_jsonl_path)
                self.assertEqual({path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}, before)

    def test_issued_invalid_ancestry_is_blocked_without_divergence_latch(self):
        import sqlite3
        import subprocess
        self.store.append(_draft('additional', 'new'))
        result = subprocess.run([sys.executable, '-B', '-c',
            'from tests.test_tm_posix_snapshot_lifecycle import _crash_snapshot_worker; '
            f'_crash_snapshot_worker({str(self.identity.configured_jsonl_path)!r}, {str(self.destination)!r}, True, "issued")'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        with closing(sqlite3.connect(self.identity.canonical_sidecar_path)) as connection:
            connection.execute("UPDATE tm_snapshot_receipt SET record_count=999 WHERE status='issued'")
            connection.commit()
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        outcome = self.service.recover_configured_refresh(self.store)
        self.assertEqual(outcome.state.value, 'BLOCKED', outcome)
        self.assertIn('RECOVERY.ANCESTRY_INVALID', outcome.diagnostics)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}, before)

    def test_issued_export_cancels_exact_existing_jsonl_without_manifest(self):
        import subprocess
        self.destination.write_bytes(b'previous user output')
        result = subprocess.run([sys.executable, '-B', '-c',
            'from tests.test_tm_posix_snapshot_lifecycle import _crash_snapshot_worker; '
            f'_crash_snapshot_worker({str(self.identity.configured_jsonl_path)!r}, {str(self.destination)!r}, False, "issued")'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        outcome = self.service.recover_configured_refresh(self.store)
        self.assertEqual(outcome.state.value, 'CANCELLED', outcome)
        self.assertEqual(self.destination.read_bytes(), b'previous user output')
        self.assertFalse(self.destination.with_name(self.destination.name + '.localcat-snapshot.json').exists())

    def test_export_refuses_manifest_without_jsonl_before_issued(self):
        manifest = self.destination.with_name(self.destination.name + '.localcat-snapshot.json')
        manifest.write_bytes(b'previous user manifest')
        before = self.identity.canonical_sidecar_path.read_bytes()
        outcome = self.service.export_jsonl(self.store, self.destination)
        self.assertIs(type(outcome), ExportFailure, outcome)
        assert isinstance(outcome, ExportFailure)
        self.assertEqual(outcome.error_code, 'EXPORT.PAIR_INCONSISTENT')
        self.assertEqual(manifest.read_bytes(), b'previous user manifest')
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.identity.canonical_sidecar_path.read_bytes(), before)

    def test_issued_recovery_rejects_same_bytes_foreign_prior_inode(self):
        import subprocess
        # Use a valid export so the old digest pair also exists in the ledger.
        self.assertIs(type(self.service.export_jsonl(self.store, self.destination)), ExportReport)
        result = subprocess.run([sys.executable, '-B', '-c',
            'from tests.test_tm_posix_snapshot_lifecycle import _crash_snapshot_worker; '
            f'_crash_snapshot_worker({str(self.identity.configured_jsonl_path)!r}, {str(self.destination)!r}, False, "issued")'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        foreign = self.output / 'foreign'
        foreign.write_bytes(self.destination.read_bytes())
        foreign.replace(self.destination)
        before = {path.name: path.read_bytes() for path in self.output.iterdir() if path.is_file()}
        db_before = self.identity.canonical_sidecar_path.read_bytes()
        outcome = self.service.recover_configured_refresh(self.store)
        self.assertEqual(outcome.state.value, 'BLOCKED', outcome)
        self.assertEqual(self.identity.canonical_sidecar_path.read_bytes(), db_before)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir() if path.is_file()}, before)

    def _crash_export_or_refresh(self, destination: Path, *, configured: bool, boundary: str) -> None:
        import subprocess
        result = subprocess.run([sys.executable, '-B', '-c',
            'from tests.test_tm_posix_snapshot_lifecycle import _crash_snapshot_worker; '
            f'_crash_snapshot_worker({str(self.identity.configured_jsonl_path)!r}, '
            f'{str(destination)!r}, {configured!r}, {boundary!r})'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)

    def _receipt_rows_and_handoffs(self) -> tuple[list[tuple[str, str, int]], int]:
        import sqlite3
        with closing(sqlite3.connect(self.identity.canonical_sidecar_path)) as connection:
            rows = connection.execute('SELECT snapshot_id,status,record_count FROM tm_snapshot_receipt ORDER BY rowid').fetchall()
            handoffs = connection.execute("SELECT count(*) FROM tm_meta WHERE key LIKE 'artifact_handoff.%'").fetchone()[0]
        return rows, handoffs

    def _assert_current_attempt_after_prior(self, *, recover: bool, fault: bool) -> None:
        import hashlib
        self.store.append(_draft('first appended', 'fourth'))
        if recover:
            self._crash_export_or_refresh(self.destination, configured=True, boundary='jsonl')
        else:
            self.assertIs(type(self.service.refresh_configured_snapshot(self.store)), ExportReport)
        prior_rows, _ = self._receipt_rows_and_handoffs()
        old_snapshot_id = prior_rows[-1][0]
        self.store.append(_draft('second appended', 'fifth'))
        actual_replace = migration._bound_publish_candidate
        fired = []
        def replace(parent, lease, publication, prior, *args, **kwargs):
            destination = self.identity.configured_jsonl_path.parent / publication.name
            if fault and destination == self.identity.snapshot_manifest_path:
                fired.append(True)
                raise OSError('new refresh manifest publication fails')
            return actual_replace(parent, lease, publication, prior, *args, **kwargs)
        with patch.object(migration, '_bound_publish_candidate', replace):
            result = self.service.refresh_configured_snapshot(self.store)
        rows, handoffs = self._receipt_rows_and_handoffs()
        manifest = json.loads(self.identity.snapshot_manifest_path.read_text())['payload']['receipt']
        self.assertEqual(rows[-2], (old_snapshot_id, 'completed', 4))
        self.assertNotEqual(rows[-1][0], old_snapshot_id)
        self.assertEqual(hashlib.sha256(self.identity.configured_jsonl_path.read_bytes()).hexdigest(), manifest['jsonl_digest'])
        if fault:
            self.assertTrue(fired)
            self.assertIs(type(result), ExportFailure, result)
            assert isinstance(result, ExportFailure)
            self.assertFalse(result.publication_committed, result)
            self.assertFalse(result.publication_commit_ambiguous, result)
            self.assertEqual(rows[-1][1:], ('cancelled', 5))
            self.assertEqual(manifest['snapshot_id'], old_snapshot_id)
            self.assertEqual(len(self.identity.configured_jsonl_path.read_text().splitlines()), 4)
        else:
            self.assertIs(type(result), ExportReport, result)
            self.assertEqual(rows[-1][1:], ('completed', 5))
            self.assertEqual(manifest['snapshot_id'], rows[-1][0])
            self.assertEqual(len(self.identity.configured_jsonl_path.read_text().splitlines()), 5)
        self.assertEqual(handoffs, 0)

    def test_new_refresh_failure_after_old_recovery_uses_current_receipt_commit_only(self) -> None:
        self._assert_current_attempt_after_prior(recover=True, fault=True)

    def test_new_refresh_failure_without_old_recovery_restores_prior_pair(self) -> None:
        self._assert_current_attempt_after_prior(recover=False, fault=True)

    def test_new_refresh_success_after_old_recovery_commits_current_receipt(self) -> None:
        self._assert_current_attempt_after_prior(recover=True, fault=False)

    def _assert_two_cold_opens(self, *, blocked: bool = False) -> None:
        from tm_engine import open_canonical_tm_store
        for _ in range(2):
            if blocked:
                with self.assertRaisesRegex(ValueError, 'TM.CANONICAL_RECOVERY_FAILED'):
                    open_canonical_tm_store(self.identity.configured_jsonl_path)
                continue
            store = open_canonical_tm_store(self.identity.configured_jsonl_path)
            assert isinstance(store, storage.SQLiteTMStore)
            try:
                self.assertEqual([r.target_raw for r in store.exact_records('same')], ['winner', 'first'])
                self.assertEqual(store.source_binding_monitor.observe().state.value, 'VERIFIED_CURRENT')
            finally:
                store.coordinator.close_family_session()

    def _completed_export_and_close(self) -> None:
        self.assertIs(type(self.service.export_jsonl(self.store, self.destination)), ExportReport)
        rows, handoffs = self._receipt_rows_and_handoffs()
        self.assertFalse(any(row[1] == 'issued' for row in rows))
        self.assertEqual(handoffs, 0)
        self.owner.close_family_session()

    def test_finished_export_directory_can_move_before_cold_open(self) -> None:
        self._completed_export_and_close()
        before = _disk_bytes(self.root)
        self.output.rename(self.base / 'completed-output')
        self._assert_two_cold_opens()
        self.assertEqual(_disk_bytes(self.root), before)

    def test_finished_export_pair_can_be_deleted_before_cold_open(self) -> None:
        self._completed_export_and_close()
        before = _disk_bytes(self.root)
        self.destination.unlink()
        self.destination.with_name(self.destination.name + '.localcat-snapshot.json').unlink()
        self._assert_two_cold_opens()
        self.assertEqual(_disk_bytes(self.root), before)

    def test_finished_export_directory_does_not_block_other_pending_export(self) -> None:
        self._completed_export_and_close()
        second = self.base / 'second-output'
        second.mkdir()
        destination = second / 'snapshot.jsonl'
        self._crash_export_or_refresh(destination, configured=False, boundary='jsonl')
        self.output.rename(self.base / 'completed-output')
        self._assert_two_cold_opens()
        rows, handoffs = self._receipt_rows_and_handoffs()
        self.assertFalse(any(row[1] == 'issued' for row in rows))
        self.assertEqual(handoffs, 0)
        self.assertEqual(len(destination.read_text().splitlines()), 3)

    def _assert_pending_destination_stays_required(self, boundary: str) -> None:
        self.owner.close_family_session()
        self._crash_export_or_refresh(self.destination, configured=False, boundary=boundary)
        rows, handoffs = self._receipt_rows_and_handoffs()
        self.assertEqual(rows[-1][1], 'issued' if boundary == 'issued' else 'completed')
        self.assertEqual(handoffs, 1)
        before = _disk_bytes(self.root)
        moved = self.base / 'unfinished-output'
        self.output.rename(moved)
        output_before = _disk_bytes(moved)
        self._assert_two_cold_opens(blocked=True)
        self.assertEqual(_disk_bytes(self.root), before)
        self.assertEqual(_disk_bytes(moved), output_before)

    def test_issued_export_directory_remains_required_by_recovery(self) -> None:
        self._assert_pending_destination_stays_required('issued')

    def test_terminal_handoff_directory_remains_required_by_recovery(self) -> None:
        self._assert_pending_destination_stays_required('completed')


def _disk_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def _draft(source, target):
    return TMRecordDraft(source_raw=source, target_raw=target, speaker_raw=None,
                         context_prev_raw=None, context_next_raw=None, file_source=None, provenance=())


def _crash_snapshot_worker(source, destination, configured, boundary):
    import os
    from tm_engine import open_canonical_tm_store
    store = open_canonical_tm_store(Path(source))
    assert isinstance(store, storage.SQLiteTMStore)
    owner = store.coordinator
    service = migration.TMMigrationService(resource_identity=owner._resource_identity,
        canonical_store_id=owner.canonical_store_id, coordinator=owner)
    register_name = 'register_issued_refresh_receipt' if configured else 'register_issued_export_receipt'
    complete_name = 'complete_issued_refresh_receipt' if configured else 'complete_issued_export_receipt'
    actual_register, actual_complete = getattr(store, register_name), getattr(store, complete_name)
    actual_replace = migration._replace_path
    actual_bound = migration._bound_publish_candidate
    def bound(parent, lease, publication, prior, **kwargs):
        result = actual_bound(parent, lease, publication, prior, **kwargs)
        wanted = Path(source) if configured else Path(destination)
        if ((boundary == 'jsonl' and publication.name == wanted.name)
                or (boundary == 'manifest' and publication.name == wanted.name + '.localcat-snapshot.json')):
            os._exit(86)
        return result
    def register(*args, **kwargs):
        actual_register(*args, **kwargs)
        if boundary == 'issued':
            os._exit(86)
    def complete(*args, **kwargs):
        actual_complete(*args, **kwargs)
        if boundary == 'completed':
            os._exit(86)
    def replace(source_path, target, *args, **kwargs):
        result = actual_replace(source_path, target, *args, **kwargs)
        wanted = Path(source) if configured else Path(destination)
        if ((boundary == 'jsonl' and target == wanted)
                or (boundary == 'manifest' and target == wanted.with_name(wanted.name + '.localcat-snapshot.json'))):
            os._exit(86)
        return result
    with patch.object(store, register_name, register), patch.object(store, complete_name, complete), patch.object(migration, '_replace_path', replace), patch.object(migration, '_bound_publish_candidate', bound):
        outcome = (service.refresh_configured_snapshot(store) if configured
                   else service.export_jsonl(store, Path(destination)))
    raise AssertionError(f'未到达真实崩溃边界：{outcome}')
