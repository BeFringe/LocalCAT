"""POSIX adapter for the backend-neutral platform file contracts.

This module confines the shared adapter's POSIX-only file locking and dirfd
primitives.  Composition imports it only after selecting a POSIX host.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
from pathlib import Path, PurePath
import stat
import time
from typing import Callable, Iterator

from platform_fs_contracts import (
    BoundContentFacts,
    BoundDirectoryAuthority,
    BoundRegularFile,
    BoundSynchronizedRegularFile,
    CandidateFile,
    CandidateContentFacts,
    EntrySnapshot,
    ExistingFileDurability,
    FileObjectIdentity,
    LedgerEntryObservation,
    LedgerEnumerationLimits,
    LockLease,
    LockPolicy,
    LockWait,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateAccessEvidence,
    PrivateStorageProof,
    ProcessFileLock,
    PublishFacts,
    PublishMode,
    RootedDirectoryAuthority,
    RootedFileSystem,
    validate_relative_name,
)


__all__ = ["PosixPlatformAdapter"]

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_SYNCHRONIZED_READ_FLAGS = _READ_FLAGS
_DIRECTORY_FLAGS = _READ_FLAGS | os.O_DIRECTORY
_CANDIDATE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_READ_CHUNK = 1024 * 1024
_STREAM_CHUNK = 64 * 1024
_FaultInjector = Callable[[str], None]


def _platform_error(code: PlatformFileErrorCode, *, retryable: bool) -> PlatformFileError:
    return PlatformFileError(code, retryable=retryable)


def _map_os_error(
    error: OSError,
    *,
    fallback: PlatformFileErrorCode,
    retryable: bool,
) -> PlatformFileError:
    if error.errno in {errno.ELOOP, errno.EMLINK}:
        return _platform_error(PlatformFileErrorCode.REPARSE_REJECTED, retryable=False)
    if error.errno in {errno.ESTALE}:
        return _platform_error(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)
    return _platform_error(fallback, retryable=retryable)


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _duplicate_fd(descriptor: int) -> int:
    duplicate = os.dup(descriptor)
    try:
        os.set_inheritable(duplicate, False)
    except BaseException:
        _close_fd(duplicate)
        raise
    return duplicate


def _integer_bytes(value: int) -> bytes:
    if value < 0:
        raise ValueError("POSIX identity field must be non-negative")
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _identity_from_stat(result: os.stat_result) -> FileObjectIdentity:
    if result.st_nlink < 1:
        raise _platform_error(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )
    if stat.S_ISDIR(result.st_mode):
        kind = "directory"
    elif stat.S_ISREG(result.st_mode):
        kind = "regular"
    else:
        raise _platform_error(
            PlatformFileErrorCode.REPARSE_REJECTED,
            retryable=False,
        )
    return FileObjectIdentity(
        platform="posix",
        volume_id=_integer_bytes(result.st_dev),
        file_id=_integer_bytes(result.st_ino),
        kind=kind,
        link_count=result.st_nlink,
    )


def _modified_token(result: os.stat_result) -> bytes:
    return f"{result.st_mtime_ns}:{result.st_ctime_ns}".encode("ascii")


def _snapshot_from_stat(result: os.stat_result) -> EntrySnapshot:
    return EntrySnapshot(
        identity=_identity_from_stat(result),
        byte_count=result.st_size,
        modified_token=_modified_token(result),
        reparse_free=True,
    )


def _read_fd_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(descriptor, _READ_CHUNK, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _write_fd_all(descriptor: int, payload: bytes) -> None:
    os.ftruncate(descriptor, 0)
    offset = 0
    while offset < len(payload):
        written = os.pwrite(descriptor, payload[offset:], offset)
        if written <= 0:
            raise OSError(errno.EIO, "short POSIX write")
        offset += written


def _digest_fd_exact(descriptor: int, byte_count: int) -> CandidateContentFacts:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_count:
        requested = min(_STREAM_CHUNK, byte_count - offset)
        chunk = os.pread(descriptor, requested, offset)
        if len(chunk) != requested:
            raise OSError(errno.EIO, "short POSIX candidate readback")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, offset):
        raise OSError(errno.EIO, "candidate grew during readback")
    return CandidateContentFacts(offset, digest.digest())


def _same_identity(left: FileObjectIdentity, right: FileObjectIdentity) -> bool:
    return (
        left.platform == right.platform
        and left.volume_id == right.volume_id
        and left.file_id == right.file_id
        and left.kind == right.kind
    )


def _hit_fault(injector: _FaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _normalize_lock_platform_error(error: PlatformFileError) -> PlatformFileError:
    if error.code in {
        PlatformFileErrorCode.LOCK_CONTENDED.value,
        PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
    }:
        return error
    return _platform_error(
        PlatformFileErrorCode.LOCK_UNAVAILABLE,
        retryable=False,
    )


def _capture_directory_identities(
    descriptors: tuple[int, ...],
) -> tuple[FileObjectIdentity, ...]:
    identities: list[FileObjectIdentity] = []
    for descriptor in descriptors:
        identity = _identity_from_stat(os.fstat(descriptor))
        if identity.kind != "directory":
            raise _platform_error(
                PlatformFileErrorCode.REPARSE_REJECTED,
                retryable=False,
            )
        identities.append(identity)
    return tuple(identities)


def _reprove_directory_chain(
    descriptors: tuple[int, ...],
    names: tuple[str | None, ...],
    identities: tuple[FileObjectIdentity, ...],
) -> None:
    if not (
        len(descriptors) == len(names) == len(identities)
        and descriptors
        and names[0] is None
    ):
        raise _platform_error(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            retryable=False,
        )
    try:
        for index, (descriptor, expected) in enumerate(
            zip(descriptors, identities, strict=True)
        ):
            actual = _identity_from_stat(os.fstat(descriptor))
            if actual.kind != "directory" or not _same_identity(actual, expected):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            if index == 0:
                continue
            component = names[index]
            assert component is not None
            entry = os.stat(
                component,
                dir_fd=descriptors[index - 1],
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.REPARSE_REJECTED,
                    retryable=False,
                )
            entry_identity = _identity_from_stat(entry)
            if entry_identity.kind != "directory" or not _same_identity(
                entry_identity,
                expected,
            ):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
    except PlatformFileError:
        raise
    except OSError as error:
        raise _map_os_error(
            error,
            fallback=PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        ) from None


def _remove_owned_regular_entry(
    parent_fd: int,
    name: str,
    expected: FileObjectIdentity,
    fault_injector: _FaultInjector | None,
    reprove_parent: Callable[[], None],
) -> None:
    reprove_parent()
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    result_identity = _identity_from_stat(result)
    if (
        stat.S_ISLNK(result.st_mode)
        or result_identity.kind != "regular"
        or result_identity.link_count != 1
        or not _same_identity(result_identity, expected)
    ):
        raise _platform_error(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
    _hit_fault(fault_injector, "candidate_cleanup_before_final_reproof")
    reprove_parent()
    try:
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    final_identity = _identity_from_stat(final)
    if (
        stat.S_ISLNK(final.st_mode)
        or final_identity.kind != "regular"
        or final_identity.link_count != 1
        or not _same_identity(final_identity, expected)
    ):
        raise _platform_error(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
    # ADR-020 deliberately does not describe this pathname operation as CAS.
    # A non-cooperating same-user writer after this reproof remains visible risk.
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _remove_owned_empty_directory(
    parent_fd: int,
    name: str,
    expected: FileObjectIdentity,
    fault_injector: _FaultInjector | None,
    reprove_parent: Callable[[], None],
) -> None:
    reprove_parent()
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(result.st_mode) or not _same_identity(
        _identity_from_stat(result),
        expected,
    ):
        raise _platform_error(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
    _hit_fault(fault_injector, "private_cleanup_before_final_reproof")
    reprove_parent()
    try:
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(final.st_mode) or not _same_identity(
        _identity_from_stat(final),
        expected,
    ):
        raise _platform_error(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
    # This second reproof is conservative evidence, not portable conditional rmdir.
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


_DIRECTORY_AUTHORITY_SLOTS = (
    "_directory_fds",
    "_directory_names",
    "_directory_identities",
    "_fault_injector",
)


class _DirectoryAuthorityMixin:
    __slots__ = ()

    def __init__(
        self,
        directory_fds: tuple[int, ...],
        directory_names: tuple[str | None, ...],
        fault_injector: _FaultInjector | None,
    ) -> None:
        try:
            super().__init__()
            if not directory_fds or len(directory_fds) != len(directory_names):
                raise ValueError("directory authority requires at least one retained fd")
            identities = _capture_directory_identities(directory_fds)
            _reprove_directory_chain(directory_fds, directory_names, identities)
        except BaseException:
            for descriptor in reversed(directory_fds):
                _close_fd(descriptor)
            raise
        self._directory_fds = directory_fds
        self._directory_names = directory_names
        self._directory_identities = identities
        self._fault_injector = fault_injector

    @property
    def _directory_fd(self) -> int:
        return self._directory_fds[-1]

    @property
    def _directory_identity(self) -> FileObjectIdentity:
        return self._directory_identities[-1]

    def _reprove(self) -> None:
        _reprove_directory_chain(
            self._directory_fds,
            self._directory_names,
            self._directory_identities,
        )

    def _inspect_entry(self, name: str) -> EntrySnapshot | None:
        self._reprove()
        try:
            result = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            ) from None
        if stat.S_ISLNK(result.st_mode):
            raise _platform_error(
                PlatformFileErrorCode.REPARSE_REJECTED,
                retryable=False,
            )
        return _snapshot_from_stat(result)

    def _observe_ledger_entries(
        self,
        lease: LockLease,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        if not isinstance(lease, _PosixLockLease) or not lease._matches_parent(
            self._directory_fds,
            self._directory_names,
            self._directory_identities,
        ):
            raise _platform_error(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            )

        def observe_once() -> tuple[LedgerEntryObservation, ...]:
            observations: list[LedgerEntryObservation] = []
            total_bytes = 0
            try:
                with os.scandir(self._directory_fd) as entries:
                    for entry in entries:
                        name = entry.name
                        if name in {".", ".."}:
                            continue
                        try:
                            name = validate_relative_name(name)
                        except (TypeError, ValueError):
                            raise _platform_error(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                retryable=False,
                            ) from None
                        if len(observations) >= limits.maximum_entries:
                            raise _platform_error(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                retryable=False,
                            )
                        try:
                            encoded_name = name.encode("utf-8", errors="strict")
                        except UnicodeEncodeError:
                            raise _platform_error(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                retryable=False,
                            ) from None
                        if len(encoded_name) > limits.maximum_name_bytes:
                            raise _platform_error(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                retryable=False,
                            )
                        before = os.stat(
                            name,
                            dir_fd=self._directory_fd,
                            follow_symlinks=False,
                        )
                        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                            raise _platform_error(
                                PlatformFileErrorCode.REPARSE_REJECTED,
                                retryable=False,
                            )
                        if before.st_nlink != 1:
                            raise _platform_error(
                                PlatformFileErrorCode.IDENTITY_STALE,
                                retryable=True,
                            )
                        descriptor = os.open(name, _READ_FLAGS, dir_fd=self._directory_fd)
                        try:
                            retained = os.fstat(descriptor)
                            after = os.stat(
                                name,
                                dir_fd=self._directory_fd,
                                follow_symlinks=False,
                            )
                        finally:
                            _close_fd(descriptor)
                        before_snapshot = _snapshot_from_stat(before)
                        retained_snapshot = _snapshot_from_stat(retained)
                        after_snapshot = _snapshot_from_stat(after)
                        if not (
                            before_snapshot == retained_snapshot == after_snapshot
                            and retained_snapshot.identity.kind == "regular"
                            and retained_snapshot.identity.link_count == 1
                        ):
                            raise _platform_error(
                                PlatformFileErrorCode.IDENTITY_STALE,
                                retryable=True,
                            )
                        total_bytes += retained_snapshot.byte_count
                        if total_bytes > limits.maximum_total_bytes:
                            raise _platform_error(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                retryable=False,
                            )
                        observations.append(
                            LedgerEntryObservation(name, retained_snapshot)
                        )
            except PlatformFileError:
                raise
            except OSError as error:
                raise _map_os_error(
                    error,
                    fallback=PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                ) from None
            observations.sort(key=lambda observation: observation.name)
            return tuple(observations)

        self._reprove()
        first = observe_once()
        self._reprove()
        second = observe_once()
        self._reprove()
        if first != second:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        try:
            terminal_lease_match = lease._matches_parent(
                self._directory_fds,
                self._directory_names,
                self._directory_identities,
            )
        except Exception:
            raise _platform_error(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            ) from None
        if not terminal_lease_match:
            raise _platform_error(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            )
        return second

    def _create_candidate(self, name: str, *, private: bool) -> CandidateFile:
        self._reprove()
        descriptor: int | None = None
        directory_fds: tuple[int, ...] | None = None
        created_identity: FileObjectIdentity | None = None
        try:
            descriptor = os.open(
                name,
                _CANDIDATE_FLAGS,
                0o600 if private else 0o666,
                dir_fd=self._directory_fd,
            )
            created_identity = _identity_from_stat(os.fstat(descriptor))
            entry = os.stat(
                name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            entry_identity = _identity_from_stat(entry)
            if (
                stat.S_ISLNK(entry.st_mode)
                or created_identity.kind != "regular"
                or created_identity.link_count != 1
                or entry_identity.link_count != 1
                or not _same_identity(created_identity, entry_identity)
            ):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            if private:
                os.fchmod(descriptor, 0o600)
            directory_fds = _duplicate_directory_chain(self._directory_fds)
            _hit_fault(self._fault_injector, "candidate_after_create")
            transferred_descriptor = descriptor
            transferred_directories = directory_fds
            descriptor = None
            directory_fds = None
            return _PosixCandidateFile(
                transferred_descriptor,
                transferred_directories,
                self._directory_names,
                self._directory_identities,
                name,
                created_identity,
                self._fault_injector,
            )
        except BaseException as error:
            if directory_fds is not None:
                for retained in reversed(directory_fds):
                    _close_fd(retained)
            cleanup_error: BaseException | None = None
            if created_identity is not None:
                try:
                    _remove_owned_regular_entry(
                        self._directory_fd,
                        name,
                        created_identity,
                        self._fault_injector,
                        self._reprove,
                    )
                except BaseException as caught:
                    cleanup_error = caught
            if descriptor is not None:
                _close_fd(descriptor)
            if cleanup_error is not None:
                raise _platform_error(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                ) from None
            if isinstance(error, PlatformFileError):
                raise error
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None

    def _rename_candidate(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PublishFacts:
        self._reprove()
        if not isinstance(candidate, _PosixCandidateFile):
            raise _platform_error(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        candidate._reprove_owner(
            self._directory_fds,
            self._directory_names,
            self._directory_identities,
        )
        if mode is PublishMode.REPLACE_UNDER_LOCK:
            if not isinstance(lease, _PosixLockLease) or not lease._matches_parent(
                self._directory_fds,
                self._directory_names,
                self._directory_identities,
            ):
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )

        named = False
        try:
            if mode is PublishMode.CREATE_IF_ABSENT:
                os.link(
                    candidate._name,
                    destination,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                named = True
                os.unlink(candidate._name, dir_fd=self._directory_fd)
            else:
                os.replace(
                    candidate._name,
                    destination,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
                named = True
            candidate._mark_named(destination)
            _hit_fault(self._fault_injector, "candidate_after_naming")
            facts = candidate._publish_facts(mode)
            candidate._reprove_named_entry()
            os.fsync(self._directory_fd)
            candidate._reprove_named_entry()
        except FileExistsError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None
        except BaseException as error:
            if named:
                raise _platform_error(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                ) from None
            if isinstance(error, PlatformFileError):
                raise error
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None
        return facts

    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: PublishFacts,
    ) -> BoundRegularFile:
        self._reprove()
        chain: tuple[int, ...] | None = None
        descriptor: int | None = None
        authority: _PosixBoundRegularFile | None = None
        try:
            chain = _duplicate_directory_chain(self._directory_fds)
            descriptor = os.open(
                destination,
                _READ_FLAGS,
                dir_fd=chain[-1],
            )
            transferred_chain = chain
            transferred_descriptor = descriptor
            chain = None
            descriptor = None
            authority = _PosixBoundRegularFile(
                transferred_chain,
                self._directory_names,
                transferred_descriptor,
                destination,
                self._fault_injector,
            )
            _hit_fault(self._fault_injector, "published_after_open")
            facts = authority._publish_facts(preliminary_facts.mode)
            if facts != preliminary_facts:
                authority.close()
                authority = None
                raise _platform_error(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            return authority
        except BaseException as error:
            if authority is not None:
                authority.close()
            if descriptor is not None:
                _close_fd(descriptor)
            if chain is not None:
                for retained in reversed(chain):
                    _close_fd(retained)
            if isinstance(error, PlatformFileError):
                raise error
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            ) from None

    def _create_pending_publication(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> PendingPublication:
        return _PosixPendingPublication(preliminary_facts, retained_destination)

    def _unlink_owned(self, name: str, expected: FileObjectIdentity) -> None:
        self._reprove()
        try:
            _remove_owned_regular_entry(
                self._directory_fd,
                name,
                expected,
                self._fault_injector,
                self._reprove,
            )
        except BaseException as error:
            if isinstance(error, PlatformFileError):
                raise error
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None

    def _close_authority(self) -> None:
        for descriptor in reversed(self._directory_fds):
            _close_fd(descriptor)


class _PosixRootedDirectory(_DirectoryAuthorityMixin, RootedDirectoryAuthority):
    __slots__ = _DIRECTORY_AUTHORITY_SLOTS


class _PosixBoundDirectory(_DirectoryAuthorityMixin, BoundDirectoryAuthority):
    __slots__ = _DIRECTORY_AUTHORITY_SLOTS


def _duplicate_directory_chain(descriptors: tuple[int, ...]) -> tuple[int, ...]:
    duplicates: list[int] = []
    try:
        for descriptor in descriptors:
            duplicates.append(_duplicate_fd(descriptor))
    except BaseException:
        for duplicate in reversed(duplicates):
            _close_fd(duplicate)
        raise
    return tuple(duplicates)


def _open_directory_chain(
    root: _PosixRootedDirectory,
    components: tuple[str, ...],
) -> tuple[tuple[int, ...], tuple[str | None, ...]]:
    root._reprove()
    descriptors: list[int] = [_duplicate_fd(root._directory_fd)]
    names: list[str | None] = [None]
    try:
        for component in components:
            descriptor = os.open(
                component,
                _DIRECTORY_FLAGS,
                dir_fd=descriptors[-1],
            )
            identity = _identity_from_stat(os.fstat(descriptor))
            if identity.kind != "directory":
                raise _platform_error(
                    PlatformFileErrorCode.REPARSE_REJECTED,
                    retryable=False,
                )
            descriptors.append(descriptor)
            names.append(component)
        identities = _capture_directory_identities(tuple(descriptors))
        _reprove_directory_chain(tuple(descriptors), tuple(names), identities)
    except PlatformFileError:
        for descriptor in reversed(descriptors):
            _close_fd(descriptor)
        raise
    except OSError as error:
        for descriptor in reversed(descriptors):
            _close_fd(descriptor)
        raise _map_os_error(
            error,
            fallback=PlatformFileErrorCode.OUTSIDE_ROOT,
            retryable=False,
        ) from None
    return tuple(descriptors), tuple(names)


class _PosixBoundRegularFile(BoundRegularFile):
    __slots__ = (
        "_directory_fds",
        "_directory_names",
        "_directory_identities",
        "_descriptor",
        "_entry_name",
        "_bound_identity",
        "_fault_injector",
    )

    def __init__(
        self,
        directory_fds: tuple[int, ...],
        directory_names: tuple[str | None, ...],
        descriptor: int,
        entry_name: str,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__()
        self._directory_fds = directory_fds
        try:
            self._directory_identities = _capture_directory_identities(directory_fds)
            _reprove_directory_chain(
                directory_fds,
                directory_names,
                self._directory_identities,
            )
            result = os.fstat(descriptor)
            identity = _identity_from_stat(result)
            if identity.kind != "regular":
                raise _platform_error(
                    PlatformFileErrorCode.REPARSE_REJECTED,
                    retryable=False,
                )
        except BaseException:
            _close_fd(descriptor)
            for item in reversed(directory_fds):
                _close_fd(item)
            raise
        self._descriptor = descriptor
        self._directory_names = directory_names
        self._entry_name = entry_name
        self._bound_identity = identity
        self._fault_injector = fault_injector

    def _reprove(self) -> os.stat_result:
        try:
            _reprove_directory_chain(
                self._directory_fds,
                self._directory_names,
                self._directory_identities,
            )
            descriptor_stat = os.fstat(self._descriptor)
            descriptor_identity = _identity_from_stat(descriptor_stat)
            entry_stat = os.stat(
                self._entry_name,
                dir_fd=self._directory_fds[-1],
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_stat.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.REPARSE_REJECTED,
                    retryable=False,
                )
            entry_identity = _identity_from_stat(entry_stat)
            if not (
                _same_identity(descriptor_identity, self._bound_identity)
                and _same_identity(entry_identity, self._bound_identity)
            ):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            return descriptor_stat
        except PlatformFileError:
            raise
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            ) from None

    def _read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected_snapshot: EntrySnapshot,
    ) -> bytes:
        before = _snapshot_from_stat(self._reprove())
        if before != expected_snapshot:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        expected_count = min(
            maximum_bytes,
            max(0, expected_snapshot.byte_count - offset),
        )
        try:
            if expected_count == 0:
                if os.pread(self._descriptor, 1, offset):
                    raise _platform_error(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
                payload = b""
            else:
                payload = os.pread(self._descriptor, expected_count, offset)
                if len(payload) != expected_count:
                    raise _platform_error(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
        except PlatformFileError:
            raise
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            ) from None
        after = _snapshot_from_stat(self._reprove())
        if after != expected_snapshot:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return payload

    def _identity(self) -> FileObjectIdentity:
        return _identity_from_stat(self._reprove())

    def _snapshot(self) -> EntrySnapshot:
        return _snapshot_from_stat(self._reprove())

    def _content_facts(self) -> BoundContentFacts:
        before = _snapshot_from_stat(self._reprove())
        try:
            actual = _digest_fd_exact(self._descriptor, before.byte_count)
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            ) from None
        after = _snapshot_from_stat(self._reprove())
        if after != before:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return BoundContentFacts(after, actual.content_sha256)

    def _publish_facts(self, mode: PublishMode) -> PublishFacts:
        expected = self.snapshot()
        digest = hashlib.sha256()
        offset = 0
        while offset < expected.byte_count:
            chunk = self.read_at(
                offset,
                min(_STREAM_CHUNK, expected.byte_count - offset),
                expected,
            )
            digest.update(chunk)
            offset += len(chunk)
        if self.read_at(offset, 1, expected) or self.snapshot() != expected:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return PublishFacts(
            mode=mode,
            destination_identity=self._identity(),
            content_sha256=digest.digest(),
            byte_count=offset,
            reparse_free=True,
        )

    def _close_authority(self) -> None:
        _close_fd(self._descriptor)
        for descriptor in reversed(self._directory_fds):
            _close_fd(descriptor)


class _PosixBoundSynchronizedRegularFile(
    _PosixBoundRegularFile,
    BoundSynchronizedRegularFile,
):
    def __init__(
        self,
        directory_fds: tuple[int, ...],
        directory_names: tuple[str | None, ...],
        descriptor: int,
        entry_name: str,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__(
            directory_fds,
            directory_names,
            descriptor,
            entry_name,
            fault_injector,
        )
        if self._bound_identity.link_count != 1:
            self._close_authority()
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )

    def _reprove(self) -> os.stat_result:
        result = super()._reprove()
        if _identity_from_stat(result).link_count != 1:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return result

    def _synchronize_content(
        self,
        expected: BoundContentFacts,
    ) -> BoundContentFacts:
        try:
            os.fsync(self._descriptor)
            os.fsync(self._directory_fds[-1])
        except OSError:
            raise _platform_error(
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                retryable=False,
            ) from None
        return expected


class _PosixCandidateFile(CandidateFile):
    __slots__ = (
        "_descriptor",
        "_directory_fds",
        "_directory_names",
        "_directory_identities",
        "_name",
        "_bound_identity",
        "_named_destination",
        "_fault_injector",
        "_flushed_facts",
    )

    def __init__(
        self,
        descriptor: int,
        directory_fds: tuple[int, ...],
        directory_names: tuple[str | None, ...],
        directory_identities: tuple[FileObjectIdentity, ...],
        name: str,
        bound_identity: FileObjectIdentity,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__()
        try:
            captured_directories = _capture_directory_identities(directory_fds)
            if not (
                len(captured_directories) == len(directory_identities)
                and all(
                    _same_identity(left, right)
                    for left, right in zip(
                        captured_directories,
                        directory_identities,
                        strict=True,
                    )
                )
            ):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            _reprove_directory_chain(
                directory_fds,
                directory_names,
                captured_directories,
            )
            descriptor_identity = _identity_from_stat(os.fstat(descriptor))
            entry_stat = os.stat(
                name,
                dir_fd=directory_fds[-1],
                follow_symlinks=False,
            )
            entry_identity = _identity_from_stat(entry_stat)
            if (
                stat.S_ISLNK(entry_stat.st_mode)
                or descriptor_identity.kind != "regular"
                or descriptor_identity.link_count != 1
                or entry_identity.link_count != 1
                or not _same_identity(descriptor_identity, bound_identity)
                or not _same_identity(entry_identity, bound_identity)
            ):
                raise _platform_error(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
        except BaseException:
            _close_fd(descriptor)
            for retained in reversed(directory_fds):
                _close_fd(retained)
            raise
        self._descriptor = descriptor
        self._directory_fds = directory_fds
        self._directory_names = directory_names
        self._directory_identities = captured_directories
        self._name = name
        self._bound_identity = bound_identity
        self._named_destination: str | None = None
        self._fault_injector = fault_injector
        self._flushed_facts: CandidateContentFacts | None = None

    def _reprove_owner(
        self,
        owner_fds: tuple[int, ...],
        owner_names: tuple[str | None, ...],
        owner_identities: tuple[FileObjectIdentity, ...],
    ) -> None:
        if not (
            self._directory_names == owner_names
            and len(self._directory_identities) == len(owner_identities)
            and all(
                _same_identity(left, right)
                for left, right in zip(
                    self._directory_identities,
                    owner_identities,
                    strict=True,
                )
            )
        ):
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        _reprove_directory_chain(owner_fds, owner_names, owner_identities)
        self._reprove_named_entry()

    def _reprove_named_entry(self) -> os.stat_result:
        try:
            _reprove_directory_chain(
                self._directory_fds,
                self._directory_names,
                self._directory_identities,
            )
            actual_stat = os.fstat(self._descriptor)
            actual_file = _identity_from_stat(actual_stat)
            entry_name = self._named_destination or self._name
            entry_stat = os.stat(
                entry_name,
                dir_fd=self._directory_fds[-1],
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_stat.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.REPARSE_REJECTED,
                    retryable=False,
                )
            entry_identity = _identity_from_stat(entry_stat)
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            ) from None
        if not (
            actual_file.kind == "regular"
            and actual_file.link_count == 1
            and entry_identity.link_count == 1
            and _same_identity(actual_file, self._bound_identity)
            and _same_identity(entry_identity, self._bound_identity)
        ):
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return actual_stat

    def _current_stat(self) -> os.stat_result:
        return self._reprove_named_entry()

    def _mark_named(self, destination: str) -> None:
        self._named_destination = destination

    def _write_chunks(self, chunks: Iterator[bytes]) -> CandidateContentFacts:
        self._reprove_named_entry()
        try:
            os.ftruncate(self._descriptor, 0)
            digest = hashlib.sha256()
            offset = 0
            for chunk in chunks:
                chunk_offset = 0
                while chunk_offset < len(chunk):
                    written = os.pwrite(
                        self._descriptor,
                        chunk[chunk_offset:],
                        offset,
                    )
                    if written <= 0:
                        raise OSError(errno.EIO, "short POSIX candidate write")
                    chunk_offset += written
                    offset += written
                digest.update(chunk)
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None
        proof = self._reprove_named_entry()
        facts = CandidateContentFacts(offset, digest.digest())
        if proof.st_size != facts.byte_count:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        self._flushed_facts = None
        return facts

    def _flush_content(self, expected: CandidateContentFacts) -> CandidateContentFacts:
        before = self._reprove_named_entry()
        if before.st_size != expected.byte_count:
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        try:
            os.fsync(self._descriptor)
            actual = _digest_fd_exact(self._descriptor, expected.byte_count)
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None
        after = self._reprove_named_entry()
        if (
            _snapshot_from_stat(before) != _snapshot_from_stat(after)
            or actual != expected
        ):
            raise _platform_error(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        self._flushed_facts = actual
        return actual

    def _publish_facts(self, mode: PublishMode) -> PublishFacts:
        if self._flushed_facts is None:
            raise _platform_error(
                PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            )
        before = self._reprove_named_entry()
        try:
            actual = _digest_fd_exact(self._descriptor, self._flushed_facts.byte_count)
        except OSError:
            raise _platform_error(
                PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            ) from None
        after = self._reprove_named_entry()
        if (
            _snapshot_from_stat(before) != _snapshot_from_stat(after)
            or actual != self._flushed_facts
        ):
            raise _platform_error(
                PlatformFileErrorCode.PUBLISH_FAILED,
                retryable=True,
            )
        return PublishFacts(
            mode=mode,
            destination_identity=_identity_from_stat(after),
            content_sha256=actual.content_sha256,
            byte_count=actual.byte_count,
            reparse_free=True,
        )

    def _identity(self) -> FileObjectIdentity:
        return _identity_from_stat(self._current_stat())

    def _close_authority(self) -> None:
        _close_fd(self._descriptor)
        for descriptor in reversed(self._directory_fds):
            _close_fd(descriptor)


class _PosixPendingPublication(PendingPublication):
    def _terminal_reproof(
        self,
        retained_destination: BoundRegularFile,
        preliminary_facts: PublishFacts,
    ) -> PublishFacts:
        if not isinstance(retained_destination, _PosixBoundRegularFile):
            raise _platform_error(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
        terminal = retained_destination._publish_facts(preliminary_facts.mode)
        if terminal != preliminary_facts:
            raise _platform_error(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
        return terminal


class _PosixLockLease(LockLease):
    __slots__ = (
        "_descriptor",
        "_directory_fds",
        "_directory_names",
        "_directory_identities",
        "_entry_name",
        "_entry_identity",
        "_payload",
    )

    def __init__(
        self,
        descriptor: int,
        directory_fds: tuple[int, ...],
        directory_names: tuple[str | None, ...],
        directory_identities: tuple[FileObjectIdentity, ...],
        entry_name: str,
        entry_identity: FileObjectIdentity,
        payload: bytes,
    ) -> None:
        super().__init__()
        self._descriptor = descriptor
        self._directory_fds = directory_fds
        self._directory_names = directory_names
        self._directory_identities = directory_identities
        self._entry_name = entry_name
        self._entry_identity = entry_identity
        self._payload = payload

    def _reprove_lock(self, *, check_payload: bool = True) -> None:
        try:
            _reprove_directory_chain(
                self._directory_fds,
                self._directory_names,
                self._directory_identities,
            )
            before = os.fstat(self._descriptor)
            before_identity = _identity_from_stat(before)
            entry = os.stat(
                self._entry_name,
                dir_fd=self._directory_fds[-1],
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
            entry_identity = _identity_from_stat(entry)
            payload = _read_fd_all(self._descriptor) if check_payload else None
            after = os.fstat(self._descriptor)
            after_identity = _identity_from_stat(after)
            if not (
                _same_identity(before_identity, self._entry_identity)
                and _same_identity(entry_identity, self._entry_identity)
                and _same_identity(after_identity, self._entry_identity)
                and before_identity.link_count == 1
                and entry_identity.link_count == 1
                and after_identity.link_count == 1
                and _modified_token(before) == _modified_token(after)
                and (not check_payload or payload == self._payload)
                and stat.S_IMODE(after.st_mode) == 0o600
            ):
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
        except PlatformFileError as error:
            raise _normalize_lock_platform_error(error) from None
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            ) from None

    def _matches_parent(
        self,
        owner_fds: tuple[int, ...],
        owner_names: tuple[str | None, ...],
        owner_identities: tuple[FileObjectIdentity, ...],
    ) -> bool:
        self._require_open()
        self._reprove_lock()
        _reprove_directory_chain(owner_fds, owner_names, owner_identities)
        return (
            self._directory_names == owner_names
            and len(self._directory_identities) == len(owner_identities)
            and all(
                _same_identity(left, right)
                for left, right in zip(
                    self._directory_identities,
                    owner_identities,
                    strict=True,
                )
            )
        )

    def _close_authority(self) -> None:
        unlock_error: PlatformFileError | None = None
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        except OSError:
            unlock_error = _platform_error(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            )
        finally:
            _close_fd(self._descriptor)
            for descriptor in reversed(self._directory_fds):
                _close_fd(descriptor)
        if unlock_error is not None:
            raise unlock_error from None


class _PosixPrivateEvidence(PrivateAccessEvidence):
    __slots__ = ("_descriptor", "_identity")

    def __init__(self, descriptor: int) -> None:
        super().__init__()
        try:
            identity = _identity_from_stat(os.fstat(descriptor))
        except BaseException:
            _close_fd(descriptor)
            raise
        self._descriptor = descriptor
        self._identity = identity

    def _close_authority(self) -> None:
        _close_fd(self._descriptor)


class PosixPlatformAdapter(
    RootedFileSystem,
    ExistingFileDurability,
    ProcessFileLock,
    PrivateStorageProof,
):
    """Injectable POSIX implementation of the shared low-level capabilities."""

    def __init__(self, *, _fault_injector: _FaultInjector | None = None) -> None:
        if _fault_injector is not None and not callable(_fault_injector):
            raise TypeError("_fault_injector must be callable")
        self._fault_injector = _fault_injector

    def _bind_root(self, root: Path) -> RootedDirectoryAuthority:
        descriptor: int | None = None
        try:
            descriptor = os.open(root, _DIRECTORY_FLAGS)
            transferred = descriptor
            descriptor = None
            return _PosixRootedDirectory(
                (transferred,),
                (None,),
                self._fault_injector,
            )
        except PlatformFileError:
            raise
        except OSError as error:
            if descriptor is not None:
                _close_fd(descriptor)
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            ) from None

    def _open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        if not isinstance(root, _PosixRootedDirectory):
            raise _platform_error(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        components = tuple(relative.parts)
        directories, directory_names = _open_directory_chain(
            root,
            components[:-1],
        )
        retained_directories: tuple[int, ...] | None = directories
        descriptor: int | None = None
        try:
            descriptor = os.open(
                components[-1],
                _READ_FLAGS,
                dir_fd=retained_directories[-1],
            )
            transferred_directories = retained_directories
            transferred_descriptor = descriptor
            retained_directories = None
            descriptor = None
            return _PosixBoundRegularFile(
                transferred_directories,
                directory_names,
                transferred_descriptor,
                components[-1],
                self._fault_injector,
            )
        except PlatformFileError:
            if descriptor is not None:
                _close_fd(descriptor)
            if retained_directories is not None:
                for item in reversed(retained_directories):
                    _close_fd(item)
            raise
        except OSError as error:
            if descriptor is not None:
                _close_fd(descriptor)
            if retained_directories is not None:
                for item in reversed(retained_directories):
                    _close_fd(item)
            if error.errno in {errno.ENOENT, errno.EACCES, errno.EPERM}:
                raise _platform_error(
                    PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                    retryable=False,
                ) from None
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            ) from None

    def _open_existing_for_synchronization(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundSynchronizedRegularFile:
        if not isinstance(root, _PosixRootedDirectory):
            raise _platform_error(
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                retryable=False,
            )
        components = tuple(relative.parts)
        directories, directory_names = _open_directory_chain(
            root,
            components[:-1],
        )
        retained_directories: tuple[int, ...] | None = directories
        descriptor: int | None = None
        try:
            descriptor = os.open(
                components[-1],
                _SYNCHRONIZED_READ_FLAGS,
                dir_fd=retained_directories[-1],
            )
            transferred_directories = retained_directories
            transferred_descriptor = descriptor
            retained_directories = None
            descriptor = None
            return _PosixBoundSynchronizedRegularFile(
                transferred_directories,
                directory_names,
                transferred_descriptor,
                components[-1],
                self._fault_injector,
            )
        except PlatformFileError:
            if descriptor is not None:
                _close_fd(descriptor)
            if retained_directories is not None:
                for item in reversed(retained_directories):
                    _close_fd(item)
            raise
        except OSError as error:
            if descriptor is not None:
                _close_fd(descriptor)
            if retained_directories is not None:
                for item in reversed(retained_directories):
                    _close_fd(item)
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                retryable=False,
            ) from None

    def _bind_parent(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundDirectoryAuthority:
        if not isinstance(root, _PosixRootedDirectory):
            raise _platform_error(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        directories, directory_names = _open_directory_chain(
            root,
            tuple(relative.parts[:-1]),
        )
        try:
            return _PosixBoundDirectory(
                directories,
                directory_names,
                self._fault_injector,
            )
        except PlatformFileError:
            raise
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            ) from None

    def _acquire(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease:
        if not isinstance(parent, _DirectoryAuthorityMixin):
            raise _platform_error(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            )
        descriptor: int | None = None
        lease_directory_fds: tuple[int, ...] | None = None
        lease: _PosixLockLease | None = None
        locked = False
        created = False
        try:
            parent._reprove()
            try:
                descriptor = os.open(
                    name,
                    _LOCK_FLAGS | os.O_EXCL,
                    0o600,
                    dir_fd=parent._directory_fd,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(
                    name,
                    os.O_RDWR
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent._directory_fd,
                )
            _hit_fault(self._fault_injector, "lock_before_initial_proof")
            descriptor_stat = os.fstat(descriptor)
            identity = _identity_from_stat(descriptor_stat)
            entry_stat = os.stat(
                name,
                dir_fd=parent._directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_stat.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
            entry_identity = _identity_from_stat(entry_stat)
            if not (
                identity.kind == "regular"
                and identity.link_count == 1
                and entry_identity.link_count == 1
                and _same_identity(identity, entry_identity)
            ):
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
            if created:
                os.fchmod(descriptor, 0o600)
                descriptor_stat = os.fstat(descriptor)
                identity = _identity_from_stat(descriptor_stat)
                entry_identity = _identity_from_stat(
                    os.stat(
                        name,
                        dir_fd=parent._directory_fd,
                        follow_symlinks=False,
                    )
                )
                if not (
                    identity.link_count == 1
                    and entry_identity.link_count == 1
                    and _same_identity(identity, entry_identity)
                ):
                    raise _platform_error(
                        PlatformFileErrorCode.LOCK_UNAVAILABLE,
                        retryable=False,
                    )
            elif stat.S_IMODE(descriptor_stat.st_mode) != 0o600:
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
            self._flock(descriptor, policy)
            locked = True
            lease_directory_fds = _duplicate_directory_chain(parent._directory_fds)
            lease = _PosixLockLease(
                descriptor,
                lease_directory_fds,
                parent._directory_names,
                parent._directory_identities,
                name,
                identity,
                payload,
            )
            descriptor = None
            lease_directory_fds = None
            _hit_fault(self._fault_injector, "lock_after_lease")
            lease._reprove_lock(check_payload=False)
            existing = _read_fd_all(lease._descriptor)
            if existing == b"":
                lease._reprove_lock(check_payload=False)
                if payload != b"":
                    _write_fd_all(lease._descriptor, payload)
                os.fsync(lease._descriptor)
                os.fsync(lease._directory_fds[-1])
                lease._reprove_lock()
            elif existing != payload:
                raise _platform_error(
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                )
            else:
                lease._reprove_lock()
            return lease
        except BaseException as error:
            if lease is not None:
                lease.close()
            if descriptor is not None:
                if locked:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                _close_fd(descriptor)
            if lease_directory_fds is not None:
                for retained in reversed(lease_directory_fds):
                    _close_fd(retained)
            if isinstance(error, PlatformFileError):
                raise _normalize_lock_platform_error(error) from None
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=False,
            ) from None

    @staticmethod
    def _flock(descriptor: int, policy: LockPolicy) -> None:
        if policy.wait is LockWait.BLOCK:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        if policy.wait is LockWait.FAIL_FAST:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise _platform_error(
                        PlatformFileErrorCode.LOCK_CONTENDED,
                        retryable=True,
                    ) from None
                raise _map_os_error(
                    error,
                    fallback=PlatformFileErrorCode.LOCK_UNAVAILABLE,
                    retryable=False,
                ) from None
            return

        assert policy.timeout_seconds is not None
        deadline = time.monotonic() + policy.timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                }:
                    raise _map_os_error(
                        error,
                        fallback=PlatformFileErrorCode.LOCK_UNAVAILABLE,
                        retryable=False,
                    ) from None
                if time.monotonic() >= deadline:
                    raise _platform_error(
                        PlatformFileErrorCode.LOCK_CONTENDED,
                        retryable=True,
                    ) from None
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def _create_private_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        if not isinstance(parent, _DirectoryAuthorityMixin):
            raise _platform_error(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )
        parent._reprove()
        descriptor: int | None = None
        duplicated_parent_fds: tuple[int, ...] | None = None
        chain: tuple[int, ...] | None = None
        created_identity: FileObjectIdentity | None = None
        try:
            os.mkdir(name, 0o700, dir_fd=parent._directory_fd)
            created_stat = os.stat(
                name,
                dir_fd=parent._directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(created_stat.st_mode):
                raise _platform_error(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            created_identity = _identity_from_stat(created_stat)
            if created_identity.kind != "directory":
                raise _platform_error(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            _hit_fault(self._fault_injector, "private_after_create")
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent._directory_fd)
            if not _same_identity(
                _identity_from_stat(os.fstat(descriptor)),
                created_identity,
            ):
                raise _platform_error(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            os.fsync(parent._directory_fd)
            duplicated_parent_fds = _duplicate_directory_chain(parent._directory_fds)
            directory_names = parent._directory_names + (name,)
            chain = duplicated_parent_fds + (descriptor,)
            duplicated_parent_fds = None
            descriptor = None
            _hit_fault(self._fault_injector, "private_before_authority_transfer")
            transferred_chain = chain
            chain = None
            return _PosixBoundDirectory(
                transferred_chain,
                directory_names,
                self._fault_injector,
            )
        except BaseException as error:
            if chain is not None:
                for retained in reversed(chain):
                    _close_fd(retained)
            if duplicated_parent_fds is not None:
                for retained in reversed(duplicated_parent_fds):
                    _close_fd(retained)
            if descriptor is not None:
                _close_fd(descriptor)
            cleanup_error: BaseException | None = None
            if created_identity is not None:
                try:
                    _remove_owned_empty_directory(
                        parent._directory_fd,
                        name,
                        created_identity,
                        self._fault_injector,
                        parent._reprove,
                    )
                except BaseException as caught:
                    cleanup_error = caught
            if cleanup_error is not None:
                raise _platform_error(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                ) from None
            if isinstance(error, PlatformFileError):
                raise error
            if not isinstance(error, Exception):
                raise
            raise _map_os_error(
                error if isinstance(error, OSError) else OSError(errno.EIO, "fault"),
                fallback=PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            ) from None

    def _prove_private(
        self,
        authority: BoundDirectoryAuthority | BoundRegularFile,
    ) -> PrivateAccessEvidence:
        if isinstance(authority, _DirectoryAuthorityMixin):
            authority._reprove()
            source_fd = authority._directory_fd
        elif isinstance(authority, _PosixBoundRegularFile):
            authority._reprove()
            source_fd = authority._descriptor
        else:
            raise _platform_error(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )
        try:
            result = os.fstat(source_fd)
            if result.st_uid != os.geteuid() or stat.S_IMODE(result.st_mode) & 0o077:
                raise _platform_error(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            if stat.S_ISREG(result.st_mode) and result.st_nlink != 1:
                raise _platform_error(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            return _PosixPrivateEvidence(_duplicate_fd(source_fd))
        except PlatformFileError:
            raise
        except OSError as error:
            raise _map_os_error(
                error,
                fallback=PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            ) from None
