"""Phase recovery, publication, and rollback for durable activations (Task 5.R1).

Extracted from ``tm_sqlite_store.py`` without behavior change: this module
consumes the frozen contracts, the shared activation journal types/codec
(``tm_activation_journal``), and a narrow ``_StoreValidationPort`` so it
has no dependency on the store module.  The coordinator in
``tm_sqlite_store`` implements the port and delegates
``publish_activation``/``recover_durable_activation``/
``rollback_durable_activation`` here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, cast

import tm_contracts as contract_module
from tm_content_attestation import (
    ActiveContentAttestation,
    ContentAttestationError,
    ContentSemanticFacts,
    _capture_content_file,
    _create_active_content_attestation,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SNAPSHOT_MANIFEST_VERSION,
    SealedStage,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_activation_journal import (
    ActivationPreparationError,
    ActivationRecoveryReport,
    _ACTIVATION_JOURNAL_VERSION,
    _ActivationCleanupReservation,
    _ActivationFileIdentity,
    _ActivationJournalHandle,
    _ActivationJournalPhase,
    _ActivationJournalRecord,
    _ActivationPreparation,
    _CanonicalStoreRef,
    _PHASE_SEQUENCE,
    _PriorActivationRef,
    _PriorAssetCapture,
    _RecoveryBackupAsset,
    _SQLiteGenerationView,
    _StoreRuntimeRef,
    _activation_file_identity,
    _activation_journal_path,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_lineage_marker_state_complete,
    _activation_lineage_marker_temp_path,
    _activation_rollback_eligible,
    _activation_terminal_coexistence_valid,
    _activation_terminal_path,
    _activation_terminal_temp_path,
    _capture_activation_file,
    _ensure_activation_lineage_marker,
    _fsync_activation_directory,
    _fsync_activation_file,
    _fsync_recovery_backup,
    _lstat_activation_journal_identity,
    _lstat_activation_lineage_marker_identity,
    _lstat_activation_terminal_identity,
    _lstat_any_entry,
    _open_activation_journal_temp,
    _parse_activation_journal_bytes,
    _quarantine_failed_activation_artifacts,
    _read_activation_file_bytes,
    _read_activation_journal_file,
    _remove_journal_proven_backups,
    _remove_orphaned_activation_temp,
    _remove_orphaned_rollback_temp,
    _remove_owned_activation_journal_final,
    _remove_owned_activation_terminal_final,
    _replace_activation_file,
    _replay_activation_journal,
    _retire_cancelled_candidate_assets,
    _require_first_activation_absence,
    _rollback_terminal_prior_closes,
    _write_recovery_backup,
)

_SCHEMA_UPGRADE_META_KEY = "schema_upgrade_origin"
_SCHEMA_UPGRADE_META_VALUE = "schema-upgrade-v1"


class _StoreValidationPort(Protocol):
    """Narrow store-side surface consumed by the recovery functions."""

    @property
    def resource_identity(self) -> CanonicalResourceIdentity: ...

    @property
    def canonical_store_id(self) -> str: ...

    def _activate_candidate_store_id(self, candidate_id: str) -> None: ...

    @property
    def view(self) -> _SQLiteGenerationView | None: ...

    @view.setter
    def view(self, value: _SQLiteGenerationView | None) -> None: ...

    @property
    def state(self) -> str: ...

    @state.setter
    def state(self, value: str) -> None: ...

    @property
    def preparation(self) -> _ActivationPreparation | None: ...

    @preparation.setter
    def preparation(self, value: _ActivationPreparation | None) -> None: ...

    @property
    def cleanup_reservation(self) -> _ActivationCleanupReservation | None: ...

    @cleanup_reservation.setter
    def cleanup_reservation(
        self,
        value: _ActivationCleanupReservation | None,
    ) -> None: ...

    @property
    def cleanup_in_progress(self) -> bool: ...

    @property
    def active_lease_count(self) -> int: ...

    @property
    def drain_timeout_seconds(self) -> float: ...

    @property
    def sealed_registry(self) -> Any: ...

    store_schema_error: type[RuntimeError]

    def notify_all(self) -> None: ...

    def drain_for_transition(self) -> None:
        """Stop new leases, drain live leases, prove the view, then ACTIVATING.

        Narrow coordinator port operation used by recovery/rollback before
        any disk mutation: the coordinator condition is held for the whole
        transition, ``READY -> DRAINING`` rejects new operation leases, the
        condition is released while waiting so in-flight leases can finish
        and decrement, and the view/generation must be unchanged before
        ``ACTIVATING``.  A drain timeout or an observed view change restores
        ``READY`` and notifies waiters so the resource is left in a coherent
        non-transition state with no disk/generation transition.
        """
        ...

    def open_configured_connection(
        self,
        database_path: Path,
        *,
        require_existing: bool = False,
    ) -> Any: ...
    def read_meta(self, connection: sqlite3.Connection) -> dict[str, str]: ...
    def meta_int(self, meta: dict[str, str], key: str) -> int: ...
    def meta_bool(self, meta: dict[str, str], key: str) -> bool: ...
    def read_source_binding_facts(
        self,
        connection: sqlite3.Connection,
        lease: _SQLiteGenerationView,
    ) -> Any: ...
    def read_source_binding_facts_in_transaction(
        self,
        connection: sqlite3.Connection,
        lease: _SQLiteGenerationView,
    ) -> Any: ...
    def binding_from_ledger_row(self, row: tuple[object, ...]) -> SnapshotBinding: ...
    def configured_pair_diagnostics(
        self,
        binding: SnapshotBinding,
        *,
        identity: CanonicalResourceIdentity,
        canonical_store_id: str,
        head_revision: int,
        cumulative_record_counts: tuple[tuple[int, int], ...],
    ) -> tuple[str, ...]: ...
    def table_count(self, connection: sqlite3.Connection, table_name: str) -> int: ...
    def validate_store_identity(
        self,
        connection: sqlite3.Connection,
        *,
        resource_id: str,
        canonical_store_id: str,
        target_identity: str,
    ) -> None: ...
    def inspect_stage_schema(
        self,
        stage_ref: Any,
        *,
        canonical_store_id: str,
        _allow_diverged_runtime: bool = False,
        _allow_sealed: bool = False,
        _allow_active: bool = False,
        _expected_active_generation: int | None = None,
        _expected_activation_digest: str | None = None,
    ) -> Any: ...
    def validate_candidate_proof_index(
        self,
        connection: sqlite3.Connection,
        *,
        required_sizes: tuple[int, ...],
        fts5_available: bool,
    ) -> tuple[tuple[tuple[int, int], ...], int]: ...
    def stage_closure_digest(
        self,
        connection: sqlite3.Connection,
    ) -> str: ...
    def write_journal(
        self,
        record: _ActivationJournalRecord,
        journal_path: Path,
        *,
        expected_final_identity: _ActivationFileIdentity | None,
    ) -> _ActivationJournalHandle: ...
    def write_terminal(
        self,
        record: _ActivationJournalRecord,
    ) -> _ActivationFileIdentity: ...

    @property
    def stage_seal_error(self) -> type[RuntimeError]:
        """The registry's sealed-mode error type for readiness/token seams."""
        ...

    def validate_stage_facts(
        self,
        stage_ref: Any,
        *,
        canonical_store_id: str,
    ) -> Any:
        """Re-prove one SEALED stage's seal facts from disk.

        Mirrors the stage sealer's private sealed-mode validation so this
        module never imports the sealer: the returned facts carry
        resource/target identity, schema/fold/index versions, record/origin/
        FTS counts, gram counts, exact parity digest, and the ledger receipt.
        """
        ...

    def accepted_jsonl_row(
        self,
        payload: object,
    ) -> tuple[
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
    ] | None:
        """One migration-accepted JSONL row, or None when rejected."""
        ...

    def build_sealed_binding(
        self,
        identity: CanonicalResourceIdentity,
        receipt: SnapshotReceipt,
        manifest: SnapshotManifest,
    ) -> SnapshotBinding:
        """Deterministic MIGRATION_SOURCE snapshot binding construction."""
        ...

    def build_seal_evidence(
        self,
        facts: Any,
        binding: SnapshotBinding,
        *,
        stage_file_digest: str,
        manifest_temp_digest: str,
    ) -> Any:
        """Reconstruct one sealed stage's validation evidence from facts."""
        ...


class _CoordinatorPublishPort(_StoreValidationPort, Protocol):
    """Publish-side port that adds the coordinator's after-effect seam."""

    def advance_after_effect(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
        *,
        next_generation: int | None = None,
        activation_digest: str | None = None,
        active_content_attestation: ActiveContentAttestation | None = None,
    ) -> _ActivationJournalHandle: ...

class _ActivationGateBGrant(Protocol):
    @property
    def resource_id(self) -> str: ...

    @property
    def target_identity(self) -> str: ...

    @property
    def canonical_store_id(self) -> str: ...

    @property
    def expected_prior_generation(self) -> int | None: ...


def _require_activation_grant_identity(
    grant: _ActivationGateBGrant,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    prior_view: _SQLiteGenerationView | None,
    current_generation: int | None,
) -> None:
    if (
        grant.resource_id != identity.resource_id
        or grant.target_identity != identity.target_identity
        or grant.canonical_store_id != canonical_store_id
        or grant.expected_prior_generation != current_generation
    ):
        code = (
            "ACTIVATION.GENERATION_STALE"
            if grant.expected_prior_generation != current_generation
            else "ACTIVATION.IDENTITY_MISMATCH"
        )
        raise ActivationPreparationError(code, retryable=False)
    if prior_view is not None and (
        prior_view.canonical_store_id != canonical_store_id
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )


def _require_activation_token_identity(
    token: contract_module._ActivationToken,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    current_generation: int | None,
) -> None:
    if (
        token.resource_id != identity.resource_id
        or token.target_identity != identity.target_identity
        or token.canonical_store_id != canonical_store_id
        or token.expected_prior_generation != current_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )


def _recovery_acceptable_store_ids(
    record: _ActivationJournalRecord,
) -> frozenset[str]:
    """The coordinator store ids one journal may be recovered under.

    A pending activation leaves the prior store id as the current
    coordinator authority.  A completed (GENERATION_PUBLISHED) explicit
    replacement may be recovered by a fresh coordinator still bound to the
    journal-recorded prior id (the crash window before the in-memory
    candidate-id switch) or by a coordinator already bound to the
    candidate id (idempotent replay); both authenticate the same record
    and the recovery adopts the candidate id before READY.  Ordinary
    same-id activations bind exactly the journal's candidate id, which is
    implicitly the prior id.
    """

    if record.prior_canonical_store_id is not None:
        acceptable = {record.prior_canonical_store_id}
        if record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
            acceptable.add(record.canonical_store_id)
        return frozenset(acceptable)
    return frozenset({record.canonical_store_id})


def _require_activation_grant_identity_replacement(
    grant: _ActivationGateBGrant,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    prior_view: _SQLiteGenerationView | None,
    current_generation: int | None,
) -> None:
    """Validate one explicit replacement grant (Task 5.10 seam).

    The resource/target/generation facts bind exactly like an ordinary
    activation, but the grant's candidate store id must differ from the
    coordinator's current (prior) store id: only the explicit replacement
    operation may accept a changed store id, and the prior view must still
    carry the coordinator's current id.
    """

    if (
        grant.resource_id != identity.resource_id
        or grant.target_identity != identity.target_identity
        or grant.expected_prior_generation != current_generation
    ):
        code = (
            "ACTIVATION.GENERATION_STALE"
            if grant.expected_prior_generation != current_generation
            else "ACTIVATION.IDENTITY_MISMATCH"
        )
        raise ActivationPreparationError(code, retryable=False)
    if grant.canonical_store_id == canonical_store_id:
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )
    if prior_view is not None and (
        prior_view.canonical_store_id != canonical_store_id
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )


def _require_activation_token_identity_replacement(
    token: contract_module._ActivationToken,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    candidate_store_id: str,
    current_generation: int | None,
) -> None:
    """Validate one explicit replacement token (Task 5.10 seam).

    The token must bind the candidate store id of the sealed stage while
    the coordinator keeps the prior store id until durable publication.
    """

    if (
        token.resource_id != identity.resource_id
        or token.target_identity != identity.target_identity
        or token.canonical_store_id != candidate_store_id
        or token.canonical_store_id == canonical_store_id
        or token.expected_prior_generation != current_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )


def _capture_prior_assets(
    port: _StoreValidationPort,
    view: _SQLiteGenerationView,
    *,
    identity: CanonicalResourceIdentity,
    replacement: bool = False,
) -> tuple[_PriorAssetCapture, ...]:
    """Capture the prior DB/manifest/source for one activation preparation.

    The strict mode requires the prior binding to be healthy and current
    (DB, manifest bytes, and source bytes all equal the binding closure).
    The explicit replacement mode (Task 5.10) intentionally tolerates the
    configured-file divergence it exists to disambiguate: the prior ledger
    binding must still exist, but a latched ``SOURCE_DIVERGED`` state, a
    source file that no longer matches the prior receipt, a missing
    adjacent manifest, and an externally altered regular-file manifest are
    all expected.  Canonical ledger/ancestry corruption
    (``facts.diagnostic_codes``) is never tolerated, in either mode: an
    explicit import must not silently repair a corrupt canonical.  The
    exact observed prior bytes (and the manifest absence) are captured as
    rollback authority; symlink/directory/multi-link/foreign manifest
    entries still fail closed and are never touched.
    """

    if (
        view.stage.resource_identity != identity
        or view.stage.resource_identity.resource_id != identity.resource_id
        or view.stage.resource_identity.target_identity != identity.target_identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )
    with port.open_configured_connection(
        view.stage.staged_db_path,
        require_existing=True,
    ) as connection:
        facts = port.read_source_binding_facts(connection, view)
    binding = facts.binding
    if (
        binding is None
        or facts.diagnostic_codes
        or (
            not replacement
            and (
                facts.divergence_latched
                or port.configured_pair_diagnostics(
                    binding,
                    identity=identity,
                    canonical_store_id=view.canonical_store_id,
                    head_revision=facts.head_revision,
                    cumulative_record_counts=facts.cumulative_record_counts,
                )
            )
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_BINDING_INVALID",
            retryable=False,
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
    database = captures[0]
    manifest = next(
        (
            capture
            for capture in captures
            if capture.asset_kind == "MANIFEST"
        ),
        None,
    )
    source = captures[-1]
    if not replacement and source.digest != binding.receipt.jsonl_digest:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_BINDING_INVALID",
            retryable=False,
        )
    if manifest is not None and not replacement:
        manifest_payload = _read_activation_file_bytes(manifest)
        if manifest_payload != contract_to_json(
            binding.manifest
        ).encode("utf-8"):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_BINDING_INVALID",
                retryable=False,
            )
        try:
            decoded = contract_from_json(manifest_payload.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_BINDING_INVALID",
                retryable=False,
            ) from error
        if (
            type(decoded) is not SnapshotManifest
            or decoded != binding.manifest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_BINDING_INVALID",
                retryable=False,
            )
    return tuple(captures)


def _canonical_activation_ref(
    identity: CanonicalResourceIdentity,
    *,
    journal_id: str,
) -> _CanonicalStoreRef:
    return _CanonicalStoreRef(
        stage_id=f"canonical.{journal_id}",
        resource_identity=identity,
        staged_db_path=identity.canonical_sidecar_path,
        manifest_temp_path=identity.snapshot_manifest_path,
    )


