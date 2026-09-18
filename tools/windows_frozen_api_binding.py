"""Decode the realized x64 E8 binder and validate its externally observed calls.

This is a deliberately closed instruction grammar for the approved MSVC binder,
not a general disassembler. Every byte and branch of the selected .pdata
function is consumed; unfamiliar compiler output requires review, not guessing.
Addresses prove the realized binding, not C signatures or arbitrary reachability.
"""
from __future__ import annotations

import re
import struct


class BindingError(ValueError):
    pass


PROLOG = bytes.fromhex('48895c2408574883ec20488bfa488bd94885c9')


class Decoder:
    def __init__(self, data, start):
        self.data, self.start, self.pos, self.count = data, start, 0, 0

    @property
    def rva(self):
        return self.start + self.pos

    def fixed(self, hex_bytes):
        raw = bytes.fromhex(hex_bytes)
        if self.data[self.pos:self.pos + len(raw)] != raw:
            raise BindingError(f'unknown or unexpected instruction at RVA {self.rva:x}')
        self.pos += len(raw)
        self.count += 1

    def relative(self, prefix, suffix=b''):
        raw = bytes.fromhex(prefix)
        self.fixed(prefix)
        if self.pos + 4 + len(suffix) > len(self.data):
            raise BindingError('truncated relative instruction')
        delta = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        if self.data[self.pos:self.pos + len(suffix)] != suffix:
            raise BindingError('unexpected immediate')
        self.pos += len(suffix)
        return self.rva + delta

    def branch(self, condition):
        if self.data[self.pos:self.pos + 1] == bytes([0x74 if condition == 'zero' else 0x75]):
            self.fixed('74' if condition == 'zero' else '75')
            if self.pos >= len(self.data):
                raise BindingError('truncated short branch')
            delta = struct.unpack_from('<b', self.data, self.pos)[0]
            self.pos += 1
            return self.rva + delta
        return self.relative('0f84' if condition == 'zero' else '0f85')


def decode_binder(data, start, getproc_iat, read_name, writable, expected):
    """Decode all normal/state/missing blocks and their exact branch edges."""
    d = Decoder(data, start)
    try:
        for op in ('48895c2408', '57', '4883ec20', '488bfa', '488bd9', '4885c9'):
            d.fixed(op)
        state_edges = [d.branch('zero')]
        configured = d.relative('803d', b'\0')
        state_edges.append(d.branch('not-zero'))
        error_slot = d.relative('48833d', b'\0')
        state_edges.append(d.branch('not-zero'))
        if not writable(configured) or not writable(error_slot):
            raise BindingError('state outside writable image')
        name_rva = d.relative('488d15')
        rows, null_edges = [], []
        for index in range(len(expected)):
            call = d.rva
            if d.relative('ff15') != getproc_iat:
                raise BindingError('call does not use the GetProcAddress IAT')
            returned = d.rva
            d.fixed('4885c0')
            null_branch = d.rva
            null_target = d.branch('zero')
            null_edges.append(null_target)
            next_name = d.relative('488d15') if index + 1 < len(expected) else None
            store = d.rva
            slot = d.relative('488905')
            if slot % 8 or not writable(slot) or not writable(slot + 7):
                raise BindingError('binding slot outside aligned writable image')
            name = read_name(name_rva)
            if name not in expected:
                raise BindingError('unapproved bound symbol')
            rows.append({'symbol': name, 'kind': expected[name], 'name_rva': name_rva,
                         'call_rva': call, 'return_rva': returned, 'slot_rva': slot,
                         'store_rva': store, 'stored_rva': d.rva,
                         'null_branch_rva': null_branch, 'null_target_rva': null_target})
            if next_name is not None:
                d.fixed('488bcb')
                name_rva = next_name
        d.fixed('33c0')
        for op in ('488b5c2430', '4883c420', '5f'):
            d.fixed(op)
        success_return = d.rva
        d.fixed('c3')
        missing = d.rva
        if read_name(d.relative('488d05')) != 'FROZEN_ENTRY.CAPI_MISSING':
            raise BindingError('wrong missing diagnostic')
        for op in ('488907', 'b8ffffffff', '488b5c2430', '4883c420', '5f', 'c3'):
            d.fixed(op)
        state = d.rva
        d.fixed('488b5c2430')
        if read_name(d.relative('488d05')) != 'FROZEN_ENTRY.CAPI_STATE':
            raise BindingError('wrong state diagnostic')
        for op in ('488902', 'b8ffffffff', '4883c420', '5f', 'c3'):
            d.fixed(op)
        if (d.pos != len(data) or set(state_edges) != {state} or set(null_edges) != {missing}
                or len({row['symbol'] for row in rows}) != len(expected)
                or len({row['slot_rva'] for row in rows}) != len(expected)
                or error_slot != next(row['slot_rva'] for row in rows if row['symbol'] == 'PyExc_RuntimeError')):
            raise BindingError('incomplete or ambiguous binding control flow')
        return {'function_rva': start, 'function_end_rva': d.rva,
                'success_return_rva': success_return, 'missing_rva': missing,
                'state_rva': state, 'instruction_count': d.count, 'bindings': rows}
    except (KeyError, StopIteration, struct.error, UnicodeError) as exc:
        raise BindingError('invalid binder data or referenced name') from exc


