"""Observer checks; synthetic logs never replace real child fault runs."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import trace_windows_frozen_retained_failures as retained


class RetainedFailureTests(unittest.TestCase):
    def test_machine_instruction_changes_fail_closed(self):
        expected = {0x100: 'ff1501020304', 0x200: '0f8411223344'}
        image = {rva: bytes.fromhex(value) for rva, value in expected.items()}
        retained.check_instructions(lambda rva, size: image[rva][:size], expected)
        for rva in expected:
            changed = dict(image)
            changed[rva] = bytes([image[rva][0] ^ 1]) + image[rva][1:]
            with self.assertRaises(retained.ProbeInputError):
                retained.check_instructions(lambda address, size: changed[address][:size], expected)

    def test_unknown_profile_and_unlocked_pe_are_rejected(self):
        with self.assertRaises(retained.ProbeInputError):
            retained.render_commands('syntax-failure', {})
        with self.assertRaises(retained.ProbeInputError):
            retained.verify_machine_sites(b'wrong exe', b'wrong python', [])

    def test_rebound_pe_is_not_exempt_from_instruction_checks(self):
        with patch.object(retained,'byte_fact',side_effect=lambda data: {'sha256':
                '2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea' if data==b'exe' else retained.PYTHON_SHA}), \
                patch('pefile.PE',side_effect=AssertionError('machine decoder reached')) as decoder:
            with self.assertRaisesRegex(AssertionError,'machine decoder reached'):
                retained.verify_machine_sites(b'exe',b'python',[])
            decoder.assert_called_once()

    def test_profile_names_describe_the_injected_failure(self):
        self.assertEqual(retained.PROFILES, (
            'control', 'clear-before-missing-path', 'compile-filename-allocation',
            'body-allocation', 'install-allocation'))

    def test_only_one_child_return_is_mutated_and_no_new_capi_is_called(self):
        for profile in retained.PROFILES:
            script = retained.render_commands(profile, retained.TEST_ANCHORS)
            self.assertEqual(script.count('r rax=0;'), 0 if profile == 'control' else 1)
            self.assertEqual(script.count('r rip=@rip+6;'), int(profile.endswith('allocation')))
            for forbidden in ('r rsp=', 'r rip=poi(@rsp)', '.call ', 'eb ', 'eq '):
                self.assertNotIn(forbidden, script)
            self.assertIn('NtCreateFile', script)
            self.assertIn('NtQueryAttributesFile', script)
            self.assertIn('@$tid == @$t14', script)
            self.assertIn('W3_RF_GUARD_REJECT', script)
            self.assertLess(max(map(len, script.splitlines())), 4096)
            self.assertEqual(script.splitlines()[-1], 'g')

    def trace(self, profile):
        events = retained.expected_events(profile)
        text = ('ModLoad: 00000001`80000000 00000001`81000000 C:\\bundle\\_internal\\python314.dll\n'
                'W3_RF_PYTHON_BASE 00000001`80000000\n' + '\n'.join(events) + '\n')
        code = '0' if profile == 'control' else '1'
        return text + 'Last event: 123.456: Exit process 0:123, code ' + code + '\n'

    def stderr(self, profile):
        if profile == 'control':
            return 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'
        frames = ('  File "<frozen importlib._bootstrap>", line ' +
                  ('1570' if profile == 'install-allocation' else '1568') +
                  ', in _install_external_importers\n')
        if profile == 'body-allocation':
            frames += '  File "<localcat-retained:importlib-external>", line 233, in <module>\n'
        elif profile == 'install-allocation':
            frames += ('  File "<localcat-retained:importlib-external>", line 1560, in _install\n'
                       '  File "<localcat-retained:importlib-external>", line 1546, in _get_supported_file_loaders\n')
        error = ('RuntimeError: FROZEN_ENTRY.INTERPRETER_PATH_CLEAR_FAILED' if profile.startswith('clear-') else
                 'object address  : 000001437591DF00\nobject refcount : 3\n'
                 'object type     : 00007FFAD86F0860\nobject type name: MemoryError\n'
                 'object repr     : MemoryError()\nlost sys.stderr')
        return ('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nTraceback (most recent call last):\n' + frames + error +
                '\nFROZEN_ENTRY.INITIALIZATION_FAILED\n')

    def test_each_profile_requires_its_complete_ordered_timeline(self):
        for profile in retained.PROFILES:
            with self.subTest(profile=profile):
                output, error = self.trace(profile), self.stderr(profile)
                result = retained.validate_trace(output, error, profile, 'C:/bundle/localcat-spike.exe')
                self.assertEqual(result['observed_debuggee_exit_code'], int(profile != 'control'))
                for marker in retained.expected_events(profile):
                    with self.assertRaises(retained.ProbeInputError):
                        retained.validate_trace(output.replace(marker + '\n', '', 1), error, profile,
                                                'C:/bundle/localcat-spike.exe')

    def test_duplicate_events_wrong_exit_missing_pyerr_and_late_handoff_reject(self):
        for profile in retained.PROFILES[1:]:
            output, error = self.trace(profile), self.stderr(profile)
            for changed, diagnostic in (
                (output + 'W3_RF_INJECT\n', error),
                (output + 'W3_RF_HANDOFF\n', error),
                (output.replace('code 1', 'code 0'), error),
                (output.replace('Last event: 123.456: Exit process 0:123, code 1\n', ''), error),
                (output, 'FROZEN_ENTRY.INITIALIZATION_FAILED\n'),
                (output + 'W3_RF_GUARD_REJECT\n', error),
                (output + 'W3_RF_UNKNOWN\n', error),
            ):
                with self.assertRaises(retained.ProbeInputError):
                    retained.validate_trace(changed, diagnostic, profile, 'C:/bundle/localcat-spike.exe')

    def test_exact_anchored_zero_timestamp_warning_only(self):
        output, error = self.trace('control'), self.stderr('control')
        warning = '*** WARNING: Unable to verify timestamp for C:\\bundle\\localcat-spike.exe\n'
        result = retained.validate_trace(warning + output, error, 'control', 'C:/bundle/localcat-spike.exe')
        self.assertEqual(len(result['known_diagnostics']), 1)
        for prefix in (warning * 2, warning.replace('localcat-spike.exe', 'other.exe'),
                       'Unable to insert breakpoint\n', 'WaitForEvent failed\n',
                       'Numeric expression missing\n', '*** WARNING: checksum mismatch\n'):
            with self.assertRaises(retained.ProbeInputError):
                retained.validate_trace(prefix + output, error, 'control', 'C:/bundle/localcat-spike.exe')

    def test_real_early_initialization_memoryerror_display_fallback_is_exact(self):
        profile = 'compile-filename-allocation'
        stderr = self.stderr(profile)
        result = retained.validate_trace(self.trace(profile), stderr, profile, 'C:/bundle/localcat-spike.exe')
        self.assertEqual(result['python_error_display'], 'EARLY_INITIALIZATION_MEMORYERROR_FALLBACK')
        for missing in ('object type name: MemoryError', 'object repr     : MemoryError()', 'lost sys.stderr'):
            with self.assertRaises(retained.ProbeInputError):
                retained.validate_trace(self.trace(profile), stderr.replace(missing, ''), profile,
                                        'C:/bundle/localcat-spike.exe')

    def test_traceback_must_match_the_actual_locked_profile(self):
        profile = 'body-allocation'
        stderr = self.stderr(profile)
        for changed in (
            stderr.replace('Traceback (most recent call last):\n', ''),
            stderr.replace('  File "<localcat-retained:importlib-external>", line 233, in <module>\n', ''),
            stderr.replace('line 233', 'line 1546'),
            stderr.replace('<localcat-retained:importlib-external>', 'C:/untrusted/external.py'),
            stderr.replace('object refcount : 3', 'object refcount : 0'),
        ):
            with self.assertRaises(retained.ProbeInputError):
                retained.validate_trace(self.trace(profile), changed, profile, 'C:/bundle/localcat-spike.exe')

    def test_complete_stderr_rejects_extra_conflicting_or_misplaced_exceptions(self):
        for profile in retained.PROFILES:
            output, stderr = self.trace(profile), self.stderr(profile)
            for extra in ('MemoryError\n', 'RuntimeError: unexpected secondary failure\n',
                          'Traceback (most recent call last):\n', 'unrecognized output\n'):
                for changed in (stderr + extra, extra + stderr,
                                stderr.replace('FROZEN_ENTRY.', extra + 'FROZEN_ENTRY.', 1)):
                    with self.subTest(profile=profile, changed=changed):
                        with self.assertRaises(retained.ProbeInputError):
                            retained.validate_trace(output, changed, profile, 'C:/bundle/localcat-spike.exe')

    def test_wrong_module_and_malformed_or_source_reopening_file_record_reject(self):
        profile = 'body-allocation'
        output, error = self.trace(profile), self.stderr(profile)
        for changed in (
            output.replace('W3_RF_PYTHON_BASE 00000001`80000000', 'W3_RF_PYTHON_BASE 00000001`90000000'),
            output + 'W3_RF_FILE unknown\n',
            output + 'W3_RF_FILE NtOpenFile root=0 path=' + retained.FILENAME + '\n',
        ):
            with self.assertRaises(retained.ProbeInputError):
                retained.validate_trace(changed, error, profile, 'C:/bundle/localcat-spike.exe')
        with self.assertRaises(retained.ProbeInputError):
            retained.validate_trace(output, error, profile, 'C:/bundle/localcat-spike.exe', 'C:/other/python314.dll')

    def test_pollution_remains_a_scoped_discovery_decoy_and_any_access_rejects(self):
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            facts = retained.prepare_pollution(cwd)
            self.assertIn('traceback.py', facts)
            self.assertIn('encodings/__init__.py', facts)
            self.assertTrue(any(name.endswith('.pyd') for name in facts))
            self.assertTrue(all((cwd / name).is_file() for name in facts))
            self.assertIn(b'RETAINED_FAILURE_DECOY_EXECUTED', (cwd / 'traceback.py').read_bytes())
        profile = 'body-allocation'
        for extra in ('W3_RF_FILE NtOpenFile root=0 path=\\??\\C:\\decoy\\traceback.py\n',
                      'ModLoad: 00000001`88000000 00000001`89000000 C:\\decoy\\encodings.cp314-win_amd64.pyd\n',
                      'RETAINED_FAILURE_DECOY_EXECUTED\n'):
            with self.assertRaises(retained.ProbeInputError):
                retained.validate_trace(self.trace(profile) + extra, self.stderr(profile), profile,
                                        'C:/bundle/localcat-spike.exe', polluted_cwd='C:/decoy')

    @unittest.skipUnless(os.name == 'nt', 'launch orchestration requires Windows')
    def test_external_anchors_and_post_run_dist_observer_script_drift(self):
        import pefile
        for drift in ('dist', 'observer', 'script', 'pefile', 'metadata'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'artifacts/windows').mkdir(parents=True)
                release = root / 'release.json'
                release.write_bytes(b'release')
                cdb = root / 'cdb.exe'
                cdb.write_bytes(b'cdb')
                parser_source = root / 'pefile.py'
                parser_source.write_bytes(b'locked parser source')
                metadata_state = ['locked metadata']
                candidate = {'candidate_input_digest': 'candidate',
                    'evidence_producer': {'pefile': {'source': {}, 'version': pefile.__version__,
                        'distribution_metadata': retained.byte_fact(b'locked metadata')}},
                    'runtime': {'cpython': {'dll': {}}}, 'entry_contract': {'python_c_api': []}}
                def simulate(command, cwd, environment, directory, name, commands, **kwargs):
                    output = self.trace('control').replace('C:\\bundle\\_internal\\python314.dll',
                                                         str(root / 'bundle/_internal/python314.dll'))
                    (directory / 'cdb.stdout').write_text(output, encoding='utf-8')
                    (directory / 'cdb.stderr').write_text(self.stderr('control'), encoding='utf-8')
                    commands.append({'exit_code': 0})
                    if drift == 'script':
                        (directory / 'retained.cdb').write_text('changed script', encoding='ascii')
                    elif drift == 'pefile':
                        parser_source.write_bytes(b'changed parser source')
                    elif drift == 'metadata':
                        metadata_state[0] = 'changed metadata'
                before = {'executable': {}}
                verify_results = [before, retained.ProbeInputError('dist drift')] if drift == 'dist' else [before, before]
                with (patch.object(retained, 'ROOT', root),
                      patch.object(retained, 'verify_release_binding', side_effect=verify_results) as verify,
                      patch.object(retained, 'load_candidate', return_value=candidate),
                      patch.object(retained, 'checked_bytes', return_value=b'pinned bytes'),
                      patch.object(pefile, '__file__', str(parser_source)),
                      patch.object(retained.importlib.metadata, 'distribution', return_value=SimpleNamespace(read_text=lambda _: metadata_state[0])),
                      patch.object(retained, 'verify_machine_sites', return_value=retained.TEST_ANCHORS),
                      patch.object(retained, 'observer_inventory', side_effect=[{'helper': 'before'}, {'helper': 'after' if drift == 'observer' else 'before'}]),
                      patch.object(retained, 'record_command', side_effect=simulate)):
                    with self.assertRaises(retained.ProbeInputError):
                        retained.run(SimpleNamespace(dist=root / 'bundle', release=release,
                            expected_release_sha256='release-sha', expected_commit='commit',
                            expected_candidate_digest='candidate', cdb=cdb, profile='control'))
                    self.assertEqual(verify.call_args_list[0].kwargs, {
                        'expected_release_sha256': 'release-sha',
                        'expected_repository_commit': 'commit', 'expected_candidate_input_digest': 'candidate'})
                    self.assertEqual(verify.call_count, 2)
                evidence, = (root / 'artifacts/windows').glob('retained-*/evidence.json')
                result = json.loads(evidence.read_bytes())
                self.assertEqual(result['status'], 'INCOMPLETE')
                self.assertFalse(result['post_observation_inputs_unchanged'])


if __name__ == '__main__':
    unittest.main()
