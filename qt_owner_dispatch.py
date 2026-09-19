"""Post-bootstrap Qt scheduling; this module never owns native authority."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock, get_ident
from typing import Callable, Iterator, TypeVar, cast

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal, Slot

_T = TypeVar("_T")


@dataclass(eq=False, slots=True)
class _Window:
    owner: object
    revoked: bool = False


@dataclass(eq=False, slots=True)
class _Request:
    window: _Window
    operation: Callable[[], object]
    done: bool = False
    result: object = None
    error: BaseException | None = None


class _OwnerThreadDispatcher(QObject):
    """Queue platform-owned operations without granting those operations trust.

    The producer owns proof windows and native handles. These tokens only scope
    scheduling and are never accepted by Core as input or publication authority.
    """

    requested = Signal(object)

    def __init__(self) -> None:
        application = QCoreApplication.instance()
        if application is None or QThread.currentThread() != application.thread():
            raise RuntimeError("owner dispatcher requires the Qt application thread")
        super().__init__()
        self._thread_id = get_ident()
        self._condition = Condition(RLock())
        self._window: _Window | None = None
        self._pending: list[_Request] = []
        self._executing = False
        self._closed = False
        self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)
        application.aboutToQuit.connect(self.close)
        # A bound QObject receiver is disconnected during destruction. Keep a
        # plain Python callback so deletion still wakes existing waiters.
        self.destroyed.connect(lambda: self.close())

    def _require_window(self, window: _Window) -> None:
        if type(window) is not _Window or window.owner is not self:
            raise RuntimeError("dispatch window is foreign")
        if self._closed or window.revoked or self._window is not window:
            raise RuntimeError("dispatch window is closed or revoked")

    def _revoke(self, window: _Window) -> None:
        window.revoked = True
        for request in self._pending:
            if request.window is window and not request.done:
                request.error = RuntimeError("dispatch window was revoked")
                request.done = True
        self._condition.notify_all()

    @contextmanager
    def window(self) -> Iterator[_Window]:
        with self._condition:
            if self._closed:
                raise RuntimeError("owner dispatcher is closed")
            if self._window is not None:
                self._revoke(self._window)
                raise RuntimeError("dispatch windows cannot be nested or concurrent")
            window = _Window(self)
            self._window = window
        try:
            yield window
        finally:
            with self._condition:
                self._revoke(window)
                if self._window is window:
                    self._window = None

    def cancel(self, window: _Window) -> None:
        with self._condition:
            if type(window) is not _Window or window.owner is not self:
                raise RuntimeError("dispatch window is foreign")
            self._revoke(window)

    @Slot()
    def close(self) -> None:
        # No join: the owner thread may be precisely what a worker awaits.
        with self._condition:
            self._closed = True
            if self._window is not None:
                self._revoke(self._window)
            self._condition.notify_all()

    def require_worker_wait_allowed(self) -> None:
        if get_ident() == self._thread_id:
            with self._condition:
                if self._window is not None:
                    self._revoke(self._window)
            raise RuntimeError("Qt owner cannot block waiting for its worker")

    def call(self, window: _Window, operation: Callable[[], _T]) -> _T:
        if not callable(operation):
            raise TypeError("dispatch operation must be callable")
        with self._condition:
            self._require_window(window)
            if get_ident() == self._thread_id and self._executing:
                self._revoke(window)
                raise RuntimeError("owner dispatch cannot reenter")
            request = _Request(window, operation)
            self._pending.append(request)
        try:
            if get_ident() == self._thread_id:
                self._execute(request)
            else:
                try:
                    self.requested.emit(request)
                except BaseException:
                    self.cancel(window)
                    raise
            with self._condition:
                self._condition.wait_for(lambda: request.done)
                if request.error is not None:
                    raise request.error
                self._require_window(window)
                return cast(_T, request.result)
        finally:
            with self._condition:
                if request in self._pending:
                    self._pending.remove(request)

    @Slot(object)
    def _execute(self, value: object) -> None:
        if type(value) is not _Request:
            return
        request = value
        with self._condition:
            if request.done:
                return
            try:
                self._require_window(request.window)
                if get_ident() != self._thread_id or self._executing:
                    raise RuntimeError("dispatch execution escaped its owner")
                self._executing = True
            except BaseException as error:
                request.error = error
                request.done = True
                self._revoke(request.window)
                return
        try:
            result = request.operation()
        except BaseException as error:
            with self._condition:
                request.error = error
                request.done = True
                self._revoke(request.window)
        else:
            with self._condition:
                if not request.done:
                    request.result = result
                    request.done = True
        finally:
            with self._condition:
                self._executing = False
                self._condition.notify_all()


def create_owner_thread_dispatcher() -> _OwnerThreadDispatcher:
    return _OwnerThreadDispatcher()
