from __future__ import annotations

import unittest

from tests.test_windows_frozen_crt_locale_fallback import trace as locale_trace
from tools import verify_windows_frozen_flags0_canary as flags0


EXE = 'C:/bundle/localcat-spike.exe'
SYSTEM32 = 'C:/Windows/System32'


def trace(location):
    prefix = locale_trace('flags0').split('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n')[0]
    events = ['W3_FC_POLICY_RETURN tid=1 value=1']
    markers = (['FROZEN_ENTRY.INVENTORY_EXTRA'] if location in flags0.DIST_POLLUTION else
               ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.SPIKE_COMPLETED'])
    for marker in markers:
        events.append(f'W3_FC_DIAGNOSTIC tid=1 owner=7e78 bytes={len(marker):x} text={marker}')
        if location not in flags0.DIST_POLLUTION:
            events.append(marker)
    events += ['W3_CL_EXIT', 'Last event: 1.1: Exit process 0:1, code '+str(int(location in flags0.DIST_POLLUTION))]
    return prefix+'\n'.join(events)+'\n', '\n'.join(markers)+'\n'


class Flags0CanaryTests(unittest.TestCase):
    def parse(self, location, raw=None, err=None):
        actual, stderr = trace(location)
        return flags0.validate_trace(actual if raw is None else raw, stderr if err is None else err,
            location, executable_path=EXE, system32=SYSTEM32, bait_paths=['C:/bait/kernel32.dll'])

    def test_only_fixed_flags0_six_locations(self):
        self.assertEqual(flags0.LOCATIONS, ('cwd', 'path', 'appdir', 'local-file', 'local-dir', 'sxs-external'))
        for location in flags0.LOCATIONS:
            result = self.parse(location)
            self.assertEqual(result['debuggee_exit_code'], int(location in flags0.DIST_POLLUTION))
            self.assertEqual([r['flags'] for r in result['loader_calls']], [0x800, 0x800, 0])
            self.assertFalse(result['os_failure_observed'])
            self.assertEqual(result['loader_calls'][-1]['name'], 'kernel32')

    def test_every_event_required_once_and_in_order(self):
        for location in flags0.LOCATIONS:
            raw, _ = trace(location); lines = raw.splitlines(keepends=True)
            for index, line in enumerate(lines):
                if not line.startswith(('W3_', 'FROZEN_ENTRY.')):
                    continue
                for changed in (lines[:index]+lines[index+1:], lines[:index]+[line]+lines[index:],
                                [line]+lines[:index]+lines[index+1:] if index else []):
                    if changed and changed != lines:
                        with self.subTest(location=location, line=line), self.assertRaises(ValueError):
                            self.parse(location, ''.join(changed))

    def test_zero_flags_really_reached_before_policy_and_inventory_rejection(self):
        for location in flags0.LOCATIONS:
            raw, _ = trace(location)
            for old, new in (
                ('flags=0 name=kernel32', 'flags=800 name=kernel32'),
                ('flags=0 name=kernel32', 'flags=0 name=unapproved'),
                ('W3_CL_ERROR_INJECT value=57', 'W3_CL_ERROR_INJECT value=7e'),
                ('W3_FC_POLICY_RETURN tid=1 value=1', 'W3_FC_POLICY_RETURN tid=2 value=1'),
                ('sp=200 original=', 'sp=201 original='),
                ('C:/Windows/System32/kernelbase.dll', 'C:/bait/kernelbase.dll'),
                ('pointer=180001234', 'pointer=180001235'),
                ('W3_CL_MAP_END id=1', 'W3_CL_MAP_END id=9'),
                ('W3_CL_E1 maps=4', 'W3_CL_E1 maps=3'),
            ):
                with self.subTest(location=location, old=old), self.assertRaises(ValueError):
                    self.parse(location, raw.replace(old, new))
            if location in flags0.DIST_POLLUTION:
                for old, new in [('INVENTORY_EXTRA', 'SPIKE_COMPLETED'), ('bytes=1c', 'bytes=1b'),
                                 ('W3_FC_DIAGNOSTIC tid=1', 'W3_FC_DIAGNOSTIC tid=2')]:
                    with self.assertRaises(ValueError):
                        self.parse(location, raw.replace(old, new))

    def test_canary_mapping_or_execution_anywhere_rejects(self):
        for location in flags0.LOCATIONS:
            raw, _ = trace(location)
            for line in ('LOCALCAT_DLL_CANARY_PROCESS_ATTACH',
                         'ModLoad: 0000000181000000 0000000181100000 C:/bait/kernel32.dll'):
                with self.assertRaises(ValueError):
                    self.parse(location, raw+line+'\n')

    def test_debugger_errors_extra_stderr_and_bad_exits_reject(self):
        for location in flags0.LOCATIONS:
            raw, err = trace(location); last = raw.splitlines()[-1]+'\n'
            for extra in ('Failed to set breakpoint', "Couldn't insert breakpoint", 'Cannot continue execution',
                          'W3_FC_GUARD_REJECT', 'W3_CL_GUARD_REJECT'):
                with self.assertRaises(ValueError):
                    self.parse(location, raw.replace('W3_CL_EXIT', extra+'\nW3_CL_EXIT'))
            for changed in (last+raw.replace(last, ''), raw+last, raw.replace(last, ''),
                            raw.replace('W3_CL_EXIT\n', 'W3_CL_EXIT\ninterposed\n')):
                with self.assertRaises(ValueError): self.parse(location, changed)
            for changed in ('', err+'MemoryError\n', err*2):
                with self.assertRaises(ValueError): self.parse(location, err=changed)

    def test_script_retains_real_calls_and_adds_only_observation(self):
        script = flags0.render_commands()
        self.assertIn('0x170b5', script)
        self.assertIn('0x103d', script)
        self.assertIn('0x7e78', script)
        self.assertIn('W3_FC_POLICY_RETURN', script)
        self.assertIn('W3_FC_DIAGNOSTIC', script)
        self.assertNotIn('r rip=', script)
        self.assertNotIn('.call ', script)
        self.assertNotRegex(script, r'(?:^|[;{])\s*e[bdqw]\s')

    def test_exact_bait_additions_preserve_internal_dist(self):
        before = {'files':{'localcat-spike.exe':{'sha256':'exe'}, '_internal/kernel32.dll':{'sha256':'real'}},
                  'directories':['_internal']}
        for location in flags0.LOCATIONS:
            additions = flags0.dist_additions(location, b'bait')
            expected = flags0.canary.inventory_with_additions(before, additions)
            self.assertEqual(expected['files']['_internal/kernel32.dll'], {'sha256':'real'})
            self.assertEqual(expected['files']['localcat-spike.exe'], {'sha256':'exe'})
            self.assertEqual(bool(additions), location in flags0.DIST_POLLUTION)
        with self.assertRaises(ValueError): flags0.dist_additions('unknown', b'bait')


if __name__ == '__main__':
    unittest.main()
