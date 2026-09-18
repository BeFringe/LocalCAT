"""Observe exact compile buffers and their evaluated code objects in a W3 PE.

Software breakpoints affect timing. Matching evaluation entries do not prove
there were no other evaluations, or replace retained-handle/race verification.
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
    ROOT, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import PINNED_PYTHON_SHA256, trace_environment
from tools.windows_frozen_release import verify_release_binding


class SourceTraceError(ValueError):
    pass


def render_source_commands(entries, content, directory, return_sites, compile_rva):
    heads = [int.from_bytes(data[:8], 'little') for data in content]
    if (len(entries) != len(content) or len(entries) > 7 or not entries
            or any(len(data) < 8 or b'\0' in data for data in content)
            or len(set(heads)) != len(heads)):
        raise SourceTraceError('source capture selectors are incomplete or ambiguous')
    directory = str(directory).replace('\\', '/')
    if any(ch in directory for ch in ('"','\n','\r',';')):
        raise SourceTraceError('unsafe debugger output path')
    commands = ['.echo W3_SOURCE_BEGIN', 'r @$t0=0n99',
                'sxe -c ".echo W3_SOURCE_EXIT;.lastevent;q" epr']
    commands.extend(f'r @$t{10+i}=0' for i in range(len(entries)))
    capture = 'r @$t0=0n99;'
    for i,(entry,head) in enumerate(zip(entries,heads)):
        if re.fullmatch('[a-z0-9-]+',entry['id']) is None:
            raise SourceTraceError('unsafe entry ID')
        capture += (f'.if (poi(@rcx)==0x{head:x}) {{ r @$t0=0n{i};'
                    f'.printf \\"W3_SOURCE_COMPILE {i} input=%p start=%x\\", @rcx, @r8d;.echo;'
                    f'.writemem \\"{directory}/{entry["id"]}.bin\\" @rcx L0n{entry["bytes"]+1}; }};')
    capture += '.if (@$t0==0n99) { .echo W3_SOURCE_UNEXPECTED_COMPILE; };gc'
    commands.append('bu python314!Py_CompileStringObject "'+capture+'"')
    returned = '.printf \\"W3_SOURCE_RETURN id=%d code=%p\\", @$t0, @rax;.echo;'
    for i in range(len(entries)):
        returned += f'.if (@$t0==0n{i}) {{ r @$t{10+i}=@rax; }};'
    for ret in return_sites:
        delta = ret-compile_rva
        address = f'python314!Py_CompileStringObject{"+" if delta>=0 else "-"}0x{abs(delta):x}'
        commands.append(f'bu {address} "{returned}gc"')
    evaluated = ''
    for i in range(len(entries)):
        evaluated += (f'.if (@rcx==@$t{10+i}) {{ .printf \\"W3_SOURCE_EXECUTE {i} code=%p\\", @rcx;'
                      f'.echo;r @$t{10+i}=0; }};')
    commands += [f'bu python314!PyEval_EvalCode "{evaluated}gc"','g','']
    return '\n'.join(commands)


def validate_source_trace(output, stderr, entries, content, captures, *, executable_path):
    known = []
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable_path).replace('/','\\')
    if len(re.findall(re.escape(warning)+r'\r?\n',output)) == 1:
        output = re.sub(re.escape(warning)+r'\r?\n','',output)
        known.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    if any(word in (output+'\n'+stderr).lower() for word in (
            'warning','syntax error','couldn\'t resolve','unable to','failed','access violation',
            'numeric expression missing','range error','waitforevent','illegal column count')):
        raise SourceTraceError('uncertain debugger observation')
    native = ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED']
    if [line.strip() for line in stderr.splitlines() if line.strip().startswith('FROZEN_ENTRY.')] != native:
        raise SourceTraceError('contradictory or incomplete native stderr')
    events = [line.strip() for line in output.splitlines()
              if line.strip().startswith(('W3_SOURCE_','FROZEN_ENTRY.'))]
    if (events[:2] != ['W3_SOURCE_BEGIN',native[0]]
            or events[-2:] != [native[1],'W3_SOURCE_EXIT']
            or len(events) != 4+3*len(entries)
            or set(captures) != {entry['id'] for entry in entries}):
        raise SourceTraceError('incomplete or extra execution events/buffers')
    seen = set()
    for pos in range(2,len(events)-2,3):
        start = re.fullmatch(r'W3_SOURCE_COMPILE (\d+) input=([0-9a-fA-F`]+) start=101',events[pos])
        ret = re.fullmatch(r'W3_SOURCE_RETURN id=(\d+) code=([0-9a-fA-F`]+)',events[pos+1])
        use = re.fullmatch(r'W3_SOURCE_EXECUTE (\d+) code=([0-9a-fA-F`]+)',events[pos+2])
        if not start or not ret or not use:
            raise SourceTraceError('invalid compile/return/evaluation sequence')
        index = int(start[1])
        if (index in seen or not 0 <= index < len(entries) or ret[1] != start[1] or use[1] != start[1]
                or not int(start[2].replace('`',''),16) or not int(ret[2].replace('`',''),16)
                or int(ret[2].replace('`',''),16) != int(use[2].replace('`',''),16)):
            raise SourceTraceError('wrong or repeated returned/evaluated code object')
        seen.add(index)
        if captures[entries[index]['id']] != content[index]+b'\0':
            raise SourceTraceError('compile buffer differs from exact retained source')
    exits = re.findall(r'^Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$',output,re.M)
    if len(exits)!=1 or int(exits[0],16)!=0:
        raise SourceTraceError('missing natural successful process exit')
    return {'executed_entry_ids':[entry['id'] for entry in entries],
            'debuggee_exit_code':0,'known_diagnostics':known}


def verify_compile_sites(dll):
    if byte_fact(dll)['sha256'] != PINNED_PYTHON_SHA256:
        raise SourceTraceError('compile callsites require the pinned Python DLL')
    import pefile
    with pefile.PE(data=dll) as pe:
        exports = {s.name.decode():s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols
                   if s.name and not s.forwarder}
        compile_rva = exports['Py_CompileStringObject']
        return_sites = (0x312cc2,0xd260)
        for ret in return_sites:
            instruction = pe.get_data(ret-5,5)
            if instruction[0] != 0xe8 or ret+struct.unpack('<i',instruction[1:])[0] != compile_rva:
                raise SourceTraceError('native/builtin compile callsite changed')
        return return_sites,compile_rva


def run(args):
    if os.name != 'nt':
        raise SourceTraceError('execution observation requires Windows')
    release_bytes = args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes,args.dist,
            expected_release_sha256=args.expected_release_sha256,
            expected_repository_commit=args.expected_commit,
            expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify()
    prelink = checked_bytes(args.prelink,release['prelink'])
    # These bytes are already bound by the external release digest.
    import json
    entries = [e for e in json.loads(prelink)['entries']
               if e['role'] in ('interpreter','bootstrap','critical-source')]
    if len(entries)!=7:
        raise SourceTraceError('not the approved minimal source closure')
    content = [checked_bytes(args.dist/e['path'],e) for e in entries]
    candidate = load_candidate(ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json')
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise SourceTraceError('current candidate differs from external expectation')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__),parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if (metadata is None or pefile.__version__ != parser_lock['version']
            or byte_fact(metadata.encode()) != {k:parser_lock['distribution_metadata'][k] for k in ('bytes','sha256')}):
        raise SourceTraceError('unlocked PE parser')
    executable = args.dist/'localcat-spike.exe'
    with pefile.PE(data=checked_bytes(executable,release['executable'])) as pe:
        if pe.FILE_HEADER.TimeDateStamp != 0:
            raise SourceTraceError('expected deterministic packaged PE')
    dll_path = args.dist/'_internal/python314.dll'
    return_sites,compile_rva = verify_compile_sites(checked_bytes(dll_path,candidate['runtime']['cpython']['dll']))
    directory = Path(tempfile.mkdtemp(prefix='source-execution-',dir=ROOT/'artifacts/windows'))
    (directory/'cwd').mkdir(); (directory/'symbols').mkdir()
    script = directory/'source.cdb'
    script.write_text(render_source_commands(entries,content,directory,return_sites,compile_rva),encoding='ascii',newline='\n')
    def inventory():
        return {**observer_inventory(args.cdb),Path(__file__).name:byte_fact(Path(__file__).read_bytes()),
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile-METADATA':byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode())}
    observers = inventory()
    evidence = {'classification':'EXACT_SOURCE_EXECUTION_NOT_FULL_W3_GATE','status':'INCOMPLETE',
                'release_sha256':args.expected_release_sha256,'prelink':byte_fact(prelink),
                'observers':observers,'script':byte_fact(script.read_bytes()),'commands':[],
                'post_observation_inputs_unchanged':False}
    print('SOURCE_EXECUTION_TRACE='+str(directory),flush=True)
    try:
        record_command([str(args.cdb),'-y',str(directory/'symbols'),'-cf',str(script),str(executable)],
                       directory/'cwd',trace_environment(os.environ,'normal'),directory,'cdb',evidence['commands'],timeout=45)
        output = (directory/'cdb.stdout').read_text(encoding='utf-8')
        stderr = (directory/'cdb.stderr').read_text(encoding='utf-8')
        modules = re.findall(r'^ModLoad: [0-9a-fA-F`]+ [0-9a-fA-F`]+\s+([^\r\n]*[\\/]python314\.dll)\r?$',output,re.M|re.I)
        def normalize(path):
            return path.removeprefix('\\\\?\\').replace('/','\\').lower()
        if len(modules)!=1 or normalize(modules[0])!=normalize(str(dll_path)):
            raise SourceTraceError('observer did not resolve the packaged Python DLL')
        captures = {e['id']:(directory/(e['id']+'.bin')).read_bytes() for e in entries}
        evidence['trace'] = validate_source_trace(output,stderr,entries,content,captures,executable_path=executable)
        evidence['buffers'] = {name:byte_fact(data) for name,data in captures.items()}
        verify()
        if (args.release.read_bytes()!=release_bytes or args.prelink.read_bytes()!=prelink or inventory()!=observers
                or byte_fact(script.read_bytes())!=evidence['script']):
            raise SourceTraceError('observation input changed')
        evidence['post_observation_inputs_unchanged']=True
        evidence['status']='OBSERVED_EXACT_COMPILES_AND_EVALUATIONS'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory,evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dist','release','prelink','cdb'):
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ('expected-release-sha256','expected-commit','expected-candidate-digest'):
        parser.add_argument('--'+name,required=True)
    run(parser.parse_args())
