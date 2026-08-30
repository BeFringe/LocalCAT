"""Activation journal/terminal codec and durable file protocol (Task 5.R1).

Extracted from ``tm_sqlite_store.py`` without behavior change: the journal
phase machine (PREPARED -> DB_REPLACED -> MANIFEST_PUBLISHED ->
GENERATION_PUBLISHED), the terminal record protocol, the exclusive
temporary/replace/fsync primitives, and the shared activation types.  This
module is a leaf: it imports only frozen contracts and the standard library
and never imports ``tm_sqlite_store`` or ``SQLiteTMStore``.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path, PurePath

import tm_contracts as contract_module
from tm_content_attestation import (
    ActiveContentAttestation,
    PortableActiveContentAttestation,
    PortableContentFileProof,
    PortableSealedContentAttestation,
    SealedContentAttestation,
    _active_content_attestation_from_mapping,
    _active_content_attestation_record_from_mapping,
    _active_content_attestation_record_to_mapping,
    _active_content_attestation_to_mapping,
    _portable_active_content_attestation_from_mapping,
    _portable_active_content_attestation_to_mapping,
    _portable_content_file_proof_from_mapping,
    _portable_content_file_proof_to_mapping,
    _portable_sealed_content_attestation_from_mapping,
    _portable_sealed_content_attestation_to_mapping,
    _sealed_content_attestation_from_mapping,
    _sealed_content_attestation_to_mapping,
    _sealed_content_attestation_record_from_mapping,
    _sealed_content_attestation_record_to_mapping,
)
from platform_fs_contracts import (
    DEVICE_SECRET_SIZE_BYTES,
    BoundDirectoryAuthority,
    CandidateContentFacts,
    CandidateFile,
    ExistingFileRetirement,
    LedgerEnumerationLimits,
    LockLease,
    LockedDescendantNamespaceInspection,
    PendingPublication,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateProofContext,
    PrivateProofObjectRole,
    PublishMode,
    RetainedRetirement,
    RetirementDirectoryAuthority,
    RootedDirectoryAuthority,
    WindowsPrivateProof,
    decode_windows_private_proof,
    encode_windows_private_proof,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SealedStage,
)

_NATIVE_PATH_TYPE = type(Path())


@dataclass(frozen=True)
class _CanonicalStoreRef:
    """Coordinator-private reference to the published canonical paths.

    ``MutableStageRef`` intentionally rejects the canonical sidecar path.
    Once Task 5.7 has durably moved a sealed stage, operation leases still
    need an immutable path-bearing view, but that view must not weaken the
    public mutable-stage contract.  This exact private type is therefore the
    only representation accepted for an already-published sidecar.
    """

    stage_id: str
    resource_identity: CanonicalResourceIdentity
    staged_db_path: Path
    manifest_temp_path: Path

    def __post_init__(self) -> None:
        if type(self.stage_id) is not str or not self.stage_id:
            raise TypeError("canonical store reference id is invalid")
        if type(self.resource_identity) is not CanonicalResourceIdentity:
            raise TypeError("canonical store resource identity is invalid")
        if type(self.staged_db_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("canonical database path is invalid")
        if type(self.manifest_temp_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("canonical manifest path is invalid")
        if (
            self.staged_db_path
            != self.resource_identity.canonical_sidecar_path
            or self.manifest_temp_path
            != self.resource_identity.snapshot_manifest_path
        ):
            raise ValueError("canonical store paths are not deterministic")


@dataclass(frozen=True)

class _PriorActivationRef:
    """Coordinator-private prior-generation view for Task 5.8 cancel recovery.

    A durable PREPARED journal records the prior database path and the prior
    manifest file path.  After recovery cancels the preparation, the prior
    generation must be visible again; neither ``MutableStageRef`` (its
    temporary manifest path must differ from the final manifest) nor
    ``_CanonicalStoreRef`` (it requires the canonical sidecar path) can
    represent an arbitrary already-active prior path, so this exact private
    type is the only representation accepted for a restored prior view.
    """

    stage_id: str
    resource_identity: CanonicalResourceIdentity
    staged_db_path: Path
    manifest_temp_path: Path

    def __post_init__(self) -> None:
        if type(self.stage_id) is not str or not self.stage_id:
            raise TypeError("prior activation reference id is invalid")
        if type(self.resource_identity) is not CanonicalResourceIdentity:
            raise TypeError("prior activation resource identity is invalid")
        if type(self.staged_db_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("prior activation database path is invalid")
        if type(self.manifest_temp_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("prior activation manifest path is invalid")
        if (
            not self.staged_db_path.is_absolute()
            or ".." in self.staged_db_path.parts
            or not self.manifest_temp_path.is_absolute()
            or ".." in self.manifest_temp_path.parts
        ):
            raise ValueError("prior activation paths must be absolute")



type _StoreRuntimeRef = (
    MutableStageRef | _CanonicalStoreRef | _PriorActivationRef
)

@dataclass(frozen=True)
class _SQLiteGenerationView:
    stage: _StoreRuntimeRef
    canonical_store_id: str
    generation: int
    fts5_available: bool
    active_content_attestation: (
        ActiveContentAttestation | PortableActiveContentAttestation | None
    ) = None


class ActivationPreparationError(RuntimeError):
    """Stable code-only Task 5.5 failure with no path or TM payload."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        reason_code: str | None = None,
    ) -> None:
        if type(code) is not str or not code.startswith("ACTIVATION."):
            raise TypeError("activation error code is invalid")
        if type(retryable) is not bool:
            raise TypeError("activation retryable flag is invalid")
        if reason_code is not None and type(reason_code) is not str:
            raise TypeError("activation reason code is invalid")
        self.code = code
        self.retryable = retryable
        self.reason_code = reason_code
        super().__init__(code)



_ACTIVATION_RECOVERY_ACTIONS = frozenset(
    {"CANCELLED", "COMPLETED", "ROLLED_BACK"}
)

@dataclass(frozen=True)
class ActivationRecoveryReport:
    """Code-only outcome of one Task 5.8/5.9 activation recovery.

    The report carries only the journal phase recovered from, the action
    taken (CANCELLED, COMPLETED, or ROLLED_BACK), and the resulting
    generation; it never exposes filesystem paths, token ids, nonces, or
    raw journal JSON.
    """

    phase: str
    action: str
    generation: int | None

    def __post_init__(self) -> None:
        if (
            type(self.phase) is not str
            or self.phase
            not in {
                phase.value for phase in _PHASE_SEQUENCE
            }
        ):
            raise TypeError("recovery phase must be a code-only activation phase")
        if (
            type(self.action) is not str
            or self.action not in _ACTIVATION_RECOVERY_ACTIONS
        ):
            raise TypeError("recovery action is invalid")
        if self.generation is not None and (
            type(self.generation) is not int
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("recovery generation is invalid")


@dataclass(frozen=True)

class ActivationBackupEvidence:
    """Code-only, digest-backed evidence for one same-directory backup."""

    asset_kind: str
    original_digest: str
    backup_digest: str
    original_identity: tuple[int, int]
    backup_identity: tuple[int, int]

    def __post_init__(self) -> None:
        if self.asset_kind not in {"DATABASE", "MANIFEST"}:
            raise ValueError("activation backup asset kind is invalid")
        for digest in (self.original_digest, self.backup_digest):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("activation backup digest is invalid")
        for identity in (self.original_identity, self.backup_identity):
            if (
                type(identity) is not tuple
                or len(identity) != 2
                or any(type(value) is not int or value < 0 for value in identity)
            ):
                raise ValueError("activation backup identity is invalid")
        if self.original_digest != self.backup_digest:
            raise ValueError("activation backup digest does not close")
        if self.original_identity == self.backup_identity:
            raise ValueError("activation backup must be a distinct file")


@dataclass(frozen=True)

class _ActivationFileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)

class _PriorAssetCapture:
    asset_kind: str
    path: Path = field(repr=False, compare=False)
    identity: _ActivationFileIdentity = field(repr=False, compare=False)
    digest: str


@dataclass(frozen=True)

class _RecoveryBackupAsset:
    asset_kind: str
    original_path: Path = field(repr=False, compare=False)
    backup_path: Path = field(repr=False, compare=False)
    original_identity: _ActivationFileIdentity = field(
        repr=False,
        compare=False,
    )
    backup_identity: _ActivationFileIdentity = field(
        repr=False,
        compare=False,
    )
    evidence: ActivationBackupEvidence


@dataclass(frozen=True)

class _OwnedRecoveryPath:
    path: Path = field(repr=False, compare=False)
    identity: _ActivationFileIdentity = field(repr=False, compare=False)


@dataclass(frozen=True)

class _ActivationCleanupReservation:
    token: contract_module._ActivationToken | None = field(
        repr=False,
        compare=False,
    )
    prior_view: _SQLiteGenerationView | None = field(
        repr=False,
        compare=False,
    )
    owned_paths: tuple[_OwnedRecoveryPath, ...] = field(
        repr=False,
        compare=False,
    )



_ACTIVATION_PREPARATION_FACTORY_KEY = object()


@dataclass(frozen=True, slots=True, init=False)
class _ActivationPreparation:
    """Single-use coordinator-held capability for Tasks 5.6-5.9.

    Its repr is deliberately code-only.  Path-bearing registry snapshots,
    tokens, prior views, and backup paths remain hidden implementation state.
    """

    preparation_id: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    prior_canonical_store_id: str | None
    expected_prior_generation: int | None
    gate_b_grant_digest: str
    had_prior_canonical: bool
    prior_manifest_absent: bool
    backup_evidence: tuple[ActivationBackupEvidence, ...]
    _token: contract_module._ActivationToken = field(
        repr=False,
        compare=False,
    )
    _physical_snapshot: object = field(
        repr=False,
        compare=False,
    )
    _prior_view: _SQLiteGenerationView | None = field(
        repr=False,
        compare=False,
    )
    _backup_assets: tuple[_RecoveryBackupAsset, ...] = field(
        repr=False,
        compare=False,
    )
    _sealed_stage: SealedStage = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        *,
        preparation_id: str,
        resource_id: str,
        target_identity: str,
        canonical_store_id: str,
        prior_canonical_store_id: str | None,
        expected_prior_generation: int | None,
        gate_b_grant_digest: str,
        had_prior_canonical: bool,
        prior_manifest_absent: bool,
        backup_evidence: tuple[ActivationBackupEvidence, ...],
        _token: contract_module._ActivationToken,
        _physical_snapshot: object,
        _prior_view: _SQLiteGenerationView | None,
        _backup_assets: tuple[_RecoveryBackupAsset, ...],
        _sealed_stage: SealedStage,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _ACTIVATION_PREPARATION_FACTORY_KEY:
            raise TypeError("activation preparations require the Core factory")
        if type(preparation_id) is not str or not preparation_id:
            raise TypeError("activation preparation id is invalid")
        if type(had_prior_canonical) is not bool:
            raise TypeError("activation prior-presence flag is invalid")
        if type(prior_manifest_absent) is not bool:
            raise TypeError("activation prior-manifest-presence flag is invalid")
        if not had_prior_canonical and prior_manifest_absent:
            raise ValueError("first activation has no prior manifest to be absent")
        if had_prior_canonical != bool(_backup_assets):
            raise ValueError("activation prior asset state is inconsistent")
        backup_kinds = {asset.asset_kind for asset in _backup_assets}
        if had_prior_canonical:
            expected_backup_kinds = (
                {"DATABASE"} if prior_manifest_absent
                else {"DATABASE", "MANIFEST"}
            )
            if backup_kinds != expected_backup_kinds:
                raise ValueError("activation prior asset set is incomplete")
        if backup_evidence != tuple(
            asset.evidence for asset in _backup_assets
        ):
            raise ValueError("activation backup evidence is inconsistent")
        if type(_sealed_stage) is not SealedStage:
            raise TypeError("activation sealed stage is invalid")
        if prior_canonical_store_id is not None and (
            type(prior_canonical_store_id) is not str
            or not prior_canonical_store_id.strip()
        ):
            raise TypeError(
                "prior canonical store id must be a non-empty string or None"
            )
        if (
            prior_canonical_store_id is not None
            and prior_canonical_store_id == canonical_store_id
        ):
            raise ValueError(
                "explicit replacement must use a different canonical store id"
            )
        for name, value in (
            ("preparation_id", preparation_id),
            ("resource_id", resource_id),
            ("target_identity", target_identity),
            ("canonical_store_id", canonical_store_id),
            ("prior_canonical_store_id", prior_canonical_store_id),
            ("expected_prior_generation", expected_prior_generation),
            ("gate_b_grant_digest", gate_b_grant_digest),
            ("had_prior_canonical", had_prior_canonical),
            ("prior_manifest_absent", prior_manifest_absent),
            ("backup_evidence", backup_evidence),
            ("_token", _token),
            ("_physical_snapshot", _physical_snapshot),
            ("_prior_view", _prior_view),
            ("_backup_assets", _backup_assets),
            ("_sealed_stage", _sealed_stage),
        ):
            object.__setattr__(self, name, value)



class _ActivationJournalPhase(str, Enum):
    """Module-private strict activation journal phase (Task 5.6).

    Phases are strictly monotonic and code-only: callers may never pass a
    phase string or an arbitrary phase object as authority.
    """

    PREPARED = "PREPARED"
    DB_REPLACED = "DB_REPLACED"
    MANIFEST_PUBLISHED = "MANIFEST_PUBLISHED"
    GENERATION_PUBLISHED = "GENERATION_PUBLISHED"



_PHASE_SEQUENCE = (
    _ActivationJournalPhase.PREPARED,
    _ActivationJournalPhase.DB_REPLACED,
    _ActivationJournalPhase.MANIFEST_PUBLISHED,
    _ActivationJournalPhase.GENERATION_PUBLISHED,
)

_ACTIVATION_JOURNAL_VERSION = "activation-journal-v2"
_ACTIVATION_JOURNAL_FACTORY_KEY = object()


_ACTIVATION_JOURNAL_DIGEST_FIELDS = frozenset(
    {
        "artifact_seal_digest",
        "evidence_digest",
        "gate_b_grant_digest",
        "manifest_temp_digest",
        "new_manifest_digest",
        "sealed_stage_digest",
        "snapshot_receipt_digest",
        "source_jsonl_digest",
        "stage_db_digest",
        "target_identity",
    }
)

_ACTIVATION_JOURNAL_OPTIONAL_DIGEST_FIELDS = frozenset(
    {
        "prior_db_backup_digest",
        "prior_db_digest",
        "prior_manifest_backup_digest",
        "prior_manifest_digest",
        "prior_receipt_digest",
    }
)

_ACTIVATION_JOURNAL_IDENTITY_FIELDS = frozenset(
    {
        "activation_nonce",
        "artifact_id",
        "canonical_store_id",
        "journal_id",
        "journal_version",
        "new_receipt_id",
        "preparation_id",
        "registry_namespace",
        "resource_id",
        "token_id",
        "token_version",
    }
)

_ACTIVATION_JOURNAL_PATH_FIELDS = frozenset(
    {
        "candidate_manifest_temp_path",
        "candidate_stage_db_path",
        "journal_path",
        "new_manifest_path",
    }
)

_ACTIVATION_JOURNAL_OPTIONAL_PATH_FIELDS = frozenset(
    {
        "prior_db_backup_path",
        "prior_db_path",
        "prior_manifest_backup_path",
        "prior_manifest_path",
    }
)

_ACTIVATION_JOURNAL_IDENTITY_PAIR_FIELDS = frozenset(
    {
        "candidate_manifest_temp_identity",
        "candidate_stage_db_identity",
        "source_jsonl_identity",
    }
)

_ACTIVATION_JOURNAL_OPTIONAL_IDENTITY_PAIR_FIELDS = frozenset(
    {
        "prior_db_backup_identity",
        "prior_db_identity",
        "prior_manifest_backup_identity",
        "prior_manifest_identity",
    }
)

_ACTIVATION_JOURNAL_PRIOR_OPTIONAL_FIELDS = frozenset(
    {
        "prior_binding_snapshot_id",
        "prior_canonical_store_id",
        "prior_db_backup_digest",
        "prior_db_backup_identity",
        "prior_db_backup_path",
        "prior_db_digest",
        "prior_db_identity",
        "prior_db_path",
        "prior_manifest_backup_digest",
        "prior_manifest_backup_identity",
        "prior_manifest_backup_path",
        "prior_manifest_digest",
        "prior_manifest_identity",
        "prior_manifest_path",
        "prior_receipt_digest",
    }
)

_ACTIVATION_JOURNAL_RECORD_FIELDS = frozenset(
    _ACTIVATION_JOURNAL_DIGEST_FIELDS
    | _ACTIVATION_JOURNAL_IDENTITY_FIELDS
    | _ACTIVATION_JOURNAL_PATH_FIELDS
    | _ACTIVATION_JOURNAL_IDENTITY_PAIR_FIELDS
    | _ACTIVATION_JOURNAL_PRIOR_OPTIONAL_FIELDS
    | {
        "active_content_attestation",
        "expected_prior_generation",
        "had_prior_canonical",
        "phase",
        "prior_generation",
        "prior_manifest_absent",
        "sealed_content_attestation",
    }
)

_ACTIVATION_JOURNAL_ENVELOPE_FIELDS = (
    _ACTIVATION_JOURNAL_RECORD_FIELDS | {"record_digest"}
)

@dataclass(frozen=True)
class _ActivationJournalRecord:
    """Frozen strict closure for one durable activation journal phase.

    The record is the on-disk canonical JSON payload's in-memory mirror; it
    carries no path authority by itself.  The coordinator derives every
    field from registry-owned and preparation-owned facts, and every
    reload/advance re-proves the closure against those live facts.
    """

    journal_id: str
    journal_version: str
    journal_path: Path
    phase: _ActivationJournalPhase
    preparation_id: str
    registry_namespace: str
    token_id: str
    token_version: str
    activation_nonce: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    prior_canonical_store_id: str | None
    expected_prior_generation: int | None
    prior_generation: int | None
    gate_b_grant_digest: str
    evidence_digest: str
    snapshot_receipt_digest: str
    stage_db_digest: str
    manifest_temp_digest: str
    source_jsonl_digest: str
    new_receipt_id: str
    new_manifest_path: Path
    new_manifest_digest: str
    candidate_stage_db_path: Path
    candidate_manifest_temp_path: Path
    candidate_stage_db_identity: tuple[int, int]
    candidate_manifest_temp_identity: tuple[int, int]
    source_jsonl_identity: tuple[int, int]
    had_prior_canonical: bool
    prior_manifest_absent: bool
    prior_binding_snapshot_id: str | None
    prior_receipt_digest: str | None
    prior_manifest_digest: str | None
    prior_db_path: Path | None
    prior_manifest_path: Path | None
    prior_db_digest: str | None
    prior_db_identity: tuple[int, int] | None
    prior_manifest_identity: tuple[int, int] | None
    prior_db_backup_path: Path | None
    prior_manifest_backup_path: Path | None
    prior_db_backup_digest: str | None
    prior_manifest_backup_digest: str | None
    prior_db_backup_identity: tuple[int, int] | None
    prior_manifest_backup_identity: tuple[int, int] | None
    sealed_content_attestation: SealedContentAttestation
    active_content_attestation: ActiveContentAttestation | None

    def __post_init__(self) -> None:
        _validate_activation_journal_record(self)


@dataclass(frozen=True, slots=True, init=False)

