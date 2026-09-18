"""Build benign native bait and observe a bounded, externally bound entry matrix.

Test inputs only: no product dependencies, runtime gate, or system settings change.
This is not full E0/race proof, nor an all-path/no-hit loader closure proof.
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
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_windows_frozen_custom_entry import (
    finalize_compile_environment, verified_toolchain_aggregates,
)
from tools.probe_windows_frozen_custom_runw import (
    ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_entry import trace_environment
from tools.trace_windows_frozen_loader_resolution import (
    norm, integer, verify_sites, render_commands, parse_loader_events,
    validate_profile_calls, validate_stderr, PROFILES,
)
from tools.verify_windows_frozen_payload import inventory, PROFILE, EXE, MANIFEST
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors

MARKER = 'LOCALCAT_DLL_CANARY_PROCESS_ATTACH'
CLASSIFICATION = 'NATIVE_CANARY_POLLUTION_OBSERVATION_NOT_FULL_W3'
BAIT_NAMES = ('python314.dll', 'vcruntime140.dll', 'kernel32.dll', 'kernelbase.dll',
              'ntdll.dll', 'user32.dll', 'bcrypt.dll', 'psapi.dll',
              'api-ms-win-core-synch-l1-2-0.dll', 'api-ms-win-core-fibers-l1-1-1.dll',
              'api-ms-win-core-fibers-l1-1-2.dll', 'api-ms-win-appmodel-runtime-l1-1-2.dll',
              'api-ms-win-core-localization-l1-2-1.dll')
BASE_CASES = (('control', 'clean', 'normal'),
         ('cwd-normal', 'cwd', 'normal'), ('cwd-stats', 'cwd', 'mimalloc-stats'),
         ('cwd-policy-false', 'cwd', 'policy-false'),
         ('path-normal', 'path', 'normal'), ('path-stats', 'path', 'mimalloc-stats'),
         ('path-policy-false', 'path', 'policy-false'),
         ('appdir-normal', 'appdir', 'normal'), ('appdir-policy-false', 'appdir', 'policy-false'),
         ('local-file-normal', 'local-file', 'normal'), ('local-file-policy-false', 'local-file', 'policy-false'),
         ('local-dir-normal', 'local-dir', 'normal'), ('local-dir-policy-false', 'local-dir', 'policy-false'),
         ('sxs-external-normal', 'sxs-external', 'normal'), ('sxs-external-policy-false', 'sxs-external', 'policy-false'))
CASES = BASE_CASES + tuple(('acp-1252-'+name, location, 'acp-1252-'+profile)
                           for name, location, profile in BASE_CASES)
REDIRECTIONS = ('local-file', 'local-dir', 'sxs-external')
DIST_POLLUTION = ('appdir', *REDIRECTIONS)
LIMITATIONS = [
    'Only fixed named prelaunch CWD/PATH/appdir/.local/external-SxS bait on this candidate and host.',
    'No reparse/alias/retained-race, arbitrary activation-context mutation or complete E0 resolution proof.',
    'No-hit is not unreachable; conditional bcrypt and legacy flags=0 fallback may not run.',
    'Dist additions must fail the existing inventory check; embedded PE manifest is never resigned.',
    'PEB/lmf paths are observed loader metadata, not retained file identities.',
    'Policy-false skips one E1 CALL in our new debuggee; not a naturally occurring OS failure.',
    'ACP-1252 replaces one actual GetACP return in our new debuggee, not the OS ANSI code page.',
    'Positive controls validate neutral DLL loading and actual .local/private-SxS redirection, not API-set filename semantics.',
    'Only own CDB child is timed out; Windows debugger lifetime controls its own debuggee.',
]


class CanaryError(ValueError):
    pass


def base_profile(profile):
    if profile not in PROFILES:
        raise CanaryError('unknown canary profile')
    return profile.removeprefix('acp-1252-')


def redirection_layout(location, exe_name, names, dll_bytes):
    """Fixed, application-controlled test inputs; never an OS configuration change."""
    if location not in REDIRECTIONS or any(re.fullmatch(r'[A-Za-z0-9_.-]+', n) is None
            or n in ('.', '..') for n in (exe_name, *names)):
        raise CanaryError('invalid redirection location or basename')
    if location == 'local-file':
        return {exe_name + '.local': b'', **{n: dll_bytes for n in names}}
    if location == 'local-dir':
        return {exe_name + '.local/' + n: dll_bytes for n in names}
    identity = ('<assemblyIdentity type="win32" name="LocalCAT.Canary" '
                'version="1.0.0.0" processorArchitecture="amd64"/>')
    start = '<?xml version="1.0" encoding="UTF-8"?><assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
    application = start + '<dependency><dependentAssembly>' + identity + '</dependentAssembly></dependency></assembly>'
    assembly = start + identity + ''.join('<file name="' + n + '"/>' for n in names) + '</assembly>'
    return {exe_name + '.manifest': application.encode('utf-8'),
            'LocalCAT.Canary/LocalCAT.Canary.manifest': assembly.encode('utf-8'),
            **{'LocalCAT.Canary/' + n: dll_bytes for n in names}}


def inventory_with_additions(before, additions):
    files = dict(before['files']); directories = set(before['directories'])
    for relative, data in additions.items():
        if relative.casefold() in {name.casefold() for name in files}:
            raise CanaryError('addition would overwrite existing member')
        files[relative] = byte_fact(data)
        directories.update(p.as_posix() for p in Path(relative).parents if str(p) != '.')
    return {'files': dict(sorted(files.items())), 'directories': sorted(directories)}


def build_commands(cl, link, kernel32, source, directory):
    commands = []
    for dll, name in ((True, 'localcat-loader-canary'), (False, 'canary-control')):
        obj = directory / (name + '.obj')
        command = [str(cl), '/nologo', '/c', '/TC', '/W4', '/WX', '/O1', '/GS-', '/Zl', '/Brepro']
        if dll:
            command += ['/DLOCALCAT_CANARY_DLL']
        commands += [command + ['/Fo:' + str(obj), str(source)]]
        command = [str(link), '/nologo', '/NODEFAULTLIB', '/MACHINE:X64', '/INCREMENTAL:NO',
                   '/MANIFEST:NO', '/Brepro', '/DYNAMICBASE', '/NXCOMPAT']
        command += ['/DLL', '/ENTRY:DllMain'] if dll else ['/ENTRY:LocalCatCanaryControl', '/SUBSYSTEM:WINDOWS']
        commands += [command + ['/OUT:' + str(directory / (name + ('.dll' if dll else '.exe'))),
                                str(obj), str(kernel32)]]
    return commands


def case_environment(original, location, profile, bait, temporary):
    values = {key.upper(): value for key, value in original.items()}
    base = {'SystemRoot': values['SYSTEMROOT'], 'WINDIR': values['SYSTEMROOT']}
    base.update({key: values[key] for key in ('SYSTEMDRIVE', 'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS') if key in values})
    env = trace_environment(base, 'mimalloc-stats' if base_profile(profile) == 'mimalloc-stats' else 'normal')
    # Apply the single deliberate override AFTER the common sanitizer resets PATH.
    if location == 'path':
        env['PATH'] = str(bait) + os.pathsep + env['PATH']
    env['TEMP'] = env['TMP'] = str(temporary)
    # CDB expands LOCALAPPDATA for its cache; an absent variable creates literal
    # %LOCALAPPDATA% directories in the debugger/debuggee CWD, polluting the bait.
    env['LOCALAPPDATA'] = env['APPDATA'] = str(temporary)
    return env


def expected_inventory(before, location, bait_fact):
    result = {'files': dict(before['files']), 'directories': list(before['directories'])}
    if location == 'appdir':
        if set(BAIT_NAMES) & set(result['files']):
            raise CanaryError('bait would overwrite a dist member')
        result['files'].update({name: bait_fact for name in BAIT_NAMES})
    return result


def module_events(output):
    result = []
    for start, end, path in re.findall(r'^ModLoad:\s+([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+([^\r\n]+)', output, re.M):
        if integer(end) <= integer(start):
            raise CanaryError('invalid module event range')
        result.append({'base': integer(start), 'end': integer(end), 'path': path.strip()})
    return result


def reject_canary(output, stderr, bait_paths):
    if MARKER in output + stderr:
        raise CanaryError('canary DllMain executed')
    paths = {norm(str(p)) for p in bait_paths}
    if any(norm(row['path']) in paths for row in module_events(output)):
        raise CanaryError('canary mapped, even if DllMain was not reached')


def actual_exit(output, marker='W3_LD_EXIT'):
    pattern = r'Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$'
    exits = re.findall('^' + pattern, output, re.M)
    terminal = re.findall('^' + re.escape(marker) + r'\r?\n' + pattern, output, re.M)
    if len(exits) != 1 or terminal != exits or len(re.findall(r'^Last event:', output, re.M)) != 1:
        raise CanaryError('missing, ambiguous or out-of-order debuggee exit')
    return int(exits[0], 16)


def validate_positive(output, stderr, dll):
    events = [s.strip() for s in output.splitlines() if s.strip().startswith('W3_CANARY_') or MARKER in s]
    if events != ['W3_CANARY_CONTROL_BEGIN', 'W3_CANARY_CONTROL_ENTRY', MARKER, 'W3_CANARY_CONTROL_EXIT']:
        raise CanaryError('positive control did not execute exactly one DllMain')
    loaded = [row for row in module_events(output) if norm(row['path']) == norm(str(dll))]
    if len(loaded) != 1 or actual_exit(output, 'W3_CANARY_CONTROL_EXIT') != 0 or stderr.strip():
        raise CanaryError('positive control path, exit or stderr mismatch')
    lines = [line.strip() for line in output.splitlines()]
    load_positions = [index for index, line in enumerate(lines)
                      if any(norm(row['path']) == norm(str(dll)) for row in module_events(line))]
    if (len(load_positions) != 1 or not lines.index('W3_CANARY_CONTROL_ENTRY') < load_positions[0]
            < lines.index(MARKER) < lines.index('W3_CANARY_CONTROL_EXIT')):
        raise CanaryError('positive DLL load is outside entry/DllMain/exit order')
    return {'module': loaded[0], 'debuggee_exit_code': 0, 'dllmain_marker': MARKER}


def validate_appdir(output, stderr, system32, profile='normal'):
    """Exact early inventory-rejection profile, without fabricating success events."""
    if base_profile(profile) != 'normal':
        raise CanaryError('appdir inventory rejection requires a normal profile')
    acp_profile = profile.startswith('acp-1252-')
    if stderr.splitlines() != ['FROZEN_ENTRY.INVENTORY_EXTRA'] or actual_exit(output) != 1:
        raise CanaryError('wrong appdir failure diagnostic/exit')
    events = [s.strip() for s in output.splitlines() if s.strip().startswith(('W3_LD_', 'FROZEN_ENTRY.'))]
    blocks = re.findall(r'^W3_LD_HOST_BEGIN\r?\n(.*?)^W3_LD_HOST_END\r?$', output, re.M | re.S)
    events = [s for s in events if not s.startswith('W3_LD_PEB ')]
    if (len(events) != (29 if acp_profile else 22) or events[:2] != ['W3_LD_BEGIN', 'W3_LD_PE_ENTRY']
            or events[-1] != 'W3_LD_EXIT' or len(blocks) != (5 if acp_profile else 4)):
        raise CanaryError('incomplete appdir timeline')
    cursor = 2; calls = []; acp = []
    expected = [('a24d', 'api-ms-win-core-synch-l1-2-0'), ('a24d', 'api-ms-win-core-fibers-l1-1-1'),
                ('17061', 'api-ms-win-core-fibers-l1-1-2')]
    if acp_profile:
        expected += [('17061', 'api-ms-win-core-localization-l1-2-1')]
    expected += [('17061', 'api-ms-win-appmodel-runtime-l1-1-2')]
    for index, (rva, name) in enumerate(expected):
        if acp_profile and index == 3:
            acp = events[cursor:cursor+3]
            returned = re.fullmatch(r'W3_LD_ACP_RETURN value=([0-9a-f]+)', acp[0])
            if (not returned or not 0 < int(returned[1], 16) <= 0xffff
                    or acp[1:] != ['W3_LD_ACP_INJECT value=4e4', 'W3_LD_ACP_TABLE value=4e4']):
                raise CanaryError('wrong appdir ACP return/stimulus/table order')
            cursor += 3
        if index == len(expected)-1:
            if events[cursor:cursor+3] != ['W3_LD_POLICY_CALL flags=800', 'W3_LD_POLICY_RETURN value=1',
                    'W3_LD_DIAGNOSTIC owner=7e78 bytes=1c text=FROZEN_ENTRY.INVENTORY_EXTRA']:
                raise CanaryError('wrong appdir policy/rejection order')
            cursor += 3
        call = re.fullmatch(r'W3_LD_CALL exe:' + rva + r' tid=([0-9a-f`]+) sp=([0-9a-f`]+) flags=800 name=' + name, events[cursor])
        ret = re.fullmatch(r'W3_LD_RETURN exe:' + rva + r' tid=([0-9a-f`]+) sp=([0-9a-f`]+) module=([0-9a-f`]+)', events[cursor+1])
        if not call or not ret or call.groups() != ret.groups()[:2] or events[cursor+2:cursor+4] != ['W3_LD_HOST_BEGIN', 'W3_LD_HOST_END']:
            raise CanaryError('wrong appdir loader pair')
        cursor += 4
        rows = re.findall(r'^([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+\S+\s+([^\r\n]+)\r?$', blocks[index], re.M)
        peb = re.findall(r'^W3_LD_PEB module=([0-9a-fA-F`]+) path=([^\r\n]+)\r?$', blocks[index], re.M)
        if len(rows) != 1 or len(peb) != 1:
            raise CanaryError('missing appdir host')
        base = integer(ret[3]); path = peb[0][1].strip(); shown = rows[0][2].strip()
        if (not base or base != integer(rows[0][0]) or base != integer(peb[0][0]) or integer(rows[0][1]) <= base
                or norm(ntpath.dirname(path)) != norm(system32)
                or norm(shown) != norm(path if ntpath.isabs(shown) else ntpath.basename(path))):
            raise CanaryError('appdir returned host is not the system module')
        calls.append({'rva': int(rva, 16), 'name': name, 'module': base, 'host': path,
                      'thread': integer(call[1]), 'stack': integer(call[2]), 'flags': 0x800})
    if (cursor != len(events)-1 or len({row['thread'] for row in calls}) != 1
            or len(re.findall(r'^W3_LD_PEB ', output, re.M)) != len(expected)):
        raise CanaryError('wrong appdir thread or extra PEB record')
    return {'calls': calls, 'debuggee_exit_code': 1, 'native_markers': ['FROZEN_ENTRY.INVENTORY_EXTRA'],
            'acp_stimulus': acp}


def clean_debugger_output(output, stderr, exe):
    warning = '*** WARNING: Unable to verify timestamp for ' + str(exe).replace('/', '\\')
    if output.count(warning) == 1:
        output = output.replace(warning + '\n', '')
    diagnostic = (output + '\n' + stderr).replace('FROZEN_ENTRY.DLL_POLICY_FAILED', '')
    if any(word in diagnostic.lower() for word in ('warning', 'syntax error', "couldn't resolve", 'unable to', 'failed',
           'access violation', 'numeric expression missing', 'range error', 'waitforevent', 'illegal column count')):
        raise CanaryError('uncertain debugger observation')
    return output


def write_json(path, value):
    path.write_bytes(canonical(value))


def verify_diagnostic_site(executable):
    """The pinned EXE's native marker WriteFile CALL, not a global API hook."""
    import pefile
    with pefile.PE(data=executable) as pe:
        rva = 0x7e78
        code = pe.get_data(rva, 6)
        imports = {i.address: i.name for d in pe.DIRECTORY_ENTRY_IMPORT for i in d.imports}
        if code[:2] != b'\xff\x15' or imports.get(pe.OPTIONAL_HEADER.ImageBase + rva + 6 + struct.unpack('<i', code[2:])[0]) != b'WriteFile':
            raise CanaryError('native diagnostic instruction/IAT changed')
    return rva


