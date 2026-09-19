"""Closed exact-byte windows shared by Core validation consumers.

Only the source adapter currently has production admission. The explicit frozen
test adapter exercises the same consumer/lifetime code with test provenance;
neither its Python type nor its scripted bytes attest a native producer.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
from threading import get_ident
from types import MemberDescriptorType
from typing import Iterator, TypeVar, cast, final
from weakref import finalize

from platform_source_authority import (
    RootedSourceAuthority, RootedSourceFile, compose_rooted_source_authority,
)
from tm_retrieval_capability import (
    _InputPublicationLifetime, _InputPublicationWindow, _PreparedInputPublication,
    _ReferenceAssignment, _publication_reference_type,
)


_FACTORY_KEY = object()
_SOURCE_COMPOSITION_KEY = object()
_RETAINED_SOURCE_COMPOSITION_KEY = object()
_TEST_FROZEN_KEY = object()
_PATH_TYPE = type(Path())
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
})


def _relative_input_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("input id must be an exact string")
    if (
        not value or "\\" in value or ":" in value
        or any(ord(char) < 32 for char in value)
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or any(
            part in {"", ".", ".."} or part.endswith((".", " "))
            or part.split(".", 1)[0].upper() in _RESERVED_NAMES
            for part in value.split("/")
        )
    ):
        raise ValueError("input id must be canonical and bundle-relative")
    return value


@final
class _SourceInputAdapter:
    """Own source-only locators, retained files and source handle release."""

    __slots__ = ("__authority", "__external", "__authorities", "__release", "__owned", "__weakref__")  # pyright: ignore[reportUninitializedInstanceVariable]

    def __init__(self, root: Path | RootedSourceAuthority, *, _key: object) -> None:
        if _key is not _SOURCE_COMPOSITION_KEY and _key is not _RETAINED_SOURCE_COMPOSITION_KEY and _key is not _FACTORY_KEY:
            raise TypeError("source adapter requires internal composition")
        if getattr(sys, "frozen", False):
            raise RuntimeError("frozen inputs require the trusted frozen producer")
        self.__owned = _key is not _FACTORY_KEY
        if self.__owned:
            if type(root) is not _PATH_TYPE:
                raise TypeError("source composition requires an exact Path")
            self.__authority = compose_rooted_source_authority(cast(Path, root))
        else:
            if type(root) is not RootedSourceAuthority:
                raise TypeError("borrowed input requires exact source authority")
            self.__authority = root
        self.__external: dict[Path, RootedSourceAuthority] = {}
        self.__authorities = [self.__authority]
        # Retain only source authorities, never this adapter or common owner.
        # Native close must use its own original-thread rule.
        self.__release = finalize(self, _close_source_authorities, self.__authorities) if self.__owned else None

    def is_production(self) -> bool:
        return self.__owned

    def reprove(self) -> None:
        for authority in self.__authorities:
            authority.reprove()

    def associate_path(self, path: Path) -> None:
        if not self.__owned:
            raise TypeError("external source input requires internal composition")
        if type(path) is not _PATH_TYPE or not path.is_absolute():
            raise TypeError("external source input must be an exact absolute Path")
        try:
            path.relative_to(self.__authority.root_path)
        except ValueError:
            if path not in self.__external:
                authority = next((current for current in self.__external.values() if current.root_path == path.parent), None)
                if authority is None:
                    authority = compose_rooted_source_authority(path.parent)
                    self.__authorities.append(authority)
                self.__external[path] = authority

    @contextmanager
    def proof_window(self) -> Iterator[None]:
        with ExitStack() as stack:
            for authority in self.__authorities:
                stack.enter_context(authority.proof_window())
            # Keep external contracts bound in later fingerprint-only windows.
            for path, authority in self.__external.items():
                self.read_bytes(path.name, authority)
            yield

    def locate(self, locator: Path) -> tuple[str, object | None]:
        if type(locator) is not _PATH_TYPE or not locator.is_absolute():
            raise TypeError("input locator must be an exact absolute Path")
        try:
            return (_relative_input_id(locator.relative_to(self.__authority.root_path).as_posix()), None)
        except ValueError:
            if locator in self.__external:
                return (_relative_input_id(locator.name), self.__external[locator])
            raise ValueError("input locator is outside this source composition") from None

    def require_root_locator(self, locator: Path) -> None:
        if _source_input_locator(locator) != self.__authority.root_path:
            raise ValueError("repository locator does not match the input session")

    def read_bytes(self, relative: str, domain: object | None) -> bytes:
        authority = self.__authority
        if domain is not None:
            if type(domain) is not RootedSourceAuthority or not any(domain is item for item in self.__external.values()):
                raise ValueError("input domain is foreign to this source adapter")
            authority = domain
        locator = authority.root_path.joinpath(*relative.split("/"))
        if domain is not None and self.__external.get(locator) is not authority:
            raise ValueError("external input is not explicitly associated")
        source = authority.bind_path(locator)
        if type(source) is not RootedSourceFile or not source._is_owned_by(authority):
            raise TypeError("source read is not bound to this exact authority")
        if source.path != locator:
            raise ValueError("source read does not match the requested relative id")
        return source.content

    def close(self) -> None:
        if self.__release is not None:
            self.__release()


@final
class _TestFrozenInputAdapter:
    """Scripted retained bytes for provisional consumer tests, never admission."""

    __slots__ = ("__entries", "__terminals", "__window", "__closed")

    def __init__(self, entries: tuple[tuple[str, bytes], ...], terminal_results: tuple[bool, ...], *, _key: object) -> None:
        if _key is not _TEST_FROZEN_KEY or type(entries) is not tuple or type(terminal_results) is not tuple:
            raise TypeError("test frozen inputs require exact scripted tuples")
        snapshot: dict[str, bytes] = {}
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not bytes:
                raise TypeError("test frozen entries require an exact id/bytes pair")
            relative = _relative_input_id(entry[0])
            if relative in snapshot:
                raise ValueError("duplicate test frozen input id")
            snapshot[relative] = entry[1]
        if any(type(result) is not bool for result in terminal_results):
            raise TypeError("test frozen terminal results must be exact booleans")
        self.__entries = snapshot
        self.__terminals = terminal_results
        self.__window = 0
        self.__closed = False

    def reprove(self) -> None:
        if self.__closed:
            raise RuntimeError("test frozen adapter is revoked")

    @contextmanager
    def proof_window(self) -> Iterator[None]:
        self.reprove()
        index = self.__window
        self.__window += 1
        try:
            yield
        finally:
            self.reprove()
            if index < len(self.__terminals) and not self.__terminals[index]:
                raise RuntimeError("test frozen terminal reproof failed")

    def read_bytes(self, relative: str, domain: object | None) -> bytes:
        self.reprove()
        if domain is not None:
            raise ValueError("test frozen inputs cannot use source domains")
        return self.__entries[relative]

    def close(self) -> None:
        self.__closed = True


@final
class _GateInputOwner(metaclass=_publication_reference_type):
    """One closed adapter/epoch, with strictly sequential consumer windows."""

    __slots__ = ("__adapter", "__thread", "__native_closed", "__publication_lifetime", "__retained", "__epoch", "__window", "__window_started", "__weakref__")  # pyright: ignore[reportUninitializedInstanceVariable]

    def __init__(self, adapter: _SourceInputAdapter | _TestFrozenInputAdapter, *, _key: object, retained: bool = False) -> None:
        if _key is not _FACTORY_KEY or type(adapter) not in (_SourceInputAdapter, _TestFrozenInputAdapter):
            raise TypeError("input owner requires a closed Core composition adapter")
        self.__adapter = adapter
        self.__thread = get_ident()
        self.__native_closed = False
        # Innermost, memory-only lock. Never hold it across adapter work.
        self.__publication_lifetime = _InputPublicationLifetime()
        self.__retained = retained
        self.__epoch = object()
        self.__window: object | None = None
        self.__window_started = False

    def _require_live(self) -> None:
        if self.__publication_lifetime.is_revoked() or self.__native_closed or get_ident() != self.__thread:
            raise RuntimeError("input owner is closed or belongs to another thread")
        self.__adapter.reprove()

    def _require_epoch(self, epoch: object) -> None:
        self._require_live()
        if epoch is not self.__epoch:
            raise RuntimeError("input owner epoch has changed")

    def _require_production(self) -> None:
        self._require_live()
        if type(self.__adapter) is not _SourceInputAdapter or not self.__adapter.is_production():
            raise TypeError("production inputs require internal authority composition")

    @property
    def provenance(self) -> str:
        self._require_live()
        if type(self.__adapter) is _TestFrozenInputAdapter:
            return "test-frozen"
        return "source-production" if self.__adapter.is_production() else "source-borrowed"

    def _is_retained(self) -> bool:
        self._require_live()
        return self.__retained

    def _associate_source_path(self, path: Path) -> None:
        self._require_live()
        if self.__window is not None or type(self.__adapter) is not _SourceInputAdapter:
            raise TypeError("source domains must be composed before the input window")
        self.__adapter.associate_path(path)

    def _locate(self, locator: Path) -> tuple[str, object | None]:
        self._require_live()
        if type(self.__adapter) is not _SourceInputAdapter:
            raise TypeError("frozen inputs require relative ids, not source locators")
        return self.__adapter.locate(locator)

    def _require_root_locator(self, locator: Path) -> None:
        self._require_live()
        if type(self.__adapter) is not _SourceInputAdapter:
            raise TypeError("frozen inputs have no source repository locator")
        self.__adapter.require_root_locator(locator)

    def _read(self, relative: str, domain: object | None) -> bytes:
        self._require_live()
        return self.__adapter.read_bytes(relative, domain)

    def _start_window(self, stack: ExitStack) -> tuple[object, object]:
        self._require_live()
        if self.__window is None or self.__window_started:
            raise RuntimeError("input window must be opened once by its owner")
        self.__window_started = True
        stack.enter_context(self.__adapter.proof_window())
        return self.__epoch, self.__window

    def _revoke_publication(self) -> None:
        """Any scheduler thread may revoke; native close stays owner-thread-only.

        Release this lock before waking schedulers or closing native handles.
        No Host/publisher/worker lock or thread-affine proof is acquired here.
        """
        self.__publication_lifetime.revoke()

    def _close_native(self) -> None:
        if get_ident() != self.__thread:
            raise RuntimeError("input owner belongs to another thread")
        if not self.__native_closed:
            self.__native_closed = True
            try:
                self.__adapter.close()
            except BaseException:
                self._revoke_publication()
                raise

    def _prepare_publication(self, session: _GateInputSession, *, close_owner: bool) -> None:
        if close_owner:
            # Only the public retained-owner terminal consumes the entire owner.
            # Shared explicit sessions retain their owner for future windows.
            if not self.__retained:
                raise ValueError("shared input owner cannot be consumed by publication")
            self._close_native()
        session._publication_window().prepare()

    def _publication_window(self, guards: object) -> _InputPublicationWindow:
        return self.__publication_lifetime.window(guards)

    @contextmanager
    def open_session(self) -> Iterator[_GateInputSession]:
        self._require_live()
        if self.__window is not None:
            raise RuntimeError("input owner already has an active window")
        self.__window = object()
        session: _GateInputSession | None = None
        try:
            session = _GateInputSession(self, _key=_FACTORY_KEY)
            yield session
        except BaseException:
            if session is not None:
                session._abort()
            raise
        finally:
            try:
                if session is not None:
                    session._finish()
            finally:
                self.__window = None
                self.__window_started = False

    def close(self) -> None:
        if get_ident() != self.__thread:
            raise RuntimeError("input owner belongs to another thread")
        self._revoke_publication()
        self._close_native()


@final
class _GateInputReference:
    __slots__ = ("__session", "__relative", "__domain")

    def __init__(self, session: _GateInputSession, relative: str, *, _key: object, _domain: object | None = None) -> None:
        if _key is not _FACTORY_KEY:
            raise TypeError("input references require a live Core session")
        _require_session(session)
        self.__session = session
        self.__relative = _relative_input_id(relative)
        self.__domain = _domain

    def _read(self) -> bytes:
        return self.__session._read(self.__relative, self.__domain)

    def _belongs_to(self, session: _GateInputSession) -> bool:
        self.__session._require_live()
        return self.__session is session

    def _matches_id(self, relative: str) -> bool:
        return self.__domain is None and self.__relative == relative

    def _same_input(self, other: _GateInputReference) -> bool:
        return self.__session is other.__session and self.__relative == other.__relative and self.__domain is other.__domain


@final
class _GateInputSession(metaclass=_publication_reference_type):
    __slots__ = ("__owner", "__epoch", "__window_identity", "__thread", "__active", "__window", "__ids", "__completed", "__aborted", "__publication")

    def __init__(self, owner: _GateInputOwner, *, _key: object) -> None:
        if _key is not _FACTORY_KEY or type(owner) is not _GateInputOwner:
            raise TypeError("input session requires a closed Core owner")
        self.__owner = owner
        self.__thread = get_ident()
        self.__active = False
        self.__completed = False
        self.__aborted = False
        self.__ids: set[str] = set()
        self.__window = ExitStack()
        try:
            self.__epoch, self.__window_identity = owner._start_window(self.__window)
            # Capture only original builtin slots. At commit these exact live
            # fields are checked without calling native reproof. Construction
            # failure must exit the already-entered proof window as well.
            self.__publication = owner._publication_window(tuple(
                (cast(MemberDescriptorType, vars(kind)[name]), target, expected, expected)
                for kind, name, target, expected in (
                    (_GateInputOwner, "_GateInputOwner__epoch", owner, self.__epoch),
                    (_GateInputOwner, "_GateInputOwner__window", owner, self.__window_identity),
                    (_GateInputSession, "_GateInputSession__owner", self, owner),
                    (_GateInputSession, "_GateInputSession__epoch", self, self.__epoch),
                    (_GateInputSession, "_GateInputSession__completed", self, True),
                    (_GateInputSession, "_GateInputSession__active", self, False),
                    (_GateInputSession, "_GateInputSession__aborted", self, False),
                )
            ))
        except BaseException:
            self.__window.close()
            raise
        self.__active = True

    def _require_live(self) -> None:
        if get_ident() != self.__thread or not self.__active or self.__aborted or self.__publication.failed():
            raise RuntimeError("input session is closed, revoked or belongs to another thread")
        self.__owner._require_epoch(self.__epoch)

    def _production_owner(self) -> _GateInputOwner:
        self._require_live()
        self.__owner._require_production()
        return self.__owner

    def _completed_owner(self) -> _GateInputOwner:
        if not self.__completed or self.__aborted or self.__publication.failed():
            raise RuntimeError("input receipt requires a successfully completed window")
        self.__owner._require_epoch(self.__epoch)
        self.__owner._require_production()
        return self.__owner

    def _require_same_epoch(self, completed: _GateInputSession) -> None:
        if type(completed) is not _GateInputSession or self._production_owner() is not completed._completed_owner() or self.__epoch is not completed.__epoch:
            raise ValueError("input windows do not belong to the same production epoch")

    @property
    def provenance(self) -> str:
        self._require_live()
        return self.__owner.provenance

    @property
    def consumed_ids(self) -> tuple[str, ...]:
        self._require_live()
        return tuple(sorted(self.__ids))

    def input(self, relative_id: str) -> _GateInputReference:
        self._require_live()
        return _GateInputReference(self, relative_id, _key=_FACTORY_KEY)

    def input_path(self, locator: Path) -> _GateInputReference:
        """Translate a source diagnostic locator without filesystem reads."""
        self._require_live()
        relative, domain = self.__owner._locate(locator)
        return _GateInputReference(self, relative, _key=_FACTORY_KEY, _domain=domain)

    def _resolve_input(self, locator: Path | None, reference: _GateInputReference | None, default_id: str) -> _GateInputReference:
        """Bind optional source diagnostics to this window's exact input."""
        self._require_live()
        if reference is not None:
            self._require_bound_input(reference)
            if locator is not None and not reference._same_input(self.input_path(_source_input_locator(locator))):
                raise ValueError("input locator conflicts with the session reference")
            return reference
        if locator is not None:
            return self.input_path(_source_input_locator(locator))
        return self.input(default_id)

    def _require_validation_locators(self, root: Path | None, approved: Path | None, relative_id: str) -> None:
        self._require_live()
        relative = _relative_input_id(relative_id)
        if root is not None:
            self.__owner._require_root_locator(root)
        if approved is not None and not self.input_path(_source_input_locator(approved))._matches_id(relative):
            raise ValueError("approved roots locator conflicts with the session input id")

    def read_bytes(self, relative_id: str) -> bytes:
        return self._read(relative_id, None)

    def _read(self, relative_id: str, domain: object | None) -> bytes:
        self._require_live()
        relative = _relative_input_id(relative_id)
        content = self.__owner._read(relative, domain)
        if type(content) is not bytes:
            raise TypeError("input adapter did not return exact bytes")
        self.__ids.add(relative)
        return content

    def terminal_reproof(self) -> None:
        if get_ident() != self.__thread or not self.__active:
            raise RuntimeError("input session is closed or belongs to another thread")
        try:
            try:
                self.__owner._require_epoch(self.__epoch)
            finally:
                self.__window.__exit__(None, None, None)
            self.__owner._require_epoch(self.__epoch)
            self.__completed = not self.__aborted
        except Exception as error:
            self.__completed = False
            raise RuntimeError("Core input terminal reproof failed") from error
        finally:
            self.__active = False

    def _finish(self) -> None:
        if self.__active:
            self.terminal_reproof()

    def _abort(self) -> None:
        if self.__publication.completed():
            return
        self.__aborted = True
        self.__completed = False

    def _prepare_publication(self, *, close_owner: bool = False) -> None:
        """Finish every fallible window operation before the memory boundary."""
        if get_ident() != self.__thread:
            raise RuntimeError("input publication belongs to another thread")
        if type(close_owner) is not bool:
            raise TypeError("publication owner disposition must be an exact bool")
        if self.__active:
            self.terminal_reproof()
        self.__owner._prepare_publication(self, close_owner=close_owner)

    def _publication_window(self) -> _InputPublicationWindow:
        return self.__publication

    def _publication_completed(self) -> bool:
        """Completion fact only; never grants input/provenance authority."""
        return self.__publication.completed()

    def _prepare_reference_publication(self, assignments: object, result: _PublicationT) -> _PreparedInputPublication[_PublicationT]:
        self._require_live()
        return self.__publication.plan(assignments, result)

    def _commit_publication(self, plan: _PreparedInputPublication[_PublicationT], additional: tuple[_ReferenceAssignment, ...] = ()) -> _PublicationT:
        if get_ident() != self.__thread:
            raise RuntimeError("input publication belongs to another thread")
        return self.__publication.commit(plan, additional)

    def _require_bound_input(self, reference: _InputFile) -> None:
        self._require_live()
        if type(reference) is not _GateInputReference:
            raise TypeError("validation roots require an exact input reference")
        if not reference._belongs_to(self):
            raise ValueError("validation roots belong to a foreign input session")