def extract_binding(executable_bytes, python_bytes, api):
    """Use .pdata and PE section/import/export metadata, with no source regex."""
    import pefile
    expected = {name: 'function' for name in api['function_symbols']}
    expected.update({name: 'data' for name in api['data_symbols']})
    if len(expected) != api['symbol_count'] or api['symbol_count'] != 38:
        raise BindingError('invalid exact approved API table')
    with pefile.PE(data=executable_bytes) as exe, pefile.PE(data=python_bytes) as dll:
        if exe.FILE_HEADER.Machine != 0x8664 or dll.FILE_HEADER.Machine != 0x8664:
            raise BindingError('only approved x64 images are decodable')
        if exe.FILE_HEADER.TimeDateStamp != 0:
            raise BindingError('expected deterministic packaged PE timestamp')
        imports = [item.address - exe.OPTIONAL_HEADER.ImageBase
                   for desc in exe.DIRECTORY_ENTRY_IMPORT for item in desc.imports
                   if desc.dll.upper() == b'KERNEL32.DLL' and item.name == b'GetProcAddress']
        if len(imports) != 1:
            raise BindingError('GetProcAddress IAT is not unique')
        def section(pe, rva):
            matches = [s for s in pe.sections if s.VirtualAddress <= rva < s.VirtualAddress + s.Misc_VirtualSize]
            if len(matches) != 1:
                raise BindingError('RVA does not belong to a unique section')
            return matches[0]
        def writable(rva):
            return bool(section(exe, rva).Characteristics & 0x80000000)
        def name(rva):
            flags = section(exe, rva).Characteristics
            if flags & (0x80000000 | 0x20000000) or not flags & 0x40000000:
                raise BindingError('symbol name is not immutable image data')
            data = exe.get_data(rva, 128)
            end = data.find(b'\0')
            if end < 1:
                raise BindingError('missing terminated symbol name')
            return data[:end].decode('ascii')
        candidates = []
        for record in exe.DIRECTORY_ENTRY_EXCEPTION:
            begin, end = record.struct.BeginAddress, record.struct.EndAddress
            if exe.get_data(begin, len(PROLOG)) == PROLOG:
                if not section(exe, begin).Characteristics & 0x20000000 or end <= begin:
                    raise BindingError('invalid .pdata binder range')
                # Several compiled functions share the register-save prolog.
                # Decode the complete state-guard block, rather than locating a
                # string or an FF15 byte pattern inside arbitrary instructions.
                probe = Decoder(exe.get_data(begin, end - begin), begin)
                try:
                    for op in ('48895c2408', '57', '4883ec20', '488bfa', '488bd9', '4885c9'):
                        probe.fixed(op)
                    probe.branch('zero')
                    probe.relative('803d', b'\0'); probe.branch('not-zero')
                    probe.relative('48833d', b'\0'); probe.branch('not-zero')
                    probe.relative('488d15')
                    if probe.relative('ff15') == imports[0]:
                        candidates.append((begin, end))
                except BindingError:
                    continue
        if len(candidates) != 1:
            raise BindingError('no unique complete binder function')
        begin, end = candidates[0]
        result = decode_binder(exe.get_data(begin, end - begin), begin, imports[0], name, writable, expected)
        exports = {}
        for symbol in dll.DIRECTORY_ENTRY_EXPORT.symbols:
            if symbol.name:
                key = symbol.name.decode('ascii')
                if key in expected:
                    if key in exports or symbol.forwarder or not symbol.address:
                        raise BindingError('duplicate, forwarded or missing Python export')
                    flags = section(dll, symbol.address).Characteristics
                    if bool(flags & 0x20000000) != (expected[key] == 'function'):
                        raise BindingError('Python export kind disagrees with exact API')
                    exports[key] = symbol.address
        if set(exports) != set(expected):
            raise BindingError('Python exports do not close exact binding table')
        for row in result['bindings']:
            row['export_rva'] = exports[row['symbol']]
        result['executable_entry_rva'] = exe.OPTIONAL_HEADER.AddressOfEntryPoint
        result['executable_timestamp'] = exe.FILE_HEADER.TimeDateStamp
        result['getproc_iat_rva'] = imports[0]
        return result


