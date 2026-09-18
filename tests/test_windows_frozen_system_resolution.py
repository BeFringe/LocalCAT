"""External OS resolution facts never become application runtime inputs."""
import struct
import unittest
import argparse
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tools import capture_windows_frozen_system_resolution as observer

from tools.capture_windows_frozen_system_resolution import (
    ResolutionFactsError, parse_api_set_namespace, initial_system_modules, clean_capture,
)


def namespace(values=(('', 'kernelbase.dll'), ('other.exe', 'kernel32.dll')), revision='0'):
    name = ('api-ms-win-core-test-l1-1-'+revision).encode('utf-16le')
    value_offset = 52
    blob = bytearray(value_offset + 20*len(values))
    def add(text):
        offset = len(blob)
        data = text.encode('utf-16le')
        blob.extend(data)
        return offset, len(data)
    name_offset = len(blob); blob.extend(name)
    for index, (alias, host) in enumerate(values):
        ao, al = add(alias); ho, hl = add(host)
        struct.pack_into('<5I', blob, value_offset+20*index, 0, ao, al, ho, hl)
    struct.pack_into('<7I', blob, 0, 6, len(blob), 0, 1, 28, 0, 31)
    struct.pack_into('<6I', blob, 28, 0, name_offset, len(name), len('api-ms-win-core-test-l1-1'.encode('utf-16le')), value_offset, len(values))
    return bytes(blob)


