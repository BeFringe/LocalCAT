"""Live rooted authority for LocalCAT current-source composition.

This module deliberately exposes only source-file binding and reproof.  Path
objects remain locators; the platform backend's retained root and regular-file
handles are the authority.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import sys
from threading import RLock
from typing import Iterator, final

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    BoundRegularFile,
    EntrySnapshot,
    OpaqueAuthority,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    RootedDirectoryAuthority,
    validate_root_path,
)


def _relative_locator(path: Path, root: Path) -> PurePath:
    """Return a backend-neutral relative locator without filesystem lookup."""

    if type(path) is not type(Path()):
        raise TypeError("source path must be a concrete pathlib Path")
    if not path.is_absolute():
        raise ValueError("source path must be absolute")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("source path must be within the authority root") from None
    if not relative.parts:
        raise ValueError("source binding requires a file below the authority root")
    if sys.platform == "win32":
        return PureWindowsPath(*relative.parts)
    return PurePosixPath(*relative.parts)


@final
class RootedSourceFile:
    """One immutable source snapshot retained by a live platform file handle."""

    __slots__ = (
        "__content",
        "__content_sha256",
        "__file",
        "__lock",
        "__owner",
        "__path",
        "__relative",
        "__snapshot",
    )

    def __init__(
        self,
        *,
        owner: RootedSourceAuthority,
        path: Path,
        relative: PurePath,
        file: BoundRegularFile,
        snapshot: EntrySnapshot,
        content: bytes,
        content_sha256: bytes,
    ) -> None:
        if type(owner) is not RootedSourceAuthority:
            raise TypeError("source file requires RootedSourceAuthority")
        if type(path) is not type(Path()) or not path.is_absolute():
            raise TypeError("source file path must be an absolute concrete Path")
        if type(relative) not in (PurePosixPath, PureWindowsPath):
            raise TypeError("source file relative locator must be exact PurePath")
        if relative.is_absolute() or not relative.parts:
            raise ValueError("source file relative locator must be non-empty")
        if not isinstance(file, BoundRegularFile):
            raise TypeError("source file requires a bound regular-file authority")
        if type(snapshot) is not EntrySnapshot:
            raise TypeError("source file snapshot must be exact EntrySnapshot")
        if type(content) is not bytes:
            raise TypeError("source file content must be exact bytes")
        if type(content_sha256) is not bytes or len(content_sha256) != 32:
            raise TypeError("source file digest must be 32 exact bytes")
        if snapshot.byte_count != len(content):
            raise ValueError("source file snapshot contradicts captured bytes")
        if hashlib.sha256(content).digest() != content_sha256:
            raise ValueError("source file digest contradicts captured bytes")
        self.__owner = owner
        self.__path = path
        self.__relative = relative
        self.__file = file
        self.__snapshot = snapshot
        self.__content = content
        self.__content_sha256 = content_sha256
        self.__lock = RLock()

    @property
    def path(self) -> Path:
        return self.__path

    @property
    def content(self) -> bytes:
        return self.__content

    @property
    def content_sha256(self) -> bytes:
        return self.__content_sha256

    def is_current(self) -> bool:
        """Reprove root, live identity and exact bytes without path authority."""

        try:
            return self.__owner._reprove_file(self)
        except (OSError, PlatformFileError):
            return False

    def _retained_is_current(self) -> bool:
        with self.__lock:
            capture = self.__file.capture_content()
        return (
            capture.facts.snapshot == self.__snapshot
            and capture.content == self.__content
            and capture.facts.content_sha256 == self.__content_sha256
        )

    def _bound_file(self) -> BoundRegularFile:
        return self.__file

    def _relative_path(self) -> PurePath:
        return self.__relative

    def _captured_snapshot(self) -> EntrySnapshot:
        return self.__snapshot

    def _captured_content(self) -> bytes:
        return self.__content

    def _captured_content_sha256(self) -> bytes:
        return self.__content_sha256

    def _is_owned_by(self, owner: RootedSourceAuthority) -> bool:
        return self.__owner is owner


@final
class RootedSourceAuthority(OpaqueAuthority):
    """Source-only authority rooted by the composed platform backend."""

    __slots__ = (
        "__backend",
        "__files",
        "__lock",
        "__proof_window_files",
        "__root",
        "__root_path",
    )

    def __init__(
        self,
        *,
        root_path: Path,
        backend: PlatformFileBackend,
        root: RootedDirectoryAuthority,
    ) -> None:
        super().__init__()
        self.__root_path = validate_root_path(root_path)
        if not isinstance(backend, PlatformFileBackend):
            raise TypeError("source authority requires PlatformFileBackend")
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("source authority requires rooted directory authority")
        self.__backend = backend
        self.__root = root
        self.__files: dict[PurePath, RootedSourceFile] = {}
        self.__lock = RLock()
        self.__proof_window_files: set[PurePath] | None = None

    @property
    def root_path(self) -> Path:
        self._require_open()
        return self.__root_path

    def reprove(self) -> None:
        self._require_open()
        with self.__lock:
            self._reprove_root()

    def _reprove_root(self, *, force: bool = False) -> None:
        """Reprove the root once per synchronous source-proof window."""

        if force or self.__proof_window_files is None:
            self.__root.reprove()

    @contextmanager
    def proof_window(self) -> Iterator[None]:
        """Reuse source proofs only within one synchronous composition operation."""

        self._require_open()
        with self.__lock:
            if self.__proof_window_files is not None:
                raise RuntimeError("source proof windows cannot be nested")
            self._reprove_root(force=True)
            self.__proof_window_files = set()
            try:
                yield
                for relative in tuple(self.__proof_window_files):
                    source = self.__files.get(relative)
                    if source is None or not self._reprove_file_now(source):
                        raise PlatformFileError(
                            PlatformFileErrorCode.IDENTITY_STALE,
                            retryable=True,
                        )
                self._reprove_root(force=True)
            finally:
                self.__proof_window_files = None

    def bind_path(self, path: Path) -> RootedSourceFile:
        """Bind an absolute locator below the retained root exactly once."""

        self._require_open()
        relative = _relative_locator(path, self.__root_path)
        with self.__lock:
            cached = self.__files.get(relative)
            if cached is not None:
                if not cached.is_current():
                    raise PlatformFileError(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
                return cached
            self._reprove_root()
            bound = self.__backend.open_regular(self.__root, relative)
            try:
                capture = bound.capture_content()
                self._reprove_root()
                source = RootedSourceFile(
                    owner=self,
                    path=self.__root_path.joinpath(*relative.parts),
                    relative=relative,
                    file=bound,
                    snapshot=capture.facts.snapshot,
                    content=capture.content,
                    content_sha256=capture.facts.content_sha256,
                )
            except BaseException:
                bound.close()
                raise
            self.__files[relative] = source
            if self.__proof_window_files is not None:
                self.__proof_window_files.add(relative)
            return source

    def _reprove_file(self, source: RootedSourceFile) -> bool:
        self._require_open()
        if type(source) is not RootedSourceFile or not source._is_owned_by(self):
            raise PermissionError("source file belongs to another rooted authority")
        with self.__lock:
            relative = source._relative_path()
            if (
                self.__proof_window_files is not None
                and relative in self.__proof_window_files
            ):
                return True
            current = self._reprove_file_now(source)
            if current and self.__proof_window_files is not None:
                self.__proof_window_files.add(relative)
            return current

    def _reprove_file_now(self, source: RootedSourceFile) -> bool:
        if not source._retained_is_current():
            return False
        self._reprove_root()
        rebound = self.__backend.open_regular(
            self.__root,
            source._relative_path(),
        )
        try:
            capture = rebound.capture_content()
            self._reprove_root()
        finally:
            rebound.close()
        captured_snapshot = source._captured_snapshot()
        captured_content = source._captured_content()
        captured_digest = source._captured_content_sha256()
        return (
            capture.facts.snapshot == captured_snapshot
            and capture.content == captured_content
            and capture.facts.content_sha256 == captured_digest
        )

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        with self.__lock:
            files = tuple(self.__files.values())
            self.__files.clear()
            for source in reversed(files):
                try:
                    source._bound_file().close()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
            try:
                self.__root.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


def compose_rooted_source_authority(
    root: Path,
    *,
    backend: PlatformFileBackend | None = None,
) -> RootedSourceAuthority:
    """Compose and retain the platform root before any source graph is trusted."""

    checked_root = validate_root_path(root)
    selected_backend = (
        compose_platform_file_backend(checked_root)
        if backend is None
        else backend
    )
    if not isinstance(selected_backend, PlatformFileBackend):
        raise TypeError("source authority requires PlatformFileBackend")
    rooted = selected_backend.bind_root(checked_root)
    try:
        rooted.reprove()
        authority = RootedSourceAuthority(
            root_path=checked_root,
            backend=selected_backend,
            root=rooted,
        )
        authority.reprove()
        return authority
    except BaseException:
        rooted.close()
        raise


__all__ = [
    "RootedSourceAuthority",
    "RootedSourceFile",
    "compose_rooted_source_authority",
]
