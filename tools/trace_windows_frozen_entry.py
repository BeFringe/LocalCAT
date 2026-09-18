"""Observe a built W3 entry with CDB; never infer call closure from no hits.

Software breakpoints perturb execution. This evidence complements source and
binary reachability analysis; it is not an enforcement mechanism or E0/E4 PASS.
Only the explicit newly launched debuggee is controlled, never an existing app.
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
from tools.windows_frozen_packaging import tree_inventory


# Bound below to the exact pinned python314.dll and decoded FF15 IAT targets.
# This inventory is a set of observation points, not an exhaustive CFG claim.
PYTHON_LOADER_SITES = {
    0x10EF3D: "LoadLibraryW", 0x18B86A: "LoadLibraryA",
    0x18B8A8: "LoadLibraryA", 0x18B8DD: "LoadLibraryA",
    0x1B9602: "LoadLibraryExW", 0x1B9921: "LoadLibraryExW",
    0x25940E: "LoadLibraryExW", 0x25949C: "LoadLibraryExW",
    0x28A557: "LoadLibraryW", 0x2B059F: "LoadLibraryA", 0x2B0660: "LoadLibraryA",
}
PINNED_PYTHON_SHA256 = "0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700"


def render_commands(python_entry, anchor_rva):
    # A module-qualified export keeps unresolved breakpoints associated with
    # the not-yet-loaded DLL. Bare module+RVA produces repeated CDB warnings.
    def address(rva):
        delta = rva - anchor_rva
        return f"python314!Py_Initialize{'+' if delta >= 0 else '-'}0x{abs(delta):x}"

    commands = [
        ".echo W3_TRACE_BEGIN",
        'sxe -c ".echo W3_PROCESS_EXIT;.lastevent;q" epr',
        'sxe -c ".echo W3_ACCESS_VIOLATION;.lastevent;kb;q" av',
        'bp @$exentry ".echo W3_PE_ENTRY;kb 6;gc"',
        'bu KERNELBASE!SetDefaultDllDirectories ".echo W3_DLL_POLICY;r rcx;kb 8;gc"',
        'bu KERNELBASE!LoadLibraryExW ".echo W3_LOAD_LIBRARY_EX;du @rcx;r r8;kb 8;gc"',
        'bu ntdll!LdrLoadDll ".echo W3_LDR_LOAD;du poi(@r8+8);kb 12;gc"',
        f'bu {address(python_entry)} ".echo W3_PYTHON_DLL_ENTRY;r rdx;kb 8;gc"',
        f'bu {address(0x18B5F4)} ".echo W3_RANDOM_REINIT_CHECK;db {address(0x5BA4CC)} L1;gc"',
        f'bu {address(0x2B0644)} ".echo W3_RANDOM_HELPER;dq {address(0x664EC8)} L1;kb 8;gc"',
    ]
    for rva, name in PYTHON_LOADER_SITES.items():
        display = "du" if name.endswith("W") else "da"
        commands.append(f'bu {address(rva)} ".echo W3_PY_LOADER_{rva:08x};'
                        f'{display} @rcx;r r8;kb 12;gc"')
    return "\n".join([*commands, "lm", "g"]) + "\n"


def summarize(output):
    required = ["W3_TRACE_BEGIN", "W3_PE_ENTRY", "W3_DLL_POLICY", "W3_PYTHON_DLL_ENTRY",
                "FROZEN_ENTRY.SPIKE_COMPLETED", "W3_PROCESS_EXIT"]
    lines = [line.strip() for line in output.splitlines()]
    observed = [line for line in lines if line in required or line.startswith("W3_PY_LOADER_")
                or line in {"W3_LDR_LOAD", "W3_LOAD_LIBRARY_EX", "W3_PYTHON_DLL_ENTRY",
                            "W3_RANDOM_REINIT_CHECK", "W3_RANDOM_HELPER", "W3_ACCESS_VIOLATION"}]
    errors = [line for line in lines if any(token in line.lower() for token in (
        "syntax error", "couldn't resolve error", "breakpoint expression", "unable to insert breakpoint",
        "unable to resolve unqualified symbol", "contains symbols not qualified"))]
    positions = [observed.index(name) if name in observed else -1 for name in required]
    exits = re.findall(r"^Last event: [^\n]+: Exit process [^,\n]+, code ([0-9a-fA-F]+)\r?$",
                       output, re.MULTILINE)
    exit_code = int(exits[0], 16) if len(exits) == 1 else None
    return {"classification": "OBSERVER_NOT_CALL_CLOSURE_PROOF", "markers": observed,
            "debugger_errors": errors,
            "observed_debuggee_exit_code": exit_code,
            "observed_sequence_complete": not errors and "W3_ACCESS_VIOLATION" not in observed
            and exit_code == 0 and -1 not in positions and positions == sorted(positions),
            "limitations": ["no-hit is not unreachable", "software breakpoints alter timing",
                            "debugger exit is not by itself debuggee success",
                            "initial breakpoint is after static module loading, not E0 resolution proof"]}


def trace_environment(original, profile):
    if profile not in {"normal", "mimalloc-stats"}:
        raise ProbeInputError("unknown observer child profile")
    environment = original.copy()
    for key in list(environment):
        if (key.upper().startswith(("PYTHON", "QT_", "_NT_", "MIMALLOC_"))
                or key.upper() == "__PYVENV_LAUNCHER__"):
            environment.pop(key, None)
    environment["PATH"] = str(Path(original["SystemRoot"]) / "System32")
    if profile == "mimalloc-stats":
        environment["MIMALLOC_SHOW_STATS"] = "1"
    return environment


def run(args):
    if os.name != "nt":
        raise ProbeInputError("CDB entry observer requires Windows")
    build = args.diagnostic_build.resolve()
    report = json.loads((build / "diagnostic.json").read_bytes())
    candidate = load_candidate(ROOT / "packaging/windows/frozen-entry/candidate-input.lock.json")
    if (report.get("status") != "EXECUTION_DIAGNOSTIC_COLLECTED"
            or report["candidate_input_digest"] != candidate["candidate_input_digest"]):
        raise ProbeInputError("observer requires an executed packaged build of the current candidate")
    bundle = build / "packaging/dist/localcat-spike"
    if tree_inventory(bundle) != report["packaging"]["dist"]:
        raise ProbeInputError("observed packaged bundle drift")
    executable = bundle / "localcat-spike.exe"
    checked_bytes(executable, report["packaged_pe"])
    python = bundle / "_internal/python314.dll"
    python_bytes = checked_bytes(python, candidate["runtime"]["cpython"]["dll"])
    if byte_fact(python_bytes)["sha256"] != PINNED_PYTHON_SHA256:
        raise ProbeInputError("callsite offsets do not belong to this Python DLL")
    import pefile
    with pefile.PE(data=python_bytes) as pe:
        iat = {item.address: item.name.decode("ascii") for descriptor in pe.DIRECTORY_ENTRY_IMPORT
               for item in descriptor.imports if item.name}
        for rva, expected in PYTHON_LOADER_SITES.items():
            instruction = pe.get_data(rva, 6)
            address = pe.OPTIONAL_HEADER.ImageBase + rva + 6 + struct.unpack("<i", instruction[2:])[0]
            if instruction[:2] != b"\xff\x15" or iat.get(address) != expected:
                raise ProbeInputError("loader callsite bytes or IAT binding changed")
        python_entry = pe.OPTIONAL_HEADER.AddressOfEntryPoint
        anchors = [symbol.address for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols
                   if symbol.name == b"Py_Initialize" and not symbol.forwarder]
        if len(anchors) != 1:
            raise ProbeInputError("missing unique local export for deferred breakpoint anchor")
    parent = ROOT / "artifacts/windows"
    directory = Path(tempfile.mkdtemp(prefix="entry-trace-", dir=parent))
    (directory / "symbols").mkdir()
    (directory / "cwd").mkdir()
    script = directory / "entry.cdb"
    script.write_text(render_commands(python_entry, anchors[0]), encoding="ascii", newline="\n")
    evidence = {"classification": "OBSERVER_NOT_CALL_CLOSURE_PROOF", "commands": [],
                "candidate_input_digest": candidate["candidate_input_digest"],
                "executable": byte_fact(executable.read_bytes()), "python": byte_fact(python_bytes),
                "cdb": byte_fact(args.cdb.read_bytes()), "script": byte_fact(script.read_bytes()),
                "observer": byte_fact(Path(__file__).read_bytes()), "status": "INCOMPLETE",
                "post_observation_inputs_unchanged": False}
    evidence["child_profile"] = args.profile
    print("ENTRY_TRACE=" + str(directory), flush=True)
    environment = trace_environment(os.environ, args.profile)
    try:
        completed = record_command(
            [str(args.cdb), "-y", str(directory / "symbols"), "-cf", str(script), str(executable)],
            directory / "cwd", environment, directory, "cdb", evidence["commands"], timeout=45)
        evidence["trace"] = summarize(completed.stdout.decode("utf-8", "replace"))
        if tree_inventory(bundle) != report["packaging"]["dist"]:
            raise ProbeInputError("debugger changed on-disk packaged inputs")
        evidence["post_observation_inputs_unchanged"] = True
        evidence["status"] = "OBSERVED" if evidence["trace"]["observed_sequence_complete"] else "INCOMPLETE_TRACE"
    finally:
        (directory / "evidence.json").write_bytes(canonical(evidence))
    return directory, evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic-build", type=Path, required=True)
    parser.add_argument("--cdb", type=Path, required=True)
    parser.add_argument("--profile", choices=("normal", "mimalloc-stats"), default="normal")
    _, evidence = run(parser.parse_args())
    raise SystemExit(0 if evidence["status"] == "OBSERVED" else 1)
