"""Task 5.14 configured snapshot refresh crash recovery (Cluster F).

Recovery closes the Task 5.13 issued-receipt crash window before the
source binding monitor can misclassify a legitimate publication window
as external divergence.  It is a pure Core module: it consumes only the
frozen contracts and the strict activation-journal file primitives and
drives one narrow ``_SnapshotRecoveryPort`` implemented by
``tm_sqlite_store``, so it never imports the store or migration modules.

The recovery matrix is fixed by the approved design:

1. issued refresh receipt + configured pair still equals the old
   completed binding pair -> atomically cancel only the issued receipt;
   the old binding stays valid.
2. issued refresh receipt + JSONL equals the issued digest while the
   manifest is old or absent -> deterministically reconstruct the exact
   manifest from the ledger receipt, publish it through the exclusive
   same-directory temporary with file fsync, strict identity/digest
   proof, atomic replace and parent fsync, then atomically complete the
   issued receipt and rebind.
3. issued refresh receipt + JSONL and manifest both equal the issued
   receipt/manifest -> atomically complete and rebind without
   republishing canonical records.
4. either configured file is unsafe/unprovable, ledger identity/paths/
   revision ancestry is invalid, multiple ambiguous issued refresh
   receipts exist, or the pair matches neither the old completed
   binding nor one valid issued receipt -> durably latch
   ``SOURCE_DIVERGED``; foreign/unproven paths are never touched and
   canonical authority stays.

Issued arbitrary-destination export receipts reuse the same protocol
with the export consequence model: completion/cancellation never
touches the active binding and never latches or clears resource
divergence; unprovable export windows fail closed with evidence
preserved.  A pre-existing divergence latch is never cleared.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import stat
from typing import Protocol

from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_activation_journal import (
    ActivationPreparationError,
    _ActivationFileIdentity,
    _capture_activation_file,
    _close_activation_journal,
    _fsync_activation_directory,
    _fsync_activation_journal,
    _open_activation_journal_temp,
    _remove_owned_activation_journal_temp,
    _write_activation_journal_bytes,
)


_EXPORT_MANIFEST_SUFFIX = ".localcat-snapshot.json"
_EXPORT_JSONL_TEMP_SUFFIX = ".localcat-export.jsonl.tmp"
_EXPORT_MANIFEST_TEMP_SUFFIX = ".localcat-export.manifest.tmp"
_EXPORT_JSONL_RECOVERY_SUFFIX = ".localcat-export-recovery.jsonl.bak"
_EXPORT_MANIFEST_RECOVERY_SUFFIX = ".localcat-export-recovery.manifest.bak"

_NATIVE_PATH_TYPE = type(Path())

_MAX_RECOVERY_ROUNDS = 8


class RecoveryError(RuntimeError):
    """Stable code-only recovery failure with no path or TM payload."""

    def __init__(self, error_code: str, *, retryable: bool) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("recovery error code is invalid")
        if type(retryable) is not bool:
            raise TypeError("recovery retryable flag is invalid")
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


class RefreshRecoveryState(str, Enum):
    """Stable public classification of one Task 5.14 recovery outcome."""

    NOOP = "NOOP"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    DIVERGED = "DIVERGED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class IssuedReceiptRecovery:
    """One issued receipt's durable recovery result."""

    snapshot_id: str
    state: RefreshRecoveryState
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not str or not self.snapshot_id:
            raise TypeError("snapshot id must be a non-empty string")
        if type(self.state) is not RefreshRecoveryState:
            raise TypeError("recovery state must be RefreshRecoveryState")
        if type(self.diagnostics) is not tuple or any(
            type(code) is not str for code in self.diagnostics
        ):
            raise TypeError("recovery diagnostics must be string codes")


@dataclass(frozen=True)
class RefreshRecoveryOutcome:
    """Code-only outcome of one snapshot publication recovery pass.

    ``state`` is the overall classification: ``NOOP`` when nothing was
    issued or a pre-existing divergence latch was preserved,
    ``CANCELLED``/``COMPLETED`` when every issued refresh receipt was
    durably reconciled, ``DIVERGED`` when the divergence latch was
    durably set, and ``BLOCKED`` when classification or effects could
    not be proven and evidence was preserved for a later replay.
    """

    state: RefreshRecoveryState
    receipts: tuple[IssuedReceiptRecovery, ...] = ()
    snapshot_id: str | None = None
    diagnostics: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not RefreshRecoveryState:
            raise TypeError("recovery state must be RefreshRecoveryState")
        if type(self.receipts) is not tuple or any(
            type(item) is not IssuedReceiptRecovery
            for item in self.receipts
        ):
            raise TypeError("recovery receipts must be IssuedReceiptRecovery")
        if self.snapshot_id is not None and (
            type(self.snapshot_id) is not str or not self.snapshot_id
        ):
            raise TypeError("snapshot id must be a non-empty string")
        if type(self.diagnostics) is not tuple or any(
            type(code) is not str for code in self.diagnostics
        ):
            raise TypeError("recovery diagnostics must be string codes")
        if self.error_code is not None and (
            type(self.error_code) is not str or not self.error_code
        ):
            raise TypeError("recovery error code must be a non-empty string")
        if type(self.retryable) is not bool:
            raise TypeError("recovery retryable flag must be a built-in bool")


@dataclass(frozen=True)
class IssuedReceiptFacts:
    """One durable ledger receipt row captured in a single snapshot."""

    snapshot_id: str
    resource_id: str
    canonical_store_id: str
    exported_revision: int
    jsonl_digest: str
    record_count: int
    format_version: str
    destination_jsonl_path: Path
    destination_manifest_path: Path
    status: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.snapshot_id, "snapshot_id"),
            (self.resource_id, "resource_id"),
            (self.canonical_store_id, "canonical_store_id"),
            (self.jsonl_digest, "jsonl_digest"),
            (self.format_version, "format_version"),
            (self.status, "status"),
        ):
            if type(value) is not str or not value:
                raise TypeError(f"{field_name} must be a non-empty string")
        if (
            type(self.exported_revision) is not int
            or self.exported_revision < 0
        ):
            raise TypeError("exported revision must be a non-negative integer")
        if type(self.record_count) is not int or self.record_count < 0:
            raise TypeError("record count must be a non-negative integer")
        if type(self.destination_jsonl_path) is not type(Path()):
            raise TypeError("destination jsonl path must be an exact Path")
        if type(self.destination_manifest_path) is not type(Path()):
            raise TypeError("destination manifest path must be an exact Path")

    @property
    def receipt(self) -> SnapshotReceipt:
        return SnapshotReceipt(
            snapshot_id=self.snapshot_id,
            resource_id=self.resource_id,
            canonical_store_id=self.canonical_store_id,
            exported_revision=self.exported_revision,
            jsonl_digest=self.jsonl_digest,
            record_count=self.record_count,
            format_version=self.format_version,
        )


@dataclass(frozen=True)
class _ArtifactHandoffFacts:
    """One durable write-once ownership record for one issued receipt.

    The publisher records the exclusive temporary identities at
    registration, the recovery-copy identities after the copies are
    prepared, and the prior final JSONL/manifest identity+digest (or
    explicit proven absence) captured before any publication replace.
    Recovery treats a matching regular single-link file as owned only
    when its exact identity was durably handed off; content equality
    alone never proves ownership, and a prior record is either proven
    absent (``prior_*_absent`` True), proven present (digest+identity),
    or unrecorded (all three ``None``) which always fails closed.  The
    exact immediate-parent device/inode proven by a strict real
    non-symlink parent-chain proof at registration is durably recorded
    (``artifact_parent_identity``); a missing legacy identity always
    fails terminal replay closed and is never inferred.
    """

    snapshot_id: str
    jsonl_temp_identity: tuple[int, int] | None
    manifest_temp_identity: tuple[int, int] | None
    artifact_parent_identity: tuple[int, int] | None = None
    jsonl_recovery_identity: tuple[int, int] | None = None
    manifest_recovery_identity: tuple[int, int] | None = None
    prior_jsonl_identity: tuple[int, int] | None = None
    prior_jsonl_digest: str | None = None
    prior_jsonl_absent: bool | None = None
    prior_manifest_identity: tuple[int, int] | None = None
    prior_manifest_digest: str | None = None
    prior_manifest_absent: bool | None = None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not str or not self.snapshot_id:
            raise TypeError("handoff snapshot id must be a non-empty string")
        for value, field_name in (
            (self.jsonl_temp_identity, "jsonl_temp_identity"),
            (self.manifest_temp_identity, "manifest_temp_identity"),
            (self.artifact_parent_identity, "artifact_parent_identity"),
            (self.jsonl_recovery_identity, "jsonl_recovery_identity"),
            (self.manifest_recovery_identity, "manifest_recovery_identity"),
        ):
            if value is not None and (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not int
                or type(value[1]) is not int
            ):
                raise TypeError(
                    f"{field_name} must be a device/inode identity pair"
                )
        for value, field_name in (
            (self.prior_jsonl_identity, "prior_jsonl_identity"),
            (self.prior_manifest_identity, "prior_manifest_identity"),
        ):
            if value is not None and (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not int
                or type(value[1]) is not int
            ):
                raise TypeError(
                    f"{field_name} must be a device/inode identity pair"
                )
        for digest, field_name in (
            (self.prior_jsonl_digest, "prior_jsonl_digest"),
            (self.prior_manifest_digest, "prior_manifest_digest"),
        ):
            if digest is not None and (
                type(digest) is not str
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
            ):
                raise TypeError(
                    f"{field_name} must be a 64-hex digest string"
                )
        for absent, field_name in (
            (self.prior_jsonl_absent, "prior_jsonl_absent"),
            (self.prior_manifest_absent, "prior_manifest_absent"),
        ):
            if absent is not None and type(absent) is not bool:
                raise TypeError(f"{field_name} must be a bool or None")


