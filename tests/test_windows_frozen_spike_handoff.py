"""Behavior tests of spike assertions with fake native authority, NOT E10 proof.

Real native ownership/type flags and the new final PE require separate packaged
execution. These fakes deliberately exercise the Python selftest's assertions.
"""
import builtins
import _thread
from pathlib import Path
from types import SimpleNamespace
import unittest


BOOTSTRAP = Path(__file__).resolve().parents[1] / 'packaging/windows/frozen-entry/spike/localcat_frozen_bootstrap.py'
UNAVAILABLE = 'FROZEN_ENTRY.AUTHORITY_UNAVAILABLE'
SERIALIZABLE = 'FROZEN_ENTRY.AUTHORITY_NOT_SERIALIZABLE'


def fake_native(defect=None):
    state = SimpleNamespace(owner=_thread.get_ident(), closed=False, calls=[], takes=0, successful_takes=0)
    class OpaqueMeta(type):
        def __new__(mcls, name, bases, namespace):
            if any(isinstance(base, OpaqueMeta) for base in bases) and defect != 'subclass':
                raise TypeError('fake authority is not an acceptable base type')
            return super().__new__(mcls, name, bases, namespace)

        def __setattr__(cls, name, value):
            if defect != 'type-write':
                raise TypeError('fake authority type is immutable')

    class Authority(metaclass=OpaqueMeta):
        __slots__ = ()

        def __new__(cls):
            if defect == 'construct':
                return object.__new__(cls)
            raise RuntimeError('FROZEN_ENTRY.AUTHORITY_NOT_CONSTRUCTIBLE')

        def __setattr__(self, name, value):
            if defect == 'instance-write':
                return
            if name == '__class__':
                raise TypeError('fake authority class is immutable')
            raise AttributeError('fake authority has no writable attributes')

        def check(self, operation):
            worker = _thread.get_ident() != state.owner
            state.calls.append((operation, worker, state.closed))
            if worker:
                if defect == 'wrong-worker-error':
                    raise RuntimeError('unexpected worker error')
                if defect != 'worker-' + operation:
                    raise RuntimeError(UNAVAILABLE)
            if state.closed and defect != 'closed-' + operation:
                raise RuntimeError(UNAVAILABLE)

        def read_verified(self, entry):
            self.check('read')
            if entry == 'critical-source':
                return b'def verify_fixture(value):\n    return value == b"fixture"\n'
            return b'fixture'

        def module_reproof(self, entry):
            self.check('reproof')
            return (True, entry)

        def close(self):
            self.check('close')
            state.closed = True
            return True

        def __copy__(self):
            if defect == 'copy':
                return self
            raise RuntimeError(SERIALIZABLE)

        def __deepcopy__(self, memo):
            if defect == 'deepcopy':
                return self
            raise RuntimeError(SERIALIZABLE)

        def __reduce__(self):
            if defect == 'reduce':
                return ('forged',)
            raise RuntimeError(SERIALIZABLE)

        def __reduce_ex__(self, protocol):
            if defect == 'reduce-ex':
                return ('forged',)
            raise RuntimeError(SERIALIZABLE)

    issued = object.__new__(Authority)
    def take():
        state.takes += 1
        if defect == 'duplicate-take':
            state.successful_takes += 1
            return issued
        raise RuntimeError('FROZEN_ENTRY.HANDOFF_UNAVAILABLE')

    # A Python class cannot emulate the C type's object.__new__ safety check.
    # Intercept only this builtin in the test namespace; the real spike uses
    # the real builtin and must be independently exercised in a fresh PE.
    class FakeObjectBuiltin:
        @staticmethod
        def __new__(kind):
            if defect == 'base-construct':
                return issued
            raise TypeError('object.__new__ is unsafe for fake opaque type')

    return SimpleNamespace(FrozenBootstrapAuthority=Authority, take_attestation=take), issued, state, FakeObjectBuiltin


