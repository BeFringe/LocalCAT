"""Core 10.11：公开 owner 跨操作组合；既有 phase/SQL 细矩阵另行复验。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tm_contracts import CanonicalResourceIdentity, ExportReport, MigrationReport, SourceBindingState
from tm_engine import SourceUnit, TMEngine, open_canonical_tm_store
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteStoreLifecycleError, SQLiteTMStore
from tests.test_tm_sqlite_store import _draft


_COLD = """
import json, os, sys
from pathlib import Path
from tm_engine import open_canonical_tm_store
runtime = open_canonical_tm_store(Path(sys.argv[1]))
assert runtime is not None
print(json.dumps(dict(pid=os.getpid(), generation=runtime.coordinator.current_generation,
    targets=[r.target_raw for r in runtime.exact_records(sys.argv[2])],
    state=runtime.source_binding_monitor.observe().state.value,
    healthy=runtime.health().healthy)), flush=True)
runtime.coordinator.close_family_session()
"""

_WAIT = """
import json, sys, threading, time
from pathlib import Path
from tm_engine import open_canonical_tm_store
from tm_sqlite_store import SQLiteStoreLifecycleError
from tests.test_tm_sqlite_store import _draft
runtime = open_canonical_tm_store(Path(sys.argv[1]))
assert runtime is not None
owner = runtime.coordinator
print('READY', flush=True)
input()
mode = sys.argv[2]
cancelled = threading.Event()
timer = threading.Timer(0.15, cancelled.set)
if mode == 'cancel': timer.start()
started = time.monotonic()
try:
    with owner._operation_lease(timeout_seconds=0.15 if mode == 'timeout' else 2,
                                cancelled=cancelled):
        raise AssertionError('waiter acquired held W1')
except SQLiteStoreLifecycleError as error:
    print(json.dumps(dict(code=error.code, elapsed=time.monotonic()-started,
        active=owner._family._active_count, leases=owner._active_lease_count)), flush=True)
finally:
    if mode == 'cancel': timer.join(2)