def _validate_activation_publication_authority(
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    registry: Any,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> None:
    """Validate token/registry/source/backup facts after candidate movement."""

    stage = preparation._sealed_stage
    token = preparation._token
    if (
        type(record) is not _ActivationJournalRecord
        or type(stage) is not SealedStage
        or type(token) is not contract_module._ActivationToken
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    try:
        contract_module._validate_activation_token_for_stage(token, stage)
        physical = registry.resolve_physical_readiness(stage)
        token_entry = registry._token_entry(token)
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        ) from error
    evidence = physical.evidence
    receipt = evidence.source_binding.receipt
    if (
        token_entry.state
        is not contract_module.ActivationCapabilityState.TOKEN_ISSUED
        or registry.registry_namespace != record.registry_namespace
        or physical.registry_namespace != record.registry_namespace
        or physical.artifact_id != record.artifact_id
        or physical.artifact_seal_digest != record.artifact_seal_digest
        or physical.sealed_stage_digest != record.sealed_stage_digest
        or physical.resource_id != identity.resource_id
        or physical.resource_id != record.resource_id
        or physical.target_identity != identity.target_identity
        or physical.target_identity != record.target_identity
        or physical.canonical_store_id != canonical_store_id
        or physical.canonical_store_id != record.canonical_store_id
        or physical.expected_prior_generation
        != record.expected_prior_generation
        or physical.snapshot_receipt_digest
        != record.snapshot_receipt_digest
        or contract_module.stage_validation_evidence_digest(evidence)
        != record.evidence_digest
        or evidence.stage_file_digest != record.stage_db_digest
        or evidence.manifest_temp_digest != record.manifest_temp_digest
        or receipt.snapshot_id != record.new_receipt_id
        or receipt.jsonl_digest != record.source_jsonl_digest
        or physical.mutable_stage.staged_db_path
        != record.candidate_stage_db_path
        or physical.mutable_stage.manifest_temp_path
        != record.candidate_manifest_temp_path
        or (
            physical.database_identity.device,
            physical.database_identity.inode,
        )
        != record.candidate_stage_db_identity
        or (
            physical.manifest_identity.device,
            physical.manifest_identity.inode,
        )
        != record.candidate_manifest_temp_identity
        or preparation.preparation_id != record.preparation_id
        or preparation.resource_id != record.resource_id
        or preparation.target_identity != record.target_identity
        or preparation.canonical_store_id != record.canonical_store_id
        or preparation.prior_canonical_store_id
        != record.prior_canonical_store_id
        or preparation.expected_prior_generation
        != record.expected_prior_generation
        or preparation.gate_b_grant_digest != record.gate_b_grant_digest
        or preparation.had_prior_canonical != record.had_prior_canonical
        or record.new_manifest_path != identity.snapshot_manifest_path
        or record.journal_path != _activation_journal_path(identity)
        or record.new_manifest_digest != record.manifest_temp_digest
        or token.token_id != record.token_id
        or token.token_version != record.token_version
        or token.activation_nonce != record.activation_nonce
        or token.artifact_id != record.artifact_id
        or token.artifact_seal_digest != record.artifact_seal_digest
        or token.sealed_stage_digest != record.sealed_stage_digest
        or token.snapshot_receipt_digest != record.snapshot_receipt_digest
        or token.expected_prior_generation
        != record.expected_prior_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    source = _capture_journal_closure_file(identity.configured_jsonl_path)
    if (
        (source[0].device, source[0].inode) != record.source_jsonl_identity
        or source[1] != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    if record.had_prior_canonical:
        backups = {asset.asset_kind: asset for asset in preparation._backup_assets}
        expected_backup_kinds = (
            {"DATABASE"} if record.prior_manifest_absent
            else {"DATABASE", "MANIFEST"}
        )
        if set(backups) != expected_backup_kinds:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        backup_entries = [
            (
                "DATABASE",
                record.prior_db_backup_path,
                record.prior_db_backup_identity,
                record.prior_db_backup_digest,
            ),
        ]
        if not record.prior_manifest_absent:
            backup_entries.append(
                (
                    "MANIFEST",
                    record.prior_manifest_backup_path,
                    record.prior_manifest_backup_identity,
                    record.prior_manifest_backup_digest,
                )
            )
        for kind, path, expected_identity, expected_digest in (
            backup_entries
        ):
            if path is None or expected_identity is None or expected_digest is None:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            capture = _capture_journal_closure_file(path)
            asset = backups[kind]
            if (
                (capture[0].device, capture[0].inode) != expected_identity
                or capture[1] != expected_digest
                or asset.backup_path != path
                or asset.backup_identity != capture[0]
                or asset.evidence.backup_digest != expected_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
    elif preparation._backup_assets:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )


def _replace_activation_database(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Atomically move the sealed candidate into the canonical sidecar."""

    candidate = _capture_journal_closure_file(record.candidate_stage_db_path)
    if (
        (candidate[0].device, candidate[0].inode)
        != record.candidate_stage_db_identity
        or candidate[1] != record.stage_db_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.DB_CANDIDATE_INVALID",
            retryable=False,
        )
    canonical = identity.canonical_sidecar_path
    if record.had_prior_canonical:
        if record.prior_db_path == canonical:
            prior = _capture_journal_closure_file(canonical)
            if (
                (prior[0].device, prior[0].inode) != record.prior_db_identity
                or prior[1] != record.prior_db_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PRIOR_ASSET_INVALID",
                    retryable=False,
                )
        elif _lstat_any_entry(canonical):
            raise ActivationPreparationError(
                "ACTIVATION.CANONICAL_TARGET_OCCUPIED",
                retryable=False,
            )
    elif _lstat_any_entry(canonical):
        raise ActivationPreparationError(
            "ACTIVATION.CANONICAL_TARGET_OCCUPIED",
            retryable=False,
        )
    try:
        _replace_activation_file(record.candidate_stage_db_path, canonical)
        _fsync_activation_directory(canonical.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.DB_REPLACE_FAILED",
            retryable=True,
        ) from error
    published = _capture_journal_closure_file(canonical)
    if (
        published[0] != candidate[0]
        or published[1] != record.stage_db_digest
        or _lstat_any_entry(record.candidate_stage_db_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.DB_REPLACE_UNPROVEN",
            retryable=False,
        )


def _validate_replaced_activation_database(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> Any:
    """Rehash the sealed attestation, then reopen schema/integrity/FK."""

    sealed = record.sealed_content_attestation
    try:
        canonical = _capture_content_file(identity.canonical_sidecar_path)
        manifest = _capture_content_file(record.candidate_manifest_temp_path)
        source = _capture_content_file(identity.configured_jsonl_path)
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        ) from error
    if (
        canonical != sealed.database
        or manifest != sealed.manifest
        or source != sealed.source
        or _lstat_any_entry(record.candidate_stage_db_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        )
    active_ref = _canonical_activation_ref(identity, journal_id=record.journal_id)
    try:
        snapshot = port.inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_sealed=True,
        )
        with port.open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [
                ("ok",)
            ]:
                raise port.store_schema_error("STORE.INTEGRITY_CHECK_FAILED")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise port.store_schema_error("STORE.FOREIGN_KEY_CHECK_FAILED")
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        ) from error
    if snapshot.activation_status != "SEALED":
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        )
    return snapshot


def _activation_publication_digest(
    record: _ActivationJournalRecord,
    *,
    next_generation: int,
) -> str:
    payload = {
        "activation_nonce": record.activation_nonce,
        "artifact_id": record.artifact_id,
        "evidence_digest": record.evidence_digest,
        "generation": next_generation,
        "journal_id": record.journal_id,
        "manifest_digest": record.new_manifest_digest,
        "sealed_stage_digest": record.sealed_stage_digest,
        "token_id": record.token_id,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _publish_activation_receipt(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
) -> None:
    """Durably complete the issued receipt/binding inside the new DB."""

    evidence = preparation._sealed_stage.evidence
    binding = evidence.source_binding
    receipt = binding.receipt
    canonical_capture = _capture_journal_closure_file(
        identity.canonical_sidecar_path
    )
    if (
        (canonical_capture[0].device, canonical_capture[0].inode)
        != record.candidate_stage_db_identity
        or canonical_capture[1] != record.stage_db_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECEIPT_PUBLICATION_INVALID",
            retryable=False,
        )
    try:
        with port.open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                meta = port.read_meta(connection)
                if (
                    meta.get("resource_id") != identity.resource_id
                    or meta.get("canonical_store_id") != canonical_store_id
                    or meta.get("target_identity") != identity.target_identity
                    or meta.get("activation_status") != "SEALED"
                    or "activation_digest" in meta
                    or port.meta_int(meta, "generation") != 0
                ):
                    raise port.store_schema_error(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
                rows = connection.execute(
                    "SELECT snapshot_id, resource_id, canonical_store_id, "
                    "exported_revision, jsonl_digest, record_count, "
                    "format_version, destination_jsonl_path, "
                    "destination_manifest_path, status "
                    "FROM tm_snapshot_receipt ORDER BY snapshot_id"
                ).fetchall()
                expected_row = (
                    receipt.snapshot_id,
                    receipt.resource_id,
                    receipt.canonical_store_id,
                    receipt.exported_revision,
                    receipt.jsonl_digest,
                    receipt.record_count,
                    receipt.format_version,
                    Path.__str__(identity.configured_jsonl_path),
                    Path.__str__(identity.snapshot_manifest_path),
                    "issued",
                )
                if rows != [expected_row]:
                    raise port.store_schema_error("STORE.RECEIPT_INVALID")
                if connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_binding"
                ).fetchone() != (0,):
                    raise port.store_schema_error("STORE.BINDING_INVALID")
                updated = connection.execute(
                    "UPDATE tm_snapshot_receipt SET status = 'completed' "
                    "WHERE snapshot_id = ? AND status = 'issued'",
                    (receipt.snapshot_id,),
                )
                if updated.rowcount != 1:
                    raise port.store_schema_error("STORE.RECEIPT_INVALID")
                connection.execute(
                    "INSERT INTO tm_snapshot_binding("
                    "binding_id, configured_jsonl_path, manifest_path, "
                    "snapshot_kind, snapshot_id, binding_version) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        Path.__str__(identity.configured_jsonl_path),
                        Path.__str__(identity.snapshot_manifest_path),
                        binding.snapshot_kind.value,
                        receipt.snapshot_id,
                        binding.binding_version,
                    ),
                )
                status = connection.execute(
                    "UPDATE tm_meta SET value = 'ACTIVE' "
                    "WHERE key = 'activation_status' AND value = 'SEALED'"
                )
                generation = connection.execute(
                    "UPDATE tm_meta SET value = ? "
                    "WHERE key = 'generation' AND value = '0'",
                    (str(next_generation),),
                )
                connection.execute(
                    "INSERT INTO tm_meta(key, value) VALUES "
                    "('activation_digest', ?)",
                    (activation_digest,),
                )
                if status.rowcount != 1 or generation.rowcount != 1:
                    raise port.store_schema_error(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        after = _activation_file_identity(identity.canonical_sidecar_path)
        if after != canonical_capture[0]:
            raise OSError("canonical identity changed")
        _fsync_activation_file(identity.canonical_sidecar_path, after)
    except Exception as error:
        if isinstance(error, ActivationPreparationError):
            raise
        raise ActivationPreparationError(
            "ACTIVATION.RECEIPT_PUBLICATION_FAILED",
            retryable=True,
        ) from error


def _publish_activation_manifest(
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    identity: CanonicalResourceIdentity,
) -> None:
    """Atomically publish and fsync the new adjacent manifest."""

    manifest_temp = _capture_journal_closure_file(
        record.candidate_manifest_temp_path
    )
    if (
        (manifest_temp[0].device, manifest_temp[0].inode)
        != record.candidate_manifest_temp_identity
        or manifest_temp[1] != record.manifest_temp_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_CANDIDATE_INVALID",
            retryable=False,
        )
    final_path = identity.snapshot_manifest_path
    if record.had_prior_canonical:
        if record.prior_manifest_absent:
            if _lstat_any_entry(final_path):
                raise ActivationPreparationError(
                    "ACTIVATION.MANIFEST_TARGET_OCCUPIED",
                    retryable=False,
                )
        else:
            prior = _capture_journal_closure_file(final_path)
            if (
                (prior[0].device, prior[0].inode)
                != record.prior_manifest_identity
                or prior[1] != record.prior_manifest_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PRIOR_ASSET_INVALID",
                    retryable=False,
                )
    elif _lstat_any_entry(final_path):
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_TARGET_OCCUPIED",
            retryable=False,
        )
    try:
        _replace_activation_file(record.candidate_manifest_temp_path, final_path)
        _fsync_activation_directory(final_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_PUBLICATION_FAILED",
            retryable=True,
        ) from error
    final = _capture_journal_closure_file(final_path)
    if (
        final[0] != manifest_temp[0]
        or final[1] != record.new_manifest_digest
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_PUBLICATION_UNPROVEN",
            retryable=False,
        )
    payload = _read_activation_file_bytes(
        _PriorAssetCapture("MANIFEST", final_path, final[0], final[1])
    )
    try:
        decoded = contract_from_json(payload.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_PUBLICATION_INVALID",
            retryable=False,
        ) from error
    if (
        type(decoded) is not SnapshotManifest
        or decoded != preparation._sealed_stage.evidence.source_binding.manifest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.MANIFEST_PUBLICATION_INVALID",
            retryable=False,
        )


def _activation_exact_parity_digest(
    port: _StoreValidationPort,connection: sqlite3.Connection) -> str:
    return _activation_winners_parity_digest(port, connection)


def _activation_winners_parity_digest(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    *,
    boundary: int | None = None,
) -> str:
    """Last-valid-wins exact winners digest over one record set.

    Without a boundary the full store is covered; with a boundary the
    digest covers records up to ``boundary`` so a schema-upgrade
    canonical whose binding receipt describes a historical export
    (``VERIFIED_HISTORY``) can be re-proven against its configured
    JSONL snapshot without comparing the full store.
    """

    if boundary is None:
        rows = connection.execute(
            "SELECT source_raw, target_raw FROM tm_record "
            "ORDER BY record_id"
        )
    else:
        rows = connection.execute(
            "SELECT source_raw, target_raw FROM tm_record "
            "WHERE record_id <= ? ORDER BY record_id",
            (boundary,),
        )
    winners: dict[str, str] = {}
    for source_raw, target_raw in rows:
        if type(source_raw) is not str or type(target_raw) is not str:
            raise port.store_schema_error("STORE.RECORD_INVALID")
        winners[source_raw] = target_raw
    digest = hashlib.sha256()
    for source_raw in sorted(winners):
        for value in (source_raw, winners[source_raw]):
            encoded = value.encode("utf-8")
            digest.update(str(len(encoded)).encode("ascii"))
            digest.update(b":")
            digest.update(encoded)
            digest.update(b";")
    return digest.hexdigest()


def _activation_boundary_parity_digest(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    boundary: int,
) -> str:
    """Winners parity digest over the records at one exported revision."""

    return _activation_winners_parity_digest(
        port,
        connection,
        boundary=boundary,
    )


def _schema_upgrade_marker(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
) -> str | None:
    """The durable schema-upgrade origin marker, or None when absent.

    A marker-bearing canonical was published by the Task 5.11 schema
    upgrade: its binding receipt legitimately describes a historical
    export whose record count and JSONL parity are a subset of the
    current store, so the record-count and JSONL-parity closures below
    compare against the exported-revision boundary instead of the full
    store.  Any malformed or foreign marker fails closed.
    """

    marker = port.read_meta(connection).get(_SCHEMA_UPGRADE_META_KEY)
    if marker is None:
        return None
    if marker != _SCHEMA_UPGRADE_META_VALUE:
        raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
    return marker


def _receipt_boundary_record_count(
    port: _StoreValidationPort,
    facts: Any,
    receipt: SnapshotReceipt,
) -> int:
    """The proven record count at one receipt's exported revision."""

    record_count_at_revision = {0: 0}
    record_count_at_revision.update(
        dict(getattr(facts, "cumulative_record_counts", ()))
    )
    boundary = record_count_at_revision.get(receipt.exported_revision)
    if type(boundary) is not int or boundary < 0:
        raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
    return boundary


def _validate_activation_indexes(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    *,
    semantic_facts: ContentSemanticFacts,
    fts5_available: bool,
) -> tuple[tuple[tuple[int, int], ...], int]:
    required_sizes = tuple(
        size for size, _count in semantic_facts.gram_counts
    )
    actual_count_rows, actual_fts_count = port.validate_candidate_proof_index(
        connection,
        required_sizes=required_sizes,
        fts5_available=fts5_available,
    )
    if dict(actual_count_rows) != dict(semantic_facts.gram_counts):
        raise port.store_schema_error("STORE.CANDIDATE_INDEX_INVALID")
    if fts5_available:
        if actual_fts_count != semantic_facts.fts_count:
            raise port.store_schema_error("STORE.CANDIDATE_INDEX_INVALID")
    elif semantic_facts.fts_count != 0:
        raise port.store_schema_error("STORE.CANDIDATE_INDEX_INVALID")
    return actual_count_rows, actual_fts_count


def _validate_published_activation_set(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation | None,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
) -> tuple[_CanonicalStoreRef, Any, ActiveContentAttestation]:
    """Revalidate the complete active DB/source/receipt/manifest generation."""

    database = _capture_journal_closure_file(identity.canonical_sidecar_path)
    manifest = _capture_journal_closure_file(identity.snapshot_manifest_path)
    if (
        (database[0].device, database[0].inode)
        != record.candidate_stage_db_identity
        or (manifest[0].device, manifest[0].inode)
        != record.candidate_manifest_temp_identity
        or manifest[1] != record.new_manifest_digest
        or _lstat_any_entry(record.candidate_stage_db_path)
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_SET_INVALID",
            retryable=False,
        )
    active_ref = _canonical_activation_ref(identity, journal_id=record.journal_id)
    try:
        snapshot = port.inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=next_generation,
            _expected_activation_digest=activation_digest,
        )
        with port.open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                port.validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if connection.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise port.store_schema_error("STORE.INTEGRITY_CHECK_FAILED")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise port.store_schema_error("STORE.FOREIGN_KEY_CHECK_FAILED")
                sealed_semantic = record.sealed_content_attestation.semantic_facts
                schema_upgrade = _schema_upgrade_marker(port, connection)
                record_count = port.table_count(connection, "tm_record")
                origin_batch_count = port.table_count(
                    connection,
                    "tm_origin_batch",
                )
                exact_parity_digest = _activation_exact_parity_digest(
                    port,
                    connection,
                )
                if (
                    record_count != sealed_semantic.record_count
                    or origin_batch_count != sealed_semantic.origin_batch_count
                    or exact_parity_digest
                    != sealed_semantic.exact_parity_digest
                ):
                    raise port.store_schema_error("STORE.ACTIVE_COUNT_MISMATCH")
                gram_counts, fts_count = _validate_activation_indexes(port,
                    connection,
                    semantic_facts=sealed_semantic,
                    fts5_available=snapshot.fts5_available,
                )
                lease = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=canonical_store_id,
                    generation=next_generation,
                    fts5_available=snapshot.fts5_available,
                )
                facts = port.read_source_binding_facts_in_transaction(
                    connection,
                    lease,
                )
                if (
                    facts.binding is None
                    or facts.divergence_latched
                    or facts.diagnostic_codes
                    or facts.binding.receipt.snapshot_id
                    != record.new_receipt_id
                    or snapshot_receipt_digest(facts.binding.receipt)
                    != record.snapshot_receipt_digest
                    or facts.binding.receipt.jsonl_digest
                    != record.source_jsonl_digest
                    or port.configured_pair_diagnostics(
                        facts.binding,
                        identity=identity,
                        canonical_store_id=canonical_store_id,
                        head_revision=facts.head_revision,
                        cumulative_record_counts=facts.cumulative_record_counts,
                    )
                ):
                    raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
                logical_closure_digest = port.stage_closure_digest(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except Exception as error:
        if isinstance(error, ActivationPreparationError):
            raise
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_SET_INVALID",
            retryable=False,
        ) from error
    try:
        database_proof = _capture_content_file(identity.canonical_sidecar_path)
        manifest_proof = _capture_content_file(identity.snapshot_manifest_path)
        source_proof = _capture_content_file(identity.configured_jsonl_path)
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_SET_INVALID",
            retryable=False,
        ) from error
    sealed = record.sealed_content_attestation
    if (
        (database_proof.device, database_proof.inode)
        != (sealed.database.device, sealed.database.inode)
        or manifest_proof != sealed.manifest
        or source_proof != sealed.source
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_SET_INVALID",
            retryable=False,
        )
    sealed_semantic = sealed.semantic_facts
    semantic_facts = ContentSemanticFacts(
        schema_version=snapshot.schema_version,
        schema_digest=sealed_semantic.schema_digest,
        fold_version=snapshot.fold_version,
        index_version=snapshot.candidate_index_version,
        candidate_index_kind=snapshot.candidate_index_kind,
        fts5_available=snapshot.fts5_available,
        sqlite_runtime_version=snapshot.sqlite_runtime_version,
        unicode_runtime_version=snapshot.unicode_runtime_version,
        journal_mode=snapshot.journal_mode,
        synchronous=snapshot.synchronous,
        foreign_keys=snapshot.foreign_keys,
        busy_timeout_ms=snapshot.busy_timeout_ms,
        wal_enabled=snapshot.wal_enabled,
        extension_loading_enabled=snapshot.extension_loading_enabled,
        record_count=record_count,
        origin_batch_count=origin_batch_count,
        origin_batch_id=sealed_semantic.origin_batch_id,
        origin_batch_kind=sealed_semantic.origin_batch_kind,
        exported_revision=facts.binding.receipt.exported_revision,
        fts_count=fts_count,
        gram_counts=gram_counts,
        exact_parity_digest=exact_parity_digest,
        logical_closure_digest=logical_closure_digest,
    )
    active_attestation = _create_active_content_attestation(
        sealed_attestation_digest=sealed.attestation_digest,
        journal_id=record.journal_id,
        resource_id=record.resource_id,
        target_identity=record.target_identity,
        canonical_store_id=canonical_store_id,
        snapshot_receipt_digest=record.snapshot_receipt_digest,
        generation=next_generation,
        activation_digest=activation_digest,
        database=database_proof,
        manifest=manifest_proof,
        source=source_proof,
        semantic_facts=semantic_facts,
    )
    return active_ref, snapshot, active_attestation


def _revalidate_active_content_attestation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
    attestation: ActiveContentAttestation | None = None,
) -> tuple[_CanonicalStoreRef, Any]:
    """Rehash exact active bytes and reopen health without semantic scans."""

    active = (
        record.active_content_attestation
        if attestation is None
        else attestation
    )
    sealed = record.sealed_content_attestation
    if type(active) is not ActiveContentAttestation or (
        active.sealed_attestation_digest != sealed.attestation_digest
        or active.journal_id != record.journal_id
        or active.resource_id != record.resource_id
        or active.target_identity != record.target_identity
        or active.canonical_store_id != canonical_store_id
        or active.snapshot_receipt_digest != record.snapshot_receipt_digest
        or active.generation != next_generation
        or active.activation_digest != activation_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
            retryable=False,
        )
    try:
        database = _capture_content_file(identity.canonical_sidecar_path)
    except ContentAttestationError as error:
        code = (
            "ACTIVATION.ACTIVE_ATTESTATION_ASSET_MISSING"
            if error.error_code == "CONTENT_ATTESTATION.FILE_MISSING"
            else "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID"
        )
        raise ActivationPreparationError(
            code,
            retryable=False,
        ) from error
    try:
        manifest = _capture_content_file(identity.snapshot_manifest_path)
    except ContentAttestationError as error:
        code = (
            "ACTIVATION.ACTIVE_ATTESTATION_ASSET_MISSING"
            if error.error_code == "CONTENT_ATTESTATION.FILE_MISSING"
            else "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID"
        )
        raise ActivationPreparationError(
            code,
            retryable=False,
        ) from error
    try:
        source = _capture_content_file(identity.configured_jsonl_path)
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        ) from error
    if (
        (database.device, database.inode)
        != (active.database.device, active.database.inode)
        or (manifest.device, manifest.inode)
        != (active.manifest.device, active.manifest.inode)
        or (source.device, source.inode)
        != (active.source.device, active.source.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        )
    if (
        database != active.database
        or manifest != active.manifest
        or source != active.source
        or _lstat_any_entry(record.candidate_stage_db_path)
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
            retryable=False,
        )
    active_ref = _canonical_activation_ref(identity, journal_id=record.journal_id)
    try:
        snapshot = port.inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=next_generation,
            _expected_activation_digest=activation_digest,
        )
        with port.open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [
                ("ok",)
            ]:
                raise port.store_schema_error("STORE.INTEGRITY_CHECK_FAILED")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise port.store_schema_error("STORE.FOREIGN_KEY_CHECK_FAILED")
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
            retryable=False,
        ) from error
    return active_ref, snapshot


