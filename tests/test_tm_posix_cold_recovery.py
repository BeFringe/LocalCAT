"""Core 10.6：真实独立进程的 DB_REPLACED 恢复与冷查询。"""
from __future__ import annotations

import json
import itertools
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import tm_activation_journal as journal
import tm_activation_recovery as recovery
import tm_migration as migration
import tm_sqlite_store as store
from tests import test_tm_posix_builder_seal as fixtures
from platform_fs_contracts import (BoundExistingFileMutationGuard, LockLease, PendingPublication,
                                  PlatformFileError, PlatformFileErrorCode)
from tm_contracts import AssetPreservationState, CanonicalResourceIdentity
from tm_engine import open_canonical_tm_store


_CHILD = r'''
import json, os, sqlite3, sys
from pathlib import Path
from unittest.mock import patch
if os.environ.get('CORE_COLD_BASELINE'):
    sys.path.insert(0, os.environ['CORE_COLD_BASELINE'])
import tm_activation_recovery as recovery
import tm_activation_journal as journal
import tm_migration as migration
import tm_sqlite_store as store
from tm_contracts import CanonicalResourceIdentity
from tm_engine import open_canonical_tm_store
if os.environ.get('CORE_COLD_BASELINE'):
    import hashlib
    print(json.dumps({'baseline_modules': {module.__name__: {'path': module.__file__, 'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()} for module in (recovery, journal, migration, store, sys.modules['tm_engine'])}}), file=sys.stderr, flush=True)
root, mode, window = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
source = root / 'memory.jsonl'
identity = CanonicalResourceIdentity.from_configured_jsonl('tm.cold', source)

def emit(**facts):
    print(json.dumps(dict(pid=os.getpid(), **facts), ensure_ascii=False), flush=True)

if mode in {'initial', 'replacement'}:
    source.write_text('{"source":"hello","target":"first"}\n', encoding='utf-8')
    owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold')
    service = migration.TMMigrationService(resource_identity=identity, canonical_store_id='store.cold', coordinator=owner)
    if mode == 'replacement':
        result = service.activate_initial(source, identity.resource_id)
        assert type(result) is migration.MigrationReport, result
        source.write_text('{"source":"hello","target":"second"}\n', encoding='utf-8')
    def crash(*args, **kwargs):
        borrow = args[0].posix_activation_borrow if window == 'before_receipt' else kwargs['caller_borrow']
        phase = owner._read_durable_activation_phase(caller_borrow=borrow)
        emit(action='crash', window=window, phase=phase, generation=owner.current_generation)
        os._exit(73)
    target = '_publish_activation_receipt' if window == 'before_receipt' else '_publish_activation_manifest'
    with patch.object(recovery, target, crash):
        result = service.activate_initial(source, identity.resource_id) if mode == 'initial' else service.import_snapshot(source, identity.resource_id)
    raise AssertionError(result)

real_connect = sqlite3.connect
connections = []
def connect(*args, **kwargs):
    # The first cold DB pathname read must already have a live family/W1.
    families = tuple(store._FAMILY_SESSIONS.values())
    assert len(families) == 1, 'cold SQLite read before family reservation'
    family = families[0]
    assert family._operation_tokens, 'cold SQLite read before W1'
    family.reprove()
    connection = real_connect(*args, **kwargs)
    connections.append(connection)
    return connection

if mode in {'recover', 'initial_retry'}:
    owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold')
    with patch.object(sqlite3, 'connect', connect):
        if mode == 'initial_retry':
            service = migration.TMMigrationService(resource_identity=identity, canonical_store_id='store.cold', coordinator=owner)
            result = service.activate_initial(source, identity.resource_id)
            assert type(result) is migration.MigrationReport, result
            report = None
        else:
            report = owner.recover_durable_activation()
    runtime = store.SQLiteTMStore.from_coordinator(owner)
else:
    with patch.object(sqlite3, 'connect', connect):
        runtime = open_canonical_tm_store(source, **({} if mode == 'open_unknown' else {'expected_resource_id': identity.resource_id}))
    assert runtime is not None
    owner = runtime._coordinator
    report = None
with runtime.query_lease() as view:
    targets = [item.target_raw for item in view.exact_records('hello')]
assert runtime.health().healthy
for connection in connections:
    try:
        connection.execute('SELECT 1')
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError('SQL connection leaked')
emit(action=None if report is None else report.action, phase=owner.durable_activation_phase,
     generation=owner.current_generation, targets=targets, state=owner.state,
     active_count=owner._family._active_count, active_leases=owner._active_lease_count)
owner.close_family_session()
'''


