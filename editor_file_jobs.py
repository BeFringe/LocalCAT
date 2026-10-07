"""Qt-free ownership for background project file operations.

A worker owns its result until the GUI takes it. Disposing a job before or
while it runs arranges cleanup on the worker, never closes a live save owner
concurrently, and requires no GUI event loop to release a late candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable

from parser_composition import new_cancellation_token


@dataclass
class FileOpenCandidate:
    save_service: object
    binding: object | None
    session: object | None = None
    runtime: object | None = None

    def close(self):
        if self.session is not None:
            self.session.close()


@dataclass(frozen=True)
class SingleTLImportReview:
    """Display-only facts; the Controller retains the unpublished owner."""
    source_path: Path
    default_name: str
    segment_count: int


@dataclass
class FileImportPublication:
    candidate: FileOpenCandidate
    result: object

    def close(self):
        self.candidate.close()


@dataclass(frozen=True)
class ControllerFileOutcome:
    kind: str
    path: Path
    accepted: bool
    result: object | None = None
    cancelled: bool = False
    safe_code: str | None = None


@dataclass
class FileExportPreparation:
    """Worker-owned Application candidate, consumed only by the Controller."""
    prepared: object
    runtime: object

    def close(self):
        self.prepared.close()


@dataclass(frozen=True)
class FileExportPublication:
    result: object
    runtime: object


class ControllerFileJob:
    """One issued operation; contains no Controller, Qt, or SQLite callbacks."""

    def __init__(self, kind: str, path: Path, context: tuple,
                 operation: Callable, *, session=None, service=None,
                 cancellation=None, cleanup=None, run_when_cancelled=False):
        self._identity = (kind, path, context, session, service)
        self.cancellation = cancellation if cancellation is not None else new_cancellation_token()
        self._cleanup_callback = cleanup
        self._run_when_cancelled = run_when_cancelled
        self._operation = operation
        self._lock = Lock()
        self._started = False
        self._operation_invoked = False
        self._done = False
        self._disposed = False
        self._taken = False
        self._value = None
        self._error = None

    @property
    def kind(self):
        return self._identity[0]

    @property
    def path(self):
        return self._identity[1]

    @property
    def context(self):
        return self._identity[2]

    @property
    def session(self):
        return self._identity[3]

    @property
    def service(self):
        return self._identity[4]

    @property
    def disposed(self):
        with self._lock:
            return self._disposed

    @property
    def done(self):
        with self._lock:
            return self._done

    @property
    def operation_invoked(self):
        with self._lock:
            return self._operation_invoked

    def cancel(self):
        self.cancellation.cancel()

    def run(self):
        with self._lock:
            if self._started:
                raise RuntimeError('PROJECT.FILE.ALREADY_STARTED')
            self._started = True
        value, error = None, None
        try:
            if self._run_when_cancelled or not self.cancellation.cancelled:
                with self._lock:
                    self._operation_invoked = True
                value = self._operation(self.cancellation)
        except Exception as caught:
            error = caught
        with self._lock:
            self._done = True
            if self._disposed:
                self._cleanup(value)
            else:
                self._value, self._error = value, error

    def fail_before_start(self, error: Exception) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError('PROJECT.FILE.ALREADY_STARTED')
            self._started = self._done = True
            self._error = error
            if self._cleanup_callback is not None:
                self._cleanup(None)

    def _cleanup(self, value):
        if self._cleanup_callback is not None:
            self._cleanup_callback(value)
            return
        if self.kind in {'open', 'prepare_tl', 'import_tl'} and value is not None:
            value.close()
        elif self.kind in {'save', 'import_tl'} and self.session is not None:
            self.session.close()

    def take_result(self):
        with self._lock:
            if not self._done or self._taken or self._disposed:
                raise RuntimeError('PROJECT.FILE.RESULT_UNAVAILABLE')
            self._taken = True
            value, error = self._value, self._error
            self._value = self._error = None
            return value, error

    def dispose(self):
        """Revoke delivery; cleanup never races the operation using its owner."""
        self.cancel()
        with self._lock:
            if self._disposed or self._taken:
                return
            self._disposed = True
            if self._done or not self._started:
                self._cleanup(self._value)
                self._value = self._error = None
