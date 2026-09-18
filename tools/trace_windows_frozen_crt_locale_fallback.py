"""Observe the fixed CRT locale fallback using application-visible API returns.

All selected APIs execute before their return registers are changed. This is
not evidence that Windows failed an API or that all locale environments work.
No on-disk application bytes or global system settings are changed. Apart from
debugger breakpoints, only the selected API return registers are written.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import ntpath
import os
from pathlib import Path
import re
import struct
import tempfile

from tools.probe_windows_frozen_custom_runw import (
    ROOT, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.windows_frozen_release import verify_release_binding


class LocaleTraceError(ValueError):
    pass


PROFILES = ('acp-control', 'module-failure', 'flags0', 'lcmap-missing', 'both-missing')
CLASSIFICATION = 'APPLICATION_VISIBLE_CRT_API_RETURN_FAULT_NOT_OS_FAILURE'
TEXT_SHA = 'e6f316d59dc36bfce3cc0a366f2010f09b361d9f0d74eff4f5d8ad66ea98fa8c'
API_SET = 'api-ms-win-core-localization-l1-2-1'
IAT_CALLS = {0x15755:'GetACP', 0x17061:'LoadLibraryExW', 0x170B5:'LoadLibraryExW',
             0x17073:'GetLastError', 0x17192:'GetProcAddress', 0x1730F:'LCMapStringW',
             0x1037:'SetDefaultDllDirectories'}
NATIVE = 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n'


def verify_sites(executable):
    """Anchor the whole native code plus every data/IAT input used by the probe."""
    import pefile
    try:
        with pefile.PE(data=executable) as pe:
            text = [s for s in pe.sections if s.Name.rstrip(b'\0') == b'.text']
            if (len(text) != 1 or byte_fact(text[0].get_data())['sha256'] != TEXT_SHA
                    or pe.OPTIONAL_HEADER.AddressOfEntryPoint != 0x9100 or pe.FILE_HEADER.TimeDateStamp != 0):
                raise LocaleTraceError('unreviewed native code or entry')
            base = pe.OPTIONAL_HEADER.ImageBase
            imports = {i.address:i.name.decode() for d in pe.DIRECTORY_ENTRY_IMPORT for i in d.imports if i.name}
            for rva, name in IAT_CALLS.items():
                code = pe.get_data(rva, 6)
                if code[:2] != b'\xff\x15' or imports.get(base+rva+6+struct.unpack('<i', code[2:])[0]) != name:
                    raise LocaleTraceError('actual call instruction or IAT differs')
            for rva in (0x23AD0, 0x23AE8):
                if struct.unpack('<II', pe.get_data(rva, 8)) != (5, 18):
                    raise LocaleTraceError('locale candidates changed')
            for index, name in ((5, API_SET), (18, 'kernel32')):
                address, = struct.unpack('<Q', pe.get_data(0x234D0+index*8, 8))
                expected = (name+'\0').encode('utf-16le')
                if pe.get_data(address-base, len(expected)) != expected:
                    raise LocaleTraceError('locale target name changed')
            for rva, name in ((0x23AD8, 'LCMapStringEx'), (0x23AF0, 'LocaleNameToLCID')):
                if pe.get_string_at_rva(rva) != name.encode():
                    raise LocaleTraceError('locale symbol name changed')
    except (pefile.PEFormatError, AttributeError, struct.error) as exc:
        raise LocaleTraceError('invalid pinned PE structure') from exc
    return {'native_text_sha256':TEXT_SHA, 'entry':0x9100, 'iat_calls':IAT_CALLS,
            'module_candidates':[API_SET, 'kernel32'], 'scope':'fixed native instructions; external release also required'}


def render_commands(profile):
    if profile not in PROFILES:
        raise LocaleTraceError('unknown locale profile')
    def emit(fmt, values=''):
        return f'.printf \\"{fmt}\\"'+(', '+values if values else '')+';.echo;'
    def guard(condition, body):
        return f'.if ({condition}) {{ {body} }} .else {{ .echo W3_CL_GUARD_REJECT; }};'
    def bp(rva, body, active=True):
        if active:
            body = '.if (@$t1==1) { '+body+' };'
        return f'bp @$t0+0x{rva:x} "{body}gc"'
    same = '@$tid==@$t2'
    host = ('.echo W3_CL_HOST_BEGIN;lmf a @rax;'
        'r @$t16=poi(@$peb+0x18)+0x20;r @$t17=poi(@$t16);r @$t18=0;'
        '.while (@$t17!=@$t16) {.if (@$t18>=0n256) {.break;};'
        '.if ((@rax>=poi(@$t17+0x20)) & (@rax<(poi(@$t17+0x20)+dwo(@$t17+0x30)))) {'+
        emit('W3_CL_PEB module=%p path=%msu', 'poi(@$t17+0x20), @$t17+0x38')+
        '};r @$t17=poi(@$t17);r @$t18=@$t18+1;};.echo W3_CL_HOST_END;')
    commands = ['.echo W3_CL_BEGIN', 'r @$t0=@$exentry-0x9100',
        *[f'r @$t{i}=0' for i in range(1, 20)],
        'u @$t0+0x17061 L1',
        'sxe -c ".echo W3_CL_EXIT;.lastevent;q" epr',
        'sxe -c ".echo W3_CL_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_CL_ACCESS_VIOLATION;.lastevent;gn" av',
        bp(0x9100, 'r @$t2=@$tid;'+emit('W3_CL_ENTRY tid=%x', '@$tid'), False),
        bp(0x1575B, guard(f'({same}) & (@$t19==0) & (@eax!=0)',
            emit('W3_CL_ACP_RETURN tid=%x sp=%p original=%x', '@$tid, @rsp, @eax')+
            'r eax=0x4e4;r @$t19=1;r @$t1=1;.echo W3_CL_ACP_INJECT value=4e4;'), False),
        bp(0x1586B, guard(same, emit('W3_CL_TABLE value=%x', '@ecx'))),
        bp(0x17238, guard(f'({same}) & (@$t4==0)',
            'r @$t3=@$t3+1;r @$t4=@rsp;r @$t5=0;'+
            emit('W3_CL_MAP_BEGIN id=%x tid=%x sp=%p flags=%x locale=%p', '@$t3, @$tid, @rsp, @edx, @rcx')))]
    for site in (0x17061, 0x170B5):
        commands.append(bp(site, guard(f'({same}) & (@$t6==0)',
            f'r @$t6=0x{site:x};r @$t7=@rsp;r @$t8=@rcx;'+
            emit(f'W3_CL_LOAD_CALL site={site:x} tid=%x sp=%p flags=%x name=%mu', '@$tid, @rsp, @r8d, @rcx'))))
        inject = ''
        if site == 0x17061 and profile in ('module-failure', 'flags0'):
            condition = '(@edi==5) & (@$t10==0)'
            if profile == 'flags0': condition = f'({condition}) | ((@edi==0n18) & (@$t10==1))'
            error = 126 if profile == 'module-failure' else 87
            inject = '.if ('+condition+') { '+guard('@$t8==poi(@$t0+0x234d0+@edi*8)',
                f'r rax=0;r @$t10=@$t10+1;r @$t12=0n{error};'+
                emit(f'W3_CL_LOAD_INJECT site={site:x} tid=%x sp=%p value=0', '@$tid, @rsp'))+' };'
        commands.append(bp(site+6, guard(f'({same}) & (@$t6==0x{site:x}) & (@rsp==@$t7) & (@rax!=0)',
            emit(f'W3_CL_LOAD_RETURN site={site:x} tid=%x sp=%p original=%p', '@$tid, @rsp, @rax')+
            host+inject+'r @$t6=0;')))
    commands += [bp(0x17073, guard(f'({same}) & (@$t12!=0) & (@rsp==@$t7)',
        emit('W3_CL_ERROR_CALL tid=%x sp=%p', '@$tid, @rsp'))),
        bp(0x17079, guard(f'({same}) & (@$t12!=0) & (@rsp==@$t7)',
            emit('W3_CL_ERROR_RETURN tid=%x sp=%p original=%x', '@$tid, @rsp, @eax')+
            'r eax=@$t12;'+emit('W3_CL_ERROR_INJECT value=%x', '@eax')+'r @$t12=0;')),
        bp(0x17192, guard(f'({same}) & (@$t6==0) & ((@rdx==@$t0+0x23ad8) | (@rdx==@$t0+0x23af0))',
            'r @$t6=3;r @$t7=@rsp;r @$t8=@rdx;'+
            emit('W3_CL_GPA_CALL tid=%x sp=%p module=%p name=%ma', '@$tid, @rsp, @rcx, @rdx')))]
    gpa_inject = ''
    if profile in ('lcmap-missing', 'both-missing'):
        condition = '(@$t8==@$t0+0x23ad8) & (@$t11==0)'
        if profile == 'both-missing': condition = f'({condition}) | ((@$t8==@$t0+0x23af0) & (@$t11==1))'
        gpa_inject = '.if ('+condition+') { r rax=0;r @$t11=@$t11+1;'+emit(
            'W3_CL_GPA_INJECT name=%ma value=0', '@$t8')+' };'
    commands += [bp(0x17198, guard(f'({same}) & (@$t6==3) & (@rsp==@$t7) & (@rax!=0)',
        emit('W3_CL_GPA_RETURN tid=%x sp=%p original=%p', '@$tid, @rsp, @rax')+host+gpa_inject+'r @$t6=0;')),
        bp(0x172DC, guard(same, 'r @$t5=1;'+emit('W3_CL_DYNAMIC_CALL tid=%x sp=%p pointer=%p', '@$tid, @rsp, @rax'))),
        bp(0x172E1, guard(f'({same}) & (@$t5==1)', emit('W3_CL_DYNAMIC_RETURN tid=%x sp=%p value=%x', '@$tid, @rsp, @eax'))),
        bp(0x172E8, guard(same, 'r @$t5=2;r @$t13=@rsp;'+emit('W3_CL_LCID_CALL tid=%x sp=%p locale=%p', '@$tid, @rsp, @rcx'))),
        bp(0x172ED, guard(f'({same}) & (@$t5==2) & (@rsp==@$t13)', emit('W3_CL_LCID_RETURN tid=%x sp=%p value=%x', '@$tid, @rsp, @eax'))),
        bp(0x17374, guard(same, 'r @$t14=1;'+emit('W3_CL_LCID_DYNAMIC_CALL tid=%x sp=%p pointer=%p', '@$tid, @rsp, @rax'))),
        bp(0x17379, guard(f'({same}) & (@$t14==1)', emit('W3_CL_LCID_DYNAMIC_RETURN tid=%x sp=%p value=%x', '@$tid, @rsp, @eax')+'r @$t14=0;')),
        bp(0x1737E, guard(same, 'r @$t14=2;'+emit('W3_CL_DOWNLEVEL_CALL tid=%x sp=%p locale=%p', '@$tid, @rsp, @rcx'))),
        bp(0x17383, '.if (@$t14==2) { '+guard(same, emit('W3_CL_DOWNLEVEL_RETURN tid=%x sp=%p value=%x', '@$tid, @rsp, @eax')+'r @$t14=0;')+' };'),
        bp(0x1730F, guard(f'({same}) & (@$t5==2)', emit('W3_CL_STATIC_CALL tid=%x sp=%p lcid=%x flags=%x', '@$tid, @rsp, @ecx, @edx'))),
        bp(0x17315, '.if (@$t5==2) { '+guard(same, emit('W3_CL_STATIC_RETURN tid=%x sp=%p value=%x', '@$tid, @rsp, @eax'))+' };'),
        bp(0x17329, guard(f'({same}) & (@rsp==@$t4) & (@$t6==0)',
            emit('W3_CL_MAP_END id=%x tid=%x sp=%p value=%x', '@$t3, @$tid, @rsp, @eax')+'r @$t4=0;')),
        bp(0x1037, guard(f'({same}) & (@$t1==1) & (@$t4==0) & (@$t6==0) & (@$t12==0)',
            emit('W3_CL_E1 maps=%x', '@$t3')+'r @$t1=0;'), False)]
    return '\n'.join([*commands, 'g', ''])


def validate_trace(output, stderr, profile, *, executable_path, system32):
    if profile not in PROFILES:
        raise LocaleTraceError('unknown profile')
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable_path).replace('/', '\\')
    diagnostics = []
    if output.count(warning+'\n') == 1:
        output = output.replace(warning+'\n', '')
        diagnostics.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    if any(word in (output+'\n'+stderr).lower() for word in ('warning', 'unable to', "couldn't", 'cannot', 'failed',
            'syntax error', 'bad syntax', 'waitforevent', 'access violation', 'memory access error',
            'numeric expression missing', 'range error', 'illegal column count', 'breakpoint expression')):
        raise LocaleTraceError('debugger uncertainty')
    raw = output.splitlines()
    hosts = []
    for match in re.finditer(r'^W3_CL_HOST_BEGIN\r?\n(.*?)^W3_CL_HOST_END\r?$', output, re.M|re.S):
        block = match[1]
        rows = re.findall(r'^([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+\S+\s+([^\r\n]+)\r?$', block, re.M)
        peb = re.findall(r'^W3_CL_PEB module=([0-9a-fA-F`]+) path=([^\r\n]+)\r?$', block, re.M)
        if len(rows) != 1 or len(peb) != 1:
            raise LocaleTraceError('ambiguous host path')
        hosts.append((rows[0], peb[0]))
    lines = [line for line in raw if line.startswith(('W3_CL_', 'FROZEN_ENTRY.'))]
    number = lambda value: int(value.replace('`', ''), 16)
    norm = lambda value: ntpath.normcase(ntpath.normpath(value.removeprefix('\\\\?\\')))
    integer = r'([0-9a-fA-F`]+)'
    def take(pattern):
        if not lines:
            raise LocaleTraceError('incomplete locale trace')
        found = re.fullmatch(pattern, lines.pop(0))
        if not found:
            raise LocaleTraceError('unexpected, missing, or repeated locale event')
        return found
    take('W3_CL_BEGIN')
    tid = number(take('W3_CL_ENTRY tid='+integer)[1])
    acp = take('W3_CL_ACP_RETURN tid='+integer+' sp='+integer+' original='+integer)
    if not tid or number(acp[1]) != tid or not number(acp[2]) or not number(acp[3]):
        raise LocaleTraceError('not a real owner ACP return')
    take('W3_CL_ACP_INJECT value=4e4'); take('W3_CL_TABLE value=4e4')
    loader_calls = []; queried = {}; chosen_module = 0
    def host(pointer, *, module):
        take('W3_CL_HOST_BEGIN')
        take('W3_CL_PEB module='+integer+' path=(.+)')
        take('W3_CL_HOST_END')
        if not hosts:
            raise LocaleTraceError('missing raw host block')
        (start, end, shown), (peb_base, path) = hosts.pop(0)
        begin, limit = number(start), number(end)
        if (not begin or not begin <= pointer < limit or module and pointer != begin
                or number(peb_base) != begin or norm(ntpath.dirname(path)) != norm(str(system32))
                or ntpath.basename(path).lower() not in ('kernelbase.dll', 'kernel32.dll')
                or norm(shown.strip()) != norm(path if ntpath.isabs(shown.strip()) else ntpath.basename(path))):
            raise LocaleTraceError('returned address is not the paired System32 host')
    def pair(prefix, tail):
        call = take('W3_CL_'+prefix+'_CALL tid='+integer+' sp='+integer+tail)
        if number(call[1]) != tid or not number(call[2]):
            raise LocaleTraceError('wrong call thread or frame')
        return call
    def returned(prefix, call, field='value'):
        result = take('W3_CL_'+prefix+'_RETURN tid='+integer+' sp='+integer+f' {field}='+integer)
        if result.groups()[:2] != call.groups()[:2]:
            raise LocaleTraceError('return frame does not match real call')
        return number(result[3])
    def load(name, site=0x17061, error=None):
        flags = 0x800 if site == 0x17061 else 0
        call = take(f'W3_CL_LOAD_CALL site={site:x} tid='+integer+' sp='+integer+f' flags={flags:x} name='+re.escape(name))
        result = take(f'W3_CL_LOAD_RETURN site={site:x} tid='+integer+' sp='+integer+' original='+integer)
        if number(call[1]) != tid or not number(call[2]) or result.groups()[:2] != call.groups()[:2] or not number(result[3]):
            raise LocaleTraceError('wrong real loader return pair')
        pointer = number(result[3]); host(pointer, module=True)
        loader_calls.append({'site':site,'name':name,'flags':flags,'original_module':pointer,'injected_error':error})
        if error is not None:
            injected = take(f'W3_CL_LOAD_INJECT site={site:x} tid='+integer+' sp='+integer+' value=0')
            if injected.groups()[:2] != call.groups()[:2]:
                raise LocaleTraceError('loader injection belongs to another call')
            error_call = pair('ERROR', '')
            if error_call.groups()[:2] != call.groups()[:2]:
                raise LocaleTraceError('LastError is not the matching CRT failure branch')
            returned('ERROR', error_call, 'original')
            take(f'W3_CL_ERROR_INJECT value={error:x}')
        return pointer
    def resolve(name, missing):
        call = pair('GPA', ' module='+integer+' name='+re.escape(name))
        if number(call[3]) != chosen_module:
            raise LocaleTraceError('query is not on the first available cached module')
        result = returned('GPA', call, 'original')
        if not result:
            raise LocaleTraceError('selected symbol was not present before injection')
        host(result, module=False)
        if missing:
            take('W3_CL_GPA_INJECT name='+re.escape(name)+' value=0')
        queried[name] = result
        return result
    maps = []; missing = profile in ('lcmap-missing', 'both-missing')
    while lines and lines[0].startswith('W3_CL_MAP_BEGIN '):
        begin = take('W3_CL_MAP_BEGIN id='+integer+' tid='+integer+' sp='+integer+' flags='+integer+' locale='+integer)
        index, owner, stack, flags, locale_name = map(number, begin.groups())
        if index != len(maps)+1 or owner != tid or not stack or flags not in (0x100, 0x200):
            raise LocaleTraceError('wrong map owner, sequence, or flags')
        if not maps:
            error = 126 if profile == 'module-failure' else 87 if profile == 'flags0' else None
            chosen_module = load(API_SET, error=error)
            if error is not None:
                chosen_module = load('kernel32', error=87 if profile == 'flags0' else None)
                if profile == 'flags0': chosen_module = load('kernel32', site=0x170B5)
            resolve('LCMapStringEx', missing)
        if not missing:
            call = pair('DYNAMIC', ' pointer='+integer)
            if number(call[3]) != queried['LCMapStringEx']:
                raise LocaleTraceError('not the real resolved LCMapStringEx pointer')
            value = returned('DYNAMIC', call)
        else:
            conversion = pair('LCID', ' locale='+integer)
            if number(conversion[3]) != locale_name:
                raise LocaleTraceError('locale conversion argument changed')
            if not maps: resolve('LocaleNameToLCID', profile == 'both-missing')
            if profile == 'both-missing':
                call = pair('DOWNLEVEL', ' locale='+integer)
                if number(call[3]) != locale_name: raise LocaleTraceError('downlevel argument changed')
                converted = returned('DOWNLEVEL', call)
            else:
                call = pair('LCID_DYNAMIC', ' pointer='+integer)
                if number(call[3]) != queried['LocaleNameToLCID']: raise LocaleTraceError('wrong conversion pointer')
                converted = returned('LCID_DYNAMIC', call)
            if returned('LCID', conversion) != converted: raise LocaleTraceError('conversion return differs')
            call = pair('STATIC', ' lcid='+integer+' flags='+integer)
            if number(call[3]) != converted or number(call[4]) != flags:
                raise LocaleTraceError('static LCMapStringW arguments differ')
            value = returned('STATIC', call)
        end = take('W3_CL_MAP_END id='+integer+' tid='+integer+' sp='+integer+' value='+integer)
        if list(map(number, end.groups())) != [index, tid, stack, value]:
            raise LocaleTraceError('map return does not follow the observed callee')
        maps.append({'id':index,'flags':flags,'result':value,'locale':locale_name})
    count = number(take('W3_CL_E1 maps='+integer)[1])
    if (count != len(maps) or not 2 <= count <= 4 or [row['flags'] for row in maps] != sorted(row['flags'] for row in maps)
            or set(row['flags'] for row in maps) != {0x100,0x200} or hosts):
        raise LocaleTraceError('incomplete natural casing sequence before E1')
    for flag in (0x100, 0x200):
        casing = [row for row in maps if row['flags'] == flag]
        if len(casing) > 2 or len(casing) == 2 and casing[0]['result'] == 0:
            raise LocaleTraceError('not the fixed CRT size-then-data casing sequence')
    if lines != ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED','W3_CL_EXIT'] or stderr != NATIVE:
        raise LocaleTraceError('incomplete or contradictory natural completion')
    exits = [i for i,line in enumerate(raw) if line.startswith('Last event:')]
    terminal = raw.index('W3_CL_EXIT')
    result = re.fullmatch(r'Last event: .*: Exit process .*?, code ([0-9a-fA-F]+)\s*', raw[exits[0]]) if len(exits)==1 else None
    if result is None or int(result[1],16) != 0 or exits[0] != terminal+1:
        raise LocaleTraceError('missing real successful process exit')
    return {'debuggee_exit_code':0, 'original_acp':number(acp[3]), 'forced_acp':1252,
            'phase':'E0.5_BEFORE_SET_DEFAULT_DLL_DIRECTORIES',
            'map_calls':count, 'maps':maps, 'loader_calls':loader_calls,
            'queried_symbols':list(queried), 'known_diagnostics':diagnostics, 'os_failure_observed':False}


def run(args):
    if os.name != 'nt' or args.profile not in PROFILES:
        raise LocaleTraceError('Windows and an explicit locale profile are required')
    release_bytes = args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes, args.dist,
            expected_release_sha256=args.expected_release_sha256,
            expected_repository_commit=args.expected_commit,
            expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify()
    candidate_path = ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate_bytes = candidate_path.read_bytes(); candidate = load_candidate(candidate_path)
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise LocaleTraceError('candidate differs from external expected digest')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    def parser_metadata():
        value = importlib.metadata.distribution('pefile').read_text('METADATA')
        if value is None: raise LocaleTraceError('parser metadata unavailable')
        return value.encode()
    if (pefile.__version__ != parser_lock['version'] or byte_fact(parser_metadata()) !=
            {key:parser_lock['distribution_metadata'][key] for key in ('bytes','sha256')}):
        raise LocaleTraceError('unlocked parser metadata')
    executable = args.dist/'localcat-spike.exe'
    anchors = verify_sites(checked_bytes(executable, release['executable']))
    directory = Path(tempfile.mkdtemp(prefix=f'crt-locale-{args.profile}-', dir=ROOT/'artifacts/windows'))
    (directory/'cwd').mkdir(); (directory/'symbols').mkdir()
    script = directory/'locale.cdb'; script.write_text(render_commands(args.profile), encoding='ascii', newline='\n')
    def inventory():
        return {**observer_inventory(args.cdb), Path(__file__).name:byte_fact(Path(__file__).read_bytes()),
            'pefile':byte_fact(Path(pefile.__file__).read_bytes()), 'pefile-METADATA':byte_fact(parser_metadata())}
    observers = inventory()
    evidence = {'classification':CLASSIFICATION, 'status':'INCOMPLETE', 'profile':args.profile,
        'release_sha256':args.expected_release_sha256, 'repository_commit':args.expected_commit,
        'candidate_input_digest':args.expected_candidate_digest, 'anchors':anchors,
        'observers':observers, 'script':byte_fact(script.read_bytes()), 'commands':[],
        'post_observation_inputs_unchanged':False,
        'limitations':['APIs really execute before application-visible return registers are replaced',
            'not an OS failure; a successful LoadLibrary may have added a process-local reference',
            'ACP response is controlled; no global locale or system settings change',
            'only fixed pre-E1 locale wrappers; not all CRT, locales, E0, or W3 acceptance',
            'returned system host is observation, not independent pre-load system authority']}
    print('CRT_LOCALE_TRACE='+str(directory), flush=True)
    try:
        record_command([str(args.cdb),'-y',str(directory/'symbols'),'-cf',str(script),str(executable)],
            directory/'cwd',trace_environment(os.environ,'normal'),directory,'cdb',evidence['commands'],timeout=45)
        evidence['trace'] = validate_trace((directory/'cdb.stdout').read_text(encoding='utf8'),
            (directory/'cdb.stderr').read_text(encoding='utf8'),args.profile,executable_path=executable,
            system32=getattr(args,'system32',str(Path(os.environ['SystemRoot'])/'System32')))
        verify()
        if (args.release.read_bytes()!=release_bytes or candidate_path.read_bytes()!=candidate_bytes
                or inventory()!=observers or byte_fact(script.read_bytes())!=evidence['script']):
            raise LocaleTraceError('release, candidate, observer, or script changed during observation')
        evidence['post_observation_inputs_unchanged']=True
        evidence['status']='OBSERVED_CONTROLLED_LOCALE_FALLBACK'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory,evidence


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dist','release','cdb'):
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ('expected-release-sha256','expected-commit','expected-candidate-digest'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--profile',choices=PROFILES,default='acp-control')
    run(parser.parse_args())
