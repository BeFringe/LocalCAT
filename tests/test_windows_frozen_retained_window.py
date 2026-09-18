"""Bounded windows and rooted layouts are not a complete race proof."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import verify_windows_frozen_retained_window as window


DIST = 'C:/evidence/dist'


def trace(stage='pre-dispatch', failure=None):
    rows = ['W3_RW_BEGIN', 'W3_RW_ENTRY tid=12', 'W3_RW_POLICY_CALL tid=12 flags=800',
            'W3_RW_POLICY_RETURN tid=12 value=1']
    if failure:
        rows += [f'W3_RW_DIAG tid=12 bytes={len(failure):x} text={failure}']
    else:
        for name, base in [('vcruntime140.dll', '70000000'), ('python314.dll', '71000000')]:
            rows += [f'W3_RW_CALL tid=12 sp=1000 flags=900 name={DIST}/_internal/{name}']
            if stage == 'pre-dispatch' and name == 'vcruntime140.dll':
                rows += ['W3_RW_PAUSE pre-dispatch', 'W3_RW_RESUME']
            rows += [f'ModLoad: {base} {int(base,16)+0x10000:x} {DIST}/_internal/{name}',
                     f'W3_RW_RETURN tid=12 sp=1000 module={base}']
        rows += ['W3_RW_BINDER tid=12 python=71000000']
        if stage == 'binder-entry':
            rows += ['W3_RW_PAUSE binder-entry', 'W3_RW_RESUME']
        for marker in window.SUCCESS:
            rows += [f'W3_RW_DIAG tid=12 bytes={len(marker):x} text={marker}', marker]
    rows += ['W3_RW_EXIT', f'Last event: 1.2: Exit process 1, code {int(bool(failure))}']
    return '\n'.join(rows) + '\n'


class WindowTests(unittest.TestCase):
    def test_bounded_matrix(self):
        self.assertEqual(window.STAGES, ('pre-dispatch', 'binder-entry'))
        self.assertEqual(len(window.OPERATIONS), 32)
        self.assertEqual(len(set(window.OPERATIONS)), 32)
        self.assertEqual(set(window.ROOTED_CASES), {'root-junction', 'ancestor-junction', 'internal-junction', 'critical-hardlink'})
        self.assertIn('NOT_FULL_RACE', window.CLASSIFICATION)

    def test_both_actual_windows_require_exact_timeline(self):
        for stage in window.STAGES:
            with self.subTest(stage=stage):
                result = window.validate_trace(trace(stage), '\n'.join(window.SUCCESS)+'\n', DIST, stage)
                self.assertEqual(result['debuggee_exit_code'], 0)
                self.assertEqual(result['pause_stage'], stage)

    def test_missing_duplicate_wrong_thread_path_or_stage_rejected(self):
        raw = trace()
        mutations = [('W3_RW_PAUSE pre-dispatch\n', ''), ('W3_RW_RESUME\n', 'W3_RW_RESUME\nW3_RW_RESUME\n'),
                     ('tid=12 sp=1000 module=', 'tid=13 sp=1000 module='), ('flags=900', 'flags=800'),
                     ('ModLoad: 71000000 71010000 C:/evidence', 'ModLoad: 71000000 71010000 C:/wrong'),
                     ('W3_RW_BINDER tid=12 python=71000000', 'W3_RW_BINDER tid=12 python=70000000'),
                     ('W3_RW_PAUSE pre-dispatch', 'W3_RW_PAUSE binder-entry'), ('code 0', 'code 1')]
        for old, new in mutations:
            with self.subTest(old=old), self.assertRaises(window.WindowError):
                window.validate_trace(raw.replace(old, new), '\n'.join(window.SUCCESS)+'\n', DIST, 'pre-dispatch')
        with self.assertRaises(window.WindowError):
            window.validate_trace(raw, '\n'.join(window.SUCCESS)+'\nFORGED\n', DIST, 'pre-dispatch')

    def test_rooted_rejection_requires_no_dispatch_or_native_module(self):
        marker = 'FROZEN_ENTRY.REPARSE_REJECTED'
        raw = trace(failure=marker)
        window.validate_trace(raw, marker+'\n', DIST, 'root-junction')
        for bad in [raw.replace('W3_RW_EXIT', 'W3_RW_CALL tid=12 sp=1000 flags=900 name=X\nW3_RW_EXIT'),
                    raw+'ModLoad: 71000000 71100000 C:/other/python314.dll\n',
                    raw.replace(marker, 'FROZEN_ENTRY.ROOTED_OPEN_FAILED'), raw.replace('code 1', 'code 0')]:
            with self.assertRaises(window.WindowError):
                window.validate_trace(bad, marker+'\n', DIST, 'root-junction')

    def test_first_module_cannot_load_before_pre_call_pause(self):
        raw = trace()
        module = 'ModLoad: 70000000 70010000 C:/evidence/dist/_internal/vcruntime140.dll\n'
        early = raw.replace(module, '').replace('W3_RW_PAUSE pre-dispatch\n', module+'W3_RW_PAUSE pre-dispatch\n')
        with self.assertRaises(window.WindowError):
            window.validate_trace(early, '\n'.join(window.SUCCESS)+'\n', DIST, 'pre-dispatch')

    def test_first_module_cannot_load_during_controlled_pause(self):
        raw = trace()
        module = 'ModLoad: 70000000 70010000 C:/evidence/dist/_internal/vcruntime140.dll\n'
        paused = raw.replace(module, '').replace('W3_RW_RESUME\n', module+'W3_RW_RESUME\n')
        with self.assertRaises(window.WindowError):
            window.validate_trace(paused, '\n'.join(window.SUCCESS)+'\n', DIST, 'pre-dispatch')

    def test_piped_resume_prompt_is_not_lost_or_confused_with_echoed_commands(self):
        raw = trace().replace('W3_RW_RESUME\n', '0:000> W3_RW_RESUME\n').replace(
            'ModLoad: 70000000', '0:000> ModLoad: 70000000')
        window.validate_trace(raw, '\n'.join(window.SUCCESS)+'\n', DIST, 'pre-dispatch')
        binder = trace('binder-entry').replace('W3_RW_RESUME\n', '0:000> W3_RW_RESUME\n').replace(
            'W3_RW_DIAG tid=12 bytes=25', '0:000> W3_RW_DIAG tid=12 bytes=25')
        window.validate_trace(binder, '\n'.join(window.SUCCESS)+'\n', DIST, 'binder-entry')
        with self.assertRaises(window.WindowError):
            window.validate_trace(raw.replace('0:000> W3_RW_RESUME', '0:000> .echo W3_RW_RESUME'),
                                  '\n'.join(window.SUCCESS)+'\n', DIST, 'pre-dispatch')

    def test_exact_error_and_no_success_can_pass_denial(self):
        good = OSError('sharing violation'); good.winerror = 32
        bad = OSError('permission'); bad.winerror = 5
        result = window.attempt(lambda: (_ for _ in ()).throw(good))
        window.validate_attempts([dict(result, operation=op, path=path) for op,path in window.OPERATIONS], retained=True)
        for effect in (lambda: None, lambda: (_ for _ in ()).throw(bad)):
            rows = [dict(window.attempt(effect), operation=op, path=path) for op,path in window.OPERATIONS]
            with self.assertRaises(window.WindowError):
                window.validate_attempts(rows, retained=True)
        with self.assertRaises(window.WindowError):
            window.validate_attempts([], retained=True)

    def test_script_pins_application_calls_and_never_changes_registers(self):
        anchors = {'entry':0x9100, 'binder':0x2260, 'dispatch':0x6847, 'diagnostic':0x7e78}
        for stage in window.STAGES:
            script = window.render_commands(anchors, stage)
            self.assertIn('bp @$t0+0x6847', script)
            self.assertIn('bp @$t0+0x2260', script)
            self.assertNotIn('KERNELBASE!', script)
            self.assertNotIn('r rip=', script)
            self.assertNotIn('r rax=', script)
            self.assertEqual(script.count('.echo W3_RW_PAUSE '+stage), 1)

    def test_unsafe_operation_paths_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bundle = root/'dist'; bundle.mkdir()
            for relative in ('../outside', '/outside', 'C:/outside', '_internal/../../outside'):
                with self.subTest(relative=relative), self.assertRaises(window.WindowError):
                    window.controlled_target(root, bundle, relative)

    def test_exact_new_pe_guard_precedes_decoder(self):
        with mock.patch.object(window, 'verify_sites') as decode:
            with self.assertRaises(window.WindowError):
                window.machine_anchors(b'not-final', b'python', {})
            decode.assert_not_called()

    def test_positive_controls_really_change_files_and_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; (source/'_internal').mkdir(parents=True)
            (source/'_internal/localcat_spike_critical.py').write_bytes(b'original')
            for index, operation in enumerate(('write', 'replace', 'rename')):
                case = root/str(index); case.mkdir()
                row = window.positive_operation(source, case, operation, '_internal/localcat_spike_critical.py')
                self.assertTrue(row['succeeded']); self.assertTrue(row['exact_delta'])
            case = root/'directory'; case.mkdir()
            self.assertTrue(window.positive_operation(source, case, 'rename', '.')['exact_delta'])

    def test_cli_help_from_unrelated_cwd(self):
        tool = Path(__file__).resolve().parents[1]/'tools/verify_windows_frozen_retained_window.py'
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-B', str(tool), '--help'], cwd=cwd,
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--expected-release-sha256', result.stdout)


if __name__ == '__main__':
    unittest.main()
