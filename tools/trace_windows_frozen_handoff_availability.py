"""Observe the first-take guard under one controlled native availability fault.

This is a premint state-fault model, not actual early scheduling or a missing
allocation. Only our new child is changed; the original value is restored after
the real take-error branch has entered PyErr_SetString, before normal cleanup.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import trace_windows_frozen_handoff_identity as identity
from tools.probe_windows_frozen_custom_runw import (
    ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors

PROFILES = ('control', 'unarmed-first-take', 'missing-bundle-first-take')
CLASSIFICATION = 'CONTROLLED_PREMINT_AVAILABILITY_STATE_FAULT'
ERROR_RETURN = bytes.fromhex('488b4c2438e874950000eb17')


class AvailabilityError(ValueError):
    pass


def verify_machine_sites(executable, python, approved_api):
    anchors = identity.verify_machine_sites(executable, python, approved_api)
    import pefile
    with pefile.PE(data=executable) as pe:
        if (pe.get_data(0x2D82, len(ERROR_RETURN)) != ERROR_RETURN or not any(
                row.struct.BeginAddress <= 0x2D82 and 0x2D82+len(ERROR_RETURN) <= row.struct.EndAddress
                for row in pe.DIRECTORY_ENTRY_EXCEPTION)):
            raise AvailabilityError('first-take error return/cleanup instruction changed')
    return {**anchors, 'take_error_return': ERROR_RETURN.hex(),
            'fault_globals': {'bundle': 0x2D0B0, 'armed': 0x2D0BA},
            'fault_model': CLASSIFICATION}


def guarded(conditions, body):
    for condition in reversed(conditions):
        body = f'.if ({condition}) {{ {body} }} .else {{ .echo W3_HA_GUARD_REJECT; }};'
    return body


def render_commands(profile):
    if profile not in PROFILES:
        raise AvailabilityError('unknown availability profile')
    def at(rva, body):
        return f'bp @$t0+0x{rva:x} "{body}gc"'
    def py(rva):
        delta = rva-identity.EXPORTS['Py_Initialize']
        return f'python314!Py_Initialize{"+" if delta >= 0 else "-"}0x{abs(delta):x}'
    def printf(text, values):
        return f'.printf \\"{text}\\", {values};.echo;'
    ready = ['@$t6 == 0', 'by(@$t0+0x2d0ba) == 1', 'by(@$t0+0x2d0bc) == 0',
             'poi(@$t0+0x2d0b0) != 0', 'poi(@$t0+0x2d158) != 0',
             'poi(@$t0+0x2d160) == 0', 'by(@$t0+0x2d0bb) == 0',
             'by(poi(@$t0+0x2d0b0)+0x231d98) == 1', '@$tid == dwo(@$t0+0x2d150)']
    first = (f'r @$t1={py(0)};r @$t2=@$tid;r @$t3=poi(@$t0+0x2d0b0);'
             'r @$t4=poi(@$t0+0x2d158);r @$t6=1;' + printf('W3_HA_PYTHON_BASE %p', '@$t1') +
             printf('W3_HA_READY bundle=%p type=%p issued=%p taken=%x state=%x armed=%x closed=%x tid=%x owner=%x',
                    '@$t3, @$t4, poi(@$t0+0x2d160), by(@$t0+0x2d0bb), by(@$t3+0x231d98), '
                    'by(@$t0+0x2d0ba), by(@$t0+0x2d0bc), @$tid, dwo(@$t0+0x2d150)'))
    if profile == 'unarmed-first-take':
        first += 'eb @$t0+0x2d0ba 0;r @$t6=2;' + printf('W3_HA_INJECT field=armed original=1 value=%x', 'by(@$t0+0x2d0ba)')
    elif profile == 'missing-bundle-first-take':
        first += 'eq @$t0+0x2d0b0 0;r @$t6=2;' + printf('W3_HA_INJECT field=bundle original=%p value=%p', '@$t3, poi(@$t0+0x2d0b0)')
    commands = ['.echo W3_HA_BEGIN', 'r @$t0=@$exentry-0x9100',
                *[f'r @$t{i}=0' for i in range(1, 8)],
                'sxe -c ".echo W3_HA_EXIT;.lastevent;q" epr',
                'sxe -c ".echo W3_HA_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_HA_ACCESS_VIOLATION;.lastevent;gn" av',
                at(0x2994, guarded(ready, first))]
    for rva, result in ((0x3A24, 1), (0x3A2B, 0)):
        body = guarded(['@$tid == @$t2', f'@eax == {result}'],
                       printf('W3_HA_OWNER result=%x', '@eax') + ('r @$t6=0;' if profile == 'control' else ''))
        commands.append(at(rva, f'.if ((@$t6 == 1) | (@$t6 == 2)) {{ {body} }};'))
    commands += [at(0x2A24, guarded(['by(@$t0+0x2d0bb) == 1', 'poi(@$t0+0x2d160) != 0'], '.echo W3_HA_MINTED;')),
                 at(0x2D32, '.echo W3_HA_BOOTSTRAP_EVAL;'),
                 at(0x2D38, '.if (@rax == 0) { .echo W3_HA_EVAL_NULL; } .else { .echo W3_HA_EVAL_OK; };')]
    if profile == 'control':
        commands.append(at(0x2D82, '.echo W3_HA_UNEXPECTED_TAKE_ERROR;'))
    if profile != 'control':
        common = ['@$t6 == 2', '@$tid == @$t2', 'poi(@rsp) == @$t0+0x2d82',
                  f'@rcx == poi(@$t1+0x{identity.EXPORTS["PyExc_RuntimeError"]:x})',
                  'poi(@$t0+0x2d160) == 0', 'by(@$t0+0x2d0bb) == 0',
                  'poi(@$t0+0x2d158) == @$t4', 'by(@$t0+0x2d0bc) == 0',
                  'by(@$t3+0x231d98) == 1']
        if profile == 'unarmed-first-take':
            common += ['by(@$t0+0x2d0ba) == 0', 'poi(@$t0+0x2d0b0) == @$t3']
            restore = 'eb @$t0+0x2d0ba 1;' + printf('W3_HA_RESTORE field=armed value=%x', 'by(@$t0+0x2d0ba)')
        else:
            common += ['poi(@$t0+0x2d0b0) == 0', 'by(@$t0+0x2d0ba) == 1']
            restore = 'eq @$t0+0x2d0b0 @$t3;' + printf('W3_HA_RESTORE field=bundle value=%p', 'poi(@$t0+0x2d0b0)')
        error = guarded(common, printf('W3_HA_ERROR %ma', '@rdx')+restore+'r @$t6=3;')
        commands.append(f'bp {py(identity.EXPORTS["PyErr_SetString"])} ".if (@$t6 == 2) {{ {error} }};gc"')
        intact = ['@$tid == @$t2', 'by(@$t0+0x2d0ba) == 1', 'by(@$t0+0x2d0bc) == 0',
                  'poi(@$t0+0x2d0b0) == @$t3', 'poi(@$t0+0x2d160) == 0',
                  'by(@$t0+0x2d0bb) == 0', 'by(@$t3+0x231d98) == 1']
        commands.append(at(0x2D82, guarded(['@$t6 == 3', *intact], '.echo W3_HA_ERROR_SET;r @$t6=4;')))
        commands.append(at(0x2000, '.if (@$t6 == 4) { '+guarded(intact, '.echo W3_HA_ABORT;r @$t6=5;')+' };'))
        commands.append(at(0x203A, '.if (@$t6 == 5) { '+guarded([
            '@$tid == @$t2', 'by(@$t0+0x2d0ba) == 0', 'by(@$t0+0x2d0bc) == 1',
            'poi(@$t0+0x2d0b0) == 0', 'poi(@$t0+0x2d160) == 0', 'by(@$t0+0x2d0bb) == 0'],
            '.echo W3_HA_REVOKED;r @$t6=6;')+' };'))
        commands.append(at(0x14DF, guarded(['@$t6 == 6'], printf('W3_HA_NATIVE_FAILURE %ma', '@rcx'))))
        for rva, name in ((0x5980, 'FIND'), (0x7760, 'READ'), (0x6660, 'NATIVE_REPROOF')):
            commands.append(at(rva, f'.if (@$t6 >= 2) {{ .echo W3_HA_UNEXPECTED_{name}; }};'))
    return '\n'.join([*commands, 'g', '']).replace('\\\\"', '\\"')


def validate_trace(output, stderr, profile, *, executable_path, python_path):
    if profile not in PROFILES:
        raise AvailabilityError('unknown profile')
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable_path).replace('/', '\\')
    known = []
    if output.count(warning+'\n') == 1:
        output = output.replace(warning+'\n', '')
        known.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    if any(token in (output+'\n'+stderr).lower() for token in (
            'warning', 'unable to', "couldn't resolve", 'waitforevent', 'bad syntax', 'syntax error',
            'numeric expression missing', 'range error', 'illegal column count', 'breakpoint expression',
            'failed to set breakpoint', "couldn't insert breakpoint", 'cannot continue execution',
            'access violation', 'memory access error')):
        raise AvailabilityError('uncertain debugger observation')
    lines = [line for line in output.splitlines() if line.startswith(('W3_HA_', 'FROZEN_ENTRY.'))]
    number = lambda value: int(value.replace('`', ''), 16)
    def take(pattern):
        match = re.fullmatch(pattern, lines.pop(0)) if lines else None
        if match is None:
            raise AvailabilityError('missing, repeated or out-of-order availability event')
        return match
    take('W3_HA_BEGIN'); take('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN')
    base = number(take(r'W3_HA_PYTHON_BASE ([0-9a-fA-F`]+)')[1])
    modules = list(re.finditer(r'^ModLoad: ([0-9a-fA-F`]+) [0-9a-fA-F`]+\s+([^\r\n]*[\\/]python314\.dll)$', output, re.M|re.I))
    initialization = re.search(r'^FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN$', output, re.M)
    normalize = lambda value: value.removeprefix('\\\\?\\').replace('/', '\\').lower()
    if (not base or base % 0x10000 or len(modules) != 1 or number(modules[0][1]) != base
            or normalize(modules[0][2]) != normalize(str(python_path))
            or initialization is None or modules[0].start() >= initialization.start()):
        raise AvailabilityError('not the packaged Python DLL')
    ready = take(r'W3_HA_READY bundle=([0-9a-fA-F`]+) type=([0-9a-fA-F`]+) issued=([0-9a-fA-F`]+) '
                 r'taken=0 state=1 armed=1 closed=0 tid=([0-9a-fA-F]+) owner=([0-9a-fA-F]+)')
    bundle, type_pointer, issued, thread, owner = map(number, ready.groups())
    if not bundle or not type_pointer or issued or not thread or thread != owner:
        raise AvailabilityError('not an otherwise ready unissued first take')
    if profile == 'control':
        tail = ['W3_HA_OWNER result=1', 'W3_HA_MINTED', 'W3_HA_BOOTSTRAP_EVAL', 'W3_HA_EVAL_OK',
                'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_HA_EXIT']
        marker = 'FROZEN_ENTRY.SPIKE_COMPLETED'
    else:
        field = 'armed' if profile == 'unarmed-first-take' else 'bundle'
        original, injected = map(number, take(r'W3_HA_INJECT field='+field+r' original=([0-9a-fA-F`]+) value=([0-9a-fA-F`]+)').groups())
        # Read actual written zero from the event, not just the requested write.
        if injected != 0 or original != (1 if field == 'armed' else bundle):
            raise AvailabilityError('not the declared single availability mutation')
        take('W3_HA_OWNER result=0'); take('W3_HA_ERROR FROZEN_ENTRY.HANDOFF_UNAVAILABLE')
        if number(take(r'W3_HA_RESTORE field='+field+r' value=([0-9a-fA-F`]+)')[1]) != original:
            raise AvailabilityError('cleanup did not regain the exact original state')
        marker = 'FROZEN_ENTRY.BOOTSTRAP_EXECUTION_UNAVAILABLE'
        tail = ['W3_HA_ERROR_SET', 'W3_HA_ABORT', 'W3_HA_REVOKED', 'W3_HA_NATIVE_FAILURE '+marker, 'W3_HA_EXIT']
    if lines != tail or stderr != 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n'+marker+'\n':
        raise AvailabilityError('unexpected mint/evaluation/data-plane/cleanup or stderr')
    pattern = r'Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$'
    exits = re.findall('^'+pattern, output, re.M)
    terminal = re.findall(r'^W3_HA_EXIT\r?\n'+pattern, output, re.M)
    if (len(exits) != 1 or terminal != exits or len(re.findall(r'^Last event:', output, re.M)) != 1
            or int(exits[0], 16) != int(profile != 'control')):
        raise AvailabilityError('missing or nonterminal natural process exit')
    return {'debuggee_exit_code': int(exits[0], 16), 'known_diagnostics': known,
            'bundle': bundle, 'type': type_pointer, 'thread': thread, 'injections': int(profile != 'control'),
            'actual_early_scheduling_observed': False, 'authority_issued_before_fault': False}


def observers(cdb):
    import pefile
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if metadata is None:
        raise AvailabilityError('PE parser metadata unavailable')
    return {**observer_inventory(cdb), Path(__file__).name: byte_fact(Path(__file__).read_bytes()),
            Path(identity.__file__).name: byte_fact(Path(identity.__file__).read_bytes()),
            'pefile': byte_fact(Path(pefile.__file__).read_bytes()), 'pefile-METADATA': byte_fact(metadata.encode())}


def run(args):
    if os.name != 'nt':
        raise AvailabilityError('actual availability observation requires Windows')
    for name in ('dist', 'release', 'cdb'):
        path = Path(getattr(args, name)).absolute(); _no_reparse_ancestors(path); setattr(args, name, path)
    release_bytes = args.release.read_bytes()
    external = dict(expected_release_sha256=args.expected_release_sha256,
                    expected_repository_commit=args.expected_commit, expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify_release_binding(release_bytes, args.dist, **external)
    candidate_path = ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate_bytes = candidate_path.read_bytes(); candidate = load_candidate(candidate_path)
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise AvailabilityError('candidate differs from external expected digest')
    import pefile
    lock = candidate['evidence_producer']['pefile']; checked_bytes(Path(pefile.__file__), lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if (metadata is None or pefile.__version__ != lock['version'] or
            byte_fact(metadata.encode()) != {k:lock['distribution_metadata'][k] for k in ('bytes', 'sha256')}):
        raise AvailabilityError('unlocked PE parser')
    executable = args.dist/'localcat-spike.exe'; python = args.dist/'_internal/python314.dll'
    exe_bytes = checked_bytes(executable, release['executable'])
    dll_bytes = checked_bytes(python, candidate['runtime']['cpython']['dll'])
    anchors = verify_machine_sites(exe_bytes, dll_bytes, candidate['entry_contract']['python_c_api'])
    directory = Path(tempfile.mkdtemp(prefix='handoff-availability-'+args.profile+'-', dir=ROOT/'artifacts/windows'))
    (directory/'cwd').mkdir(); (directory/'symbols').mkdir()
    script = directory/'availability.cdb'; script.write_text(render_commands(args.profile), encoding='ascii', newline='\n')
    before = observers(args.cdb)
    evidence = {'classification': CLASSIFICATION, 'status': 'INCOMPLETE', 'profile': args.profile,
                'anchors': external, 'machine_anchors': anchors, 'executable': byte_fact(exe_bytes),
                'python': byte_fact(dll_bytes), 'observers': before, 'script': byte_fact(script.read_bytes()),
                'commands': [], 'post_observation_inputs_unchanged': False,
                'limitations': ['controlled premint availability state faults, not actual early scheduling or lost allocation',
                    'one native field is restored only after the irreversible real take-error branch for normal cleanup',
                    'software breakpoints alter timing; no cross-process or additional interpreter support',
                    'selected native data-plane entrypoints observed, not all OS IO; not full W3 acceptance']}
    print('HANDOFF_AVAILABILITY_TRACE='+str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), '-y', str(directory/'symbols'), '-i', str(args.dist),
                            '-cf', str(script), str(executable)], directory/'cwd', trace_environment(os.environ, 'normal'),
                           directory, 'cdb', evidence['commands'], timeout=45)
        except ProbeInputError:
            if args.profile == 'control' or not evidence['commands'] or evidence['commands'][-1].get('exit_code') != 1:
                raise
        if evidence['commands'][-1]['exit_code'] != int(args.profile != 'control'):
            raise AvailabilityError('unexpected debugger exit')
        evidence['trace'] = validate_trace((directory/'cdb.stdout').read_text(encoding='utf-8'),
            (directory/'cdb.stderr').read_text(encoding='utf-8'), args.profile, executable_path=executable, python_path=python)
        verify_release_binding(release_bytes, args.dist, **external)
        if (args.release.read_bytes() != release_bytes or candidate_path.read_bytes() != candidate_bytes
                or observers(args.cdb) != before or byte_fact(script.read_bytes()) != evidence['script']):
            raise AvailabilityError('release, candidate, observers or script changed')
        evidence['post_observation_inputs_unchanged'] = True
        evidence['status'] = 'OBSERVED_CONTROLLED_PREMINT_PATH'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist', 'release', 'cdb'):
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--profile', choices=PROFILES, default='control')
    args = parser.parse_args(argv)
    try:
        _, evidence = run(args)
    except (OSError, ValueError, SystemExit) as exc:
        print('HANDOFF_AVAILABILITY_REJECTED: '+str(exc), file=sys.stderr); return 1
    return 0 if evidence['status'] == 'OBSERVED_CONTROLLED_PREMINT_PATH' else 1


if __name__ == '__main__':
    raise SystemExit(main())
