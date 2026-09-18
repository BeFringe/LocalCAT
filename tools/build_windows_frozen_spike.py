"""Build a clean, externally anchored W3 spike; do not grant W3 acceptance."""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import re

from tools import probe_windows_frozen_custom_runw as probe
from tools.audit_windows_frozen_custom_entry import finalize_compile_environment
from tools.prepare_windows_frozen_entry_inputs import _evidence_producer_fact
from tools.windows_frozen_packaging import APPLICATION_MANIFEST, canonical, fact
from tools.windows_frozen_patch_series import Patch
from tools.windows_frozen_release import (
    ReleaseBindingError, clean_source_context, create_release_binding,
    verify_release_binding, _no_reparse_ancestors,
)


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging/windows/frozen-entry/candidate-input.lock.json"


def empty_carchive(version):
    major, minor, _ = map(int, version.split("."))
    library = f"python{major}{minor}.dll".encode("ascii")
    # Pinned PyInstaller CArchive cookie. An empty archive has no preceding
    # records, options or TOC; require the entire PE overlay to equal it.
    return struct.pack("!8sIIII64s", b"MEI\014\013\012\013\016", 88, 0, 0,
                       major * 100 + minor, library)


def check_pe_contract(actual, manifests, overlay, expected_imports, python_version):
    if (actual["subsystem"] != 2 or actual["timestamp"] != 0
            or actual["security_directory_size"] != 0 or actual["debug_directory_types"] != [16]
            or manifests != [APPLICATION_MANIFEST] or overlay != empty_carchive(python_version)
            or not probe.import_delta(actual, expected_imports)["exact_match"]):
        raise ReleaseBindingError("actual packaged PE differs from approved contract")


def verify_packaged_pe(path, lock):
    import pefile
    actual = probe.audit_pe(path, "final packaged custom runw")
    data = Path(path).read_bytes()
    with pefile.PE(data=data) as pe:
        manifests = []
        for resource in getattr(getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None), "entries", []):
            if resource.id == 24:
                for name in resource.directory.entries:
                    for language in name.directory.entries:
                        item = language.data.struct
                        manifests.append(pe.get_data(item.OffsetToData, item.Size))
        actual.update(subsystem=pe.OPTIONAL_HEADER.Subsystem, timestamp=pe.FILE_HEADER.TimeDateStamp,
                      security_directory_size=pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size,
                      debug_directory_types=[entry.struct.Type for entry in getattr(pe, "DIRECTORY_ENTRY_DEBUG", [])])
        overlay_start = pe.get_overlay_data_start_offset()
        overlay = data[overlay_start:] if overlay_start is not None else b""
    check_pe_contract(actual, manifests, overlay, lock["entry_contract"]["expected_pe"],
                      lock["runtime"]["cpython"]["version"])


def verify_files(root, expected):
    result = {}
    for name, content in expected.items():
        path = _no_reparse_ancestors(Path(root) / name)
        if path.read_bytes() != content:
            raise ReleaseBindingError("build file differs from independently prepared input: " + name)
        result[name] = fact(content)
    return result