class _ActivationJournalHandle:
    """Factory-gated journal capability returned by Task 5.6.

    The handle binds one exact on-disk journal file: its deterministic path,
    its published file identity, its phase, and its record digest.  It is
    never the authority; every transition re-reads and re-validates the
    durable journal and the coordinator's live preparation/registry facts.
    Its repr is deliberately code-only.
    """

    journal_id: str
    phase: _ActivationJournalPhase
    record_digest: str
    preparation_id: str
    journal_path: Path = field(repr=False, compare=False)
    file_identity: _ActivationFileIdentity = field(repr=False, compare=False)
    _record: _ActivationJournalRecord = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        *,
        journal_id: str,
        journal_path: Path,
        file_identity: _ActivationFileIdentity,
        phase: _ActivationJournalPhase,
        record_digest: str,
        preparation_id: str,
        _record: _ActivationJournalRecord,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _ACTIVATION_JOURNAL_FACTORY_KEY:
            raise TypeError(
                "activation journal handles require the Core factory"
            )
        if type(journal_id) is not str or not journal_id.strip():
            raise TypeError("activation journal id is invalid")
        if type(journal_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("activation journal path is invalid")
        if not journal_path.is_absolute() or ".." in journal_path.parts:
            raise ValueError("activation journal path must be absolute")
        if type(file_identity) is not _ActivationFileIdentity:
            raise TypeError("activation journal file identity is invalid")
        if type(phase) is not _ActivationJournalPhase:
            raise TypeError("activation journal phase is invalid")
        _require_activation_journal_digest(record_digest, "activation journal digest")
        if type(preparation_id) is not str or not preparation_id.strip():
            raise TypeError("activation journal preparation id is invalid")
        if type(_record) is not _ActivationJournalRecord:
            raise TypeError("activation journal record is invalid")
        if (
            _record.journal_id != journal_id
            or _record.journal_path != journal_path
            or _record.phase is not phase
            or _record.preparation_id != preparation_id
        ):
            raise ValueError("activation journal handle does not close")
        if _activation_journal_digest(_record) != record_digest:
            raise ValueError("activation journal handle digest mismatch")
        for name, value in (
            ("journal_id", journal_id),
            ("journal_path", journal_path),
            ("file_identity", file_identity),
            ("phase", phase),
            ("record_digest", record_digest),
            ("preparation_id", preparation_id),
            ("_record", _record),
        ):
            object.__setattr__(self, name, value)



def _require_activation_journal_digest(
    value: object,
    field_name: str,
) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")



def _validate_journal_native_path(value: object, field_name: str) -> None:
    if (
        type(value) is not _NATIVE_PATH_TYPE
        or not value.is_absolute()
        or ".." in value.parts
    ):
        raise TypeError(
            f"{field_name} must be an absolute normalized Path"
        )



def _validate_journal_native_identity_pair(
    value: object,
    field_name: str,
) -> None:
    if type(value) is not tuple or len(value) != 2:
        raise TypeError(f"{field_name} must be an identity pair")
    first, second = value
    if (
        type(first) is not int
        or isinstance(first, bool)
        or type(second) is not int
        or isinstance(second, bool)
        or first < 0
        or second < 0
    ):
        raise ValueError(
            f"{field_name} must contain non-negative integers"
        )



def _activation_journal_path(identity: CanonicalResourceIdentity) -> Path:
    """Deterministic journal path adjacent to the canonical sidecar."""

    return identity.canonical_sidecar_path.with_name(
        f".{identity.canonical_sidecar_path.name}.localcat-activation-journal.json"
    )



def _activation_journal_temp_path(journal_path: Path) -> Path:
    return journal_path.with_name(f"{journal_path.name}.tmp")



def _activation_terminal_path(identity: CanonicalResourceIdentity) -> Path:
    """Deterministic terminal record path adjacent to the canonical sidecar.

    The terminal record is the durable terminal authority that survives a
    cancelled (``PREPARED`` closure = CANCELLED/prior authority) or completed
    (``GENERATION_PUBLISHED`` closure = CONSUMED/new canonical authority)
    activation.  It mirrors the full authenticated main journal closure and is
    never caller-supplied: every terminal read/write/retire is identity-bound
    to this exact deterministic path.
    """

    return identity.canonical_sidecar_path.with_name(
        f".{identity.canonical_sidecar_path.name}.localcat-activation-terminal.json"
    )



def _activation_terminal_temp_path(terminal_path: Path) -> Path:
    return terminal_path.with_name(f"{terminal_path.name}.tmp")



def _lstat_activation_terminal_identity(
    path: Path,
) -> _ActivationFileIdentity | None:
    """Regular-file terminal identity, None when absent, fail-closed otherwise.

    A terminal must be an exact regular single-link file; symlinks, hard
    links, directories, and any other foreign entry fail closed and are
    never followed, used, or overwritten.
    """

    try:
        return _lstat_activation_journal_identity(path)
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_STATE_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error



_LINEAGE_MARKER_VERSION = "activated-lineage-v1"

_LINEAGE_MARKER_ENVELOPE_FIELDS = frozenset(
    {
        "lineage_version",
        "resource_id",
        "target_identity",
        "record_digest",
    }
)



def _activation_lineage_marker_path(
    identity: CanonicalResourceIdentity,
) -> Path:
    """Deterministic write-once activated-lineage marker path."""

    return identity.canonical_sidecar_path.with_name(
        f".{identity.canonical_sidecar_path.name}"
        ".localcat-activated-lineage.json"
    )



def _activation_lineage_marker_temp_path(marker_path: Path) -> Path:
    return marker_path.with_name(f"{marker_path.name}.tmp")



def _lstat_activation_lineage_marker_identity(
    path: Path,
) -> _ActivationFileIdentity | None:
    """Regular single-link marker identity, None when absent, fail-closed.

    The marker is a journal-managed durable fact, so a symlink, hardlink,
    directory, or any other foreign entry fails closed and is never
    followed, used, or overwritten.
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    return _ActivationFileIdentity(observed.st_dev, observed.st_ino)



def _activation_lineage_marker_payload(
    identity: CanonicalResourceIdentity,
) -> bytes:
    """Canonical strict-JSON payload for one activated-lineage marker.

    The marker binds only the stable lineage facts: the codec version, the
    resource id, and the stable target identity.  It deliberately does not
    bind ``canonical_store_id`` (or any mutable coordinator identity):
    explicit import/rebuild may create a new canonical store id, while
    later generations likewise leave this write-once marker unchanged.
    """

    payload = {
        "lineage_version": _LINEAGE_MARKER_VERSION,
        "resource_id": identity.resource_id,
        "target_identity": identity.target_identity,
    }
    mapping = dict(payload)
    mapping["record_digest"] = contract_module._stable_digest(payload)
    return json.dumps(
        mapping,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")



def _parse_activation_lineage_marker_bytes(
    payload: bytes,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Strictly parse and revalidate one durable lineage marker."""

    if type(payload) is not bytes:
        raise TypeError("activation lineage marker payload must be bytes")
    try:
        serialized = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate lineage marker key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            serialized,
            parse_constant=reject_non_finite,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    if type(value) is not dict:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    mapping: dict[str, object] = value
    if set(mapping) != _LINEAGE_MARKER_ENVELOPE_FIELDS:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    try:
        canonical = json.dumps(
            mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    if canonical != serialized:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    digest_field = mapping["record_digest"]
    _require_activation_journal_digest(digest_field, "record_digest")
    payload_mapping = {
        key: value
        for key, value in mapping.items()
        if key != "record_digest"
    }
    if contract_module._stable_digest(payload_mapping) != digest_field:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    if mapping["lineage_version"] != _LINEAGE_MARKER_VERSION:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    for field_name, expected in (
        ("resource_id", identity.resource_id),
        ("target_identity", identity.target_identity),
    ):
        value = mapping[field_name]
        if type(value) is not str or value != expected:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )



def _read_activation_lineage_marker(
    path: Path,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Durably read and strictly revalidate one lineage marker."""

    marker_identity = _lstat_activation_lineage_marker_identity(path)
    if marker_identity is None:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    try:
        payload, observed_identity = _read_activation_journal_file(
            path,
            marker_identity,
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error
    if observed_identity != marker_identity:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    _parse_activation_lineage_marker_bytes(
        payload,
        identity=identity,
    )



def _read_activation_lineage_marker_bytes(
    path: Path,
    expected_identity: _ActivationFileIdentity | None,
) -> bytes:
    """Identity-bound O_NOFOLLOW read with post-read path revalidation.

    The descriptor is opened without following symlinks and the open-time
    fstat must prove a regular file with the exact expected identity; the
    same path is re-lstat'ed after the descriptor read so a swap or a
    hardlink added mid-read fails closed.  The two-link handoff state
    (marker publication temporary linked to the final) is allowed here
    because the caller separately proves the paired inode state.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        identity = _ActivationFileIdentity(observed.st_dev, observed.st_ino)
        if (
            expected_identity is not None
            and identity != expected_identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    finally:
        os.close(descriptor)
    try:
        final = os.lstat(path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    if (
        not stat.S_ISREG(final.st_mode)
        or (final.st_dev, final.st_ino)
        != (identity.device, identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    return bytes(payload)



def _revalidate_activation_lineage_marker_final(
    marker_path: Path,
    expected_identity: _ActivationFileIdentity,
    expected_bytes: bytes,
    identity: CanonicalResourceIdentity,
) -> None:
    """Strictly revalidate one published marker final after handoff."""

    final_identity = _lstat_activation_lineage_marker_identity(
        marker_path
    )
    if final_identity is None or final_identity != expected_identity:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    payload = _read_activation_lineage_marker_bytes(
        marker_path,
        expected_identity,
    )
    if payload != expected_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    _parse_activation_lineage_marker_bytes(
        payload,
        identity=identity,
    )



def _unlink_activation_lineage_marker_handoff_temp(
    temp_path: Path,
    marker_path: Path,
    expected_identity: _ActivationFileIdentity,
) -> None:
    """Identity-bound unlink of the temporary after the two-link handoff.

    Both names must still be the same exact regular inode with exactly two
    links (the temporary plus the final); a foreign, hardlinked, swapped,
    or vanished entry fails closed and is never removed.
    """

    try:
        temp_observed = os.lstat(temp_path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    try:
        final_observed = os.lstat(marker_path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    if (
        not stat.S_ISREG(temp_observed.st_mode)
        or temp_observed.st_nlink != 2
        or (temp_observed.st_dev, temp_observed.st_ino)
        != (expected_identity.device, expected_identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    if (
        not stat.S_ISREG(final_observed.st_mode)
        or final_observed.st_nlink != 2
        or (final_observed.st_dev, final_observed.st_ino)
        != (temp_observed.st_dev, temp_observed.st_ino)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    try:
        os.unlink(temp_path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    try:
        _fsync_activation_directory(temp_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error



def _publish_activation_lineage_marker_link(
    temp_path: Path,
    marker_path: Path,
    temp_identity: _ActivationFileIdentity,
) -> None:
    """Narrow fault-injection seam for the atomic no-clobber link."""

    os.link(temp_path, marker_path)



def _publish_activation_lineage_marker_from_temp(
    temp_path: Path,
    marker_path: Path,
    temp_identity: _ActivationFileIdentity,
    expected_bytes: bytes,
    identity: CanonicalResourceIdentity,
) -> None:
    """Atomically publish a fully fsynced temporary as the marker final.

    The hard-link operation fails with ``FileExistsError`` when the final
    already exists, so a concurrently inserted foreign final (symlink,
    regular file, directory, or hardlink) is never silently overwritten.
    The parent directory is fsynced after the link, the temporary is
    unlinked only while it is still the exact same inode paired with the
    final, and the parent is fsynced again.  Any failure before the
    temporary unlink leaves the recoverable two-link handoff (or the owned
    temporary) for a later replay; any foreign final/temp is never removed
    or overwritten.
    """

    try:
        _publish_activation_lineage_marker_link(
            temp_path,
            marker_path,
            temp_identity,
        )
    except FileExistsError as error:
        # A foreign final won the race: it is never overwritten.  The
        # temporary is still exclusively ours, so it is cleaned
        # identity-bound and the fail-stop is non-retryable.
        _ = _remove_owned_activation_journal_temp(
            temp_path,
            temp_identity,
        )
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    try:
        _fsync_activation_directory(marker_path.parent)
        _unlink_activation_lineage_marker_handoff_temp(
            temp_path,
            marker_path,
            temp_identity,
        )
        _fsync_activation_directory(marker_path.parent)
    except ActivationPreparationError:
        raise
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    _revalidate_activation_lineage_marker_final(
        marker_path,
        temp_identity,
        expected_bytes,
        identity,
    )



def _finish_activation_lineage_marker_handoff(
    temp_path: Path,
    marker_path: Path,
    handoff_identity: _ActivationFileIdentity,
    expected_bytes: bytes,
    identity: CanonicalResourceIdentity,
) -> None:
    """Finish one interrupted two-link marker handoff after a crash.

    The temporary two-link handoff is accepted only when the final and the
    temporary are the same exact inode and the bytes equal the
    deterministic marker payload; the temporary is then unlinked and the
    single-link final revalidated.  Any other symlink/hardlink/foreign
    final or temp fails closed and is never removed or overwritten.
    """

    try:
        final_observed = os.lstat(marker_path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    try:
        temp_observed = os.lstat(temp_path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    if (
        not stat.S_ISREG(final_observed.st_mode)
        or final_observed.st_nlink != 2
        or (final_observed.st_dev, final_observed.st_ino)
        != (handoff_identity.device, handoff_identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    if (
        not stat.S_ISREG(temp_observed.st_mode)
        or temp_observed.st_nlink != 2
        or (temp_observed.st_dev, temp_observed.st_ino)
        != (handoff_identity.device, handoff_identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    payload = _read_activation_lineage_marker_bytes(
        marker_path,
        handoff_identity,
    )
    if payload != expected_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    try:
        os.unlink(temp_path)
        _fsync_activation_directory(marker_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    _revalidate_activation_lineage_marker_final(
        marker_path,
        handoff_identity,
        expected_bytes,
        identity,
    )



def _write_activation_lineage_marker_temp(
    temp_path: Path,
    marker_path: Path,
    expected_bytes: bytes,
    identity: CanonicalResourceIdentity,
) -> None:
    """Exclusively create, fully write, and fsync the marker temporary."""

    descriptor = -1
    temp_identity: _ActivationFileIdentity | None = None
    try:
        descriptor, temp_identity = _open_activation_journal_temp(
            temp_path
        )
    except FileExistsError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        ) from error
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    assert temp_identity is not None
    try:
        try:
            _write_activation_journal_bytes(descriptor, expected_bytes)
            _fsync_activation_journal(descriptor)
            _close_activation_journal(descriptor)
            descriptor = -1
        except OSError as error:
            _ = _remove_owned_activation_journal_temp(
                temp_path,
                temp_identity,
            )
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=True,
            ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    temp_bytes = _read_activation_lineage_marker_bytes(
        temp_path,
        temp_identity,
    )
    if temp_bytes != expected_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    _publish_activation_lineage_marker_from_temp(
        temp_path,
        marker_path,
        temp_identity,
        expected_bytes,
        identity,
    )



def _activation_lineage_marker_state_complete(
    identity: CanonicalResourceIdentity,
) -> _ActivationFileIdentity | None:
    """Strictly validate that one final/temp lineage-marker state is complete.

    Returns the exact final identity when a complete published marker
    exists, or ``None`` when both the final and the temporary are absent.
    A valid final is accepted only when the temporary is absent (the
    single-link final is strictly revalidated) or when the final and the
    temporary are the same exact regular inode in the expected two-link
    handoff carrying the deterministic payload, in which case the handoff
    is finished durably (temporary unlinked and parent fsynced) and the
    single-link final revalidated.  Any non-paired regular, symlink,
    directory, extra-link, wrong-identity, or wrong-byte temporary fails
    closed and is never deleted or overwritten; a foreign final likewise
    fails closed.
    """

    marker_path = _activation_lineage_marker_path(identity)
    temp_path = _activation_lineage_marker_temp_path(marker_path)
    expected_bytes = _activation_lineage_marker_payload(identity)
    try:
        temp_observed = os.lstat(temp_path)
    except FileNotFoundError:
        temp_observed = None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    try:
        final_observed = os.lstat(marker_path)
    except FileNotFoundError:
        final_observed = None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    if final_observed is None:
        if temp_observed is not None:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        return None
    if temp_observed is None:
        marker_identity = _lstat_activation_lineage_marker_identity(
            marker_path
        )
        if marker_identity is None:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        _read_activation_lineage_marker(
            marker_path,
            identity=identity,
        )
        return marker_identity
    if (
        not stat.S_ISREG(temp_observed.st_mode)
        or not stat.S_ISREG(final_observed.st_mode)
        or (temp_observed.st_dev, temp_observed.st_ino)
        != (final_observed.st_dev, final_observed.st_ino)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    _finish_activation_lineage_marker_handoff(
        temp_path,
        marker_path,
        _ActivationFileIdentity(
            final_observed.st_dev,
            final_observed.st_ino,
        ),
        expected_bytes,
        identity,
    )
    return _ActivationFileIdentity(
        final_observed.st_dev,
        final_observed.st_ino,
    )



def _ensure_activation_lineage_marker(
    identity: CanonicalResourceIdentity,
) -> None:
    """Durably publish (or revalidate) the write-once activated-lineage marker.

    The marker records that this resource/target has crossed physical
    activation at least once and binds only version + resource_id +
    target_identity + digest, so it keeps validating unchanged across
    store ids, generations, imports, and rebuilds.  Publication is an
    atomic no-clobber protocol: an exclusive deterministic temporary is
    fully written and fsynced, the final is published with a hard-link
    that fails if the final already exists, the parent is fsynced, the
    temporary is unlinked only while it is still the exact paired inode,
    and the parent is fsynced again.  An existing valid marker is
    revalidated, never rewritten; an interrupted owned temporary
    (byte-exact regular single-link file) resumes publication.  A final
    that already exists is accepted only when the temporary is absent or
    the exact paired two-link handoff with the deterministic payload is
    finished durably; any other conflicting temporary fails closed and is
    never removed or overwritten.
    """

    marker_path = _activation_lineage_marker_path(identity)
    temp_path = _activation_lineage_marker_temp_path(marker_path)
    expected_bytes = _activation_lineage_marker_payload(identity)
    try:
        final_observed = os.lstat(marker_path)
    except FileNotFoundError:
        final_observed = None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    if final_observed is not None:
        _ = _activation_lineage_marker_state_complete(identity)
        return
    try:
        temp_observed = os.lstat(temp_path)
    except FileNotFoundError:
        temp_observed = None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=True,
        ) from error
    if temp_observed is not None:
        if (
            not stat.S_ISREG(temp_observed.st_mode)
            or temp_observed.st_nlink != 1
        ):
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        temp_identity = _ActivationFileIdentity(
            temp_observed.st_dev,
            temp_observed.st_ino,
        )
        temp_bytes = _read_activation_lineage_marker_bytes(
            temp_path,
            temp_identity,
        )
        if temp_bytes != expected_bytes:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        _publish_activation_lineage_marker_from_temp(
            temp_path,
            marker_path,
            temp_identity,
            expected_bytes,
            identity,
        )
        return
    _write_activation_lineage_marker_temp(
        temp_path,
        marker_path,
        expected_bytes,
        identity,
    )



def _lstat_any_entry(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_STATE_INVALID",
            retryable=False,
        ) from error
    return True



def _lstat_activation_journal_identity(
    path: Path,
) -> _ActivationFileIdentity | None:
    """Regular-file identity, None when absent, fail-closed for other kinds."""

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_STATE_INVALID",
            retryable=False,
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_FINAL_EXISTS",
            retryable=False,
        )
    return _ActivationFileIdentity(observed.st_dev, observed.st_ino)



def _open_activation_journal_temp(
    path: Path,
) -> tuple[int, _ActivationFileIdentity]:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        observed = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
    ):
        os.close(descriptor)
        raise OSError(
            "activation journal temporary is not an exclusive regular file"
        )
    return descriptor, _ActivationFileIdentity(observed.st_dev, observed.st_ino)



def _write_activation_journal_bytes(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("activation journal write made no progress")
        view = view[written:]



def _fsync_activation_journal(descriptor: int) -> None:
    os.fsync(descriptor)



def _close_activation_journal(descriptor: int) -> None:
    os.close(descriptor)



def _read_activation_journal_file(
    path: Path,
    expected_identity: _ActivationFileIdentity | None,
) -> tuple[bytes, _ActivationFileIdentity]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PARSE_INVALID",
                retryable=False,
            )
        identity = _ActivationFileIdentity(observed.st_dev, observed.st_ino)
        if (
            expected_identity is not None
            and identity != expected_identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_HANDLE_STALE",
                retryable=False,
            )
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error
    finally:
        os.close(descriptor)
    if _lstat_activation_journal_identity(path) != identity:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_HANDLE_STALE",
            retryable=False,
        )
    return bytes(payload), identity



def _remove_owned_activation_journal_temp(
    path: Path,
    expected_identity: _ActivationFileIdentity,
) -> bool:
    """Remove exactly the owned temporary; return True only when provable."""

    try:
        observed = os.lstat(path)
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
        os.unlink(path)
    except OSError:
        return False
    try:
        _fsync_activation_directory(path.parent)
    except OSError:
        return False
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False



def _remove_owned_activation_journal_final(
    path: Path,
    expected_identity: _ActivationFileIdentity,
) -> None:
    """Durably remove exactly the handled journal after a Task 5.8 cancel.

    The journal is the durable single-use token record.  For a terminal
    ``PREPARED`` cancellation the journal is retired only after the prior/
    legacy state is proven unchanged and every journal-owned backup is
    provably cleaned; for a completed activation the journal is instead
    retained as the durable consumed marker (see
    :meth:`ResourceStoreCoordinator.recover_durable_activation`).  Absence
    is never accepted as proof here: the caller already loaded and identity-
    proven this exact journal, so a vanished file is a tamper/mismatch and
    the recovery fails closed with the durable state preserved.  Every step
    (identity, unlink, directory fsync, absence revalidation) must be
    provable or the recovery fails closed and the journal stays recoverable.
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        ) from error
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != (expected_identity.device, expected_identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        )
    try:
        os.unlink(path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        ) from error
    try:
        _fsync_activation_directory(path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        ) from error
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            retryable=True,
        ) from error
    raise ActivationPreparationError(
        "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
        retryable=True,
    )



def _remove_owned_activation_terminal_final(
    path: Path,
    expected_identity: _ActivationFileIdentity,
) -> None:
    """Durably retire exactly the proven terminal record after handoff.

    The prior terminal authority is strictly retired only after the new
    PREPARED main journal (or the new CANCELLED terminal) is durable and
    revalidated, so every crash point leaves at least one valid authority.
    Every step (identity, unlink, directory fsync, absence revalidation)
    must be provable or the terminal stays recoverable and recovery fails
    closed with both authorities preserved.
    """

    try:
        _remove_owned_activation_journal_final(path, expected_identity)
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_RETIRE_FAILED",
            retryable=error.retryable,
            reason_code=error.code,
        ) from error



def _remove_orphaned_activation_temp(path: Path) -> None:
    """Strictly remove one orphaned handoff temporary (regular single-link).

    A crash can leave a durable journal/terminal temporary behind while the
    surviving authority lives at the sibling path.  Only an exact regular
    single-link file at the deterministic temporary path is removable; a
    foreign, linked, or unprovable entry fails closed and is never removed.
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TEMP_CLEANUP_FAILED",
            retryable=True,
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TEMP_CLEANUP_FAILED",
            retryable=False,
        )
    identity = _ActivationFileIdentity(observed.st_dev, observed.st_ino)
    if not _remove_owned_activation_journal_temp(path, identity):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TEMP_CLEANUP_FAILED",
            retryable=True,
        )



def _terminal_new_authority_closes_main_prior(
    terminal_record: _ActivationJournalRecord,
    main_record: _ActivationJournalRecord,
) -> bool:
    """True when a CONSUMED terminal closes the pending main journal's prior.

    During the next-activation handoff the retained CONSUMED terminal is the
    previous completed generation's published closure: its published DB file
    identity (the same file now lives at the canonical sidecar path), the
    published manifest file identity/digest, the completed receipt, and the
    generation must equal exactly what the new pending main journal records
    as its unchanged prior.
    """

    if (
        terminal_record.phase
        is not _ActivationJournalPhase.GENERATION_PUBLISHED
        or not main_record.had_prior_canonical
        or main_record.prior_generation is None
        or main_record.prior_db_path is None
        or main_record.prior_db_identity is None
        or main_record.prior_db_digest is None
        or main_record.prior_manifest_absent
        or main_record.prior_manifest_path is None
        or main_record.prior_manifest_identity is None
        or main_record.prior_manifest_digest is None
        or main_record.prior_receipt_digest is None
        or main_record.prior_binding_snapshot_id is None
    ):
        return False
    terminal_generation = (
        0
        if terminal_record.expected_prior_generation is None
        else terminal_record.expected_prior_generation + 1
    )
    return (
        terminal_generation == main_record.prior_generation
        and terminal_record.candidate_stage_db_identity
        == main_record.prior_db_identity
        and terminal_record.new_manifest_path
        == main_record.prior_manifest_path
        and terminal_record.new_manifest_digest
        == main_record.prior_manifest_digest
        and terminal_record.candidate_manifest_temp_identity
        == main_record.prior_manifest_identity
        and terminal_record.new_receipt_id
        == main_record.prior_binding_snapshot_id
        and terminal_record.snapshot_receipt_digest
        == main_record.prior_receipt_digest
    )



def _terminal_prior_closure_matches(
    terminal_record: _ActivationJournalRecord,
    main_record: _ActivationJournalRecord,
) -> bool:
    """True when a CANCELLED terminal retains the pending main journal's prior.

    A CANCELLED terminal is a ``PREPARED``-phase closure whose authority is
    its prior generation; the pending main journal (same or a later
    activation) must close exactly that same unchanged prior.
    """

    if terminal_record.phase is not _ActivationJournalPhase.PREPARED:
        return False
    if terminal_record.had_prior_canonical != main_record.had_prior_canonical:
        return False
    if not main_record.had_prior_canonical:
        return True
    if (
        terminal_record.expected_prior_generation
        != main_record.expected_prior_generation
        or terminal_record.prior_generation != main_record.prior_generation
        or terminal_record.prior_db_path != main_record.prior_db_path
        or terminal_record.prior_db_identity != main_record.prior_db_identity
        or terminal_record.prior_db_digest != main_record.prior_db_digest
        or terminal_record.prior_manifest_absent
        != main_record.prior_manifest_absent
        or terminal_record.prior_manifest_path
        != main_record.prior_manifest_path
        or terminal_record.prior_manifest_identity
        != main_record.prior_manifest_identity
        or terminal_record.prior_manifest_digest
        != main_record.prior_manifest_digest
        or terminal_record.prior_receipt_digest
        != main_record.prior_receipt_digest
        or terminal_record.prior_binding_snapshot_id
        != main_record.prior_binding_snapshot_id
    ):
        return False
    return True



def _rollback_terminal_prior_closes(
    terminal_record: _ActivationJournalRecord,
    main_record: _ActivationJournalRecord,
) -> bool:
    """True when a PREPARED rollback terminal closes the pending main prior.

    Task 5.9 writes the prior-authority terminal only after the restored
    prior pair is durable and revalidated, then retires the pending main
    journal.  A crash in that window leaves a PREPARED terminal beside the
    pending main journal; the terminal's prior identities are the restored
    copies while the main journal records the original identities.  Both
    close the same canonical state: the restored copies are byte-identical,
    so every digest, the source identity/digest, the binding receipt, and
    the generation match.  The terminal must be the same activation (same
    journal id) and the pending main journal still takes precedence;
    recovery re-runs the rollback idempotently and retires/rewrites the
    terminal.  Any other difference is a foreign/tampered terminal.
    """

    if (
        terminal_record.journal_id != main_record.journal_id
        or terminal_record.phase is not _ActivationJournalPhase.PREPARED
        or terminal_record.had_prior_canonical
        != main_record.had_prior_canonical
    ):
        return False
    if not main_record.had_prior_canonical:
        return True
    if (
        terminal_record.expected_prior_generation
        != main_record.expected_prior_generation
        or terminal_record.prior_generation != main_record.prior_generation
        or terminal_record.prior_db_path != main_record.prior_db_path
        or terminal_record.prior_db_digest != main_record.prior_db_digest
        or terminal_record.prior_manifest_absent
        != main_record.prior_manifest_absent
        or terminal_record.prior_manifest_path
        != main_record.prior_manifest_path
        or terminal_record.prior_manifest_digest
        != main_record.prior_manifest_digest
        or terminal_record.prior_receipt_digest
        != main_record.prior_receipt_digest
        or terminal_record.prior_binding_snapshot_id
        != main_record.prior_binding_snapshot_id
        or terminal_record.source_jsonl_identity
        != main_record.source_jsonl_identity
        or terminal_record.source_jsonl_digest
        != main_record.source_jsonl_digest
    ):
        return False
    return True



def _activation_terminal_coexistence_valid(
    main_record: _ActivationJournalRecord,
    terminal_record: _ActivationJournalRecord,
) -> bool:
    """Deterministic terminal/main coexistence rule (Task 5.8/5.9 handoff).

    The pending main journal always takes precedence; a coexisting terminal
    is tolerated only when it closes the same canonical state: a CONSUMED
    terminal's published closure must equal the pending main journal's prior
    closure, a CANCELLED terminal's prior closure must equal the pending main
    journal's prior closure, a terminal beside a terminal main journal must
    be the identical closure (or an older CONSUMED closure of the same
    prior), and a Task 5.9 rollback terminal (restored prior identities)
    must close the pending main journal's prior digests.  Any other
    coexistence is a foreign/tampered terminal and fails closed without
    being used or overwritten.
    """

    if (
        terminal_record.journal_version != _ACTIVATION_JOURNAL_VERSION
        or terminal_record.resource_id != main_record.resource_id
        or terminal_record.target_identity != main_record.target_identity
    ):
        return False
    if main_record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
        if (
            terminal_record.phase
            is not _ActivationJournalPhase.GENERATION_PUBLISHED
        ):
            return (
                terminal_record.canonical_store_id
                == main_record.canonical_store_id
                and _rollback_terminal_prior_closes(
                    terminal_record,
                    main_record,
                )
            )
        if terminal_record == main_record:
            return True
        return _terminal_new_authority_closes_main_prior(
            terminal_record,
            main_record,
        )
    if terminal_record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
        return _terminal_new_authority_closes_main_prior(
            terminal_record,
            main_record,
        )
    return (
        (
            terminal_record.canonical_store_id
            == main_record.canonical_store_id
            and _rollback_terminal_prior_closes(
                terminal_record,
                main_record,
            )
        )
        or _terminal_prior_closure_matches(terminal_record, main_record)
    )



def _remove_journal_proven_backups(
    record: _ActivationJournalRecord,
) -> None:
    """Strictly clean only the journal-proven owned recovery backups.

    The durable journal is the sole surviving ownership locator after a
    restart, so each backup path, identity, and digest comes from the
    journal record itself.  A missing backup is an already-proven prior
    partial cleanup and is skipped idempotently; any present file must be
    the exact journal-owned regular file (identity and digest, no hard
    links) or the cleanup fails closed with every backup and the journal
    preserved (the Task 5.9 seam).  Each unlink is followed by a parent
    directory fsync and a final absence postcondition, so a crash at any
    boundary resumes from the journal without orphaning authority files.
    """

    if not record.had_prior_canonical:
        return
    owned: list[_OwnedRecoveryPath] = []
    expected_digests: list[str] = []
    _backup_triples = [
        (
            record.prior_db_backup_path,
            record.prior_db_backup_identity,
            record.prior_db_backup_digest,
        ),
    ]
    if not record.prior_manifest_absent:
        _backup_triples.append(
            (
                record.prior_manifest_backup_path,
                record.prior_manifest_backup_identity,
                record.prior_manifest_backup_digest,
            )
        )
    for path, identity_value, digest_value in _backup_triples:
        if path is None or identity_value is None or digest_value is None:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
        owned.append(
            _OwnedRecoveryPath(
                path=path,
                identity=_ActivationFileIdentity(
                    identity_value[0],
                    identity_value[1],
                ),
            )
        )
        expected_digests.append(digest_value)
    parents = {entry.path.parent for entry in owned}
    for entry, expected_digest in zip(owned, expected_digests):
        try:
            observed = os.lstat(entry.path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=True,
            ) from error
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (entry.identity.device, entry.identity.inode)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=False,
            )
        try:
            capture = _capture_activation_file(
                entry.path,
                asset_kind="JOURNAL_CLOSURE",
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=False,
            ) from error
        if capture.digest != expected_digest:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=False,
            )
        try:
            _unlink_recovery_backup(entry.path)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=True,
            ) from error
        try:
            _fsync_recovery_deletion_directory(entry.path.parent)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=True,
            ) from error
        _require_recovery_path_absent(entry.path)
    for parent in parents:
        try:
            _fsync_recovery_deletion_directory(parent)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
                retryable=True,
            ) from error
    for entry in owned:
        _require_recovery_path_absent(entry.path)


def _activation_file_identity(path: Path) -> _ActivationFileIdentity:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    return _ActivationFileIdentity(observed.st_dev, observed.st_ino)



def _capture_activation_file(
    path: Path,
    *,
    asset_kind: str,
) -> _PriorAssetCapture:
    # Single-link closure: the journal-managed file must be an exact
    # regular single-link entry at the initial lstat, at the descriptor
    # fstat/read, and at the final revalidation.  A hardlink added at any
    # of these seams fails closed so a foreign link is never captured,
    # copied, or retired as a journal-owned asset.
    try:
        initial = os.lstat(path)
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
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    try:
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
        identity = _ActivationFileIdentity(observed.st_dev, observed.st_ino)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    finally:
        os.close(descriptor)
    if _activation_file_identity(path) != identity:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    return _PriorAssetCapture(
        asset_kind=asset_kind,
        path=path,
        identity=identity,
        digest=digest.hexdigest(),
    )



def _read_activation_file_bytes(capture: _PriorAssetCapture) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(capture.path, flags)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (capture.identity.device, capture.identity.inode)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    finally:
        os.close(descriptor)
    # Final revalidation: the path must still be the exact same regular
    # single-link entry after the descriptor read.  A path swap or a
    # hardlink added during the read fails closed so a foreign link is
    # never returned as a journal-owned asset.
    if _activation_file_identity(capture.path) != capture.identity:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    return bytes(payload)



def _capture_pre_drain_assets(
    view: _SQLiteGenerationView | None,
    *,
    identity: CanonicalResourceIdentity,
    replacement: bool = False,
) -> tuple[_PriorAssetCapture, ...]:
    if view is None:
        _require_first_activation_absence(identity)
        return (
            _capture_activation_file(
                identity.configured_jsonl_path,
                asset_kind="SOURCE",
            ),
        )
    captures = [
        _capture_activation_file(
            view.stage.staged_db_path,
            asset_kind="DATABASE",
        ),
    ]
    if not replacement or _lstat_any_entry(
        identity.snapshot_manifest_path
    ):
        captures.append(
            _capture_activation_file(
                identity.snapshot_manifest_path,
                asset_kind="MANIFEST",
            )
        )
    captures.append(
        _capture_activation_file(
            identity.configured_jsonl_path,
            asset_kind="SOURCE",
        )
    )
    return tuple(captures)



def _require_same_asset_captures(
    before: tuple[_PriorAssetCapture, ...],
    after: tuple[_PriorAssetCapture, ...],
) -> None:
    before_facts = tuple(
        (item.asset_kind, item.identity, item.digest) for item in before
    )
    after_facts = tuple(
        (item.asset_kind, item.identity, item.digest) for item in after
    )
    if before_facts != after_facts:
        raise ActivationPreparationError(
            "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
            retryable=False,
        )



def _require_first_activation_absence(
    identity: CanonicalResourceIdentity,
) -> None:
    for path in (
        identity.canonical_sidecar_path,
        identity.snapshot_manifest_path,
    ):
        try:
            _ = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            ) from error
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_UNEXPECTED",
            retryable=False,
        )



def _open_recovery_backup(path: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o600)



def _write_recovery_backup(source_descriptor: int, backup_descriptor: int) -> None:
    while True:
        chunk = os.read(source_descriptor, 1024 * 1024)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(backup_descriptor, view)
            if written <= 0:
                raise OSError("recovery backup write made no progress")
            view = view[written:]



def _fsync_recovery_backup(descriptor: int) -> None:
    os.fsync(descriptor)



def _fsync_activation_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)



def _fsync_recovery_directory(path: Path) -> None:
    _fsync_activation_directory(path)



def _unlink_recovery_backup(path: Path) -> None:
    os.unlink(path)



def _fsync_recovery_deletion_directory(path: Path) -> None:
    _fsync_activation_directory(path)



def _require_recovery_path_absent(path: Path) -> None:
    try:
        _ = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.CLEANUP_FAILED",
            retryable=True,
        ) from error
    raise ActivationPreparationError(
        "ACTIVATION.CLEANUP_FAILED",
        retryable=True,
    )



def _remove_recovery_path(owned: _OwnedRecoveryPath) -> None:
    try:
        observed = os.lstat(owned.path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.CLEANUP_FAILED",
            retryable=True,
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != (owned.identity.device, owned.identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.CLEANUP_FAILED",
            retryable=True,
        )
    try:
        _unlink_recovery_backup(owned.path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.CLEANUP_FAILED",
            retryable=True,
        ) from error
    _require_recovery_path_absent(owned.path)



def _create_recovery_backup(
    capture: _PriorAssetCapture,
    *,
    backup_path: Path,
    owned_paths: list[_OwnedRecoveryPath],
) -> _RecoveryBackupAsset:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_descriptor = -1
    backup_descriptor = -1
    backup_identity: _ActivationFileIdentity | None = None
    try:
        source_descriptor = os.open(capture.path, source_flags)
        source_observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_observed.st_mode)
            or source_observed.st_nlink != 1
            or (source_observed.st_dev, source_observed.st_ino)
            != (capture.identity.device, capture.identity.inode)
        ):
            raise OSError("source identity changed")
        backup_descriptor = _open_recovery_backup(backup_path)
        backup_observed = os.fstat(backup_descriptor)
        if not stat.S_ISREG(backup_observed.st_mode):
            raise OSError("backup is not regular")
        backup_identity = _ActivationFileIdentity(
            backup_observed.st_dev,
            backup_observed.st_ino,
        )
        owned_paths.append(
            _OwnedRecoveryPath(
                path=backup_path,
                identity=backup_identity,
            )
        )
        _write_recovery_backup(source_descriptor, backup_descriptor)
        _fsync_recovery_backup(backup_descriptor)
        os.close(source_descriptor)
        source_descriptor = -1
        os.close(backup_descriptor)
        backup_descriptor = -1
        backup_capture = _capture_activation_file(
            backup_path,
            asset_kind=capture.asset_kind,
        )
        if backup_capture.identity != backup_identity:
            raise OSError("backup identity changed")
        current = _capture_activation_file(
            capture.path,
            asset_kind=capture.asset_kind,
        )
        if current.identity != capture.identity or current.digest != capture.digest:
            raise ActivationPreparationError(
                "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                retryable=False,
            )
        evidence = ActivationBackupEvidence(
            asset_kind=capture.asset_kind,
            original_digest=capture.digest,
            backup_digest=backup_capture.digest,
            original_identity=(capture.identity.device, capture.identity.inode),
            backup_identity=(
                backup_capture.identity.device,
                backup_capture.identity.inode,
            ),
        )
        return _RecoveryBackupAsset(
            asset_kind=capture.asset_kind,
            original_path=capture.path,
            backup_path=backup_path,
            original_identity=capture.identity,
            backup_identity=backup_capture.identity,
            evidence=evidence,
        )
    finally:
        if source_descriptor >= 0:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if backup_descriptor >= 0:
            try:
                os.close(backup_descriptor)
            except OSError:
                pass



def _create_recovery_backups(
    captures: tuple[_PriorAssetCapture, ...],
    *,
    preparation_id: str,
    owned_paths: list[_OwnedRecoveryPath],
    manifest_absent: bool = False,
) -> tuple[_RecoveryBackupAsset, ...]:
    backup_captures = tuple(
        capture
        for capture in captures
        if capture.asset_kind in {"DATABASE", "MANIFEST"}
    )
    expected_kinds = (
        ("DATABASE",) if manifest_absent else ("DATABASE", "MANIFEST")
    )
    if tuple(capture.asset_kind for capture in backup_captures) != (
        expected_kinds
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_SET_INCOMPLETE",
            retryable=False,
        )
    suffix = preparation_id.removeprefix("preparation.")
    created: list[_RecoveryBackupAsset] = []
    for capture in backup_captures:
        label = capture.asset_kind.lower()
        backup_path = capture.path.with_name(
            f".{capture.path.name}.localcat-recovery.{suffix}.{label}.bak"
        )
        created.append(
            _create_recovery_backup(
                capture,
                backup_path=backup_path,
                owned_paths=owned_paths,
            )
        )
    parents = {asset.backup_path.parent for asset in created}
    if len(parents) != 1:
        raise OSError("recovery backups are not adjacent")
    _fsync_recovery_directory(next(iter(parents)))
    return tuple(created)



def _revalidate_prior_assets(captures: tuple[_PriorAssetCapture, ...]) -> None:
    for capture in captures:
        current = _capture_activation_file(
            capture.path,
            asset_kind=capture.asset_kind,
        )
        if current.identity != capture.identity or current.digest != capture.digest:
            raise ActivationPreparationError(
                "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                retryable=False,
            )



def _remove_recovery_backups(
    owned_paths: tuple[_OwnedRecoveryPath, ...],
) -> None:
    parents = {owned.path.parent for owned in owned_paths}
    for owned in owned_paths:
        _remove_recovery_path(owned)
    for parent in parents:
        try:
            _fsync_recovery_deletion_directory(parent)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.CLEANUP_FAILED",
                retryable=True,
            ) from error
    for owned in owned_paths:
        _require_recovery_path_absent(owned.path)



def _replace_activation_file(source: Path, destination: Path) -> None:
    """Narrow fault-injection seam for one same-directory atomic replace."""

    os.replace(source, destination)



def _fsync_activation_file(
    path: Path,
    expected_identity: _ActivationFileIdentity,
) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (expected_identity.device, expected_identity.inode)
        ):
            raise OSError("activation file identity changed")
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)



_ROLLBACK_ELIGIBLE_ERROR_CODES = frozenset(
    {
        "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
        "ACTIVATION.RECOVERY_COMPLETION_INVALID",
        "ACTIVATION.RECOVERY_MISMATCH",
        "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
    }
)

def _activation_rollback_eligible(
    error: ActivationPreparationError,
) -> bool:
    """True when a proven journal's new-asset/effect closure fails.

    These errors mean the durable journal authenticates but the new
    DB/receipt/binding/manifest/effect cannot be re-proven from disk, so
    Task 5.9 must restore the prior authority or quarantine the failed
    first activation.  Authority-level failures (tampered journal/terminal,
    source mutation, missing or mutated backups, cleanup and durability
    faults) are never eligible and keep the fail-stop semantics.
    """

    return error.code in _ROLLBACK_ELIGIBLE_ERROR_CODES



def _activation_quarantine_directory(
    identity: CanonicalResourceIdentity,
    record: _ActivationJournalRecord,
) -> Path:
    """Deterministic adjacent quarantine directory for one failed activation.

    The directory name is derived only from the durable journal facts
    (journal id), so a fresh coordinator re-derives the exact same path and
    repeated rollback never duplicates quarantine entries.
    """

    root = identity.canonical_sidecar_path.parent / (
        ".localcat-activation-quarantine-v1"
    )
    return root / record.journal_id



def _require_quarantine_directory(quarantine_dir: Path) -> None:
    """Create (or validate) the deterministic quarantine directory durably.

    The directory is created level by level with strict lstat validation:
    an existing entry must be a real directory, never a symlink, and each
    parent directory is fsynced so a crash after quarantine leaves durable
    evidence.  A foreign entry at the deterministic path fails closed.
    """

    root = quarantine_dir.parent
    for entry in (root, quarantine_dir):
        try:
            os.mkdir(entry)
        except FileExistsError:
            pass
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        try:
            observed = os.lstat(entry)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=False,
            )
        try:
            _fsync_activation_directory(entry.parent)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error



