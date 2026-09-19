"""Core consumption of fresh benchmark worker transports.

Mode/EXE/pipe values select transport, never input or publication authority.
The frozen production composition is intentionally fail-closed until the
platform owner supplies its E10 transport and exact authority integration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import cast

from tm_gate_inputs import (
    _GateInputSession,
    _compose_source_input_owner,
    _require_production_session,
    _require_session,
    _source_input_locator,
)

_WORKER_MODULES = {
    "migration": "tm_benchmark_process",
    "query": "tm_benchmark_query_process",
}


def _worker_command(kind: str) -> tuple[str, ...]:
    if type(kind) is not str or kind not in _WORKER_MODULES:
        raise ValueError("unknown benchmark worker mode")
    if getattr(sys, "frozen", False):
        return (sys.executable, "--localcat-tm-worker", kind)
    return (sys.executable, "-m", _WORKER_MODULES[kind], "--worker")


type _TestFrozenTransport = Callable[[tuple[str, ...], bytes, float], subprocess.CompletedProcess[bytes]]


def _run_worker_child(
    kind: str,
    request_json: str,
    *,
    timeout_seconds: float,
    test_mode: bool,
    _test_frozen_transport: _TestFrozenTransport | None = None,
) -> subprocess.CompletedProcess[str]:
    """Consume a platform transport without supplying child authorization.

The future platform production branch belongs here, alongside source launch;
it must create directed pipes and independently bootstrap the same candidate.
The explicit test transport cannot produce non-test benchmark evidence.
"""
    command = _worker_command(kind)
    if type(request_json) is not str or type(test_mode) is not bool:
        raise TypeError("invalid worker transport request")
    if type(timeout_seconds) is not float or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("invalid worker timeout")
    if _test_frozen_transport is not None and not test_mode:
        raise TypeError("test transport cannot produce final evidence")
    if getattr(sys, "frozen", False):
        if _test_frozen_transport is None:
            from frozen_worker_transport import run_frozen_child
            completed = run_frozen_child(command, request_json.encode("utf-8"), timeout_seconds)
        else:
            completed = _test_frozen_transport(command, request_json.encode("utf-8"), timeout_seconds)
        if type(completed) is not subprocess.CompletedProcess or type(completed.returncode) is not int:
            raise OSError("invalid frozen transport result")
        if type(completed.stdout) is not bytes or type(completed.stderr) is not bytes:
            raise OSError("frozen transport requires binary result pipes")
        try:
            result = completed.stdout.decode("utf-8")
            diagnostics = completed.stderr.decode("utf-8")
            # Windowed mode has one result pipe. Core alone interprets its
            # success/error grammar using the actual child exit status.
            return subprocess.CompletedProcess(command, completed.returncode, result if completed.returncode == 0 else "", diagnostics if completed.returncode == 0 else result + diagnostics)
        except UnicodeDecodeError as error:
            raise OSError("invalid frozen transport encoding") from error
    if _test_frozen_transport is not None:
        raise TypeError("frozen test transport cannot replace source launch")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONWARNINGS"] = "ignore"
    return subprocess.run(
        list(command), input=request_json, capture_output=True, text=True,
        encoding="utf-8", cwd=Path(__file__).resolve().parent,
        env=environment, timeout=timeout_seconds, check=False,
    )


@dataclass(frozen=True)
class _WorkerPipes:
    """Already-attached binary endpoints; native handle ownership stays platform-side."""

    request: io.BufferedIOBase | io.RawIOBase
    result: io.BufferedIOBase | io.RawIOBase
    errors: io.BufferedIOBase | io.RawIOBase | None = None

    def __post_init__(self) -> None:
        streams = (self.request, self.result) if self.errors is None else (self.request, self.result, self.errors)
        if any(not isinstance(stream, (io.BufferedIOBase, io.RawIOBase)) for stream in streams):
            raise TypeError("worker endpoints must be binary streams")
        if len({id(stream) for stream in streams}) != len(streams) or any(stream.closed for stream in streams):
            raise ValueError("worker endpoints must be distinct and open")
        if not self.request.readable() or not self.result.writable() or (self.errors is not None and not self.errors.writable()):
            raise ValueError("worker endpoint direction is invalid")

    def read_request(self) -> bytes:
        payload = self.request.read()
        if type(payload) is not bytes:
            raise OSError("worker request pipe returned invalid bytes")
        return payload

    def write_result(self, payload: str) -> None:
        self._write(self.result, payload)

    def write_error(self, payload: str) -> None:
        self._write(self.result if self.errors is None else self.errors, payload)

    @staticmethod
    def _write(stream: io.BufferedIOBase | io.RawIOBase, payload: str) -> None:
        data = payload.encode("utf-8")
        offset = 0
        while offset < len(data):
            count = stream.write(data[offset:])
            if type(count) is not int or count <= 0 or count > len(data) - offset:
                raise OSError("worker pipe write was truncated")
            offset += count
        stream.flush()


def _source_worker_pipes() -> _WorkerPipes:
    """Only the source CLI adapts console streams to the shared worker core."""
    if getattr(sys, "frozen", False):
        raise RuntimeError("frozen worker endpoints require platform E10 attachment")
    return _WorkerPipes(
        cast(io.BufferedIOBase, sys.stdin.buffer),
        cast(io.BufferedIOBase, sys.stdout.buffer),
        cast(io.BufferedIOBase, sys.stderr.buffer),
    )


@contextmanager
def _worker_input_window(
    session: _GateInputSession | None,
    *,
    test_mode: bool,
    contract_path: Path | None = None,
) -> Iterator[_GateInputSession]:
    """Borrow the caller window or compose one independent source epoch."""
    if session is not None:
        yield _require_session(session) if test_mode else _require_production_session(session)
        return
    if getattr(sys, "frozen", False):
        raise RuntimeError("frozen inputs require the trusted frozen producer")
    with _compose_source_input_owner(Path(__file__).resolve().parent) as owner:
        if contract_path is not None:
            owner._associate_source_path(_source_input_locator(contract_path))
        with owner.open_session() as owned_session:
            yield owned_session