def _restore_activation_file(
    record: _ActivationJournalRecord,
    backup_path: Path,
    expected_digest: str,
    destination: Path,
) -> _ActivationFileIdentity:
    """Restore one journal-owned backup to its authority path (Task 5.9).

    Streams the backup into an exclusive deterministic temporary in the
    destination directory, fsyncs the copy, atomically replaces the
    destination, fsyncs the published file and the parent directory, and
    re-proves the published identity.  The backup itself is never consumed,
    so a crash at any boundary resumes idempotently from the journal; a
    leftover temporary from a crashed restore is removed only when it is
    an exact regular single-link file at the deterministic path.
    """

    temp_path = destination.parent / (
        f"{destination.name}.localcat-rollback.{record.journal_id}.tmp"
    )
    _remove_orphaned_rollback_temp(temp_path)
    source_descriptor = -1
    temp_descriptor = -1
    temp_identity: _ActivationFileIdentity | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_descriptor = os.open(backup_path, flags)
        source_observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_observed.st_mode)
            or source_observed.st_nlink != 1
        ):
            raise OSError("rollback backup is not an exact single-link file")
        temp_descriptor, temp_identity = _open_activation_journal_temp(
            temp_path
        )
        _write_recovery_backup(source_descriptor, temp_descriptor)
        _fsync_recovery_backup(temp_descriptor)
        os.close(source_descriptor)
        source_descriptor = -1
        os.close(temp_descriptor)
        temp_descriptor = -1
        assert temp_identity is not None
        temp_capture = _capture_activation_file(
            temp_path,
            asset_kind="JOURNAL_CLOSURE",
        )
        if (
            temp_capture.identity != temp_identity
            or temp_capture.digest != expected_digest
        ):
            raise OSError("rollback temporary content mismatch")
        os.replace(temp_path, destination)
        published_identity = _activation_file_identity(destination)
        _fsync_activation_file(destination, published_identity)
        _fsync_activation_directory(destination.parent)
        return published_identity
    except ActivationPreparationError:
        raise
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ROLLBACK_RESTORE_FAILED",
            retryable=True,
        ) from error
    finally:
        if source_descriptor >= 0:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if temp_descriptor >= 0:
            try:
                os.close(temp_descriptor)
            except OSError:
                pass


def _require_rollback_backups(
    record: _ActivationJournalRecord,
) -> tuple[tuple[Path, str], tuple[Path, str] | None]:
    """Verify the journal-owned prior backups are present and intact.

    The durable journal is the only surviving ownership locator, so each
    backup path, identity, and digest comes from the journal record itself.
    A missing or mutated backup (or a foreign/hardlinked entry) fails closed
    before any mutation: without a restorable prior authority the pending
    journal must stay recoverable for manual intervention.  A replacement
    whose prior manifest was absent owns no manifest backup and returns
    ``None`` for it.
    """

    if not record.had_prior_canonical:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_BACKUP_INVALID",
            retryable=False,
        )
    owned: list[tuple[Path, str]] = []
    for path, identity_value, digest_value in (
        (
            record.prior_db_backup_path,
            record.prior_db_backup_identity,
            record.prior_db_backup_digest,
        ),
        (
            record.prior_manifest_backup_path,
            record.prior_manifest_backup_identity,
            record.prior_manifest_backup_digest,
        ),
    ):
        if record.prior_manifest_absent and path is None:
            continue
        if path is None or identity_value is None or digest_value is None:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=False,
            )
        try:
            observed = os.lstat(path)
        except FileNotFoundError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=False,
            ) from error
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=True,
            ) from error
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != identity_value
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=False,
            )
        try:
            capture = _capture_activation_file(
                path,
                asset_kind="JOURNAL_CLOSURE",
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=False,
            ) from error
        if capture.digest != digest_value:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
                retryable=False,
            )
        owned.append((path, digest_value))
    if record.prior_manifest_absent:
        return owned[0], None
    return owned[0], owned[1]


def _rollback_restored_prior_view(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    fts5_available: bool,
) -> _SQLiteGenerationView:
    """Build the one restored prior generation view for a rollback."""

    prior_db_path = record.prior_db_path
    assert prior_db_path is not None
    prior_manifest_path = (
        identity.snapshot_manifest_path
        if record.prior_manifest_absent
        else record.prior_manifest_path
    )
    assert prior_manifest_path is not None
    if prior_db_path == identity.canonical_sidecar_path:
        prior_ref: _CanonicalStoreRef | _PriorActivationRef = (
            _canonical_activation_ref(
                identity,
                journal_id=record.journal_id,
            )
        )
    else:
        prior_ref = _PriorActivationRef(
            stage_id=f"prior.{record.journal_id}",
            resource_identity=identity,
            staged_db_path=prior_db_path,
            manifest_temp_path=prior_manifest_path,
        )
    return _SQLiteGenerationView(
        stage=prior_ref,
        canonical_store_id=canonical_store_id,
        generation=(
            0 if record.prior_generation is None else record.prior_generation
        ),
        fts5_available=fts5_available,
    )


def _recovery_artifact_seal_digest(
    record: _ActivationJournalRecord,
) -> str:
    """Re-prove the journal's artifact seal digest from its own fields.

    Mirrors ``tm_contracts._artifact_seal_digest`` exactly; the durable
    journal is the only surviving token/artifact authority after a restart,
    so the cross-field closure must hold without any in-memory registry.
    """

    return contract_module._stable_digest(
        {
            "artifact_id": record.artifact_id,
            "canonical_store_id": record.canonical_store_id,
            "evidence_digest": record.evidence_digest,
            "manifest_temp_path": str(record.candidate_manifest_temp_path),
            "registry_namespace": record.registry_namespace,
            "resource_id": record.resource_id,
            "staged_db_path": str(record.candidate_stage_db_path),
            "target_identity": record.target_identity,
        }
    )


def _recovery_sealed_stage_digest(
    record: _ActivationJournalRecord,
) -> str:
    """Re-prove the journal's sealed stage digest from its own fields.

    Mirrors ``tm_contracts._sealed_stage_contract_digest`` exactly and binds
    the token nonce, artifact, evidence, and expected generation together.
    """

    return contract_module._stable_digest(
        {
            "activation_nonce": record.activation_nonce,
            "artifact_id": record.artifact_id,
            "artifact_seal_digest": record.artifact_seal_digest,
            "canonical_store_id": record.canonical_store_id,
            "evidence_digest": record.evidence_digest,
            "expected_prior_generation": record.expected_prior_generation,
            "registry_namespace": record.registry_namespace,
            "resource_id": record.resource_id,
            "snapshot_receipt_digest": record.snapshot_receipt_digest,
            "target_identity": record.target_identity,
        }
    )


def _recovery_expected_manifest_bytes(
    receipt: SnapshotReceipt,
) -> bytes:
    """Deterministic adjacent manifest bytes for one activation receipt."""

    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return contract_to_json(manifest).encode("utf-8")


def _recovery_jsonl_winners_digest(
    port: _StoreValidationPort,
    path: Path,
) -> str:
    """Last-valid-wins exact winners digest over the configured JSONL."""

    accepted_row = port.accepted_jsonl_row

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    winners: dict[str, str] = {}
    with path.open("rb") as stream:
        for raw_line in stream:
            try:
                decoded_line = raw_line.decode("utf-8")
                payload = json.loads(
                    decoded_line,
                    parse_constant=reject_non_finite,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ):
                continue
            row = accepted_row(payload)
            if row is None:
                continue
            winners[row[0]] = row[1]
    digest = hashlib.sha256()
    for source_raw in sorted(winners):
        for value in (source_raw, winners[source_raw]):
            encoded = value.encode("utf-8")
            digest.update(str(len(encoded)).encode("ascii"))
            digest.update(b":")
            digest.update(encoded)
            digest.update(b";")
    return digest.hexdigest()


def _recover_activation_indexes(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
) -> None:
    """Re-prove gram/FTS index closure without in-memory sealed evidence.

    Gram sizes are fixed by the schema capability (1-2 with FTS5, 1-3 with
    the fallback index), exactly as the StageSealer derives them, so the
    per-record expected gram set and per-size counts are independently
    recomputable from the records themselves.
    """

    required_sizes = (1, 2) if fts5_available else (1, 2, 3)
    _ = port.validate_candidate_proof_index(
        connection,
        required_sizes=required_sizes,
        fts5_available=fts5_available,
    )


def _recovery_receipt_row(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
) -> tuple[SnapshotReceipt, str]:
    """Read exactly one receipt ledger row; fail closed otherwise."""

    rows = connection.execute(
        "SELECT snapshot_id, resource_id, canonical_store_id, "
        "exported_revision, jsonl_digest, record_count, format_version, "
        "destination_jsonl_path, destination_manifest_path, status "
        "FROM tm_snapshot_receipt ORDER BY snapshot_id"
    ).fetchall()
    if len(rows) != 1:
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    row = rows[0]
    if (
        type(row[0]) is not str
        or type(row[1]) is not str
        or type(row[2]) is not str
        or type(row[3]) is not int
        or type(row[4]) is not str
        or type(row[5]) is not int
        or type(row[6]) is not str
        or type(row[7]) is not str
        or type(row[8]) is not str
        or type(row[9]) is not str
    ):
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    receipt = SnapshotReceipt(
        snapshot_id=row[0],
        resource_id=row[1],
        canonical_store_id=row[2],
        exported_revision=row[3],
        jsonl_digest=row[4],
        record_count=row[5],
        format_version=row[6],
    )
    return receipt, row[9]


def _recovery_completed_binding(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    record: _ActivationJournalRecord,
) -> SnapshotBinding:
    """Read the single completed binding and prove its journal closure."""

    rows = connection.execute(
        "SELECT b.configured_jsonl_path, b.manifest_path, "
        "b.snapshot_kind, b.binding_version, "
        "r.snapshot_id, r.resource_id, r.canonical_store_id, "
        "r.exported_revision, r.jsonl_digest, r.record_count, "
        "r.format_version, r.destination_jsonl_path, "
        "r.destination_manifest_path, r.status "
        "FROM tm_snapshot_binding AS b "
        "LEFT JOIN tm_snapshot_receipt AS r "
        "ON r.snapshot_id = b.snapshot_id "
        "WHERE b.binding_id = 1"
    ).fetchall()
    if len(rows) != 1:
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    binding = port.binding_from_ledger_row(rows[0])
    if rows[0][13] != "completed":
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    if (
        binding.configured_jsonl_path != identity.configured_jsonl_path
        or binding.manifest_path != identity.snapshot_manifest_path
        or binding.receipt.resource_id != identity.resource_id
        or binding.receipt.canonical_store_id != canonical_store_id
        or binding.receipt.snapshot_id != record.new_receipt_id
        or snapshot_receipt_digest(binding.receipt)
        != record.snapshot_receipt_digest
        or binding.receipt.jsonl_digest != record.source_jsonl_digest
    ):
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    return binding


def _preflight_recovered_manifest(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
) -> None:
    """Prove the DB_REPLACED manifest window before any receipt mutation.

    The candidate temporary must still be exactly the journal's file, or
    the final manifest must already be exactly the published new file
    (crash after the replace, before the MANIFEST_PUBLISHED journal).
    Anything else fail-stops before the receipt is touched.
    """

    try:
        if _lstat_any_entry(record.candidate_manifest_temp_path):
            temporary = _capture_journal_closure_file(
                record.candidate_manifest_temp_path
            )
            if (
                (temporary[0].device, temporary[0].inode)
                != record.candidate_manifest_temp_identity
                or temporary[1] != record.manifest_temp_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                    retryable=False,
                )
            return
        published = _capture_journal_closure_file(
            identity.snapshot_manifest_path
        )
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    if (
        (published[0].device, published[0].inode)
        != record.candidate_manifest_temp_identity
        or published[1] != record.new_manifest_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )


def _recovery_prior_completed_binding(
    port: _StoreValidationPort,
    connection: sqlite3.Connection,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    record: _ActivationJournalRecord,
) -> SnapshotBinding:
    """Read the single completed prior binding and prove journal closure."""

    rows = connection.execute(
        "SELECT b.configured_jsonl_path, b.manifest_path, "
        "b.snapshot_kind, b.binding_version, "
        "r.snapshot_id, r.resource_id, r.canonical_store_id, "
        "r.exported_revision, r.jsonl_digest, r.record_count, "
        "r.format_version, r.destination_jsonl_path, "
        "r.destination_manifest_path, r.status "
        "FROM tm_snapshot_binding AS b "
        "LEFT JOIN tm_snapshot_receipt AS r "
        "ON r.snapshot_id = b.snapshot_id "
        "WHERE b.binding_id = 1"
    ).fetchall()
    if len(rows) != 1:
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    binding = port.binding_from_ledger_row(rows[0])
    if rows[0][13] != "completed":
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    if (
        binding.configured_jsonl_path != identity.configured_jsonl_path
        or binding.manifest_path != identity.snapshot_manifest_path
        or binding.receipt.resource_id != identity.resource_id
        or binding.receipt.canonical_store_id != canonical_store_id
        or binding.receipt.snapshot_id != record.prior_binding_snapshot_id
        or snapshot_receipt_digest(binding.receipt)
        != record.prior_receipt_digest
    ):
        raise port.store_schema_error("STORE.RECEIPT_INVALID")
    return binding


def _recovery_mismatch(
    error: ActivationPreparationError,
) -> ActivationPreparationError:
    """Re-surface one non-recovery activation error as a Task 5.8 mismatch."""

    if error.code.startswith("ACTIVATION.RECOVERY"):
        return error
    return ActivationPreparationError(
        "ACTIVATION.RECOVERY_MISMATCH",
        retryable=False,
        reason_code=error.code,
    )


def _recovery_capture_journal_file(
    path: Path,
) -> tuple[_ActivationFileIdentity, str]:
    """Capture one journal-closure file with a Task 5.8 fail-stop code."""

    try:
        return _capture_journal_closure_file(path)
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error