def _quarantine_owned_activation_artifact(
    path: Path,
    expected_identity: tuple[int, int],
    quarantine_dir: Path,
    *,
    authority_path: bool,
    allow_identity: tuple[int, int] | None = None,
    allow_digest: str | None = None,
) -> bool:
    """Move one journal-owned failed artifact into quarantine; False if absent.

    The artifact must be an exact regular single-link file with the
    journal-recorded identity; a foreign, symlinked, or hardlinked entry is
    never moved or deleted.  On an authority path (canonical sidecar or
    manifest final) a foreign entry fails closed because it would poison the
    restored pair; on a stage path it is left untouched.  A journal-proven
    prior artifact at an authority path (``allow_identity``, the original
    prior inode, or ``allow_digest``, a byte-identical prior copy restored
    from the journal-owned backups) is the prior pair of a pending
    pre-publication phase and is left untouched: the prior pair is what
    Task 5.9 restores, never a failed artifact.  The move is an atomic
    same-directory rename followed by directory fsync, and an existing
    quarantine entry is never overwritten (an already-quarantined artifact
    is skipped idempotently).
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        if authority_path:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FOREIGN",
                retryable=False,
            )
        return False
    observed_identity = (observed.st_dev, observed.st_ino)
    if observed_identity != expected_identity:
        if (
            allow_identity is not None
            and observed_identity == allow_identity
        ):
            return False
        if allow_digest is not None and authority_path:
            try:
                capture = _capture_activation_file(
                    path,
                    asset_kind="JOURNAL_CLOSURE",
                )
            except ActivationPreparationError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                    reason_code=error.code,
                ) from error
            if capture.digest == allow_digest:
                return False
        if authority_path:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FOREIGN",
                retryable=False,
            )
        return False
    target = quarantine_dir / path.name
    try:
        target_observed = os.lstat(target)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    else:
        if (
            not stat.S_ISREG(target_observed.st_mode)
            or (target_observed.st_dev, target_observed.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FOREIGN",
                retryable=False,
            )
        return False
    try:
        os.rename(path, target)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    try:
        _fsync_activation_directory(quarantine_dir)
        _fsync_activation_directory(path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=False,
        )
    try:
        final = os.lstat(target)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    if (final.st_dev, final.st_ino) != (observed.st_dev, observed.st_ino):
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=False,
        )
    return True



def _quarantine_failed_activation_artifacts(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Quarantine every journal-owned failed asset of one activation.

    The set covers the deterministic canonical sidecar, the published
    manifest final, the unpublished manifest temporary, and the candidate
    stage database.  Each entry is identity/digest-bound, moved with strict
    exclusivity, and never overwrites a foreign entry; authority paths fail
    closed on foreign entries while stage paths leave them untouched.
    """

    quarantine_dir = _activation_quarantine_directory(identity, record)
    _require_quarantine_directory(quarantine_dir)
    prior_db_identity = (
        record.prior_db_identity
        if record.had_prior_canonical
        and record.prior_db_path == identity.canonical_sidecar_path
        else None
    )
    prior_manifest_identity = (
        record.prior_manifest_identity
        if record.had_prior_canonical
        and record.prior_manifest_path == identity.snapshot_manifest_path
        else None
    )
    prior_db_digest = (
        record.prior_db_digest
        if record.had_prior_canonical
        and record.prior_db_path == identity.canonical_sidecar_path
        else None
    )
    prior_manifest_digest = (
        record.prior_manifest_digest
        if record.had_prior_canonical
        and record.prior_manifest_path == identity.snapshot_manifest_path
        else None
    )
    for path, expected_identity, authority, allow_identity, allow_digest in (
        (
            identity.canonical_sidecar_path,
            record.candidate_stage_db_identity,
            True,
            prior_db_identity,
            prior_db_digest,
        ),
        (
            identity.snapshot_manifest_path,
            record.candidate_manifest_temp_identity,
            True,
            prior_manifest_identity,
            prior_manifest_digest,
        ),
        (
            record.candidate_manifest_temp_path,
            record.candidate_manifest_temp_identity,
            False,
            None,
            None,
        ),
        (
            record.candidate_stage_db_path,
            record.candidate_stage_db_identity,
            False,
            None,
            None,
        ),
    ):
        _quarantine_owned_activation_artifact(
            path,
            expected_identity,
            quarantine_dir,
            authority_path=authority,
            allow_identity=allow_identity,
            allow_digest=allow_digest,
        )



def _require_cancelled_candidate_quarantine_closure(
    quarantine_dir: Path,
    expected_identity: tuple[int, int],
) -> None:
    """Prove one cancelled candidate inode already sits in quarantine.

    Absence at the journal-recorded stage name is accepted only when the
    exact recorded inode is found as a regular single-link entry inside
    the deterministic quarantine directory (under the candidate or the
    canonical basename, depending on cancellation/rollback).  Every
    quarantine entry must be a regular single-link file and the scan is
    bounded to this one deterministic directory, never a filesystem-wide
    search.  A missing inode means the candidate was externally deleted
    or moved, which fails closed: absence is never accepted merely
    because an authority path is absent or holds a different file.
    """

    try:
        names = os.listdir(quarantine_dir)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.QUARANTINE_FAILED",
            retryable=True,
        ) from error
    for name in names:
        try:
            observed = os.lstat(quarantine_dir / name)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FOREIGN",
                retryable=False,
            )
        if (observed.st_dev, observed.st_ino) == expected_identity:
            return
    raise ActivationPreparationError(
        "ACTIVATION.QUARANTINE_MISSING",
        retryable=False,
    )


def _quarantine_candidate_rename(source: Path, target: Path) -> None:
    """Narrow fault-injection seam for one cancelled-candidate rename."""

    os.rename(source, target)



def _retire_cancelled_candidate_assets(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Quarantine the journal-proven candidate DB/manifest pair of a cancel.

    A PREPARED cancellation retires the exact candidate stage database and
    temporary manifest after the CANCELLED terminal is durable and the main
    journal is retired, so a later deterministic migration retry rebuilds a
    fresh stage instead of failing with ``MIGRATION.STAGE_SEALED``.  Each
    file must still be the exact journal-owned regular single-link file
    (identity-bound); a mutated, symlinked, hardlinked, or foreign entry
    fails closed and is never removed.  Absence is accepted only when the
    deterministic quarantine closure proves it: the exact same inode must
    already sit at the deterministic quarantine target, proving an earlier
    interrupted retirement.  The move is an atomic same-directory rename
    followed by directory fsyncs, and the quarantine target is never
    overwritten, so every crash window resumes idempotently from the
    terminal authority.
    """

    quarantine_dir = _activation_quarantine_directory(identity, record)
    _require_quarantine_directory(quarantine_dir)
    for path, expected_identity in (
        (
            record.candidate_stage_db_path,
            record.candidate_stage_db_identity,
        ),
        (
            record.candidate_manifest_temp_path,
            record.candidate_manifest_temp_identity,
        ),
    ):
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            _require_cancelled_candidate_quarantine_closure(
                quarantine_dir,
                expected_identity,
            )
            continue
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (expected_identity[0], expected_identity[1])
        ):
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FOREIGN",
                retryable=False,
            )
        target = quarantine_dir / path.name
        try:
            target_observed = os.lstat(target)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        else:
            if (
                not stat.S_ISREG(target_observed.st_mode)
                or (target_observed.st_dev, target_observed.st_ino)
                != (observed.st_dev, observed.st_ino)
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                )
            continue
        try:
            _quarantine_candidate_rename(path, target)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        try:
            _fsync_activation_directory(quarantine_dir)
            _fsync_activation_directory(path.parent)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=False,
            )
        try:
            final = os.lstat(target)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=True,
            ) from error
        if (final.st_dev, final.st_ino) != (observed.st_dev, observed.st_ino):
            raise ActivationPreparationError(
                "ACTIVATION.QUARANTINE_FAILED",
                retryable=False,
            )


def _remove_orphaned_rollback_temp(path: Path) -> None:
    """Strictly remove one deterministic rollback temporary after a crash.

    Only an exact regular single-link file at the deterministic temporary
    path (derived from the journal id) is removable; a foreign, linked, or
    unprovable entry fails closed and is never removed.
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ROLLBACK_RESTORE_FAILED",
            retryable=True,
        ) from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.ROLLBACK_RESTORE_FAILED",
            retryable=False,
        )
    identity = _ActivationFileIdentity(observed.st_dev, observed.st_ino)
    if not _remove_owned_activation_journal_temp(path, identity):
        raise ActivationPreparationError(
            "ACTIVATION.ROLLBACK_RESTORE_FAILED",
            retryable=True,
        )



def _replay_activation_journal(
    preparation: _ActivationPreparation,
    record: _ActivationJournalRecord,
    journal_path: Path,
    existing_identity: _ActivationFileIdentity,
) -> _ActivationJournalHandle:
    """Replay only an exact durable PREPARED journal for the same closure."""

    try:
        disk_bytes, disk_identity = _read_activation_journal_file(
            journal_path,
            existing_identity,
        )
        disk_record = _parse_activation_journal_bytes(
            disk_bytes,
            expected_journal_path=journal_path,
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
            retryable=False,
            reason_code=error.code,
        ) from error
    if (
        disk_record != record
        or disk_record.phase is not _ActivationJournalPhase.PREPARED
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
            retryable=False,
        )
    try:
        _fsync_activation_directory(journal_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            retryable=False,
        ) from error
    try:
        fsynced_bytes, fsynced_identity = _read_activation_journal_file(
            journal_path,
            disk_identity,
        )
        fsynced_record = _parse_activation_journal_bytes(
            fsynced_bytes,
            expected_journal_path=journal_path,
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            retryable=False,
            reason_code=error.code,
        ) from error
    if (
        fsynced_bytes != disk_bytes
        or fsynced_identity != disk_identity
        or fsynced_record != disk_record
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            retryable=False,
        )
    return _ActivationJournalHandle(
        journal_id=record.journal_id,
        journal_path=journal_path,
        file_identity=fsynced_identity,
        phase=record.phase,
        record_digest=_activation_journal_digest(record),
        preparation_id=record.preparation_id,
        _record=record,
        _factory_key=_ACTIVATION_JOURNAL_FACTORY_KEY,
    )

def _require_legacy_content_attestations_for_durable_write(
    record: _ActivationJournalRecord,
) -> None:
    """Keep the durable journal writer on its approved v2 byte contract.

    Portable owner-memory values cannot enter this v2 durable schema until
    their live authority route and a portable journal schema are approved, so
    reject them before creating or replacing any journal/terminal file.
    """

    sealed = record.sealed_content_attestation
    if type(sealed) is PortableSealedContentAttestation:
        raise ActivationPreparationError(
            "ACTIVATION.ATTESTATION_UNAVAILABLE",
            retryable=False,
        )
    if type(sealed) is not SealedContentAttestation:
        raise TypeError("sealed content attestation must be exact legacy v2")
    active = record.active_content_attestation
    if type(active) is PortableActiveContentAttestation:
        raise ActivationPreparationError(
            "ACTIVATION.ATTESTATION_UNAVAILABLE",
            retryable=False,
        )
    if active is not None and type(active) is not ActiveContentAttestation:
        raise TypeError("active content attestation must be exact legacy v2")


def _write_activation_journal(
    record: _ActivationJournalRecord,
    journal_path: Path,
    *,
    expected_final_identity: _ActivationFileIdentity | None,
) -> _ActivationJournalHandle:
    """Publish one journal record with strict exclusive temp + fsync order.

    ``expected_final_identity is None`` requires the final path to be
    absent (first publication); otherwise the final must still be exactly
    the given file (phase advance).  Failures before final publication
    remove only the owned temporary with strict identity and directory
    fsync; failures after publication fail-stop with a code-only error.
    """

    _require_legacy_content_attestations_for_durable_write(record)
    expected_bytes = _serialize_activation_journal_record(
        record
    ).encode("utf-8")
    temp_path = _activation_journal_temp_path(journal_path)
    descriptor = -1
    temp_identity: _ActivationFileIdentity | None = None
    published = False
    try:
        if _lstat_any_entry(temp_path):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_TEMP_EXISTS",
                retryable=False,
            )
        try:
            descriptor, temp_identity = _open_activation_journal_temp(
                temp_path
            )
        except OSError as error:
            if _lstat_any_entry(temp_path):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_TEMP_EXISTS",
                    retryable=False,
                ) from error
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_WRITE_FAILED",
                retryable=True,
            ) from error
        assert temp_identity is not None
        _write_activation_journal_bytes(descriptor, expected_bytes)
        _fsync_activation_journal(descriptor)
        _close_activation_journal(descriptor)
        descriptor = -1
        temp_bytes, _temp_observed = _read_activation_journal_file(
            temp_path,
            temp_identity,
        )
        if temp_bytes != expected_bytes:
            raise OSError("activation journal temporary content mismatch")
        if expected_final_identity is None:
            if (
                _lstat_activation_journal_identity(journal_path)
                is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_FINAL_EXISTS",
                    retryable=False,
                )
        else:
            observed_final = _lstat_activation_journal_identity(
                journal_path
            )
            if observed_final != expected_final_identity:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_HANDLE_STALE",
                    retryable=False,
                )
        os.replace(temp_path, journal_path)
        published = True
        final_identity = _lstat_activation_journal_identity(journal_path)
        if final_identity != temp_identity:
            raise OSError(
                "activation journal final identity changed after publish"
            )
        _fsync_activation_directory(journal_path.parent)
    except ActivationPreparationError:
        raise
    except FileExistsError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TEMP_EXISTS",
            retryable=False,
        ) from error
    except OSError as error:
        if published:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
                retryable=False,
            ) from error
        cleaned = (
            temp_identity is not None
            and _remove_owned_activation_journal_temp(
                temp_path,
                temp_identity,
            )
        )
        if not cleaned:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLEANUP_FAILED",
                retryable=True,
            ) from error
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_WRITE_FAILED",
            retryable=True,
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        final_bytes, final_observed = _read_activation_journal_file(
            journal_path,
            final_identity,
        )
        if final_bytes != expected_bytes:
            raise OSError("activation journal final content mismatch")
        final_record = _parse_activation_journal_bytes(
            final_bytes,
            expected_journal_path=journal_path,
        )
    except (ActivationPreparationError, OSError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            retryable=False,
        ) from error
    if final_record != record:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            retryable=False,
        )
    return _ActivationJournalHandle(
        journal_id=record.journal_id,
        journal_path=journal_path,
        file_identity=final_observed,
        phase=record.phase,
        record_digest=_activation_journal_digest(record),
        preparation_id=record.preparation_id,
        _record=record,
        _factory_key=_ACTIVATION_JOURNAL_FACTORY_KEY,
    )

def _write_activation_terminal(
    identity: CanonicalResourceIdentity,
    record: _ActivationJournalRecord,
) -> _ActivationFileIdentity:
    """Publish one terminal record with strict exclusive temp + fsync.

    The terminal file mirrors the full authenticated closure of ``record``
    (``PREPARED`` = CANCELLED/prior authority, ``GENERATION_PUBLISHED`` =
    CONSUMED/new canonical authority) at the deterministic terminal path.
    The final path must be absent; a foreign terminal is never
    overwritten.  Failures before publication remove only the owned
    temporary with strict identity and directory fsync; failures after
    publication fail-stop with the durable terminal in place.
    """

    _require_legacy_content_attestations_for_durable_write(record)
    terminal_path = _activation_terminal_path(identity)
    expected_bytes = _serialize_activation_journal_record(
        record
    ).encode("utf-8")
    temp_path = _activation_terminal_temp_path(terminal_path)
    descriptor = -1
    temp_identity: _ActivationFileIdentity | None = None
    published = False
    try:
        if _lstat_any_entry(temp_path):
            raise ActivationPreparationError(
                "ACTIVATION.TERMINAL_TEMP_EXISTS",
                retryable=False,
            )
        try:
            descriptor, temp_identity = _open_activation_journal_temp(
                temp_path
            )
        except OSError as error:
            if _lstat_any_entry(temp_path):
                raise ActivationPreparationError(
                    "ACTIVATION.TERMINAL_TEMP_EXISTS",
                    retryable=False,
                ) from error
            raise ActivationPreparationError(
                "ACTIVATION.TERMINAL_WRITE_FAILED",
                retryable=True,
            ) from error
        assert temp_identity is not None
        _write_activation_journal_bytes(descriptor, expected_bytes)
        _fsync_activation_journal(descriptor)
        _close_activation_journal(descriptor)
        descriptor = -1
        temp_bytes, _temp_observed = _read_activation_journal_file(
            temp_path,
            temp_identity,
        )
        if temp_bytes != expected_bytes:
            raise OSError("activation terminal temporary content mismatch")
        if _lstat_activation_terminal_identity(terminal_path) is not None:
            raise ActivationPreparationError(
                "ACTIVATION.TERMINAL_FINAL_EXISTS",
                retryable=False,
            )
        os.replace(temp_path, terminal_path)
        published = True
        final_identity = _lstat_activation_terminal_identity(
            terminal_path
        )
        if final_identity != temp_identity:
            raise OSError(
                "activation terminal identity changed after publish"
            )
        _fsync_activation_directory(terminal_path.parent)
    except ActivationPreparationError:
        raise
    except FileExistsError as error:
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_TEMP_EXISTS",
            retryable=False,
        ) from error
    except OSError as error:
        if published:
            raise ActivationPreparationError(
                "ACTIVATION.TERMINAL_DURABILITY_UNPROVEN",
                retryable=False,
            ) from error
        cleaned = (
            temp_identity is not None
            and _remove_owned_activation_journal_temp(
                temp_path,
                temp_identity,
            )
        )
        if not cleaned:
            raise ActivationPreparationError(
                "ACTIVATION.TERMINAL_CLEANUP_FAILED",
                retryable=True,
            ) from error
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_WRITE_FAILED",
            retryable=True,
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        final_bytes, final_observed = _read_activation_journal_file(
            terminal_path,
            final_identity,
        )
        if final_bytes != expected_bytes:
            raise OSError("activation terminal final content mismatch")
        final_record = _parse_activation_journal_bytes(
            final_bytes,
            expected_journal_path=record.journal_path,
        )
    except (ActivationPreparationError, OSError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_DURABILITY_UNPROVEN",
            retryable=False,
        ) from error
    if final_record != record:
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_DURABILITY_UNPROVEN",
            retryable=False,
        )
    return final_observed

def _activation_journal_record_payload(
    record: _ActivationJournalRecord,
) -> dict[str, object]:
    def identity_pair(
        value: tuple[int, int] | None,
    ) -> list[int] | None:
        if value is None:
            return None
        return [value[0], value[1]]

    def optional_str(value: Path | None) -> str | None:
        return None if value is None else str(value)

    return {
        "active_content_attestation": (
            None
            if record.active_content_attestation is None
            else _active_content_attestation_to_mapping(
                record.active_content_attestation
            )
        ),
        "activation_nonce": record.activation_nonce,
        "artifact_id": record.artifact_id,
        "artifact_seal_digest": record.artifact_seal_digest,
        "candidate_manifest_temp_identity": identity_pair(
            record.candidate_manifest_temp_identity
        ),
        "candidate_manifest_temp_path": str(
            record.candidate_manifest_temp_path
        ),
        "candidate_stage_db_identity": identity_pair(
            record.candidate_stage_db_identity
        ),
        "candidate_stage_db_path": str(record.candidate_stage_db_path),
        "canonical_store_id": record.canonical_store_id,
        "prior_canonical_store_id": record.prior_canonical_store_id,
        "evidence_digest": record.evidence_digest,
        "expected_prior_generation": record.expected_prior_generation,
        "gate_b_grant_digest": record.gate_b_grant_digest,
        "had_prior_canonical": record.had_prior_canonical,
        "journal_id": record.journal_id,
        "prior_manifest_absent": record.prior_manifest_absent,
        "journal_path": str(record.journal_path),
        "journal_version": record.journal_version,
        "manifest_temp_digest": record.manifest_temp_digest,
        "new_manifest_digest": record.new_manifest_digest,
        "new_manifest_path": str(record.new_manifest_path),
        "new_receipt_id": record.new_receipt_id,
        "phase": record.phase.value,
        "preparation_id": record.preparation_id,
        "prior_binding_snapshot_id": record.prior_binding_snapshot_id,
        "prior_db_backup_digest": record.prior_db_backup_digest,
        "prior_db_backup_identity": identity_pair(
            record.prior_db_backup_identity
        ),
        "prior_db_backup_path": optional_str(record.prior_db_backup_path),
        "prior_db_digest": record.prior_db_digest,
        "prior_db_identity": identity_pair(record.prior_db_identity),
        "prior_db_path": optional_str(record.prior_db_path),
        "prior_generation": record.prior_generation,
        "prior_manifest_backup_digest": record.prior_manifest_backup_digest,
        "prior_manifest_backup_identity": identity_pair(
            record.prior_manifest_backup_identity
        ),
        "prior_manifest_backup_path": optional_str(
            record.prior_manifest_backup_path
        ),
        "prior_manifest_digest": record.prior_manifest_digest,
        "prior_manifest_identity": identity_pair(
            record.prior_manifest_identity
        ),
        "prior_manifest_path": optional_str(record.prior_manifest_path),
        "prior_receipt_digest": record.prior_receipt_digest,
        "registry_namespace": record.registry_namespace,
        "resource_id": record.resource_id,
        "sealed_content_attestation": (
            _sealed_content_attestation_to_mapping(
                record.sealed_content_attestation
            )
        ),
        "sealed_stage_digest": record.sealed_stage_digest,
        "snapshot_receipt_digest": record.snapshot_receipt_digest,
        "source_jsonl_digest": record.source_jsonl_digest,
        "source_jsonl_identity": identity_pair(record.source_jsonl_identity),
        "stage_db_digest": record.stage_db_digest,
        "target_identity": record.target_identity,
        "token_id": record.token_id,
        "token_version": record.token_version,
    }



def _activation_journal_digest(record: _ActivationJournalRecord) -> str:
    return contract_module._stable_digest(
        _activation_journal_record_payload(record)
    )



def _serialize_activation_journal_record(
    record: _ActivationJournalRecord,
) -> str:
    envelope = _activation_journal_record_payload(record)
    envelope["record_digest"] = _activation_journal_digest(record)
    return json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )



def _decode_journal_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value



def _decode_journal_digest(value: object, field_name: str) -> str:
    decoded = _decode_journal_string(value, field_name)
    _require_activation_journal_digest(decoded, field_name)
    return decoded



def _decode_journal_int(value: object, field_name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    return value



def _decode_journal_optional_int(
    value: object,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    decoded = _decode_journal_int(value, field_name)
    if decoded < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return decoded



def _decode_journal_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")
    return value



def _decode_journal_path(value: object, field_name: str) -> Path:
    decoded = _decode_journal_string(value, field_name)
    path = Path(decoded)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be an absolute normalized path")
    return path



def _decode_journal_optional_path(
    value: object,
    field_name: str,
) -> Path | None:
    if value is None:
        return None
    return _decode_journal_path(value, field_name)



def _decode_journal_identity_pair(
    value: object,
    field_name: str,
) -> tuple[int, int]:
    if type(value) is not list or len(value) != 2:
        raise TypeError(f"{field_name} must be a two-item array")
    first = _decode_journal_int(value[0], f"{field_name} device")
    second = _decode_journal_int(value[1], f"{field_name} inode")
    if first < 0 or second < 0:
        raise ValueError(f"{field_name} must contain non-negative integers")
    return (first, second)



def _decode_journal_optional_identity_pair(
    value: object,
    field_name: str,
) -> tuple[int, int] | None:
    if value is None:
        return None
    return _decode_journal_identity_pair(value, field_name)



def _decode_journal_phase(value: object) -> _ActivationJournalPhase:
    if type(value) is not str:
        raise TypeError("journal phase must be a string")
    for member in _PHASE_SEQUENCE:
        if member.value == value:
            return member
    raise ValueError("journal phase is not a supported activation phase")



def _decode_optional_journal_identity(
    value: object,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _decode_journal_string(value, field_name)



def _decode_optional_journal_digest(
    value: object,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _decode_journal_digest(value, field_name)



def _decode_activation_journal_record(
    mapping: dict[str, object],
    *,
    expected_journal_path: Path,
) -> _ActivationJournalRecord:
    journal_path = _decode_journal_path(
        mapping["journal_path"],
        "journal_path",
    )
    if journal_path != expected_journal_path:
        raise ValueError("journal path does not match the expected path")
    phase = _decode_journal_phase(mapping["phase"])
    record = _ActivationJournalRecord(
        journal_id=_decode_journal_string(mapping["journal_id"], "journal_id"),
        journal_version=_decode_journal_string(
            mapping["journal_version"],
            "journal_version",
        ),
        journal_path=journal_path,
        phase=phase,
        preparation_id=_decode_journal_string(
            mapping["preparation_id"],
            "preparation_id",
        ),
        registry_namespace=_decode_journal_string(
            mapping["registry_namespace"],
            "registry_namespace",
        ),
        token_id=_decode_journal_string(mapping["token_id"], "token_id"),
        token_version=_decode_journal_string(
            mapping["token_version"],
            "token_version",
        ),
        activation_nonce=_decode_journal_string(
            mapping["activation_nonce"],
            "activation_nonce",
        ),
        artifact_id=_decode_journal_string(
            mapping["artifact_id"],
            "artifact_id",
        ),
        artifact_seal_digest=_decode_journal_digest(
            mapping["artifact_seal_digest"],
            "artifact_seal_digest",
        ),
        sealed_stage_digest=_decode_journal_digest(
            mapping["sealed_stage_digest"],
            "sealed_stage_digest",
        ),
        resource_id=_decode_journal_string(
            mapping["resource_id"],
            "resource_id",
        ),
        target_identity=_decode_journal_digest(
            mapping["target_identity"],
            "target_identity",
        ),
        canonical_store_id=_decode_journal_string(
            mapping["canonical_store_id"],
            "canonical_store_id",
        ),
        prior_canonical_store_id=_decode_optional_journal_identity(
            mapping["prior_canonical_store_id"],
            "prior_canonical_store_id",
        ),
        expected_prior_generation=_decode_journal_optional_int(
            mapping["expected_prior_generation"],
            "expected_prior_generation",
        ),
        prior_generation=_decode_journal_optional_int(
            mapping["prior_generation"],
            "prior_generation",
        ),
        gate_b_grant_digest=_decode_journal_digest(
            mapping["gate_b_grant_digest"],
            "gate_b_grant_digest",
        ),
        evidence_digest=_decode_journal_digest(
            mapping["evidence_digest"],
            "evidence_digest",
        ),
        snapshot_receipt_digest=_decode_journal_digest(
            mapping["snapshot_receipt_digest"],
            "snapshot_receipt_digest",
        ),
        stage_db_digest=_decode_journal_digest(
            mapping["stage_db_digest"],
            "stage_db_digest",
        ),
        manifest_temp_digest=_decode_journal_digest(
            mapping["manifest_temp_digest"],
            "manifest_temp_digest",
        ),
        source_jsonl_digest=_decode_journal_digest(
            mapping["source_jsonl_digest"],
            "source_jsonl_digest",
        ),
        new_receipt_id=_decode_journal_string(
            mapping["new_receipt_id"],
            "new_receipt_id",
        ),
        new_manifest_path=_decode_journal_path(
            mapping["new_manifest_path"],
            "new_manifest_path",
        ),
        new_manifest_digest=_decode_journal_digest(
            mapping["new_manifest_digest"],
            "new_manifest_digest",
        ),
        candidate_stage_db_path=_decode_journal_path(
            mapping["candidate_stage_db_path"],
            "candidate_stage_db_path",
        ),
        candidate_manifest_temp_path=_decode_journal_path(
            mapping["candidate_manifest_temp_path"],
            "candidate_manifest_temp_path",
        ),
        candidate_stage_db_identity=_decode_journal_identity_pair(
            mapping["candidate_stage_db_identity"],
            "candidate_stage_db_identity",
        ),
        candidate_manifest_temp_identity=_decode_journal_identity_pair(
            mapping["candidate_manifest_temp_identity"],
            "candidate_manifest_temp_identity",
        ),
        source_jsonl_identity=_decode_journal_identity_pair(
            mapping["source_jsonl_identity"],
            "source_jsonl_identity",
        ),
        had_prior_canonical=_decode_journal_bool(
            mapping["had_prior_canonical"],
            "had_prior_canonical",
        ),
        prior_manifest_absent=_decode_journal_bool(
            mapping["prior_manifest_absent"],
            "prior_manifest_absent",
        ),
        prior_binding_snapshot_id=_decode_optional_journal_identity(
            mapping["prior_binding_snapshot_id"],
            "prior_binding_snapshot_id",
        ),
        prior_receipt_digest=_decode_optional_journal_digest(
            mapping["prior_receipt_digest"],
            "prior_receipt_digest",
        ),
        prior_manifest_digest=_decode_optional_journal_digest(
            mapping["prior_manifest_digest"],
            "prior_manifest_digest",
        ),
        prior_db_path=_decode_journal_optional_path(
            mapping["prior_db_path"],
            "prior_db_path",
        ),
        prior_manifest_path=_decode_journal_optional_path(
            mapping["prior_manifest_path"],
            "prior_manifest_path",
        ),
        prior_db_digest=_decode_optional_journal_digest(
            mapping["prior_db_digest"],
            "prior_db_digest",
        ),
        prior_db_identity=_decode_journal_optional_identity_pair(
            mapping["prior_db_identity"],
            "prior_db_identity",
        ),
        prior_manifest_identity=_decode_journal_optional_identity_pair(
            mapping["prior_manifest_identity"],
            "prior_manifest_identity",
        ),
        prior_db_backup_path=_decode_journal_optional_path(
            mapping["prior_db_backup_path"],
            "prior_db_backup_path",
        ),
        prior_manifest_backup_path=_decode_journal_optional_path(
            mapping["prior_manifest_backup_path"],
            "prior_manifest_backup_path",
        ),
        prior_db_backup_digest=_decode_optional_journal_digest(
            mapping["prior_db_backup_digest"],
            "prior_db_backup_digest",
        ),
        prior_manifest_backup_digest=_decode_optional_journal_digest(
            mapping["prior_manifest_backup_digest"],
            "prior_manifest_backup_digest",
        ),
        prior_db_backup_identity=_decode_journal_optional_identity_pair(
            mapping["prior_db_backup_identity"],
            "prior_db_backup_identity",
        ),
        prior_manifest_backup_identity=_decode_journal_optional_identity_pair(
            mapping["prior_manifest_backup_identity"],
            "prior_manifest_backup_identity",
        ),
        sealed_content_attestation=(
            _sealed_content_attestation_from_mapping(
                mapping["sealed_content_attestation"]
            )
        ),
        active_content_attestation=(
            None
            if mapping["active_content_attestation"] is None
            else _active_content_attestation_from_mapping(
                mapping["active_content_attestation"]
            )
        ),
    )
    return record



def _parse_activation_journal_bytes(
    payload: bytes,
    *,
    expected_journal_path: Path,
) -> _ActivationJournalRecord:
    """Strictly parse one durable journal file into its frozen record."""

    if type(payload) is not bytes:
        raise TypeError("activation journal payload must be bytes")
    try:
        serialized = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate journal key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            serialized,
            parse_constant=reject_non_finite,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error
    if type(value) is not dict:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        )
    mapping: dict[str, object] = value
    if set(mapping) != _ACTIVATION_JOURNAL_ENVELOPE_FIELDS:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        )
    try:
        canonical = json.dumps(
            mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error
    if canonical != serialized:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        )
    digest_field = mapping["record_digest"]
    _require_activation_journal_digest(digest_field, "record_digest")
    payload_mapping = {
        key: value
        for key, value in mapping.items()
        if key != "record_digest"
    }
    if contract_module._stable_digest(payload_mapping) != digest_field:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        )
    try:
        record = _decode_activation_journal_record(
            payload_mapping,
            expected_journal_path=expected_journal_path,
        )
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID",
            retryable=False,
        ) from error
    return record



