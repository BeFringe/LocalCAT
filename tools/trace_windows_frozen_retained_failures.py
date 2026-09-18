"""Bounded E9 return-value faults in a newly created, unchanged packaged child.

The compile profile fails filename allocation, not syntax parsing. Body/install
profiles fail a real allocation called by the retained source. CPython's own
NULL branch sets MemoryError. Missing sys.path is a legal borrowed-NULL result;
the native adapter itself sets RuntimeError. No C API is added or called by the
observer. These controlled failures are not natural OOM or the entire W3 gate.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import tempfile

from tools.probe_windows_frozen_custom_runw import (
    ROOT, ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.windows_frozen_api_binding import extract_binding
from tools.windows_frozen_packaging import tree_inventory
from tools.windows_frozen_release import verify_release_binding


PROFILES = ('control', 'clear-before-missing-path', 'compile-filename-allocation',
            'body-allocation', 'install-allocation')
# Payload-only rebind: the approved native instructions/layout remain identical.
# Every exact PE still requires its own external release anchor and site checks.
EXE_SHAS = frozenset({
    'e0302d378e61e79e4f7eb8c16d88fd7ebc595380aa92eb8371a3c6c6c364d2cf',
    '41f5ffae97c458040d23ad4fa3afcfea43f8ecb0ca1b7e196299b8fcd63ec2f5',
    '2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea',
})
PYTHON_SHA = '0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700'
FILENAME = '<localcat-retained:importlib-external>'
EXPORTS = {'Py_Initialize': 0x309B84, 'PySys_GetObject': 0x3146C0,
           'PyErr_SetString': 0xC2158, 'PyExc_RuntimeError': 0x5DFCF0,
           'Py_CompileStringExFlags': 0x312C6C, 'PyEval_EvalCode': 0xF3518,
           'PyErr_NoMemory': 0x1B38A4, 'PyUnicode_New': 0x3EB70,
           '_PyEval_EvalFrameDefault': 0x43860}
TEST_ANCHORS = {'entry': 0x9100, 'exports': EXPORTS}
# Closed offsets/bytes for the exact digests above, not a general decoder.
NATIVE_CODE = {
    0x141E: 'e8cd1a0000', 0x1423: '85c00f85aa000000',
    0x1446: 'e8f50b0000', 0x14D5: 'e8260b0000', 0x14DF: 'e85c470000',
    0x14E4: 'b801000000', 0x3B39: '4183fd04',
    0x3B43: '488b0546950200488d0d2fc60100ff1579b70100',
    0x3B57: 'bdffffffff488bd8896c24304885c00f8409030000',
    0x3B6C: '40383d4c9502000f85fc020000',
    0x3E75: '488b0d2c920200488d1585c50100488b0506910200',
    0x3E91: '488b09ff1536b40100', 0x3EB4: 'e847e1ffff',
    0x404C: '488b05fd8f02004533c941b801010000c7442420ffffffff',
    0x4064: 'ff1566b20100488b4c24384c8be8',
    0x4084: '4d85ed0f84cf000000',
    0x40AC: '488b05a58f0200498bcdff1514b20100',
    0x40BC: '488bf84885c00f848f000000',
    0x415C: '488d0595c30100488905fe8f0200',
}
PYTHON_CODE = {
    0x312C9C: 'e8ab08e6ff', 0x312CA1: '488bd84885c07434',
    0x3EC06: 'ff15bcc76500488bd84885c00f848da21b00',
    0x1F8EA5: 'e8faa9fbff', 0x1C22F4: 'e903000000',
    0x1C232B: 'e8f0bee7ff488bf84885c0', 0x43860: '4055534154488dac',
}


def check_instructions(read, expected):
    for rva, encoded in expected.items():
        data = bytes.fromhex(encoded)
        if read(rva, len(data)) != data:
            raise ProbeInputError(f'locked machine instruction changed at RVA {rva:x}')


def verify_machine_sites(executable, python, approved_api):
    if byte_fact(executable)['sha256'] not in EXE_SHAS or byte_fact(python)['sha256'] != PYTHON_SHA:
        raise ProbeInputError('retained failure offsets require the exact final PE and Python DLL')
    import pefile
    with pefile.PE(data=executable) as exe, pefile.PE(data=python) as dll:
        for pe, sites in ((exe, NATIVE_CODE), (dll, PYTHON_CODE)):
            check_instructions(pe.get_data, sites)
            for rva, encoded in sites.items():
                # This five-byte leaf thunk has no unwind entry; its verified
                # E9 jump lands at the implementation's real pdata boundary.
                if pe is dll and rva == 0x1C22F4:
                    if not any(entry.struct.BeginAddress == 0x1C22FC for entry in pe.DIRECTORY_ENTRY_EXCEPTION):
                        raise ProbeInputError('builtin leaf thunk lost its pdata target')
                    continue
                if not any(entry.struct.BeginAddress <= rva and rva + len(encoded)//2 <= entry.struct.EndAddress
                           for entry in pe.DIRECTORY_ENTRY_EXCEPTION):
                    raise ProbeInputError('machine guard is outside an actual pdata function range')
        actual = {item.name.decode(): item.address for item in dll.DIRECTORY_ENTRY_EXPORT.symbols
                  if item.name and not item.forwarder}
        if any(actual.get(name) != rva for name, rva in EXPORTS.items()):
            raise ProbeInputError('Python machine/export anchors changed or forwarded')
        if exe.OPTIONAL_HEADER.AddressOfEntryPoint != 0x9100 or exe.FILE_HEADER.TimeDateStamp != 0:
            raise ProbeInputError('unrecognized packaged entry')
        table = extract_binding(executable, python, approved_api)
        slots = {row['symbol']: row['slot_rva'] for row in table['bindings']}
        if any(slots.get(name) != rva for name, rva in {
            'PySys_GetObject': 0x2D090, 'PyErr_SetString': 0x2CF90,
            'PyExc_RuntimeError': 0x2D0A8, 'Py_CompileStringExFlags': 0x2D050,
            'PyEval_EvalCode': 0x2D058}.items()):
            raise ProbeInputError('actual E8 slot differs from E9 machine callsite')
        return {'entry': exe.OPTIONAL_HEADER.AddressOfEntryPoint, 'exports': dict(EXPORTS),
                'native_code': NATIVE_CODE, 'python_code': PYTHON_CODE,
                'layouts': {'tstate_current_frame': 0x48, 'frame_previous': 8,
                            'code_name': 0x78, 'code_filename': 0x70, 'ascii_data': 0x28}}


def guarded(conditions, body, reject=False):
    for condition in reversed(conditions):
        body = f'.if ({condition}) {{ {body} }}' + (' .else { .echo W3_RF_GUARD_REJECT; }' if reject else '') + ';'
    return body


def bytes_guard(pointer, text):
    data = text.encode('ascii') + b'\0'
    result = []
    offset = 0
    while offset < len(data):
        count = next(size for size in (8, 4, 2, 1) if size <= len(data)-offset)
        read = {8: 'poi', 4: 'dwo', 2: 'wo', 1: 'by'}[count]
        result.append(f'{read}(({pointer})+0x{offset:x}) == 0x{int.from_bytes(data[offset:offset+count], "little"):x}')
        offset += count
    return result


def unicode_guard(pointer, text):
    return [f'poi(({pointer})+0x10) == 0x{len(text):x}',
            f'(dwo(({pointer})+0x20) & 0x7c) == 0x64', *bytes_guard(f'({pointer})+0x28', text)]


def render_commands(profile, anchors):
    if profile not in PROFILES:
        raise ProbeInputError('unknown retained failure profile')
    exports = anchors['exports']
    def py(rva):
        delta = rva - exports['Py_Initialize']
        return f'python314!Py_Initialize{"+" if delta >= 0 else "-"}0x{abs(delta):x}'
    def bp(address, command, number=''):
        return f'bp{number} {address} "{command}gc"'
    def inject():
        return '.echo W3_RF_INJECT;r @$t9=1;be 80 81 82 83;r rax=0;' + ('r rip=@rip+6;' if profile.endswith('allocation') else '')
    commands = ['.echo W3_RF_BEGIN', f'r @$t0=@$exentry-0x{anchors["entry"]:x}']
    commands += [f'r @$t{i}=0' for i in range(1, 20)]
    commands += ['sxe -c ".echo W3_RF_EXIT;.lastevent;q" epr',
                 'sxe -c ".echo W3_RF_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_RF_ACCESS_VIOLATION;.lastevent;gn" av',
                 bp('@$t0+0x141e', f'r @$t1={py(0)};r @$t14=@$tid;.printf \\"W3_RF_PYTHON_BASE %p\\", @$t1;.echo;.echo W3_RF_INIT_ENTER;'),
                 bp(py(0x43860), guarded(['@$tid == @$t14'], 'r @$t15=@rcx;bd 71;'), 71),
                 bp('@$t0+0x3a30', 'r @$t10=@ecx;' + guarded(['@ecx == 4'], '.echo W3_RF_ADAPTER;'))]
    clear_guards = ['@$tid == @$t14', '@$t10 == 4', 'by(@$t0+0x2d0bf) == 0',
                    f'@rax == @$t1+0x{exports["PySys_GetObject"]:x}', *bytes_guard('@rcx', 'path')]
    commands.append(bp('@$t0+0x3b51', guarded(clear_guards, '.echo W3_RF_CLEAR_CALL;', True)))
    clear_return = (guarded(['@$t9 == 0', '@$tid == @$t14', '@$t10 == 4'], inject(), True)
                    if profile == 'clear-before-missing-path' else '')
    commands.append(bp('@$t0+0x3b57', clear_return + '.if (@rax == 0) {.echo W3_RF_CLEAR_NULL;} .else {.echo W3_RF_CLEAR_OK;};'))
    commands.append(bp('@$t0+0x3e94', guarded([
        '@$t9 == 1', '@$tid == @$t14', f'@rax == @$t1+0x{exports["PyErr_SetString"]:x}',
        f'@rcx == poi(@$t1+0x{exports["PyExc_RuntimeError"]:x})',
        *bytes_guard('@rdx', 'FROZEN_ENTRY.INTERPRETER_PATH_CLEAR_FAILED')], '.echo W3_RF_RUNTIME_ERROR;', True)))
    compile_guards = ['@$tid == @$t14', 'by(@$t0+0x2d0bf) == 1',
                      f'@rax == @$t1+0x{exports["Py_CompileStringExFlags"]:x}', '@rcx != 0',
                      '@r8d == 0x101', '@r9 == 0', 'dwo(@rsp+0x20) == 0xffffffff', *bytes_guard('@rdx', FILENAME)]
    commands.append(bp('@$t0+0x4064', guarded(['@$t10 == 4'], guarded(compile_guards,
        'r @$t8=1;.echo W3_RF_COMPILE_CALL;', True))))
    commands.append(bp(py(0x312C9C), guarded(['@$t8 == 1', '@$t7 == 0'], guarded([
        '@$tid == @$t14', *bytes_guard('@rcx', FILENAME)], 'r @$t11=1;be 70;', True))))
    commands.append(bp(py(0x312CA1), guarded(['@$t8 == 1'], 'r @$t11=0;')))
    commands.append(bp('@$t0+0x406a', guarded(['@$t10 == 4'],
        '.if (@rax == 0) {.echo W3_RF_COMPILE_NULL;} .else {.echo W3_RF_COMPILE_OK;};r @$t8=0;')))
    commands.append(bp('@$t0+0x40b6', guarded(['@$t10 == 4'], guarded([
        '@$tid == @$t14', f'@rax == @$t1+0x{exports["PyEval_EvalCode"]:x}', '@rcx != 0', '@rdx != 0', '@rdx == @r8'],
        'r @$t8=2;.echo W3_RF_EVAL_CALL;', True))))
    commands.append(bp('@$t0+0x40bc', guarded(['@$t10 == 4'],
        '.if (@rax == 0) {.echo W3_RF_EVAL_NULL;} .else {.echo W3_RF_EVAL_OK;r @$t8=3;};')))
    # Runtime frame layouts are pinned DLL facts, not APIs added to the product.
    owner = ''
    for phase, flag, names in ((2, 5, ['<module>']),
                               (3, 6, ['_get_supported_file_loaders', '_install', '_install_external_importers'])):
        # Keep each debugger command below CDB's 4096-character input limit.
        # Accumulate boolean facts in debugger-only registers; no child memory
        # changes. A failed read/guard is never accepted by the log validator.
        body = 'r @$t16=1;r @$t18=poi(@$t15+0x48);'
        for index, name in enumerate(names):
            body += 'r @$t19=(poi(@$t18) & 0xfffffffffffffffe);r @$t17=poi(@$t19+0x78);'
            conditions = unicode_guard('@$t17', name)
            body += 'r @$t16=(@$t16 & ' + ' & '.join(f'({check})' for check in conditions) + ');'
            if index < (1 if phase == 2 else 2):
                body += 'r @$t17=poi(@$t19+0x70);'
                body += 'r @$t16=(@$t16 & ' + ' & '.join(f'({check})' for check in unicode_guard('@$t17', FILENAME)) + ');'
            if index + 1 < len(names):
                body += 'r @$t18=poi(@$t18+8);'
        body += guarded(['@$t16 == 1'], f'r @$t11={phase};r @$t{flag}=1;be 70;', True)
        owner += guarded([f'@$t8 == {phase}', f'@$t{flag} == 0'],
                         guarded(['@$tid == @$t14', '@$t15 != 0'], body, True))
    commands.append(bp(py(0x1C232B), owner))
    commands.append(bp(py(0x1C2330), 'r @$t11=0;'))
    allocation = ''
    for phase, label, chosen, size in ((1, 'COMPILE', 'compile-filename-allocation', 0x4F),
                                       (2, 'BODY', 'body-allocation', 0x3D),
                                       (3, 'INSTALL', 'install-allocation', 0x3D)):
        action = f'.echo W3_RF_TARGET_{label};r @$t11=0;bd 70;' + ('r @$t7=1;' if phase == 1 else '')
        if profile == chosen:
            action += guarded(['@$t9 == 0'], inject(), True)
        allocation += guarded([f'@$t11 == {phase}'], guarded([
            '@$tid == @$t14', f'@$t8 == {phase}', '@rcx == 0', f'@rdx == 0x{size:x}'], action, True))
    commands.append(bp(py(0x3EC06), allocation, 70))
    commands.append(bp(py(0x1F8EA5), guarded(['@$t9 == 1'], '.echo W3_RF_NULL_BRANCH;')))
    commands.append(bp(py(0x1B38A4), guarded(['@$t9 == 1', f'poi(@rsp) == {py(0x1F8EAA)}'], '.echo W3_RF_PYERR_NOMEM;')))
    commands.append(bp('@$t0+0x415c', guarded(['@$t9 == 1'], '.echo W3_RF_SOURCE_FAILURE;')))
    commands.append(bp('@$t0+0x2000', guarded(['@$t9 == 1', '@$t3 == 0'], 'r @$t3=1;.echo W3_RF_ABORT;')))
    commands.append(bp('@$t0+0x1423', '.if (@eax == 0) {.echo W3_RF_INIT_OK;} .else {.if (@eax == 0xffffffff) {.echo W3_RF_INIT_ERROR;} .else {.echo W3_RF_GUARD_REJECT;};};'))
    commands.append(bp('@$t0+0x2040', '.echo W3_RF_HANDOFF;'))
    commands.append(bp('@$t0+0x14df', guarded(bytes_guard('@rcx', 'FROZEN_ENTRY.INITIALIZATION_FAILED'), '.echo W3_RF_NATIVE_FAILURE;', True)))
    for number, (name, root, path) in enumerate((('NtCreateFile', 'poi(@r8+8)', 'poi(@r8+0x10)'),
                             ('NtOpenFile', 'poi(@r8+8)', 'poi(@r8+0x10)'),
                             ('NtQueryAttributesFile', 'poi(@rcx+8)', 'poi(@rcx+0x10)'),
                             ('NtQueryFullAttributesFile', 'poi(@rcx+8)', 'poi(@rcx+0x10)')), 80):
        commands.append(bp(f'ntdll!{name}', guarded(['@$t9 == 1'],
            f'.printf \\"W3_RF_FILE {name} root=%p path=%msu\\", {root}, {path};.echo;'), number))
    return '\n'.join([*commands, 'bd 70 80 81 82 83', 'g', '']).replace('\\\\"', '\\"')


def expected_events(profile):
    if profile not in PROFILES:
        raise ProbeInputError('unknown retained failure profile')
    events = ['W3_RF_BEGIN', 'W3_RF_INIT_ENTER', 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
              'W3_RF_ADAPTER', 'W3_RF_CLEAR_CALL']
    if profile == 'clear-before-missing-path':
        return events + ['W3_RF_INJECT', 'W3_RF_CLEAR_NULL', 'W3_RF_RUNTIME_ERROR',
                         'W3_RF_ABORT', 'W3_RF_INIT_ERROR', 'W3_RF_NATIVE_FAILURE', 'W3_RF_EXIT']
    events += ['W3_RF_CLEAR_OK', 'W3_RF_COMPILE_CALL', 'W3_RF_TARGET_COMPILE']
    if profile != 'compile-filename-allocation':
        events += ['W3_RF_COMPILE_OK', 'W3_RF_EVAL_CALL', 'W3_RF_TARGET_BODY']
    if profile in ('control', 'install-allocation'):
        events += ['W3_RF_EVAL_OK', 'W3_RF_TARGET_INSTALL']
    if profile == 'control':
        return events + ['W3_RF_INIT_OK', 'W3_RF_HANDOFF', 'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_RF_EXIT']
    events += ['W3_RF_INJECT', 'W3_RF_NULL_BRANCH', 'W3_RF_PYERR_NOMEM']
    if profile == 'compile-filename-allocation':
        events += ['W3_RF_COMPILE_NULL', 'W3_RF_SOURCE_FAILURE']
    elif profile == 'body-allocation':
        events += ['W3_RF_EVAL_NULL', 'W3_RF_SOURCE_FAILURE']
    return events + ['W3_RF_ABORT', 'W3_RF_INIT_ERROR', 'W3_RF_NATIVE_FAILURE', 'W3_RF_EXIT']


def prepare_pollution(cwd):
    sources = ('traceback.py', '_bootstrap_external.py', '_frozen_importlib_external.py',
               'encodings/__init__.py', 'encodings/utf_8.py', 'encodings/_win_cp_codecs.py')
    native = ('encodings.cp314-win_amd64.pyd', 'traceback.cp314-win_amd64.pyd')
    facts = {}
    for name in (*sources, *native):
        # The .pyd is intentionally non-executable: this checks discovery/file
        # access, not a successful hostile native import or executable canary.
        data = (b'raise RuntimeError("RETAINED_FAILURE_DECOY_EXECUTED")\n' if name in sources
                else b'MZLocalCAT non-executable discovery decoy\n')
        target = cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        facts[name] = byte_fact(data)
    return facts


def validate_stderr(stderr, profile):
    """Match the entire captured stream for this exact locked fault path.

    These traceback frames and the fallback display were observed on all five
    final-PE profiles. A different/secondary exception or a missing source frame
    is not an accepted alternative. Only object addresses/refcount are dynamic.
    """
    prefix = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n'
    if profile == 'control':
        if stderr != prefix + 'FROZEN_ENTRY.SPIKE_COMPLETED\n':
            raise ProbeInputError('unexpected complete control stderr')
        return None
    frames = {
        'clear-before-missing-path': [(1568, '<frozen importlib._bootstrap>', '_install_external_importers')],
        'compile-filename-allocation': [(1568, '<frozen importlib._bootstrap>', '_install_external_importers')],
        'body-allocation': [(1568, '<frozen importlib._bootstrap>', '_install_external_importers'),
                            (233, FILENAME, '<module>')],
        'install-allocation': [(1570, '<frozen importlib._bootstrap>', '_install_external_importers'),
                               (1560, FILENAME, '_install'), (1546, FILENAME, '_get_supported_file_loaders')],
    }[profile]
    prefix += 'Traceback (most recent call last):\n'
    prefix += ''.join(f'  File "{filename}", line {line}, in {function}\n' for line, filename, function in frames)
    suffix = 'FROZEN_ENTRY.INITIALIZATION_FAILED\n'
    if profile == 'clear-before-missing-path':
        if stderr != prefix + 'RuntimeError: FROZEN_ENTRY.INTERPRETER_PATH_CLEAR_FAILED\n' + suffix:
            raise ProbeInputError('unexpected complete clear-before exception stderr')
        return 'NORMAL_EXCEPTION_DISPLAY'
    pattern = (re.escape(prefix) +
               r'object address  : ([0-9a-fA-F]{16})\nobject refcount : ([1-9][0-9]*)\n'
               r'object type     : ([0-9a-fA-F]{16})\nobject type name: MemoryError\n'
               r'object repr     : MemoryError\(\)\nlost sys\.stderr\n' + re.escape(suffix))
    match = re.fullmatch(pattern, stderr)
    if match is None or not all(int(match[index], 16) for index in (1, 3)):
        raise ProbeInputError('unexpected complete allocation exception stderr')
    return 'EARLY_INITIALIZATION_MEMORYERROR_FALLBACK'


def validate_trace(output, stderr, profile, executable_path, python_path=None, polluted_cwd=None):
    expected = expected_events(profile)
    known = []
    warning = '*** WARNING: Unable to verify timestamp for ' + executable_path.replace('/', '\\')
    if output.count(warning + '\n') == 1:
        output = output.replace(warning + '\n', '')
        known.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    combined = output + '\n' + stderr
    if any(token in combined.lower() for token in (
        'warning', 'unable to', 'couldn\'t resolve', 'waitforevent', 'bad syntax', 'syntax error',
        'numeric expression missing', 'range error', 'illegal column count', 'breakpoint expression',
        'access violation', 'memory access error')):
        raise ProbeInputError('debugger uncertainty in retained failure observation')
    bases = re.findall(r'^W3_RF_PYTHON_BASE ([0-9a-fA-F`]+)$', output, re.MULTILINE)
    modules = re.findall(r'^ModLoad: ([0-9a-fA-F`]+) [0-9a-fA-F`]+\s+([^\r\n]*[\\/]python314\.dll)$', output, re.MULTILINE | re.IGNORECASE)
    number = lambda text: int(text.replace('`', ''), 16)
    normalize = lambda text: text.removeprefix('\\\\?\\').replace('/', '\\').lower()
    if 'RETAINED_FAILURE_DECOY_EXECUTED' in combined:
        raise ProbeInputError('polluted source executed')
    if (len(bases) != 1 or len(modules) != 1 or number(bases[0]) == 0
            or number(bases[0]) % 0x10000 or number(bases[0]) != number(modules[0][0])
            or python_path is not None and normalize(modules[0][1]) != normalize(python_path)):
        raise ProbeInputError('Python base does not identify the exact packaged module')
    files = [line for line in output.splitlines() if line.startswith('W3_RF_FILE ')]
    for line in files:
        if not re.fullmatch(r'W3_RF_FILE Nt(?:CreateFile|OpenFile|QueryAttributesFile|QueryFullAttributesFile) '
                            r'root=[0-9a-fA-F`]+ path=\S[^\r\n]*', line):
            raise ProbeInputError('malformed file observation record')
    if polluted_cwd is not None:
        prefix = normalize(polluted_cwd).rstrip('\\') + '\\'
        loads = [line for line in output.splitlines() if line.startswith('ModLoad:')]
        if any(prefix in normalize(line) for line in [*files, *loads]):
            raise ProbeInputError('polluted cwd reached file discovery or module loading')
    events = [line.strip() for line in output.splitlines() if line.startswith(('W3_RF_', 'FROZEN_ENTRY.'))
              and not line.startswith(('W3_RF_PYTHON_BASE ', 'W3_RF_FILE '))]
    if events != expected:
        raise ProbeInputError('retained failure timeline missing, repeated, out of order, or wrong branch')
    exits = re.findall(r'^Last event: .*: Exit process .*?, code ([0-9a-fA-F]+)\s*$', output, re.MULTILINE)
    if len(exits) != 1 or int(exits[0], 16) != int(profile != 'control'):
        raise ProbeInputError('missing real expected process exit')
    error_display = validate_stderr(stderr, profile)
    if profile != 'control':
        if any(FILENAME.lower() in line.lower() for line in files):
            raise ProbeInputError('logical retained filename caused a disk file request')
    return {'observed_debuggee_exit_code': int(exits[0], 16), 'known_diagnostics': known,
            'markers': events, 'python_error_display': error_display, 'file_calls_after_injection': files}


def run(args):
    if os.name != 'nt':
        raise ProbeInputError('actual retained failure observation requires Windows')
    release_bytes = args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes, args.dist, expected_release_sha256=args.expected_release_sha256,
            expected_repository_commit=args.expected_commit, expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify()
    candidate = load_candidate(ROOT / 'packaging/windows/frozen-entry/candidate-input.lock.json')
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise ProbeInputError('candidate differs from external expected digest')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if (pefile.__version__ != parser_lock['version'] or metadata is None
            or byte_fact(metadata.encode()) != {key: parser_lock['distribution_metadata'][key] for key in ('bytes', 'sha256')}):
        raise ProbeInputError('unlocked pefile metadata')
    exe = args.dist / 'localcat-spike.exe'
    python = args.dist / '_internal/python314.dll'
    exe_bytes = checked_bytes(exe, release['executable'])
    python_bytes = checked_bytes(python, candidate['runtime']['cpython']['dll'])
    anchors = verify_machine_sites(exe_bytes, python_bytes, candidate['entry_contract']['python_c_api'])
    directory = Path(tempfile.mkdtemp(prefix=f'retained-{args.profile}-', dir=ROOT / 'artifacts/windows'))
    (directory / 'cwd').mkdir()
    (directory / 'symbols').mkdir()
    pollution = prepare_pollution(directory / 'cwd') if getattr(args, 'polluted_cwd', False) else {}
    cwd_inventory = tree_inventory(directory / 'cwd')
    environment = trace_environment(os.environ, 'normal')
    if pollution:
        environment['PYTHONPATH'] = str(directory / 'cwd')
    script = directory / 'retained.cdb'
    script.write_text(render_commands(args.profile, anchors), encoding='ascii', newline='\n')
    def inventory():
        current_metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
        if current_metadata is None:
            raise ProbeInputError('PE parser metadata unavailable during inventory')
        return {**observer_inventory(args.cdb), Path(__file__).name: byte_fact(Path(__file__).read_bytes()),
                'pefile': byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile-METADATA': byte_fact(current_metadata.encode())}
    observers = inventory()
    evidence = {'classification': 'CONTROLLED_RETAINED_FAILURES_NOT_W3_GATE', 'status': 'INCOMPLETE',
                'profile': args.profile, 'release_sha256': args.expected_release_sha256,
                'repository_commit': args.expected_commit, 'candidate_input_digest': args.expected_candidate_digest,
                'executable': byte_fact(exe_bytes), 'python': byte_fact(python_bytes), 'anchors': anchors,
                'observers': observers, 'script': byte_fact(script.read_bytes()), 'commands': [],
                'post_observation_inputs_unchanged': False,
                'pollution': pollution, 'polluted_pythonpath': bool(pollution),
                'limitations': ['controlled single return, not natural OOM or syntax failure',
                    'one pinned compiler/runtime, unknown layouts/instructions reject',
                    'software breakpoints alter timing; no-hit is not proof of unreachability',
                    'file observations cover named ntdll calls after injection, not all IO',
                    'polluted .pyd files are discovery decoys, not executable native canaries',
                    'does not grant all W3 assertions or product readiness']}
    print('RETAINED_FAILURE_TRACE=' + str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), '-y', str(directory / 'symbols'), '-i', str(args.dist),
                            '-cf', str(script), str(exe)], directory / 'cwd',
                           environment, directory, 'cdb', evidence['commands'], timeout=45)
        except ProbeInputError:
            if args.profile == 'control' or not evidence['commands'] or evidence['commands'][-1].get('exit_code') != 1:
                raise
        output = (directory / 'cdb.stdout').read_text(encoding='utf-8', errors='strict')
        stderr = (directory / 'cdb.stderr').read_text(encoding='utf-8', errors='strict')
        evidence['trace'] = validate_trace(output, stderr, args.profile, str(exe), str(python),
                                           str(directory / 'cwd') if pollution else None)
        verify()
        if (args.release.read_bytes() != release_bytes or inventory() != observers
                or byte_fact(script.read_bytes()) != evidence['script']
                or tree_inventory(directory / 'cwd') != cwd_inventory):
            raise ProbeInputError('release, observer, or script changed during execution')
        evidence['post_observation_inputs_unchanged'] = True
        evidence['status'] = 'OBSERVED_CONTROLLED_RETAINED_PATH'
    finally:
        (directory / 'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--expected-release-sha256', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--expected-candidate-digest', required=True)
    parser.add_argument('--cdb', type=Path, required=True)
    parser.add_argument('--profile', choices=PROFILES, default='control')
    parser.add_argument('--polluted-cwd', action='store_true', help='scoped traceback/encodings/source and non-executable .pyd discovery decoys')
    _, result = run(parser.parse_args())
    raise SystemExit(0 if result['status'] == 'OBSERVED_CONTROLLED_RETAINED_PATH' else 1)
