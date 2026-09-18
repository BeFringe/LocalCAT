import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.windows_frozen_api_binding import BindingError, decode_binder, extract_binding, validate_trace

NORMAL_STDERR = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'


def fixture(symbols=('PyBool_FromLong', 'PyExc_RuntimeError')):
    start, iat, missing, state = 0x1000, 0x8000, 0x9000, 0x9100
    names = {0x6000 + 0x100 * index: name for index, name in enumerate(symbols)}
    names.update({missing: 'FROZEN_ENTRY.CAPI_MISSING', state: 'FROZEN_ENTRY.CAPI_STATE'})
    code, branches = bytearray(), []
    def put(value):
        code.extend(bytes.fromhex(value))
    def rel(prefix, target):
        raw = bytes.fromhex(prefix)
        code.extend(raw + struct.pack('<i', target - (start + len(code) + len(raw) + 4)))
    def branch(prefix, label):
        branches.append((len(code) + len(bytes.fromhex(prefix)), label))
        rel(prefix, start + len(code) + len(bytes.fromhex(prefix)) + 4)
    put('48895c2408574883ec20488bfa488bd94885c9')
    branch('0f84', 'state')
    rel('803d', 0xA000 - 1); put('00')
    branch('0f85', 'state')
    rel('48833d', 0xB000 + 8 * (len(symbols) - 1) - 1); put('00')
    branch('0f85', 'state')
    rel('488d15', 0x6000)
    for index in range(len(symbols)):
        rel('ff15', iat); put('4885c0'); branch('0f84', 'missing')
        if index + 1 < len(symbols):
            rel('488d15', 0x6000 + 0x100 * (index + 1))
        rel('488905', 0xB000 + 8 * index)
        if index + 1 < len(symbols):
            put('488bcb')
    put('33c0488b5c24304883c4205fc3')
    missing_at = start + len(code)
    rel('488d05', missing); put('488907b8ffffffff488b5c24304883c4205fc3')
    state_at = start + len(code)
    put('488b5c2430'); rel('488d05', state); put('488902b8ffffffff4883c4205fc3')
    for pos, target in branches:
        struct.pack_into('<i', code, pos, {'missing': missing_at, 'state': state_at}[target] - (start + pos + 4))
    return bytes(code), start, iat, names


def decode(code=None, names=None, iat=None):
    original, start, original_iat, original_names = fixture()
    return decode_binder(code or original, start, original_iat if iat is None else iat,
                         (names or original_names).__getitem__, lambda rva: 0xA000 <= rva < 0xC000,
                         {'PyBool_FromLong': 'function', 'PyExc_RuntimeError': 'data'})


class BinderTests(unittest.TestCase):
    def test_complete_instruction_stream_and_cfg(self):
        table = decode()
        self.assertEqual([x['symbol'] for x in table['bindings']], ['PyBool_FromLong', 'PyExc_RuntimeError'])
        self.assertEqual([x['slot_rva'] for x in table['bindings']], [0xB000, 0xB008])
        self.assertEqual(table['function_rva'], 0x1000)
        self.assertGreater(table['instruction_count'], 30)

    def test_unknown_instruction_or_trailing_code_rejected(self):
        code, *_ = fixture()
        for value in (b'\x90' + code[1:], code + b'\x90'):
            with self.assertRaises(BindingError):
                decode(value)

    def test_name_iat_slot_and_null_branch_changes_rejected(self):
        code, start, iat, names = fixture()
        changed_names = dict(names, **{})
        changed_names[0x6100] = 'PyUnexpected'
        with self.assertRaises(BindingError): decode(names=changed_names)
        with self.assertRaises(BindingError): decode(iat=iat + 8)
        table = decode()
        for field, replacement in [('slot_rva', 0xB000), ('null_target_rva', table['success_return_rva'])]:
            data = bytearray(code)
            row = table['bindings'][-1]
            at = row['store_rva'] if field == 'slot_rva' else row['null_branch_rva']
            width = 7 if field == 'slot_rva' else 6
            struct.pack_into('<i', data, at - start + width - 4, replacement - at - width)
            with self.assertRaises(BindingError): decode(bytes(data))