def _validate_activation_journal_record(
    record: _ActivationJournalRecord,
) -> None:
    if record.journal_version != _ACTIVATION_JOURNAL_VERSION:
        raise ValueError("unsupported activation journal version")
    if type(record.sealed_content_attestation) is not SealedContentAttestation:
        raise TypeError("sealed content attestation is required")
    sealed = record.sealed_content_attestation
    if (
        sealed.resource_id != record.resource_id
        or sealed.target_identity != record.target_identity
        or sealed.canonical_store_id != record.canonical_store_id
        or sealed.snapshot_receipt_digest
        != record.snapshot_receipt_digest
        or sealed.expected_prior_generation
        != record.expected_prior_generation
        or sealed.evidence_digest != record.evidence_digest
        or sealed.database.sha256 != record.stage_db_digest
        or (sealed.database.device, sealed.database.inode)
        != record.candidate_stage_db_identity
        or sealed.manifest.sha256 != record.manifest_temp_digest
        or (sealed.manifest.device, sealed.manifest.inode)
        != record.candidate_manifest_temp_identity
        or sealed.source.sha256 != record.source_jsonl_digest
        or (sealed.source.device, sealed.source.inode)
        != record.source_jsonl_identity
    ):
        raise ValueError("sealed content attestation does not close journal")
    if record.phase in {
        _ActivationJournalPhase.PREPARED,
        _ActivationJournalPhase.DB_REPLACED,
    }:
        if record.active_content_attestation is not None:
            raise ValueError("active attestation precedes manifest publication")
    else:
        active = record.active_content_attestation
        if type(active) is not ActiveContentAttestation:
            raise TypeError("published phase requires active attestation")
        expected_generation = (
            0
            if record.expected_prior_generation is None
            else record.expected_prior_generation + 1
        )
        expected_activation_digest = hashlib.sha256(
            json.dumps(
                {
                    "activation_nonce": record.activation_nonce,
                    "artifact_id": record.artifact_id,
                    "evidence_digest": record.evidence_digest,
                    "generation": expected_generation,
                    "journal_id": record.journal_id,
                    "manifest_digest": record.new_manifest_digest,
                    "sealed_stage_digest": record.sealed_stage_digest,
                    "token_id": record.token_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        active_semantic = active.semantic_facts
        sealed_semantic = sealed.semantic_facts
        if (
            active.sealed_attestation_digest
            != sealed.attestation_digest
            or active.journal_id != record.journal_id
            or active.resource_id != record.resource_id
            or active.target_identity != record.target_identity
            or active.canonical_store_id != record.canonical_store_id
            or active.snapshot_receipt_digest
            != record.snapshot_receipt_digest
            or active.manifest != sealed.manifest
            or active.source != sealed.source
            or active.database.device != sealed.database.device
            or active.database.inode != sealed.database.inode
            or active.generation != expected_generation
            or active.activation_digest != expected_activation_digest
            or active_semantic.schema_version
            != sealed_semantic.schema_version
            or active_semantic.schema_digest != sealed_semantic.schema_digest
            or active_semantic.fold_version != sealed_semantic.fold_version
            or active_semantic.index_version != sealed_semantic.index_version
            or active_semantic.candidate_index_kind
            != sealed_semantic.candidate_index_kind
            or active_semantic.fts5_available
            != sealed_semantic.fts5_available
            or active_semantic.sqlite_runtime_version
            != sealed_semantic.sqlite_runtime_version
            or active_semantic.unicode_runtime_version
            != sealed_semantic.unicode_runtime_version
            or active_semantic.journal_mode != sealed_semantic.journal_mode
            or active_semantic.synchronous != sealed_semantic.synchronous
            or active_semantic.foreign_keys != sealed_semantic.foreign_keys
            or active_semantic.busy_timeout_ms
            != sealed_semantic.busy_timeout_ms
            or active_semantic.wal_enabled != sealed_semantic.wal_enabled
            or active_semantic.extension_loading_enabled
            != sealed_semantic.extension_loading_enabled
            or active_semantic.record_count != sealed_semantic.record_count
            or active_semantic.receipt_boundary_record_count
            != sealed_semantic.receipt_boundary_record_count
            or active_semantic.origin_batch_count
            != sealed_semantic.origin_batch_count
            or active_semantic.origin_batch_id
            != sealed_semantic.origin_batch_id
            or active_semantic.origin_batch_kind
            != sealed_semantic.origin_batch_kind
            or active_semantic.exported_revision
            != sealed_semantic.exported_revision
            or active_semantic.fts_count != sealed_semantic.fts_count
            or active_semantic.receipt_boundary_fts_count
            != sealed_semantic.receipt_boundary_fts_count
            or active_semantic.gram_counts != sealed_semantic.gram_counts
            or active_semantic.exact_parity_digest
            != sealed_semantic.exact_parity_digest
        ):
            raise ValueError("active content attestation does not close journal")
    for field_name in _ACTIVATION_JOURNAL_IDENTITY_FIELDS:
        value = getattr(record, field_name)
        if type(value) is not str or not value.strip():
            raise TypeError(f"{field_name} must be a non-empty string")
    for field_name in _ACTIVATION_JOURNAL_DIGEST_FIELDS:
        _require_activation_journal_digest(
            getattr(record, field_name),
            field_name,
        )
    for field_name in _ACTIVATION_JOURNAL_OPTIONAL_DIGEST_FIELDS:
        value = getattr(record, field_name)
        if value is not None:
            _require_activation_journal_digest(value, field_name)
    for field_name in _ACTIVATION_JOURNAL_PATH_FIELDS:
        _validate_journal_native_path(
            getattr(record, field_name),
            field_name,
        )
    for field_name in _ACTIVATION_JOURNAL_OPTIONAL_PATH_FIELDS:
        value = getattr(record, field_name)
        if value is not None:
            _validate_journal_native_path(value, field_name)
    for field_name in _ACTIVATION_JOURNAL_IDENTITY_PAIR_FIELDS:
        _validate_journal_native_identity_pair(
            getattr(record, field_name),
            field_name,
        )
    for field_name in _ACTIVATION_JOURNAL_OPTIONAL_IDENTITY_PAIR_FIELDS:
        value = getattr(record, field_name)
        if value is not None:
            _validate_journal_native_identity_pair(value, field_name)
    if record.prior_canonical_store_id is not None and (
        type(record.prior_canonical_store_id) is not str
        or not record.prior_canonical_store_id.strip()
    ):
        raise TypeError(
            "prior canonical store id must be a non-empty string or None"
        )
    if (
        record.prior_canonical_store_id is not None
        and record.prior_canonical_store_id == record.canonical_store_id
    ):
        raise ValueError(
            "explicit replacement must use a different canonical store id"
        )
    if record.expected_prior_generation is not None and (
        record.expected_prior_generation < 0
    ):
        raise ValueError("expected prior generation must be non-negative")
    if record.prior_generation is not None and record.prior_generation < 0:
        raise ValueError("prior generation must be non-negative")
    if type(record.phase) is not _ActivationJournalPhase:
        raise TypeError("journal phase must be a code-only activation phase")
    if record.phase not in _PHASE_SEQUENCE:
        raise ValueError("journal phase is not in the fixed sequence")
    if record.new_manifest_digest != record.manifest_temp_digest:
        raise ValueError(
            "new final manifest digest must match the temporary manifest digest"
        )
    if record.had_prior_canonical:
        if (
            record.prior_generation is None
            or record.expected_prior_generation != record.prior_generation
            or record.prior_binding_snapshot_id is None
            or record.prior_receipt_digest is None
            or record.prior_db_path is None
            or record.prior_db_digest is None
            or record.prior_db_identity is None
            or record.prior_db_backup_path is None
            or record.prior_db_backup_digest is None
            or record.prior_db_backup_identity is None
        ):
            raise ValueError(
                "activation journal prior facts are incomplete"
            )
        if record.prior_db_digest != record.prior_db_backup_digest:
            raise ValueError(
                "activation journal prior database digest does not close"
            )
        if record.prior_manifest_absent:
            if (
                record.prior_manifest_path is not None
                or record.prior_manifest_digest is not None
                or record.prior_manifest_identity is not None
                or record.prior_manifest_backup_path is not None
                or record.prior_manifest_backup_digest is not None
                or record.prior_manifest_backup_identity is not None
            ):
                raise ValueError(
                    "activation journal absent prior manifest must not "
                    "carry manifest facts"
                )
        else:
            if (
                record.prior_manifest_path is None
                or record.prior_manifest_digest is None
                or record.prior_manifest_identity is None
                or record.prior_manifest_backup_path is None
                or record.prior_manifest_backup_digest is None
                or record.prior_manifest_backup_identity is None
            ):
                raise ValueError(
                    "activation journal prior facts are incomplete"
                )
            if (
                record.prior_manifest_digest
                != record.prior_manifest_backup_digest
            ):
                raise ValueError(
                    "activation journal prior manifest digest does not close"
                )
    else:
        if record.prior_manifest_absent:
            raise ValueError(
                "first activation journal must explicitly encode "
                "manifest absence as false"
            )
        if (
            record.prior_canonical_store_id is not None
            or record.prior_generation is not None
            or record.expected_prior_generation is not None
            or record.prior_binding_snapshot_id is not None
            or record.prior_receipt_digest is not None
            or record.prior_manifest_digest is not None
            or record.prior_db_path is not None
            or record.prior_manifest_path is not None
            or record.prior_db_digest is not None
            or record.prior_db_identity is not None
            or record.prior_manifest_identity is not None
            or record.prior_db_backup_path is not None
            or record.prior_manifest_backup_path is not None
            or record.prior_db_backup_digest is not None
            or record.prior_manifest_backup_digest is not None
            or record.prior_db_backup_identity is not None
            or record.prior_manifest_backup_identity is not None
        ):
            raise ValueError(
                "first activation journal must explicitly encode absence"
            )


# The portable codec is deliberately separate from the historical v2 codec
# above. None of the v2 field sets, serializers, parsers, or golden bytes are
# shared with or widened by this schema.
_PORTABLE_ACTIVATION_JOURNAL_VERSION = "activation-journal-v3"
_PORTABLE_ACTIVATION_JOURNAL_PHASE = "PREPARED"
_PORTABLE_ACTIVATION_JOURNAL_CLOSURES = frozenset({"PENDING", "CANCELLED"})
_PORTABLE_ACTIVATION_JOURNAL_MAX_BYTES = 256 * 1024


def _portable_activation_canonical_json(mapping: dict[str, object]) -> bytes:
    return json.dumps(
        mapping,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_portable_activation_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{field_name} must be a non-empty built-in string")
    return value


def _require_portable_activation_digest(value: object, field_name: str) -> str:
    result = _require_portable_activation_string(value, field_name)
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return result


def _require_portable_activation_basename(value: object, field_name: str) -> str:
    result = _require_portable_activation_string(value, field_name)
    if (
        result in {".", ".."}
        or "/" in result
        or "\\" in result
        or Path(result).name != result
    ):
        raise ValueError(f"{field_name} must be one normalized basename")
    return result


def _require_portable_activation_nonnegative_int(
    value: object,
    field_name: str,
) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field_name} must be a non-negative built-in integer")
    return value


@dataclass(frozen=True, slots=True)
class _PortableActivationJournalUnsigned:
    """Portable PREPARED owner facts before the nested W2 proof is minted."""

    journal_version: str
    closure: str
    phase: str
    journal_id: str
    preparation_id: str
    registry_namespace: str
    token_id: str
    token_version: str
    activation_nonce: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    expected_prior_generation: int | None
    gate_b_grant_digest: str
    evidence_digest: str
    snapshot_receipt_digest: str
    stage_db_digest: str
    manifest_temp_digest: str
    source_jsonl_digest: str
    new_receipt_id: str
    new_manifest_digest: str
    candidate_stage_db_name: str
    candidate_manifest_temp_name: str
    private_directory_name: str
    device_key_name: str
    journal_name: str
    terminal_name: str
    lock_payload_digest: str
    sealed_content_attestation: PortableSealedContentAttestation
    active_content_attestation: None

    def __post_init__(self) -> None:
        if self.journal_version != _PORTABLE_ACTIVATION_JOURNAL_VERSION:
            raise ValueError("portable activation journal version is unsupported")
        if self.closure not in _PORTABLE_ACTIVATION_JOURNAL_CLOSURES:
            raise ValueError("portable activation journal closure is invalid")
        if self.phase != _PORTABLE_ACTIVATION_JOURNAL_PHASE:
            raise ValueError("portable activation journal phase is invalid")
        for field_name in (
            "journal_id", "preparation_id", "registry_namespace", "token_id",
            "token_version", "activation_nonce", "artifact_id", "resource_id",
            "canonical_store_id", "new_receipt_id",
        ):
            _require_portable_activation_string(getattr(self, field_name), field_name)
        for field_name in (
            "artifact_seal_digest", "sealed_stage_digest", "target_identity",
            "gate_b_grant_digest", "evidence_digest", "snapshot_receipt_digest",
            "stage_db_digest", "manifest_temp_digest", "source_jsonl_digest",
            "new_manifest_digest", "lock_payload_digest",
        ):
            _require_portable_activation_digest(getattr(self, field_name), field_name)
        for field_name in (
            "candidate_stage_db_name", "candidate_manifest_temp_name",
            "private_directory_name", "device_key_name", "journal_name",
            "terminal_name",
        ):
            _require_portable_activation_basename(getattr(self, field_name), field_name)
        if self.expected_prior_generation is not None:
            raise ValueError("portable first activation must not carry a prior generation")
        if self.active_content_attestation is not None:
            raise ValueError("PREPARED must not carry an active content attestation")
        if type(self.sealed_content_attestation) is not PortableSealedContentAttestation:
            raise TypeError(
                "portable activation journal requires exact portable sealed attestation"
            )


_PORTABLE_ACTIVATION_UNSIGNED_FIELDS = frozenset(
    item.name for item in fields(_PortableActivationJournalUnsigned)
)
_PORTABLE_ACTIVATION_RECORD_FIELDS = (
    _PORTABLE_ACTIVATION_UNSIGNED_FIELDS
    | {"private_directory_proof", "record_digest"}
)


def _portable_activation_unsigned_to_mapping(
    unsigned: _PortableActivationJournalUnsigned,
) -> dict[str, object]:
    if type(unsigned) is not _PortableActivationJournalUnsigned:
        raise TypeError("portable journal unsigned facts are invalid")
    return {
        "activation_nonce": unsigned.activation_nonce,
        "active_content_attestation": None,
        "artifact_id": unsigned.artifact_id,
        "artifact_seal_digest": unsigned.artifact_seal_digest,
        "candidate_manifest_temp_name": unsigned.candidate_manifest_temp_name,
        "candidate_stage_db_name": unsigned.candidate_stage_db_name,
        "canonical_store_id": unsigned.canonical_store_id,
        "closure": unsigned.closure,
        "device_key_name": unsigned.device_key_name,
        "evidence_digest": unsigned.evidence_digest,
        "expected_prior_generation": unsigned.expected_prior_generation,
        "gate_b_grant_digest": unsigned.gate_b_grant_digest,
        "journal_id": unsigned.journal_id,
        "journal_name": unsigned.journal_name,
        "journal_version": unsigned.journal_version,
        "lock_payload_digest": unsigned.lock_payload_digest,
        "manifest_temp_digest": unsigned.manifest_temp_digest,
        "new_manifest_digest": unsigned.new_manifest_digest,
        "new_receipt_id": unsigned.new_receipt_id,
        "phase": unsigned.phase,
        "preparation_id": unsigned.preparation_id,
        "private_directory_name": unsigned.private_directory_name,
        "registry_namespace": unsigned.registry_namespace,
        "resource_id": unsigned.resource_id,
        "sealed_content_attestation": _sealed_content_attestation_record_to_mapping(
            unsigned.sealed_content_attestation
        ),
        "sealed_stage_digest": unsigned.sealed_stage_digest,
        "snapshot_receipt_digest": unsigned.snapshot_receipt_digest,
        "source_jsonl_digest": unsigned.source_jsonl_digest,
        "stage_db_digest": unsigned.stage_db_digest,
        "target_identity": unsigned.target_identity,
        "terminal_name": unsigned.terminal_name,
        "token_id": unsigned.token_id,
        "token_version": unsigned.token_version,
    }


def _portable_activation_owner_context_sha256(
    unsigned: _PortableActivationJournalUnsigned,
) -> bytes:
    """Digest owner facts only: neither nested proof nor record digest."""

    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_activation_unsigned_to_mapping(unsigned)
        )
    ).digest()


@dataclass(frozen=True, slots=True)
class _PortableActivationJournalRecord:
    unsigned: _PortableActivationJournalUnsigned
    private_directory_proof: WindowsPrivateProof
    record_digest: str

    def __post_init__(self) -> None:
        if type(self.unsigned) is not _PortableActivationJournalUnsigned:
            raise TypeError("portable activation unsigned facts are invalid")
        if type(self.private_directory_proof) is not WindowsPrivateProof:
            raise TypeError("portable activation private proof is invalid")
        if self.private_directory_proof.object_role is not PrivateProofObjectRole.PRIVATE_DIRECTORY:
            raise ValueError("portable activation proof must bind private directory")
        if (
            self.private_directory_proof.owner_context_sha256
            != _portable_activation_owner_context_sha256(self.unsigned)
        ):
            raise ValueError("portable activation proof owner context does not close")
        _require_portable_activation_digest(self.record_digest, "record_digest")
        if self.record_digest != _portable_activation_record_digest(
            self.unsigned, self.private_directory_proof
        ):
            raise ValueError("portable activation record digest does not close")


def _portable_activation_proof_to_mapping(
    proof: WindowsPrivateProof,
) -> dict[str, object]:
    value = json.loads(encode_windows_private_proof(proof).decode("utf-8"))
    if type(value) is not dict:
        raise TypeError("private proof encoder did not return an object")
    return value


def _portable_activation_proof_from_mapping(mapping: object) -> WindowsPrivateProof:
    if type(mapping) is not dict:
        raise TypeError("private directory proof must be an exact object")
    return decode_windows_private_proof(_portable_activation_canonical_json(mapping))


def _portable_activation_record_payload(
    unsigned: _PortableActivationJournalUnsigned,
    proof: WindowsPrivateProof,
) -> dict[str, object]:
    mapping = _portable_activation_unsigned_to_mapping(unsigned)
    mapping["private_directory_proof"] = _portable_activation_proof_to_mapping(proof)
    return mapping


def _portable_activation_record_digest(
    unsigned: _PortableActivationJournalUnsigned,
    proof: WindowsPrivateProof,
) -> str:
    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_activation_record_payload(unsigned, proof)
        )
    ).hexdigest()


def _create_portable_activation_journal_record(
    unsigned: _PortableActivationJournalUnsigned,
    proof: WindowsPrivateProof,
) -> _PortableActivationJournalRecord:
    return _PortableActivationJournalRecord(
        unsigned=unsigned,
        private_directory_proof=proof,
        record_digest=_portable_activation_record_digest(unsigned, proof),
    )


def _serialize_portable_activation_journal_record(
    record: _PortableActivationJournalRecord,
) -> bytes:
    if type(record) is not _PortableActivationJournalRecord:
        raise TypeError("portable activation journal record is invalid")
    mapping = _portable_activation_record_payload(
        record.unsigned, record.private_directory_proof
    )
    mapping["record_digest"] = record.record_digest
    return _portable_activation_canonical_json(mapping) + b"\n"


def _parse_portable_activation_journal_bytes(
    serialized: bytes,
) -> _PortableActivationJournalRecord:
    if type(serialized) is not bytes:
        raise TypeError("portable activation journal bytes must be exact bytes")
    if not serialized or len(serialized) > _PORTABLE_ACTIVATION_JOURNAL_MAX_BYTES:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID", retryable=False
        )

    class _DuplicatePortableKey(ValueError):
        pass

    def reject_constant(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number is not allowed")

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key, value in pairs:
            if key in mapping:
                raise _DuplicatePortableKey(key)
            mapping[key] = value
        return mapping

    try:
        mapping = json.loads(
            serialized.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
        if type(mapping) is not dict or set(mapping) != _PORTABLE_ACTIVATION_RECORD_FIELDS:
            raise ValueError("portable journal fields are invalid")
        unsigned = _PortableActivationJournalUnsigned(
            journal_version=mapping["journal_version"],
            closure=mapping["closure"],
            phase=mapping["phase"],
            journal_id=mapping["journal_id"],
            preparation_id=mapping["preparation_id"],
            registry_namespace=mapping["registry_namespace"],
            token_id=mapping["token_id"],
            token_version=mapping["token_version"],
            activation_nonce=mapping["activation_nonce"],
            artifact_id=mapping["artifact_id"],
            artifact_seal_digest=mapping["artifact_seal_digest"],
            sealed_stage_digest=mapping["sealed_stage_digest"],
            resource_id=mapping["resource_id"],
            target_identity=mapping["target_identity"],
            canonical_store_id=mapping["canonical_store_id"],
            expected_prior_generation=mapping["expected_prior_generation"],
            gate_b_grant_digest=mapping["gate_b_grant_digest"],
            evidence_digest=mapping["evidence_digest"],
            snapshot_receipt_digest=mapping["snapshot_receipt_digest"],
            stage_db_digest=mapping["stage_db_digest"],
            manifest_temp_digest=mapping["manifest_temp_digest"],
            source_jsonl_digest=mapping["source_jsonl_digest"],
            new_receipt_id=mapping["new_receipt_id"],
            new_manifest_digest=mapping["new_manifest_digest"],
            candidate_stage_db_name=mapping["candidate_stage_db_name"],
            candidate_manifest_temp_name=mapping["candidate_manifest_temp_name"],
            private_directory_name=mapping["private_directory_name"],
            device_key_name=mapping["device_key_name"],
            journal_name=mapping["journal_name"],
            terminal_name=mapping["terminal_name"],
            lock_payload_digest=mapping["lock_payload_digest"],
            sealed_content_attestation=_sealed_content_attestation_record_from_mapping(
                mapping["sealed_content_attestation"]
            ),
            active_content_attestation=mapping["active_content_attestation"],
        )
        record = _PortableActivationJournalRecord(
            unsigned=unsigned,
            private_directory_proof=_portable_activation_proof_from_mapping(
                mapping["private_directory_proof"]
            ),
            record_digest=mapping["record_digest"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID", retryable=False
        ) from error
    if _serialize_portable_activation_journal_record(record) != serialized:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PARSE_INVALID", retryable=False
        )
    return record


# Canonical publication is a distinct append-only chain.  Its field set and
# parser are intentionally not shared with the v3 PREPARED/CANCELLED envelope.
_PORTABLE_PUBLICATION_VERSION = "activation-publication-v1"
_PORTABLE_PUBLICATION_PHASES = (
    "DB_REPLACED",
    "MANIFEST_PUBLISHED",
    "GENERATION_PUBLISHED",
)
_PORTABLE_PUBLICATION_PHASE_NAMES = {
    "DB_REPLACED": "activation-publication-db-replaced-v1.json",
    "MANIFEST_PUBLISHED": "activation-publication-manifest-published-v1.json",
    "GENERATION_PUBLISHED": "activation-publication-generation-published-v1.json",
}
_PORTABLE_PUBLICATION_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class _PortablePublicationPhaseUnsigned:
    publication_version: str
    phase: str
    predecessor_digest: str
    journal_id: str
    preparation_id: str
    token_id: str
    token_version: str
    activation_nonce: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    generation: int
    canonical_database_name: str
    canonical_database_size: int
    canonical_database_sha256: str
    canonical_manifest_name: str
    canonical_manifest_size: int
    canonical_manifest_sha256: str
    private_directory_name: str
    device_key_name: str
    sealed_content_attestation: PortableSealedContentAttestation
    active_content_attestation: PortableActiveContentAttestation | None

    def __post_init__(self) -> None:
        if self.publication_version != _PORTABLE_PUBLICATION_VERSION:
            raise ValueError("portable publication version is unsupported")
        if self.phase not in _PORTABLE_PUBLICATION_PHASES:
            raise ValueError("portable publication phase is invalid")
        for field_name in (
            "journal_id",
            "preparation_id",
            "token_id",
            "token_version",
            "activation_nonce",
            "resource_id",
            "canonical_store_id",
        ):
            _require_portable_activation_string(getattr(self, field_name), field_name)
        for field_name in (
            "predecessor_digest",
            "target_identity",
            "canonical_database_sha256",
            "canonical_manifest_sha256",
        ):
            _require_portable_activation_digest(getattr(self, field_name), field_name)
        for field_name in (
            "canonical_database_name",
            "canonical_manifest_name",
            "private_directory_name",
            "device_key_name",
        ):
            _require_portable_activation_basename(getattr(self, field_name), field_name)
        _require_portable_activation_nonnegative_int(
            self.canonical_database_size,
            "canonical_database_size",
        )
        _require_portable_activation_nonnegative_int(
            self.canonical_manifest_size,
            "canonical_manifest_size",
        )
        if self.generation != 0 or type(self.generation) is not int:
            raise ValueError("portable first publication generation must be zero")
        sealed = self.sealed_content_attestation
        if type(sealed) is not PortableSealedContentAttestation:
            raise TypeError("portable publication requires exact sealed attestation")
        if (
            sealed.resource_id != self.resource_id
            or sealed.target_identity != self.target_identity
            or sealed.canonical_store_id != self.canonical_store_id
            or sealed.expected_prior_generation is not None
        ):
            raise ValueError("portable publication sealed attestation does not bind")
        active = self.active_content_attestation
        if self.phase == "DB_REPLACED":
            if active is not None:
                raise ValueError("DB_REPLACED must not carry active attestation")
            expected_database = sealed.database
            expected_manifest = sealed.manifest
        else:
            if type(active) is not PortableActiveContentAttestation:
                raise TypeError("published manifest phases require active attestation")
            if (
                active.resource_id != self.resource_id
                or active.target_identity != self.target_identity
                or active.canonical_store_id != self.canonical_store_id
                or active.generation != 0
                or active.journal_id != self.journal_id
                or active.sealed_attestation_digest != sealed.attestation_digest
                or active.snapshot_receipt_digest != sealed.snapshot_receipt_digest
                or active.source != sealed.source
            ):
                raise ValueError("portable publication active attestation does not bind")
            expected_database = active.database
            expected_manifest = active.manifest
        if (
            expected_database.size != self.canonical_database_size
            or expected_database.sha256 != self.canonical_database_sha256
            or expected_manifest.size != self.canonical_manifest_size
            or expected_manifest.sha256 != self.canonical_manifest_sha256
        ):
            raise ValueError("portable publication canonical content does not bind")


_PORTABLE_PUBLICATION_UNSIGNED_FIELDS = frozenset(
    item.name for item in fields(_PortablePublicationPhaseUnsigned)
)
_PORTABLE_PUBLICATION_RECORD_FIELDS = (
    _PORTABLE_PUBLICATION_UNSIGNED_FIELDS
    | {"private_directory_proof", "record_digest"}
)


def _portable_publication_unsigned_to_mapping(
    unsigned: _PortablePublicationPhaseUnsigned,
) -> dict[str, object]:
    if type(unsigned) is not _PortablePublicationPhaseUnsigned:
        raise TypeError("portable publication unsigned facts are invalid")
    active = unsigned.active_content_attestation
    return {
        "activation_nonce": unsigned.activation_nonce,
        "active_content_attestation": (
            None
            if active is None
            else _active_content_attestation_record_to_mapping(active)
        ),
        "canonical_database_name": unsigned.canonical_database_name,
        "canonical_database_sha256": unsigned.canonical_database_sha256,
        "canonical_database_size": unsigned.canonical_database_size,
        "canonical_manifest_name": unsigned.canonical_manifest_name,
        "canonical_manifest_sha256": unsigned.canonical_manifest_sha256,
        "canonical_manifest_size": unsigned.canonical_manifest_size,
        "canonical_store_id": unsigned.canonical_store_id,
        "device_key_name": unsigned.device_key_name,
        "generation": unsigned.generation,
        "journal_id": unsigned.journal_id,
        "phase": unsigned.phase,
        "predecessor_digest": unsigned.predecessor_digest,
        "preparation_id": unsigned.preparation_id,
        "private_directory_name": unsigned.private_directory_name,
        "publication_version": unsigned.publication_version,
        "resource_id": unsigned.resource_id,
        "sealed_content_attestation": _sealed_content_attestation_record_to_mapping(
            unsigned.sealed_content_attestation
        ),
        "target_identity": unsigned.target_identity,
        "token_id": unsigned.token_id,
        "token_version": unsigned.token_version,
    }


def _portable_publication_owner_context_sha256(
    unsigned: _PortablePublicationPhaseUnsigned,
) -> bytes:
    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_publication_unsigned_to_mapping(unsigned)
        )
    ).digest()


@dataclass(frozen=True, slots=True)
class _PortablePublicationPhaseRecord:
    unsigned: _PortablePublicationPhaseUnsigned
    private_directory_proof: WindowsPrivateProof
    record_digest: str

    def __post_init__(self) -> None:
        if type(self.unsigned) is not _PortablePublicationPhaseUnsigned:
            raise TypeError("portable publication unsigned facts are invalid")
        if type(self.private_directory_proof) is not WindowsPrivateProof:
            raise TypeError("portable publication private proof is invalid")
        if (
            self.private_directory_proof.object_role
            is not PrivateProofObjectRole.PRIVATE_DIRECTORY
            or self.private_directory_proof.owner_context_sha256
            != _portable_publication_owner_context_sha256(self.unsigned)
        ):
            raise ValueError("portable publication private proof does not bind")
        _require_portable_activation_digest(self.record_digest, "record_digest")
        if self.record_digest != _portable_publication_record_digest(
            self.unsigned,
            self.private_directory_proof,
        ):
            raise ValueError("portable publication record digest does not close")


def _portable_publication_record_payload(
    unsigned: _PortablePublicationPhaseUnsigned,
    proof: WindowsPrivateProof,
) -> dict[str, object]:
    mapping = _portable_publication_unsigned_to_mapping(unsigned)
    mapping["private_directory_proof"] = _portable_activation_proof_to_mapping(proof)
    return mapping


