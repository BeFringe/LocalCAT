"""Exercise the fixed pre-E1 CRT flags=0 fallback against six native bait layouts.

The first two LoadLibraryExW calls execute successfully before their visible
returns/LastError are changed. This is not a natural OS load failure or an
unmapped-system-DLL experiment. No runtime bytes or OS settings are modified.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import math
import ntpath
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import trace_windows_frozen_crt_locale_fallback as locale
from tools import verify_windows_frozen_loader_canary as canary
from tools.probe_windows_frozen_custom_runw import byte_fact, canonical, checked_bytes, load_candidate
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.verify_windows_frozen_payload import inventory, EXE, MANIFEST, PROFILE
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors

LOCATIONS = ('cwd', 'path', 'appdir', 'local-file', 'local-dir', 'sxs-external')
DIST_POLLUTION = canary.DIST_POLLUTION
CLASSIFICATION = 'FIXED_CRT_FLAGS0_BAIT_COMBINATION_NOT_OS_FAILURE_OR_FULL_W3'
LIMITATIONS = [
    'Only the fixed pre-E1 locale flags=0 kernel32 fallback and six application-controlled layouts.',
    'Two real flags=0x800 loads succeed before NULL/87 is injected; modules may already be mapped/referenced.',
    'Actual GetACP return is changed once to 1252, not an OS locale/configuration change.',
    'No general loader/race/OS-internal claim; returned hosts are metadata, not retained file authority.',
    'Neutral .local/private-SxS controls prove redirection fixtures work, not universal system-name semantics.',
    'Dist additions must still fail the existing inventory after the real flags=0 fallback; no embedded manifest change.',
]


class Flags0Error(ValueError):
    pass


def render_commands():
    commands = locale.render_commands('flags0')
    extra = [
        'bp @$t0+0x103d ".if (@$tid==@$t2) {.printf \\"W3_FC_POLICY_RETURN tid=%x value=%x\\", @$tid, @eax;.echo;} .else {.echo W3_FC_GUARD_REJECT;};gc"',
        'bp @$t0+0x7e78 ".if (@$tid==@$t2) {.printf \\"W3_FC_DIAGNOSTIC tid=%x owner=7e78 bytes=%x text=%ma\\", @$tid, @r8d, @rdx;.echo;} .else {.echo W3_FC_GUARD_REJECT;};gc"',
    ]
    if not commands.endswith('g\n'):
        raise Flags0Error('locale script has no unique final continue')
    return commands[:-2]+'\n'.join(extra)+'\ng\n'


def dist_additions(location, dll_bytes):
    if location not in LOCATIONS:
        raise Flags0Error('unknown fixed layout')
    if location in canary.REDIRECTIONS:
        return canary.redirection_layout(location, EXE, canary.BAIT_NAMES, dll_bytes)
    return {name:dll_bytes for name in canary.BAIT_NAMES} if location == 'appdir' else {}


def validate_trace(output, stderr, location, *, executable_path, system32, bait_paths):
    """Parse the actual flags0 prefix AND actual rejection/success tail; no synthetic events."""
    if location not in LOCATIONS:
        raise Flags0Error('unknown fixed layout')
    canary.reject_canary(output, stderr, bait_paths)
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable_path).replace('/', '\\')+'\n'
    diagnostics = []
    if output.count(warning) == 1:
        output = output.replace(warning, '')
        diagnostics.append('CDB_TIMESTAMP_UNAVAILABLE_FOR_EXTERNALLY_HASHED_ZERO_TIMESTAMP_PE')
    if any(word in (output+'\n'+stderr).lower() for word in (
            'warning', 'unable to', "couldn't", 'cannot', 'failed', 'syntax error', 'bad syntax',
            'waitforevent', 'access violation', 'memory access error', 'numeric expression missing',
            'range error', 'illegal column count', 'breakpoint expression')):
        raise Flags0Error('uncertain debugger observation')
    hosts = re.findall(r'^W3_CL_HOST_BEGIN\r?\n(.*?)^W3_CL_HOST_END\r?$', output, re.M|re.S)
    lines = [s for s in output.splitlines() if s.startswith(('W3_', 'FROZEN_ENTRY.'))]
    integer = r'([0-9a-fA-F`]+)'
    number = lambda text: int(text.replace('`', ''), 16)
    def take(pattern):
        found = re.fullmatch(pattern, lines.pop(0)) if lines else None
        if found is None:
            raise Flags0Error('missing, repeated, or out-of-order flags0 event')
        return found
    def host(pointer, module):
        take('W3_CL_HOST_BEGIN'); take('W3_CL_PEB module='+integer+' path=(.+)'); take('W3_CL_HOST_END')
        if not hosts:
            raise Flags0Error('missing actual host metadata')
        raw = hosts.pop(0)
        rows = re.findall(r'^([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+\S+\s+([^\r\n]+)\r?$', raw, re.M)
        peb = re.findall(r'^W3_CL_PEB module=([0-9a-fA-F`]+) path=([^\r\n]+)\r?$', raw, re.M)
        if len(rows) != 1 or len(peb) != 1:
            raise Flags0Error('ambiguous host metadata')
        start, end, shown = rows[0]; base, path = peb[0]; begin, limit = number(start), number(end)
        if (not begin or not begin <= pointer < limit or module and pointer != begin or number(base) != begin
                or canary.norm(ntpath.dirname(path)) != canary.norm(str(system32))
                or ntpath.basename(path).lower() not in ('kernelbase.dll','kernel32.dll')
                or canary.norm(shown.strip()) != canary.norm(path if ntpath.isabs(shown.strip()) else ntpath.basename(path))):
            raise Flags0Error('return is not its paired System32 host')
        return path
    take('W3_CL_BEGIN')
    tid = number(take('W3_CL_ENTRY tid='+integer)[1])
    acp = take('W3_CL_ACP_RETURN tid='+integer+' sp='+integer+' original='+integer)
    if not tid or number(acp[1]) != tid or not number(acp[2]) or not number(acp[3]):
        raise Flags0Error('not an actual owner ACP return')
    take('W3_CL_ACP_INJECT value=4e4'); take('W3_CL_TABLE value=4e4')
    calls = []; maps = []; pointer = None
    while lines and lines[0].startswith('W3_CL_MAP_BEGIN '):
        begin = take('W3_CL_MAP_BEGIN id='+integer+' tid='+integer+' sp='+integer+' flags='+integer+' locale='+integer)
        index, owner, stack, flag, locale_name = map(number, begin.groups())
        if index != len(maps)+1 or owner != tid or not stack or flag not in (0x100,0x200):
            raise Flags0Error('wrong natural casing operation')
        if not maps:
            for site, name, flags, inject in ((0x17061,locale.API_SET,0x800,True),
                    (0x17061,'kernel32',0x800,True),(0x170B5,'kernel32',0,False)):
                call = take(f'W3_CL_LOAD_CALL site={site:x} tid='+integer+' sp='+integer+f' flags={flags:x} name='+re.escape(name))
                ret = take(f'W3_CL_LOAD_RETURN site={site:x} tid='+integer+' sp='+integer+' original='+integer)
                if number(call[1]) != tid or not number(call[2]) or ret.groups()[:2] != call.groups() or not number(ret[3]):
                    raise Flags0Error('not a paired real loader call')
                module = number(ret[3]); path = host(module, True)
                calls.append({'site':site,'name':name,'flags':flags,'original_module':module,'host':path})
                if inject:
                    changed = take(f'W3_CL_LOAD_INJECT site={site:x} tid='+integer+' sp='+integer+' value=0')
                    error = take('W3_CL_ERROR_CALL tid='+integer+' sp='+integer)
                    error_return = take('W3_CL_ERROR_RETURN tid='+integer+' sp='+integer+' original='+integer)
                    if changed.groups() != call.groups() or error.groups() != call.groups() or error_return.groups()[:2] != call.groups():
                        raise Flags0Error('NULL/LastError stimulus belongs to another frame')
                    take('W3_CL_ERROR_INJECT value=57')
            gpa = take('W3_CL_GPA_CALL tid='+integer+' sp='+integer+' module='+integer+' name=LCMapStringEx')
            ret = take('W3_CL_GPA_RETURN tid='+integer+' sp='+integer+' original='+integer)
            if (number(gpa[1]) != tid or not number(gpa[2]) or number(gpa[3]) != module
                    or ret.groups()[:2] != gpa.groups()[:2] or not number(ret[3])):
                raise Flags0Error('not the actual flags0 module symbol resolution')
            pointer = number(ret[3]); host(pointer, False)
        call = take('W3_CL_DYNAMIC_CALL tid='+integer+' sp='+integer+' pointer='+integer)
        ret = take('W3_CL_DYNAMIC_RETURN tid='+integer+' sp='+integer+' value='+integer)
        if number(call[1]) != tid or not number(call[2]) or number(call[3]) != pointer or ret.groups()[:2] != call.groups()[:2]:
            raise Flags0Error('not the returned LCMapStringEx call')
        value = number(ret[3])
        end = take('W3_CL_MAP_END id='+integer+' tid='+integer+' sp='+integer+' value='+integer)
        if list(map(number,end.groups())) != [index,tid,stack,value]:
            raise Flags0Error('map result differs from callee')
        maps.append({'id':index,'flags':flag,'result':value,'locale':locale_name})
    count = number(take('W3_CL_E1 maps='+integer)[1])
    if (count != len(maps) or not 2 <= count <= 4 or hosts
            or [r['flags'] for r in maps] != sorted(r['flags'] for r in maps)
            or {r['flags'] for r in maps} != {0x100,0x200}):
        raise Flags0Error('incomplete casing before E1')
    for flag in (0x100,0x200):
        casing = [r for r in maps if r['flags']==flag]
        if len(casing)>2 or len(casing)==2 and casing[0]['result']==0:
            raise Flags0Error('not fixed size-then-data casing')
    policy = take('W3_FC_POLICY_RETURN tid='+integer+' value=1')
    if number(policy[1]) != tid:
        raise Flags0Error('policy returned on another thread')
    rejected = location in DIST_POLLUTION
    markers = ['FROZEN_ENTRY.INVENTORY_EXTRA'] if rejected else ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN','FROZEN_ENTRY.SPIKE_COMPLETED']
    for marker in markers:
        diagnostic = take('W3_FC_DIAGNOSTIC tid='+integer+f' owner=7e78 bytes={len(marker):x} text='+re.escape(marker))
        if number(diagnostic[1]) != tid:
            raise Flags0Error('diagnostic on another thread')
        if not rejected:
            take(re.escape(marker))
    take('W3_CL_EXIT')
    if lines or stderr != '\n'.join(markers)+'\n' or canary.actual_exit(output,'W3_CL_EXIT') != int(rejected):
        raise Flags0Error('wrong real terminal rejection/completion')
    return {'debuggee_exit_code':int(rejected),'original_acp':number(acp[3]),'forced_acp':1252,
            'loader_calls':calls,'maps':maps,'known_diagnostics':diagnostics,'os_failure_observed':False,
            'flags0_before_policy':True,'native_markers':markers}


def run_case(args, location, output, source_pre, dll):
    directory = output/location; directory.mkdir(); bundle = directory/'dist'
    shutil.copytree(args.dist,bundle)
    if inventory(bundle)!=source_pre:
        raise Flags0Error('copy differs before stimulus')
    bait=directory/'bait'; bait.mkdir(); cwd=directory/'cwd'; cwd.mkdir(); temporary=directory/'temp'; temporary.mkdir()
    data=dll.read_bytes(); additions=dist_additions(location,data); targets=[]
    for relative, value in additions.items():
        target=bundle/relative
        if target.exists(): raise Flags0Error('would overwrite original member')
        target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(value)
        if relative.endswith('.dll'): targets.append(target)
    if location in ('cwd','path'):
        for name in canary.BAIT_NAMES:
            target=bait/name; target.write_bytes(data); targets.append(target)
    pre=inventory(bundle); bait_pre=inventory(bait)
    expected=canary.inventory_with_additions(source_pre,additions)
    expected_bait={'files':{n:byte_fact(data) for n in canary.BAIT_NAMES} if location in ('cwd','path') else {},'directories':[]}
    if pre!=expected or bait_pre!=expected_bait: raise Flags0Error('unexpected prelaunch delta')
    if location=='cwd': cwd=bait
    env=canary.case_environment(os.environ,location,'normal',bait,temporary)
    record={'location':location,'profile':'flags0','pre':pre,'bait_pre':bait_pre,'targets':[str(p) for p in targets],'passed':False}
    canary.write_json(directory/'prelaunch.json',record)
    try:
        raw,err,observed=canary.cdb_child(args,directory,bundle/EXE,cwd,env,render_commands(),int(location in DIST_POLLUTION))
        record.update(observed)
        record['trace']=validate_trace(raw,err,location,executable_path=bundle/EXE,
            system32=Path(os.environ['SystemRoot'])/'System32',bait_paths=targets)
        record['post']=inventory(bundle); record['bait_post']=inventory(bait)
        if record['post']!=pre or record['bait_post']!=bait_pre: raise Flags0Error('case bytes/layout changed')
        record['passed']=True
    finally:
        canary.write_json(directory/'result.json',record)
    return record


def run(args):
    if os.name!='nt' or not math.isfinite(args.timeout) or not 0<args.timeout<=60:
        raise Flags0Error('Windows and bounded timeout required')
    for name in ('dist','release','cdb','vcvarsall'):
        path=Path(getattr(args,name)).absolute(); _no_reparse_ancestors(path); setattr(args,name,path)
    release_bytes=args.release.read_bytes()
    external=dict(expected_release_sha256=args.expected_release_sha256,expected_repository_commit=args.expected_commit,
                  expected_candidate_input_digest=args.expected_candidate_digest)
    binding=verify_release_binding(release_bytes,args.dist,**external)
    if set(binding['dist'])!=set(PROFILE)|{EXE,MANIFEST}: raise Flags0Error('not the minimal twelve-file dist')
    candidate_path=ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json'; candidate=load_candidate(candidate_path)
    if candidate['candidate_input_digest']!=args.expected_candidate_digest: raise Flags0Error('wrong candidate')
    import pefile
    metadata=importlib.metadata.distribution('pefile').read_text('METADATA')
    lock=candidate['evidence_producer']['pefile']; checked_bytes(Path(pefile.__file__),lock['source'])
    if metadata is None or pefile.__version__!=lock['version'] or byte_fact(metadata.encode())!={k:lock['distribution_metadata'][k] for k in ('bytes','sha256')}:
        raise Flags0Error('unlocked parser')
    executable=checked_bytes(args.dist/EXE,binding['executable'])
    anchors=locale.verify_sites(executable); anchors['diagnostic_call']=canary.verify_diagnostic_site(executable)
    source_pre=inventory(args.dist); artifacts=ROOT/'artifacts/windows'; artifacts.mkdir(parents=True,exist_ok=True); _no_reparse_ancestors(artifacts)
    output=Path(tempfile.mkdtemp(prefix='flags0-canary-',dir=artifacts)); print('FLAGS0_CANARY='+str(output),flush=True)
    observer=output/'observer'; observer.mkdir()
    files=['tools/'+name for name in observer_inventory(args.cdb) if name!='cdb.exe']
    files+=['tools/'+name for name in ('verify_windows_frozen_flags0_canary.py','trace_windows_frozen_crt_locale_fallback.py',
        'verify_windows_frozen_loader_canary.py','trace_windows_frozen_loader_resolution.py',
        'verify_windows_frozen_payload.py','audit_windows_frozen_custom_entry.py')]
    files+=['tests/native/windows_frozen_loader_canary.c','tests/test_windows_frozen_flags0_canary.py',
        'tests/test_windows_frozen_crt_locale_fallback.py','packaging/windows/frozen-entry/candidate-input.lock.json']
    code={}
    for relative in sorted(set(files)):
        data=(ROOT/relative).read_bytes(); code[relative]=byte_fact(data)
        target=observer/relative; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    observer_pre=inventory(observer)
    def tools():
        return {'cdb':byte_fact(args.cdb.read_bytes()),'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
            'metadata':byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode()),
            'python':byte_fact(Path(sys.executable).read_bytes())}
    tools_pre=tools(); anchor_bytes=canonical(external); (output/'anchors.json').write_bytes(anchor_bytes); (output/'release.json').write_bytes(release_bytes)
    replay=[sys.executable,'-B',str(observer/'tools/verify_windows_frozen_flags0_canary.py'),
        '--dist',str(args.dist),'--release',str(args.release),'--cdb',str(args.cdb),'--vcvarsall',str(args.vcvarsall),
        '--expected-release-sha256',args.expected_release_sha256,'--expected-commit',args.expected_commit,
        '--expected-candidate-digest',args.expected_candidate_digest,'--timeout',str(args.timeout)]
    canary.write_json(output/'command.json',{'actual_argv':sys.argv,'caller_cwd':str(Path.cwd()),'replay_argv':replay,
        'powershell':'& '+' '.join("'"+a.replace("'","''")+"'" for a in replay)})
    report={'classification':CLASSIFICATION,'limitations':LIMITATIONS,'anchors':external,'machine_anchors':anchors,
        'output':str(output),'source_pre':source_pre,'release_pre':byte_fact(release_bytes),'code_inventory':code,
        'observer_pre':observer_pre,'tools_pre':tools_pre,'expected_cases':list(LOCATIONS),'cases':[],
        'build':{'commands':[]},'passed':False}
    canary.write_json(output/'prelaunch.json',report)
    try:
        build=output/'build'; build.mkdir(); dll,exe=canary.compile_fixture(args,candidate,build,report['build'])
        report['build_pre']=inventory(build)
        report['positive_controls']=[canary.positive_control(args,output,dll,exe,location) for location in ('clean','local-dir','sxs-external')]
        for location in LOCATIONS:
            if inventory(args.dist)!=source_pre or args.release.read_bytes()!=release_bytes: raise Flags0Error('source drift')
            print('FLAGS0_CASE='+location,flush=True); report['cases'].append(run_case(args,location,output,source_pre,dll))
        report['build_post']=inventory(build); report['source_post']=inventory(args.dist)
        report['observer_post']=inventory(observer); report['tools_post']=tools()
        if (report['build_post']!=report['build_pre'] or report['source_post']!=source_pre
                or report['observer_post']!=observer_pre or report['tools_post']!=tools_pre
                or any(byte_fact((ROOT/p).read_bytes())!=fact for p,fact in code.items())
                or (output/'anchors.json').read_bytes()!=anchor_bytes or (output/'release.json').read_bytes()!=release_bytes
                or args.release.read_bytes()!=release_bytes):
            raise Flags0Error('post-observation input drift')
        verify_release_binding(release_bytes,args.dist,**external)
        report['passed']=len(report['cases'])==len(LOCATIONS) and all(c['passed'] for c in report['cases'])
    except (OSError,ValueError,SystemExit) as exc:
        report['error']=str(exc)
    finally:
        canary.write_json(output/'result.json',report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dist','release','cdb','vcvarsall'): parser.add_argument('--'+name,required=True,type=Path)
    for name in ('expected-release-sha256','expected-commit','expected-candidate-digest'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--timeout',type=float,default=45); args=parser.parse_args(argv)
    try: report=run(args)
    except (OSError,ValueError,SystemExit) as exc:
        print('FLAGS0_INPUT_REJECTED: '+str(exc),file=sys.stderr); return 2
    print(canonical({'classification':CLASSIFICATION,'passed':report['passed'],'evidence':report['output'],'error':report.get('error')}).decode())
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
