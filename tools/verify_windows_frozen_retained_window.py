"""Observe two retained windows and four prelaunch rooted-path counterexamples.

All mutations target new private copies. No product code or global settings change.
This is bounded observation, not complete race, E0, alias or reparse coverage.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import importlib.metadata
import math
import ntpath
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe_windows_frozen_custom_runw import byte_fact, canonical, checked_bytes, load_candidate, record_command
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.trace_windows_frozen_loader_resolution import verify_sites, norm, integer
from tools.verify_windows_frozen_payload import inventory, PROFILE, EXE, MANIFEST
from tools.windows_frozen_api_binding import extract_binding
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors

EXE_SHA = '2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea'
STAGES = ('pre-dispatch', 'binder-entry')
OPERATIONS = tuple((op, path) for path in PROFILE for op in ('write', 'replace', 'rename')) + (
    ('rename', '_internal'), ('rename', '.'))
ROOTED_CASES = {'root-junction': 'FROZEN_ENTRY.REPARSE_REJECTED',
                'ancestor-junction': 'FROZEN_ENTRY.REPARSE_REJECTED',
                'internal-junction': 'FROZEN_ENTRY.REPARSE_REJECTED',
                'critical-hardlink': 'FROZEN_ENTRY.MULTI_LINK_REJECTED'}
SUCCESS = ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN', 'FROZEN_ENTRY.SPIKE_COMPLETED']
CLASSIFICATION = 'CONTROLLED_RETAINED_WINDOWS_AND_ROOTED_LAYOUTS_NOT_FULL_RACE'
LIMITATIONS = [
    'Only first dispatcher CALL before execution and E8 binder entry, not arbitrary scheduling or all races.',
    'Ten payload files write/replace/rename and two directory renames; no arbitrary ACL/volume/network attacker.',
    'Three junction placements and one hardlink alias; not every reparse tag, alias, ancestor swap or filesystem.',
    'No .local/SxS or complete E0, CWD access or system-provider closure claim.',
    'CDB module events are observed paths, not additional retained identity authority.',
    'Only newly launched CDB/debuggee is controlled; timeout terminates only that child.',
]
REPLACEMENT = b'LOCALCAT_UNVERIFIED_REPLACEMENT\n'


class WindowError(ValueError):
    pass


def write_json(path, value):
    path.write_bytes(canonical(value))


def machine_anchors(executable, python, api):
    if byte_fact(executable)['sha256'] != EXE_SHA:
        raise WindowError('retained windows require the exact new final PE')
    sites = verify_sites(executable, python)
    table = extract_binding(executable, python, api)
    import pefile
    with pefile.PE(data=executable) as pe:
        code = pe.get_data(0x7e78, 6)
        imports = {i.address: i.name for d in pe.DIRECTORY_ENTRY_IMPORT for i in d.imports}
        if code[:2] != b'\xff\x15' or imports.get(pe.OPTIONAL_HEADER.ImageBase + 0x7e7e + struct.unpack('<i', code[2:])[0]) != b'WriteFile':
            raise WindowError('native diagnostic WriteFile instruction/IAT changed')
    return {'entry': sites['entry'], 'dispatch': 0x6847, 'diagnostic': 0x7e78,
            'binder': table['function_rva'], 'binder_end': table['function_end_rva'],
            'binder_decode': table}


def render_commands(anchors, stage):
    if stage not in STAGES and stage not in ROOTED_CASES:
        raise WindowError('unknown stage')
    pause = '.if (@$t7==0) {r @$t7=1;.echo W3_RW_PAUSE pre-dispatch;} .else {gc;}' if stage == 'pre-dispatch' else 'gc'
    binder = '.echo W3_RW_PAUSE binder-entry' if stage == 'binder-entry' else 'gc'
    return '\n'.join([
        '.echo W3_RW_BEGIN', f'r @$t0=@$exentry-0x{anchors["entry"]:x}', 'r @$t7=0',
        'sxe -c ".echo W3_RW_EXIT;.lastevent;q" epr',
        'sxe -c ".echo W3_RW_AV;.lastevent;q" av',
        'bp @$exentry ".printf \\"W3_RW_ENTRY tid=%x\\", @$tid;.echo;gc"'.replace('\\\\', '\\'),
        'bp @$t0+0x1037 ".printf \\"W3_RW_POLICY_CALL tid=%x flags=%x\\", @$tid, @ecx;.echo;gc"'.replace('\\\\', '\\'),
        'bp @$t0+0x103d ".printf \\"W3_RW_POLICY_RETURN tid=%x value=%x\\", @$tid, @eax;.echo;gc"'.replace('\\\\', '\\'),
        f'bp @$t0+0x{anchors["dispatch"]:x} ".printf \\"W3_RW_CALL tid=%x sp=%p flags=%x name=%mu\\", @$tid, @rsp, @r8d, @rcx;.echo;{pause}"'.replace('\\\\', '\\'),
        f'bp @$t0+0x{anchors["dispatch"]+6:x} ".printf \\"W3_RW_RETURN tid=%x sp=%p module=%p\\", @$tid, @rsp, @rax;.echo;gc"'.replace('\\\\', '\\'),
        f'bp @$t0+0x{anchors["binder"]:x} ".printf \\"W3_RW_BINDER tid=%x python=%p\\", @$tid, @rcx;.echo;{binder}"'.replace('\\\\', '\\'),
        f'bp @$t0+0x{anchors["diagnostic"]:x} ".printf \\"W3_RW_DIAG tid=%x bytes=%x text=%ma\\", @$tid, @r8d, @rdx;.echo;gc"'.replace('\\\\', '\\'),
        'g', ''])


def validate_trace(output, stderr, dist, stage):
    if stage not in STAGES and stage not in ROOTED_CASES:
        raise WindowError('unknown observation profile')
    # Piped interactive input leaves a prompt on the same line as the resume
    # .echo result or first event. Do not strip prompts from echoed commands.
    output = re.sub(r'^\d+:\d+> (?=W3_RW_|ModLoad: |FROZEN_ENTRY\.)', '', output, flags=re.M)
    rows = [line.strip() for line in output.splitlines() if line.strip().startswith(('W3_RW_', 'FROZEN_ENTRY.', 'ModLoad:'))]
    events = [row for row in rows if not row.startswith('ModLoad:')]
    entry = re.fullmatch(r'W3_RW_ENTRY tid=([0-9a-f`]+)', events[1]) if len(events)>1 else None
    if not entry:
        raise WindowError('missing actual PE entry')
    tid = entry[1]
    expected = ['W3_RW_BEGIN', entry[0], f'W3_RW_POLICY_CALL tid={tid} flags=800', f'W3_RW_POLICY_RETURN tid={tid} value=1']
    wanted = [ROOTED_CASES[stage]] if stage in ROOTED_CASES else SUCCESS
    if stderr.splitlines() != wanted:
        raise WindowError('stderr disagrees with exact profile')
    native = []
    for row in rows:
        match = re.fullmatch(r'ModLoad:\s+([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+(.+)', row)
        if match and ntpath.basename(match[3]).lower() in ('vcruntime140.dll', 'python314.dll'):
            if integer(match[2]) <= integer(match[1]):
                raise WindowError('invalid module range')
            native.append((row, integer(match[1]), match[3]))
    if stage in STAGES:
        if len(native) != 2:
            raise WindowError('wrong native module event count')
        for index, name in enumerate(('vcruntime140.dll', 'python314.dll')):
            call = next((s for s in events[len(expected):] if s.startswith('W3_RW_CALL ')), '')
            match = re.fullmatch(r'W3_RW_CALL tid=([0-9a-f`]+) sp=([0-9a-f`]+) flags=900 name=(.+)', call)
            module_row, base, path = native[index]
            wanted_path = ntpath.join(dist, '_internal', name)
            if not match or match[1] != tid or norm(match[3]) != norm(wanted_path) or norm(path) != norm(wanted_path) or not base:
                raise WindowError('wrong dispatcher thread, flags, module or path')
            expected.append(call)
            if index == 0 and stage == 'pre-dispatch':
                expected += ['W3_RW_PAUSE pre-dispatch', 'W3_RW_RESUME']
            returns = [s for s in events if re.fullmatch(r'W3_RW_RETURN tid='+re.escape(tid)+r' sp='+re.escape(match[2])+r' module=([0-9a-f`]+)', s)]
            ret = next((s for s in returns if integer(s.split('module=')[1]) == base), '')
            if not ret or not rows.index(call) < rows.index(module_row) < rows.index(ret):
                raise WindowError('module event is not inside its application CALL/return')
            if index == 0 and stage == 'pre-dispatch':
                pause, resume = 'W3_RW_PAUSE pre-dispatch', 'W3_RW_RESUME'
                if (pause not in rows or resume not in rows or not
                        rows.index(call) < rows.index(pause) < rows.index(resume) < rows.index(module_row) < rows.index(ret)):
                    raise WindowError('first module load is not after the controlled pre-call pause/resume')
            expected.append(ret)
        binder = next((s for s in events if s.startswith('W3_RW_BINDER ')), '')
        match = re.fullmatch(r'W3_RW_BINDER tid='+re.escape(tid)+r' python=([0-9a-f`]+)', binder)
        if not match or integer(match[1]) != native[1][1]:
            raise WindowError('binder did not receive observed Python HMODULE')
        expected.append(binder)
        if stage == 'binder-entry':
            expected += ['W3_RW_PAUSE binder-entry', 'W3_RW_RESUME']
    elif native:
        raise WindowError('rooted rejection loaded a declared DLL')
    for marker in wanted:
        expected.append(f'W3_RW_DIAG tid={tid} bytes={len(marker):x} text={marker}')
        if stage in STAGES:
            expected.append(marker)
    expected.append('W3_RW_EXIT')
    exits = re.findall(r'^Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$', output, re.M)
    exit_code = int(stage in ROOTED_CASES)
    if events != expected or len(exits) != 1 or int(exits[0], 16) != exit_code:
        raise WindowError('incomplete, repeated or out-of-order observation')
    return {'debuggee_exit_code': exit_code, 'pause_stage': stage if stage in STAGES else None,
            'markers': wanted, 'native_modules': [{'base':b, 'path':p} for _,b,p in native]}


def clean_output(output, stderr, exe):
    warning = '*** WARNING: Unable to verify timestamp for ' + str(exe).replace('/', '\\')
    if output.count(warning) == 1:
        output = output.replace(warning+'\n', '')
    if any(token in (output+'\n'+stderr).lower() for token in ('warning', 'syntax error', "couldn't resolve", 'unable to',
            'access violation', 'numeric expression missing', 'range error', 'waitforevent', 'illegal column count', 'failed')):
        raise WindowError('uncertain debugger observation')
    return output


def controlled_target(case, bundle, relative):
    if ntpath.isabs(relative) or '..' in Path(relative).parts or relative not in {'.', '_internal', *PROFILE}:
        raise WindowError('operation outside fixed copied target set')
    base = case.resolve(strict=True); target = (bundle/relative).absolute()
    if target == base or not target.resolve(strict=True).is_relative_to(base):
        raise WindowError('mutation target outside new private case')
    _no_reparse_ancestors(target)
    return target


def write_first_byte(target):
    original = target.read_bytes()
    if not original:
        raise WindowError('empty write control target')
    if os.name != 'nt':  # Unit tests also exercise the exact delta on other hosts.
        with target.open('r+b') as stream:
            stream.write(bytes([original[0]^1]))
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WriteFile.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(target), 0x40000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data = ctypes.create_string_buffer(bytes([original[0]^1])); count = wintypes.DWORD()
        if not kernel.WriteFile(handle, data, 1, ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if count.value != 1:
            raise WindowError('short controlled write')
    finally:
        kernel.CloseHandle(handle)


def attempt(action):
    try:
        action()
    except OSError as error:
        return {'succeeded':False, 'winerror':getattr(error, 'winerror', None), 'errno':error.errno, 'error':str(error)}
    return {'succeeded':True, 'winerror':None}


def validate_attempts(rows, *, retained):
    if [(r['operation'],r['path']) for r in rows] != list(OPERATIONS):
        raise WindowError('missing, repeated or changed operation matrix')
    if retained:
        good = all(not r['succeeded'] and r['winerror']==32 for r in rows)
    else:
        good = all(r['succeeded'] and r.get('exact_delta') for r in rows)
    if not good:
        raise WindowError('operation result disagrees with retained/positive profile')


def mutate(case, bundle, operation, relative, index):
    target = controlled_target(case, bundle, relative)
    sibling = case/('replacement-'+str(index))
    if sibling.exists():
        raise WindowError('replacement target already exists')
    if operation == 'replace':
        sibling.write_bytes(REPLACEMENT)
        action = lambda: os.replace(sibling, target)
    elif operation == 'rename':
        action = lambda: target.rename(sibling)
    elif operation == 'write':
        action = lambda: write_first_byte(target)
    else:
        raise WindowError('unknown mutation')
    result = dict(attempt(action), operation=operation, path=relative, target=str(target), destination=str(sibling))
    if operation == 'replace':
        result['replacement_bytes'] = byte_fact(REPLACEMENT)
        if not result['succeeded'] and sibling.read_bytes() != REPLACEMENT:
            raise WindowError('failed replacement changed source bytes')
    return result, target, sibling


def positive_operation(source, case, operation, relative):
    bundle = case/'dist'; shutil.copytree(source, bundle)
    pre = inventory(bundle)
    target = controlled_target(case, bundle, relative)
    original = None if target.is_dir() else target.read_bytes()
    result, target, destination = mutate(case, bundle, operation, relative, 0)
    expected = {'files':dict(pre['files']), 'directories':list(pre['directories'])}
    if not result['succeeded']:
        result['exact_delta'] = False
    elif operation == 'rename' and relative == '.':
        result['exact_delta'] = not bundle.exists() and inventory(destination)==pre
    elif operation == 'rename' and relative == '_internal':
        expected = {'files':{p:v for p,v in pre['files'].items() if not p.startswith('_internal/')},
                    'directories':[p for p in pre['directories'] if p!='_internal' and not p.startswith('_internal/')]}
        wanted = {'files':{p[len('_internal/'):]:v for p,v in pre['files'].items() if p.startswith('_internal/')},
                  'directories':[p[len('_internal/'):] for p in pre['directories'] if p.startswith('_internal/')]}
        result['exact_delta'] = inventory(bundle)==expected and inventory(destination)==wanted
    else:
        if operation == 'write':
            expected['files'][relative] = byte_fact(bytes([original[0]^1])+original[1:])
        elif operation == 'replace':
            expected['files'][relative] = byte_fact(REPLACEMENT)
        else:
            del expected['files'][relative]
        result['exact_delta'] = inventory(bundle)==expected and (operation!='rename' or destination.read_bytes()==original)
    result['pre'] = pre
    result['post'] = inventory(destination if operation=='rename' and relative=='.' and result['succeeded'] else bundle)
    if not result['exact_delta']:
        raise WindowError('positive operation did not produce its exact expected delta')
    write_json(case/'result.json', result)
    return result


def case_environment(temporary):
    system = os.environ['SystemRoot']
    env = trace_environment({'SystemRoot':system, 'WINDIR':system}, 'normal')
    env.update(TEMP=str(temporary), TMP=str(temporary), LOCALAPPDATA=str(temporary), APPDATA=str(temporary))
    return env


def cdb_child(args, case, exe, stage, anchors, callback):
    for name in ('symbols','cwd','temp'):
        (case/name).mkdir()
    script = case/'observe.cdb'; script.write_text(render_commands(anchors, stage), encoding='ascii', newline='\n')
    env = case_environment(case/'temp')
    command = [str(args.cdb), '-y',str(case/'symbols'), '-cf',str(script),str(exe)]
    record = {'argv':command, 'cwd':str(case/'cwd'), 'environment':env, 'timeout':args.timeout,
              'script_pre':byte_fact(script.read_bytes()), 'interactions':[], 'exit_code':None}
    write_json(case/'command.json', record)
    stdout, stderr, events = [], [], queue.Queue()
    process = subprocess.Popen(command, cwd=case/'cwd', env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    def reader(stream, collected, notify):
        for line in iter(stream.readline, b''):
            collected.append(line)
            if notify:
                events.put(line)
    threads = [threading.Thread(target=reader,args=(process.stdout,stdout,True),daemon=True),
               threading.Thread(target=reader,args=(process.stderr,stderr,False),daemon=True)]
    for thread in threads:
        thread.start()
    try:
        if stage in STAGES:
            deadline = time.monotonic()+args.timeout
            while True:
                remaining = deadline-time.monotonic()
                if remaining<=0 or process.poll() is not None:
                    raise WindowError('debuggee did not pause at exact retained window')
                try:
                    line = events.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    continue
                if line.strip()==('W3_RW_PAUSE '+stage).encode():
                    break
            callback()
            command_bytes = b'.echo W3_RW_RESUME\ng\n'
            process.stdin.write(command_bytes); process.stdin.flush()
            record['interactions'].append(command_bytes.decode())
        process.wait(timeout=args.timeout)
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(b'q\n'); process.stdin.flush(); record['interactions'].append('q\n')
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill(); process.wait(timeout=5)
                record['terminated_own_child'] = True
        for thread in threads:
            thread.join(timeout=5)
        record['exit_code'] = process.returncode
        (case/'cdb.stdout').write_bytes(b''.join(stdout)); (case/'cdb.stderr').write_bytes(b''.join(stderr))
        record['stdout'] = byte_fact(b''.join(stdout)); record['stderr'] = byte_fact(b''.join(stderr))
        record['script_post'] = byte_fact(script.read_bytes())
        write_json(case/'observation.json', record)
        for stream in (process.stdin,process.stdout,process.stderr):
            stream.close()
    if any(t.is_alive() for t in threads) or record['script_post']!=record['script_pre']:
        raise WindowError('incomplete pipe capture or script drift')
    output = b''.join(stdout).decode('utf-8'); error = b''.join(stderr).decode('utf-8')
    if record['exit_code']!=int(stage in ROOTED_CASES):
        raise WindowError('wrong real debugger exit')
    return clean_output(output,error,exe), error, record


def layout_inventory(root):
    """Record deliberate reparse nodes WITHOUT traversing them."""
    result = {}; pending = [root]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            info = path.lstat(); name = path.relative_to(root).as_posix()
            if getattr(info,'st_file_attributes',0)&0x400 or stat.S_ISLNK(info.st_mode):
                result[name] = {'kind':'reparse', 'tag':getattr(info,'st_reparse_tag',None), 'target':os.readlink(path)}
            elif path.is_dir():
                result[name] = {'kind':'directory'}; pending.append(path)
            elif path.is_file():
                result[name] = {'kind':'file', **byte_fact(path.read_bytes()), 'links':info.st_nlink}
            else:
                raise WindowError('unexpected layout member kind')
    return dict(sorted(result.items()))


def rooted_layout(args, case, stage, source_pre, commands):
    layout = case/'layout'; layout.mkdir(); physical = layout/'physical'; physical.mkdir()
    bundle = physical/'dist'; shutil.copytree(args.dist, bundle)
    if inventory(bundle)!=source_pre:
        raise WindowError('rooted source copy differs')
    before = layout_inventory(layout); expected = dict(before); launch = bundle
    if stage == 'critical-hardlink':
        target = bundle/'_internal/localcat_spike_critical.py'; alias = layout/'critical-alias.py'
        os.link(target, alias)
        key = 'physical/dist/_internal/localcat_spike_critical.py'
        expected[key] = {**expected[key], 'links':2}; expected['critical-alias.py'] = expected[key]
        if not os.path.samefile(target,alias):
            raise WindowError('hardlink positive layout creation failed')
    else:
        if stage == 'internal-junction':
            link = bundle/'_internal'; target = layout/'retained-internal'; link.rename(target)
            prefix = 'physical/dist/_internal'
            for name in list(expected):
                if name==prefix or name.startswith(prefix+'/'):
                    expected['retained-internal'+name[len(prefix):]] = expected.pop(name)
        else:
            link = layout/'alias'; target = bundle if stage=='root-junction' else physical
            launch = link if stage=='root-junction' else link/'dist'
        if any(char in str(path) for path in (link,target) for char in '%\r\n"'):
            raise WindowError('unsafe junction command path')
        command = f'"{Path(os.environ["SystemRoot"])/"System32/cmd.exe"}" /d /c mklink /J "{link}" "{target}"'
        record_command(command,case,case_environment(case),case,'junction',commands,timeout=args.timeout)
        value = {'kind':'reparse','tag':0xa0000003,'target':'\\??\\'+str(target)}
        actual = layout_inventory(layout).get(link.relative_to(layout).as_posix())
        # readlink represents mount-point substitution names as \\?\ on Python.
        if not actual or actual['tag']!=value['tag'] or norm(actual['target'])!=norm(str(target)):
            raise WindowError('junction did not resolve to the explicit copied target')
        expected[link.relative_to(layout).as_posix()] = actual
    pre = layout_inventory(layout)
    if pre!=dict(sorted(expected.items())) or byte_fact((launch/EXE).read_bytes())!=source_pre['files'][EXE]:
        raise WindowError('unexpected rooted layout delta or executable change')
    return launch, {'before':before,'expected':expected,'pre':pre,'root':str(layout)}


def run_case(args, output, stage, source_pre, anchors):
    case = output/stage; case.mkdir()
    record = {'stage':stage,'attempts':[],'commands':[],'passed':False}
    try:
        if stage in STAGES:
            bundle = case/'dist'; shutil.copytree(args.dist,bundle)
            if inventory(bundle)!=source_pre:
                raise WindowError('window source copy differs')
            record['pre'] = inventory(bundle)
            def callback():
                for index,(operation,path) in enumerate(OPERATIONS):
                    row,_,_ = mutate(case,bundle,operation,path,index); record['attempts'].append(row)
                    write_json(case/'attempts.json',record['attempts'])
                    if row['succeeded'] or row['winerror']!=32 or inventory(bundle)!=record['pre']:
                        raise WindowError('retained mutation did not fail unchanged with Winerror32')
                validate_attempts(record['attempts'], retained=True)
        else:
            bundle, record['layout'] = rooted_layout(args,case,stage,source_pre,record['commands'])
            callback = lambda: None
        raw,err,record['observation'] = cdb_child(args,case,bundle/EXE,stage,anchors,callback)
        record['trace'] = validate_trace(raw,err,str(bundle),stage)
        if stage in STAGES:
            record['post'] = inventory(bundle)
            if record['post']!=record['pre']:
                raise WindowError('retained copy changed after child exit')
        else:
            record['layout']['post'] = layout_inventory(Path(record['layout']['root']))
            if record['layout']['post']!=record['layout']['pre']:
                raise WindowError('rooted layout changed during child')
        record['passed'] = True
    finally:
        write_json(case/'result.json',record)
    return record


def run(args):
    if os.name!='nt' or not math.isfinite(args.timeout) or not 0<args.timeout<=60:
        raise WindowError('Windows required; timeout must be in (0,60]')
    for name in ('dist','release','cdb'):
        path = Path(getattr(args,name)).absolute(); _no_reparse_ancestors(path); setattr(args,name,path)
    external = dict(expected_release_sha256=args.expected_release_sha256, expected_repository_commit=args.expected_commit,
                    expected_candidate_input_digest=args.expected_candidate_digest)
    release_bytes = args.release.read_bytes(); release = verify_release_binding(release_bytes,args.dist,**external)
    if set(release['dist'])!=set(PROFILE)|{EXE,MANIFEST}:
        raise WindowError('requires exact twelve-file candidate')
    candidate = load_candidate(ROOT/'packaging/windows/frozen-entry/candidate-input.lock.json')
    if candidate['candidate_input_digest']!=args.expected_candidate_digest:
        raise WindowError('candidate differs from external anchor')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']; checked_bytes(Path(pefile.__file__),parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if metadata is None or pefile.__version__!=parser_lock['version'] or byte_fact(metadata.encode())!={k:parser_lock['distribution_metadata'][k] for k in ('bytes','sha256')}:
        raise WindowError('unlocked PE parser')
    anchors = machine_anchors(checked_bytes(args.dist/EXE,release['executable']),
        checked_bytes(args.dist/'_internal/python314.dll',candidate['runtime']['cpython']['dll']),candidate['entry_contract']['python_c_api'])
    artifacts = ROOT/'artifacts/windows'; artifacts.mkdir(parents=True,exist_ok=True); _no_reparse_ancestors(artifacts)
    output = Path(tempfile.mkdtemp(prefix='retained-window-',dir=artifacts))
    print('RETAINED_WINDOW='+str(output),flush=True)
    observer = output/'observer'; observer.mkdir(); code = {}
    files = ['tools/'+name for name in observer_inventory(args.cdb) if name!='cdb.exe'] + [
        'tools/trace_windows_frozen_loader_resolution.py','tools/verify_windows_frozen_payload.py',
        'tools/verify_windows_frozen_retained_window.py','tests/test_windows_frozen_retained_window.py',
        'packaging/windows/frozen-entry/candidate-input.lock.json']
    for name in sorted(set(files)):
        data = (ROOT/name).read_bytes(); code[name] = byte_fact(data)
        target = observer/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    def tool_inventory():
        return {'cdb':byte_fact(args.cdb.read_bytes()),'python':byte_fact(Path(sys.executable).read_bytes()),
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile_metadata':byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode()),
                'cmd':byte_fact((Path(os.environ['SystemRoot'])/'System32/cmd.exe').read_bytes())}
    report = {'classification':CLASSIFICATION,'limitations':LIMITATIONS,'anchors':external,'machine_anchors':anchors,
              'source_pre':inventory(args.dist),'release_pre':byte_fact(release_bytes),'code_inventory':code,
              'observer_pre':inventory(observer),'tools_pre':tool_inventory(),'positive_controls':[],'cases':[],
              'output':str(output),'passed':False}
    (output/'release.json').write_bytes(release_bytes); anchor_bytes = canonical(external); (output/'anchors.json').write_bytes(anchor_bytes)
    replay = [sys.executable,'-B',str(observer/'tools/verify_windows_frozen_retained_window.py')]
    for name in ('dist','release','cdb','expected_release_sha256','expected_commit','expected_candidate_digest','timeout'):
        replay += ['--'+name.replace('_','-'),str(getattr(args,name))]
    write_json(output/'command.json',{'actual_argv':sys.argv,'caller_cwd':str(Path.cwd()),'replay_argv':replay,
        'powershell':'& '+' '.join("'"+arg.replace("'","''")+"'" for arg in replay)})
    write_json(output/'prelaunch.json',report)
    try:
        positive = output/'positive-controls'; positive.mkdir()
        for index,(operation,path) in enumerate(OPERATIONS):
            case = positive/f'{index:02d}-{operation}'; case.mkdir()
            report['positive_controls'].append(positive_operation(args.dist,case,operation,path))
        validate_attempts(report['positive_controls'],retained=False)
        for stage in (*STAGES,*ROOTED_CASES):
            if inventory(args.dist)!=report['source_pre'] or args.release.read_bytes()!=release_bytes:
                raise WindowError('source drift; no further child launched')
            print('RETAINED_CASE='+stage,flush=True)
            report['cases'].append(run_case(args,output,stage,report['source_pre'],anchors))
        report['source_post'] = inventory(args.dist); report['observer_post'] = inventory(observer); report['tools_post'] = tool_inventory()
        report['release_post'] = byte_fact(args.release.read_bytes())
        if (report['source_post']!=report['source_pre'] or report['observer_post']!=report['observer_pre']
                or report['tools_post']!=report['tools_pre'] or report['release_post']!=report['release_pre']
                or (output/'anchors.json').read_bytes()!=anchor_bytes or (output/'release.json').read_bytes()!=release_bytes
                or any(byte_fact((ROOT/name).read_bytes())!=fact for name,fact in code.items())):
            raise WindowError('source, anchor or observer changed')
        verify_release_binding(release_bytes,args.dist,**external)
        report['passed'] = len(report['cases'])==6 and all(row['passed'] for row in report['cases'])
    except (OSError,ValueError,subprocess.TimeoutExpired) as error:
        report['error'] = str(error)
    finally:
        write_json(output/'result.json',report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist','release','cdb'):
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ('expected-release-sha256','expected-commit','expected-candidate-digest'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--timeout',type=float,default=45)
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (OSError,ValueError) as error:
        print('RETAINED_WINDOW_INPUT_REJECTED: '+str(error),file=sys.stderr); return 2
    print(canonical({'classification':CLASSIFICATION,'passed':report['passed'],'evidence':report['output'],'error':report.get('error')}).decode())
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
