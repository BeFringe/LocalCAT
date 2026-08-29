"""Resource-owned helpers over the backend-neutral platform file contracts.

This module deliberately contains no resource/package grammar.  It only adapts
retained rooted authorities to bounded reads used by Resource owners.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator

from platform_fs_contracts import (
    BoundRegularFile,
    EntrySnapshot,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
)


_READ_CHUNK = 64 * 1024


def platform_relative_path(*parts: str) -> PurePosixPath | PureWindowsPath:
    """Return an exact pure path for the active backend without resolving it."""

    if not parts:
        raise ValueError("a relative platform path must contain at least one part")
    if os.name == "nt":
        return PureWindowsPath(*parts)
    return PurePosixPath(*parts)


@contextmanager
def open_rooted_regular(
    backend: PlatformFileBackend,
    path: Path,
) -> Iterator[BoundRegularFile]:
    """Bind one absolute, single-link regular file and retain its exact handle."""

    source = bind_rooted_regular(backend, path)
    try:
        yield source
    finally:
        source.close()


def bind_rooted_regular(
    backend: PlatformFileBackend,
    path: Path,
) -> BoundRegularFile:
    """Return one caller-owned retained regular-file authority."""

    if not isinstance(backend, PlatformFileBackend):
        raise TypeError("resource platform backend must satisfy PlatformFileBackend")
    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError("rooted resource path must be an absolute concrete Path")
    root = None
    source = None
    try:
        root = backend.bind_root(path.parent)
        source = backend.open_regular(root, platform_relative_path(path.name))
        snapshot = source.snapshot()
        if (
            snapshot.identity.kind != "regular"
            or snapshot.identity.link_count != 1
            or not snapshot.reparse_free
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.REPARSE_REJECTED,
                retryable=False,
            )
        root.close()
        root = None
        return source
    except BaseException:
        if source is not None:
            source.close()
        raise
    finally:
        if root is not None:
            root.close()


def read_bound_all(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one small owner artifact while preserving the exact live baseline."""

    if not isinstance(source, BoundRegularFile):
        raise TypeError("resource source must be BoundRegularFile")
    if type(snapshot) is not EntrySnapshot:
        raise TypeError("resource snapshot must be exact EntrySnapshot")
    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise TypeError("maximum resource bytes must be nonnegative int")
    if snapshot.byte_count > maximum_bytes:
        raise PlatformFileError(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            retryable=False,
        )
    chunks: list[bytes] = []
    offset = 0
    while offset < snapshot.byte_count:
        chunk = source.read_at(
            offset,
            min(_READ_CHUNK, snapshot.byte_count - offset),
            snapshot,
        )
        chunks.append(chunk)
        offset += len(chunk)
    if source.snapshot() != snapshot:
        raise PlatformFileError(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )
    return b"".join(chunks)


def digest_bound(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
) -> bytes:
    """Digest one retained file without materializing it."""

    if not isinstance(source, BoundRegularFile):
        raise TypeError("resource source must be BoundRegularFile")
    if type(snapshot) is not EntrySnapshot:
        raise TypeError("resource snapshot must be exact EntrySnapshot")
    digest = hashlib.sha256()
    offset = 0
    while offset < snapshot.byte_count:
        chunk = source.read_at(
            offset,
            min(_READ_CHUNK, snapshot.byte_count - offset),
            snapshot,
        )
        digest.update(chunk)
        offset += len(chunk)
    if source.snapshot() != snapshot:
        raise PlatformFileError(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )
    return digest.digest()


def iter_bound_chunks(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    *,
    start: int = 0,
    length: int | None = None,
) -> Iterator[bytes]:
    """Yield one exact retained byte range in contract-sized chunks."""

    if not isinstance(source, BoundRegularFile):
        raise TypeError("resource source must be BoundRegularFile")
    if type(snapshot) is not EntrySnapshot:
        raise TypeError("resource snapshot must be exact EntrySnapshot")
    if type(start) is not int or start < 0 or start > snapshot.byte_count:
        raise ValueError("resource chunk start is outside its snapshot")
    if length is None:
        length = snapshot.byte_count - start
    if type(length) is not int or length < 0 or start + length > snapshot.byte_count:
        raise ValueError("resource chunk length is outside its snapshot")
    offset = start
    remaining = length
    while remaining:
        chunk = source.read_at(
            offset,
            min(_READ_CHUNK, remaining),
            snapshot,
        )
        yield chunk
        offset += len(chunk)
        remaining -= len(chunk)
    if source.snapshot() != snapshot:
        raise PlatformFileError(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )


class BoundRegularFileStream(io.RawIOBase):
    """Read-only seekable stream over one retained regular-file authority."""

    def __init__(
        self,
        source: BoundRegularFile,
        snapshot: EntrySnapshot,
        *,
        start: int = 0,
        length: int | None = None,
    ) -> None:
        super().__init__()
        if not isinstance(source, BoundRegularFile):
            raise TypeError("resource stream source must be BoundRegularFile")
        if type(snapshot) is not EntrySnapshot:
            raise TypeError("resource stream snapshot must be exact EntrySnapshot")
        if type(start) is not int or start < 0 or start > snapshot.byte_count:
            raise ValueError("resource stream start is outside its snapshot")
        if length is None:
            length = snapshot.byte_count - start
        if type(length) is not int or length < 0 or start + length > snapshot.byte_count:
            raise ValueError("resource stream length is outside its snapshot")
        self._source = source
        self._snapshot = snapshot
        self._start = start
        self._length = length
        self._offset = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._offset

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if type(offset) is not int or type(whence) is not int:
            raise TypeError("resource stream seek values must be exact int")
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._offset + offset
        elif whence == io.SEEK_END:
            target = self._length + offset
        else:
            raise ValueError("unsupported resource stream whence")
        if target < 0 or target > self._length:
            raise ValueError("resource stream seek is outside the bounded member")
        self._offset = target
        return target

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        if type(size) is not int:
            raise TypeError("resource stream read size must be exact int")
        remaining = self._length - self._offset
        if size < 0 or size > remaining:
            size = remaining
        chunks: list[bytes] = []
        while size:
            count = min(_READ_CHUNK, size)
            chunk = self._source.read_at(
                self._start + self._offset,
                count,
                self._snapshot,
            )
            chunks.append(chunk)
            self._offset += len(chunk)
            size -= len(chunk)
        return b"".join(chunks)


__all__ = [
    "BoundRegularFileStream",
    "bind_rooted_regular",
    "digest_bound",
    "iter_bound_chunks",
    "open_rooted_regular",
    "platform_relative_path",
    "read_bound_all",
]
