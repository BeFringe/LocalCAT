"""Backend-neutral contracts for rooted file authority, locking, and publish."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import threading
from typing import Callable, Iterable, Iterator, Protocol, runtime_checkable


class PlatformFileErrorCode(str, Enum):
    CAPABILITY_UNAVAILABLE = "PLATFORM.FS.CAPABILITY_UNAVAILABLE"
    ENTRY_UNAVAILABLE = "PLATFORM.FS.ENTRY_UNAVAILABLE"
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
    PlatformFileErrorCode.ENTRY_UNAVAILABLE: False,
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
        if name in {"__traceback__", "__cause__", "__context__"}:
            BaseException.__setattr__(self, name, value)
            return
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


@dataclass(frozen=True, slots=True)
class BoundContentFacts(_LiveOnlyValue):
    """Exact bytes observed through one live rooted file authority."""

    snapshot: EntrySnapshot
    content_sha256: bytes

    def __post_init__(self) -> None:
        if type(self.snapshot) is not EntrySnapshot:
            raise TypeError("snapshot must be exact EntrySnapshot")
        if type(self.content_sha256) is not bytes:
            raise TypeError("content_sha256 must be exact bytes")
        if len(self.content_sha256) != hashlib.sha256().digest_size:
            raise ValueError("content_sha256 must be an exact SHA-256 digest")


@dataclass(frozen=True, slots=True)
class BoundContentCapture(_LiveOnlyValue):
    """Materialized bytes and facts from one exact live-handle capture."""

    content: bytes
    facts: BoundContentFacts

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise TypeError("content must be exact bytes")
        if type(self.facts) is not BoundContentFacts:
            raise TypeError("facts must be exact BoundContentFacts")
        if self.facts.snapshot.byte_count != len(self.content):
            raise ValueError("content byte count contradicts captured facts")
        if hashlib.sha256(self.content).digest() != self.facts.content_sha256:
            raise ValueError("content digest contradicts captured facts")


@dataclass(frozen=True, slots=True)
class CandidateContentFacts:
    byte_count: int
    content_sha256: bytes

    def __post_init__(self) -> None:
        if type(self.byte_count) is not int:
            raise TypeError("byte_count must be exact int")
        if self.byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        if type(self.content_sha256) is not bytes:
            raise TypeError("content_sha256 must be exact bytes")
        if len(self.content_sha256) != hashlib.sha256().digest_size:
            raise ValueError("content_sha256 must be an exact SHA-256 digest")


@dataclass(frozen=True, slots=True)
class LedgerEnumerationLimits:
    maximum_entries: int
    maximum_name_bytes: int
    maximum_total_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("maximum_entries", self.maximum_entries),
            ("maximum_name_bytes", self.maximum_name_bytes),
            ("maximum_total_bytes", self.maximum_total_bytes),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be exact int")
        if self.maximum_entries < 1 or self.maximum_entries > 4096:
            raise ValueError("maximum_entries must be from 1 through 4096")
        if self.maximum_name_bytes < 1 or self.maximum_name_bytes > 4096:
            raise ValueError("maximum_name_bytes must be from 1 through 4096")
        if self.maximum_total_bytes < 0 or self.maximum_total_bytes > (1 << 63) - 1:
            raise ValueError("maximum_total_bytes is outside the supported range")


@dataclass(frozen=True, slots=True)
class LedgerEntryObservation(_LiveOnlyValue):
    """One bounded direct-child observation; it is not an entry authority or CAS."""

    name: str
    snapshot: EntrySnapshot

    def __post_init__(self) -> None:
        validate_relative_name(self.name)
        if type(self.snapshot) is not EntrySnapshot:
            raise TypeError("snapshot must be exact EntrySnapshot")
        if not (
            self.snapshot.identity.kind == "regular"
            and self.snapshot.identity.link_count == 1
            and self.snapshot.reparse_free
        ):
            raise ValueError("ledger observation must be direct regular single-link")


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


class OutputLockFinishResult(str, Enum):
    """Stable, body-free result of optional output-control lock retirement."""

    RETIRED = "retired"
    ABSENT = "absent"
    IN_USE = "in_use"
    NOT_PROVEN = "not_proven"


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


WINDOWS_PRIVATE_PROOF_SCHEMA = 1
WINDOWS_PRIVATE_SECURITY_PROFILE_ID = "WindowsPrivateSecurityV2"
WINDOWS_PRIVATE_PROOF_DOMAIN_TAG = b"localcat.windows.private-proof.v1\0"
DEVICE_SECRET_SIZE_BYTES = 32
DEVICE_KEY_ID_HASH_ALGORITHM = "sha256"
DEVICE_KEY_ID_SIZE_BYTES = 32
PRIVATE_PROOF_DIGEST_SIZE_BYTES = 32
WINDOWS_PRIVATE_PROOF_MAX_BYTES = 4096


class PrivateProofObjectRole(str, Enum):
    PRIVATE_DIRECTORY = "PRIVATE_DIRECTORY"
    DEVICE_KEY = "DEVICE_KEY"
    ATTESTATION = "ATTESTATION"
    DEVICE_KEY_CANDIDATE = "DEVICE_KEY_CANDIDATE"
    ATTESTATION_CANDIDATE = "ATTESTATION_CANDIDATE"


def _require_sha256_bytes(value: object, field_name: str) -> bytes:
    if type(value) is not bytes:
        raise TypeError(f"{field_name} must be exact bytes")
    if len(value) != PRIVATE_PROOF_DIGEST_SIZE_BYTES:
        raise ValueError(f"{field_name} must contain exactly 32 bytes")
    return value


def derive_device_key_id(device_secret: bytes) -> bytes:
    """Derive the persistent key id from exact raw device-secret bytes."""

    if type(device_secret) is not bytes:
        raise TypeError("device_secret must be exact bytes")
    if len(device_secret) != DEVICE_SECRET_SIZE_BYTES:
        raise ValueError("device_secret must contain exactly 32 bytes")
    return hashlib.sha256(device_secret).digest()


@dataclass(frozen=True, slots=True)
class PrivateProofContext:
    """Owner-selected role and digest of bytes canonicalized by that owner."""

    object_role: PrivateProofObjectRole
    owner_context_sha256: bytes

    def __post_init__(self) -> None:
        if type(self.object_role) is not PrivateProofObjectRole:
            raise TypeError("object_role must be exact PrivateProofObjectRole")
        _require_sha256_bytes(self.owner_context_sha256, "owner_context_sha256")


@dataclass(frozen=True, slots=True)
class WindowsPrivateProof:
    """Untrusted persistent Windows private-proof value, never live authority."""

    schema: int
    object_role: PrivateProofObjectRole
    security_profile_id: str
    owner_sid_sha256: bytes
    authority_descriptor_sha256: bytes
    owner_context_sha256: bytes
    device_key_id: bytes
    device_secret_mac: bytes

    def __post_init__(self) -> None:
        if type(self.schema) is not int:
            raise TypeError("schema must be exact int")
        if self.schema != WINDOWS_PRIVATE_PROOF_SCHEMA:
            raise ValueError("private proof schema is unsupported")
        if type(self.object_role) is not PrivateProofObjectRole:
            raise TypeError("object_role must be exact PrivateProofObjectRole")
        if type(self.security_profile_id) is not str:
            raise TypeError("security_profile_id must be exact str")
        if self.security_profile_id != WINDOWS_PRIVATE_SECURITY_PROFILE_ID:
            raise ValueError("private proof security profile is unsupported")
        for field_name in (
            "owner_sid_sha256",
            "authority_descriptor_sha256",
            "owner_context_sha256",
            "device_key_id",
            "device_secret_mac",
        ):
            _require_sha256_bytes(getattr(self, field_name), field_name)


_WINDOWS_PRIVATE_PROOF_FIELDS = frozenset(
    {
        "schema",
        "object_role",
        "security_profile_id",
        "owner_sid_sha256",
        "authority_descriptor_sha256",
        "owner_context_sha256",
        "device_key_id",
        "device_secret_mac",
    }
)


def _windows_private_proof_unsigned_payload(
    proof: WindowsPrivateProof,
) -> dict[str, object]:
    if type(proof) is not WindowsPrivateProof:
        raise TypeError("proof must be exact WindowsPrivateProof")
    return {
        "schema": proof.schema,
        "object_role": proof.object_role.value,
        "security_profile_id": proof.security_profile_id,
        "owner_sid_sha256": proof.owner_sid_sha256.hex(),
        "authority_descriptor_sha256": proof.authority_descriptor_sha256.hex(),
        "owner_context_sha256": proof.owner_context_sha256.hex(),
        "device_key_id": proof.device_key_id.hex(),
    }


def _canonical_private_proof_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_windows_private_proof_unsigned(proof: WindowsPrivateProof) -> bytes:
    """Encode the exact MAC projection, excluding only device_secret_mac."""

    return _canonical_private_proof_json(
        _windows_private_proof_unsigned_payload(proof)
    )


def windows_private_proof_mac_message(proof: WindowsPrivateProof) -> bytes:
    """Frame the domain tag and canonical unsigned projection for HMAC."""

    return WINDOWS_PRIVATE_PROOF_DOMAIN_TAG + encode_windows_private_proof_unsigned(proof)


def encode_windows_private_proof(proof: WindowsPrivateProof) -> bytes:
    """Encode one strict nested proof mapping without an owner envelope or LF."""

    payload = _windows_private_proof_unsigned_payload(proof)
    payload["device_secret_mac"] = proof.device_secret_mac.hex()
    return _canonical_private_proof_json(payload)


def _decode_sha256_hex(value: object, field_name: str) -> bytes:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exact str")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return bytes.fromhex(value)


def decode_windows_private_proof(serialized: bytes) -> WindowsPrivateProof:
    """Decode only the exact canonical nested Windows private-proof mapping."""

    if type(serialized) is not bytes:
        raise TypeError("serialized proof must be exact bytes")
    if len(serialized) > WINDOWS_PRIVATE_PROOF_MAX_BYTES:
        raise ValueError("serialized private proof exceeds the input limit")

    class _DuplicateKeyError(ValueError):
        pass

    def reject_non_finite(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number is not allowed")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key, value in pairs:
            if key in mapping:
                raise _DuplicateKeyError("duplicate JSON key")
            mapping[key] = value
        return mapping

    try:
        decoded = serialized.decode("utf-8")
        value = json.loads(
            decoded,
            parse_constant=reject_non_finite,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValueError("serialized private proof is not strict JSON") from error
    if type(value) is not dict or set(value) != _WINDOWS_PRIVATE_PROOF_FIELDS:
        raise ValueError("serialized private proof fields are invalid")
    role_value = value["object_role"]
    if type(role_value) is not str:
        raise TypeError("object_role must be exact str")
    try:
        role = PrivateProofObjectRole(role_value)
    except ValueError:
        raise ValueError("object_role is unsupported") from None
    proof = WindowsPrivateProof(
        schema=value["schema"],  # type: ignore[arg-type]
        object_role=role,
        security_profile_id=value["security_profile_id"],  # type: ignore[arg-type]
        owner_sid_sha256=_decode_sha256_hex(
            value["owner_sid_sha256"],
            "owner_sid_sha256",
        ),
        authority_descriptor_sha256=_decode_sha256_hex(
            value["authority_descriptor_sha256"],
            "authority_descriptor_sha256",
        ),
        owner_context_sha256=_decode_sha256_hex(
            value["owner_context_sha256"],
            "owner_context_sha256",
        ),
        device_key_id=_decode_sha256_hex(value["device_key_id"], "device_key_id"),
        device_secret_mac=_decode_sha256_hex(
            value["device_secret_mac"],
            "device_secret_mac",
        ),
    )
    if encode_windows_private_proof(proof) != serialized:
        raise ValueError("serialized private proof is not canonical")
    return proof


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

    if type(root) is not type(Path()):
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

    def _mark_authority_transferred(self) -> None:
        """Transfer close ownership without invoking the backend close hook."""

        self._require_open()
        self.__closed = True

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


class MutableFileReservation(OpaqueAuthority, ABC):
    """CREATE_NEW ownership retained while an external writer mutates the file."""

    __slots__ = ("__created_identity",)

    def __init__(self, created_identity: FileObjectIdentity) -> None:
        super().__init__()
        if type(created_identity) is not FileObjectIdentity:
            raise TypeError("created_identity must be exact FileObjectIdentity")
        if created_identity.kind != "regular" or created_identity.link_count != 1:
            raise ValueError("mutable reservation requires one regular-file link")
        self.__created_identity = created_identity

    def identity(self) -> FileObjectIdentity:
        """Reprove the retained file, origin parent and origin name as one object."""

        self._require_open()
        identity = self._reprove_identity()
        if type(identity) is not FileObjectIdentity:
            raise TypeError(
                "backend reservation reproof must return exact FileObjectIdentity"
            )
        if (
            identity != self.__created_identity
            or identity.kind != "regular"
            or identity.link_count != 1
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return identity

    @abstractmethod
    def _reprove_identity(self) -> FileObjectIdentity: ...


class RetirementSourceDirectoryAuthority(OpaqueAuthority, ABC):
    """Rooted, rename-compatible authority for one exact source parent/name."""

    def reprove(self) -> None:
        self._require_open()
        result = self._reprove_parent()
        if result is not None:
            raise TypeError("backend source-directory reproof must return None")

    @abstractmethod
    def _reprove_parent(self) -> None: ...


class ExistingRetirementSource(OpaqueAuthority, ABC):
    """Live, rooted authority for retiring one already-existing file.

    Unlike ``MutableFileReservation`` this authority does not claim creator
    ownership.  It proves exact portable content through a handle opened with
    the platform's rename/delete access and is consumed only by the dedicated
    existing-file retirement protocol.
    """

    __slots__ = ("__identity", "__expected_content")

    def __init__(
        self,
        identity: FileObjectIdentity,
        expected_content: CandidateContentFacts,
    ) -> None:
        super().__init__()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("identity must be exact FileObjectIdentity")
        if identity.kind != "regular" or identity.link_count != 1:
            raise ValueError("retirement source requires one regular-file link")
        if type(expected_content) is not CandidateContentFacts:
            raise TypeError("expected_content must be exact CandidateContentFacts")
        self.__identity = identity
        self.__expected_content = expected_content

    def reprove(self) -> BoundContentFacts:
        self._require_open()
        facts = self._reprove_source()
        if type(facts) is not BoundContentFacts:
            raise TypeError("backend source reproof must return exact BoundContentFacts")
        if (
            facts.snapshot.identity != self.__identity
            or facts.snapshot.identity.kind != "regular"
            or facts.snapshot.identity.link_count != 1
            or facts.snapshot.byte_count != self.__expected_content.byte_count
            or facts.content_sha256 != self.__expected_content.content_sha256
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return facts

    @abstractmethod
    def _reprove_source(self) -> BoundContentFacts: ...


class RetainedRetirement(OpaqueAuthority, ABC):
    """Live proof that one owned file now occupies the exact retirement name.

    The authority is intentionally non-serializable.  It retains both namespace
    positions so callers can reconcile an interrupted exclusive move without
    authorizing from a historical identity value alone.
    """

    __slots__ = ("__owned_identity", "__transferred_reservation")

    def __init__(self, owned_identity: FileObjectIdentity) -> None:
        super().__init__()
        if type(owned_identity) is not FileObjectIdentity:
            raise TypeError("owned_identity must be exact FileObjectIdentity")
        if owned_identity.kind != "regular" or owned_identity.link_count != 1:
            raise ValueError("retirement requires one regular-file link")
        self.__owned_identity = owned_identity
        self.__transferred_reservation: MutableFileReservation | None = None

    def _accept_reservation_transfer(
        self,
        reservation: MutableFileReservation,
    ) -> None:
        self._require_open()
        if not isinstance(reservation, MutableFileReservation):
            raise TypeError("reservation must be MutableFileReservation")
        reservation._require_open()
        if self.__transferred_reservation is not None:
            raise TypeError("retirement already owns a reservation")
        self.__transferred_reservation = reservation
        try:
            reservation._mark_authority_transferred()
        except BaseException:
            if not reservation.closed:
                self.__transferred_reservation = None
            raise

    def reprove(self) -> EntrySnapshot:
        self._require_open()
        snapshot = self._reprove_retirement()
        if type(snapshot) is not EntrySnapshot:
            raise TypeError("backend retirement reproof must return exact EntrySnapshot")
        if (
            snapshot.identity != self.__owned_identity
            or snapshot.identity.kind != "regular"
            or snapshot.identity.link_count != 1
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return snapshot

    @abstractmethod
    def _reprove_retirement(self) -> EntrySnapshot: ...

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        try:
            self._close_retirement_authority()
        except BaseException as error:
            first_error = error
        reservation = self.__transferred_reservation
        self.__transferred_reservation = None
        if reservation is not None:
            try:
                reservation._close_authority()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    @abstractmethod
    def _close_retirement_authority(self) -> None: ...


class RetirementDirectoryAuthority(OpaqueAuthority, ABC):
    """Dedicated namespace authority that cannot publish or mint other capabilities."""

    def reprove(self) -> None:
        self._require_open()
        result = self._reprove()
        if result is not None:
            raise TypeError("backend retirement-directory reproof must return None")

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


class BoundRegularFile(OpaqueAuthority, ABC):
    _MAXIMUM_BOUNDED_READ_BYTES = 64 * 1024

    def read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected: EntrySnapshot,
    ) -> bytes:
        self._require_open()
        if type(offset) is not int:
            raise TypeError("offset must be exact int")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if type(maximum_bytes) is not int:
            raise TypeError("maximum_bytes must be exact int")
        if maximum_bytes < 1 or maximum_bytes > self._MAXIMUM_BOUNDED_READ_BYTES:
            raise ValueError("maximum_bytes must be from 1 through 65536")
        if type(expected) is not EntrySnapshot:
            raise TypeError("expected must be exact EntrySnapshot")
        if offset > expected.byte_count:
            raise ValueError("offset must not exceed the expected byte count")
        payload = self._read_at(offset, maximum_bytes, expected)
        if type(payload) is not bytes:
            raise TypeError("backend read_at must return exact bytes")
        expected_count = min(maximum_bytes, expected.byte_count - offset)
        if len(payload) != expected_count:
            raise ValueError("backend read_at returned a non-exact byte count")
        return payload

    @abstractmethod
    def _read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected: EntrySnapshot,
    ) -> bytes: ...

    def iter_exact_chunks(self, expected: EntrySnapshot) -> Iterator[bytes]:
        """Stream one exact file generation through this live authority."""

        self._require_open()
        if type(expected) is not EntrySnapshot:
            raise TypeError("expected must be exact EntrySnapshot")
        chunks = self._iter_exact_chunks(expected)

        def checked_chunks() -> Iterator[bytes]:
            try:
                byte_count = 0
                for chunk in chunks:
                    if type(chunk) is not bytes:
                        raise TypeError("backend exact stream chunks must be exact bytes")
                    if not chunk or len(chunk) > self._MAXIMUM_BOUNDED_READ_BYTES:
                        raise ValueError(
                            "backend exact stream chunks must contain from 1 through 65536 bytes"
                        )
                    byte_count += len(chunk)
                    if byte_count > expected.byte_count:
                        raise PlatformFileError(
                            PlatformFileErrorCode.IDENTITY_STALE,
                            retryable=True,
                        )
                    yield chunk
                if byte_count != expected.byte_count:
                    raise PlatformFileError(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
            finally:
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()

        return checked_chunks()

    def _iter_exact_chunks(self, expected: EntrySnapshot) -> Iterator[bytes]:
        offset = 0
        while offset < expected.byte_count:
            maximum_bytes = min(
                self._MAXIMUM_BOUNDED_READ_BYTES,
                expected.byte_count - offset,
            )
            chunk = self.read_at(offset, maximum_bytes, expected)
            offset += len(chunk)
            yield chunk
        if self.read_at(offset, 1, expected) or self.snapshot() != expected:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )

    def read_all(self) -> bytes:
        self._require_open()
        expected = self.snapshot()
        chunks: list[bytes] = []
        offset = 0
        while offset < expected.byte_count:
            maximum_bytes = min(
                self._MAXIMUM_BOUNDED_READ_BYTES,
                expected.byte_count - offset,
            )
            chunk = self.read_at(offset, maximum_bytes, expected)
            chunks.append(chunk)
            offset += len(chunk)
        if self.read_at(offset, 1, expected):
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        if self.snapshot() != expected:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return b"".join(chunks)

    def capture_content(self) -> BoundContentCapture:
        """Materialize one exact generation together with its live facts."""

        self._require_open()
        capture = self._capture_content()
        if type(capture) is not BoundContentCapture:
            raise TypeError(
                "backend capture_content must return exact BoundContentCapture"
            )
        return capture

    def _capture_content(self) -> BoundContentCapture:
        return BoundContentCapture(self.read_all(), self.content_facts())

    def content_facts(self) -> BoundContentFacts:
        """Hash exact bytes without materializing the complete file in memory."""

        self._require_open()
        facts = self._content_facts()
        if type(facts) is not BoundContentFacts:
            raise TypeError("backend content_facts must return exact BoundContentFacts")
        return facts

    def _content_facts(self) -> BoundContentFacts:
        expected = self.snapshot()
        digest = hashlib.sha256()
        offset = 0
        while offset < expected.byte_count:
            maximum_bytes = min(
                self._MAXIMUM_BOUNDED_READ_BYTES,
                expected.byte_count - offset,
            )
            chunk = self.read_at(offset, maximum_bytes, expected)
            digest.update(chunk)
            offset += len(chunk)
        if self.read_at(offset, 1, expected) or self.snapshot() != expected:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return BoundContentFacts(expected, digest.digest())

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


class BoundSynchronizedRegularFile(BoundRegularFile, ABC):
    """A rooted existing-file authority with a content durability operation."""

    def synchronize_content(
        self,
        expected: BoundContentFacts,
    ) -> BoundContentFacts:
        self._require_open()
        if type(expected) is not BoundContentFacts:
            raise TypeError("expected must be exact BoundContentFacts")
        before = self.content_facts()
        if before != expected:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        synchronized = self._synchronize_content(before)
        if type(synchronized) is not BoundContentFacts:
            raise TypeError(
                "backend synchronize_content must return exact BoundContentFacts"
            )
        if synchronized != before:
            raise ValueError("backend synchronized content facts contradict the input")
        after = self.content_facts()
        if after != before:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return after

    @abstractmethod
    def _synchronize_content(
        self,
        expected: BoundContentFacts,
    ) -> BoundContentFacts: ...


class BoundExistingFileMutationGuard(OpaqueAuthority, ABC):
    """Live identity guard for one existing file mutated by an external writer.

    The guard does not write, synchronize, publish, or interpret file content.
    A backend may use its retained handle to prevent delete/rename while a
    pathname-only writer is active, but every reproof must still bind the same
    regular single-link object to the same rooted name.
    """

    __slots__ = ("__identity",)

    def __init__(self, identity: FileObjectIdentity) -> None:
        super().__init__()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("identity must be exact FileObjectIdentity")
        if identity.kind != "regular" or identity.link_count != 1:
            raise ValueError("mutation guard requires one regular-file link")
        self.__identity = identity

    def reprove(self) -> FileObjectIdentity:
        self._require_open()
        identity = self._reprove_guard()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("backend mutation guard must return exact identity")
        if (
            identity != self.__identity
            or identity.kind != "regular"
            or identity.link_count != 1
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        return identity

    @abstractmethod
    def _reprove_guard(self) -> FileObjectIdentity: ...


class CandidateFile(OpaqueAuthority, ABC):
    _MAXIMUM_STREAM_CHUNK_BYTES = 64 * 1024

    __slots__ = ("__content_facts", "__flushed", "__write_started")

    def __init__(
        self,
        *,
        _recovered_content: CandidateContentFacts | None = None,
    ) -> None:
        super().__init__()
        if (
            _recovered_content is not None
            and type(_recovered_content) is not CandidateContentFacts
        ):
            raise TypeError("_recovered_content must be exact CandidateContentFacts")
        self.__content_facts = _recovered_content
        self.__flushed = _recovered_content is not None
        self.__write_started = _recovered_content is not None

    def write_all(self, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("payload must be exact bytes")
        self.write_chunks(
            (
                payload[offset : offset + self._MAXIMUM_STREAM_CHUNK_BYTES]
                for offset in range(0, len(payload), self._MAXIMUM_STREAM_CHUNK_BYTES)
            ),
            maximum_bytes=len(payload),
        )

    def write_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        maximum_bytes: int,
    ) -> CandidateContentFacts:
        self._require_open()
        if self.__write_started:
            raise ValueError("candidate accepts exactly one content stream")
        if type(maximum_bytes) is not int:
            raise TypeError("maximum_bytes must be exact int")
        if maximum_bytes < 0 or maximum_bytes > (1 << 63) - 1:
            raise ValueError("maximum_bytes is outside the supported range")
        try:
            iterator = iter(chunks)
        except TypeError:
            raise TypeError("chunks must be an iterable of exact bytes") from None
        self.__write_started = True
        self.__content_facts = None
        self.__flushed = False
        digest = hashlib.sha256()
        byte_count = 0
        consumed = False

        def checked_chunks() -> Iterator[bytes]:
            nonlocal byte_count, consumed
            for chunk in iterator:
                if type(chunk) is not bytes:
                    raise TypeError("stream chunks must be exact bytes")
                if not chunk or len(chunk) > self._MAXIMUM_STREAM_CHUNK_BYTES:
                    raise ValueError("stream chunks must contain from 1 through 65536 bytes")
                byte_count += len(chunk)
                if byte_count > maximum_bytes:
                    raise ValueError("candidate stream exceeds maximum_bytes")
                digest.update(chunk)
                yield chunk
            consumed = True

        result = self._write_chunks(checked_chunks())
        if not consumed:
            raise TypeError("backend write_chunks must consume the exact stream once")
        expected = CandidateContentFacts(byte_count, digest.digest())
        if type(result) is not CandidateContentFacts:
            raise TypeError("backend write_chunks must return exact CandidateContentFacts")
        if result != expected:
            raise ValueError("backend candidate content facts contradict the input stream")
        self.__content_facts = result
        return result

    @abstractmethod
    def _write_chunks(self, chunks: Iterator[bytes]) -> CandidateContentFacts: ...

    def flush_content(self) -> None:
        self._require_open()
        if self.__content_facts is None:
            raise ValueError("candidate content must be written before flush")
        self.__flushed = False
        result = self._flush_content(self.__content_facts)
        if type(result) is not CandidateContentFacts:
            raise TypeError("backend flush_content must return exact CandidateContentFacts")
        if result != self.__content_facts:
            raise ValueError("backend flushed content facts contradict the written stream")
        self.__flushed = True

    @abstractmethod
    def _flush_content(self, expected: CandidateContentFacts) -> CandidateContentFacts: ...

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
        if self.__content_facts is None or not self.__flushed:
            raise ValueError("candidate must contain flushed content before publish")


class LockLease(OpaqueAuthority, ABC):
    """One retained process-lock lease with an explicit proof boundary."""

    def reprove(self) -> None:
        """Reprove the handle-bound lock while retaining its ownership."""

        self._require_open()
        result = self._reprove_lock()
        if result is not None:
            raise TypeError("backend lock reproof must return None")

    @abstractmethod
    def _reprove_lock(self) -> None: ...

    def reprove_binding(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
    ) -> None:
        """Reprove this lease against one exact live parent/name/payload binding."""

        self._require_open()
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be BoundDirectoryAuthority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        if type(payload) is not bytes:
            raise TypeError("lock payload must be exact bytes")
        result = self._reprove_binding(parent, checked_name, payload)
        if result is not None:
            raise TypeError("backend lock binding reproof must return None")

    @abstractmethod
    def _reprove_binding(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
    ) -> None: ...


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


class DeviceSecretAuthority(OpaqueAuthority, ABC):
    """Issuer-owned retained key handle independent of its binding input."""

    def reprove(self) -> None:
        self._require_open()
        result = self._reprove()
        if result is not None:
            raise TypeError("backend device-secret reproof must return None")

    @abstractmethod
    def _reprove(self) -> None: ...


class VerifiedPrivateProof(OpaqueAuthority, ABC):
    """Issuer-bound proof awaiting one locked terminal consumption."""

    __slots__ = ("__consume_lock", "__consumed")

    def __init__(self) -> None:
        super().__init__()
        self.__consume_lock = threading.Lock()
        self.__consumed = False

    def _consume_once(self, operation: Callable[[], None]) -> None:
        if not callable(operation):
            raise TypeError("consumption operation must be callable")
        with self.__consume_lock:
            self._require_open()
            if self.__consumed:
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            try:
                result = operation()
                if result is not None:
                    raise TypeError("backend verified-proof consumption must return None")
                self.__consumed = True
            finally:
                self.close()


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

    def observe_ledger_entries(
        self,
        lease: LockLease,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        """Observe a bounded locked namespace; callers must reopen every entry."""

        self._require_open()
        if not isinstance(lease, LockLease):
            raise TypeError("lease must be LockLease")
        lease._require_open()
        if type(limits) is not LedgerEnumerationLimits:
            raise TypeError("limits must be exact LedgerEnumerationLimits")
        result = self._observe_ledger_entries(lease, limits)
        if type(result) is not tuple:
            raise TypeError("backend ledger observations must be an exact tuple")
        if len(result) > limits.maximum_entries:
            raise ValueError("backend ledger observations exceed the entry limit")
        previous_name: str | None = None
        total_bytes = 0
        for observation in result:
            if type(observation) is not LedgerEntryObservation:
                raise TypeError("backend returned a non-exact ledger observation")
            name_bytes = observation.name.encode("utf-8", errors="strict")
            if len(name_bytes) > limits.maximum_name_bytes:
                raise ValueError("backend ledger observation exceeds the name limit")
            if previous_name is not None and observation.name <= previous_name:
                raise ValueError("backend ledger observations must be uniquely sorted")
            previous_name = observation.name
            total_bytes += observation.snapshot.byte_count
            if total_bytes > limits.maximum_total_bytes:
                raise ValueError("backend ledger observations exceed the byte limit")
        return result

    @abstractmethod
    def _observe_ledger_entries(
        self,
        lease: LockLease,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]: ...

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
class MutableFileReservationService(Protocol):
    def reserve_mutable_file(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> MutableFileReservation:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be BoundDirectoryAuthority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        reservation = self._reserve_mutable_file(parent, checked_name)
        if not isinstance(reservation, MutableFileReservation):
            raise TypeError(
                "backend reservation must return MutableFileReservation"
            )
        reservation._require_open()
        return reservation

    @abstractmethod
    def _reserve_mutable_file(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> MutableFileReservation: ...


@runtime_checkable
class OwnedNamespaceRetirement(Protocol):
    """Create rooted retirement directories and exclusively move an owned file."""

    def bind_or_create_child_directory(
        self,
        parent: BoundDirectoryAuthority | RetirementDirectoryAuthority,
        name: str,
    ) -> RetirementDirectoryAuthority:
        if not isinstance(
            parent,
            (BoundDirectoryAuthority, RetirementDirectoryAuthority),
        ):
            raise TypeError("parent must be a bound or retirement directory authority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        authority = self._bind_or_create_child_directory(parent, checked_name)
        if not isinstance(authority, RetirementDirectoryAuthority):
            raise TypeError("backend child bind must return RetirementDirectoryAuthority")
        authority._require_open()
        return authority

    @abstractmethod
    def _bind_or_create_child_directory(
        self,
        parent: BoundDirectoryAuthority | RetirementDirectoryAuthority,
        name: str,
    ) -> RetirementDirectoryAuthority: ...

    def retire_owned_exclusive(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        reservation: MutableFileReservation,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        if not isinstance(source_parent, BoundDirectoryAuthority):
            raise TypeError("source_parent must be BoundDirectoryAuthority")
        if not isinstance(target_parent, RetirementDirectoryAuthority):
            raise TypeError("target_parent must be RetirementDirectoryAuthority")
        source_parent._require_open()
        target_parent._require_open()
        checked_source = validate_relative_name(source_name)
        checked_target = validate_relative_name(target_name)
        if not isinstance(reservation, MutableFileReservation):
            raise TypeError("reservation must be MutableFileReservation")
        reservation._require_open()
        authority = self._retire_owned_exclusive(
            source_parent,
            checked_source,
            reservation,
            target_parent,
            checked_target,
        )
        if not isinstance(authority, RetainedRetirement):
            raise TypeError("backend retirement must return RetainedRetirement")
        authority._require_open()
        if not reservation.closed:
            authority.close()
            raise TypeError("backend retirement must consume the reservation")
        return authority

    @abstractmethod
    def _retire_owned_exclusive(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        reservation: MutableFileReservation,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement: ...


@runtime_checkable
class LockedDescendantNamespaceInspection(Protocol):
    """Observe one descendant namespace under an exact ancestor W1 lease."""

    def observe_descendant_entries(
        self,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        descendant: BoundDirectoryAuthority,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        if not isinstance(lease, LockLease):
            raise TypeError("lease must be LockLease")
        if not isinstance(descendant, BoundDirectoryAuthority):
            raise TypeError("descendant must be a bound directory authority")
        root._require_open()
        lease._require_open()
        descendant._require_open()
        if type(limits) is not LedgerEnumerationLimits:
            raise TypeError("limits must be exact LedgerEnumerationLimits")
        result = self._observe_descendant_entries(root, lease, descendant, limits)
        if type(result) is not tuple:
            raise TypeError("backend descendant observations must be an exact tuple")
        if len(result) > limits.maximum_entries:
            raise ValueError("backend descendant observations exceed the entry limit")
        previous_name: str | None = None
        total_bytes = 0
        for observation in result:
            if type(observation) is not LedgerEntryObservation:
                raise TypeError("backend returned a non-exact descendant observation")
            name_bytes = observation.name.encode("utf-8", errors="strict")
            if len(name_bytes) > limits.maximum_name_bytes:
                raise ValueError("backend descendant observation exceeds the name limit")
            if previous_name is not None and observation.name <= previous_name:
                raise ValueError("backend descendant observations must be uniquely sorted")
            previous_name = observation.name
            total_bytes += observation.snapshot.byte_count
            if total_bytes > limits.maximum_total_bytes:
                raise ValueError("backend descendant observations exceed the byte limit")
        return result

    @abstractmethod
    def _observe_descendant_entries(
        self,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        descendant: BoundDirectoryAuthority,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]: ...


@runtime_checkable
class ExactEmptyChildDirectoryRetirement(Protocol):
    """Bind and remove one exact empty direct child directory.

    This narrow capability is intentionally not part of ``PlatformFileBackend``.
    The retained child is live, process-local authority; removal consumes it and
    never authorizes recursive deletion or a path-only operation.
    """

    def bind_existing_child_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be a bound directory authority")
        parent._require_open()
        checked_name = validate_relative_name(name)
        authority = self._bind_existing_child_directory(parent, checked_name)
        if not isinstance(authority, BoundDirectoryAuthority):
            raise TypeError("backend child bind must return BoundDirectoryAuthority")
        authority._require_open()
        return authority

    @abstractmethod
    def _bind_existing_child_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority: ...

    def remove_empty_owned_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        child: BoundDirectoryAuthority,
    ) -> None:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be a bound directory authority")
        if not isinstance(child, BoundDirectoryAuthority):
            raise TypeError("child must be a bound directory authority")
        parent._require_open()
        child._require_open()
        checked_name = validate_relative_name(name)
        result = self._remove_empty_owned_directory(parent, checked_name, child)
        if result is not None:
            raise TypeError("backend empty-directory removal must return None")
        if not child.closed:
            raise TypeError("backend empty-directory removal must consume child")

    @abstractmethod
    def _remove_empty_owned_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        child: BoundDirectoryAuthority,
    ) -> None: ...


@runtime_checkable
class ExistingFileRetirement(Protocol):
    """Retire or freshly rebind one exact existing file without creator state."""

    def bind_retirement_source_directory(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> RetirementSourceDirectoryAuthority:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        root._require_open()
        checked_relative = validate_relative_path(relative)
        authority = self._bind_retirement_source_directory(
            root,
            checked_relative,
        )
        try:
            if not isinstance(authority, RetirementSourceDirectoryAuthority):
                raise TypeError(
                    "backend source-directory bind must return "
                    "RetirementSourceDirectoryAuthority"
                )
            authority._require_open()
            authority.reprove()
            return authority
        except BaseException:
            if isinstance(authority, OpaqueAuthority):
                try:
                    authority.close()
                except BaseException:
                    pass
            raise

    @abstractmethod
    def _bind_retirement_source_directory(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> RetirementSourceDirectoryAuthority: ...

    def open_existing_retirement_source(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        expected_content: CandidateContentFacts,
    ) -> ExistingRetirementSource:
        if not isinstance(source_parent, RetirementSourceDirectoryAuthority):
            raise TypeError(
                "source_parent must be RetirementSourceDirectoryAuthority"
            )
        source_parent._require_open()
        if type(expected_content) is not CandidateContentFacts:
            raise TypeError("expected_content must be exact CandidateContentFacts")
        authority = self._open_existing_retirement_source(
            source_parent,
            expected_content,
        )
        try:
            if not isinstance(authority, ExistingRetirementSource):
                raise TypeError("backend source open must return ExistingRetirementSource")
            authority._require_open()
            authority.reprove()
            return authority
        except BaseException:
            if isinstance(authority, ExistingRetirementSource):
                try:
                    authority.close()
                except BaseException:
                    pass
            raise

    @abstractmethod
    def _open_existing_retirement_source(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        expected_content: CandidateContentFacts,
    ) -> ExistingRetirementSource: ...

    def retire_existing_exclusive(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        source: ExistingRetirementSource,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        if not isinstance(source_parent, RetirementSourceDirectoryAuthority):
            raise TypeError(
                "source_parent must be RetirementSourceDirectoryAuthority"
            )
        if not isinstance(target_parent, RetirementDirectoryAuthority):
            raise TypeError("target_parent must be RetirementDirectoryAuthority")
        if not isinstance(source, ExistingRetirementSource):
            raise TypeError("source must be ExistingRetirementSource")
        source_parent._require_open()
        target_parent._require_open()
        source._require_open()
        checked_target = validate_relative_name(target_name)
        authority = self._retire_existing_exclusive(
            source_parent,
            source,
            target_parent,
            checked_target,
        )
        if not isinstance(authority, RetainedRetirement):
            raise TypeError("backend retirement must return RetainedRetirement")
        authority._require_open()
        if not source.closed:
            authority.close()
            raise TypeError("backend retirement must consume the source authority")
        return authority

    @abstractmethod
    def _retire_existing_exclusive(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        source: ExistingRetirementSource,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement: ...

    def rebind_existing_retirement(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
    ) -> RetainedRetirement:
        if not isinstance(source_parent, BoundDirectoryAuthority):
            raise TypeError("source_parent must be BoundDirectoryAuthority")
        if not isinstance(target_parent, RetirementDirectoryAuthority):
            raise TypeError("target_parent must be RetirementDirectoryAuthority")
        source_parent._require_open()
        target_parent._require_open()
        checked_source = validate_relative_name(source_name)
        checked_target = validate_relative_name(target_name)
        if type(expected_content) is not CandidateContentFacts:
            raise TypeError("expected_content must be exact CandidateContentFacts")
        authority = self._rebind_existing_retirement(
            source_parent,
            checked_source,
            target_parent,
            checked_target,
            expected_content,
        )
        try:
            if not isinstance(authority, RetainedRetirement):
                raise TypeError("backend rebind must return RetainedRetirement")
            authority._require_open()
            authority.reprove()
            return authority
        except BaseException:
            if isinstance(authority, RetainedRetirement):
                try:
                    authority.close()
                except BaseException:
                    pass
            raise

    @abstractmethod
    def _rebind_existing_retirement(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
    ) -> RetainedRetirement: ...


@runtime_checkable
class ExistingCandidateRecovery(Protocol):
    """Recover one exact internal candidate under its live owner authorities.

    The lease and mutation guard must bind the same owner parent/root chain as
    ``source_parent``; the capability never authorizes a final replacement.
    ``require_private`` additionally proves the selected object's private
    profile through the retained file handle before any rebind can occur and
    keeps that proof live on the returned publishable candidate.
    """

    def recover_existing_candidate(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
        *,
        owner_lease: LockLease,
        mutation_guard: BoundExistingFileMutationGuard,
        require_private: bool = False,
    ) -> CandidateFile:
        if not isinstance(source_parent, BoundDirectoryAuthority):
            raise TypeError("source_parent must be BoundDirectoryAuthority")
        if not isinstance(target_parent, RetirementDirectoryAuthority):
            raise TypeError("target_parent must be RetirementDirectoryAuthority")
        source_parent._require_open()
        target_parent._require_open()
        if not isinstance(owner_lease, LockLease):
            raise TypeError("owner_lease must be LockLease")
        if not isinstance(mutation_guard, BoundExistingFileMutationGuard):
            raise TypeError(
                "mutation_guard must be BoundExistingFileMutationGuard"
            )
        owner_lease._require_open()
        mutation_guard._require_open()
        if type(require_private) is not bool:
            raise TypeError("require_private must be exact bool")
        checked_source = validate_relative_name(source_name)
        checked_target = validate_relative_name(target_name)
        if type(expected_content) is not CandidateContentFacts:
            raise TypeError("expected_content must be exact CandidateContentFacts")
        authority = self._recover_existing_candidate(
            source_parent,
            checked_source,
            target_parent,
            checked_target,
            expected_content,
            owner_lease=owner_lease,
            mutation_guard=mutation_guard,
            require_private=require_private,
        )
        try:
            if not isinstance(authority, CandidateFile):
                raise TypeError("backend recovery must return CandidateFile")
            authority._require_publishable()
            return authority
        except BaseException:
            if isinstance(authority, CandidateFile):
                try:
                    authority.close()
                except BaseException:
                    pass
            raise

    @abstractmethod
    def _recover_existing_candidate(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
        *,
        owner_lease: LockLease,
        mutation_guard: BoundExistingFileMutationGuard,
        require_private: bool,
    ) -> CandidateFile: ...


@runtime_checkable
class ExistingFileDurability(Protocol):
    def open_existing_for_synchronization(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundSynchronizedRegularFile:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        root._require_open()
        checked_relative = validate_relative_path(relative)
        authority = self._open_existing_for_synchronization(root, checked_relative)
        if not isinstance(authority, BoundSynchronizedRegularFile):
            raise TypeError(
                "backend synchronization open must return BoundSynchronizedRegularFile"
            )
        authority._require_open()
        return authority

    @abstractmethod
    def _open_existing_for_synchronization(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundSynchronizedRegularFile: ...


@runtime_checkable
class ExistingFileMutationGuard(Protocol):
    def guard_existing_for_mutation(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundExistingFileMutationGuard:
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("root must be RootedDirectoryAuthority")
        root._require_open()
        checked_relative = validate_relative_path(relative)
        authority = self._guard_existing_for_mutation(root, checked_relative)
        if not isinstance(authority, BoundExistingFileMutationGuard):
            raise TypeError(
                "backend mutation guard must return BoundExistingFileMutationGuard"
            )
        authority._require_open()
        authority.reprove()
        return authority

    @abstractmethod
    def _guard_existing_for_mutation(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundExistingFileMutationGuard: ...


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
class ExistingProcessFileLock(Protocol):
    """Acquire one already-existing lock without creating or rewriting it."""

    def acquire_existing(
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
        lease = self._acquire_existing(
            parent,
            checked_name,
            payload,
            policy,
        )
        if not isinstance(lease, LockLease):
            raise TypeError("backend existing acquire must return LockLease")
        lease._require_open()
        return lease

    @abstractmethod
    def _acquire_existing(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease: ...


@runtime_checkable
class OutputArtifactLockRetirement(Protocol):
    """Optionally retire one approved, already-finished output lock carrier."""

    def finish_output_lock(
        self,
        parent: BoundDirectoryAuthority,
        lease: LockLease,
    ) -> OutputLockFinishResult:
        if not isinstance(parent, BoundDirectoryAuthority):
            raise TypeError("parent must be BoundDirectoryAuthority")
        parent._require_open()
        if not isinstance(lease, LockLease):
            raise TypeError("lease must be LockLease")
        lease._require_open()
        result = self._finish_output_lock(parent, lease)
        if type(result) is not OutputLockFinishResult:
            raise TypeError(
                "backend output lock retirement must return exact "
                "OutputLockFinishResult"
            )
        return result

    @abstractmethod
    def _finish_output_lock(
        self,
        parent: BoundDirectoryAuthority,
        lease: LockLease,
    ) -> OutputLockFinishResult: ...


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
class PersistentPrivateProof(Protocol):
    """Independent persistent proof port; never part of PlatformFileBackend.

    Concrete backends must accept only their exact private-evidence, secret,
    and verified-proof classes issued by the same backend issuer.  The generic
    facade deliberately knows no backend concrete authority type and an
    ``isinstance`` check alone never authorizes a persistent operation.
    """

    def bind_device_secret(
        self,
        secret_file: BoundRegularFile,
    ) -> DeviceSecretAuthority:
        if not isinstance(secret_file, BoundRegularFile):
            raise TypeError("secret_file must be BoundRegularFile")
        secret_file._require_open()
        secret = self._bind_device_secret(secret_file)
        if not isinstance(secret, DeviceSecretAuthority):
            raise TypeError("backend secret binding must return DeviceSecretAuthority")
        secret._require_open()
        return secret

    @abstractmethod
    def _bind_device_secret(
        self,
        secret_file: BoundRegularFile,
    ) -> DeviceSecretAuthority:
        """Duplicate/retain the key handle; never borrow caller ownership."""
        ...

    def mint(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        context: PrivateProofContext,
    ) -> WindowsPrivateProof:
        if not isinstance(target, PrivateAccessEvidence):
            raise TypeError("target must be PrivateAccessEvidence")
        if not isinstance(secret, DeviceSecretAuthority):
            raise TypeError("secret must be DeviceSecretAuthority")
        if type(context) is not PrivateProofContext:
            raise TypeError("context must be exact PrivateProofContext")
        target._require_open()
        secret._require_open()
        proof = self._mint(target, secret, context)
        if type(proof) is not WindowsPrivateProof:
            raise TypeError("backend mint must return exact WindowsPrivateProof")
        if (
            proof.object_role is not context.object_role
            or proof.owner_context_sha256 != context.owner_context_sha256
        ):
            raise ValueError("backend proof does not bind the requested owner context")
        return proof

    @abstractmethod
    def _mint(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        context: PrivateProofContext,
    ) -> WindowsPrivateProof:
        """Require exact concrete target/secret types from this issuer."""
        ...

    def verify(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        proof: WindowsPrivateProof,
        expected_context: PrivateProofContext,
    ) -> VerifiedPrivateProof:
        if not isinstance(target, PrivateAccessEvidence):
            raise TypeError("target must be PrivateAccessEvidence")
        if not isinstance(secret, DeviceSecretAuthority):
            raise TypeError("secret must be DeviceSecretAuthority")
        if type(proof) is not WindowsPrivateProof:
            raise TypeError("proof must be exact WindowsPrivateProof")
        if type(expected_context) is not PrivateProofContext:
            raise TypeError("expected_context must be exact PrivateProofContext")
        target._require_open()
        secret._require_open()
        if (
            proof.object_role is not expected_context.object_role
            or proof.owner_context_sha256 != expected_context.owner_context_sha256
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )
        verified = self._verify(target, secret, proof, expected_context)
        if not isinstance(verified, VerifiedPrivateProof):
            raise TypeError("backend verify must return VerifiedPrivateProof")
        verified._require_open()
        return verified

    @abstractmethod
    def _verify(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        proof: WindowsPrivateProof,
        expected_context: PrivateProofContext,
    ) -> VerifiedPrivateProof:
        """Require exact concrete target/secret types from this issuer."""
        ...

    def consume_verified(
        self,
        verified: VerifiedPrivateProof,
        expected_context: PrivateProofContext,
    ) -> None:
        if not isinstance(verified, VerifiedPrivateProof):
            raise TypeError("verified must be VerifiedPrivateProof")
        if type(expected_context) is not PrivateProofContext:
            raise TypeError("expected_context must be exact PrivateProofContext")
        result = self._consume_verified(verified, expected_context)
        if result is not None:
            raise TypeError("backend verified-proof consumption must return None")

    @abstractmethod
    def _consume_verified(
        self,
        verified: VerifiedPrivateProof,
        expected_context: PrivateProofContext,
    ) -> None:
        """Validate exact type, issuer and context before accepting ownership.

        Rejection leaves the token caller-owned.  Once accepted, invoke
        ``VerifiedPrivateProof._consume_once(verified, terminal_operation)``
        explicitly so an untrusted subclass cannot override the one-shot lock
        or terminal close.
        """
        ...


@runtime_checkable
class PlatformFileBackend(
    RootedFileSystem,
    MutableFileReservationService,
    OwnedNamespaceRetirement,
    ExistingFileDurability,
    ProcessFileLock,
    PrivateStorageProof,
    Protocol,
):
    """Aggregate capability implemented by every composed platform backend."""

    pass
