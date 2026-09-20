"""Exercise the actual ordinary EXE; no injected Core inputs or authorities."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from frozen_candidate import load_candidate
from frozen_worker_transport import _worker_header


def clean_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("PYTHON", "QT_", "QML", "_PYI", "PYINSTALLER"))}
    environment["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
    return environment


def close_window(pid: int) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    found = []

    @callback_type
    def visit(window, _parameter):
        process = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process))
        if process.value == pid and user32.IsWindowVisible(window):
            if not user32.PostMessageW(window, 0x0010, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            found.append(True)
        return True

    user32.EnumWindows(visit, 0)
    return bool(found)


def child_handles(parent_pid: int) -> list[tuple[int, int]]:
    """Retain actual process handles so PID reuse cannot fake cleanup evidence."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("executable", wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entries = []
    row = ProcessEntry()
    row.size = ctypes.sizeof(row)
    try:
        present = kernel.Process32FirstW(snapshot, ctypes.byref(row))
        while present:
            if row.parent == parent_pid and row.executable.lower() == "localcat.exe":
                handle = kernel.OpenProcess(0x00100000 | 0x1000 | 1, False, row.pid)
                if handle:
                    entries.append((row.pid, handle))
            present = kernel.Process32NextW(snapshot, ctypes.byref(row))
    finally:
        kernel.CloseHandle(snapshot)
    return entries


def check_running_close(executable: Path, work: Path, options: dict, *, terminate_child: bool = False) -> dict:
    marker = work / "closing.json"
    parent = subprocess.Popen([str(executable), "--smoke-test", "--data-dir", str(work / "closing-data"),
                               "--bundle-smoke-marker", str(marker)],
                              creationflags=subprocess.DETACHED_PROCESS, **options)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    children = []
    try:
        deadline = time.monotonic() + 30
        while parent.poll() is None and time.monotonic() < deadline:
            children = child_handles(parent.pid)
            if children:
                break
            time.sleep(0.01)
        assert children, "no running actual worker observed"
        started = time.monotonic()
        if terminate_child:
            assert kernel.TerminateProcess(children[0][1], 99)
        else:
            assert close_window(parent.pid)
        parent.wait(timeout=15)
        for _pid, handle in children:
            assert kernel.WaitForSingleObject(handle, 0) == 0, "child survived product close"
        late = child_handles(parent.pid)
        try:
            assert not late, "late child remained after product exit"
        finally:
            for _pid, handle in late:
                kernel.CloseHandle(handle)
        payload = json.loads(marker.read_bytes())
        assert "error_type" in payload["ordinary_core"]
        return {"parent_exit": parent.returncode, "child_pids": [pid for pid, _handle in children],
                "children_exited": True, "elapsed_seconds": time.monotonic() - started,
                "outcome": payload["ordinary_core"]}
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        for _pid, handle in children:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 99)
                kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)