@unittest.skipIf(sys.platform == 'win32', 'POSIX v2 冷恢复')
class PosixColdRecoveryTests(unittest.TestCase):
    def child(self, root, mode, window='unused'):
        result = subprocess.run([sys.executable, '-c', _CHILD, str(root), mode, window],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=20)
        print(json.dumps({'mode': mode, 'window': window, 'returncode': result.returncode,
                          'stdout': result.stdout, 'stderr': result.stderr}, ensure_ascii=False), flush=True)
        return result

    def journey(self, mode, window):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            crashed = self.child(root, mode, window)
            self.assertEqual(crashed.returncode, 73, crashed.stderr + crashed.stdout)
            facts = json.loads(crashed.stdout)
            self.assertEqual(facts['phase'], 'DB_REPLACED')
            pids = [facts['pid']]
            for action in ('recover', 'recover', 'open', 'open'):
                result = self.child(root, action)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                facts = json.loads(result.stdout)
                self.assertEqual(facts['generation'], 0 if mode == 'initial' else 1)
                self.assertEqual(facts['targets'], ['first' if mode == 'initial' else 'second'])
                self.assertEqual(facts['state'], 'READY')
                self.assertEqual((facts['active_count'], facts['active_leases']), (0, 0))
                pids.append(facts['pid'])
            self.assertEqual(len(set(pids)), len(pids))

    def test_initial_before_receipt(self):
        self.journey('initial', 'before_receipt')

    def test_initial_after_receipt(self):
        self.journey('initial', 'after_receipt')

    def test_replacement_before_receipt(self):
        self.journey('replacement', 'before_receipt')

    def test_replacement_after_receipt(self):
        self.journey('replacement', 'after_receipt')

    def test_initial_retry_uses_one_original_recovery_owner(self):
        for window in ('before_receipt', 'after_receipt'):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                self.assertEqual(self.child(root, 'initial', window).returncode, 73)
                result = self.child(root, 'initial_retry')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['generation'], 0)

    def test_unknown_family_factory_recovers_before_first_sql(self):
        for window in ('before_receipt', 'after_receipt'):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                self.assertEqual(self.child(root, 'initial', window).returncode, 73)
                result = self.child(root, 'open_unknown')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['targets'], ['first'])

    def test_unrelated_child_directory_keeps_normal_cold_recovery_available(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertEqual(self.child(root, 'initial', 'before_receipt').returncode, 73)
            (root / 'unrelated-documents').mkdir()
            (root / 'unrelated-link').symlink_to('missing-document')
            result = self.child(root, 'recover')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['targets'], ['first'])


