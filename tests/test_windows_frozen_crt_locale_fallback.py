from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import trace_windows_frozen_crt_locale_fallback as locale


EXE = 'C:/bundle/localcat-spike.exe'
SYSTEM32 = 'C:/Windows/System32'
STDERR = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'


def trace(profile):
    events = ['W3_CL_BEGIN', 'W3_CL_ENTRY tid=1',
              'W3_CL_ACP_RETURN tid=1 sp=100 original=fde9', 'W3_CL_ACP_INJECT value=4e4',
              'W3_CL_TABLE value=4e4']
    module = 0x180000000
    pointer = module + 0x1234
    def host():
        events.extend(['W3_CL_HOST_BEGIN',
            '0000000180000000 0000000180100000 kernelbase C:/Windows/System32/kernelbase.dll',
            'W3_CL_PEB module=180000000 path=C:/Windows/System32/kernelbase.dll', 'W3_CL_HOST_END'])
    def load(name, site=0x17061, error=None):
        flags = 0x800 if site == 0x17061 else 0
        events.append(f'W3_CL_LOAD_CALL site={site:x} tid=1 sp=200 flags={flags:x} name={name}')
        events.append(f'W3_CL_LOAD_RETURN site={site:x} tid=1 sp=200 original={module:x}')
        host()
        if error is not None:
            events.extend([f'W3_CL_LOAD_INJECT site={site:x} tid=1 sp=200 value=0',
                'W3_CL_ERROR_CALL tid=1 sp=200', 'W3_CL_ERROR_RETURN tid=1 sp=200 original=0',
                f'W3_CL_ERROR_INJECT value={error:x}'])
    def gpa(name, missing):
        events.extend([f'W3_CL_GPA_CALL tid=1 sp=200 module={module:x} name={name}',
                       f'W3_CL_GPA_RETURN tid=1 sp=200 original={pointer:x}'])
        host()
        if missing:
            events.append(f'W3_CL_GPA_INJECT name={name} value=0')
    for index in range(1, 5):
        flag = 0x100 if index <= 2 else 0x200
        events.append(f'W3_CL_MAP_BEGIN id={index:x} tid=1 sp=300 flags={flag:x} locale=0')
        if index == 1:
            error = 126 if profile == 'module-failure' else 87 if profile == 'flags0' else None
            load('api-ms-win-core-localization-l1-2-1', error=error)
            if error is not None:
                load('kernel32', error=87 if profile == 'flags0' else None)
                if profile == 'flags0':
                    load('kernel32', site=0x170b5)
            gpa('LCMapStringEx', profile in ('lcmap-missing', 'both-missing'))
        if profile in ('lcmap-missing', 'both-missing'):
            events.append('W3_CL_LCID_CALL tid=1 sp=280 locale=0')
            if index == 1:
                gpa('LocaleNameToLCID', profile == 'both-missing')
            if profile == 'both-missing':
                events.extend(['W3_CL_DOWNLEVEL_CALL tid=1 sp=250 locale=0',
                               'W3_CL_DOWNLEVEL_RETURN tid=1 sp=250 value=0'])
            else:
                events.extend([f'W3_CL_LCID_DYNAMIC_CALL tid=1 sp=250 pointer={pointer:x}',
                               'W3_CL_LCID_DYNAMIC_RETURN tid=1 sp=250 value=0'])
            events.extend(['W3_CL_LCID_RETURN tid=1 sp=280 value=0',
                f'W3_CL_STATIC_CALL tid=1 sp=280 lcid=0 flags={flag:x}',
                'W3_CL_STATIC_RETURN tid=1 sp=280 value=100'])
        else:
            events.extend([f'W3_CL_DYNAMIC_CALL tid=1 sp=280 pointer={pointer:x}',
                           'W3_CL_DYNAMIC_RETURN tid=1 sp=280 value=100'])
        events.append(f'W3_CL_MAP_END id={index:x} tid=1 sp=300 value=100')
    events.extend(['W3_CL_E1 maps=4', 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
                   'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_CL_EXIT',
                   'Last event: 1.1: Exit process 0:1, code 0'])
    return '\n'.join(events)+'\n'


