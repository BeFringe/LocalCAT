"""Core 10.8：schema 源/目标 SQL 与原资源家族生命周期。"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_schema_upgrade as schema
import tm_sqlite_store as store
from tm_contracts import SchemaUpgradeReport
from tests.test_tm_schema_upgrade import _legacy_fixture, _service


@unittest.skipIf(sys.platform == "win32", "POSIX schema 生命周期")
class PosixSchemaLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity, self.prior, self.owner, self.activation_digest = _legacy_fixture(self.root, fts5_available=False)
        self.addCleanup(self.owner.close_family_session)
        self.service = _service(self.owner, self.identity)

    def test_public_upgrade_every_sql_entry_holds_original_family(self):
        real_connect = sqlite3.connect
        calls = []
        def connect(*args, **kwargs):
            self.assertTrue(self.owner._family._operation_tokens, "schema SQL 未持原家族借用")
            self.owner._family._root.reprove_location()
            calls.append(str(args[0]))
            return real_connect(*args, **kwargs)
        with patch.object(sqlite3, "connect", connect), patch.object(store, "_probe_fts5", return_value=False):
            result = self.service.upgrade_schema(self.prior.staged_db_path)
        self.assertIs(type(result), SchemaUpgradeReport, result)
        self.assertGreater(len(calls), 8)
        self.assertEqual(self.owner._family._active_count, 0)

    def test_direct_ticket_mint_holds_family_before_first_sql(self):
        real_connect = sqlite3.connect
        def connect(*args, **kwargs):
            self.assertTrue(self.owner._family._operation_tokens, "ticket preflight 未持原家族借用")
            return real_connect(*args, **kwargs)
        with patch.object(sqlite3, "connect", connect):
            ticket = self.owner.prepare_schema_upgrade_ticket()
        self.assertTrue(ticket.backup_path.is_file())
        self.owner.retire_schema_upgrade_ticket(ticket)
        self.owner.release_schema_upgrade_locator_snapshot()

    def test_backup_closes_both_connections_before_raw_guard_and_hash(self):
        from platform_fs_contracts import BoundExistingFileMutationGuard
        alive = set()
        closed = []
        real_connect = sqlite3.connect
        real_guard_close = BoundExistingFileMutationGuard.close
        real_capture = schema.SchemaUpgradeIO.capture
        class Connection(sqlite3.Connection):
            def close(connection):
                result = super(Connection, connection).close()
                alive.discard(connection)
                closed.append(connection)
                return result
        def connect(*args, **kwargs):
            kwargs["factory"] = Connection
            connection = real_connect(*args, **kwargs)
            alive.add(connection)
            return connection
        def close_guard(guard):
            self.assertFalse(alive, "raw guard 早于backup双连接关闭")
            return real_guard_close(guard)
        def capture(io, path, **kwargs):
            self.assertFalse(alive, "raw hash 与backup SQL并存")
            return real_capture(io, path, **kwargs)
        with self.owner._schema_upgrade_scope() as io:
            with patch.object(sqlite3, "connect", connect), patch.object(BoundExistingFileMutationGuard, "close", close_guard), patch.object(schema.SchemaUpgradeIO, "capture", capture):
                result = schema._create_schema_upgrade_backup(self.prior.staged_db_path, self.root / "backup.sqlite3", io=io)
            self.assertEqual(len(set(closed)), 2)
            self.assertEqual(len(result[1]), 64)

    def test_failed_sql_close_retains_exact_connections_and_guards_until_retry(self):
        from platform_fs_contracts import BoundExistingFileMutationGuard
        real_connect = sqlite3.connect
        real_guard_close = BoundExistingFileMutationGuard.close
        blocked = True
        connections = []
        closed_guards = []
        class Connection(sqlite3.Connection):
            def close(connection):
                if blocked:
                    raise OSError("schema close remains blocked")
                return super(Connection, connection).close()
        def connect(*args, **kwargs):
            kwargs["factory"] = Connection
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        def close_guard(guard):
            closed_guards.append(guard)
            return real_guard_close(guard)
        with patch.object(sqlite3, "connect", connect), patch.object(BoundExistingFileMutationGuard, "close", close_guard):
            with self.assertRaisesRegex(OSError, "schema close remains blocked"):
                with self.owner._schema_upgrade_scope() as io:
                    schema._create_schema_upgrade_backup(self.prior.staged_db_path, self.root / "backup.sqlite3", io=io)
            self.assertEqual(len(connections), 2)
            self.assertEqual(closed_guards, [])
            self.assertTrue(self.owner._family._pending_activation_closes)
            self.assertTrue(self.owner._family._recovery_required)
            blocked = False
            with self.owner._family_transition(recovery=True) as operation:
                with self.owner._posix_recovery_port(borrow=operation):
                    pass
            self.assertEqual(len(closed_guards), 2)
            self.assertFalse(self.owner._family._pending_activation_closes)

    def test_directory_drift_before_backup_sql_rejects_real_continuation(self):
        import shutil
        real_connect = sqlite3.connect
        moved = self.root.with_name(self.root.name + ".moved")
        self.addCleanup(lambda: shutil.rmtree(moved, ignore_errors=True))
        sql_calls = []
        def connect(*args, **kwargs):
            sql_calls.append(args)
            return real_connect(*args, **kwargs)
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            with self.owner._schema_upgrade_scope() as io:
                self.root.rename(moved)
                shutil.copytree(moved, self.root)
                with patch.object(sqlite3, "connect", connect):
                    schema._create_schema_upgrade_backup(self.prior.staged_db_path, self.root / "backup.sqlite3", io=io)
        self.assertEqual(sql_calls, [])
        self.assertFalse((self.root / "backup.sqlite3").exists())
        self.assertFalse((moved / "backup.sqlite3").exists())

    def test_persistent_journal_failure_stops_family_until_original_cancel_owner_finishes(self):
        from tm_contracts import SchemaUpgradeFailure
        prior_bytes = self.prior.staged_db_path.read_bytes()
        with patch.object(store, "_probe_fts5", return_value=False):
            peer = store.ResourceStoreCoordinator(
                self.prior, canonical_store_id=self.owner.canonical_store_id,
                _allow_legacy_schema=True, _allow_active=True,
                _expected_active_generation=self.owner.current_generation,
                _expected_activation_digest=self.activation_digest,
            )
        self.addCleanup(peer.close_family_session)
        runtime = store.SQLiteTMStore.from_coordinator(peer)
        with patch.object(store, "_probe_fts5", return_value=False), patch.object(store, "_write_activation_journal", side_effect=OSError("persistent journal failure")) as writes:
            result = self.service.upgrade_schema(self.prior.staged_db_path)
        self.assertIs(type(result), SchemaUpgradeFailure)
        self.assertGreaterEqual(writes.call_count, 2)
        self.assertEqual(self.owner.state, "ACTIVATING")
        self.assertTrue(self.owner._family._recovery_required)
        preparation = self.owner._preparation
        self.assertIsNotNone(preparation)
        self.assertEqual(self.prior.staged_db_path.read_bytes(), prior_bytes)
        from tests.test_tm_sqlite_store import _draft
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            runtime.exact_records("prior")
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            runtime.append(_draft("peer", "must not append"))
        with patch.object(store, "_probe_fts5", return_value=False):
            self.owner.recover_durable_activation()
        self.assertEqual(self.owner.state, "READY")
        self.assertFalse(self.owner._family._recovery_required)
        self.assertEqual(self.prior.staged_db_path.read_bytes(), prior_bytes)

    def _capture_failure(self, *, business: bool, closing: bool):
        import tm_activation_journal as journal
        from platform_fs_contracts import OpaqueAuthority, PlatformFileError, PlatformFileErrorCode
        primary = PlatformFileError(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)
        close_error = PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False)
        real_capture = journal._capture_posix_bound_content
        real_close = OpaqueAuthority.close
        retained = []
        blocked = closing
        def capture(backend, opened):
            if not retained:
                retained.append(opened)
                if business:
                    raise primary
            return real_capture(backend, opened)
        def close(opened):
            if blocked and retained and opened is retained[0]:
                raise close_error
            return real_close(opened)
        with patch.object(journal, "_capture_posix_bound_content", capture), patch.object(OpaqueAuthority, "close", close):
            with self.assertRaises(BaseException) as raised:
                self.service.upgrade_schema(self.prior.staged_db_path)
            caught = raised.exception
            if business and closing:
                self.assertEqual(getattr(caught, "reason_code", None), primary.code)
                self.assertIs(caught.__cause__, primary)
                self.assertTrue(any(close_error.code in note for note in caught.__notes__))
            else:
                self.assertIs(caught, primary if business else close_error)
            if closing:
                self.assertTrue(self.owner._family._recovery_required)
                self.assertTrue(self.owner._family._pending_activation_closes)
                self.assertFalse(retained[0].closed)
                with self.assertRaises(Exception):
                    self.owner.recover_durable_activation()
                self.assertTrue(self.owner._family._pending_activation_closes)
                self.assertFalse(retained[0].closed)
                blocked = False
                with self.owner._family_transition(recovery=True) as operation:
                    with self.owner._posix_recovery_port(borrow=operation):
                        pass
                self.assertTrue(retained[0].closed)
                self.assertFalse(self.owner._family._pending_activation_closes)

    def test_capture_business_error_preserves_exact_platform_error(self):
        self._capture_failure(business=True, closing=False)

    def test_capture_close_error_retains_exact_reader(self):
        self._capture_failure(business=False, closing=True)

    def test_capture_dual_failure_retains_primary_and_exact_reader(self):
        self._capture_failure(business=True, closing=True)

    def test_real_hot_journal_recovers_before_schema_preservation_baseline(self):
        import subprocess
        database = self.prior.staged_db_path
        before = database.read_bytes()
        result = subprocess.run([sys.executable, "-B", "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA journal_mode=DELETE')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE tm_record SET target_raw = ?", ('uncommitted' * 10000,))
os._exit(0)
""", str(database)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(database.with_name(database.name + "-journal").exists())
        self.assertNotEqual(database.read_bytes(), before)
        with patch.object(store, "_probe_fts5", return_value=False):
            outcome = self.service.upgrade_schema(database)
        self.assertIs(type(outcome), SchemaUpgradeReport, outcome)
        self.assertFalse(database.with_name(database.name + "-journal").exists())
        from contextlib import closing
        with closing(sqlite3.connect(outcome.backup_path)) as connection:
            self.assertFalse(connection.execute("SELECT 1 FROM tm_record WHERE target_raw LIKE 'uncommitted%' LIMIT 1").fetchall())

    def test_outer_close_failure_keeps_durable_publication_fact(self):
        import tm_migration as migration
        expected_generation = self.owner.current_generation + 1
        original_exit = migration._InitialActivationResourceReservation.__exit__
        def fail_tail(reservation, *args):
            original_exit(reservation, *args)
            raise OSError("schema outer close failed")
        with patch.object(store, "_probe_fts5", return_value=False), patch.object(migration._InitialActivationResourceReservation, "__exit__", fail_tail):
            with self.assertRaises(OSError) as raised:
                self.service.upgrade_schema(self.prior.staged_db_path)
        self.assertEqual(str(raised.exception), "schema outer close failed")
        self.assertIn(f"Schema generation durably published: {expected_generation}", raised.exception.__notes__)
        self.assertEqual(self.owner.current_generation, expected_generation)
        self.assertTrue(self.owner._family._recovery_required)

    def test_real_root_drift_and_actual_reader_close_interrupt_preserve_both(self):
        import shutil
        import tm_activation_journal as journal
        from platform_fs_contracts import OpaqueAuthority, PlatformFileError
        original_capture = journal._capture_posix_bound_content
        original_close = OpaqueAuthority.close
        moved = self.root.with_name(self.root.name + ".capture-moved")
        self.addCleanup(lambda: shutil.rmtree(moved, ignore_errors=True))
        readers = []
        interrupt = KeyboardInterrupt("after actual schema reader close")
        def capture(backend, opened):
            if not readers:
                readers.append(opened)
                self.root.rename(moved)
                shutil.copytree(moved, self.root)
            return original_capture(backend, opened)
        def close(opened):
            was_open = not opened.closed
            original_close(opened)
            if readers and opened is readers[0] and was_open:
                raise interrupt
        with patch.object(journal, "_capture_posix_bound_content", capture), patch.object(OpaqueAuthority, "close", close):
            with self.assertRaises(BaseException) as raised:
                self.service.upgrade_schema(self.prior.staged_db_path)
        caught = raised.exception
        facts = []
        pending = [caught]
        seen = set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            facts.append(current)
            pending.extend((current.__cause__, current.__context__))
        self.assertTrue(any(isinstance(fact, (PlatformFileError, store.SQLiteStoreLifecycleError)) for fact in facts), facts)
        self.assertTrue(any("KeyboardInterrupt" in note for fact in facts for note in getattr(fact, "__notes__", ())))
        self.assertTrue(readers[0].closed, "reader已经走实际close后才触发中断")
        self.assertTrue(self.owner._family._pending_activation_closes)
        self.assertTrue(self.owner._family._recovery_required)
        self.assertNotEqual(self.owner.state, "READY")

    def test_ticket_locator_copy_failure_removes_only_owned_partial_candidate(self):
        from platform_fs_contracts import CandidateFile
        original_write = CandidateFile.write_chunks
        before = set(self.root.iterdir())
        def write_then_fail(candidate, *args, **kwargs):
            original_write(candidate, *args, **kwargs)
            raise OSError("schema locator candidate write failed")
        with patch.object(CandidateFile, "write_chunks", write_then_fail):
            with self.assertRaisesRegex(OSError, "schema locator candidate write failed"):
                self.owner.prepare_schema_upgrade_ticket()
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIsNone(self.owner._schema_upgrade_ticket)
        self.assertIsNone(self.owner.schema_upgrade_locator_snapshot)
        self.assertEqual(self.owner.state, "READY")

    def _public_persistent_backup_close(self, side, *, fail_recovery_close=False):
        import tm_activation_journal as journal
        from platform_fs_contracts import BoundExistingFileMutationGuard
        with patch.object(store, "_probe_fts5", return_value=False):
            peer = store.ResourceStoreCoordinator(
                self.prior, canonical_store_id=self.owner.canonical_store_id,
                _allow_legacy_schema=True, _allow_active=True,
                _expected_active_generation=self.owner.current_generation,
                _expected_activation_digest=self.activation_digest,
            )
        self.addCleanup(peer.close_family_session)
        runtime = store.SQLiteTMStore.from_coordinator(peer)
        original_connect = sqlite3.connect
        original_capture = journal._CallerHeldPosixActivationBorrow.capture
        original_remove = schema.SchemaUpgradeIO.remove
        original_promote = schema.SchemaUpgradeIO.promote
        original_guard_close = BoundExistingFileMutationGuard.close
        group = []
        cursors = []
        statement_closes = set()
        live = set()
        blocked = set()
        raw_while_live = []
        failure = OSError(f"persistent schema backup {side} close")
        class Cursor(sqlite3.Cursor):
            def close(cursor):
                result = super(Cursor, cursor).close()
                statement_closes.add(cursor)
                return result
        class Connection(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                if connection.backup_target and sql in ("PRAGMA journal_mode=DELETE", "PRAGMA synchronous=FULL"):
                    cursor = connection.cursor(factory=Cursor)
                    cursor.execute(sql, *args, **kwargs)
                    cursors.append(cursor)
                    return cursor
                return super(Connection, connection).execute(sql, *args, **kwargs)
            def backup(connection, destination, *args, **kwargs):
                result = super(Connection, connection).backup(destination, *args, **kwargs)
                if not group:
                    group.extend((connection, destination))
                    blocked.add(connection if side == "source" else destination)
                return result
            def close(connection):
                if connection in blocked:
                    raise failure
                result = super(Connection, connection).close()
                live.discard(connection)
                return result
        def connect(*args, **kwargs):
            kwargs["factory"] = Connection
            connection = original_connect(*args, **kwargs)
            connection.backup_target = ".localcat-schema-upgrade." in str(args[0])
            live.add(connection)
            return connection
        def observe(label, operation):
            def call(*args, **kwargs):
                if group and (any(connection in live for connection in group) or any(cursor not in statement_closes for cursor in cursors)):
                    raw_while_live.append(label)
                return operation(*args, **kwargs)
            return call
        caught = None
        outcome = None
        try:
            with patch.object(sqlite3, "connect", connect), patch.object(journal._CallerHeldPosixActivationBorrow, "capture", observe("capture/proof", original_capture)), patch.object(schema.SchemaUpgradeIO, "remove", observe("remove", original_remove)), patch.object(schema.SchemaUpgradeIO, "promote", observe("promote", original_promote)), patch.object(BoundExistingFileMutationGuard, "close", observe("raw guard close", original_guard_close)), patch.object(store, "_probe_fts5", return_value=False):
                try:
                    outcome = self.service.upgrade_schema(self.prior.staged_db_path)
                except BaseException as error:
                    caught = error
                self.assertEqual(len(group), 2, "实际双连接backup必须执行")
                self.assertTrue(self.owner._family._pending_activation_closes)
                pending = tuple(self.owner._family._pending_activation_closes)
                with self.assertRaises(Exception):
                    self.owner.recover_durable_activation()
                self.assertEqual(tuple(self.owner._family._pending_activation_closes), pending)
                exact = next(iter(blocked))
                self.assertIsNotNone(exact.execute("PRAGMA schema_version").fetchone())
                self.assertEqual(raw_while_live, [], "任一SQL仍活着时禁止全部raw观察/删除/发布/guard close")
                self.assertIsNone(outcome, "关闭未完成时不能返回VERIFIED_UNCHANGED")
                self.assertIs(caught, failure)
                self.assertNotEqual(self.owner.state, "READY")
                from tests.test_tm_sqlite_store import _draft
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    runtime.exact_records("prior")
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    runtime.append(_draft("peer", "must not append"))
                blocked.clear()
                if fail_recovery_close:
                    from platform_fs_contracts import LockLease
                    original_lease_close = LockLease.close
                    closing = OSError("schema prior recovery outer close")
                    def lease_close(lease):
                        original_lease_close(lease)
                        raise closing
                    with patch.object(LockLease, "close", lease_close):
                        with self.assertRaises(OSError) as recovery_error:
                            self.owner.recover_durable_activation()
                    self.assertIs(recovery_error.exception, closing)
                    self.assertTrue(any("Schema prior recovery verified" in note for note in closing.__notes__))
                    self.assertIsNotNone(self.owner._schema_upgrade_close_prior)
                    self.assertTrue(self.owner._family._recovery_required)
                    self.assertNotEqual(self.owner.state, "READY")
                    self.assertFalse(self.owner._family._pending_activation_closes)
                    return
                self.owner.recover_durable_activation()
                self.assertFalse(self.owner._family._pending_activation_closes)
                self.assertFalse(self.owner._family._recovery_required)
                self.assertEqual(self.owner.state, "READY")
                for connection in group:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("PRAGMA schema_version")
                self.assertEqual(len(cursors), 2, "只观察产品backup的两个PRAGMA statement")
                self.assertEqual(set(cursors), statement_closes, "产品自身明确关闭每个真实PRAGMA cursor")
        finally:
            blocked.clear()
            if self.owner._family._pending_activation_closes:
                with self.owner._family_transition(recovery=True) as operation:
                    with self.owner._posix_recovery_port(borrow=operation):
                        pass

    def test_public_backup_source_close_failure_blocks_failure_raw_io_until_owner_retry(self):
        self._public_persistent_backup_close("source")

    def test_public_backup_destination_close_failure_blocks_failure_raw_io_until_owner_retry(self):
        self._public_persistent_backup_close("destination")

    def test_prior_schema_recovery_outer_close_preserves_verified_fact_and_prior_owner(self):
        self._public_persistent_backup_close("source", fail_recovery_close=True)

    def _configured_schema_sql_errors(self, *, business, closing):
        from platform_fs_contracts import BoundExistingFileMutationGuard, PlatformFileError, PlatformFileErrorCode
        primary = PlatformFileError(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)
        close_error = PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False)
        original_close = BoundExistingFileMutationGuard.close
        guards = []
        def close(guard):
            was_open = not guard.closed
            original_close(guard)
            if closing and was_open and not guards:
                guards.append(guard)
                raise close_error
        with patch.object(BoundExistingFileMutationGuard, "close", close):
            with self.assertRaises(BaseException) as raised:
                with self.owner._schema_upgrade_scope() as io:
                    with store._open_configured_connection(self.prior.staged_db_path, require_existing=True, _stage_borrow=io):
                        if business:
                            raise primary
        caught = raised.exception
        if business and closing:
            self.assertEqual(caught.reason_code, primary.code)
            self.assertIs(caught.__cause__, primary)
            self.assertTrue(any(close_error.code in note for note in caught.__notes__))
        else:
            self.assertIs(caught, primary if business else close_error)
        if closing:
            self.assertTrue(guards[0].closed)
            self.assertTrue(self.owner._family._pending_activation_closes)
            with self.owner._family_transition(recovery=True) as operation:
                with self.owner._posix_recovery_port(borrow=operation):
                    pass

    def test_configured_schema_sql_business_error_preserves_original_type(self):
        self._configured_schema_sql_errors(business=True, closing=False)

    def test_configured_schema_sql_close_error_preserves_original_type(self):
        self._configured_schema_sql_errors(business=False, closing=True)

    def test_configured_schema_sql_double_error_preserves_exact_primary(self):
        self._configured_schema_sql_errors(business=True, closing=True)

    def test_public_schema_sql_double_error_does_not_enter_nonexistent_cleanup(self):
        from contextlib import contextmanager
        from platform_fs_contracts import BoundExistingFileMutationGuard, PlatformFileError, PlatformFileErrorCode
        primary = PlatformFileError(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)
        close_error = PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False)
        original_reprove = schema.SchemaUpgradeIO.reprove_sql
        original_close = BoundExistingFileMutationGuard.close
        original_open = store._open_configured_connection
        calls = 0
        failed = False
        guards = []
        sql_errors = []
        @contextmanager
        def observe(*args, **kwargs):
            try:
                with original_open(*args, **kwargs) as connection:
                    yield connection
            except BaseException as error:
                sql_errors.append(error)
                raise
        def reprove(io, path, purpose):
            nonlocal calls, failed
            original_reprove(io, path, purpose)
            calls += 1
            if calls == 2:
                failed = True
                raise primary
        def close(guard):
            was_open = not guard.closed
            original_close(guard)
            if failed and was_open and not guards:
                guards.append(guard)
                raise close_error
        with patch.object(store, "_open_configured_connection", observe), patch.object(schema.SchemaUpgradeIO, "reprove_sql", reprove), patch.object(BoundExistingFileMutationGuard, "close", close), patch.object(store, "_probe_fts5", return_value=False), patch.object(self.owner, "retry_failed_activation_cleanup", wraps=self.owner.retry_failed_activation_cleanup) as cleanup:
            with self.assertRaises(BaseException) as raised:
                self.service.upgrade_schema(self.prior.staged_db_path)
            self.assertEqual(cleanup.call_count, 0)
        self.assertTrue(failed)
        self.assertTrue(guards[0].closed)
        self.assertIsNone(self.owner._cleanup_reservation)
        self.assertIs(raised.exception, sql_errors[0])
        self.assertEqual(raised.exception.reason_code, primary.code)
        self.assertIs(raised.exception.__cause__, primary)
        self.assertTrue(any(close_error.code in note for note in raised.exception.__notes__))
        self.assertTrue(self.owner._family._pending_activation_closes)
        self.assertNotEqual(self.owner.state, "READY")
        with patch.object(store, "_probe_fts5", return_value=False):
            self.owner.recover_durable_activation()
        self.assertEqual(self.owner.state, "READY")
        self.assertFalse(self.owner._family._pending_activation_closes)

    def _early_schema_close(self, *, capture=False, invalid_prior=False, guard_close=False, outer_close=False):
        import tm_activation_journal as journal
        from platform_fs_contracts import OpaqueAuthority, BoundExistingFileMutationGuard, LockLease
        with patch.object(store, "_probe_fts5", return_value=False):
            peer = store.ResourceStoreCoordinator(
                self.prior, canonical_store_id=self.owner.canonical_store_id,
                _allow_legacy_schema=True, _allow_active=True,
                _expected_active_generation=self.owner.current_generation,
                _expected_activation_digest=self.activation_digest,
            )
        self.addCleanup(peer.close_family_session)
        runtime = store.SQLiteTMStore.from_coordinator(peer)
        real_connect, real_close = sqlite3.connect, OpaqueAuthority.close
        real_guard_close = BoundExistingFileMutationGuard.close
        real_capture = journal._capture_posix_bound_content
        exact_sql, exact_readers, executed = [], [], []
        blocked = True
        fault = OSError("first capture close remains blocked" if capture else "initial recover_store close remains blocked")
        class Connection(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                if connection is exact_sql[0]:
                    executed.append(sql)
                return result
            def close(connection):
                if not capture and not guard_close and blocked and exact_sql and connection is exact_sql[0]:
                    raise fault
                return super().close()
        def connect(*args, **kwargs):
            if exact_sql:
                return real_connect(*args, **kwargs)
            kwargs["factory"] = Connection
            connection = real_connect(*args, **kwargs)
            if not exact_sql:
                exact_sql.append(connection)
            return connection
        def observe_capture(backend, reader):
            result = real_capture(backend, reader)
            if capture and not exact_readers:
                exact_readers.append(reader)
            return result
        def close_reader(authority):
            if capture and blocked and exact_readers and authority is exact_readers[0]:
                raise fault
            return real_close(authority)
        def close_guard(guard):
            if guard_close and blocked and guard is self.owner._schema_upgrade_prior_guard:
                raise fault
            return real_guard_close(guard)
        saved_manifest = self.identity.snapshot_manifest_path.read_bytes()
        try:
            with patch.object(sqlite3, "connect", connect), patch.object(journal, "_capture_posix_bound_content", observe_capture), patch.object(OpaqueAuthority, "close", close_reader), patch.object(BoundExistingFileMutationGuard, "close", close_guard), patch.object(store, "_probe_fts5", return_value=False):
                with self.assertRaises(OSError) as raised:
                    self.service.upgrade_schema(self.prior.staged_db_path)
                self.assertIs(raised.exception, fault)
                self.assertIn("PRAGMA schema_version", executed)
                self.assertIsNotNone(self.owner._schema_upgrade_close_prior)
                original_guard = self.owner._schema_upgrade_prior_guard
                self.assertIsNotNone(original_guard)
                self.assertFalse(original_guard.closed)
                pending = tuple(self.owner._family._pending_activation_closes)
                self.assertTrue(pending)
                with self.assertRaises(Exception):
                    self.owner.recover_durable_activation()
                self.assertEqual(tuple(self.owner._family._pending_activation_closes), pending)
                self.assertIs(self.owner._schema_upgrade_prior_guard, original_guard)
                from tests.test_tm_sqlite_store import _draft
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    runtime.exact_records("same")
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    runtime.append(_draft("peer", "must not append"))
                blocked = False
                if invalid_prior:
                    self.identity.snapshot_manifest_path.write_bytes(b"invalid manifest")
                    with self.assertRaises(Exception):
                        self.owner.recover_durable_activation()
                    self.assertNotEqual(self.owner.state, "READY")
                    self.assertTrue(self.owner._family._recovery_required)
                    self.identity.snapshot_manifest_path.write_bytes(saved_manifest)
                if outer_close:
                    real_lease_close = LockLease.close
                    closing = OSError("early schema recovery outer close")
                    def close_lease(lease):
                        real_lease_close(lease)
                        raise closing
                    with patch.object(LockLease, "close", close_lease):
                        with self.assertRaises(OSError) as recovered:
                            self.owner.recover_durable_activation()
                    self.assertIs(recovered.exception, closing)
                    self.assertTrue(any("Schema prior recovery verified" in note for note in closing.__notes__))
                    self.assertIsNotNone(self.owner._schema_upgrade_close_prior[1])
                    self.assertTrue(original_guard.closed)
                    self.assertTrue(self.owner._family._recovery_required)
                    self.assertNotEqual(self.owner.state, "READY")
                    return
                self.owner.recover_durable_activation()
                self.assertEqual(self.owner.state, "READY")
                self.assertFalse(self.owner._family._pending_activation_closes)
                self.assertFalse(self.owner._family._recovery_required)
                self.assertIsNone(self.owner._schema_upgrade_close_prior)
                self.assertIsNone(self.owner._schema_upgrade_prior_guard)
                self.assertTrue(original_guard.closed)
                with self.assertRaises(sqlite3.ProgrammingError):
                    exact_sql[0].execute("PRAGMA schema_version")
                if capture:
                    self.assertTrue(exact_readers[0].closed)
                # 显式v1 owner原路径先由原借用实际读SQL；不宣称其为canonical runtime。
                with self.owner._schema_upgrade_scope() as io:
                    with io.connection(self.prior.staged_db_path) as connection:
                        rows = connection.execute("SELECT target_raw FROM tm_record WHERE source_raw='same'").fetchall()
                self.assertTrue(rows)
                outcome = self.service.upgrade_schema(self.prior.staged_db_path)
                self.assertIs(type(outcome), SchemaUpgradeReport, outcome)
                self.assertTrue(store.SQLiteTMStore.from_coordinator(self.owner).exact_records("same"))
        finally:
            blocked = False
            self.identity.snapshot_manifest_path.write_bytes(saved_manifest)
            if self.owner._family._pending_activation_closes:
                with self.owner._family_transition(recovery=True) as operation:
                    with self.owner._posix_recovery_port(borrow=operation):
                        pass

    def test_first_real_schema_sql_close_resumes_original_owner(self):
        self._early_schema_close()

    def test_first_schema_capture_close_resumes_original_owner(self):
        self._early_schema_close(capture=True)

    def test_early_schema_recovery_rejects_unprovable_prior(self):
        self._early_schema_close(invalid_prior=True)

    def test_first_schema_proof_guard_close_resumes_original_owner(self):
        self._early_schema_close(guard_close=True)

    def test_early_schema_recovery_outer_close_retains_verified_prior(self):
        self._early_schema_close(outer_close=True)

    def test_first_schema_proof_rejects_same_bytes_replacement_of_guarded_db(self):
        import shutil
        with patch.object(store, "_probe_fts5", return_value=False):
            peer = store.ResourceStoreCoordinator(
                self.prior, canonical_store_id=self.owner.canonical_store_id,
                _allow_legacy_schema=True, _allow_active=True,
                _expected_active_generation=self.owner.current_generation,
                _expected_activation_digest=self.activation_digest,
            )
        self.addCleanup(peer.close_family_session)
        original_capture = schema.SchemaUpgradeIO.capture
        replacement = self.root / "guard-pinned-original.sqlite3"
        guards = []
        def capture(io, path, **kwargs):
            guard = kwargs.get("expected_guard")
            if guard is not None and not guards:
                self.assertFalse(io._sql_paths)
                self.assertFalse(io._connections)
                guards.append(guard)
                path.rename(replacement)
                shutil.copyfile(replacement, path)
            return original_capture(io, path, **kwargs)
        before = self.prior.staged_db_path.read_bytes()
        with patch.object(schema.SchemaUpgradeIO, "capture", capture):
            with self.assertRaises(Exception):
                self.service.upgrade_schema(self.prior.staged_db_path)
        self.assertEqual(len(guards), 1)
        self.assertEqual(self.prior.staged_db_path.read_bytes(), before)
        self.assertEqual(replacement.read_bytes(), before)
        self.assertTrue(guards[0].closed, "无活SQL时原owner仍完成可关guard")
        self.assertEqual(self.owner.state, "FAILED")
        self.assertIs(peer._family, self.owner._family)
        self.assertEqual(peer.state, "FAILED")
