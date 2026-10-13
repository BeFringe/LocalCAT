"""Core 10.7：真实 runtime 入口的借用、终端证明与失败隔离。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import errno
import json
from pathlib import Path
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import tm_engine
import tm_sqlite_store as module
from tests.test_tm_activation_journal import SOURCE_BYTES, _identity
from tests.test_tm_sqlite_store import _draft
from tm_contracts import MigrationReport, SourceBindingState
from tm_migration import TMMigrationService
from platform_fs_contracts import BoundRegularFile, PlatformFileError


def _runtime_reader_failure_probe(entry, cut, phase, interrupt, outer_exception):
    """公开入口继续真实平台代码；只在指定物理观察/关闭边界干预。"""
    import platform_fs_posix

    fixture = PosixRuntimeEntrypointTests("runTest")
    fixture.setUp()
    try:
        family = fixture.owner._family
        original_root, original_view = family._root, fixture.owner._view
        original_index = fixture.owner._current_index_validation
        real_open = family._backend.open_regular
        real_close = platform_fs_posix.os.close
        real_snapshot, real_facts = BoundRegularFile.snapshot, BoundRegularFile.content_facts
        real_capture = module._posix_runtime_database_capture
        real_content, real_runtime_close = module._capture_posix_bound_content, module._close_runtime_file
        real_connect, real_reprove = module.sqlite3.connect, module._SQLiteOperationLease.reprove
        selected = None
        capture_number = 0
        moved = False
        primaries, body_errors, signals, sql_after_signal, terminal_errors = [], [], [], [], []
        close_errors = []
        primary_fields = []
        reproof_after_close = []
        caller_error = LookupError("caller's already handled exception")

        def move():
            nonlocal moved
            fixture.assertFalse(moved)
            moved = True
            if cut == "name":
                path = fixture.identity.configured_jsonl_path
                path.rename(path.with_suffix(".retained"))
                path.write_bytes(b"changed source only\n")
            else:
                fixture.move_and_clone()

        def opening(root, relative):
            nonlocal selected
            reader = real_open(root, relative)
            matching = (
                entry == "health" and capture_number == phase
                and relative.name == fixture.identity.canonical_sidecar_path.name
            ) or (entry == "pair" and relative.name == fixture.identity.configured_jsonl_path.name)
            if matching:
                fixture.assertIsNone(selected)
                selected = reader
                if cut in {"before", "root", "name"}:
                    move()
            return reader

        def snapshot(reader):
            try:
                return real_snapshot(reader)
            except BaseException as error:
                if reader is selected:
                    body_errors.append(error)
                raise

        def facts(reader):
            try:
                return real_facts(reader)
            except BaseException as error:
                if reader is selected:
                    body_errors.append(error)
                raise

        def content(backend, reader):
            if reader is selected and cut == "inside":
                move()
            try:
                result = real_content(backend, reader)
            except BaseException as error:
                if reader is selected:
                    body_errors.append(error)
                raise
            if reader is selected and cut == "after":
                move()
            return result

        def capture(lease):
            nonlocal capture_number
            capture_number += 1
            return real_capture(lease)

        def closing(fd):
            real_close(fd)
            if (interrupt and selected is not None
                    and fd == selected._descriptor and not signals):
                signals.append(fd)
                try:
                    signal.raise_signal(signal.SIGINT)
                except KeyboardInterrupt as error:
                    close_errors.append(error)
                    raise

        def runtime_close(lease, opened, primary):
            if opened is selected:
                primaries.append(primary)
                if isinstance(primary, PlatformFileError):
                    primary_fields.append((primary.code, primary.retryable))
            return real_runtime_close(lease, opened, primary)

        def connect(*args, **kwargs):
            if signals:
                sql_after_signal.append(args[0])
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(lambda statement: sql_after_signal.append(statement) if signals else None)
            return connection

        def reprove(lease):
            if selected is not None and selected.closed:
                reproof_after_close.append(lease)
            try:
                return real_reprove(lease)
            except module.SQLiteStoreLifecycleError as error:
                if selected is not None and selected.closed:
                    terminal_errors.append((error, error.__cause__, (
                        error.code, error.resource_id, error.generation, error.retryable,
                    )))
                raise

        def invoke():
            if entry == "health":
                return fixture.store.health()
            return module.SourceBindingMonitor(fixture.owner).observe()

        fixture.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        caught = result = None
        with patch.object(family._backend, "open_regular", side_effect=opening), \
                patch.object(BoundRegularFile, "snapshot", snapshot), \
                patch.object(BoundRegularFile, "content_facts", facts), \
                patch.object(module, "_capture_posix_bound_content", content), \
                patch.object(module, "_posix_runtime_database_capture", capture), \
                patch.object(module, "_close_runtime_file", runtime_close), \
                patch.object(platform_fs_posix.os, "close", closing), \
                patch.object(module.sqlite3, "connect", side_effect=connect), \
                patch.object(module._SQLiteOperationLease, "reprove", reprove):
            try:
                if outer_exception:
                    try:
                        raise caller_error
                    except LookupError:
                        result = invoke()
                else:
                    result = invoke()
            except BaseException as error:
                caught = error

        fixture.assertIsNotNone(selected)
        fixture.assertEqual(len(primaries), 1)
        fixture.assertTrue(selected.closed)
        fixture.assertIs(selected._read_prefix, original_root._read_prefix)
        shared_prefix = selected._read_prefix.descriptors
        owned_directories = selected._directory_fds[len(shared_prefix):]
        for fd in (selected._descriptor, *owned_directories):
            with fixture.assertRaises(OSError) as closed:
                os.fstat(fd)
            fixture.assertEqual(closed.exception.errno, errno.EBADF)
        fixture.assertFalse(original_root.closed)
        for fd in shared_prefix:
            os.fstat(fd)  # The live family owner still owns this shared prefix.
        fixture.assertEqual(sql_after_signal, [])
        fixture.assertIs(family._root, original_root)
        fixture.assertIs(fixture.owner._view, original_view)
        fixture.assertEqual(family._active_count, 0)
        fixture.assertEqual(fixture.owner._active_lease_count, 0)
        chain, current = [], caught
        while current is not None and not any(current is item for item in chain):
            chain.append(current)
            current = current.__cause__
        fixture.assertIsNone(current, "明确 cause 链必须无环")
        fixture.assertFalse(any(item is caller_error for item in chain))
        primary = primaries[0]
        notes = " ".join(note for error in chain for note in getattr(error, "__notes__", ()))
        if interrupt:
            fixture.assertEqual(len(signals), 1)
            fixture.assertIsNone(result)
            fixture.assertTrue(family._recovery_required)
            fixture.assertEqual(len(family._pending_activation_closes), 1)
            callback = family._pending_activation_closes[0]
            fixture.assertTrue(any(cell.cell_contents is selected for cell in callback.__closure__ or ()))
            if primary is None:
                fixture.assertIs(type(caught), KeyboardInterrupt)
                fixture.assertIs(caught, close_errors[0])
            else:
                fixture.assertIn("Runtime reader close failed: KeyboardInterrupt", notes)
        else:
            fixture.assertEqual(signals, [])
            fixture.assertEqual(family._pending_activation_closes, [])

        root_failed = cut not in {"none", "name"}
        if root_failed:
            fixture.assertEqual(fixture.owner.state, "FAILED")
            fixture.assertEqual(family._state, "FAILED")
            fixture.assertTrue(any(primary is item for item in chain), "确切 primary 未保留")
            fixture.assertIsNone(result)
            if entry == "health" and phase > 1:
                fixture.assertIsNone(fixture.owner._current_index_validation)
            else:
                fixture.assertIs(fixture.owner._current_index_validation, original_index)
            if entry == "health" and cut in {"before", "after"}:
                fixture.assertIsInstance(primary, PlatformFileError)
                fixture.assertEqual(primary.code, "PLATFORM.FS.IDENTITY_STALE")
                fixture.assertEqual((primary.code, primary.retryable), primary_fields[0])
                fixture.assertEqual(getattr(primary, "__notes__", ()), ())
                fixture.assertTrue(any(primary is error for error in body_errors))
                if interrupt:
                    carrier = next(error for error in chain if error.__cause__ is primary)
                    fixture.assertIs(type(carrier), module.SQLiteStoreSchemaError)
                    fixture.assertEqual(str(carrier), primary.code)
                elif phase == 1:
                    fixture.assertIs(caught, primary)
            elif entry == "health":
                fixture.assertIs(type(primary), module.ContentAttestationError)
                fixture.assertEqual(str(primary), "ATTESTATION.FILE_CHANGED")
                fixture.assertIsInstance(primary.__cause__, PlatformFileError)
            else:
                fixture.assertIs(type(primary), module.SQLiteStoreLifecycleError)
                fixture.assertIsInstance(primary.__cause__, PlatformFileError)
            if entry == "pair" or phase > 1:
                fixture.assertEqual(len(terminal_errors), 1)
                terminal, prior_cause, fields = terminal_errors[0]
                fixture.assertIs(caught, terminal)
                fixture.assertEqual((caught.code, caught.resource_id, caught.generation, caught.retryable), fields)
                fixture.assertEqual(fields, ("STORE.RESOURCE_IDENTITY_STALE", "tm.primary", 0, False))
                if entry == "health":
                    fixture.assertIsNotNone(prior_cause)
                else:
                    # Pair already failed the family during its own reproof;
                    # the terminal refusal has no separate platform cause.
                    fixture.assertIsNone(prior_cause)
                if prior_cause is not None:
                    fixture.assertIn(f"Runtime terminal reproof cause: {type(prior_cause).__name__}: {prior_cause}", notes)
                fixture.assertTrue(reproof_after_close)
        elif not interrupt:
            fixture.assertIsNone(caught)
            fixture.assertTrue(reproof_after_close, "成功路径仍需终端复证")
            if cut == "name":
                fixture.assertEqual(result.state, SourceBindingState.SOURCE_DIVERGED)
                source = fixture.identity.configured_jsonl_path.read_bytes()
                fixture.assertTrue(fixture.store.exact_records("same"))
                fixture.store.append(_draft("diverged update", "保留"))
                fixture.assertEqual(fixture.identity.configured_jsonl_path.read_bytes(), source)
                fixture.assertEqual(module.SourceBindingMonitor(fixture.owner).observe().state,
                                    SourceBindingState.SOURCE_DIVERGED)
            else:
                fixture.assertTrue(result.healthy)
        fixture.owner.close_family_session()
        fixture.assertTrue(original_root.closed)
        for fd in shared_prefix:
            with fixture.assertRaises(OSError) as closed:
                os.fstat(fd)
            fixture.assertEqual(closed.exception.errno, errno.EBADF)
        print(json.dumps(dict(entry=entry, cut=cut, phase=phase, interrupt=interrupt,
                              outer_exception=outer_exception, captures=capture_number,
                              signals=len(signals), sql_after_signal=0, all_fds_closed=True,
                              reader_owned_fds_closed=True, shared_prefix_retained_until_owner_close=True,
                              primary_preserved=primary is None or any(primary is e for e in chain),
                              terminal_reproofs=len(reproof_after_close),
                              public_error=None if caught is None else type(caught).__name__), ensure_ascii=False))
    finally:
        fixture.doCleanups()


def _maintenance_close_sigint_probe(
    outer_exception: bool, body_failure: str | None,
) -> None:
    """在独立进程的真实最后目录 FD 关闭后递送 SIGINT。"""
    import platform_fs_posix

    fixture = PosixRuntimeEntrypointTests("runTest")
    fixture.setUp()
    try:
        fixture.owner.close_family_session()
        real_close = platform_fs_posix.os.close
        real_facts = tm_engine._activation_facts
        roots = []
        delivered = []
        terminal_descriptor = None
        body_error = (
            OSError("maintenance body read failed") if body_failure == "mapped"
            else LookupError("maintenance body failed")
        )

        def arm_after_read(*args, **kwargs):
            nonlocal terminal_descriptor
            result = real_facts(*args, **kwargs)
            root = kwargs["caller_borrow"].root
            roots.append(root)
            # Adapter closes the root chain in reverse order. Interrupt only
            # after its last real close, retaining no live FD in this probe.
            terminal_descriptor = root._directory_fds[0]
            if body_failure is not None:
                raise body_error
            return result

        def close_then_interrupt(descriptor):
            real_close(descriptor)
            if descriptor == terminal_descriptor:
                delivered.append(descriptor)
                signal.raise_signal(signal.SIGINT)

        fixture.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        caught = None

        def read_facts():
            nonlocal caught
            try:
                tm_engine.canonical_authority_facts(fixture.identity.configured_jsonl_path)
            except BaseException as error:
                caught = error

        with patch.object(platform_fs_posix.os, "close", close_then_interrupt), \
                patch.object(tm_engine, "_activation_facts", side_effect=arm_after_read):
            if outer_exception:
                try:
                    raise RuntimeError("caller's already handled exception")
                except RuntimeError:
                    read_facts()
            else:
                read_facts()
        fixture.assertEqual(len(delivered), 1)
        fixture.assertEqual(len(roots), 1)
        fixture.assertTrue(roots[0].closed)
        for descriptor in roots[0]._directory_fds:
            with fixture.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            fixture.assertEqual(closed.exception.errno, errno.EBADF)
        family = module._FAMILY_SESSIONS[fixture.identity.configured_jsonl_path]
        fixture.assertEqual(family._state, "FAILED")
        if body_failure is None:
            fixture.assertIsInstance(caught, KeyboardInterrupt)
        else:
            if body_failure == "mapped":
                fixture.assertIsInstance(caught, ValueError)
                fixture.assertEqual(str(caught), "TM.CANONICAL_IDENTITY_MISSING")
                fixture.assertIs(caught.__cause__, body_error)
            else:
                fixture.assertIs(caught, body_error)
            fixture.assertIn("KeyboardInterrupt", " ".join(getattr(caught, "__notes__", ())))
        print("SIGINT=1; directory FDs closed; family=FAILED; exception preserved")
    finally:
        fixture.doCleanups()


def _maintenance_locator_sigint_probe(
    outer_exception: bool, body_failure: str | None,
    interrupt: bool = True, entrypoint: str = "maintenance",
) -> None:
    """在真实 cold locator reader 收尾测试中断与主次错误。"""
    import platform_fs_posix

    fixture = PosixRuntimeEntrypointTests("runTest")
    fixture.setUp()
    try:
        fixture.owner.close_family_session()
        real_locator = tm_engine._posix_cold_resource_locator
        real_close = platform_fs_posix.os.close
        real_chunks = BoundRegularFile.iter_exact_chunks
        real_connect = module.sqlite3.connect
        readers, roots, delivered, connections_after_signal, body_errors = [], [], [], [], []
        shared_prefix_checks = []
        target = None

        def close_then_interrupt(descriptor):
            real_close(descriptor)
            if interrupt and descriptor == target and not delivered:
                delivered.append(descriptor)
                signal.raise_signal(signal.SIGINT)

        def chunks(reader, expected):
            if body_failure == "platform":
                # Change the real file after snapshot: the platform reader
                # itself must report the stale generation, without a stub.
                path = fixture.root / reader._entry_name
                path.write_bytes(path.read_bytes() + b" ")
            try:
                yield from real_chunks(reader, expected)
                if body_failure == "os":
                    raise OSError("locator read failed")
            except BaseException as error:
                body_errors.append(error)
                raise

        def locator(path, backend, root):
            roots.append(root)
            real_open = backend.open_regular

            def opening(parent, relative):
                nonlocal target
                reader = real_open(parent, relative)
                readers.append(reader)
                target = reader._descriptor
                return reader

            with patch.object(backend, "open_regular", side_effect=opening), \
                    patch.object(platform_fs_posix.os, "close", close_then_interrupt), \
                    patch.object(BoundRegularFile, "iter_exact_chunks", chunks):
                try:
                    return real_locator(path, backend, root)
                finally:
                    reader = readers[0]
                    fixture.assertTrue(reader.closed)
                    with fixture.assertRaises(OSError) as closed:
                        os.fstat(reader._descriptor)
                    fixture.assertEqual(closed.exception.errno, errno.EBADF)
                    fixture.assertIs(reader._read_prefix, root._read_prefix)
                    fixture.assertFalse(root.closed)
                    for descriptor in reader._read_prefix.descriptors:
                        os.fstat(descriptor)
                    shared_prefix_checks.append(True)

        def connect(*args, **kwargs):
            if delivered:
                connections_after_signal.append(args[0])
            return real_connect(*args, **kwargs)

        fixture.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        caught = result = None
        endpoint = (tm_engine.canonical_authority_facts if entrypoint == "maintenance"
                    else tm_engine.open_canonical_tm_store)

        def read():
            nonlocal caught, result
            try:
                result = endpoint(fixture.identity.configured_jsonl_path)
            except BaseException as error:
                caught = error

        with patch.object(tm_engine, "_posix_cold_resource_locator", side_effect=locator), \
                patch.object(module.sqlite3, "connect", side_effect=connect):
            if outer_exception:
                try:
                    raise LookupError("caller's already handled exception")
                except LookupError:
                    read()
            else:
                read()
        fixture.assertEqual(len(delivered), int(interrupt))
        fixture.assertEqual(len(readers), 1)
        fixture.assertEqual(shared_prefix_checks, [True])
        fixture.assertTrue(readers[0].closed)
        fixture.assertTrue(roots[0].closed)
        for descriptor in (readers[0]._descriptor, *readers[0]._directory_fds):
            with fixture.assertRaises(OSError) as closed:
                os.fstat(descriptor)
            fixture.assertEqual(closed.exception.errno, errno.EBADF)
        print(f"SIGINT={len(delivered)}; reader FDs closed; SQL after signal={len(connections_after_signal)}")
        fixture.assertEqual(connections_after_signal, [], "中断后不可继续实际 SQL")
        fixture.assertIsNone(result)
        fixture.assertNotIn(fixture.identity.configured_jsonl_path, module._FAMILY_SESSIONS)
        if body_failure is None:
            fixture.assertIsInstance(caught, KeyboardInterrupt)
        else:
            fixture.assertIsInstance(caught, ValueError)
            fixture.assertEqual(str(caught), "TM.CANONICAL_IDENTITY_MISSING"
                                if entrypoint == "maintenance" else "TM.CANONICAL_UNHEALTHY")
            fixture.assertEqual(len(body_errors), 1)
            chain = []
            while caught is not None and all(caught is not item for item in chain):
                chain.append(caught)
                caught = caught.__cause__
            fixture.assertIsNone(caught, "主错与关闭事实 cause 链必须无环")
            fixture.assertTrue(any(item is body_errors[0] for item in chain))
            if body_failure == "platform":
                fixture.assertIsInstance(body_errors[0], PlatformFileError)
                fixture.assertEqual(body_errors[0].code, "PLATFORM.FS.IDENTITY_STALE")
            if interrupt:
                notes = " ".join(note for item in chain for note in getattr(item, "__notes__", ()))
                fixture.assertIn("KeyboardInterrupt", notes)
            else:
                fixture.assertIs(chain[0].__cause__, body_errors[0])
    finally:
        fixture.doCleanups()


@unittest.skipIf(sys.platform == "win32", "POSIX runtime 入口")
class PosixRuntimeEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "resource"
        self.root.mkdir()
        self.identity = _identity(self.root)
        self.identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
        self.owner = module.ResourceStoreCoordinator(
            resource_identity=self.identity, canonical_store_id="store.primary",
        )
        service = TMMigrationService(
            resource_identity=self.identity, canonical_store_id="store.primary",
            coordinator=self.owner,
        )
        self.assertIs(type(service.activate_initial(
            self.identity.configured_jsonl_path, self.identity.resource_id,
        )), MigrationReport)
        self.store = module.SQLiteTMStore.from_coordinator(self.owner)
        self.addCleanup(self.owner.close_family_session)

    def move_and_clone(self) -> None:
        self.root.rename(self.base / "original")
        shutil.copytree(self.base / "original", self.root)

    def assert_stopped(self) -> None:
        self.assertEqual(self.owner.state, "FAILED")
        self.assertEqual(self.owner._active_lease_count, 0)
        self.assertEqual(self.owner._family._active_count, 0)
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            self.store.append(_draft("forbidden", "拒绝"))
        with self.assertRaises(ValueError):
            tm_engine.open_canonical_tm_store(self.identity.configured_jsonl_path)

    def _assert_runtime_failure_in_child(self, entry, cut, phase, interrupt, outer_exception=True):
        handler = signal.getsignal(signal.SIGINT)
        result = subprocess.run([
            sys.executable, "-B", "-c",
            "from tests.test_tm_posix_runtime_entrypoints import _runtime_reader_failure_probe; "
            f"_runtime_reader_failure_probe({entry!r}, {cut!r}, {phase!r}, {interrupt!r}, {outer_exception!r})",
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertIs(signal.getsignal(signal.SIGINT), handler)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        facts = json.loads(result.stdout)
        self.assertTrue(facts["all_fds_closed"])
        self.assertTrue(facts["reader_owned_fds_closed"])
        self.assertTrue(facts["shared_prefix_retained_until_owner_close"])
        self.assertEqual(facts["sql_after_signal"], 0)
        self.assertTrue(facts["primary_preserved"])

    def test_runtime_health_three_captures_keep_primary_and_sigint_close_facts(self):
        for phase in (1, 2, 3):
            for cut in ("before", "inside", "after"):
                with self.subTest(phase=phase, cut=cut):
                    self._assert_runtime_failure_in_child("health", cut, phase, True)

    def test_runtime_health_three_captures_preserve_single_failure_contract(self):
        for phase in (1, 2, 3):
            for cut in ("before", "inside", "after"):
                with self.subTest(phase=phase, cut=cut):
                    self._assert_runtime_failure_in_child("health", cut, phase, False)

    def test_runtime_pair_root_and_file_rejection_keep_distinct_failure_contracts(self):
        for cut in ("root", "name"):
            for interrupt in (False, True):
                with self.subTest(cut=cut, interrupt=interrupt):
                    self._assert_runtime_failure_in_child("pair", cut, 1, interrupt)

    def test_runtime_close_sigint_without_primary_ignores_callers_old_except(self):
        for outer in (False, True):
            for entry, phase in (("health", 1), ("health", 2), ("health", 3), ("pair", 1)):
                with self.subTest(entry=entry, phase=phase, outer=outer):
                    self._assert_runtime_failure_in_child(entry, "none", phase, True, outer)

    def test_runtime_successful_health_keeps_terminal_reproof_inside_callers_except(self):
        for outer in (False, True):
            with self.subTest(outer=outer):
                self._assert_runtime_failure_in_child("health", "none", 3, False, outer)

    def test_runtime_terminal_reproof_preserves_old_cause_chain_notes_and_fields(self):
        # Supplement the real platform matrix with a writable cause chain:
        # platform errors deliberately cannot carry diagnostic notes.
        primary = LookupError("current health body")
        deepest = ValueError("old terminal detail")
        deepest.add_note("deep terminal note")
        old_cause = module.SQLiteStoreLifecycleError(
            "STORE.OPERATION_BORROW_INVALID", resource_id="tm.primary", generation=7, retryable=True,
        )
        old_cause.add_note("old terminal note")
        old_cause.__cause__ = deepest
        terminal = module.SQLiteStoreLifecycleError(
            "STORE.RESOURCE_IDENTITY_STALE", resource_id="tm.primary", generation=9, retryable=False,
        )
        terminal.__cause__ = old_cause
        terminal.add_note("existing terminal note")
        real_reprove = module._SQLiteOperationLease.reprove
        armed = False

        def body(*args, **kwargs):
            nonlocal armed
            armed = True
            raise primary

        def reprove(lease):
            real_reprove(lease)
            if armed:
                raise terminal

        with patch.object(module, "_store_health_body", side_effect=body), \
                patch.object(module._SQLiteOperationLease, "reprove", reprove):
            with self.assertRaises(module.SQLiteStoreLifecycleError) as caught:
                self.store.health()
        self.assertIs(caught.exception, terminal)
        self.assertIs(terminal.__cause__, primary)
        self.assertEqual((terminal.code, terminal.resource_id, terminal.generation, terminal.retryable),
                         ("STORE.RESOURCE_IDENTITY_STALE", "tm.primary", 9, False))
        notes = " ".join(terminal.__notes__)
        for fact in ("existing terminal note", "SQLiteStoreLifecycleError: STORE.OPERATION_BORROW_INVALID",
                     "old terminal note", "ValueError: old terminal detail", "deep terminal note"):
            self.assertIn(fact, notes)
        self.assertIsNone(primary.__cause__)
        self.assertIs(old_cause.__cause__, deepest)
        self.assertEqual(self.owner._active_lease_count, 0)
        self.assertEqual(self.owner._family._active_count, 0)

    def test_runtime_terminal_same_primary_never_causes_itself(self):
        error = module.SQLiteStoreLifecycleError(
            "STORE.RESOURCE_IDENTITY_STALE", resource_id="tm.primary", generation=0, retryable=False,
        )
        armed = False
        real_reprove = module._SQLiteOperationLease.reprove

        def body(*args, **kwargs):
            nonlocal armed
            armed = True
            raise error

        def reprove(lease):
            real_reprove(lease)
            if armed:
                raise error

        with patch.object(module, "_store_health_body", side_effect=body), \
                patch.object(module._SQLiteOperationLease, "reprove", reprove):
            with self.assertRaises(module.SQLiteStoreLifecycleError) as caught:
                self.store.health()
        self.assertIs(caught.exception, error)
        self.assertIsNone(error.__cause__)

    def test_runtime_terminal_only_failure_does_not_chain_callers_old_except(self):
        real_body = module._store_health_body
        caller_error = LookupError("old caller exception")
        completed = []

        def body(*args, **kwargs):
            result = real_body(*args, **kwargs)
            completed.append(result)
            self.move_and_clone()
            return result

        with patch.object(module, "_store_health_body", side_effect=body):
            try:
                raise caller_error
            except LookupError:
                with self.assertRaises(module.SQLiteStoreLifecycleError) as caught:
                    self.store.health()
        self.assertTrue(completed[0].healthy)
        self.assertEqual(caught.exception.code, "STORE.RESOURCE_IDENTITY_STALE")
        self.assertIsInstance(caught.exception.__cause__, PlatformFileError)
        self.assertIsNone(caught.exception.__cause__.__cause__)
        self.assertEqual(self.owner._family._active_count, 0)
        self.assertEqual(self.owner._active_lease_count, 0)
        self.assertEqual(self.owner.state, "FAILED")

    def test_health_after_append_reuses_only_terminal_proven_current_bytes(self) -> None:
        self.store.append(_draft("new", "新增"))
        with patch.object(module, "validate_candidate_proof_index",
                          wraps=module.validate_candidate_proof_index) as validator:
            self.assertTrue(self.store.health().healthy)
            first = validator.call_count
            self.assertEqual(first, 1)
            self.assertTrue(self.store.health().healthy)
            self.assertEqual(validator.call_count, first)
            self.store.append(_draft("second", "再次"))
            after_append = validator.call_count
            self.assertTrue(self.store.health().healthy)
            self.assertEqual(validator.call_count, after_append + 1)

    def test_health_rejects_content_change_after_sql_before_success(self) -> None:
        self.store.append(_draft("new", "新增"))
        original = module._open_health_connection

        @contextmanager
        def mutate_after_sql(lease):
            with original(lease) as connection:
                yield connection
            changed = sqlite3.connect(self.identity.canonical_sidecar_path)
            try:
                changed.execute("UPDATE tm_record SET target_raw='changed' WHERE record_id=1")
                changed.commit()
            finally:
                changed.close()

        with patch.object(module, "_open_health_connection", mutate_after_sql):
            with self.assertRaises(module.SQLiteStoreSchemaError):
                self.store.health()
        self.assertIsNone(self.owner._current_index_validation)

    def test_source_monitor_reads_pair_under_original_root(self) -> None:
        family = self.owner._family
        original = family._backend.open_regular
        seen = []

        def observe(root, relative):
            if relative.name in {self.identity.configured_jsonl_path.name,
                                  self.identity.snapshot_manifest_path.name}:
                seen.append((root, relative.name))
            return original(root, relative)

        with patch.object(family._backend, "open_regular", side_effect=observe):
            observation = module.SourceBindingMonitor(self.owner).observe()
        self.assertEqual(observation.state, SourceBindingState.VERIFIED_CURRENT)
        self.assertEqual({name for _, name in seen}, {
            self.identity.configured_jsonl_path.name,
            self.identity.snapshot_manifest_path.name,
        })
        self.assertTrue(all(root is family._root for root, _ in seen))

    def test_source_diverged_lookup_update_preserved_without_source_write(self) -> None:
        changed = b'{"source":"same","target":"foreign"}\n'
        self.identity.configured_jsonl_path.write_bytes(changed)
        self.identity.snapshot_manifest_path.write_bytes(b"{}")
        observation = self.store.source_binding_monitor.observe()
        self.assertEqual(observation.state, SourceBindingState.SOURCE_DIVERGED)
        engine = tm_engine.TMEngine.from_open_canonical_store(
            self.store, configured_jsonl=self.identity.configured_jsonl_path,
            expected_resource_id=self.identity.resource_id,
        )
        self.assertNotEqual(engine.query_exact("same").target, "foreign")
        self.assertTrue(engine.save_record(tm_engine.SourceUnit("new", "new"), "新增"))
        self.assertEqual(engine.query_exact("new").target, "新增")
        self.assertEqual(self.store.source_binding_monitor.observe().state,
                         SourceBindingState.SOURCE_DIVERGED)
        self.assertEqual(self.identity.configured_jsonl_path.read_bytes(), changed)
        self.assertEqual(self.identity.snapshot_manifest_path.read_bytes(), b"{}")

    def test_root_drift_during_pair_observation_does_not_latch_divergence(self) -> None:
        original = module._configured_pair_diagnostics

        def moved(*args, **kwargs):
            self.move_and_clone()
            return original(*args, **kwargs)

        with patch.object(module, "_configured_pair_diagnostics", side_effect=moved):
            with self.assertRaises(module.SQLiteStoreLifecycleError):
                module.SourceBindingMonitor(self.owner).observe()
        self.assert_stopped()
        for root in (self.root, self.base / "original"):
            connection = sqlite3.connect(root / self.identity.canonical_sidecar_path.name)
            try:
                self.assertEqual(connection.execute(
                    "SELECT value FROM tm_meta WHERE key='divergence_latched'"
                ).fetchone(), ("0",))
            finally:
                connection.close()

    def test_missing_database_on_reconnect_is_not_created(self) -> None:
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            with self.store.query_lease() as view:
                view._candidate_connection()
                view._close_connection()
                self.identity.canonical_sidecar_path.rename(self.root / "retained.sqlite3")
                view.exact_records("same")
        self.assertFalse(self.identity.canonical_sidecar_path.exists())
        self.assert_stopped()

    def test_reconnect_observed_root_swap_never_opens_clone(self) -> None:
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            with self.store.query_lease() as view:
                view._candidate_connection()
                view._close_connection()
                self.move_and_clone()
                with patch.object(module.sqlite3, "connect", wraps=module.sqlite3.connect) as opened:
                    try:
                        view._candidate_connection()
                    finally:
                        self.assertEqual(opened.call_count, 0)
        self.assert_stopped()

    def test_actual_connect_reproves_after_locator_preparation(self) -> None:
        original = module._sqlite_connection_target

        def move_after_locator(*args, **kwargs):
            target = original(*args, **kwargs)
            self.move_and_clone()
            return target

        with patch.object(module, "_sqlite_connection_target", side_effect=move_after_locator), \
                patch.object(module.sqlite3, "connect", wraps=module.sqlite3.connect) as opened:
            with self.assertRaises(module.SQLiteStoreLifecycleError):
                self.store.exact_records("same")
        self.assertEqual(opened.call_count, 0, "真实 connect 前须消费原 borrow")
        self.assert_stopped()

    def test_actual_connect_reproves_before_connection_policy_sql(self) -> None:
        original = module.sqlite3.connect

        def move_after_connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            self.move_and_clone()
            return connection

        with patch.object(module.sqlite3, "connect", side_effect=move_after_connect), \
                patch.object(module, "_pragma_text", wraps=module._pragma_text) as policy:
            with self.assertRaises(module.SQLiteStoreLifecycleError):
                self.store.exact_records("same")
        self.assertEqual(policy.call_count, 0, "已观察到漂移后不可继续配置 SQL")
        self.assert_stopped()

    def test_cached_connection_reproves_before_returning_late_rows(self) -> None:
        original = module._record_from_row
        moved = False

        def late(row):
            nonlocal moved
            result = original(row)
            if not moved:
                moved = True
                self.move_and_clone()
            return result

        refused_at_method = False
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            with self.store.query_lease() as view:
                record = view.exact_records("same")[0]
                with patch.object(module, "_record_from_row", side_effect=late):
                    try:
                        view.records_by_id((record.record_id,))
                    except module.SQLiteStoreLifecycleError:
                        refused_at_method = True
        self.assertTrue(refused_at_method, "晚到 rows 不可先返回再依赖外层拒绝")
        self.assert_stopped()

    def test_maintenance_identity_waits_for_existing_lazy_sql_owner(self) -> None:
        started = threading.Event()

        def read_facts():
            started.set()
            return tm_engine.canonical_authority_facts(self.identity.configured_jsonl_path)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.query_lease() as view:
                connection = view._candidate_connection()
                connection.execute("BEGIN")
                connection.execute("SELECT * FROM tm_record").fetchall()
                future = pool.submit(read_facts)
                self.assertTrue(started.wait(1))
                # Maintenance SQL must not close another same-inode FD while
                # the canonical owner's SQLite transaction remains live.
                threading.Event().wait(0.15)
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=3),
                             (self.identity.resource_id, "store.primary"))

    def _assert_maintenance_sigint_in_child(self, *, outer_exception, body_failure=None):
        handler = signal.getsignal(signal.SIGINT)
        result = subprocess.run([
            sys.executable, "-B", "-c",
            "from tests.test_tm_posix_runtime_entrypoints import _maintenance_close_sigint_probe; "
            f"_maintenance_close_sigint_probe({outer_exception!r}, {body_failure!r})",
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertIs(signal.getsignal(signal.SIGINT), handler)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SIGINT=1; directory FDs closed; family=FAILED", result.stdout)

    def test_maintenance_close_sigint_inside_callers_except_propagates(self) -> None:
        self._assert_maintenance_sigint_in_child(outer_exception=True)

    def test_maintenance_close_sigint_without_outer_exception_propagates(self) -> None:
        self._assert_maintenance_sigint_in_child(outer_exception=False)

    def test_maintenance_mapped_body_failure_keeps_sigint_close_facts(self) -> None:
        self._assert_maintenance_sigint_in_child(outer_exception=True, body_failure="mapped")

    def test_maintenance_unmapped_body_failure_keeps_sigint_close_facts(self) -> None:
        self._assert_maintenance_sigint_in_child(outer_exception=True, body_failure="unmapped")

    def test_cold_maintenance_facts_remain_read_only_inside_callers_except(self) -> None:
        self.owner.close_family_session()
        with patch.object(module.ResourceStoreCoordinator, "rehydrate_runtime_authority",
                          side_effect=AssertionError("maintenance cannot rehydrate authority")):
            expected = (self.identity.resource_id, "store.primary")
            self.assertEqual(tm_engine.canonical_authority_facts(
                self.identity.configured_jsonl_path), expected)
            try:
                raise LookupError("caller's already handled exception")
            except LookupError:
                self.assertEqual(tm_engine.canonical_authority_facts(
                    self.identity.configured_jsonl_path), expected)
        self.assertNotIn(self.identity.configured_jsonl_path, module._FAMILY_SESSIONS)

    def _assert_locator_sigint_in_child(
        self, *, outer_exception=True, body_failure=None, interrupt=True, entrypoint="maintenance",
    ):
        handler = signal.getsignal(signal.SIGINT)
        result = subprocess.run([
            sys.executable, "-B", "-c",
            "from tests.test_tm_posix_runtime_entrypoints import _maintenance_locator_sigint_probe; "
            f"_maintenance_locator_sigint_probe({outer_exception!r}, {body_failure!r}, {interrupt!r}, {entrypoint!r})",
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertIs(signal.getsignal(signal.SIGINT), handler)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reader FDs closed; SQL after signal=0", result.stdout)

    def test_locator_sigint_inside_callers_except_stops_before_sql(self) -> None:
        self._assert_locator_sigint_in_child()

    def test_locator_sigint_without_outer_exception_stops_before_sql(self) -> None:
        self._assert_locator_sigint_in_child(outer_exception=False)

    def test_locator_body_failure_keeps_sigint_close_facts(self) -> None:
        self._assert_locator_sigint_in_child(body_failure="os")

    def test_locator_platform_failure_keeps_sigint_close_facts(self) -> None:
        for entrypoint in ("maintenance", "factory"):
            with self.subTest(entrypoint=entrypoint):
                self._assert_locator_sigint_in_child(body_failure="platform", entrypoint=entrypoint)

    def test_locator_platform_failure_keeps_public_codes_without_sigint(self) -> None:
        for entrypoint in ("maintenance", "factory"):
            with self.subTest(entrypoint=entrypoint):
                self._assert_locator_sigint_in_child(body_failure="platform", interrupt=False,
                                                    entrypoint=entrypoint)

    def test_health_and_lazy_reconnect_do_not_open_raw_db_during_sql(self) -> None:
        family = self.owner._family
        original_open = family._backend.open_regular
        original_connection = module._open_configured_connection
        live = 0

        @contextmanager
        def tracked(*args, **kwargs):
            nonlocal live
            with original_connection(*args, **kwargs) as connection:
                live += 1
                try:
                    yield connection
                finally:
                    live -= 1

        def checked(root, relative):
            if relative.name == self.identity.canonical_sidecar_path.name:
                self.assertEqual(live, 0, "SQL 存活时不得额外开关同 DB raw FD")
            return original_open(root, relative)

        with patch.object(module, "_open_configured_connection", tracked), \
                patch.object(family._backend, "open_regular", side_effect=checked):
            self.store.append(_draft("new", "新增"))
            with self.store.query_lease() as view:
                view._candidate_connection()
                self.assertTrue(view.health().healthy)
                self.assertTrue(view.exact_records("new"))
                self.assertTrue(view.records_by_id((1,)))

    def test_health_closes_before_capture_and_keeps_failed_reader_owned(self) -> None:
        self.store.append(_draft("new", "新增"))
        family = self.owner._family
        original_open = family._backend.open_regular
        opened_readers = []

        def remember(root, relative):
            opened = original_open(root, relative)
            if relative.name == self.identity.canonical_sidecar_path.name:
                opened_readers.append(opened)
            return opened

        with patch.object(family._backend, "open_regular", side_effect=remember):
            # This reader is created by the actual terminal health capture.
            original_health = module._open_health_connection
            close_patch = None

            @contextmanager
            def fail_terminal_close(lease):
                nonlocal close_patch
                with original_health(lease) as connection:
                    yield connection
                reader_class = type(opened_readers[-1])
                real_close = reader_class.close

                def reject_close(reader):
                    if reader is opened_readers[-1] and not reader.closed:
                        raise OSError("terminal runtime close failed")
                    return real_close(reader)

                close_patch = patch.object(reader_class, "close", reject_close)
                close_patch.start()

            try:
                with patch.object(module, "_open_health_connection", fail_terminal_close):
                    with self.assertRaisesRegex(OSError, "terminal runtime close failed"):
                        self.store.health()
            finally:
                if close_patch is not None:
                    close_patch.stop()
        self.assertIsNone(self.owner._current_index_validation)
        self.assertFalse(opened_readers[-1].closed)
        self.assertTrue(family._pending_activation_closes)
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            self.store.exact_records("same")
        # Explicit recovery owns the retained callback; no replacement reader.
        with self.owner._family_transition(recovery=True) as borrow:
            with self.owner._posix_recovery_port(borrow=borrow):
                pass
        self.assertTrue(opened_readers[-1].closed)
        self.assertFalse(family._pending_activation_closes)

    def test_real_hot_journal_recovers_before_health_content_baseline(self) -> None:
        database = self.identity.canonical_sidecar_path
        before = database.read_bytes()
        result = subprocess.run([
            sys.executable, "-B", "-c", """
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
        self.assertNotEqual(database.read_bytes(), before, "故障进程必须已实际溢出页面")
        self.assertTrue(self.store.health().healthy)
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse(database.with_name(database.name + "-journal").exists())
        self.assertTrue(self.store.exact_records("same"))

    def test_cancelled_waiter_cannot_revive_observed_root_failure(self) -> None:
        cancelled = threading.Event()
        family = self.owner._family

        def waiting():
            with self.owner._operation_lease(cancelled=cancelled):
                return "unexpected"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(module.SQLiteStoreLifecycleError):
                with self.store.query_lease() as view:
                    future = pool.submit(waiting)
                    with family._condition:
                        self.assertTrue(family._condition.wait_for(lambda: family._active_count == 2, 1))
                    self.move_and_clone()
                    try:
                        view.exact_records("same")
                    except module.SQLiteStoreLifecycleError:
                        pass
                    cancelled.set()
                    with self.assertRaises(module.SQLiteStoreLifecycleError):
                        future.result(timeout=3)
        self.assert_stopped()
        # Returning the original pathname cannot clear an observed failure.
        shutil.rmtree(self.root)
        (self.base / "original").rename(self.root)
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            self.store.health()

    def test_unsafe_pair_is_diverged_while_canonical_remains_usable(self) -> None:
        external = self.base / "external"
        external.write_bytes(b"must not be used")
        self.identity.configured_jsonl_path.unlink()
        self.identity.configured_jsonl_path.symlink_to(external)
        observation = module.SourceBindingMonitor(self.owner).observe()
        self.assertEqual(observation.state, SourceBindingState.SOURCE_DIVERGED)
        self.assertIn("SOURCE_BINDING.JSONL_UNSAFE", observation.diagnostic_codes)
        self.store.append(_draft("new", "新增"))
        self.assertTrue(self.store.exact_records("new"))
        self.assertEqual(external.read_bytes(), b"must not be used")

    def test_root_failure_does_not_stop_another_real_canonical_resource(self) -> None:
        peer_root = self.base / "peer"
        peer_root.mkdir()
        identity = _identity(peer_root)
        identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
        owner = module.ResourceStoreCoordinator(
            resource_identity=identity, canonical_store_id="store.peer",
        )
        self.addCleanup(owner.close_family_session)
        service = TMMigrationService(
            resource_identity=identity, canonical_store_id="store.peer", coordinator=owner,
        )
        self.assertIs(type(service.activate_initial(identity.configured_jsonl_path, identity.resource_id)), MigrationReport)
        peer = module.SQLiteTMStore.from_coordinator(owner)
        self.move_and_clone()
        with self.assertRaises(module.SQLiteStoreLifecycleError):
            self.store.health()
        self.assert_stopped()
        peer.append(_draft("healthy", "其他资源"))
        self.assertEqual(peer.exact_records("healthy")[0].target_raw, "其他资源")
        self.assertTrue(peer.health().healthy)

    def test_pair_root_failure_remains_primary_when_reader_close_also_fails(self):
        family = self.owner._family
        original_open = family._backend.open_regular
        original_facts = BoundRegularFile.content_facts
        original_close = BoundRegularFile.close
        retained = []
        pair_errors = []
        original_pair = module._runtime_pair_file_state
        def pair(*args, **kwargs):
            try:
                return original_pair(*args, **kwargs)
            except BaseException as error:
                pair_errors.append(error)
                raise
        def opened(root, relative):
            result = original_open(root, relative)
            if relative.name == self.identity.configured_jsonl_path.name:
                retained.append(result)
            return result
        def content(reader):
            result = original_facts(reader)
            if retained and reader is retained[0]:
                self.move_and_clone()
                raise OSError('pair read interrupted after root move')
            return result
        def closing(reader):
            if retained and reader is retained[0]:
                raise OSError('pair reader close secondary')
            return original_close(reader)
        try:
            with patch.object(family._backend, 'open_regular', side_effect=opened), patch.object(BoundRegularFile, 'content_facts', content), patch.object(BoundRegularFile, 'close', closing), patch.object(module, '_runtime_pair_file_state', side_effect=pair):
                with self.assertRaisesRegex(module.SQLiteStoreLifecycleError, 'STORE.RESOURCE_IDENTITY_STALE'):
                    module.SourceBindingMonitor(self.owner).observe()
                self.assertIsInstance(pair_errors[0], module.SQLiteStoreLifecycleError)
                self.assertIn('pair reader close secondary', ' '.join(getattr(pair_errors[0], '__notes__', ())))
        finally:
            for reader in retained:
                original_close(reader)