class SystemResolutionTests(unittest.TestCase):
    def test_candidate_and_parser_metadata_are_reread_after_observation(self):
        import pefile
        for drift in ('candidate', 'parser-metadata'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                candidate_path = root/'packaging/windows/frozen-entry/candidate-input.lock.json'
                candidate_path.parent.mkdir(parents=True)
                candidate_path.write_bytes(b'original candidate')
                (root/'artifacts/windows').mkdir(parents=True)
                (root/'tools').mkdir()
                (root/'tools/trace_windows_frozen_loader_resolution.py').write_bytes(b'loader')
                release_path = root/'release.json'; release_path.write_bytes(b'release')
                metadata = {'value':'original metadata'}
                candidate = {'candidate_input_digest':'1'*64, 'runtime':{'cpython':{'dll':{}}},
                    'entry_contract':{'dynamic_loader':{'application_system_calls':[]}},
                    'evidence_producer':{'pefile':{'source':{}, 'version':pefile.__version__,
                        'distribution_metadata':observer.byte_fact(metadata['value'].encode())}}}
                def command(argv, cwd, env, directory, label, records, **kwargs):
                    (directory/'cdb.stdout').write_text('trace', encoding='utf-8')
                    (directory/'cdb.stderr').write_text('', encoding='utf-8')
                    (directory/'child-apiset.bin').write_bytes(b'captured')
                    if drift == 'candidate': candidate_path.write_bytes(b'changed candidate')
                    else: metadata['value'] = 'changed metadata'
                mocks = {'ROOT':root, 'os':SimpleNamespace(name='nt', environ={}),
                    'verify_release_binding':lambda *a, **k:{'executable':{}},
                    'load_candidate':lambda *a:candidate, 'checked_bytes':lambda *a:b'',
                    'verify_sites':lambda *a:{}, 'render_commands':lambda *a:'g\n',
                    'observer_inventory':lambda *a:{}, 'system_facts':lambda:{'system32':'C:/Windows/System32'},
                    'record_command':command, 'trace_environment':lambda *a:{},
                    'clean_capture':lambda *a:'trace', 'parse_api_set_namespace':lambda *a:{},
                    'initial_system_modules':lambda *a:[], 'parse_loader_events':lambda *a:{'calls':[]},
                    'validate_profile_calls':lambda *a:None, 'validate_stderr':lambda *a:None}
                for name, value in mocks.items(): stack.enter_context(patch.object(observer, name, value))
                stack.enter_context(patch.object(observer.importlib.metadata, 'distribution',
                    return_value=SimpleNamespace(read_text=lambda name:metadata['value'])))
                stack.enter_context(patch.object(observer.ctypes, 'sizeof', return_value=8))
                args = argparse.Namespace(dist=root/'dist', release=release_path, cdb=root/'cdb.exe',
                    expected_release_sha256='2'*64, expected_commit='3'*40, expected_candidate_digest='1'*64)
                with self.assertRaisesRegex(ResolutionFactsError, 'inputs changed'):
                    observer.run(args)

    def test_default_and_importer_specific_mapping(self):
        blob = namespace()
        default = parse_api_set_namespace(blob, {'api-ms-win-core-test-l1-1-0.dll': 'localcat-spike.exe'})
        self.assertEqual(default['api-ms-win-core-test-l1-1-0.dll']['host'], 'kernelbase.dll')
        self.assertEqual(default['api-ms-win-core-test-l1-1-0.dll']['name_relation'], 'exact-captured-name')
        alias = parse_api_set_namespace(blob, {'api-ms-win-core-test-l1-1-0': 'other.exe'})
        self.assertEqual(alias['api-ms-win-core-test-l1-1-0']['host'], 'kernel32.dll')

    def test_malformed_or_ambiguous_mapping_is_rejected(self):
        raw = namespace()
        mutations = [raw[:-1], raw+b'x', namespace((('', '../evil.dll'),)),
                     namespace((('', 'kernelbase.dll'), ('', 'kernel32.dll')))]
        for offset, value in ((0, 5), (4, 1), (12, 0xffffffff), (16, 0xfffffffc),
                              (32, 0xffffffff), (36, 3), (44, 0xfffffffc), (48, 0)):
            changed = bytearray(raw); struct.pack_into('<I', changed, offset, value)
            mutations.append(bytes(changed))
        for blob in mutations:
            with self.subTest(blob=blob), self.assertRaises(ResolutionFactsError):
                parse_api_set_namespace(blob, {'api-ms-win-core-test-l1-1-0': 'localcat-spike.exe'})
        with self.assertRaises(ResolutionFactsError):
            parse_api_set_namespace(raw, {'api-ms-win-absent': 'localcat-spike.exe'})

    def test_captured_revision_correlation_is_not_an_os_resolver_claim(self):
        row = parse_api_set_namespace(namespace(revision='4'),
            {'api-ms-win-core-test-l1-1-0': 'localcat-spike.exe'})['api-ms-win-core-test-l1-1-0']
        self.assertEqual(row['schema_contract'], 'api-ms-win-core-test-l1-1-4')
        self.assertEqual(row['name_relation'], 'captured-hash-prefix-correlation-not-OS-resolver')
        with self.assertRaises(ResolutionFactsError):
            parse_api_set_namespace(namespace(revision='4'),
                {'api-ms-win-core-test-l2-1-0': 'localcat-spike.exe'})

    def test_missing_alias_or_ambiguous_hashed_prefix_is_not_guessed(self):
        with self.assertRaises(ResolutionFactsError):
            parse_api_set_namespace(namespace((('other.exe', 'kernelbase.dll'),)),
                {'api-ms-win-core-test-l1-1-0': 'localcat-spike.exe'})
        blob = bytearray(namespace(revision='4'))
        entry = blob[28:52]
        second = ('api-ms-win-core-test-l1-1-5').encode('utf-16le')
        no = len(blob); blob.extend(second)
        eo = len(blob); blob.extend(entry); blob.extend(entry)
        struct.pack_into('<I', blob, eo+24+4, no)
        struct.pack_into('<I', blob, 4, len(blob))
        struct.pack_into('<II', blob, 12, 2, eo)
        with self.assertRaises(ResolutionFactsError):
            parse_api_set_namespace(bytes(blob), {'api-ms-win-core-test-l1-1-0': 'localcat-spike.exe'})

    def test_capture_requires_unambiguous_early_markers_and_no_debugger_errors(self):
        markers = ('W3_SYS_INITIAL_MODULES_BEGIN', 'W3_SYS_INITIAL_MODULES_END',
                   'W3_SYS_MAP_BEGIN', 'W3_SYS_MAP_END', 'W3_LD_PE_ENTRY')
        raw = '\n'.join(markers)+'\n'
        self.assertEqual(clean_capture(raw, '', 'E:/bundle/localcat-spike.exe'), raw)
        mutations = [raw.replace(m, '') for m in markers]
        mutations += [raw.replace(m, m+'\n'+m) for m in markers]
        mutations += ['\n'.join(reversed(markers))+'\n']
        errors = ('Failed to set breakpoint', "Couldn't insert breakpoint", 'Cannot continue execution',
                  'Memory access error', 'Unable to read memory', 'Syntax error',
                  'W3_SYS_MAP_REJECT', '*** WARNING: unknown')
        mutations += [raw+error+'\n' for error in errors]
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ResolutionFactsError):
                clean_capture(changed, '', 'E:/bundle/localcat-spike.exe')
        for error in errors[:-2]:
            with self.subTest(error=error), self.assertRaises(ResolutionFactsError):
                clean_capture(raw, error, 'E:/bundle/localcat-spike.exe')
        warning = '*** WARNING: Unable to verify timestamp for E:\\bundle\\localcat-spike.exe\n'
        self.assertEqual(clean_capture(warning+raw, '', 'E:/bundle/localcat-spike.exe'), raw)
        with self.assertRaises(ResolutionFactsError):
            clean_capture(warning+warning+raw, '', 'E:/bundle/localcat-spike.exe')

    def test_initial_module_events_require_system_path_before_pe_entry(self):
        exe = 'E:/bundle/localcat-spike.exe'
        trace = ('ModLoad: 100000 120000 '+exe+'\n'
                 'ModLoad: 700000 800000 C:/Windows/System32/ntdll.dll\n'
                 'ModLoad: 900000 a00000 C:/Windows/System32/kernel32.dll\n'
                 'ModLoad: b00000 c00000 C:/Windows/System32/user32.dll\n'
                 'W3_SYS_INITIAL_MODULES_BEGIN\n'
                 'W3_SYS_LDR base=100000 path=E:/bundle/localcat-spike.exe\n'
                 'W3_SYS_LDR base=700000 path=C:/Windows/System32/ntdll.dll\n'
                 'W3_SYS_LDR base=900000 path=C:/Windows/System32/kernel32.dll\n'
                 'W3_SYS_LDR base=b00000 path=C:/Windows/System32/user32.dll\n'
                 'W3_SYS_INITIAL_MODULES_END\n'
                 'W3_LD_PE_ENTRY\n')
        self.assertEqual(len(initial_system_modules(trace, exe, 'C:/Windows/System32')), 3)
        partial = trace.replace('ModLoad: 700000 800000 C:/Windows/System32/ntdll.dll',
                                'ModLoad: 700000 800000 ntdll.dll')
        self.assertEqual(len(initial_system_modules(partial, exe, 'C:/Windows/System32')), 3)
        for changed in (trace.replace('System32/ntdll.dll', 'Temp/ntdll.dll'),
                        trace.replace('W3_LD_PE_ENTRY', ''),
                        trace.replace('900000 a00000', '900000 800000'),
                        trace.replace('W3_SYS_INITIAL_MODULES_BEGIN', ''),
                        trace.replace('base=700000', 'base=900000'),
                        trace.replace('W3_SYS_LDR base=700000 path=C:/Windows/System32/ntdll.dll',
                                      'W3_SYS_LDR base=700000 path=ntdll.dll'),
                        trace.replace('ModLoad: b00000 c00000', 'ModLoad: f00000 f10000'),
                        trace.replace('ModLoad: 900000 a00000 C:/Windows/System32/kernel32.dll',
                                      'ModLoad: 900000 a00000 C:/Windows/System32/kernelbase.dll')):
            with self.assertRaises(ResolutionFactsError):
                initial_system_modules(changed, exe, 'C:/Windows/System32')


if __name__ == '__main__':
    unittest.main()