@dataclass(frozen=True)
class _RefreshRecoveryFacts:
    """Ledger/binding facts for one resource in one SQLite snapshot."""

    resource_id: str
    canonical_store_id: str
    generation: int
    configured_jsonl_path: Path
    snapshot_manifest_path: Path
    head_revision: int
    record_count: int
    cumulative_record_counts: tuple[tuple[int, int], ...]
    divergence_latched: bool
    binding: SnapshotBinding | None
    binding_invalid: bool
    receipts: tuple[IssuedReceiptFacts, ...]
    canonical_fingerprint: str
    handoffs: tuple[_ArtifactHandoffFacts, ...] = ()
    authority_paths: frozenset[Path] = frozenset()
    canonical_sidecar_path: Path | None = None
    target_identity_fragment: str | None = None

    def __post_init__(self) -> None:
        if type(self.handoffs) is not tuple or any(
            type(item) is not _ArtifactHandoffFacts
            for item in self.handoffs
        ):
            raise TypeError("handoffs must be artifact handoff facts")
        if type(self.authority_paths) is not frozenset or any(
            type(path) is not type(Path()) for path in self.authority_paths
        ):
            raise TypeError("authority paths must be an exact Path frozenset")
        if self.canonical_sidecar_path is not None and type(
            self.canonical_sidecar_path
        ) is not type(Path()):
            raise TypeError(
                "canonical sidecar path must be an exact Path or None"
            )
        if self.target_identity_fragment is not None and (
            type(self.target_identity_fragment) is not str
            or not self.target_identity_fragment
        ):
            raise TypeError(
                "target identity fragment must be a non-empty string or None"
            )


@dataclass(frozen=True)
class _RecoveryArtifactPaths:
    """Deterministic same-directory artifact family (Task 5.12 frozen)."""

    destination: Path
    manifest: Path
    jsonl_temp: Path
    manifest_temp: Path
    jsonl_recovery: Path
    manifest_recovery: Path

    def __post_init__(self) -> None:
        for value in (
            self.destination,
            self.manifest,
            self.jsonl_temp,
            self.manifest_temp,
            self.jsonl_recovery,
            self.manifest_recovery,
        ):
            if type(value) is not type(Path()):
                raise TypeError("artifact paths must be exact Path values")
        if self.manifest != self.destination.with_name(
            f"{self.destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ):
            raise ValueError("manifest path is not deterministic")


def _recovery_artifact_paths(destination: Path) -> _RecoveryArtifactPaths:
    if type(destination) is not type(Path()):
        raise TypeError("destination must be an exact Path")
    return _RecoveryArtifactPaths(
        destination=destination,
        manifest=destination.with_name(
            f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ),
        jsonl_temp=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_TEMP_SUFFIX}"
        ),
        manifest_temp=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_TEMP_SUFFIX}"
        ),
        jsonl_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_RECOVERY_SUFFIX}"
        ),
        manifest_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_RECOVERY_SUFFIX}"
        ),
    )


def _recovery_destination_safe(
    facts: _RefreshRecoveryFacts,
    issued: IssuedReceiptFacts,
) -> str | None:
    """Task 5.12-equivalent recovery-time destination safety proof.

    Returns ``None`` when the ledger destination pair is safe to read,
    publish, cancel or complete, or a stable error code when it is not.
    The rules mirror the Task 5.12 export preflight without importing
    ``tm_migration``: absolute deterministic adjacent names, no ``..``
    traversal or empty/dot name, every parent component a real
    directory (no symlink chain), a writable/executable immediate
    parent, and no alias with the configured JSONL/manifest, sidecar,
    activation journal/marker or their deterministic families.
    """

    destination = issued.destination_jsonl_path
    manifest = issued.destination_manifest_path
    if (
        not destination.is_absolute()
        or not manifest.is_absolute()
        or ".." in destination.parts
        or ".." in manifest.parts
        or destination.name in {"", ".", ".."}
        or manifest.name in {"", ".", ".."}
        or destination == manifest
        or manifest
        != destination.with_name(f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}")
    ):
        return "RECOVERY.EXPORT_PATH_INVALID"
    if facts.canonical_sidecar_path is not None:
        sidecar = facts.canonical_sidecar_path
        sidecar_fragment = facts.target_identity_fragment
    else:
        sidecar = None
        sidecar_fragment = None
    paths = _recovery_artifact_paths(destination)
    for candidate in (
        destination,
        manifest,
        paths.jsonl_temp,
        paths.manifest_temp,
        paths.jsonl_recovery,
        paths.manifest_recovery,
    ):
        if candidate in facts.authority_paths:
            return "RECOVERY.EXPORT_PATH_ALIASED"
        if (
            sidecar is not None
            and sidecar_fragment is not None
            and candidate.parent == sidecar.parent
        ):
            name = candidate.name
            if name.startswith(f".{sidecar.name}.localcat-"):
                return "RECOVERY.EXPORT_PATH_ALIASED"
            if name.startswith(".localcat-") and sidecar_fragment in name:
                return "RECOVERY.EXPORT_PATH_ALIASED"
    if not _parent_chain_safe(destination):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    return None


def _parent_chain_safe(destination: Path) -> bool:
    """Real non-symlink writable/executable parent chain for one file.

    Every path component from the root down to the immediate parent
    must be a real directory (``lstat``, so a symlinked component is
    rejected) and the immediate parent must be writable and executable.
    """

    parent = destination.parent
    chain = [parent]
    chain.extend(parent.parents)
    for candidate in reversed(chain):
        try:
            observed = os.lstat(candidate)
        except (OSError, ValueError):
            return False
        if not stat.S_ISDIR(observed.st_mode):
            return False
    try:
        observed = os.lstat(parent)
    except (OSError, ValueError):
        return False
    if not (
        observed.st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_mode
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        return False
    return True


def _artifact_parent_proof(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> str | None:
    """One strict identity-bound proof of the immediate parent chain.

    Returns ``None`` only when the full parent chain is real/
    non-symlink/writable/executable and the current immediate-parent
    device/inode still equals the exact identity recorded at issued
    registration; any other observation returns a stable blocker code.
    A missing legacy identity fails closed and is never inferred as new
    authority.
    """

    if expected_identity is None:
        return "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING"
    if not _parent_chain_safe(destination):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    try:
        observed = os.lstat(destination.parent)
    except (OSError, ValueError):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    if (observed.st_dev, observed.st_ino) != expected_identity:
        return "RECOVERY.EXPORT_PARENT_REPLACED"
    return None


def _open_recovery_parent_chain_no_follow(destination: Path) -> int:
    """Open ``destination.parent`` component-by-component from the root.

    Every pathname component from the filesystem root down to the
    immediate parent is opened with ``O_DIRECTORY|O_NOFOLLOW`` against
    the previously retained descriptor, so a symlinked, missing or
    non-directory component (including an ancestor swapped to a symlink
    between an earlier validation and this walk) fails the open instead
    of redirecting the descriptor into another tree.  The caller owns
    the returned descriptor.  Only usable where ``dir_fd``-relative
    opens are available; the caller maps any unsupported-platform
    failure to its fail-closed code.
    """

    parent = destination.parent
    if not parent.is_absolute():
        raise OSError("recovery parent path is not absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parent.parts[0], flags)
    try:
        for part in parent.parts[1:]:
            next_descriptor = os.open(
                part,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _RecoveryParentHandle:
    """Component-bound no-follow parent dirfd for terminal recovery.

    Binding walks every component of the advertised parent pathname
    from the filesystem root with ``O_DIRECTORY|O_NOFOLLOW`` per
    component (one retained descriptor per level), so an ancestor
    swapped to a symlink between the row validation and the cleanup can
    never redirect the retained descriptor into another tree.  The
    final descriptor must be a real writable/executable directory whose
    exact device/inode equals the durable handoff identity recorded at
    issued registration.  Every terminal artifact probe,
    reconstruction open/replace, cleanup unlink and the parent fsync
    resolve only deterministic basenames relative to this descriptor;
    the advertised parent pathname is never re-resolved for destructive
    work.  ``reprove`` re-walks the advertised pathname component-wise
    and requires it to still resolve to the retained identity, raising
    ``RecoveryError`` with the stable post-validation codes
    (``RECOVERY.EXPORT_PARENT_REPLACED`` for a replaced real directory,
    ``RECOVERY.CLEANUP_FAILED`` for an unsafe chain) so the caller
    keeps the handoff and fails closed.  The raw descriptor is private.
    """

    __slots__ = (
        "destination",
        "identity",
        "_descriptor",
        "_closed",
    )

    def __init__(
        self,
        destination: Path,
        descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        if type(destination) is not _NATIVE_PATH_TYPE:
            raise TypeError("destination must be an exact native Path")
        if type(descriptor) is not int or descriptor < 0:
            raise TypeError("parent descriptor must be a non-negative int")
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or type(identity[0]) is not int
            or type(identity[1]) is not int
        ):
            raise TypeError("parent identity must be a device/inode pair")
        self.destination = destination
        self._descriptor = descriptor
        self.identity = identity
        self._closed = False

    @classmethod
    def bind(
        cls,
        destination: Path,
        expected_identity: tuple[int, int],
    ) -> _RecoveryParentHandle:
        """Validate the full parent chain and retain one no-follow dirfd.

        The component-wise walk fails closed with
        ``RECOVERY.EXPORT_PARENT_UNSAFE`` on any missing, symlinked,
        non-directory or unsupported component (including platforms
        without component-safe binding) and
        ``RECOVERY.EXPORT_PARENT_REPLACED`` when the walked directory's
        exact device/inode differs from the durable handoff identity;
        a missing legacy identity fails closed with
        ``RECOVERY.EXPORT_PARENT_IDENTITY_MISSING``.
        """

        if (
            type(expected_identity) is not tuple
            or len(expected_identity) != 2
            or type(expected_identity[0]) is not int
            or type(expected_identity[1]) is not int
        ):
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING",
                retryable=False,
            )
        if not (
            hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
        ):
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_UNSAFE",
                retryable=False,
            )
        descriptor = -1
        try:
            descriptor = _open_recovery_parent_chain_no_follow(
                destination
            )
            observed = os.fstat(descriptor)
        except (OSError, TypeError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_UNSAFE",
                retryable=False,
            ) from None
        try:
            if not stat.S_ISDIR(observed.st_mode):
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_UNSAFE",
                    retryable=False,
                )
            if not (
                observed.st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                and observed.st_mode
                & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            ):
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_UNSAFE",
                    retryable=False,
                )
            if (observed.st_dev, observed.st_ino) != expected_identity:
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_REPLACED",
                    retryable=False,
                )
        except RecoveryError:
            os.close(descriptor)
            raise
        return cls(
            destination,
            descriptor,
            (observed.st_dev, observed.st_ino),
        )

    @property
    def descriptor(self) -> int:
        """Private descriptor access for dirfd-relative syscalls."""

        if self._closed:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )
        return self._descriptor

    def fsync(self) -> None:
        """Fsync the retained directory descriptor for durability."""

        try:
            os.fsync(self.descriptor)
        except OSError:
            raise RecoveryError(
                "RECOVERY.EXPORT_IO_FAILED",
                retryable=False,
            ) from None

    def reprove(self) -> None:
        """Re-walk the advertised pathname and require the identity.

        The advertised full parent pathname is re-proven
        component-by-component with ``O_DIRECTORY|O_NOFOLLOW`` per
        component, so an ancestor symlink can never be accepted as the
        proven chain.  A replaced real directory raises
        ``RECOVERY.EXPORT_PARENT_REPLACED`` (non-retryable) and an
        unsafe chain raises ``RECOVERY.CLEANUP_FAILED`` (retryable), so
        the caller keeps the handoff and fails closed without touching
        the moved/replaced parent or any attacker path.
        """

        descriptor = -1
        try:
            descriptor = _open_recovery_parent_chain_no_follow(
                self.destination
            )
            observed = os.fstat(descriptor)
        except (OSError, TypeError, ValueError):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not stat.S_ISDIR(observed.st_mode):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
        if not (
            observed.st_mode
            & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            and observed.st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
        if (observed.st_dev, observed.st_ino) != self.identity:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )

    def open(
        self,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        """Open one basename relative to the retained parent descriptor."""

        _require_recovery_basename(name)
        return os.open(
            name,
            flags,
            dir_fd=self.descriptor,
            mode=mode,
        )

    def lstat(self, name: str) -> os.stat_result:
        """No-follow stat of one basename relative to the retained fd."""

        _require_recovery_basename(name)
        return os.stat(
            name,
            dir_fd=self.descriptor,
            follow_symlinks=False,
        )

    def unlink(self, name: str) -> None:
        """Unlink one basename relative to the retained parent descriptor."""

        _require_recovery_basename(name)
        os.unlink(name, dir_fd=self.descriptor)

    def replace(self, source: str, destination: str) -> None:
        """Rename one basename to another relative to the retained fd."""

        _require_recovery_basename(source)
        _require_recovery_basename(destination)
        os.replace(
            source,
            destination,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )

    def close(self) -> None:
        """Best-effort close of the retained descriptor."""

        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._descriptor)
        except OSError:
            pass

    def __enter__(self) -> _RecoveryParentHandle:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()