@unittest.skipIf(sys.platform == 'win32', 'POSIX 冷恢复借用')
class PosixColdRecoveryBorrowTests(unittest.TestCase):
    setUp = fixtures.PosixBuilderSealTests.setUp

    def test_pending_observation_keeps_original_root_and_lease(self):
        (self.root / 'documents').mkdir()
        (self.root / 'link').symlink_to('unavailable')
        observed = []
        backend_type = type(self.owner._family._backend)
        real_observe = backend_type.observe_root_children
        with self.owner._posix_recovery_port() as port:
            borrow = port.posix_activation_borrow

            def observe(backend, root, lease, limits, cancellation):
                self.assertIs(backend, borrow.backend)
                self.assertIs(root, borrow.root)
                self.assertIs(lease, borrow.lease)
                borrow.reprove()
                entries = real_observe(backend, root, lease, limits, cancellation)
                observed.extend(item.name for item in entries)
                return entries

            with patch.object(backend_type, 'observe_root_children', observe):
                store._finish_cold_schema_upgrade_pending(
                    self.identity.canonical_sidecar_path, completed=True, caller_borrow=borrow)
            self.assertFalse(borrow.root.closed)
            self.assertFalse(borrow.lease.closed)
        self.assertIn('documents', observed)
        self.assertIn('link', observed)
        self.assertEqual(self.owner._family._active_count, 0)

    def test_every_matching_pending_kind_is_preserved_and_rejected(self):
        for kind in ('file', 'directory', 'link', 'fifo', 'hardlink', 'malformed'):
            with self.subTest(kind=kind):
                path = self.root / f'.{self.identity.canonical_sidecar_path.name}.localcat-schema-upgrade.{"invalid!" if kind == "malformed" else "token"}.pending'
                if kind == 'directory':
                    path.mkdir()
                elif kind == 'link':
                    path.symlink_to('missing')
                elif kind == 'fifo':
                    os.mkfifo(path)
                elif kind == 'hardlink':
                    os.link(self.source, path)
                else:
                    path.write_bytes(b'untrusted pending')
                before = path.lstat()
                try:
                    with self.owner._posix_recovery_port() as port:
                        with self.assertRaises(store.ActivationPreparationError) as caught:
                            store._finish_cold_schema_upgrade_pending(
                                self.identity.canonical_sidecar_path, completed=True,
                                caller_borrow=port.posix_activation_borrow)
                        self.assertEqual(caught.exception.code, 'ACTIVATION.UPGRADE_PENDING_UNSAFE')
                    after = path.lstat()
                    self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_size),
                                     (after.st_dev, after.st_ino, after.st_mode, after.st_size))
                finally:
                    path.rmdir() if kind == 'directory' else path.unlink()

    def test_incomplete_observation_cannot_skip_pending(self):
        backend_type = type(self.owner._family._backend)
        failure = PlatformFileError(PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, retryable=False)
        with self.owner._posix_recovery_port() as port:
            with patch.object(backend_type, 'observe_root_children', side_effect=failure):
                with self.assertRaises(PlatformFileError) as caught:
                    store._finish_cold_schema_upgrade_pending(
                        self.identity.canonical_sidecar_path, completed=False,
                        caller_borrow=port.posix_activation_borrow)
            self.assertIs(caught.exception, failure)

    def test_corrupt_journal_retry_preserves_failure_without_new_path_reads(self):
        path = journal._activation_journal_path(self.identity)
        path.write_bytes(b'not a journal')
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationFailure)
        self.assertEqual(first.error_code, 'MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE')
        state = self.owner.state
        # A second facade on this owner must preserve the same failure facts.
        service = migration.TMMigrationService(resource_identity=self.identity,
            canonical_store_id='store.builder', coordinator=self.owner)
        with patch.object(migration, '_try_file_digest', side_effect=AssertionError('unbound source read')), patch.object(service, '_validate_source_preconditions', side_effect=AssertionError('unbound source validation')):
            second = service.activate_initial(self.source, self.identity.resource_id)
            with self.assertRaisesRegex(migration.MigrationPreflightError, 'RESOURCE_IDENTITY_MISMATCH'):
                service.activate_initial(self.root / 'other.jsonl', self.identity.resource_id)
            with self.assertRaisesRegex(migration.MigrationPreflightError, 'RESOURCE_IDENTITY_MISMATCH'):
                service.activate_initial(self.source, 'foreign.resource')
        self.assertTrue(second.canonical_authority_ambiguous)
        self.assertFalse(second.canonical_authority_published)
        self.assertEqual(second.error_code, first.error_code)
        self.assertEqual(second.original_source_preservation.state, AssetPreservationState.UNVERIFIED)
        self.assertEqual(second.original_source_preservation.before_digest, first.original_source_preservation.before_digest)
        self.assertEqual(self.owner.state, state)
        self.assertTrue(self.owner._initial_activation_authority_is_unavailable())
        self.assertIsNone(self.owner.current_generation)
        self.assertEqual(path.read_bytes(), b'not a journal')

    def test_foreign_same_parent_borrow_never_enters_recovery(self):
        identity = CanonicalResourceIdentity.from_configured_jsonl('tm.other', self.root / 'other.jsonl')
        other = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.other')
        self.addCleanup(other.close_family_session)
        with other._posix_recovery_port() as port:
            with patch.object(store, 'recover_durable_activation', side_effect=AssertionError('foreign borrow reached recovery')):
                with self.assertRaisesRegex(store.ActivationPreparationError, 'RESERVATION_MISMATCH'):
                    self.owner.recover_durable_activation(_posix_borrow=port.posix_activation_borrow)
        self.assertEqual(self.owner._family._active_count, 0)
        self.assertEqual(other._family._active_count, 0)

    def test_wrong_resource_w1_is_rejected_before_observation(self):
        identity = CanonicalResourceIdentity.from_configured_jsonl('tm.other', self.root / 'other.jsonl')
        other = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.other')
        self.addCleanup(other.close_family_session)
        with self.assertRaises(PlatformFileError):
            with self.owner._posix_recovery_port() as own, other._posix_recovery_port() as foreign:
                borrow = own.posix_activation_borrow
                borrow.lease = foreign.posix_activation_borrow.lease
                with patch.object(type(borrow.backend), 'observe_root_children', side_effect=AssertionError('wrong W1 observation')):
                    store._finish_cold_schema_upgrade_pending(self.identity.canonical_sidecar_path,
                        completed=True, caller_borrow=borrow)
        self.assertEqual(self.owner._family._active_count, 0)
        self.assertFalse(self.owner._family._operation_tokens)

    def test_released_borrow_cannot_resume_cold_recovery(self):
        with self.owner._posix_recovery_port() as port:
            borrow = port.posix_activation_borrow
        with patch.object(store, 'recover_durable_activation', side_effect=AssertionError('released borrow reached recovery')):
            with self.assertRaisesRegex(store.ActivationPreparationError, 'RESERVATION_MISMATCH'):
                self.owner.recover_durable_activation(_posix_borrow=borrow)


