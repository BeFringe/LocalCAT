"""Core 10.4：真实 builder/seal → DB_REPLACED → receipt SQL。"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from pathlib import Path, PurePath
import sqlite3
import sys
import unittest
from unittest.mock import patch

import tm_activation_recovery as recovery
import tm_activation_journal as journal
import tm_migration as migration
import tm_sqlite_store as store
from tests import test_tm_posix_builder_seal as fixtures
from platform_fs_contracts import PendingPublication, BoundExistingFileMutationGuard, CandidateFile
from platform_fs import compose_platform_file_backend


class _AfterReceipt(Exception):
    pass


@unittest.skipIf(sys.platform == "win32", "POSIX receipt 交接")
class PosixReceiptHandoffTests(unittest.TestCase):
    setUp = fixtures.PosixBuilderSealTests.setUp
    attempt = fixtures.PosixBuilderSealTests.attempt
    build = fixtures.PosixBuilderSealTests.build

    @contextmanager
    def prepared(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            self.build(preflight, attempt, salt)
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            prepared = self.owner.activate(sealed)
            handle = self.owner.publish_prepared_activation(prepared)
            yield reservation, attempt, prepared, handle

    def publish(self, reservation, attempt, prepared, handle):
        # Production passes the original reservation borrow at this seam.
        inputs = reservation.posix_activation_inputs(attempt)
        return self.owner.publish_activation(prepared, handle, **inputs)

    def test_real_handoff_orders_journal_pending_sql_and_final_capture(self):
        with self.prepared() as (reservation, attempt, prepared, handle):
            events = []
            connections = []
            real_connect = sqlite3.connect
            backend_type = type(compose_platform_file_backend(self.root))
            real_guard = backend_type.guard_existing_for_mutation
            real_terminal = PendingPublication.terminal_reproof
            real_close = PendingPublication.close
            real_guard_close = BoundExistingFileMutationGuard.close
            guards = []

            def connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connections.append(connection)
                events.append("sql")
                return connection

            def guard(backend, root, path):
                self.assertIs(root, reservation._root)
                self.assertEqual(root, self.owner._family._root)
                disk = journal._parse_activation_journal_bytes(handle.journal_path.read_bytes(), expected_journal_path=handle.journal_path)
                self.assertEqual(disk.phase.value, "DB_REPLACED")
                self.assertEqual(self.owner.state, "ACTIVATING")
                result = real_guard(backend, root, path)
                guards.append(result)
                events.append("guard")
                return result

            def terminal(pending):
                if pending.preliminary_facts().content_sha256 == bytes.fromhex(handle._record.stage_db_digest):
                    self.assertTrue(guards)
                    self.assertEqual(pending.retained_destination().identity(), guards[-1].reprove())
                    events.append("pending-terminal")
                return real_terminal(pending)

            def close(pending):
                is_db = pending.preliminary_facts().content_sha256 == bytes.fromhex(handle._record.stage_db_digest)
                result = real_close(pending)
                if is_db:
                    events.append("pending-close")
                return result

            def close_guard(value):
                if value in guards:
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute("SELECT 1")
                    events.append("guard-close")
                return real_guard_close(value)

            with patch.object(sqlite3, "connect", connect), patch.object(backend_type, "guard_existing_for_mutation", guard), patch.object(PendingPublication, "terminal_reproof", terminal), patch.object(PendingPublication, "close", close), patch.object(BoundExistingFileMutationGuard, "close", close_guard), patch.object(recovery, "_publish_activation_manifest", side_effect=_AfterReceipt):
                with self.assertRaises(_AfterReceipt):
                    self.publish(reservation, attempt, prepared, handle)
            self.assertIn("guard", events)
            self.assertLess(events.index("guard"), events.index("pending-terminal"))
            self.assertLess(events.index("pending-terminal"), events.index("pending-close"))
            self.assertLess(events.index("pending-close"), events.index("sql"))
            self.assertLess(events.index("sql"), events.index("guard-close"))
            self.assertIsNone(self.owner.current_generation)
            self.assertEqual(self.owner.state, "ACTIVATING")
            with closing(sqlite3.connect(self.identity.canonical_sidecar_path)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM tm_meta WHERE key='activation_status'").fetchone(), ("ACTIVE",))

    def test_real_public_migration_reaches_receipt_with_original_borrow(self):
        entered = []
        real_receipt = recovery._publish_activation_receipt

        def receipt(port, *args, **kwargs):
            self.assertIs(port.posix_activation_borrow.root, self.owner._family._root)
            self.assertIsNotNone(port.receipt_handoff.guard)
            result = real_receipt(port, *args, **kwargs)
            entered.append(result)
            return result

        with patch.object(recovery, "_publish_activation_receipt", receipt), patch.object(recovery, "_publish_activation_manifest", side_effect=_AfterReceipt):
            with self.assertRaises(_AfterReceipt):
                self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertEqual(len(entered), 1)
        self.assertEqual(self.owner.state, "ACTIVATING")
        self.assertIsNone(self.owner.current_generation)

    def test_receipt_retains_real_sqlite_write_lock_and_captures_only_after_sql_close(self):
        import subprocess
        with self.prepared() as (reservation, attempt, prepared, handle):
            connections = []
            real_connect = sqlite3.connect
            real_closure = store._CoordinatorStorePort.stage_closure_digest
            real_capture = recovery._PosixReceiptHandoff.capture
            real_sql_close = store._close_activation_receipt_connection
            closed_cursors = []
            captures = []
            contenders = []

            def connect(*args, **kwargs):
                result = real_connect(*args, **kwargs)
                self.assertIs(type(result), sqlite3.Connection)
                connections.append(result)
                return result

            def capture(handoff, **kwargs):
                for connection in connections:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")
                result = real_capture(handoff, **kwargs)
                self.assertIsNotNone(handoff.guard)
                captures.append(kwargs.get("synchronize", False))
                return result

            def close_sql(connection):
                import gc
                cursors = [value for value in gc.get_objects()
                           if type(value) is sqlite3.Cursor and value.connection is connection]
                self.assertGreaterEqual(len(cursors), 3)
                for cursor in cursors:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        cursor.fetchone()
                closed_cursors.append(len(cursors))
                real_sql_close(connection)

            def closure(port, connection):
                # Child has no family W1: SQLite itself must still block it.
                child = subprocess.run([sys.executable, "-c", "import sqlite3,sys\nc=sqlite3.connect(sys.argv[1], timeout=0)\ntry:\n c.execute('BEGIN IMMEDIATE')\nexcept sqlite3.OperationalError as e:\n print(str(e))\nelse:\n c.rollback(); sys.exit(4)\nfinally:\n c.close()", str(self.identity.canonical_sidecar_path)], capture_output=True, text=True, check=False)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertIn("locked", child.stdout)
                contenders.append(True)
                return real_closure(port, connection)

            with patch.object(sqlite3, "connect", connect), patch.object(store._CoordinatorStorePort, "stage_closure_digest", closure), patch.object(recovery._PosixReceiptHandoff, "capture", capture), patch.object(store, "_close_activation_receipt_connection", close_sql), patch.object(recovery, "_publish_activation_manifest", side_effect=_AfterReceipt):
                with self.assertRaises(_AfterReceipt):
                    self.publish(reservation, attempt, prepared, handle)
            self.assertEqual(len(closed_cursors), 1)
            self.assertEqual(contenders, [True])
            self.assertEqual(captures, [False, True])

    def _fault(self, point):
        with self.prepared() as (reservation, attempt, prepared, handle):
            connections = []
            guards = []
            pending_closes = []
            final_captures = []
            journal_candidates = []
            real_connect = sqlite3.connect
            real_guard = type(reservation._backend).guard_existing_for_mutation
            real_pending_terminal = PendingPublication.terminal_reproof
            real_pending_close = PendingPublication.close
            real_guard_close = BoundExistingFileMutationGuard.close
            real_guard_reprove = BoundExistingFileMutationGuard.reprove
            real_flush = CandidateFile.flush_content
            real_create = type(reservation._root).create_candidate
            real_read = journal._CallerHeldPosixActivationBorrow.read_journal
            real_capture = recovery._PosixReceiptHandoff.capture
            real_close_cursor = recovery._close_activation_receipt_cursor
            real_close_sql = store._close_activation_receipt_connection
            real_commit = recovery._commit_activation_receipt
            real_rollback = recovery._rollback_activation_receipt
            real_closure = store._CoordinatorStorePort.stage_closure_digest
            body_error = sqlite3.DatabaseError("receipt-primary")

            def is_database(pending):
                return pending.preliminary_facts().content_sha256 == bytes.fromhex(handle._record.stage_db_digest)

            def flush(candidate):
                if point == "journal-flush" and candidate in journal_candidates:
                    raise OSError("journal-flush")
                return real_flush(candidate)

            def reprove_guard(value):
                if point == "guard-reproof" and value in guards:
                    raise OSError("guard-reproof")
                return real_guard_reprove(value)

            def create(root, name, **kwargs):
                if point == "journal-write" and name == journal._activation_journal_temp_path(handle.journal_path).name:
                    raise OSError("journal-write")
                candidate = real_create(root, name, **kwargs)
                if name == journal._activation_journal_temp_path(handle.journal_path).name:
                    journal_candidates.append(candidate)
                return candidate

            def read(borrow, path, expected):
                result = real_read(borrow, path, expected)
                if point == "journal-reproof" and path == handle.journal_path:
                    parsed = journal._parse_activation_journal_bytes(result[0], expected_journal_path=path)
                    if parsed.phase.value == "DB_REPLACED":
                        raise OSError("journal-reproof")
                return result

            def guard(backend, root, path):
                if point == "guard-acquire":
                    raise OSError("guard-acquire")
                if point == "guard-identity":
                    path = PurePath(attempt.stage.manifest_temp_path.name)
                value = real_guard(backend, root, path)
                guards.append(value)
                return value

            def terminal(pending):
                if ((is_database(pending) and point == "pending-terminal")
                        or (not is_database(pending) and point == "journal-terminal")):
                    raise OSError(point)
                return real_pending_terminal(pending)

            def close_pending(pending):
                database = is_database(pending)
                result = real_pending_close(pending)
                if not database and point == "journal-close":
                    raise OSError("journal-close")
                if database:
                    pending_closes.append(pending)
                    if point == "pending-close":
                        raise OSError("pending-close")
                return result

            def connect(*args, **kwargs):
                if point == "sql-open":
                    raise sqlite3.OperationalError("sql-open")
                result = real_connect(*args, **kwargs)
                connections.append(result)
                return result

            def closure(port, connection):
                if point in ("sql-body", "rollback", "primary-plus-close", "primary-plus-cursor-close", "primary-plus-guard-close"):
                    raise body_error
                return real_closure(port, connection)

            def commit(connection):
                if point == "commit-before":
                    raise sqlite3.OperationalError("commit-before")
                if point == "commit-native":
                    connection.set_authorizer(lambda action, one, two, db, trigger: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and one == "COMMIT" else sqlite3.SQLITE_OK)
                real_commit(connection)
                if point == "commit-after":
                    raise sqlite3.OperationalError("commit-after")

            def rollback(connection):
                if point == "rollback":
                    raise sqlite3.OperationalError("rollback-secondary")
                return real_rollback(connection)

            def close_cursor(cursor):
                if point in ("cursor-close-before", "primary-plus-cursor-close"):
                    raise sqlite3.OperationalError("cursor-close-before")
                real_close_cursor(cursor)
                if point == "cursor-close-after":
                    raise sqlite3.OperationalError("cursor-close-after")

            def close_sql(connection):
                if point in ("sql-close-before", "primary-plus-close"):
                    raise sqlite3.OperationalError("sql-close-before")
                real_close_sql(connection)
                if point == "sql-close-after":
                    raise sqlite3.OperationalError("sql-close-after")

            def capture(handoff, **kwargs):
                if kwargs.get("synchronize"):
                    final_captures.append(True)
                    if point == "final-capture":
                        raise OSError("final-capture")
                return real_capture(handoff, **kwargs)

            def guard_close(value):
                if value in guards:
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute("SELECT 1")
                result = real_guard_close(value)
                if value in guards and point in ("guard-close", "primary-plus-guard-close"):
                    raise OSError("guard-close")
                return result

            with ExitStack() as patches:
                for owner, name, replacement in (
                    (CandidateFile, "flush_content", flush),
                    (BoundExistingFileMutationGuard, "reprove", reprove_guard),
                    (type(reservation._root), "create_candidate", create),
                    (journal._CallerHeldPosixActivationBorrow, "read_journal", read),
                    (type(reservation._backend), "guard_existing_for_mutation", guard),
                    (PendingPublication, "terminal_reproof", terminal),
                    (PendingPublication, "close", close_pending),
                    (BoundExistingFileMutationGuard, "close", guard_close),
                    (sqlite3, "connect", connect),
                    (store._CoordinatorStorePort, "stage_closure_digest", closure),
                    (recovery, "_commit_activation_receipt", commit),
                    (recovery, "_rollback_activation_receipt", rollback),
                    (recovery, "_close_activation_receipt_cursor", close_cursor),
                    (store, "_close_activation_receipt_connection", close_sql),
                    (recovery._PosixReceiptHandoff, "capture", capture),
                ):
                    patches.enter_context(patch.object(owner, name, replacement))
                manifest = patches.enter_context(patch.object(recovery, "_publish_activation_manifest", side_effect=AssertionError("must not publish manifest")))
                with self.assertRaises((journal.ActivationPreparationError, OSError)) as raised:
                    self.publish(reservation, attempt, prepared, handle)
                manifest.assert_not_called()
                if point in ("sql-body", "rollback", "primary-plus-close", "primary-plus-cursor-close", "primary-plus-guard-close"):
                    self.assertIs(raised.exception.__cause__, body_error)
            parsed = journal._parse_activation_journal_bytes(handle.journal_path.read_bytes(), expected_journal_path=handle.journal_path)
            self.assertEqual(parsed.phase.value, "PREPARED" if point in ("journal-write", "journal-flush") else "DB_REPLACED")
            self.assertEqual(self.owner.state, "ACTIVATING")
            self.assertIsNone(self.owner.current_generation)
            if point in ("journal-write", "journal-flush", "journal-reproof", "journal-terminal", "journal-close", "guard-acquire", "guard-identity", "guard-reproof", "pending-terminal", "pending-close"):
                self.assertEqual(connections, [])
            if point not in ("final-capture", "guard-close"):
                self.assertEqual(final_captures, [])
            self.assertEqual(len(pending_closes), 1)
            for guard_value in guards:
                self.assertTrue(guard_value.closed)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            with closing(real_connect(self.identity.canonical_sidecar_path)) as connection:
                status = connection.execute("SELECT value FROM tm_meta WHERE key='activation_status'").fetchone()[0]
            self.assertEqual(status, "ACTIVE" if point in ("commit-after", "cursor-close-before", "cursor-close-after", "sql-close-before", "sql-close-after", "final-capture", "guard-close") else "SEALED")

    def test_fault_matrix_keeps_truthful_phase_and_never_skips_sql_cleanup(self):
        for point in ("journal-write", "journal-flush", "journal-reproof", "journal-terminal", "journal-close", "guard-acquire", "guard-identity", "guard-reproof", "pending-terminal", "pending-close", "sql-open", "sql-body", "commit-before", "commit-native", "commit-after", "rollback", "cursor-close-before", "cursor-close-after", "sql-close-before", "sql-close-after", "primary-plus-close", "primary-plus-cursor-close", "primary-plus-guard-close", "final-capture", "guard-close"):
            with self.subTest(point=point):
                fixture = PosixReceiptHandoffTests()
                fixture.setUp()
                try:
                    fixture._fault(point)
                finally:
                    fixture.doCleanups()

    def test_multibatch_closure_failure_finalizes_statement_before_guard_close(self):
        import json
        import subprocess
        import tm_stage_sealer as sealer

        writer = """import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0)
