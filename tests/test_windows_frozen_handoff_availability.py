"""Synthetic parser checks are not substitutes for actual premint guard runs."""
import unittest
from unittest.mock import patch

from tools import trace_windows_frozen_handoff_availability as availability


EXE = 'C:/bundle/localcat-spike.exe'
DLL = 'C:/bundle/_internal/python314.dll'


def trace(profile):
    lines = ['ModLoad: 00000001`80000000 00000001`81000000 C:/bundle/_internal/python314.dll',
             'W3_HA_BEGIN', 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
             'W3_HA_PYTHON_BASE 00000001`80000000',
             'W3_HA_READY bundle=00000000`10000000 type=00000000`20000000 issued=0 taken=0 state=1 armed=1 closed=0 tid=12 owner=12']
    if profile == 'control':
        lines += ['W3_HA_OWNER result=1', 'W3_HA_MINTED', 'W3_HA_BOOTSTRAP_EVAL',
                  'W3_HA_EVAL_OK', 'FROZEN_ENTRY.SPIKE_COMPLETED']
    else:
        field, original = ('armed', '1') if profile == 'unarmed-first-take' else ('bundle', '00000000`10000000')
        lines += [f'W3_HA_INJECT field={field} original={original} value=0', 'W3_HA_OWNER result=0',
                  'W3_HA_ERROR FROZEN_ENTRY.HANDOFF_UNAVAILABLE',
                  f'W3_HA_RESTORE field={field} value={original}', 'W3_HA_ERROR_SET',
                  'W3_HA_ABORT', 'W3_HA_REVOKED',
                  'W3_HA_NATIVE_FAILURE FROZEN_ENTRY.BOOTSTRAP_EXECUTION_UNAVAILABLE']
    lines += ['W3_HA_EXIT', 'Last event: 1.2: Exit process 1, code '+('0' if profile == 'control' else '1'), '']
    return '\n'.join(lines)


def stderr(profile):
    return 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.'+(
        'SPIKE_COMPLETED' if profile == 'control' else 'BOOTSTRAP_EXECUTION_UNAVAILABLE')+'\n'