def check_early_close(executable: Path, work: Path, options: dict) -> dict:
    marker = work / "early-close.json"
    parent = subprocess.Popen([str(executable), "--smoke-test", "--data-dir", str(work / "early-data"),
                               "--bundle-smoke-marker", str(marker)],
                              creationflags=subprocess.DETACHED_PROCESS, **options)
    closed = False
    try:
        deadline = time.monotonic() + 30
        while parent.poll() is None and time.monotonic() < deadline:
            if close_window(parent.pid):
                closed = True
                break
            time.sleep(0.005)
        assert closed
        parent.wait(timeout=15)
        outcome = json.loads(marker.read_bytes())["ordinary_core"]
        assert "error_type" in outcome
        assert outcome["generations_after_failure"]["retrieval"] == 0
        return {"parent_exit": parent.returncode, "outcome": outcome}
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def check_real_child_reply_faults(candidate, work: Path, fingerprint: str, options: dict) -> dict:
    """Real EXE children, then external faults on their observed pipe responses.

    This harness creates test-only wire data, never a Core session or authority.
    The unmodified response must pass production consumers before fault copies.
    """
    import tm_benchmark as benchmark
    import tm_benchmark_process as migration
    import tm_benchmark_query_process as query
    from tm_contracts import BenchmarkExecutionPath, contract_to_json
    from frozen_candidate import canonical_json
    from frozen_worker_transport import _collect_child, _ordinary_child_response

    run_root = work / "reply-faults"
    run_root.mkdir()
    contract = benchmark._parse_benchmark_contract_text(candidate.read_data("benchmark_tm_contract.json").decode("utf-8"))
    records = tuple(benchmark.iter_corpus_records(seed=contract.corpus_seed, record_count=12))
    fixture = run_root / "fixture.jsonl"
    fixture_digest, count = migration._generate_fixture(fixture, records)
    execution_path = BenchmarkExecutionPath.GRAM_FALLBACK
    # Fingerprint is observed from this candidate's real smoke, and the child
    # independently checks it. No source digest or mock session substitutes it.
    fields = dict(contract_digest=migration.benchmark_contract_digest(contract),
                  corpus_digest=benchmark.benchmark_digest(contract.corpus_generator_version, "corpus", [benchmark._record_payload(row) for row in records]),
                  corpus_record_count=count, fixture_digest=fixture_digest, fixture_path=str(fixture),
                  fixture_record_count=count, run_root=str(run_root), execution_path=execution_path,
                  resource_id="tm.reply-check", canonical_store_id="store.reply-check", test_mode=True,
                  proof_query_version=migration.CANDIDATE_PROOF_QUERY_VERSION, implementation_fingerprint=fingerprint)
    request = dict(fields, execution_path=execution_path.value, contract_json=contract_to_json(contract),
                   protocol=migration.PROCESS_WORKER_PROTOCOL_VERSION,
                   protocol_digest=migration.worker_protocol_digest(**fields))
    checks = {}
    process_evidence = None
    for kind in ("migration", "query"):
        if kind == "query":
            request = query._request_payload(mode="probe", process_evidence=process_evidence,
                                            run_root=str(run_root), fixture_path=str(fixture))
        command = (str(candidate.executable), "--localcat-tm-worker", kind)
        header = _worker_header(candidate.candidate_id, uuid4().hex)
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        # Popen retains the real Win32 process handle through wait/collection.
        raw, diagnostics = _collect_child(child, header + canonical_json(request), 60.0)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        assert kernel.WaitForSingleObject(int(child._handle), 0) == 0
        assert child.returncode == 0 and diagnostics == b""
        assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
        completed = _ordinary_child_response(command, child.returncode, raw, diagnostics, header)
        if kind == "migration":
            process_evidence = migration._evidence_from_stdout(completed.stdout.decode("utf-8"))
            assert process_evidence.child_pid == child.pid and process_evidence.test_mode
            assert not process_evidence.final_evidence
            decode = migration._evidence_from_stdout
        else:
            response = query._read_child_response(completed.stdout.decode("utf-8"))
            assert response["kind"] == "probe" and response["protocol"] == query.QUERY_WORKER_PROTOCOL_VERSION
            assert response["query_pid"] == child.pid
            probe = query.query_probe_from_payload(response["payload"])
            query._adjudicate_probe_against_process_evidence(
                probe=probe, process_evidence=process_evidence, query_child_pid=child.pid,
                request_protocol_digest=request["protocol_digest"])
            decode = query._read_child_response
        observed_header = json.loads(header)
        faults = {
            "wrong_reply_candidate": _worker_header("0" * 64, observed_header["request_id"]) + completed.stdout,
            "wrong_reply_request": _worker_header(candidate.candidate_id, "0" * 32) + completed.stdout,
            "truncated_reply_header": header[:-1],
            "malformed_reply_payload": header + b"[]",
            "truncated_reply_payload": header + completed.stdout[:len(completed.stdout) // 2],
        }
        rejected = {}
        for name, altered in faults.items():
            try:
                reply = _ordinary_child_response(command, child.returncode, altered, diagnostics, header)
                decode(reply.stdout.decode("utf-8"))
            except (OSError, ValueError, TypeError) as error:
                rejected[name] = {"exception": type(error).__name__, "diagnostic": str(error)}
            else:
                raise AssertionError("production consumer accepted " + name)
        checks[kind] = {"candidate_id": candidate.candidate_id, "parent_pid": os.getpid(), "child_pid": child.pid,
                        "child_exit": child.returncode, "retained_handle_signaled": True, "pipe_endpoints_closed": True,
                        "normal_response_validated": True, "faults_rejected": rejected,
                        "evidence_kind": "actual_exe_with_external_response_fault_injection", "final_evidence": False}
    return checks


def run_checks(executable: Path, report: Path) -> dict:
    candidate = load_candidate(executable, verify_payload=True)
    environment = clean_environment()
    work = report.parent / ("entry-" + uuid4().hex[:8])
    work.mkdir()
    facts = {"candidate_id": candidate.candidate_id, "final_evidence": False, "checks": {}}
    checks = facts["checks"]
    options = dict(cwd=executable.parent.parent, env=environment, close_fds=True)

    # No console or inherited standard streams, as with a graphical launch.
    marker = work / "smoke.json"
    child = subprocess.Popen([str(executable), "--smoke-test", "--data-dir", str(work / "data"),
                              "--bundle-smoke-marker", str(marker)],
                             creationflags=subprocess.DETACHED_PROCESS, **options)
    try:
        code = child.wait(timeout=360)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    smoke = json.loads(marker.read_bytes())
    core = smoke["ordinary_core"]
    assert code == 0 and core["candidate_id"] == candidate.candidate_id
    assert core["standard_streams_none"] == [True, True, True]
    assert core["test_mode"] and not core["final_evidence"]
    pids = [entry[key] for entry in core["paths"] for key in ("migration_pid", "query_pid")]
    assert len(set(pids)) == 4 and child.pid not in pids
    checks["detached_product_chain"] = smoke
    checks["actual_child_response_faults"] = check_real_child_reply_faults(
        candidate, work, core["implementation_fingerprint"], options)

    cases = {
        "missing_header": b"",
        "wrong_candidate": _worker_header("0" * 64, uuid4().hex) + b"{}",
        "malformed_header": b'{"protocol":\n',
        "invalid_request": _worker_header(candidate.candidate_id, uuid4().hex) + b"{}",
        "truncated_request": _worker_header(candidate.candidate_id, uuid4().hex) + b'{"version":',
    }
    for kind in ("migration", "query"):
        for name, request in cases.items():
            result = subprocess.run([str(executable), "--localcat-tm-worker", kind], input=request,
                                    capture_output=True, timeout=20, **options)
            assert result.returncode != 0 and not result.stderr
            detail = {"exit_code": result.returncode}
            if name in ("invalid_request", "truncated_request"):
                header, payload = result.stdout.split(b"\n", 1)
                assert header == request.split(b"\n", 1)[0]
                error = json.loads(payload)
                expected = ("PROCESS" if kind == "migration" else "QUERY") + ".REQUEST_INVALID"
                if kind == "migration" and name == "invalid_request":
                    expected = "PROCESS.CHILD_FAILED"  # Existing missing-field classification.
                assert error == {"error_code": expected}
                detail.update(error)
            else:
                assert result.stdout == b""
            checks[kind + "/" + name] = detail
        child = subprocess.Popen([str(executable), "--localcat-tm-worker", kind],
                                 creationflags=subprocess.DETACHED_PROCESS, **options)
        try:
            assert child.wait(timeout=20) != 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        checks[kind + "/missing_pipes"] = {"exit_code": child.returncode}
        for name, incoming, outgoing in (("wrong_input_pipe", subprocess.DEVNULL, subprocess.PIPE),
                                         ("wrong_output_pipe", subprocess.PIPE, subprocess.DEVNULL)):
            child = subprocess.Popen([str(executable), "--localcat-tm-worker", kind], stdin=incoming,
                                     stdout=outgoing, stderr=subprocess.PIPE, **options)
            try:
                output, errors = child.communicate(timeout=20)
                assert child.returncode != 0 and not output and not errors
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
            checks[kind + "/" + name] = {"exit_code": child.returncode}

    # Normal product entry, with neither a smoke selector nor a project.
    child = subprocess.Popen([str(executable), "--data-dir", str(work / "home-data")],
                             creationflags=subprocess.DETACHED_PROCESS, **options)
    deadline = time.monotonic() + 30
    closed = False
    try:
        while child.poll() is None and time.monotonic() < deadline:
            if close_window(child.pid):
                closed = True
                break
            time.sleep(0.05)
        assert closed and child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    checks["normal_home_close"] = {"exit_code": child.returncode, "visible_window": closed}
    checks["early_close_before_gate_c_install"] = check_early_close(executable, work, options)
    checks["close_with_running_worker"] = check_running_close(executable, work, options)
    terminated = work / "terminated"
    terminated.mkdir()
    checks["worker_abnormal_exit"] = check_running_close(executable, terminated, options, terminate_child=True)

    project = work / "project.txt"
    project.write_text("Ordinary frozen project entry.\n", encoding="utf-8")
    project_marker = work / "project.json"
    child = subprocess.Popen([str(executable), "--project", str(project), "--data-dir", str(work / "data"),
                              "--bundle-smoke-marker", str(project_marker)],
                             creationflags=subprocess.DETACHED_PROCESS, **options)
    try:
        deadline = time.monotonic() + 30
        while child.poll() is None and not project_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert project_marker.exists() and close_window(child.pid)
        assert child.wait(timeout=20) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    checks["project_parameter_close"] = {"exit_code": child.returncode, "marker": json.loads(project_marker.read_bytes())}

    # Only this explicitly supplied disposable distribution copy is changed;
    # the input record is restored even if the rejection assertion fails.
    record = candidate.bundle_root / "localcat-owner-inputs.json"
    held = record.with_name("ordinary-check-inputs-" + uuid4().hex + ".tmp")
    record.rename(held)
    try:
        child = subprocess.Popen([str(executable), "--data-dir", str(work / "missing-input-data")],
                                 creationflags=subprocess.DETACHED_PROCESS, **options)
        try:
            assert child.wait(timeout=20) != 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        assert not (work / "missing-input-data").exists()
        checks["missing_owner_inputs"] = {"exit_code": child.returncode, "data_directory_created": False}
    finally:
        held.rename(record)
    load_candidate(executable, verify_payload=True)
    report.write_text(json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return facts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    result = run_checks(args.executable.resolve(), report)
    print(json.dumps({"candidate_id": result["candidate_id"], "checks": len(result["checks"]),
                      "report": str(report), "final_evidence": False}))