def render_case_commands(anchors, location, profile):
    commands = render_commands(anchors, profile)
    if location in DIST_POLLUTION and base_profile(profile) == 'normal':
        rva = anchors['diagnostic_call']
        command = (f'bp @$t0+0x{rva:x} ".printf \\"W3_LD_DIAGNOSTIC owner={rva:x} bytes=%x text=%ma\\", @r8d, @rdx;.echo;gc"')
        commands = commands.replace('.echo W3_INITIAL_MODULES_BEGIN', command + '\n.echo W3_INITIAL_MODULES_BEGIN', 1)
    return commands


def checked_fixture_pe(path, dll):
    import pefile
    with pefile.PE(data=path.read_bytes()) as pe:
        imports = {d.dll.decode().upper(): sorted(i.name.decode() for i in d.imports) for d in pe.DIRECTORY_ENTRY_IMPORT}
        wanted = {'KERNEL32.DLL': ['OutputDebugStringW'] if dll else ['ExitProcess', 'LoadLibraryExW']}
        if pe.FILE_HEADER.Machine != 0x8664 or imports != wanted or bool(pe.FILE_HEADER.Characteristics & 0x2000) != dll:
            raise CanaryError('fixture PE has unapproved imports/architecture/kind')
        for index in (9, 11, 13):  # TLS, bound imports, delay imports
            if pe.OPTIONAL_HEADER.DATA_DIRECTORY[index].Size:
                raise CanaryError('fixture has unexpected TLS/bound/delay directory')
        return {'machine': 'x64', 'imports': imports, 'entry': pe.OPTIONAL_HEADER.AddressOfEntryPoint,
                'bytes': byte_fact(path.read_bytes())}