def _portable_publication_record_digest(
    unsigned: _PortablePublicationPhaseUnsigned,
    proof: WindowsPrivateProof,
) -> str:
    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_publication_record_payload(unsigned, proof)
        )
    ).hexdigest()


def _create_portable_publication_phase_record(
    unsigned: _PortablePublicationPhaseUnsigned,
    proof: WindowsPrivateProof,
) -> _PortablePublicationPhaseRecord:
    return _PortablePublicationPhaseRecord(
        unsigned=unsigned,
        private_directory_proof=proof,
        record_digest=_portable_publication_record_digest(unsigned, proof),
    )


def _serialize_portable_publication_phase_record(
    record: _PortablePublicationPhaseRecord,
) -> bytes:
    if type(record) is not _PortablePublicationPhaseRecord:
        raise TypeError("portable publication record is invalid")
    mapping = _portable_publication_record_payload(
        record.unsigned,
        record.private_directory_proof,
    )
    mapping["record_digest"] = record.record_digest
    return _portable_activation_canonical_json(mapping) + b"\n"


def _parse_portable_publication_phase_bytes(
    serialized: bytes,
) -> _PortablePublicationPhaseRecord:
    if type(serialized) is not bytes:
        raise TypeError("portable publication bytes must be exact bytes")
    if not serialized or len(serialized) > _PORTABLE_PUBLICATION_MAX_BYTES:
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_PARSE_INVALID",
            retryable=False,
        )

    class _DuplicatePortablePublicationKey(ValueError):
        pass

    def reject_constant(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number is not allowed")

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key, value in pairs:
            if key in mapping:
                raise _DuplicatePortablePublicationKey(key)
            mapping[key] = value
        return mapping

    try:
        mapping = json.loads(
            serialized.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
        if type(mapping) is not dict or set(mapping) != _PORTABLE_PUBLICATION_RECORD_FIELDS:
            raise ValueError("portable publication fields are invalid")
        active_mapping = mapping["active_content_attestation"]
        active = (
            None
            if active_mapping is None
            else _active_content_attestation_record_from_mapping(active_mapping)
        )
        unsigned = _PortablePublicationPhaseUnsigned(
            publication_version=mapping["publication_version"],
            phase=mapping["phase"],
            predecessor_digest=mapping["predecessor_digest"],
            journal_id=mapping["journal_id"],
            preparation_id=mapping["preparation_id"],
            token_id=mapping["token_id"],
            token_version=mapping["token_version"],
            activation_nonce=mapping["activation_nonce"],
            resource_id=mapping["resource_id"],
            target_identity=mapping["target_identity"],
            canonical_store_id=mapping["canonical_store_id"],
            generation=mapping["generation"],
            canonical_database_name=mapping["canonical_database_name"],
            canonical_database_size=mapping["canonical_database_size"],
            canonical_database_sha256=mapping["canonical_database_sha256"],
            canonical_manifest_name=mapping["canonical_manifest_name"],
            canonical_manifest_size=mapping["canonical_manifest_size"],
            canonical_manifest_sha256=mapping["canonical_manifest_sha256"],
            private_directory_name=mapping["private_directory_name"],
            device_key_name=mapping["device_key_name"],
            sealed_content_attestation=_sealed_content_attestation_record_from_mapping(
                mapping["sealed_content_attestation"]
            ),
            active_content_attestation=active,
        )
        record = _PortablePublicationPhaseRecord(
            unsigned=unsigned,
            private_directory_proof=_portable_activation_proof_from_mapping(
                mapping["private_directory_proof"]
            ),
            record_digest=mapping["record_digest"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_PARSE_INVALID",
            retryable=False,
        ) from error
    if _serialize_portable_publication_phase_record(record) != serialized:
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_PARSE_INVALID",
            retryable=False,
        )
    return record


# Replacement activation is deliberately a new envelope.  The v3 PREPARED
# record and activation-publication-v1 chain above remain the generation-zero
# protocol and must never be widened to describe N -> N+1 replacement.
_PORTABLE_REPLACEMENT_VERSION = "activation-replacement-v1"
_PORTABLE_REPLACEMENT_OPERATION = "REPLACEMENT"
_PORTABLE_REPLACEMENT_PHASES = (
    "PREPARED",
    "DB_REPLACED",
    "MANIFEST_PUBLISHED",
    "GENERATION_PUBLISHED",
    "READY",
)
_PORTABLE_REPLACEMENT_CURRENT_NAME = "activation-replacement-current-v1.json"
_PORTABLE_REPLACEMENT_PHASE_NAMES = {
    "PREPARED": "activation-replacement-pending-v1.json",
    "DB_REPLACED": "activation-replacement-db-replaced-v1.json",
    "MANIFEST_PUBLISHED": "activation-replacement-manifest-published-v1.json",
    "GENERATION_PUBLISHED": "activation-replacement-generation-published-v1.json",
    "READY": _PORTABLE_REPLACEMENT_CURRENT_NAME,
}
_PORTABLE_REPLACEMENT_RECORD_NAMES = frozenset(
    _PORTABLE_REPLACEMENT_PHASE_NAMES.values()
)
_PORTABLE_REPLACEMENT_MAX_BYTES = 1024 * 1024
_PORTABLE_REPLACEMENT_SNAPSHOT_FACTORY_KEY = object()


def _portable_replacement_phase_name(phase: str) -> str:
    try:
        return _PORTABLE_REPLACEMENT_PHASE_NAMES[phase]
    except (KeyError, TypeError) as error:
        raise ValueError("portable replacement phase is invalid") from error


def _portable_replacement_backup_name(
    preparation_id: str,
    asset_kind: str,
) -> str:
    preparation = _require_portable_activation_string(
        preparation_id,
        "preparation_id",
    )
    if asset_kind not in {"DATABASE", "MANIFEST"}:
        raise ValueError("portable replacement backup kind is invalid")
    preparation_digest = hashlib.sha256(preparation.encode("utf-8")).hexdigest()
    suffix = "database" if asset_kind == "DATABASE" else "manifest"
    return (
        f"activation-replacement-prior-{preparation_digest}-{suffix}-v1.backup"
    )


@dataclass(frozen=True, slots=True)
class _PortableReplacementBackupProof:
    """Persisted backup content facts; never a historical live FileId."""

    asset_kind: str
    backup_name: str
    content: PortableContentFileProof

    def __post_init__(self) -> None:
        if self.asset_kind not in {"DATABASE", "MANIFEST"}:
            raise ValueError("portable replacement backup kind is invalid")
        _require_portable_activation_basename(self.backup_name, "backup_name")
        if type(self.content) is not PortableContentFileProof:
            raise TypeError("portable replacement backup content is invalid")


def _portable_replacement_backup_to_mapping(
    backup: _PortableReplacementBackupProof,
) -> dict[str, object]:
    if type(backup) is not _PortableReplacementBackupProof:
        raise TypeError("portable replacement backup proof is invalid")
    return {
        "asset_kind": backup.asset_kind,
        "backup_name": backup.backup_name,
        "content": _portable_content_file_proof_to_mapping(backup.content),
    }


def _portable_replacement_backup_from_mapping(
    mapping: object,
) -> _PortableReplacementBackupProof:
    if type(mapping) is not dict or set(mapping) != {
        "asset_kind",
        "backup_name",
        "content",
    }:
        raise ValueError("portable replacement backup fields are invalid")
    return _PortableReplacementBackupProof(
        asset_kind=mapping["asset_kind"],
        backup_name=mapping["backup_name"],
        content=_portable_content_file_proof_from_mapping(mapping["content"]),
    )


@dataclass(frozen=True, slots=True)
class _PortableReplacementUnsigned:
    """Strict N -> N+1 owner facts before the nested W2 proof is minted."""

    replacement_version: str
    operation: str
    phase: str
    predecessor_digest: str
    journal_id: str
    preparation_id: str
    registry_namespace: str
    token_id: str
    token_version: str
    activation_nonce: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    prior_authority_digest: str
    prior_canonical_store_id: str
    prior_generation: int
    prior_active_content_attestation: PortableActiveContentAttestation
    backup_proofs: tuple[
        _PortableReplacementBackupProof,
        _PortableReplacementBackupProof,
    ]
    candidate_canonical_store_id: str
    next_generation: int
    gate_b_grant_digest: str
    evidence_digest: str
    source_jsonl_digest: str
    new_receipt_id: str
    new_manifest_digest: str
    candidate_stage_db_name: str
    candidate_manifest_temp_name: str
    canonical_database_name: str
    canonical_manifest_name: str
    private_directory_name: str
    device_key_name: str
    lock_payload_digest: str
    sealed_content_attestation: PortableSealedContentAttestation
    active_content_attestation: PortableActiveContentAttestation | None

    def __post_init__(self) -> None:
        if self.replacement_version != _PORTABLE_REPLACEMENT_VERSION:
            raise ValueError("portable replacement version is unsupported")
        if self.operation != _PORTABLE_REPLACEMENT_OPERATION:
            raise ValueError("portable replacement discriminator is invalid")
        if self.phase not in _PORTABLE_REPLACEMENT_PHASES:
            raise ValueError("portable replacement phase is invalid")
        for field_name in (
            "journal_id",
            "preparation_id",
            "registry_namespace",
            "token_id",
            "token_version",
            "activation_nonce",
            "artifact_id",
            "resource_id",
            "prior_canonical_store_id",
            "candidate_canonical_store_id",
            "new_receipt_id",
        ):
            _require_portable_activation_string(getattr(self, field_name), field_name)
        for field_name in (
            "predecessor_digest",
            "artifact_seal_digest",
            "sealed_stage_digest",
            "target_identity",
            "prior_authority_digest",
            "gate_b_grant_digest",
            "evidence_digest",
            "source_jsonl_digest",
            "new_manifest_digest",
            "lock_payload_digest",
        ):
            _require_portable_activation_digest(getattr(self, field_name), field_name)
        if (
            self.phase == "PREPARED"
            and self.predecessor_digest != self.prior_authority_digest
        ):
            raise ValueError(
                "portable replacement PREPARED predecessor is not prior authority"
            )
        for field_name in (
            "candidate_stage_db_name",
            "candidate_manifest_temp_name",
            "canonical_database_name",
            "canonical_manifest_name",
            "private_directory_name",
            "device_key_name",
        ):
            _require_portable_activation_basename(getattr(self, field_name), field_name)
        _require_portable_activation_nonnegative_int(
            self.prior_generation,
            "prior_generation",
        )
        _require_portable_activation_nonnegative_int(
            self.next_generation,
            "next_generation",
        )
        if self.next_generation != self.prior_generation + 1:
            raise ValueError("portable replacement generation does not advance once")
        if self.candidate_canonical_store_id == self.prior_canonical_store_id:
            raise ValueError("portable replacement candidate store must be fresh")
        if self.candidate_stage_db_name == self.candidate_manifest_temp_name:
            raise ValueError("portable replacement candidate names must be distinct")
        if self.canonical_database_name == self.canonical_manifest_name:
            raise ValueError("portable replacement canonical names must be distinct")

        prior = self.prior_active_content_attestation
        if type(prior) is not PortableActiveContentAttestation:
            raise TypeError("portable replacement prior attestation is invalid")
        if (
            prior.resource_id != self.resource_id
            or prior.target_identity != self.target_identity
            or prior.canonical_store_id != self.prior_canonical_store_id
            or prior.generation != self.prior_generation
        ):
            raise ValueError("portable replacement prior attestation does not bind")
        if type(self.backup_proofs) is not tuple or len(self.backup_proofs) != 2:
            raise TypeError("portable replacement requires two exact backup proofs")
        database_backup, manifest_backup = self.backup_proofs
        if (
            type(database_backup) is not _PortableReplacementBackupProof
            or type(manifest_backup) is not _PortableReplacementBackupProof
            or database_backup.asset_kind != "DATABASE"
            or manifest_backup.asset_kind != "MANIFEST"
            or database_backup.backup_name
            != _portable_replacement_backup_name(self.preparation_id, "DATABASE")
            or manifest_backup.backup_name
            != _portable_replacement_backup_name(self.preparation_id, "MANIFEST")
            or database_backup.content != prior.database
            or manifest_backup.content != prior.manifest
        ):
            raise ValueError("portable replacement backup proofs do not bind")

        sealed = self.sealed_content_attestation
        if type(sealed) is not PortableSealedContentAttestation:
            raise TypeError("portable replacement sealed attestation is invalid")
        if (
            sealed.resource_id != self.resource_id
            or sealed.target_identity != self.target_identity
            or sealed.canonical_store_id != self.candidate_canonical_store_id
            or sealed.expected_prior_generation != self.prior_generation
            or sealed.evidence_digest != self.evidence_digest
            or sealed.source.sha256 != self.source_jsonl_digest
        ):
            raise ValueError("portable replacement sealed attestation does not bind")

        active = self.active_content_attestation
        if self.phase in {"PREPARED", "DB_REPLACED"}:
            if active is not None:
                raise ValueError(
                    "portable replacement pre-manifest phase cannot be active"
                )
        else:
            if type(active) is not PortableActiveContentAttestation:
                raise TypeError(
                    "portable replacement published phase requires active attestation"
                )
            if (
                active.journal_id != self.journal_id
                or active.resource_id != self.resource_id
                or active.target_identity != self.target_identity
                or active.canonical_store_id != self.candidate_canonical_store_id
                or active.generation != self.next_generation
                or active.sealed_attestation_digest != sealed.attestation_digest
                or active.snapshot_receipt_digest != sealed.snapshot_receipt_digest
                or active.manifest != sealed.manifest
                or active.source != sealed.source
            ):
                raise ValueError("portable replacement active attestation does not bind")


_PORTABLE_REPLACEMENT_UNSIGNED_FIELDS = frozenset(
    item.name for item in fields(_PortableReplacementUnsigned)
)
_PORTABLE_REPLACEMENT_RECORD_FIELDS = (
    _PORTABLE_REPLACEMENT_UNSIGNED_FIELDS
    | {"private_directory_proof", "record_digest"}
)


def _portable_replacement_unsigned_to_mapping(
    unsigned: _PortableReplacementUnsigned,
) -> dict[str, object]:
    if type(unsigned) is not _PortableReplacementUnsigned:
        raise TypeError("portable replacement unsigned facts are invalid")
    active = unsigned.active_content_attestation
    return {
        "activation_nonce": unsigned.activation_nonce,
        "active_content_attestation": (
            None
            if active is None
            else _portable_active_content_attestation_to_mapping(active)
        ),
        "artifact_id": unsigned.artifact_id,
        "artifact_seal_digest": unsigned.artifact_seal_digest,
        "backup_proofs": [
            _portable_replacement_backup_to_mapping(backup)
            for backup in unsigned.backup_proofs
        ],
        "candidate_canonical_store_id": unsigned.candidate_canonical_store_id,
        "candidate_manifest_temp_name": unsigned.candidate_manifest_temp_name,
        "candidate_stage_db_name": unsigned.candidate_stage_db_name,
        "canonical_database_name": unsigned.canonical_database_name,
        "canonical_manifest_name": unsigned.canonical_manifest_name,
        "device_key_name": unsigned.device_key_name,
        "evidence_digest": unsigned.evidence_digest,
        "gate_b_grant_digest": unsigned.gate_b_grant_digest,
        "journal_id": unsigned.journal_id,
        "lock_payload_digest": unsigned.lock_payload_digest,
        "new_manifest_digest": unsigned.new_manifest_digest,
        "new_receipt_id": unsigned.new_receipt_id,
        "next_generation": unsigned.next_generation,
        "operation": unsigned.operation,
        "phase": unsigned.phase,
        "predecessor_digest": unsigned.predecessor_digest,
        "preparation_id": unsigned.preparation_id,
        "prior_active_content_attestation": (
            _portable_active_content_attestation_to_mapping(
                unsigned.prior_active_content_attestation
            )
        ),
        "prior_authority_digest": unsigned.prior_authority_digest,
        "prior_canonical_store_id": unsigned.prior_canonical_store_id,
        "prior_generation": unsigned.prior_generation,
        "private_directory_name": unsigned.private_directory_name,
        "registry_namespace": unsigned.registry_namespace,
        "replacement_version": unsigned.replacement_version,
        "resource_id": unsigned.resource_id,
        "sealed_content_attestation": (
            _portable_sealed_content_attestation_to_mapping(
                unsigned.sealed_content_attestation
            )
        ),
        "sealed_stage_digest": unsigned.sealed_stage_digest,
        "source_jsonl_digest": unsigned.source_jsonl_digest,
        "target_identity": unsigned.target_identity,
        "token_id": unsigned.token_id,
        "token_version": unsigned.token_version,
    }


def _portable_replacement_owner_context_sha256(
    unsigned: _PortableReplacementUnsigned,
) -> bytes:
    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_replacement_unsigned_to_mapping(unsigned)
        )
    ).digest()


@dataclass(frozen=True, slots=True)
class _PortableReplacementRecord:
    unsigned: _PortableReplacementUnsigned
    private_directory_proof: WindowsPrivateProof
    record_digest: str

    def __post_init__(self) -> None:
        if type(self.unsigned) is not _PortableReplacementUnsigned:
            raise TypeError("portable replacement unsigned facts are invalid")
        if type(self.private_directory_proof) is not WindowsPrivateProof:
            raise TypeError("portable replacement private proof is invalid")
        if (
            self.private_directory_proof.object_role
            is not PrivateProofObjectRole.PRIVATE_DIRECTORY
            or self.private_directory_proof.owner_context_sha256
            != _portable_replacement_owner_context_sha256(self.unsigned)
        ):
            raise ValueError("portable replacement private proof does not bind")
        _require_portable_activation_digest(self.record_digest, "record_digest")
        if self.record_digest != _portable_replacement_record_digest(
            self.unsigned,
            self.private_directory_proof,
        ):
            raise ValueError("portable replacement record digest does not close")


def _portable_replacement_record_payload(
    unsigned: _PortableReplacementUnsigned,
    proof: WindowsPrivateProof,
) -> dict[str, object]:
    mapping = _portable_replacement_unsigned_to_mapping(unsigned)
    mapping["private_directory_proof"] = _portable_activation_proof_to_mapping(proof)
    return mapping


def _portable_replacement_record_digest(
    unsigned: _PortableReplacementUnsigned,
    proof: WindowsPrivateProof,
) -> str:
    return hashlib.sha256(
        _portable_activation_canonical_json(
            _portable_replacement_record_payload(unsigned, proof)
        )
    ).hexdigest()


def _create_portable_replacement_record(
    unsigned: _PortableReplacementUnsigned,
    proof: WindowsPrivateProof,
) -> _PortableReplacementRecord:
    return _PortableReplacementRecord(
        unsigned=unsigned,
        private_directory_proof=proof,
        record_digest=_portable_replacement_record_digest(unsigned, proof),
    )


def _serialize_portable_replacement_record(
    record: _PortableReplacementRecord,
) -> bytes:
    if type(record) is not _PortableReplacementRecord:
        raise TypeError("portable replacement record is invalid")
    mapping = _portable_replacement_record_payload(
        record.unsigned,
        record.private_directory_proof,
    )
    mapping["record_digest"] = record.record_digest
    return _portable_activation_canonical_json(mapping) + b"\n"


