"""Host-side consumption of the platform's future trusted frozen producer.

No object in this module grants production provenance. Core admission is the
only constructor gate; its currently unavailable producer stays fail-closed.
The test constructor uses Core's permanently provisional test-frozen adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from importlib.machinery import ModuleSpec, SourceFileLoader
from pathlib import Path
from threading import Lock, RLock, local
from types import ModuleType
from typing import Any, Iterator, cast, final

import tm_gate_inputs as _inputs

_CONSTRUCTION_KEY = object()


class _OwnerWaitGuardLock:
    """A reentrant Host lock that never blocks the native owner on a worker.

    This changes waiting only. It cannot admit input or publish capability.
    The RLock hooks preserve Condition's recursion save/restore semantics.
    """

    def __init__(self, source: _FrozenCapabilitySource) -> None:
        self.__lock = RLock()
        self.__source = source

    def require_worker_wait_allowed(self) -> None:
        self.__source.require_worker_wait_allowed()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking:
            return self.__lock.acquire(False, timeout)
        if self.__lock.acquire(False):
            return True
        self.require_worker_wait_allowed()
        return self.__lock.acquire(True, timeout)

    def release(self) -> None:
        self.__lock.release()

    def locked(self) -> bool:
        return self.__lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()

    def _is_owned(self) -> bool:
        return cast(bool, cast(Any, self.__lock)._is_owned())

    def _release_save(self) -> object:
        return cast(Any, self.__lock)._release_save()

    def _acquire_restore(self, state: object) -> None:
        self.require_worker_wait_allowed()
        cast(Any, self.__lock)._acquire_restore(state)


@final
@dataclass(frozen=True, slots=True)
class _FrozenSourceFile:
    owner: _FrozenCapabilitySource
    relative_id: str
    content: bytes
    path: Path = field(init=False)
    content_sha256: bytes = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.owner.root_path.joinpath(*self.relative_id.split("/")))
        object.__setattr__(self, "content_sha256", hashlib.sha256(self.content).digest())

    def is_current(self) -> bool:
        try:
            return self.owner._read(self.relative_id) == self.content
        except (OSError, RuntimeError, KeyError):
            return False

    def matches_module(self, module: ModuleType) -> bool:
        return self.owner._matches_module(self, module)


@final
class _FrozenCapabilitySource:
    """Diagnostic locators plus exact bytes from the same Core input owner.

    Platform 7.2 owns the opaque high-level authority and the implementation of
    ``_reprove_executed_module(module, relative_id, content)``. That operation
    must reject unless retained execution, loader identity, origin and every
    co_filename still bind this module to these exact bytes. It returns no
    authority or caller-interpretable PASS flag. It is called only after Core
    admitted the same authority, never on arbitrary callback/metadata input.
    """

    def __init__(self, authority: object, *, _key: object, _test_entries: tuple[tuple[str, bytes], ...] | None = None, _test_root: Path | None = None, _test_scheduler: object | None = None) -> None:
        if _key is not _CONSTRUCTION_KEY:
            raise TypeError("frozen Host inputs require internal composition")
        self.__authority = authority
        self.__test_entries = _test_entries
        self.__local = local()
        self.__operation_lock = Lock()
        self.__owner_lock = Lock()
        self.__active_owner: _inputs._GateInputOwner | None = None
        self.__closed = False
        self.__scheduler: Any = _test_scheduler
        self.__files: dict[str, _FrozenSourceFile] = {}
        if _test_entries is None:
            with self.operation():
                owner = cast(_inputs._GateInputOwner, self.__local.owner)
                owner._require_production()
                root = cast(Any, authority).root_path
        else:
            root = _test_root
        if not isinstance(root, Path) or type(root) is not type(Path()) or not root.is_absolute():
            raise TypeError("trusted source diagnostic root must be an absolute Path")
        self.__root = root

    @property
    def root_path(self) -> Path:
        self._require_open()
        return self.__root

    def _require_open(self) -> None:
        if self.__closed:
            raise RuntimeError("frozen Host inputs were revoked")

    @contextmanager
    def operation(self) -> Iterator[None]:
        self._require_open()
        if getattr(self.__local, "owner", None) is not None:
            yield
            return
        # Never block the UI behind a worker whose native proof needs the UI.
        if not self.__operation_lock.acquire(blocking=False):
            raise RuntimeError("frozen Host input operation is already active")
        try:
            if self.__test_entries is not None:
                with _inputs._compose_test_frozen_input_owner(self.__test_entries) as owner:
                    with self._registered_owner(owner):
                        yield
            else:
                owner = _inputs._compose_frozen_input_owner(self.__authority)
                try:
                    with self._registered_owner(owner):
                        owner._require_production()
                        yield
                finally:
                    owner.close()
        finally:
            self.__operation_lock.release()

    @contextmanager
    def _registered_owner(self, owner: _inputs._GateInputOwner) -> Iterator[None]:
        # Registration and close cannot pass one another. This lock protects
        # only the owner reference; proof/native work is always outside it.
        with self.__owner_lock:
            self._require_open()
            self.__active_owner = owner
        self.__local.owner = owner
        try:
            yield
        finally:
            self.__local.owner = None
            with self.__owner_lock:
                self.__active_owner = None

    @contextmanager
    def input_session(self) -> Iterator[_inputs._GateInputSession]:
        with self.operation():
            owner = cast(_inputs._GateInputOwner, self.__local.owner)
            with owner.open_session() as session:
                self.__local.session = session
                try:
                    self._require_open()
                    yield session
                    if not session._publication_completed():
                        self._require_open()
                except BaseException:
                    session._abort()
                    raise
                finally:
                    self.__local.session = None

    @contextmanager
    def proof_window(self) -> Iterator[None]:
        with self.input_session():
            yield

    def reprove(self) -> None:
        self._require_open()
        session = getattr(self.__local, "session", None)
        if session is not None:
            session._require_live()
        else:
            with self.input_session():
                pass

    def _read(self, relative: str) -> bytes:
        self._require_open()
        session = getattr(self.__local, "session", None)
        if session is not None:
            return cast(_inputs._GateInputSession, session).read_bytes(relative)
        with self.input_session() as current:
            return current.read_bytes(relative)

    def bind_path(self, locator: Path) -> _FrozenSourceFile:
        if type(locator) is not type(Path()) or not locator.is_absolute():
            raise TypeError("frozen source locator must be an absolute Path")
        relative = _inputs._relative_input_id(locator.relative_to(self.root_path).as_posix())
        content = self._read(relative)
        previous = self.__files.get(relative)
        if previous is not None:
            if content != previous.content:
                raise RuntimeError("frozen retained source bytes changed")
            return previous
        result = _FrozenSourceFile(self, relative, content)
        self.__files[relative] = result
        return result

    def _matches_module(self, source: _FrozenSourceFile, module: ModuleType) -> bool:
        self._require_open()
        if source.owner is not self or not source.is_current():
            return False
        spec = module.__spec__
        loader = module.__loader__
        if type(spec) is not ModuleSpec or loader is None or spec.loader is not loader:
            return False
        if str(source.path) != module.__file__ or spec.origin != module.__file__:
            return False
        if self.__test_entries is not None:
            # Only provisional tests can use checkout loaders. The actual Gate
            # production check still rejects this owner before publication.
            return (
                type(loader) is SourceFileLoader
                and loader.name == module.__name__
                and loader.path == module.__file__
            )
        if type(loader) is SourceFileLoader:
            return False
        with self.operation():
            cast(_inputs._GateInputOwner, self.__local.owner)._require_production()
            cast(Any, self.__authority)._reprove_executed_module(module, source.relative_id, source.content)
        return True

    def require_worker_wait_allowed(self) -> None:
        try:
            if self.__scheduler is not None:
                self.__scheduler.require_worker_wait_allowed()
            if self.__test_entries is None and not self.__closed:
                cast(Any, self.__authority).require_worker_wait_allowed()
        except RuntimeError:
            self.close()
            raise

    def install_owner_scheduler(self, scheduler: object) -> None:
        """Inject post-TCB scheduling into the admitted platform producer.

        The structural port is window/call/cancel/close/wait-guard. Platform
        owns the callbacks and all native operations; it never imports Qt.
        """
        self._require_open()
        if self.__scheduler is not None:
            raise RuntimeError("owner scheduler was already installed")
        if self.__test_entries is not None:
            raise TypeError("provisional Host inputs cannot install a producer")
        with self.operation():
            cast(_inputs._GateInputOwner, self.__local.owner)._require_production()
            cast(Any, self.__authority)._install_owner_scheduler(scheduler)
        self.__scheduler = scheduler

    def close(self) -> None:
        with self.__owner_lock:
            if self.__closed:
                return
            self.__closed = True
            if self.__active_owner is not None:
                self.__active_owner._revoke_publication()
        # The platform high-level authority owns cancellation, waiter wakeup
        # and native close on its original thread; no native object is exposed.
        try:
            if self.__scheduler is not None:
                self.__scheduler.close()
        finally:
            if self.__test_entries is None:
                cast(Any, self.__authority).close()


def _compose_frozen_capability_source(authority: object) -> _FrozenCapabilitySource:
    return _FrozenCapabilitySource(authority, _key=_CONSTRUCTION_KEY)


def _test_frozen_capability_source(entries: tuple[tuple[str, bytes], ...], root: Path, *, _scheduler: object | None = None) -> _FrozenCapabilitySource:
    return _FrozenCapabilitySource(None, _key=_CONSTRUCTION_KEY, _test_entries=entries, _test_root=root, _test_scheduler=_scheduler)


class _OrdinaryCapabilityInputs:
    """Host operation scope for the ordinary Core adapter, without source files.

    One operation retains its owner across run/persist/publish windows. Close
    revokes that owner; the background transport observes revocation and reaps
    its child. No native scheduler or UI-thread join is needed.
    """

    def __init__(self) -> None:
        from frozen_candidate import running_candidate
        self.candidate = running_candidate()
        self.root_path = self.candidate.bundle_root
        self.__local = local()
        self.__operation_lock = Lock()
        self.__owner_lock = Lock()
        self.__owner = None
        self.__closed = False

    def _require_open(self) -> None:
        if self.__closed:
            raise RuntimeError("ordinary Host inputs were revoked")

    @contextmanager
    def operation(self):
        self._require_open()
        if getattr(self.__local, "owner", None) is not None:
            yield
            return
        if not self.__operation_lock.acquire(blocking=False):
            raise RuntimeError("ordinary Host input operation is already active")
        owner = None
        try:
            owner = _inputs._compose_ordinary_input_owner()
            with self.__owner_lock:
                self._require_open()
                self.__owner = owner
            self.__local.owner = owner
            yield
        finally:
            self.__local.owner = None
            with self.__owner_lock:
                self.__owner = None
            if owner is not None:
                owner.close()
            self.__operation_lock.release()

    @contextmanager
    def input_session(self):
        with self.operation():
            with self.__local.owner.open_session() as session:
                self.__local.session = session
                try:
                    self._require_open()
                    yield session
                    if not session._publication_completed():
                        self._require_open()
                except BaseException:
                    session._abort()
                    raise
                finally:
                    self.__local.session = None

    @contextmanager
    def proof_window(self):
        # Host binding work shares the operation, not an overlapping session.
        with self.operation():
            self.reprove()
            yield
            self.reprove()

    def reprove(self) -> None:
        self._require_open()
        owner = getattr(self.__local, "owner", None)
        if owner is not None:
            owner._require_live()

    def read_data(self, relative: str) -> bytes:
        self._require_open()
        session = getattr(self.__local, "session", None)
        if session is not None:
            return session.read_bytes(relative)
        with self.input_session() as current:
            return current.read_bytes(relative)

    def check_locator(self, path: Path) -> None:
        self.reprove()
        relative = path.relative_to(self.root_path).as_posix()
        if relative == ".":
            return
        if relative.endswith(".py"):
            import importlib
            self.candidate.require_module(importlib.import_module(relative[:-3].replace("/", ".")))
        else:
            self.read_data(relative)

    def require_worker_wait_allowed(self) -> None:
        from threading import get_ident, main_thread
        if get_ident() == main_thread().ident:
            raise RuntimeError("ordinary worker wait cannot block the UI thread")

    def close(self) -> None:
        with self.__owner_lock:
            self.__closed = True
            if self.__owner is not None:
                self.__owner._revoke_publication()