def setup_environment(args, msvc, sdk, directory, evidence):
    """Bound and record vcvars itself; retain the existing locked projection check."""
    if any(c in str(args.vcvarsall) for c in '%\r\n'):
        raise CanaryError('invalid vcvarsall path')
    system = Path(os.environ['SystemRoot']) / 'System32'
    initial = {'SystemRoot': os.environ['SystemRoot'], 'WINDIR': os.environ['SystemRoot'],
               'PATH': str(system), 'TEMP': str(directory), 'TMP': str(directory),
               'ComSpec': str(system / 'cmd.exe'), 'VSCMD_SKIP_SENDTELEMETRY': '1'}
    # subprocess's list2cmdline escaping is not cmd.exe's /c quoting grammar.
    command = (f'"{system / "cmd.exe"}" /d /c '
               f'call "{args.vcvarsall}" x64 {sdk} -vcvars_ver={msvc} >nul && set')
    record_command(command, directory, initial, directory, 'vcvars', evidence['commands'], timeout=args.timeout)
    expanded = dict(line.split('=', 1) for line in (directory / 'vcvars.stdout').read_text(encoding='utf-8').splitlines()
                    if '=' in line and not line.startswith('='))
    allowed = {'include','lib','libpath','ucrtversion','universalsdkdir','universalcrtsdkdir',
               'vctoolsinstalldir','vctoolsversion','windowssdkbinpath','windowssdkdir','windowssdkversion'}
    environment = {key: value for key, value in expanded.items() if key.casefold() in allowed}
    environment.update(initial)
    environment['PATH'] = str(Path(environment['VCToolsInstallDir']) / 'bin/Hostx64/x64') + os.pathsep + str(system)
    environment, projection = finalize_compile_environment(environment, msvc, sdk)
    return environment, projection


