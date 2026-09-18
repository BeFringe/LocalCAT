import unittest
import importlib.metadata
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tools import verify_windows_frozen_e9_paths as e9


def normal_fixture():
    events = ['W3_EP_BEGIN', 'W3_EP_WINDOW_BEGIN', 'W3_EP_CONFIG_RETURN 0', 'W3_EP_INIT_BEGIN',
              'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'W3_EP_ADAPTER 4 tid=2', 'W3_EP_PATH_GET',
              'W3_EP_PATH_RETURN 0000000000001000', 'W3_EP_PATH_CLEARED 1',
              'W3_EP_COMPILE 4 cleared=1 name=<localcat-retained:importlib-external>', 'W3_EP_INSTALL']
    for index, name in ((0, 'encoding-package'), (1, 'encoding-aliases'), (3, 'encoding-win'), (2, 'encoding-utf8')):
        events += [f'W3_EP_ADAPTER {index} tid=2', f'W3_EP_COMPILE {index} cleared=1 name=<localcat-retained:{name}>']
    events += ['W3_EP_FILE_BEGIN NtOpenFile tid=2 root=0 path=C:\\bundle\\_internal\\python314.dll',
               'W3_EP_FILE_END', 'W3_EP_INIT_RETURN 0', 'W3_EP_WINDOW_END', 'W3_EP_HANDOFF',
               'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_EP_EXIT', 'Last event: 1.2: Exit process 0:1, code 0']
    return '\n'.join(events) + '\n'


def stats_fixture():
    return ('W3_EP_STATS_CALL tid=2 sp=0000000000003000 name=psapi.dll\n'
            'W3_EP_STATS_RETURN tid=2 sp=0000000000003000 module=0000000010000000\n'
            'W3_EP_STATS_HOST_BEGIN\nstart end module name\n'
            '00000000`10000000 00000000`10010000 PSAPI C:\\Windows\\System32\\psapi.dll\n'
            'W3_EP_STATS_HOST_END\n')


def failure_fixture(profile):
    events = ['W3_EP_BEGIN']
    if profile == 'clear-failure-null-handles':
        events.append('W3_EP_NULL_HANDLES 0000000000000000 0000000000000000 0000000000000000')
    events += ['W3_EP_WINDOW_BEGIN', 'W3_EP_CONFIG_RETURN 0', 'W3_EP_INIT_BEGIN',
               'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'W3_EP_ADAPTER 4 tid=2',
               'W3_EP_PATH_GET', 'W3_EP_INJECT_BORROWED_NULL', 'W3_EP_PATH_RETURN 0000000000000000',
               'W3_EP_RUNTIME_ERROR', 'W3_EP_ABORT', 'W3_EP_ABORT', 'W3_EP_INIT_RETURN ffffffff',
               'W3_EP_ABORT', 'W3_EP_DIAGNOSTIC FROZEN_ENTRY.INITIALIZATION_FAILED']
    error = ('Traceback (most recent call last):\n'
             '  File "<frozen importlib._bootstrap>", line 1568, in _install_external_importers\n'
             'RuntimeError: FROZEN_ENTRY.INTERPRETER_PATH_CLEAR_FAILED\n')
    if profile == 'clear-failure-null-handles':
        events.append('W3_EP_UI_FALLBACK')
    else:
        events.extend(stats_fixture().splitlines())
        error = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n' + error + 'FROZEN_ENTRY.INITIALIZATION_FAILED\n'
        labels = ('reserved', 'committed', 'reset', 'purged', 'touched', 'segments', '-abandoned', '-cached',
                  'pages', '-abandoned', '-extended', '-noretire', 'mmaps', 'commits', 'resets', 'purges',
                  'threads', 'searches', 'numa nodes', 'elapsed')
        error += 'heap stats: peak total freed current unit count\n'
        error += ''.join(label + ': 0\n' for label in labels)
        error += 'process: user: 0 s, system: 0 s, faults: 0, rss: 0 B, commit: 0 B\n'
    events += ['W3_EP_EXIT', 'Last event: 1.2: Exit process 0:1, code 1']
    return '\n'.join(events) + '\n', error


