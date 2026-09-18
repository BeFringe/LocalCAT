"""Bounded final-PE E9 path observations, not an all-IO or whole-W3 proof.

Exact native/source control flow remains the closure argument. These traces
cross-check it on new copies, including legitimate retained-file opens. A
RootDirectory-relative NT name must resolve to the real File handle or reject.
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
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

from tools.probe_windows_frozen_custom_runw import (
    ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_api_binding import observer_inventory
from tools.trace_windows_frozen_retained_failures import (
    bytes_guard, unicode_guard, guarded, verify_machine_sites, prepare_pollution,
    validate_stderr as validate_failure_stderr,
)
from tools.trace_windows_frozen_loader_resolution import validate_stderr as validate_loader_stderr
from tools.verify_windows_frozen_payload import inventory, _environment
from tools.windows_frozen_release import verify_release_binding

PROFILES = ('normal', 'parent-pyvenv', 'exe-pth', 'dll-pth', 'launcher-empty',
            'launcher-nonempty', 'semicolon', 'clear-failure-stats', 'clear-failure-null-handles')
NATIVE_TEXT_SHA = 'e6f316d59dc36bfce3cc0a366f2010f09b361d9f0d74eff4f5d8ad66ea98fa8c'
EARLY = {'exe-pth': 'FROZEN_ENTRY.INVENTORY_EXTRA',
         'dll-pth': 'FROZEN_ENTRY.INVENTORY_EXTRA',
         'launcher-empty': 'FROZEN_ENTRY.LAUNCHER_ENVIRONMENT_REJECTED',
         'launcher-nonempty': 'FROZEN_ENTRY.LAUNCHER_ENVIRONMENT_REJECTED',
         'semicolon': 'FROZEN_ENTRY.INITIALIZATION_PATH_INVALID'}
SOURCES = ('encoding-package', 'encoding-aliases', 'encoding-utf8', 'encoding-win', 'importlib-external')


def number(value):
    return int(value.replace('`', ''), 16)


def normalize_path(path, devices):
    path = path.replace('/', '\\')
    if path.lower() in ('\\??\\mountpointmanager', '\\device\\cng'):
        return path.lower()
    for prefix in ('\\??\\', '\\\\?\\'):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if path.lower().startswith('\\device\\'):
        matches = [(key, value) for key, value in devices.items()
                   if path.lower() == key.lower() or path.lower().startswith(key.lower() + '\\')]
        if len(matches) != 1:
            raise ProbeInputError('unresolved NT device path')
        key, value = matches[0]
        path = value + path[len(key):]
    if not re.match(r'^[a-zA-Z]:\\', path) or any(piece in ('.', '..') for piece in path.split('\\')):
        raise ProbeInputError('unknown, relative, or traversal file path')
    return ntpath.normcase(ntpath.normpath(path))


def parse_files(output, devices):
    files, current = [], None
    for line in output.splitlines():
        if line.startswith('W3_EP_FILE_BEGIN '):
            if current is not None:
                raise ProbeInputError('nested NT file observation')
            match = re.fullmatch(r'W3_EP_FILE_BEGIN (NtCreateFile|NtOpenFile|NtQueryAttributesFile|NtQueryFullAttributesFile) '
                                 r'tid=([0-9a-fA-F`]+) root=([0-9a-fA-F`]+) path=(.+)', line)
            if match is None:
                raise ProbeInputError('malformed NT file observation')
            current = {'api': match[1], 'thread': number(match[2]), 'root': number(match[3]),
                       'raw_path': match[4], 'handle_output': []}
        elif line == 'W3_EP_FILE_END':
            if current is None:
                raise ProbeInputError('unmatched NT file terminator')
            name = current['raw_path']
            if current['root']:
                block = '\n'.join(current['handle_output'])
                names = re.findall(r'^\s*Name\s+([^\n]+)$', block, re.M)
                handles = re.findall(r'^Handle ([0-9a-fA-F`]+)\s*$', block, re.M)
                if (len(names) != 1 or len(handles) != 1 or number(handles[0]) != current['root']
                        or re.search(r'^\s*Type\s+File\s*$', block, re.M) is None
                        or ntpath.isabs(name) or re.match(r'^[a-zA-Z]:', name)):
                    raise ProbeInputError('RootDirectory handle not unambiguously resolved')
                name = names[0].rstrip('\\') + '\\' + name
            elif current['handle_output']:
                raise ProbeInputError('unexpected handle resolution for absolute NT name')
            current['path'] = normalize_path(name, devices)
            files.append(current)
            current = None
        elif line.startswith('W3_EP_FILE_'):
            raise ProbeInputError('unknown or malformed NT file event')
        elif current is not None:
            if line.startswith(('W3_EP_', 'FROZEN_ENTRY.', 'Last event:')):
                raise ProbeInputError('NT file record crosses another event or process exit')
            if line.strip():
                current['handle_output'].append(line)
    if current is not None:
        raise ProbeInputError('unterminated NT file observation')
    return files


def classify_files(files, bundle, members, windows, devices):
    bundle = normalize_path(str(bundle), devices)
    system = normalize_path(str(windows), devices)
    allowed = {bundle}
    parent = ntpath.dirname(bundle)
    while parent and parent != ntpath.dirname(parent):
        allowed.add(parent)
        parent = ntpath.dirname(parent)
    allowed.add(parent)
    for member in members:
        name = bundle + '\\' + member.replace('/', '\\').lower()
        allowed.add(name)
        parent = ntpath.dirname(name)
        while parent.startswith(bundle + '\\'):
            allowed.add(parent)
            parent = ntpath.dirname(parent)
    counts = {'retained': 0, 'trusted_os': 0}
    for item in files:
        path = item['path']
        if path in allowed:
            counts['retained'] += 1
        elif (path in ('\\??\\mountpointmanager', '\\device\\cng') or path.startswith(system + '\\system32\\')
              or path.startswith(system + '\\winsxs\\')):
            # Existing OS trust boundary, not new file-authority requirements.
            counts['trusted_os'] += 1
        elif re.fullmatch(r'[a-z]:\\program files\\windowsapps\\microsoft\.languageexperiencepack[^\\]+\\windows\\system32\\[^\\]+\\[0-9a-f]+\\tzres\.dll\.mui', path):
            counts['trusted_os'] += 1
        else:
            raise ProbeInputError('undeclared/non-system E9 file request: ' + path)
    return counts


def validate_exit(output, expected):
    all_exits = re.findall(r'^Last event: .*: Exit process .*?, code ([0-9a-fA-F]+)\s*$', output, re.M)
    terminal = re.findall(r'^W3_EP_EXIT\nLast event: [^\n]*: Exit process [^,\n]*, code ([0-9a-fA-F]+)\s*$', output, re.M)
    if len(all_exits) != 1 or len(terminal) != 1 or all_exits != terminal or int(terminal[0], 16) != expected:
        raise ProbeInputError('missing, misplaced, duplicate, or wrong terminal process exit')
    return expected


def validate_stats(output, profile, windows, devices):
    events = [line for line in output.splitlines() if line.startswith('W3_EP_STATS_')]
    if profile != 'clear-failure-stats':
        if events:
            raise ProbeInputError('unexpected stats loader observation')
        return None
    if len(events) != 4 or events[2:] != ['W3_EP_STATS_HOST_BEGIN', 'W3_EP_STATS_HOST_END']:
        raise ProbeInputError('missing, duplicate, or unknown stats event')
    call = re.fullmatch(r'W3_EP_STATS_CALL tid=([0-9a-f]+) sp=([0-9a-f`]+) name=psapi.dll', events[0])
    returned = re.fullmatch(r'W3_EP_STATS_RETURN tid=([0-9a-f]+) sp=([0-9a-f`]+) module=([0-9a-f`]+)', events[1])
    if (call is None or returned is None or (call[1], call[2]) != (returned[1], returned[2])
            or number(call[1]) == 0 or number(returned[3]) == 0):
        raise ProbeInputError('unpaired stats load call/return')
    host = re.findall(r'^W3_EP_STATS_HOST_BEGIN\n(.*?)^W3_EP_STATS_HOST_END$', output, re.M | re.S)
    rows = re.findall(r'^([0-9a-f`]+)\s+([0-9a-f`]+)\s+\S+\s+([a-zA-Z]:[^\n]+)$', host[0] if len(host) == 1 else '', re.M)
    expected = normalize_path(str(windows) + '/System32/psapi.dll', devices)
    if len(rows) != 1 or number(rows[0][0]) != number(returned[3]) or normalize_path(rows[0][2], devices) != expected:
        raise ProbeInputError('stats HMODULE is not the observed System32 PSAPI module')
    lines = output.splitlines()
    diagnostics = [index for index, line in enumerate(lines)
                   if line == 'W3_EP_DIAGNOSTIC FROZEN_ENTRY.INITIALIZATION_FAILED']
    exits = [index for index, line in enumerate(lines) if line == 'W3_EP_EXIT']
    positions = [index for index, line in enumerate(lines) if line.startswith('W3_EP_STATS_')]
    if (len(diagnostics) != 1 or len(exits) != 1
            or not diagnostics[0] < positions[0] < positions[1] < positions[2] < positions[3] < exits[0]):
        raise ProbeInputError('stats not in actual failure cleanup')
    return {'call': events[0], 'return': events[1], 'system_host': expected}


def render_commands(profile, anchors):
    if profile not in PROFILES:
        raise ProbeInputError('unknown E9 path profile')
    def py(rva):
        delta = rva - 0x309B84
        return f'python314!Py_Initialize{"+" if delta >= 0 else "-"}0x{abs(delta):x}'
    def bp(where, code):
        return f'bu {where} "{code}gc"'
    commands = ['.echo W3_EP_BEGIN', f'r @$t0=@$exentry-0x{anchors["entry"]:x}']
    commands += [f'r @$t{i}=0' for i in range(1, 20)]
    commands += ['sxe -c ".echo W3_EP_EXIT;.lastevent;q" epr',
                 'sxe -c ".echo W3_EP_ACCESS_VIOLATION;.lastevent;gn" -c2 ".echo W3_EP_ACCESS_VIOLATION;.lastevent;gn" av',
        bp('@$t0+0x140c', 'r @$t4=1;r @$t14=@$tid;.echo W3_EP_WINDOW_BEGIN;'),
        bp('@$t0+0x1411', '.printf \\"W3_EP_CONFIG_RETURN %x\\", @eax;.echo;'),
        bp('@$t0+0x141e', '.echo W3_EP_INIT_BEGIN;'),
        bp('@$t0+0x1423', '.printf \\"W3_EP_INIT_RETURN %x\\", @eax;.echo;.if (@eax==0) {r @$t4=0;.echo W3_EP_WINDOW_END;};'),
        bp('@$t0+0x3a30', 'r @$t10=@ecx;.printf \\"W3_EP_ADAPTER %x tid=%x\\", @ecx, @$tid;.echo;'),
        bp(py(0x43860), '.if (@$tid==@$t14) {r @$t15=@rcx;};')]
    if profile == 'clear-failure-null-handles':
        # Observer-only state fault at the PE entry, before this image's CRT.
        # x64 RTL_USER_PROCESS_PARAMETERS StandardInput/Output/Error. Do not
        # call arbitrary debugger functions or change the parent process.
        commands.append(bp('@$exentry', 'r @$t7=poi(@$peb+0x20);eq @$t7+0x20 0 0 0;'
                           '.printf \\"W3_EP_NULL_HANDLES %p %p %p\\", poi(@$t7+0x20), poi(@$t7+0x28), poi(@$t7+0x30);.echo;'))
        conditions = ['@rcx==0', '@r9d==0x10', *bytes_guard('@r8', 'LocalCAT'),
                      *bytes_guard('@rdx', 'FROZEN_ENTRY.INITIALIZATION_FAILED')]
        commands.append(bp('@$t0+0x5c84', guarded(conditions,
            '.echo W3_EP_UI_FALLBACK;r rax=1;r rip=@rip+6;', True).replace('W3_RF_', 'W3_EP_')))
    check = ['@$tid==@$t14', '@$t10==4', 'by(@$t0+0x2d0bf)==0',
             f'@rax=={py(0x3146C0)}', *bytes_guard('@rcx', 'path')]
    commands.append(bp('@$t0+0x3b51', guarded(check, '.echo W3_EP_PATH_GET;', True).replace('W3_RF_', 'W3_EP_')))
    action = ''
    if profile.startswith('clear-failure-'):
        action = guarded(['@$t9==0', '@$tid==@$t14', '@$t10==4', '@rax!=0'],
                         '.echo W3_EP_INJECT_BORROWED_NULL;r @$t9=1;r rax=0;', True).replace('W3_RF_', 'W3_EP_')
    commands.append(bp('@$t0+0x3b57', action + '.printf \\"W3_EP_PATH_RETURN %p\\", @rax;.echo;'))
    commands.append(bp('@$t0+0x3c4c', '.printf \\"W3_EP_PATH_CLEARED %x\\", by(@$t0+0x2d0bf);.echo;'))
    commands.append(bp('@$t0+0x4064', '.printf \\"W3_EP_COMPILE %x cleared=%x name=%ma\\", @$t10, by(@$t0+0x2d0bf), @rdx;.echo;'))
    commands.append(bp('@$t0+0x40bc', '.if (@$t10==4) {.if (@rax!=0) {r @$t8=1;} .else {.echo W3_EP_GUARD_REJECT;};};'))
    # Prove the actual built-in call's current Python frame chain at install.
    body = 'r @$t16=1;r @$t18=poi(@$t15+0x48);'
    names = ('_get_supported_file_loaders', '_install', '_install_external_importers')
    for index, name in enumerate(names):
        body += 'r @$t19=(poi(@$t18)&0xfffffffffffffffe);r @$t17=poi(@$t19+0x78);'
        body += 'r @$t16=(@$t16 & ' + ' & '.join('(' + check + ')' for check in unicode_guard('@$t17', name)) + ');'
        if index < 2:
            body += 'r @$t17=poi(@$t19+0x70);'
            body += 'r @$t16=(@$t16 & ' + ' & '.join('(' + check + ')' for check in unicode_guard('@$t17', '<localcat-retained:importlib-external>')) + ');'
        if index < 2:
            body += 'r @$t18=poi(@$t18+8);'
    body += '.if (@$t16==1) {.echo W3_EP_INSTALL;} .else {.echo W3_EP_GUARD_REJECT;};'
    commands.append(bp(py(0x1C22F4), '.if (@$t8==1) {' + guarded(['@$t4==1', '@$tid==@$t14', '@$t15!=0'], body, True).replace('W3_RF_', 'W3_EP_') + '};'))
    commands += [bp('@$t0+0x2000', '.if (@$t4==1) {.echo W3_EP_ABORT;};'),
                 bp('@$t0+0x2040', '.echo W3_EP_HANDOFF;'),
                 bp('@$t0+0x3e94', guarded([f'@rax=={py(0xC2158)}', f'@rcx==poi({py(0x5DFCF0)})',
                    *bytes_guard('@rdx', 'FROZEN_ENTRY.INTERPRETER_PATH_CLEAR_FAILED')],
                    '.echo W3_EP_RUNTIME_ERROR;', True).replace('W3_RF_', 'W3_EP_')),
                 bp('@$t0+0x14df', '.printf \\"W3_EP_DIAGNOSTIC %ma\\", @rcx;.echo;')]
    for name, root, string in (('NtCreateFile', 'poi(@r8+8)', 'poi(@r8+0x10)'),
                               ('NtOpenFile', 'poi(@r8+8)', 'poi(@r8+0x10)'),
                               ('NtQueryAttributesFile', 'poi(@rcx+8)', 'poi(@rcx+0x10)'),
                               ('NtQueryFullAttributesFile', 'poi(@rcx+8)', 'poi(@rcx+0x10)')):
        code = (f'.printf \\"W3_EP_FILE_BEGIN {name} tid=%x root=%p path=%msu\\", @$tid, {root}, {string};.echo;'
                f'.if ({root}!=0) {{!handle {root} f File;}};.echo W3_EP_FILE_END;')
        commands.append(bp('ntdll!' + name, '.if (@$t4==1) {' + code + '};'))
    # The failure+stats profile observes the exact existing mimalloc PSAPI load.
    commands += [bp(py(0x2B059F), '.printf \\"W3_EP_STATS_CALL tid=%x sp=%p name=%ma\\", @$tid, @rsp, @rcx;.echo;'),
                 bp(py(0x2B05A5), '.printf \\"W3_EP_STATS_RETURN tid=%x sp=%p module=%p\\", @$tid, @rsp, @rax;.echo;.echo W3_EP_STATS_HOST_BEGIN;lmf a @rax;.echo W3_EP_STATS_HOST_END;')]
    return '\n'.join([*commands, 'g', '']).replace('\\\\"', '\\"')


def _devices():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.QueryDosDeviceW
    query.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    query.restype = ctypes.c_uint32
    result = {}
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        buffer = ctypes.create_unicode_buffer(32768)
        if query(letter + ':', buffer, len(buffer)):
            result[buffer.value] = letter + ':'
    return result


def validate_trace(output, stderr, profile, bundle, members, windows, devices):
    warning = '*** WARNING: Unable to verify timestamp for ' + str(Path(bundle) / 'localcat-spike.exe').replace('/', '\\')
    if output.count(warning + '\n') == 1:
        output = output.replace(warning + '\n', '')
    if re.search(r'warning|unable to|couldn.t|cannot|failed to|bad syntax|syntax error|memory access error|access violation|waitforevent', output, re.I):
        raise ProbeInputError('debugger uncertainty in E9 observation')
    failure = profile in EARLY or profile.startswith('clear-failure-')
    validate_exit(output, int(failure))
    stats = validate_stats(output, profile, windows, devices)
    active = False
    exited = False
    for line in output.splitlines():
        if line == 'W3_EP_WINDOW_BEGIN':
            if active or exited:
                raise ProbeInputError('duplicate E9 window start')
            active = True
        elif line == 'W3_EP_WINDOW_END':
            active = False
        elif line == 'W3_EP_EXIT':
            active = False
            exited = True
        elif line.startswith('W3_EP_FILE_') and not active:
            raise ProbeInputError('file observation outside claimed E9 window')
    files = parse_files(output, devices)
    counts = classify_files(files, bundle, members, windows, devices)
    events = [line for line in output.splitlines() if line.startswith(('W3_EP_', 'FROZEN_ENTRY.'))
              and not line.startswith(('W3_EP_FILE_', 'W3_EP_STATS_'))]
    if profile in EARLY:
        marker = EARLY[profile]
        expected = ['W3_EP_BEGIN']
        if profile == 'semicolon':
            expected += ['W3_EP_WINDOW_BEGIN', 'W3_EP_CONFIG_RETURN 0', 'W3_EP_INIT_BEGIN']
            # Filled from exact native path: initialization rejects before begin.
            expected += ['W3_EP_ABORT', 'W3_EP_INIT_RETURN ffffffff', 'W3_EP_ABORT']
        expected += ['W3_EP_DIAGNOSTIC ' + marker, 'W3_EP_EXIT']
        if events != expected or stderr != marker + '\n':
            raise ProbeInputError('wrong early layout rejection or stderr')
    else:
        if profile == 'clear-failure-null-handles':
            if len(events) < 2 or events[1] != 'W3_EP_NULL_HANDLES 0000000000000000 0000000000000000 0000000000000000':
                raise ProbeInputError('NULL handles not injected before native configuration')
            events = [events[0], *events[2:]]
        if events[:4] != ['W3_EP_BEGIN', 'W3_EP_WINDOW_BEGIN', 'W3_EP_CONFIG_RETURN 0', 'W3_EP_INIT_BEGIN']:
            raise ProbeInputError('E9 trace did not start before configure/initialize')
        if profile.startswith('clear-failure-'):
            if re.fullmatch(r'W3_EP_ADAPTER 4 tid=[0-9a-f]+', events[5]) is None:
                raise ProbeInputError('missing real external adapter')
            expected_mid = ['W3_EP_PATH_GET', 'W3_EP_INJECT_BORROWED_NULL', 'W3_EP_PATH_RETURN 0000000000000000',
                            'W3_EP_RUNTIME_ERROR', 'W3_EP_ABORT', 'W3_EP_ABORT', 'W3_EP_INIT_RETURN ffffffff',
                            'W3_EP_ABORT', 'W3_EP_DIAGNOSTIC FROZEN_ENTRY.INITIALIZATION_FAILED']
            expected_mid += (['W3_EP_UI_FALLBACK'] if profile == 'clear-failure-null-handles' else []) + ['W3_EP_EXIT']
            if events[6:] != expected_mid or events[4] != 'FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN':
                raise ProbeInputError('wrong injected-clear failure sequence')
            if profile == 'clear-failure-null-handles':
                # Win32 standard-handle slots are NULL at PE entry. The CRT's
                # inherited descriptor 2 is a separate channel retained by CDB.
                validate_failure_stderr('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n' + stderr +
                                       'FROZEN_ENTRY.INITIALIZATION_FAILED\n', 'clear-before-missing-path')
            else:
                split = stderr.find('heap stats:')
                if split < 0:
                    raise ProbeInputError('failure did not emit real mimalloc stats')
                validate_failure_stderr(stderr[:split].rstrip() + '\n', 'clear-before-missing-path')
                # Reuse the strict stats grammar; the original prefix above is
                # independently checked as failure, never as a successful run.
                validate_loader_stderr('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nFROZEN_ENTRY.SPIKE_COMPLETED\n' + stderr[split:], 'mimalloc-stats')
        else:
            adapters = [int(m[1], 16) for line in events if (m := re.fullmatch(r'W3_EP_ADAPTER ([0-4]) tid=[0-9a-f]+', line))]
            if adapters != [4, 0, 1, 3, 2]:
                raise ProbeInputError('missing, duplicate, or reordered retained adapters')
            if events.count('W3_EP_PATH_CLEARED 1') != 1 or events.count('W3_EP_INSTALL') != 1:
                raise ProbeInputError('path not cleared once or importlib install not exactly once')
            compiled = [line for line in events if line.startswith('W3_EP_COMPILE ')]
            if len(compiled) != 5 or any('cleared=1 name=<localcat-retained:' not in line for line in compiled):
                raise ProbeInputError('source compilation before clear or incomplete retained compilation')
            if events[-5:] != ['W3_EP_INIT_RETURN 0', 'W3_EP_WINDOW_END', 'W3_EP_HANDOFF', 'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_EP_EXIT']:
                raise ProbeInputError('normal E9 did not finish and hand off')
            if not counts['retained']:
                raise ProbeInputError('missing legitimate retained-file positive control')
            thread = re.fullmatch(r'W3_EP_ADAPTER 4 tid=([0-9a-f]+)', events[5])
            pointer = re.fullmatch(r'W3_EP_PATH_RETURN ([0-9a-fA-F]{16})', events[7])
            if thread is None or pointer is None or number(thread[1]) == 0 or number(pointer[1]) == 0:
                raise ProbeInputError('invalid adapter thread or real borrowed path object')
            expected = events[:4] + ['FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',
                'W3_EP_ADAPTER 4 tid=' + thread[1], 'W3_EP_PATH_GET', events[7], 'W3_EP_PATH_CLEARED 1',
                'W3_EP_COMPILE 4 cleared=1 name=<localcat-retained:importlib-external>', 'W3_EP_INSTALL']
            for index in (0, 1, 3, 2):
                expected += [f'W3_EP_ADAPTER {index} tid=' + thread[1],
                             f'W3_EP_COMPILE {index} cleared=1 name=<localcat-retained:{SOURCES[index]}>']
            expected += ['W3_EP_INIT_RETURN 0', 'W3_EP_WINDOW_END', 'W3_EP_HANDOFF', 'FROZEN_ENTRY.SPIKE_COMPLETED', 'W3_EP_EXIT']
            if events != expected:
                raise ProbeInputError('unknown, duplicate, or out-of-order E9 event')
            validate_failure_stderr(stderr, 'control')
    if any('GUARD_REJECT' in line for line in events):
        raise ProbeInputError('machine/frame guard rejected')
    return {'events': events, 'files': files, 'file_classes': counts, 'stats': stats, 'exit_code': int(failure)}


def run(args):
    if os.name != 'nt':
        raise ProbeInputError('actual E9 observation requires Windows')
    release_bytes = args.release.read_bytes()
    anchors = dict(expected_release_sha256=args.expected_release_sha256,
                   expected_repository_commit=args.expected_commit,
                   expected_candidate_input_digest=args.expected_candidate_digest)
    release = verify_release_binding(release_bytes, args.dist, **anchors)
    candidate_path = ROOT / 'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate_bytes = candidate_path.read_bytes()
    candidate = load_candidate(candidate_path)
    if candidate_path.read_bytes() != candidate_bytes:
        raise ProbeInputError('candidate changed during validation')
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise ProbeInputError('candidate not equal to external expectation')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if metadata is None or byte_fact(metadata.encode()) != {key: parser_lock['distribution_metadata'][key] for key in ('bytes', 'sha256')}:
        raise ProbeInputError('unlocked PE parser metadata')
    source_inventory = inventory(args.dist)
    machine = verify_machine_sites((args.dist / 'localcat-spike.exe').read_bytes(),
        (args.dist / '_internal/python314.dll').read_bytes(), candidate['entry_contract']['python_c_api'])
    with pefile.PE(str(args.dist / 'localcat-spike.exe')) as pe:
        text = next(s.get_data() for s in pe.sections if s.Name.rstrip(b'\0') == b'.text')
        if byte_fact(text)['sha256'] != NATIVE_TEXT_SHA:
            raise ProbeInputError('full native instruction layout changed')
    directory = Path(tempfile.mkdtemp(prefix='e9-paths-' + args.profile + '-', dir=ROOT / 'artifacts/windows'))
    bundle = directory / ('bundle;part' if args.profile == 'semicolon' else 'bundle with spaces')
    shutil.copytree(args.dist, bundle)
    if inventory(bundle) != source_inventory:
        raise ProbeInputError('fresh copy differs')
    if args.profile == 'parent-pyvenv':
        (directory / 'pyvenv.cfg').write_bytes(b'home = C:\\LOCALCAT_E9_POISON\ninclude-system-site-packages = true\n')
    for profile, name in (('exe-pth', 'localcat-spike._pth'), ('dll-pth', '_internal/python314._pth')):
        if args.profile == profile:
            (bundle / name).write_bytes(b'C:\\LOCALCAT_E9_POISON\nimport site\n')
    (directory / 'cwd').mkdir()
    (directory / 'symbols').mkdir()
    (directory / 'temp').mkdir()
    (directory / 'debugger-localappdata').mkdir()
    prepare_pollution(directory / 'cwd')
    environment, _ = _environment(directory / 'temp')
    environment['LOCALAPPDATA'] = str(directory / 'debugger-localappdata')
    environment['PYTHONPATH'] = str(directory / 'cwd')
    if args.profile.startswith('launcher-'):
        environment['__PYVENV_LAUNCHER__'] = '' if args.profile.endswith('empty') else str(directory / 'foreign-python.exe')
    if args.profile == 'clear-failure-stats':
        environment['MIMALLOC_SHOW_STATS'] = '1'
    script = directory / 'e9.cdb'
    script.write_text(render_commands(args.profile, machine), encoding='ascii', newline='\n')
    def observers():
        current = importlib.metadata.distribution('pefile').read_text('METADATA')
        return {**observer_inventory(args.cdb), **{name: byte_fact((ROOT / 'tools' / name).read_bytes()) for name in (
            Path(__file__).name, 'trace_windows_frozen_retained_failures.py', 'trace_windows_frozen_loader_resolution.py',
            'verify_windows_frozen_payload.py')}, 'pefile': byte_fact(Path(pefile.__file__).read_bytes()),
            'pefile-METADATA': byte_fact(current.encode()) if current is not None else None}
    before = observers()
    copied = inventory(bundle)
    pollution = inventory(directory / 'cwd')
    layout_files = {name: byte_fact((directory / name).read_bytes()) for name in ('pyvenv.cfg',) if (directory / name).exists()}
    devices = _devices()
    evidence = {'classification': 'BOUNDED_FINAL_PE_E9_PATH_OBSERVATION_NOT_W3_GATE', 'status': 'INCOMPLETE',
                'profile': args.profile, 'external_anchors': anchors, 'candidate_input': byte_fact(candidate_bytes),
                'source_inventory': source_inventory,
                'copy_inventory': copied, 'pollution': pollution, 'layout_files': layout_files, 'devices': devices,
                'environment': environment, 'observers': before, 'script': byte_fact(script.read_bytes()),
                'commands': [], 'limitations': ['named NT file APIs only; source/machine review remains the closure argument',
                'system file paths are observed within the existing trusted OS boundary, not per-file native authority',
                'software breakpoints alter timing; no-hit is not proof of unreachability',
                'discovery .pyd decoys are not executable DllMain probes',
                'NULL handles profile sets Win32 process parameters at PE entry; inherited CRT descriptor 2 is not removed',
                'UI fallback call is observed then returned IDOK; no claim of real dialog presentation']}
    print('E9_PATH_TRACE=' + str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), '-y', str(directory / 'symbols'), '-cf', str(script),
                            str(bundle / 'localcat-spike.exe')], directory / 'cwd', environment,
                           directory, 'cdb', evidence['commands'], timeout=60)
        except ProbeInputError:
            if args.profile not in EARLY and not args.profile.startswith('clear-failure-') or not evidence['commands'] or evidence['commands'][-1]['exit_code'] != 1:
                raise
        output = (directory / 'cdb.stdout').read_text(encoding='utf-8', errors='strict')
        stderr = (directory / 'cdb.stderr').read_text(encoding='utf-8', errors='strict')
        evidence['trace'] = validate_trace(output, stderr, args.profile, bundle, release['dist'], environment['SystemRoot'], devices)
        verify_release_binding(args.release.read_bytes(), args.dist, **anchors)
        if (args.release.read_bytes() != release_bytes or candidate_path.read_bytes() != candidate_bytes
                or inventory(args.dist) != source_inventory
                or inventory(bundle) != copied or inventory(directory / 'cwd') != pollution
                or {name: byte_fact((directory / name).read_bytes()) for name in layout_files} != layout_files
                or observers() != before or byte_fact(script.read_bytes()) != evidence['script']):
            raise ProbeInputError('input, copied layout, observer, script, or pollution changed')
        evidence['status'] = 'OBSERVED_BOUNDED_E9_PATHS'
    finally:
        (directory / 'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist', 'release', 'cdb'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--profile', choices=PROFILES, default='normal')
    _, result = run(parser.parse_args())
    raise SystemExit(0 if result['status'] == 'OBSERVED_BOUNDED_E9_PATHS' else 1)