def compile_fixture(args, candidate, directory, evidence):
    msvc = candidate['toolchain']['msvc']['toolset_version']
    sdk = candidate['toolchain']['windows_sdk']['version']
    environment, projection = setup_environment(args, msvc, sdk, directory, evidence)
    aggregates = verified_toolchain_aggregates(environment, sdk, candidate)
    tool_bin = Path(environment['VCToolsInstallDir']) / 'bin/Hostx64/x64'
    cl, link = tool_bin / 'cl.exe', tool_bin / 'link.exe'
    kernel32 = Path(environment['WindowsSdkDir']) / 'Lib' / sdk / 'um/x64/kernel32.lib'
    for key, path in (('cl', cl), ('link', link)):
        checked_bytes(path, candidate['toolchain']['msvc']['tools'][key])
    source = ROOT / 'tests/native/windows_frozen_loader_canary.c'
    evidence.update(environment=environment, projection=projection, aggregates_pre=aggregates,
                    inputs={str(p): byte_fact(p.read_bytes()) for p in (source, cl, link, kernel32, args.vcvarsall)})
    for index, command in enumerate(build_commands(cl, link, kernel32, source, directory)):
        record_command(command, directory, environment, directory, 'build-' + str(index), evidence['commands'], timeout=args.timeout)
    evidence['aggregates_post'] = verified_toolchain_aggregates(environment, sdk, candidate)
    if evidence['aggregates_post'] != aggregates or any(byte_fact(Path(p).read_bytes()) != fact for p, fact in evidence['inputs'].items()):
        raise CanaryError('fixture build inputs changed')
    dll, exe = directory / 'localcat-loader-canary.dll', directory / 'canary-control.exe'
    evidence['dll'] = checked_fixture_pe(dll, True); evidence['exe'] = checked_fixture_pe(exe, False)
    return dll, exe