class E9PathTests(unittest.TestCase):
    def test_file_namespace_is_exact_in_normal_and_failure_windows(self):
        for profile in ('normal', 'clear-failure-stats', 'clear-failure-null-handles'):
            raw, error = ((normal_fixture(), 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n')
                          if profile == 'normal' else failure_fixture(profile))
            kwargs = dict(profile=profile, bundle='C:/bundle', members=['_internal/python314.dll'],
                          windows='C:/Windows', devices={})
            e9.validate_trace(raw, error, **kwargs)
            for unknown in ('W3_EP_FILE_UNKNOWN', 'W3_EP_FILE_BEGIN', 'W3_EP_FILE_END extra'):
                with self.subTest(profile=profile, unknown=unknown), self.assertRaises(e9.ProbeInputError):
                    e9.validate_trace(raw.replace('W3_EP_PATH_GET', unknown + '\nW3_EP_PATH_GET'), error, **kwargs)

    def test_failure_file_window_closes_at_real_process_exit(self):
        block = ('W3_EP_FILE_BEGIN NtOpenFile tid=2 root=0 path=C:\\bundle\\_internal\\python314.dll\n'
                 'W3_EP_FILE_END\n')
        for profile in ('clear-failure-stats', 'clear-failure-null-handles'):
            raw, error = failure_fixture(profile)
            kwargs = dict(profile=profile, bundle='C:/bundle', members=['_internal/python314.dll'],
                          windows='C:/Windows', devices={})
            e9.validate_trace(raw.replace('W3_EP_PATH_GET', block + 'W3_EP_PATH_GET'), error, **kwargs)
            for changed in (raw + block, raw + 'W3_EP_WINDOW_BEGIN\n' + block):
                with self.subTest(profile=profile), self.assertRaises(e9.ProbeInputError):
                    e9.validate_trace(changed, error, **kwargs)

    def test_entire_stats_return_and_host_must_precede_exit(self):
        raw, error = failure_fixture('clear-failure-stats')
        stats = stats_fixture()
        for index in (0, 1, 2, 5):
            prefix = ''.join(stats.splitlines(keepends=True)[:index])
            tail = stats[len(prefix):]
            changed = raw.replace(stats, prefix) + tail
            with self.subTest(index=index), self.assertRaises(e9.ProbeInputError):
                e9.validate_trace(changed, error, 'clear-failure-stats', 'C:/bundle', [], 'C:/Windows', {})

    def test_root_relative_names_require_real_handle_resolution(self):
        raw = ('W3_EP_FILE_BEGIN NtCreateFile tid=2 root=44 path=encodings\\utf_8.py\n'
               'Handle 44\n  Type          File\n  Name          \\Device\\HarddiskVolume3\\bundle\\_internal\n'
               'W3_EP_FILE_END\n')
        files = e9.parse_files(raw, {'\\Device\\HarddiskVolume3': 'C:'})
        self.assertEqual(files[0]['path'], 'c:\\bundle\\_internal\\encodings\\utf_8.py')
        for altered in (raw.replace('  Name', '  Unknown'), raw.replace('root=44', 'root=0'),
                        raw.replace('W3_EP_FILE_END\n', ''), raw + 'W3_EP_FILE_END\n'):
            with self.subTest(altered=altered), self.assertRaises(e9.ProbeInputError):
                e9.parse_files(altered, {'\\Device\\HarddiskVolume3': 'C:'})

    def test_unknown_device_and_relative_paths_fail_closed(self):
        for path in ('relative.py', '\\Device\\Unknown\\file', '\\??\\C:\\bundle\\..\\escape'):
            with self.subTest(path=path), self.assertRaises(e9.ProbeInputError):
                e9.normalize_path(path, {'\\Device\\HarddiskVolume3': 'C:'})

    def test_exact_file_policy_accepts_retained_and_os_positive_controls(self):
        mapping = {'\\Device\\HarddiskVolume3': 'C:'}
        paths = [{'path': 'c:\\bundle\\_internal\\encodings\\utf_8.py'},
                 {'path': 'c:\\windows\\system32\\locale.nls'}]
        counts = e9.classify_files(paths, 'C:/bundle', ['_internal/encodings/utf_8.py'], 'C:/Windows', mapping)
        self.assertEqual(counts, {'retained': 1, 'trusted_os': 1})
        for name in ('c:\\pollution\\traceback.py', 'c:\\bundle\\_internal\\evil.py',
                     'c:\\windows-not-system\\x'):
            with self.subTest(name=name), self.assertRaises(e9.ProbeInputError):
                e9.classify_files([{'path': name}], 'C:/bundle', ['_internal/encodings/utf_8.py'], 'C:/Windows', mapping)

    def test_exit_is_unique_and_follows_terminal_marker(self):
        good = 'W3_EP_EXIT\nLast event: 1.2: Exit process 0:1, code 0\n'
        self.assertEqual(e9.validate_exit(good, 0), 0)
        for raw in ('Last event: 1.2: Exit process 0:1, code 0\nW3_EP_EXIT\n',
                    good + good, good.replace('code 0', 'code 1')):
            with self.subTest(raw=raw), self.assertRaises(e9.ProbeInputError):
                e9.validate_exit(raw, 0)

    def test_stats_are_paired_with_actual_system_host_in_failure_cleanup(self):
        trace = ('\nW3_EP_DIAGNOSTIC FROZEN_ENTRY.INITIALIZATION_FAILED\n'
                 'W3_EP_STATS_CALL tid=2 sp=0000000000003000 name=psapi.dll\n'
                 'W3_EP_STATS_RETURN tid=2 sp=0000000000003000 module=0000000010000000\n'
                 'W3_EP_STATS_HOST_BEGIN\nstart end module name\n'
                 '00000000`10000000 00000000`10010000 PSAPI C:\\Windows\\System32\\psapi.dll\n'
                 'W3_EP_STATS_HOST_END\nW3_EP_EXIT\n')
        self.assertEqual(e9.validate_stats(trace, 'clear-failure-stats', 'C:/Windows', {})['system_host'],
                         'c:\\windows\\system32\\psapi.dll')
        for changed in (trace.replace('module=0000000010000000', 'module=0000000020000000'),
                        trace.replace('C:\\Windows\\System32', 'C:\\pollution'),
                        trace.replace('tid=2 sp=0000000000003000 module', 'tid=3 sp=0000000000003000 module'),
                        trace.replace('W3_EP_STATS_HOST_END', 'W3_EP_STATS_EXTRA\nW3_EP_STATS_HOST_END')):
            with self.subTest(changed=changed), self.assertRaises(e9.ProbeInputError):
                e9.validate_stats(changed, 'clear-failure-stats', 'C:/Windows', {})

    def test_window_starts_before_configure_and_full_nt_roots_are_logged(self):
        text = e9.render_commands('normal', {'entry': 0x9100})
        self.assertIn('@$t0+0x140c', text)
        self.assertIn('W3_EP_WINDOW_BEGIN', text)
        for name in ('NtCreateFile', 'NtOpenFile', 'NtQueryAttributesFile', 'NtQueryFullAttributesFile'):
            self.assertIn('ntdll!' + name, text)
        self.assertIn('!handle', text)
        self.assertIn('W3_EP_INSTALL', text)
        self.assertTrue(all(len(line) < 4096 for line in text.splitlines()))

    def test_null_handle_model_is_before_crt_and_suppresses_only_native_ui_call(self):
        self.assertIn('clear-failure-null-handles', e9.PROFILES)
        script = e9.render_commands('clear-failure-null-handles', {'entry': 0x9100})
        self.assertIn('bu @$exentry', script)
        self.assertIn('W3_EP_NULL_HANDLES', script)
        self.assertIn('@$t0+0x5c84', script)
        self.assertIn('W3_EP_UI_FALLBACK', script)
        self.assertIn('r rip=@rip+6', script)

    def test_unknown_or_extra_normal_event_is_rejected(self):
        trace = normal_fixture()
        kwargs = dict(profile='normal', bundle='C:/bundle', members=['_internal/python314.dll'],
                      windows='C:/Windows', devices={})
        e9.validate_trace(trace, 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n', **kwargs)
        for changed in (trace.replace('W3_EP_PATH_CLEARED 1', 'W3_EP_UNKNOWN\nW3_EP_PATH_CLEARED 1'),
                        trace.replace('W3_EP_INSTALL', 'W3_EP_INSTALL\nW3_EP_INSTALL'),
                        trace.replace('W3_EP_INSTALL', 'W3_EP_INSTALL\nW3_EP_STATS_UNKNOWN'),
                        trace.replace('W3_EP_WINDOW_END', 'W3_EP_WINDOW_END\nW3_EP_FILE_BEGIN NtOpenFile tid=2 root=0 path=C:\\bundle\\_internal\\python314.dll\nW3_EP_FILE_END')):
            with self.assertRaises(e9.ProbeInputError):
                e9.validate_trace(changed, 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n', **kwargs)

    def test_complete_normal_timeline_rejects_every_deleted_repeated_or_changed_event(self):
        trace = normal_fixture()
        kwargs = dict(profile='normal', bundle='C:/bundle', members=['_internal/python314.dll'],
                      windows='C:/Windows', devices={})
        stderr = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'
        for line in trace.splitlines():
            if not line.startswith(('W3_EP_', 'FROZEN_ENTRY.')) or line.startswith('W3_EP_FILE_'):
                continue
            for changed in (trace.replace(line + '\n', '', 1),
                            trace.replace(line + '\n', line + '\n' + line + '\n', 1),
                            trace.replace(line + '\n', 'W3_EP_WRONG\n', 1)):
                with self.subTest(line=line, changed=changed), self.assertRaises(e9.ProbeInputError):
                    e9.validate_trace(changed, stderr, **kwargs)
        for changed in (trace.replace('tid=2', 'tid=3', 1),
                        trace.replace('PATH_RETURN 0000000000001000', 'PATH_RETURN 0000000000000000'),
                        trace.replace('cleared=1', 'cleared=0'),
                        trace.replace('importlib-external>', 'evil>')):
            with self.subTest(changed=changed), self.assertRaises(e9.ProbeInputError):
                e9.validate_trace(changed, stderr, **kwargs)
        for changed in (stderr + 'RuntimeError\n', stderr.replace('SPIKE_COMPLETED', 'INITIALIZATION_FAILED')):
            with self.assertRaises(e9.ProbeInputError):
                e9.validate_trace(trace, changed, **kwargs)

    def test_absolute_root_policy_does_not_admit_ambient_files_below_retained_ancestors(self):
        for name in ('c:\\checkout\\evil.py', 'c:\\checkout\\evidence\\cwd\\traceback.py'):
            with self.assertRaises(e9.ProbeInputError):
                e9.classify_files([{'path': name}], 'C:/checkout/evidence/bundle',
                                  ['_internal/python314.dll'], 'C:/Windows', {})

    def test_native_failure_diagnostic_is_required_before_real_nonzero_exit(self):
        for profile in ('launcher-empty', 'launcher-nonempty', 'exe-pth', 'dll-pth'):
            marker = e9.EARLY[profile]
            raw = 'W3_EP_BEGIN\nW3_EP_DIAGNOSTIC ' + marker + '\nW3_EP_EXIT\nLast event: 1.2: Exit process 0:1, code 1\n'
            kwargs = dict(profile=profile, bundle='C:/bundle', members=[], windows='C:/Windows', devices={})
            e9.validate_trace(raw, marker + '\n', **kwargs)
            for changed in (raw.replace('code 1', 'code 0'), raw.replace(marker, 'FROZEN_ENTRY.WRONG'),
                            raw.replace('W3_EP_DIAGNOSTIC ' + marker + '\n', ''),
                            raw.replace('W3_EP_EXIT', 'W3_EP_HANDOFF\nW3_EP_EXIT')):
                with self.subTest(profile=profile, changed=changed), self.assertRaises(e9.ProbeInputError):
                    e9.validate_trace(changed, marker + '\n', **kwargs)

    @unittest.skipUnless(os.name == 'nt', 'Windows observer runner with Windows path/PE fixtures')
    def test_external_anchors_and_post_run_drift_are_checked(self):
        import pefile
        for drift in (None, 'release', 'candidate', 'source', 'copy', 'pollution', 'helper', 'metadata', 'script'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'artifacts/windows').mkdir(parents=True)
                (root / 'tools').mkdir()
                for name in ('verify_windows_frozen_e9_paths.py', 'trace_windows_frozen_retained_failures.py',
                             'trace_windows_frozen_loader_resolution.py', 'verify_windows_frozen_payload.py'):
                    (root / 'tools' / name).write_bytes(b'fixed observer')
                dist = root / 'input'
                (dist / '_internal').mkdir(parents=True)
                (dist / 'localcat-spike.exe').write_bytes(b'exe')
                (dist / '_internal/python314.dll').write_bytes(b'dll')
                release = root / 'release.json'
                release.write_bytes(b'externally anchored release')
                candidate_path = root / 'packaging/windows/frozen-entry/candidate-input.lock.json'
                candidate_path.parent.mkdir(parents=True)
                candidate_path.write_bytes(b'externally anchored candidate')
                args = SimpleNamespace(dist=dist, release=release, expected_release_sha256='a'*64,
                    expected_commit='b'*40, expected_candidate_digest='c'*64, cdb=root/'cdb.exe', profile='normal')
                meta = ['META']
                parser = {'source': {}, 'distribution_metadata': e9.byte_fact(b'META')}
                candidate = {'candidate_input_digest': 'c'*64, 'evidence_producer': {'pefile': parser},
                             'entry_contract': {'python_c_api': []}}
                section = SimpleNamespace(Name=b'.text\0\0\0', get_data=lambda: b'code')
                pe = unittest.mock.MagicMock()
                pe.__enter__.return_value.sections = [section]
                def runner(command, cwd, env, directory, name, records, timeout):
                    self.assertEqual(timeout, 60)  # Fixed offline observer budget, not a product startup limit.
                    bundle = Path(command[-1]).parent
                    output = normal_fixture().replace('C:\\bundle', str(bundle))
                    (directory / 'cdb.stdout').write_text(output)
                    (directory / 'cdb.stderr').write_text('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n')
                    records.append({'exit_code': 0})
                    targets = {'release': release, 'candidate': candidate_path, 'source': dist/'_internal/python314.dll',
                               'copy': bundle/'_internal/python314.dll', 'pollution': cwd/'traceback.py',
                               'helper': root/'tools/trace_windows_frozen_retained_failures.py',
                               'script': directory/'e9.cdb'}
                    if drift in targets:
                        targets[drift].write_bytes(b'changed during run')
                    if drift == 'metadata':
                        meta[0] = 'changed metadata'
                distribution = SimpleNamespace(read_text=lambda _: meta[0])
                with patch.object(e9, 'ROOT', root), patch.object(e9, 'load_candidate', return_value=candidate), \
                     patch.object(e9, 'checked_bytes'), patch.object(e9, 'verify_machine_sites', return_value={'entry':0x9100}), \
                     patch.object(pefile, 'PE', return_value=pe), patch.object(e9, 'NATIVE_TEXT_SHA', e9.byte_fact(b'code')['sha256']), \
                     patch.object(e9, 'observer_inventory', return_value={}), patch.object(e9, '_devices', return_value={}), \
                     patch.object(e9.importlib.metadata, 'distribution', return_value=distribution), \
                     patch.object(e9, '_environment', return_value=({'SystemRoot': 'C:/Windows'}, root)), \
                     patch.object(e9, 'record_command', side_effect=runner), \
                     patch.object(e9, 'verify_release_binding', return_value={'dist': e9.inventory(dist)['files']}) as verify:
                    if drift is None:
                        _, evidence = e9.run(args)
                        self.assertEqual(evidence['status'], 'OBSERVED_BOUNDED_E9_PATHS')
                    else:
                        with self.assertRaises(e9.ProbeInputError):
                            e9.run(args)
                    self.assertEqual(verify.call_count, 2)
                    self.assertEqual(verify.call_args.kwargs, dict(expected_release_sha256='a'*64,
                        expected_repository_commit='b'*40, expected_candidate_input_digest='c'*64))


if __name__ == '__main__':
    unittest.main()
