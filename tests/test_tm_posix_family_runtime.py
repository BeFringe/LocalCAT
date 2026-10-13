"""10.2a/10.2b 真实公开 canonical 操作与 POSIX 家族生命周期。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import tm_sqlite_store as store_module
from tests.test_tm_sqlite_store import _draft
from tm_engine import open_canonical_tm_store
from tm_sqlite_store import SQLiteStoreLifecycleError, SQLiteTMStore
from tests.test_tm_activation_journal import _identity, SOURCE_BYTES
from tm_migration import TMMigrationService
from tm_contracts import MigrationReport


@unittest.skipIf(sys.platform == "win32", "POSIX 家族运行时")
class PosixFamilyRuntimeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.owners = []
        self._store: SQLiteTMStore | None = None

    @property
    def store(self) -> SQLiteTMStore:
        assert self._store is not None
        return self._store

    @property
    def identity(self):
        return self.store.coordinator._resource_identity

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        identity = _identity(self.root)
        identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
        coordinator = store_module.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
        service = TMMigrationService(resource_identity=identity, canonical_store_id="store.primary", coordinator=coordinator)
        outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
        self.assertIs(type(outcome), MigrationReport, outcome)
        self._store = SQLiteTMStore.from_coordinator(coordinator)
        self.owners = [coordinator]
        self.addCleanup(self.close_owners)

    def close_owners(self) -> None:
        for owner in reversed(self.owners):
            owner.close_family_session()

    def sibling(self) -> SQLiteTMStore:
        store = open_canonical_tm_store(self.identity.configured_jsonl_path)
        assert store is not None
        self.owners.append(store.coordinator)
        return store

    def test_query_append_and_lazy_health_hold_one_family_operation(self) -> None:
        owner = self.store.coordinator
        family = owner._family
        assert family is not None
        self.store.append(_draft("new", "新增"))
        with self.store.query_lease() as view:
            self.assertEqual(family._active_count, 1)
            self.assertEqual(owner._active_lease_count, 1)
            self.assertEqual(view.exact_records("new")[0].target_raw, "新增")
            view._candidate_connection()
            self.assertTrue(view.health().healthy)
            self.assertIsNone(view._connection)
            self.assertEqual(view.exact_records("new")[0].target_raw, "新增")
            self.assertEqual(family._active_count, 1)
        self.assertEqual(family._active_count, 0)
        self.assertEqual(owner._active_lease_count, 0)
        self.assertFalse(self.store._query_view_tokens)

    def test_sibling_append_waits_for_lazy_view_then_completes(self) -> None:
        sibling = self.sibling()
        started = threading.Event()
        finished = threading.Event()

        def append() -> None:
            started.set()
            sibling.append(_draft("peer", "并发"))
            finished.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.query_lease() as view:
                view._candidate_connection()
                future = pool.submit(append)
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.15), "append 必须等待原 query lease")
            future.result(timeout=3)
        self.assertEqual(self.store.exact_records("peer")[0].target_raw, "并发")
        self.assertEqual(sibling.coordinator._active_lease_count, 0)

    def test_operation_lease_timeout_and_cancel_return_counts(self) -> None:
        sibling = self.sibling()
        cancelled = threading.Event()
        with self.store.query_lease():
            with ThreadPoolExecutor(max_workers=1) as pool:
                def attempt(cancel: threading.Event | None) -> str:
                    try:
                        with sibling.coordinator._operation_lease(
                            timeout_seconds=0.1 if cancel is None else 2.0, cancelled=cancel,
                        ):
                            return "unexpected"
                    except SQLiteStoreLifecycleError as error:
                        return error.code
                self.assertEqual(pool.submit(attempt, None).result(timeout=2), "STORE.FAMILY_BUSY")
                pending = pool.submit(attempt, cancelled)
                family = sibling.coordinator._family
                assert family is not None
                with family._condition:
                    self.assertTrue(family._condition.wait_for(lambda: family._active_count == 2, 0.2))
                cancelled.set()
                self.assertEqual(pending.result(timeout=2), "STORE.OPERATION_CANCELLED")
        self.assertEqual(sibling.coordinator._active_lease_count, 0)
        assert sibling.coordinator._family is not None
        self.assertEqual(sibling.coordinator._family._active_count, 0)
        self.assertEqual(sibling.coordinator._family._admitted_threads, {})
        self.assertTrue(sibling.health().healthy)

    def test_real_replacement_waits_for_lazy_view_and_completes(self) -> None:
        from tm_contracts import MigrationReport
        from tm_migration import TMMigrationService
        sibling = self.sibling()
        service = TMMigrationService(
            resource_identity=self.identity,
            canonical_store_id=sibling.coordinator.canonical_store_id,
            coordinator=sibling.coordinator,
        )
        family = sibling.coordinator._family
        assert family is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.query_lease() as view:
                self.assertTrue(view.exact_records("same"))
                future = pool.submit(service.import_snapshot, self.identity.configured_jsonl_path, self.identity.resource_id)
                with family._condition:
                    self.assertTrue(family._condition.wait_for(lambda: family._state == "DRAINING", 1))
                self.assertFalse(future.done())
            outcome = future.result(timeout=5)
        self.assertIsInstance(outcome, MigrationReport, repr(outcome))
        self.assertEqual(sibling.coordinator.current_generation, 1)
        self.assertTrue(sibling.exact_records("same"))
        self.assertEqual(family._active_count, 0)
        self.assertEqual(family._state, "READY")

    def process(self, code: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.identity.configured_jsonl_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        def close() -> None:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        self.addCleanup(close)
        return process

    def line(self, process: subprocess.Popen[str], timeout: float = 5) -> str:
        import select
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        self.assertTrue(ready, "子进程未在期限内响应")
        line = process.stdout.readline().strip()
        self.assertTrue(line, "子进程提前退出")
        return line

    def send(self, process: subprocess.Popen[str], command: str) -> None:
        assert process.stdin is not None
        process.stdin.write(command + "\n")
        process.stdin.flush()

    def test_two_real_processes_serialize_then_append_and_query(self) -> None:
        code = '''
from pathlib import Path
import sys
from tm_engine import open_canonical_tm_store
from tests.test_tm_sqlite_store import _draft
store = open_canonical_tm_store(Path(sys.argv[1]))
print("READY", flush=True)
source = input()
print("START", flush=True)
store.append(_draft(source, "process-target"))
print(store.exact_records(source)[0].target_raw, flush=True)
store.coordinator.close_family_session()
'''
        first, second = self.process(code), self.process(code)
        self.assertEqual(self.line(first), "READY")
        self.assertEqual(self.line(second), "READY")
        import select
        with self.store.query_lease() as view:
            view._candidate_connection()
            self.send(first, "process-one")
            self.send(second, "process-two")
            self.assertEqual(self.line(first), "START")
            self.assertEqual(self.line(second), "START")
            assert first.stdout is not None and second.stdout is not None
            ready, _, _ = select.select([first.stdout, second.stdout], [], [], 0.15)
            self.assertFalse(ready, "两个真实进程均须等待原 W1")
        for process in (first, second):
            self.assertEqual(self.line(process), "process-target")
            self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(self.store.exact_records("process-one"))
        self.assertTrue(self.store.exact_records("process-two"))

    def test_waiting_old_generation_cannot_append_after_real_process_replace(self) -> None:
        from tm_sqlite_store import SQLiteStoreSchemaError
        process = self.process('''
from pathlib import Path
import sys
from tm_engine import open_canonical_tm_store
from tm_migration import TMMigrationService
from tm_contracts import MigrationReport
store = open_canonical_tm_store(Path(sys.argv[1]))
owner = store.coordinator
service = TMMigrationService(resource_identity=owner._resource_identity,
    canonical_store_id=owner.canonical_store_id, coordinator=owner)
original = service._posix_explicit_disambiguation_reserved
def held(source, resource, **inputs):
    print("HELD", flush=True)
    input()
    return original(source, resource, **inputs)
service._posix_explicit_disambiguation_reserved = held
result = service.import_snapshot(Path(sys.argv[1]), owner.resource_id)
print("REPLACED" if type(result) is MigrationReport else repr(result), flush=True)
owner.close_family_session()
''')
        self.assertEqual(self.line(process), "HELD")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.store.append, _draft("old-generation", "不得写入"))
            family = self.store.coordinator._family
            assert family is not None
            with family._condition:
                self.assertTrue(family._condition.wait_for(lambda: family._active_count == 1, 1))
            self.assertFalse(future.done())
            self.send(process, "CONTINUE")
            self.assertEqual(self.line(process), "REPLACED")
            with self.assertRaises(SQLiteStoreSchemaError):
                future.result(timeout=3)
        self.assertEqual(process.wait(timeout=3), 0)
        current = self.sibling()
        self.assertEqual(current.coordinator.current_generation, 1)
        self.assertEqual(current.exact_records("old-generation"), ())
        self.assertTrue(current.health().healthy)
        self.assertEqual(family._active_count, 0)
        self.assertEqual(self.store.coordinator._active_lease_count, 0)

    def test_nested_health_does_not_release_sqlite_read_lock_via_raw_close(self) -> None:
        import tm_sqlite_store as module
        code = '''
import sqlite3, sys
from pathlib import Path
connection = sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=rw", uri=True, timeout=0.05, isolation_level=None)
try:
    connection.execute("BEGIN EXCLUSIVE")
    print("UNLOCKED")
except sqlite3.OperationalError:
    print("LOCKED")
finally:
    connection.close()
'''
        original = module._read_source_binding_facts_in_transaction
        observed = []
        def probe(connection, lease):
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.identity.canonical_sidecar_path)],
                capture_output=True, text=True, timeout=3, check=True,
            )
            observed.append(result.stdout.strip())
            return original(connection, lease)
        with self.store.query_lease() as view:
            lazy = view._candidate_connection()
            lazy.execute("BEGIN")
            # The owner materializes each statement before returning a DTO.
            # Keep the read transaction live, with no escaped private cursor.
            self.assertTrue(lazy.execute("SELECT * FROM tm_record").fetchall())
            with patch.object(module, "_read_source_binding_facts_in_transaction", side_effect=probe):
                self.assertTrue(view.health().healthy)
            self.assertEqual(observed, ["LOCKED"])
            self.assertIsNone(view._connection)
            self.assertTrue(view.exact_records("same"))

    def test_repeated_operation_fd_and_count_balance(self) -> None:
        import os
        owner = self.store.coordinator
        # The persistent W1 file is initialized before taking the FD baseline.
        self.store.health()
        before = len(os.listdir("/dev/fd"))
        for index in range(8):
            self.store.append(_draft(f"balanced-{index}", "value"))
            with self.store.query_lease() as view:
                view._candidate_connection()
                self.assertTrue(view.health().healthy)
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        self.assertEqual(owner._active_lease_count, 0)
        assert owner._family is not None
        self.assertEqual(owner._family._active_count, 0)
        self.assertEqual(owner._family._operation_tokens, {})
        self.assertEqual(owner._family._admitted_threads, {})

    def test_observed_database_swap_latches_failure_and_releases_lazy_lease(self) -> None:
        import shutil
        owner = self.store.coordinator
        clone = self.identity.canonical_sidecar_path.with_name("clone.sqlite3")
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.RESOURCE_IDENTITY_STALE"):
            with self.store.query_lease() as view:
                view._candidate_connection()
                shutil.copyfile(self.identity.canonical_sidecar_path, clone)
                clone.replace(self.identity.canonical_sidecar_path)
                view.exact_records("same")
        self.assertEqual(owner.state, "FAILED")
        self.assertEqual(owner._active_lease_count, 0)
        assert owner._family is not None
        self.assertEqual(owner._family._active_count, 0)
        self.assertFalse(self.store._query_view_tokens)
        with self.assertRaises(SQLiteStoreLifecycleError):
            self.store.append(_draft("forbidden", "value"))
        with self.assertRaisesRegex(ValueError, "TM.CANONICAL_UNHEALTHY"):
            open_canonical_tm_store(self.identity.configured_jsonl_path)

    def test_new_coordinator_factory_waits_before_observing_live_database(self) -> None:
        started = threading.Event()
        def opening() -> SQLiteTMStore:
            started.set()
            store = open_canonical_tm_store(self.identity.configured_jsonl_path)
            assert store is not None
            self.owners.append(store.coordinator)
            return store
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.query_lease() as view:
                connection = view._candidate_connection()
                connection.execute("BEGIN")
                self.assertTrue(connection.execute("SELECT * FROM tm_record").fetchall())
                future = pool.submit(opening)
                self.assertTrue(started.wait(1))
                family = self.store.coordinator._family
                assert family is not None
                with family._condition:
                    self.assertTrue(family._condition.wait_for(
                        lambda: family._state == "DRAINING" or family._active_count == 2, 1,
                    ))
                result = subprocess.run(
                    [sys.executable, "-c", """
import sqlite3, sys
from pathlib import Path
connection = sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=rw',
    uri=True, timeout=0.05, isolation_level=None)
try:
    connection.execute('BEGIN EXCLUSIVE')
    print('UNLOCKED')
except sqlite3.OperationalError:
    print('LOCKED')
finally:
    connection.close()
""", str(self.identity.canonical_sidecar_path)],
                    capture_output=True, text=True, timeout=3, check=True,
                )
                self.assertEqual(result.stdout.strip(), "LOCKED")
                self.assertFalse(future.done(), "factory 必须先排空原 lease，不能提前 raw capture")
            opened = future.result(timeout=3)
        self.assertTrue(opened.exact_records("same"))