def cdb_child(args, directory, exe, cwd, environment, script_text, expected_exit):
    script = directory / 'observe.cdb'; script.write_text(script_text, encoding='ascii', newline='\n')
    (directory / 'symbols').mkdir()
    command = [str(args.cdb), '-y', str(directory / 'symbols'), '-cf', str(script), str(exe)]
    record = {'commands': [], 'cwd': str(cwd), 'environment': environment,
              'script_pre': byte_fact(script.read_bytes()), 'expected_exit': expected_exit}
    write_json(directory / 'command.json', {**record, 'argv': command, 'timeout': args.timeout})
    try:
        try:
            record_command(command, cwd, environment, directory, 'cdb', record['commands'], timeout=args.timeout)
        except ProbeInputError:
            if not record['commands'] or record['commands'][-1].get('exit_code') != expected_exit:
                raise
        if record['commands'][-1]['exit_code'] != expected_exit:
            raise CanaryError('wrong CDB exit')
        output = (directory / 'cdb.stdout').read_text(encoding='utf-8')
        stderr = (directory / 'cdb.stderr').read_text(encoding='utf-8')
        record['script_post'] = byte_fact(script.read_bytes())
        if record['script_post'] != record['script_pre']:
            raise CanaryError('observation script changed')
        return clean_debugger_output(output, stderr, exe), stderr, record
    finally:
        write_json(directory / 'observation.json', record)


