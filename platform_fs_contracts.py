"""Backend-neutral contracts for rooted file authority, locking, and publish."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Protocol, runtime_checkable


class PlatformFileErrorCode(str, Enum):
    CAPABILITY_UNAVAILABLE = "PLATFORM.FS.CAPABILITY_UNAVAILABLE"
    OUTSIDE_ROOT = "PLATFORM.FS.OUTSIDE_ROOT"
    REPARSE_REJECTED = "PLATFORM.FS.REPARSE_REJECTED"
    IDENTITY_STALE = "PLATFORM.FS.IDENTITY_STALE"
    LOCK_CONTENDED = "PLATFORM.FS.LOCK_CONTENDED"
    LOCK_UNAVAILABLE = "PLATFORM.FS.LOCK_UNAVAILABLE"
    PRIVATE_STORAGE_UNPROVEN = "PLATFORM.FS.PRIVATE_STORAGE_UNPROVEN"
    DURABILITY_UNAVAILABLE = "PLATFORM.FS.DURABILITY_UNAVAILABLE"
    PUBLISH_FAILED = "PLATFORM.FS.PUBLISH_FAILED"
    RECOVERY_REQUIRED = "PLATFORM.FS.RECOVERY_REQUIRED"


_FIXED_RETRYABILITY = {
    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE: False,
    PlatformFileErrorCode.OUTSIDE_ROOT: False,
    PlatformFileErrorCode.REPARSE_REJECTED: False,
    PlatformFileErrorCode.IDENTITY_STALE: True,
    PlatformFileErrorCode.LOCK_CONTENDED: True,
    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN: False,
    PlatformFileErrorCode.DURABILITY_UNAVAILABLE: False,
    PlatformFileErrorCode.PUBLISH_FAILED: True,
    PlatformFileErrorCode.RECOVERY_REQUIRED: True,
}


class PlatformFileError(RuntimeError):
    """Body-free platform failure carrying a stable code and retry fact."""

    __slots__ = ("__code", "__retryable")

    def __init__(
        self,
        code: PlatformFileErrorCode,
        *,
        retryable: bool,
    ) -> None:
        if type(code) is not PlatformFileErrorCode:
            raise TypeError("code must be exact PlatformFileErrorCode")
        if type(retryable) is not bool:
            raise TypeError("retryable must be exact bool")
        fixed_retryability = _FIXED_RETRYABILITY.get(code)
        if fixed_retryability is not None and retryable is not fixed_retryability:
            raise ValueError("retryable contradicts the stable platform error contract")
        object.__setattr__(self, "_PlatformFileError__code", code.value)
        object.__setattr__(self, "_PlatformFileError__retryable", retryable)
        super().__init__(code.value)

    @property
    def code(self) -> str:
        return self.__code

    @property
    def retryable(self) -> bool:
        return self.__retryable

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("PlatformFileError is immutable")

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            raise AttributeError("PlatformFileError has no public attribute dictionary")
        return super().__getattribute__(name)

    def add_note(self, note: str) -> None:
        del note
        raise TypeError("PlatformFileError does not accept diagnostic body text")


class _LiveOnlyValue:
    __slots__ = ()

    def __reduce__(self) -> object:
        raise TypeError(f"{type(self).__name__} is live-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError(f"{type(self).__name__} is live-only")

    def __copy__(self) -> object:
        raise TypeError(f"{type(self).__name__} is live-only")

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise TypeError(f"{type(self).__name__} is live-only")


@dataclass(frozen=True, slots=True)
class FileObjectIdentity(_LiveOnlyValue):
    """Opaque physical identity for comparison while verified authorities live."""

    platform: str
    volume_id: bytes
    file_id: bytes
    kind: str
    link_count: int

    def __post_init__(self) -> None:
        if type(self.platform) is not str:
            raise TypeError("platform must be exact str")
        if self.platform not in {"posix", "windows"}:
            raise ValueError("platform is unsupported")
        for name, value in (
            ("volume_id", self.volume_id),
            ("file_id", self.file_id),
        ):
            if type(value) is not bytes:
                raise TypeError(f"{name} must be exact bytes")
            if not value:
                raise ValueError(f"{name} must be non-empty")
        if self.platform == "windows" and len(self.file_id) != 16:
            raise ValueError("windows file_id must be exactly 128 bits")
        if type(self.kind) is not str:
            raise TypeError("kind must be exact str")
        if self.kind not in {"directory", "regular"}:
            raise ValueError("kind is unsupported")
        if type(self.link_count) is not int:
            raise TypeError("link_count must be exact int")
        if self.link_count < 1:
            raise ValueError("link_count must be positive")


@dataclass(frozen=True, slots=True)
class EntrySnapshot(_LiveOnlyValue):
    identity: FileObjectIdentity
    byte_count: int
    modified_token: bytes
    reparse_free: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not FileObjectIdentity:
            raise TypeError("identity must be exact FileObjectIdentity")
        if type(self.byte_count) is not int:
            raise TypeError("byte_count must be exact int")
        if self.byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        if type(self.modified_token) is not bytes:
            raise TypeError("modified_token must be exact bytes")
        if not self.modified_token:
            raise ValueError("modified_token must be non-empty")
        if type(self.reparse_free) is not bool:
            raise TypeError("reparse_free must be exact bool")


class PublishMode(str, Enum):
    CREATE_IF_ABSENT = "create_if_absent"
    REPLACE_UNDER_LOCK = "replace_under_lock"


@dataclass(frozen=True, slots=True)
class PublishFacts(_LiveOnlyValue):
    """Low-level naming/readback facts; never a business success receipt."""

    mode: PublishMode
    destination_identity: FileObjectIdentity
    content_sha256: bytes
    byte_count: int
    reparse_free: bool

    def __post_init__(self) -> None:
        if type(self.mode) is not PublishMode:
            raise TypeError("mode must be exact PublishMode")
        if type(self.destination_identity) is not FileObjectIdentity:
            raise TypeError("destination_identity must be exact FileObjectIdentity")
        if type(self.content_sha256) is not bytes:
            raise TypeError("content_sha256 must be exact bytes")
        if len(self.content_sha256) != 32:
            raise ValueError("content_sha256 must contain exactly 32 bytes")
        if type(self.byte_count) is not int:
            raise TypeError("byte_count must be exact int")
        if self.byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        if type(self.reparse_free) is not bool:
            raise TypeError("reparse_free must be exact bool")
        if not self.reparse_free:
            raise ValueError("published destination facts must be reparse-free")


class LockWait(str, Enum):
    FAIL_FAST = "fail_fast"
    BLOCK = "block"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class LockPolicy:
    wait: LockWait
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if type(self.wait) is not LockWait:
            raise TypeError("wait must be exact LockWait")
        if self.wait is LockWait.TIMEOUT:
            if type(self.timeout_seconds) is not float:
                raise TypeError("timeout_seconds must be exact float for timeout policy")
            if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
                raise ValueError("timeout_seconds must be positive and finite")
        elif self.timeout_seconds is not None:
            raise ValueError("timeout_seconds is only valid for timeout policy")


def validate_relative_name(name: str) -> str:
    """Validate one backend-neutral relative entry name without normalizing it."""

    if type(name) is not str:
        raise TypeError("relative name must be exact str")
    if not name or name in {".", ".."}:
        raise ValueError("relative name is empty or reserved")
    if "\0" in name or "/" in name or "\\" in name:
        raise ValueError("relative name must be exactly one safe component")
    return name


def validate_relative_path(relative: PurePath) -> PurePath:
    """Validate a non-empty backend-neutral relative path without resolving it."""

    if type(relative) not in {PurePosixPath, PureWindowsPath}:
        raise TypeError("relative path must be an exact pathlib pure path")
    if relative.is_absolute() or relative.anchor or not relative.parts:
        raise ValueError("relative path must be non-empty and unanchored")
    for part in relative.parts:
        validate_relative_name(part)
    return relative


def validate_root_path(root: Path) -> Path:
    """Validate the neutral shape of a root before a backend proves it."""

    if not isinstance(root, Path):
        raise TypeError("root must be a concrete pathlib Path")
    if not root.is_absolute():
        raise ValueError("root must be absolute")
    return root


class OpaqueAuthority(ABC):
    """Non-copyable context-managed authority with one-way close semantics."""

    __slots__ = ("__closed",)

    def __init__(self) -> None:
        self.__closed = False

    @property
    def closed(self) -> bool:
        return self.__closed

    def _require_open(self) -> None:
        if self.__closed:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )

    def close(self) -> None:
        if self.__closed:
            return
        try:
            self._close_authority()
        finally:
            self.__closed = True

    @abstractmethod
    def _close_authority(self) -> None:
        """Release the backend-owned authority without exposing its representation."""

    def __enter__(self) -> OpaqueAuthority:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def __reduce__(self) -> object:
        raise TypeError(f"{type(self).__name__} is an opaque authority")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError(f"{type(self).__name__} is an opaque authority")

    def __copy__(self) -> object:
        raise TypeError(f"{type(self).__name__} is an opaque authority")

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise TypeError(f"{type(self).__name__} is an opaque authority")


class BoundRegularFile(OpaqueAuthority, ABC):
    def read_all(self) -> bytes:
        self._require_open()
        payload = self._read_all()
        if type(payload) is not bytes:
            raise TypeError("backend read_all must return exact bytes")
        return payload

    @abstractmethod
    def _read_all(self) -> bytes: ...

    def identity(self) -> FileObjectIdentity:
        self._require_open()
        identity = self._identity()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("backend identity must return exact FileObjectIdentity")
        return identity

    @abstractmethod
    def _identity(self) -> FileObjectIdentity: ...

    def snapshot(self) -> EntrySnapshot:
        self._require_open()
        snapshot = self._snapshot()
        if type(snapshot) is not EntrySnapshot:
            raise TypeError("backend snapshot must return exact EntrySnapshot")
        return snapshot

    @abstractmethod
    def _snapshot(self) -> EntrySnapshot: ...


class CandidateFile(OpaqueAuthority, ABC):
    __slots__ = ("__has_content", "__flushed")

    def __init__(self) -> None:
        super().__init__()
        self.__has_content = False
        self.__flushed = False

    def write_all(self, payload: bytes) -> None:
        self._require_open()
        if type(payload) is not bytes:
            raise TypeError("payload must be exact bytes")
        result = self._write_all(payload)
        if result is not None:
            raise TypeError("backend write_all must return None")
        self.__has_content = True
        self.__flushed = False

    @abstractmethod
    def _write_all(self, payload: bytes) -> None: ...

    def flush_content(self) -> None:
        self._require_open()
        if not self.__has_content:
            raise ValueError("candidate content must be written before flush")
        result = self._flush_content()
        if result is not None:
            raise TypeError("backend flush_content must return None")
        self.__flushed = True

    @abstractmethod
    def _flush_content(self) -> None: ...

    def identity(self) -> FileObjectIdentity:
        self._require_open()
        identity = self._identity()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("backend identity must return exact FileObjectIdentity")
        return identity

    @abstractmethod
    def _identity(self) -> FileObjectIdentity: ...

    def _require_publishable(self) -> None:
        self._require_open()
        if not self.__has_content or not self.__flushed:
            raise ValueError("candidate must contain flushed content before publish")


class LockLease(OpaqueAuthority, ABC):
    pass


class PendingPublication(OpaqueAuthority, ABC):
    __slots__ = ("__preliminary_facts", "__retained_destination")

    def __init__(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> None:
        super().__init__()
        if type(preliminary_facts) is not PublishFacts:
            raise TypeError("preliminary facts must be exact PublishFacts")
        if not isinstance(retained_destination, BoundRegularFile):
            raise TypeError("retained destination must be BoundRegularFile")
        retained_destination._require_open()
        self.__preliminary_facts = preliminary_facts
        self.__retained_destination = retained_destination

    def preliminary_facts(self) -> PublishFacts:
        self._require_open()
        self._require_retained_destination_open()
        return self.__preliminary_facts

    def retained_destination(self) -> BoundRegularFile:
        self._require_open()
        return self._require_retained_destination_open()

    def terminal_reproof(self) -> PublishFacts:
        self._require_open()
        retained_destination = self._require_retained_destination_open()
        facts = self._terminal_reproof(
            retained_destination,
            self.__preliminary_facts,
        )
        if type(facts) is not PublishFacts:
            raise TypeError("backend terminal facts must be exact PublishFacts")
        return facts

    @abstractmethod
    def _terminal_reproof(
        self,
        retained_destination: BoundRegularFile,
        preliminary_facts: PublishFacts,
    ) -> PublishFacts: ...

    def _require_retained_destination_open(self) -> BoundRegularFile:
        self.__retained_destination._require_open()
        return self.__retained_destination

    def _owns_retained_destination(self, authority: BoundRegularFile) -> bool:
        return self.__retained_destination is authority

    def _close_authority(self) -> None:
        try:
            self._close_publication()
        finally:
            self.__retained_destination.close()

    def _close_publication(self) -> None:
        """Optional backend cleanup beyond closing the retained destination."""


class PrivateAccessEvidence(OpaqueAuthority, ABC):
    pass


class BoundDirectoryAuthority(OpaqueAuthority, ABC):
    def reprove(self) -> None:
        self._require_open()
        result = self._reprove()
        if result is not None:
            raise TypeError("backend reprove must return None")

    @abstractmethod
    def _reprove(self) -> None: ...

    def inspect_entry(self, name: str) -> EntrySnapshot | None:
        self._require_open()
        checked_name = validate_relative_name(name)
        snapshot = self._inspect_entry(checked_name)
        if snapshot is not None and type(snapshot) is not EntrySnapshot:
            raise TypeError("backend inspect_entry must return EntrySnapshot or None")
        return snapshot

    @abstractmethod
    def _inspect_entry(self, name: str) -> EntrySnapshot | None: ...

    def create_candidate(self, name: str, *, private: bool) -> CandidateFile:
        self._require_open()
        checked_name = validate_relative_name(name)
        if type(private) is not bool:
            raise TypeError("private must be exact bool")
        candidate = self._create_candidate(checked_name, private=private)
        if not isinstance(candidate, CandidateFile):
            raise TypeError("backend create_candidate must return CandidateFile")
        candidate._require_open()
        return candidate

    @abstractmethod
    def _create_candidate(self, name: str, *, private: bool) -> CandidateFile: ...

    def begin_publish(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PendingPublication:
        self._require_open()
        if not isinstance(candidate, CandidateFile):
            raise TypeError("candidate must be CandidateFile")
        candidate._require_publishable()
        checked_destination = validate_relative_name(destination)
        if type(mode) is not PublishMode:
            raise TypeError("mode must be exact PublishMode")
        if mode is PublishMode.CREATE_IF_ABSENT:
            if lease is not None:
                raise ValueError("create-if-absent must not consume a replacement lease")
        else:
            if not isinstance(lease, LockLease):
                raise ValueError("replace-under-lock requires an open LockLease")
            lease._require_open()
        try:
            preliminary_facts = self._rename_candidate(
                candidate,
                checked_destination,
                mode=mode,
                lease=lease,
            )
        finally:
            candidate.close()
        if type(preliminary_facts) is not PublishFacts:
            raise TypeError("backend rename must return exact PublishFacts")
        if preliminary_facts.mode is not mode:
            raise ValueError("backend preliminary facts contradict publish mode")

        retained_destination = self._open_published_destination(
            checked_destination,
            preliminary_facts,
        )
        if not isinstance(retained_destination, BoundRegularFile):
            raise TypeError("backend reopen must return BoundRegularFile")
        retained_destination._require_open()
        pending: object | None = None
        try:
            pending = self._create_pending_publication(
                preliminary_facts,
                retained_destination,
            )
            if not isinstance(pending, PendingPublication):
                raise TypeError("backend pending factory must return PendingPublication")
            pending._require_open()
            if not pending._owns_retained_destination(retained_destination):
                raise ValueError("pending publication must own the exact retained destination")
        except BaseException:
            try:
                if isinstance(pending, PendingPublication):
                    pending.close()
            finally:
                retained_destination.close()
            raise
        return pending

    @abstractmethod
    def _rename_candidate(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PublishFacts: ...

    @abstractmethod
    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: PublishFacts,
    ) -> BoundRegularFile: ...

    @abstractmethod
    def _create_pending_publication(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> PendingPublication: ...

    def unlink_owned(self, name: str, expected: FileObjectIdentity) -> None:
        self._require_open()
        checked_name = validate_relative_name(name)
        if type(expected) is not FileObjectIdentity:
            raise TypeError("expected must be exact FileObjectIdentity")
        result = self._unlink_owned(checked_name, expected)
        if result is not None:
            raise TypeError("backend unlink_owned must return None")

    @abstractmethod
    def _unlink_owned(self, name: str, expected: FileObjectIdentity) -> None: ...


class RootedDirectoryAuthority(BoundDirectoryAuthority, ABC):
    pass


@runtime_checkable
class RootedFileSystem(Protocol):
    def bind_root(self, root: Path) -> RootedDirectoryAuthority:
        checked_root = validate_root_path(root)
        authority = self._bind_root(checked_root)
        if not isinstance(authority, RootedDirectoryAuthority):
            raise TypeError("backend bind_root must return RootedDirectoryAuthority")
        authority._require_open()
        return authority

    @abstractmethod
    def _bind_root(self, root: Path) -> RootedDirectoryAuthority: ...

    def open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        root._require_open()
        checked_relative = validate_relative_path(relative)
        authority = self._open_regular(root, checked_relative)
        if not isinstance(authority, BoundRegularFile):
            raise TypeError("backend open_regular must return BoundRegularFile")
        authority._require_open()
        return authority

    @abstractmethod
    def _open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile: ...

    def bind_parent(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundDirectoryAuthority:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        root._require_open()
        checked_relative = validate_relative_path(relative)
        authority = self._bind_parent(root, checked_relative)
        if not isinstance(authority, BoundDirectoryAuthority):
            raise TypeError("backend bind_parent must return BoundDirectoryAuthority")
        authority._require_open()
        return authority

    @abstractmethod
    def _bind_parent(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundDirectoryAuthority: ...


@runtime_checkable
class ProcessFileLock(Protocol):
    def acquire(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be BoundDirectoryAuthority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        if type(payload) is not bytes:
            raise TypeError("lock payload must be exact bytes")
        if type(policy) is not LockPolicy:
            raise TypeError("policy must be exact LockPolicy")
        lease = self._acquire(parent, checked_name, payload, policy)
        if not isinstance(lease, LockLease):
            raise TypeError("backend acquire must return LockLease")
        lease._require_open()
        return lease

    @abstractmethod
    def _acquire(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease: ...


@runtime_checkable
class PrivateStorageProof(Protocol):
    def create_private_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be BoundDirectoryAuthority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        authority = self._create_private_directory(parent, checked_name)
        if not isinstance(authority, BoundDirectoryAuthority):
            raise TypeError("backend private directory must be BoundDirectoryAuthority")
        authority._require_open()
        return authority

    @abstractmethod
    def _create_private_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority: ...

    def prove_private(
        self,
        authority: BoundDirectoryAuthority | BoundRegularFile,
    ) -> PrivateAccessEvidence:
        if not isinstance(authority, (BoundDirectoryAuthority, BoundRegularFile)):
            raise TypeError("authority must be a bound file or directory authority")
        authority._require_open()
        evidence = self._prove_private(authority)
        if not isinstance(evidence, PrivateAccessEvidence):
            raise TypeError("backend prove_private must return PrivateAccessEvidence")
        evidence._require_open()
        return evidence

    @abstractmethod
    def _prove_private(
        self,
        authority: BoundDirectoryAuthority | BoundRegularFile,
    ) -> PrivateAccessEvidence: ...


@runtime_checkable
class PlatformFileBackend(
    RootedFileSystem,
    ProcessFileLock,
    PrivateStorageProof,
    Protocol,
):
    """Aggregate capability implemented by every composed platform backend."""

    pass