_PublicationT = TypeVar("_PublicationT")


def _require_session(value: object) -> _GateInputSession:
    if type(value) is not _GateInputSession:
        raise TypeError("Core validation requires an exact input session")
    value._require_live()
    return value


def _require_production_session(value: object) -> _GateInputSession:
    session = _require_session(value)
    session._production_owner()
    return session


@contextmanager
def _open_gate_input_session(authority: object) -> Iterator[_GateInputSession]:
    """Borrow exact source authority with permanently provisional provenance."""
    if type(authority) is not RootedSourceAuthority:
        raise TypeError("Core inputs require an exact source authority")
    adapter = _SourceInputAdapter(authority, _key=_FACTORY_KEY)
    owner = _GateInputOwner(adapter, _key=_FACTORY_KEY)
    try:
        with owner.open_session() as session:
            yield session
    finally:
        owner.close()


def _close_source_authorities(authorities: list[RootedSourceAuthority]) -> None:
    error: BaseException | None = None
    for authority in reversed(authorities):
        try:
            authority.close()
        except BaseException as caught:
            error = error or caught
    if error is not None:
        raise error


def _retain_source_input_owner(root: Path) -> _GateInputOwner:
    return _GateInputOwner(_SourceInputAdapter(root, _key=_RETAINED_SOURCE_COMPOSITION_KEY), _key=_FACTORY_KEY, retained=True)