class HandoffAvailabilityTests(unittest.TestCase):
    def parse(self, raw, profile, err=None):
        return availability.validate_trace(raw, stderr(profile) if err is None else err, profile,
                                           executable_path=EXE, python_path=DLL)

    def test_profiles_are_explicit_state_faults_not_early_scheduling(self):
        self.assertEqual(availability.PROFILES, ('control', 'unarmed-first-take', 'missing-bundle-first-take'))
        self.assertEqual(availability.CLASSIFICATION, 'CONTROLLED_PREMINT_AVAILABILITY_STATE_FAULT')
        for profile in availability.PROFILES:
            result = self.parse(trace(profile), profile)
            self.assertEqual(result['debuggee_exit_code'], int(profile != 'control'))
            self.assertEqual(result['injections'], int(profile != 'control'))
            self.assertFalse(result['actual_early_scheduling_observed'])

    def test_missing_repeated_or_reordered_events_fail_closed(self):
        for profile in availability.PROFILES:
            raw = trace(profile)
            events = [line+'\n' for line in raw.splitlines() if line.startswith(('W3_HA_', 'FROZEN_ENTRY.'))]
            for event in events:
                for changed in (raw.replace(event, '', 1), raw.replace(event, event+event, 1)):
                    with self.subTest(profile=profile, event=event), self.assertRaises(availability.AvailabilityError):
                        self.parse(changed, profile)

    def test_fault_cannot_mint_eval_read_or_restore_before_error(self):
        for profile in availability.PROFILES[1:]:
            raw = trace(profile)
            restore = next(line+'\n' for line in raw.splitlines() if line.startswith('W3_HA_RESTORE'))
            bad = [raw.replace('W3_HA_ABORT', label+'\nW3_HA_ABORT') for label in (
                'W3_HA_MINTED', 'W3_HA_BOOTSTRAP_EVAL', 'W3_HA_UNEXPECTED_READ')]
            bad += [raw.replace(restore, '').replace('W3_HA_ERROR ', restore+'W3_HA_ERROR '),
                    raw.replace('result=0', 'result=1'), raw.replace('state=1', 'state=2'),
                    raw.replace('issued=0', 'issued=3'), raw.replace('taken=0', 'taken=1'),
                    raw.replace('tid=12 owner=12', 'tid=13 owner=12')]
            for changed in bad:
                with self.subTest(profile=profile), self.assertRaises(availability.AvailabilityError):
                    self.parse(changed, profile)

    def test_restore_requires_the_original_state_and_exact_error(self):
        raw = trace('missing-bundle-first-take')
        for old, new in [('RESTORE field=bundle value=00000000`10000000', 'RESTORE field=bundle value=0'),
                         ('INJECT field=bundle original=00000000`10000000', 'INJECT field=bundle original=1'),
                         ('HANDOFF_UNAVAILABLE', 'AUTHORITY_UNAVAILABLE')]:
            with self.assertRaises(availability.AvailabilityError):
                self.parse(raw.replace(old, new), 'missing-bundle-first-take')

    def test_python_module_must_be_loaded_before_interpreter_start_marker(self):
        for profile in availability.PROFILES:
            raw = trace(profile); loaded = raw.splitlines()[0]+'\n'; without = raw.replace(loaded, '')
            for changed in (without+loaded, without.replace('W3_HA_PYTHON_BASE', loaded+'W3_HA_PYTHON_BASE')):
                with self.subTest(profile=profile), self.assertRaises(availability.AvailabilityError):
                    self.parse(changed, profile)

    def test_exit_must_be_natural_unique_terminal_and_stderr_exact(self):
        for profile in availability.PROFILES:
            raw = trace(profile); last = raw.splitlines()[-1]+'\n'
            for changed in (last+raw.replace(last, ''), raw+last, raw.replace(last, ''),
                            raw.replace('W3_HA_EXIT\n', 'W3_HA_EXIT\nintervening\n')):
                with self.assertRaises(availability.AvailabilityError):
                    self.parse(changed, profile)
            with self.assertRaises(availability.AvailabilityError):
                self.parse(raw, profile, stderr(profile)+'MemoryError\n')

    def test_debugger_breakpoint_or_continue_failure_invalidates_no_hit_claim(self):
        messages = ('Failed to set breakpoint at localcat_spike+0x5980',
                    "Couldn't insert breakpoint at localcat_spike+0x7760", 'Cannot continue execution')
        for profile in availability.PROFILES:
            for message in messages:
                raw = trace(profile).replace('W3_HA_EXIT\n', message+'\nW3_HA_EXIT\n')
                with self.subTest(profile=profile, message=message), self.assertRaises(availability.AvailabilityError):
                    self.parse(raw, profile)

    def test_normal_deferred_python_breakpoint_still_requires_full_actual_trace(self):
        notice = "Bp expression 'python314!Py_Initialize-0x247a2c' could not be resolved, adding deferred bp\n"
        for profile in availability.PROFILES:
            self.parse(notice+trace(profile), profile)

    def test_script_only_changes_and_restores_one_native_state(self):
        for profile in availability.PROFILES:
            script = availability.render_commands(profile)
            self.assertIn('0x2994', script)
            self.assertIn('0x2d82', script)
            self.assertIn('W3_HA_GUARD_REJECT', script)
            self.assertEqual(script.count('eb @$t0+0x2d0ba'), 2 if profile == 'unarmed-first-take' else 0)
            self.assertEqual(script.count('eq @$t0+0x2d0b0'), 2 if profile == 'missing-bundle-first-take' else 0)
            for forbidden in ('r rip=', 'r rax=', '.call ', 'r rsp=', 'r eax='):
                self.assertNotIn(forbidden, script)
            self.assertLess(max(map(len, script.splitlines())), 4096)

    def test_unknown_profile_and_unverified_machine_are_rejected(self):
        with self.assertRaises(availability.AvailabilityError):
            availability.render_commands('early-interpreter')
        with self.assertRaises(ValueError):
            availability.verify_machine_sites(b'bad exe', b'bad dll', [])

    def test_observer_inventory_includes_reused_identity_guard(self):
        with patch.object(availability, 'observer_inventory', return_value={'existing': {'sha256':'checked'}}):
            inventory = availability.observers(None)
        for name in ('trace_windows_frozen_handoff_availability.py', 'trace_windows_frozen_handoff_identity.py',
                     'pefile', 'pefile-METADATA', 'existing'):
            self.assertIn(name, inventory)


if __name__ == '__main__':
    unittest.main()
