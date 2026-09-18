"""Pair application loader calls with returned HMODULE and debugger module paths.

This observer does not certify E0 or callsite reachability. Initial modules
have already loaded when CDB stops; observed paths are not pre-load authority.
Only a newly created child is controlled. No Windows configuration is changed.
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
    ROOT, ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import PYTHON_LOADER_SITES, PINNED_PYTHON_SHA256, trace_environment
from tools.windows_frozen_release import verify_release_binding


class ResolutionError(ValueError):
    pass


# Independently built payload revisions with identical native code/data layouts.
# A release external anchor remains mandatory; this is not an arbitrary-PE bypass.
EXE_SHAS = frozenset({
    'e0302d378e61e79e4f7eb8c16d88fd7ebc595380aa92eb8371a3c6c6c364d2cf',
    '41f5ffae97c458040d23ad4fa3afcfea43f8ecb0ca1b7e196299b8fcd63ec2f5',
    '2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea',
})
EXE_SITES = {0x6847:'LoadLibraryExW', 0xa24d:'LoadLibraryExW', 0xa285:'LoadLibraryExW',
             0x17061:'LoadLibraryExW', 0x170b5:'LoadLibraryExW'}
BASE_PROFILES = ('normal', 'mimalloc-stats', 'policy-false')
PROFILES = (*BASE_PROFILES, *('acp-1252-'+name for name in BASE_PROFILES))
VC_NAMES = {'api-ms-win-core-synch-l1-2-0','api-ms-win-core-fibers-l1-1-1','kernel32'}
UCRT_NAMES = {'api-ms-win-core-fibers-l1-1-2','kernelbase','api-ms-win-appmodel-runtime-l1-1-2',
              'api-ms-win-core-localization-l1-2-1','kernel32'}
PY_NAMES = {0x18b86a:'kernelbase.dll',0x18b8a8:'ntdll.dll',0x18b8dd:'kernel32.dll',
            0x2b059f:'psapi.dll',0x2b0660:'bcrypt.dll'}


def norm(path):
    return ntpath.normcase(ntpath.normpath(path.removeprefix('\\\\?\\')))


def integer(value):
    return int(value.replace('`',''),16)


def parse_loader_events(output, system32, dist, profile='normal'):
    """Parse only our exact events; pair by thread, callsite and stack pointer."""
    if profile not in PROFILES:
        raise ResolutionError('unknown loader profile')
    acp_profile = profile.startswith('acp-1252-')
    profile = profile.removeprefix('acp-1252-')
    events = [line.strip() for line in output.splitlines()
              if line.strip().startswith(('W3_LD_','FROZEN_ENTRY.')) and not line.strip().startswith('W3_LD_PEB ')]
    if not events or events[0]!='W3_LD_BEGIN' or events[-1]!='W3_LD_EXIT':
        raise ResolutionError('missing observation boundaries')
    blocks = re.findall(r'^W3_LD_HOST_BEGIN\r?\n(.*?)^W3_LD_HOST_END\r?$', output,re.M|re.S)
    hosts=[]
    for block in blocks:
        rows=re.findall(r'^([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+\S+\s+([^\r\n]+)\r?$',block,re.M)
        peb=re.findall(r'^W3_LD_PEB module=([0-9a-fA-F`]+) path=([^\r\n]+)\r?$',block,re.M)
        if len(rows)!=1 or len(peb)!=1 or not ntpath.isabs(peb[0][1]):
            raise ResolutionError('ambiguous or missing returned module path')
        shown=rows[0][2].strip();path=peb[0][1].strip()
        if integer(peb[0][0])!=integer(rows[0][0]) or norm(shown)!=norm(path if ntpath.isabs(shown) else ntpath.basename(path)):
            raise ResolutionError('debugger module and PEB path disagree')
        hosts.append((integer(rows[0][0]),integer(rows[0][1]),path))
    if len(re.findall(r'^W3_LD_PEB ',output,re.M))!=len(hosts):
        raise ResolutionError('unpaired PEB path record')
    stacks={}; calls=[]; policy=False; policy_calls=0; policy_returns=0; entries=0; injected=0
    host_index=0; pending=None; host_open=False; native=[]; acp=[]
    for event in events[1:-1]:
        call=re.fullmatch(r'W3_LD_CALL (exe|python):([0-9a-f]+) tid=([0-9a-f`]+) sp=([0-9a-f`]+) flags=(none|[0-9a-f]+) name=(.+)',event,re.I)
        returned=re.fullmatch(r'W3_LD_RETURN (exe|python):([0-9a-f]+) tid=([0-9a-f`]+) sp=([0-9a-f`]+) module=([0-9a-f`]+)',event,re.I)
        if pending is not None and event not in ('W3_LD_HOST_BEGIN','W3_LD_HOST_END'):
            raise ResolutionError('return is not immediately followed by its host')
        if event=='W3_LD_PE_ENTRY':
            entries+=1
            if entries!=1 or calls or policy_calls:
                raise ResolutionError('wrong PE entry order')
        elif call:
            owner,rva,tid,sp,flags,name=call.groups(); rva=int(rva,16)
            flags=None if flags=='none' else int(flags,16)
            if not entries or owner=='exe' and rva not in EXE_SITES or owner=='python' and rva not in PYTHON_LOADER_SITES:
                raise ResolutionError('unknown loader callsite')
            if owner=='exe' and rva==0x6847:
                if not policy or native or flags!=0x900 or norm(name) not in {
                    norm(ntpath.join(dist,'_internal',dll)) for dll in ('python314.dll','vcruntime140.dll')}:
                    raise ResolutionError('unapproved native dispatcher call')
                kind='bundled'
            elif owner=='exe':
                names=VC_NAMES if rva in (0xa24d,0xa285) else UCRT_NAMES
                primary=rva in (0xa24d,0x17061)
                if name.lower() not in names or flags!=(0x800 if primary else 0):
                    raise ResolutionError('unapproved CRT loader call')
                if not primary and name.lower().startswith(('api-ms-','ext-ms-')):
                    raise ResolutionError('API-set used default search fallback')
                exit_probe=name.lower()=='api-ms-win-appmodel-runtime-l1-1-2'
                terminal=native==['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED'] or profile=='policy-false' and policy_returns==1
                if exit_probe != terminal or not exit_probe and policy_calls:
                    raise ResolutionError('CRT target reached outside its declared stage')
                kind='system'
            else:
                if not policy or flags is not None or PY_NAMES.get(rva)!=name.lower():
                    raise ResolutionError('unapproved Python loader call')
                if rva in (0x18b86a,0x18b8a8,0x18b8dd):
                    parents=stacks.get(integer(tid),[])
                    if native or not parents or parents[-1]['kind']!='bundled' or ntpath.basename(parents[-1]['name']).lower()!='python314.dll':
                        raise ResolutionError('memory initializer is outside Python DLL load')
                kind='system'
            item={'owner':owner,'rva':rva,'thread':integer(tid),'stack':integer(sp),'name':name,
                  'flags':flags,'stage':'after-policy' if policy else 'before-policy','kind':kind}
            stacks.setdefault(item['thread'],[]).append(item); calls.append(item)
        elif returned:
            owner,rva,tid,sp,base=returned.groups(); thread=integer(tid)
            stack=stacks.get(thread,[])
            if not stack:
                raise ResolutionError('unmatched loader return')
            item=stack.pop()
            if (item['owner'],item['rva'],item['stack'])!=(owner,int(rva,16),integer(sp)):
                raise ResolutionError('wrong loader return owner or stack')
            item['module']=integer(base)
            if item['module']:
                pending=item
            else:
                item['host']=None
        elif event=='W3_LD_HOST_BEGIN':
            if pending is None or host_open or host_index>=len(hosts):
                raise ResolutionError('orphan module-path block')
            host_open=True
        elif event=='W3_LD_HOST_END':
            if pending is None or not host_open:
                raise ResolutionError('missing module-path block')
            base,end,path=hosts[host_index];host_index+=1
            if base!=pending['module'] or end<=base:
                raise ResolutionError('resolved path is not the returned HMODULE')
            if pending['kind']=='system':
                if norm(ntpath.dirname(path))!=norm(system32):
                    raise ResolutionError('system call resolved outside System32')
                name=pending['name'].lower()
                if not name.startswith(('api-ms-','ext-ms-')):
                    wanted=name if name.endswith('.dll') else name+'.dll'
                    if ntpath.basename(path).lower()!=wanted:
                        raise ResolutionError('direct system DLL returned another basename')
            elif norm(path)!=norm(pending['name']):
                raise ResolutionError('dispatcher returned another module')
            pending['host']=path;pending=None;host_open=False
        elif event.startswith('W3_LD_ACP_'):
            if not acp_profile or policy_calls or any(stacks.values()) or len(calls)!=3:
                raise ResolutionError('ACP stimulus is outside the fixed pre-policy initializer')
            if not acp:
                match=re.fullmatch(r'W3_LD_ACP_RETURN value=([0-9a-f]+)',event)
                if not match or not 0<int(match[1],16)<=0xffff:
                    raise ResolutionError('missing real ACP return')
            elif event!=('W3_LD_ACP_INJECT value=4e4' if len(acp)==1 else 'W3_LD_ACP_TABLE value=4e4') or len(acp)>2:
                raise ResolutionError('repeated or wrong ACP stimulus/table')
            acp.append(event)
        elif event=='W3_LD_POLICY_CALL flags=800':
            policy_calls+=1
            if policy_calls!=1 or any(stacks.values()) or native:
                raise ResolutionError('invalid policy call order')
        elif event=='W3_LD_POLICY_INJECT_FALSE':
            injected+=1
            if profile!='policy-false' or policy_calls!=1 or policy_returns:
                raise ResolutionError('unexpected policy injection')
        elif event.startswith('W3_LD_POLICY_RETURN value='):
            value=re.fullmatch(r'W3_LD_POLICY_RETURN value=([0-9a-f]+)',event)
            policy_returns+=1
            if not value or policy_calls!=1 or policy_returns!=1:
                raise ResolutionError('invalid policy return')
            policy=int(value[1],16)!=0
            if policy==(profile=='policy-false'):
                raise ResolutionError('policy result disagrees with profile')
        elif event.startswith('FROZEN_ENTRY.'):
            if any(stacks.values()) or not policy or policy_returns!=1:
                raise ResolutionError('Python stage overlaps loader or missing policy')
            native.append(event)
        else:
            raise ResolutionError('unknown or uncertain loader event')
    wanted=[] if profile=='policy-false' else [
        'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED']
    exits=re.findall(r'^Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$',output,re.M)
    terminal_exits=re.findall(r'^W3_LD_EXIT\r?\nLast event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$',output,re.M)
    if (native!=wanted or entries!=1 or policy_returns!=1 or injected!=(profile=='policy-false')
            or any(stacks.values()) or pending or host_open or host_index!=len(hosts)
            or len(exits)!=1 or terminal_exits!=exits or int(exits[0],16)!=(profile=='policy-false')
            or len(acp)!=(3 if acp_profile else 0)):
        raise ResolutionError('incomplete loader/policy/exit timeline')
    return {'calls':calls,'debuggee_exit_code':int(exits[0],16),'native_markers':native,'acp_stimulus':acp,
            'expected_stderr_markers':['FROZEN_ENTRY.DLL_POLICY_FAILED'] if profile=='policy-false' else wanted}


def render_commands(anchors, profile):
    if profile not in PROFILES:
        raise ResolutionError('unknown profile')
    acp_profile=profile.startswith('acp-1252-')
    profile=profile.removeprefix('acp-1252-')
    def py(rva):
        delta=rva-anchors['python_anchor']
        return f'python314!Py_Initialize{"+" if delta>=0 else "-"}0x{abs(delta):x}'
    commands=['.echo W3_LD_BEGIN', f'r @$t0=@$exentry-0x{anchors["entry"]:x}',
              'sxe -c ".echo W3_LD_EXIT;.lastevent;q" epr',
              'sxe -c ".echo W3_LD_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_LD_ACCESS_VIOLATION;.lastevent;gn" av',
              'bp @$exentry ".echo W3_LD_PE_ENTRY;gc"']
    inject=('.echo W3_LD_POLICY_INJECT_FALSE;r rax=0;r rip=@rip+6;'
            '.echo W3_LD_POLICY_RETURN value=0;') if profile=='policy-false' else ''
    commands += ['bp @$t0+0x1037 ".printf \\"W3_LD_POLICY_CALL flags=%x\\", @ecx;.echo;'+inject+'gc"',
                 'bp @$t0+0x103d ".printf \\"W3_LD_POLICY_RETURN value=%x\\", @eax;.echo;gc"']
    if acp_profile:
        commands += ['r @$t4=0',
            'bp @$t0+0x1575b ".if (@$t4==0) {.printf \\"W3_LD_ACP_RETURN value=%x\\", @eax;.echo;r eax=0x4e4;r @$t4=1;.echo W3_LD_ACP_INJECT value=4e4;} .else {.echo W3_LD_ACP_REPEAT;};gc"',
            'bp @$t0+0x1586b ".printf \\"W3_LD_ACP_TABLE value=%x\\", @ecx;.echo;gc"']
    for owner,sites in (('exe',EXE_SITES),('python',PYTHON_LOADER_SITES)):
        for rva,api in sites.items():
            address=f'@$t0+0x{rva:x}' if owner=='exe' else py(rva)
            ret=f'@$t0+0x{rva+6:x}' if owner=='exe' else py(rva+6)
            flag='flags=%x' if api=='LoadLibraryExW' else 'flags=none'
            args=', @r8d' if api=='LoadLibraryExW' else ''
            fmt='%mu' if api.endswith('W') else '%ma'
            bp='bp' if owner=='exe' else 'bu'
            commands.append(f'{bp} {address} ".printf \\"W3_LD_CALL {owner}:{rva:x} tid=%x sp=%p {flag} name={fmt}\\", @$tid, @rsp{args}, @rcx;.echo;gc"')
            # SDK winternl.h x64 PEB/LDR_DATA_TABLE_ENTRY layout. Read only the
            # debuggee's loader list; this is observed metadata, not live-file proof.
            peb=('r @$t1=poi(@$peb+0x18)+0x20;r @$t2=poi(@$t1);r @$t3=0;'
                 '.while (@$t2!=@$t1) {.if (@$t3>=0n256) {.break;};'
                 '.if (poi(@$t2+0x20)==@rax) {'
                 '.printf \\"W3_LD_PEB module=%p path=%msu\\", poi(@$t2+0x20), @$t2+0x38;.echo;};'
                 'r @$t2=poi(@$t2);r @$t3=@$t3+1;};')
            commands.append(f'{bp} {ret} ".printf \\"W3_LD_RETURN {owner}:{rva:x} tid=%x sp=%p module=%p\\", @$tid, @rsp, @rax;.echo;.if (@rax!=0) {{.echo W3_LD_HOST_BEGIN;lmf a @rax;{peb}.echo W3_LD_HOST_END;}};gc"')
    commands+=['.echo W3_INITIAL_MODULES_BEGIN','lmf','.echo W3_INITIAL_MODULES_END','g','']
    return '\n'.join(commands)


def validate_profile_calls(result, profile):
    """Required calls for these exact binary profiles, not an all-path CFG proof."""
    if profile not in PROFILES:
        raise ResolutionError('unknown profile')
    acp_profile=profile.startswith('acp-1252-')
    profile=profile.removeprefix('acp-1252-')
    expected=[('exe',0xa24d,'api-ms-win-core-synch-l1-2-0'),
              ('exe',0xa24d,'api-ms-win-core-fibers-l1-1-1'),
              ('exe',0x17061,'api-ms-win-core-fibers-l1-1-2')]
    if acp_profile:
        expected += [('exe',0x17061,'api-ms-win-core-localization-l1-2-1')]
    if profile!='policy-false':
        expected += [('exe',0x6847,'vcruntime140.dll'),('exe',0x6847,'python314.dll'),
                     ('python',0x18b86a,'kernelbase.dll'),('python',0x18b8a8,'ntdll.dll'),
                     ('python',0x18b8dd,'kernel32.dll')]
    expected += [('exe',0x17061,'api-ms-win-appmodel-runtime-l1-1-2')]
    if profile=='mimalloc-stats':
        expected += [('python',0x2b059f,'psapi.dll')]
    calls=result['calls']
    actual=[(c['owner'],c['rva'],ntpath.basename(c['name']).lower()) for c in calls]
    # The mimalloc RNG cache can already be populated by another in-DLL path.
    # When its loader does run, keep it in the evidence and require E1 policy.
    optional=('python',0x2b0660,'bcrypt.dll')
    if actual.count(optional)>1:
        raise ResolutionError('repeated conditional RNG load')
    if ([row for row in actual if row!=optional]!=expected
            or any(not c.get('module') or not c.get('host') for c in calls)
            or len({c['thread'] for c in calls})!=1):
        raise ResolutionError('incomplete or changed loader profile')


def validate_stderr(stderr, profile):
    if profile not in PROFILES:
        raise ResolutionError('unknown profile')
    profile=profile.removeprefix('acp-1252-')
    lines=[line.strip() for line in stderr.splitlines() if line.strip()]
    wanted=['FROZEN_ENTRY.DLL_POLICY_FAILED'] if profile=='policy-false' else [
        'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED']
    if profile!='mimalloc-stats':
        if lines!=wanted:
            raise ResolutionError('unexpected or contradictory stderr')
        return
    header='heap stats: peak total freed current unit count'
    labels=['reserved','committed','reset','purged','touched','segments','-abandoned','-cached','pages',
            '-abandoned','-extended','-noretire','mmaps','commits','resets','purges','threads','searches',
            'numa nodes','elapsed','process']
    if lines[:2]!=wanted or len(lines)!=3+len(labels) or ' '.join(lines[2].split())!=header:
        raise ResolutionError('unexpected stats output structure')
    for label,line in zip(labels,lines[3:]):
        if not line.startswith(label+':'):
            raise ResolutionError('unexpected stats field')
        values=line[len(label)+1:].strip()
        if label=='process':
            pattern=r'user: [0-9.]+ s, system: [0-9.]+ s, faults: [0-9]+, rss: [0-9.]+ (?:[KMGT]iB|B), commit: [0-9.]+ (?:[KMGT]iB|B)'
        else:
            pattern=r'[0-9.\s]+(?:(?:[KMGT]iB|B|ok|avg|s)\s*)?'
        if re.fullmatch(pattern,values) is None:
            raise ResolutionError('unexpected stats value or extra stderr')


def verify_sites(executable, python):
    if byte_fact(executable)['sha256'] not in EXE_SHAS or byte_fact(python)['sha256']!=PINNED_PYTHON_SHA256:
        raise ResolutionError('loader offsets require the exact final binaries')
    import pefile
    result={}
    for owner,data,sites in (('exe',executable,{**EXE_SITES,0x1037:'SetDefaultDllDirectories',0x15755:'GetACP'}),
                             ('python',python,PYTHON_LOADER_SITES)):
        with pefile.PE(data=data) as pe:
            imports={i.address:i.name.decode() for d in pe.DIRECTORY_ENTRY_IMPORT for i in d.imports if i.name}
            for rva,name in sites.items():
                instruction=pe.get_data(rva,6)
                if instruction[:2]!=b'\xff\x15' or imports.get(pe.OPTIONAL_HEADER.ImageBase+rva+6+struct.unpack('<i',instruction[2:])[0])!=name:
                    raise ResolutionError('loader instruction/IAT changed')
            if owner=='exe':
                if pe.get_data(0x1586b,6)!=bytes.fromhex('81f9e9fd0000'):
                    raise ResolutionError('CRT codepage comparison changed')
                result['entry']=pe.OPTIONAL_HEADER.AddressOfEntryPoint
            else:
                result['python_anchor']=next(s.address for s in pe.DIRECTORY_ENTRY_EXPORT.symbols
                                             if s.name==b'Py_Initialize' and not s.forwarder)
    return result


def run(args):
    if os.name!='nt':
        raise ResolutionError('Windows loader observation required')
    release_bytes=args.release.read_bytes()
    def verify():
        return verify_release_binding(release_bytes,args.dist,expected_release_sha256=args.expected_release_sha256,
            expected_repository_commit=args.expected_commit,expected_candidate_input_digest=args.expected_candidate_digest)
    release=verify()
    candidate=load_candidate(ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json')
    if candidate['candidate_input_digest']!=args.expected_candidate_digest:
        raise ResolutionError('wrong candidate input')
    import pefile
    lock=candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__),lock['source'])
    metadata=importlib.metadata.distribution('pefile').read_text('METADATA')
    if pefile.__version__!=lock['version'] or metadata is None or byte_fact(metadata.encode())!={k:lock['distribution_metadata'][k] for k in ('bytes','sha256')}:
        raise ResolutionError('unlocked PE parser')
    exe=args.dist/'localcat-spike.exe'
    anchors=verify_sites(checked_bytes(exe,release['executable']),
                         checked_bytes(args.dist/'_internal/python314.dll',candidate['runtime']['cpython']['dll']))
    directory=Path(tempfile.mkdtemp(prefix='loader-resolution-'+args.profile+'-',dir=ROOT/'artifacts/windows'))
    (directory/'symbols').mkdir();(directory/'cwd').mkdir()
    script=directory/'loader.cdb'
    script.write_text(render_commands(anchors,args.profile),encoding='ascii',newline='\n')
    def inventory():
        return {**observer_inventory(args.cdb),Path(__file__).name:byte_fact(Path(__file__).read_bytes()),
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile-METADATA':byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode())}
    observers=inventory()
    evidence={'classification':'PAIRED_LOADER_HOST_OBSERVATION_NOT_E0_OR_CALL_CLOSURE_PROOF','status':'INCOMPLETE',
              'release_sha256':args.expected_release_sha256,'repository_commit':args.expected_commit,
              'candidate_input_digest':args.expected_candidate_digest,'profile':args.profile,
              'observers':observers,'script':byte_fact(script.read_bytes()),'anchors':anchors,'commands':[],
              'post_observation_inputs_unchanged':False}
    print('LOADER_RESOLUTION='+str(directory),flush=True)
    try:
        try:
            record_command([str(args.cdb),'-y',str(directory/'symbols'),'-cf',str(script),str(exe)],directory/'cwd',
                           trace_environment(os.environ,'mimalloc-stats' if args.profile.endswith('mimalloc-stats') else 'normal'),
                           directory,'cdb',evidence['commands'],timeout=45)
        except ProbeInputError:
            if not args.profile.endswith('policy-false') or not evidence['commands'] or evidence['commands'][-1].get('exit_code')!=1:
                raise
        output=(directory/'cdb.stdout').read_text(encoding='utf-8')
        stderr=(directory/'cdb.stderr').read_text(encoding='utf-8')
        warning='*** WARNING: Unable to verify timestamp for '+str(exe).replace('/','\\')
        if output.count(warning)==1:
            output=output.replace(warning+'\n','')
        # Remove the one required native diagnostic before checking debugger errors.
        clean=(output+'\n'+stderr).replace('FROZEN_ENTRY.DLL_POLICY_FAILED','')
        if any(word in clean.lower() for word in ('warning','syntax error','couldn\'t resolve','unable to','failed',
            'access violation','numeric expression missing','range error','waitforevent','illegal column count')):
            raise ResolutionError('uncertain debugger observation')
        result=parse_loader_events(output,str(Path(os.environ['SystemRoot'])/'System32'),str(args.dist),args.profile)
        validate_profile_calls(result,args.profile)
        validate_stderr(stderr,args.profile)
        evidence['trace']=result
        verify()
        if args.release.read_bytes()!=release_bytes or inventory()!=observers or byte_fact(script.read_bytes())!=evidence['script']:
            raise ResolutionError('observer inputs changed')
        evidence['post_observation_inputs_unchanged']=True;evidence['status']='OBSERVED_PAIRED_LOADER_HOSTS'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory,evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dist','release','cdb'):
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ('expected-release-sha256','expected-commit','expected-candidate-digest'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--profile',choices=PROFILES,default='normal')
    run(parser.parse_args())
