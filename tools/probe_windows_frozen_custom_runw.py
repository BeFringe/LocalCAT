"""Build and run the real custom runw for diagnosis, NOT the final W3 gate.

This replays the five ordered upstream patches and checks their native source
projection. It is not the final release producer, a PyInstaller packaged
distribution, E0 approval, or a mandatory matrix result.
All outputs remain under artifacts/windows for independent inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from tools.audit_w3_stock_bootloader import audit_pe, audit_sources
from tools.prepare_windows_frozen_entry_inputs import (
    CandidateInputError, _directory_input_fact, _runtime_native_closure, _validate_dynamic_loader,
)
from tools.windows_frozen_manifest import SourceEntry, prepare_manifest
from tools.windows_frozen_patch_series import Patch, replay_patch_series, source_inventory_digest
from tools.windows_frozen_packaging import (
    APPLICATION_MANIFEST, candidate_packaging_fact, prepare_packaging, verify_packaging_result,
)


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "packaging/windows/frozen-entry/native"
SCHEMA = "localcat.windows-custom-runw-diagnostic.v1"
MODULES = ("localcat_frozen_entry", "localcat_frozen_bootstrap", "localcat_manifest",
           "localcat_native_closure", "localcat_rooted_io", "localcat_sha256")


class ProbeInputError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def byte_fact(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def duplicate_check(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ProbeInputError("duplicate JSON key: " + name)
        result[name] = value
    return result


def load_candidate(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=duplicate_check)
    if (value.get("schema") != "localcat.windows-frozen-entry-candidate-input.v3"
            or value.get("status") != "MATERIALIZED"):
        raise ProbeInputError("expected current materialized v3 candidate")
    payload = {key: item for key, item in value.items()
               if key not in {"candidate_input_digest", "status", "assertions"}}
    if hashlib.sha256(canonical(payload)).hexdigest() != value.get("candidate_input_digest"):
        raise ProbeInputError("candidate self digest mismatch")
    try:
        _validate_dynamic_loader(value.get("entry_contract", {}).get("dynamic_loader"))
    except CandidateInputError as exc:
        raise ProbeInputError(str(exc)) from exc
    return value


def packaging_preflight(lock, dependency_source):
    expected = lock.get("evidence_producer", {}).get("custom_packaging")
    if not expected or candidate_packaging_fact(ROOT, dependency_source) != expected:
        raise ProbeInputError("packaging input missing or differs from candidate lock")
    return expected


def import_delta(actual, expected):
    result = {}
    for table in ("imports", "delay_imports"):
        def pairs(source):
            return {(row["dll"].upper(), symbol) for row in source[table] for symbol in row["symbols"]}
        seen, target = pairs(actual), pairs(expected)
        result[table] = {"added": [list(item) for item in sorted(seen - target)],
                         "removed": [list(item) for item in sorted(target - seen)]}
    result["exact_match"] = not any(result[table][kind] for table in ("imports", "delay_imports")
                                    for kind in ("added", "removed"))
    return result


def replay_native_sources(pristine, patches, contract, native_templates):
    # The caller has authenticated the complete pristine build-driver inventory
    # against the candidate lock before supplying these bytes.
    replayed = replay_patch_series(pristine, patches, patch_contract=contract,
                                   expected_base_digest=source_inventory_digest(pristine))
    if set(native_templates) != set(contract["owned_new_files"]):
        raise ProbeInputError("native template inventory differs from the approved owner set")
    for name, content in native_templates.items():
        if replayed.files[name] != content:
            raise ProbeInputError("replayed native template mismatch: " + name)
    return replayed


def prepare_source_manifest(sources, entries, candidate_digest, *, applied_sources_digest):
    digest = source_inventory_digest(sources)
    facts = {"schema": "localcat.windows-diagnostic-template-inventory.v1", "sha256": digest,
             "files": [{"path": name, **byte_fact(data)} for name, data in sorted(sources.items())]}
    prepared = prepare_manifest(entries, candidate_input_digest=candidate_digest,
                                applied_sources_digest=applied_sources_digest)
    header = prepared.render_header(sources["bootloader/src/localcat_manifest.h"].decode("utf-8"))
    return prepared, header, facts


def checked_bytes(path, expected):
    data = Path(path).read_bytes()
    if byte_fact(data) != {key: expected[key] for key in ("bytes", "sha256")}:
        raise ProbeInputError("locked content mismatch: " + str(path))
    return data


def directory_matches(path, expected):
    actual = _directory_input_fact(path, expected["role"])
    if actual != expected:
        raise ProbeInputError("locked directory mismatch: " + str(path))
    return {"path": str(path), **actual}


def toolchain_inputs(lock):
    tools = {}
    for name, expected in lock["toolchain"]["msvc"]["tools"].items():
        found = shutil.which(expected["name"])
        if not found:
            raise ProbeInputError("activate locked vcvars environment; missing " + expected["name"])
        path = Path(found).resolve()
        tools[name] = {"path": str(path), **byte_fact(checked_bytes(path, expected))}
    cl = Path(tools["cl"]["path"])
    rc = Path(tools["rc"]["path"])
    msvc, sdk = cl.parents[3], rc.parents[3]
    version = lock["toolchain"]["windows_sdk"]["version"]
    specifications = [
        (cl.parent, lock["toolchain"]["msvc"]["compiler_bin"]),
        (msvc / "include", lock["toolchain"]["msvc"]["headers"]),
        (msvc / "lib/x64", lock["toolchain"]["msvc"]["libraries"]),
        (rc.parent, lock["toolchain"]["windows_sdk"]["binary_inputs"]),
        (sdk / "Include" / version, lock["toolchain"]["windows_sdk"]["headers"]),
        (sdk / "Lib" / version / "um/x64", lock["toolchain"]["windows_sdk"]["libraries"]),
        (sdk / "Lib" / version / "ucrt/x64", lock["toolchain"]["windows_sdk"]["ucrt_libraries"]),
    ]
    return {"tools": tools, "directories": [directory_matches(path, expected) for path, expected in specifications]}


def runtime_entries(runtime, lock):
    native = _runtime_native_closure(runtime)
    if native != lock["runtime"]["cpython"]["native_closure"]:
        raise ProbeInputError("runtime recursive native inventory mismatch")
    if {member["name"].lower() for member in native["local_members"]} != {"python314.dll", "vcruntime140.dll"}:
        raise ProbeInputError("diagnostic currently requires the approved two-member runtime")
    entries = []
    for member in native["local_members"]:
        python = member["name"].lower() == "python314.dll"
        entries.append(SourceEntry("python-runtime" if python else "vcruntime-runtime",
            "_internal/" + member["name"], "native", checked_bytes(runtime / member["name"], member),
            ("vcruntime-runtime",) if python else ()))
    for member in lock["runtime"]["cpython"]["interpreter_sources"]:
        relative = member["path"].removeprefix("Lib/")
        entries.append(SourceEntry(member["id"], "_internal/" + relative, "interpreter",
                                   checked_bytes(runtime / member["path"], member)))
    spike = ROOT / "packaging/windows/frozen-entry/spike"
    entries.extend([
        SourceEntry("bootstrap", "_internal/localcat_frozen_bootstrap.py", "bootstrap",
                    (spike / "localcat_frozen_bootstrap.py").read_bytes(),
                    ("critical-source", "fixture", "python-runtime")),
        SourceEntry("critical-source", "_internal/localcat_spike_critical.py", "critical-source",
                    (spike / "localcat_spike_critical.py").read_bytes(), ("fixture",)),
        SourceEntry("fixture", "_internal/localcat_spike_fixture.txt", "fixture",
                    (spike / "localcat_spike_fixture.txt").read_bytes()),
    ])
    return entries


def record_command(command, cwd, environment, directory, name, records, *, timeout):
    record = {"name": name, "command": command, "cwd": str(cwd), "exit_code": None}
    records.append(record)
    start = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        (directory / (name + ".stdout")).write_bytes(exc.stdout or b"")
        (directory / (name + ".stderr")).write_bytes(exc.stderr or b"")
        record.update(timed_out=True, elapsed_seconds=time.monotonic() - start)
        raise ProbeInputError(name + " timed out") from exc
    record.update(exit_code=completed.returncode, elapsed_seconds=time.monotonic() - start,
                  stdout=byte_fact(completed.stdout), stderr=byte_fact(completed.stderr))
    (directory / (name + ".stdout")).write_bytes(completed.stdout)
    (directory / (name + ".stderr")).write_bytes(completed.stderr)
    if completed.returncode:
        raise ProbeInputError(name + " failed with exit " + str(completed.returncode))
    return completed


def record_windowed_no_standard_handles(command, cwd, environment, records, *, timeout):
    """Run this probe's own GUI child with explicit NULL handles, no observer.

    Keeping all three Popen streams as None leaves this STARTUPINFO untouched;
    close_fds=True makes CreateProcess's bInheritHandles false. In particular,
    DEVNULL/capture_output would create real handles and invalidate this mode.
    """
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    startup.hStdInput = startup.hStdOutput = startup.hStdError = 0
    record = {"name": "runw-no-standard-handles", "command": command, "cwd": str(cwd),
              "exit_code": None, "standard_handles": {"stdin": "NULL", "stdout": "NULL", "stderr": "NULL"},
              "startup_flags": ["STARTF_USESTDHANDLES", "STARTF_USESHOWWINDOW"],
              "show_window": "SW_HIDE", "creation_flags": ["CREATE_NO_WINDOW"],
              "inherit_handles": False, "debugger": "NOT_ATTACHED_BY_PROBE",
              "native_markers": "NOT_OBSERVED_NO_OUTPUT_CHANNEL"}
    records.append(record)
    started = time.monotonic()
    try:
        child = subprocess.Popen(command, cwd=cwd, env=environment, startupinfo=startup,
                                 creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
                                 stdin=None, stdout=None, stderr=None, shell=False)
        record["pid"] = child.pid
        try:
            record["exit_code"] = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            record["timed_out"] = True
            # A native failure dialog can wait for input. Terminate only the
            # exact process we launched, never search/kill unrelated runw apps.
            child.kill()
            try:
                record["exit_code"] = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record["termination"] = "TEST_CHILD_TERMINATION_UNCONFIRMED"
            else:
                record["termination"] = "TEST_CHILD_KILLED_ON_TIMEOUT"
            raise ProbeInputError("runw-no-standard-handles timed out") from exc
        if record["exit_code"]:
            raise ProbeInputError("runw-no-standard-handles failed with exit " + str(record["exit_code"]))
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
    return record


def run_probe(args):
    if os.name != "nt":
        raise ProbeInputError("real custom runw diagnostic requires Windows")
    output_parent = Path(args.output_parent).resolve()
    if not output_parent.is_relative_to((ROOT / "artifacts/windows").resolve()):
        raise ProbeInputError("persistent diagnostic output must stay under artifacts/windows")
    output_parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="custom-runw-", dir=output_parent))
    producer_bytes = Path(__file__).read_bytes()
    print("CUSTOM_RUNW_DIAGNOSTIC=" + str(directory), flush=True)
    report: dict[str, Any] = {"schema": SCHEMA, "classification": "DIAGNOSTIC_ONLY_NOT_W3_GATE",
              "limitations": ["five-patch source replay is not the final release producer",
                              "captured-stream and explicit NULL-standard-handle GUI runs are controlled diagnostics, not a desktop launch journey",
                              "NULL-handle mode records process exit only; it does not observe native stage markers or attach a debugger",
                              "no PyInstaller appended CArchive or final distribution packaging",
                              "no full E0-E11 mandatory matrix or reproducibility claim"],
              "status": "INCOMPLETE", "commands": []}
    try:
        lock = load_candidate(args.candidate_input)
        if args.packaging_dependencies:
            packaging_target = packaging_preflight(lock, args.packaging_dependencies)
        report["candidate_input_digest"] = lock["candidate_input_digest"]
        inputs = directory / "inputs"
        inputs.mkdir()
        (inputs / "candidate-input.lock.json").write_bytes(canonical(lock))
        native_dir = Path(args.native_directory).resolve()
        native_files = {"bootloader/src/" + name + suffix: (native_dir / (name + suffix)).read_bytes()
                        for name in MODULES for suffix in (".c", ".h")}
        (inputs / "native-templates").mkdir()
        for name, data in native_files.items():
            (inputs / "native-templates" / Path(name).name).write_bytes(data)
        report["toolchain"] = toolchain_inputs(lock)
        runtime = Path(args.runtime_root).resolve()
        report["python_headers"] = directory_matches(runtime / "include", lock["runtime"]["cpython"]["headers"])
        checked_bytes(Path(sys.base_prefix) / "python314.dll", lock["runtime"]["cpython"]["dll"])
        pristine = Path(args.pristine_source).resolve()
        audit = audit_sources(pristine)
        if not audit["pinned_hashes_match"] or audit["source_scan"] != lock["runtime"]["pyinstaller"]["source_scan"]:
            raise ProbeInputError("pristine upstream source mismatch")
        report["pristine_source_scan"] = audit["source_scan"]
        report["pristine_build_driver"] = directory_matches(pristine / "bootloader", lock["runtime"]["pyinstaller"]["build_driver"])
        source = directory / "pyinstaller"
        shutil.copytree(pristine, source)
        directory_matches(source / "bootloader", lock["runtime"]["pyinstaller"]["build_driver"])
        files = {path.relative_to(source).as_posix(): path.read_bytes()
                 for path in (source / "bootloader").rglob("*") if path.is_file()}
        patch_contract = lock["entry_contract"]["patch"]
        report["patches"] = []
        patches = []
        for declaration in patch_contract["series"]:
            name = declaration["id"]
            data = (ROOT / "packaging/windows/frozen-entry/patches" / (name + ".patch")).read_bytes()
            (inputs / (name + ".patch")).write_bytes(data)
            report["patches"].append({"id": name, **byte_fact(data)})
            patches.append(Patch(name, data))
        replayed = replay_native_sources(files, patches, patch_contract, native_files)
        files = replayed.files
        (directory / "applied-sources.json").write_bytes(replayed.provenance_bytes)
        report["applied_sources"] = byte_fact(replayed.provenance_bytes)
        producer_inputs = getattr(args, "producer_inputs", None)
        entries = producer_inputs.entries if producer_inputs is not None else runtime_entries(runtime, lock)
        if producer_inputs is not None:
            (directory / "producer-inputs.json").write_bytes(canonical(producer_inputs.provenance))
            report.update(producer_inputs=byte_fact(canonical(producer_inputs.provenance)))
        prepared, header, source_facts = prepare_source_manifest(
            files, entries, lock["candidate_input_digest"],
            applied_sources_digest=replayed.applied_sources_digest)
        report["template_source_inventory"] = source_facts
        for name, content in files.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_bytes(content)
        generated = directory / "generated"
        generated.mkdir()
        header_bytes = header.encode("utf-8")
        (source / "bootloader/src/localcat_manifest.h").write_bytes(header_bytes)
        (generated / "localcat_manifest.h").write_bytes(header_bytes)
        report["rendered_manifest_header"] = byte_fact(header_bytes)
        (directory / "prelink.json").write_bytes(prepared.prelink_bytes)
        (directory / "template-source-inventory.json").write_bytes(canonical(source_facts))
        report["prelink"] = byte_fact(prepared.prelink_bytes)
        report["runtime_manifest"] = byte_fact(prepared.runtime_bytes)
        bundle = directory / "bundle"
        bundle.mkdir()
        for entry in entries:
            target = bundle / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry.content)
        (bundle / "localcat-runtime.manifest").write_bytes(prepared.runtime_bytes)
        environment = os.environ.copy()
        for key in ("CL", "_CL_", "LINK", "_LINK_", "PYTHONPATH", "PYTHONHOME", "PYTHONHASHSEED"):
            environment.pop(key, None)
        report["build_environment"] = {key: environment.get(key) for key in (
            "PATH", "INCLUDE", "LIB", "LIBPATH", "VCToolsVersion", "WindowsSDKVersion")}
        report["producer"] = {"script": byte_fact(producer_bytes),
                              "python_executable": str(Path(sys.executable).resolve())}
        (inputs / Path(__file__).name).write_bytes(producer_bytes)
        command = [sys.executable, "-B", "waf", "configure", "--target-arch=64bit",
                   "--localcat-python-include=" + str(runtime / "include"),
                   "--localcat-generated-include=" + str(generated),
                   "build_releasew", "install_releasew", "-j1", "-v"]
        record_command(command, source / "bootloader", environment, directory, "waf", report["commands"], timeout=300)
        installed = source / "PyInstaller/bootloader/Windows-64bit-intel/runw.exe"
        report["result_pe"] = audit_pe(installed, "diagnostic custom runw")
        # The reused stock auditor's allowlist is not the current custom contract.
        report["result_pe"].pop("entry_name_allowlist")
        report["result_pe"].pop("non_allowlisted_static_import_names")
        import pefile
        with pefile.PE(str(installed)) as pe:
            report["result_pe"].update(
                entrypoint_rva=pe.OPTIONAL_HEADER.AddressOfEntryPoint,
                timestamp=pe.FILE_HEADER.TimeDateStamp,
                security_directory_size=pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size,
                debug_directory_types=[entry.struct.Type for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", [])])
        report["expected_pe_delta"] = import_delta(report["result_pe"], lock["entry_contract"]["expected_pe"])
        bound = sorted(set(re.findall(rb"BIND\((Py\w+)\)", native_files["bootloader/src/localcat_frozen_bootstrap.c"])))
        bound_names = [name.decode() for name in bound]
        expected = set(lock["entry_contract"]["python_c_api"]["function_symbols"])
        report["python_api_source_projection"] = {"bound_functions": bound_names,
            "missing_target_functions": sorted(expected - set(bound_names)),
            "extra_functions": sorted(set(bound_names) - expected), "is_runtime_binding_proof": False}
        executable = bundle / "localcat-custom-runw.exe"
        shutil.copyfile(installed, executable)
        if args.packaging_dependencies:
            packaging_dir = directory / "packaging"
            driver, packaging_inputs = prepare_packaging(
                packaging_dir, archive=pristine.parent / lock["runtime"]["pyinstaller"]["sdist"]["name"],
                sdist_fact=lock["runtime"]["pyinstaller"]["sdist"], runw=installed,
                bundle=bundle, entries=entries, dependency_source=args.packaging_dependencies,
                candidate_digest=lock["candidate_input_digest"], packaging_target=packaging_target)
            packaging_environment = environment.copy()
            for key in list(packaging_environment):
                if key.upper().startswith(("PYTHON", "PYINSTALLER", "_PYINSTALLER", "QT_")):
                    packaging_environment.pop(key, None)
            packaging_environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            report["packaging_inputs"] = byte_fact(canonical(packaging_inputs))
            record_command([sys.executable, "-I", "-S", "-B", str(driver), str(packaging_dir),
                            report["packaging_inputs"]["sha256"]],
                           packaging_dir, packaging_environment, directory, "pyinstaller", report["commands"], timeout=120)
            report["packaging"] = verify_packaging_result(packaging_dir, packaging_inputs)
            executable = packaging_dir / "dist/localcat-spike/localcat-spike.exe"
            packaged = audit_pe(executable, "PyInstaller packaged custom runw")
            packaged.pop("entry_name_allowlist")
            packaged.pop("non_allowlisted_static_import_names")
            with pefile.PE(str(executable)) as pe:
                manifests = []
                for resource in getattr(pe, "DIRECTORY_ENTRY_RESOURCE").entries:
                    if resource.id == 24:
                        for name in resource.directory.entries:
                            for language in name.directory.entries:
                                item = language.data.struct
                                manifests.append(pe.get_data(item.OffsetToData, item.Size))
                packaged.update(timestamp=pe.FILE_HEADER.TimeDateStamp,
                                security_directory_size=pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size,
                                debug_directory_types=[entry.struct.Type for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", [])])
                if (manifests != [APPLICATION_MANIFEST] or pe.OPTIONAL_HEADER.Subsystem != 2
                        or packaged["security_directory_size"] != 0
                        or packaged["debug_directory_types"] != [16] or packaged["timestamp"] != 0):
                    raise ProbeInputError("packaged PE manifest/security/debug/windowed contract mismatch")
            report["packaged_pe"] = packaged
            report["packaged_pe_delta"] = import_delta(packaged, lock["entry_contract"]["expected_pe"])
            if not report["packaged_pe_delta"]["exact_match"]:
                raise ProbeInputError("packaged PE imports differ from approved contract")
            report["limitations"].remove("no PyInstaller appended CArchive or final distribution packaging")
            report["limitations"].append("candidate-bound packaging inputs are not the realized-build lock or complete W3 approval")
        report["status"] = "BUILD_DIAGNOSTIC_COLLECTED"
        if args.run:
            cwd = directory / "non-bundle-cwd"
            cwd.mkdir()
            run_environment = environment.copy()
            for key in list(run_environment):
                if key.upper().startswith(("PYTHON", "QT_")) or key.upper() == "__PYVENV_LAUNCHER__":
                    run_environment.pop(key, None)
            run_environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            report["run_environment_projection"] = {"PATH": run_environment["PATH"], "python_qt_variables": "removed"}
            record_command([str(executable)], cwd, run_environment, directory, "runw", report["commands"], timeout=60)
            record_windowed_no_standard_handles([str(executable)], cwd, run_environment, report["commands"], timeout=60)
            report["status"] = "EXECUTION_DIAGNOSTIC_COLLECTED"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["status"] = "DIAGNOSTIC_FAILED"
        raise
    finally:
        (directory / "diagnostic.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return directory, report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--pristine-source", type=Path, required=True)
    result.add_argument("--runtime-root", type=Path, default=Path(sys.base_prefix))
    result.add_argument("--candidate-input", type=Path, default=ROOT / "packaging/windows/frozen-entry/candidate-input.lock.json")
    result.add_argument("--native-directory", type=Path, default=NATIVE)
    result.add_argument("--output-parent", type=Path, default=ROOT / "artifacts/windows")
    result.add_argument("--run", action="store_true")
    result.add_argument("--packaging-dependencies", type=Path,
                        help="explicit site-packages source to snapshot for isolated PyInstaller packaging")
    return result


if __name__ == "__main__":
    run_probe(parser().parse_args())
