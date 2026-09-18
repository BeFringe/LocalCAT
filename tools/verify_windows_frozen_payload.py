"""Replay a bounded Task 1.6 prelaunch payload/pollution matrix on a bound PE.

This is evidence, not a release authority, a retained-handle race test, a full
MANIFEST_EXACT parser suite, or proof of absence of every checkout/CWD read.
The deliberately fixed twelve-file profile must not discover/authorize roots.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.windows_frozen_packaging import canonical, fact
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors


EXE = "localcat-spike.exe"
MANIFEST = "localcat-runtime.manifest"
EXTERNAL = "_internal/importlib/_bootstrap_external.py"
PROFILE = {
    "_internal/localcat_spike_critical.py": "critical-source",
    "_internal/localcat_frozen_bootstrap.py": "bootstrap",
    "_internal/encodings/__init__.py": "interpreter",
    "_internal/encodings/aliases.py": "interpreter",
    "_internal/encodings/utf_8.py": "interpreter",
    "_internal/encodings/_win_cp_codecs.py": "interpreter",
    EXTERNAL: "interpreter",
    "_internal/localcat_spike_fixture.txt": "fixture",
    "_internal/python314.dll": "native",
    "_internal/vcruntime140.dll": "native",
}
CLASSIFICATION = "PRELAUNCH_PAYLOAD_EVIDENCE_NOT_FULL_W3_GATE"
PROBE_TOKEN = "LOCALCAT_PAYLOAD_PROBE_EXECUTED"
BEGIN = "FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN"
COMPLETE = "FROZEN_ENTRY.SPIKE_COMPLETED"
LIMITATIONS = [
    "Only prelaunch changes; no retained-handle swap/race or full W3 acceptance.",
    "Manifest digest corruption only; not the complete MANIFEST_EXACT parser matrix.",
    "Control CWD is existing System32 outside checkout; polluted CWD/PYTHONPATH are evidence subdirectories inside checkout.",
    "Fixed source probes and invalid-PE .pyd discovery poison only; not a valid extension/DllMain probe or all CWD/checkout reads.",
    "No new application/system native roots authorized; no system-internal provider/service audit.",
    "Exit/stage markers are observed; DLL load ordering and all filesystem reads require separate trace evidence.",
]
BOUNDED_COVERAGE = {
    "W3.CUSTOM.SOURCE_EXACT_BYTES": "Prelaunch same-size tamper/missing for critical, bootstrap and five interpreter files; three unresigned call-site additions.",
    "W3.CUSTOM.SOURCE_ONLY": "Undeclared .py and critical .pyc/.pyz/__pycache__ copies.",
    "W3.CUSTOM.FIXTURE_EXACT_BYTES": "Prelaunch fixture tamper/missing, not proof-after-open swap.",
    "W3.CUSTOM.MANIFEST_EXACT": "Same-size manifest digest corruption only.",
    "W3.CUSTOM.NATIVE_CLOSURE_PRELOAD": "Two declared DLLs tamper/missing and duplicate Python DLL basename, not system-root expansion.",
    "W3.CUSTOM.NONREPO_CWD": "Existing external System32 CWD control and fixed CWD/PYTHONPATH pollution probes.",
}


@dataclass(frozen=True)
class Case:
    name: str
    operation: str = "control"
    path: str = ""
    content: bytes = b""
    marker: str = COMPLETE
    exit_code: int = 0

    @property
    def markers(self):
        return [BEGIN, COMPLETE] if self.exit_code == 0 else [self.marker]


def _cases():
    cases = [Case("control")]
    for index, name in enumerate(PROFILE):
        label = "%02d-%s" % (index, Path(name).name.replace(".", "-"))
        cases.append(Case(label + "-tamper", "tamper", name,
                          marker="FROZEN_ENTRY.ENTRY_DIGEST_MISMATCH", exit_code=1))
        cases.append(Case(label + "-missing", "missing", name,
                          marker="FROZEN_ENTRY.ROOTED_OPEN_FAILED", exit_code=1))
    cases.append(Case("manifest-tamper", "tamper", MANIFEST,
                      marker="FROZEN_ENTRY.MANIFEST_DIGEST_MISMATCH", exit_code=1))
    for label, name, marker in (
        ("extra-py", "_internal/undeclared.py", "INVENTORY_EXTRA"),
        ("extra-pyc", "_internal/localcat_spike_critical.pyc", "SOURCE_DUPLICATE"),
        ("extra-pyz", "_internal/localcat_spike_critical.pyz", "SOURCE_DUPLICATE"),
        ("extra-pycache", "_internal/__pycache__/localcat_spike_critical.pyc", "SOURCE_DUPLICATE"),
        ("duplicate-dll-basename", "_internal/encodings/python314.dll", "INVENTORY_EXTRA"),
    ):
        cases.append(Case(label, "extra", name, b"undeclared payload probe\n",
                          "FROZEN_ENTRY." + marker, 1))
    for label, statement in (
        ("nt-stat", b"import nt\nnt.stat('.')\n"),
        ("nt-startfile", b"import nt\nnt.startfile('LOCALCAT_UNREACHABLE_PROBE')\n"),
        ("imp-create-dynamic", b"import _imp\nimport _frozen_importlib\n"
         b"_imp.create_dynamic(_frozen_importlib.ModuleSpec('_localcat_probe', None, "
         b"origin='LOCALCAT_UNREACHABLE_PROBE.pyd'))\n"),
    ):
        # These unresigned additions change size. ENTRY_STALE is the earlier
        # exact-size guard; ordinary same-size tamper separately tests digest.
        cases.append(Case("external-" + label, "append", EXTERNAL,
                          b"\n# Unresigned, never-authorized call-site counterexample\n" + statement,
                          "FROZEN_ENTRY.ENTRY_STALE", 1))
    cases.extend((Case("cwd-pollution", "cwd-pollution"),
                  Case("pythonpath-pollution", "pythonpath-pollution")))
    return tuple(cases)


CASES = _cases()


def inventory(root):
    """Include forbidden names and empty directories without following reparse."""
    root = Path(root).absolute()
    _no_reparse_ancestors(root)
    if not root.is_dir():
        raise ValueError("inventory root is not a directory: " + str(root))
    files, directories, seen = {}, [], set()
    pending = [root]
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir()):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse member rejected: " + str(path))
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in seen:
                raise ValueError("case-fold duplicate: " + relative)
            seen.add(relative.casefold())
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = fact(path.read_bytes())
            else:
                raise ValueError("nonregular member: " + relative)
    return {"files": dict(sorted(files.items())), "directories": sorted(directories)}


def _write_json(path, value):
    path.write_bytes(canonical(value))


def run_child(command, *, cwd, env, timeout, stdout_path, stderr_path):
    """Wait a bounded time; only terminate the Popen child this call created."""
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, shell=False)
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            child.wait(timeout=10)
    return {"returncode": child.returncode, "timed_out": timed_out}


def _environment(temp):
    # Deliberate allowlist, not a copy of credentials or caller Python/Qt state.
    inherited = {key.upper(): value for key, value in os.environ.items()}
    if not inherited.get("SYSTEMROOT"):
        raise ValueError("Windows SYSTEMROOT is required")
    system = Path(inherited["SYSTEMROOT"]) / "System32"
    _no_reparse_ancestors(system)
    if not system.is_dir():
        raise ValueError("System32 CWD is unavailable")
    env = {"SystemRoot": inherited["SYSTEMROOT"], "WINDIR": inherited["SYSTEMROOT"],
           "PATH": str(system), "TEMP": str(temp), "TMP": str(temp)}
    for key in ("SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"):
        if key in inherited:
            env[key] = inherited[key]
    return env, system


def _expected_inventory(before, case, source):
    files = dict(before["files"])
    directories = set(before["directories"])
    if case.operation == "missing":
        del files[case.path]
    elif case.operation in {"tamper", "append", "extra"}:
        data = case.content
        if case.operation == "tamper":
            original = (source / case.path).read_bytes()
            if not original:
                raise ValueError("cannot same-size tamper empty member")
            data = original[:-1] + bytes([original[-1] ^ 1])
        elif case.operation == "append":
            data = (source / case.path).read_bytes() + case.content
        elif case.name == "duplicate-dll-basename":
            data = (source / "_internal/python314.dll").read_bytes()
        files[case.path] = fact(data)
        for parent in Path(case.path).parents:
            if str(parent) != ".":
                directories.add(parent.as_posix())
    return {"files": dict(sorted(files.items())), "directories": sorted(directories)}


def _mutate(bundle, case):
    target = bundle / case.path
    if case.operation == "missing":
        target.unlink()  # Only this case's fresh copy, never an input/evidence tree.
    elif case.operation == "tamper":
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(data)
    elif case.operation == "append":
        with target.open("ab") as stream:
            stream.write(case.content)
    elif case.operation == "extra":
        target.parent.mkdir(parents=True, exist_ok=True)
        if case.name == "duplicate-dll-basename":
            shutil.copyfile(bundle / "_internal/python314.dll", target)
        else:
            target.write_bytes(case.content)


def _probes(root):
    sentinel = root / "PROBE_EXECUTED.txt"
    code = ("import _io\n_io.open(%r, 'wb').write(%r)\nraise RuntimeError(%r)\n" %
            (str(sentinel), PROBE_TOKEN.encode("ascii"), PROBE_TOKEN)).encode("utf-8")
    # Invalid PE bytes cannot execute native code. They poison discovery only.
    for name in ("traceback.py", "encodings/__init__.py", "encodings/utf_8.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(code)
    for name in ("traceback.pyd", "encodings/utf_8.pyd", "_localcat_probe.pyd"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"INVALID_PE_DISCOVERY_POISON\n" + PROBE_TOKEN.encode("ascii"))


def _run_case(case, source, source_before, output, timeout, runner):
    directory = output / case.name
    directory.mkdir()
    bundle = directory / "dist"
    shutil.copytree(source, bundle)
    if inventory(bundle) != source_before:
        raise ValueError("copy differs before mutation: " + case.name)
    expected = _expected_inventory(source_before, case, source)
    _mutate(bundle, case)
    pre = inventory(bundle)
    probes = directory / "pollution"
    probes.mkdir()
    empty_cwd = directory / "empty-cwd"
    empty_cwd.mkdir()
    temp = directory / "temp"
    temp.mkdir()
    environment, system_cwd = _environment(temp)
    cwd = system_cwd if case.operation == "control" else empty_cwd
    if case.operation in {"cwd-pollution", "pythonpath-pollution"}:
        _probes(probes)
        if case.operation == "cwd-pollution":
            cwd = probes
        else:
            environment["PYTHONPATH"] = str(probes)
    probes_pre = inventory(probes)
    command = [str(bundle / EXE)]
    command_record = {"argv": command, "cwd": str(cwd), "environment": environment,
                      "timeout_seconds": timeout, "shell": False}
    _write_json(directory / "command.json", command_record)
    stdout, stderr = directory / "stdout.bin", directory / "stderr.bin"
    stdout.write_bytes(b"")
    stderr.write_bytes(b"")
    result = {"name": case.name, "operation": case.operation, "path": case.path,
              "expected_returncode": case.exit_code, "expected_markers": case.markers,
              "expected_pre": expected, "pre": pre, "command": command_record,
              "exact_delta": pre == expected, "probes_pre": probes_pre,
              "returncode": None, "timed_out": False, "error": None}
    _write_json(directory / "prelaunch.json", result)
    try:
        if not result["exact_delta"] or pre["files"][EXE] != source_before["files"][EXE]:
            raise ValueError("unexpected mutation; child not started")
        result.update(runner(command, cwd=cwd, env=environment, timeout=timeout,
                             stdout_path=stdout, stderr_path=stderr))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    try:
        result["post"] = inventory(bundle)
        result["probes_post"] = inventory(probes)
    except (OSError, ValueError) as exc:
        result["post"] = None
        result["probes_post"] = None
        result["error"] = str(exc)
    out, err = stdout.read_bytes(), stderr.read_bytes()
    result["stdout"] = fact(out)
    result["stderr"] = fact(err)
    result["markers"] = err.decode("utf-8", errors="replace").splitlines()
    result["executable_unchanged"] = (result["post"] is not None and
        pre["files"].get(EXE) == result["post"]["files"].get(EXE) == source_before["files"][EXE])
    result["probe_not_executed"] = (result["probes_post"] == probes_pre and
        PROBE_TOKEN.encode("ascii") not in out + err and not (probes / "PROBE_EXECUTED.txt").exists())
    result["passed"] = bool(not result["error"] and not result["timed_out"] and
        result["returncode"] == case.exit_code and result["markers"] == case.markers and
        out == b"" and result["exact_delta"] and result["post"] == pre and
        result["executable_unchanged"] and result["probe_not_executed"])
    _write_json(directory / "result.json", result)
    return result


def _output_directory(output, dist):
    artifacts = ROOT / "artifacts/windows"
    if artifacts == dist or dist in artifacts.parents:
        raise ValueError("evidence cannot be placed inside source dist")
    _no_reparse_ancestors(artifacts)
    if output is None:
        artifacts.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="payload-verification-", dir=artifacts))
    if ".." in Path(output).parts:
        raise ValueError("output cannot contain parent traversal")
    output = Path(output).absolute()
    _no_reparse_ancestors(output)
    if output == artifacts or artifacts not in output.parents or output.exists():
        raise ValueError("output must be a NEW directory beneath " + str(artifacts))
    if output == dist or dist in output.parents:
        raise ValueError("evidence cannot be placed inside source dist")
    output.mkdir(parents=True)
    return output


def run_verification(*, dist, release, expected_release_sha256,
                     expected_repository_commit, expected_candidate_input_digest,
                     output=None, timeout=30, _runner=run_child):
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be finite and in (0, 60] seconds")
    dist, release = Path(dist).absolute(), Path(release).absolute()
    _no_reparse_ancestors(release)
    release_before = release.read_bytes()
    anchors = dict(expected_release_sha256=expected_release_sha256,
                   expected_repository_commit=expected_repository_commit,
                   expected_candidate_input_digest=expected_candidate_input_digest)
    binding = verify_release_binding(release_before, dist, **anchors)
    if set(binding["dist"]) != set(PROFILE) | {EXE, MANIFEST}:
        raise ValueError("this verifier requires the exact twelve-file minimal-spike profile")
    source_before = inventory(dist)
    output = _output_directory(output, dist)
    observer = output / "observer"
    (observer / "tools").mkdir(parents=True)
    code_inventory = {}
    for name in ("verify_windows_frozen_payload.py", "windows_frozen_release.py",
                 "windows_frozen_manifest.py", "windows_frozen_packaging.py"):
        source = ROOT / "tools" / name
        data = source.read_bytes()
        code_inventory["tools/" + name] = fact(data)
        (observer / "tools" / name).write_bytes(data)
    observer_before = inventory(observer)
    anchor_bytes = canonical(anchors)
    (output / "anchors.json").write_bytes(anchor_bytes)
    (output / "release.json").write_bytes(release_before)
    invocation = [sys.executable, "-B", str(observer / "tools/verify_windows_frozen_payload.py"), "--dist", str(dist),
        "--release", str(release), "--expected-release-sha256", expected_release_sha256,
        "--expected-repository-commit", expected_repository_commit,
        "--expected-candidate-input-digest", expected_candidate_input_digest,
        "--timeout", str(timeout)]
    _write_json(output / "command.json", {"replay_argv": invocation, "actual_argv": sys.argv,
        "caller_cwd": str(Path.cwd()),
        "python": sys.version, "platform": sys.platform,
        "powershell": "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in invocation)})
    report = {"schema": "localcat.windows-frozen-payload-evidence.v1",
        "classification": CLASSIFICATION, "limitations": LIMITATIONS,
        "bounded_coverage": BOUNDED_COVERAGE, "passed": False,
        "output": str(output), "source_dist": str(dist), "source_release": str(release),
        "anchors": anchors, "release_pre": fact(release_before), "source_pre": source_before,
        "code_inventory": code_inventory, "observer_pre": observer_before,
        "expected_cases": [case.name for case in CASES], "cases": [], "error": None}
    _write_json(output / "prelaunch.json", report)
    try:
        for case in CASES:
            if inventory(dist) != source_before or release.read_bytes() != release_before:
                raise ValueError("source input drift; no further child launched")
            report["cases"].append(_run_case(case, dist, source_before, output, timeout, _runner))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    finally:
        try:
            report["source_post"] = inventory(dist)
            report["release_post"] = fact(release.read_bytes())
            report["observer_post"] = inventory(observer)
            report["source_unchanged"] = report["source_post"] == source_before
            report["release_unchanged"] = report["release_post"] == fact(release_before)
            report["anchors_unchanged"] = (output / "anchors.json").read_bytes() == anchor_bytes
            report["observer_unchanged"] = report["observer_post"] == observer_before and all(
                fact((ROOT / name).read_bytes()) == digest for name, digest in code_inventory.items())
            verify_release_binding(release.read_bytes(), dist, **anchors)
        except (OSError, ValueError) as exc:
            report["error"] = str(exc)
        for key in ("source_unchanged", "release_unchanged", "anchors_unchanged", "observer_unchanged"):
            report.setdefault(key, False)
        report["passed"] = bool(not report["error"] and len(report["cases"]) == len(CASES) and
            all(case["passed"] for case in report["cases"]) and
            all(report[key] for key in ("source_unchanged", "release_unchanged", "anchors_unchanged", "observer_unchanged")))
        _write_json(output / "result.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dist", "release", "expected-release-sha256", "expected-repository-commit", "expected-candidate-input-digest"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", help="new evidence directory beneath repository artifacts/windows")
    parser.add_argument("--timeout", type=float, default=30, help="per-child seconds, at most 60")
    arguments = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise ValueError("actual payload replay requires Windows")
        result = run_verification(**vars(arguments))
    except (OSError, ValueError) as exc:
        print("PAYLOAD_VERIFIER_INPUT_REJECTED: " + str(exc), file=sys.stderr)
        return 2
    print(canonical({"classification": CLASSIFICATION, "passed": result["passed"],
                     "evidence": result["output"]}).decode("utf-8"))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
