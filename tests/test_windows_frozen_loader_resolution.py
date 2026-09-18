"""Paired application loader observations must resolve the returned module."""
import unittest
from unittest.mock import patch
from tools import trace_windows_frozen_loader_resolution as loader

from tools.trace_windows_frozen_loader_resolution import (
    ResolutionError, parse_loader_events, render_commands, validate_profile_calls, validate_stderr,
)


SYSTEM = 'C:/Windows/System32'
DIST = 'E:/final/dist'


def trace(name='api-ms-win-core-synch-l1-2-0', host='C:/Windows/System32/KERNELBASE.dll'):
    return '\n'.join([
        'W3_LD_BEGIN', 'W3_LD_PE_ENTRY',
        f'W3_LD_CALL exe:a24d tid=12 sp=1000 flags=800 name={name}',
        'W3_LD_RETURN exe:a24d tid=12 sp=1000 module=70000000',
        'W3_LD_HOST_BEGIN',
        f'00000000`70000000 00000000`70040000 KERNELBASE {host}',
        f'W3_LD_PEB module=70000000 path={host}',
        'W3_LD_HOST_END',
        'W3_LD_POLICY_CALL flags=800', 'W3_LD_POLICY_RETURN value=1',
        'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.SPIKE_COMPLETED',
        'W3_LD_EXIT', 'Last event: 1.2: Exit process 1, code 0', '',
    ])


def acp_failure_trace():
    def call(name, site):
        return '\n'.join(trace(name).splitlines()[2:8]).replace('exe:a24d', 'exe:'+site)
    return '\n'.join([
        'W3_LD_BEGIN', 'W3_LD_PE_ENTRY',
        call('api-ms-win-core-synch-l1-2-0', 'a24d'),
        call('api-ms-win-core-fibers-l1-1-1', 'a24d'),
        call('api-ms-win-core-fibers-l1-1-2', '17061'),
        'W3_LD_ACP_RETURN value=fde9', 'W3_LD_ACP_INJECT value=4e4',
        'W3_LD_ACP_TABLE value=4e4',
        call('api-ms-win-core-localization-l1-2-1', '17061'),
        'W3_LD_POLICY_CALL flags=800', 'W3_LD_POLICY_INJECT_FALSE',
        'W3_LD_POLICY_RETURN value=0',
        call('api-ms-win-appmodel-runtime-l1-1-2', '17061'),
        'W3_LD_EXIT', 'Last event: 1.2: Exit process 1, code 1', '',
    ])


