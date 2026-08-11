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
    registration and the recovery-copy identities after the copies are
    prepared, both before any destructive use.  Recovery treats a
    matching regular single-link file as owned only when its exact
    identity was durably handed off; content equality alone never
    proves ownership.
    """

    snapshot_id: str
    jsonl_temp_identity: tuple[int, int] | None
    manifest_temp_identity: tuple[int, int] | None
    jsonl_recovery_identity: tuple[int, int] | None
    manifest_recovery_identity: tuple[int, int] | None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not str or not self.snapshot_id:
            raise TypeError("handoff snapshot id must be a non-empty string")
        for value, field_name in (
            (self.jsonl_temp_identity, "jsonl_temp_identity"),
            (self.manifest_temp_identity, "manifest_temp_identity"),
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
    parent = destination.parent
    chain = [parent]
    chain.extend(parent.parents)
    for candidate in reversed(chain):
        try:
            observed = os.lstat(candidate)
        except (OSError, ValueError):
            return "RECOVERY.EXPORT_PARENT_UNSAFE"
        if not stat.S_ISDIR(observed.st_mode):
            return "RECOVERY.EXPORT_PARENT_UNSAFE"
    try:
        observed = os.lstat(parent)
    except (OSError, ValueError):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    if not (
        observed.st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_mode
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    return None


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
        jsonl_identity: tuple[int, int] | None = None,
        manifest_identity: tuple[int, int] | None = None,
    ) -> None: ...

    def cancel_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None: ...

    def latch_source_divergence(self, expected_fingerprint: str) -> bool: ...


def _strict_file_state(
    path: Path,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """One descriptor-based no-follow proof of a configured pair entry.

    Returns ``("absent", None, None)`` when the path does not exist,
    ``("unsafe", None, None)`` when the path is a symlink, directory,
    multi-link entry, unreadable, or whose terminal identity is not
    stable, and ``("present", digest, identity)`` only for a regular
    single-link file whose bytes are hashed through the descriptor and
    whose terminal ``lstat`` still reports the same device/inode.
    Pathname hashing is never used, so a foreign same-byte inode cannot
    masquerade as a stable owned entry.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
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


def _path_exists(path: Path) -> bool:
    try:
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
) -> tuple[int, int]:
    """Publish the exact ledger-reconstructed manifest for one receipt.

    Uses the exclusive same-directory temporary, file fsync, strict
    re-open identity/digest proof, a strict destination proof (exact old
    digest and regular single-link entry, or provable absence), atomic
    replace, parent fsync and a terminal identity/digest re-proof.  Any
    failure removes only the temporary this call created and fails
    closed, leaving the destination untouched.
    """

    paths = _recovery_artifact_paths(destination)
    manifest = _manifest_for_receipt(
        receipt,
        SnapshotKind.EXPLICIT_EXPORT,
    )
    payload = _manifest_bytes(manifest)
    expected_digest = hashlib.sha256(payload).hexdigest()
    descriptor = -1
    temp_identity: _ActivationFileIdentity | None = None
    stale_state, stale_digest, _stale_identity = _strict_file_state(
        paths.manifest_temp
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
        expected_identity = _artifact_expected_identity(
            handoff,
            "manifest_temp",
        )
        if (
            expected_identity is None
            or _stale_identity != expected_identity
        ):
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_UNPROVEN",
                retryable=False,
            )
        if not _remove_content_proven_artifact(
            paths.manifest_temp,
            stale_digest,
            expected_identity=expected_identity,
        ):
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_CLEANUP_FAILED",
                retryable=True,
            )
    elif stale_state == "unsafe":
        raise RecoveryError(
            "RECOVERY.MANIFEST_TEMP_UNSAFE",
            retryable=False,
        )
    try:
        descriptor, temp_identity = _open_activation_journal_temp(
            paths.manifest_temp
        )
        _write_activation_journal_bytes(descriptor, payload)
        _fsync_activation_journal(descriptor)
        _close_activation_journal(descriptor)
        descriptor = -1
        capture = _capture_activation_file(
            paths.manifest_temp,
            asset_kind="RECOVERY_MANIFEST_TEMP",
        )
        if capture.digest != expected_digest or (
            capture.identity.device,
            capture.identity.inode,
        ) != (temp_identity.device, temp_identity.inode):
            raise RecoveryError(
                "RECOVERY.MANIFEST_TEMP_INVALID",
                retryable=False,
            )
        if expected_manifest_digest is None:
            try:
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
            state, digest, _identity = _strict_file_state(manifest_path)
            if state != "present" or digest != expected_manifest_digest:
                raise RecoveryError(
                    "RECOVERY.MANIFEST_DESTINATION_CHANGED",
                    retryable=False,
                )
        os.replace(paths.manifest_temp, manifest_path)
        _fsync_activation_directory(manifest_path.parent)
        published = _capture_activation_file(
            manifest_path,
            asset_kind="RECOVERY_MANIFEST",
        )
        if published.digest != expected_digest or (
            published.identity.device,
            published.identity.inode,
        ) != (temp_identity.device, temp_identity.inode):
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
    finally:
        if descriptor >= 0:
            _close_activation_journal(descriptor)
        if temp_identity is not None and _path_exists(
            paths.manifest_temp
        ):
            _remove_owned_activation_journal_temp(
                paths.manifest_temp,
                temp_identity,
            )


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
) -> str | None:
    """Blocking error when an existing artifact matches expected content
    but has no durable ownership proof.

    Content equality alone is never ownership: a same-byte foreign
    inode without the exact handed-off identity is unprovable and must
    be preserved, never unlinked or replaced.
    """

    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path)
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