def positive_control(args, output, dll, exe, location='clean'):
    directory = output / ('positive-control' if location == 'clean' else 'positive-' + location); directory.mkdir()
    local_exe = directory / exe.name
    shutil.copyfile(exe, local_exe)
    additions = ({dll.name: dll.read_bytes()} if location == 'clean' else
                 redirection_layout(location, exe.name, (dll.name,), dll.read_bytes()))
    paths = [local_exe]
    for relative, data in additions.items():
        path = directory / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data); paths.append(path)
    dll_relative = dll.name if location == 'clean' else (
        exe.name + '.local/' + dll.name if location == 'local-dir' else 'LocalCAT.Canary/' + dll.name)
    local_dll = directory / dll_relative
    before = {str(p): byte_fact(p.read_bytes()) for p in paths}
    temp = directory / 'temp'; temp.mkdir()
    script = '\n'.join(['.echo W3_CANARY_CONTROL_BEGIN',
        'sxe -c ".echo W3_CANARY_CONTROL_EXIT;.lastevent;q" epr',
        'sxe -c ".echo W3_CANARY_CONTROL_AV;.lastevent;q" av',
        'bp @$exentry ".echo W3_CANARY_CONTROL_ENTRY;gc"', 'g', ''])
    env = case_environment(os.environ, 'clean', 'normal', directory, temp)
    raw, err, record = cdb_child(args, directory, local_exe, directory, env, script, 0)
    record['trace'] = validate_positive(raw, err, local_dll)
    record['pre'] = before; record['post'] = {str(p): byte_fact(p.read_bytes()) for p in paths}
    if record['post'] != before:
        raise CanaryError('positive fixture changed')
    record['passed'] = True; write_json(directory / 'result.json', record)
    return record


