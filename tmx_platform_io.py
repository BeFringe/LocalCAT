"""TMX-owned bounded I/O over backend-neutral rooted file authorities."""

from __future__ import annotations

import hashlib
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


_READ_CHUNK_BYTES = 64 * 1024


def platform_relative_path(*parts: str) -> PurePosixPath | PureWindowsPath:
    if not parts:
        raise ValueError("a TMX relative path must contain at least one part")
    if os.name == "nt":
        return PureWindowsPath(*parts)
    return PurePosixPath(*parts)


def iter_bound_chunks(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
) -> Iterator[bytes]:
    """Yield exact retained bytes and finish with a live snapshot reproof."""

    if not isinstance(source, BoundRegularFile):
        raise TypeError("TMX source must be BoundRegularFile")
    if type(snapshot) is not EntrySnapshot:
        raise TypeError("TMX source snapshot must be exact EntrySnapshot")
    offset = 0
    while offset < snapshot.byte_count:
        chunk = source.read_at(
            offset,
            min(_READ_CHUNK_BYTES, snapshot.byte_count - offset),
            snapshot,
        )
        yield chunk
        offset += len(chunk)
    if source.snapshot() != snapshot:
        raise PlatformFileError(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )


def digest_bound(source: BoundRegularFile, snapshot: EntrySnapshot) -> bytes:
    digest = hashlib.sha256()
    for chunk in iter_bound_chunks(source, snapshot):
        digest.update(chunk)
    return digest.digest()


def read_bound_all(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    *,
    maximum_bytes: int,
) -> bytes:
    if type(maximum_bytes) is not int:
        raise TypeError("maximum TMX bytes must be exact int")
    if maximum_bytes < 0:
        raise ValueError("maximum TMX bytes must be non-negative")
    if snapshot.byte_count > maximum_bytes:
        raise PlatformFileError(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            retryable=False,
        )
    return b"".join(iter_bound_chunks(source, snapshot))


def read_rooted_all(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one absolute regular file through the composed rooted authority."""

    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError("TMX rooted path must be an absolute concrete Path")
    from platform_fs import compose_platform_file_backend

    backend: PlatformFileBackend = compose_platform_file_backend(path.parent)
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
        return read_bound_all(source, snapshot, maximum_bytes=maximum_bytes)
    finally:
        if source is not None:
            source.close()
        if root is not None:
            root.close()


__all__ = [
    "digest_bound",
    "iter_bound_chunks",
    "platform_relative_path",
    "read_bound_all",
    "read_rooted_all",
]
