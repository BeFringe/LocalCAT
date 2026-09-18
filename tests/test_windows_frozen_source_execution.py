import unittest

from tools.trace_windows_frozen_source_execution import (
    SourceTraceError, render_source_commands, validate_source_trace,
)


class SourceExecutionTests(unittest.TestCase):
    def fixture(self):
        entries = [{'id':'bootstrap','bytes':9,'sha256':'unused'},
                   {'id':'critical-source','bytes':9,'sha256':'unused'}]
        content = [b'value=42\n',b'value=43\n']
        captures = {e['id']:data+b'\0' for e,data in zip(entries,content)}
        events = ['W3_SOURCE_BEGIN','FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN']
        for i in range(2):
            events += [f'W3_SOURCE_COMPILE {i} input=1000 start=101',
                       f'W3_SOURCE_RETURN id={i} code={2000+i:x}',
                       f'W3_SOURCE_EXECUTE {i} code={2000+i:x}']
        events += ['FROZEN_ENTRY.SPIKE_COMPLETED','W3_SOURCE_EXIT',
                   'Last event: 1.2: Exit process 0:1, code 0']
        stderr = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'
        return '\n'.join(events),stderr,entries,content,captures

    def test_exact_buffers_returned_objects_and_execution_order(self):
        args = self.fixture()
        result = validate_source_trace(*args, executable_path='C:/bundle/localcat-spike.exe')
        self.assertEqual(result['executed_entry_ids'],['bootstrap','critical-source'])
        self.assertEqual(result['debuggee_exit_code'],0)
        self.assertNotIn('1000',str(result))

    def test_wrong_bytes_missing_nul_or_extra_buffer_rejected(self):
        output,stderr,entries,content,captures = self.fixture()
        for replacement in (b'value=00\n\0',content[0],content[0]+b'extra\0'):
            bad = dict(captures,bootstrap=replacement)
            with self.subTest(replacement=replacement),self.assertRaises(SourceTraceError):
                validate_source_trace(output,stderr,entries,content,bad,executable_path='x')
        with self.assertRaises(SourceTraceError):
            validate_source_trace(output,stderr,entries,content,{},executable_path='x')

    def test_missing_duplicate_unknown_wrong_object_and_wrong_start_rejected(self):
        output,stderr,entries,content,captures = self.fixture()
        changes = [output.replace('W3_SOURCE_EXECUTE 0 code=7d0',''),
                   output.replace('W3_SOURCE_EXECUTE 0 code=7d0','W3_SOURCE_EXECUTE 0 code=7d1'),
                   output.replace('id=0 code=7d0','id=0 code=0'),
                   output.replace('start=101','start=100'),
                   output.replace('W3_SOURCE_COMPILE 1','W3_SOURCE_COMPILE 0'),
                   output.replace('W3_SOURCE_EXIT','W3_SOURCE_UNKNOWN\nW3_SOURCE_EXIT'),
                   output.replace('code 0','code 1'),
                   output.replace('FROZEN_ENTRY.SPIKE_COMPLETED',''),
                   output+'\nW3_SOURCE_EXECUTE 0 code=7d0']
        for changed in changes:
            with self.subTest(changed=changed),self.assertRaises(SourceTraceError):
                validate_source_trace(changed,stderr,entries,content,captures,executable_path='x')

    def test_command_echo_is_not_a_runtime_event(self):
        output,stderr,entries,content,captures = self.fixture()
        output='0:000> bp x "W3_SOURCE_EXECUTE 0 code=7d0"\n'+output
        validate_source_trace(output,stderr,entries,content,captures,executable_path='x')

    def test_only_exact_single_executable_timestamp_diagnostic_is_allowed(self):
        output,stderr,entries,content,captures = self.fixture()
        warning='*** WARNING: Unable to verify timestamp for C:\\bundle\\localcat-spike.exe\n'
        interrupted=output.replace('id=0 code=', 'id='+warning+'0 code=')
        result=validate_source_trace(interrupted,stderr,entries,content,captures,
                                     executable_path='C:/bundle/localcat-spike.exe')
        self.assertEqual(len(result['known_diagnostics']),1)
        for changed in (interrupted+warning,interrupted.replace('C:\\bundle','C:\\elsewhere'),
                        output+'\nSyntax error',output+'\nUnable to resolve breakpoint'):
            with self.assertRaises(SourceTraceError):
                validate_source_trace(changed,stderr,entries,content,captures,
                                      executable_path='C:/bundle/localcat-spike.exe')

    def test_stderr_failure_or_unknown_marker_cannot_pass(self):
        output,stderr,entries,content,captures=self.fixture()
        for bad in ('',stderr*2,stderr+'FROZEN_ENTRY.COMPILE_FAILED\n'):
            with self.assertRaises(SourceTraceError):
                validate_source_trace(output,bad,entries,content,captures,executable_path='x')

    def test_script_reads_buffers_and_observes_return_without_modifying_them(self):
        _,_,entries,content,_=self.fixture()
        script=render_source_commands(entries,content,'C:/evidence',(0x312cc2,0xd260),0xd3a8)
        self.assertIn('.writemem',script)
        self.assertIn('L0n10',script)
        self.assertIn('python314!PyEval_EvalCode',script)
        self.assertNotIn('r rax=',script)
        self.assertNotIn('r rip=',script)
        self.assertIn('W3_SOURCE_UNEXPECTED_COMPILE',script)
        with self.assertRaises(SourceTraceError):
            render_source_commands(entries,[content[0],content[0]],'C:/evidence',(1,2),0)


if __name__=='__main__':
    unittest.main()