def _export_artifact_expectations(
    destination: Path,
    issued: IssuedReceiptFacts,
    *,
    prior_digests: set[str],
) -> tuple[tuple[Path, set[str], str], ...]:
    """Deterministic artifact expectations for one issued export."""

    paths = _recovery_artifact_paths(destination)
    manifest_digest = _manifest_digest_for_receipt(issued.receipt)
    return (
        (paths.jsonl_temp, {issued.jsonl_digest}, "jsonl_temp"),
        (paths.manifest_temp, {manifest_digest}, "manifest_temp"),
        (paths.jsonl_recovery, set(prior_digests), "jsonl_recovery"),
        (paths.manifest_recovery, set(prior_digests), "manifest_recovery"),
    )


def _refresh_artifact_expectations(
    facts: _RefreshRecoveryFacts,
    receipts: tuple[IssuedReceiptFacts, ...],
) -> tuple[tuple[Path, set[str], str], ...]:
    """Deterministic artifact expectations for one issued refresh set."""

    paths = _recovery_artifact_paths(facts.configured_jsonl_path)
    jsonl_digests = {receipt.jsonl_digest for receipt in receipts}
    manifest_digests = {
        _manifest_digest_for_receipt(receipt.receipt)
        for receipt in receipts
    }
    expectations: list[tuple[Path, set[str], str]] = [
        (paths.jsonl_temp, set(jsonl_digests), "jsonl_temp"),
        (paths.manifest_temp, set(manifest_digests), "manifest_temp"),
    ]
    if facts.binding is not None:
        expectations.append(
            (
                paths.jsonl_recovery,
                {facts.binding.receipt.jsonl_digest},
                "jsonl_recovery",
            )
        )
        expectations.append(
            (
                paths.manifest_recovery,
                {
                    hashlib.sha256(
                        _manifest_bytes(facts.binding.manifest)
                    ).hexdigest()
                },
                "manifest_recovery",
            )
        )
    return tuple(expectations)


def _remove_content_proven_artifact(
    path: Path,
    expected_digest: str,
    *,
    expected_identity: tuple[int, int] | None,
) -> bool:
    """Remove one deterministic artifact proven by strict content proof
    plus the durable handed-off identity.

    The unlink is identity-bound to the proven inode with a parent
    fsync and an absence re-proof.  A foreign, linked, unsafe,
    digest-mismatched or identity-less entry fails closed and is never
    removed.
    """

    state, digest, identity = _strict_file_state(path)
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
    return _remove_owned_activation_journal_temp(
        path,
        _ActivationFileIdentity(identity[0], identity[1]),
    )


