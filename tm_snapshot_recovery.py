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

import tm_snapshot_artifacts as snapshot_artifacts_module


_EXPORT_JSONL_RECOVERY_SUFFIX = snapshot_artifacts_module._EXPORT_JSONL_RECOVERY_SUFFIX
"""_EXPORT_JSONL_RECOVERY_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_JSONL_TEMP_SUFFIX = snapshot_artifacts_module._EXPORT_JSONL_TEMP_SUFFIX
"""_EXPORT_JSONL_TEMP_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_RECOVERY_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_RECOVERY_SUFFIX
"""_EXPORT_MANIFEST_RECOVERY_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_TEMP_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_TEMP_SUFFIX
"""_EXPORT_MANIFEST_TEMP_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_SUFFIX
"""_EXPORT_MANIFEST_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""

_NATIVE_PATH_TYPE = type(Path())

_MAX_RECOVERY_ROUNDS = 8


RecoveryError = snapshot_artifacts_module.RecoveryError
"""RecoveryError late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
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


_ArtifactHandoffFacts = snapshot_artifacts_module._ArtifactHandoffFacts
"""_ArtifactHandoffFacts late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
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


_RecoveryArtifactPaths = snapshot_artifacts_module._RecoveryArtifactPaths
"""_RecoveryArtifactPaths late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
def _recovery_artifact_paths(destination: Path) -> _RecoveryArtifactPaths:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._recovery_artifact_paths(
        destination=destination,
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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._parent_chain_safe(
        destination=destination,
    )

def _artifact_parent_proof(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> str | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_parent_proof(
        destination=destination,
        expected_identity=expected_identity,
    )

def _open_recovery_parent_chain_no_follow(destination: Path) -> int:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._open_recovery_parent_chain_no_follow(
        destination=destination,
    )

_RecoveryParentHandle = snapshot_artifacts_module._RecoveryParentHandle
"""_RecoveryParentHandle late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
def _require_recovery_basename(name: str) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._require_recovery_basename(
        name=name,
    )

def _after_recovery_parent_bound(
    destination: Path,
    parent_identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._after_recovery_parent_bound(
        destination=destination,
        parent_identity=parent_identity,
    )

def _after_recovery_manifest_source_proved(
    destination: Path,
    manifest_temp_name: str,
    manifest_name: str,
    expected_source_identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._after_recovery_manifest_source_proved(
        destination=destination,
        manifest_temp_name=manifest_temp_name,
        manifest_name=manifest_name,
        expected_source_identity=expected_source_identity,
    )

def _prove_recovery_manifest_source(
    manifest_temp: Path,
    manifest_temp_name: str,
    *,
    expected_digest: str,
    expected_identity: tuple[int, int],
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._prove_recovery_manifest_source(
        manifest_temp=manifest_temp,
        manifest_temp_name=manifest_temp_name,
        expected_digest=expected_digest,
        expected_identity=expected_identity,
        parent=parent,
        parent_capture=_recovery_parent_capture,
    )

def _prove_recovery_manifest_destination(
    manifest_path: Path,
    *,
    expected_manifest_digest: str | None,
    handoff: _ArtifactHandoffFacts,
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._prove_recovery_manifest_destination(
        manifest_path=manifest_path,
        expected_manifest_digest=expected_manifest_digest,
        handoff=handoff,
        parent=parent,
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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._strict_file_state(
        path=path,
        parent=parent,
    )

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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._manifest_for_receipt(
        receipt=receipt,
        kind=kind,
    )

def _manifest_bytes(manifest: SnapshotManifest) -> bytes:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._manifest_bytes(
        manifest=manifest,
    )

def _manifest_digest_for_receipt(receipt: SnapshotReceipt) -> str:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._manifest_digest_for_receipt(
        receipt=receipt,
    )

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
        row_error = _refresh_receipt_row_error(facts, receipt)
        if row_error is not None:
            return _RefreshDecision(
                action="diverged",
                receipts=issued,
                error_code=row_error,
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


def _refresh_receipt_row_error(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
) -> str | None:
    """Return the stable structural error for one configured row."""

    if (
        receipt.resource_id != facts.resource_id
        or receipt.canonical_store_id != facts.canonical_store_id
    ):
        return "RECOVERY.LEDGER_IDENTITY_INVALID"
    if (
        receipt.destination_jsonl_path != facts.configured_jsonl_path
        or receipt.destination_manifest_path
        != facts.snapshot_manifest_path
    ):
        return "RECOVERY.LEDGER_PATH_INVALID"
    try:
        receipt.receipt
    except (TypeError, ValueError):
        return "RECOVERY.LEDGER_RECEIPT_INVALID"
    expected_count = _record_count_at(facts, receipt.exported_revision)
    if expected_count is None or receipt.record_count != expected_count:
        return "RECOVERY.ANCESTRY_INVALID"
    return None


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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_expected_identity(
        handoff=handoff,
        artifact=artifact,
    )

def _unproven_artifact_code(
    handoff: _ArtifactHandoffFacts | None,
    expectations: tuple[tuple[Path, set[str], str], ...],
    *,
    parent: _RecoveryParentHandle | None = None,
) -> str | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._unproven_artifact_code(
        handoff=handoff,
        expectations=expectations,
        parent=parent,
    )

def _prior_handoff_digests(
    handoff: _ArtifactHandoffFacts | None,
    artifact: str,
) -> set[str]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._prior_handoff_digests(
        handoff=handoff,
        artifact=artifact,
    )

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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._remove_owned_recovery_artifact(
        parent=parent,
        name=name,
        expected_identity=expected_identity,
    )

_RecoveryFileCapture = snapshot_artifacts_module._RecoveryFileCapture
"""_RecoveryFileCapture late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
def _recovery_parent_capture(
    parent: _RecoveryParentHandle,
    name: str,
    asset_kind: str,
) -> _RecoveryFileCapture:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._recovery_parent_capture(
        parent=parent,
        name=name,
        asset_kind=asset_kind,
    )

def _recovery_parent_open_exclusive(
    parent: _RecoveryParentHandle,
    name: str,
) -> tuple[int, _ActivationFileIdentity]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._recovery_parent_open_exclusive(
        parent=parent,
        name=name,
    )

def _remove_content_proven_artifact(
    path: Path,
    expected_digest: str,
    *,
    expected_identity: tuple[int, int] | None,
    parent: _RecoveryParentHandle | None = None,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._remove_content_proven_artifact(
        path=path,
        expected_digest=expected_digest,
        expected_identity=expected_identity,
        parent=parent,
    )

def _recovery_cleanup_parent_gate(
    destination: Path,
    parent: _RecoveryParentHandle | None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._recovery_cleanup_parent_gate(
        destination=destination,
        parent=parent,
    )

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
    if handoff is None:
        return _ExportReconciliation(
            IssuedReceiptRecovery(
                issued.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                ("RECOVERY.HANDOFF_MISSING",),
            ),
            ("RECOVERY.HANDOFF_MISSING",),
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
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._fsync_artifact_parent(
        destination=destination,
        expected_identity=expected_identity,
        parent=parent,
    )

def _terminal_receipt_row_blocker(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
) -> str | None:
    """Validate one terminal receipt independently of handoff identity.

    This is the handoff-independent prefix of terminal replay validation:
    resource/canonical identity, receipt shape, exact ancestry/count and
    configured-vs-arbitrary destination safety.  It is also used before
    accepting a terminal receipt with no handoff as a legitimate
    post-clear NOOP.
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
            "RECOVERY.LEDGER_IDENTITY_INVALID"
            if configured
            else "RECOVERY.EXPORT_LEDGER_IDENTITY_INVALID"
        )
    try:
        receipt.receipt
    except (TypeError, ValueError):
        return (
            "RECOVERY.LEDGER_RECEIPT_INVALID"
            if configured
            else "RECOVERY.EXPORT_RECEIPT_INVALID"
        )
    expected_count = _record_count_at(facts, receipt.exported_revision)
    if expected_count is None or receipt.record_count != expected_count:
        return (
            "RECOVERY.ANCESTRY_INVALID"
            if configured
            else "RECOVERY.EXPORT_ANCESTRY_INVALID"
        )
    if configured:
        if not _parent_chain_safe(receipt.destination_jsonl_path):
            return "RECOVERY.EXPORT_PARENT_UNSAFE"
    else:
        destination_error = _recovery_destination_safe(facts, receipt)
        if destination_error is not None:
            return destination_error
    return None


def _terminal_handoff_row_blocker(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
) -> tuple[str | None, _RecoveryParentHandle | None]:
    """Complete pre-cleanup validation and bind handoff parent authority."""

    blocker = _terminal_receipt_row_blocker(facts, receipt)
    if blocker is not None:
        return blocker, None
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


def _terminal_missing_handoff_blocker(
    facts: _RefreshRecoveryFacts,
    receipt: IssuedReceiptFacts,
) -> str | None:
    """Fail closed when a terminal missing handoff still has artifacts.

    A terminal receipt with no handoff is the normal state only after
    handoff clear and when all four deterministic temp/recovery members
    are provably absent beneath a safe current parent chain.  Any member,
    unsafe observation, invalid receipt or destination keeps every asset
    untouched and blocks recovery.
    """

    blocker = _terminal_receipt_row_blocker(facts, receipt)
    if blocker is not None:
        return blocker
    for handoff in facts.handoffs:
        linked = _receipt_facts_for(facts, handoff.snapshot_id)
        if (
            linked is not None
            and linked.destination_jsonl_path
            == receipt.destination_jsonl_path
            and linked.destination_manifest_path
            == receipt.destination_manifest_path
        ):
            return None
    if _missing_handoff_artifacts_present(receipt):
        return "RECOVERY.HANDOFF_MISSING"
    return None


def _missing_handoff_artifacts_present(
    receipt: IssuedReceiptFacts,
) -> bool:
    """Whether any deterministic temp/recovery member is not absent."""

    paths = _recovery_artifact_paths(receipt.destination_jsonl_path)
    for artifact in (
        paths.jsonl_temp,
        paths.manifest_temp,
        paths.jsonl_recovery,
        paths.manifest_recovery,
    ):
        try:
            os.lstat(artifact)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


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
    handoff_ids = {handoff.snapshot_id for handoff in facts.handoffs}
    for receipt in facts.receipts:
        if (
            receipt.status not in {"completed", "cancelled"}
            or receipt.snapshot_id in handoff_ids
        ):
            continue
        blocker = _terminal_missing_handoff_blocker(facts, receipt)
        if blocker is None:
            continue
        per_receipt.append(
            IssuedReceiptRecovery(
                receipt.snapshot_id,
                RefreshRecoveryState.BLOCKED,
                (blocker,),
            )
        )
        diagnostics.append(blocker)
        state = RefreshRecoveryState.BLOCKED
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
    read_error_code: str | None = None
    read_error_retryable = False
    rounds = 0
    while True:
        rounds += 1
        if rounds > _MAX_RECOVERY_ROUNDS:
            overall = RefreshRecoveryState.BLOCKED
            diagnostics.append("RECOVERY.ROUND_LIMIT")
            break
        try:
            facts = port.read_recovery_facts()
        except RecoveryError as error:
            overall = RefreshRecoveryState.BLOCKED
            read_error_code = error.error_code
            read_error_retryable = error.retryable
            diagnostics.append(error.error_code)
            break
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
        if not facts.binding_invalid and all(
            _refresh_receipt_row_error(facts, receipt) is None
            for receipt in issued_refresh
        ):
            missing_handoffs = tuple(
                receipt
                for receipt in issued_refresh
                if _handoff_for(facts, receipt.snapshot_id) is None
            )
            if missing_handoffs:
                for receipt in missing_handoffs:
                    per_receipt.append(
                        IssuedReceiptRecovery(
                            receipt.snapshot_id,
                            RefreshRecoveryState.BLOCKED,
                            ("RECOVERY.HANDOFF_MISSING",),
                        )
                    )
                overall = RefreshRecoveryState.BLOCKED
                diagnostics.append("RECOVERY.HANDOFF_MISSING")
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
    if read_error_code is None:
        try:
            final_facts = port.read_recovery_facts()
        except RecoveryError as error:
            overall = RefreshRecoveryState.BLOCKED
            read_error_code = error.error_code
            read_error_retryable = error.retryable
            diagnostics.append(error.error_code)
            export_results = ()
            export_diagnostics = ()
        else:
            export_results, export_diagnostics = _reconcile_issued_exports(
                port,
                final_facts,
            )
    else:
        export_results = ()
        export_diagnostics = ()
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
        error_code=read_error_code,
        retryable=read_error_retryable,
    )
