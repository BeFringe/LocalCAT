"""Core 10.9：原家族恢复、实际回滚和恢复收尾。"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_activation_journal as journal
import tm_activation_recovery as recovery
import tm_sqlite_store as store
from tests.test_tm_activation_journal import _first_prepared
from tests.test_tm_activation_rollback import _db_replaced_window


@unittest.skipIf(sys.platform == 'win32', 'POSIX activation recovery')
class PosixActivationRecoveryTests(unittest.TestCase):
    def test_stage_role_requires_sealed_content_before_first_rollback_at_both_locations(self):
        from tests.test_tm_activation_recovery import _fresh
        for quarantined in (False, True):
            for damaged in (False, True):
                with self.subTest(quarantined=quarantined, damaged=damaged), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    identity, original, _, _, handle = _first_prepared(root)
                    stage = handle._record.candidate_stage_db_path
                    try:
                        if quarantined:
                            actual = recovery._quarantine_failed_activation_artifacts
                            primary = OSError('after real quarantine before terminal')
                            hits = []
                            def stop_after_quarantine(*args, **kwargs):
                                actual(*args, **kwargs)
                                hits.append(True)
                                raise primary
                            interrupted = _fresh(identity)
                            try:
                                with patch.object(recovery, '_quarantine_failed_activation_artifacts', stop_after_quarantine):
                                    with self.assertRaises(OSError) as raised:
                                        interrupted.rollback_durable_activation()
                                self.assertIs(raised.exception, primary)
                                self.assertEqual(hits, [True])
                                self.assertTrue(interrupted._family._recovery_required)
                                self.assertTrue(handle.journal_path.exists())
                            finally:
                                interrupted.close_family_session()
                            self.assertFalse(stage.exists())
                            stage = journal._activation_quarantine_directory(identity, handle._record) / stage.name
                            self.assertTrue(stage.is_file())
                        if damaged:
                            stage.write_bytes(b'damaged owned stage')
                        def files():
                            return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
                        before = files()
                        for attempt in range(2):
                            fresh = _fresh(identity)
                            try:
                                call = fresh.rollback_durable_activation if attempt == 0 else fresh.recover_durable_activation
                                if damaged:
                                    with self.assertRaises(journal.ActivationPreparationError) as raised:
                                        call()
                                    self.assertEqual(raised.exception.code, (
                                        'ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID'
                                        if attempt == 1 and not quarantined
                                        else 'ACTIVATION.QUARANTINE_FOREIGN'
                                    ))
                                    self.assertTrue(fresh._family._recovery_required)
                                    self.assertEqual(fresh.state, 'ACTIVATING')
                                    self.assertEqual(files(), before)
                                    self.assertTrue(handle.journal_path.exists())
                                    self.assertFalse(journal._activation_terminal_path(identity).exists())
                                else:
                                    result = call()
                                    if attempt == 0:
                                        self.assertEqual(result.action, 'ROLLED_BACK')
                                    else:
                                        self.assertIsNone(result)
                                    self.assertFalse(fresh._family._recovery_required)
                                    self.assertEqual(identity.configured_jsonl_path.read_bytes(), before[identity.configured_jsonl_path.name])
                            finally:
                                fresh.close_family_session()
                    finally:
                        original.close_family_session()

    def test_published_binding_failure_preserves_generation_journal_pair_and_primary(self):
        from contextlib import closing
        import sqlite3
        from tests.test_tm_initial_activation_publication import _fixture
        from tm_contracts import MigrationFailure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity, owner, service = _fixture(root)
            actual_publish = owner.publish_activation
            actual_replay = recovery._replay_terminal_recovery
            errors, published = [], []
            def files():
                return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
            def publish_then_rebind(*args, **kwargs):
                generation = actual_publish(*args, **kwargs)
                self.assertEqual(generation, 0)
                with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
                    with connection:
                        connection.execute('UPDATE tm_snapshot_binding SET configured_jsonl_path = ? WHERE binding_id = 1', (str(root / 'foreign.jsonl'),))
                        connection.execute('UPDATE tm_snapshot_receipt SET destination_jsonl_path = ?', (str(root / 'foreign.jsonl'),))
                published.append(files())
                return generation
            def observe_replay(*args, **kwargs):
                try:
                    return actual_replay(*args, **kwargs)
                except journal.ActivationPreparationError as error:
                    errors.append(error)
                    raise
            try:
                with patch.object(owner, 'publish_activation', publish_then_rebind), patch.object(recovery, '_replay_terminal_recovery', observe_replay):
                    outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
                    self.assertIsInstance(outcome, MigrationFailure)
                    self.assertTrue(outcome.canonical_authority_published)
                    self.assertEqual(outcome.active_generation, 0)
                    self.assertEqual(owner.current_generation, 0)
                    self.assertEqual(owner.state, 'ACTIVATING')
                    self.assertTrue(owner._family._recovery_required)
                    self.assertEqual(files(), published[0])
                    journal_path = journal._activation_journal_path(identity)
                    record = journal._parse_activation_journal_bytes(journal_path.read_bytes(), expected_journal_path=journal_path)
                    self.assertIs(record.phase, journal._ActivationJournalPhase.GENERATION_PUBLISHED)
                    self.assertFalse(journal._activation_terminal_path(identity).exists())
                    for _ in range(2):
                        fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                        try:
                            with self.assertRaises(journal.ActivationPreparationError) as raised:
                                fresh.recover_durable_activation()
                            self.assertIs(raised.exception, errors[-1])
                            self.assertTrue(fresh._family._recovery_required)
                            self.assertEqual(fresh.state, 'ACTIVATING')
                            self.assertEqual(files(), published[0])
                        finally:
                            fresh.close_family_session()
            finally:
                owner.close_family_session()

    def test_recovery_generation_write_failure_uses_actual_phase_before_rollback(self):
        from contextlib import closing
        import sqlite3
        from tests.test_tm_activation_rollback import _manifest_published_window
        for damaged in (False, True):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                identity, owner, _, prepared, handle = _first_prepared(root)
                _manifest_published_window(owner, prepared, handle)
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                actual_write = store._CoordinatorStorePort.write_journal
                actual_validate = recovery._revalidate_recovered_active_set
                published, errors = [], []
                def files():
                    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
                def write_then_damage(port, record, *args, **kwargs):
                    result = actual_write(port, record, *args, **kwargs)
                    if record.phase is journal._ActivationJournalPhase.GENERATION_PUBLISHED:
                        if damaged:
                            with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
                                connection.execute('PRAGMA user_version=99')
                                connection.commit()
                        published.append(files())
                    return result
                def observe_validation(*args, **kwargs):
                    try:
                        return actual_validate(*args, **kwargs)
                    except journal.ActivationPreparationError as error:
                        errors.append(error)
                        raise
                try:
                    with patch.object(store._CoordinatorStorePort, 'write_journal', write_then_damage), patch.object(recovery, '_revalidate_recovered_active_set', observe_validation):
                        for attempt in range(2):
                            if damaged:
                                with self.assertRaises(journal.ActivationPreparationError) as raised:
                                    fresh.recover_durable_activation()
                                self.assertEqual(raised.exception.code, 'ACTIVATION.RECOVERY_ACTIVE_SET_INVALID')
                                self.assertIs(raised.exception, errors[-1])
                                self.assertIsNotNone(raised.exception.__cause__)
                                self.assertEqual(fresh.state, 'ACTIVATING')
                                self.assertTrue(fresh._family._recovery_required)
                                self.assertEqual(files(), published[0])
                                self.assertFalse(journal._activation_terminal_path(identity).exists())
                            else:
                                report = fresh.recover_durable_activation()
                                self.assertEqual((report.action, report.generation), ('COMPLETED', 0))
                                self.assertFalse(fresh._family._recovery_required)
                            record = journal._parse_activation_journal_bytes(handle.journal_path.read_bytes(), expected_journal_path=handle.journal_path)
                            self.assertIs(record.phase, journal._ActivationJournalPhase.GENERATION_PUBLISHED)
                        self.assertEqual(len(published), 1)
                finally:
                    fresh.close_family_session()
                    owner.close_family_session()

    def test_explicit_rollback_borrows_original_family_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            identity, owner, _, prepared, handle = _first_prepared(Path(directory))
            _db_replaced_window(owner, prepared, handle)
            root = owner._family._root
            actual = store.rollback_durable_activation
            seen = []
            def observe(port):
                self.assertIsNotNone(port.posix_activation_borrow)
                self.assertIs(port.posix_activation_borrow.root, root)
                port.posix_activation_borrow.reprove()
                seen.append(port.posix_activation_borrow)
                return actual(port)
            with patch.object(store, 'rollback_durable_activation', observe):
                report = owner.rollback_durable_activation()
            self.assertEqual(report.action, 'ROLLED_BACK')
            self.assertEqual(len(seen), 1)
            self.assertIsNone(owner.current_generation)
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(owner._family._recovery_required)
            self.assertIsNone(owner.recover_durable_activation())
            owner.close_family_session()

    def test_generation_recovery_publishes_exact_existing_marker_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            identity, owner, _, prepared, handle = _first_prepared(Path(directory))
            with patch.object(recovery, '_ensure_activation_lineage_marker', side_effect=OSError('marker pause')):
                with self.assertRaises(OSError):
                    owner.publish_activation(prepared, handle)
            marker = journal._activation_lineage_marker_path(identity)
            temporary = journal._activation_lineage_marker_temp_path(marker)
            temporary.write_bytes(journal._activation_lineage_marker_payload(identity))
            original_inode = temporary.stat().st_ino
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            try:
                report = fresh.recover_durable_activation()
                self.assertEqual(report.action, 'COMPLETED')
                self.assertEqual(marker.stat().st_ino, original_inode)
                self.assertFalse(temporary.exists())
                self.assertEqual(fresh.recover_durable_activation().generation, 0)
            finally:
                fresh.close_family_session()
                owner.close_family_session()

    def test_manifest_pending_close_failure_retains_exact_object_until_retry(self):
        from platform_fs_contracts import PendingPublication
        for after_close in (False, True):
            with self.subTest(after_close=after_close), tempfile.TemporaryDirectory() as directory:
                identity, owner, _, prepared, handle = _first_prepared(Path(directory))
                _db_replaced_window(owner, prepared, handle)
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                root = fresh._family._root
                begin = type(root).begin_publish
                close = PendingPublication.close
                pendings, failed, retried = [], [], []
                def observe_begin(current, candidate, destination, **kwargs):
                    pending = begin(current, candidate, destination, **kwargs)
                    if current is root and str(destination) == identity.snapshot_manifest_path.name:
                        pendings.append(pending)
                    return pending
                def fail_close(pending):
                    if pending in pendings and not failed:
                        failed.append(pending)
                        if after_close:
                            close(pending)
                        raise OSError('manifest pending close')
                    if pending in failed:
                        retried.append(pending)
                    return close(pending)
                try:
                    with patch.object(type(root), 'begin_publish', observe_begin), patch.object(PendingPublication, 'close', fail_close):
                        with self.assertRaisesRegex(OSError, 'manifest pending close'):
                            fresh.recover_durable_activation()
                        self.assertEqual(failed, pendings)
                        self.assertEqual(len(fresh._family._pending_activation_closes), 1)
                        self.assertTrue(fresh._family._recovery_required)
                        self.assertEqual(fresh.recover_durable_activation().generation, 0)
                        self.assertEqual(retried, failed)
                        self.assertFalse(fresh._family._pending_activation_closes)
                    self.assertEqual(fresh.recover_durable_activation().generation, 0)
                finally:
                    fresh.close_family_session()
                    owner.close_family_session()

    def test_failed_sql_close_keeps_exact_connection_and_guard_before_raw_recovery(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            identity, owner, _, prepared, handle = _first_prepared(Path(directory))
            _db_replaced_window(owner, prepared, handle)
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            real_connect = sqlite3.connect
            events, owned = [], []
            class RetainedConnection(sqlite3.Connection):
                def close(connection):
                    events.append(('close', connection))
                    if connection is owned[0] and events.count(('close', connection)) == 1:
                        raise OSError('SQL close retained')
                    return super(RetainedConnection, connection).close()
            def connect(*args, **kwargs):
                kwargs['factory'] = RetainedConnection
                connection = real_connect(*args, **kwargs)
                owned.append(connection)
                return connection
            capture = journal._CallerHeldPosixActivationBorrow.capture
            def watch_capture(borrow, path, **kwargs):
                if path == identity.canonical_sidecar_path and owned:
                    self.assertGreaterEqual(events.count(('close', owned[0])), 2)
                return capture(borrow, path, **kwargs)
            try:
                with patch.object(store.sqlite3, 'connect', connect):
                    with self.assertRaises(Exception):
                        fresh.recover_durable_activation()
                self.assertTrue(fresh._family._pending_activation_closes)
                self.assertEqual(events.count(('close', owned[0])), 1)
                with patch.object(journal._CallerHeldPosixActivationBorrow, 'capture', watch_capture):
                    report = fresh.recover_durable_activation()
                self.assertEqual(report.action, 'COMPLETED')
                self.assertEqual(events.count(('close', owned[0])), 2)
                self.assertFalse(fresh._family._pending_activation_closes)
            finally:
                fresh.close_family_session()
                owner.close_family_session()

    def test_root_clone_before_rollback_keeps_journal_and_clone_unchanged(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'resource'
            root.mkdir()
            identity, owner, _, prepared, handle = _first_prepared(root)
            _db_replaced_window(owner, prepared, handle)
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            original = Path(directory) / 'original'
            root.rename(original)
            shutil.copytree(original, root)
            def files(path):
                return {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()}
            before = files(root)
            try:
                with self.assertRaises(Exception):
                    fresh.rollback_durable_activation()
                self.assertEqual(files(root), before)
                self.assertTrue((original / handle.journal_path.name).exists())
                self.assertTrue(fresh._family._state == 'FAILED')
            finally:
                fresh.close_family_session()
                owner.close_family_session()

    def test_outer_w1_close_keeps_verified_completion_fact_and_stops_family(self):
        from platform_fs_contracts import LockLease
        with tempfile.TemporaryDirectory() as directory:
            identity, owner, _, prepared, handle = _first_prepared(Path(directory))
            _db_replaced_window(owner, prepared, handle)
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            close = LockLease.close
            failure = OSError('outer W1 close')
            hits = []
            def fail_close(lease):
                close(lease)
                hits.append(lease)
                raise failure
            try:
                with patch.object(LockLease, 'close', fail_close):
                    with self.assertRaises(OSError) as raised:
                        fresh.recover_durable_activation()
                self.assertIs(raised.exception, failure)
                self.assertEqual(len(hits), 1)
                self.assertEqual(raised.exception._verified_recovery_report.action, 'COMPLETED')
                self.assertEqual(raised.exception._verified_recovery_report.generation, 0)
                self.assertTrue(fresh._family._recovery_required)
                self.assertEqual(fresh._family._state, 'FAILED')
                self.assertEqual(journal._parse_activation_journal_bytes(handle.journal_path.read_bytes(), expected_journal_path=handle.journal_path).phase, journal._ActivationJournalPhase.GENERATION_PUBLISHED)
            finally:
                fresh.close_family_session()
                owner.close_family_session()

    def test_complete_manifest_window_rolls_back_and_replays_legacy(self):
        from tests.test_tm_activation_rollback import _manifest_published_window
        with tempfile.TemporaryDirectory() as directory:
            identity, owner, _, prepared, handle = _first_prepared(Path(directory))
            before = identity.configured_jsonl_path.read_bytes()
            _manifest_published_window(owner, prepared, handle)
            report = owner.rollback_durable_activation()
            self.assertEqual((report.phase, report.action, report.generation), ('MANIFEST_PUBLISHED', 'ROLLED_BACK', None))
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), before)
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertFalse(handle.journal_path.exists())
            owner.close_family_session()
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            try:
                self.assertIsNone(fresh.recover_durable_activation())
                self.assertIsNone(fresh.recover_durable_activation())
                self.assertEqual(identity.configured_jsonl_path.read_bytes(), before)
            finally:
                fresh.close_family_session()

    def test_real_canonical_prior_restore_retries_after_naming_and_before_terminal(self):
        from tests.test_tm_initial_activation_rollback import _fixture
        from tm_contracts import MigrationReport
        from tm_migration import _scan_jsonl
        for cut in ('restored', 'terminal', 'manifest'):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as directory:
                identity, owner, service = _fixture(Path(directory))
                self.assertIs(type(service.activate_initial(identity.configured_jsonl_path, identity.resource_id)), MigrationReport)
                stage, _, _ = service._build_stage(identity.configured_jsonl_path, preflight=_scan_jsonl(identity.configured_jsonl_path),
                    canonical_store_id='store.replacement', immediate_seal=False, batch_kind='import', batch_prefix='import',
                    snapshot_prefix='snapshot.import', stage_prefix='import', path_salt='rollback-test', batch_id='import.' + 'a' * 32)
                sealed = owner._seal_stage(stage, canonical_store_id='store.replacement', expected_prior_generation=0)
                prepared = owner.activate_replacement(sealed)
                handle = owner.publish_prepared_activation(prepared)
                before = identity.canonical_sidecar_path.read_bytes()
                source = identity.configured_jsonl_path.read_bytes()
                manifest = identity.snapshot_manifest_path.read_bytes()
                journal_before = handle.journal_path.read_bytes()
                if cut == 'manifest':
                    from tests.test_tm_activation_rollback import _manifest_published_window
                    _manifest_published_window(owner, prepared, handle)
                    report = owner.rollback_durable_activation()
                    self.assertEqual((report.phase, report.action, report.generation), ('MANIFEST_PUBLISHED', 'ROLLED_BACK', 0))
                    self.assertFalse(handle.journal_path.exists())
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                    self.assertEqual(identity.snapshot_manifest_path.read_bytes(), manifest)
                    self.assertEqual(identity.configured_jsonl_path.read_bytes(), source)
                else:
                    identity.canonical_sidecar_path.unlink()
                    actual = recovery._restore_activation_file
                    failure = OSError('after rooted prior restore')
                    def fail_after_restore(*args, **kwargs):
                        actual(*args, **kwargs)
                        raise failure
                    seam = (patch.object(recovery, '_restore_activation_file', fail_after_restore) if cut == 'restored'
                            else patch.object(owner, '_write_activation_terminal_locked', side_effect=failure))
                    with seam:
                        with self.assertRaises(OSError) as raised:
                            owner.rollback_durable_activation()
                    self.assertIs(raised.exception, failure)
                    self.assertTrue(owner._family._recovery_required)
                    self.assertEqual(handle.journal_path.read_bytes(), journal_before)
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                    self.assertEqual(identity.snapshot_manifest_path.read_bytes(), manifest)
                    self.assertEqual(identity.configured_jsonl_path.read_bytes(), source)
                owner.close_family_session()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                try:
                    report = fresh.recover_durable_activation()
                    self.assertEqual((report.action, report.generation), ('CANCELLED' if cut == 'manifest' else 'ROLLED_BACK', 0))
                    self.assertEqual(fresh.recover_durable_activation().generation, 0)
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                    self.assertEqual(identity.snapshot_manifest_path.read_bytes(), manifest)
                    self.assertEqual(identity.configured_jsonl_path.read_bytes(), source)
                    with fresh._operation_lease():
                        pass
                finally:
                    fresh.close_family_session()

    def test_receipt_commit_error_before_or_after_commit_waits_for_explicit_recovery(self):
        import sqlite3
        import tm_migration as migration
        from tests.test_tm_initial_activation_rollback import _fixture
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as directory:
                identity, owner, service = _fixture(Path(directory))
                source = identity.configured_jsonl_path.read_bytes()
                primary = sqlite3.OperationalError('receipt commit outcome unknown')
                commit = recovery._commit_activation_receipt
                receipt = recovery._publish_activation_receipt
                observed = []
                def fail_commit(connection):
                    if committed:
                        commit(connection)
                    raise primary
                def record_primary(*args, **kwargs):
                    try:
                        return receipt(*args, **kwargs)
                    except journal.ActivationPreparationError as error:
                        observed.append(error)
                        raise
                with patch.object(recovery, '_commit_activation_receipt', fail_commit), patch.object(recovery, '_publish_activation_receipt', record_primary), patch.object(owner, 'rollback_durable_activation', side_effect=AssertionError('blind rollback')):
                    outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
                self.assertIs(type(outcome), migration.MigrationFailure)
                self.assertTrue(outcome.canonical_authority_ambiguous)
                self.assertEqual(outcome.stage, 'RECOVERY')
                self.assertEqual(len(observed), 1)
                self.assertIs(observed[0].__cause__, primary)
                self.assertTrue(owner._family._recovery_required)
                self.assertIsNone(owner.current_generation)
                path = journal._activation_journal_path(identity)
                record = journal._parse_activation_journal_bytes(path.read_bytes(), expected_journal_path=path)
                self.assertEqual(record.phase.value, 'DB_REPLACED')
                self.assertFalse(journal._activation_terminal_path(identity).exists())
                self.assertFalse(journal._activation_quarantine_directory(identity, record).exists())
                connection = sqlite3.connect(identity.canonical_sidecar_path)
                try:
                    self.assertEqual(connection.execute("SELECT value FROM tm_meta WHERE key = 'activation_status'").fetchone(), ('ACTIVE' if committed else 'SEALED',))
                    self.assertEqual(connection.execute('SELECT status FROM tm_snapshot_receipt').fetchall(), [('completed' if committed else 'issued',)])
                finally:
                    connection.close()
                self.assertEqual(identity.configured_jsonl_path.read_bytes(), source)
                owner.close_family_session()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                try:
                    self.assertEqual(fresh.recover_durable_activation().action, 'COMPLETED')
                    self.assertEqual(fresh.current_generation, 0)
                    self.assertEqual(fresh.recover_durable_activation().generation, 0)
                    self.assertEqual(identity.configured_jsonl_path.read_bytes(), source)
                finally:
                    fresh.close_family_session()

    def test_foreign_stage_or_manifest_temp_refuses_first_and_repeated_rollback(self):
        from tests.test_tm_activation_rollback import _manifest_published_window, _assert_unprovable_preservation_stops
        for role in ('stage', 'manifest'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                identity, owner, _, prepared, handle = _first_prepared(Path(directory))
                try:
                    if role == 'stage':
                        _db_replaced_window(owner, prepared, handle)
                        foreign = handle._record.candidate_stage_db_path
                    else:
                        _manifest_published_window(owner, prepared, handle)
                        foreign = handle._record.candidate_manifest_temp_path
                    foreign.write_bytes(b'foreign object occupying an old candidate name')
                    _assert_unprovable_preservation_stops(self, identity, handle._record, handle.journal_path, 'ACTIVATION.QUARANTINE_FOREIGN')
                    self.assertEqual(foreign.read_bytes(), b'foreign object occupying an old candidate name')
                finally:
                    owner.close_family_session()