@unittest.skipIf(sys.platform == 'win32', 'POSIX 冷恢复故障')
class PosixColdRecoveryFailureTests(unittest.TestCase):
    child = PosixColdRecoveryTests.child

    def setup_crash(self, root, window='before_receipt'):
        self.assertEqual(self.child(root, 'initial', window).returncode, 73)
        identity = CanonicalResourceIdentity.from_configured_jsonl('tm.cold', root / 'memory.jsonl')
        owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold')
        self.addCleanup(owner.close_family_session)
        return identity, owner

    def test_matching_pending_during_real_recovery_stops_public_admission(self):
        for action in ('recover_durable_activation', 'rehydrate_runtime_authority'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                identity, owner = self.setup_crash(root)
                path = root / f'.{identity.canonical_sidecar_path.name}.localcat-schema-upgrade.invalid!.pending'
                path.mkdir()
                with self.assertRaisesRegex(store.ActivationPreparationError, 'UPGRADE_PENDING_UNSAFE'):
                    getattr(owner, action)()
                self.assertEqual(owner.state, 'ACTIVATING')
                self.assertEqual(owner._state, 'ACTIVATING')
                self.assertTrue(path.is_dir())
                self.assertFalse(owner._family._operation_tokens)
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    with store.SQLiteTMStore.from_coordinator(owner).query_lease():
                        self.fail('pending recovery admitted a query')
                owner.close_family_session()

    def test_failed_recovery_stops_other_owner_of_same_family_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            identity, owner = self.setup_crash(root)
            peer = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold')
            self.addCleanup(peer.close_family_session)
            peer.rehydrate_runtime_authority()
            runtime = store.SQLiteTMStore.from_coordinator(peer)
            with runtime.query_lease() as view:
                self.assertEqual(view.exact_records('hello')[0].target_raw, 'first')
            other_source = root / 'other.jsonl'
            other_source.write_text('{"source":"hello","target":"independent"}\n')
            other_identity = CanonicalResourceIdentity.from_configured_jsonl('tm.other', other_source)
            other = store.ResourceStoreCoordinator(resource_identity=other_identity, canonical_store_id='store.other')
            self.addCleanup(other.close_family_session)
            other_service = migration.TMMigrationService(resource_identity=other_identity,
                canonical_store_id='store.other', coordinator=other)
            self.assertIs(type(other_service.activate_initial(other_source, other_identity.resource_id)), migration.MigrationReport)
            pending = root / f'.{identity.canonical_sidecar_path.name}.localcat-schema-upgrade.bad.pending'
            pending.mkdir()
            with self.assertRaisesRegex(store.ActivationPreparationError, 'UPGRADE_PENDING_UNSAFE'):
                owner.recover_durable_activation()
            self.assertIs(owner._family, peer._family)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                with runtime.query_lease():
                    self.fail('same-family peer admitted after recovery failure')
            with store.SQLiteTMStore.from_coordinator(other).query_lease() as view:
                self.assertEqual(view.exact_records('hello')[0].target_raw, 'independent')
            self.assertTrue(pending.is_dir())
            # Only a complete retry through the same original family may
            # release the recovery stop; this fixture removes its own pending.
            retained_root = owner._family._root
            pending.rmdir()
            owner.recover_durable_activation()
            self.assertIs(owner._family._root, retained_root)
            with runtime.query_lease() as view:
                self.assertEqual(view.exact_records('hello')[0].target_raw, 'first')
            # A cold factory may also complete recovery, but it must consume
            # this same retained root rather than minting a replacement owner.
            pending.mkdir()
            with self.assertRaisesRegex(store.ActivationPreparationError, 'UPGRADE_PENDING_UNSAFE'):
                owner.recover_durable_activation()
            pending.rmdir()
            from tm_engine import open_canonical_tm_store
            reopened = open_canonical_tm_store(identity.configured_jsonl_path)
            self.assertIsNotNone(reopened)
            self.assertIs(reopened.coordinator._family._root, retained_root)
            reopened.coordinator.close_family_session()
            for item in (owner, peer, other):
                self.assertEqual(item._active_lease_count, 0)
                item.close_family_session()

    def test_cold_drain_timeout_preserves_ordinary_admission_and_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            identity, owner = self.setup_crash(root)
            owner.recover_durable_activation()
            runtime = store.SQLiteTMStore.from_coordinator(owner)
            owner._drain_timeout_seconds = 0.02
            entered, release = threading.Event(), threading.Event()

            def query():
                with runtime.query_lease() as view:
                    entered.set()
                    release.wait(5)
                    return view.exact_records('hello')[0].target_raw

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending_query = pool.submit(query)
                self.assertTrue(entered.wait(5))
                try:
                    with self.assertRaises(store.SQLiteStoreLifecycleError) as caught:
                        owner.recover_durable_activation()
                    self.assertEqual(caught.exception.code, 'STORE.FAMILY_BUSY')
                    self.assertFalse(owner._family._recovery_required)
                    self.assertEqual(owner.state, 'READY')
                finally:
                    release.set()
                self.assertEqual(pending_query.result(5), 'first')
            owner.recover_durable_activation()
            with runtime.query_lease() as view:
                self.assertEqual(view.exact_records('hello')[0].target_raw, 'first')
            self.assertEqual(owner.current_generation, 0)
            owner.close_family_session()

    def test_original_root_clone_before_first_recovery_sql_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder).resolve()
            root = parent / 'resource'
            root.mkdir()
            identity, owner = self.setup_crash(root)
            original_root = owner._family._root
            backend_type = type(owner._family._backend)
            real_guard = backend_type.guard_existing_for_mutation
            original = parent / 'original'
            clones = []

            def guard(backend, borrowed_root, path):
                self.assertIs(borrowed_root, original_root)
                root.rename(original)
                shutil.copytree(original, root)
                clones.append({p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
                return real_guard(backend, borrowed_root, path)

            with patch.object(backend_type, 'guard_existing_for_mutation', guard), patch.object(sqlite3, 'connect', side_effect=AssertionError('clone SQL')):
                with self.assertRaises((PlatformFileError, store.SQLiteStoreLifecycleError, store.ActivationPreparationError)):
                    owner.recover_durable_activation()
            self.assertEqual(len(clones), 1)
            self.assertEqual(clones[0], {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
            self.assertIs(owner._family._root, original_root)
            self.assertEqual(owner._family._state, 'FAILED')
            self.assertEqual(owner._family._active_count, 0)
            self.assertFalse(owner._family._operation_tokens)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                owner.recover_durable_activation()

    def test_owner_w1_close_failure_never_issues_success_or_masks_primary(self):
        for fail_body in (False, True):
            with self.subTest(fail_body=fail_body), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                identity, owner = self.setup_crash(root)
                real_close = LockLease.close
                real_row = recovery._recovery_receipt_row
                body_error = sqlite3.OperationalError('recovery primary')
                close_error = OSError('W1 close secondary')
                closed = []

                def close(lease):
                    real_close(lease)
                    closed.append(lease)
                    raise close_error

                def row(*args, **kwargs):
                    if fail_body:
                        raise body_error
                    return real_row(*args, **kwargs)

                with patch.object(LockLease, 'close', close), patch.object(recovery, '_recovery_receipt_row', row):
                    with self.assertRaises((store.ActivationPreparationError, OSError)) as caught:
                        owner.recover_durable_activation()
                if fail_body:
                    self.assertIs(caught.exception.__cause__, body_error)
                else:
                    self.assertIs(caught.exception, close_error)
                self.assertEqual(len(closed), 1)
                self.assertTrue(closed[0].closed)
                self.assertEqual(owner.state, 'FAILED')
                self.assertEqual(owner._state, 'ACTIVATING')
                self.assertEqual(owner._family._state, 'FAILED')
                self.assertEqual(owner._family._active_count, 0)
                self.assertFalse(owner._family._operation_tokens)
                self.assertTrue(journal._activation_journal_path(identity).exists())
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    owner.recover_durable_activation()
                owner.close_family_session()

    def test_cold_receipt_failure_matrix_preserves_primary_and_closes(self):
        for window, point in itertools.product(('before_receipt', 'after_receipt'), ('sql-open', 'sql-body', 'sql-close-before', 'sql-close-after',
                      'guard-close', 'primary-plus-sql-close', 'primary-plus-guard-close',
                      'manifest-terminal', 'manifest-close')):
            with self.subTest(window=window, point=point), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                identity, owner = self.setup_crash(root, window)
                connections, guards, receipt_entries, manifest_pending = [], [], [], []
                real_connect = sqlite3.connect
                real_complete = recovery._complete_recovered_receipt
                real_receipt_row = recovery._recovery_receipt_row
                real_guard_close = BoundExistingFileMutationGuard.close
                real_sql_close = store._close_activation_receipt_connection
                real_terminal = PendingPublication.terminal_reproof
                real_pending_close = PendingPublication.close
                real_manifest = recovery._complete_recovered_manifest
                primary = sqlite3.OperationalError('cold receipt body')

                def complete(port, *args, **kwargs):
                    self.assertIs(port.posix_activation_borrow.root, owner._family._root)
                    self.assertIsNotNone(port.receipt_handoff.guard)
                    receipt_entries.append(port.receipt_handoff)
                    guards.append(port.receipt_handoff.guard)
                    return real_complete(port, *args, **kwargs)

                def connect(*args, **kwargs):
                    if receipt_entries and point == 'sql-open':
                        raise sqlite3.OperationalError('cold receipt open')
                    connection = real_connect(*args, **kwargs)
                    connections.append(connection)
                    return connection

                def receipt_row(*args, **kwargs):
                    if receipt_entries and point in ('sql-body', 'primary-plus-sql-close', 'primary-plus-guard-close'):
                        raise primary
                    return real_receipt_row(*args, **kwargs)

                def close_sql(connection):
                    if point in ('sql-close-before', 'primary-plus-sql-close'):
                        raise sqlite3.OperationalError('cold receipt close')
                    real_sql_close(connection)
                    if point == 'sql-close-after':
                        raise sqlite3.OperationalError('cold receipt close')

                def close_guard(guard):
                    if guard in guards:
                        for connection in connections:
                            with self.assertRaises(sqlite3.ProgrammingError):
                                connection.execute('SELECT 1')
                    real_guard_close(guard)
                    if guard in guards and point in ('guard-close', 'primary-plus-guard-close'):
                        raise OSError('cold guard close')

                def manifest(*args, **kwargs):
                    pending = real_manifest(*args, **kwargs)
                    if pending is not None:
                        manifest_pending.append(pending)
                    return pending

                def terminal(pending):
                    if pending in manifest_pending:
                        if point == 'manifest-terminal':
                            raise OSError('cold manifest terminal')
                    return real_terminal(pending)

                def close_pending(pending):
                    target = pending in manifest_pending
                    real_pending_close(pending)
                    if target and point == 'manifest-close':
                        raise OSError('cold manifest close')

                with patch.object(recovery, '_complete_recovered_receipt', complete), patch.object(recovery, '_complete_recovered_manifest', manifest), patch.object(sqlite3, 'connect', connect), patch.object(recovery, '_recovery_receipt_row', receipt_row), patch.object(store, '_close_activation_receipt_connection', close_sql), patch.object(BoundExistingFileMutationGuard, 'close', close_guard), patch.object(PendingPublication, 'terminal_reproof', terminal), patch.object(PendingPublication, 'close', close_pending):
                    with self.assertRaises((store.ActivationPreparationError, OSError, sqlite3.Error)) as caught:
                        owner.recover_durable_activation()
                self.assertEqual(len(receipt_entries), 1)
                self.assertTrue(all(guard.closed for guard in guards))
                self.assertTrue(all(pending.closed for pending in manifest_pending))
                for connection in connections:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute('SELECT 1')
                if point.startswith('primary-') or point == 'sql-body':
                    self.assertIs(caught.exception.__cause__, primary)
                self.assertIsNone(owner.current_generation)
                self.assertEqual(owner.state, 'ACTIVATING')
                self.assertEqual(owner._state, 'ACTIVATING')
                self.assertEqual((owner._family._active_count, owner._active_lease_count), (0, 0))
                self.assertFalse(owner._family._operation_tokens)
                disk = journal._parse_activation_journal_bytes(journal._activation_journal_path(identity).read_bytes(), expected_journal_path=journal._activation_journal_path(identity))
                self.assertIn(disk.phase.value, ('DB_REPLACED', 'MANIFEST_PUBLISHED'))
                owner.close_family_session()


@unittest.skipIf(sys.platform == 'win32', 'POSIX 已知家族 legacy 完成')
class PosixColdLegacyPendingTests(unittest.TestCase):
    @contextmanager
    def legacy(self, *, registered=True):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / 'memory.jsonl'
            source.write_text('{"source":"hello","target":"first"}\n')
            identity = CanonicalResourceIdentity.from_configured_jsonl('tm.legacy', source)
            owner = (store.ResourceStoreCoordinator(resource_identity=identity,
                     canonical_store_id='store.legacy') if registered else None)
            try:
                yield root, identity, owner
            finally:
                if owner is not None:
                    owner.close_family_session()

    def pending(self, root, identity, kind):
        token = 'invalid!' if kind == 'malformed' else 'bad\n' if kind == 'unavailable' else 'token'
        path = root / f'.{identity.canonical_sidecar_path.name}.localcat-schema-upgrade.{token}.pending'
        if kind == 'directory':
            path.mkdir()
        elif kind == 'link':
            path.symlink_to('missing')
        elif kind == 'fifo':
            os.mkfifo(path)
        elif kind == 'hardlink':
            anchor = root / 'anchor'
            anchor.write_bytes(b'untrusted pending')
            os.link(anchor, path)
        else:
            path.write_bytes(b'untrusted pending')
        return path

    def invoke(self, route, identity, owner):
        if route in ('recover_durable_activation', 'rehydrate_runtime_authority'):
            return getattr(owner, route)()
        if route in ('registered-factory', 'explicit-factory'):
            return open_canonical_tm_store(identity.configured_jsonl_path,
                **({'expected_resource_id': identity.resource_id} if route == 'explicit-factory' else {}))
        service = migration.TMMigrationService(resource_identity=identity,
            canonical_store_id='store.legacy', coordinator=owner)
        return service.activate_initial(identity.configured_jsonl_path, identity.resource_id)

    def test_known_legacy_pending_kinds_are_preserved_without_business_publication(self):
        routes = ('recover_durable_activation', 'rehydrate_runtime_authority',
                  'registered-factory', 'explicit-factory', 'initial')
        for route, kind in itertools.product(routes,
                ('file', 'directory', 'link', 'fifo', 'hardlink', 'malformed', 'unavailable')):
            with self.subTest(route=route, kind=kind), self.legacy(registered=route != 'explicit-factory') as (root, identity, owner):
                pending = self.pending(root, identity, kind)
                before = pending.lstat()
                source_bytes = identity.configured_jsonl_path.read_bytes()
                if route == 'initial':
                    result = self.invoke(route, identity, owner)
                    self.assertIs(type(result), migration.MigrationFailure)
                    self.assertTrue(result.canonical_authority_ambiguous)
                    self.assertFalse(result.canonical_authority_published)
                else:
                    with self.assertRaises((store.ActivationPreparationError, ValueError)) as caught:
                        self.invoke(route, identity, owner)
                    self.assertIn('UPGRADE_PENDING_UNSAFE', str(caught.exception))
                after = pending.lstat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_size),
                                 (after.st_dev, after.st_ino, after.st_mode, after.st_size))
                self.assertEqual(identity.configured_jsonl_path.read_bytes(), source_bytes)
                self.assertFalse(identity.canonical_sidecar_path.exists())
                self.assertFalse(identity.snapshot_manifest_path.exists())
                self.assertFalse(journal._activation_journal_path(identity).exists())
                if owner is not None:
                    self.assertIsNone(owner.current_generation)
                    self.assertTrue(owner._family._recovery_required)
                    self.assertFalse(owner._family._operation_tokens)

    def test_incomplete_observation_blocks_known_legacy_routes(self):
        for route in ('recover_durable_activation', 'rehydrate_runtime_authority',
                      'registered-factory', 'explicit-factory', 'initial'):
            with self.subTest(route=route), self.legacy(registered=route != 'explicit-factory') as (root, identity, owner):
                from platform_fs import compose_platform_file_backend
                backend_type = type(owner._family._backend if owner is not None else compose_platform_file_backend(root))
                failure = PlatformFileError(PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, retryable=False)
                with patch.object(backend_type, 'observe_root_children', side_effect=failure) as observation:
                    if route == 'initial':
                        result = self.invoke(route, identity, owner)
                        self.assertIs(type(result), migration.MigrationFailure)
                        self.assertFalse(result.canonical_authority_published)
                    else:
                        with self.assertRaises((store.ActivationPreparationError, ValueError)):
                            self.invoke(route, identity, owner)
                    self.assertGreater(observation.call_count, 0)
                self.assertFalse(identity.canonical_sidecar_path.exists())

    def test_real_failure_then_none_retry_keeps_stop_until_full_pending_free_completion(self):
        for route in ('recover_durable_activation', 'rehydrate_runtime_authority', 'registered-factory'):
            with self.subTest(route=route), self.legacy() as (root, identity, owner):
                original_root = owner._family._root
                pending = self.pending(root, identity, 'directory')
                bad_journal = journal._activation_journal_path(identity)
                bad_journal.write_bytes(b'bad journal')
                with self.assertRaises(store.ActivationPreparationError):
                    owner.recover_durable_activation()
                self.assertTrue(owner._family._recovery_required)
                bad_journal.unlink()
                with self.assertRaises((store.ActivationPreparationError, ValueError)):
                    self.invoke(route, identity, owner)
                self.assertTrue(owner._family._recovery_required)
                # A context used merely to read facts is not completion evidence.
                with owner._family_transition(recovery=True) as borrow:
                    with owner._posix_recovery_port(borrow=borrow):
                        pass
                self.assertTrue(owner._family._recovery_required)
                pending.rmdir()
                self.assertIsNone(self.invoke(route, identity, owner))
                self.assertIs(owner._family._root, original_root)
                self.assertFalse(owner._family._recovery_required)
                self.assertFalse(owner._family._operation_tokens)

    def test_normal_known_legacy_none_and_initial_activation_remain_available(self):
        for route in ('recover_durable_activation', 'rehydrate_runtime_authority',
                      'registered-factory', 'explicit-factory'):
            with self.subTest(route=route), self.legacy(registered=route != 'explicit-factory') as (root, identity, owner):
                (root / 'unrelated-directory').mkdir()
                (root / 'unrelated-link').symlink_to('missing')
                self.assertIsNone(self.invoke(route, identity, owner))
                self.assertFalse(identity.canonical_sidecar_path.exists())
                if owner is not None:
                    self.assertEqual(owner.state, 'READY')
                    self.assertFalse(owner._family._recovery_required)
                    self.assertIs(type(self.invoke('initial', identity, owner)), migration.MigrationReport)
                    self.assertEqual(owner.current_generation, 0)

    def test_later_failed_recovery_revokes_completion_on_same_borrow(self):
        with self.legacy() as (root, identity, owner):
            bad = journal._activation_journal_path(identity)
            bad.write_bytes(b'bad journal')
            with self.assertRaises(store.ActivationPreparationError):
                owner.recover_durable_activation()
            bad.unlink()
            with owner._family_transition(recovery=True) as borrow:
                self.assertIsNone(owner.rehydrate_runtime_authority(_borrow=borrow))
                self.pending(root, identity, 'directory')
                with self.assertRaises(store.ActivationPreparationError):
                    owner.rehydrate_runtime_authority(_borrow=borrow)
            self.assertTrue(owner._family._recovery_required)