@contextmanager
def _compose_source_input_owner(root: Path) -> Iterator[_GateInputOwner]:
    """Source provenance does not replace CapabilityHost loader/code anchors."""
    owner = _GateInputOwner(_SourceInputAdapter(root, _key=_SOURCE_COMPOSITION_KEY), _key=_FACTORY_KEY)
    try:
        yield owner
    finally:
        owner.close()


def _compose_frozen_input_owner(authority: object) -> _GateInputOwner:
    """Unique production admission boundary awaiting real platform 7.2 proof.

    Do not admit by frozen boolean, Python private key/type, callback or digest.
    Native/loader provenance and original-thread scheduling belong to 7.2.
    """
    raise RuntimeError("production frozen input admission is unavailable")


@contextmanager
def _compose_test_frozen_input_owner(entries: tuple[tuple[str, bytes], ...], *, terminal_results: tuple[bool, ...] = ()) -> Iterator[_GateInputOwner]:
    adapter = _TestFrozenInputAdapter(entries, terminal_results, _key=_TEST_FROZEN_KEY)
    owner = _GateInputOwner(adapter, _key=_FACTORY_KEY)
    try:
        yield owner
    finally:
        owner.close()


@contextmanager
def _open_test_frozen_input_session(entries: tuple[tuple[str, bytes], ...], *, terminal_results: tuple[bool, ...] = ()) -> Iterator[_GateInputSession]:
    with _compose_test_frozen_input_owner(entries, terminal_results=terminal_results) as owner:
        with owner.open_session() as session:
            yield session


