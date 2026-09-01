"""Platform-neutral rooted I/O for TM release validators.

The validators keep their evidence schemas and owner-specific error vocabulary.
This module only supplies the shared source-read and canonical-publication
primitives needed by those validators on POSIX and Windows.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import tempfile

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    LockPolicy,
    LockWait,
    PlatformFileError,
    PublishMode,
)


_WINDOWS_RELEASE_LOCK_NAME = ".localcat-release-evidence.lock"
_WINDOWS_RELEASE_LOCK_PAYLOAD = b"localcat.release-evidence-owner.v1\0"


def strict_read_regular(
    root: Path,
    relative: Path,
    *,
    parent_error: str,
    nonregular_error: str,
    source_error: str,
    identity_error: str,
) -> tuple[bytes, str]:
    """Read exact bytes from one already-canonical repository-relative path."""

    if sys.platform == "win32":
        return _windows_strict_read_regular(
            root,
            relative,
            nonregular_error=nonregular_error,
            source_error=source_error,
            identity_error=identity_error,
        )
    return _posix_strict_read_regular(
        root,
        relative,
        parent_error=parent_error,
        nonregular_error=nonregular_error,
        source_error=source_error,
        identity_error=identity_error,
    )


def validate_evidence_target(path: Path, *, target_error: str) -> None:
    """Reject a non-regular, aliased, or multiply-linked existing target."""

    if sys.platform == "win32":
        _windows_validate_evidence_target(path, target_error=target_error)
        return
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ValueError(target_error)


def atomic_write(
    path: Path,
    payload: bytes,
    validate_snapshot: Callable[[], None],
    *,
    candidate_prefix: str,
    identity_error: str,
    readback_error: str,
    platform_error: str,
) -> None:
    """Publish canonical evidence after validating inputs before and after."""

    if sys.platform == "win32":
        _windows_atomic_write(
            path,
            payload,
            validate_snapshot,
            candidate_prefix=candidate_prefix,
            identity_error=identity_error,
            readback_error=readback_error,
            platform_error=platform_error,
        )
        return
    _posix_atomic_write(
        path,
        payload,
        validate_snapshot,
        candidate_prefix=candidate_prefix,
        identity_error=identity_error,
        readback_error=readback_error,
    )


def _windows_strict_read_regular(
    root: Path,
    relative: Path,
    *,
    nonregular_error: str,
    source_error: str,
    identity_error: str,
) -> tuple[bytes, str]:
    try:
        return _windows_strict_read_regular_impl(
            root,
            relative,
            nonregular_error=nonregular_error,
            identity_error=identity_error,
        )
    except (PlatformFileError, OSError) as error:
        raise ValueError(source_error) from error


def _windows_strict_read_regular_impl(
    root: Path,
    relative: Path,
    *,
    nonregular_error: str,
    identity_error: str,
) -> tuple[bytes, str]:
    backend = compose_platform_file_backend(root)
    root_authority = None
    source = None
    try:
        root_authority = backend.bind_root(root)
        source = backend.open_regular(
            root_authority,
            PureWindowsPath(*relative.parts),
        )
        before = source.content_facts()
        snapshot = before.snapshot
        if not (
            snapshot.identity.kind == "regular"
            and snapshot.identity.link_count == 1
            and snapshot.reparse_free
        ):
            raise ValueError(nonregular_error)
        payload = source.read_all()
        after = source.content_facts()
        if (
            after != before
            or len(payload) != snapshot.byte_count
            or hashlib.sha256(payload).digest() != before.content_sha256
        ):
            raise ValueError(identity_error)
        return payload, before.content_sha256.hex()
    finally:
        if source is not None:
            source.close()
        if root_authority is not None:
            root_authority.close()


def _windows_validate_evidence_target(
    path: Path,
    *,
    target_error: str,
) -> None:
    try:
        _windows_validate_evidence_target_impl(path, target_error=target_error)
    except (PlatformFileError, OSError) as error:
        raise ValueError(target_error) from error


def _windows_validate_evidence_target_impl(
    path: Path,
    *,
    target_error: str,
) -> None:
    parent = path.parent.resolve(strict=True)
    backend = compose_platform_file_backend(parent)
    root_authority = None
    parent_authority = None
    try:
        root_authority = backend.bind_root(parent)
        parent_authority = backend.bind_parent(
            root_authority,
            PureWindowsPath(path.name),
        )
        observed = parent_authority.inspect_entry(path.name)
        if observed is not None and not (
            observed.identity.kind == "regular"
            and observed.identity.link_count == 1
            and observed.reparse_free
        ):
            raise ValueError(target_error)
    finally:
        if parent_authority is not None:
            parent_authority.close()
        if root_authority is not None:
            root_authority.close()


def _windows_atomic_write(
    path: Path,
    payload: bytes,
    validate_snapshot: Callable[[], None],
    *,
    candidate_prefix: str,
    identity_error: str,
    readback_error: str,
    platform_error: str,
) -> None:
    try:
        _windows_atomic_write_impl(
            path,
            payload,
            validate_snapshot,
            candidate_prefix=candidate_prefix,
            identity_error=identity_error,
            readback_error=readback_error,
        )
    except (PlatformFileError, OSError) as error:
        raise ValueError(platform_error) from error


def _windows_atomic_write_impl(
    path: Path,
    payload: bytes,
    validate_snapshot: Callable[[], None],
    *,
    candidate_prefix: str,
    identity_error: str,
    readback_error: str,
) -> None:
    parent_path = path.parent.resolve(strict=True)
    backend = compose_platform_file_backend(parent_path)
    root_authority = None
    parent = None
    lease = None
    candidate = None
    pending = None
    candidate_identity = None
    candidate_name = f"{candidate_prefix}{path.name}.tmp"
    digest = hashlib.sha256(payload).digest()
    try:
        root_authority = backend.bind_root(parent_path)
        parent = backend.bind_parent(
            root_authority,
            PureWindowsPath(path.name),
        )
        lease = backend.acquire(
            parent,
            _WINDOWS_RELEASE_LOCK_NAME,
            _WINDOWS_RELEASE_LOCK_PAYLOAD,
            LockPolicy(LockWait.BLOCK),
        )
        lease.reprove_binding(
            parent,
            _WINDOWS_RELEASE_LOCK_NAME,
            _WINDOWS_RELEASE_LOCK_PAYLOAD,
        )
        observed = parent.inspect_entry(path.name)
        if observed is not None and not (
            observed.identity.kind == "regular"
            and observed.identity.link_count == 1
            and observed.reparse_free
        ):
            raise ValueError(identity_error)
        candidate = parent.create_candidate(candidate_name, private=False)
        candidate_identity = candidate.identity()
        candidate.write_all(payload)
        candidate.flush_content()

        validate_snapshot()
        pending = parent.begin_publish(
            candidate,
            path.name,
            mode=(
                PublishMode.CREATE_IF_ABSENT
                if observed is None
                else PublishMode.REPLACE_UNDER_LOCK
            ),
            lease=None if observed is None else lease,
        )
        candidate = None
        candidate_identity = None

        preliminary = pending.preliminary_facts()
        retained = pending.retained_destination()
        retained_facts = retained.content_facts()
        readback = retained.read_all()
        if (
            preliminary.content_sha256 != digest
            or preliminary.byte_count != len(payload)
            or retained_facts.content_sha256 != digest
            or retained_facts.snapshot.byte_count != len(payload)
            or retained_facts.snapshot.identity.kind != "regular"
            or retained_facts.snapshot.identity.link_count != 1
            or not retained_facts.snapshot.reparse_free
            or readback != payload
        ):
            raise ValueError(readback_error)
        validate_snapshot()
        try:
            terminal = pending.terminal_reproof()
        except PlatformFileError as error:
            raise ValueError(readback_error) from error
        if terminal != preliminary:
            raise ValueError(identity_error)
    finally:
        if pending is not None:
            pending.close()
        if candidate is not None:
            candidate.close()
        if candidate_identity is not None and parent is not None:
            try:
                parent.unlink_owned(candidate_name, candidate_identity)
            except (PlatformFileError, OSError):
                pass
        if lease is not None:
            lease.close()
        if parent is not None:
            parent.close()
        if root_authority is not None:
            root_authority.close()


def _posix_strict_read_regular(
    root: Path,
    relative: Path,
    *,
    parent_error: str,
    nonregular_error: str,
    source_error: str,
    identity_error: str,
) -> tuple[bytes, str]:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    directory_descriptor = os.open(root, directory_flags)
    file_descriptor = -1
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            observed = os.fstat(next_descriptor)
            if not stat.S_ISDIR(observed.st_mode):
                os.close(next_descriptor)
                raise ValueError(parent_error)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        filename = relative.parts[-1]
        file_descriptor = os.open(
            filename,
            file_flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError(nonregular_error)
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        terminal = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(terminal.st_mode)
            or terminal.st_nlink != 1
            or (terminal.st_dev, terminal.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(identity_error)
        return b"".join(chunks), digest.hexdigest()
    except OSError as error:
        raise ValueError(source_error) from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(directory_descriptor)


def _posix_atomic_write(
    path: Path,
    payload: bytes,
    validate_snapshot: Callable[[], None],
    *,
    candidate_prefix: str,
    identity_error: str,
    readback_error: str,
) -> None:
    parent = path.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=candidate_prefix,
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            published_identity = os.fstat(stream.fileno())
        validate_snapshot()
        os.replace(temporary, path)
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        parent_descriptor = os.open(parent, flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        observed = os.lstat(path)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (published_identity.st_dev, published_identity.st_ino)
        ):
            raise ValueError(identity_error)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        read_descriptor = os.open(path, flags)
        try:
            readback = bytearray()
            while chunk := os.read(read_descriptor, 1024 * 1024):
                readback.extend(chunk)
            terminal = os.fstat(read_descriptor)
        finally:
            os.close(read_descriptor)
        if (
            bytes(readback) != payload
            or terminal.st_nlink != 1
            or (terminal.st_dev, terminal.st_ino)
            != (published_identity.st_dev, published_identity.st_ino)
        ):
            raise ValueError(readback_error)
        validate_snapshot()
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "atomic_write",
    "strict_read_regular",
    "validate_evidence_target",
]