def _revalidate_recovered_prior_set(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    allow_restored_identities: bool = False,
) -> bool:
    """Re-prove the prior DB/manifest/binding from disk.

    The prior generation is exactly what a PREPARED cancellation restores,
    so every journal-recorded prior fact (database, manifest, source,
    binding receipt, generation) is re-captured and re-proven before the
    journal is retired.  ``allow_restored_identities`` is the Task 5.9
    rollback relaxation: a rollback restores the prior pair as byte-identical
    copies from the journal-authenticated backups, which necessarily changes
    the file identities while preserving every digest.  The strict mode
    (unchanged prior, PREPARED cancellation) still binds the exact recorded
    identities; the relaxed mode binds content digests plus the unchanged
    source identity/digest.  Returns the prior database's FTS5 capability
    for the restored view.
    """

    if not record.had_prior_canonical or record.prior_db_path is None:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    if (
        (record.prior_manifest_path is None) != record.prior_manifest_absent
        or (
            record.prior_manifest_path is not None
            and record.prior_manifest_path
            != identity.snapshot_manifest_path
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    prior_db_path = record.prior_db_path
    prior_manifest_path = (
        identity.snapshot_manifest_path
        if record.prior_manifest_absent
        else record.prior_manifest_path
    )
    assert prior_db_path is not None
    assert prior_manifest_path is not None
    if prior_db_path == identity.canonical_sidecar_path:
        prior_ref: _CanonicalStoreRef | _PriorActivationRef = (
            _canonical_activation_ref(
                identity,
                journal_id=record.journal_id,
            )
        )
    else:
        prior_ref = _PriorActivationRef(
            stage_id=f"prior.{record.journal_id}",
            resource_identity=identity,
            staged_db_path=prior_db_path,
            manifest_temp_path=prior_manifest_path,
        )
    prior_view = _SQLiteGenerationView(
        stage=prior_ref,
        canonical_store_id=canonical_store_id,
        generation=(
            0 if record.prior_generation is None else record.prior_generation
        ),
        fts5_available=False,
    )
    try:
        _prior_captures = _capture_prior_assets(port,
            prior_view,
            identity=identity,
            replacement=record.prior_canonical_store_id is not None,
        )
        _prior_by_kind = {
            capture.asset_kind: capture for capture in _prior_captures
        }
        database = _prior_by_kind["DATABASE"]
        manifest = _prior_by_kind.get("MANIFEST")
        source = _prior_by_kind["SOURCE"]
        with port.open_configured_connection(
            prior_db_path,
            require_existing=True,
        ) as connection:
            meta = port.read_meta(connection)
            prior_status = meta.get("activation_status")
            if prior_status == "UNPUBLISHED":
                if (
                    record.prior_generation != 0
                    or "activation_digest" in meta
                ):
                    raise port.store_schema_error(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
            elif prior_status == "ACTIVE":
                if (
                    port.meta_int(meta, "generation")
                    != record.prior_generation
                    or meta.get("activation_digest") is None
                ):
                    raise port.store_schema_error(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
            else:
                raise port.store_schema_error(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
            _recovery_prior_completed_binding(port,
                connection,
                identity=identity,
                canonical_store_id=canonical_store_id,
                record=record,
            )
            fts5_available = port.meta_bool(meta, "fts5_available")
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    except (OSError, sqlite3.Error, port.store_schema_error) as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
            retryable=False,
            reason_code=getattr(error, "error_code", None),
        ) from error
    if (
        database.digest != record.prior_db_digest
        or (source.identity.device, source.identity.inode)
        != record.source_jsonl_identity
        or source.digest != record.source_jsonl_digest
        or (
            not allow_restored_identities
            and (database.identity.device, database.identity.inode)
            != record.prior_db_identity
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
            retryable=False,
        )
    if record.prior_manifest_absent:
        if manifest is not None:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
                retryable=False,
            )
    else:
        if manifest is None or (
            manifest.digest != record.prior_manifest_digest
            or (
                not allow_restored_identities
                and (
                    manifest.identity.device,
                    manifest.identity.inode,
                )
                != record.prior_manifest_identity
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
                retryable=False,
            )
    return fts5_available


def _revalidate_recovered_sealed_database(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    stage_ref: _StoreRuntimeRef,
    database_path: Path,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> None:
    """Cold rehash sealed bytes and reopen schema/integrity/FK."""

    sealed = record.sealed_content_attestation
    if not _lstat_any_entry(database_path):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    try:
        database = _capture_content_file(database_path)
        manifest = _capture_content_file(record.candidate_manifest_temp_path)
        source = _capture_content_file(identity.configured_jsonl_path)
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        ) from error
    if (
        database != sealed.database
        or manifest != sealed.manifest
        or source != sealed.source
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )
    try:
        snapshot = port.inspect_stage_schema(
            stage_ref,
            canonical_store_id=canonical_store_id,
            _allow_sealed=True,
        )
        with port.open_configured_connection(
            database_path,
            require_existing=True,
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [
                ("ok",)
            ]:
                raise port.store_schema_error("STORE.INTEGRITY_CHECK_FAILED")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise port.store_schema_error("STORE.FOREIGN_KEY_CHECK_FAILED")
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
            reason_code=getattr(error, "error_code", None),
        ) from error
    if snapshot.activation_status != "SEALED":
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )


def _revalidate_recovered_active_set(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
    require_manifest_published: bool,
) -> Any:
    """Cold rehash a durable active attestation and reopen health."""

    if not require_manifest_published:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        )
    try:
        _active_ref, snapshot = _revalidate_active_content_attestation(
            port,
            record,
            identity=identity,
            canonical_store_id=canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
        )
    except ActivationPreparationError as error:
        if error.code == "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID":
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ASSET_MUTATED",
                retryable=False,
            ) from error
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        ) from error
    return snapshot


def _revalidate_discovered_active_set(
    port: _StoreValidationPort,
    identity: CanonicalResourceIdentity,
    *,
    canonical_store_id: str,
) -> tuple[int, bool]:
    """Re-prove one complete active canonical generation from disk alone.

    Task 5.8 terminal authority when no journal survives: the deterministic
    canonical sidecar and adjacent manifest must form one fully completed
    activation.  Every fact (schema, meta generation/activation digest,
    integrity, receipt/binding closure, manifest bytes, exact parity, and
    index closure) is recomputed from disk and must agree before the
    generation view is authorized; a foreign or tampered pair fails closed
    and never authorizes a store.  Returns ``(generation, fts5_available)``.
    """

    try:
        db_path = identity.canonical_sidecar_path
        manifest_path = identity.snapshot_manifest_path
        if not _lstat_any_entry(db_path):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                retryable=False,
            )
        if not _lstat_any_entry(manifest_path):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                retryable=False,
            )
        active_ref = _canonical_activation_ref(
            identity,
            journal_id="discovery",
        )
        with port.open_configured_connection(
            db_path,
            require_existing=True,
        ) as connection:
            meta = port.read_meta(connection)
            if meta.get("activation_status") != "ACTIVE":
                raise port.store_schema_error(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
            generation = port.meta_int(meta, "generation")
            activation_digest = meta.get("activation_digest")
            if (
                type(activation_digest) is not str
                or len(activation_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in activation_digest
                )
            ):
                raise port.store_schema_error(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
        snapshot = port.inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=generation,
            _expected_activation_digest=activation_digest,
        )
        with port.open_configured_connection(
            db_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                port.validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if connection.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise port.store_schema_error(
                        "STORE.INTEGRITY_CHECK_FAILED"
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise port.store_schema_error(
                        "STORE.FOREIGN_KEY_CHECK_FAILED"
                    )
                lease = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=canonical_store_id,
                    generation=generation,
                    fts5_available=snapshot.fts5_available,
                )
                facts = port.read_source_binding_facts_in_transaction(
                    connection,
                    lease,
                )
                if (
                    facts.binding is None
                    or facts.divergence_latched
                    or facts.diagnostic_codes
                ):
                    raise port.store_schema_error(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                binding = facts.binding
                receipt_row, receipt_status = _recovery_receipt_row(port,
                    connection
                )
                if (
                    receipt_status != "completed"
                    or receipt_row != binding.receipt
                ):
                    raise port.store_schema_error("STORE.RECEIPT_INVALID")
                schema_upgrade = _schema_upgrade_marker(port, connection)
                if (
                    schema_upgrade is None
                    and port.table_count(connection, "tm_record")
                    != binding.receipt.record_count
                ):
                    raise port.store_schema_error("STORE.ACTIVE_COUNT_MISMATCH")
                jsonl_parity = _recovery_jsonl_winners_digest(port,
                    identity.configured_jsonl_path
                )
                if schema_upgrade is not None:
                    boundary = _receipt_boundary_record_count(
                        port,
                        facts,
                        binding.receipt,
                    )
                    parity_matches = (
                        _activation_boundary_parity_digest(
                            port,
                            connection,
                            boundary,
                        )
                        == jsonl_parity
                    )
                else:
                    parity_matches = (
                        _activation_exact_parity_digest(port, connection)
                        == jsonl_parity
                    )
                if not parity_matches:
                    raise port.store_schema_error("STORE.ACTIVE_COUNT_MISMATCH")
                _recover_activation_indexes(port,
                    connection,
                    fts5_available=snapshot.fts5_available,
                )
                pair_diagnostics = port.configured_pair_diagnostics(
                    binding,
                    identity=identity,
                    canonical_store_id=canonical_store_id,
                    head_revision=facts.head_revision,
                    cumulative_record_counts=facts.cumulative_record_counts,
                )
                if pair_diagnostics:
                    raise port.store_schema_error(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except ActivationPreparationError as error:
        raise error
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_DISCOVERY_FAILED",
            retryable=True,
        ) from error
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
            reason_code=getattr(error, "error_code", None),
        ) from error
    return generation, snapshot.fts5_available


def _complete_recovered_receipt(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
) -> None:
    """Idempotently finish the issued receipt/binding publication.

    A SEALED database still holding the exact issued receipt is completed in
    one transaction (receipt completed, binding inserted, ACTIVE, generation,
    activation digest); an already ACTIVE database whose completed
    receipt/binding and meta exactly match the journal is accepted without
    rewriting.  Both branches fsync the database and revalidate its identity.
    """

    canonical_capture = _capture_journal_closure_file(
        identity.canonical_sidecar_path
    )
    if (
        (canonical_capture[0].device, canonical_capture[0].inode)
        != record.candidate_stage_db_identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )
    try:
        with port.open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                meta = port.read_meta(connection)
                status = meta.get("activation_status")
                if status == "SEALED":
                    if canonical_capture[1] != record.stage_db_digest:
                        raise port.store_schema_error(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                    receipt, receipt_status = _recovery_receipt_row(port,
                        connection
                    )
                    if receipt_status != "issued":
                        raise port.store_schema_error("STORE.RECEIPT_INVALID")
                    if (
                        receipt.snapshot_id != record.new_receipt_id
                        or snapshot_receipt_digest(receipt)
                        != record.snapshot_receipt_digest
                        or receipt.jsonl_digest != record.source_jsonl_digest
                        or receipt.resource_id != identity.resource_id
                        or receipt.canonical_store_id != canonical_store_id
                    ):
                        raise port.store_schema_error("STORE.RECEIPT_INVALID")
                    if connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone() != (0,):
                        raise port.store_schema_error("STORE.BINDING_INVALID")
                    if (
                        port.meta_int(meta, "generation") != 0
                        or "activation_digest" in meta
                    ):
                        raise port.store_schema_error(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                    connection.commit()
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        current_meta = port.read_meta(connection)
                        if (
                            current_meta.get("activation_status")
                            != "SEALED"
                            or port.meta_int(current_meta, "generation") != 0
                            or "activation_digest" in current_meta
                        ):
                            raise port.store_schema_error(
                                "STORE.ACTIVATION_STATE_INVALID"
                            )
                        current_receipt, current_status = (
                            _recovery_receipt_row(port, connection)
                        )
                        if (
                            current_receipt != receipt
                            or current_status != "issued"
                        ):
                            raise port.store_schema_error(
                                "STORE.RECEIPT_INVALID"
                            )
                        updated = connection.execute(
                            "UPDATE tm_snapshot_receipt SET status = "
                            "'completed' WHERE snapshot_id = ? "
                            "AND status = 'issued'",
                            (receipt.snapshot_id,),
                        )
                        if updated.rowcount != 1:
                            raise port.store_schema_error(
                                "STORE.RECEIPT_INVALID"
                            )
                        connection.execute(
                            "INSERT INTO tm_snapshot_binding("
                            "binding_id, configured_jsonl_path, "
                            "manifest_path, snapshot_kind, snapshot_id, "
                            "binding_version) VALUES (1, ?, ?, ?, ?, ?)",
                            (
                                Path.__str__(identity.configured_jsonl_path),
                                Path.__str__(
                                    identity.snapshot_manifest_path
                                ),
                                SnapshotKind.MIGRATION_SOURCE.value,
                                receipt.snapshot_id,
                                contract_module.SNAPSHOT_BINDING_VERSION,
                            ),
                        )
                        status = connection.execute(
                            "UPDATE tm_meta SET value = 'ACTIVE' "
                            "WHERE key = 'activation_status' "
                            "AND value = 'SEALED'"
                        )
                        generation = connection.execute(
                            "UPDATE tm_meta SET value = ? "
                            "WHERE key = 'generation' AND value = '0'",
                            (str(next_generation),),
                        )
                        connection.execute(
                            "INSERT INTO tm_meta(key, value) VALUES "
                            "('activation_digest', ?)",
                            (activation_digest,),
                        )
                        if status.rowcount != 1 or generation.rowcount != 1:
                            raise port.store_schema_error(
                                "STORE.ACTIVATION_STATE_INVALID"
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                elif status == "ACTIVE":
                    _ = _recovery_completed_binding(port,
                        connection,
                        identity=identity,
                        canonical_store_id=canonical_store_id,
                        record=record,
                    )
                    if (
                        port.meta_int(meta, "generation") != next_generation
                        or meta.get("activation_digest") != activation_digest
                    ):
                        raise port.store_schema_error(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                else:
                    raise port.store_schema_error(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        after = _activation_file_identity(identity.canonical_sidecar_path)
        if after != canonical_capture[0]:
            raise OSError("canonical identity changed")
        _fsync_activation_file(identity.canonical_sidecar_path, after)
    except Exception as error:
        if isinstance(error, ActivationPreparationError):
            raise
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_RECEIPT_FAILED",
            retryable=True,
        ) from error


def _complete_recovered_manifest(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> None:
    """Idempotently finish the adjacent manifest publication.

    The candidate temporary manifest must match the journal exactly and its
    bytes must equal the deterministic manifest for the completed ledger
    receipt.  An already-published manifest (crash after the replace, before
    the MANIFEST_PUBLISHED journal) is revalidated in place without the
    temporary; otherwise the temporary is atomically replaced, fsynced, and
    revalidated.  A missing temporary with a missing or prior-owned final is
    tamper and fails closed, preserving the journal and backups.
    """

    with port.open_configured_connection(
        identity.canonical_sidecar_path,
        require_existing=True,
    ) as connection:
        connection.execute("BEGIN")
        try:
            receipt, receipt_status = _recovery_receipt_row(port, connection)
            if receipt_status != "completed":
                raise port.store_schema_error("STORE.RECEIPT_INVALID")
            if (
                receipt.snapshot_id != record.new_receipt_id
                or snapshot_receipt_digest(receipt)
                != record.snapshot_receipt_digest
                or receipt.jsonl_digest != record.source_jsonl_digest
            ):
                raise port.store_schema_error("STORE.RECEIPT_INVALID")
        finally:
            connection.rollback()
    expected_bytes = _recovery_expected_manifest_bytes(receipt)
    final_path = identity.snapshot_manifest_path
    if not _lstat_any_entry(record.candidate_manifest_temp_path):
        try:
            final_capture = _capture_activation_file(
                final_path,
                asset_kind="MANIFEST",
            )
            final_bytes = _read_activation_file_bytes(final_capture)
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                retryable=False,
            ) from error
        if (
            (
                final_capture.identity.device,
                final_capture.identity.inode,
            )
            != record.candidate_manifest_temp_identity
            or final_capture.digest != record.new_manifest_digest
            or final_bytes != expected_bytes
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                retryable=False,
            )
        _fsync_activation_file(final_path, final_capture.identity)
        return
    manifest_temp = _capture_activation_file(
        record.candidate_manifest_temp_path,
        asset_kind="MANIFEST",
    )
    if (
        (
            manifest_temp.identity.device,
            manifest_temp.identity.inode,
        )
        != record.candidate_manifest_temp_identity
        or manifest_temp.digest != record.manifest_temp_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )
    temp_bytes = _read_activation_file_bytes(manifest_temp)
    if temp_bytes != expected_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )
    if _lstat_any_entry(final_path):
        final_capture = _capture_activation_file(
            final_path,
            asset_kind="MANIFEST",
        )
        published = (
            (
                final_capture.identity.device,
                final_capture.identity.inode,
            )
            == record.candidate_manifest_temp_identity
            and final_capture.digest == record.new_manifest_digest
        )
        if published:
            if _read_activation_file_bytes(final_capture) != expected_bytes:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                    retryable=False,
                )
            _fsync_activation_file(final_path, final_capture.identity)
            return
        if record.had_prior_canonical and not record.prior_manifest_absent:
            if (
                (
                    final_capture.identity.device,
                    final_capture.identity.inode,
                )
                != record.prior_manifest_identity
                or final_capture.digest != record.prior_manifest_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                    retryable=False,
                )
        else:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_COMPLETION_INVALID",
                retryable=False,
            )
    elif record.had_prior_canonical and not record.prior_manifest_absent:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )
    try:
        _replace_activation_file(record.candidate_manifest_temp_path, final_path)
        _fsync_activation_directory(final_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MANIFEST_FAILED",
            retryable=True,
        ) from error
    final_capture = _capture_activation_file(
        final_path,
        asset_kind="MANIFEST",
    )
    if (
        final_capture.identity != manifest_temp.identity
        or final_capture.digest != record.new_manifest_digest
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )
    if _read_activation_file_bytes(final_capture) != expected_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_COMPLETION_INVALID",
            retryable=False,
        )


def _capture_journal_closure_file(
    path: Path,
) -> tuple[_ActivationFileIdentity, str]:
    try:
        capture = _capture_activation_file(path, asset_kind="JOURNAL_CLOSURE")
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
            reason_code=error.code,
        ) from error
    return capture.identity, capture.digest


