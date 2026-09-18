"""Capture external system-entry resolution facts for the fixed W3 candidate.

The child API-set namespace, KnownDLL registry and initial module events are OS
observations, not application input authority or a system-servicing lock. This
tool does not inspect CNG internals or add runtime environment prerequisites.
An initial debugger stop is after static mapping: combine these observations
with the exact PE/manifest, documented search rules and independent redirection
counterexamples; do not call this output a pre-load enforcement mechanism.
"""
from __future__ import annotations

import argparse
import ctypes
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
from tools.trace_windows_frozen_loader_resolution import (
    norm, integer, verify_sites, render_commands, parse_loader_events,
    validate_profile_calls, validate_stderr,
)
from tools.windows_frozen_release import verify_release_binding


class ResolutionFactsError(ValueError):
    pass


MAX_NAMESPACE = 16 * 1024 * 1024
CLASSIFICATION = 'EXTERNAL_SYSTEM_RESOLUTION_FACTS_NOT_PRELOAD_ENFORCEMENT'


def parse_api_set_namespace(blob, requested):
    """Decode only the observed x64 v6 namespace, not an OS support policy."""
    def region(offset, size):
        if offset < 0 or size < 0 or offset > len(blob) or size > len(blob)-offset:
            raise ResolutionFactsError('API-set region exceeds captured bytes')
        return blob[offset:offset+size]
    def string(offset, size):
        if size % 2:
            raise ResolutionFactsError('odd API-set UTF-16 size')
        try:
            text = region(offset, size).decode('utf-16le')
        except UnicodeDecodeError as exc:
            raise ResolutionFactsError('invalid API-set UTF-16') from exc
        if '\0' in text:
            raise ResolutionFactsError('embedded NUL in API-set name')
        return text.lower()
    if not 28 <= len(blob) <= MAX_NAMESPACE:
        raise ResolutionFactsError('invalid API-set namespace size')
    version, size, flags, count, entry_offset, hash_offset, factor = struct.unpack('<7I', region(0, 28))
    if version != 6 or size != len(blob) or not 0 < count <= 65536 or entry_offset < 28:
        raise ResolutionFactsError('unrecognized API-set namespace header')
    region(entry_offset, count*24)
    entries = {}
    for index in range(count):
        _, no, nl, hl, vo, vc = struct.unpack('<6I', region(entry_offset+index*24, 24))
        name = string(no, nl)
        if not name or name in entries or hl > nl or hl % 2:
            raise ResolutionFactsError('ambiguous API-set namespace entry')
        entries[name] = (vo, vc, name[:hl//2])
    result = {}
    for request, importer in requested.items():
        key = request.lower().removesuffix('.dll')
        schema_key = key
        relation = 'exact-captured-name'
        if key not in entries:
            # This is a correlation of captured bytes with actual calls below,
            # NOT an implementation of the Windows API-set resolver or a claim
            # that arbitrary contract revisions are interchangeable.
            stem, separator, revision = key.rpartition('-')
            matches = [name for name, (_, _, hashed) in entries.items()
                       if separator and revision.isdecimal() and hashed == stem]
            if len(matches) != 1:
                raise ResolutionFactsError('no unique captured API-set name correlation')
            schema_key = matches[0]
            relation = 'captured-hash-prefix-correlation-not-OS-resolver'
        vo, vc, hashed_name = entries[schema_key]
        if not 0 < vc <= 256:
            raise ResolutionFactsError('invalid API-set value count')
        region(vo, vc*20)
        values = {}
        for index in range(vc):
            _, no, nl, ho, hl = struct.unpack('<5I', region(vo+index*20, 20))
            alias, host = string(no, nl), string(ho, hl)
            if alias in values or re.fullmatch(r'[a-z0-9_.-]+\.dll', host) is None:
                raise ResolutionFactsError('ambiguous or non-basename API-set host')
            values[alias] = host
        alias = ntpath.basename(importer).lower()
        selected = alias if alias in values else ''
        if selected not in values:
            raise ResolutionFactsError('no applicable API-set host')
        result[request] = {'importer':importer, 'selected_alias':selected,
                           'schema_contract':schema_key, 'schema_hashed_name':hashed_name,
                           'name_relation':relation,
                           'host':values[selected], 'values':values}
    return result


def initial_system_modules(output, executable, system32):
    entries = list(re.finditer(r'^W3_LD_PE_ENTRY\r?$', output, re.M))
    if len(entries) != 1:
        raise ResolutionFactsError('missing or repeated executable entry')
    snapshot = re.findall(r'^W3_SYS_INITIAL_MODULES_BEGIN\r?$(.*?)^W3_SYS_INITIAL_MODULES_END\r?$',
                          output[:entries[0].start()], re.M | re.S)
    if len(snapshot) != 1:
        raise ResolutionFactsError('missing or repeated initial module file snapshot')
    files = {}
    for start, path in re.findall(
            r'^W3_SYS_LDR base=([0-9a-fA-F`]+) path=([^\r\n]+)\r?$', snapshot[0], re.M):
        key = integer(start)
        if key in files or not key or not ntpath.isabs(path):
            raise ResolutionFactsError('ambiguous initial module file snapshot')
        files[key] = path.strip()
    if not files:
        raise ResolutionFactsError('empty initial module file snapshot')
    rows = re.findall(r'^ModLoad: ([0-9a-fA-F`]+)\s+([0-9a-fA-F`]+)\s+([^\r\n]+)\r?$',
                      output[:entries[0].start()], re.M)
    exe_seen = 0; result = []; names = set(); observed = set()
    for start, end, path in rows:
        key = (integer(start), integer(end))
        if key[1] <= key[0] or key in observed:
            raise ResolutionFactsError('invalid initial module range')
        observed.add(key)
        if key[0] in files:
            if ntpath.isabs(path) and norm(path) != norm(files[key[0]]):
                raise ResolutionFactsError('module event disagrees with file snapshot')
            path = files[key[0]]
        if norm(path) == norm(executable):
            exe_seen += 1
            continue
        name = ntpath.basename(path).lower()
        if norm(ntpath.dirname(path)) != norm(system32) or name in names:
            raise ResolutionFactsError('initial module outside System32 or duplicate basename')
        names.add(name)
        result.append({'base':integer(start), 'end':integer(end), 'path':path})
    if (not files.keys() <= {base for base, end in observed} or exe_seen != 1
            or not {'ntdll.dll', 'kernel32.dll', 'user32.dll'} <= names):
        raise ResolutionFactsError('incomplete process-creation module events')
    return result


def system_facts():
    import winreg
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_dir = kernel.GetSystemDirectoryW
    get_dir.argtypes = (ctypes.c_wchar_p, ctypes.c_uint); get_dir.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_dir(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ResolutionFactsError('cannot observe native system directory')
    known = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
            r'SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs',
            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        for index in range(winreg.QueryInfoKey(key)[1]):
            name, value, kind = winreg.EnumValue(key, index)
            if isinstance(value, str) and value.lower().endswith('.dll'):
                if kind != winreg.REG_SZ or ntpath.basename(value) != value:
                    raise ResolutionFactsError('unrecognized KnownDLL registry value')
                known[name] = value.lower()
    # The registry is a configured seed list, not the live KnownDll object
    # directory. Kernel dependencies and ntdll need not be separate values.
    if not {'kernel32.dll', 'user32.dll'} <= set(known.values()):
        raise ResolutionFactsError('required known system names absent from observed registry')
    controls = {}
    for path, name in ((r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options', 'DevOverrideEnable'),
                       (r'SOFTWARE\Microsoft\Windows\CurrentVersion\SideBySide', 'PreferExternalManifest')):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path,
                    access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value, kind = winreg.QueryValueEx(key, name)
                controls[name] = {'value':value, 'registry_type':kind}
        except FileNotFoundError:
            controls[name] = {'absent':True}
    return {'system32':buffer.value, 'known_dlls':known, 'redirection_controls':controls,
            'scope':'observed trusted OS state; not candidate/runtime prerequisites'}


def clean_capture(output, stderr, executable):
    warning = '*** WARNING: Unable to verify timestamp for '+str(executable).replace('/', '\\')
    clean = output.replace(warning+'\n', '') if output.count(warning) == 1 else output
    if any(word in (clean+'\n'+stderr).lower() for word in (
            'warning', 'syntax error', "couldn't resolve", 'unable to', 'failed', 'access violation',
            "couldn't insert", 'cannot continue', 'could not', 'memory access error',
            'numeric expression missing', 'range error', 'waitforevent', 'illegal column count')) or re.search(r'^W3_SYS_MAP_REJECT\r?$', clean, re.M):
        raise ResolutionFactsError('uncertain debugger capture')
    positions = [list(re.finditer('^'+name+r'\r?$', clean, re.M))
                 for name in ('W3_SYS_INITIAL_MODULES_BEGIN', 'W3_SYS_INITIAL_MODULES_END',
                              'W3_SYS_MAP_BEGIN', 'W3_SYS_MAP_END', 'W3_LD_PE_ENTRY')]
    if (any(len(p) != 1 for p in positions)
            or [p[0].start() for p in positions] != sorted(p[0].start() for p in positions)):
        raise ResolutionFactsError('initial metadata capture is not before executable entry')
    return clean


def run(args):
    if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ResolutionFactsError('x64 Windows observation required')
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
        raise ResolutionFactsError('wrong candidate')
    import pefile
    lock = candidate['evidence_producer']['pefile']
    def parser_metadata():
        value = importlib.metadata.distribution('pefile').read_text('METADATA')
        if value is None:
            raise ResolutionFactsError('parser metadata unavailable')
        return value.encode()
    checked_bytes(Path(pefile.__file__), lock['source'])
    if (pefile.__version__ != lock['version']
            or byte_fact(parser_metadata()) != {k:lock['distribution_metadata'][k] for k in ('bytes','sha256')}):
        raise ResolutionFactsError('unlocked PE parser')
    exe = (args.dist/'localcat-spike.exe').resolve()
    anchors = verify_sites(checked_bytes(exe, release['executable']),
        checked_bytes(args.dist/'_internal/python314.dll', candidate['runtime']['cpython']['dll']))
    directory = Path(tempfile.mkdtemp(prefix='system-resolution-', dir=ROOT/'artifacts/windows'))
    (directory/'symbols').mkdir(); (directory/'cwd').mkdir()
    script = directory/'system.cdb'; capture = directory/'child-apiset.bin'
    commands = render_commands(anchors, 'acp-1252-normal')
    # SDK winternl.h x64 InMemoryOrderModuleList and FullDllName offsets;
    # same read-only loader metadata observation as the paired-call tracer.
    dump = ('.echo W3_SYS_INITIAL_MODULES_BEGIN\n'
        'r @$t10=poi(@$peb+0x18)+0x20\nr @$t11=poi(@$t10)\nr @$t12=0\n'
        '.while (@$t11!=@$t10) {.if (@$t12>=0n256) {.echo W3_SYS_MAP_REJECT;.break;};'
        '.printf "W3_SYS_LDR base=%p path=%msu", poi(@$t11+0x20), @$t11+0x38;.echo;'
        'r @$t11=poi(@$t11);r @$t12=@$t12+1;}\n.echo W3_SYS_INITIAL_MODULES_END\n'
        'r @$t9=poi(@$peb+0x68)\n.echo W3_SYS_MAP_BEGIN\n'
        '.if ((@$t9!=0) & (dwo(@$t9)==6) & (dwo(@$t9+4)>=0n28) & (dwo(@$t9+4)<=0x1000000)) { '
        f'.writemem "{capture.as_posix()}" @$t9 L?dwo(@$t9+4); '
        '} .else {.echo W3_SYS_MAP_REJECT;}\n.echo W3_SYS_MAP_END\n')
    script.write_text(commands.replace('g\n', dump+'g\n'), encoding='ascii', newline='\n')
    def inventory():
        return {**observer_inventory(args.cdb), Path(__file__).name:byte_fact(Path(__file__).read_bytes()),
                'loader_resolution':byte_fact((ROOT/'tools/trace_windows_frozen_loader_resolution.py').read_bytes()),
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()), 'pefile-METADATA':byte_fact(parser_metadata())}
    observers = inventory(); facts = system_facts()
    evidence = {'classification':CLASSIFICATION, 'status':'INCOMPLETE', 'commands':[],
        'release_sha256':args.expected_release_sha256, 'repository_commit':args.expected_commit,
        'candidate_input_digest':args.expected_candidate_digest, 'observers':observers,
        'script':byte_fact(script.read_bytes()), 'os_facts':facts, 'post_observation_inputs_unchanged':False}
    print('SYSTEM_RESOLUTION='+str(directory), flush=True)
    try:
        record_command([str(args.cdb), '-y', str(directory/'symbols'), '-cf', str(script), str(exe)],
            directory/'cwd', trace_environment(os.environ, 'normal'), directory, 'cdb', evidence['commands'], timeout=45)
        output = (directory/'cdb.stdout').read_text(encoding='utf-8')
        stderr = (directory/'cdb.stderr').read_text(encoding='utf-8')
        output = clean_capture(output, stderr, exe)
        requested = {target:'localcat-spike.exe'
            for call in candidate['entry_contract']['dynamic_loader']['application_system_calls']
            if call['owner'] == 'custom-entry-static-crt'
            for target in call['targets'] if target.startswith(('api-ms-', 'ext-ms-'))}
        raw = capture.read_bytes()
        evidence['child_api_set_namespace'] = byte_fact(raw)
        evidence['api_set_resolution'] = parse_api_set_namespace(raw, requested)
        evidence['initial_modules'] = initial_system_modules(output, str(exe), facts['system32'])
        trace = parse_loader_events(output, facts['system32'], str(args.dist), 'acp-1252-normal')
        validate_profile_calls(trace, 'acp-1252-normal'); validate_stderr(stderr, 'acp-1252-normal')
        for call in trace['calls']:
            if call['name'] in requested and ntpath.basename(call['host']).lower() != evidence['api_set_resolution'][call['name']]['host']:
                raise ResolutionFactsError('observed CRT host disagrees with child API-set namespace')
        evidence['paired_loader_trace'] = trace
        verify()
        if (args.release.read_bytes() != release_bytes or candidate_path.read_bytes() != candidate_bytes
                or inventory() != observers or system_facts() != facts
                or byte_fact(script.read_bytes()) != evidence['script']):
            raise ResolutionFactsError('observation inputs changed')
        evidence['post_observation_inputs_unchanged'] = True
        evidence['status'] = 'CAPTURED_BOUND_OS_RESOLUTION_FACTS'
    finally:
        (directory/'evidence.json').write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dist', 'release', 'cdb'):
        parser.add_argument('--'+name, required=True, type=Path)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--'+name, required=True)
    run(parser.parse_args())
