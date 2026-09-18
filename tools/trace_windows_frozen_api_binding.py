"""Observe the final packaged E8 binding, with explicit external release anchors.

Only a new debugger child is controlled. The NULL profiles modify one API
return in that child's registers; original on-disk executable/DLL bytes remain
unchanged. This bounded evidence is not the full W3 gate or a signature proof.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import tempfile

from tools.probe_windows_frozen_custom_runw import (
    ROOT, ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_entry import trace_environment
from tools.windows_frozen_api_binding import BindingError, extract_binding, fault_index, validate_trace
from tools.windows_frozen_release import verify_release_binding


def render_commands(table, profile):
    selected = fault_index(table, profile)
    final_slots = ''.join(f'.printf \\"W3_API_FINAL_SLOT {index} value=%p\\", poi(@$t0+0x{row["slot_rva"]:x});.echo;'
                          for index, row in enumerate(table['bindings']))
    commands = [
        '.echo W3_API_BEGIN',
        f'r @$t0=@$exentry-0x{table["executable_entry_rva"]:x}',
        'sxe -c ".echo W3_API_EXIT;.lastevent;q" epr',
        'sxe -c ".echo W3_API_ACCESS_VIOLATION;.lastevent;q" av',
        f'bp @$t0+0x{table["function_rva"]:x} ".printf \\"W3_API_BINDER python=%p\\", @rcx;.echo;gc"',
        f'bp @$t0+0x{table["success_return_rva"]:x} "{final_slots}.printf \\"W3_API_SUCCESS value=%x\\", @eax;.echo;gc"',
        f'bp @$t0+0x{table["missing_rva"]:x} ".echo W3_API_MISSING;gc"',
    ]
    for index, row in enumerate(table['bindings']):
        commands.append(f'bp @$t0+0x{row["call_rva"]:x} ".printf \\"W3_API_CALL {index} base=%p name=%ma\\", @rcx, @rdx;.echo;gc"')
        inject = f'.echo W3_API_INJECT_NULL {index};r rax=0;' if index == selected else ''
        commands.append(f'bp @$t0+0x{row["return_rva"]:x} "{inject}.printf \\"W3_API_RETURN {index} value=%p\\", @rax;.echo;gc"')
        commands.append(f'bp @$t0+0x{row["stored_rva"]:x} ".printf \\"W3_API_SLOT {index} value=%p\\", poi(@$t0+0x{row["slot_rva"]:x});.echo;gc"')
    return '\n'.join([*commands, 'g', ''])


def observer_inventory(cdb):
    names = ('trace_windows_frozen_api_binding.py', 'windows_frozen_api_binding.py',
             'windows_frozen_release.py', 'windows_frozen_packaging.py',
             'probe_windows_frozen_custom_runw.py', 'trace_windows_frozen_entry.py',
             'audit_w3_stock_bootloader.py', 'prepare_windows_frozen_entry_inputs.py',
             'windows_frozen_manifest.py', 'windows_frozen_patch_series.py')
    return {**{name: byte_fact((ROOT / 'tools' / name).read_bytes()) for name in names},
            'cdb.exe': byte_fact(cdb.read_bytes())}


def run(args):
    if os.name != 'nt':
        raise BindingError('actual binding observation requires Windows')
    release_bytes = args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes, args.dist,
                                      expected_release_sha256=args.expected_release_sha256,
                                      expected_repository_commit=args.expected_commit,
                                      expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify()
    candidate = load_candidate(ROOT / 'packaging/windows/frozen-entry/candidate-input.lock.json')
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise BindingError('candidate differs from external expectation')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    distribution = importlib.metadata.distribution('pefile')
    metadata = distribution.read_text('METADATA')
    if (pefile.__version__ != parser_lock['version'] or metadata is None
            or byte_fact(metadata.encode()) != {k: parser_lock['distribution_metadata'][k] for k in ('bytes', 'sha256')}):
        raise BindingError('unlocked pefile parser metadata')
    executable = args.dist / 'localcat-spike.exe'
    pe_bytes = checked_bytes(executable, release['executable'])
    dll_bytes = checked_bytes(args.dist / '_internal/python314.dll', candidate['runtime']['cpython']['dll'])
    table = extract_binding(pe_bytes, dll_bytes, candidate['entry_contract']['python_c_api'])
    directory = Path(tempfile.mkdtemp(prefix=f'api-binding-{args.profile}-', dir=ROOT / 'artifacts/windows'))
    (directory / 'symbols').mkdir()
    (directory / 'cwd').mkdir()
    script = directory / 'binding.cdb'
    script.write_text(render_commands(table, args.profile), encoding='ascii', newline='\n')
    observers = observer_inventory(args.cdb)
    stable = {'schema': 'localcat.windows-frozen-api-binding.v1',
              'classification': 'REALIZED_E8_BINDING_NOT_W3_GATE',
              'release_sha256': args.expected_release_sha256,
              'repository_commit': args.expected_commit,
              'candidate_input_digest': args.expected_candidate_digest,
              'executable': byte_fact(pe_bytes), 'python': byte_fact(dll_bytes), 'decoded': table,
              'signature_authority': 'locked headers and clean build, not observed addresses'}
    (directory / 'binding-table.json').write_bytes(canonical(stable))
    evidence = {'classification': stable['classification'], 'status': 'INCOMPLETE',
                'profile': args.profile, 'table': byte_fact(canonical(stable)),
                'observers': observers, 'script': byte_fact(script.read_bytes()),
                'commands': [], 'post_observation_inputs_unchanged': False}
    print('API_BINDING_TRACE=' + str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), '-y', str(directory / 'symbols'), '-i', str(args.dist),
                            '-cf', str(script), str(executable)],
                           directory / 'cwd', trace_environment(os.environ, 'normal'), directory,
                           'cdb', evidence['commands'], timeout=45)
        except ProbeInputError:
            if args.profile == 'normal' or not evidence['commands'] or evidence['commands'][-1].get('exit_code') != 1:
                raise
        output = (directory / 'cdb.stdout').read_text(encoding='utf-8', errors='strict')
        stderr = (directory / 'cdb.stderr').read_text(encoding='utf-8', errors='strict')
        evidence['trace'] = validate_trace(output, stderr, table, args.profile,
                                           python_path=str(args.dist / '_internal/python314.dll'),
                                           executable_path=str(executable))
        verify()
        if args.release.read_bytes() != release_bytes or observer_inventory(args.cdb) != observers:
            raise BindingError('release or observer changed during observation')
        if byte_fact(script.read_bytes()) != evidence['script']:
            raise BindingError('observer commands changed during observation')
        evidence['post_observation_inputs_unchanged'] = True
        evidence['status'] = 'OBSERVED_EXACT_BINDING_PATH'
    finally:
        (directory / 'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', required=True, type=Path)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--expected-release-sha256', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--expected-candidate-digest', required=True)
    parser.add_argument('--cdb', required=True, type=Path)
    parser.add_argument('--profile', choices=('normal', 'function-null', 'data-null'), default='normal')
    _, evidence = run(parser.parse_args())
    raise SystemExit(0 if evidence['status'] == 'OBSERVED_EXACT_BINDING_PATH' else 1)
