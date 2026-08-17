"""Activation journal/terminal codec and durable file protocol (Task 5.R1).

Extracted from ``tm_sqlite_store.py`` without behavior change: the journal
phase machine (PREPARED -> DB_REPLACED -> MANIFEST_PUBLISHED ->
GENERATION_PUBLISHED), the terminal record protocol, the exclusive
temporary/replace/fsync primitives, and the shared activation types.  This
module is a leaf: it imports only frozen contracts and the standard library
and never imports ``tm_sqlite_store`` or ``SQLiteTMStore``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import tm_contracts as contract_module
from tm_content_attestation import (
    ActiveContentAttestation,
    SealedContentAttestation,
    _active_content_attestation_from_mapping,
    _active_content_attestation_to_mapping,
    _sealed_content_attestation_from_mapping,
    _sealed_content_attestation_to_mapping,
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
    active_content_attestation: ActiveContentAttestation | None = None


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
