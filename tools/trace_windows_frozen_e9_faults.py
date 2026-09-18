"""Observe controlled allocation failures in a real, unchanged W3 candidate.

Only a newly launched debuggee is affected. A single malloc/realloc call is
skipped with a NULL result; Py_SetPath and initialization execute their real
error handling. This is not natural resource exhaustion or a complete E9 gate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import struct
import tempfile

from tools.probe_windows_frozen_custom_runw import (
    ROOT, ProbeInputError, byte_fact, canonical, checked_bytes, load_candidate, record_command,
)
from tools.trace_windows_frozen_entry import (
    PINNED_PYTHON_SHA256, render_commands, summarize, trace_environment,
)
from tools.windows_frozen_packaging import tree_inventory


# Addresses/bytes belong only to PINNED_PYTHON_SHA256. The stack guard selects
# the exact owner call, not arbitrary uses of the shared allocator helper.
FAULT_SITES = {
    "path-alloc-null": {
        "call_rva": 0xE87E0, "call_bytes": "ff155ac02300", "import": "malloc",
        "owner_return_rva": 0x309432, "owner_call_rva": 0x30942D,
        "owner_call_bytes": "e862f3ddff", "stack_return_offset": 0x28,
        "null_branch_rva": 0xE8814,
    },
    "inittab-alloc-null": {
        "call_rva": 0x2DED7C, "call_bytes": "ff15b65a0400", "import": "realloc",
        "owner_return_rva": 0x2E0D67, "owner_call_rva": 0x2E0D62,
        "owner_call_bytes": "e871dfffff", "stack_return_offset": 0x28,
        "null_branch_rva": 0x2DEDD8,
    },
}
PROFILES = ("control", *FAULT_SITES)
FATAL_EXIT_CODES = {3, 0x40000015, 0xC0000409}


def verify_fault_sites(python_bytes):
    if byte_fact(python_bytes)["sha256"] != PINNED_PYTHON_SHA256:
        raise ProbeInputError("fault offsets do not belong to this Python DLL")
    import pefile
    with pefile.PE(data=python_bytes) as pe:
        iat = {item.address: item.name.decode("ascii")
               for descriptor in pe.DIRECTORY_ENTRY_IMPORT
               for item in descriptor.imports if item.name}
        for site in FAULT_SITES.values():
            rva = site["call_rva"]
            instruction = pe.get_data(rva, 6)
            address = pe.OPTIONAL_HEADER.ImageBase + rva + 6 + struct.unpack("<i", instruction[2:])[0]
            if (instruction.hex() != site["call_bytes"] or iat.get(address) != site["import"]
                    or pe.get_data(site["owner_call_rva"], 5).hex() != site["owner_call_bytes"]):
                raise ProbeInputError("allocation callsite or owner call bytes changed")
        anchors = [symbol.address for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols
                   if symbol.name == b"Py_Initialize" and not symbol.forwarder]
        if len(anchors) != 1:
            raise ProbeInputError("missing unique local Python export anchor")
        return pe.OPTIONAL_HEADER.AddressOfEntryPoint, anchors[0]


def render_fault_commands(profile, python_entry, anchor_rva):
    if profile not in PROFILES:
        raise ProbeInputError("unknown E9 observer profile")

    def address(rva):
        delta = rva - anchor_rva
        return f"python314!Py_Initialize{'+' if delta >= 0 else '-'}0x{abs(delta):x}"

    enable_file_trace = "be 80 81 82 83;" if profile != "control" else ""
    extra = [
        "r @$t8=0", "r @$t9=0",
        f'bu python314!Py_SetPath ".echo W3_E9_SET_PATH;r @$t8=1;{enable_file_trace}kb 8;gc"',
        'bu python314!Py_InitializeFromInitConfig ".echo W3_E9_INIT_CONFIG;r @$t8=2;kb 8;gc"',
        'bu python314!PyImport_ExtendInittab ".echo W3_E9_EXTEND_INITTAB;kb 8;gc"',
        f'bu {address(0x30A020)} ".echo W3_E9_FATAL;da @rcx;da @rdx;kb 12;gc"',
        'bu KERNELBASE!WriteFile ".if (@$t8 != 0) { .echo W3_E9_WRITE;da /c 40 @rdx L40; };gc"',
    ]
    # Deliver fatal exceptions to the process. Do not use q at the exception:
    # debugger termination would not prove the real process termination path.
    for code in ("40000015", "c0000409"):
        command = '.echo W3_E9_TERMINATING_EXCEPTION;.lastevent;kb 12;gn'
        extra.append(f'sxe -c "{command}" -c2 "{command}" {code}')
    file_sites = (
        ("NtCreateFile", "poi(@r8+8)", "poi(@r8+0x10)"),
        ("NtOpenFile", "poi(@r8+8)", "poi(@r8+0x10)"),
        ("NtQueryAttributesFile", "poi(@rcx+8)", "poi(@rcx+0x10)"),
        ("NtQueryFullAttributesFile", "poi(@rcx+8)", "poi(@rcx+0x10)"),
    ) if profile != "control" else ()
    for index, (name, root, path) in enumerate(file_sites, 80):
        extra.append(f'bp{index} ntdll!{name} ".if (@$t8 != 0) {{ '
                     f'.printf \\"W3_E9_PATH {name} phase=%d root=%p name=%msu\\", '
                     f'@$t8, {root}, {path}; .echo; }};gc"')
    if file_sites:
        extra.append("bd 80 81 82 83")
    if profile != "control":
        site = FAULT_SITES[profile]
        phase = 1 if profile == "path-alloc-null" else 2
        guards = ["@$t9 == 0", f"@$t8 == {phase}"]
        guards += ["@rcx == 2"] if phase == 1 else ["@rcx == 0", "@rdx > 0"]
        guards += [f'poi(@rsp+0x{site["stack_return_offset"]:x}) == {address(site["owner_return_rva"])}']
        extra.append(
            f'bu {address(site["call_rva"])} "' + "".join(f".if ({guard}) {{ " for guard in guards)
            + '.echo W3_E9_ALLOC_NULL;kb 12;r rcx;r rdx;r @$t9=1;r rax=0;r rip=@rip+6; '
            + "}; " * len(guards) + 'gc"')
        extra.append(f'bu {address(site["null_branch_rva"])} ".if (@$t9 == 1) {{ '
                     '.echo W3_E9_NULL_RETURN_BRANCH;kb 10; };gc"')
    commands = render_commands(python_entry, anchor_rva)
    return commands.replace("\nlm\ng\n", "\n" + "\n".join(extra) + "\nlm\ng\n")


def summarize_fault(output, stderr, profile):
    if profile not in PROFILES:
        raise ProbeInputError("unknown E9 observer profile")
    trace = summarize(output)
    combined = output + "\n" + stderr
    errors = summarize(combined)["debugger_errors"]
    errors += [line for line in combined.splitlines() if any(text in line.lower() for text in (
        "offset expression evaluation failed", "waitforevent failed", "bad syntax",
        "numeric expression missing", "range error", "illegal column count"))]
    lines = [line.strip() for line in output.splitlines()]
    # This failure diagnostic goes to stderr, not OutputDebugString. The
    # WriteFile breakpoint supplies its position on the debugger timeline.
    lines = ["FROZEN_ENTRY.INITIALIZATION_FAILED" if re.fullmatch(
        r'[0-9a-fA-F`]+\s+"FROZEN_ENTRY\.INITIALIZATION_FAILED"', line) else line for line in lines]
    ordered = ["W3_TRACE_BEGIN", "W3_PE_ENTRY", "W3_DLL_POLICY", "W3_PYTHON_DLL_ENTRY",
               "FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN", "W3_E9_SET_PATH"]
    forbidden = ["W3_ACCESS_VIOLATION"]
    if profile == "control":
        ordered += ["W3_E9_INIT_CONFIG", "FROZEN_ENTRY.SPIKE_COMPLETED"]
        forbidden += ["W3_E9_ALLOC_NULL", "W3_E9_FATAL"]
        code_matches = trace["observed_debuggee_exit_code"] == 0
        detail_matches = True
    elif profile == "path-alloc-null":
        ordered += ["W3_E9_ALLOC_NULL", "W3_E9_NULL_RETURN_BRANCH", "W3_E9_FATAL"]
        forbidden += ["W3_E9_INIT_CONFIG", "FROZEN_ENTRY.SPIKE_COMPLETED"]
        code_matches = trace["observed_debuggee_exit_code"] in FATAL_EXIT_CODES
        detail_matches = "Fatal Python error: Py_SetPath: out of memory" in combined
    else:
        ordered += ["W3_E9_INIT_CONFIG", "W3_E9_EXTEND_INITTAB", "W3_E9_ALLOC_NULL",
                    "W3_E9_NULL_RETURN_BRANCH", "FROZEN_ENTRY.INITIALIZATION_FAILED"]
        forbidden += ["W3_E9_FATAL", "FROZEN_ENTRY.SPIKE_COMPLETED"]
        code_matches = trace["observed_debuggee_exit_code"] == 1
        detail_matches = "FROZEN_ENTRY.INITIALIZATION_FAILED" in stderr.splitlines()
    ordered += ["W3_PROCESS_EXIT"]
    # The shared observer also records DLL detach. Only the first DLL entry
    # anchors this sequence; fault and initialization markers must remain unique.
    positions = [lines.index(marker) if (lines.count(marker) == 1 or
                 marker == "W3_PYTHON_DLL_ENTRY" and marker in lines) else -1 for marker in ordered]
    complete = (not errors and code_matches and detail_matches and -1 not in positions
                and positions == sorted(positions) and not any(marker in lines for marker in forbidden))
    return {"classification": "CONTROLLED_ALLOCATION_FAILURE_NOT_W3_GATE",
            "profile": profile, "observed_debuggee_exit_code": trace["observed_debuggee_exit_code"],
            "debugger_errors": errors, "expected_path_observed": complete,
            "markers": [line for line in lines if line in ordered or line in forbidden],
            "limitations": ["single controlled allocation failure, not natural OOM",
                            "not all interpreter initialization failure stages",
                            "no-hit is not unreachable or proof of no file access",
                            "software breakpoints alter execution timing",
                            "no W3 mandatory assertion is granted by this observer"]}


def run(args):
    if os.name != "nt" or args.profile not in PROFILES:
        raise ProbeInputError("E9 observer requires Windows and a known profile")
    build = args.diagnostic_build.resolve()
    report = json.loads((build / "diagnostic.json").read_bytes())
    candidate = load_candidate(ROOT / "packaging/windows/frozen-entry/candidate-input.lock.json")
    if (report.get("status") != "EXECUTION_DIAGNOSTIC_COLLECTED"
            or report["candidate_input_digest"] != candidate["candidate_input_digest"]):
        raise ProbeInputError("E9 observer requires the current executed packaged candidate")
    bundle = build / "packaging/dist/localcat-spike"
    if tree_inventory(bundle) != report["packaging"]["dist"]:
        raise ProbeInputError("observed packaged bundle drift")
    executable = bundle / "localcat-spike.exe"
    checked_bytes(executable, report["packaged_pe"])
    python_bytes = checked_bytes(bundle / "_internal/python314.dll", candidate["runtime"]["cpython"]["dll"])
    python_entry, anchor = verify_fault_sites(python_bytes)
    directory = Path(tempfile.mkdtemp(prefix=f"e9-fault-{args.profile}-", dir=ROOT / "artifacts/windows"))
    (directory / "symbols").mkdir()
    (directory / "cwd").mkdir()
    script = directory / "entry.cdb"
    script.write_text(render_fault_commands(args.profile, python_entry, anchor), encoding="ascii", newline="\n")
    evidence = {"classification": "CONTROLLED_ALLOCATION_FAILURE_NOT_W3_GATE", "status": "INCOMPLETE",
                "candidate_input_digest": candidate["candidate_input_digest"], "commands": [],
                "profile": args.profile, "fault_site": FAULT_SITES.get(args.profile),
                "file_call_observation": args.profile != "control",
                "executable": byte_fact(executable.read_bytes()), "python": byte_fact(python_bytes),
                "script": byte_fact(script.read_bytes()), "observer": byte_fact(Path(__file__).read_bytes()),
                "cdb": byte_fact(args.cdb.read_bytes()), "post_observation_inputs_unchanged": False}
    print("E9_FAULT_TRACE=" + str(directory), flush=True)
    try:
        try:
            record_command([str(args.cdb), "-y", str(directory / "symbols"), "-cf", str(script), str(executable)],
                           directory / "cwd", trace_environment(os.environ, "normal"), directory, "cdb",
                           evidence["commands"], timeout=45)
        except ProbeInputError:
            # Expected failing debuggees may give CDB a nonzero exit. This is
            # never sufficient evidence: require its real exit event below.
            if not evidence["commands"] or evidence["commands"][-1].get("exit_code") not in {1, *FATAL_EXIT_CODES}:
                raise
        output = (directory / "cdb.stdout").read_text(encoding="utf-8", errors="replace")
        stderr = (directory / "cdb.stderr").read_text(encoding="utf-8", errors="replace")
        evidence["trace"] = summarize_fault(output, stderr, args.profile)
        if tree_inventory(bundle) != report["packaging"]["dist"]:
            raise ProbeInputError("debugger changed on-disk packaged inputs")
        evidence["post_observation_inputs_unchanged"] = True
        if evidence["trace"]["expected_path_observed"]:
            evidence["status"] = "OBSERVED_CONTROLLED_PATH"
    finally:
        (directory / "evidence.json").write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic-build", type=Path, required=True)
    parser.add_argument("--cdb", type=Path, required=True)
    parser.add_argument("--profile", choices=PROFILES, default="control")
    _, evidence = run(parser.parse_args())
    raise SystemExit(0 if evidence["status"] == "OBSERVED_CONTROLLED_PATH" else 1)