class LocaleFallbackTests(unittest.TestCase):
    def validate(self, profile, output=None, stderr=STDERR):
        return locale.validate_trace(trace(profile) if output is None else output, stderr, profile,
            executable_path=EXE, system32=SYSTEM32)

    def test_exact_profiles_and_real_return_stimulus_classification(self):
        self.assertEqual(locale.PROFILES, ('acp-control', 'module-failure', 'flags0', 'lcmap-missing', 'both-missing'))
        for profile in locale.PROFILES:
            with self.subTest(profile=profile):
                facts = self.validate(profile)
                self.assertEqual(facts['debuggee_exit_code'], 0)
                self.assertFalse(facts['os_failure_observed'])
                self.assertEqual(facts['map_calls'], 4)

    def test_missing_or_duplicate_events_fail_closed(self):
        for profile in locale.PROFILES:
            output = trace(profile)
            lines = output.splitlines(keepends=True)
            for index, line in enumerate(lines):
                if not line.startswith(('W3_CL_', 'FROZEN_ENTRY.')):
                    continue
                for changed in (lines[:index]+lines[index+1:], lines[:index]+[line]+lines[index:]):
                    with self.subTest(profile=profile, index=index), self.assertRaises(locale.LocaleTraceError):
                        self.validate(profile, ''.join(changed))

    def test_wrong_phase_return_pair_flags_error_host_symbol_or_downlevel_is_rejected(self):
        for profile, old, new in (
            ('flags0', 'name=kernel32', 'name=unreviewed.dll'),
            ('flags0', 'flags=0 name=kernel32', 'flags=0 name=api-ms-win-core-localization-l1-2-1'),
            ('flags0', 'W3_CL_ERROR_INJECT value=57', 'W3_CL_ERROR_INJECT value=7e'),
            ('module-failure', 'W3_CL_ERROR_INJECT value=7e', 'W3_CL_ERROR_INJECT value=57'),
            ('acp-control', 'sp=200 original=', 'sp=201 original='),
            ('acp-control', 'original=180000000', 'original=0'),
            ('acp-control', 'C:/Windows/System32/kernelbase.dll', 'C:/bundle/kernelbase.dll'),
            ('acp-control', 'module=180000000 path=', 'module=180000001 path='),
            ('acp-control', 'pointer=180001234', 'pointer=190001234'),
            ('acp-control', 'W3_CL_MAP_END id=1', 'W3_CL_MAP_END id=2'),
            ('both-missing', 'W3_CL_DOWNLEVEL_CALL', 'W3_CL_LCID_DYNAMIC_CALL'),
            ('lcmap-missing', 'name=LocaleNameToLCID', 'name=LCMapStringEx'),
            ('both-missing', 'W3_CL_STATIC_CALL tid=1', 'W3_CL_STATIC_CALL tid=2'),
            ('acp-control', 'W3_CL_E1 maps=4', 'W3_CL_E1 maps=3'),
            ('acp-control', 'code 0', 'code 1'),
        ):
            with self.subTest(profile=profile, old=old), self.assertRaises(locale.LocaleTraceError):
                self.validate(profile, trace(profile).replace(old, new))

    def test_complete_stderr_and_debugger_certainty(self):
        for changed in ('', STDERR*2, STDERR+'MemoryError\n', STDERR+'FROZEN_ENTRY.UNKNOWN\n'):
            with self.subTest(stderr=changed), self.assertRaises(locale.LocaleTraceError):
                self.validate('acp-control', stderr=changed)
        for extra in ('W3_CL_UNKNOWN', 'W3_CL_GUARD_REJECT', 'Memory access error', 'WARNING: unknown',
                      'Failed to set breakpoint', "Couldn't insert breakpoint", 'Cannot continue execution'):
            with self.subTest(extra=extra), self.assertRaises(locale.LocaleTraceError):
                self.validate('acp-control', trace('acp-control').replace('W3_CL_E1', extra+'\nW3_CL_E1'))
        warning = '*** WARNING: Unable to verify timestamp for '+EXE.replace('/', '\\')+'\n'
        self.assertEqual(len(self.validate('acp-control', warning+trace('acp-control'))['known_diagnostics']), 1)
        with self.assertRaises(locale.LocaleTraceError):
            self.validate('acp-control', warning*2+trace('acp-control'))

    def test_exit_must_be_the_unique_terminal_event_after_exit_marker(self):
        original = trace('acp-control')
        event = 'Last event: 1.1: Exit process 0:1, code 0\n'
        for changed in (
            event+original.replace(event, ''),
            original.replace(event, '').replace('W3_CL_EXIT\n', event+'W3_CL_EXIT\n'),
            original+event,
            original.replace('W3_CL_EXIT\n', 'W3_CL_EXIT\ninterposed event\n'),
        ):
            with self.subTest(output=changed[-150:]), self.assertRaises(locale.LocaleTraceError):
                self.validate('acp-control', changed)

    def test_each_casing_operation_has_at_most_one_size_and_one_data_call(self):
        original = trace('acp-control')
        changed = original.replace('W3_CL_MAP_BEGIN id=3 tid=1 sp=300 flags=200',
                                   'W3_CL_MAP_BEGIN id=3 tid=1 sp=300 flags=100')
        with self.assertRaises(locale.LocaleTraceError):
            self.validate('acp-control', changed)
        changed = original.replace('W3_CL_DYNAMIC_RETURN tid=1 sp=280 value=100',
                                   'W3_CL_DYNAMIC_RETURN tid=1 sp=280 value=0', 1).replace(
                                   'W3_CL_MAP_END id=1 tid=1 sp=300 value=100',
                                   'W3_CL_MAP_END id=1 tid=1 sp=300 value=0')
        with self.assertRaises(locale.LocaleTraceError):
            self.validate('acp-control', changed)

    def test_commands_never_skip_real_api_or_change_memory_or_system_configuration(self):
        for profile in locale.PROFILES:
            script = locale.render_commands(profile)
            self.assertNotIn('r rip=', script)
            self.assertNotIn('.call', script)
            self.assertNotRegex(script, r'(?:^|[;{])\s*e[bdqw]\s')
            self.assertIn('0x17067', script)
            self.assertIn('0x17079', script)
            self.assertIn('0x17198', script)
            self.assertIn('W3_CL_GUARD_REJECT', script)
            self.assertLess(max(map(len, script.splitlines())), 4096)

    def test_machine_guard_rejects_wrong_bytes_without_interpretation(self):
        with self.assertRaises(locale.LocaleTraceError):
            locale.verify_sites(b'not PE')

    @unittest.skipUnless(os.name == 'nt', 'Windows observer orchestration')
    def test_external_anchors_and_all_post_run_inputs_must_be_unchanged(self):
        import pefile
        for drift in (None, 'dist', 'release', 'candidate', 'helper', 'observer', 'script', 'pefile', 'metadata'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); (root/'artifacts/windows').mkdir(parents=True)
                lock = root/'packaging/windows/frozen-entry/candidate-input.lock.json'
                lock.parent.mkdir(parents=True); lock.write_bytes(b'candidate')
                release = root/'release.json'; release.write_bytes(b'release')
                parser = root/'pefile.py'; parser.write_bytes(b'parser')
                source = root/'observer.py'; source.write_bytes(b'observer')
                metadata = ['metadata']
                candidate = {'candidate_input_digest':'candidate', 'evidence_producer':{'pefile':{
                    'source':{}, 'version':pefile.__version__, 'distribution_metadata':locale.byte_fact(b'metadata')}}}
                def simulate(command, cwd, environment, directory, name, records, **kwargs):
                    (directory/'cdb.stdout').write_text(trace('acp-control'), encoding='utf8')
                    (directory/'cdb.stderr').write_text(STDERR, encoding='utf8')
                    records.append({'exit_code':0})
                    changed = {'release':release,'candidate':lock,'pefile':parser,'observer':source,
                               'script':directory/'locale.cdb'}.get(drift)
                    if changed is not None: changed.write_bytes(b'changed')
                    if drift == 'metadata': metadata[0] = 'changed'
                checks = [{'executable':{}}, locale.LocaleTraceError('dist changed')] if drift == 'dist' else [{'executable':{}}]*2
                with (patch.object(locale, 'ROOT', root), patch.object(locale, '__file__', str(source)),
                      patch.object(locale, 'verify_release_binding', side_effect=checks) as verify,
                      patch.object(locale, 'load_candidate', return_value=candidate),
                      patch.object(locale, 'checked_bytes', return_value=b'pinned'),
                      patch.object(locale, 'verify_sites', return_value={}),
                      patch.object(pefile, '__file__', str(parser)),
                      patch.object(locale.importlib.metadata, 'distribution', return_value=SimpleNamespace(read_text=lambda _:metadata[0])),
                      patch.object(locale, 'observer_inventory', side_effect=[{'helper':'before'}, {'helper':'changed' if drift=='helper' else 'before'}]),
                      patch.object(locale, 'record_command', side_effect=simulate)):
                    args = SimpleNamespace(dist=root/'bundle', release=release, cdb=root/'cdb.exe', profile='acp-control',
                        expected_release_sha256='external', expected_commit='commit', expected_candidate_digest='candidate',
                        system32=SYSTEM32)
                    if drift is None:
                        _, evidence = locale.run(args)
                        self.assertEqual(evidence['status'], 'OBSERVED_CONTROLLED_LOCALE_FALLBACK')
                    else:
                        with self.assertRaises(locale.LocaleTraceError): locale.run(args)
                    self.assertEqual(verify.call_count, 2)
                    self.assertEqual(verify.call_args_list[0].kwargs, {'expected_release_sha256':'external',
                        'expected_repository_commit':'commit','expected_candidate_input_digest':'candidate'})
                path, = (root/'artifacts/windows').glob('crt-locale-*/evidence.json')
                self.assertEqual(json.loads(path.read_bytes())['post_observation_inputs_unchanged'], drift is None)


if __name__ == '__main__':
    unittest.main()
