"""Directed same-executable transports for ordinary and native frozen entries."""
from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from uuid import uuid4
from collections.abc import Callable
from typing import Any

_ORDINARY_PROTOCOL = "localcat-ordinary-worker-v1"


def _worker_header(candidate_id: str, request_id: str) -> bytes:
    from frozen_candidate import canonical_json
    return canonical_json({"protocol": _ORDINARY_PROTOCOL, "candidate_id": candidate_id,
                           "request_id": request_id}) + b"\n"


def _parse_worker_header(raw: bytes) -> dict:
    try:
        header = json.loads(raw)
        if (set(header) != {"protocol", "candidate_id", "request_id"}
                or header["protocol"] != _ORDINARY_PROTOCOL
                or not isinstance(header["candidate_id"], str) or len(header["candidate_id"]) != 64
                or not isinstance(header["request_id"], str) or len(header["request_id"]) != 32):
            raise ValueError("invalid worker handshake")
        return header
    except (ValueError, TypeError) as exc:
        raise OSError("invalid worker handshake") from exc


def _collect_child(child: subprocess.Popen, request: bytes, timeout: float,
                   check_live: Callable[[], None] | None = None) -> tuple[bytes, bytes]:
    """Background wait; cancellation and success both finish process ownership."""
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if check_live is not None:
                check_live()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(child.args, timeout)
            try:
                return child.communicate(request if first else None, timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                first = False
    finally:
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    child.kill()
                    try:
                        child.wait(timeout=5.0)
                    except subprocess.TimeoutExpired as exc:
                        raise OSError("ordinary worker could not be reaped") from exc
        finally:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


def run_ordinary_child(command: tuple[str, ...], request: bytes, timeout: float,
                       *, check_live: Callable[[], None] | None = None) -> subprocess.CompletedProcess[bytes]:
    from frozen_candidate import running_candidate
    candidate = running_candidate()
    executable = str(candidate.executable)
    if command not in ((executable, "--localcat-tm-worker", "migration"),
                       (executable, "--localcat-tm-worker", "query")):
        raise OSError("worker command differs from the current candidate")
    if type(request) is not bytes:
        raise TypeError("ordinary worker request must be bytes")
    header = _worker_header(candidate.candidate_id, uuid4().hex)
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(("PYTHON", "PYINSTALLER", "_PYI", "QT_", "MIMALLOC_"))
                   and name.upper() != "__PYVENV_LAUNCHER__"}
    environment["PATH"] = os.path.join(os.environ["SYSTEMROOT"], "System32")
    # Popen supplies STARTUPINFOEX's explicit stdin/stdout/stderr handle list;
    # the child opens those OS handles even when Python's streams are None.
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             close_fds=True, cwd=candidate.executable.parent, env=environment)
    result, diagnostics = _collect_child(child, header + request, timeout, check_live)
    return _ordinary_child_response(command, child.returncode, result, diagnostics, header)


def _ordinary_child_response(command, returncode: int, result: bytes, diagnostics: bytes,
                             expected_header: bytes) -> subprocess.CompletedProcess[bytes]:
    """The production parent consumes only its child's exact reply association."""
    first_line, separator, payload = result.partition(b"\n")
    if not separator or _parse_worker_header(first_line) != _parse_worker_header(expected_header):
        raise OSError("worker candidate or request mismatch")
    return subprocess.CompletedProcess(command, returncode, payload, diagnostics)


def run_frozen_child(command: tuple[str, ...], request: bytes, timeout: float) -> subprocess.CompletedProcess[bytes]:
    from tm_gate_inputs import _require_native_frozen_producer

    try:
        bootstrap = sys.modules["frozen_source_bootstrap"]
        authority: Any = getattr(bootstrap, "_producer")
        _require_native_frozen_producer(authority)
        authority.require_worker_wait_allowed()
        executable = getattr(bootstrap, "_executable")
        if command not in ((executable, "--localcat-tm-worker", "migration"),
                           (executable, "--localcat-tm-worker", "query")):
            raise ValueError("worker command is not this native-bound executable")
        authority._invoke(lambda: getattr(bootstrap, "_raw").module_reproof("bootstrap"))
    except (KeyError, AttributeError, RuntimeError, ValueError, TypeError) as error:
        raise OSError("frozen worker requires the platform E10 transport") from error
    if type(request) is not bytes:
        raise TypeError("frozen worker request must be exact bytes")
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(("PYTHON", "PYINSTALLER", "_PYINSTALLER", "QT_", "MIMALLOC_"))
                   and name.upper() != "__PYVENV_LAUNCHER__"}
    system_root = next(value for name, value in environment.items() if name.upper() == "SYSTEMROOT")
    environment["PATH"] = os.path.join(system_root, "System32")
    # CPython's Windows Popen constructs a STARTUPINFOEX handle-list containing
    # only its child stdin/stdout/stderr ends when close_fds=True. The native
    # retained handles were created non-inheritable and are never handed down.
    return subprocess.run(command, input=request, stdin=None, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, close_fds=True, check=False,
                          cwd=os.path.dirname(executable), env=environment, timeout=timeout)