@unittest.skipIf(sys.platform == "win32", "POSIX rooted pair input profile")
class PosixRuntimePairProfileTests(unittest.TestCase):
    def _case(self, role, entry, case):
        """在真实文件动作边界注入变化，继续平台和两个公开 monitor。"""
        import platform_fs_posix

        fixture = PosixRuntimeEntrypointTests("runTest")
        try:
            fixture.setUp()
            self._observe_case(fixture, role, entry, case, platform_fs_posix)
        finally:
            # 每个矩阵案例及时关闭 retained root，不能累积到外层 test 收尾。
            fixture.doCleanups()

    def _observe_case(self, fixture, role, entry, case, platform):
        family = fixture.owner._family
        original_root, original_view = family._root, fixture.owner._view
        path = (fixture.identity.configured_jsonl_path if role == "jsonl"
                else fixture.identity.snapshot_manifest_path)
        external = fixture.base / "external"
        payload = path.read_bytes()
        if case in {"hardlink", "observed_link_removed"}:
            os.link(path, external)
            self.assertEqual(path.stat().st_nlink, 2)
        elif case == "content":
            path.write_bytes(payload + b"\n")
        elif case in {"missing", "directory", "symlink"}:
            path.unlink()
            if case == "directory":
                path.mkdir()
            elif case == "symlink":
                external.write_bytes(payload)
                path.symlink_to(external)

        selected, mutations, close_errors = [], [], []
        active = False
        reads = snapshots = 0
        real_pair = module._runtime_pair_file_state
        real_inspect = type(original_root).inspect_entry
        real_open = family._backend.open_regular
        real_facts, real_snapshot = BoundRegularFile.content_facts, BoundRegularFile.snapshot
        real_close, real_pread = BoundRegularFile.close, platform.os.pread

        def mutate():
            if mutations:
                return
            mutations.append(case)
            if case in {"open_link", "during_link", "after_link", "terminal_link", "close_pending"}:
                os.link(path, external)
                self.assertEqual(path.stat().st_nlink, 2)
            elif case.endswith("metadata"):
                value = path.stat()
                os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000))
            elif case in {"after_name_replace", "safe_before_open"}:
                previous = path.stat()
                path.rename(external)
                path.write_bytes(payload)
                self.assertNotEqual(path.stat().st_ino, previous.st_ino)
                self.assertEqual(path.read_bytes(), external.read_bytes())
                self.assertEqual((path.stat().st_nlink, external.stat().st_nlink), (1, 1))
            elif case == "observed_link_removed":
                external.unlink()
                self.assertEqual(path.stat().st_nlink, 1)
            elif case == "root_drift":
                fixture.move_and_clone()

        def runtime(lease, selected_path):
            nonlocal active
            active = selected_path == path
            try:
                return real_pair(lease, selected_path)
            finally:
                active = False

        def inspect(root, name):
            observed = real_inspect(root, name)
            if active and root is original_root and name == path.name:
                if case == "observed_link_removed":
                    self.assertEqual(observed.identity.link_count, 2)
                    mutate()
                elif case == "safe_before_open":
                    self.assertEqual(observed.identity.link_count, 1)
                    mutate()
            return observed

        def opening(root, relative):
            opened = real_open(root, relative)
            if active and relative.name == path.name:
                selected.append(opened)
                if case in {"open_link", "close_pending"}:
                    mutate()
            return opened

        def snapshot(opened):
            nonlocal snapshots
            if active and any(opened is reader for reader in selected):
                snapshots += 1
                if snapshots == 2 and case in {"terminal_link", "terminal_metadata"}:
                    mutate()
            return real_snapshot(opened)

        def facts(opened):
            nonlocal reads
            if active and any(opened is reader for reader in selected):
                reads += 1
                if case == "before_facts_metadata":
                    mutate()
                value = real_facts(opened)
                if case in {"after_link", "after_metadata", "after_name_replace", "root_drift"}:
                    mutate()
                return value
            return real_facts(opened)

        def pread(fd, count, offset):
            value = real_pread(fd, count, offset)
            if active and any(fd == reader._descriptor for reader in selected):
                if case in {"during_link", "during_metadata"}:
                    mutate()
            return value

        def close(opened):
            if (active and any(opened is reader for reader in selected)
                    and case == "close_pending" and not close_errors):
                error = OSError("controlled exact reader close failure")
                close_errors.append(error)
                raise error
            return real_close(opened)

        result = caught = None
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, "_runtime_pair_file_state", runtime))
            stack.enter_context(patch.object(type(original_root), "inspect_entry", inspect))
            stack.enter_context(patch.object(family._backend, "open_regular", side_effect=opening))
            stack.enter_context(patch.object(BoundRegularFile, "snapshot", snapshot))
            stack.enter_context(patch.object(BoundRegularFile, "content_facts", facts))
            stack.enter_context(patch.object(BoundRegularFile, "close", close))
            stack.enter_context(patch.object(platform.os, "pread", pread))
            monitor = (fixture.store.source_binding_monitor if entry == "store"
                       else module.SourceBindingMonitor(fixture.owner))
            try:
                result = monitor.observe()
            except BaseException as error:
                caught = error

        self.assertIs(family._root, original_root)
        self.assertIs(fixture.owner._view, original_view)
        self.assertEqual((fixture.owner._active_lease_count, family._active_count), (0, 0))
        if case == "close_pending":
            # 合成 close failure 仅验证已有 pending 协议；真实 SIGINT 另有子进程测试。
            self.assertIs(caught, close_errors[0])
            self.assertIsNone(result)
            self.assertEqual(len(selected), 1)
            self.assertEqual(len(family._pending_activation_closes), 1)
            callback = family._pending_activation_closes[0]
            self.assertTrue(any(cell.cell_contents is selected[0]
                                for cell in callback.__closure__ or ()))
            self.assertFalse(selected[0].closed)
            self.assertTrue(family._recovery_required)
            try:
                with self.assertRaises(module.SQLiteStoreLifecycleError):
                    fixture.store.exact_records("same")
                with fixture.owner._family_transition(recovery=True) as borrow:
                    with fixture.owner._posix_recovery_port(borrow=borrow):
                        pass
                self.assertFalse(family._pending_activation_closes)
            finally:
                real_close(selected[0])
            self.assertEqual(reads, 0, "open 后已见多链接不应读正文")
        elif case == "root_drift":
            self.assertIsInstance(caught, module.SQLiteStoreLifecycleError)
            self.assertIsNone(result)
            fixture.assert_stopped()
            with self.assertRaises(module.SQLiteStoreLifecycleError):
                fixture.store.exact_records("same")
        else:
            self.assertIsNone(caught)
            self.assertEqual(fixture.owner.state, "READY")
            safe = case in {"single", "safe_before_open"}
            self.assertEqual(result.state, SourceBindingState.VERIFIED_CURRENT if safe
                             else SourceBindingState.SOURCE_DIVERGED)
            suffix = "UNSAFE"
            if case == "missing":
                suffix = "MISSING"
            elif case == "content":
                suffix = "DIGEST_MISMATCH" if role == "jsonl" else "MISMATCH"
            if not safe:
                self.assertIn(f"SOURCE_BINDING.{role.upper()}_{suffix}", result.diagnostic_codes)
            if case in {"hardlink", "observed_link_removed", "open_link", "directory", "symlink", "missing"}:
                self.assertEqual(reads, 0, "已见 unsafe/absent 不得读正文")

            def fingerprint(item):
                if not item.exists() and not item.is_symlink():
                    return None
                stat = item.lstat()
                body = (os.readlink(item) if item.is_symlink() else item.read_bytes()
                        if item.is_file() else tuple(item.iterdir()))
                return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_nlink,
                        stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, body)

            paths = (fixture.identity.configured_jsonl_path,
                     fixture.identity.snapshot_manifest_path, external)
            before = tuple(fingerprint(item) for item in paths)
            self.assertTrue(fixture.store.exact_records("same"))
            fixture.store.append(_draft("pair profile update", "保留canonical"))
            self.assertEqual(fixture.store.exact_records("pair profile update")[0].target_raw,
                             "保留canonical")
            self.assertEqual(tuple(fingerprint(item) for item in paths), before)
            if not safe:
                self.assertEqual(module.SourceBindingMonitor(fixture.owner).observe().state,
                                 SourceBindingState.SOURCE_DIVERGED)
        self.assertTrue(all(reader.closed for reader in selected))
        if case not in {"single", "content", "missing", "directory", "symlink", "hardlink"}:
            self.assertEqual(mutations, [case], "指定真实文件变化必须发生")
        self.assertIs(family._root, original_root)
        self.assertIs(fixture.owner._view, original_view)
        self.assertEqual((fixture.owner._active_lease_count, family._active_count), (0, 0))

    def _matrix(self, cases):
        for role in ("jsonl", "manifest"):
            for entry in ("owner", "store"):
                for case in cases:
                    with self.subTest(role=role, entry=entry, case=case):
                        self._case(role, entry, case)

    def test_pair_profile_initial_files_and_safe_preopen_replacement(self):
        self._matrix(("single", "content", "missing", "directory", "symlink",
                      "hardlink", "observed_link_removed", "safe_before_open"))

    def test_pair_profile_open_and_content_capture_window(self):
        self._matrix(("open_link", "during_link", "during_metadata", "before_facts_metadata",
                      "after_link", "after_metadata", "after_name_replace",
                      "terminal_link", "terminal_metadata"))

    def test_pair_profile_root_failure_and_exact_pending_close(self):
        self._matrix(("root_drift", "close_pending"))


if __name__ == "__main__":
    unittest.main()