def _rollback_inconsistent_activation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    journal_identity: _ActivationFileIdentity,
) -> ActivationRecoveryReport:
    """Restore exactly one complete prior/legacy authority (Task 5.9).

    Called when the durable journal authenticates but the new
    DB/receipt/binding/manifest/effect closure cannot be proven at any
    pending phase.  With a prior canonical generation the journal-owned
    backups restore the prior DB and prior manifest/binding as one set
    (atomic replace, file fsync, directory fsync, then full
    schema/identity/integrity/FK/count/receipt/binding/index/source
    revalidation); without a prior canonical every journal-owned failed
    sidecar/manifest/candidate/temporary artifact is quarantined
    deterministically and the configured JSONL remains the legacy
    authority.  Only after the restored/legacy authority is durable and
    revalidated is the PREPARED prior-closure terminal published, the
    main journal retired, and the journal-owned backups removed, so a
    crash at every boundary resumes idempotently and repeated rollback
    never creates a generation or duplicates quarantine.
    """

    identity = port.resource_identity
    canonical_store_id = port.canonical_store_id
    source = _recovery_capture_journal_file(
        identity.configured_jsonl_path
    )
    if (
        (source[0].device, source[0].inode)
        != record.source_jsonl_identity
        or source[1] != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ASSET_MUTATED",
            retryable=False,
        )
    _quarantine_failed_activation_artifacts(
        record,
        identity=identity,
    )
    if not record.had_prior_canonical:
        try:
            _require_first_activation_absence(identity)
        except ActivationPreparationError as error:
            raise _recovery_mismatch(error) from error
        fts5_available = False
        prior_generation = None
        terminal_record = replace(
            record,
            phase=_ActivationJournalPhase.PREPARED,
            active_content_attestation=None,
        )
    else:
        if (
            record.prior_db_path is None
            or (
                not record.prior_manifest_absent
                and record.prior_manifest_path is None
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
        try:
            fts5_available = _revalidate_recovered_prior_set(port,
                record,
                identity=identity,
                canonical_store_id=canonical_store_id,
                allow_restored_identities=True,
            )
        except ActivationPreparationError:
            db_backup, manifest_backup = _require_rollback_backups(record)
            _restore_activation_file(
                record,
                db_backup[0],
                db_backup[1],
                record.prior_db_path,
            )
            if manifest_backup is not None:
                assert record.prior_manifest_path is not None
                _restore_activation_file(
                    record,
                    manifest_backup[0],
                    manifest_backup[1],
                    record.prior_manifest_path,
                )
            fts5_available = _revalidate_recovered_prior_set(port,
                record,
                identity=identity,
                canonical_store_id=canonical_store_id,
                allow_restored_identities=True,
            )
        db_identity = _recovery_capture_journal_file(
            record.prior_db_path
        )[0]
        if record.prior_manifest_absent:
            manifest_identity = None
        else:
            assert record.prior_manifest_path is not None
            manifest_identity = _recovery_capture_journal_file(
                record.prior_manifest_path
            )[0]
        terminal_record = replace(
            record,
            phase=_ActivationJournalPhase.PREPARED,
            active_content_attestation=None,
            prior_db_identity=(db_identity.device, db_identity.inode),
            prior_manifest_identity=(
                None
                if manifest_identity is None
                else (manifest_identity.device, manifest_identity.inode)
            ),
        )
        prior_generation = record.prior_generation
    _retire_coexisting_terminal(port, record)
    _ = port.write_terminal(terminal_record)
    _remove_owned_activation_journal_final(
        _activation_journal_path(identity),
        journal_identity,
    )
    _remove_journal_proven_backups(record)
    if record.had_prior_canonical:
        port.view = _rollback_restored_prior_view(
            record,
            identity=identity,
            canonical_store_id=canonical_store_id,
            fts5_available=fts5_available,
        )
    else:
        port.view = None
    return ActivationRecoveryReport(
        phase=record.phase.value,
        action="ROLLED_BACK",
        generation=prior_generation,
    )


def _require_cancelled_lineage_consistency(
    identity: CanonicalResourceIdentity,
    *,
    had_prior_canonical: bool,
) -> None:
    """Validate the write-once lineage fact before a PREPARED cancellation.

    A cancellation returns to a prior canonical generation only when the
    durable activated-lineage marker exists and the final/temp marker
    state is complete: the temporary must be absent or the exact paired
    two-link handoff with the deterministic payload (finished durably),
    and any conflicting temporary fails closed and is never removed or
    overwritten.  A cancelled first activation was genuinely never
    activated, so the marker final and temporary must both be absent; a
    foreign, tampered, hardlinked, or leftover marker/temp fails closed
    because it would otherwise turn a never-activated legacy resource
    into a claimed-activated one.
    """

    marker_path = _activation_lineage_marker_path(identity)
    if had_prior_canonical:
        marker_identity = (
            _activation_lineage_marker_state_complete(identity)
        )
        if marker_identity is None:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
            )
        return
    if _lstat_any_entry(marker_path):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    if _lstat_any_entry(
        _activation_lineage_marker_temp_path(marker_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )


def _discover_active_canonical(
    port: _StoreValidationPort,
) -> ActivationRecoveryReport | None:
    """Re-prove and rehydrate the active canonical generation (no journal).

    With no journal on disk the deterministic canonical sidecar/manifest
    pair is the only surviving authority, and the write-once
    activated-lineage marker is the persistent fact that distinguishes a
    true never-activated legacy resource from a resource that crossed
    physical activation but lost its completed authority.  A fully
    validated completed activation with a valid marker is hydrated into
    the one in-memory view and reported as a terminal COMPLETED; an
    absent pair with no marker and no marker temporary is the unchanged
    legacy state (``None``).  The marker final/temp state must be
    complete: a conflicting non-paired, symlink, directory, extra-link,
    wrong-identity, or wrong-byte temporary beside a valid final fails
    closed and is never removed or overwritten, and any marker-family
    entry without a pair is never silently ignored.  A marker with a
    missing/partial/tampered pair, a pair without a valid marker
    (authority with no transition record is never silently trusted), or
    any foreign/tampered entry fails closed in ``ACTIVATING`` and never
    authorizes a store.
    """

    identity = port.resource_identity
    marker_path = _activation_lineage_marker_path(identity)
    temp_path = _activation_lineage_marker_temp_path(marker_path)
    if (
        not _lstat_any_entry(identity.canonical_sidecar_path)
        and not _lstat_any_entry(identity.snapshot_manifest_path)
    ):
        try:
            marker_identity = _lstat_activation_lineage_marker_identity(
                marker_path
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.LINEAGE_MARKER_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        if marker_identity is not None or _lstat_any_entry(temp_path):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                retryable=False,
            )
        if port.view is not None:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                retryable=False,
            )
        return None
    try:
        marker_identity = _activation_lineage_marker_state_complete(
            identity
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error
    if marker_identity is None:
        raise ActivationPreparationError(
            "ACTIVATION.LINEAGE_MARKER_INVALID",
            retryable=False,
        )
    try:
        generation, fts5_available = _revalidate_discovered_active_set(port,
            identity,
            canonical_store_id=port.canonical_store_id,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_DISCOVERY_FAILED",
            retryable=True,
        ) from error
    port.view = _SQLiteGenerationView(
        stage=_canonical_activation_ref(
            identity,
            journal_id="discovery",
        ),
        canonical_store_id=port.canonical_store_id,
        generation=generation,
        fts5_available=fts5_available,
    )
    return ActivationRecoveryReport(
        phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
        action="COMPLETED",
        generation=generation,
    )


def _load_recovery_journal(
    port: _StoreValidationPort,
    journal_path: Path,
    journal_identity: _ActivationFileIdentity,
    *,
    expected_record_journal_path: Path | None = None,
) -> _ActivationJournalRecord:
    """Durably read and strictly parse one pending recovery journal."""

    if expected_record_journal_path is None:
        expected_record_journal_path = journal_path
    try:
        disk_bytes, disk_identity = _read_activation_journal_file(
            journal_path,
            journal_identity,
        )
        disk_record = _parse_activation_journal_bytes(
            disk_bytes,
            expected_journal_path=expected_record_journal_path,
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error
    try:
        _fsync_activation_directory(journal_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            retryable=False,
        ) from error
    try:
        fsynced_bytes, fsynced_identity = _read_activation_journal_file(
            journal_path,
            disk_identity,
        )
        fsynced_record = _parse_activation_journal_bytes(
            fsynced_bytes,
            expected_journal_path=expected_record_journal_path,
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error
    if (
        fsynced_bytes != disk_bytes
        or fsynced_identity != disk_identity
        or fsynced_record != disk_record
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            retryable=False,
        )
    return fsynced_record


def _load_recovery_terminal(
    port: _StoreValidationPort,
    terminal_path: Path,
    terminal_identity: _ActivationFileIdentity,
) -> _ActivationJournalRecord:
    """Durably read and strictly parse one terminal record.

    The terminal file mirrors the full authenticated main journal
    closure, so the record's own ``journal_path`` must still close the
    deterministic main journal path; the terminal file's own identity is
    proven by the caller-provided lstat identity.
    """

    try:
        return _load_recovery_journal(port,
            terminal_path,
            terminal_identity,
            expected_record_journal_path=_activation_journal_path(
                port.resource_identity
            ),
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error


def _coexisting_terminal(
    port: _StoreValidationPort,
    main_record: _ActivationJournalRecord,
) -> tuple[_ActivationFileIdentity, _ActivationJournalRecord] | None:
    """Load and rule-validate a coexisting terminal record, if any.

    Returns ``None`` when no terminal file exists; otherwise the exact
    terminal identity plus its fully re-proven record.  A foreign or
    tampered terminal (or any coexistence that does not close the same
    canonical state) fails closed and is never used or overwritten.
    """

    terminal_path = _activation_terminal_path(port.resource_identity)
    try:
        terminal_identity = _lstat_activation_terminal_identity(
            terminal_path
        )
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            retryable=False,
            reason_code=error.code,
        ) from error
    if terminal_identity is None:
        return None
    terminal_record = _load_recovery_terminal(port,
        terminal_path,
        terminal_identity,
    )
    _revalidate_recovery_authority(port,
        terminal_record,
        check_view=False,
    )
    if not _activation_terminal_coexistence_valid(
        main_record,
        terminal_record,
    ):
        raise ActivationPreparationError(
            "ACTIVATION.TERMINAL_COEXISTENCE_INVALID",
            retryable=False,
        )
    return terminal_identity, terminal_record


def _retire_coexisting_terminal(
    port: _StoreValidationPort,
    main_record: _ActivationJournalRecord,
) -> None:
    """Strictly retire a validated coexisting terminal record.

    Called only after the pending main journal (or the new CANCELLED
    terminal) is durable and revalidated, so the prior terminal may be
    retired without ever leaving the resource without a valid authority.
    """

    coexisting = _coexisting_terminal(port, main_record)
    if coexisting is None:
        return
    terminal_identity, _terminal_record = coexisting
    _remove_owned_activation_terminal_final(
        _activation_terminal_path(port.resource_identity),
        terminal_identity,
    )


def _revalidate_recovery_authority(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    *,
    check_view: bool = True,
) -> None:
    """Re-prove the journal's coordinator and token authority.

    The journal is the only surviving authority after a restart, so its
    identity bindings, nonce/artifact/sealed-stage digest closure, prior
    coherence, path containment, and any live view lineage are re-proven
    before any phase may mutate disk state.  ``check_view`` may be
    disabled only for a coexisting terminal record whose consistency is
    already governed by the deterministic coexistence rule against the
    validated pending main journal.
    """

    identity = port.resource_identity
    if (
        record.journal_version != _ACTIVATION_JOURNAL_VERSION
        or record.journal_id != f"journal.{record.preparation_id}"
        or record.journal_path != _activation_journal_path(identity)
        or record.resource_id != identity.resource_id
        or record.target_identity != identity.target_identity
        or port.canonical_store_id
        not in _recovery_acceptable_store_ids(record)
        or record.registry_namespace
        != f"coordinator.{identity.target_identity}"
        or record.new_manifest_path != identity.snapshot_manifest_path
        or record.expected_prior_generation != record.prior_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    if record.had_prior_canonical:
        required_prior = [
            record.expected_prior_generation,
            record.prior_db_path,
            record.prior_db_digest,
            record.prior_db_identity,
            record.prior_binding_snapshot_id,
            record.prior_receipt_digest,
            record.prior_db_backup_path,
            record.prior_db_backup_digest,
            record.prior_db_backup_identity,
        ]
        manifest_prior = [
            record.prior_manifest_path,
            record.prior_manifest_digest,
            record.prior_manifest_identity,
            record.prior_manifest_backup_path,
            record.prior_manifest_backup_digest,
            record.prior_manifest_backup_identity,
        ]
        if any(value is None for value in required_prior):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
        if record.prior_manifest_absent:
            if any(value is not None for value in manifest_prior):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_MISMATCH",
                    retryable=False,
                )
        elif any(value is None for value in manifest_prior):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
    else:
        if record.prior_manifest_absent:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
        for value in (
            record.expected_prior_generation,
            record.prior_generation,
            record.prior_db_path,
            record.prior_db_digest,
            record.prior_db_identity,
            record.prior_manifest_path,
            record.prior_manifest_digest,
            record.prior_manifest_identity,
            record.prior_binding_snapshot_id,
            record.prior_receipt_digest,
            record.prior_db_backup_path,
            record.prior_db_backup_digest,
            record.prior_db_backup_identity,
            record.prior_manifest_backup_path,
            record.prior_manifest_backup_digest,
            record.prior_manifest_backup_identity,
        ):
            if value is not None:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_MISMATCH",
                    retryable=False,
                )
    canonical_dir = identity.canonical_sidecar_path.parent
    manifest_dir = identity.snapshot_manifest_path.parent
    if (
        record.candidate_stage_db_path.parent != canonical_dir
        or record.candidate_manifest_temp_path.parent != manifest_dir
        or (
            record.prior_db_path is not None
            and record.prior_db_path.parent != canonical_dir
        )
        or (
            record.prior_manifest_path is not None
            and record.prior_manifest_path.parent != manifest_dir
        )
        or (
            record.prior_db_backup_path is not None
            and record.prior_db_backup_path.parent != canonical_dir
        )
        or (
            record.prior_manifest_backup_path is not None
            and record.prior_manifest_backup_path.parent != manifest_dir
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    if (
        _recovery_artifact_seal_digest(record)
        != record.artifact_seal_digest
        or _recovery_sealed_stage_digest(record)
        != record.sealed_stage_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TOKEN_AUTHORITY_INVALID",
            retryable=False,
        )
    if check_view and port.view is not None:
        if record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
            expected_view_generation = (
                0
                if record.expected_prior_generation is None
                else record.expected_prior_generation + 1
            )
            if (
                port.view.stage.staged_db_path
                != identity.canonical_sidecar_path
                or port.view.generation != expected_view_generation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_VIEW_MISMATCH",
                    retryable=False,
                )
        elif (
            not record.had_prior_canonical
            or record.prior_db_path is None
            or port.view.stage.staged_db_path != record.prior_db_path
            or port.view.generation != record.prior_generation
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_VIEW_MISMATCH",
                retryable=False,
            )


def _complete_prepared_cancellation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    journal_identity: _ActivationFileIdentity,
) -> ActivationRecoveryReport:
    """Cancel exactly one still-PREPARED activation (Task 5.8).

    The candidate DB/manifest must still be exactly the journal's files
    (nothing was replaced), the source must be unchanged, and the prior
    generation must be intact and healthy.  Only then is the CANCELLED
    terminal record durably published (retaining the full authenticated
    closure as the prior authority), the journal durably retired, and
    the prior view restored; the candidate assets stay owned by the
    sealed stage and no generation is published.
    """

    identity = port.resource_identity
    source = _recovery_capture_journal_file(
        identity.configured_jsonl_path
    )
    if (
        (source[0].device, source[0].inode)
        != record.source_jsonl_identity
        or source[1] != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ASSET_MUTATED",
            retryable=False,
        )
    candidate_ref = MutableStageRef(
        stage_id=f"candidate.{record.journal_id}",
        resource_identity=identity,
        staged_db_path=record.candidate_stage_db_path,
        manifest_temp_path=record.candidate_manifest_temp_path,
    )
    try:
        _revalidate_recovered_sealed_database(port,
            record,
            stage_ref=candidate_ref,
            database_path=record.candidate_stage_db_path,
            identity=identity,
            canonical_store_id=record.canonical_store_id,
        )
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    if record.had_prior_canonical:
        fts5_available = _revalidate_recovered_prior_set(port,
            record,
            identity=identity,
            canonical_store_id=port.canonical_store_id,
        )
        if (
            record.prior_db_path != identity.canonical_sidecar_path
            and _lstat_any_entry(identity.canonical_sidecar_path)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
    else:
        try:
            _require_first_activation_absence(identity)
        except ActivationPreparationError as error:
            raise _recovery_mismatch(error) from error
        fts5_available = False
    _require_cancelled_lineage_consistency(
        identity,
        had_prior_canonical=record.had_prior_canonical,
    )
    _remove_journal_proven_backups(record)
    _retire_coexisting_terminal(port, record)
    _ = port.write_terminal(record)
    _remove_owned_activation_journal_final(
        _activation_journal_path(identity),
        journal_identity,
    )
    # The CANCELLED terminal and retired journal are durable, so the
    # journal-proven candidate DB/manifest pair may now be quarantined:
    # a crash at any boundary leaves main or terminal authority able to
    # resume, and a fresh deterministic migration retry no longer hits
    # MIGRATION.STAGE_SEALED.  Mutated/symlinked/hardlinked/foreign
    # candidates fail closed and are never removed.
    _retire_cancelled_candidate_assets(record, identity=identity)
    if record.had_prior_canonical:
        prior_db_path = record.prior_db_path
        assert prior_db_path is not None
        prior_manifest_path = (
            identity.snapshot_manifest_path
            if record.prior_manifest_absent
            else record.prior_manifest_path
        )
        assert prior_manifest_path is not None
        if prior_db_path == identity.canonical_sidecar_path:
            prior_ref: _CanonicalStoreRef | _PriorActivationRef = (
                _canonical_activation_ref(
                    identity,
                    journal_id=record.journal_id,
                )
            )
        else:
            prior_ref = _PriorActivationRef(
                stage_id=f"prior.{record.journal_id}",
                resource_identity=identity,
                staged_db_path=prior_db_path,
                manifest_temp_path=prior_manifest_path,
            )
        port.view = _SQLiteGenerationView(
            stage=prior_ref,
            canonical_store_id=port.canonical_store_id,
            generation=(
                0
                if record.prior_generation is None
                else record.prior_generation
            ),
            fts5_available=fts5_available,
        )
    return ActivationRecoveryReport(
        phase=_ActivationJournalPhase.PREPARED.value,
        action="CANCELLED",
        generation=record.prior_generation,
    )


def _recover_manifest_publication(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    journal_identity: _ActivationFileIdentity,
) -> ActivationRecoveryReport:
    """Finish the DB_REPLACED window one truthful phase at a time.

    The canonical DB must already be exactly the journal's candidate
    with the candidate path consumed.  The issued receipt is completed
    durably, the new manifest is published durably, and the full active
    set is re-proven before the journal is advanced to
    MANIFEST_PUBLISHED; only then is the one generation view built and
    the journal advanced to GENERATION_PUBLISHED.  A failure at any
    boundary leaves the journal at the last truthful durable phase, so a
    restart resumes idempotently and never produces a second generation.
    """

    identity = port.resource_identity
    canonical = _recovery_capture_journal_file(
        identity.canonical_sidecar_path
    )
    if (
        (canonical[0].device, canonical[0].inode)
        != record.candidate_stage_db_identity
        or _lstat_any_entry(record.candidate_stage_db_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        )
    try:
        _preflight_recovered_manifest(record, identity=identity)
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    source = _recovery_capture_journal_file(
        identity.configured_jsonl_path
    )
    if (
        (source[0].device, source[0].inode)
        != record.source_jsonl_identity
        or source[1] != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ASSET_MUTATED",
            retryable=False,
        )
    next_generation = (
        0
        if record.expected_prior_generation is None
        else record.expected_prior_generation + 1
    )
    activation_digest = _activation_publication_digest(
        record,
        next_generation=next_generation,
    )
    _complete_recovered_receipt(port,
        record,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
    )
    _complete_recovered_manifest(port,
        record,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
    )
    _active_ref, _snapshot, active_attestation = (
        _validate_published_activation_set(port,
        record,
        preparation=None,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        )
    )
    journal_path = _activation_journal_path(identity)
    manifest_record = replace(
        record,
        phase=_ActivationJournalPhase.MANIFEST_PUBLISHED,
        active_content_attestation=active_attestation,
    )
    journal_identity = port.write_journal(
        manifest_record,
        journal_path,
        expected_final_identity=journal_identity,
    ).file_identity
    return _recover_generation_publication(port,
        manifest_record,
        journal_identity,
        report_phase=_ActivationJournalPhase.DB_REPLACED,
    )


def _recover_generation_publication(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    journal_identity: _ActivationFileIdentity,
    *,
    report_phase: _ActivationJournalPhase,
) -> ActivationRecoveryReport:
    """Publish the one generation only after MANIFEST_PUBLISHED is durable.

    The complete published active set is re-proven from disk against the
    journal's exact generation and activation digest, the one in-memory
    view is built, and only then is the journal advanced to
    GENERATION_PUBLISHED and the same set re-proven once more.  On any
    journal-write failure the in-memory view is withdrawn and the
    journal stays at the truthful MANIFEST_PUBLISHED phase.  The token
    is never re-consumed and no second generation is ever created; the
    terminal journal is retained as the durable consumed marker.
    """

    identity = port.resource_identity
    next_generation = (
        0
        if record.expected_prior_generation is None
        else record.expected_prior_generation + 1
    )
    activation_digest = _activation_publication_digest(
        record,
        next_generation=next_generation,
    )
    snapshot = _revalidate_recovered_active_set(port,
        record,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        require_manifest_published=True,
    )
    port.view = _SQLiteGenerationView(
        stage=_canonical_activation_ref(
            identity,
            journal_id=record.journal_id,
        ),
        canonical_store_id=record.canonical_store_id,
        generation=next_generation,
        fts5_available=snapshot.fts5_available,
    )
    try:
        _retire_coexisting_terminal(port, record)
        terminal_record = replace(
            record,
            phase=_ActivationJournalPhase.GENERATION_PUBLISHED,
        )
        journal_path = _activation_journal_path(identity)
        journal_identity = port.write_journal(
            terminal_record,
            journal_path,
            expected_final_identity=journal_identity,
        ).file_identity
    except BaseException:
        port.view = None
        raise
    _ = _revalidate_recovered_active_set(port,
        terminal_record,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        require_manifest_published=True,
    )
    # The completed main journal is durable at GENERATION_PUBLISHED and the
    # full active set is re-proven, so the write-once activated-lineage
    # marker may be ensured.  A marker failure keeps the completed journal
    # as the cold-recovery authority and the view is withheld under
    # ACTIVATING for a fresh recovery to resume.
    _ensure_activation_lineage_marker(identity)
    # The GENERATION_PUBLISHED journal, the re-proven active set, and the
    # write-once marker are durable: coordinator authority switches to the
    # candidate store id so later operations observe the new generation.
    port._activate_candidate_store_id(record.canonical_store_id)
    _remove_journal_proven_backups(record)
    return ActivationRecoveryReport(
        phase=report_phase.value,
        action="COMPLETED",
        generation=next_generation,
    )


def _replay_terminal_recovery(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
    journal_identity: _ActivationFileIdentity,
) -> ActivationRecoveryReport:
    """Idempotently replay a durable terminal GENERATION_PUBLISHED journal.

    The complete published active set is re-proven from disk against the
    journal's exact generation and activation digest and the one
    in-memory view is rehydrated; journal-owned backups are cleaned once
    (idempotently) and the terminal journal is retained as the durable
    consumed marker.  The token is never re-consumed and the terminal
    phase is never advanced, so repeated replays (same or fresh
    coordinator) observe exactly one completed canonical generation.
    """

    identity = port.resource_identity
    next_generation = (
        0
        if record.expected_prior_generation is None
        else record.expected_prior_generation + 1
    )
    activation_digest = _activation_publication_digest(
        record,
        next_generation=next_generation,
    )
    snapshot = _revalidate_recovered_active_set(port,
        record,
        identity=identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        require_manifest_published=True,
    )
    port.view = _SQLiteGenerationView(
        stage=_canonical_activation_ref(
            identity,
            journal_id=record.journal_id,
        ),
        canonical_store_id=record.canonical_store_id,
        generation=next_generation,
        fts5_available=snapshot.fts5_available,
    )
    _ensure_activation_lineage_marker(identity)
    port._activate_candidate_store_id(record.canonical_store_id)
    _remove_journal_proven_backups(record)
    return ActivationRecoveryReport(
        phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
        action="COMPLETED",
        generation=next_generation,
    )


def _replay_cancelled_terminal_recovery(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
) -> ActivationRecoveryReport | None:
    """Idempotently replay a durable CANCELLED terminal (PREPARED closure).

    The unchanged prior generation recorded by the terminal is re-proven
    from disk (database, manifest, source, binding receipt, generation)
    and rehydrated as the one in-memory view.  No generation is
    published, no token is resumed or replayed, and the terminal record
    is retained so any number of fresh coordinators authenticate and
    rehydrate the same prior authority.  A cancelled first activation
    (no prior) re-proves the absent legacy state and reports ``None``.
    """

    identity = port.resource_identity
    if record.phase is not _ActivationJournalPhase.PREPARED:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            retryable=False,
        )
    _require_cancelled_lineage_consistency(
        identity,
        had_prior_canonical=record.had_prior_canonical,
    )
    if not record.had_prior_canonical:
        try:
            _require_first_activation_absence(identity)
        except ActivationPreparationError as error:
            raise _recovery_mismatch(error) from error
        _remove_journal_proven_backups(record)
        _retire_cancelled_candidate_assets(record, identity=identity)
        return None
    try:
        fts5_available = _revalidate_recovered_prior_set(port,
            record,
            identity=identity,
            canonical_store_id=port.canonical_store_id,
            allow_restored_identities=True,
        )
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    # A Task 5.9 rollback removes the journal-owned backups only after
    # the restored authority and terminal are durable; a crash in that
    # window leaves terminal-only state with backups still present, so
    # the terminal replay cleans them idempotently.  A normal Task 5.8
    # cancellation already removed every backup before the terminal, so
    # this is a no-op for the unchanged-prior path.
    _remove_journal_proven_backups(record)
    # A PREPARED cancellation retires the journal-proven candidate
    # DB/manifest pair only after the CANCELLED terminal; a crash in that
    # window leaves terminal-only state with the candidates still present,
    # so the terminal replay finishes the retirement idempotently before
    # reporting the prior/legacy state.
    _retire_cancelled_candidate_assets(record, identity=identity)
    if (
        record.prior_db_path != identity.canonical_sidecar_path
        and _lstat_any_entry(identity.canonical_sidecar_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    prior_db_path = record.prior_db_path
    assert prior_db_path is not None
    prior_manifest_path = (
        identity.snapshot_manifest_path
        if record.prior_manifest_absent
        else record.prior_manifest_path
    )
    assert prior_manifest_path is not None
    if prior_db_path == identity.canonical_sidecar_path:
        prior_ref: _CanonicalStoreRef | _PriorActivationRef = (
            _canonical_activation_ref(
                identity,
                journal_id=record.journal_id,
            )
        )
    else:
        prior_ref = _PriorActivationRef(
            stage_id=f"prior.{record.journal_id}",
            resource_identity=identity,
            staged_db_path=prior_db_path,
            manifest_temp_path=prior_manifest_path,
        )
    port.view = _SQLiteGenerationView(
        stage=prior_ref,
        canonical_store_id=port.canonical_store_id,
        generation=(
            0
            if record.prior_generation is None
            else record.prior_generation
        ),
        fts5_available=fts5_available,
    )
    return ActivationRecoveryReport(
        phase=_ActivationJournalPhase.PREPARED.value,
        action="CANCELLED",
        generation=record.prior_generation,
    )


def _load_activation_transition_record(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
    handle: _ActivationJournalHandle,
    next_phase: _ActivationJournalPhase,
) -> _ActivationJournalRecord:
    """Load one exact current journal record and enforce phase order."""

    if (
        port.state != "ACTIVATING"
        or port.preparation is not preparation
        or port.cleanup_reservation is not None
        or port.cleanup_in_progress
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_STATE_INVALID",
            retryable=True,
        )
    if (
        handle.journal_path
        != _activation_journal_path(port.resource_identity)
        or handle.preparation_id != preparation.preparation_id
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_HANDLE_INVALID",
            retryable=False,
        )
    try:
        disk_bytes, _disk_identity = _read_activation_journal_file(
            handle.journal_path,
            handle.file_identity,
        )
        disk_record = _parse_activation_journal_bytes(
            disk_bytes,
            expected_journal_path=handle.journal_path,
        )
    except ActivationPreparationError as error:
        if error.code == "ACTIVATION.JOURNAL_HANDLE_STALE":
            raise
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_HANDLE_STALE",
            retryable=False,
        ) from error
    if (
        disk_record != handle._record
        or disk_record.phase is not handle.phase
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_HANDLE_STALE",
            retryable=False,
        )
    current = disk_record.phase
    if next_phase is current:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PHASE_REPEATED",
            retryable=False,
        )
    if _PHASE_SEQUENCE.index(next_phase) < _PHASE_SEQUENCE.index(current):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PHASE_BACKWARD",
            retryable=False,
        )
    if (
        _PHASE_SEQUENCE.index(next_phase)
        > _PHASE_SEQUENCE.index(current) + 1
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PHASE_SKIP",
            retryable=False,
        )
    return disk_record


def _advance_activation_journal_after_effect(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
    handle: _ActivationJournalHandle,
    next_phase: _ActivationJournalPhase,
    *,
    next_generation: int | None = None,
    activation_digest: str | None = None,
    active_content_attestation: ActiveContentAttestation | None = None,
) -> _ActivationJournalHandle:
    """Advance only after re-proving the phase-specific durable effect."""

    disk_record = _load_activation_transition_record(port,
        preparation,
        handle,
        next_phase,
    )
    _revalidate_activation_effect_closure(port,
        preparation,
        disk_record,
        next_phase=next_phase,
        next_generation=next_generation,
        activation_digest=activation_digest,
        active_content_attestation=active_content_attestation,
    )
    next_record = replace(
        disk_record,
        phase=next_phase,
        active_content_attestation=(
            active_content_attestation
            if next_phase is _ActivationJournalPhase.MANIFEST_PUBLISHED
            else disk_record.active_content_attestation
        ),
    )
    return port.write_journal(
        next_record,
        handle.journal_path,
        expected_final_identity=handle.file_identity,
    )


def _publish_activation_journal(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
) -> _ActivationJournalHandle:
    """Write PREPARED, replay it, or supersede a proven terminal record.

    An existing byte-identical PREPARED journal for the same closure is
    replayed.  A durable terminal record (``GENERATION_PUBLISHED`` =
    CONSUMED or ``PREPARED`` = CANCELLED) is superseded atomically: the
    old terminal authority is first mirrored to the deterministic
    terminal path (or validated when already there), the occupied main
    journal path is then retired only after that copy is durable, the
    new PREPARED journal is written/fsynced/revalidated, and only then is
    the prior terminal strictly retired.  A crash at every point leaves
    at least one valid authority.  Any mid-flight journal, foreign
    terminal, or unparsable file fail-stops without being clobbered.
    """

    journal_path = _activation_journal_path(port.resource_identity)
    terminal_path = _activation_terminal_path(port.resource_identity)
    record = _build_activation_journal_record(port, preparation)
    existing = _lstat_activation_journal_identity(journal_path)
    if existing is None:
        coexisting = _coexisting_terminal(port, record)
        handle = port.write_journal(
            record,
            journal_path,
            expected_final_identity=None,
        )
        if coexisting is not None:
            _remove_owned_activation_terminal_final(
                terminal_path,
                coexisting[0],
            )
        return handle
    try:
        disk_bytes, _disk_identity = _read_activation_journal_file(
            journal_path,
            existing,
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
    if disk_record.phase is _ActivationJournalPhase.PREPARED:
        coexisting = _coexisting_terminal(port, record)
        handle = _replay_activation_journal(
            preparation,
            record,
            journal_path,
            existing,
        )
        if coexisting is not None:
            _remove_owned_activation_terminal_final(
                terminal_path,
                coexisting[0],
            )
        return handle
    identity = port.resource_identity
    if (
        disk_record.phase is not _ActivationJournalPhase.GENERATION_PUBLISHED
        or disk_record.journal_path != journal_path
        or disk_record.resource_id != identity.resource_id
        or disk_record.target_identity != identity.target_identity
        or disk_record.canonical_store_id != port.canonical_store_id
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
            retryable=False,
        )
    coexisting = _coexisting_terminal(port, disk_record)
    if coexisting is None:
        terminal_identity = port.write_terminal(
            disk_record
        )
    else:
        terminal_identity = coexisting[0]
    # The superseded GENERATION_PUBLISHED record is the last authority
    # that authenticates the completed activation's prior DB/manifest
    # backups.  Both the main journal and its terminal mirror are durable
    # at this point, so the journal-proven backups are cleaned idempotently
    # (identity/digest-bound, parent fsync, never globbed) before the main
    # journal is retired; a crash at every boundary leaves a main or
    # terminal record able to resume the cleanup and no backup becomes
    # ownerless.
    _remove_journal_proven_backups(disk_record)
    _remove_owned_activation_journal_final(journal_path, existing)
    handle = port.write_journal(
        record,
        journal_path,
        expected_final_identity=None,
    )
    _remove_owned_activation_terminal_final(
        terminal_path,
        terminal_identity,
    )
    return handle


def _build_activation_journal_record(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
) -> _ActivationJournalRecord:
    """Derive the complete PREPARED closure from live registry facts.

    Every fact comes from coordinator-owned state: the registry's sealed
    entry and physical readiness snapshot, the registry token, the
    preparation's backups, the prior generation view, and the canonical
    resource identity.  No caller-supplied path, grant, token, nonce,
    phase, or mapping is accepted as authority.
    """

    stage_seal_error = port.stage_seal_error
    stage = preparation._sealed_stage
    token = preparation._token
    registry = cast(Any, port.sealed_registry)
    try:
        contract_module._validate_activation_token_for_stage(token, stage)
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        ) from error
    try:
        physical = registry.resolve_physical_readiness(stage)
    except stage_seal_error as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        ) from error
    identity = port.resource_identity
    evidence = physical.evidence
    receipt = evidence.source_binding.receipt
    if (
        physical.registry_namespace != registry.registry_namespace
        or physical.resource_id != identity.resource_id
        or physical.target_identity != identity.target_identity
        or physical.canonical_store_id != preparation.canonical_store_id
        or (
            (preparation.prior_canonical_store_id is None)
            != (preparation.canonical_store_id == port.canonical_store_id)
        )
        or (
            preparation.prior_canonical_store_id is not None
            and preparation.prior_canonical_store_id
            != port.canonical_store_id
        )
        or physical.mutable_stage.resource_identity != identity
        or physical.expected_prior_generation
        != preparation.expected_prior_generation
        or physical.snapshot_receipt_digest
        != contract_module.snapshot_receipt_digest(receipt)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    db_capture = _capture_journal_closure_file(
        physical.mutable_stage.staged_db_path
    )
    if (
        (db_capture[0].device, db_capture[0].inode)
        != (physical.database_identity.device, physical.database_identity.inode)
        or db_capture[1] != evidence.stage_file_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    manifest_capture = _capture_journal_closure_file(
        physical.mutable_stage.manifest_temp_path
    )
    if (
        (manifest_capture[0].device, manifest_capture[0].inode)
        != (
            physical.manifest_identity.device,
            physical.manifest_identity.inode,
        )
        or manifest_capture[1] != evidence.manifest_temp_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    source_capture = _capture_journal_closure_file(
        identity.configured_jsonl_path
    )
    sealed_attestation = physical.sealed_content_attestation
    if (
        source_capture[1] != receipt.jsonl_digest
        or (
            source_capture[0].device,
            source_capture[0].inode,
        )
        != (
            sealed_attestation.source.device,
            sealed_attestation.source.inode,
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    journal_path = _activation_journal_path(identity)
    prior_view = preparation._prior_view
    had_prior = preparation.had_prior_canonical
    prior_generation: int | None = None
    prior_binding_snapshot_id: str | None = None
    prior_receipt_digest_value: str | None = None
    prior_manifest_digest_value: str | None = None
    prior_db_path: Path | None = None
    prior_manifest_path: Path | None = None
    prior_db_digest_value: str | None = None
    prior_db_identity_value: tuple[int, int] | None = None
    prior_manifest_identity_value: tuple[int, int] | None = None
    prior_db_backup_path: Path | None = None
    prior_manifest_backup_path: Path | None = None
    prior_db_backup_digest_value: str | None = None
    prior_manifest_backup_digest_value: str | None = None
    prior_db_backup_identity_value: tuple[int, int] | None = None
    prior_manifest_backup_identity_value: tuple[int, int] | None = None
    prior_manifest_absent_value: bool = False
    prior_manifest_asset: _RecoveryBackupAsset | None = None
    if had_prior:
        if prior_view is None or port.view is not prior_view:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        try:
            replacement = preparation.prior_canonical_store_id is not None
            captures = _capture_prior_assets(port,
                prior_view,
                identity=identity,
                replacement=replacement,
            )
            with port.open_configured_connection(
                prior_view.stage.staged_db_path,
                require_existing=True,
            ) as connection:
                facts = port.read_source_binding_facts(connection, prior_view)
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        _prior_by_kind = {
            capture.asset_kind: capture for capture in captures
        }
        database = _prior_by_kind["DATABASE"]
        manifest = _prior_by_kind.get("MANIFEST")
        source = _prior_by_kind["SOURCE"]
        if (
            facts.binding is None
            or facts.diagnostic_codes
            or (not replacement and facts.divergence_latched)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        binding = facts.binding
        prior_manifest_absent_value = manifest is None
        backups_by_kind = {
            asset.asset_kind: asset for asset in preparation._backup_assets
        }
        expected_backup_kinds = (
            {"DATABASE"} if prior_manifest_absent_value
            else {"DATABASE", "MANIFEST"}
        )
        if set(backups_by_kind) != expected_backup_kinds:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        prior_db_asset = backups_by_kind["DATABASE"]
        if (
            database.identity != prior_db_asset.original_identity
            or database.digest
            != prior_db_asset.evidence.original_digest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_ASSET_MUTATED",
                retryable=False,
            )
        prior_db_backup_capture = _capture_journal_closure_file(
            prior_db_asset.backup_path
        )
        if (
            prior_db_backup_capture
            != (
                prior_db_asset.backup_identity,
                prior_db_asset.evidence.backup_digest,
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_ASSET_MUTATED",
                retryable=False,
            )
        if not prior_manifest_absent_value:
            prior_manifest_asset = backups_by_kind["MANIFEST"]
            assert manifest is not None
            if (
                manifest.identity != prior_manifest_asset.original_identity
                or manifest.digest
                != prior_manifest_asset.evidence.original_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
            prior_manifest_backup_capture = _capture_journal_closure_file(
                prior_manifest_asset.backup_path
            )
            if (
                prior_manifest_backup_capture
                != (
                    prior_manifest_asset.backup_identity,
                    prior_manifest_asset.evidence.backup_digest,
                )
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
        if (
            preparation.expected_prior_generation
            != prior_view.generation
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        prior_generation = prior_view.generation
        prior_binding_snapshot_id = binding.receipt.snapshot_id
        prior_receipt_digest_value = contract_module.snapshot_receipt_digest(
            binding.receipt
        )
        prior_db_path = database.path
        prior_db_digest_value = database.digest
        prior_db_identity_value = (
            database.identity.device,
            database.identity.inode,
        )
        prior_db_backup_path = prior_db_asset.backup_path
        prior_db_backup_digest_value = (
            prior_db_asset.evidence.backup_digest
        )
        prior_db_backup_identity_value = (
            prior_db_asset.backup_identity.device,
            prior_db_asset.backup_identity.inode,
        )
        if prior_manifest_absent_value:
            prior_manifest_digest_value = None
            prior_manifest_path = None
            prior_manifest_identity_value = None
            prior_manifest_backup_path = None
            prior_manifest_backup_digest_value = None
            prior_manifest_backup_identity_value = None
        else:
            assert manifest is not None
            assert not prior_manifest_absent_value
            assert prior_manifest_asset is not None
            prior_manifest_digest_value = manifest.digest
            prior_manifest_path = identity.snapshot_manifest_path
            prior_manifest_identity_value = (
                manifest.identity.device,
                manifest.identity.inode,
            )
            prior_manifest_backup_path = prior_manifest_asset.backup_path
            prior_manifest_backup_digest_value = (
                prior_manifest_asset.evidence.backup_digest
            )
            prior_manifest_backup_identity_value = (
                prior_manifest_asset.backup_identity.device,
                prior_manifest_asset.backup_identity.inode,
            )
    else:
        if port.view is not None or prior_view is not None:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        try:
            _require_first_activation_absence(identity)
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
    try:
        state = registry.state(stage)
        registry._token_entry(token)
    except stage_seal_error as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        ) from error
    if state is not contract_module.ActivationCapabilityState.TOKEN_ISSUED:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        )
    record = _ActivationJournalRecord(
        journal_id=f"journal.{preparation.preparation_id}",
        journal_version=_ACTIVATION_JOURNAL_VERSION,
        journal_path=journal_path,
        phase=_ActivationJournalPhase.PREPARED,
        preparation_id=preparation.preparation_id,
        registry_namespace=registry.registry_namespace,
        token_id=token.token_id,
        token_version=token.token_version,
        activation_nonce=token.activation_nonce,
        artifact_id=physical.artifact_id,
        artifact_seal_digest=physical.artifact_seal_digest,
        sealed_stage_digest=physical.sealed_stage_digest,
        resource_id=physical.resource_id,
        target_identity=physical.target_identity,
        canonical_store_id=physical.canonical_store_id,
        prior_canonical_store_id=preparation.prior_canonical_store_id,
        expected_prior_generation=preparation.expected_prior_generation,
        prior_generation=prior_generation,
        gate_b_grant_digest=preparation.gate_b_grant_digest,
        evidence_digest=contract_module.stage_validation_evidence_digest(
            evidence
        ),
        snapshot_receipt_digest=physical.snapshot_receipt_digest,
        stage_db_digest=evidence.stage_file_digest,
        manifest_temp_digest=evidence.manifest_temp_digest,
        source_jsonl_digest=receipt.jsonl_digest,
        new_receipt_id=receipt.snapshot_id,
        new_manifest_path=identity.snapshot_manifest_path,
        new_manifest_digest=evidence.manifest_temp_digest,
        candidate_stage_db_path=physical.mutable_stage.staged_db_path,
        candidate_manifest_temp_path=(
            physical.mutable_stage.manifest_temp_path
        ),
        candidate_stage_db_identity=(
            physical.database_identity.device,
            physical.database_identity.inode,
        ),
        candidate_manifest_temp_identity=(
            physical.manifest_identity.device,
            physical.manifest_identity.inode,
        ),
        source_jsonl_identity=(
            source_capture[0].device,
            source_capture[0].inode,
        ),
        had_prior_canonical=had_prior,
        prior_manifest_absent=(
            prior_manifest_absent_value
            if had_prior
            else False
        ),
        prior_binding_snapshot_id=prior_binding_snapshot_id,
        prior_receipt_digest=prior_receipt_digest_value,
        prior_manifest_digest=prior_manifest_digest_value,
        prior_db_path=prior_db_path,
        prior_manifest_path=prior_manifest_path,
        prior_db_digest=prior_db_digest_value,
        prior_db_identity=prior_db_identity_value,
        prior_manifest_identity=prior_manifest_identity_value,
        prior_db_backup_path=prior_db_backup_path,
        prior_manifest_backup_path=prior_manifest_backup_path,
        prior_db_backup_digest=prior_db_backup_digest_value,
        prior_manifest_backup_digest=prior_manifest_backup_digest_value,
        prior_db_backup_identity=prior_db_backup_identity_value,
        prior_manifest_backup_identity=(
            prior_manifest_backup_identity_value
        ),
        sealed_content_attestation=sealed_attestation,
        active_content_attestation=None,
    )
    return record


def _revalidate_activation_journal_closure(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
    record: _ActivationJournalRecord,
) -> None:
    """Re-prove one durable journal record against live facts.

    The caller must hold the coordinator condition lock.  Registry-owned
    token and preparation facts are revalidated; candidate, prior,
    backup, source, view, and generation facts are re-captured from disk.
    Any mutation, token cancellation/consumption, inode swap, or
    coordinator/preparation change invalidates the journal.
    """

    stage_seal_error = port.stage_seal_error
    stage = preparation._sealed_stage
    token = preparation._token
    if type(stage) is not SealedStage:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    if type(token) is not contract_module._ActivationToken:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        )
    registry = cast(Any, port.sealed_registry)
    if registry.registry_namespace != record.registry_namespace:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    try:
        contract_module._validate_activation_token_for_stage(token, stage)
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        ) from error
    try:
        physical = registry.resolve_physical_readiness(stage)
    except stage_seal_error as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        ) from error
    evidence = physical.evidence
    receipt = evidence.source_binding.receipt
    if (
        physical.artifact_id != record.artifact_id
        or physical.artifact_seal_digest != record.artifact_seal_digest
        or physical.sealed_stage_digest != record.sealed_stage_digest
        or physical.resource_id != record.resource_id
        or physical.target_identity != record.target_identity
        or physical.canonical_store_id != record.canonical_store_id
        or physical.snapshot_receipt_digest
        != record.snapshot_receipt_digest
        or physical.expected_prior_generation
        != record.expected_prior_generation
        or contract_module.stage_validation_evidence_digest(evidence)
        != record.evidence_digest
        or evidence.stage_file_digest != record.stage_db_digest
        or evidence.manifest_temp_digest != record.manifest_temp_digest
        or receipt.snapshot_id != record.new_receipt_id
        or receipt.jsonl_digest != record.source_jsonl_digest
        or physical.mutable_stage.staged_db_path
        != record.candidate_stage_db_path
        or physical.mutable_stage.manifest_temp_path
        != record.candidate_manifest_temp_path
        or (physical.database_identity.device, physical.database_identity.inode)
        != record.candidate_stage_db_identity
        or (
            physical.manifest_identity.device,
            physical.manifest_identity.inode,
        )
        != record.candidate_manifest_temp_identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    identity = port.resource_identity
    if (
        record.new_manifest_path != identity.snapshot_manifest_path
        or record.journal_path != _activation_journal_path(identity)
        or record.preparation_id != preparation.preparation_id
        or record.gate_b_grant_digest != preparation.gate_b_grant_digest
        or record.had_prior_canonical != preparation.had_prior_canonical
        or record.resource_id != preparation.resource_id
        or record.target_identity != preparation.target_identity
        or record.canonical_store_id != preparation.canonical_store_id
        or record.prior_canonical_store_id
        != preparation.prior_canonical_store_id
        or record.expected_prior_generation
        != preparation.expected_prior_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    db_capture = _capture_journal_closure_file(
        physical.mutable_stage.staged_db_path
    )
    if (
        (db_capture[0].device, db_capture[0].inode)
        != record.candidate_stage_db_identity
        or db_capture[1] != record.stage_db_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    manifest_capture = _capture_journal_closure_file(
        physical.mutable_stage.manifest_temp_path
    )
    if (
        (manifest_capture[0].device, manifest_capture[0].inode)
        != record.candidate_manifest_temp_identity
        or manifest_capture[1] != record.manifest_temp_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    source_capture = _capture_journal_closure_file(
        identity.configured_jsonl_path
    )
    if (
        (source_capture[0].device, source_capture[0].inode)
        != record.source_jsonl_identity
        or source_capture[1] != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_ASSET_MUTATED",
            retryable=False,
        )
    view = port.view
    if record.had_prior_canonical:
        replacement = record.prior_canonical_store_id is not None
        if (
            view is None
            or view is not preparation._prior_view
            or view.generation != record.prior_generation
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        try:
            database = _capture_activation_file(
                view.stage.staged_db_path,
                asset_kind="DATABASE",
            )
            if record.prior_manifest_absent:
                if _lstat_any_entry(identity.snapshot_manifest_path):
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_ASSET_MUTATED",
                        retryable=False,
                    )
                manifest = None
            else:
                manifest = _capture_activation_file(
                    identity.snapshot_manifest_path,
                    asset_kind="MANIFEST",
                )
            with port.open_configured_connection(
                view.stage.staged_db_path,
                require_existing=True,
            ) as connection:
                facts = port.read_source_binding_facts(connection, view)
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        if (
            (database.identity.device, database.identity.inode)
            != record.prior_db_identity
            or database.digest != record.prior_db_digest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_ASSET_MUTATED",
                retryable=False,
            )
        if manifest is not None and (
            (manifest.identity.device, manifest.identity.inode)
            != record.prior_manifest_identity
            or manifest.digest != record.prior_manifest_digest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_ASSET_MUTATED",
                retryable=False,
            )
        if (
            facts.binding is None
            or facts.diagnostic_codes
            or (
                not replacement
                and (
                    facts.divergence_latched
                    or port.configured_pair_diagnostics(
                        facts.binding,
                        identity=identity,
                        canonical_store_id=view.canonical_store_id,
                        head_revision=facts.head_revision,
                        cumulative_record_counts=facts.cumulative_record_counts,
                    )
                )
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        binding = facts.binding
        if (
            binding.receipt.snapshot_id
            != record.prior_binding_snapshot_id
            or contract_module.snapshot_receipt_digest(binding.receipt)
            != record.prior_receipt_digest
            or database.path != record.prior_db_path
            or (
                manifest is None
            ) != record.prior_manifest_absent
            or (
                manifest is not None
                and manifest.path != record.prior_manifest_path
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        if manifest is not None and not replacement:
            manifest_payload = _read_activation_file_bytes(manifest)
            if manifest_payload != contract_to_json(
                binding.manifest
            ).encode("utf-8"):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
        backups_by_kind = {
            asset.asset_kind: asset for asset in preparation._backup_assets
        }
        expected_backup_kinds = (
            {"DATABASE"} if record.prior_manifest_absent
            else {"DATABASE", "MANIFEST"}
        )
        if set(backups_by_kind) != expected_backup_kinds:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        prior_db_asset = backups_by_kind["DATABASE"]
        db_backup_capture = _capture_journal_closure_file(
            prior_db_asset.backup_path
        )
        if (
            (
                db_backup_capture[0].device,
                db_backup_capture[0].inode,
            )
            != record.prior_db_backup_identity
            or db_backup_capture[1] != record.prior_db_backup_digest
            or db_backup_capture
            != (
                prior_db_asset.backup_identity,
                prior_db_asset.evidence.backup_digest,
            )
            or prior_db_asset.backup_path != record.prior_db_backup_path
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_ASSET_MUTATED",
                retryable=False,
            )
        if not record.prior_manifest_absent:
            prior_manifest_asset = backups_by_kind["MANIFEST"]
            manifest_backup_capture = _capture_journal_closure_file(
                prior_manifest_asset.backup_path
            )
            if (
                (
                    manifest_backup_capture[0].device,
                    manifest_backup_capture[0].inode,
                )
                != record.prior_manifest_backup_identity
                or manifest_backup_capture[1]
                != record.prior_manifest_backup_digest
                or manifest_backup_capture
                != (
                    prior_manifest_asset.backup_identity,
                    prior_manifest_asset.evidence.backup_digest,
                )
                or prior_manifest_asset.backup_path
                != record.prior_manifest_backup_path
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
    else:
        if view is not None or record.prior_generation is not None:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        try:
            _require_first_activation_absence(identity)
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
    try:
        state = registry.state(stage)
        registry._token_entry(token)
    except stage_seal_error as error:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        ) from error
    if state is not contract_module.ActivationCapabilityState.TOKEN_ISSUED:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        )
    if (
        token.token_id != record.token_id
        or token.token_version != record.token_version
        or token.activation_nonce != record.activation_nonce
        or token.artifact_id != record.artifact_id
        or token.artifact_seal_digest != record.artifact_seal_digest
        or token.sealed_stage_digest != record.sealed_stage_digest
        or token.snapshot_receipt_digest != record.snapshot_receipt_digest
        or token.expected_prior_generation
        != record.expected_prior_generation
        or token.resource_id != record.resource_id
        or token.target_identity != record.target_identity
        or token.canonical_store_id != record.canonical_store_id
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_TOKEN_INVALID",
            retryable=False,
        )


def _revalidate_activation_effect_closure(
    port: _StoreValidationPort,
    preparation: _ActivationPreparation,
    record: _ActivationJournalRecord,
    *,
    next_phase: _ActivationJournalPhase,
    next_generation: int | None,
    activation_digest: str | None,
    active_content_attestation: ActiveContentAttestation | None,
) -> None:
    """Re-prove immutable authority plus the already-durable effect.

    PREPARED validation intentionally follows the older candidate/prior
    closure above.  Once the candidate DB has moved, this validator uses
    the canonical path and the phase's actual durable facts; it never
    pretends that the consumed candidate path or replaced prior inode is
    still present.
    """

    _validate_activation_publication_authority(
        record,
        preparation=preparation,
        registry=cast(Any, port.sealed_registry),
        identity=port.resource_identity,
        canonical_store_id=record.canonical_store_id,
    )
    if next_phase is _ActivationJournalPhase.DB_REPLACED:
        if (
            next_generation is not None
            or activation_digest is not None
            or active_content_attestation is not None
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        _validate_replaced_activation_database(port,
            record,
            preparation=preparation,
            identity=port.resource_identity,
            canonical_store_id=record.canonical_store_id,
        )
        return
    if (
        type(next_generation) is not int
        or isinstance(next_generation, bool)
        or next_generation < 0
        or type(activation_digest) is not str
    ):
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            retryable=False,
        )
    if next_phase is _ActivationJournalPhase.MANIFEST_PUBLISHED:
        if type(active_content_attestation) is not ActiveContentAttestation:
            raise ActivationPreparationError(
                "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                retryable=False,
            )
    elif active_content_attestation is not None:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
            retryable=False,
        )
    active_ref, _snapshot = _revalidate_active_content_attestation(port,
        record,
        identity=port.resource_identity,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        attestation=active_content_attestation,
    )
    if next_phase is _ActivationJournalPhase.MANIFEST_PUBLISHED:
        return
    if next_phase is not _ActivationJournalPhase.GENERATION_PUBLISHED:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PHASE_INVALID",
            retryable=False,
        )
    view = port.view
    if (
        view is None
        or type(view.stage) is not _CanonicalStoreRef
        or view.stage != active_ref
        or view.canonical_store_id != record.canonical_store_id
        or view.generation != next_generation
    ):
        raise ActivationPreparationError(
            "ACTIVATION.GENERATION_PUBLICATION_INVALID",
            retryable=False,
        )


def recover_durable_activation(port: _StoreValidationPort) -> ActivationRecoveryReport | None:
    """Idempotently finish exactly one durable activation (Task 5.8).

    Reconstructs the activation authority from the adjacent durable
    journal after a restart: no live preparation, token, or registry is
    required.  A journal is continued only when every phase-relevant
    fact matches disk, and each next journal phase is published only
    after its matching effect is durable and independently revalidated
    (DB_REPLACED -> receipt/manifest -> MANIFEST_PUBLISHED -> the one
    in-memory generation -> GENERATION_PUBLISHED).  Every mismatch or
    unproven write fail-stops in ``ACTIVATING`` with the journal at the
    last truthful durable phase (the Task 5.9 rollback seam).

    A terminal ``GENERATION_PUBLISHED`` journal is retained as the
    durable consumed marker: replay re-proves the completed canonical
    generation, hydrates the view, never re-consumes the token, and
    never creates a second generation.  When no journal survives, the
    separate deterministic terminal record retains the full
    authenticated closure: a ``PREPARED`` terminal means CANCELLED/prior
    authority (re-proven and rehydrated without any generation
    publication or token replay), a ``GENERATION_PUBLISHED`` terminal
    means CONSUMED/new canonical authority.  A pending main journal
    always takes precedence, and terminal/main coexistence is accepted
    only under the deterministic closure rule.  When neither authority
    survives, the deterministic active canonical pair is discovered and
    re-proven from disk alone, so a fresh coordinator rehydrates exactly
    the completed canonical generation or the unchanged prior/legacy
    state without relying on bare absence or caller memory.
    """

    if (
        port.state not in {"READY", "ACTIVATING"}
        or port.preparation is not None
        or port.cleanup_reservation is not None
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_STATE_INVALID",
            retryable=True,
        )
    journal_path = _activation_journal_path(port.resource_identity)
    terminal_path = _activation_terminal_path(port.resource_identity)
    # Recovery mutates durable authority files, so a READY coordinator's
    # live operation leases must be drained first: READY -> DRAINING
    # rejects new leases, the wait releases the coordinator condition so
    # in-flight leases finish, the view/generation is proven unchanged,
    # and only then ACTIVATING.  A drain timeout restores READY without
    # any disk transition.  An ACTIVATING coordinator without a live
    # preparation (for example one that fail-stopped refusing the
    # rollback of a completed journal) holds no leases and needs no
    # drain.
    if port.state == "READY":
        port.drain_for_transition()
    try:
        try:
            journal_identity = _lstat_activation_journal_identity(
                journal_path
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        try:
            terminal_identity = _lstat_activation_terminal_identity(
                terminal_path
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        journal_temp_path = _activation_journal_temp_path(
            journal_path
        )
        terminal_temp_path = _activation_terminal_temp_path(
            terminal_path
        )
        if _lstat_any_entry(journal_temp_path):
            if (
                journal_identity is not None
                or terminal_identity is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_JOURNAL_TEMP_CONFLICT",
                    retryable=False,
                )
            _remove_orphaned_activation_temp(journal_temp_path)
        if _lstat_any_entry(terminal_temp_path):
            if (
                terminal_identity is not None
                or journal_identity is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_TERMINAL_TEMP_CONFLICT",
                    retryable=False,
                )
            _remove_orphaned_activation_temp(terminal_temp_path)
        if journal_identity is None:
            if terminal_identity is None:
                # A live in-memory view never bypasses the cold discovery:
                # the marker and the active canonical pair are re-proven
                # from disk (or fail closed), so a deleted/tampered
                # authority is never silently trusted from caller memory.
                report = _discover_active_canonical(port)
            else:
                terminal_record = _load_recovery_terminal(port,
                    terminal_path,
                    terminal_identity,
                )
                _revalidate_recovery_authority(port,
                    terminal_record
                )
                if (
                    terminal_record.phase
                    is _ActivationJournalPhase.GENERATION_PUBLISHED
                ):
                    report = _replay_terminal_recovery(port,
                        terminal_record,
                        terminal_identity,
                    )
                elif (
                    terminal_record.phase
                    is _ActivationJournalPhase.PREPARED
                ):
                    report = (
                        _replay_cancelled_terminal_recovery(port,
                            terminal_record,
                        )
                    )
                else:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                        retryable=False,
                    )
            port.preparation = None
            port.cleanup_reservation = None
            port.state = "READY"
            port.notify_all()
            return report
        record = _load_recovery_journal(port,
            journal_path,
            journal_identity,
        )
        _revalidate_recovery_authority(port, record)
        rollback_terminal_coexists = False
        if terminal_identity is not None:
            terminal_record = _load_recovery_terminal(port,
                terminal_path,
                terminal_identity,
            )
            _revalidate_recovery_authority(port,
                terminal_record
            )
            if not _activation_terminal_coexistence_valid(
                record,
                terminal_record,
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.TERMINAL_COEXISTENCE_INVALID",
                    retryable=False,
                )
            if _rollback_terminal_prior_closes(
                terminal_record,
                record,
            ):
                rollback_terminal_coexists = True
        try:
            if record.phase is _ActivationJournalPhase.PREPARED:
                report = _complete_prepared_cancellation(port,
                    record,
                    journal_identity,
                )
            elif record.phase is _ActivationJournalPhase.DB_REPLACED:
                report = _recover_manifest_publication(port,
                    record,
                    journal_identity,
                )
            elif (
                record.phase
                is _ActivationJournalPhase.MANIFEST_PUBLISHED
            ):
                report = _recover_generation_publication(port,
                    record,
                    journal_identity,
                    report_phase=_ActivationJournalPhase.MANIFEST_PUBLISHED,
                )
            else:
                if rollback_terminal_coexists:
                    next_generation = (
                        0
                        if record.expected_prior_generation is None
                        else record.expected_prior_generation + 1
                    )
                    activation_digest = _activation_publication_digest(
                        record,
                        next_generation=next_generation,
                    )
                    try:
                        _revalidate_recovered_active_set(port,
                            record,
                            identity=port.resource_identity,
                            canonical_store_id=record.canonical_store_id,
                            next_generation=next_generation,
                            activation_digest=activation_digest,
                            require_manifest_published=True,
                        )
                    except ActivationPreparationError:
                        pass
                    else:
                        raise ActivationPreparationError(
                            "ACTIVATION.TERMINAL_COEXISTENCE_INVALID",
                            retryable=False,
                        )
                report = _replay_terminal_recovery(port,
                    record,
                    journal_identity,
                )
        except ActivationPreparationError as error:
            if not _activation_rollback_eligible(error):
                raise
            # Task 5.9: completion cannot be proven, so restore one
            # complete prior authority (or the legacy first-activation
            # state) instead of leaving the resource fail-stopped.
            report = _rollback_inconsistent_activation(port,
                record,
                journal_identity,
            )
        port.preparation = None
        port.cleanup_reservation = None
        port.state = "READY"
        port.notify_all()
        return report
    except BaseException:
        port.notify_all()
        raise


def rollback_durable_activation(
    port: _StoreValidationPort,
) -> ActivationRecoveryReport | None:
    """Roll back one pending/inconsistent activation (Task 5.9).

    Narrow coordinator entry point that restores exactly one complete
    prior authority (or the legacy first-activation state) instead of
    completing a pending activation.  It is usable both from ``READY``
    (fresh start with a durable journal) and from ``ACTIVATING`` after a
    fail-stop (for example a failed :meth:`publish_activation`), and it
    is idempotent: repeated calls never create a generation, consume a
    token twice, duplicate quarantine, or delete foreign files.  The
    main journal and any coexisting terminal are re-read and
    authenticated before any mutation, and the pending main journal
    takes precedence under the Task 5.8 coexistence rules.  A fully
    proven ``GENERATION_PUBLISHED`` journal (a completed activation) is
    refused; recovery of that state is
    :meth:`recover_durable_activation`.
    """

    stage_seal_error = port.stage_seal_error
    if (
        port.state not in {"READY", "ACTIVATING"}
        or port.cleanup_in_progress
        or port.cleanup_reservation is not None
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ROLLBACK_STATE_INVALID",
            retryable=True,
        )
    journal_path = _activation_journal_path(port.resource_identity)
    terminal_path = _activation_terminal_path(port.resource_identity)
    if port.state == "READY":
        # A rollback invoked from READY mutates/switches the authority, so
        # drain live leases first (READY -> DRAINING -> proven -> ACTIVATING).
        # A fail-stopped ACTIVATING rollback needs no drain: ACTIVATING is
        # entered only after a completed drain, so no lease can be live.
        port.drain_for_transition()
    try:
        try:
            journal_identity = _lstat_activation_journal_identity(
                journal_path
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        try:
            terminal_identity = _lstat_activation_terminal_identity(
                terminal_path
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error
        journal_temp_path = _activation_journal_temp_path(
            journal_path
        )
        terminal_temp_path = _activation_terminal_temp_path(
            terminal_path
        )
        if _lstat_any_entry(journal_temp_path):
            if (
                journal_identity is not None
                or terminal_identity is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_JOURNAL_TEMP_CONFLICT",
                    retryable=False,
                )
            _remove_orphaned_activation_temp(journal_temp_path)
        if _lstat_any_entry(terminal_temp_path):
            if (
                terminal_identity is not None
                or journal_identity is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_TERMINAL_TEMP_CONFLICT",
                    retryable=False,
                )
            _remove_orphaned_activation_temp(terminal_temp_path)
        if journal_identity is None:
            if terminal_identity is None:
                # A live in-memory view never bypasses the cold discovery:
                # the marker and the active canonical pair are re-proven
                # from disk (or fail closed), so a deleted/tampered
                # authority is never silently trusted from caller memory.
                report = _discover_active_canonical(port)
            else:
                terminal_record = _load_recovery_terminal(port,
                    terminal_path,
                    terminal_identity,
                )
                _revalidate_recovery_authority(port,
                    terminal_record
                )
                if (
                    terminal_record.phase
                    is _ActivationJournalPhase.GENERATION_PUBLISHED
                ):
                    report = _replay_terminal_recovery(port,
                        terminal_record,
                        terminal_identity,
                    )
                elif (
                    terminal_record.phase
                    is _ActivationJournalPhase.PREPARED
                ):
                    report = (
                        _replay_cancelled_terminal_recovery(port,
                            terminal_record,
                        )
                    )
                else:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                        retryable=False,
                    )
            port.preparation = None
            port.cleanup_reservation = None
            port.state = "READY"
            port.notify_all()
            return report
        record = _load_recovery_journal(port,
            journal_path,
            journal_identity,
        )
        _revalidate_recovery_authority(port, record)
        if terminal_identity is not None:
            terminal_record = _load_recovery_terminal(port,
                terminal_path,
                terminal_identity,
            )
            _revalidate_recovery_authority(port,
                terminal_record
            )
            if not _activation_terminal_coexistence_valid(
                record,
                terminal_record,
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.TERMINAL_COEXISTENCE_INVALID",
                    retryable=False,
                )
        if (
            record.phase
            is _ActivationJournalPhase.GENERATION_PUBLISHED
        ):
            next_generation = (
                0
                if record.expected_prior_generation is None
                else record.expected_prior_generation + 1
            )
            activation_digest = _activation_publication_digest(
                record,
                next_generation=next_generation,
            )
            try:
                _revalidate_recovered_active_set(port,
                    record,
                    identity=port.resource_identity,
                    canonical_store_id=record.canonical_store_id,
                    next_generation=next_generation,
                    activation_digest=activation_digest,
                    require_manifest_published=True,
                )
            except ActivationPreparationError:
                pass
            else:
                raise ActivationPreparationError(
                    "ACTIVATION.ROLLBACK_COMPLETED_INVALID",
                    retryable=False,
                )
        if port.preparation is not None:
            try:
                entry = port.sealed_registry._token_entry(
                    port.preparation._token
                )
            except stage_seal_error:
                entry = None
            if entry is not None and (
                entry.state
                is contract_module.ActivationCapabilityState.TOKEN_ISSUED
            ):
                try:
                    port.sealed_registry.cancel(
                        port.preparation._token
                    )
                except stage_seal_error as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.ROLLBACK_TOKEN_CANCEL_FAILED",
                        retryable=True,
                        reason_code=cast(str, getattr(error, "error_code")),
                    ) from error
        report = _rollback_inconsistent_activation(port,
            record,
            journal_identity,
        )
        port.preparation = None
        port.cleanup_reservation = None
        port.state = "READY"
        port.notify_all()
        return report
    except BaseException:
        port.notify_all()
        raise


def publish_activation(
    port: _CoordinatorPublishPort,
    preparation: _ActivationPreparation,
    handle: _ActivationJournalHandle,
) -> int:
    """Publish one sealed DB/manifest set and exactly one generation.

    The caller must first obtain ``preparation`` from :meth:`activate`
    and a durable PREPARED ``handle`` from
    :meth:`publish_prepared_activation`.  Every journal phase is written
    only after its matching file effect is durable and independently
    revalidated.  Any failure leaves the coordinator fail-stopped in
    ``ACTIVATING`` with the last truthful journal phase for Tasks 5.8/5.9;
    this method never rewinds or claims READY on a partial publication.
    """

    if type(preparation) is not _ActivationPreparation:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_PREPARATION_INVALID",
            retryable=False,
        )
    if type(handle) is not _ActivationJournalHandle:
        raise ActivationPreparationError(
            "ACTIVATION.JOURNAL_HANDLE_INVALID",
            retryable=False,
        )
    stage_seal_error = port.stage_seal_error
    prepared_record = _load_activation_transition_record(port,
        preparation,
        handle,
        _ActivationJournalPhase.DB_REPLACED,
    )
    # PREPARED keeps the full candidate/prior closure.  It is the
    # last point at which that validation is meaningful because the
    # following atomic replace consumes the candidate path.
    _revalidate_activation_journal_closure(port,
        preparation,
        prepared_record,
    )

    _replace_activation_database(
        prepared_record,
        identity=port.resource_identity,
    )
    _validate_replaced_activation_database(port,
        prepared_record,
        preparation=preparation,
        identity=port.resource_identity,
        canonical_store_id=prepared_record.canonical_store_id,
    )
    handle = port.advance_after_effect(
        preparation,
        handle,
        _ActivationJournalPhase.DB_REPLACED,
    )

    next_generation = (
        0
        if prepared_record.expected_prior_generation is None
        else prepared_record.expected_prior_generation + 1
    )
    activation_digest = _activation_publication_digest(
        prepared_record,
        next_generation=next_generation,
    )
    _publish_activation_receipt(port,
        prepared_record,
        preparation=preparation,
        identity=port.resource_identity,
        canonical_store_id=prepared_record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
    )
    _publish_activation_manifest(
        prepared_record,
        preparation=preparation,
        identity=port.resource_identity,
    )
    active_ref, active_snapshot, active_attestation = (
        _validate_published_activation_set(port,
        prepared_record,
        preparation=preparation,
        identity=port.resource_identity,
        canonical_store_id=prepared_record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
        )
    )
    handle = port.advance_after_effect(
        preparation,
        handle,
        _ActivationJournalPhase.MANIFEST_PUBLISHED,
        next_generation=next_generation,
        activation_digest=activation_digest,
        active_content_attestation=active_attestation,
    )
    active_record = handle._record

    # Rehash and reopen the attested active set immediately before the
    # in-memory generation switch. State remains ACTIVATING, so no operation
    # can observe this view until the final phase and token consumption are
    # both durable/complete.
    active_ref, active_snapshot = _revalidate_active_content_attestation(port,
        active_record,
        identity=port.resource_identity,
        canonical_store_id=prepared_record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
    )
    prior_view = port.view
    port.view = _SQLiteGenerationView(
        stage=active_ref,
        canonical_store_id=prepared_record.canonical_store_id,
        generation=next_generation,
        fts5_available=active_snapshot.fts5_available,
    )
    try:
        _retire_coexisting_terminal(port, active_record)
        handle = port.advance_after_effect(
            preparation,
            handle,
            _ActivationJournalPhase.GENERATION_PUBLISHED,
            next_generation=next_generation,
            activation_digest=activation_digest,
        )
    except BaseException:
        # The final journal did not durably acknowledge the in-memory
        # publication.  State is still ACTIVATING and no lease could
        # observe it, so restore the prior visible generation while
        # leaving the durable DB/manifest set for recovery.
        port.view = prior_view
        raise
    final_record = handle._record
    final_ref, _final_snapshot = _revalidate_active_content_attestation(port,
        final_record,
        identity=port.resource_identity,
        canonical_store_id=prepared_record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
    )
    if port.view is None or port.view.stage != final_ref:
        raise ActivationPreparationError(
            "ACTIVATION.GENERATION_PUBLICATION_INVALID",
            retryable=False,
        )
    try:
        port.sealed_registry.consume(preparation._token)
    except stage_seal_error as error:
        raise ActivationPreparationError(
            "ACTIVATION.TOKEN_CONSUME_FAILED",
            retryable=True,
            reason_code=cast(str, getattr(error, "error_code")),
        ) from error
    # Only after the GENERATION_PUBLISHED journal is durable, the final
    # active-set revalidation passed, and the live token is consumed is the
    # write-once activated-lineage marker ensured.  A marker failure leaves
    # the completed main journal as the cold-recovery authority (the view
    # stays withheld under ACTIVATING) and a fresh recovery resumes the
    # publication; the marker is never cleared by rollback or cancellation
    # and survives every later generation/import/upgrade.
    _ensure_activation_lineage_marker(port.resource_identity)
    # The completed activation is durable (GENERATION_PUBLISHED journal,
    # final revalidation, consumed token, marker): coordinator authority
    # switches to the candidate store id before READY so the replacement
    # becomes the resource's current canonical identity.
    port._activate_candidate_store_id(prepared_record.canonical_store_id)
    port.preparation = None
    port.state = "READY"
    port.notify_all()
    return next_generation