class HandoffSelftestTests(unittest.TestCase):
    def execute(self, defect=None, thread_api=_thread):
        native, authority, state, object_builtin = fake_native(defect)
        imports = []
        def limited_import(name, *args, **kwargs):
            imports.append(name)
            if name == '_localcat_frozen_bootstrap':
                return native
            if name == '_thread':
                return thread_api
            raise AssertionError('spike added an external import: ' + name)
        available = dict(vars(builtins), __import__=limited_import, object=object_builtin)
        namespace = {'__builtins__': available, '__name__': 'localcat_spike_bootstrap',
                     '__file__': '_internal/localcat_frozen_bootstrap.py', '_authority': authority}
        self.last_namespace = namespace
        code = compile(BOOTSTRAP.read_bytes(), str(BOOTSTRAP), 'exec')
        exec(code, namespace, namespace)
        return namespace, state, imports

    def test_complete_structured_facts_and_native_first_take_are_preserved(self):
        namespace, state, imports = self.execute()
        facts = namespace['_handoff_selftest']
        self.assertEqual(facts['schema'], 'localcat.w3.handoff-selftest.v1')
        self.assertEqual(facts['scope'], 'spike-only')
        self.assertEqual(facts['state'], 'COMPLETE')
        self.assertEqual(facts['threads']['owner'], _thread.get_ident())
        self.assertNotEqual(facts['threads']['worker'], facts['threads']['owner'])
        self.assertEqual(facts['threads']['worker'], facts['threads']['started'])
        checks = {check[0]: check[1:] for check in facts['checks']}
        expected_labels = {
            'duplicate-take', 'construct', 'direct-new', 'base-construct', 'subclass',
            'instance-write', 'instance-type-write', 'type-write', 'copy', 'deepcopy',
            'reduce', 'reduce-ex', 'worker-read', 'worker-reproof', 'worker-close',
            'owner-read-after-worker', 'close', 'saved-read-after-close',
            'saved-reproof-after-close', 'saved-close-after-close', 'saved-loader-after-close',
            'read-after-close', 'reproof-after-close', 'close-after-close', 'take-after-close',
        }
        self.assertEqual(set(checks), expected_labels)
        self.assertEqual(len(facts['checks']), len(expected_labels))
        self.assertEqual(checks['duplicate-take'], ('RuntimeError', 'FROZEN_ENTRY.HANDOFF_UNAVAILABLE'))
        for operation in ('read', 'reproof', 'close'):
            self.assertEqual(checks['worker-' + operation], ('RuntimeError', UNAVAILABLE))
            self.assertEqual(checks['saved-' + operation + '-after-close'], ('RuntimeError', UNAVAILABLE))
        self.assertEqual(checks['saved-loader-after-close'], ('RuntimeError', UNAVAILABLE))
        self.assertEqual(checks['owner-read-after-worker'], ('OK',))
        self.assertEqual(set(facts['excluded']), {'early-take', 'wrong-interpreter', 'cross-process-pickle'})
        self.assertEqual(imports, ['_localcat_frozen_bootstrap', '_thread'])
        self.assertEqual(state.successful_takes, 0)
        self.assertEqual(state.takes, 2)
        self.assertTrue(state.closed)

    def test_each_weakened_native_contract_fails_the_spike_assertions(self):
        for defect in ('duplicate-take', 'construct', 'base-construct', 'subclass',
                       'instance-write', 'type-write', 'copy', 'deepcopy', 'reduce', 'reduce-ex',
                       'worker-read', 'worker-reproof', 'worker-close', 'wrong-worker-error',
                       'closed-read', 'closed-reproof', 'closed-close'):
            with self.subTest(defect=defect):
                with self.assertRaisesRegex(RuntimeError, 'FROZEN_ENTRY.HANDOFF_SELFTEST_'):
                    self.execute(defect)
                self.assertEqual(self.last_namespace['_handoff_selftest']['state'], 'RUNNING')

    def test_completed_worker_releases_both_sides_of_bounded_wait(self):
        calls = []
        class TracedLock:
            def __init__(self):
                self.lock = _thread.allocate_lock()
            def acquire(self, blocking=True, timeout=-1):
                calls.append(('acquire', blocking, timeout))
                return self.lock.acquire(blocking, timeout)
            def release(self):
                calls.append(('release',))
                self.lock.release()
        thread_api = SimpleNamespace(allocate_lock=TracedLock, start_new_thread=_thread.start_new_thread,
                                     get_ident=_thread.get_ident)
        self.execute(thread_api=thread_api)
        self.assertEqual([call for call in calls if call[0] == 'acquire'],
                         [('acquire', False, -1), ('acquire', True, 5.0)])
        self.assertEqual(calls.count(('release',)), 2)

    def test_worker_timeout_is_bounded_and_does_not_fake_completed_evidence(self):
        calls = []
        class NeverSignalled:
            def acquire(self, blocking=True, timeout=-1):
                calls.append(('acquire', blocking, timeout))
                return not blocking
            def release(self):
                calls.append(('release',))
        thread_api = SimpleNamespace(allocate_lock=NeverSignalled, start_new_thread=lambda callback, args: 1,
                                     get_ident=_thread.get_ident)
        with self.assertRaisesRegex(RuntimeError, 'FROZEN_ENTRY.HANDOFF_SELFTEST_WORKER_TIMEOUT'):
            self.execute(thread_api=thread_api)
        self.assertEqual(self.last_namespace['_handoff_selftest']['state'], 'RUNNING')
        self.assertEqual(calls, [('acquire', False, -1), ('acquire', True, 5.0)])

    def test_failed_worker_start_releases_initial_completion_lock(self):
        calls = []
        class InitialLock:
            def acquire(self, blocking=True, timeout=-1):
                calls.append(('acquire', blocking, timeout))
                return True
            def release(self):
                calls.append(('release',))
        def failed_start(*args):
            raise RuntimeError('thread creation failed')
        thread_api = SimpleNamespace(allocate_lock=InitialLock, start_new_thread=failed_start,
                                     get_ident=_thread.get_ident)
        with self.assertRaisesRegex(RuntimeError, 'thread creation failed'):
            self.execute(thread_api=thread_api)
        self.assertEqual(calls, [('acquire', False, -1), ('release',)])

    def test_worker_must_differ_from_owner_and_match_the_created_thread(self):
        owner = _thread.get_ident()
        def wrong_started_id(callback, args):
            return _thread.start_new_thread(callback, args) + 1
        for identity, start in ((lambda: owner, _thread.start_new_thread),
                                (_thread.get_ident, wrong_started_id)):
            thread_api = SimpleNamespace(allocate_lock=_thread.allocate_lock, get_ident=identity,
                                         start_new_thread=start)
            with self.assertRaisesRegex(RuntimeError, 'FROZEN_ENTRY.HANDOFF_SELFTEST_THREAD_IDENTITY'):
                self.execute(thread_api=thread_api)


if __name__ == '__main__':
    unittest.main()