def _cleanup_refresh_artifacts(
    facts: _RefreshRecoveryFacts,
    receipts: tuple[IssuedReceiptFacts, ...],
) -> tuple[str, ...]:
    """Clean owned refresh artifacts after a durable outcome.

    Every removed artifact must be proven by its exact digest and its
    durably handed-off identity; a matching same-byte inode without the
    handoff proof fails closed (``RECOVERY.ARTIFACT_UNPROVEN``) and is
    never removed.
    """

    diagnostics: list[str] = []
    paths = _recovery_artifact_paths(facts.configured_jsonl_path)
    jsonl_digests = {receipt.jsonl_digest for receipt in receipts}
    manifest_digests = {
        _manifest_digest_for_receipt(receipt.receipt)
        for receipt in receipts
    }
    expectations: list[tuple[Path, set[str], str]] = [
        (paths.jsonl_temp, set(jsonl_digests), "jsonl_temp"),
        (paths.manifest_temp, set(manifest_digests), "manifest_temp"),
    ]
    if facts.binding is not None:
        expectations.append(
            (
                paths.jsonl_recovery,
                {facts.binding.receipt.jsonl_digest},
                "jsonl_recovery",
            )
        )
        expectations.append(
            (
                paths.manifest_recovery,
                {
                    hashlib.sha256(
                        _manifest_bytes(facts.binding.manifest)
                    ).hexdigest()
                },
                "manifest_recovery",
            )
        )
    handoff = _handoff_for(facts, receipts[0].snapshot_id)
    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path)
        if state == "absent":
            continue
        if state != "present" or digest not in digests:
            diagnostics.append("RECOVERY.ARTIFACT_CONFLICT")
            continue
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
        ):
            diagnostics.append("RECOVERY.CLEANUP_FAILED")
    return tuple(sorted(set(diagnostics)))


def _cleanup_export_artifacts(
    destination: Path,
    issued: IssuedReceiptFacts,
    *,
    prior_digests: set[str],
    handoff: _ArtifactHandoffFacts | None,
) -> tuple[str, ...]:
    """Clean owned export artifacts after a durable outcome.

    Same digest-plus-handoff-identity ownership proof as the refresh
    cleanup; unprovable matching entries fail closed and are preserved.
    """

    diagnostics: list[str] = []
    paths = _recovery_artifact_paths(destination)
    manifest_digest = _manifest_digest_for_receipt(issued.receipt)
    expectations: list[tuple[Path, set[str], str]] = [
        (paths.jsonl_temp, {issued.jsonl_digest}, "jsonl_temp"),
        (paths.manifest_temp, {manifest_digest}, "manifest_temp"),
        (paths.jsonl_recovery, set(prior_digests), "jsonl_recovery"),
        (paths.manifest_recovery, set(prior_digests), "manifest_recovery"),
    ]
    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path)
        if state == "absent":
            continue
        if not digests:
            diagnostics.append("RECOVERY.EXPORT_CLEANUP_UNPROVABLE")
            continue
        if state != "present" or digest not in digests:
            diagnostics.append("RECOVERY.ARTIFACT_CONFLICT")
            continue
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
        ):
            diagnostics.append("RECOVERY.CLEANUP_FAILED")
    return tuple(sorted(set(diagnostics)))