def _require_recovery_basename(name: str) -> None:
    """One deterministic artifact name must be a pure safe basename."""

    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("recovery artifact name is not a safe basename")


def _after_recovery_parent_bound(
    destination: Path,
    parent_identity: tuple[int, int],
) -> None:
    """Late-bound seam invoked once the recovery parent dirfd is bound.

    Test-only fault-injection point: runs immediately after the parent
    descriptor has been bound and identity-proven against the durable
    handoff and before any descriptor-relative artifact probe or
    reconstruction, so a hostile parent rename/replacement exactly at
    that boundary can be reproduced without re-resolving the parent
    pathname.
    """


def _after_recovery_manifest_source_proved(
    destination: Path,
    manifest_temp_name: str,
    manifest_name: str,
    expected_source_identity: tuple[int, int],
) -> None:
    """Late-bound seam invoked immediately before the manifest replace.

    Runs after the exact handed-off manifest temp identity has been
    re-proven and immediately before the parent-relative rename, so a
    source-name swap exactly at that mutation boundary can be
    reproduced deterministically without re-resolving the parent
    pathname for destructive work.  Test-only fault-injection point.
    """


def _prove_recovery_manifest_source(
    manifest_temp: Path,
    manifest_temp_name: str,
    *,
    expected_digest: str,
    expected_identity: tuple[int, int],
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """Prove the durable manifest temp by exact digest AND handed-off inode.

    A same-byte foreign inode swapped into the temp slot after an
    earlier proof fails closed with ``RECOVERY.MANIFEST_TEMP_INVALID``
    before any rename; with a retained parent handle the capture is
    descriptor-relative.
    """

    if parent is not None:
        capture = _recovery_parent_capture(
            parent,
            manifest_temp_name,
            "RECOVERY_MANIFEST_TEMP",
        )
    else:
        capture = _capture_activation_file(
            manifest_temp,
            asset_kind="RECOVERY_MANIFEST_TEMP",
        )
    if capture.digest != expected_digest or (
        capture.identity.device,
        capture.identity.inode,
    ) != expected_identity:
        raise RecoveryError(
            "RECOVERY.MANIFEST_TEMP_INVALID",
            retryable=False,
        )


def _prove_recovery_manifest_destination(
    manifest_path: Path,
    *,
    expected_manifest_digest: str | None,
    handoff: _ArtifactHandoffFacts,
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """Prove the manifest destination against the durable handoff record.

    The destination must still match the exact prior manifest digest
    AND handed-off inode, or the recorded exact absence; any divergence
    -- including a same-byte foreign inode swapped in after an earlier
    proof -- fails closed with the existing
    ``RECOVERY.MANIFEST_DESTINATION_*`` codes before any rename, so a
    concurrent destination replacement in the late seam can never be
    silently overwritten.
    """

    if expected_manifest_digest is None:
        if handoff.prior_jsonl_absent is not True:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        if handoff.prior_manifest_absent is not True:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        try:
            if parent is not None:
                parent.lstat(manifest_path.name)
            else:
                os.lstat(manifest_path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNSAFE",
                retryable=False,
            ) from error
        else:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_CHANGED",
                retryable=False,
            )
    else:
        if (
            handoff.prior_manifest_absent is not False
            or handoff.prior_manifest_digest
            != expected_manifest_digest
            or handoff.prior_manifest_identity is None
        ):
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        state, digest, identity = _strict_file_state(
            manifest_path,
            parent=parent,
        )
        if state != "present" or digest != expected_manifest_digest:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_CHANGED",
                retryable=False,
            )
        if identity != handoff.prior_manifest_identity:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )


def _bind_recovery_parent(
    receipt: IssuedReceiptFacts,
    handoff: _ArtifactHandoffFacts | None,
) -> _RecoveryParentHandle | None:
    """Bind the durable parent dirfd for one receipt's recovery work.

    Returns ``None`` when the receipt carries no cleanup-pending
    handoff (the caller keeps the pre-existing path-based fallback and
    its unprovable/conflict closure); a handoff without a recorded
    parent identity fails closed with
    ``RECOVERY.EXPORT_PARENT_IDENTITY_MISSING``.  ``_after_recovery_parent_bound``
    is the late-bound fault-injection seam (race reproduction exactly
    at the bind boundary).
    """

    if handoff is None:
        return None
    if handoff.artifact_parent_identity is None:
        raise RecoveryError(
            "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING",
            retryable=False,
        )
    handle = _RecoveryParentHandle.bind(
        receipt.destination_jsonl_path,
        handoff.artifact_parent_identity,
    )
    _after_recovery_parent_bound(
        receipt.destination_jsonl_path,
        handoff.artifact_parent_identity,
    )
    return handle


class _SnapshotRecoveryPort(Protocol):
    """Narrow store seam consumed by this module (never the store module).

    Every read returns one SQLite snapshot; every effect is atomic in
    one transaction; expected store failures are converted to
    ``RecoveryError`` by the adapter.  Unexpected programmer errors
    propagate unchanged.
    """

    def read_recovery_facts(self) -> _RefreshRecoveryFacts: ...

    def cancel_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None: ...

    def complete_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int] | None = None,
        manifest_identity: tuple[int, int] | None = None,
    ) -> None: ...

    def complete_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
    ) -> None: ...

    def cancel_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None: ...

    def clear_issued_receipt_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None: ...

    def latch_source_divergence(self, expected_fingerprint: str) -> bool: ...