try:
 c.execute('BEGIN IMMEDIATE')
 c.execute("UPDATE tm_meta SET value=value WHERE key='generation'")
 c.commit()
 print('committed')
except sqlite3.Error as e:
 print(getattr(e,'sqlite_errorname',type(e).__name__))
finally:
 c.close()
"""
        for cleanup_failure in (None, 'connection-and-guard', 'closure-close-before', 'closure-close-after'):
            with self.subTest(cleanup_failure=cleanup_failure):
                fixture = PosixReceiptHandoffTests()
                fixture.setUp()
                try:
                    fixture.source.write_text(''.join(
                        json.dumps({'source': f'line {i}', 'target': f'target {i}'}) + '\n'
                        for i in range(2050)), encoding='utf-8')
                    self.assertEqual(sealer._STAGE_CLOSURE_FETCH_ROWS, 2048)
                    with fixture.prepared() as (reservation, attempt, prepared, handle):
                        observations = []
                        framed_records = []
                        native_connection_closed = []
                        captures = []
                        closure_close_faults = []
                        primary = MemoryError('receipt multibatch framing')
                        real_frame = sealer._stage_closure_row_frame
                        real_connection_close = store._close_activation_receipt_connection
                        real_guard_close = BoundExistingFileMutationGuard.close
                        real_capture = recovery._PosixReceiptHandoff.capture
                        real_closure_close = sealer._close_stage_closure_cursor

                        def frame(row):
                            if len(row) == 15 and type(row[0]) is int:
                                framed_records.append(row[0])
                                raise primary
                            return real_frame(row)

                        def connection_close(connection):
                            real_connection_close(connection)
                            native_connection_closed.append(True)
                            if cleanup_failure == 'connection-and-guard':
                                raise sqlite3.OperationalError('connection cleanup secondary')

                        def closure_close(cursor):
                            target = bool(framed_records) and cleanup_failure in ('closure-close-before', 'closure-close-after')
                            if target:
                                closure_close_faults.append(cleanup_failure)
                            if target and cleanup_failure == 'closure-close-before':
                                raise sqlite3.OperationalError('closure cleanup secondary before')
                            real_closure_close(cursor)
                            if target:
                                raise sqlite3.OperationalError('closure cleanup secondary after')

                        def observe_writer():
                            child = subprocess.run(
                                [sys.executable, '-c', writer, str(fixture.identity.canonical_sidecar_path)],
                                capture_output=True, text=True, check=True,
                            )
                            return child.stdout.strip()

                        def guard_close(guard):
                            # The retained primary traceback is still live here.
                            # A real writer, not Python's connection state, proves
                            # native statements finished before the raw close.
                            observations.append(('before', observe_writer()))
                            real_guard_close(guard)
                            observations.append(('after', observe_writer()))
                            if cleanup_failure == 'connection-and-guard':
                                raise OSError('guard cleanup secondary')

                        def capture(handoff, **kwargs):
                            captures.append(kwargs.get('synchronize', False))
                            return real_capture(handoff, **kwargs)

                        with patch.object(sealer, '_stage_closure_row_frame', frame), patch.object(sealer, '_close_stage_closure_cursor', closure_close), patch.object(store, '_close_activation_receipt_connection', connection_close), patch.object(BoundExistingFileMutationGuard, 'close', guard_close), patch.object(recovery._PosixReceiptHandoff, 'capture', capture), patch.object(recovery, '_publish_activation_manifest') as manifest:
                            with self.assertRaises(journal.ActivationPreparationError) as caught:
                                fixture.publish(reservation, attempt, prepared, handle)
                            self.assertIs(caught.exception.__cause__, primary)
                            manifest.assert_not_called()
                        self.assertEqual(len(framed_records), 1)
                        self.assertEqual(native_connection_closed, [True])
                        self.assertEqual(captures, [False])
                        self.assertEqual(closure_close_faults, [cleanup_failure] if cleanup_failure in ('closure-close-before', 'closure-close-after') else [])
                        self.assertEqual(observations, [('before', 'committed'), ('after', 'committed')])
                        parsed = journal._parse_activation_journal_bytes(handle.journal_path.read_bytes(), expected_journal_path=handle.journal_path)
                        self.assertEqual(parsed.phase.value, 'DB_REPLACED')
                        self.assertIsNone(fixture.owner.current_generation)
                finally:
                    fixture.doCleanups()

    def test_public_receipt_fault_keeps_db_replaced_without_cold_reconciliation(self):
        entered = []
        real_receipt = recovery._publish_activation_receipt
        def receipt(*args, **kwargs):
            entered.append(True)
            return real_receipt(*args, **kwargs)
        with patch.object(recovery, "_publish_activation_receipt", receipt), patch.object(recovery, "_commit_activation_receipt", side_effect=sqlite3.OperationalError("commit")), patch.object(self.owner, "rollback_durable_activation", side_effect=AssertionError("blind rollback")), patch.object(self.service, "_reconcile_initial_activation_failure", side_effect=AssertionError("cold failure reconciliation")):
            outcome = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertEqual(entered, [True])
        self.assertIs(type(outcome), migration.MigrationFailure)
        self.assertTrue(outcome.canonical_authority_ambiguous)
        self.assertIsNone(self.owner.current_generation)
        record = journal._parse_activation_journal_bytes(journal._activation_journal_path(self.identity).read_bytes(), expected_journal_path=journal._activation_journal_path(self.identity))
        self.assertEqual(record.phase.value, "DB_REPLACED")

    def test_original_root_drift_between_journal_and_guard_stops_without_adopting_clone(self):
        import shutil
        moved = self.parent / "original-moved"
        connections = []
        clones = []
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            with self.prepared() as (reservation, attempt, prepared, handle):
                real_guard = type(reservation._backend).guard_existing_for_mutation
                def guard(backend, root, path):
                    self.root.rename(moved)
                    shutil.copytree(moved, self.root)
                    clones.append({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
                    return real_guard(backend, root, path)
                with patch.object(type(reservation._backend), "guard_existing_for_mutation", guard), patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: connections.append(True)), patch.object(recovery, "_publish_activation_manifest", side_effect=AssertionError("clone publication")):
                    with self.assertRaises((journal.ActivationPreparationError, store.SQLiteStoreLifecycleError)):
                        self.publish(reservation, attempt, prepared, handle)
        self.assertEqual(connections, [])
        self.assertEqual(self.owner._family._state, "FAILED")
        self.assertEqual(clones[0], {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertEqual(journal._parse_activation_journal_bytes((moved / handle.journal_path.name).read_bytes(), expected_journal_path=handle.journal_path).phase.value, "DB_REPLACED")
        shutil.rmtree(self.root)
        moved.rename(self.root)
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            with self.owner._family_operation():
                self.fail("failed original owner rebound")

    def test_existing_generation_replacement_passes_real_borrow_to_receipt(self):
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        receipts = []
        real_receipt = recovery._publish_activation_receipt
        def receipt(port, *args, **kwargs):
            self.assertIs(port.posix_activation_borrow.root, self.owner._family._root)
            self.assertIsNotNone(port.receipt_handoff.guard)
            result = real_receipt(port, *args, **kwargs)
            receipts.append(kwargs["next_generation"])
            return result
        with patch.object(recovery, "_publish_activation_receipt", receipt), patch.object(recovery, "_publish_activation_manifest", side_effect=_AfterReceipt):
            with self.assertRaises(_AfterReceipt):
                self.service.import_snapshot(self.source, self.identity.resource_id)
        self.assertEqual(receipts, [1])
        self.assertEqual(self.owner.state, "ACTIVATING")

    def test_journal_name_replacement_during_candidate_flush_is_not_overwritten(self):
        import os
        with self.prepared() as (reservation, attempt, prepared, handle):
            candidates = []
            real_create = type(reservation._root).create_candidate
            real_flush = CandidateFile.flush_content
            original_bytes = handle.journal_path.read_bytes()
            def create(root, name, **kwargs):
                candidate = real_create(root, name, **kwargs)
                if name == journal._activation_journal_temp_path(handle.journal_path).name:
                    candidates.append(candidate)
                return candidate
            def flush(candidate):
                result = real_flush(candidate)
                if candidate in candidates:
                    clone = self.root / "foreign-journal"
                    clone.write_bytes(original_bytes)
                    os.replace(clone, handle.journal_path)
                return result
            with patch.object(type(reservation._root), "create_candidate", create), patch.object(CandidateFile, "flush_content", flush), patch.object(sqlite3, "connect", side_effect=AssertionError("receipt entered after journal drift")):
                with self.assertRaises(journal.ActivationPreparationError):
                    self.publish(reservation, attempt, prepared, handle)
            self.assertEqual(handle.journal_path.read_bytes(), original_bytes)
            self.assertEqual(self.owner.state, "ACTIVATING")
            self.assertTrue(self.identity.canonical_sidecar_path.exists())
