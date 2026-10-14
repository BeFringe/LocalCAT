"""Core 10.6/10.9/10.11：公开首开恢复未完成 replacement 的真实 prior。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tm_contracts import CanonicalResourceIdentity, _stable_digest
import tm_activation_journal as journal


_CHILD = r'''
import json, os, sqlite3, sys
from pathlib import Path
from unittest.mock import patch
import tm_activation_recovery as recovery
import tm_sqlite_store as store
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_engine import TMEngine, SourceUnit, canonical_authority_facts
from tm_migration import TMMigrationService

root, mode, window = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
source = root / 'memory.jsonl'
identity = CanonicalResourceIdentity.from_configured_jsonl('tm.public.cold', source)
def emit(**facts):
    print(json.dumps(dict(pid=os.getpid(), **facts)), flush=True)

if mode in {'initial', 'replacement'}:
    source.write_text('{"source":"hello","target":"first"}\n')
    owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.public.prior')
    service = TMMigrationService(resource_identity=identity, canonical_store_id=owner.canonical_store_id, coordinator=owner)
    if mode == 'replacement':
        assert type(service.activate_initial(source, identity.resource_id)) is MigrationReport
        source.write_text('{"source":"hello","target":"second"}\n')
    def crash(*args, **kwargs):
        emit(phase=owner.durable_activation_phase)
        os._exit(73)
    original_advance = store._CoordinatorStorePort.advance_after_effect
    def advance(port, preparation, handle, phase, **kwargs):
        result = original_advance(port, preparation, handle, phase, **kwargs)
        if phase.value == 'MANIFEST_PUBLISHED':
            crash()
        return result
    injection = (patch.object(store._CoordinatorStorePort, 'advance_after_effect', advance)
                 if window == 'after_manifest' else
                 patch.object(recovery, '_publish_activation_receipt' if window == 'before_receipt' else '_publish_activation_manifest', crash))
    with injection:
        result = service.activate_initial(source, identity.resource_id) if mode == 'initial' else service.import_snapshot(source, identity.resource_id)
    raise AssertionError(result)

connections = []
original_connect = sqlite3.connect
def connect(*args, **kwargs):
    families = tuple(store._FAMILY_SESSIONS.values())
    assert len(families) == 1 and families[0]._operation_tokens
    families[0].reprove()
    connection = original_connect(*args, **kwargs)
    connections.append(connection)
    return connection

try:
    with patch.object(sqlite3, 'connect', connect):
        engine = TMEngine(str(source), **({} if mode == 'unknown' else {'expected_resource_id': identity.resource_id}))
    assert engine.canonical_store is not None
    owner = engine.canonical_store.coordinator
    if mode == 'append':
        assert engine.save_record(SourceUnit(id='added', text='added'), 'saved')
    query = engine.query_exact('hello')
    added = engine.query_exact('added')
    facts = dict(ok=True, generation=owner.current_generation,
                 store_id=owner.canonical_store_id, authority=canonical_authority_facts(source),
                 target=None if query is None else query.target,
                 added=None if added is None else added.target,
                 state=owner.state)
    owner.close_family_session()
    for connection in connections:
        try:
            connection.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError('SQL connection leaked')
except Exception as error:
    facts = dict(ok=False, error=str(error), error_type=type(error).__name__)
emit(**facts)
'''


@unittest.skipIf(sys.platform == 'win32', 'POSIX v2 公开冷恢复')
class PublicColdOpenIdentityTests(unittest.TestCase):
    def child(self, root: Path, mode: str, window: str = 'unused') -> dict:
        result = subprocess.run(
            [sys.executable, '-B', '-c', _CHILD, str(root), mode, window],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 73 if mode in {'initial', 'replacement'} else 0,
                         result.stderr + result.stdout)
        return json.loads(result.stdout)

    def fingerprint(self, root: Path) -> dict:
        return {str(path.relative_to(root)): (path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in root.rglob('*') if path.is_file()}

    def test_public_first_opener_recovers_initial_and_replacement(self):
        for scenario in ('initial', 'replacement'):
            for window in ('before_receipt', 'after_receipt', 'after_manifest'):
                for opener in ('known', 'unknown'):
                    with self.subTest(scenario=scenario, window=window, opener=opener), tempfile.TemporaryDirectory() as folder:
                        root = Path(folder).resolve()
                        crashed = self.child(root, scenario, window)
                        self.assertEqual(crashed['phase'], 'MANIFEST_PUBLISHED' if window == 'after_manifest' else 'DB_REPLACED')
                        pids = [crashed['pid']]
                        for action in (opener, 'known', 'unknown'):
                            result = self.child(root, action)
                            self.assertTrue(result['ok'], result)
                            self.assertEqual(result['generation'], 0 if scenario == 'initial' else 1)
                            self.assertEqual(result['target'], 'first' if scenario == 'initial' else 'second')
                            self.assertEqual(result['state'], 'READY')
                            self.assertEqual(result['authority'], ['tm.public.cold', result['store_id']])
                            pids.append(result['pid'])
                        self.assertEqual(len(set(pids)), len(pids))

    def test_rehashed_forged_prior_is_rejected_without_mutation(self):
        for window in ('before_receipt', 'after_receipt', 'after_manifest'):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                self.child(root, 'replacement', window)
                identity = CanonicalResourceIdentity.from_configured_jsonl('tm.public.cold', root / 'memory.jsonl')
                path = journal._activation_journal_path(identity)
                payload = json.loads(path.read_bytes())
                payload['prior_canonical_store_id'] = 'forged.public.prior'
                payload.pop('record_digest')
                payload['record_digest'] = _stable_digest(payload)
                path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
                parsed = journal._parse_activation_journal_bytes(path.read_bytes(), expected_journal_path=path)
                self.assertEqual(parsed.prior_canonical_store_id, 'forged.public.prior')
                before = self.fingerprint(root)
                for opener in ('known', 'unknown'):
                    result = self.child(root, opener)
                    self.assertFalse(result['ok'], result)
                    self.assertIn('ACTIVATION.RECOVERY_PRIOR_SET_INVALID', result['error'])
                    self.assertEqual(self.fingerprint(root), before)

    def test_completed_replacement_allows_append_and_source_divergence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.child(root, 'replacement', 'after_receipt')
            opened = self.child(root, 'known')
            self.assertTrue(opened['ok'], opened)
            appended = self.child(root, 'append')
            self.assertTrue(appended['ok'], appended)
            source = root / 'memory.jsonl'
            source.write_text('{"source":"external","target":"changed"}\n')
            before_source = source.read_bytes()
            result = self.child(root, 'unknown')
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['generation'], 1)
            self.assertEqual(result['target'], 'second')
            self.assertEqual(result['added'], 'saved')
            self.assertEqual(source.read_bytes(), before_source)

    def test_malformed_main_never_falls_back_to_current_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.child(root, 'replacement', 'before_receipt')
            identity = CanonicalResourceIdentity.from_configured_jsonl('tm.public.cold', root / 'memory.jsonl')
            journal._activation_journal_path(identity).write_text('{broken')
            before = self.fingerprint(root)
            result = self.child(root, 'unknown')
            self.assertFalse(result['ok'], result)
            self.assertEqual(self.fingerprint(root), before)


if __name__ == '__main__':
    unittest.main()