class LoaderResolutionTests(unittest.TestCase):
    def test_acp_profile_pairs_localization_before_policy_even_on_policy_failure(self):
        result = parse_loader_events(acp_failure_trace(), SYSTEM, DIST, 'acp-1252-policy-false')
        validate_profile_calls(result, 'acp-1252-policy-false')
        self.assertEqual(len(result['acp_stimulus']), 3)
        self.assertEqual(result['calls'][3]['stage'], 'before-policy')
        self.assertEqual(result['calls'][3]['host'], SYSTEM+'/KERNELBASE.dll')

    def test_acp_stimulus_missing_repeated_changed_or_late_is_rejected(self):
        sample = acp_failure_trace()
        mutations = [
            ('W3_LD_ACP_RETURN value=fde9', ''),
            ('W3_LD_ACP_RETURN value=fde9', 'W3_LD_ACP_RETURN value=0'),
            ('W3_LD_ACP_RETURN value=fde9', 'W3_LD_ACP_RETURN value=10000'),
            ('W3_LD_ACP_INJECT value=4e4', 'W3_LD_ACP_INJECT value=3a8'),
            ('W3_LD_ACP_TABLE value=4e4', 'W3_LD_ACP_TABLE value=fde9'),
            ('W3_LD_ACP_TABLE value=4e4', 'W3_LD_ACP_TABLE value=4e4\nW3_LD_ACP_REPEAT'),
            ('W3_LD_POLICY_RETURN value=0', 'W3_LD_POLICY_RETURN value=0\nW3_LD_ACP_TABLE value=4e4'),
            ('api-ms-win-core-localization-l1-2-1', 'unknown-localization.dll'),
        ]
        for old, new in mutations:
            with self.subTest(new=new), self.assertRaises(ResolutionError):
                parse_loader_events(sample.replace(old, new), SYSTEM, DIST, 'acp-1252-policy-false')
        with self.assertRaises(ResolutionError):
            parse_loader_events(sample, SYSTEM, DIST, 'policy-false')

    def test_acp_profile_cannot_omit_the_localization_call(self):
        result = parse_loader_events(acp_failure_trace(), SYSTEM, DIST, 'acp-1252-policy-false')
        del result['calls'][3]
        with self.assertRaises(ResolutionError):
            validate_profile_calls(result, 'acp-1252-policy-false')

    def test_process_exit_record_must_follow_terminal_exit_event(self):
        sample = acp_failure_trace()
        exit_record = 'Last event: 1.2: Exit process 1, code 1'
        mutations = [
            sample.replace(exit_record, '').replace('W3_LD_BEGIN', 'W3_LD_BEGIN\n'+exit_record),
            sample.replace(exit_record, ''),
            sample.replace(exit_record, exit_record+'\n'+exit_record),
            sample.replace(exit_record, 'intervening event\n'+exit_record),
        ]
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ResolutionError):
                parse_loader_events(changed, SYSTEM, DIST, 'acp-1252-policy-false')

    def test_acp_profile_injects_only_a_single_real_return_and_records_table(self):
        from tools.trace_windows_frozen_loader_resolution import render_commands
        script = render_commands({'entry':0x9100,'python_anchor':0x309b84}, 'acp-1252-normal')
        self.assertIn('0x1575b', script)
        self.assertIn('r eax=0x4e4', script)
        self.assertIn('W3_LD_ACP_TABLE', script)
        self.assertIn('W3_LD_ACP_REPEAT', script)

    def test_manifest_rebound_pe_still_reaches_machine_validation(self):
        new_sha='2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea'
        for sha,accepted in ((new_sha,True),('0'*64,False)):
            with self.subTest(sha=sha), patch.object(loader,'byte_fact',side_effect=lambda data:
                {'sha256':sha if data==b'exe' else loader.PINNED_PYTHON_SHA256}), \
                    patch('pefile.PE',side_effect=AssertionError('machine decoder reached')) as decoder:
                if accepted:
                    with self.assertRaisesRegex(AssertionError,'machine decoder reached'):
                        loader.verify_sites(b'exe',b'python')
                    decoder.assert_called_once()
                else:
                    with self.assertRaises(ResolutionError):
                        loader.verify_sites(b'exe',b'python')
                    decoder.assert_not_called()

    def parse(self, output):
        return parse_loader_events(output, SYSTEM, DIST)

    def test_returned_base_is_resolved_even_without_modload(self):
        result = self.parse(trace())
        self.assertEqual(result['calls'][0]['host'], 'C:/Windows/System32/KERNELBASE.dll')
        self.assertEqual(result['calls'][0]['stage'], 'before-policy')

    def test_foreign_host_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace(host='C:/cwd/KERNELBASE.dll'))

    def test_system32_subdirectory_not_system_host(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace(host=SYSTEM+'/shadow/KERNELBASE.dll'))

    def test_wrong_return_base_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('module=70000000', 'module=70010000'))

    def test_no_host_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('W3_LD_HOST_BEGIN', 'lost'))

    def test_ntdll_bare_debugger_name_requires_actual_peb_path(self):
        sample=trace().replace('KERNELBASE C:/Windows/System32/KERNELBASE.dll', 'ntdll ntdll.dll')
        sample=sample.replace('W3_LD_PEB module=70000000 path=C:/Windows/System32/KERNELBASE.dll',
                              'W3_LD_PEB module=70000000 path=C:/Windows/System32/ntdll.dll')
        self.assertEqual(self.parse(sample)['calls'][0]['host'],'C:/Windows/System32/ntdll.dll')

    def test_lmf_and_peb_disagreement_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('W3_LD_PEB module=70000000 path=C:/Windows/System32/KERNELBASE.dll',
                                      'W3_LD_PEB module=70000000 path=C:/Windows/System32/other.dll'))

    def test_direct_system_name_must_match_returned_basename(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace(name='kernel32',host='C:/Windows/System32/ntdll.dll'))

    def test_wrong_stack_or_thread_rejected(self):
        for field in ('tid=13 sp=1000', 'tid=12 sp=1008'):
            with self.subTest(field=field), self.assertRaises(ResolutionError):
                self.parse(trace().replace('RETURN exe:a24d tid=12 sp=1000', 'RETURN exe:a24d '+field))

    def test_unknown_callsite_or_name_rejected(self):
        for old,new in [('exe:a24d','exe:ffff'),('api-ms-win-core-synch-l1-2-0','SHELL32.dll')]:
            with self.subTest(new=new), self.assertRaises(ResolutionError):
                self.parse(trace().replace(old,new))

    def test_ansi_has_no_flags(self):
        commands=render_commands({'entry':0x9100,'python_anchor':0x309b84},'normal')
        line=next(line for line in commands.splitlines() if 'W3_LD_CALL python:18b86a' in line)
        self.assertIn('flags=none',line)
        self.assertNotIn('@r8',line)

    def test_negative_policy_never_masquerades_as_success(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('POLICY_RETURN value=1','POLICY_RETURN value=0'))

    def test_policy_failure_has_no_python_stage(self):
        sample=trace().replace('W3_LD_POLICY_RETURN value=1',
            'W3_LD_POLICY_INJECT_FALSE\nW3_LD_POLICY_RETURN value=0')
        sample=sample.replace('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n','')
        sample=sample.replace('code 0','code 1')
        result=parse_loader_events(sample,SYSTEM,DIST,'policy-false')
        self.assertEqual(result['expected_stderr_markers'],['FROZEN_ENTRY.DLL_POLICY_FAILED'])

    def test_ambiguous_host_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('W3_LD_HOST_END',
                '00000000`70000000 00000000`70050000 other C:/Windows/System32/other.dll\nW3_LD_HOST_END'))

    def test_missing_call_return_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('W3_LD_RETURN exe:a24d tid=12 sp=1000 module=70000000',''))

    def test_unknown_event_rejected(self):
        with self.assertRaises(ResolutionError):
            self.parse(trace().replace('W3_LD_EXIT','W3_LD_UNKNOWN\nW3_LD_EXIT'))

    def test_exit_loader_cannot_run_between_path_begin_and_completion(self):
        block='\n'.join(trace().splitlines()[2:8]).replace('exe:a24d','exe:17061').replace(
            'api-ms-win-core-synch-l1-2-0','api-ms-win-appmodel-runtime-l1-1-2')
        sample=trace().replace('FROZEN_ENTRY.SPIKE_COMPLETED',block+'\nFROZEN_ENTRY.SPIKE_COMPLETED')
        with self.assertRaises(ResolutionError):
            self.parse(sample)

    def test_partial_paired_trace_is_not_complete_profile(self):
        with self.assertRaises(ResolutionError):
            validate_profile_calls(self.parse(trace()),'normal')

    def test_stats_process_detach_is_after_crt_exit_policy(self):
        sites=[('exe',0xa24d,'api-ms-win-core-synch-l1-2-0'),
               ('exe',0xa24d,'api-ms-win-core-fibers-l1-1-1'),
               ('exe',0x17061,'api-ms-win-core-fibers-l1-1-2'),
               ('exe',0x6847,'vcruntime140.dll'),('exe',0x6847,'python314.dll'),
               ('python',0x18b86a,'kernelbase.dll'),('python',0x18b8a8,'ntdll.dll'),
               ('python',0x18b8dd,'kernel32.dll'),
               ('exe',0x17061,'api-ms-win-appmodel-runtime-l1-1-2'),('python',0x2b059f,'psapi.dll')]
        calls=[dict(owner=o,rva=r,name=n,module=1,host='observed',thread=1) for o,r,n in sites]
        validate_profile_calls({'calls':calls},'mimalloc-stats')
        calls[-1],calls[-2]=calls[-2],calls[-1]
        with self.assertRaises(ResolutionError):
            validate_profile_calls({'calls':calls},'mimalloc-stats')

    def test_control_stderr_rejects_extra_exception(self):
        valid='FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'
        validate_stderr(valid,'normal')
        with self.assertRaises(ResolutionError):
            validate_stderr(valid+'MemoryError\n','normal')

    def test_failure_stderr_requires_exact_diagnostic(self):
        validate_stderr('FROZEN_ENTRY.DLL_POLICY_FAILED\n','policy-false')
        with self.assertRaises(ResolutionError):
            validate_stderr('FROZEN_ENTRY.DLL_POLICY_FAILED\nother\n','policy-false')


if __name__=='__main__':
    unittest.main()