def run_case(args, case, output, source_pre, dll, anchors):
    name, location, profile = case
    directory = output / name; directory.mkdir(); bundle = directory / 'dist'
    shutil.copytree(args.dist, bundle)
    if inventory(bundle) != source_pre:
        raise CanaryError('dist copy differs before pollution')
    bait = directory / 'bait'; bait.mkdir(); cwd = directory / 'cwd'; cwd.mkdir(); temp = directory / 'temp'; temp.mkdir()
    targets = []
    additions = {}
    if location in REDIRECTIONS:
        additions = redirection_layout(location, EXE, BAIT_NAMES, dll.read_bytes())
        for relative, data in additions.items():
            target = bundle / relative
            if target.exists():
                raise CanaryError('refusing to overwrite redirection target')
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            if relative.endswith('.dll'):
                targets.append(target)
    elif location != 'clean':
        for basename in BAIT_NAMES:
            target = (bundle if location == 'appdir' else bait) / basename
            if target.exists():
                raise CanaryError('refusing to overwrite bait target')
            shutil.copyfile(dll, target); targets.append(target)
    pre = inventory(bundle); bait_pre = inventory(bait)
    expected = (inventory_with_additions(source_pre, additions) if location in REDIRECTIONS else
                expected_inventory(source_pre, location, byte_fact(dll.read_bytes())))
    if pre != expected:
        raise CanaryError('unexpected prelaunch dist delta')
    expected_bait = {'files': {n: byte_fact(dll.read_bytes()) for n in BAIT_NAMES} if location in ('cwd', 'path') else {}, 'directories': []}
    if bait_pre != expected_bait:
        raise CanaryError('unexpected bait directory delta')
    if location == 'cwd':
        cwd = bait
    elif location == 'clean':
        cwd = Path(os.environ['SystemRoot']) / 'System32'
    env = case_environment(os.environ, location, profile, bait, temp)
    expected_exit = 1 if base_profile(profile) == 'policy-false' or location in DIST_POLLUTION else 0
    record = {'name': name, 'location': location, 'profile': profile, 'pre': pre, 'bait_pre': bait_pre,
              'targets': [str(p) for p in targets], 'passed': False}
    write_json(directory / 'prelaunch.json', record)
    try:
        raw, err, observed = cdb_child(args, directory, bundle / EXE, cwd, env,
                                     render_case_commands(anchors, location, profile), expected_exit)
        record.update(observed)
        reject_canary(raw, err, targets)
        system32 = str(Path(os.environ['SystemRoot']) / 'System32')
        if location in DIST_POLLUTION and base_profile(profile) == 'normal':
            trace = validate_appdir(raw, err, system32, profile)
        else:
            trace = parse_loader_events(raw, system32, str(bundle), profile)
            validate_profile_calls(trace, profile); validate_stderr(err, profile)
        record['trace'] = trace; record['module_events'] = module_events(raw)
        record['post'] = inventory(bundle); record['bait_post'] = inventory(bait)
        if record['post'] != pre or record['bait_post'] != bait_pre or pre['files'][EXE] != source_pre['files'][EXE]:
            raise CanaryError('case inputs or EXE changed')
        record['passed'] = True
    finally:
        write_json(directory / 'result.json', record)
    return record