class FakePE:
    def __init__(self):
        self.functions = ['PyBool_FromLong', *(f'PyFunction{i}' for i in range(36))]
        self.symbols = [*self.functions, 'PyExc_RuntimeError']
        self.api = {'function_symbols': self.functions, 'data_symbols': ['PyExc_RuntimeError'], 'symbol_count': 38}
        self.code, self.start, self.iat, self.names = fixture(self.symbols)
        self.FILE_HEADER = SimpleNamespace(Machine=0x8664, TimeDateStamp=0)
        self.OPTIONAL_HEADER = SimpleNamespace(ImageBase=0x140000000, AddressOfEntryPoint=0x1500)
        self.sections = [SimpleNamespace(VirtualAddress=0x1000, Misc_VirtualSize=0x3000, Characteristics=0x60000000),
                         SimpleNamespace(VirtualAddress=0x6000, Misc_VirtualSize=0x4000, Characteristics=0x40000000),
                         SimpleNamespace(VirtualAddress=0xA000, Misc_VirtualSize=0x2000, Characteristics=0xC0000000)]
        self.DIRECTORY_ENTRY_IMPORT = [SimpleNamespace(dll=b'KERNEL32.DLL', imports=[
            SimpleNamespace(name=b'GetProcAddress', address=0x140000000 + self.iat)])]
        self.DIRECTORY_ENTRY_EXCEPTION = [SimpleNamespace(struct=SimpleNamespace(BeginAddress=self.start, EndAddress=self.start + len(self.code)))]
        self.DIRECTORY_ENTRY_EXPORT = SimpleNamespace(symbols=[
            SimpleNamespace(name=name.encode(), forwarder=None, address=0x1100 + 16 * index if index < 37 else 0xA100)
            for index, name in enumerate(self.symbols)])

    def __enter__(self): return self
    def __exit__(self, *_): pass
    def get_data(self, rva, length):
        if self.start <= rva < self.start + len(self.code):
            return self.code[rva - self.start:rva - self.start + length]
        return self.names[rva].encode() + b'\0'


class ImageTests(unittest.TestCase):
    def test_unique_pdata_range_and_direct_exports(self):
        exe, dll = FakePE(), FakePE()
        with patch('pefile.PE', side_effect=[exe, dll]):
            table = extract_binding(b'exe', b'dll', exe.api)
        self.assertEqual(len(table['bindings']), 38)
        self.assertEqual(table['bindings'][-1]['export_rva'], 0xA100)

    def test_forwarded_missing_duplicate_wrong_kind_export_rejected(self):
        for change in ('forward', 'missing', 'duplicate', 'kind'):
            exe, dll = FakePE(), FakePE()
            if change == 'forward': dll.DIRECTORY_ENTRY_EXPORT.symbols[0].forwarder = b'other.PyBool_FromLong'
            if change == 'missing': dll.DIRECTORY_ENTRY_EXPORT.symbols.pop()
            if change == 'duplicate': dll.DIRECTORY_ENTRY_EXPORT.symbols.append(dll.DIRECTORY_ENTRY_EXPORT.symbols[0])
            if change == 'kind': dll.DIRECTORY_ENTRY_EXPORT.symbols[-1].address = 0x1100
            with self.subTest(change=change), patch('pefile.PE', side_effect=[exe, dll]), self.assertRaises(BindingError):
                extract_binding(b'exe', b'dll', exe.api)

    def test_ambiguous_function_iat_and_extra_code_rejected(self):
        for change in ('pdata', 'iat', 'bytes'):
            exe, dll = FakePE(), FakePE()
            if change == 'pdata': exe.DIRECTORY_ENTRY_EXCEPTION *= 2
            if change == 'iat': exe.DIRECTORY_ENTRY_IMPORT *= 2
            if change == 'bytes':
                exe.code += b'\x90'
                exe.DIRECTORY_ENTRY_EXCEPTION[0].struct.EndAddress += 1
            with self.subTest(change=change), patch('pefile.PE', side_effect=[exe, dll]), self.assertRaises(BindingError):
                extract_binding(b'exe', b'dll', exe.api)


def trace_fixture(profile='normal'):
    table = decode()
    table['bindings'][0]['export_rva'] = 0x100
    table['bindings'][1]['export_rva'] = 0x200
    lines = ['W3_API_BEGIN', 'ModLoad: 0000000010000000 0000000010700000 C:\\bundle\\_internal\\python314.dll',
             'W3_API_BINDER python=0000000010000000']
    selected = None if profile == 'normal' else (0 if profile == 'function-null' else 1)
    for index, row in enumerate(table['bindings']):
        lines.append(f'W3_API_CALL {index} base=0000000010000000 name={row["symbol"]}')
        if index == selected:
            lines += [f'W3_API_INJECT_NULL {index}', f'W3_API_RETURN {index} value=0000000000000000']
            break
        lines += [f'W3_API_RETURN {index} value={0x10000000 + row["export_rva"]:016x}',
                  f'W3_API_SLOT {index} value={0x10000000 + row["export_rva"]:016x}']
    if selected is None:
        lines += [f'W3_API_FINAL_SLOT {index} value={0x10000000 + row["export_rva"]:016x}'
                  for index, row in enumerate(table['bindings'])]
        lines += ['W3_API_SUCCESS value=0',
                  'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.SPIKE_COMPLETED']
    else:
        lines += ['W3_API_MISSING']
    lines += ['W3_API_EXIT', f'Last event: 12.34: Exit process 0:12, code {0 if selected is None else 1}']
    return '\n'.join(lines), table