def verify_waf_inputs(directory, toolchain, projection, runtime):
    """Check Waf's effective cache AND the actual release compile/link commands."""
    bootloader = directory / "pyinstaller/bootloader"
    cache = {}
    tree = ast.parse((bootloader / "build/c4che/releasew_cache.py").read_bytes())
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ReleaseBindingError("nonliteral Waf configuration")
        name = node.targets[0].id
        if name in cache:
            raise ReleaseBindingError("duplicate Waf configuration")
        cache[name] = ast.literal_eval(node.value)

    def paths(values, cwd=None):
        return [os.path.normcase(str(((cwd / value) if cwd else Path(value)).resolve())) for value in values]

    tools = toolchain["tools"]
    for key, role in (("CC", "cl"), ("CXX", "cl"), ("LINK_CC", "link"), ("LINK_CXX", "link"),
                      ("WINRC", "rc"), ("MT", "mt")):
        if paths(cache.get(key, [])) != paths([tools[role]["path"]]):
            raise ReleaseBindingError("Waf selected a different tool: " + key)
    include = [item["path"] for item in projection["include_roots"]]
    libraries = [item["path"] for item in projection["lib_roots"]]
    if (not cache.get("NO_MSVC_DETECT") or paths(cache.get("INCLUDES", [])) != paths(include)
            or paths(cache.get("LIBPATH", [])) != paths(libraries)):
        raise ReleaseBindingError("Waf compiler search roots differ from locked inputs")
    cwd = bootloader / "build/releasew"
    expected_includes = paths(["src", "../../src", str(runtime / "include"), str(directory / "generated"), *include], cwd)
    wanted_sources = {"main.c", *(name + ".c" for name in probe.MODULES)}
    compiled, links, in_release = set(), 0, False
    for line in (directory / "waf.stdout").read_text(encoding="utf-8", errors="strict").splitlines():
        if line.startswith("Waf: Entering directory"):
            in_release = True
        if not in_release:
            continue
        match = re.search(r"\brunner (\[.*\])$", line)
        if not match:
            continue
        command = ast.literal_eval(match[1])
        if type(command) is not list or any(type(item) is not str for item in command):
            raise ReleaseBindingError("invalid Waf command")
        if paths(command[:1]) == paths([tools["cl"]["path"]]):
            actual_includes = [item[2:] for item in command[1:] if item.startswith("/I")]
            sources = [item for item in command[1:] if item.lower().endswith(".c")]
            if (paths(actual_includes, cwd) != expected_includes or len(sources) != 1
                    or paths(sources, cwd) != paths([str(bootloader / "src" / Path(sources[0]).name)])
                    or Path(sources[0]).name not in wanted_sources
                    or Path(sources[0]).name in compiled
                    or any(flag not in command for flag in ("/MT", "/Brepro", "/guard:cf", "/WX"))):
                raise ReleaseBindingError("actual compile command differs from locked inputs")
            compiled.add(Path(sources[0]).name)
        elif paths(command[:1]) == paths([tools["link"]["path"]]):
            actual_libraries = [item[len("/LIBPATH:"):] for item in command[1:] if item.startswith("/LIBPATH:")]
            if paths(actual_libraries) != paths(libraries) or "/Brepro" not in command:
                raise ReleaseBindingError("actual link command differs from locked inputs")
            links += 1
        else:
            raise ReleaseBindingError("unexpected release build command")
    if compiled != wanted_sources or links != 1:
        raise ReleaseBindingError("incomplete release compile/link evidence")


def expected_inputs(pristine, runtime, lock):
    pristine = _no_reparse_ancestors(pristine)
    runtime = _no_reparse_ancestors(runtime)
    probe.directory_matches(pristine / "bootloader", lock["runtime"]["pyinstaller"]["build_driver"])
    files = {path.relative_to(pristine).as_posix(): path.read_bytes()
             for path in (pristine / "bootloader").rglob("*") if path.is_file()}
    contract = lock["entry_contract"]["patch"]
    patches = [Patch(item["id"], (ROOT / "packaging/windows/frozen-entry/patches" / (item["id"] + ".patch")).read_bytes())
               for item in contract["series"]]
    native = {"bootloader/src/" + name + suffix: (probe.NATIVE / (name + suffix)).read_bytes()
              for name in probe.MODULES for suffix in (".c", ".h")}
    replayed = probe.replay_native_sources(files, patches, contract, native)
    prepared, header, templates = probe.prepare_source_manifest(
        replayed.files, probe.runtime_entries(runtime, lock), lock["candidate_input_digest"],
        applied_sources_digest=replayed.applied_sources_digest)
    evidence = {"applied-sources.json": replayed.provenance_bytes,
                "prelink.json": prepared.prelink_bytes,
                "template-source-inventory.json": canonical(templates),
                "generated/localcat_manifest.h": header.encode("utf-8"),
                "inputs/candidate-input.lock.json": canonical(lock)}
    sources = {"pyinstaller/" + name: content for name, content in replayed.files.items()}
    sources["pyinstaller/bootloader/src/localcat_manifest.h"] = header.encode("utf-8")
    return prepared, evidence, sources


