"""Valid native bait is observed, never authorized as product runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import tools.verify_windows_frozen_loader_canary as canary

from tools.verify_windows_frozen_loader_canary import (
    BAIT_NAMES, CASES, MARKER, CanaryError, build_commands, case_environment,
    expected_inventory, module_events, reject_canary, validate_positive,
    validate_appdir, CLASSIFICATION, render_case_commands,
)


class CanaryTests(unittest.TestCase):
    def test_fixed_matrix_and_names(self):
        self.assertEqual(len(CASES), 30)
        self.assertEqual(len(BAIT_NAMES), 13)
        self.assertIn('python314.dll', BAIT_NAMES)
        self.assertIn('api-ms-win-appmodel-runtime-l1-1-2.dll', BAIT_NAMES)
        self.assertIn('api-ms-win-core-localization-l1-2-1.dll', BAIT_NAMES)
        self.assertNotIn('shell32.dll', BAIT_NAMES)
        self.assertIn('NOT_FULL_W3', CLASSIFICATION)
        original = CASES[:15]
        self.assertEqual(CASES[15:], tuple(('acp-1252-'+name, location, 'acp-1252-'+profile)
                                         for name, location, profile in original))
        self.assertEqual(len({name for name, _, _ in CASES}), 30)

    def test_profile_normalization_is_exact(self):
        for profile in ('normal', 'mimalloc-stats', 'policy-false'):
            self.assertEqual(canary.base_profile(profile), profile)
            self.assertEqual(canary.base_profile('acp-1252-'+profile), profile)
        for profile in ('', 'acp-1252-', 'acp-1252-acp-1252-normal', 'acp-1251-normal'):
            with self.subTest(profile=profile), self.assertRaises(CanaryError):
                canary.base_profile(profile)

    def test_build_has_explicit_no_crt_entry_and_locked_library(self):
        commands = build_commands(Path('C:/cl.exe'), Path('C:/link.exe'),
                                  Path('C:/sdk/kernel32.lib'), Path('C:/fixture.c'), Path('C:/build'))
        self.assertEqual(len(commands), 4)
        self.assertIn('/DLOCALCAT_CANARY_DLL', commands[0])
        self.assertIn('/ENTRY:DllMain', commands[1])
        self.assertIn('/ENTRY:LocalCatCanaryControl', commands[3])
        for index in (1, 3):
            self.assertIn('/NODEFAULTLIB', commands[index])
            self.assertEqual(commands[index][-1], 'C:\\sdk\\kernel32.lib' if os.name == 'nt' else 'C:/sdk/kernel32.lib')

    def test_path_poison_survives_sanitization_and_is_case_local(self):
        original = {'SystemRoot': 'C:/Windows', 'PATH': 'C:/ambient',
                    'PYTHONPATH': 'C:/ambient', 'MIMALLOC_SHOW_STATS': '1', 'SECRET': 'not-for-evidence'}
        env = case_environment(original, 'path', 'normal', Path('C:/bait'), Path('C:/temp'))
        self.assertEqual(env['PATH'].split(os.pathsep)[0], str(Path('C:/bait')))
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('MIMALLOC_SHOW_STATS', env)
        self.assertNotIn('SECRET', env)
        self.assertEqual(env.get('LOCALAPPDATA'),str(Path('C:/temp')))
        self.assertEqual(env.get('APPDATA'),str(Path('C:/temp')))
        self.assertEqual(original['PATH'], 'C:/ambient')
        stats = case_environment(original, 'cwd', 'mimalloc-stats', Path('C:/bait'), Path('C:/temp'))
        self.assertEqual(stats['MIMALLOC_SHOW_STATS'], '1')
        self.assertNotIn('bait', stats['PATH'])

    def test_acp_stats_environment_preserves_only_the_requested_option(self):
        original = {'SystemRoot': 'C:/Windows', 'MIMALLOC_SHOW_STATS': 'ambient'}
        for profile in ('normal', 'mimalloc-stats', 'policy-false'):
            env = case_environment(original, 'path', 'acp-1252-'+profile, Path('C:/bait'), Path('C:/temp'))
            self.assertEqual(env['PATH'].split(os.pathsep)[0], str(Path('C:/bait')))
            self.assertEqual(env.get('MIMALLOC_SHOW_STATS'), '1' if profile == 'mimalloc-stats' else None)

    def test_only_appdir_adds_exact_bait_files(self):
        before = {'files': {'localcat-spike.exe': {'sha256': 'original'}}, 'directories': ['_internal']}
        fact = {'bytes': 2, 'sha256': 'bait'}
        expected = expected_inventory(before, 'appdir', fact)
        self.assertEqual(set(expected['files']) - set(before['files']), set(BAIT_NAMES))
        self.assertEqual(expected['files']['localcat-spike.exe'], before['files']['localcat-spike.exe'])
        self.assertEqual(expected_inventory(before, 'path', fact), before)
        self.assertEqual(len(before['files']), 1)

    def test_redirection_layout_is_explicit_and_never_overwrites_internal(self):
        token = b'canary'
        local = canary.redirection_layout('local-dir', 'app.exe', ('neutral.dll',), token)
        self.assertEqual(local, {'app.exe.local/neutral.dll': token})
        local = canary.redirection_layout('local-file', 'app.exe', ('neutral.dll',), token)
        self.assertEqual(local, {'app.exe.local': b'', 'neutral.dll': token})
        sxs = canary.redirection_layout('sxs-external', 'app.exe', ('neutral.dll',), token)
        self.assertEqual(set(sxs), {'app.exe.manifest', 'LocalCAT.Canary/LocalCAT.Canary.manifest',
                                    'LocalCAT.Canary/neutral.dll'})
        self.assertIn(b'dependentAssembly', sxs['app.exe.manifest'])
        self.assertIn(b'<file name="neutral.dll"', sxs['LocalCAT.Canary/LocalCAT.Canary.manifest'])
        for value in ('../escape.dll', '_internal/python314.dll', 'a"b.dll'):
            with self.subTest(value=value), self.assertRaises(CanaryError):
                canary.redirection_layout('sxs-external', 'app.exe', (value,), token)

    def test_redirection_case_inventory_records_directories_and_empty_dotlocal(self):
        before = {'files': {'localcat-spike.exe': {'sha256': 'original'}}, 'directories': ['_internal']}
        layout = canary.redirection_layout('local-dir', 'localcat-spike.exe', ('neutral.dll',), b'x')
        expected = canary.inventory_with_additions(before, layout)
        self.assertEqual(expected['directories'], ['_internal', 'localcat-spike.exe.local'])
        self.assertEqual(expected['files']['localcat-spike.exe.local/neutral.dll'], canary.byte_fact(b'x'))
        with self.assertRaises(CanaryError):
            canary.inventory_with_additions(before, {'localcat-spike.exe': b'overwritten'})

    def test_every_redirection_profile_has_native_early_rejection_probe(self):
        for location in ('appdir', 'local-dir', 'local-file', 'sxs-external'):
            for profile in ('normal', 'acp-1252-normal'):
                script = render_case_commands({'entry':0x9100,'python_anchor':0x309b84,'diagnostic_call':0x7e78}, location, profile)
                self.assertIn('W3_LD_DIAGNOSTIC', script)
                if profile.startswith('acp-1252-'):
                    self.assertIn('W3_LD_ACP_RETURN', script)
                    self.assertIn('W3_LD_ACP_TABLE', script)

    def test_modload_poison_fails_even_without_dllmain(self):
        raw = 'ModLoad: 00000000`10000000 00000000`10003000   C:\\bait\\python314.dll\n'
        self.assertEqual(module_events(raw)[0]['base'], 0x10000000)
        with self.assertRaises(CanaryError):
            reject_canary(raw, '', ['C:/bait/python314.dll'])

    def test_marker_alone_fails(self):
        with self.assertRaises(CanaryError):
            reject_canary(MARKER+'\n', '', [])
        with self.assertRaises(CanaryError):
            reject_canary('', MARKER, [])

    def test_positive_requires_real_marker_path_entry_and_exit(self):
        raw = ('W3_CANARY_CONTROL_BEGIN\nW3_CANARY_CONTROL_ENTRY\n'
               'ModLoad: 10000000 10003000 C:\\control\\localcat-loader-canary.dll\n'
               + MARKER + '\nW3_CANARY_CONTROL_EXIT\nLast event: 1.2: Exit process 1, code 0\n')
        validate_positive(raw, '', 'C:/control/localcat-loader-canary.dll')
        for sample in (raw.replace(MARKER, ''), raw.replace('C:\\control', 'C:\\wrong'),
                       raw.replace('code 0', 'code 1'), raw.replace(MARKER, MARKER+'\n'+MARKER),
                       raw.replace('W3_CANARY_CONTROL_ENTRY', '')):
            with self.subTest(sample=sample), self.assertRaises(CanaryError):
                validate_positive(sample, '', 'C:/control/localcat-loader-canary.dll')
        with self.assertRaises(CanaryError):
            validate_positive(raw, 'unexpected', 'C:/control/localcat-loader-canary.dll')

    def test_appdir_failure_has_real_policy_and_only_crt_calls(self):
        sample = appdir_trace()
        result = validate_appdir(sample, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32')
        self.assertEqual(len(result['calls']), 4)
        for old, new in [('value=1', 'value=0'), ('tid=12 sp=1000 module=', 'tid=13 sp=1000 module='),
                         ('code 1', 'code 0'), ('C:/Windows/System32/KERNELBASE.dll', 'C:/bait/KERNELBASE.dll')]:
            with self.subTest(old=old), self.assertRaises(CanaryError):
                validate_appdir(sample.replace(old, new), 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32')
        with self.assertRaises(CanaryError):
            validate_appdir(sample, 'FROZEN_ENTRY.INVENTORY_EXTRA\nextra\n', 'C:/Windows/System32')

    def test_appdir_diagnostic_is_observed_at_real_write_call(self):
        commands=render_case_commands({'entry':0x9100,'python_anchor':0x309b84,'diagnostic_call':0x7e78}, 'appdir', 'normal')
        self.assertIn('bp @$t0+0x7e78', commands)
        self.assertIn('W3_LD_DIAGNOSTIC', commands)
        for old,new in [('owner=7e78','owner=7e79'),('bytes=1c','bytes=1b')]:
            with self.assertRaises(CanaryError):
                validate_appdir(appdir_trace().replace(old,new),'FROZEN_ENTRY.INVENTORY_EXTRA\n','C:/Windows/System32')

    def test_appdir_missing_pair_and_early_exit_probe_rejected(self):
        sample = appdir_trace()
        start=sample.index('W3_LD_CALL'); end=sample.index('W3_LD_HOST_END')+len('W3_LD_HOST_END\n')
        diagnostic='W3_LD_DIAGNOSTIC owner=7e78 bytes=1c text=FROZEN_ENTRY.INVENTORY_EXTRA\n'
        for bad in (sample[:start]+sample[end:], sample.replace(diagnostic,'').replace(
                'W3_LD_EXIT', diagnostic+'W3_LD_EXIT')):
            with self.assertRaises(CanaryError):
                validate_appdir(bad, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32')

    def test_acp_appdir_retains_three_stimulus_events_and_one_localization_pair(self):
        result = validate_appdir(appdir_trace(acp=True), 'FROZEN_ENTRY.INVENTORY_EXTRA\n',
                                 'C:/Windows/System32', 'acp-1252-normal')
        self.assertEqual(len(result['calls']), 5)
        self.assertEqual(result['calls'][3]['name'], 'api-ms-win-core-localization-l1-2-1')
        self.assertEqual(result['acp_stimulus'], ['W3_LD_ACP_RETURN value=fde9',
                         'W3_LD_ACP_INJECT value=4e4', 'W3_LD_ACP_TABLE value=4e4'])
        self.assertEqual(result['debuggee_exit_code'], 1)

    def test_appdir_exit_record_must_follow_exit_marker_once_without_intervening_output(self):
        for acp in (False, True):
            sample = appdir_trace(acp=acp)
            profile = 'acp-1252-normal' if acp else 'normal'
            terminal = 'Last event: 1.2: Exit process 1, code 1\n'
            mutations = [terminal+sample.replace(terminal, ''), sample.replace(terminal, ''),
                         sample+terminal, sample.replace('W3_LD_EXIT\n', 'W3_LD_EXIT\nintervening output\n'),
                         sample+'Last event: malformed\n']
            for index, raw in enumerate(mutations):
                with self.subTest(acp=acp, index=index), self.assertRaises(CanaryError):
                    validate_appdir(raw, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32', profile)

    def test_positive_exit_record_must_follow_its_actual_exit_marker(self):
        terminal = 'Last event: 1.2: Exit process 1, code 0\n'
        raw = ('W3_CANARY_CONTROL_BEGIN\nW3_CANARY_CONTROL_ENTRY\n'
               'ModLoad: 10000000 10003000 C:\\control\\localcat-loader-canary.dll\n'
               + MARKER + '\nW3_CANARY_CONTROL_EXIT\n'+terminal)
        for sample in (terminal+raw.replace(terminal, ''), raw.replace(terminal, ''), raw+terminal,
                       raw.replace('W3_CANARY_CONTROL_EXIT\n', 'W3_CANARY_CONTROL_EXIT\nintervening output\n')):
            with self.subTest(sample=sample), self.assertRaises(CanaryError):
                validate_positive(sample, '', 'C:/control/localcat-loader-canary.dll')

    def test_positive_canary_module_must_load_between_entry_and_dllmain(self):
        loaded = 'ModLoad: 10000000 10003000 C:\\control\\localcat-loader-canary.dll\n'
        raw = ('W3_CANARY_CONTROL_BEGIN\nW3_CANARY_CONTROL_ENTRY\n' + loaded + MARKER
               + '\nW3_CANARY_CONTROL_EXIT\nLast event: 1.2: Exit process 1, code 0\n')
        without = raw.replace(loaded, '')
        for sample in (loaded+without,
                       without.replace(MARKER+'\n', MARKER+'\n'+loaded),
                       without.replace('W3_CANARY_CONTROL_EXIT\n', 'W3_CANARY_CONTROL_EXIT\n'+loaded),
                       without+loaded):
            with self.subTest(sample=sample), self.assertRaises(CanaryError):
                validate_positive(sample, '', 'C:/control/localcat-loader-canary.dll')

    def test_acp_appdir_rejects_missing_repeated_moved_or_forged_stimulus_and_pair(self):
        sample = appdir_trace(acp=True)
        stimulus = 'W3_LD_ACP_RETURN value=fde9\nW3_LD_ACP_INJECT value=4e4\nW3_LD_ACP_TABLE value=4e4\n'
        start = sample.index('W3_LD_CALL exe:17061 tid=12 sp=1000 flags=800 name=api-ms-win-core-localization')
        end = sample.index('W3_LD_HOST_END\n', start) + len('W3_LD_HOST_END\n')
        pair = sample[start:end]
        bad = [sample.replace(line+'\n', '', 1) for line in stimulus.splitlines()]
        bad += [sample.replace(stimulus, stimulus+stimulus), sample.replace(pair, ''),
                sample.replace(pair, pair+pair), sample.replace(stimulus, '').replace('W3_LD_POLICY_CALL', stimulus+'W3_LD_POLICY_CALL'),
                sample.replace(stimulus, '').replace('W3_LD_PE_ENTRY\n', 'W3_LD_PE_ENTRY\n'+stimulus),
                sample.replace('value=fde9', 'value=0'), sample.replace('value=fde9', 'value=10000'),
                sample.replace('W3_LD_ACP_TABLE value=4e4', 'W3_LD_ACP_TABLE value=fde9'),
                sample.replace('W3_LD_ACP_INJECT value=4e4', 'W3_LD_ACP_INJECT value=3a8'),
                sample.replace(pair, pair.replace('RETURN exe:17061 tid=12', 'RETURN exe:17061 tid=13')),
                sample.replace(pair, pair.replace('C:/Windows/System32', 'C:/bait'))]
        for index, raw in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(CanaryError):
                validate_appdir(raw, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32', 'acp-1252-normal')
        with self.assertRaises(CanaryError):
            validate_appdir(sample, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32')
        for profile in ('normal', 'policy-false', 'acp-1252-policy-false', 'acp-1252-mimalloc-stats'):
            with self.subTest(profile=profile), self.assertRaises(CanaryError):
                validate_appdir(sample, 'FROZEN_ENTRY.INVENTORY_EXTRA\n', 'C:/Windows/System32', profile)

    def test_vcvars_setup_is_bounded_recorded_and_does_not_inherit_secrets(self):
        with tempfile.TemporaryDirectory() as work:
            directory=Path(work)
            args=SimpleNamespace(vcvarsall=Path('C:/VS/vcvarsall.bat'),timeout=7)
            def recorder(command,cwd,env,out,name,records,*,timeout):
                self.assertIsInstance(command,str)  # cmd quoting must not pass literal backslash-quotes.
                self.assertNotIn('\\"',command)
                self.assertEqual(timeout,7)
                self.assertNotIn('SECRET',env)
                self.assertEqual(name,'vcvars')
                (out/'vcvars.stdout').write_text('VCToolsInstallDir=C:/VS/MSVC\nWindowsSdkDir=C:/SDK\n')
                records.append({'command':command,'exit_code':0})
            with mock.patch.dict(os.environ,{'SystemRoot':'C:/Windows','SECRET':'not-for-child'},clear=True), \
                 mock.patch.object(canary,'record_command',side_effect=recorder), \
                 mock.patch.object(canary,'finalize_compile_environment',side_effect=lambda env,*a:(env,{})):
                evidence={'commands':[]}
                env,_=canary.setup_environment(args,'14.44.35207','10.0.26100.0',directory,evidence)
            self.assertEqual(len(evidence['commands']),1)
            self.assertNotIn('SECRET',env)

    def test_cli_help_from_unrelated_cwd(self):
        tool=Path(__file__).resolve().parents[1]/'tools/verify_windows_frozen_loader_canary.py'
        with tempfile.TemporaryDirectory() as cwd:
            result=subprocess.run([sys.executable, '-B', str(tool), '--help'], cwd=cwd,
                                  capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--expected-release-sha256', result.stdout)


def appdir_trace(acp=False):
    lines=['W3_LD_BEGIN','W3_LD_PE_ENTRY']
    calls=[('a24d','api-ms-win-core-synch-l1-2-0'),
           ('a24d','api-ms-win-core-fibers-l1-1-1'),('17061','api-ms-win-core-fibers-l1-1-2')]
    if acp:
        calls += [('17061','api-ms-win-core-localization-l1-2-1')]
    calls += [('17061','api-ms-win-appmodel-runtime-l1-1-2')]
    for index,(rva,name) in enumerate(calls):
        if acp and index==3:
            lines += ['W3_LD_ACP_RETURN value=fde9', 'W3_LD_ACP_INJECT value=4e4', 'W3_LD_ACP_TABLE value=4e4']
        if index==len(calls)-1:
            lines+=['W3_LD_POLICY_CALL flags=800','W3_LD_POLICY_RETURN value=1',
                    'W3_LD_DIAGNOSTIC owner=7e78 bytes=1c text=FROZEN_ENTRY.INVENTORY_EXTRA']
        lines += [f'W3_LD_CALL exe:{rva} tid=12 sp=1000 flags=800 name={name}',
            f'W3_LD_RETURN exe:{rva} tid=12 sp=1000 module=70000000','W3_LD_HOST_BEGIN',
            '70000000 70010000 KERNELBASE C:/Windows/System32/KERNELBASE.dll',
            'W3_LD_PEB module=70000000 path=C:/Windows/System32/KERNELBASE.dll','W3_LD_HOST_END']
    return '\n'.join(lines+['W3_LD_EXIT','Last event: 1.2: Exit process 1, code 1',''])


if __name__=='__main__':
    unittest.main()