class TraceTests(unittest.TestCase):
    def test_observer_inventory_covers_indirect_local_validators(self):
        from tools import trace_windows_frozen_api_binding as observer
        names = {
            'trace_windows_frozen_api_binding.py', 'windows_frozen_api_binding.py',
            'windows_frozen_release.py', 'windows_frozen_packaging.py',
            'probe_windows_frozen_custom_runw.py', 'trace_windows_frozen_entry.py',
            'audit_w3_stock_bootloader.py', 'prepare_windows_frozen_entry_inputs.py',
            'windows_frozen_manifest.py', 'windows_frozen_patch_series.py',
        }
        indirect = {'audit_w3_stock_bootloader.py', 'prepare_windows_frozen_entry_inputs.py',
                    'windows_frozen_manifest.py', 'windows_frozen_patch_series.py'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tools').mkdir()
            for name in names:
                (root / 'tools' / name).write_bytes(name.encode())
            cdb = root / 'cdb.exe'
            cdb.write_bytes(b'observer')
            with patch.object(observer, 'ROOT', root):
                original = observer.observer_inventory(cdb)
                self.assertEqual(set(original), names | {'cdb.exe'})
                for name in indirect:
                    path = root / 'tools' / name
                    path.write_bytes(b'changed indirect verifier')
                    self.assertNotEqual(observer.observer_inventory(cdb), original)
                    path.write_bytes(name.encode())
                (root / 'tools' / 'windows_frozen_manifest.py').unlink()
                with self.assertRaises(FileNotFoundError):
                    observer.observer_inventory(cdb)

    def test_normal_and_two_null_profiles(self):
        for profile in ('normal', 'function-null', 'data-null'):
            output, table = trace_fixture(profile)
            result = validate_trace(output, NORMAL_STDERR if profile == 'normal' else 'FROZEN_ENTRY.CAPI_MISSING\n', table, profile)
            self.assertTrue(result['path_observed'])
            self.assertNotIn('10000000', str(result))

    def test_trace_missing_duplicate_forged_address_or_slot_rejected(self):
        output, table = trace_fixture()
        bad = [output.replace('W3_API_CALL 0', 'W3_API_CALL 9'),
               output.replace('0000000010000100', '0000000010000108', 1),
               output.replace('W3_API_SLOT 0 value=0000000010000100', ''),
               output + '\nW3_API_SUCCESS value=0', output.replace('code 0', 'code 1'),
               output.replace('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', ''),
               output + '\nWARNING: Unable to verify timestamp',
               output.replace('W3_API_CALL 1', 'W3_API_CALL 0')]
        bad += [output.replace('W3_API_FINAL_SLOT 0 value=0000000010000100',
                              'W3_API_FINAL_SLOT 0 value=0000000010000108')]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(BindingError):
                validate_trace(value, NORMAL_STDERR, table, 'normal')

    def test_failure_must_not_reach_e9(self):
        output, table = trace_fixture('data-null')
        with self.assertRaises(BindingError):
            validate_trace(output + '\nFROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.CAPI_MISSING\n', table, 'data-null')

    def test_stderr_cannot_contradict_profile_or_add_native_markers(self):
        normal, table = trace_fixture()
        failure, _ = trace_fixture('function-null')
        for output, stderr, profile in (
                (normal, 'FROZEN_ENTRY.CAPI_MISSING\n', 'normal'),
                (failure, 'FROZEN_ENTRY.CAPI_MISSING\nFROZEN_ENTRY.SPIKE_COMPLETED\n', 'function-null'),
                (failure, 'FROZEN_ENTRY.CAPI_MISSING\nFROZEN_ENTRY.UNKNOWN\n', 'function-null'),
                (failure, 'FROZEN_ENTRY.CAPI_MISSING\n' * 2, 'function-null'),
                (normal, NORMAL_STDERR * 2, 'normal'),
                (normal, '', 'normal')):
            with self.subTest(stderr=stderr), self.assertRaises(BindingError):
                validate_trace(output, stderr, table, profile)

    def test_zero_timestamp_diagnostic_only_for_exact_anchored_pe(self):
        output, table = trace_fixture()
        table['executable_timestamp'] = 0
        warning = '*** WARNING: Unable to verify timestamp for C:\\bundle\\localcat-spike.exe\n'
        observed = output.replace('W3_API_BINDER python=', 'W3_API_BINDER python=' + warning)
        result = validate_trace(observed, NORMAL_STDERR, table, 'normal', executable_path='C:/bundle/localcat-spike.exe')
        self.assertEqual(len(result['known_diagnostics']), 1)
        for value in (observed.replace('timestamp for C:', 'timestamp for D:'), observed + warning,
                      observed.replace('timestamp for', 'checksum for')):
            with self.assertRaises(BindingError):
                validate_trace(value, NORMAL_STDERR, table, 'normal', executable_path='C:/bundle/localcat-spike.exe')


if __name__ == '__main__':
    unittest.main()
