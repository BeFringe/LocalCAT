from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import trace_windows_frozen_handoff_identity as identity


EXE = 'C:/bundle/localcat-spike.exe'
DLL = 'C:/bundle/_internal/python314.dll'


def trace(profile):
    kind = max(1, identity.PROFILES.index(profile))
    events = ['W3_ID_BEGIN', 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
              'W3_ID_PYTHON_BASE 0000000180000000', 'W3_ID_FIRST_TAKE_READY']
    if kind != 1:
        events += ['W3_ID_MINTED', 'W3_ID_BOOTSTRAP_EVAL']
    obj, taken, state = ('0', 0, 1) if kind == 1 else ('0000000000200000', 1, 2)
    events += [f'W3_ID_TARGET kind={kind} self={obj} issued={obj} taken={taken} minted={taken} '
               f'state={state} armed=1 closed=0 tid=1234',
               'W3_ID_THREAD_CALL', 'W3_ID_THREAD current=1234 owner=1234 tid=1234',
               'W3_ID_TSTATE_CALL', 'W3_ID_STATE tstate=0000000000100000 actual=0000000000300000 owner=0000000000300000',
               'W3_ID_RETURN original=0000000000300000 owner=0000000000300000']
    if profile == 'control':
        events += ['W3_ID_COMPARE equal=1', 'W3_ID_OWNER result=1', 'W3_ID_MINTED',
                   'W3_ID_BOOTSTRAP_EVAL', 'W3_ID_EVAL_OK', 'FROZEN_ENTRY.SPIKE_COMPLETED']
        marker = 'FROZEN_ENTRY.SPIKE_COMPLETED'
    else:
        events += ['W3_ID_INJECT value=0000000000000001', 'W3_ID_COMPARE equal=0', 'W3_ID_OWNER result=0',
                   'W3_ID_ERROR FROZEN_ENTRY.'+('HANDOFF_UNAVAILABLE' if kind == 1 else 'AUTHORITY_UNAVAILABLE')]
        if kind != 1:
            events += ['W3_ID_METHOD_NULL', 'W3_ID_EVAL_NULL']
        marker = 'FROZEN_ENTRY.BOOTSTRAP_EXECUTION_'+('UNAVAILABLE' if kind == 1 else 'FAILED')
        events += ['W3_ID_ABORT', 'W3_ID_REVOKED', 'W3_ID_NATIVE_FAILURE '+marker]
    events += ['W3_ID_EXIT', f'Last event: 1.1234: Exit process 0:1, code {int(profile != "control")}']
    return ('ModLoad: 0000000180000000 0000000180700000 '+DLL+'\n'+'\n'.join(events)+'\n',
            'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n'+marker+'\n')