input()
runtime.append(_draft(mode, 'reused-'+mode))
print(runtime.exact_records(mode)[0].target_raw, flush=True)
owner.close_family_session()
"""


def _files(root: Path) -> dict[str, tuple[int, str]]:
    return {str(path.relative_to(root)): (path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob('*') if path.is_file()}


@unittest.skipIf(sys.platform == 'win32', 'POSIX source 集成矩阵')
class PosixIntegrationMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def resource(self, name: str, *, activate: bool = True, filename: str = 'memory.jsonl'):
        root = self.base / name
        root.mkdir()
        source = root / filename
        source.write_text('{"source":"hello","target":"prior"}\n', encoding='utf-8')
        identity = CanonicalResourceIdentity.from_configured_jsonl('tm.' + name, source)
        owner = ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.' + name)
        self.addCleanup(owner.close_family_session)
        service = TMMigrationService(resource_identity=identity, canonical_store_id='store.' + name, coordinator=owner)
        if activate:
            self.assertIs(type(service.activate_initial(source, identity.resource_id)), MigrationReport)
        return identity, owner, service

    def cold(self, source: Path, query: str) -> dict:
        result = subprocess.run([sys.executable, '-B', '-c', _COLD, str(source), query],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_public_chain_refresh_replace_export_and_new_identity_import(self) -> None:
        # Core 接收已登记路径；托管/默认路径的 Application 登记仍归后置 owner。
        for name, filename in (('ordinary', 'tm.jsonl'), ('registered', '用户 记忆.jsonl')):
            with self.subTest(path=name):
                identity, owner, service = self.resource(name, filename=filename)
                source = identity.configured_jsonl_path
                runtime = SQLiteTMStore.from_coordinator(owner)
                original = source.read_bytes()
                engine = TMEngine.from_open_canonical_store(runtime, configured_jsonl=source,
                                                          expected_resource_id=identity.resource_id)
                self.assertEqual(engine.query_exact('hello').target, 'prior')
                self.assertTrue(engine.save_record(SourceUnit(id='saved', text='saved'), 'confirmed'))
                self.assertEqual(source.read_bytes(), original)
                self.assertIs(type(service.refresh_configured_snapshot(runtime)), ExportReport)
                old_store_id = owner.canonical_store_id
                replaced = service.import_snapshot(source, identity.resource_id)
                self.assertIs(type(replaced), MigrationReport, replaced)
                self.assertEqual(owner.current_generation, 1)
                self.assertNotEqual(owner.canonical_store_id, old_store_id)
                self.assertEqual(runtime.exact_records('saved')[0].target_raw, 'confirmed')
                output = self.base / (name + '-export.jsonl')
                self.assertIs(type(service.export_jsonl(runtime, output)), ExportReport)
                target, target_owner, target_service = self.resource(name + '-target', activate=False)
                target.configured_jsonl_path.unlink()  # initial payload apply requires an absent destination.
                commits = []
                imported = target_service.apply_snapshot_payload(output, target.resource_id,
                    expected_digest=hashlib.sha256(output.read_bytes()).hexdigest(),
                    expected_record_count=2, initial=True, owner_commit=commits.append)
                self.assertIs(type(imported), MigrationReport, imported)
                self.assertEqual(commits, [imported])
                self.assertNotEqual(target_owner.resource_id, owner.resource_id)
                self.assertNotEqual(target_owner.canonical_store_id, owner.canonical_store_id)
                owner.close_family_session()
                target_owner.close_family_session()
                for current_source, generation in ((source, 1), (target.configured_jsonl_path, 0)):
                    first, second = self.cold(current_source, 'saved'), self.cold(current_source, 'saved')
                    self.assertNotEqual(first['pid'], second['pid'])
                    for facts in (first, second):
                        self.assertEqual(facts['generation'], generation)
                        self.assertEqual(facts['targets'], ['confirmed'])
                        self.assertTrue(facts['healthy'])

    def test_source_and_manifest_divergence_survive_save_export_and_cold_reopen(self) -> None:
        for slot in ('source', 'manifest'):
            with self.subTest(slot=slot):
                identity, owner, service = self.resource(slot)
                runtime = SQLiteTMStore.from_coordinator(owner)
                changed = identity.configured_jsonl_path if slot == 'source' else identity.snapshot_manifest_path
                changed.write_bytes(b'{"external":"content change"}\n')
                before = changed.read_bytes()
                self.assertIs(runtime.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
                self.assertEqual(runtime.exact_records('hello')[0].target_raw, 'prior')
                engine = TMEngine.from_open_canonical_store(runtime, configured_jsonl=identity.configured_jsonl_path,
                                                          expected_resource_id=identity.resource_id)
                self.assertTrue(engine.save_record(SourceUnit(id='saved', text='saved'), 'while-diverged'))
                self.assertIs(type(service.export_jsonl(runtime, self.base / (slot + '-export.jsonl'))), ExportReport)
                self.assertEqual(changed.read_bytes(), before)
                self.assertIs(runtime.source_binding_monitor.observe().state, SourceBindingState.SOURCE_DIVERGED)
                owner.close_family_session()
                facts = self.cold(identity.configured_jsonl_path, 'saved')
                self.assertEqual(facts['targets'], ['while-diverged'])
                self.assertEqual(facts['state'], SourceBindingState.SOURCE_DIVERGED.value)
                self.assertEqual(changed.read_bytes(), before)

    def test_public_builder_continues_after_clone_and_stops_only_affected_family(self) -> None:
        for replacement in (False, True):
            for different in (False, True):
                with self.subTest(replacement=replacement, different=different):
                    name = f'builder-{replacement}-{different}'
                    identity, owner, service = self.resource(name, activate=replacement)
                    healthy_identity, healthy_owner, _ = self.resource(name + '-healthy')
                    healthy = SQLiteTMStore.from_coordinator(healthy_owner)
                    root = identity.configured_jsonl_path.parent
                    moved = self.base / (name + '-moved')
                    real_build, real_connect = service._build_stage, sqlite3.connect
                    entered, sql_after_drift, before = [], [], []
                    generation = owner.current_generation
                    registry = set(owner._sealed_registry._entries)
                    def build(*args, **kwargs):
                        entered.append(True)
                        root.rename(moved)
                        shutil.copytree(moved, root)
                        if different:
                            identity.configured_jsonl_path.write_bytes(b'foreign content\n')
                        before.extend((_files(root), _files(moved)))
                        return real_build(*args, **kwargs)
                    def connect(*args, **kwargs):
                        if entered:
                            sql_after_drift.append(str(args[0]))
                        return real_connect(*args, **kwargs)
                    outcome = None
                    with patch.object(service, '_build_stage', build), patch.object(sqlite3, 'connect', connect):
                        try:
                            outcome = (service.import_snapshot if replacement else service.activate_initial)(
                                identity.configured_jsonl_path, identity.resource_id)
                        except (OSError, RuntimeError, ValueError) as error:
                            outcome = error
                    self.assertEqual(entered, [True])
                    self.assertNotIsInstance(outcome, MigrationReport)
                    self.assertEqual(sql_after_drift, [])
                    self.assertEqual((_files(root), _files(moved)), tuple(before))
                    self.assertEqual(owner.current_generation, generation)
                    self.assertEqual(set(owner._sealed_registry._entries), registry)
                    self.assertEqual(owner._family._state, 'FAILED')
                    with self.assertRaises(ValueError):
                        open_canonical_tm_store(identity.configured_jsonl_path)
                    with self.assertRaises(SQLiteStoreLifecycleError):
                        service.import_snapshot(identity.configured_jsonl_path, identity.resource_id)
                    self.assertEqual((_files(root), _files(moved)), tuple(before))
                    healthy.append(_draft('healthy', 'available'))
                    self.assertEqual(healthy.exact_records('healthy')[0].target_raw, 'available')
                    self.assertTrue(healthy.health().healthy)
                    self.assertTrue(healthy_identity.canonical_sidecar_path.exists())

    def test_two_process_timeout_and_cancel_return_ownership_then_reuse(self) -> None:
        identity, owner, _ = self.resource('waiters')
        runtime = SQLiteTMStore.from_coordinator(owner)
        processes = []
        def cleanup(process):
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        def line(process):
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], '子进程等待预算已耗尽')
            result = process.stdout.readline().strip()
            self.assertTrue(result, '子进程提前退出')
            return result
        def send(process):
            process.stdin.write('continue\n')
            process.stdin.flush()
        for mode in ('timeout', 'cancel'):
            process = subprocess.Popen([sys.executable, '-B', '-c', _WAIT, str(identity.configured_jsonl_path), mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(cleanup, process)
            processes.append(process)
            self.assertEqual(line(process), 'READY')
        with runtime.query_lease() as view:
            self.assertEqual(view.exact_records('hello')[0].target_raw, 'prior')
            for process in processes:
                send(process)
            for process, code in zip(processes, ('STORE.FAMILY_BUSY', 'STORE.OPERATION_CANCELLED')):
                facts = json.loads(line(process))
                self.assertEqual(facts['code'], code)
                self.assertLess(facts['elapsed'], 2.5)
                self.assertEqual((facts['active'], facts['leases']), (0, 0))
        for process, mode in zip(processes, ('timeout', 'cancel')):
            send(process)
            self.assertEqual(line(process), 'reused-' + mode)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(runtime.exact_records(mode)[0].target_raw, 'reused-' + mode)
        self.assertEqual(owner._family._active_count, 0)
        self.assertEqual(owner._active_lease_count, 0)


if __name__ == '__main__':
    unittest.main()