def run(args):
    if os.name != 'nt' or not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        raise CanaryError('Windows required; timeout must be in (0,60] seconds')
    for name in ('dist', 'release', 'cdb', 'vcvarsall'):
        path = Path(getattr(args, name)).absolute(); _no_reparse_ancestors(path); setattr(args, name, path)
    release_bytes = args.release.read_bytes()
    external = dict(expected_release_sha256=args.expected_release_sha256,
                    expected_repository_commit=args.expected_commit, expected_candidate_input_digest=args.expected_candidate_digest)
    binding = verify_release_binding(release_bytes, args.dist, **external)
    if set(binding['dist']) != set(PROFILE) | {EXE, MANIFEST}:
        raise CanaryError('requires exact twelve-file minimal candidate')
    candidate_path = ROOT / 'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate = load_candidate(candidate_path)
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise CanaryError('candidate input differs from external anchor')
    import pefile
    lock = candidate['evidence_producer']['pefile']; checked_bytes(Path(pefile.__file__), lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if pefile.__version__ != lock['version'] or metadata is None or byte_fact(metadata.encode()) != {k: lock['distribution_metadata'][k] for k in ('bytes', 'sha256')}:
        raise CanaryError('unlocked PE parser')
    anchors = verify_sites(checked_bytes(args.dist / EXE, binding['executable']),
                           checked_bytes(args.dist / '_internal/python314.dll', candidate['runtime']['cpython']['dll']))
    anchors['diagnostic_call'] = verify_diagnostic_site(checked_bytes(args.dist / EXE, binding['executable']))
    source_pre = inventory(args.dist)
    artifacts = ROOT / 'artifacts/windows'; artifacts.mkdir(parents=True, exist_ok=True); _no_reparse_ancestors(artifacts)
    output = Path(tempfile.mkdtemp(prefix='loader-canary-', dir=artifacts))
    print('LOADER_CANARY=' + str(output), flush=True)
    observer = output / 'observer'; observer.mkdir()
    names = [name for name in observer_inventory(args.cdb) if name != 'cdb.exe']
    files = ['tools/' + name for name in names] + ['tools/' + name for name in (
        'verify_windows_frozen_loader_canary.py', 'trace_windows_frozen_loader_resolution.py',
        'verify_windows_frozen_payload.py', 'audit_windows_frozen_custom_entry.py')]
    files += ['tests/native/windows_frozen_loader_canary.c', 'tests/test_windows_frozen_loader_canary.py',
              'packaging/windows/frozen-entry/candidate-input.lock.json']
    code = {}
    for relative in sorted(set(files)):
        data = (ROOT / relative).read_bytes(); code[relative] = byte_fact(data)
        target = observer / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    observer_pre = inventory(observer)
    tool_facts = {'cdb': byte_fact(args.cdb.read_bytes()), 'pefile': byte_fact(Path(pefile.__file__).read_bytes()),
                  'pefile_metadata': byte_fact(metadata.encode()), 'python': byte_fact(Path(sys.executable).read_bytes())}
    anchor_bytes = canonical(external); (output / 'anchors.json').write_bytes(anchor_bytes)
    (output / 'release.json').write_bytes(release_bytes)
    invocation = [sys.executable, '-B', str(observer / 'tools/verify_windows_frozen_loader_canary.py'),
        '--dist', str(args.dist), '--release', str(args.release), '--cdb', str(args.cdb), '--vcvarsall', str(args.vcvarsall),
        '--expected-release-sha256', args.expected_release_sha256, '--expected-commit', args.expected_commit,
        '--expected-candidate-digest', args.expected_candidate_digest, '--timeout', str(args.timeout)]
    write_json(output / 'command.json', {'actual_argv': sys.argv, 'caller_cwd': str(Path.cwd()), 'replay_argv': invocation,
        'powershell': '& ' + ' '.join("'" + a.replace("'", "''") + "'" for a in invocation)})
    report = {'classification': CLASSIFICATION, 'limitations': LIMITATIONS, 'anchors': external, 'machine_anchors': anchors,
              'source_pre': source_pre, 'release_pre': byte_fact(release_bytes), 'code_inventory': code,
              'observer_pre': observer_pre, 'tools_pre': tool_facts, 'output': str(output),
              'expected_cases': [c[0] for c in CASES], 'cases': [], 'build': {'commands': []}, 'passed': False}
    write_json(output / 'prelaunch.json', report)
    try:
        build = output / 'build'; build.mkdir()
        dll, exe = compile_fixture(args, candidate, build, report['build'])
        report['build_pre'] = inventory(build)
        report['positive_control'] = positive_control(args, output, dll, exe)
        report['redirection_controls'] = [positive_control(args, output, dll, exe, location)
                                          for location in ('local-dir', 'sxs-external')]
        for case in CASES:
            if inventory(args.dist) != source_pre or args.release.read_bytes() != release_bytes:
                raise CanaryError('source drift; no further child launched')
            print('CANARY_CASE=' + case[0], flush=True)
            report['cases'].append(run_case(args, case, output, source_pre, dll, anchors))
        report['build_post'] = inventory(build)
        report['source_post'] = inventory(args.dist); report['observer_post'] = inventory(observer)
        report['tools_post'] = {'cdb': byte_fact(args.cdb.read_bytes()), 'pefile': byte_fact(Path(pefile.__file__).read_bytes()),
            'pefile_metadata': byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode()),
            'python': byte_fact(Path(sys.executable).read_bytes())}
        if (report['build_pre'] != report['build_post'] or report['source_post'] != source_pre
                or report['observer_post'] != observer_pre or report['tools_post'] != tool_facts
                or any(byte_fact((ROOT / p).read_bytes()) != fact for p, fact in code.items())
                or (output / 'anchors.json').read_bytes() != anchor_bytes or args.release.read_bytes() != release_bytes):
            raise CanaryError('post-observation inputs changed')
        verify_release_binding(release_bytes, args.dist, **external)
        report['passed'] = len(report['cases']) == len(CASES) and all(c['passed'] for c in report['cases'])
    except (OSError, ValueError, SystemExit) as exc:
        report['error'] = str(exc)
    finally:
        write_json(output / 'result.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist', 'release', 'cdb', 'vcvarsall'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (OSError, ValueError, SystemExit) as exc:
        print('CANARY_INPUT_REJECTED: ' + str(exc), file=sys.stderr); return 2
    print(canonical({'classification': CLASSIFICATION, 'passed': result['passed'], 'evidence': result['output'],
                     'error': result.get('error')}).decode())
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