def fault_index(table, profile):
    if profile == 'normal':
        return None
    kind = {'function-null': 'function', 'data-null': 'data'}.get(profile)
    if kind is None:
        raise BindingError('unknown binding profile')
    return next(index for index, row in enumerate(table['bindings']) if row['kind'] == kind)


def validate_trace(output, stderr, table, profile, *, python_path=None, executable_path=None):
    """Reject missing/repeated/out-of-order events and debugger uncertainty."""
    known_diagnostics = []
    # /Brepro intentionally has a zero COFF timestamp and no PDB. CDB's
    # timestamp lookup is not our authority: the external release SHA and
    # decoded bytes are. Permit only this exact diagnostic for that exact PE;
    # do not ignore a warning about another image or a breakpoint/parse error.
    if executable_path is not None and table.get('executable_timestamp') == 0:
        warning = '*** WARNING: Unable to verify timestamp for ' + executable_path.replace('/', '\\')
        occurrences = len(re.findall(re.escape(warning) + r'\r?\n', output))
        if occurrences == 1:
            output = re.sub(re.escape(warning) + r'\r?\n', '', output)
            known_diagnostics.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    combined = output + '\n' + stderr
    if any(token in combined.lower() for token in (
            'warning', 'syntax error', 'couldn\'t resolve', 'unable to', 'failed',
            'breakpoint expression', 'waitforevent', 'access violation', 'bad syntax',
            'numeric expression missing', 'range error', 'illegal column count')):
        raise BindingError('debugger warning or error in observation')
    # This observer always captures stderr. Native markers mirror the optional
    # debug output on success, or report the terminal binding failure; neither
    # channel may contradict the profile, repeat a marker or add an unknown one.
    stderr_markers = [line.strip() for line in stderr.splitlines()
                      if line.strip().startswith('FROZEN_ENTRY.')]
    wanted_stderr = (['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.SPIKE_COMPLETED']
                     if profile == 'normal' else ['FROZEN_ENTRY.CAPI_MISSING'])
    if stderr_markers != wanted_stderr:
        raise BindingError('native stderr markers disagree with the observed profile')
    events = [line.strip() for line in output.splitlines() if line.strip().startswith(('W3_API_', 'FROZEN_ENTRY.'))]
    expected_events = ['W3_API_BEGIN']
    binder = [line for line in events if line.startswith('W3_API_BINDER')]
    if len(binder) != 1 or not re.fullmatch(r'W3_API_BINDER python=[0-9a-fA-F`]+', binder[0]):
        raise BindingError('missing unique actual Python module base')
    base = int(binder[0].split('=')[1].replace('`', ''), 16)
    if not base or base % 0x10000:
        raise BindingError('invalid loaded Python base')
    modules = re.findall(r'^ModLoad: ([0-9a-fA-F`]+) [0-9a-fA-F`]+\s+([^\r\n]*[\\/]python314\.dll)\r?$',
                         output, re.MULTILINE | re.IGNORECASE)
    def normalize(path):
        return path.removeprefix('\\\\?\\').replace('/', '\\').lower()
    if (len(modules) != 1 or int(modules[0][0].replace('`', ''), 16) != base
            or python_path is not None and normalize(modules[0][1]) != normalize(python_path)):
        raise BindingError('binder HMODULE differs from actual packaged Python module')
    expected_events.append(binder[0])
    selected = fault_index(table, profile)
    for index, row in enumerate(table['bindings']):
        call = [line for line in events if line.startswith(f'W3_API_CALL {index} ')]
        if len(call) != 1:
            raise BindingError('missing or repeated API call')
        match = re.fullmatch(r'W3_API_CALL (\d+) base=([0-9a-fA-F`]+) name=(\w+)', call[0])
        if not match or int(match[2].replace('`', ''), 16) != base or match[3] != row['symbol']:
            raise BindingError('wrong API HMODULE or name')
        expected_events.append(call[0])
        if index == selected:
            expected_events.append(f'W3_API_INJECT_NULL {index}')
        for kind in ('RETURN',) if index == selected else ('RETURN', 'SLOT'):
            observed = [line for line in events if line.startswith(f'W3_API_{kind} {index} ')]
            if len(observed) != 1:
                raise BindingError('missing or repeated API return/store')
            match = re.fullmatch(r'W3_API_\w+ \d+ value=([0-9a-fA-F`]+)', observed[0])
            wanted = 0 if index == selected else base + row['export_rva']
            if not match or int(match[1].replace('`', ''), 16) != wanted:
                raise BindingError('return or final slot differs from actual export address')
            expected_events.append(observed[0])
        if index == selected:
            break
    if selected is None:
        for index, row in enumerate(table['bindings']):
            observed = [line for line in events if line.startswith(f'W3_API_FINAL_SLOT {index} ')]
            if len(observed) != 1:
                raise BindingError('missing or repeated final slot')
            match = re.fullmatch(r'W3_API_FINAL_SLOT \d+ value=([0-9a-fA-F`]+)', observed[0])
            if not match or int(match[1].replace('`', ''), 16) != base + row['export_rva']:
                raise BindingError('final slot changed before binder success')
            expected_events.append(observed[0])
        expected_events += ['W3_API_SUCCESS value=0', 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
                            'FROZEN_ENTRY.SPIKE_COMPLETED']
        expected_exit = 0
    else:
        expected_events += ['W3_API_MISSING']
        expected_exit = 1
    expected_events.append('W3_API_EXIT')
    exits = re.findall(r'^Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$', output, re.MULTILINE)
    if events != expected_events or len(exits) != 1 or int(exits[0], 16) != expected_exit:
        raise BindingError('incomplete binding timeline or real process exit')
    return {'profile': profile, 'path_observed': True, 'observed_debuggee_exit_code': expected_exit,
            'observed_call_count': len(table['bindings']) if selected is None else selected + 1,
            'injected_symbol': None if selected is None else table['bindings'][selected]['symbol'],
            'known_diagnostics': known_diagnostics}