def _strict_file_state(
    path: Path,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """One descriptor-based no-follow proof of a configured pair entry.

    Returns ``("absent", None, None)`` when the path does not exist,
    ``("unsafe", None, None)`` when the path is a symlink, directory,
    multi-link entry, unreadable, or whose terminal identity is not
    stable, and ``("present", digest, identity)`` only for a regular
    single-link file whose bytes are hashed through the descriptor and
    whose terminal ``lstat`` still reports the same device/inode.
    Pathname hashing is never used, so a foreign same-byte inode cannot
    masquerade as a stable owned entry.  With a retained parent handle
    the probe resolves the basename relative to that descriptor and
    never re-resolves the parent pathname.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        if parent is not None:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | no_follow,
                dir_fd=parent.descriptor,
            )
        else:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
    except FileNotFoundError:
        return ("absent", None, None)
    except OSError:
        return ("unsafe", None, None)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            return ("unsafe", None, None)
        identity = (observed.st_dev, observed.st_ino)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        return ("unsafe", None, None)
    finally:
        os.close(descriptor)
    try:
        if parent is not None:
            final = os.stat(
                path.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        else:
            final = os.lstat(path)
    except OSError:
        return ("unsafe", None, None)
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != identity
    ):
        return ("unsafe", None, None)
    return ("present", digest.hexdigest(), identity)


def _path_exists(
    path: Path,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> bool:
    try:
        if parent is not None:
            os.stat(
                path.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        else:
            os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _manifest_for_receipt(
    receipt: SnapshotReceipt,
    kind: SnapshotKind,
) -> SnapshotManifest:
    """Deterministic adjacent manifest reconstructed from one receipt."""

    return SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=kind,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )


def _manifest_bytes(manifest: SnapshotManifest) -> bytes:
    return contract_to_json(manifest).encode("utf-8")


def _manifest_digest_for_receipt(receipt: SnapshotReceipt) -> str:
    return hashlib.sha256(
        _manifest_bytes(
            _manifest_for_receipt(receipt, SnapshotKind.EXPLICIT_EXPORT)
        )
    ).hexdigest()


def _record_count_at(
    facts: _RefreshRecoveryFacts,
    exported_revision: int,
) -> int | None:
    cumulative = dict(facts.cumulative_record_counts)
    cumulative[0] = 0
    return cumulative.get(exported_revision)


def _issued_refresh_receipts(
    facts: _RefreshRecoveryFacts,
) -> tuple[IssuedReceiptFacts, ...]:
    return tuple(
        receipt
        for receipt in facts.receipts
        if receipt.status == "issued"
        and receipt.destination_jsonl_path
        == facts.configured_jsonl_path
        and receipt.destination_manifest_path
        == facts.snapshot_manifest_path
    )


def _issued_export_receipts(
    facts: _RefreshRecoveryFacts,
) -> tuple[IssuedReceiptFacts, ...]:
    return tuple(
        receipt
        for receipt in facts.receipts
        if receipt.status == "issued"
        and not (
            receipt.destination_jsonl_path
            == facts.configured_jsonl_path
            and receipt.destination_manifest_path
            == facts.snapshot_manifest_path
        )
    )


@dataclass(frozen=True)
class _RefreshDecision:
    """One deterministic classification of the issued refresh set."""

    action: str
    receipts: tuple[IssuedReceiptFacts, ...] = ()
    receipt: IssuedReceiptFacts | None = None
    jsonl_identity: tuple[int, int] | None = None
    manifest_identity: tuple[int, int] | None = None
    expected_manifest_digest: str | None = None
    error_code: str | None = None


def _classify_refresh_receipts(
    facts: _RefreshRecoveryFacts,
    issued: tuple[IssuedReceiptFacts, ...],
) -> _RefreshDecision:
    """Classify one issued refresh receipt set against durable facts.

    The pair is classified strictly in the design matrix order: the old
    completed pair is checked first (cancel), then the issued
    receipt/manifest pair (complete), then the issued JSONL with an old
    or absent manifest (reconstruct), and every other observation
    durably latches ``SOURCE_DIVERGED``.
    """

    if facts.binding_invalid:
        return _RefreshDecision(
            action="diverged",
            receipts=issued,
            error_code="RECOVERY.BINDING_INVALID",
        )
    for receipt in issued:
        if (
            receipt.resource_id != facts.resource_id
            or receipt.canonical_store_id != facts.canonical_store_id
        ):
            return _RefreshDecision(
                action="diverged",
                receipts=issued,
                error_code="RECOVERY.LEDGER_IDENTITY_INVALID",
            )
        if (
            receipt.destination_jsonl_path
            != facts.configured_jsonl_path
            or receipt.destination_manifest_path
            != facts.snapshot_manifest_path
        ):
            return _RefreshDecision(
                action="diverged",
                receipts=issued,
                error_code="RECOVERY.LEDGER_PATH_INVALID",
            )
        try:
            receipt.receipt
        except (TypeError, ValueError):
            return _RefreshDecision(
                action="diverged",
                receipts=issued,
                error_code="RECOVERY.LEDGER_RECEIPT_INVALID",
            )
        expected_count = _record_count_at(
            facts,
            receipt.exported_revision,
        )
        if (
            expected_count is None
            or receipt.record_count != expected_count
        ):
            return _RefreshDecision(
                action="diverged",
                receipts=issued,
                error_code="RECOVERY.ANCESTRY_INVALID",
            )
    jsonl_state, jsonl_digest, jsonl_identity = _strict_file_state(
        facts.configured_jsonl_path
    )
    manifest_state, manifest_digest, manifest_identity = _strict_file_state(
        facts.snapshot_manifest_path
    )
    if jsonl_state == "unsafe" or manifest_state == "unsafe":
        return _RefreshDecision(
            action="diverged",
            receipts=issued,
            error_code="RECOVERY.PAIR_UNSAFE",
        )
    old_manifest_digest: str | None = None
    if facts.binding is not None:
        old_manifest_digest = hashlib.sha256(
            _manifest_bytes(facts.binding.manifest)
        ).hexdigest()
    old_pair_matches = (
        facts.binding is not None
        and jsonl_state == "present"
        and jsonl_digest == facts.binding.receipt.jsonl_digest
        and manifest_state == "present"
        and manifest_digest == old_manifest_digest
    )
    if old_pair_matches:
        return _RefreshDecision(action="cancel", receipts=issued)
    issued_manifest_digests = {
        receipt.snapshot_id: _manifest_digest_for_receipt(
            receipt.receipt
        )
        for receipt in issued
    }
    complete_matches = tuple(
        receipt
        for receipt in issued
        if jsonl_state == "present"
        and jsonl_digest == receipt.jsonl_digest
        and manifest_state == "present"
        and manifest_digest == issued_manifest_digests[receipt.snapshot_id]
    )
    if len(complete_matches) == 1:
        return _RefreshDecision(
            action="complete",
            receipt=complete_matches[0],
            jsonl_identity=jsonl_identity,
            manifest_identity=manifest_identity,
        )
    if len(complete_matches) > 1:
        return _RefreshDecision(
            action="diverged",
            receipts=issued,
            error_code="RECOVERY.AMBIGUOUS_PAIR",
        )
    jsonl_matches = tuple(
        receipt
        for receipt in issued
        if jsonl_state == "present"
        and jsonl_digest == receipt.jsonl_digest
    )
    if len(jsonl_matches) == 1:
        if manifest_state == "absent" or (
            manifest_state == "present"
            and manifest_digest == old_manifest_digest
        ):
            return _RefreshDecision(
                action="reconstruct",
                receipt=jsonl_matches[0],
                jsonl_identity=jsonl_identity,
                expected_manifest_digest=(
                    None
                    if manifest_state == "absent"
                    else old_manifest_digest
                ),
            )
        return _RefreshDecision(
            action="diverged",
            receipts=issued,
            error_code="RECOVERY.MANIFEST_FOREIGN",
        )
    if len(jsonl_matches) > 1:
        return _RefreshDecision(
            action="diverged",
            receipts=issued,
            error_code="RECOVERY.AMBIGUOUS_JSONL",
        )
    return _RefreshDecision(
        action="diverged",
        receipts=issued,
        error_code="RECOVERY.PAIR_UNMATCHED",
    )


def _publish_reconstructed_manifest(
    destination: Path,
    manifest_path: Path,
    receipt: SnapshotReceipt,
    *,
    expected_manifest_digest: str | None,
    handoff: _ArtifactHandoffFacts | None,
    parent: _RecoveryParentHandle | None = None,
) -> tuple[int, int]:
    """Publish the exact ledger-reconstructed manifest for one receipt.

    The reconstruction is restart-idempotent and never creates an
    unjournaled temporary: the durable handoff is required before any
    probe, and the deterministic ``manifest_temp`` entry is reused in
    place when (and only when) it carries the exact expected digest AND
    the exact handed-off ``manifest_temp_identity``.  The handed-off
    inode was file-fsynced by the publisher before the handoff journal
    existed, so it is carried straight through the strict re-open
    identity/digest proof, the strict destination proof (exact old
    digest and regular single-link entry, or provable absence), atomic
    replace, parent fsync and a terminal identity/digest re-proof; a
    crash at any boundary therefore leaves a state a later recovery can
    replay against the same durable inode.  An absent, unsafe,
    conflicting or identity-unproven temp fails closed without creating
    a replacement, so process death can never strand an unjournaled
    owned artifact.  The old manifest entry is replaced only against
    the durable handoff prior record: an existing manifest must match
    the recorded prior digest AND identity, and an absent manifest slot
    requires the recorded prior absence proof; any unrecorded prior
    state fails closed as ``RECOVERY.MANIFEST_DESTINATION_UNPROVEN``.
    Any failure removes nothing this call created and fails closed,
    leaving the destination and the durable handed-off temp untouched
    for an idempotent later replay.  With a retained parent handle the
    advertised pathname is re-proven before the first destructive step
    and every probe, replace, parent fsync and cleanup resolves the
    deterministic basenames relative to that descriptor, so a
    renamed/replaced parent can never redirect the reconstruction.
    """

    _recovery_cleanup_parent_gate(destination, parent)
    paths = _recovery_artifact_paths(destination)
    manifest = _manifest_for_receipt(
        receipt,
        SnapshotKind.EXPLICIT_EXPORT,
    )
    payload = _manifest_bytes(manifest)
    expected_digest = hashlib.sha256(payload).hexdigest()
    if handoff is None:
        raise RecoveryError(
            "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
            retryable=False,
        )
    expected_identity = _artifact_expected_identity(
        handoff,
        "manifest_temp",
    )
    stale_state, stale_digest, stale_identity = _strict_file_state(
        paths.manifest_temp,
        parent=parent,
    )
    if stale_state == "present":
        if stale_digest is None:
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_UNSAFE",
                retryable=False,
            )
        if stale_digest != expected_digest:
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_CONFLICT",
                retryable=False,
            )
        if (
            expected_identity is None
            or stale_identity is None
            or stale_identity != expected_identity
        ):
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_UNPROVEN",
                retryable=False,
            )
        temp_identity = stale_identity
    elif stale_state == "unsafe":
        raise RecoveryError(
            "RECOVERY.MANIFEST_TEMP_UNSAFE",
            retryable=False,
        )
    else:
        raise RecoveryError(
            "RECOVERY.MANIFEST_TEMP_UNPROVEN",
            retryable=False,
        )
    try:
        _prove_recovery_manifest_source(
            paths.manifest_temp,
            paths.manifest_temp.name,
            expected_digest=expected_digest,
            expected_identity=temp_identity,
            parent=parent,
        )
        _prove_recovery_manifest_destination(
            manifest_path,
            expected_manifest_digest=expected_manifest_digest,
            handoff=handoff,
            parent=parent,
        )
        _prove_recovery_manifest_source(
            paths.manifest_temp,
            paths.manifest_temp.name,
            expected_digest=expected_digest,
            expected_identity=temp_identity,
            parent=parent,
        )
        _after_recovery_manifest_source_proved(
            destination,
            paths.manifest_temp.name,
            manifest_path.name,
            temp_identity,
        )
        _prove_recovery_manifest_source(
            paths.manifest_temp,
            paths.manifest_temp.name,
            expected_digest=expected_digest,
            expected_identity=temp_identity,
            parent=parent,
        )
        _prove_recovery_manifest_destination(
            manifest_path,
            expected_manifest_digest=expected_manifest_digest,
            handoff=handoff,
            parent=parent,
        )
        if parent is not None:
            parent.replace(
                paths.manifest_temp.name,
                manifest_path.name,
            )
            parent.fsync()
        else:
            os.replace(paths.manifest_temp, manifest_path)
            _fsync_activation_directory(manifest_path.parent)
        if parent is not None:
            published = _recovery_parent_capture(
                parent,
                manifest_path.name,
                "RECOVERY_MANIFEST",
            )
        else:
            published = _capture_activation_file(
                manifest_path,
                asset_kind="RECOVERY_MANIFEST",
            )
        if published.digest != expected_digest or (
            published.identity.device,
            published.identity.inode,
        ) != temp_identity:
            raise RecoveryError(
                "RECOVERY.MANIFEST_PUBLISH_VERIFY_FAILED",
                retryable=False,
            )
        return (published.identity.device, published.identity.inode)
    except RecoveryError:
        raise
    except (OSError, ActivationPreparationError) as error:
        raise RecoveryError(
            "RECOVERY.MANIFEST_PUBLISH_FAILED",
            retryable=True,
        ) from error


def _handoff_for(
    facts: _RefreshRecoveryFacts,
    snapshot_id: str,
) -> _ArtifactHandoffFacts | None:
    """The durable ownership record for one receipt, if any."""

    for handoff in facts.handoffs:
        if handoff.snapshot_id == snapshot_id:
            return handoff
    return None


def _artifact_expected_identity(
    handoff: _ArtifactHandoffFacts | None,
    artifact: str,
) -> tuple[int, int] | None:
    """The durably handed-off identity for one artifact, if any."""

    if handoff is None:
        return None
    return {
        "jsonl_temp": handoff.jsonl_temp_identity,
        "manifest_temp": handoff.manifest_temp_identity,
        "jsonl_recovery": handoff.jsonl_recovery_identity,
        "manifest_recovery": handoff.manifest_recovery_identity,
    }[artifact]


def _unproven_artifact_code(
    handoff: _ArtifactHandoffFacts | None,
    expectations: tuple[tuple[Path, set[str], str], ...],
    *,
    parent: _RecoveryParentHandle | None = None,
) -> str | None:
    """Blocking error when an existing artifact matches expected content
    but has no durable ownership proof.

    Content equality alone is never ownership: a same-byte foreign
    inode without the exact handed-off identity is unprovable and must
    be preserved, never unlinked or replaced.  With a retained parent
    handle the probe resolves the basename relative to that descriptor.
    """

    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path, parent=parent)
        if state == "absent":
            continue
        if state != "present" or digest not in digests:
            continue
        expected_identity = _artifact_expected_identity(
            handoff,
            artifact,
        )
        if expected_identity is None or identity != expected_identity:
            return "RECOVERY.ARTIFACT_UNPROVEN"
    return None


def _prior_handoff_digests(
    handoff: _ArtifactHandoffFacts | None,
    artifact: str,
) -> set[str]:
    """The durably recorded prior-pair digest set for one recovery copy.

    Only a recorded present prior entry (``absent=False`` with a
    64-hex digest) yields a digest; an unrecorded or recorded-absent
    prior state yields an empty set so an existing copy can never be
    proven and fails closed.
    """

    if handoff is None:
        return set()
    if artifact == "jsonl_recovery":
        digest = handoff.prior_jsonl_digest
        if handoff.prior_jsonl_absent is False and digest is not None:
            return {digest}
        return set()
    if artifact == "manifest_recovery":
        digest = handoff.prior_manifest_digest
        if handoff.prior_manifest_absent is False and digest is not None:
            return {digest}
        return set()
    raise ValueError(f"unknown recovery artifact {artifact!r}")


def _export_artifact_expectations(
    destination: Path,
    issued: IssuedReceiptFacts,
    *,
    handoff: _ArtifactHandoffFacts | None,
) -> tuple[tuple[Path, set[str], str], ...]:
    """Deterministic artifact expectations for one issued export."""

    paths = _recovery_artifact_paths(destination)
    manifest_digest = _manifest_digest_for_receipt(issued.receipt)
    return (
        (paths.jsonl_temp, {issued.jsonl_digest}, "jsonl_temp"),
        (paths.manifest_temp, {manifest_digest}, "manifest_temp"),
        (
            paths.jsonl_recovery,
            _prior_handoff_digests(handoff, "jsonl_recovery"),
            "jsonl_recovery",
        ),
        (
            paths.manifest_recovery,
            _prior_handoff_digests(handoff, "manifest_recovery"),
            "manifest_recovery",
        ),
    )


def _refresh_artifact_expectations(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
    *,
    handoff: _ArtifactHandoffFacts | None,
) -> tuple[tuple[Path, set[str], str], ...]:
    """Deterministic artifact expectations for one issued refresh."""

    paths = _recovery_artifact_paths(facts.configured_jsonl_path)
    manifest_digest = _manifest_digest_for_receipt(receipt.receipt)
    return (
        (paths.jsonl_temp, {receipt.jsonl_digest}, "jsonl_temp"),
        (paths.manifest_temp, {manifest_digest}, "manifest_temp"),
        (
            paths.jsonl_recovery,
            _prior_handoff_digests(handoff, "jsonl_recovery"),
            "jsonl_recovery",
        ),
        (
            paths.manifest_recovery,
            _prior_handoff_digests(handoff, "manifest_recovery"),
            "manifest_recovery",
        ),
    )


def _unproven_artifact_code_for_receipt(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> str | None:
    """First unproven-artifact blocker across one receipt's expectations."""

    handoff = _handoff_for(facts, receipt.snapshot_id)
    if receipt.destination_jsonl_path == facts.configured_jsonl_path:
        expectations = _refresh_artifact_expectations(
            facts,
            receipt,
            handoff=handoff,
        )
    else:
        expectations = _export_artifact_expectations(
            receipt.destination_jsonl_path,
            receipt,
            handoff=handoff,
        )
    return _unproven_artifact_code(handoff, expectations, parent=parent)


def _remove_owned_recovery_artifact(
    parent: _RecoveryParentHandle,
    name: str,
    expected_identity: _ActivationFileIdentity,
) -> bool:
    """Remove exactly one owned deterministic artifact relative to the
    retained parent descriptor; True only when provably absent after.

    The unlink, parent fsync and absence re-proof all resolve the
    basename relative to the retained descriptor, so a parent renamed
    or replaced after the bind can never redirect the unlink into the
    replacement directory.  Any observation other than the exact
    single-link owned inode returns False and the caller fails closed.
    """

    try:
        observed = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != (expected_identity.device, expected_identity.inode)
    ):
        return False
    try:
        os.unlink(name, dir_fd=parent.descriptor)
    except OSError:
        return False
    try:
        os.fsync(parent.descriptor)
    except OSError:
        return False
    try:
        os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class _RecoveryFileCapture:
    """Strict descriptor-relative capture of one recovery entry."""

    asset_kind: str
    identity: _ActivationFileIdentity
    digest: str


