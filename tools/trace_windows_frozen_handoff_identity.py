"""Observe a single synthetic interpreter-identity mismatch in a real W3 PE.

Only RAX after a real PyThreadState_GetInterpreter call is replaced. This does
not create or impersonate a second interpreter, and is not a cross-interpreter
execution test. No native/source bytes, interpreter state, or owner are changed.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import struct
import tempfile

from tools.probe_windows_frozen_custom_runw import (
    ROOT, ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.windows_frozen_api_binding import extract_binding
from tools.windows_frozen_release import verify_release_binding


class IdentityTraceError(ValueError):
    pass


PROFILES = ('control', 'first-take', 'read', 'reproof', 'close')
CLASSIFICATION = 'API_IDENTITY_MISMATCH_NOT_REAL_CROSS_INTERPRETER'
EXE_SHA = '2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea'
PYTHON_SHA = '0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700'
# Complete localcat_owner function: the returned pointer is CMP'd, never
# dereferenced; either branch then overwrites EAX with a boolean and returns.
OWNER_CODE = ('4883ec28803def960200007458803de896020000754f48833dd2960200007445'
              '488b0529960200ff15e3b801003b055d9702007530488b0504960200ff15ceb80100'
              '488bc8488b05fc950200ff15beb80100483b052f970200750ab8010000004883c428c3'
              '33c04883c428c3')
NATIVE_CODE = {
    0x39C0: OWNER_CODE,
    0x42D0: ('40534883ec20488bd9e8e2f6ffff85c07433803dd28d020000742a483b1d6e8e02007521'
             '807b1000741b488b05af8d020080b8981d230002750bb8010000004883c4205bc3'
             '488b0d8c8d0200488d1505b90100488b05668c0200488915378e0200488b09ff1596af0100'
             '33c04883c4205bc3'),
    # First take is inlined in execute, before bootstrap source can execute.
    0x2994: ('e82710000085c00f84bc030000381d14a702000f85b0030000488b0da4a702004885c9'
             '0f84a0030000488b05eca6020080b8981d2300010f858c030000488b0558a602004533c0'
             '33d2ff15edc80100488bf84885c07511488b4c2438e80b9900008bfbe9a9030000'
             'c6401001488d8c2480000000488b05a1a6020048893d4aa70200c6059ea6020001'
             'c680981d230002'),
    0x1940: '4883ec38e8d71c0000',
    0x1995: '33c04883c438c3',
    0x19E0: '40535741564881ec80000000e82f1c0000',
    0x1A4A: '33c04881c480000000415e5f5bc3',
    0x1D20: '4883ec28e8a725000085c0750733c04883c428c3',
    0x3620: '48895c2408574883ec20488bfae89e0c000085c00f84a4000000',
    0x2D7C: 'ff154ec50100',
    0x2D32: 'ff1598c5010048894424504885c0744c',
    0x2DC0: 'ff150ac50100e835f2ffffbbffffffff',
    0x2000: ('4883ec28488b0da5b00200c605aab0020001c605a1b00200004885c9741ce83d380000'
             '488b0d86b00200e8d1a2000048c70576b00200000000004883c428c3'),
    0x14DF: 'e85c470000b801000000',
}
EXPORTS = {'PyThreadState_GetInterpreter': 0x1E5890, 'PyThreadState_Get': 0x69E14,
           'PyThread_get_thread_ident': 0x1B897C, 'PyErr_SetString': 0xC2158,
           'PyExc_RuntimeError': 0x5DFCF0, 'Py_Initialize': 0x309B84}


def check_owner_code(code):
    if code != bytes.fromhex(OWNER_CODE):
        raise IdentityTraceError('complete owner comparator differs from locked code')


def verify_machine_sites(executable, python, approved_api):
    if byte_fact(executable)['sha256'] != EXE_SHA or byte_fact(python)['sha256'] != PYTHON_SHA:
        raise IdentityTraceError('identity observer requires the exact final PE and Python DLL')
    import pefile
    with pefile.PE(data=executable) as exe, pefile.PE(data=python) as dll:
        check_owner_code(exe.get_data(0x39C0, len(bytes.fromhex(OWNER_CODE))))
        for rva, encoded in NATIVE_CODE.items():
            data = bytes.fromhex(encoded)
            if exe.get_data(rva, len(data)) != data or not any(
                    row.struct.BeginAddress <= rva and rva + len(data) <= row.struct.EndAddress
                    for row in exe.DIRECTORY_ENTRY_EXCEPTION):
                raise IdentityTraceError(f'locked native instruction/function range changed: {rva:x}')
        exports = {item.name.decode(): item.address for item in dll.DIRECTORY_ENTRY_EXPORT.symbols
                   if item.name and not item.forwarder}
        if any(exports.get(name) != rva for name, rva in EXPORTS.items()):
            raise IdentityTraceError('Python export changed or forwarded')
        if dll.get_data(EXPORTS['PyThreadState_GetInterpreter'], 5) != bytes.fromhex('488b4110c3'):
            raise IdentityTraceError('GetInterpreter no longer directly returns tstate->interp')
        if exe.OPTIONAL_HEADER.AddressOfEntryPoint != 0x9100 or exe.FILE_HEADER.TimeDateStamp != 0:
            raise IdentityTraceError('unrecognized final entry')
        base = exe.OPTIONAL_HEADER.ImageBase
        for table, name, function, flags in ((0x2C000, 'read_verified', 0x1940, 8),
                (0x2C020, 'module_reproof', 0x19E0, 8), (0x2C040, 'close', 0x1D20, 4)):
            n, f, actual_flags, doc = struct.unpack('<QQQQ', exe.get_data(table, 32))
            if (exe.get_string_at_rva(n-base) != name.encode() or f != base+function
                    or actual_flags != flags or doc):
                raise IdentityTraceError('actual native method table differs')
        binding = extract_binding(executable, python, approved_api)
        slots = {row['symbol']: row['slot_rva'] for row in binding['bindings']}
        expected = {'PyThreadState_Get': 0x2D000, 'PyThreadState_GetInterpreter': 0x2D008,
                    'PyThread_get_thread_ident': 0x2D010, 'PyErr_SetString': 0x2CF90,
                    'PyExc_RuntimeError': 0x2D0A8}
        if any(slots.get(name) != rva for name, rva in expected.items()):
            raise IdentityTraceError('real E8 binding slot does not match guarded callsite')
    return {'native_code': NATIVE_CODE, 'python_identity_code': '488b4110c3',
            'exports': EXPORTS, 'actual_binding_slots': expected,
            'identity_return_use': 'CMP only; no dereference before EAX boolean overwrite'}


def guarded(conditions, body):
    for condition in reversed(conditions):
        body = f'.if ({condition}) {{ {body} }} .else {{ .echo W3_ID_GUARD_REJECT; }};'
    return body


def render_commands(profile):
    if profile not in PROFILES:
        raise IdentityTraceError('unknown identity mismatch profile')
    chosen = max(1, PROFILES.index(profile))
    def bp(address, body):
        return f'bp {address} "{body}gc"'
    def at(rva, body):
        return bp(f'@$t0+0x{rva:x}', body)
    def py(rva):
        delta = rva - EXPORTS['Py_Initialize']
        return f'python314!Py_Initialize{"+" if delta >= 0 else "-"}0x{abs(delta):x}'
    def printf(text, values):
        return f'.printf \\"{text}\\", {values};.echo;'
    state = ['by(@$t0+0x2d0ba) == 1', 'by(@$t0+0x2d0bc) == 0',
             'poi(@$t0+0x2d0b0) != 0', '@$tid == @$t2']
    take = ['by(@$t0+0x2d0bb) == 0', 'poi(@$t0+0x2d160) == 0',
            'poi(@$t0+0x2d158) != 0', 'by(poi(@$t0+0x2d0b0)+0x231d98) == 1']
    live = ['by(@$t0+0x2d0bb) == 1', '@rcx != 0', '@rcx == poi(@$t0+0x2d160)',
            'by(@rcx+0x10) == 1', 'poi(@rcx+8) == poi(@$t0+0x2d158)',
            'by(poi(@$t0+0x2d0b0)+0x231d98) == 2']
    commands = ['.echo W3_ID_BEGIN', 'r @$t0=@$exentry-0x9100',
                *[f'r @$t{i}=0' for i in range(1, 20)],
                'sxe -c ".echo W3_ID_EXIT;.lastevent;q" epr',
                'sxe -c ".echo W3_ID_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_ID_ACCESS_VIOLATION;.lastevent;gn" av']
    # The inline first take is visited once, not the exported duplicate-take API.
    first = f'r @$t1={py(0)};r @$t2=@$tid;' + printf('W3_ID_PYTHON_BASE %p', '@$t1')
    first += guarded(state+take, '.echo W3_ID_FIRST_TAKE_READY;')
    if chosen == 1:
        first += guarded(state+take, 'r @$t3=1;' + printf(
            'W3_ID_TARGET kind=1 self=%p issued=%p taken=%x minted=0 state=%x armed=%x closed=%x tid=%x',
            '0, poi(@$t0+0x2d160), by(@$t0+0x2d0bb), by(poi(@$t0+0x2d0b0)+0x231d98), by(@$t0+0x2d0ba), by(@$t0+0x2d0bc), @$tid'))
    commands.append(at(0x2994, first))
    for kind, rva in ((2, 0x1940), (3, 0x19E0), (4, 0x1D20)):
        if kind == chosen:
            body = guarded(state+live, f'r @$t3={kind};r @$t7=@rcx;' + printf(
                f'W3_ID_TARGET kind={kind} self=%p issued=%p taken=%x minted=%x state=%x armed=%x closed=%x tid=%x',
                '@rcx, poi(@$t0+0x2d160), by(@$t0+0x2d0bb), by(@rcx+0x10), by(poi(@$t0+0x2d0b0)+0x231d98), by(@$t0+0x2d0ba), by(@$t0+0x2d0bc), @$tid'))
            commands.append(at(rva, f'.if ((@$t3 == 0) & (@$t4 == 0) & (@$tid == @$t2)) {{ {body} }};'))
    def active_body(body):
        return f'.if ((@$t3 != 0) & (@$t4 == 0)) {{ {body} }};'
    commands.append(at(0x39E7, active_body(guarded(
        [f'@rax == @$t1+0x{EXPORTS["PyThread_get_thread_ident"]:x}', '@$tid == @$t2'],
        '.echo W3_ID_THREAD_CALL;'))))
    commands.append(at(0x39ED, active_body(guarded(['@eax == dwo(@$t0+0x2d150)', '@eax != 0'],
        printf('W3_ID_THREAD current=%x owner=%x tid=%x', '@eax, dwo(@$t0+0x2d150), @$tid')))))
    commands.append(at(0x39FC, active_body(guarded(
        [f'@rax == @$t1+0x{EXPORTS["PyThreadState_Get"]:x}'], '.echo W3_ID_TSTATE_CALL;'))))
    commands.append(at(0x3A02, active_body(guarded(['@rax != 0'], 'r @$t6=@rax;'))))
    commands.append(at(0x3A0C, active_body(guarded([
        f'@rax == @$t1+0x{EXPORTS["PyThreadState_GetInterpreter"]:x}', '@rcx == @$t6',
        '@rcx != 0', 'poi(@rcx+0x10) == poi(@$t0+0x2d148)',
        f'poi(@rsp+0x28) == @$t0+0x{0x2999 if chosen == 1 else 0x42DE:x}'],
        printf('W3_ID_STATE tstate=%p actual=%p owner=%p', '@rcx, poi(@rcx+0x10), poi(@$t0+0x2d148)')))))
    returned = guarded(['@$tid == @$t2', '@rax != 0', '@rax != 1',
        '@rax == poi(@$t6+0x10)', '@rax == poi(@$t0+0x2d148)'],
        printf('W3_ID_RETURN original=%p owner=%p', '@rax, poi(@$t0+0x2d148)') +
        ('r @$t4=1;r rax=1;' + printf('W3_ID_INJECT value=%p', '@rax') if profile != 'control' else ''))
    commands.append(at(0x3A12, active_body(returned)))
    commands.append(at(0x3A19, '.if (@$t3 != 0) { '+printf('W3_ID_COMPARE equal=%x', '@zf')+' };'))
    for rva, result in ((0x3A24, 1), (0x3A2B, 0)):
        commands.append(at(rva, '.if (@$t3 != 0) { '+guarded([f'@eax == {result}'],
            printf('W3_ID_OWNER result=%x', '@eax') + ('r @$t3=0;' if profile == 'control' else ''))+' };'))
    commands.append(at(0x2A24, guarded(['by(@$t0+0x2d0bb) == 1', 'poi(@$t0+0x2d160) != 0'], '.echo W3_ID_MINTED;')))
    commands.append(at(0x2D32, '.echo W3_ID_BOOTSTRAP_EVAL;'))
    commands.append(at(0x2D38, '.if (@rax == 0) { .echo W3_ID_EVAL_NULL; } .else { .echo W3_ID_EVAL_OK; };'))
    for rva in (0x199B, 0x1A57, 0x1D33):
        commands.append(at(rva, '.if (@$t4 == 1) { '+guarded(['@rax == 0', '@$tid == @$t2'], '.echo W3_ID_METHOD_NULL;')+' };'))
    expected_return = 0x2D82 if chosen == 1 else 0x433A
    commands.append(bp(py(EXPORTS['PyErr_SetString']), '.if (@$t4 == 1) { '+guarded([
        f'poi(@rsp) == @$t0+0x{expected_return:x}', '@$tid == @$t2',
        f'@rcx == poi(@$t1+0x{EXPORTS["PyExc_RuntimeError"]:x})'],
        printf('W3_ID_ERROR %ma', '@rdx'))+' };'))
    # Rejection must not enter data-plane calls. Cleanup close is intentionally allowed.
    for rva, label in ((0x5980, 'FIND'), (0x7760, 'READ'), (0x6660, 'NATIVE_REPROOF')):
        commands.append(at(rva, f'.if (@$t4 == 1) {{ .echo W3_ID_UNEXPECTED_{label}; }};'))
    commands.append(at(0x2000, '.if ((@$t4 == 1) & (@$t17 == 0)) { r @$t17=1;.echo W3_ID_ABORT; };'))
    commands.append(at(0x203A, '.if ((@$t4 == 1) & (@$t17 == 1)) { '+guarded([
        'by(@$t0+0x2d0ba) == 0', 'by(@$t0+0x2d0bc) == 1', 'poi(@$t0+0x2d0b0) == 0'],
        '.echo W3_ID_REVOKED;r @$t17=2;')+' };'))
    commands.append(at(0x14DF, '.if (@$t4 == 1) { '+guarded(['@$t17 == 2'],
        printf('W3_ID_NATIVE_FAILURE %ma', '@rcx'))+' };'))
    return '\n'.join([*commands, 'g', '']).replace('\\\\"', '\\"')


def validate_trace(output, stderr, profile, *, executable_path, python_path):
    if profile not in PROFILES:
        raise IdentityTraceError('unknown profile')
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable_path).replace('/', '\\')
    known = []
    if output.count(warning+'\n') == 1:
        output = output.replace(warning+'\n', '')
        known.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    if any(token in (output+'\n'+stderr).lower() for token in (
            'warning', 'unable to', "couldn't resolve", 'waitforevent', 'bad syntax', 'syntax error',
            'numeric expression missing', 'range error', 'illegal column count', 'breakpoint expression',
            'access violation', 'memory access error')):
        raise IdentityTraceError('debugger uncertainty')
    lines = [line for line in output.splitlines() if line.startswith(('W3_ID_', 'FROZEN_ENTRY.'))]
    number = lambda text: int(text.replace('`', ''), 16)
    def take(pattern):
        if not lines:
            raise IdentityTraceError('incomplete identity timeline')
        match = re.fullmatch(pattern, lines.pop(0))
        if match is None:
            raise IdentityTraceError('unexpected, missing, or repeated identity event')
        return match
    take('W3_ID_BEGIN'); take('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN')
    base = number(take(r'W3_ID_PYTHON_BASE ([0-9a-fA-F`]+)')[1])
    modules = re.findall(r'^ModLoad: ([0-9a-fA-F`]+) [0-9a-fA-F`]+\s+([^\r\n]*[\\/]python314\.dll)$', output, re.M|re.I)
    normalize = lambda text: text.removeprefix('\\\\?\\').replace('/', '\\').lower()
    if (not base or base % 0x10000 or len(modules) != 1 or number(modules[0][0]) != base
            or normalize(modules[0][1]) != normalize(str(python_path))):
        raise IdentityTraceError('not the packaged Python DLL')
    take('W3_ID_FIRST_TAKE_READY')
    if profile not in ('control', 'first-take'):
        take('W3_ID_MINTED'); take('W3_ID_BOOTSTRAP_EVAL')
    target = take(r'W3_ID_TARGET kind=([1-4]) self=([0-9a-fA-F`]+) issued=([0-9a-fA-F`]+) '
                  r'taken=([01]) minted=([01]) state=([12]) armed=1 closed=0 tid=([0-9a-fA-F]+)')
    kind = max(1, PROFILES.index(profile))
    self_pointer, issued, taken, minted, state, tid = map(number, target.groups()[1:])
    if (int(target[1]) != kind or not tid or (kind == 1 and (self_pointer or issued or taken or minted or state != 1))
            or (kind != 1 and (not issued or self_pointer != issued or taken != 1 or minted != 1 or state != 2))):
        raise IdentityTraceError('target was not an otherwise valid first take or minted object')
    take('W3_ID_THREAD_CALL')
    thread = take(r'W3_ID_THREAD current=([0-9a-fA-F]+) owner=([0-9a-fA-F]+) tid=([0-9a-fA-F]+)')
    current, owner, actual_tid = map(number, thread.groups())
    if not current or current != owner or actual_tid != tid:
        raise IdentityTraceError('thread mismatch masked interpreter guard')
    take('W3_ID_TSTATE_CALL')
    context = take(r'W3_ID_STATE tstate=([0-9a-fA-F`]+) actual=([0-9a-fA-F`]+) owner=([0-9a-fA-F`]+)')
    tstate, actual, interp_owner = map(number, context.groups())
    result = take(r'W3_ID_RETURN original=([0-9a-fA-F`]+) owner=([0-9a-fA-F`]+)')
    original, returned_owner = map(number, result.groups())
    if not tstate or actual <= 1 or len({actual, interp_owner, original, returned_owner}) != 1:
        raise IdentityTraceError('original real identity was not the owner')
    if profile == 'control':
        tail = ['W3_ID_COMPARE equal=1', 'W3_ID_OWNER result=1', 'W3_ID_MINTED',
                'W3_ID_BOOTSTRAP_EVAL', 'W3_ID_EVAL_OK', 'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_ID_EXIT']
        marker = 'FROZEN_ENTRY.SPIKE_COMPLETED'
    else:
        injected = number(take(r'W3_ID_INJECT value=([0-9a-fA-F`]+)')[1])
        if injected != 1 or injected == original:
            raise IdentityTraceError('not the declared single non-NULL mismatch')
        tail = ['W3_ID_COMPARE equal=0', 'W3_ID_OWNER result=0',
                'W3_ID_ERROR FROZEN_ENTRY.'+('HANDOFF_UNAVAILABLE' if kind == 1 else 'AUTHORITY_UNAVAILABLE')]
        if kind != 1:
            tail += ['W3_ID_METHOD_NULL', 'W3_ID_EVAL_NULL']
        marker = 'FROZEN_ENTRY.BOOTSTRAP_EXECUTION_'+('UNAVAILABLE' if kind == 1 else 'FAILED')
        tail += ['W3_ID_ABORT', 'W3_ID_REVOKED', 'W3_ID_NATIVE_FAILURE '+marker, 'W3_ID_EXIT']
    if lines != tail:
        raise IdentityTraceError('rejection, data-plane exclusion, or cleanup timeline differs')
    if stderr != 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n'+marker+'\n':
        raise IdentityTraceError('contradictory or incomplete captured stderr')
    exits = re.findall(r'^Last event: .*: Exit process .*?, code ([0-9a-fA-F]+)\s*$', output, re.M)
    if len(exits) != 1 or int(exits[0], 16) != int(profile != 'control'):
        raise IdentityTraceError('missing real process exit')
    return {'debuggee_exit_code': int(exits[0], 16), 'known_diagnostics': known,
            'real_owner_identity': original, 'real_tstate': tstate, 'thread': tid,
            'issued_object': issued, 'injections': int(profile != 'control'),
            'cross_interpreter_execution_observed': False}


def run(args):
    if os.name != 'nt':
        raise IdentityTraceError('identity observation requires Windows')
    release_bytes = args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes, args.dist,
            expected_release_sha256=args.expected_release_sha256,
            expected_repository_commit=args.expected_commit,
            expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify()
    candidate_path = ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate_bytes = candidate_path.read_bytes()
    candidate = load_candidate(candidate_path)
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise IdentityTraceError('candidate differs from external expected digest')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if (metadata is None or pefile.__version__ != parser_lock['version'] or
            byte_fact(metadata.encode()) != {k:parser_lock['distribution_metadata'][k] for k in ('bytes', 'sha256')}):
        raise IdentityTraceError('unlocked PE parser metadata')
    executable = args.dist/'localcat-spike.exe'
    python = args.dist/'_internal/python314.dll'
    exe_bytes = checked_bytes(executable, release['executable'])
    dll_bytes = checked_bytes(python, candidate['runtime']['cpython']['dll'])
    anchors = verify_machine_sites(exe_bytes, dll_bytes, candidate['entry_contract']['python_c_api'])
    directory = Path(tempfile.mkdtemp(prefix=f'handoff-identity-{args.profile}-', dir=ROOT/'artifacts/windows'))
    (directory/'cwd').mkdir(); (directory/'symbols').mkdir()
    script = directory/'identity.cdb'
    script.write_text(render_commands(args.profile), encoding='ascii', newline='\n')
    def inventory():
        current_metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
        if current_metadata is None:
            raise IdentityTraceError('parser metadata unavailable')
        return {**observer_inventory(args.cdb), Path(__file__).name:byte_fact(Path(__file__).read_bytes()),
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile-METADATA':byte_fact(current_metadata.encode())}
    observers = inventory()
    evidence = {'classification':CLASSIFICATION, 'status':'INCOMPLETE', 'profile':args.profile,
        'release_sha256':args.expected_release_sha256, 'repository_commit':args.expected_commit,
        'candidate_input_digest':args.expected_candidate_digest, 'executable':byte_fact(exe_bytes),
        'python':byte_fact(dll_bytes), 'anchors':anchors, 'observers':observers,
        'script':byte_fact(script.read_bytes()), 'commands':[], 'post_observation_inputs_unchanged':False,
        'limitations':['synthetic API identity mismatch, not a real second interpreter',
            'one exact final PE/Python DLL; no interpreter support or C API added',
            'only one return register changed; software breakpoints alter timing',
            'data-plane exclusion covers selected native entrypoints after injection, not all OS IO',
            'not full HANDOFF_ONE_SHOT or W3 acceptance']}
    print('HANDOFF_IDENTITY_TRACE='+str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), '-y', str(directory/'symbols'), '-i', str(args.dist),
                '-cf', str(script), str(executable)], directory/'cwd', trace_environment(os.environ, 'normal'),
                directory, 'cdb', evidence['commands'], timeout=45)
        except ProbeInputError:
            if args.profile == 'control' or not evidence['commands'] or evidence['commands'][-1].get('exit_code') != 1:
                raise
        evidence['trace'] = validate_trace((directory/'cdb.stdout').read_text(encoding='utf-8'),
            (directory/'cdb.stderr').read_text(encoding='utf-8'), args.profile,
            executable_path=executable, python_path=python)
        verify()
        if (args.release.read_bytes() != release_bytes or candidate_path.read_bytes() != candidate_bytes or
                inventory() != observers or byte_fact(script.read_bytes()) != evidence['script']):
            raise IdentityTraceError('release, candidate, observer, or script changed during execution')
        evidence['post_observation_inputs_unchanged'] = True
        evidence['status'] = 'OBSERVED_CONTROLLED_IDENTITY_PATH'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist', 'release', 'cdb'):
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--profile', choices=PROFILES, default='control')
    run(parser.parse_args())