class HandoffIdentityTests(unittest.TestCase):
    def validate(self, profile, output=None, stderr=None):
        default_output, default_stderr = trace(profile)
        return identity.validate_trace(default_output if output is None else output,
            default_stderr if stderr is None else stderr, profile, executable_path=EXE, python_path=DLL)

    def test_profiles_validate_natural_terminal_paths_without_claiming_second_interpreter(self):
        for profile in identity.PROFILES:
            with self.subTest(profile=profile):
                facts = self.validate(profile)
                self.assertEqual(facts['debuggee_exit_code'], int(profile != 'control'))
                self.assertEqual(facts['injections'], int(profile != 'control'))
                self.assertIs(facts['cross_interpreter_execution_observed'], False)

    def test_real_state_identity_and_thread_cannot_be_faked_by_another_failed_condition(self):
        changes = [('taken=1', 'taken=0'), ('minted=1', 'minted=0'), ('state=2', 'state=1'),
                   ('armed=1', 'armed=0'), ('closed=0', 'closed=1'), ('kind=2', 'kind=3'),
                   ('current=1234', 'current=4321'), ('owner=1234', 'owner=4321'),
                   ('original=0000000000300000', 'original=0000000000400000'),
                   ('actual=0000000000300000', 'actual=0000000000400000'),
                   ('tstate=0000000000100000', 'tstate=0000000000000000'),
                   ('issued=0000000000200000', 'issued=0000000000500000'),
                   ('value=0000000000000001', 'value=0000000000000000'),
                   ('value=0000000000000001', 'value=0000000000300000')]
        output, _ = trace('read')
        for old, new in changes:
            with self.subTest(old=old), self.assertRaises(identity.IdentityTraceError):
                self.validate('read', output.replace(old, new))
        output, _ = trace('first-take')
        with self.assertRaises(identity.IdentityTraceError):
            self.validate('first-take', output.replace('taken=0', 'taken=1'))

    def test_every_event_is_required_once_and_in_order(self):
        for profile in identity.PROFILES:
            output, _ = trace(profile)
            for event in output.splitlines():
                if not event.startswith(('W3_ID_', 'FROZEN_ENTRY.')):
                    continue
                for mutated in (output.replace(event+'\n', ''), output.replace(event+'\n', event+'\n'+event+'\n')):
                    with self.subTest(profile=profile, event=event), self.assertRaises(identity.IdentityTraceError):
                        self.validate(profile, mutated)

    def test_io_extra_errors_debugger_uncertainty_and_wrong_modules_are_rejected(self):
        output, stderr = trace('close')
        for extra in ('W3_ID_UNEXPECTED_READ', 'W3_ID_UNEXPECTED_FIND', 'W3_ID_GUARD_REJECT',
                      'W3_ID_MINTED', 'W3_ID_INJECT value=0000000000000001', 'W3_ID_UNKNOWN',
                      'FROZEN_ENTRY.SPIKE_COMPLETED', 'Memory access error', 'WARNING: mystery'):
            with self.subTest(extra=extra), self.assertRaises(identity.IdentityTraceError):
                self.validate('close', output.replace('W3_ID_ABORT', extra+'\nW3_ID_ABORT'))
        for changed in ('', stderr+'MemoryError\n', stderr+'FROZEN_ENTRY.UNKNOWN\n', stderr*2):
            with self.subTest(stderr=changed), self.assertRaises(identity.IdentityTraceError):
                self.validate('close', stderr=changed)
        for changed in (output.replace(DLL, 'C:/other/python314.dll'),
                        output.replace('0000000180000000', '0000000180000001'),
                        output.replace('code 1', 'code 0')):
            with self.assertRaises(identity.IdentityTraceError):
                self.validate('close', changed)

    def test_only_exact_single_anchored_timestamp_warning_is_diagnostic(self):
        output, _ = trace('control')
        warning = '*** WARNING: Unable to verify timestamp for '+EXE.replace('/', '\\')+'\n'
        result = self.validate('control', warning+output)
        self.assertEqual(len(result['known_diagnostics']), 1)
        for changed in (warning+warning+output, warning.replace('localcat-spike', 'other')+output):
            with self.assertRaises(identity.IdentityTraceError):
                self.validate('control', changed)

    def test_wrong_disk_anchors_fail_before_machine_interpretation(self):
        with self.assertRaisesRegex(identity.IdentityTraceError, 'exact final PE'):
            identity.verify_machine_sites(b'not PE', b'not Python', [])

    @unittest.skipUnless(os.name == 'nt', 'orchestration requires Windows')
    def test_external_anchor_and_complete_post_inventory_are_required(self):
        import pefile
        for drift in (None, 'dist', 'release', 'candidate', 'helper', 'observer', 'script', 'pefile', 'metadata'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root/'artifacts/windows').mkdir(parents=True)
                candidate_path = root/'packaging/windows/frozen-entry/candidate-input.lock.json'
                candidate_path.parent.mkdir(parents=True)
                candidate_path.write_bytes(b'candidate')
                release = root/'release.json'; release.write_bytes(b'release')
                parser_source = root/'pefile.py'; parser_source.write_bytes(b'parser')
                observer_source = root/'observer.py'; observer_source.write_bytes(b'observer')
                metadata = ['metadata']
                candidate = {'candidate_input_digest':'candidate',
                    'evidence_producer':{'pefile':{'source':{}, 'version':pefile.__version__,
                        'distribution_metadata':identity.byte_fact(b'metadata')}},
                    'runtime':{'cpython':{'dll':{}}}, 'entry_contract':{'python_c_api':[]}}
                def simulate(command, cwd, environment, directory, name, records, **kwargs):
                    output, stderr = trace('control')
                    (directory/'cdb.stdout').write_text(output.replace(DLL, str(root/'bundle/_internal/python314.dll')), encoding='utf-8')
                    (directory/'cdb.stderr').write_text(stderr, encoding='utf-8')
                    records.append({'exit_code':0})
                    changed = {'release':release, 'candidate':candidate_path, 'pefile':parser_source,
                               'observer':observer_source, 'script':directory/'identity.cdb'}.get(drift)
                    if changed is not None:
                        changed.write_bytes(b'changed')
                    if drift == 'metadata':
                        metadata[0] = 'changed'
                before = {'executable':{}}
                checks = [before, identity.IdentityTraceError('dist changed')] if drift == 'dist' else [before, before]
                with (patch.object(identity, 'ROOT', root),
                      patch.object(identity, '__file__', str(observer_source)),
                      patch.object(identity, 'verify_release_binding', side_effect=checks) as verify,
                      patch.object(identity, 'load_candidate', return_value=candidate),
                      patch.object(identity, 'checked_bytes', return_value=b'pinned'),
                      patch.object(identity, 'verify_machine_sites', return_value={}),
                      patch.object(pefile, '__file__', str(parser_source)),
                      patch.object(identity.importlib.metadata, 'distribution', return_value=SimpleNamespace(read_text=lambda _:metadata[0])),
                      patch.object(identity, 'observer_inventory', side_effect=[{'helper':'before'}, {'helper':'changed' if drift == 'helper' else 'before'}]),
                      patch.object(identity, 'record_command', side_effect=simulate)):
                    args = SimpleNamespace(dist=root/'bundle', release=release, cdb=root/'cdb.exe', profile='control',
                        expected_release_sha256='external-release', expected_commit='external-commit', expected_candidate_digest='candidate')
                    if drift is None:
                        _, evidence = identity.run(args)
                        self.assertEqual(evidence['status'], 'OBSERVED_CONTROLLED_IDENTITY_PATH')
                    else:
                        with self.assertRaises(identity.IdentityTraceError):
                            identity.run(args)
                    self.assertEqual(verify.call_count, 2)
                    self.assertEqual(verify.call_args_list[0].kwargs, {
                        'expected_release_sha256':'external-release', 'expected_repository_commit':'external-commit',
                        'expected_candidate_input_digest':'candidate'})
                evidence_file, = (root/'artifacts/windows').glob('handoff-identity-*/evidence.json')
                evidence = json.loads(evidence_file.read_bytes())
                self.assertEqual(evidence['post_observation_inputs_unchanged'], drift is None)
                self.assertEqual(evidence['status'], 'OBSERVED_CONTROLLED_IDENTITY_PATH' if drift is None else 'INCOMPLETE')

    def test_profiles_are_explicitly_not_real_cross_interpreter_execution(self):
        self.assertEqual(identity.PROFILES, ('control', 'first-take', 'read', 'reproof', 'close'))
        self.assertEqual(identity.CLASSIFICATION, 'API_IDENTITY_MISMATCH_NOT_REAL_CROSS_INTERPRETER')

    def test_commands_change_only_one_child_return_register(self):
        for profile in identity.PROFILES:
            with self.subTest(profile=profile):
                script = identity.render_commands(profile)
                self.assertEqual(script.count('r rax=1;'), int(profile != 'control'))
                self.assertNotIn('r rip=', script)
                self.assertNotIn('.call', script)
                self.assertNotIn(' ewo ', script)
                self.assertIn('W3_ID_GUARD_REJECT', script)
                self.assertIn('W3_ID_EXIT', script)
                self.assertIn('0x3a12', script)
                self.assertLess(max(map(len, script.splitlines())), 4096)
        with self.assertRaises(identity.IdentityTraceError):
            identity.render_commands('wrong-interpreter')

    def test_owner_comparator_is_complete_exact_code_not_source_regex(self):
        code = bytes.fromhex(identity.OWNER_CODE)
        identity.check_owner_code(code)
        for index in range(len(code)):
            damaged = bytearray(code)
            damaged[index] ^= 1
            with self.subTest(index=index), self.assertRaises(identity.IdentityTraceError):
                identity.check_owner_code(bytes(damaged))
        with self.assertRaises(identity.IdentityTraceError):
            identity.check_owner_code(code + b'\x90')


if __name__ == '__main__':
    unittest.main()