def _recovery_parent_capture(
    parent: _RecoveryParentHandle,
    name: str,
    asset_kind: str,
) -> _RecoveryFileCapture:
    """One strict single-link capture relative to the retained parent.

    Mirrors the activation-journal capture semantics (regular
    single-link closure at the initial stat, the descriptor fstat/read
    and the final revalidation) but resolves the entry only relative to
    the retained descriptor, so a swapped parent pathname can never
    substitute a foreign entry.
    """

    try:
        initial = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow,
            dir_fd=parent.descriptor,
        )
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        identity = _ActivationFileIdentity(
            observed.st_dev,
            observed.st_ino,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        final = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino)
        != (identity.device, identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    return _RecoveryFileCapture(
        asset_kind=asset_kind,
        identity=identity,
        digest=digest.hexdigest(),
    )


def _recovery_parent_open_exclusive(
    parent: _RecoveryParentHandle,
    name: str,
) -> tuple[int, _ActivationFileIdentity]:
    """Create one exclusive deterministic temporary relative to the
    retained parent descriptor; returns the open descriptor and the
    exact created identity."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        name,
        flags,
        0o600,
        dir_fd=parent.descriptor,
    )
    try:
        observed = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        os.close(descriptor)
        raise OSError("recovery temporary is not an exclusive regular file")
    return descriptor, _ActivationFileIdentity(
        observed.st_dev,
        observed.st_ino,
    )


def _remove_content_proven_artifact(
    path: Path,
    expected_digest: str,
    *,
    expected_identity: tuple[int, int] | None,
    parent: _RecoveryParentHandle | None = None,
) -> bool:
    """Remove one deterministic artifact proven by strict content proof
    plus the durable handed-off identity.

    The unlink is identity-bound to the proven inode with a parent
    fsync and an absence re-proof.  A foreign, linked, unsafe,
    digest-mismatched or identity-less entry fails closed and is never
    removed.  With a retained parent handle the probe, unlink, fsync
    and absence re-proof resolve the basename relative to that
    descriptor and never re-resolve the parent pathname.
    """

    state, digest, identity = _strict_file_state(
        path,
        parent=parent,
    )
    if state == "absent":
        return True
    if (
        state != "present"
        or digest != expected_digest
        or expected_identity is None
        or identity is None
        or identity != expected_identity
    ):
        return False
    if parent is not None:
        return _remove_owned_recovery_artifact(
            parent,
            path.name,
            _ActivationFileIdentity(identity[0], identity[1]),
        )
    return _remove_owned_activation_journal_temp(
        path,
        _ActivationFileIdentity(identity[0], identity[1]),
    )


def _recovery_cleanup_parent_gate(
    destination: Path,
    parent: _RecoveryParentHandle | None,
) -> None:
    """Fail closed before any destructive probe when the advertised
    parent pathname can no longer be proven.

    Runs after the row validation, before the first descriptor-relative
    probe or unlink.  A replaced real directory fails closed with
    ``RECOVERY.EXPORT_PARENT_REPLACED`` without touching either the
    moved original or the replacement, and an unsafe chain (symlinked,
    missing, unwritable) fails closed with ``RECOVERY.CLEANUP_FAILED``
    so the cleanup-pending handoff is kept for a later idempotent
    replay.
    """

    if parent is not None:
        parent.reprove()


def _cleanup_refresh_artifacts(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
    *,
    handoff: _ArtifactHandoffFacts | None,
    parent: _RecoveryParentHandle | None = None,
) -> tuple[str, ...]:
    """Clean owned refresh artifacts after one durable outcome.

    Every removed artifact must be proven by its exact digest and the
    exact handed-off identity of this receipt; a matching same-byte
    inode without this receipt's handoff proof fails closed
    (``RECOVERY.ARTIFACT_UNPROVEN``) and is never removed, and an owned
    artifact that cannot be removed raises ``RECOVERY.CLEANUP_FAILED``
    so the cleanup-pending journal is kept for a later idempotent
    replay.  Any foreign, conflicting or unprovable entry (different
    bytes, unsafe symlink/directory/hardlink/unreadable entry, or an
    entry whose digest set cannot be proven) raises
    ``RECOVERY.ARTIFACT_CONFLICT`` / ``RECOVERY.EXPORT_CLEANUP_UNPROVABLE``
    instead of being appended as a diagnostic: the terminal replay
    reports ``BLOCKED`` and preserves both the foreign entry and the
    cleanup-pending journal, and never reports COMPLETED/CANCELLED for
    a receipt whose deterministic artifacts are not all proven absent.
    With a retained parent handle every probe and unlink resolves the
    basename relative to that descriptor and the advertised parent
    pathname is re-proven before the first destructive step.
    """

    _recovery_cleanup_parent_gate(receipt.destination_jsonl_path, parent)
    diagnostics: list[str] = []
    expectations = _refresh_artifact_expectations(
        facts,
        receipt,
        handoff=handoff,
    )
    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path, parent=parent)
        if state == "absent":
            continue
        if not digests:
            raise RecoveryError(
                "RECOVERY.EXPORT_CLEANUP_UNPROVABLE",
                retryable=False,
            )
        if state != "present" or digest not in digests:
            raise RecoveryError(
                "RECOVERY.ARTIFACT_CONFLICT",
                retryable=False,
            )
        expected_identity = _artifact_expected_identity(handoff, artifact)
        if expected_identity is None or identity != expected_identity:
            raise RecoveryError(
                "RECOVERY.ARTIFACT_UNPROVEN",
                retryable=False,
            )
        if not _remove_content_proven_artifact(
            path,
            digest,
            expected_identity=expected_identity,
            parent=parent,
        ):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
    return tuple(sorted(set(diagnostics)))


def _cleanup_export_artifacts(
    issued: IssuedReceiptFacts,
    *,
    handoff: _ArtifactHandoffFacts | None,
    parent: _RecoveryParentHandle | None = None,
) -> tuple[str, ...]:
    """Clean owned export artifacts after one durable outcome.

    Same digest-plus-handoff-identity ownership proof as the refresh
    cleanup; unprovable matching entries fail closed and are preserved,
    and an owned artifact that cannot be removed raises
    ``RECOVERY.CLEANUP_FAILED`` so the cleanup-pending journal is kept.
    Any foreign, conflicting or unprovable entry raises
    ``RECOVERY.ARTIFACT_CONFLICT`` / ``RECOVERY.EXPORT_CLEANUP_UNPROVABLE``
    (never a diagnostic-and-continue), so the terminal replay reports
    ``BLOCKED``, preserves the foreign entry and keeps the handoff.
    With a retained parent handle every probe and unlink resolves the
    basename relative to that descriptor and the advertised parent
    pathname is re-proven before the first destructive step.
    """

    _recovery_cleanup_parent_gate(
        issued.destination_jsonl_path,
        parent,
    )
    diagnostics: list[str] = []
    expectations = _export_artifact_expectations(
        issued.destination_jsonl_path,
        issued,
        handoff=handoff,
    )
    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path, parent=parent)
        if state == "absent":
            continue
        if not digests:
            raise RecoveryError(
                "RECOVERY.EXPORT_CLEANUP_UNPROVABLE",
                retryable=False,
            )
        if state != "present" or digest not in digests:
            raise RecoveryError(
                "RECOVERY.ARTIFACT_CONFLICT",
                retryable=False,
            )
        expected_identity = _artifact_expected_identity(handoff, artifact)
        if expected_identity is None or identity != expected_identity:
            raise RecoveryError(
                "RECOVERY.ARTIFACT_UNPROVEN",
                retryable=False,
            )
        if not _remove_content_proven_artifact(
            path,
            digest,
            expected_identity=expected_identity,
            parent=parent,
        ):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
    return tuple(sorted(set(diagnostics)))


def _prior_export_pairs(
    facts: _RefreshRecoveryFacts,
    issued: IssuedReceiptFacts,
) -> dict[str, tuple[str, str]]:
    """Prior same-destination receipt pairs provable from the ledger.

    A prior row is usable only when its resource/canonical identity
    matches the live store, its receipt is well-formed, its revision
    ancestry is exact, and it completed at the same destination pair.
    Cancelled rows have no publication authority: a cancelled receipt
    can never cancel a later issued receipt nor grant it ownership of
    the destination pair.  Any invalid or foreign prior row is excluded
    so an old-pair cancellation or manifest expectation can never be
    derived from tampered ledger state.
    """

    result: dict[str, tuple[str, str]] = {}
    for row in facts.receipts:
        if (
            row.status != "completed"
            or row.snapshot_id == issued.snapshot_id
            or row.destination_jsonl_path
            != issued.destination_jsonl_path
            or row.destination_manifest_path
            != issued.destination_manifest_path
            or row.resource_id != facts.resource_id
            or row.canonical_store_id != facts.canonical_store_id
        ):
            continue
        try:
            manifest_digest = _manifest_digest_for_receipt(row.receipt)
        except (TypeError, ValueError):
            continue
        expected_count = _record_count_at(facts, row.exported_revision)
        if (
            expected_count is None
            or row.record_count != expected_count
        ):
            continue
        result[row.snapshot_id] = (row.jsonl_digest, manifest_digest)
    return result


@dataclass(frozen=True)
class _ExportReconciliation:
    result: IssuedReceiptRecovery
    diagnostics: tuple[str, ...] = ()


def _cleanup_and_release_export(
    port: _SnapshotRecoveryPort,
    facts: _RefreshRecoveryFacts,
    issued: IssuedReceiptFacts,
    handoff: _ArtifactHandoffFacts | None,
    reconciliation: _ExportReconciliation,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> _ExportReconciliation:
    """Remove owned artifacts and release the handoff after a terminal
    export transition.

    Runs after the receipt is durably completed/cancelled: the cleanup
    removes only digest-plus-handoff-identity proven artifacts (raising
    ``RECOVERY.CLEANUP_FAILED`` when an owned artifact cannot be
    removed), and the handoff journal is released only then.  A
    failure propagates as ``RecoveryError`` so the caller reports
    ``BLOCKED`` and keeps the handoff for an idempotent later replay.
    With a retained parent handle the cleanup, fsync and re-proof
    resolve relative to that descriptor and never re-resolve the parent
    pathname.
    """

    diagnostics = _cleanup_export_artifacts(
        issued,
        handoff=handoff,
        parent=parent,
    )
    if handoff is not None:
        _fsync_artifact_parent(
            issued.destination_jsonl_path,
            handoff.artifact_parent_identity,
            parent=parent,
        )
        port.clear_issued_receipt_handoff(
            issued.snapshot_id,
            expected_generation=facts.generation,
        )
    return _ExportReconciliation(reconciliation.result, diagnostics)


def _reconcile_one_export(
    port: _SnapshotRecoveryPort,
    facts: _RefreshRecoveryFacts,
    issued: IssuedReceiptFacts,
) -> _ExportReconciliation:
    """Reconcile one issued arbitrary-destination export receipt.

    Arbitrary exports reuse the crash protocol but never rebind and
    never touch resource divergence under a valid binding: an unsafe or
    unprovable window fails closed with evidence preserved, and a
    same-byte foreign swap between classification and the completion
    transaction fails closed because the exact captured destination
    identities are required again inside the store transaction.  The
    prior destination pair is the durable handoff record (or a
    completed-only ledger prior at the same destination); an unrecorded
    prior state can never cancel or reconstruct and fails closed.
    """

    handoff = _handoff_for(facts, issued.snapshot_id)
    if (
        issued.resource_id != facts.resource_id
        or issued.canonical_store_id != facts.canonical_store_id
    ):
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                ("RECOVERY.EXPORT_LEDGER_IDENTITY_INVALID",),
            )
        )
    destination_error = _recovery_destination_safe(facts, issued)
    if destination_error is not None:
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                (destination_error,),
            )
        )
    try:
        issued.receipt
    except (TypeError, ValueError):
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                ("RECOVERY.EXPORT_RECEIPT_INVALID",),
            )
        )
    expected_count = _record_count_at(facts, issued.exported_revision)
    if expected_count is None or issued.record_count != expected_count:
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                ("RECOVERY.EXPORT_ANCESTRY_INVALID",),
            )
        )
    try:
        parent_handle = _bind_recovery_parent(issued, handoff)
    except RecoveryError as error:
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                (error.error_code,),
            )
        )
    try:
        try:
            jsonl_state, jsonl_digest, jsonl_identity = _strict_file_state(
                issued.destination_jsonl_path,
                parent=parent_handle,
            )
            manifest_state, manifest_digest, manifest_identity = (
                _strict_file_state(
                    issued.destination_manifest_path,
                    parent=parent_handle,
                )
            )
            if jsonl_state == "unsafe" or manifest_state == "unsafe":
                return _ExportReconciliation(
                    IssuedReceiptRecovery(
                        issued.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PAIR_UNSAFE",),
                    )
                )
            issued_manifest_digest = _manifest_digest_for_receipt(
                issued.receipt
            )
            unproven = _unproven_artifact_code_for_receipt(
                facts,
                issued,
                parent=parent_handle,
            )
            if unproven is not None:
                return _ExportReconciliation(
                    IssuedReceiptRecovery(
                        issued.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        (unproven,),
                    )
                )
            if (
                jsonl_state == "present"
                and jsonl_digest == issued.jsonl_digest
                and manifest_state == "present"
                and manifest_digest == issued_manifest_digest
            ):
                assert jsonl_identity is not None
                assert manifest_identity is not None
                port.complete_issued_export_receipt(
                    issued.snapshot_id,
                    expected_generation=facts.generation,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )
                return _cleanup_and_release_export(
                    port,
                    facts,
                    issued,
                    handoff,
                    _ExportReconciliation(
                        IssuedReceiptRecovery(
                            issued.snapshot_id,
                            RefreshRecoveryState.COMPLETED,
                        )
                    ),
                    parent=parent_handle,
                )
            if (
                jsonl_state == "present"
                and jsonl_digest == issued.jsonl_digest
            ):
                if manifest_state == "absent":
                    expected_manifest_digest = None
                elif (
                    handoff is not None
                    and handoff.prior_manifest_absent is False
                    and manifest_digest
                    == handoff.prior_manifest_digest
                ):
                    expected_manifest_digest = manifest_digest
                else:
                    return _ExportReconciliation(
                        IssuedReceiptRecovery(
                            issued.snapshot_id,
                            RefreshRecoveryState.BLOCKED,
                            ("RECOVERY.EXPORT_MANIFEST_FOREIGN",),
                        )
                    )
                published_manifest_identity = _publish_reconstructed_manifest(
                    issued.destination_jsonl_path,
                    issued.destination_manifest_path,
                    issued.receipt,
                    expected_manifest_digest=expected_manifest_digest,
                    handoff=handoff,
                    parent=parent_handle,
                )
                assert jsonl_identity is not None
                port.complete_issued_export_receipt(
                    issued.snapshot_id,
                    expected_generation=facts.generation,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=published_manifest_identity,
                )
                return _cleanup_and_release_export(
                    port,
                    facts,
                    issued,
                    handoff,
                    _ExportReconciliation(
                        IssuedReceiptRecovery(
                            issued.snapshot_id,
                            RefreshRecoveryState.COMPLETED,
                        )
                    ),
                    parent=parent_handle,
                )
            prior_pairs = list(
                _prior_export_pairs(facts, issued).values()
            )
            if (
                handoff is not None
                and handoff.prior_jsonl_absent is False
                and handoff.prior_manifest_absent is False
                and handoff.prior_jsonl_digest is not None
                and handoff.prior_manifest_digest is not None
            ):
                prior_pairs.append(
                    (
                        handoff.prior_jsonl_digest,
                        handoff.prior_manifest_digest,
                    )
                )
            old_pair = next(
                (
                    pair
                    for pair in prior_pairs
                    if jsonl_state == "present"
                    and jsonl_digest == pair[0]
                    and manifest_state == "present"
                    and manifest_digest == pair[1]
                ),
                None,
            )
            if old_pair is not None:
                port.cancel_issued_export_receipt(
                    issued.snapshot_id,
                    expected_generation=facts.generation,
                )
                return _cleanup_and_release_export(
                    port,
                    facts,
                    issued,
                    handoff,
                    _ExportReconciliation(
                        IssuedReceiptRecovery(
                            issued.snapshot_id,
                            RefreshRecoveryState.CANCELLED,
                        )
                    ),
                    parent=parent_handle,
                )
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    ("RECOVERY.EXPORT_PAIR_UNPROVABLE",),
                )
            )
        except RecoveryError as error:
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    (error.error_code,),
                )
            )
        except (OSError, ActivationPreparationError) as error:
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    ("RECOVERY.EXPORT_IO_FAILED",),
                )
            )
    finally:
        if parent_handle is not None:
            parent_handle.close()


def _reconcile_issued_exports(
    port: _SnapshotRecoveryPort,
    facts: _RefreshRecoveryFacts,
) -> tuple[tuple[IssuedReceiptRecovery, ...], tuple[str, ...]]:
    """Reconcile issued arbitrary-destination export receipts.

    Under a valid binding every export row keeps its separate
    BLOCKED/complete/cancel semantics and never touches resource
    divergence.  An invalid active binding makes an export-shaped row
    indistinguishable from a tampered refresh receipt, so the row is
    never cancelled or completed: divergence is durably latched and the
    issued row and its files stay untouched.
    """

    results: list[IssuedReceiptRecovery] = []
    diagnostics: list[str] = []
    latched = False
    issued = _issued_export_receipts(facts)
    if facts.binding_invalid and issued:
        rounds = 0
        while True:
            rounds += 1
            if rounds > _MAX_RECOVERY_ROUNDS:
                for row in issued:
                    results.append(
                        IssuedReceiptRecovery(
                            row.snapshot_id,
                            RefreshRecoveryState.BLOCKED,
                            ("RECOVERY.ROUND_LIMIT",),
                        )
                    )
                diagnostics.append("RECOVERY.ROUND_LIMIT")
                break
            if port.latch_source_divergence(facts.canonical_fingerprint):
                for row in issued:
                    results.append(
                        IssuedReceiptRecovery(
                            row.snapshot_id,
                            RefreshRecoveryState.DIVERGED,
                            ("RECOVERY.EXPORT_BINDING_INVALID",),
                        )
                    )
                diagnostics.append("RECOVERY.EXPORT_BINDING_INVALID")
                latched = True
                break
            facts = port.read_recovery_facts()
            issued = _issued_export_receipts(facts)
            if not issued or not facts.binding_invalid:
                break
    if not latched:
        for row in issued:
            reconciliation = _reconcile_one_export(port, facts, row)
            results.append(reconciliation.result)
            diagnostics.extend(reconciliation.diagnostics)
    return tuple(results), tuple(sorted(set(diagnostics)))


def _receipt_facts_for(
    facts: _RefreshRecoveryFacts,
    snapshot_id: str,
) -> IssuedReceiptFacts | None:
    """One receipt row fact for a snapshot id, if present."""

    for receipt in facts.receipts:
        if receipt.snapshot_id == snapshot_id:
            return receipt
    return None


def _fsync_artifact_parent(
    destination: Path,
    expected_identity: tuple[int, int] | None,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """No-follow directory-descriptor proof and fsync of the parent.

    The parent directory is opened with ``O_DIRECTORY`` and
    ``O_NOFOLLOW`` (where available), its descriptor is ``fstat``-proven
    to be the exact device/inode recorded at issued registration, the
    descriptor is fsynced, and the pathname/full chain is re-proven to
    still resolve to that same identity.  The fsync is unconditional:
    even when every deterministic artifact is already absent (a prior
    unlink succeeded but its directory fsync failed), the release must
    still durably record the absence before the cleanup-pending handoff
    journal is cleared.  Any mismatch or failure raises
    ``RecoveryError`` and the caller keeps the handoff and reports
    BLOCKED; a missing legacy identity fails closed and is never
    inferred as new authority.  With a retained parent handle the fsync
    runs on that exact retained descriptor and the advertised pathname
    is re-proven by the component-wise walk; the parent pathname is
    never re-resolved for the fsync.
    """

    if parent is not None:
        parent.fsync()
        parent.reprove()
        return
    proof_error = _artifact_parent_proof(destination, expected_identity)
    if proof_error is not None:
        raise RecoveryError(proof_error, retryable=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(destination.parent, flags)
        observed = os.fstat(descriptor)
        if not stat.S_ISDIR(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != expected_identity:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )
        os.fsync(descriptor)
    except RecoveryError:
        raise
    except OSError:
        raise RecoveryError(
            "RECOVERY.EXPORT_IO_FAILED",
            retryable=False,
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    proof_error = _artifact_parent_proof(destination, expected_identity)
    if proof_error is not None:
        raise RecoveryError(proof_error, retryable=False)


def _terminal_handoff_row_blocker(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
) -> tuple[str | None, _RecoveryParentHandle | None]:
    """Complete pre-cleanup validation of one terminal handoff row.

    Returns ``(blocker_code, None)`` when the terminal receipt must not
    be cleaned, or ``(None, parent_handle)`` when the row is safe to
    replay with the handoff's expected parent already bound as a
    component-safe no-follow dirfd (retained before leaving the
    validation, so the replay never re-resolves the parent pathname for
    destructive work).  The sweep may only run identity-bound cleanup
    after every check passes: resource/canonical identity, a well-formed
    receipt, exact revision ancestry and record count, the exact
    configured-vs-arbitrary classification (configured rows must use the
    configured JSONL+manifest pair), the deterministic manifest path and
    full authority alias closure, a real non-symlink writable/executable
    parent chain, and the exact immediate-parent device/inode recorded
    in the receipt's handoff at issued registration (bound
    component-by-component with ``O_DIRECTORY|O_NOFOLLOW`` per
    component).  Configured rows additionally require the safe real
    parent chain; arbitrary rows must pass ``_recovery_destination_safe``.
    A missing legacy parent identity fails closed
    (``RECOVERY.EXPORT_PARENT_IDENTITY_MISSING``) and a renamed/
    replaced parent fails closed (``RECOVERY.EXPORT_PARENT_REPLACED``)
    before any destructive cleanup; every file and handoff is preserved.
    """

    configured = (
        receipt.destination_jsonl_path == facts.configured_jsonl_path
        and receipt.destination_manifest_path
        == facts.snapshot_manifest_path
    )
    if (
        receipt.resource_id != facts.resource_id
        or receipt.canonical_store_id != facts.canonical_store_id
    ):
        return (
            (
                "RECOVERY.LEDGER_IDENTITY_INVALID"
                if configured
                else "RECOVERY.EXPORT_LEDGER_IDENTITY_INVALID"
            ),
            None,
        )
    try:
        receipt.receipt
    except (TypeError, ValueError):
        return (
            (
                "RECOVERY.LEDGER_RECEIPT_INVALID"
                if configured
                else "RECOVERY.EXPORT_RECEIPT_INVALID"
            ),
            None,
        )
    expected_count = _record_count_at(facts, receipt.exported_revision)
    if expected_count is None or receipt.record_count != expected_count:
        return (
            (
                "RECOVERY.ANCESTRY_INVALID"
                if configured
                else "RECOVERY.EXPORT_ANCESTRY_INVALID"
            ),
            None,
        )
    if configured:
        if not _parent_chain_safe(receipt.destination_jsonl_path):
            return "RECOVERY.EXPORT_PARENT_UNSAFE", None
    else:
        destination_error = _recovery_destination_safe(facts, receipt)
        if destination_error is not None:
            return destination_error, None
    handoff = _handoff_for(facts, receipt.snapshot_id)
    identity = (
        None
        if handoff is None
        else handoff.artifact_parent_identity
    )
    if identity is None:
        return "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING", None
    try:
        parent_handle = _RecoveryParentHandle.bind(
            receipt.destination_jsonl_path,
            identity,
        )
    except RecoveryError as error:
        return error.error_code, None
    return None, parent_handle


def _replay_terminal_handoffs(
    port: _SnapshotRecoveryPort,
    facts: _RefreshRecoveryFacts,
    *,
    per_receipt: list[IssuedReceiptRecovery],
    diagnostics: list[str],
    skip_configured: bool = False,
) -> tuple[bool, RefreshRecoveryState | None]:
    """Finish durable cleanup for every terminal receipt with a handoff.

    A crash between the terminal commit (completed/cancelled) and the
    owned-artifact cleanup leaves the receipt terminal plus its
    cleanup-pending handoff journal.  This sweep validates the full
    terminal row (identity, receipt, ancestry, configured-vs-arbitrary
    classification, deterministic manifest paths, authority alias
    closure and the real parent chain) before any destructive cleanup,
    then removes only owned deterministic artifacts, unconditionally
    fsyncs the artifact parent and releases the handoff idempotently so
    the next refresh/export is usable.  An invalid row or a cleanup/
    fsync failure keeps the handoff, reports ``BLOCKED`` with a stable
    code and never deletes a foreign or unproven entry.  When
    ``skip_configured`` is set (pre-latched configured divergence),
    configured terminal handoffs remain completely untouched while
    fully validated arbitrary-destination terminal handoffs are still
    replayed and released after their exact cleanup+fsync; arbitrary
    cleanup never alters the binding or divergence.  Returns whether
    any handoff was replayed and the overall state for the replayed
    receipts.
    """

    replayed = False
    state: RefreshRecoveryState | None = None
    for handoff in facts.handoffs:
        receipt = _receipt_facts_for(facts, handoff.snapshot_id)
        if receipt is None or receipt.status not in {
            "completed",
            "cancelled",
        }:
            continue
        configured = (
            receipt.destination_jsonl_path == facts.configured_jsonl_path
            and receipt.destination_manifest_path
            == facts.snapshot_manifest_path
        )
        if skip_configured and configured:
            continue
        replayed = True
        blocker, parent_handle = _terminal_handoff_row_blocker(
            facts,
            receipt,
        )
        if blocker is not None:
            per_receipt.append(
                IssuedReceiptRecovery(
                    receipt.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    (blocker,),
                )
            )
            diagnostics.append(blocker)
            state = RefreshRecoveryState.BLOCKED
            continue
        assert parent_handle is not None
        try:
            with parent_handle:
                if (
                    receipt.destination_jsonl_path
                    == facts.configured_jsonl_path
                ):
                    diagnostics.extend(
                        _cleanup_refresh_artifacts(
                            facts,
                            receipt,
                            handoff=handoff,
                            parent=parent_handle,
                        )
                    )
                else:
                    diagnostics.extend(
                        _cleanup_export_artifacts(
                            receipt,
                            handoff=handoff,
                            parent=parent_handle,
                        )
                    )
                _fsync_artifact_parent(
                    receipt.destination_jsonl_path,
                    handoff.artifact_parent_identity,
                    parent=parent_handle,
                )
                port.clear_issued_receipt_handoff(
                    receipt.snapshot_id,
                    expected_generation=facts.generation,
                )
        except RecoveryError as error:
            per_receipt.append(
                IssuedReceiptRecovery(
                    receipt.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    (error.error_code,),
                )
            )
            diagnostics.append(error.error_code)
            state = RefreshRecoveryState.BLOCKED
            continue
        except OSError:
            per_receipt.append(
                IssuedReceiptRecovery(
                    receipt.snapshot_id,
                    RefreshRecoveryState.BLOCKED,
                    ("RECOVERY.IO_FAILED",),
                )
            )
            diagnostics.append("RECOVERY.IO_FAILED")
            state = RefreshRecoveryState.BLOCKED
            continue
        receipt_state = (
            RefreshRecoveryState.COMPLETED
            if receipt.status == "completed"
            else RefreshRecoveryState.CANCELLED
        )
        per_receipt.append(
            IssuedReceiptRecovery(receipt.snapshot_id, receipt_state)
        )
        if state is None:
            state = receipt_state
    return replayed, state


def recover_snapshot_publication(
    port: _SnapshotRecoveryPort,
) -> RefreshRecoveryOutcome:
    """Idempotently close every issued snapshot publication crash window.

    Runs under the caller-held resource-scoped reentrant refresh/
    observation gate.  Configured refresh receipts are classified and
    reconciled first (cancellation, forward completion, reconstruction
    or a durable divergence latch); terminal receipts with a still
    pending cleanup-pending handoff are swept first so a crash between
    the terminal commit and the durable cleanup is finished
    idempotently; arbitrary-destination export receipts are then
    reconciled independently without touching the binding or
    divergence.  A pre-latched configured divergence is preserved: the
    latch is never cleared, configured terminal handoffs are never
    replayed, and only fully validated arbitrary-destination terminal
    handoffs are replayed and released after their exact cleanup+fsync.
    Replay after cancellation/completion returns the same stable state
    without a new receipt, duplicate binding or repeated destructive
    publication.
    """

    per_receipt: list[IssuedReceiptRecovery] = []
    diagnostics: list[str] = []
    overall = RefreshRecoveryState.NOOP
    rounds = 0
    while True:
        rounds += 1
        if rounds > _MAX_RECOVERY_ROUNDS:
            overall = RefreshRecoveryState.BLOCKED
            diagnostics.append("RECOVERY.ROUND_LIMIT")
            break
        facts = port.read_recovery_facts()
        if facts.divergence_latched:
            diagnostics.append("RECOVERY.DIVERGENCE_PRESERVED")
            _replayed, replay_state = _replay_terminal_handoffs(
                port,
                facts,
                per_receipt=per_receipt,
                diagnostics=diagnostics,
                skip_configured=True,
            )
            if (
                replay_state is not None
                and overall is RefreshRecoveryState.NOOP
            ):
                overall = replay_state
            break
        _replayed, replay_state = _replay_terminal_handoffs(
            port,
            facts,
            per_receipt=per_receipt,
            diagnostics=diagnostics,
        )
        if (
            replay_state is not None
            and overall is RefreshRecoveryState.NOOP
        ):
            overall = replay_state
        issued_refresh = _issued_refresh_receipts(facts)
        if not issued_refresh:
            break
        decision = _classify_refresh_receipts(facts, issued_refresh)
        try:
            if decision.action == "cancel":
                bound: list[
                    tuple[
                        IssuedReceiptFacts,
                        _ArtifactHandoffFacts | None,
                        _RecoveryParentHandle | None,
                    ]
                ] = []
                try:
                    for receipt in decision.receipts:
                        handoff = _handoff_for(
                            facts,
                            receipt.snapshot_id,
                        )
                        parent_handle = _bind_recovery_parent(
                            receipt,
                            handoff,
                        )
                        bound.append(
                            (receipt, handoff, parent_handle)
                        )
                        unproven = _unproven_artifact_code(
                            handoff,
                            _refresh_artifact_expectations(
                                facts,
                                receipt,
                                handoff=handoff,
                            ),
                            parent=parent_handle,
                        )
                        if unproven is not None:
                            raise RecoveryError(
                                unproven,
                                retryable=False,
                            )
                    for receipt, _handoff, _parent_handle in bound:
                        port.cancel_issued_refresh_receipt(
                            receipt.snapshot_id,
                            expected_generation=facts.generation,
                        )
                    for receipt, handoff, parent_handle in bound:
                        diagnostics.extend(
                            _cleanup_refresh_artifacts(
                                facts,
                                receipt,
                                handoff=handoff,
                                parent=parent_handle,
                            )
                        )
                        if handoff is not None:
                            assert parent_handle is not None
                            _fsync_artifact_parent(
                                receipt.destination_jsonl_path,
                                handoff.artifact_parent_identity,
                                parent=parent_handle,
                            )
                            port.clear_issued_receipt_handoff(
                                receipt.snapshot_id,
                                expected_generation=facts.generation,
                            )
                        per_receipt.append(
                            IssuedReceiptRecovery(
                                receipt.snapshot_id,
                                RefreshRecoveryState.CANCELLED,
                            )
                        )
                finally:
                    for _receipt, _handoff, parent_handle in bound:
                        if parent_handle is not None:
                            parent_handle.close()
                overall = RefreshRecoveryState.CANCELLED
                continue
            if decision.action in {"complete", "reconstruct"}:
                assert decision.receipt is not None
                receipt = decision.receipt
                handoff = _handoff_for(facts, receipt.snapshot_id)
                parent_handle = _bind_recovery_parent(
                    receipt,
                    handoff,
                )
                try:
                    unproven = _unproven_artifact_code(
                        handoff,
                        _refresh_artifact_expectations(
                            facts,
                            receipt,
                            handoff=handoff,
                        ),
                        parent=parent_handle,
                    )
                    if unproven is not None:
                        raise RecoveryError(
                            unproven,
                            retryable=False,
                        )
                    if decision.action == "reconstruct":
                        manifest_identity = _publish_reconstructed_manifest(
                            facts.configured_jsonl_path,
                            facts.snapshot_manifest_path,
                            receipt.receipt,
                            expected_manifest_digest=(
                                decision.expected_manifest_digest
                            ),
                            handoff=handoff,
                            parent=parent_handle,
                        )
                    else:
                        manifest_identity = decision.manifest_identity
                    port.complete_issued_refresh_receipt(
                        receipt.snapshot_id,
                        expected_generation=facts.generation,
                        jsonl_identity=decision.jsonl_identity,
                        manifest_identity=manifest_identity,
                    )
                    diagnostics.extend(
                        _cleanup_refresh_artifacts(
                            facts,
                            receipt,
                            handoff=handoff,
                            parent=parent_handle,
                        )
                    )
                    if handoff is not None:
                        assert parent_handle is not None
                        _fsync_artifact_parent(
                            receipt.destination_jsonl_path,
                            handoff.artifact_parent_identity,
                            parent=parent_handle,
                        )
                        port.clear_issued_receipt_handoff(
                            receipt.snapshot_id,
                            expected_generation=facts.generation,
                        )
                finally:
                    if parent_handle is not None:
                        parent_handle.close()
                per_receipt.append(
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    )
                )
                overall = RefreshRecoveryState.COMPLETED
                continue
            assert decision.action == "diverged"
            assert decision.error_code is not None
            if not port.latch_source_divergence(
                facts.canonical_fingerprint
            ):
                continue
            per_receipt.append(
                IssuedReceiptRecovery(
                    decision.receipts[0].snapshot_id,
                    RefreshRecoveryState.DIVERGED,
                    (decision.error_code,),
                )
            )
            overall = RefreshRecoveryState.DIVERGED
            diagnostics.append(decision.error_code)
            break
        except RecoveryError as error:
            issued = (
                decision.receipt
                if decision.receipt is not None
                else (
                    decision.receipts[0]
                    if decision.receipts
                    else None
                )
            )
            if issued is not None:
                per_receipt.append(
                    IssuedReceiptRecovery(
                        issued.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        (error.error_code,),
                    )
                )
            overall = RefreshRecoveryState.BLOCKED
            diagnostics.append(error.error_code)
            break
        except (OSError, ActivationPreparationError) as error:
            issued = (
                decision.receipt
                if decision.receipt is not None
                else (
                    decision.receipts[0]
                    if decision.receipts
                    else None
                )
            )
            if issued is not None:
                per_receipt.append(
                    IssuedReceiptRecovery(
                        issued.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.IO_FAILED",),
                    )
                )
            overall = RefreshRecoveryState.BLOCKED
            diagnostics.append("RECOVERY.IO_FAILED")
            break
    export_results, export_diagnostics = _reconcile_issued_exports(
        port,
        port.read_recovery_facts(),
    )
    per_receipt.extend(export_results)
    diagnostics.extend(export_diagnostics)
    if overall is RefreshRecoveryState.NOOP and export_results:
        if all(
            result.state is RefreshRecoveryState.DIVERGED
            for result in export_results
        ):
            overall = RefreshRecoveryState.DIVERGED
        elif all(
            result.state is RefreshRecoveryState.CANCELLED
            for result in export_results
        ):
            overall = RefreshRecoveryState.CANCELLED
        elif all(
            result.state is RefreshRecoveryState.COMPLETED
            for result in export_results
        ):
            overall = RefreshRecoveryState.COMPLETED
        else:
            overall = RefreshRecoveryState.BLOCKED
    snapshot_id: str | None = None
    for result in per_receipt:
        if result.state in {
            RefreshRecoveryState.CANCELLED,
            RefreshRecoveryState.COMPLETED,
            RefreshRecoveryState.DIVERGED,
        }:
            snapshot_id = result.snapshot_id
            break
    return RefreshRecoveryOutcome(
        state=overall,
        receipts=tuple(per_receipt),
        snapshot_id=snapshot_id,
        diagnostics=tuple(sorted(set(diagnostics))),
    )