@contextmanager
def compile_environment(lock):
    """Limit build lookup to the already pinned x64 compiler/SDK projection."""
    original = dict(os.environ)
    normalized = dict(original)
    lookup = {name.upper(): value for name, value in original.items()}
    for name in ("VCToolsInstallDir", "VCToolsVersion", "WindowsSdkDir", "WindowsSDKVersion",
                 "UniversalCRTSdkDir", "UCRTVersion"):
        value = lookup.get(name.upper())
        if not value:
            raise ReleaseBindingError("activate the pinned vcvars environment; missing " + name)
        normalized[name] = value
    environment, projection = finalize_compile_environment(normalized, lock["toolchain"]["msvc"]["toolset_version"],
                                                   lock["toolchain"]["windows_sdk"]["version"])
    cl, rc = shutil.which("cl.exe"), shutil.which("rc.exe")
    if not cl or not rc:
        raise ReleaseBindingError("activate the pinned vcvars environment")
    if (Path(cl).resolve().parents[3] != Path(normalized["VCToolsInstallDir"]).resolve()
            or Path(rc).resolve().parents[3] != Path(normalized["WindowsSdkDir"]).resolve()):
        raise ReleaseBindingError("compiler search roots and executable roots disagree")
    environment["PATH"] = os.pathsep.join((str(Path(cl).parent), str(Path(rc).parent),
                                           str(Path(lookup["SYSTEMROOT"]) / "System32")))
    for key in list(environment):
        if key.upper().startswith(("PYTHON", "PYINSTALLER", "_PYINSTALLER", "QT_", "MIMALLOC_")) or key.upper() in {
                "CL", "_CL_", "LINK", "_LINK_", "__PYVENV_LAUNCHER__", "CC", "CXX", "LINK_CC", "LINK_CXX",
                "AR", "MT", "WINRC", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LINKFLAGS", "LDFLAGS", "ARFLAGS", "RCFLAGS"}:
            environment.pop(key)
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield projection
    finally:
        os.environ.clear()
        os.environ.update(original)


def build_spike(args):
    context = clean_source_context(ROOT)
    if context["repository_commit"] != args.expected_commit:
        raise ReleaseBindingError("external repository commit mismatch")
    if fact(LOCK.read_bytes())["sha256"] != args.expected_candidate_lock_sha256:
        raise ReleaseBindingError("external candidate lock anchor mismatch")
    lock = probe.load_candidate(LOCK)
    output = _no_reparse_ancestors(args.output)
    if not output.is_relative_to(ROOT / "artifacts/windows") or output.exists():
        raise ReleaseBindingError("build output must be a new artifacts/windows directory")
    prepared, evidence, sources = expected_inputs(args.pristine_source, args.runtime_root, lock)
    producer = _evidence_producer_fact(python_root=args.runtime_root, vswhere=args.vswhere,
                                     packaging_dependencies=args.packaging_dependencies)
    if producer != lock["evidence_producer"]:
        raise ReleaseBindingError("executing build producer differs from candidate")
    if clean_source_context(ROOT) != context:
        raise ReleaseBindingError("source changed before build")
    output.mkdir(parents=True, exist_ok=False)
    probe_args = argparse.Namespace(pristine_source=args.pristine_source, runtime_root=args.runtime_root,
                                   candidate_input=LOCK, native_directory=probe.NATIVE, output_parent=output,
                                   packaging_dependencies=args.packaging_dependencies, run=True)
    with compile_environment(lock) as projection:
        directory, _ = probe.run_probe(probe_args)
        # Recheck the external tool/runtime snapshots; never accept report flags
        # in lieu of the current files used by this synchronous build.
        actual_toolchain = probe.toolchain_inputs(lock)
        verify_waf_inputs(directory, actual_toolchain, projection, args.runtime_root)
    if _evidence_producer_fact(python_root=args.runtime_root, vswhere=args.vswhere,
                              packaging_dependencies=args.packaging_dependencies) != producer:
        raise ReleaseBindingError("build producer changed during build")
    repeated = expected_inputs(args.pristine_source, args.runtime_root, lock)
    if repeated != (prepared, evidence, sources) or clean_source_context(ROOT) != context:
        raise ReleaseBindingError("build inputs changed during build")
    verify_files(directory, sources)
    build_evidence = verify_files(directory, evidence)
    dist = directory / "packaging/dist/localcat-spike"
    verify_packaged_pe(dist / "localcat-spike.exe", lock)
    data = create_release_binding(dist, source_context=context, prepared=prepared, build_evidence=build_evidence)
    # This digest is obtained by the clean orchestrator, not read from a child
    # report. The caller must retain it outside the distribution when verifying.
    digest = fact(data)["sha256"]
    verify_release_binding(data, dist, expected_release_sha256=digest,
                           expected_repository_commit=context["repository_commit"],
                           expected_candidate_input_digest=lock["candidate_input_digest"])
    with (output / "release.json").open("xb") as stream:
        stream.write(data)
    receipt = {"schema": "localcat.windows-frozen-build-receipt.v1",
               "classification": "BUILD_BINDING_NOT_W3_ACCEPTANCE",
               "repository_commit": context["repository_commit"],
               "candidate_input_digest": lock["candidate_input_digest"],
               "release_sha256": digest, "dist_relative": dist.relative_to(output).as_posix()}
    with (output / "receipt.json").open("xb") as stream:
        stream.write(canonical(receipt))
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return receipt


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--expected-commit", required=True)
    result.add_argument("--expected-candidate-lock-sha256", required=True)
    result.add_argument("--pristine-source", type=Path, required=True)
    result.add_argument("--runtime-root", type=Path, default=Path(sys.base_prefix))
    result.add_argument("--packaging-dependencies", type=Path, required=True)
    result.add_argument("--vswhere", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


if __name__ == "__main__":
    build_spike(parser().parse_args())