def _prior_export_pairs(
    facts: _RefreshRecoveryFacts,
    issued: IssuedReceiptFacts,
) -> dict[str, tuple[str, str]]:
    """Prior same-destination receipt pairs provable from the ledger.

    A prior row is usable only when its resource/canonical identity
    matches the live store, its receipt is well-formed, its revision
    ancestry is exact, and it reached a terminal status at the same
    destination pair.  Any invalid or foreign prior row is excluded so
    an old-pair cancellation or manifest expectation can never be
    derived from tampered ledger state.
    """

    result: dict[str, tuple[str, str]] = {}
    for row in facts.receipts:
        if (
            row.status not in {"completed", "cancelled"}
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
    identities are required again inside the store transaction.
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
    jsonl_state, jsonl_digest, jsonl_identity = _strict_file_state(
        issued.destination_jsonl_path
    )
    manifest_state, manifest_digest, manifest_identity = _strict_file_state(
        issued.destination_manifest_path
    )
    if jsonl_state == "unsafe" or manifest_state == "unsafe":
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                ("RECOVERY.EXPORT_PAIR_UNSAFE",),
            )
        )
    issued_manifest_digest = _manifest_digest_for_receipt(issued.receipt)
    prior = _prior_export_pairs(facts, issued)
    prior_digests = {
        digest
        for pair in prior.values()
        for digest in pair
    }
    artifact_expectations = _export_artifact_expectations(
        issued.destination_jsonl_path,
        issued,
        prior_digests=prior_digests,
    )
    unproven = _unproven_artifact_code(handoff, artifact_expectations)
    if unproven is not None:
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                (unproven,),
            )
        )
    try:
        if (
            jsonl_state == "present"
            and jsonl_digest == issued.jsonl_digest
            and manifest_state == "present"
            and manifest_digest == issued_manifest_digest
        ):
            port.complete_issued_export_receipt(
                issued.snapshot_id,
                expected_generation=facts.generation,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
            )
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.COMPLETED,
                ),
                _cleanup_export_artifacts(
                    issued.destination_jsonl_path,
                    issued,
                    prior_digests=prior_digests,
                    handoff=handoff,
                ),
            )
        if jsonl_state == "present" and jsonl_digest == issued.jsonl_digest:
            if manifest_state == "absent":
                expected_manifest_digest = None
            elif manifest_digest in {
                digest for _jsonl, digest in prior.values()
            }:
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
            )
            port.complete_issued_export_receipt(
                issued.snapshot_id,
                expected_generation=facts.generation,
                jsonl_identity=jsonl_identity,
                manifest_identity=published_manifest_identity,
            )
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.COMPLETED,
                ),
                _cleanup_export_artifacts(
                    issued.destination_jsonl_path,
                    issued,
                    prior_digests=prior_digests,
                    handoff=handoff,
                ),
            )
        old_pair = next(
            (
                (snapshot_id, pair)
                for snapshot_id, pair in prior.items()
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
            return _ExportReconciliation(
                IssuedReceiptRecovery(
                    issued.snapshot_id,
                    RefreshRecoveryState.CANCELLED,
                ),
                _cleanup_export_artifacts(
                    issued.destination_jsonl_path,
                    issued,
                    prior_digests=prior_digests,
                    handoff=handoff,
                ),
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
    return _ExportReconciliation(
        IssuedReceiptRecovery(
            issued.snapshot_id,
            RefreshRecoveryState.BLOCKED,
            ("RECOVERY.EXPORT_PAIR_UNPROVABLE",),
        )
    )


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


def recover_snapshot_publication(
    port: _SnapshotRecoveryPort,
) -> RefreshRecoveryOutcome:
    """Idempotently close every issued snapshot publication crash window.

    Runs under the caller-held resource-scoped reentrant refresh/
    observation gate.  Configured refresh receipts are classified and
    reconciled first (cancellation, forward completion, reconstruction
    or a durable divergence latch); arbitrary-destination export
    receipts are then reconciled independently without touching the
    binding or divergence.  Replay after cancellation/completion
    returns the same stable state without a new receipt, duplicate
    binding or repeated destructive publication.
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
            break
        issued_refresh = _issued_refresh_receipts(facts)
        if not issued_refresh:
            break
        decision = _classify_refresh_receipts(facts, issued_refresh)
        try:
            if decision.action == "cancel":
                unproven = _unproven_artifact_code(
                    _handoff_for(facts, decision.receipts[0].snapshot_id),
                    _refresh_artifact_expectations(facts, decision.receipts),
                )
                if unproven is not None:
                    raise RecoveryError(unproven, retryable=False)
                for receipt in decision.receipts:
                    port.cancel_issued_refresh_receipt(
                        receipt.snapshot_id,
                        expected_generation=facts.generation,
                    )
                    per_receipt.append(
                        IssuedReceiptRecovery(
                            receipt.snapshot_id,
                            RefreshRecoveryState.CANCELLED,
                        )
                    )
                overall = RefreshRecoveryState.CANCELLED
                diagnostics.extend(
                    _cleanup_refresh_artifacts(facts, decision.receipts)
                )
                continue
            if decision.action in {"complete", "reconstruct"}:
                assert decision.receipt is not None
                receipt = decision.receipt
                unproven = _unproven_artifact_code(
                    _handoff_for(facts, receipt.snapshot_id),
                    _refresh_artifact_expectations(facts, (receipt,)),
                )
                if unproven is not None:
                    raise RecoveryError(unproven, retryable=False)
                if decision.action == "reconstruct":
                    manifest_identity = _publish_reconstructed_manifest(
                        facts.configured_jsonl_path,
                        facts.snapshot_manifest_path,
                        receipt.receipt,
                        expected_manifest_digest=(
                            decision.expected_manifest_digest
                        ),
                        handoff=_handoff_for(facts, receipt.snapshot_id),
                    )
                else:
                    manifest_identity = decision.manifest_identity
                port.complete_issued_refresh_receipt(
                    receipt.snapshot_id,
                    expected_generation=facts.generation,
                    jsonl_identity=decision.jsonl_identity,
                    manifest_identity=manifest_identity,
                )
                per_receipt.append(
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    )
                )
                overall = RefreshRecoveryState.COMPLETED
                diagnostics.extend(
                    _cleanup_refresh_artifacts(facts, (receipt,))
                )
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