@contextmanager
def _open_source_input_session(root: Path) -> Iterator[_GateInputSession]:
    with _compose_source_input_owner(root) as owner:
        with owner.open_session() as session:
            yield session


type _InputRoot = Path | _GateInputSession
type _InputFile = Path | _GateInputReference


def _source_input_locator(value: Path) -> Path:
    """Normalize a public source locator lexically, without resolving links."""
    if type(value) is not _PATH_TYPE:
        raise TypeError("source input locator must be an exact Path")
    return Path(os.path.normpath(os.path.abspath(value)))


def _input_at(root: _InputRoot, relative_id: str) -> _InputFile:
    relative = _relative_input_id(relative_id)
    if type(root) is _GateInputSession:
        return _require_session(root).input(relative)
    if type(root) is not _PATH_TYPE:
        raise TypeError("input root must be an exact Path or Core session")
    return root.joinpath(*relative.split("/"))


def _read_input_bytes(value: _InputFile) -> bytes:
    if type(value) is _GateInputReference:
        return value._read()
    if type(value) is not _PATH_TYPE:
        raise TypeError("input must be an exact Path or Core input reference")
    if getattr(sys, "frozen", False):
        raise RuntimeError("frozen Core input cannot reopen a source pathname")
    path = _source_input_locator(value)
    with _open_source_input_session(path.parent) as session:
        content = session.read_bytes(path.name)
    return content


def _read_input_text(value: _InputFile) -> str:
    # Preserve universal-newline parsing, while digests consume original bytes.
    return _read_input_bytes(value).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


@contextmanager
def _source_validation_inputs(root: Path, approved: Path) -> Iterator[tuple[_GateInputSession, _GateInputReference]]:
    if type(root) is not _PATH_TYPE or type(approved) is not _PATH_TYPE:
        raise TypeError("source validation paths must be exact Paths")
    if getattr(sys, "frozen", False):
        raise RuntimeError("frozen validation requires an explicit trusted session")
    root = _source_input_locator(root)
    approved = _source_input_locator(approved)
    if not root.is_dir():
        raise ValueError("repository_root must be an existing directory")
    if not approved.is_file():
        raise ValueError("approved_roots_path must be an existing file")
    with _compose_source_input_owner(root) as owner:
        owner._associate_source_path(approved)
        with owner.open_session() as session:
            yield session, session.input_path(approved)