def _parse_portable_replacement_record_bytes(
    serialized: bytes,
) -> _PortableReplacementRecord:
    if type(serialized) is not bytes:
        raise TypeError("portable replacement bytes must be exact bytes")
    if not serialized or len(serialized) > _PORTABLE_REPLACEMENT_MAX_BYTES:
        raise ActivationPreparationError(
            "ACTIVATION.REPLACEMENT_PARSE_INVALID",
            retryable=False,
        )

    def reject_constant(value: str) -> None:
        del value
        raise ValueError("non-finite JSON number is not allowed")

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key, value in pairs:
            if key in mapping:
                raise ValueError("duplicate portable replacement key")
            mapping[key] = value
        return mapping

    try:
        mapping = json.loads(
            serialized.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=strict_object,
        )
        if type(mapping) is not dict or set(mapping) != _PORTABLE_REPLACEMENT_RECORD_FIELDS:
            raise ValueError("portable replacement fields are invalid")
        backup_mappings = mapping["backup_proofs"]
        if type(backup_mappings) is not list or len(backup_mappings) != 2:
            raise TypeError("portable replacement backup list is invalid")
        active_mapping = mapping["active_content_attestation"]
        unsigned = _PortableReplacementUnsigned(
            replacement_version=mapping["replacement_version"],
            operation=mapping["operation"],
            phase=mapping["phase"],
            predecessor_digest=mapping["predecessor_digest"],
            journal_id=mapping["journal_id"],
            preparation_id=mapping["preparation_id"],
            registry_namespace=mapping["registry_namespace"],
            token_id=mapping["token_id"],
            token_version=mapping["token_version"],
            activation_nonce=mapping["activation_nonce"],
            artifact_id=mapping["artifact_id"],
            artifact_seal_digest=mapping["artifact_seal_digest"],
            sealed_stage_digest=mapping["sealed_stage_digest"],
            resource_id=mapping["resource_id"],
            target_identity=mapping["target_identity"],
            prior_authority_digest=mapping["prior_authority_digest"],
            prior_canonical_store_id=mapping["prior_canonical_store_id"],
            prior_generation=mapping["prior_generation"],
            prior_active_content_attestation=(
                _portable_active_content_attestation_from_mapping(
                    mapping["prior_active_content_attestation"]
                )
            ),
            backup_proofs=tuple(
                _portable_replacement_backup_from_mapping(item)
                for item in backup_mappings
            ),
            candidate_canonical_store_id=mapping["candidate_canonical_store_id"],
            next_generation=mapping["next_generation"],
            gate_b_grant_digest=mapping["gate_b_grant_digest"],
            evidence_digest=mapping["evidence_digest"],
            source_jsonl_digest=mapping["source_jsonl_digest"],
            new_receipt_id=mapping["new_receipt_id"],
            new_manifest_digest=mapping["new_manifest_digest"],
            candidate_stage_db_name=mapping["candidate_stage_db_name"],
            candidate_manifest_temp_name=mapping["candidate_manifest_temp_name"],
            canonical_database_name=mapping["canonical_database_name"],
            canonical_manifest_name=mapping["canonical_manifest_name"],
            private_directory_name=mapping["private_directory_name"],
            device_key_name=mapping["device_key_name"],
            lock_payload_digest=mapping["lock_payload_digest"],
            sealed_content_attestation=(
                _portable_sealed_content_attestation_from_mapping(
                    mapping["sealed_content_attestation"]
                )
            ),
            active_content_attestation=(
                None
                if active_mapping is None
                else _portable_active_content_attestation_from_mapping(active_mapping)
            ),
        )
        record = _PortableReplacementRecord(
            unsigned=unsigned,
            private_directory_proof=_portable_activation_proof_from_mapping(
                mapping["private_directory_proof"]
            ),
            record_digest=mapping["record_digest"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.REPLACEMENT_PARSE_INVALID",
            retryable=False,
        ) from error
    if _serialize_portable_replacement_record(record) != serialized:
        raise ActivationPreparationError(
            "ACTIVATION.REPLACEMENT_PARSE_INVALID",
            retryable=False,
        )
    return record


def _portable_replacement_records_follow(
    predecessor: _PortableReplacementRecord,
    successor: _PortableReplacementRecord,
) -> bool:
    if (
        type(predecessor) is not _PortableReplacementRecord
        or type(successor) is not _PortableReplacementRecord
    ):
        return False
    try:
        predecessor_index = _PORTABLE_REPLACEMENT_PHASES.index(
            predecessor.unsigned.phase
        )
    except ValueError:
        return False
    if predecessor_index + 1 >= len(_PORTABLE_REPLACEMENT_PHASES):
        return False
    if successor.unsigned.phase != _PORTABLE_REPLACEMENT_PHASES[
        predecessor_index + 1
    ]:
        return False
    expected = replace(
        predecessor.unsigned,
        phase=successor.unsigned.phase,
        predecessor_digest=predecessor.record_digest,
        active_content_attestation=successor.unsigned.active_content_attestation,
    )
    return successor.unsigned == expected


def _portable_replacement_current_binds_pending(
    current: _PortableReplacementRecord,
    prepared: _PortableReplacementRecord,
) -> bool:
    if (
        type(current) is not _PortableReplacementRecord
        or current.unsigned.phase != "READY"
        or type(prepared) is not _PortableReplacementRecord
        or prepared.unsigned.phase != "PREPARED"
    ):
        return False
    current_active = current.unsigned.active_content_attestation
    prepared_unsigned = prepared.unsigned
    return (
        prepared_unsigned.predecessor_digest == current.record_digest
        and prepared_unsigned.prior_authority_digest == current.record_digest
        and prepared_unsigned.resource_id == current.unsigned.resource_id
        and prepared_unsigned.target_identity == current.unsigned.target_identity
        and prepared_unsigned.prior_canonical_store_id
        == current.unsigned.candidate_canonical_store_id
        and prepared_unsigned.prior_generation == current.unsigned.next_generation
        and prepared_unsigned.prior_active_content_attestation == current_active
        and prepared_unsigned.private_directory_name
        == current.unsigned.private_directory_name
        and prepared_unsigned.device_key_name == current.unsigned.device_key_name
    )


@dataclass(frozen=True, slots=True, init=False)
class _PortableReplacementNamespaceSnapshot:
    """Code-only classification of current READY plus at most one pending chain."""

    state: str
    highest_pending_phase: str | None
    current_record: _PortableReplacementRecord | None = field(repr=False)
    pending_records: tuple[_PortableReplacementRecord, ...] = field(repr=False)
    backup_proofs: tuple[
        _PortableReplacementBackupProof,
        _PortableReplacementBackupProof,
    ] | tuple[()] = field(repr=False)

    def __init__(
        self,
        *,
        state: str,
        current_record: _PortableReplacementRecord | None,
        pending_records: tuple[_PortableReplacementRecord, ...],
        backup_proofs: tuple[
            _PortableReplacementBackupProof,
            _PortableReplacementBackupProof,
        ] | tuple[()],
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_REPLACEMENT_SNAPSHOT_FACTORY_KEY:
            raise TypeError("portable replacement snapshot requires owner factory")
        if state not in {"EMPTY", "CURRENT", "PENDING", "READY_CLEANUP"}:
            raise ValueError("portable replacement namespace state is invalid")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "current_record", current_record)
        object.__setattr__(self, "pending_records", pending_records)
        object.__setattr__(self, "backup_proofs", backup_proofs)
        object.__setattr__(
            self,
            "highest_pending_phase",
            None if not pending_records else pending_records[-1].unsigned.phase,
        )

    def __reduce__(self) -> object:
        raise TypeError("portable replacement snapshot is code-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("portable replacement snapshot is code-only")


def _parse_portable_replacement_namespace(
    records: dict[str, _PortableReplacementRecord],
    backups: dict[str, PortableContentFileProof],
    *,
    base_authority_digest: str | None,
) -> _PortableReplacementNamespaceSnapshot:
    """Classify an already authenticated, bounded replacement subnamespace."""

    if type(records) is not dict or any(
        type(name) is not str or type(record) is not _PortableReplacementRecord
        for name, record in records.items()
    ):
        raise TypeError("portable replacement records are invalid")
    if type(backups) is not dict or any(
        type(name) is not str or type(proof) is not PortableContentFileProof
        for name, proof in backups.items()
    ):
        raise TypeError("portable replacement backups are invalid")
    if base_authority_digest is not None:
        _require_portable_activation_digest(
            base_authority_digest,
            "base_authority_digest",
        )
    if not set(records) <= _PORTABLE_REPLACEMENT_RECORD_NAMES:
        raise ValueError("portable replacement namespace contains foreign records")
    for name, record in records.items():
        if _portable_replacement_phase_name(record.unsigned.phase) != name:
            raise ValueError("portable replacement record occupies the wrong name")

    current = records.get(_PORTABLE_REPLACEMENT_CURRENT_NAME)
    pending_names = tuple(
        _portable_replacement_phase_name(phase)
        for phase in _PORTABLE_REPLACEMENT_PHASES[:-1]
    )
    presence = tuple(name in records for name in pending_names)
    pending_count = 0
    while pending_count < len(presence) and presence[pending_count]:
        pending_count += 1
    if any(presence[pending_count:]):
        raise ValueError("portable replacement pending phases contain a gap")
    pending = tuple(records[name] for name in pending_names[:pending_count])
    if current is not None and current.unsigned.phase != "READY":
        raise ValueError("portable replacement current record is not READY")
    for predecessor, successor in zip(pending, pending[1:]):
        if not _portable_replacement_records_follow(predecessor, successor):
            raise ValueError("portable replacement pending chain does not close")

    if not pending:
        if backups:
            raise ValueError("portable replacement completed namespace retains backups")
        state = "EMPTY" if current is None else "CURRENT"
        return _PortableReplacementNamespaceSnapshot(
            state=state,
            current_record=current,
            pending_records=(),
            backup_proofs=(),
            _factory_key=_PORTABLE_REPLACEMENT_SNAPSHOT_FACTORY_KEY,
        )

    prepared = pending[0]
    expected_backups = {
        backup.backup_name: backup.content
        for backup in prepared.unsigned.backup_proofs
    }
    if backups != expected_backups:
        raise ValueError("portable replacement durable backups do not close")

    same_operation_current = (
        current is not None
        and current.unsigned.preparation_id == prepared.unsigned.preparation_id
    )
    if same_operation_current:
        if len(pending) != len(_PORTABLE_REPLACEMENT_PHASES) - 1 or not (
            _portable_replacement_records_follow(pending[-1], current)
        ):
            raise ValueError("portable replacement READY cleanup is incomplete")
        state = "READY_CLEANUP"
    else:
        if current is None:
            if (
                base_authority_digest is None
                or prepared.unsigned.predecessor_digest != base_authority_digest
                or prepared.unsigned.prior_authority_digest
                != base_authority_digest
            ):
                raise ValueError("portable replacement base authority does not bind")
        elif not _portable_replacement_current_binds_pending(current, prepared):
            raise ValueError("portable replacement current authority does not bind")
        state = "PENDING"
    return _PortableReplacementNamespaceSnapshot(
        state=state,
        current_record=current,
        pending_records=pending,
        backup_proofs=prepared.unsigned.backup_proofs,
        _factory_key=_PORTABLE_REPLACEMENT_SNAPSHOT_FACTORY_KEY,
    )


def _portable_replacement_namespace_limits(
    backup_proofs: tuple[
        _PortableReplacementBackupProof,
        _PortableReplacementBackupProof,
    ],
) -> LedgerEnumerationLimits:
    if type(backup_proofs) is not tuple or len(backup_proofs) != 2 or any(
        type(proof) is not _PortableReplacementBackupProof
        for proof in backup_proofs
    ):
        raise TypeError("portable replacement backup proofs are invalid")
    return LedgerEnumerationLimits(
        maximum_entries=len(_PORTABLE_REPLACEMENT_RECORD_NAMES) + 2,
        maximum_name_bytes=4096,
        maximum_total_bytes=(
            _PORTABLE_REPLACEMENT_MAX_BYTES
            * len(_PORTABLE_REPLACEMENT_RECORD_NAMES)
            + sum(proof.content.size for proof in backup_proofs)
        ),
    )


_PORTABLE_JOURNAL_BORROW_FACTORY_KEY = object()
_PORTABLE_JOURNAL_HANDLE_FACTORY_KEY = object()
_PORTABLE_KEY_CANDIDATE_SUFFIX = ".candidate"
_PORTABLE_JOURNAL_CANDIDATE_SUFFIX = ".candidate"
_PORTABLE_ACTIVATION_QUARANTINE_ROOT = ".localcat-activation-quarantine-v1"
_PORTABLE_ACTIVATION_PRIVATE_LIMITS = LedgerEnumerationLimits(
    maximum_entries=4,
    maximum_name_bytes=4096,
    maximum_total_bytes=_PORTABLE_ACTIVATION_JOURNAL_MAX_BYTES * 2
    + DEVICE_SECRET_SIZE_BYTES,
)
_PORTABLE_FRESH_RECOVERY_PRIVATE_LIMITS = LedgerEnumerationLimits(
    maximum_entries=16,
    maximum_name_bytes=4096,
    maximum_total_bytes=(
        _PORTABLE_ACTIVATION_JOURNAL_MAX_BYTES * 2
        + _PORTABLE_PUBLICATION_MAX_BYTES * 6
        + DEVICE_SECRET_SIZE_BYTES
    ),
)


def _portable_activation_private_directory_name(
    identity: CanonicalResourceIdentity,
) -> str:
    if type(identity) is not CanonicalResourceIdentity:
        raise TypeError("portable journal identity is invalid")
    return f".localcat-activation-private-v1.{identity.target_identity}"


def _portable_activation_quarantine_name(
    unsigned: _PortableActivationJournalUnsigned,
) -> str:
    """Derive one bounded retirement namespace from stable owner facts."""

    if type(unsigned) is not _PortableActivationJournalUnsigned:
        raise TypeError("portable journal unsigned facts are invalid")
    material = _portable_activation_canonical_json(
        {
            "candidate_manifest_temp_name": unsigned.candidate_manifest_temp_name,
            "candidate_stage_db_name": unsigned.candidate_stage_db_name,
            "journal_id": unsigned.journal_id,
            "preparation_id": unsigned.preparation_id,
            "resource_id": unsigned.resource_id,
            "target_identity": unsigned.target_identity,
        }
    )
    return f"portable-{hashlib.sha256(material).hexdigest()}"


def _portable_activation_paired_unsigned(
    pending: _PortableActivationJournalUnsigned,
    cancelled: _PortableActivationJournalUnsigned,
) -> bool:
    if (
        type(pending) is not _PortableActivationJournalUnsigned
        or type(cancelled) is not _PortableActivationJournalUnsigned
        or pending.closure != "PENDING"
        or cancelled.closure != "CANCELLED"
    ):
        return False
    return replace(cancelled, closure="PENDING") == pending


class _CallerHeldPortableJournalBorrow:
    """Non-closing view of the initial-activation root and W1 lease."""

    __slots__ = (
        "__backend",
        "__claimed",
        "__identity",
        "__lease",
        "__lock_name",
        "__lock_payload",
        "__root",
    )

    def __init__(
        self,
        *,
        identity: CanonicalResourceIdentity,
        backend: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        lock_name: str,
        lock_payload: bytes,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_JOURNAL_BORROW_FACTORY_KEY:
            raise TypeError("portable journal borrow requires the owner factory")
        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable journal identity is invalid")
        if not isinstance(backend, PlatformFileBackend):
            raise TypeError("portable journal backend is invalid")
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("portable journal root is invalid")
        if not isinstance(lease, LockLease):
            raise TypeError("portable journal lease is invalid")
        if type(lock_name) is not str or not lock_name:
            raise TypeError("portable journal lock name is invalid")
        if type(lock_payload) is not bytes or not lock_payload:
            raise TypeError("portable journal lock payload is invalid")
        self.__identity = identity
        self.__backend = backend
        self.__claimed = False
        self.__root = root
        self.__lease = lease
        self.__lock_name = lock_name
        self.__lock_payload = lock_payload

    def reprove(
        self,
        backend: PlatformFileBackend,
        identity: CanonicalResourceIdentity,
    ) -> None:
        if backend is not self.__backend or identity != self.__identity:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        self.__lease.reprove_binding(
            self.__root,
            self.__lock_name,
            self.__lock_payload,
        )
        self.__root.reprove()

    def _authorities(
        self,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        identity: CanonicalResourceIdentity,
    ) -> tuple[RootedDirectoryAuthority, LockLease]:
        if self.__claimed:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        self.reprove(backend, identity)
        if persistent_private is not backend:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        self.__claimed = True
        return self.__root, self.__lease

    def _publication_authorities(
        self,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        identity: CanonicalResourceIdentity,
    ) -> tuple[RootedDirectoryAuthority, LockLease]:
        """Reborrow the same live owner root/lease after PREPARED was claimed."""

        if not self.__claimed:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        self.reprove(backend, identity)
        if persistent_private is not backend:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        return self.__root, self.__lease

    def _fresh_recovery_authorities(
        self,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        identity: CanonicalResourceIdentity,
    ) -> tuple[RootedDirectoryAuthority, LockLease]:
        """Claim once, then reborrow only for the same fresh recovery owner."""

        self.reprove(backend, identity)
        if persistent_private is not backend:
            raise PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        self.__claimed = True
        return self.__root, self.__lease

    def lock_payload_digest(self) -> str:
        return hashlib.sha256(self.__lock_payload).hexdigest()

    def __reduce__(self) -> object:
        raise TypeError("portable journal borrow is non-serializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("portable journal borrow is non-serializable")

    def __copy__(self) -> object:
        raise TypeError("portable journal borrow is non-copyable")

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise TypeError("portable journal borrow is non-copyable")


def _create_caller_held_portable_journal_borrow(
    *,
    identity: CanonicalResourceIdentity,
    backend: PlatformFileBackend,
    root: RootedDirectoryAuthority,
    lease: LockLease,
    lock_name: str,
    lock_payload: bytes,
) -> _CallerHeldPortableJournalBorrow:
    if not isinstance(backend, PersistentPrivateProof):
        raise PlatformFileError(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            retryable=False,
        )
    return _CallerHeldPortableJournalBorrow(
        identity=identity,
        backend=backend,
        root=root,
        lease=lease,
        lock_name=lock_name,
        lock_payload=lock_payload,
        _factory_key=_PORTABLE_JOURNAL_BORROW_FACTORY_KEY,
    )


@dataclass(frozen=True, slots=True, init=False)
class _PortablePreparedJournalHandle:
    """Code-only proof that one exact v3 PREPARED record reached terminal reproof."""

    preparation_id: str
    record_digest: str
    private_directory_name: str
    journal_name: str
    _record: _PortableActivationJournalRecord = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        record: _PortableActivationJournalRecord,
        *,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_JOURNAL_HANDLE_FACTORY_KEY:
            raise TypeError("portable journal handle requires the owner factory")
        if type(record) is not _PortableActivationJournalRecord:
            raise TypeError("portable journal record is invalid")
        object.__setattr__(self, "preparation_id", record.unsigned.preparation_id)
        object.__setattr__(self, "record_digest", record.record_digest)
        object.__setattr__(
            self,
            "private_directory_name",
            record.unsigned.private_directory_name,
        )
        object.__setattr__(self, "journal_name", record.unsigned.journal_name)
        object.__setattr__(self, "_record", record)


@dataclass(frozen=True, slots=True, init=False)
class _PortableCancelledJournalHandle:
    """Code-only proof of a durable v3 CANCELLED terminal and retirement."""

    preparation_id: str
    record_digest: str
    private_directory_name: str
    terminal_name: str
    quarantine_name: str
    _record: _PortableActivationJournalRecord = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        record: _PortableActivationJournalRecord,
        *,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_JOURNAL_HANDLE_FACTORY_KEY:
            raise TypeError("portable cancelled handle requires the owner factory")
        if (
            type(record) is not _PortableActivationJournalRecord
            or record.unsigned.closure != "CANCELLED"
        ):
            raise TypeError("portable cancelled record is invalid")
        object.__setattr__(self, "preparation_id", record.unsigned.preparation_id)
        object.__setattr__(self, "record_digest", record.record_digest)
        object.__setattr__(
            self,
            "private_directory_name",
            record.unsigned.private_directory_name,
        )
        object.__setattr__(self, "terminal_name", record.unsigned.terminal_name)
        object.__setattr__(
            self,
            "quarantine_name",
            _portable_activation_quarantine_name(record.unsigned),
        )
        object.__setattr__(self, "_record", record)


def _portable_activation_owner_error(
    error: PlatformFileError | OSError,
    *,
    namespace_armed: bool,
    publication_armed: bool,
) -> ActivationPreparationError:
    if isinstance(error, PlatformFileError):
        if (
            error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value
            or namespace_armed
            or publication_armed
        ):
            return ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        if error.code in {
            PlatformFileErrorCode.ENTRY_UNAVAILABLE.value,
            PlatformFileErrorCode.OUTSIDE_ROOT.value,
            PlatformFileErrorCode.REPARSE_REJECTED.value,
            PlatformFileErrorCode.IDENTITY_STALE.value,
            PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
        }:
            return ActivationPreparationError(
                "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                retryable=False,
            )
        if (
            error.code == PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value
            and not namespace_armed
            and not publication_armed
        ):
            return ActivationPreparationError(
                "ACTIVATION.DURABILITY_UNAVAILABLE",
                retryable=False,
            )
    if namespace_armed or publication_armed:
        return ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )
    return ActivationPreparationError(
        "ACTIVATION.DURABILITY_UNAVAILABLE",
        retryable=False,
    )


def _close_portable_journal_authorities(
    authorities: list[object],
) -> BaseException | None:
    first_error: BaseException | None = None
    for authority in reversed(authorities):
        close = getattr(authority, "close", None)
        if close is None:
            if first_error is None:
                first_error = TypeError("portable journal authority cannot close")
            continue
        try:
            close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    return first_error


def _unlink_unpublished_portable_candidate(
    parent: BoundDirectoryAuthority,
    name: str,
    candidate: CandidateFile,
) -> None:
    identity = candidate.identity()
    close_error: BaseException | None = None
    try:
        candidate.close()
    except BaseException as error:
        close_error = error
    try:
        parent.unlink_owned(name, identity)
    except BaseException as error:
        if close_error is None:
            close_error = error
    if close_error is not None:
        raise close_error


class _WindowsPortablePreparedJournalOwner:
    """One-shot W2 owner for a private device key and durable v3 PREPARED."""

    @staticmethod
    def publish(
        *,
        identity: CanonicalResourceIdentity,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection | None = None,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        unsigned: _PortableActivationJournalUnsigned,
        owner_reprove: Callable[[], None],
        owner_commit: Callable[[_PortableActivationJournalRecord], None],
    ) -> _PortablePreparedJournalHandle:
        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable journal identity is invalid")
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable journal borrow is invalid")
        if type(unsigned) is not _PortableActivationJournalUnsigned:
            raise TypeError("portable journal unsigned record is invalid")
        if not callable(owner_reprove):
            raise TypeError("portable journal owner reproof must be callable")
        if not callable(owner_commit):
            raise TypeError("portable journal owner commit must be callable")
        if descendant_inspection is not None and not isinstance(
            descendant_inspection,
            LockedDescendantNamespaceInspection,
        ):
            raise TypeError("portable journal descendant inspection is invalid")
        if unsigned.resource_id != identity.resource_id or (
            unsigned.target_identity != identity.target_identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        expected_private_directory_name = (
            _portable_activation_private_directory_name(identity)
        )
        if (
            unsigned.private_directory_name != expected_private_directory_name
            or unsigned.device_key_name != "device.key"
            or unsigned.journal_name != "activation-journal-v3.json"
            or unsigned.terminal_name != "activation-terminal-v3.json"
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )

        namespace_armed = False
        publication_armed = False
        authorities: list[object] = []
        key_candidate: CandidateFile | None = None
        journal_candidate: CandidateFile | None = None
        key_pending: PendingPublication | None = None
        journal_pending: PendingPublication | None = None
        owner_parent: BoundDirectoryAuthority | None = None
        private_parent: BoundDirectoryAuthority | None = None
        active_error: BaseException | None = None
        result: _PortablePreparedJournalHandle | None = None
        try:
            root, lease = caller_borrow._authorities(
                backend,
                persistent_private,
                identity,
            )
            if unsigned.lock_payload_digest != caller_borrow.lock_payload_digest():
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            owner_reprove()
            owner_parent = backend.bind_parent(
                root,
                PurePath(unsigned.private_directory_name),
            )
            authorities.append(owner_parent)
            private_snapshot = owner_parent.inspect_entry(
                unsigned.private_directory_name
            )
            reuse_device_key = private_snapshot is not None
            if reuse_device_key:
                if (
                    private_snapshot.identity.kind != "directory"
                    or not private_snapshot.reparse_free
                    or descendant_inspection is not backend
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                private_parent = backend.bind_parent(
                    root,
                    PurePath(
                        unsigned.private_directory_name,
                        "prepared-reuse-placeholder",
                    ),
                )
                try:
                    key_only = descendant_inspection.observe_descendant_entries(
                        root,
                        lease,
                        private_parent,
                        LedgerEnumerationLimits(
                            maximum_entries=1,
                            maximum_name_bytes=4096,
                            maximum_total_bytes=DEVICE_SECRET_SIZE_BYTES,
                        ),
                    )
                except (PlatformFileError, OSError, ValueError) as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from error
                if {entry.name for entry in key_only} != {
                    unsigned.device_key_name
                }:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
            else:
                private_parent = backend.create_private_directory(
                    owner_parent,
                    unsigned.private_directory_name,
                )
                namespace_armed = True
            authorities.append(private_parent)
            private_evidence = backend.prove_private(private_parent)
            authorities.append(private_evidence)
            caller_borrow.reprove(backend, identity)

            key_candidate_name = (
                unsigned.device_key_name + _PORTABLE_KEY_CANDIDATE_SUFFIX
            )
            journal_candidate_name = (
                unsigned.journal_name + _PORTABLE_JOURNAL_CANDIDATE_SUFFIX
            )
            for residue_name in (
                key_candidate_name,
                journal_candidate_name,
                unsigned.journal_name,
                unsigned.terminal_name,
            ):
                if private_parent.inspect_entry(residue_name) is not None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )

            if reuse_device_key:
                key_file = backend.open_regular(
                    root,
                    PurePath(
                        unsigned.private_directory_name,
                        unsigned.device_key_name,
                    ),
                )
                authorities.append(key_file)
                key_bytes = key_file.read_all()
            else:
                if private_parent.inspect_entry(unsigned.device_key_name) is not None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                key_bytes = os.urandom(DEVICE_SECRET_SIZE_BYTES)
                key_candidate = private_parent.create_candidate(
                    key_candidate_name,
                    private=True,
                )
                key_candidate.write_all(key_bytes)
                key_candidate.flush_content()
                caller_borrow.reprove(backend, identity)
                owner_reprove()
                key_candidate_identity = key_candidate.identity()
                try:
                    key_pending = private_parent.begin_publish(
                        key_candidate,
                        unsigned.device_key_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                except (PlatformFileError, OSError) as error:
                    key_candidate = None
                    if (
                        isinstance(error, PlatformFileError)
                        and error.code in {
                            PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
                            PlatformFileErrorCode.PUBLISH_FAILED.value,
                        }
                    ):
                        try:
                            private_parent.unlink_owned(
                                key_candidate_name,
                                key_candidate_identity,
                            )
                        except (PlatformFileError, OSError):
                            namespace_armed = True
                    raise
                key_candidate = None
                publication_armed = True
                authorities.append(key_pending)
                key_file = key_pending.retained_destination()

            key_identity = key_file.identity()
            if (
                key_identity.kind != "regular"
                or key_identity.link_count != 1
                or len(key_bytes) != DEVICE_SECRET_SIZE_BYTES
                or key_file.read_all() != key_bytes
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            key_evidence = backend.prove_private(key_file)
            authorities.append(key_evidence)
            secret = persistent_private.bind_device_secret(key_file)
            authorities.append(secret)
            secret.reprove()
            caller_borrow.reprove(backend, identity)

            context = PrivateProofContext(
                PrivateProofObjectRole.PRIVATE_DIRECTORY,
                _portable_activation_owner_context_sha256(unsigned),
            )
            proof = persistent_private.mint(
                private_evidence,
                secret,
                context,
            )
            record = _create_portable_activation_journal_record(unsigned, proof)
            expected_bytes = _serialize_portable_activation_journal_record(record)

            journal_candidate = private_parent.create_candidate(
                journal_candidate_name,
                private=True,
            )
            journal_candidate.write_all(expected_bytes)
            journal_candidate.flush_content()
            caller_borrow.reprove(backend, identity)
            owner_reprove()
            journal_candidate_identity = journal_candidate.identity()
            try:
                journal_pending = private_parent.begin_publish(
                    journal_candidate,
                    unsigned.journal_name,
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            except (PlatformFileError, OSError) as error:
                journal_candidate = None
                if (
                    isinstance(error, PlatformFileError)
                    and error.code in {
                        PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
                        PlatformFileErrorCode.PUBLISH_FAILED.value,
                    }
                ):
                    try:
                        private_parent.unlink_owned(
                            journal_candidate_name,
                            journal_candidate_identity,
                        )
                    except (PlatformFileError, OSError):
                        publication_armed = True
                raise
            journal_candidate = None
            publication_armed = True
            authorities.append(journal_pending)
            journal_file = journal_pending.retained_destination()
            journal_identity = journal_file.identity()
            if (
                journal_identity.kind != "regular"
                or journal_identity.link_count != 1
                or journal_file.read_all() != expected_bytes
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            journal_evidence = backend.prove_private(journal_file)
            authorities.append(journal_evidence)
            disk_record = _parse_portable_activation_journal_bytes(
                journal_file.read_all()
            )
            verified = persistent_private.verify(
                private_evidence,
                secret,
                disk_record.private_directory_proof,
                context,
            )
            authorities.append(verified)
            caller_borrow.reprove(backend, identity)
            owner_reprove()
            owner_result = owner_commit(disk_record)
            if owner_result is not None:
                raise TypeError("portable journal owner commit must return None")

            journal_evidence.close()
            authorities.remove(journal_evidence)
            owner_reprove()
            caller_borrow.reprove(backend, identity)
            terminal_journal_bytes = journal_file.read_all()
            if terminal_journal_bytes != expected_bytes or (
                _parse_portable_activation_journal_bytes(terminal_journal_bytes)
                != disk_record
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            fresh_journal_evidence = backend.prove_private(journal_file)
            authorities.append(fresh_journal_evidence)
            if (
                journal_pending.terminal_reproof()
                != journal_pending.preliminary_facts()
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            journal_pending.close()
            authorities.remove(journal_pending)
            journal_pending = None
            caller_borrow.reprove(backend, identity)

            if key_pending is not None:
                key_evidence.close()
                authorities.remove(key_evidence)
                owner_reprove()
                caller_borrow.reprove(backend, identity)
                terminal_key_identity = key_file.identity()
                if (
                    terminal_key_identity.kind != "regular"
                    or terminal_key_identity.link_count != 1
                    or key_file.read_all() != key_bytes
                    or len(key_bytes) != DEVICE_SECRET_SIZE_BYTES
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                fresh_key_evidence = backend.prove_private(key_file)
                authorities.append(fresh_key_evidence)
                secret.reprove()
                if key_pending.terminal_reproof() != key_pending.preliminary_facts():
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                key_pending.close()
                authorities.remove(key_pending)
                key_pending = None
            caller_borrow.reprove(backend, identity)
            persistent_private.consume_verified(verified, context)
            authorities.remove(verified)
            result = _PortablePreparedJournalHandle(
                disk_record,
                _factory_key=_PORTABLE_JOURNAL_HANDLE_FACTORY_KEY,
            )
        except ActivationPreparationError as error:
            active_error = (
                ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
                if namespace_armed or publication_armed
                else error
            )
        except (PlatformFileError, OSError) as error:
            active_error = _portable_activation_owner_error(
                error,
                namespace_armed=namespace_armed,
                publication_armed=publication_armed,
            )
        except BaseException as error:
            active_error = error
        finally:
            for candidate, name in (
                (journal_candidate, None if journal_candidate is None else unsigned.journal_name + _PORTABLE_JOURNAL_CANDIDATE_SUFFIX),
                (key_candidate, None if key_candidate is None else unsigned.device_key_name + _PORTABLE_KEY_CANDIDATE_SUFFIX),
            ):
                if candidate is not None and name is not None:
                    try:
                        if private_parent is None:
                            raise AssertionError(
                                "portable candidate has no private parent"
                            )
                        _unlink_unpublished_portable_candidate(
                            private_parent,
                            name,
                            candidate,
                        )
                    except BaseException as cleanup_error:
                        if active_error is None:
                            active_error = cleanup_error
                        elif isinstance(active_error, (PlatformFileError, OSError)):
                            active_error = ActivationPreparationError(
                                "ACTIVATION.RECOVERY_REQUIRED",
                                retryable=True,
                            )
            close_error = _close_portable_journal_authorities(authorities)
            if active_error is None and close_error is not None:
                if isinstance(close_error, (PlatformFileError, OSError)):
                    active_error = ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                else:
                    active_error = close_error
        if active_error is not None:
            raise active_error
        if result is None:
            raise AssertionError("portable journal owner produced no handle")
        return result


_PORTABLE_PUBLICATION_HANDLE_FACTORY_KEY = object()
_PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY = object()
_PORTABLE_PUBLICATION_CANDIDATE_SUFFIX = ".candidate"


def _portable_publication_phase_name(phase: str) -> str:
    try:
        return _PORTABLE_PUBLICATION_PHASE_NAMES[phase]
    except (KeyError, TypeError) as error:
        raise ValueError("portable publication phase is invalid") from error


def _portable_publication_chain_predecessor(
    prepared: _PortableActivationJournalRecord,
    predecessor: _PortableActivationJournalRecord | _PortablePublicationPhaseRecord,
    unsigned: _PortablePublicationPhaseUnsigned,
) -> tuple[str, bytes, WindowsPrivateProof, bytes]:
    if (
        type(prepared) is not _PortableActivationJournalRecord
        or prepared.unsigned.closure != "PENDING"
        or prepared.unsigned.phase != "PREPARED"
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
            retryable=False,
        )
    prepared_unsigned = prepared.unsigned
    if (
        unsigned.journal_id != prepared_unsigned.journal_id
        or unsigned.preparation_id != prepared_unsigned.preparation_id
        or unsigned.token_id != prepared_unsigned.token_id
        or unsigned.token_version != prepared_unsigned.token_version
        or unsigned.activation_nonce != prepared_unsigned.activation_nonce
        or unsigned.resource_id != prepared_unsigned.resource_id
        or unsigned.target_identity != prepared_unsigned.target_identity
        or unsigned.canonical_store_id != prepared_unsigned.canonical_store_id
        or unsigned.private_directory_name
        != prepared_unsigned.private_directory_name
        or unsigned.device_key_name != prepared_unsigned.device_key_name
        or unsigned.sealed_content_attestation
        != prepared_unsigned.sealed_content_attestation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
            retryable=False,
        )
    phase_index = _PORTABLE_PUBLICATION_PHASES.index(unsigned.phase)
    if phase_index == 0:
        if type(predecessor) is not _PortableActivationJournalRecord or (
            predecessor != prepared
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                retryable=False,
            )
        predecessor_name = prepared_unsigned.journal_name
        predecessor_bytes = _serialize_portable_activation_journal_record(prepared)
        predecessor_proof = prepared.private_directory_proof
        predecessor_context = _portable_activation_owner_context_sha256(
            prepared_unsigned
        )
        predecessor_digest = prepared.record_digest
    else:
        if type(predecessor) is not _PortablePublicationPhaseRecord:
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                retryable=False,
            )
        expected_phase = _PORTABLE_PUBLICATION_PHASES[phase_index - 1]
        predecessor_unsigned = predecessor.unsigned
        if (
            predecessor_unsigned.phase != expected_phase
            or predecessor_unsigned.journal_id != unsigned.journal_id
            or predecessor_unsigned.preparation_id != unsigned.preparation_id
            or predecessor_unsigned.token_id != unsigned.token_id
            or predecessor_unsigned.token_version != unsigned.token_version
            or predecessor_unsigned.activation_nonce != unsigned.activation_nonce
            or predecessor_unsigned.resource_id != unsigned.resource_id
            or predecessor_unsigned.target_identity != unsigned.target_identity
            or predecessor_unsigned.canonical_store_id
            != unsigned.canonical_store_id
            or predecessor_unsigned.generation != unsigned.generation
            or predecessor_unsigned.canonical_database_name
            != unsigned.canonical_database_name
            or predecessor_unsigned.canonical_manifest_name
            != unsigned.canonical_manifest_name
            or predecessor_unsigned.private_directory_name
            != unsigned.private_directory_name
            or predecessor_unsigned.device_key_name != unsigned.device_key_name
            or predecessor_unsigned.sealed_content_attestation
            != unsigned.sealed_content_attestation
            or (
                unsigned.phase == "GENERATION_PUBLISHED"
                and predecessor_unsigned.active_content_attestation
                != unsigned.active_content_attestation
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                retryable=False,
            )
        predecessor_name = _portable_publication_phase_name(expected_phase)
        predecessor_bytes = _serialize_portable_publication_phase_record(predecessor)
        predecessor_proof = predecessor.private_directory_proof
        predecessor_context = _portable_publication_owner_context_sha256(
            predecessor_unsigned
        )
        predecessor_digest = predecessor.record_digest
    if unsigned.predecessor_digest != predecessor_digest:
        raise ActivationPreparationError(
            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
            retryable=False,
        )
    return (
        predecessor_name,
        predecessor_bytes,
        predecessor_proof,
        predecessor_context,
    )


@dataclass(frozen=True, slots=True, init=False)
class _PortablePublicationPhaseHandle:
    phase: str
    phase_name: str
    predecessor_digest: str
    record_digest: str
    _record: _PortablePublicationPhaseRecord = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        record: _PortablePublicationPhaseRecord,
        *,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_PUBLICATION_HANDLE_FACTORY_KEY:
            raise TypeError("portable publication handle requires owner factory")
        if type(record) is not _PortablePublicationPhaseRecord:
            raise TypeError("portable publication record is invalid")
        object.__setattr__(self, "phase", record.unsigned.phase)
        object.__setattr__(
            self,
            "phase_name",
            _portable_publication_phase_name(record.unsigned.phase),
        )
        object.__setattr__(
            self,
            "predecessor_digest",
            record.unsigned.predecessor_digest,
        )
        object.__setattr__(self, "record_digest", record.record_digest)
        object.__setattr__(self, "_record", record)


@dataclass(frozen=True, slots=True, init=False)
class _PortableFreshRecoverySnapshot:
    """Verified disk facts only; never a reconstructed token or preparation."""

    state: str
    namespace_state: str
    highest_phase: str | None
    pending_record: _PortableActivationJournalRecord | None = field(
        repr=False,
    )
    cancelled_record: _PortableActivationJournalRecord | None = field(
        repr=False,
    )
    phase_records: tuple[_PortablePublicationPhaseRecord, ...] = field(
        repr=False,
    )

    def __init__(
        self,
        *,
        state: str,
        namespace_state: str,
        pending_record: _PortableActivationJournalRecord | None,
        cancelled_record: _PortableActivationJournalRecord | None,
        phase_records: tuple[_PortablePublicationPhaseRecord, ...],
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY:
            raise TypeError("portable recovery snapshot requires owner factory")
        if state not in {"NO_FACTS", "PENDING", "CANCELLED"}:
            raise ValueError("portable recovery snapshot state is invalid")
        if namespace_state not in {"ABSENT", "KEY_ONLY", "JOURNAL"}:
            raise ValueError("portable recovery namespace state is invalid")
        if type(phase_records) is not tuple or any(
            type(record) is not _PortablePublicationPhaseRecord
            for record in phase_records
        ):
            raise TypeError("portable recovery phases must be exact records")
        expected_phases = _PORTABLE_PUBLICATION_PHASES[: len(phase_records)]
        if tuple(record.unsigned.phase for record in phase_records) != expected_phases:
            raise ValueError("portable recovery phases must form one prefix")
        if state == "NO_FACTS":
            if (
                namespace_state not in {"ABSENT", "KEY_ONLY"}
                or
                pending_record is not None
                or cancelled_record is not None
                or phase_records
            ):
                raise ValueError("NO_FACTS cannot carry journal records")
        elif state == "PENDING":
            if (
                namespace_state != "JOURNAL"
                or
                type(pending_record) is not _PortableActivationJournalRecord
                or pending_record.unsigned.closure != "PENDING"
                or cancelled_record is not None
            ):
                raise ValueError("PENDING snapshot is not closed")
        elif (
            namespace_state != "JOURNAL"
            or
            type(cancelled_record) is not _PortableActivationJournalRecord
            or cancelled_record.unsigned.closure != "CANCELLED"
            or phase_records
        ):
            raise ValueError("CANCELLED snapshot is not closed")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "namespace_state", namespace_state)
        object.__setattr__(
            self,
            "highest_phase",
            None if not phase_records else phase_records[-1].unsigned.phase,
        )
        object.__setattr__(self, "pending_record", pending_record)
        object.__setattr__(self, "cancelled_record", cancelled_record)
        object.__setattr__(self, "phase_records", phase_records)

    def __reduce__(self) -> object:
        raise TypeError("portable recovery snapshot is code-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("portable recovery snapshot is code-only")


class _WindowsPortablePublicationPhaseOwner:
    """Write-once owner for one exact portable canonical-publication phase."""

    @staticmethod
    def publish(
        *,
        identity: CanonicalResourceIdentity,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        prepared: _PortableActivationJournalRecord,
        predecessor: (
            _PortableActivationJournalRecord | _PortablePublicationPhaseRecord
        ),
        unsigned: _PortablePublicationPhaseUnsigned,
        owner_reprove: Callable[[], None],
        owner_commit: Callable[[_PortablePublicationPhaseRecord], None],
        business_reprove: Callable[[_PortablePublicationPhaseRecord], None],
    ) -> _PortablePublicationPhaseHandle:
        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable publication identity is invalid")
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable publication borrow is invalid")
        if type(unsigned) is not _PortablePublicationPhaseUnsigned:
            raise TypeError("portable publication unsigned facts are invalid")
        for callback, name in (
            (owner_reprove, "owner reproof"),
            (owner_commit, "owner commit"),
            (business_reprove, "business reproof"),
        ):
            if not callable(callback):
                raise TypeError(f"portable publication {name} must be callable")
        if (
            unsigned.resource_id != identity.resource_id
            or unsigned.target_identity != identity.target_identity
            or unsigned.canonical_database_name
            != identity.canonical_sidecar_path.name
            or unsigned.canonical_manifest_name
            != identity.snapshot_manifest_path.name
            or unsigned.private_directory_name
            != _portable_activation_private_directory_name(identity)
            or unsigned.device_key_name != "device.key"
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                retryable=False,
            )
        (
            predecessor_name,
            predecessor_bytes,
            predecessor_proof,
            predecessor_context_sha256,
        ) = _portable_publication_chain_predecessor(
            prepared,
            predecessor,
            unsigned,
        )

        publication_armed = False
        owner_commit_armed = False
        authorities: list[object] = []
        candidate: CandidateFile | None = None
        pending: PendingPublication | None = None
        active_error: BaseException | None = None
        result: _PortablePublicationPhaseHandle | None = None
        try:
            root, _lease = caller_borrow._publication_authorities(
                backend,
                persistent_private,
                identity,
            )
            caller_borrow.reprove(backend, identity)
            owner_reprove()
            private_parent = backend.bind_parent(
                root,
                PurePath(
                    unsigned.private_directory_name,
                    "publication-placeholder",
                ),
            )
            authorities.append(private_parent)
            private_evidence = backend.prove_private(private_parent)
            authorities.append(private_evidence)
            key_file = backend.open_regular(
                root,
                PurePath(
                    unsigned.private_directory_name,
                    unsigned.device_key_name,
                ),
            )
            authorities.append(key_file)
            key_identity = key_file.identity()
            if (
                key_identity.kind != "regular"
                or key_identity.link_count != 1
                or len(key_file.read_all()) != DEVICE_SECRET_SIZE_BYTES
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            key_evidence = backend.prove_private(key_file)
            authorities.append(key_evidence)
            secret = persistent_private.bind_device_secret(key_file)
            authorities.append(secret)
            secret.reprove()

            try:
                predecessor_file = backend.open_regular(
                    root,
                    PurePath(unsigned.private_directory_name, predecessor_name),
                )
            except (PlatformFileError, OSError) as error:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error
            authorities.append(predecessor_file)
            predecessor_identity = predecessor_file.identity()
            if (
                predecessor_identity.kind != "regular"
                or predecessor_identity.link_count != 1
                or predecessor_file.read_all() != predecessor_bytes
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            predecessor_evidence = backend.prove_private(predecessor_file)
            authorities.append(predecessor_evidence)
            predecessor_verified = persistent_private.verify(
                private_evidence,
                secret,
                predecessor_proof,
                PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    predecessor_context_sha256,
                ),
            )
            predecessor_verified.close()
            caller_borrow.reprove(backend, identity)
            owner_reprove()

            context = PrivateProofContext(
                PrivateProofObjectRole.PRIVATE_DIRECTORY,
                _portable_publication_owner_context_sha256(unsigned),
            )
            proof = persistent_private.mint(private_evidence, secret, context)
            record = _create_portable_publication_phase_record(unsigned, proof)
            expected_bytes = _serialize_portable_publication_phase_record(record)
            phase_name = _portable_publication_phase_name(unsigned.phase)
            candidate_name = phase_name + _PORTABLE_PUBLICATION_CANDIDATE_SUFFIX
            try:
                candidate_entry = private_parent.inspect_entry(candidate_name)
                existing = private_parent.inspect_entry(phase_name)
            except (PlatformFileError, OSError) as error:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error
            if candidate_entry is not None:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            if existing is not None:
                try:
                    phase_file = backend.open_regular(
                        root,
                        PurePath(unsigned.private_directory_name, phase_name),
                    )
                except (PlatformFileError, OSError) as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from error
                authorities.append(phase_file)
                phase_identity = phase_file.identity()
                if (
                    phase_identity.kind != "regular"
                    or phase_identity.link_count != 1
                    or phase_file.read_all() != expected_bytes
                    or _parse_portable_publication_phase_bytes(
                        phase_file.read_all()
                    )
                    != record
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                phase_evidence = backend.prove_private(phase_file)
                authorities.append(phase_evidence)
                verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    record.private_directory_proof,
                    context,
                )
                authorities.append(verified)
                owner_reprove()
                caller_borrow.reprove(backend, identity)
                owner_commit_armed = True
                if owner_commit(record) is not None:
                    raise TypeError("portable publication owner commit must return None")
                if business_reprove(record) is not None:
                    raise TypeError(
                        "portable publication business reproof must return None"
                    )
                persistent_private.consume_verified(verified, context)
                authorities.remove(verified)
                result = _PortablePublicationPhaseHandle(
                    record,
                    _factory_key=_PORTABLE_PUBLICATION_HANDLE_FACTORY_KEY,
                )
            else:
                candidate = private_parent.create_candidate(
                    candidate_name,
                    private=True,
                )
                publication_armed = True
                candidate.write_all(expected_bytes)
                candidate.flush_content()
                caller_borrow.reprove(backend, identity)
                owner_reprove()
                try:
                    pending = private_parent.begin_publish(
                        candidate,
                        phase_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                except BaseException:
                    candidate.close()
                    candidate = None
                    raise
                candidate = None
                authorities.append(pending)
                phase_file = pending.retained_destination()
                phase_identity = phase_file.identity()
                if (
                    phase_identity.kind != "regular"
                    or phase_identity.link_count != 1
                    or phase_file.read_all() != expected_bytes
                    or _parse_portable_publication_phase_bytes(
                        phase_file.read_all()
                    )
                    != record
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                phase_evidence = backend.prove_private(phase_file)
                authorities.append(phase_evidence)
                verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    record.private_directory_proof,
                    context,
                )
                authorities.append(verified)
                owner_reprove()
                caller_borrow.reprove(backend, identity)
                owner_commit_armed = True
                if owner_commit(record) is not None:
                    raise TypeError("portable publication owner commit must return None")
                if business_reprove(record) is not None:
                    raise TypeError(
                        "portable publication business reproof must return None"
                    )
                phase_evidence.close()
                authorities.remove(phase_evidence)
                owner_reprove()
                caller_borrow.reprove(backend, identity)
                if (
                    phase_file.read_all() != expected_bytes
                    or _parse_portable_publication_phase_bytes(
                        phase_file.read_all()
                    )
                    != record
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                fresh_phase_evidence = backend.prove_private(phase_file)
                authorities.append(fresh_phase_evidence)
                if pending.terminal_reproof() != pending.preliminary_facts():
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                pending.close()
                authorities.remove(pending)
                pending = None
                caller_borrow.reprove(backend, identity)
                persistent_private.consume_verified(verified, context)
                authorities.remove(verified)
                result = _PortablePublicationPhaseHandle(
                    record,
                    _factory_key=_PORTABLE_PUBLICATION_HANDLE_FACTORY_KEY,
                )
        except ActivationPreparationError as error:
            active_error = (
                ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
                if publication_armed or owner_commit_armed
                else error
            )
        except (PlatformFileError, OSError) as error:
            active_error = _portable_activation_owner_error(
                error,
                namespace_armed=False,
                publication_armed=publication_armed or owner_commit_armed,
            )
            active_error.__cause__ = error
        except BaseException as error:
            active_error = error
        finally:
            if candidate is not None:
                try:
                    candidate.close()
                except BaseException as cleanup_error:
                    if active_error is None:
                        active_error = cleanup_error
            close_error = _close_portable_journal_authorities(authorities)
            if active_error is None and close_error is not None:
                if isinstance(close_error, (PlatformFileError, OSError)):
                    active_error = ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                else:
                    active_error = close_error
        if active_error is not None:
            raise active_error
        if result is None:
            raise AssertionError("portable publication owner produced no handle")
        return result


class _WindowsPortableFreshRecoveryOwner:
    """Read and authenticate the bounded portable recovery fact set."""

    @staticmethod
    def inspect(
        *,
        identity: CanonicalResourceIdentity,
        canonical_store_id: str,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> _PortableFreshRecoverySnapshot:
        if sys.platform != "win32":
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        from platform_fs_windows import WindowsPlatformAdapter

        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable recovery identity is invalid")
        _require_portable_activation_string(
            canonical_store_id,
            "canonical_store_id",
        )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable recovery borrow is invalid")
        if type(backend) is not WindowsPlatformAdapter or not (
            persistent_private is backend
            and descendant_inspection is backend
            and isinstance(descendant_inspection, LockedDescendantNamespaceInspection)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )

        authorities: list[object] = []
        active_error: BaseException | None = None
        result: _PortableFreshRecoverySnapshot | None = None

        def recovery_required(error: BaseException | None = None) -> ActivationPreparationError:
            failure = ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
            if error is not None:
                failure.__cause__ = error
            return failure

        try:
            root, lease = caller_borrow._fresh_recovery_authorities(
                backend,
                persistent_private,
                identity,
            )
            private_name = _portable_activation_private_directory_name(identity)
            private_entry = root.inspect_entry(private_name)
            if private_entry is None:
                caller_borrow.reprove(backend, identity)
                result = _PortableFreshRecoverySnapshot(
                    state="NO_FACTS",
                    namespace_state="ABSENT",
                    pending_record=None,
                    cancelled_record=None,
                    phase_records=(),
                    _factory_key=_PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY,
                )
            else:
                if (
                    private_entry.identity.kind != "directory"
                    or not private_entry.reparse_free
                ):
                    raise recovery_required()
                private_parent = backend.bind_parent(
                    root,
                    PurePath(private_name, "fresh-recovery-placeholder"),
                )
                authorities.append(private_parent)
                try:
                    observed_entries = descendant_inspection.observe_descendant_entries(
                        root,
                        lease,
                        private_parent,
                        _PORTABLE_FRESH_RECOVERY_PRIVATE_LIMITS,
                    )
                except (PlatformFileError, OSError, ValueError) as error:
                    raise recovery_required(error)
                observations = {
                    observation.name: observation.snapshot
                    for observation in observed_entries
                }
                phase_names = tuple(
                    _portable_publication_phase_name(phase)
                    for phase in _PORTABLE_PUBLICATION_PHASES
                )
                allowed_names = {
                    "device.key",
                    "activation-journal-v3.json",
                    "activation-terminal-v3.json",
                    *phase_names,
                }
                names = set(observations)
                if not names <= allowed_names or "device.key" not in names:
                    raise recovery_required()
                has_pending = "activation-journal-v3.json" in names
                has_cancelled = "activation-terminal-v3.json" in names
                present_phases = tuple(name in names for name in phase_names)
                phase_count = 0
                while phase_count < len(present_phases) and present_phases[phase_count]:
                    phase_count += 1
                if (
                    any(present_phases[phase_count:])
                    or (phase_count and not has_pending)
                    or (has_cancelled and phase_count)
                ):
                    raise recovery_required()

                private_evidence = backend.prove_private(private_parent)
                authorities.append(private_evidence)
                key_file = backend.open_regular(
                    root,
                    PurePath(private_name, "device.key"),
                )
                authorities.append(key_file)
                key_snapshot = key_file.snapshot()
                key_bytes = key_file.read_all()
                if (
                    key_snapshot != observations["device.key"]
                    or key_snapshot.identity.kind != "regular"
                    or key_snapshot.identity.link_count != 1
                    or not key_snapshot.reparse_free
                    or len(key_bytes) != DEVICE_SECRET_SIZE_BYTES
                ):
                    raise recovery_required()
                key_evidence = backend.prove_private(key_file)
                authorities.append(key_evidence)
                secret = persistent_private.bind_device_secret(key_file)
                authorities.append(secret)
                secret.reprove()

                if not (has_pending or has_cancelled):
                    if names != {"device.key"}:
                        raise recovery_required()
                    caller_borrow.reprove(backend, identity)
                    result = _PortableFreshRecoverySnapshot(
                        state="NO_FACTS",
                        namespace_state="KEY_ONLY",
                        pending_record=None,
                        cancelled_record=None,
                        phase_records=(),
                        _factory_key=_PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY,
                    )

                def verify_record_file(
                    name: str,
                    parser: Callable[[bytes], object],
                    context_digest: Callable[[object], bytes],
                ) -> object:
                    record_file = backend.open_regular(
                        root,
                        PurePath(private_name, name),
                    )
                    authorities.append(record_file)
                    record_snapshot = record_file.snapshot()
                    payload = record_file.read_all()
                    if (
                        record_snapshot != observations[name]
                        or record_snapshot.identity.kind != "regular"
                        or record_snapshot.identity.link_count != 1
                        or not record_snapshot.reparse_free
                    ):
                        raise recovery_required()
                    record_evidence = backend.prove_private(record_file)
                    authorities.append(record_evidence)
                    record = parser(payload)
                    proof = record.private_directory_proof
                    context = PrivateProofContext(
                        PrivateProofObjectRole.PRIVATE_DIRECTORY,
                        context_digest(record),
                    )
                    verified = persistent_private.verify(
                        private_evidence,
                        secret,
                        proof,
                        context,
                    )
                    authorities.append(verified)
                    caller_borrow.reprove(backend, identity)
                    return record

                def activation_context(record: object) -> bytes:
                    if type(record) is not _PortableActivationJournalRecord:
                        raise TypeError("portable recovery journal record is invalid")
                    return _portable_activation_owner_context_sha256(record.unsigned)

                def publication_context(record: object) -> bytes:
                    if type(record) is not _PortablePublicationPhaseRecord:
                        raise TypeError("portable recovery phase record is invalid")
                    return _portable_publication_owner_context_sha256(record.unsigned)

                def require_activation_binding(
                    record: _PortableActivationJournalRecord,
                    closure: str,
                ) -> None:
                    unsigned = record.unsigned
                    if (
                        unsigned.closure != closure
                        or unsigned.resource_id != identity.resource_id
                        or unsigned.target_identity != identity.target_identity
                        or unsigned.canonical_store_id != canonical_store_id
                        or unsigned.private_directory_name != private_name
                        or unsigned.device_key_name != "device.key"
                        or unsigned.journal_name != "activation-journal-v3.json"
                        or unsigned.terminal_name != "activation-terminal-v3.json"
                        or unsigned.lock_payload_digest
                        != caller_borrow.lock_payload_digest()
                    ):
                        raise recovery_required()

                pending_record: _PortableActivationJournalRecord | None = None
                if result is None and has_pending:
                    pending = verify_record_file(
                        "activation-journal-v3.json",
                        _parse_portable_activation_journal_bytes,
                        activation_context,
                    )
                    if type(pending) is not _PortableActivationJournalRecord:
                        raise TypeError("portable recovery parser returned wrong record")
                    require_activation_binding(pending, "PENDING")
                    pending_record = pending

                cancelled_record: _PortableActivationJournalRecord | None = None
                if result is None and has_cancelled:
                    cancelled = verify_record_file(
                        "activation-terminal-v3.json",
                        _parse_portable_activation_journal_bytes,
                        activation_context,
                    )
                    if type(cancelled) is not _PortableActivationJournalRecord:
                        raise TypeError("portable recovery parser returned wrong record")
                    require_activation_binding(cancelled, "CANCELLED")
                    cancelled_record = cancelled

                if (
                    result is None
                    and pending_record is not None
                    and cancelled_record is not None
                ):
                    if not _portable_record_pair_matches(
                        pending_record,
                        cancelled_record,
                    ):
                        raise recovery_required()

                phase_records: list[_PortablePublicationPhaseRecord] = []
                predecessor_digest = (
                    None if pending_record is None else pending_record.record_digest
                )
                active_attestation: PortableActiveContentAttestation | None = None
                for index in range(phase_count if result is None else 0):
                    phase_name = phase_names[index]
                    phase = verify_record_file(
                        phase_name,
                        _parse_portable_publication_phase_bytes,
                        publication_context,
                    )
                    if type(phase) is not _PortablePublicationPhaseRecord:
                        raise TypeError("portable recovery parser returned wrong phase")
                    unsigned = phase.unsigned
                    prepared_unsigned = pending_record.unsigned
                    if (
                        unsigned.phase != _PORTABLE_PUBLICATION_PHASES[index]
                        or unsigned.predecessor_digest != predecessor_digest
                        or unsigned.journal_id != prepared_unsigned.journal_id
                        or unsigned.preparation_id
                        != prepared_unsigned.preparation_id
                        or unsigned.token_id != prepared_unsigned.token_id
                        or unsigned.token_version != prepared_unsigned.token_version
                        or unsigned.activation_nonce
                        != prepared_unsigned.activation_nonce
                        or unsigned.resource_id != identity.resource_id
                        or unsigned.target_identity != identity.target_identity
                        or unsigned.canonical_store_id != canonical_store_id
                        or unsigned.canonical_database_name
                        != identity.canonical_sidecar_path.name
                        or unsigned.canonical_manifest_name
                        != identity.snapshot_manifest_path.name
                        or unsigned.private_directory_name != private_name
                        or unsigned.device_key_name != "device.key"
                        or unsigned.sealed_content_attestation
                        != prepared_unsigned.sealed_content_attestation
                    ):
                        raise recovery_required()
                    if index == 0:
                        if unsigned.active_content_attestation is not None:
                            raise recovery_required()
                    elif active_attestation is None:
                        active_attestation = unsigned.active_content_attestation
                    elif unsigned.active_content_attestation != active_attestation:
                        raise recovery_required()
                    predecessor_digest = phase.record_digest
                    phase_records.append(phase)

                caller_borrow.reprove(backend, identity)
                if result is not None:
                    pass
                elif cancelled_record is not None:
                    result = _PortableFreshRecoverySnapshot(
                        state="CANCELLED",
                        namespace_state="JOURNAL",
                        pending_record=pending_record,
                        cancelled_record=cancelled_record,
                        phase_records=(),
                        _factory_key=_PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY,
                    )
                else:
                    result = _PortableFreshRecoverySnapshot(
                        state="PENDING",
                        namespace_state="JOURNAL",
                        pending_record=pending_record,
                        cancelled_record=None,
                        phase_records=tuple(phase_records),
                        _factory_key=_PORTABLE_RECOVERY_SNAPSHOT_FACTORY_KEY,
                    )
        except ActivationPreparationError as error:
            active_error = (
                error
                if error.code == "ACTIVATION.RECOVERY_REQUIRED" and error.retryable
                else recovery_required(error)
            )
        except (PlatformFileError, OSError) as error:
            active_error = recovery_required(error)
        except BaseException as error:
            active_error = error
        finally:
            close_error = _close_portable_journal_authorities(authorities)
            if active_error is None and close_error is not None:
                active_error = (
                    recovery_required(close_error)
                    if isinstance(close_error, (PlatformFileError, OSError))
                    else close_error
                )
        if active_error is not None:
            raise active_error
        if result is None:
            raise AssertionError("portable recovery owner produced no snapshot")
        return result


def _portable_initial_stage_quarantine_name(
    identity: CanonicalResourceIdentity,
    unsigned: _PortableActivationJournalUnsigned,
) -> str:
    """Re-derive the initial-attempt quarantine from portable stage basenames."""

    if type(identity) is not CanonicalResourceIdentity:
        raise TypeError("portable stage identity is invalid")
    if type(unsigned) is not _PortableActivationJournalUnsigned:
        raise TypeError("portable stage journal record is invalid")
    database_name = unsigned.candidate_stage_db_name
    manifest_name = unsigned.candidate_manifest_temp_name
    prefix = ".localcat-migration."
    suffix_root = (
        f".{identity.target_identity[:16]}."
        f"{unsigned.sealed_content_attestation.source.sha256[:16]}"
    )
    database_suffix = suffix_root + ".sqlite3.stage"
    manifest_suffix = suffix_root + ".manifest.tmp"
    if not (
        database_name.startswith(prefix)
        and database_name.endswith(database_suffix)
        and manifest_name.startswith(prefix)
        and manifest_name.endswith(manifest_suffix)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )
    database_salt = database_name[len(prefix) : -len(database_suffix)]
    manifest_salt = manifest_name[len(prefix) : -len(manifest_suffix)]
    if (
        database_salt != manifest_salt
        or not database_salt.startswith("initial-")
        or len(database_salt) != len("initial-") + 32
        or any(character not in "0123456789abcdef" for character in database_salt[8:])
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )
    token = hashlib.sha256(
        (
            f"{database_name}\0{manifest_name}\0{database_salt}"
        ).encode("utf-8")
    ).hexdigest()
    return f"initial-{token}"


class _WindowsPortableCompletedStageRetirementOwner:
    """Retire or rebind the exact sealed stage pair after durable GEN phase."""

    @staticmethod
    def retire(
        *,
        identity: CanonicalResourceIdentity,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        existing_retirement: ExistingFileRetirement,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        snapshot: _PortableFreshRecoverySnapshot,
    ) -> None:
        if sys.platform != "win32":
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        from platform_fs_windows import WindowsPlatformAdapter

        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable completed-stage identity is invalid")
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable completed-stage borrow is invalid")
        if type(snapshot) is not _PortableFreshRecoverySnapshot:
            raise TypeError("portable completed-stage snapshot is invalid")
        if type(backend) is not WindowsPlatformAdapter or not (
            persistent_private is backend
            and descendant_inspection is backend
            and existing_retirement is backend
            and isinstance(descendant_inspection, LockedDescendantNamespaceInspection)
            and isinstance(existing_retirement, ExistingFileRetirement)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if (
            snapshot.state != "PENDING"
            or snapshot.highest_phase != "GENERATION_PUBLISHED"
            or type(snapshot.pending_record) is not _PortableActivationJournalRecord
            or len(snapshot.phase_records) != len(_PORTABLE_PUBLICATION_PHASES)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )

        authorities: list[object] = []
        active_error: BaseException | None = None
        try:
            root, lease = caller_borrow._fresh_recovery_authorities(
                backend,
                persistent_private,
                identity,
            )
            unsigned = snapshot.pending_record.unsigned
            if (
                unsigned.resource_id != identity.resource_id
                or unsigned.target_identity != identity.target_identity
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            quarantine_name = _portable_initial_stage_quarantine_name(
                identity,
                unsigned,
            )
            database_name = unsigned.candidate_stage_db_name
            manifest_name = unsigned.candidate_manifest_temp_name
            expected = {
                database_name: _portable_candidate_content(
                    unsigned.sealed_content_attestation.database.size,
                    unsigned.sealed_content_attestation.database.sha256,
                ),
                manifest_name: _portable_candidate_content(
                    unsigned.sealed_content_attestation.manifest.size,
                    unsigned.sealed_content_attestation.manifest.sha256,
                ),
            }
            limits = LedgerEnumerationLimits(
                maximum_entries=2,
                maximum_name_bytes=4096,
                maximum_total_bytes=sum(
                    facts.byte_count for facts in expected.values()
                ),
            )

            source_presence = {
                name: root.inspect_entry(name) is not None for name in expected
            }
            if len(set(source_presence.values())) != 1:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            sources_present = next(iter(source_presence.values()))

            def observe_existing_quarantine() -> set[str] | None:
                quarantine_root_entry = root.inspect_entry(
                    _PORTABLE_ACTIVATION_QUARANTINE_ROOT
                )
                if quarantine_root_entry is None:
                    return None
                if quarantine_root_entry.identity.kind != "directory":
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                root_bound = backend.bind_parent(
                    root,
                    PurePath(
                        _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                        "completed-stage-placeholder",
                    ),
                )
                try:
                    target_entry = root_bound.inspect_entry(quarantine_name)
                    if target_entry is None:
                        return None
                    if target_entry.identity.kind != "directory":
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        )
                finally:
                    root_bound.close()
                observer = backend.bind_parent(
                    root,
                    PurePath(
                        _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                        quarantine_name,
                        "completed-stage-placeholder",
                    ),
                )
                try:
                    return {
                        item.name
                        for item in descendant_inspection.observe_descendant_entries(
                            root,
                            lease,
                            observer,
                            limits,
                        )
                    }
                finally:
                    observer.close()

            before_names = observe_existing_quarantine()
            if sources_present:
                if before_names is not None and before_names:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
            elif before_names != set(expected):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )

            quarantine_root = backend.bind_or_create_child_directory(
                root,
                _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
            )
            authorities.append(quarantine_root)
            quarantine_target = backend.bind_or_create_child_directory(
                quarantine_root,
                quarantine_name,
            )
            authorities.append(quarantine_target)

            for name, expected_content in expected.items():
                source_parent: object | None = None
                source: object | None = None
                retained: RetainedRetirement | None = None
                try:
                    if sources_present:
                        source_parent = (
                            existing_retirement.bind_retirement_source_directory(
                                root,
                                PurePath(name),
                            )
                        )
                        authorities.append(source_parent)
                        source = existing_retirement.open_existing_retirement_source(
                            source_parent,
                            expected_content,
                        )
                        retained = existing_retirement.retire_existing_exclusive(
                            source_parent,
                            source,
                            quarantine_target,
                            name,
                        )
                        source = None
                    else:
                        source_parent = backend.bind_parent(root, PurePath(name))
                        authorities.append(source_parent)
                        retained = existing_retirement.rebind_existing_retirement(
                            source_parent,
                            name,
                            quarantine_target,
                            name,
                            expected_content,
                        )
                    authorities.append(retained)
                finally:
                    if source is not None:
                        source.close()
                if source_parent is not None:
                    source_parent.close()
                    authorities.remove(source_parent)
                if retained is None:
                    raise AssertionError("completed-stage retirement has no authority")
                retained.reprove()

            caller_borrow.reprove(backend, identity)
            final_names = observe_existing_quarantine()
            if final_names != set(expected):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            if any(root.inspect_entry(name) is not None for name in expected):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            for authority in authorities:
                if isinstance(authority, RetainedRetirement):
                    authority.reprove()
            caller_borrow.reprove(backend, identity)
        except ActivationPreparationError as error:
            active_error = (
                error
                if error.code == "ACTIVATION.RECOVERY_REQUIRED" and error.retryable
                else ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            )
            if active_error is not error:
                active_error.__cause__ = error
        except (PlatformFileError, OSError) as error:
            active_error = ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
            active_error.__cause__ = error
        except BaseException as error:
            active_error = error
        finally:
            close_error = _close_portable_journal_authorities(authorities)
            if active_error is None and close_error is not None:
                active_error = (
                    ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                    if isinstance(close_error, (PlatformFileError, OSError))
                    else close_error
                )
        if active_error is not None:
            raise active_error


def _portable_candidate_content(
    size: int,
    sha256: str,
) -> CandidateContentFacts:
    return CandidateContentFacts(size, bytes.fromhex(sha256))


def _portable_record_pair_matches(
    pending: _PortableActivationJournalRecord,
    cancelled: _PortableActivationJournalRecord,
) -> bool:
    return (
        type(pending) is _PortableActivationJournalRecord
        and type(cancelled) is _PortableActivationJournalRecord
        and _portable_activation_paired_unsigned(
            pending.unsigned,
            cancelled.unsigned,
        )
    )


def _portable_terminal_candidate_content(
    record: _PortableActivationJournalRecord,
) -> tuple[str, bytes, CandidateContentFacts]:
    """Return the one deterministic v3 CANCELLED candidate description."""

    if type(record) is not _PortableActivationJournalRecord:
        raise TypeError("portable terminal candidate record is invalid")
    if record.unsigned.closure != "CANCELLED":
        raise ValueError("portable terminal candidate must be CANCELLED")
    payload = _serialize_portable_activation_journal_record(record)
    name = record.unsigned.terminal_name + _PORTABLE_JOURNAL_CANDIDATE_SUFFIX
    return (
        name,
        payload,
        CandidateContentFacts(len(payload), hashlib.sha256(payload).digest()),
    )


class _WindowsPortableCancelledJournalOwner:
    """Close one exact Windows v3 PREPARED authority as CANCELLED."""

    @staticmethod
    def cancel(
        *,
        identity: CanonicalResourceIdentity,
        backend: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        existing_retirement: ExistingFileRetirement,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        owner_reprove: Callable[[_PortableActivationJournalRecord], None],
        owner_commit: Callable[[_PortableActivationJournalRecord], None],
    ) -> _PortableCancelledJournalHandle:
        if sys.platform != "win32":
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        from platform_fs_windows import WindowsPlatformAdapter

        if type(identity) is not CanonicalResourceIdentity:
            raise TypeError("portable recovery identity is invalid")
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable recovery borrow is invalid")
        if type(backend) is not WindowsPlatformAdapter:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if not (
            persistent_private is backend
            and descendant_inspection is backend
            and existing_retirement is backend
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if not callable(owner_reprove) or not callable(owner_commit):
            raise TypeError("portable recovery owner callbacks must be callable")

        authorities: list[object] = []
        terminal_candidate: CandidateFile | None = None
        terminal_candidate_retained: RetainedRetirement | None = None
        terminal_pending: PendingPublication | None = None
        private_parent: BoundDirectoryAuthority | None = None
        quarantine_root: RetirementDirectoryAuthority | None = None
        quarantine_target: RetirementDirectoryAuthority | None = None
        quarantine_bound: BoundDirectoryAuthority | None = None
        pending_file: object | None = None
        pending_evidence: object | None = None
        pending_verified: object | None = None
        terminal_file: object | None = None
        terminal_evidence: object | None = None
        terminal_verified: object | None = None
        retained_terminal: object | None = None
        retained_terminal_evidence: object | None = None
        fresh_retained_terminal_evidence: object | None = None
        publication_armed = False
        retirement_armed = False
        owner_committed = False
        terminal_candidate_retirement_required = False
        active_error: BaseException | None = None
        result: _PortableCancelledJournalHandle | None = None

        try:
            root, lease = caller_borrow._fresh_recovery_authorities(
                backend,
                persistent_private,
                identity,
            )

            def ensure_quarantine(
                unsigned: _PortableActivationJournalUnsigned,
            ) -> RetirementDirectoryAuthority:
                nonlocal quarantine_root, quarantine_target, quarantine_bound
                nonlocal retirement_armed
                if quarantine_target is not None:
                    return quarantine_target
                caller_borrow.reprove(backend, identity)
                quarantine_root = backend.bind_or_create_child_directory(
                    root,
                    _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                )
                retirement_armed = True
                authorities.append(quarantine_root)
                quarantine_name = _portable_activation_quarantine_name(unsigned)
                quarantine_target = backend.bind_or_create_child_directory(
                    quarantine_root,
                    quarantine_name,
                )
                authorities.append(quarantine_target)
                return quarantine_target

            def bind_quarantine_observer(
                unsigned: _PortableActivationJournalUnsigned,
            ) -> BoundDirectoryAuthority:
                nonlocal quarantine_bound
                if quarantine_bound is not None:
                    raise AssertionError("quarantine observer is already live")
                ensure_quarantine(unsigned)
                quarantine_bound = backend.bind_parent(
                    root,
                    PurePath(
                        _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                        _portable_activation_quarantine_name(unsigned),
                        "recovery-placeholder",
                    ),
                )
                authorities.append(quarantine_bound)
                return quarantine_bound

            def observe_existing_quarantine_names(
                unsigned: _PortableActivationJournalUnsigned,
                limits: LedgerEnumerationLimits,
            ) -> set[str]:
                quarantine_root_name = _PORTABLE_ACTIVATION_QUARANTINE_ROOT
                root_entry = root.inspect_entry(quarantine_root_name)
                if root_entry is None:
                    return set()
                if root_entry.identity.kind != "directory":
                    raise ActivationPreparationError(
                        "ACTIVATION.QUARANTINE_FOREIGN",
                        retryable=False,
                    )
                root_bound = backend.bind_parent(
                    root,
                    PurePath(quarantine_root_name, "recovery-placeholder"),
                )
                try:
                    attempt_name = _portable_activation_quarantine_name(unsigned)
                    attempt_entry = root_bound.inspect_entry(attempt_name)
                    if attempt_entry is None:
                        return set()
                    if attempt_entry.identity.kind != "directory":
                        raise ActivationPreparationError(
                            "ACTIVATION.QUARANTINE_FOREIGN",
                            retryable=False,
                        )
                finally:
                    root_bound.close()
                observer = backend.bind_parent(
                    root,
                    PurePath(
                        quarantine_root_name,
                        attempt_name,
                        "recovery-placeholder",
                    ),
                )
                try:
                    return {
                        item.name
                        for item in descendant_inspection.observe_descendant_entries(
                            root,
                            lease,
                            observer,
                            limits,
                        )
                    }
                finally:
                    observer.close()

            def retire_or_rebind(
                *,
                unsigned: _PortableActivationJournalUnsigned,
                source_name: str,
                source_relative: PurePath,
                expected_content: CandidateContentFacts,
            ) -> RetainedRetirement:
                target = ensure_quarantine(unsigned)
                caller_borrow.reprove(backend, identity)
                source_parent = None
                source = None
                retained = None
                try:
                    source_parent = (
                        existing_retirement.bind_retirement_source_directory(
                            root,
                            source_relative,
                        )
                    )
                    authorities.append(source_parent)
                    try:
                        source = existing_retirement.open_existing_retirement_source(
                            source_parent,
                            expected_content,
                        )
                    except PlatformFileError as error:
                        if error.code != PlatformFileErrorCode.ENTRY_UNAVAILABLE.value:
                            raise
                        source_parent.close()
                        authorities.remove(source_parent)
                        source_parent = backend.bind_parent(root, source_relative)
                        authorities.append(source_parent)
                        retained = existing_retirement.rebind_existing_retirement(
                            source_parent,
                            source_name,
                            target,
                            source_name,
                            expected_content,
                        )
                    else:
                        retained = existing_retirement.retire_existing_exclusive(
                            source_parent,
                            source,
                            target,
                            source_name,
                        )
                        source = None
                    authorities.append(retained)
                finally:
                    if source is not None:
                        source.close()
                if source_parent is not None:
                    source_parent.close()
                    authorities.remove(source_parent)
                    source_parent = None
                if retained is None:
                    raise AssertionError("portable retirement produced no authority")
                retained.reprove()
                return retained

            def close_pending_private_authorities() -> None:
                """Release the strict private leaf before a cross-parent B move."""

                nonlocal private_parent, private_evidence
                nonlocal key_file, key_evidence, secret
                nonlocal pending_file, pending_evidence, pending_verified
                for authority in (
                    pending_verified,
                    pending_evidence,
                    pending_file,
                    key_evidence,
                    key_file,
                    secret,
                    private_evidence,
                    private_parent,
                ):
                    if authority is not None:
                        authority.close()
                        if authority in authorities:
                            authorities.remove(authority)
                pending_verified = None
                pending_evidence = None
                pending_file = None
                key_evidence = None
                key_file = None
                secret = None
                private_evidence = None
                private_parent = None

            def rebind_pending_private_authorities(
                expected_record: _PortableActivationJournalRecord,
                expected_bytes: bytes,
                expected_key: bytes,
            ) -> None:
                """Freshly rebind W2/key/PENDING after the candidate B handoff."""

                nonlocal private_parent, private_evidence
                nonlocal key_file, key_evidence, secret
                nonlocal pending_file, pending_evidence, pending_verified
                private_parent = backend.bind_parent(
                    root,
                    PurePath(private_name, "private-recovery-placeholder"),
                )
                authorities.append(private_parent)
                rebound_names = {
                    item.name
                    for item in descendant_inspection.observe_descendant_entries(
                        root,
                        lease,
                        private_parent,
                        _PORTABLE_ACTIVATION_PRIVATE_LIMITS,
                    )
                }
                if rebound_names != {
                    expected_record.unsigned.device_key_name,
                    expected_record.unsigned.journal_name,
                }:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                        retryable=False,
                    )
                private_evidence = backend.prove_private(private_parent)
                authorities.append(private_evidence)
                key_file = backend.open_regular(
                    root,
                    PurePath(private_name, expected_record.unsigned.device_key_name),
                )
                authorities.append(key_file)
                rebound_key_identity = key_file.identity()
                if (
                    rebound_key_identity.kind != "regular"
                    or rebound_key_identity.link_count != 1
                    or key_file.read_all() != expected_key
                    or len(expected_key) != DEVICE_SECRET_SIZE_BYTES
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                        retryable=False,
                    )
                key_evidence = backend.prove_private(key_file)
                authorities.append(key_evidence)
                secret = persistent_private.bind_device_secret(key_file)
                authorities.append(secret)
                secret.reprove()
                pending_file = backend.open_regular(
                    root,
                    PurePath(private_name, expected_record.unsigned.journal_name),
                )
                authorities.append(pending_file)
                rebound_pending_identity = pending_file.identity()
                if (
                    rebound_pending_identity.kind != "regular"
                    or rebound_pending_identity.link_count != 1
                    or pending_file.read_all() != expected_bytes
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                pending_evidence = backend.prove_private(pending_file)
                authorities.append(pending_evidence)
                rebound_record = _parse_portable_activation_journal_bytes(
                    pending_file.read_all()
                )
                if rebound_record != expected_record:
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                rebound_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(
                        rebound_record.unsigned
                    ),
                )
                pending_verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    rebound_record.private_directory_proof,
                    rebound_context,
                )
                authorities.append(pending_verified)
                owner_reprove(rebound_record)
                caller_borrow.reprove(backend, identity)

            def recover_exact_terminal_candidate(
                record: _PortableActivationJournalRecord,
                expected_pending_record: _PortableActivationJournalRecord,
                expected_pending_bytes: bytes,
                expected_key: bytes,
                *,
                source_present: bool,
            ) -> RetainedRetirement:
                """Quarantine only the exact deterministic terminal candidate."""

                if type(source_present) is not bool:
                    raise TypeError("terminal candidate presence must be exact bool")
                candidate_name, candidate_bytes, expected_content = (
                    _portable_terminal_candidate_content(record)
                )
                limits = LedgerEnumerationLimits(
                    maximum_entries=1,
                    maximum_name_bytes=4096,
                    maximum_total_bytes=len(candidate_bytes),
                )
                observed = observe_existing_quarantine_names(record.unsigned, limits)
                if not observed <= {candidate_name}:
                    raise ActivationPreparationError(
                        "ACTIVATION.QUARANTINE_FOREIGN",
                        retryable=False,
                    )
                target_present = candidate_name in observed
                if source_present and target_present:
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                if not source_present and not target_present:
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )

                close_pending_private_authorities()
                source_parent = None
                source = None
                retained = None
                try:
                    if source_present:
                        source_parent = (
                            existing_retirement.bind_retirement_source_directory(
                                root,
                                PurePath(private_name, candidate_name),
                            )
                        )
                        authorities.append(source_parent)
                        source = existing_retirement.open_existing_retirement_source(
                            source_parent,
                            expected_content,
                        )
                        target = ensure_quarantine(record.unsigned)
                        retained = existing_retirement.retire_existing_exclusive(
                            source_parent,
                            source,
                            target,
                            candidate_name,
                        )
                        source = None
                    else:
                        target = ensure_quarantine(record.unsigned)
                        source_parent = backend.bind_parent(
                            root,
                            PurePath(private_name, candidate_name),
                        )
                        authorities.append(source_parent)
                        retained = existing_retirement.rebind_existing_retirement(
                            source_parent,
                            candidate_name,
                            target,
                            candidate_name,
                            expected_content,
                        )
                    authorities.append(retained)
                finally:
                    if source is not None:
                        source.close()
                if source_parent is not None:
                    source_parent.close()
                    authorities.remove(source_parent)
                if retained is None:
                    raise AssertionError("terminal candidate handoff produced no authority")
                retained.reprove()
                if observe_existing_quarantine_names(record.unsigned, limits) != {
                    candidate_name
                }:
                    raise ActivationPreparationError(
                        "ACTIVATION.QUARANTINE_FOREIGN",
                        retryable=False,
                    )
                rebind_pending_private_authorities(
                    expected_pending_record,
                    expected_pending_bytes,
                    expected_key,
                )
                proof_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(record.unsigned),
                )
                verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    record.private_directory_proof,
                    proof_context,
                )
                verified.close()
                return retained

            private_name = _portable_activation_private_directory_name(identity)
            private_parent = backend.bind_parent(
                root,
                PurePath(private_name, "private-recovery-placeholder"),
            )
            authorities.append(private_parent)
            initial_private = descendant_inspection.observe_descendant_entries(
                root,
                lease,
                private_parent,
                _PORTABLE_ACTIVATION_PRIVATE_LIMITS,
            )
            private_names = {item.name for item in initial_private}
            terminal_candidate_name = (
                "activation-terminal-v3.json"
                + _PORTABLE_JOURNAL_CANDIDATE_SUFFIX
            )
            allowed_private_names = {
                "device.key",
                "activation-journal-v3.json",
                "activation-terminal-v3.json",
                terminal_candidate_name,
            }
            if (
                not private_names <= allowed_private_names
                or "device.key" not in private_names
                or not private_names
                & {"activation-journal-v3.json", "activation-terminal-v3.json"}
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                    retryable=False,
                )
            private_evidence = backend.prove_private(private_parent)
            authorities.append(private_evidence)
            key_file = backend.open_regular(
                root,
                PurePath(private_name, "device.key"),
            )
            authorities.append(key_file)
            key_identity = key_file.identity()
            key_bytes = key_file.read_all()
            if (
                key_identity.kind != "regular"
                or key_identity.link_count != 1
                or len(key_bytes) != DEVICE_SECRET_SIZE_BYTES
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            key_evidence = backend.prove_private(key_file)
            authorities.append(key_evidence)
            secret = persistent_private.bind_device_secret(key_file)
            authorities.append(secret)
            secret.reprove()

            pending_record: _PortableActivationJournalRecord | None = None
            pending_bytes: bytes | None = None
            if "activation-journal-v3.json" in private_names:
                pending_file = backend.open_regular(
                    root,
                    PurePath(private_name, "activation-journal-v3.json"),
                )
                authorities.append(pending_file)
                pending_identity = pending_file.identity()
                pending_bytes = pending_file.read_all()
                if (
                    pending_identity.kind != "regular"
                    or pending_identity.link_count != 1
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                        retryable=False,
                    )
                pending_evidence = backend.prove_private(pending_file)
                authorities.append(pending_evidence)
                pending_record = _parse_portable_activation_journal_bytes(
                    pending_bytes
                )
                if pending_record.unsigned.closure != "PENDING":
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                        retryable=False,
                    )
                pending_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(
                        pending_record.unsigned
                    ),
                )
                pending_verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    pending_record.private_directory_proof,
                    pending_context,
                )
                authorities.append(pending_verified)

            cancelled_record: _PortableActivationJournalRecord | None = None
            cancelled_bytes: bytes | None = None
            if "activation-terminal-v3.json" in private_names:
                terminal_file = backend.open_regular(
                    root,
                    PurePath(private_name, "activation-terminal-v3.json"),
                )
                authorities.append(terminal_file)
                terminal_identity = terminal_file.identity()
                cancelled_bytes = terminal_file.read_all()
                if (
                    terminal_identity.kind != "regular"
                    or terminal_identity.link_count != 1
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                        retryable=False,
                    )
                terminal_evidence = backend.prove_private(terminal_file)
                authorities.append(terminal_evidence)
                cancelled_record = _parse_portable_activation_journal_bytes(
                    cancelled_bytes
                )
                if cancelled_record.unsigned.closure != "CANCELLED":
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                        retryable=False,
                    )
                terminal_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(
                        cancelled_record.unsigned
                    ),
                )
                terminal_verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    cancelled_record.private_directory_proof,
                    terminal_context,
                )
                authorities.append(terminal_verified)

            if pending_record is not None and cancelled_record is not None:
                if not _portable_record_pair_matches(
                    pending_record,
                    cancelled_record,
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                        retryable=False,
                    )
            elif pending_record is None and cancelled_record is None:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                    retryable=False,
                )

            authority_record = (
                pending_record if pending_record is not None else cancelled_record
            )
            if authority_record is None:
                raise AssertionError("portable recovery has no authority record")
            unsigned = authority_record.unsigned
            if (
                unsigned.resource_id != identity.resource_id
                or unsigned.target_identity != identity.target_identity
                or unsigned.private_directory_name != private_name
                or unsigned.device_key_name != "device.key"
                or unsigned.journal_name != "activation-journal-v3.json"
                or unsigned.terminal_name != "activation-terminal-v3.json"
                or unsigned.lock_payload_digest
                != caller_borrow.lock_payload_digest()
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                    retryable=False,
                )
            owner_reprove(authority_record)
            caller_borrow.reprove(backend, identity)

            source_file = backend.open_regular(
                root,
                PurePath(identity.configured_jsonl_path.name),
            )
            authorities.append(source_file)
            source_facts = source_file.content_facts()
            expected_source = unsigned.sealed_content_attestation.source
            if (
                source_facts.snapshot.identity.kind != "regular"
                or source_facts.snapshot.identity.link_count != 1
                or source_facts.snapshot.byte_count != expected_source.size
                or source_facts.content_sha256.hex() != expected_source.sha256
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_ASSET_MUTATED",
                    retryable=False,
                )
            for absent_name in (
                identity.canonical_sidecar_path.name,
                identity.snapshot_manifest_path.name,
                _activation_lineage_marker_path(identity).name,
                _activation_lineage_marker_temp_path(
                    _activation_lineage_marker_path(identity)
                ).name,
            ):
                if root.inspect_entry(absent_name) is not None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                        retryable=False,
                    )

            if cancelled_record is None:
                if pending_record is None or pending_bytes is None:
                    raise AssertionError("portable recovery cannot mint a terminal")
                cancelled_unsigned = replace(
                    pending_record.unsigned,
                    closure="CANCELLED",
                )
                terminal_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(cancelled_unsigned),
                )
                terminal_proof = persistent_private.mint(
                    private_evidence,
                    secret,
                    terminal_context,
                )
                cancelled_record = _create_portable_activation_journal_record(
                    cancelled_unsigned,
                    terminal_proof,
                )
                cancelled_bytes = _serialize_portable_activation_journal_record(
                    cancelled_record
                )
                terminal_candidate_name = (
                    cancelled_unsigned.terminal_name
                    + _PORTABLE_JOURNAL_CANDIDATE_SUFFIX
                )
                candidate_limits = LedgerEnumerationLimits(
                    maximum_entries=1,
                    maximum_name_bytes=4096,
                    maximum_total_bytes=len(cancelled_bytes),
                )
                candidate_quarantine_names = observe_existing_quarantine_names(
                    cancelled_unsigned,
                    candidate_limits,
                )
                if not candidate_quarantine_names <= {terminal_candidate_name}:
                    raise ActivationPreparationError(
                        "ACTIVATION.QUARANTINE_FOREIGN",
                        retryable=False,
                    )
                candidate_source_present = terminal_candidate_name in private_names
                if candidate_source_present or candidate_quarantine_names:
                    terminal_candidate_retained = recover_exact_terminal_candidate(
                        cancelled_record,
                        pending_record,
                        pending_bytes,
                        key_bytes,
                        source_present=candidate_source_present,
                    )
                    terminal_candidate_retirement_required = True
                if private_parent.inspect_entry(terminal_candidate_name) is not None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                        retryable=False,
                    )
                terminal_candidate = private_parent.create_candidate(
                    terminal_candidate_name,
                    private=True,
                )
                publication_armed = True
                terminal_candidate.write_all(cancelled_bytes)
                terminal_candidate.flush_content()
                owner_reprove(pending_record)
                caller_borrow.reprove(backend, identity)
                try:
                    terminal_pending = private_parent.begin_publish(
                        terminal_candidate,
                        cancelled_unsigned.terminal_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                except BaseException:
                    terminal_candidate.close()
                    terminal_candidate = None
                    raise
                terminal_candidate = None
                authorities.append(terminal_pending)
                retained_terminal = terminal_pending.retained_destination()
                retained_identity = retained_terminal.identity()
                if (
                    retained_identity.kind != "regular"
                    or retained_identity.link_count != 1
                    or retained_terminal.read_all() != cancelled_bytes
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                retained_terminal_evidence = backend.prove_private(
                    retained_terminal
                )
                authorities.append(retained_terminal_evidence)
                disk_cancelled = _parse_portable_activation_journal_bytes(
                    retained_terminal.read_all()
                )
                if disk_cancelled != cancelled_record:
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                terminal_verified = persistent_private.verify(
                    private_evidence,
                    secret,
                    disk_cancelled.private_directory_proof,
                    terminal_context,
                )
                authorities.append(terminal_verified)
                owner_reprove(pending_record)
                caller_borrow.reprove(backend, identity)
            else:
                cancelled_bytes = _serialize_portable_activation_journal_record(
                    cancelled_record
                )

            if cancelled_record is None or cancelled_bytes is None:
                raise AssertionError("portable recovery produced no terminal")
            if (
                terminal_file is None
                and retained_terminal is None
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            terminal_authority = (
                terminal_file if terminal_file is not None else retained_terminal
            )
            if (
                terminal_authority is None
                or terminal_authority.read_all() != cancelled_bytes
                or _parse_portable_activation_journal_bytes(
                    terminal_authority.read_all()
                )
                != cancelled_record
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            owner_reprove(cancelled_record)
            caller_borrow.reprove(backend, identity)
            owner_result = owner_commit(cancelled_record)
            if owner_result is not None:
                raise TypeError("portable recovery owner commit must return None")
            owner_committed = True

            if terminal_pending is not None:
                if retained_terminal is None:
                    raise AssertionError(
                        "portable terminal pending has no retained authority"
                    )
                retained_terminal_evidence.close()
                authorities.remove(retained_terminal_evidence)
                owner_reprove(cancelled_record)
                caller_borrow.reprove(backend, identity)
                if (
                    retained_terminal.read_all() != cancelled_bytes
                    or _parse_portable_activation_journal_bytes(
                        retained_terminal.read_all()
                    )
                    != cancelled_record
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                fresh_retained_terminal_evidence = backend.prove_private(
                    retained_terminal
                )
                authorities.append(fresh_retained_terminal_evidence)
                if (
                    terminal_pending.terminal_reproof()
                    != terminal_pending.preliminary_facts()
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                terminal_pending.close()
                authorities.remove(terminal_pending)
                terminal_pending = None
                retained_terminal = None
                terminal_file = backend.open_regular(
                    root,
                    PurePath(
                        private_name,
                        cancelled_record.unsigned.terminal_name,
                    ),
                )
                authorities.append(terminal_file)
                terminal_evidence = backend.prove_private(terminal_file)
                authorities.append(terminal_evidence)

            if pending_verified is not None:
                pending_verified.close()
                authorities.remove(pending_verified)
                pending_verified = None
            if pending_evidence is not None:
                pending_evidence.close()
                authorities.remove(pending_evidence)
                pending_evidence = None
            if pending_file is not None:
                pending_file.close()
                authorities.remove(pending_file)
                pending_file = None

            if pending_record is not None and pending_bytes is not None:
                expected_pending_bytes = pending_bytes
            else:
                pending_unsigned = replace(
                    cancelled_record.unsigned,
                    closure="PENDING",
                )
                pending_context = PrivateProofContext(
                    PrivateProofObjectRole.PRIVATE_DIRECTORY,
                    _portable_activation_owner_context_sha256(pending_unsigned),
                )
                pending_proof = persistent_private.mint(
                    private_evidence,
                    secret,
                    pending_context,
                )
                expected_pending_bytes = _serialize_portable_activation_journal_record(
                    _create_portable_activation_journal_record(
                        pending_unsigned,
                        pending_proof,
                    )
                )

            terminal_context = PrivateProofContext(
                PrivateProofObjectRole.PRIVATE_DIRECTORY,
                _portable_activation_owner_context_sha256(
                    cancelled_record.unsigned
                ),
            )
            if terminal_verified is not None:
                persistent_private.consume_verified(
                    terminal_verified,
                    terminal_context,
                )
                authorities.remove(terminal_verified)
                terminal_verified = None

            # A live strict handle to the private leaf correctly blocks a
            # cross-parent rename on Windows.  After the CANCELLED owner commit,
            # release every authority whose directory chain ends at that leaf;
            # the dedicated B source-parent authority will reopen only the leaf
            # with rename-compatible sharing.  All W2/file authority is rebound
            # and reproved after the exact quarantine handoff.
            for private_authority in (
                fresh_retained_terminal_evidence,
                terminal_evidence,
                terminal_file,
                key_evidence,
                key_file,
                secret,
                private_evidence,
                private_parent,
            ):
                if private_authority is not None:
                    private_authority.close()
                    if private_authority in authorities:
                        authorities.remove(private_authority)
            terminal_evidence = None
            terminal_file = None
            key_evidence = None
            key_file = None
            secret = None
            private_evidence = None
            private_parent = None
            for released_private_authority in (
                pending_file,
                pending_evidence,
                pending_verified,
                terminal_candidate,
                terminal_pending,
                retained_terminal,
                retained_terminal_evidence,
                fresh_retained_terminal_evidence,
                terminal_file,
                terminal_evidence,
                terminal_verified,
                key_file,
                key_evidence,
                secret,
                private_evidence,
                private_parent,
            ):
                if released_private_authority is not None and not getattr(
                    released_private_authority,
                    "closed",
                    False,
                ):
                    raise AssertionError(
                        "portable cancellation retained a private authority"
                    )

            target = ensure_quarantine(cancelled_record.unsigned)
            bound = bind_quarantine_observer(cancelled_record.unsigned)
            del target
            sealed = cancelled_record.unsigned.sealed_content_attestation
            base_retirement_items = (
                (
                    cancelled_record.unsigned.journal_name,
                    PurePath(
                        cancelled_record.unsigned.private_directory_name,
                        cancelled_record.unsigned.journal_name,
                    ),
                    CandidateContentFacts(
                        len(expected_pending_bytes),
                        hashlib.sha256(expected_pending_bytes).digest(),
                    ),
                ),
                (
                    cancelled_record.unsigned.candidate_stage_db_name,
                    PurePath(cancelled_record.unsigned.candidate_stage_db_name),
                    _portable_candidate_content(
                        sealed.database.size,
                        sealed.database.sha256,
                    ),
                ),
                (
                    cancelled_record.unsigned.candidate_manifest_temp_name,
                    PurePath(cancelled_record.unsigned.candidate_manifest_temp_name),
                    _portable_candidate_content(
                        sealed.manifest.size,
                        sealed.manifest.sha256,
                    ),
                ),
            )
            (
                terminal_candidate_name,
                terminal_candidate_bytes,
                expected_terminal_candidate_facts,
            ) = _portable_terminal_candidate_content(cancelled_record)
            terminal_candidate_item = (
                terminal_candidate_name,
                PurePath(
                    cancelled_record.unsigned.private_directory_name,
                    terminal_candidate_name,
                ),
                expected_terminal_candidate_facts,
            )
            allowed_quarantine_names = {
                name for name, _relative, _facts in base_retirement_items
            } | {terminal_candidate_name}
            quarantine_limits = LedgerEnumerationLimits(
                maximum_entries=len(allowed_quarantine_names),
                maximum_name_bytes=4096,
                maximum_total_bytes=(
                    len(expected_pending_bytes)
                    + sealed.database.size
                    + sealed.manifest.size
                    + len(terminal_candidate_bytes)
                ),
            )
            initial_quarantine = descendant_inspection.observe_descendant_entries(
                root,
                lease,
                bound,
                quarantine_limits,
            )
            initial_quarantine_names = {
                item.name for item in initial_quarantine
            }
            if not initial_quarantine_names <= allowed_quarantine_names:
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                )
            terminal_candidate_retirement_required = (
                terminal_candidate_retirement_required
                or terminal_candidate_name in private_names
                or terminal_candidate_name in initial_quarantine_names
            )
            retirement_items = base_retirement_items + (
                (terminal_candidate_item,)
                if terminal_candidate_retirement_required
                else ()
            )
            expected_quarantine_names = {
                name for name, _relative, _facts in retirement_items
            }
            if not initial_quarantine_names <= expected_quarantine_names:
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                )
            bound.close()
            authorities.remove(bound)
            quarantine_bound = None
            for source_name, relative, expected_content in retirement_items:
                if (
                    source_name == terminal_candidate_name
                    and terminal_candidate_retained is not None
                ):
                    terminal_candidate_retained.reprove()
                    continue
                retire_or_rebind(
                    unsigned=cancelled_record.unsigned,
                    source_name=source_name,
                    source_relative=relative,
                    expected_content=expected_content,
                )
            bound = bind_quarantine_observer(cancelled_record.unsigned)
            final_quarantine = descendant_inspection.observe_descendant_entries(
                root,
                lease,
                bound,
                quarantine_limits,
            )
            if {item.name for item in final_quarantine} != expected_quarantine_names:
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                )
            private_parent = backend.bind_parent(
                root,
                PurePath(private_name, "private-recovery-placeholder"),
            )
            authorities.append(private_parent)
            final_private = descendant_inspection.observe_descendant_entries(
                root,
                lease,
                private_parent,
                _PORTABLE_ACTIVATION_PRIVATE_LIMITS,
            )
            if {item.name for item in final_private} != {
                cancelled_record.unsigned.device_key_name,
                cancelled_record.unsigned.terminal_name,
            }:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                    retryable=False,
                )
            private_evidence = backend.prove_private(private_parent)
            authorities.append(private_evidence)
            key_file = backend.open_regular(
                root,
                PurePath(private_name, cancelled_record.unsigned.device_key_name),
            )
            authorities.append(key_file)
            rebound_key_identity = key_file.identity()
            if (
                rebound_key_identity.kind != "regular"
                or rebound_key_identity.link_count != 1
                or key_file.read_all() != key_bytes
                or len(key_bytes) != DEVICE_SECRET_SIZE_BYTES
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            key_evidence = backend.prove_private(key_file)
            authorities.append(key_evidence)
            secret = persistent_private.bind_device_secret(key_file)
            authorities.append(secret)
            secret.reprove()
            terminal_file = backend.open_regular(
                root,
                PurePath(private_name, cancelled_record.unsigned.terminal_name),
            )
            authorities.append(terminal_file)
            rebound_terminal_identity = terminal_file.identity()
            if (
                rebound_terminal_identity.kind != "regular"
                or rebound_terminal_identity.link_count != 1
                or terminal_file.read_all() != cancelled_bytes
                or _parse_portable_activation_journal_bytes(
                    terminal_file.read_all()
                )
                != cancelled_record
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            terminal_evidence = backend.prove_private(terminal_file)
            authorities.append(terminal_evidence)
            terminal_verified = persistent_private.verify(
                private_evidence,
                secret,
                cancelled_record.private_directory_proof,
                terminal_context,
            )
            authorities.append(terminal_verified)
            source_terminal = source_file.content_facts()
            if (
                source_terminal.snapshot.identity != source_facts.snapshot.identity
                or source_terminal.content_sha256 != source_facts.content_sha256
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_ASSET_MUTATED",
                    retryable=False,
                )
            for authority in authorities:
                if isinstance(authority, RetainedRetirement):
                    authority.reprove()
            if (
                terminal_file.read_all() != cancelled_bytes
                or _parse_portable_activation_journal_bytes(
                    terminal_file.read_all()
                )
                != cancelled_record
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            terminal_evidence.close()
            authorities.remove(terminal_evidence)
            terminal_evidence = None
            fresh_terminal_evidence = backend.prove_private(terminal_file)
            authorities.append(fresh_terminal_evidence)
            secret.reprove()
            caller_borrow.reprove(backend, identity)
            persistent_private.consume_verified(
                terminal_verified,
                terminal_context,
            )
            authorities.remove(terminal_verified)
            terminal_verified = None

            # Preserve the complete terminal W2 verification above, then move
            # only those exact verified bytes out of the reusable private root.
            for private_authority in (
                fresh_terminal_evidence,
                terminal_file,
                key_evidence,
                key_file,
                secret,
                private_evidence,
                private_parent,
            ):
                if private_authority is not None:
                    private_authority.close()
                    if private_authority in authorities:
                        authorities.remove(private_authority)
            fresh_terminal_evidence = None
            terminal_file = None
            key_evidence = None
            key_file = None
            secret = None
            private_evidence = None
            private_parent = None
            if bound in authorities:
                bound.close()
                authorities.remove(bound)
                quarantine_bound = None

            terminal_content = CandidateContentFacts(
                len(cancelled_bytes),
                hashlib.sha256(cancelled_bytes).digest(),
            )
            archived_terminal = retire_or_rebind(
                unsigned=cancelled_record.unsigned,
                source_name=cancelled_record.unsigned.terminal_name,
                source_relative=PurePath(
                    cancelled_record.unsigned.private_directory_name,
                    cancelled_record.unsigned.terminal_name,
                ),
                expected_content=terminal_content,
            )
            archived_terminal.reprove()
            archived_names = expected_quarantine_names | {
                cancelled_record.unsigned.terminal_name
            }
            archived_limits = LedgerEnumerationLimits(
                maximum_entries=len(archived_names),
                maximum_name_bytes=4096,
                maximum_total_bytes=(
                    quarantine_limits.maximum_total_bytes
                    + len(cancelled_bytes)
                ),
            )
            bound = bind_quarantine_observer(cancelled_record.unsigned)
            archived_observations = (
                descendant_inspection.observe_descendant_entries(
                    root,
                    lease,
                    bound,
                    archived_limits,
                )
            )
            if {item.name for item in archived_observations} != archived_names:
                raise ActivationPreparationError(
                    "ACTIVATION.QUARANTINE_FOREIGN",
                    retryable=False,
                )
            bound.close()
            authorities.remove(bound)
            quarantine_bound = None

            private_parent = backend.bind_parent(
                root,
                PurePath(private_name, "private-key-only-placeholder"),
            )
            authorities.append(private_parent)
            key_only = descendant_inspection.observe_descendant_entries(
                root,
                lease,
                private_parent,
                LedgerEnumerationLimits(
                    maximum_entries=1,
                    maximum_name_bytes=4096,
                    maximum_total_bytes=DEVICE_SECRET_SIZE_BYTES,
                ),
            )
            if {item.name for item in key_only} != {
                cancelled_record.unsigned.device_key_name
            }:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                    retryable=False,
                )
            private_evidence = backend.prove_private(private_parent)
            authorities.append(private_evidence)
            key_file = backend.open_regular(
                root,
                PurePath(private_name, cancelled_record.unsigned.device_key_name),
            )
            authorities.append(key_file)
            key_terminal_identity = key_file.identity()
            if (
                key_terminal_identity.kind != "regular"
                or key_terminal_identity.link_count != 1
                or key_file.read_all() != key_bytes
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                    retryable=False,
                )
            key_evidence = backend.prove_private(key_file)
            authorities.append(key_evidence)
            secret = persistent_private.bind_device_secret(key_file)
            authorities.append(secret)
            secret.reprove()
            archived_terminal.reprove()
            caller_borrow.reprove(backend, identity)
            result = _PortableCancelledJournalHandle(
                cancelled_record,
                _factory_key=_PORTABLE_JOURNAL_HANDLE_FACTORY_KEY,
            )
        except ActivationPreparationError as error:
            mapped_error = (
                ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
                if (
                    error.code
                    not in {
                        "ACTIVATION.RECOVERY_PRIVATE_NAMESPACE_INVALID",
                        "ACTIVATION.QUARANTINE_FOREIGN",
                    }
                    and (publication_armed or retirement_armed or owner_committed)
                )
                else error
            )
            if mapped_error is not error:
                mapped_error.__cause__ = error
            active_error = mapped_error
        except (PlatformFileError, OSError) as error:
            if (
                isinstance(error, PlatformFileError)
                and error.code
                == PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value
                and not publication_armed
                and not retirement_armed
                and not owner_committed
            ):
                active_error = ActivationPreparationError(
                    "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                    retryable=False,
                )
            else:
                active_error = ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            active_error.__cause__ = error
        except BaseException as error:
            active_error = error
        finally:
            if terminal_candidate is not None:
                try:
                    terminal_candidate.close()
                except BaseException as cleanup_error:
                    if active_error is None:
                        active_error = cleanup_error
            close_error = _close_portable_journal_authorities(authorities)
            if active_error is None and close_error is not None:
                if isinstance(close_error, (PlatformFileError, OSError)):
                    active_error = ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                else:
                    active_error = close_error
        if active_error is not None:
            raise active_error
        if result is None:
            raise AssertionError("portable cancellation owner produced no handle")
        return result
