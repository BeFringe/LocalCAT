"""Read-only JSONL migration preflight and idempotency checks."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
from typing import cast
import uuid

from tm_activation_journal import _ActivationPreparation
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    AssetKind,
    AssetPreservationEvidence,
    AssetPreservationState,
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    MigrationDiagnostic,
    MigrationFailure,
    MigrationOutcome,
    MigrationPreflight,
    MigrationReport,
    MutableStageRef,
    RecoveryLocator,
    SchemaUpgradeFailure,
    SchemaUpgradeOutcome,
    SchemaUpgradeReport,
    SealedStage,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
    TM_LEGACY_SCHEMA_VERSION,
    TM_SCHEMA_VERSION,
    ResourceStoreCoordinator,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    _APPROVED_SCHEMA_DIGESTS,
    _FTS5_STATEMENT,
    _SCHEMA_STATEMENTS,
    _SCHEMA_UPGRADE_META_KEY,
    _SCHEMA_UPGRADE_META_VALUE,
    _SchemaUpgradeSnapshotTicket,
    _legacy_completed_origin_blocks,
    _legacy_revision_ancestry,
    _open_configured_connection,
    initialize_stage_schema,
    inspect_stage_schema,
    unique_character_ngrams,
)
from tm_stage_sealer import StageSealer


_NATIVE_PATH_TYPE = type(Path())
MIGRATION_STREAM_CHUNK_SIZE = 5000

_REJECTION_DIAGNOSTICS = {
    "ROW.INVALID_UTF8": (
        "PREFLIGHT.DECODE",
        "ROW_SKIPPED_INVALID_UTF8",
    ),
    "ROW.INVALID_JSON": (
        "PREFLIGHT.PARSE",
        "ROW_SKIPPED_INVALID_JSON",
    ),
    "ROW.INVALID_SHAPE": (
        "PREFLIGHT.VALIDATE",
        "ROW_SKIPPED_INVALID_SHAPE",
    ),
    "ROW.INVALID_REQUIRED_FIELD": (
        "PREFLIGHT.VALIDATE",
        "ROW_SKIPPED_INVALID_REQUIRED_FIELD",
    ),
}


@dataclass(frozen=True)
class MigrationStageBuild:
    """One unpublished mutable stage; no sidecar reuse before activation."""

    preflight: MigrationPreflight
    mutable_stage: MutableStageRef | None
    reused_completed_revision: int | None

    def __post_init__(self) -> None:
        if type(self.preflight) is not MigrationPreflight:
            raise TypeError("preflight must be exact MigrationPreflight")
        if (
            self.mutable_stage is not None
            and type(self.mutable_stage) is not MutableStageRef
        ):
            raise TypeError(
                "mutable_stage must be exact MutableStageRef or None"
            )
        if self.reused_completed_revision is not None and (
            type(self.reused_completed_revision) is not int
            or self.reused_completed_revision < 1
        ):
            raise ValueError(
                "reused revision must be a positive built-in integer"
            )
        if (self.mutable_stage is None) == (
            self.reused_completed_revision is None
        ):
            raise ValueError(
                "stage build must contain exactly one reusable result"
            )


class _StreamingBuildObservation:
    """Mutable counters and digest accumulated while re-streaming the source."""

    __slots__ = (
        "digest",
        "valid_count",
        "invalid_count",
        "duplicate_source_count",
        "variant_count",
        "source_counts",
    )

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.valid_count = 0
        self.invalid_count = 0
        self.duplicate_source_count = 0
        self.variant_count = 0
        self.source_counts: dict[str, int] = {}


@dataclass(frozen=True)
class _CreatedFileIdentity:
    device: int
    inode: int


class MigrationPreflightError(RuntimeError):
    """Stable preflight failure that never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


class TMMigrationService:
    """Preflight one configured legacy JSONL without changing any asset."""

    def __init__(
        self,
        *,
        resource_identity: CanonicalResourceIdentity,
        canonical_store_id: str,
        coordinator: ResourceStoreCoordinator | None = None,
    ) -> None:
        self._resource_identity = _snapshot_resource_identity(resource_identity)
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        if coordinator is not None and type(coordinator) is not (
            ResourceStoreCoordinator
        ):
            raise TypeError(
                "coordinator must be exact ResourceStoreCoordinator or None"
            )
        self._canonical_store_id = canonical_store_id
        self._coordinator = coordinator

    @property
    def resource_identity(self) -> CanonicalResourceIdentity:
        return self._resource_identity

    @property
    def canonical_store_id(self) -> str:
        return self._canonical_store_id

    def preflight(self, source: Path) -> MigrationPreflight:
        """Stream exact source bytes and return safe, deterministic facts."""

        self._validate_source_preconditions(source)
        preflight = _scan_jsonl(source)
        self._reject_sidecar_reuse(preflight.source_digest)
        return preflight

    def build_mutable_stage(self, source: Path) -> MigrationStageBuild:
        """Build or reuse one complete unpublished migration stage."""

        preflight = self.preflight(source)
        stage, _stage_identity, _manifest_identity = self._build_stage(
            source,
            preflight=preflight,
            canonical_store_id=self._canonical_store_id,
            batch_kind="migration",
            batch_prefix="migration",
            snapshot_prefix="snapshot.migration",
            stage_prefix="migration",
        )
        return MigrationStageBuild(
            preflight=preflight,
            mutable_stage=stage,
            reused_completed_revision=None,
        )

    def import_snapshot(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        """Explicitly replace the active canonical with one exact snapshot.

        Task 5.10 disambiguation entry point: the source must be the
        explicitly chosen configured JSONL.  The operation builds a fresh
        mutable stage with a brand-new canonical store id, seals it, and
        drives the live coordinator's explicit replacement activation
        pipeline.  On success the resource gets a new generation with the
        new store id and binding and any ``SOURCE_DIVERGED`` state is
        cleared; on failure the prior canonical, JSONL bytes, manifest
        bytes, and divergence state are preserved.
        """

        return self._explicit_disambiguation(source, resource_id)

    def rebuild_from_snapshot(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        """Explicitly rebuild the active canonical from one exact snapshot.

        Semantically identical to :meth:`import_snapshot`: both operations
        perform the same full-replacement safety contract and differ only
        in the caller's intent.
        """

        return self._explicit_disambiguation(source, resource_id)

    def upgrade_schema(
        self,
        store_path: Path,
    ) -> SchemaUpgradeOutcome:
        """Copy-switch one old-schema canonical to the current schema.

        Task 5.11 entry point: the coordinator proves the prior v1
        ancestry and validates the complete binding/manifest/receipt/
        source/divergence closure, then mints one single-use snapshot
        ticket backed by a ``Connection.backup()`` recovery backup of
        the active old-schema store.  A fresh mutable copy of the live
        store is migrated in place (proven completion order, records
        preserved verbatim, candidate indexes rebuilt) and sealed in the
        private schema-upgrade mode; the existing seal/activate pipeline
        then publishes the equivalent new generation under the same
        canonical store id guarded by the ticket.  The old store is never
        mutated in place; every failure stage leaves it byte-identical
        and reopenable, divergence/tampering/unprovable order fails
        closed and is never repaired, and the recovery backup is
        reported as digest-backed restoration evidence.
        """

        coordinator = self._coordinator
        if coordinator is None:
            raise MigrationPreflightError("SCHEMA.COORDINATOR_UNAVAILABLE")
        active_store_path = coordinator.active_store_path
        if (
            type(store_path) is not _NATIVE_PATH_TYPE
            or active_store_path is None
            or store_path != active_store_path
        ):
            raise MigrationPreflightError("SCHEMA.ACTIVE_STORE_REQUIRED")
        store_before = _try_file_digest(store_path)
        if store_before is None:
            raise MigrationPreflightError("SCHEMA.ACTIVE_STORE_UNREADABLE")
        prior_generation = coordinator.current_generation
        if prior_generation is None:
            raise MigrationPreflightError("SCHEMA.ACTIVE_RESOURCE_REQUIRED")

        stage_label = "PREFLIGHT"
        ticket: _SchemaUpgradeSnapshotTicket | None = None
        backup_path: Path | None = None
        backup_identity: tuple[int, int] | None = None
        backup_digest: str | None = None
        prepared: _ActivationPreparation | None = None
        sealed: SealedStage | None = None
        copy_stage: MutableStageRef | None = None
        stage_identity: _CreatedFileIdentity | None = None
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            if coordinator.canonical_store_id != self._canonical_store_id:
                raise MigrationPreflightError("SCHEMA.COORDINATOR_MISMATCH")
            if _schema_version_of_store(store_path) == TM_SCHEMA_VERSION:
                raise MigrationPreflightError(
                    "SCHEMA.SCHEMA_ALREADY_CURRENT"
                )
            activation_digest = _read_active_activation_digest(store_path)
            schema = inspect_stage_schema(
                _upgrade_source_ref(
                    self._resource_identity,
                    store_path,
                ),
                canonical_store_id=self._canonical_store_id,
                _allow_legacy_schema=True,
                _allow_active=True,
                _expected_active_generation=prior_generation,
                _expected_activation_digest=activation_digest,
            )
            if schema.schema_version != TM_LEGACY_SCHEMA_VERSION:
                raise MigrationPreflightError("SCHEMA.SCHEMA_UNSUPPORTED")
            receipt, manifest_kind = _read_legacy_snapshot_facts(
                store_path,
                canonical_store_id=self._canonical_store_id,
            )
            ticket = coordinator.prepare_schema_upgrade_ticket()
            backup_path = ticket.backup_path
            backup_identity = ticket.backup_identity
            backup_digest = ticket.backup_digest
            # The ticket seam is the stabilization point.  Build and inspect
            # the candidate only from that durable recovery snapshot; the
            # live canonical is used again solely by the post-drain ticket
            # guard, which rejects any write that happened after capture.
            activation_digest = _read_active_activation_digest(backup_path)
            stage_label = "COPY"
            copy_stage = _deterministic_stage_ref(
                self._resource_identity,
                source_digest=receipt.jsonl_digest,
                stage_prefix="schema-upgrade",
                path_salt=f"upgrade.{uuid.uuid4().hex}",
            )
            stage_identity = _copy_store_into_stage(
                backup_path,
                copy_stage.staged_db_path,
            )
            inspect_stage_schema(
                copy_stage,
                canonical_store_id=self._canonical_store_id,
                _allow_legacy_schema=True,
                _allow_active=True,
                _expected_active_generation=prior_generation,
                _expected_activation_digest=activation_digest,
            )
            with _open_configured_connection(
                copy_stage.staged_db_path,
                require_existing=True,
            ) as connection:
                _migrate_schema_copy(
                    connection,
                    fts5_available=schema.fts5_available,
                )
            inspect_stage_schema(
                copy_stage,
                canonical_store_id=self._canonical_store_id,
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=manifest_kind,
                receipt=receipt,
                receipt_digest=snapshot_receipt_digest(receipt),
            )
            manifest_identity = _write_new_file(
                copy_stage.manifest_temp_path,
                contract_to_json(manifest).encode("utf-8"),
            )
            stage_label = "ACTIVATION"
            sealed = StageSealer(
                registry=coordinator.sealed_registry,
                canonical_store_id=self._canonical_store_id,
            ).seal(
                copy_stage,
                expected_prior_generation=prior_generation,
                schema_upgrade=True,
            )
            prepared = coordinator.activate(
                sealed,
                _schema_upgrade_ticket=ticket,
            )
            ticket = None
            handle = coordinator.publish_prepared_activation(prepared)
            generation = coordinator.publish_activation(prepared, handle)
            published_store_path = coordinator.active_store_path
            if published_store_path is None:
                raise MigrationPreflightError(
                    "SCHEMA.SUCCESS_UNVERIFIABLE"
                )
            success_digest = _try_file_digest(published_store_path)
            if success_digest is None:
                raise MigrationPreflightError(
                    "SCHEMA.SUCCESS_UNVERIFIABLE"
                )
            if backup_path is None or backup_digest is None:
                raise MigrationPreflightError(
                    "SCHEMA.BACKUP_UNVERIFIABLE"
                )
            return self._schema_upgrade_report(
                generation=generation,
                backup_path=backup_path,
                backup_digest=backup_digest,
                success_digest=success_digest,
            )
        except BaseException as error:
            if ticket is not None:
                try:
                    coordinator.retire_schema_upgrade_ticket(ticket)
                except Exception as retire_error:
                    if isinstance(error, Exception):
                        error = retire_error
            if (
                copy_stage is not None
                and stage_identity is not None
                and sealed is None
            ):
                if manifest_identity is not None:
                    _remove_created_file(
                        copy_stage.manifest_temp_path,
                        manifest_identity,
                    )
                _remove_created_file(
                    copy_stage.staged_db_path,
                    stage_identity,
                )
            if not isinstance(error, Exception):
                raise
            if prepared is not None:
                return self._reconcile_failed_upgrade_activation(
                    error,
                    prepared=prepared,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    backup_digest=backup_digest,
                )
            if coordinator.state == "ACTIVATING":
                # The preparation failed before any durable journal but
                # its cleanup reservation is still pending: retry the
                # narrow cleanup (or honestly fail stop on that cleanup).
                try:
                    coordinator.retry_failed_activation_cleanup()
                except Exception as cleanup_error:
                    return self._schema_upgrade_failure(
                        cleanup_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                    )
            return self._schema_upgrade_failure(
                error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
            )

    def _explicit_disambiguation(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        coordinator = self._coordinator
        stage_label = "PREFLIGHT"
        preflight: MigrationPreflight | None = None
        source_before = _try_file_digest(
            self._resource_identity.configured_jsonl_path
        )
        if source_before is None:
            raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE")
        if coordinator is None:
            raise MigrationPreflightError("IMPORT.COORDINATOR_UNAVAILABLE")
        store_path = coordinator.active_store_path
        if store_path is None:
            raise MigrationPreflightError("IMPORT.ACTIVE_RESOURCE_REQUIRED")
        store_before = _try_file_digest(
            store_path
        )
        if store_before is None:
            raise MigrationPreflightError("IMPORT.ACTIVE_STORE_UNREADABLE")
        prepared = None
        new_store_id: str | None = None
        sealed: SealedStage | None = None
        stage: MutableStageRef | None = None
        stage_identity: _CreatedFileIdentity | None = None
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            if (
                type(resource_id) is not str
                or not resource_id.strip()
            ):
                raise MigrationPreflightError("IMPORT.RESOURCE_ID_INVALID")
            if resource_id != self._resource_identity.resource_id:
                raise MigrationPreflightError(
                    "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
                )
            if coordinator.canonical_store_id != self._canonical_store_id:
                raise MigrationPreflightError("IMPORT.COORDINATOR_MISMATCH")
            prior_generation = coordinator.current_generation
            if prior_generation is None:
                raise MigrationPreflightError(
                    "IMPORT.ACTIVE_RESOURCE_REQUIRED"
                )
            self._validate_source_preconditions(source)
            preflight = _scan_jsonl(source)
            origin_token = uuid.uuid4().hex
            new_store_id = f"store.import.{origin_token}"
            stage, stage_identity, manifest_identity = self._build_stage(
                source,
                preflight=preflight,
                canonical_store_id=new_store_id,
                batch_kind="import",
                batch_prefix="import",
                snapshot_prefix="snapshot.import",
                stage_prefix="import",
                path_salt=uuid.uuid4().hex,
                batch_id=f"import.{origin_token}",
            )
            stage_label = "ACTIVATION"
            sealed = StageSealer(
                registry=coordinator.sealed_registry,
                canonical_store_id=new_store_id,
            ).seal(
                stage,
                expected_prior_generation=prior_generation,
            )
            prepared = coordinator.activate_replacement(sealed)
            handle = coordinator.publish_prepared_activation(prepared)
            generation = coordinator.publish_activation(prepared, handle)
            report = self._success_report(
                preflight=preflight,
                sealed=sealed,
                canonical_store_id=new_store_id,
                generation=generation,
            )
            # The publication is durably READY: the same service instance
            # adopts the fresh canonical store id so a second explicit
            # import/rebuild of the identical bytes succeeds as the next
            # generation without a caller-side service renewal.
            self._canonical_store_id = new_store_id
            return report
        except Exception as error:
            if (
                stage is not None
                and stage_identity is not None
                and sealed is None
            ):
                if manifest_identity is not None:
                    _remove_created_file(
                        stage.manifest_temp_path,
                        manifest_identity,
                    )
                _remove_created_file(
                    stage.staged_db_path,
                    stage_identity,
                )
            if prepared is not None:
                return self._reconcile_failed_activation(
                    error,
                    prepared=prepared,
                    sealed=sealed,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    new_store_id=new_store_id,
                )
            return self._disambiguation_failure(
                error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
            )

    def _success_report(
        self,
        *,
        preflight: MigrationPreflight,
        sealed: SealedStage,
        canonical_store_id: str,
        generation: int,
    ) -> MigrationReport:
        """Build one completed import report from sealed evidence."""

        receipt = sealed.evidence.source_binding.receipt
        if (
            type(receipt) is not SnapshotReceipt
            or receipt.canonical_store_id != canonical_store_id
        ):
            raise MigrationPreflightError("IMPORT.RECEIPT_MISMATCH")
        return MigrationReport(
            resource_id=self._resource_identity.resource_id,
            canonical_store_id=canonical_store_id,
            source_digest=preflight.source_digest,
            snapshot_receipt=receipt,
            migrated_count=preflight.valid_count,
            variant_count=preflight.variant_count,
            skipped_count=preflight.invalid_count,
            diagnostics=preflight.diagnostics,
            activated_generation=generation,
            canonical_exact_available=True,
            context_available=False,
            fuzzy_available=False,
        )

    def _reconcile_failed_activation(
        self,
        error: Exception,
        *,
        prepared: _ActivationPreparation,
        sealed: SealedStage | None,
        preflight: MigrationPreflight | None,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        source_before: str,
        store_before: str,
        store_path: Path,
        new_store_id: str | None,
    ) -> MigrationOutcome:
        """Auto-restore the prior READY service after one failed activation.

        Task 5.10 failure reconciliation: a failure before any durable
        journal cancels the live preparation (no journal means nothing was
        replaced), a durable pending journal (PREPARED through
        MANIFEST_PUBLISHED) is rolled back, and a durable
        ``GENERATION_PUBLISHED`` journal whose candidate active set is
        provable completes via fresh-coordinator recovery and reports
        success.  A rollback that cannot be proven fails stop with honest
        unverified preservation evidence and never claims VERIFIED_UNCHANGED.
        """

        try:
            journal_phase = coordinator.durable_activation_phase
        except Exception as phase_error:
            return self._disambiguation_failure(
                phase_error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
                force_unverified=True,
            )
        if journal_phase is None:
            try:
                coordinator.cancel_prepared_activation(prepared)
            except Exception as cancel_error:
                if _disambiguation_error_code(
                    cancel_error
                ) != "ACTIVATION.PREPARATION_NOT_ACTIVE":
                    return self._disambiguation_failure(
                        cancel_error,
                        preflight=preflight,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        source_before=source_before,
                        store_before=store_before,
                        store_path=store_path,
                        force_unverified=True,
                    )
            return self._disambiguation_failure(
                error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
            )
        if journal_phase == "GENERATION_PUBLISHED":
            try:
                coordinator.rollback_durable_activation()
            except Exception as rollback_error:
                if _disambiguation_error_code(
                    rollback_error
                ) != "ACTIVATION.ROLLBACK_COMPLETED_INVALID":
                    return self._disambiguation_failure(
                        rollback_error,
                        preflight=preflight,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        source_before=source_before,
                        store_before=store_before,
                        store_path=store_path,
                        force_unverified=True,
                    )
            else:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                )
            # The candidate active set is proven and the completed journal
            # is the cold-recovery authority: a fresh coordinator bound to
            # the unchanged prior id re-proves and completes the candidate
            # publication (crash-window recovery), then the high-level
            # operation reports the completed import.
            if preflight is None or new_store_id is None:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            recovery_coordinator = ResourceStoreCoordinator(
                canonical_store_id=coordinator.canonical_store_id,
                resource_identity=self._resource_identity,
            )
            report = recovery_coordinator.recover_durable_activation()
            if (
                report is None
                or getattr(report, "action", None) != "COMPLETED"
                or report.generation is None
                or recovery_coordinator.state != "READY"
                or recovery_coordinator.canonical_store_id != new_store_id
                or recovery_coordinator.current_generation
                != report.generation
            ):
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            if sealed is None:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            # The crash-window recovery completed the candidate
            # publication through a fresh coordinator bound to the
            # unchanged prior id.  The original coordinator adopts the
            # recovered authority (view, store id, generation) through
            # its narrow coordinator-owned transition and returns to
            # READY so the same live service keeps serving the completed
            # import without a caller-side restart.
            coordinator.adopt_recovered_authority(recovery_coordinator)
            adopted_report = self._success_report(
                preflight=preflight,
                sealed=sealed,
                canonical_store_id=new_store_id,
                generation=report.generation,
            )
            # The recovered publication is durably READY: the same service
            # instance adopts the fresh canonical store id so subsequent
            # explicit imports reuse this service without renewal.
            self._canonical_store_id = new_store_id
            return adopted_report
        try:
            coordinator.rollback_durable_activation()
        except Exception as rollback_error:
            return self._disambiguation_failure(
                rollback_error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
                force_unverified=True,
            )
        return self._disambiguation_failure(
            error,
            preflight=preflight,
            stage_label=stage_label,
            coordinator=coordinator,
            source_before=source_before,
            store_before=store_before,
            store_path=store_path,
        )

    def _disambiguation_failure(
        self,
        error: Exception,
        *,
        preflight: MigrationPreflight | None,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        source_before: str,
        store_before: str,
        store_path: Path,
        force_unverified: bool = False,
    ) -> MigrationFailure:
        """Build one preservation-backed failure without leaking paths.

        ``force_unverified`` marks the active store UNVERIFIED with a
        recovery locator and non-retryable when the rollback outcome could
        not be proven: an unprovable rollback never claims
        VERIFIED_UNCHANGED.
        """

        identity = self._resource_identity
        error_code = _disambiguation_error_code(error)
        retryable = _disambiguation_retryable(error)
        diagnostics = (
            () if preflight is None else preflight.diagnostics
        )
        active_generation = coordinator.current_generation
        source_observed = _try_file_digest(
            identity.configured_jsonl_path
        )
        store_observed = _try_file_digest(store_path)
        locators: list[RecoveryLocator] = []
        if source_observed == source_before:
            source_evidence = _unchanged_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
            )
        elif source_observed is not None:
            source_evidence = _changed_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
                source_observed,
            )
        else:
            source_evidence = _unverified_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
            )
            locators.append(
                RecoveryLocator(
                    path=identity.configured_jsonl_path,
                    asset_kind=AssetKind.ORIGINAL_SOURCE,
                    expected_digest=source_before,
                )
            )
            retryable = False
        if force_unverified:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            backup_path = _find_recovery_backup(
                store_path,
                label="database",
                expected_digest=store_before,
            )
            if backup_path is None:
                backup_path = store_path
            locators.append(
                RecoveryLocator(
                    path=backup_path,
                    asset_kind=AssetKind.ACTIVE_STORE,
                    expected_digest=store_before,
                )
            )
            retryable = False
        elif store_observed == store_before:
            store_evidence = _unchanged_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
        elif store_observed is not None:
            store_evidence = _changed_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
                store_observed,
            )
        else:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            backup_path = _find_recovery_backup(
                store_path,
                label="database",
                expected_digest=store_before,
            )
            if backup_path is None:
                backup_path = store_path
            locators.append(
                RecoveryLocator(
                    path=backup_path,
                    asset_kind=AssetKind.ACTIVE_STORE,
                    expected_digest=store_before,
                )
            )
            retryable = False
        locators.sort(
            key=lambda locator: locator.asset_kind.value
        )
        return MigrationFailure(
            stage=stage_label,
            error_code=error_code,
            retryable=retryable,
            diagnostics=diagnostics,
            active_generation=active_generation,
            original_source_preservation=source_evidence,
            active_store_preservation=store_evidence,
            recovery_locators=tuple(locators),
        )

    def _schema_upgrade_report(
        self,
        *,
        generation: int,
        backup_path: Path,
        backup_digest: str,
        success_digest: str,
    ) -> SchemaUpgradeReport:
        """Build one completed schema upgrade report from durable facts."""

        return SchemaUpgradeReport(
            canonical_store_id=self._canonical_store_id,
            from_version=TM_LEGACY_SCHEMA_VERSION,
            to_version=TM_SCHEMA_VERSION,
            backup_path=backup_path,
            backup_digest=backup_digest,
            success_digest=success_digest,
            activated_generation=generation,
        )

    def _reconcile_failed_upgrade_activation(
        self,
        error: Exception,
        *,
        prepared: _ActivationPreparation,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        store_before: str,
        store_path: Path,
        backup_path: Path | None,
        backup_identity: tuple[int, int] | None,
        backup_digest: str | None,
    ) -> SchemaUpgradeOutcome:
        """Auto-restore the prior READY service after one failed upgrade.

        Schema upgrade reuses the activation pipeline, so its failure
        reconciliation follows Task 5.10 exactly: a failure before any
        durable journal cancels the live preparation (nothing was
        replaced), a durable pending journal is rolled back, and a
        durable ``GENERATION_PUBLISHED`` journal whose candidate active
        set is provable completes via fresh-coordinator recovery and
        reports the completed upgrade.  A rollback that cannot be proven
        fails stop with honest unverified preservation evidence and
        never claims VERIFIED_UNCHANGED.
        """

        try:
            journal_phase = coordinator.durable_activation_phase
        except Exception as phase_error:
            return self._schema_upgrade_failure(
                phase_error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
                force_unverified=True,
            )
        if journal_phase is None:
            try:
                coordinator.cancel_prepared_activation(prepared)
            except Exception as cancel_error:
                if _schema_upgrade_error_code(
                    cancel_error
                ) != "ACTIVATION.PREPARATION_NOT_ACTIVE":
                    return self._schema_upgrade_failure(
                        cancel_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                        force_unverified=True,
                    )
            return self._schema_upgrade_failure(
                error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
            )
        if journal_phase == "GENERATION_PUBLISHED":
            try:
                coordinator.rollback_durable_activation()
            except Exception as rollback_error:
                if _schema_upgrade_error_code(
                    rollback_error
                ) != "ACTIVATION.ROLLBACK_COMPLETED_INVALID":
                    return self._schema_upgrade_failure(
                        rollback_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                        force_unverified=True,
                    )
            else:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                )
            # The candidate active set is proven and the completed
            # journal is the cold-recovery authority: a fresh coordinator
            # bound to the unchanged store id re-proves and completes the
            # candidate publication (crash-window recovery), then the
            # high-level operation reports the completed upgrade.
            recovery_coordinator = ResourceStoreCoordinator(
                canonical_store_id=coordinator.canonical_store_id,
                resource_identity=self._resource_identity,
            )
            report = recovery_coordinator.recover_durable_activation()
            if (
                report is None
                or getattr(report, "action", None) != "COMPLETED"
                or report.generation is None
                or recovery_coordinator.state != "READY"
                or recovery_coordinator.canonical_store_id
                != self._canonical_store_id
                or recovery_coordinator.current_generation
                != report.generation
            ):
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            if backup_path is None or backup_digest is None:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            new_store_path = recovery_coordinator.active_store_path
            if new_store_path is None:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            success_digest = _try_file_digest(new_store_path)
            if success_digest is None:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            # The recovered publication is durably READY: the same
            # service instance keeps the preserved canonical store id and
            # adopts the recovered view, so a subsequent upgrade call
            # observes the current schema without a caller-side renewal.
            coordinator.adopt_recovered_authority(recovery_coordinator)
            return self._schema_upgrade_report(
                generation=report.generation,
                backup_path=backup_path,
                backup_digest=backup_digest,
                success_digest=success_digest,
            )
        try:
            coordinator.rollback_durable_activation()
        except Exception as rollback_error:
            return self._schema_upgrade_failure(
                rollback_error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
                force_unverified=True,
            )
        return self._schema_upgrade_failure(
            error,
            stage_label=stage_label,
            coordinator=coordinator,
            store_before=store_before,
            store_path=store_path,
            backup_path=backup_path,
            backup_identity=backup_identity,
        )

    def _schema_upgrade_failure(
        self,
        error: Exception,
        *,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        store_before: str,
        store_path: Path,
        backup_path: Path | None = None,
        backup_identity: tuple[int, int] | None = None,
        force_unverified: bool = False,
    ) -> SchemaUpgradeFailure:
        """Build one preservation-backed schema upgrade failure.

        ``force_unverified`` marks the active store UNVERIFIED with a
        recovery locator and non-retryable when the rollback outcome could
        not be proven: an unprovable rollback never claims
        VERIFIED_UNCHANGED.  A recovery locator is only ever built from a
        path whose current bytes are re-proven to equal the preservation
        ``before_digest``; when no such path exists the failure stops
        explicitly instead of exposing a locator whose bytes do not match.
        The schema-upgrade backup and any unexposed byte-exact locator
        snapshot are strictly removed so failed attempts never accumulate
        hidden full DB copies.
        """

        error_code = _schema_upgrade_error_code(error)
        retryable = _schema_upgrade_retryable(error)
        active_generation = coordinator.current_generation
        if active_generation is None:
            active_generation = 0
        store_observed = _try_file_digest(store_path)
        locators: list[RecoveryLocator] = []
        locator_path: Path | None = None
        needs_recovery = False
        if force_unverified:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            needs_recovery = True
        elif store_observed == store_before:
            store_evidence = _unchanged_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
        elif store_observed is not None:
            store_evidence = _changed_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
                store_observed,
            )
            needs_recovery = True
        else:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            needs_recovery = True
        if needs_recovery:
            locator_path = self._proven_schema_upgrade_locator(
                coordinator,
                store_path=store_path,
                store_before=store_before,
                backup_path=backup_path,
            )
            if locator_path is None:
                self._release_unexposed_schema_upgrade_artifacts(
                    coordinator,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    locator_path=None,
                )
                raise MigrationPreflightError(
                    "SCHEMA.PRIOR_STATE_UNRECOVERABLE"
                )
            locators.append(
                RecoveryLocator(
                    path=locator_path,
                    asset_kind=AssetKind.ACTIVE_STORE,
                    expected_digest=store_before,
                )
            )
            retryable = False
        self._release_unexposed_schema_upgrade_artifacts(
            coordinator,
            backup_path=backup_path,
            backup_identity=backup_identity,
            locator_path=locator_path,
        )
        outcome = SchemaUpgradeFailure(
            stage=stage_label,
            error_code=error_code,
            retryable=retryable,
            active_generation=active_generation,
            active_store_preservation=store_evidence,
            recovery_locators=tuple(locators),
        )
        snapshot = coordinator.schema_upgrade_locator_snapshot
        if (
            snapshot is not None
            and locator_path is not None
            and snapshot.path == locator_path
        ):
            coordinator._detach_schema_upgrade_locator_snapshot(locator_path)
        return outcome

    def _proven_schema_upgrade_locator(
        self,
        coordinator: ResourceStoreCoordinator,
        *,
        store_path: Path,
        store_before: str,
        backup_path: Path | None,
    ) -> Path | None:
        """One rehash-proven byte-exact recovery locator for the prior store.

        Candidates are the activation pipeline's journal-owned byte-exact
        ``.localcat-recovery.*.database.bak`` (the Task 5.10 locator
        family), the Design-required ``Connection.backup()`` schema
        backup, the coordinator-captured byte-exact locator snapshot, and
        finally the live canonical -- each accepted only after re-hashing
        proves its current bytes equal ``store_before``.
        """

        journal_backup = _find_recovery_backup(
            store_path,
            label="database",
            expected_digest=store_before,
        )
        if journal_backup is not None:
            return journal_backup
        if (
            backup_path is not None
            and _try_file_digest(backup_path) == store_before
        ):
            return backup_path
        snapshot = coordinator.schema_upgrade_locator_snapshot
        if (
            snapshot is not None
            and _try_file_digest(snapshot.path) == store_before
        ):
            return snapshot.path
        if _try_file_digest(store_path) == store_before:
            return store_path
        return None

    def _release_unexposed_schema_upgrade_artifacts(
        self,
        coordinator: ResourceStoreCoordinator,
        *,
        backup_path: Path | None,
        backup_identity: tuple[int, int] | None,
        locator_path: Path | None,
    ) -> None:
        """Strictly remove failure artifacts that no locator exposes."""

        snapshot = coordinator.schema_upgrade_locator_snapshot
        if snapshot is not None and snapshot.path != locator_path:
            coordinator.release_schema_upgrade_locator_snapshot()
        if (
            backup_path is not None
            and backup_identity is not None
            and backup_path != locator_path
        ):
            _remove_owned_schema_upgrade_backup(
                backup_path,
                backup_identity,
            )

    def _build_stage(
        self,
        source: Path,
        *,
        preflight: MigrationPreflight,
        canonical_store_id: str,
        batch_kind: str,
        batch_prefix: str,
        snapshot_prefix: str,
        stage_prefix: str,
        path_salt: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[
        MutableStageRef,
        _CreatedFileIdentity | None,
        _CreatedFileIdentity | None,
    ]:
        """Build or reuse one complete unpublished stage (shared builder).

        The stage is deterministic per (identity, source digest, prefix)
        and is never the canonical sidecar: reuse is only accepted after
        the full stage closure is re-proven from disk, and any build
        failure removes exactly the created stage files.  Migration
        origins use the deterministic ``migration.<source digest>`` batch
        id; explicit imports pass a fresh collision-resistant
        ``import.<uuid>`` batch id whose token also shapes the snapshot
        receipt id.  The returned identities describe exactly the files
        this builder created (``None`` for a reused stage), so a caller
        that fails before a sealed artifact owns them can remove exactly
        those paths without ever touching a foreign inode.
        """

        if batch_id is None:
            batch_id = f"{batch_prefix}.{preflight.source_digest}"
        if type(batch_id) is not str or not batch_id.strip():
            raise ValueError("batch id must be a non-empty string")
        stage = _deterministic_stage_ref(
            self._resource_identity,
            source_digest=preflight.source_digest,
            stage_prefix=stage_prefix,
            path_salt=path_salt,
        )
        if stage.staged_db_path.exists() or stage.manifest_temp_path.exists():
            _validate_reusable_stage(
                stage,
                canonical_store_id=canonical_store_id,
                preflight=preflight,
                batch_kind=batch_kind,
                batch_prefix=batch_prefix,
                snapshot_prefix=snapshot_prefix,
                batch_id=batch_id,
            )
            return stage, None, None

        initialize_stage_schema(
            stage,
            canonical_store_id=canonical_store_id,
        )
        stage_identity = _created_file_identity(stage.staged_db_path)
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            store = SQLiteTMStore(
                stage,
                canonical_store_id=canonical_store_id,
            )
            observation = _StreamingBuildObservation()
            store.append_streamed_batch(
                batch_id=batch_id,
                kind=batch_kind,
                drafts=_iter_draft_pairs(source, observation),
                source_digest=preflight.source_digest,
                source_path=source,
                invalid_count=preflight.invalid_count,
                duplicate_source_count=preflight.duplicate_source_count,
                chunk_size=MIGRATION_STREAM_CHUNK_SIZE,
            )
            if not _observation_matches(observation, preflight):
                raise MigrationPreflightError("MIGRATION.SOURCE_CHANGED")
            revision = store.canonical_revision()
            if (
                revision.head_revision != 1
                or revision.record_count != preflight.valid_count
            ):
                raise MigrationPreflightError(
                    "MIGRATION.STAGE_REVISION_INVALID"
                )
            receipt = SnapshotReceipt(
                snapshot_id=_snapshot_id_for_origin(
                    batch_id=batch_id,
                    batch_kind=batch_kind,
                    snapshot_prefix=snapshot_prefix,
                    source_digest=preflight.source_digest,
                ),
                resource_id=self._resource_identity.resource_id,
                canonical_store_id=canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=preflight.source_digest,
                record_count=preflight.valid_count,
            )
            store.register_issued_snapshot_receipt(
                receipt,
                destination_jsonl_path=(
                    self._resource_identity.configured_jsonl_path
                ),
                destination_manifest_path=(
                    self._resource_identity.snapshot_manifest_path
                ),
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=receipt,
                receipt_digest=snapshot_receipt_digest(receipt),
            )
            manifest_identity = _write_new_file(
                stage.manifest_temp_path,
                contract_to_json(manifest).encode("utf-8"),
            )
            _validate_reusable_stage(
                stage,
                canonical_store_id=canonical_store_id,
                preflight=preflight,
                batch_kind=batch_kind,
                batch_prefix=batch_prefix,
                snapshot_prefix=snapshot_prefix,
                batch_id=batch_id,
            )
        except BaseException:
            if manifest_identity is not None:
                _remove_created_file(
                    stage.manifest_temp_path,
                    manifest_identity,
                )
            _remove_created_file(stage.staged_db_path, stage_identity)
            raise
        return stage, stage_identity, manifest_identity

    def _validate_source_preconditions(self, source: Path) -> None:
        if type(source) is not _NATIVE_PATH_TYPE:
            raise TypeError("source must be an exact native Path")
        if source != self._resource_identity.configured_jsonl_path:
            raise MigrationPreflightError(
                "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
            )
        _require_target_parent_writable(self._resource_identity)
        _require_source_readable(source)

    def _reject_sidecar_reuse(self, source_digest: str) -> None:
        """Fail closed on any sidecar claim before activation authority.

        No completed reuse is ever reported from the sidecar within
        Tasks 5.1-5.2: a naked structural sidecar is not canonical
        authority. Deterministic conflicts still surface as stable codes.
        """

        identity = self._resource_identity
        sidecar = identity.canonical_sidecar_path
        manifest = identity.snapshot_manifest_path
        if not sidecar.exists():
            if manifest.exists():
                raise MigrationPreflightError(
                    "MIGRATION.MANIFEST_WITHOUT_SIDECAR"
                )
            return
        if sidecar.is_symlink() or not sidecar.is_file():
            raise MigrationPreflightError("MIGRATION.SIDECAR_INVALID")

        try:
            connection = sqlite3.connect(
                f"{sidecar.as_uri()}?mode=ro",
                uri=True,
            )
            try:
                connection.execute("PRAGMA query_only = ON")
                meta_rows = connection.execute(
                    "SELECT key, value FROM tm_meta WHERE key IN ("
                    "'activation_status', 'canonical_store_id', "
                    "'resource_id', 'target_identity') ORDER BY key"
                ).fetchall()
                meta = {str(key): str(value) for key, value in meta_rows}
                if (
                    meta.get("resource_id") != identity.resource_id
                    or meta.get("canonical_store_id")
                    != self._canonical_store_id
                    or meta.get("target_identity")
                    != identity.target_identity
                ):
                    raise MigrationPreflightError(
                        "MIGRATION.SIDECAR_IDENTITY_MISMATCH"
                    )
                rows = connection.execute(
                    "SELECT source_digest, status, completed_revision "
                    "FROM tm_origin_batch WHERE kind = 'migration' "
                    "ORDER BY completed_revision, batch_id"
                ).fetchall()
            finally:
                connection.close()
        except MigrationPreflightError:
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            raise MigrationPreflightError(
                "MIGRATION.SIDECAR_INVALID"
            ) from error

        for digest_value, status_value, revision_value in rows:
            if (
                type(digest_value) is str
                and digest_value == source_digest
                and status_value == "completed"
                and type(revision_value) is int
                and revision_value >= 1
            ):
                raise MigrationPreflightError(
                    "MIGRATION.SIDECAR_NOT_REUSABLE"
                )
        if any(
            status_value == "completed"
            and type(revision_value) is int
            and revision_value >= 1
            for _digest_value, status_value, revision_value in rows
        ):
            raise MigrationPreflightError(
                "MIGRATION.SIDECAR_DIFFERENT_SOURCE"
            )
        raise MigrationPreflightError("MIGRATION.SIDECAR_NOT_REUSABLE")


def _scan_jsonl(source: Path) -> MigrationPreflight:
    """One bounded streaming pass producing preflight facts and diagnostics."""

    digest = hashlib.sha256()
    valid_count = 0
    invalid_count = 0
    duplicate_source_count = 0
    variant_count = 0
    row_count = 0
    source_counts: dict[str, int] = {}
    diagnostics: list[MigrationDiagnostic] = []

    try:
        with source.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                row_count += 1
                digest.update(raw_line)
                rejection_code, payload = _classify_jsonl_line(raw_line)
                if rejection_code is not None:
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code=rejection_code,
                            stage=_REJECTION_DIAGNOSTICS[rejection_code][0],
                            summary=_REJECTION_DIAGNOSTICS[rejection_code][1],
                        )
                    )
                    continue
                row = cast(dict[str, object], payload)
                source_raw = cast(str, row["source"])
                prior_count = source_counts.get(source_raw, 0)
                source_counts[source_raw] = prior_count + 1
                if prior_count == 1:
                    duplicate_source_count += 1
                if prior_count >= 1:
                    variant_count += 1
                    diagnostics.append(
                        MigrationDiagnostic(
                            code="ROW.DUPLICATE_SOURCE",
                            stage="PREFLIGHT.VALIDATE",
                            line_number=line_number,
                            record_id=None,
                            disposition=DiagnosticDisposition.WARNING,
                            safe_summary="ROW_PRESERVED_AS_VARIANT",
                        )
                    )
                valid_count += 1
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.SOURCE_UNREADABLE"
        ) from error

    if row_count == 0:
        raise MigrationPreflightError("MIGRATION.SOURCE_EMPTY")
    return MigrationPreflight(
        source_digest=digest.hexdigest(),
        valid_count=valid_count,
        invalid_count=invalid_count,
        duplicate_source_count=duplicate_source_count,
        variant_count=variant_count,
        diagnostics=tuple(diagnostics),
    )


def _classify_jsonl_line(
    raw_line: bytes,
) -> tuple[str | None, dict[str, object] | None]:
    """Return (rejection code, payload) with no retained line state."""

    try:
        decoded_line = raw_line.decode("utf-8")
    except UnicodeDecodeError:
        return "ROW.INVALID_UTF8", None
    try:
        payload = json.loads(
            decoded_line,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return "ROW.INVALID_JSON", None
    if type(payload) is not dict:
        return "ROW.INVALID_SHAPE", None
    row = cast(dict[str, object], payload)
    source_raw = row.get("source")
    target_raw = row.get("target")
    if (
        type(source_raw) is not str
        or source_raw == ""
        or type(target_raw) is not str
        or target_raw == ""
    ):
        return "ROW.INVALID_REQUIRED_FIELD", None
    return None, row


def _iter_draft_pairs(
    source: Path,
    observation: _StreamingBuildObservation,
) -> Iterator[tuple[TMRecordDraft, int | None]]:
    """Re-stream the source into a draft pair stream with live counting.

    The store seam groups the pairs into bounded chunks, so no per-record
    state is retained here beyond the running digest and counters.
    """

    try:
        with source.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                observation.digest.update(raw_line)
                rejection_code, payload = _classify_jsonl_line(raw_line)
                if rejection_code is not None:
                    observation.invalid_count += 1
                    continue
                row = cast(dict[str, object], payload)
                source_raw = cast(str, row["source"])
                prior_count = observation.source_counts.get(source_raw, 0)
                observation.source_counts[source_raw] = prior_count + 1
                if prior_count == 1:
                    observation.duplicate_source_count += 1
                if prior_count >= 1:
                    observation.variant_count += 1
                observation.valid_count += 1
                yield (_draft_from_jsonl(row), line_number)
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.SOURCE_UNREADABLE"
        ) from error


def _observation_matches(
    observation: _StreamingBuildObservation,
    preflight: MigrationPreflight,
) -> bool:
    return (
        observation.digest.hexdigest() == preflight.source_digest
        and observation.valid_count == preflight.valid_count
        and observation.invalid_count == preflight.invalid_count
        and observation.duplicate_source_count
        == preflight.duplicate_source_count
        and observation.variant_count == preflight.variant_count
    )


def _draft_from_jsonl(row: dict[str, object]) -> TMRecordDraft:
    source_raw = row["source"]
    target_raw = row["target"]
    if type(source_raw) is not str or type(target_raw) is not str:
        raise MigrationPreflightError("MIGRATION.ROW_VALIDATION_DRIFT")

    def optional_text(field_name: str) -> str | None:
        value = row.get(field_name)
        return value if type(value) is str else None

    return TMRecordDraft(
        source_raw=source_raw,
        target_raw=target_raw,
        speaker_raw=optional_text("speaker"),
        context_prev_raw=optional_text("context_prev"),
        context_next_raw=optional_text("context_next"),
        file_source=optional_text("file_source"),
        provenance=(("source", "legacy-jsonl"),),
    )


def _deterministic_stage_ref(
    identity: CanonicalResourceIdentity,
    *,
    source_digest: str,
    stage_prefix: str,
    path_salt: str | None = None,
) -> MutableStageRef:
    name_key = (
        f"{identity.target_identity[:16]}.{source_digest[:16]}"
        if path_salt is None
        else (
            f"{path_salt}.{identity.target_identity[:16]}."
            f"{source_digest[:16]}"
        )
    )
    parent = identity.canonical_sidecar_path.parent
    return MutableStageRef(
        stage_id=f"stage.{stage_prefix}.{name_key}",
        resource_identity=identity,
        staged_db_path=(
            parent / f".localcat-{stage_prefix}.{name_key}.sqlite3.stage"
        ).absolute(),
        manifest_temp_path=(
            parent / f".localcat-{stage_prefix}.{name_key}.manifest.tmp"
        ).absolute(),
    )


def _snapshot_id_for_origin(
    *,
    batch_id: str,
    batch_kind: str,
    snapshot_prefix: str,
    source_digest: str,
) -> str:
    """Derive the deterministic snapshot receipt id for one origin batch.

    Migration origins keep the historical ``snapshot.migration.<digest>``
    shape; explicit imports derive the id from the fresh import batch
    token so two identical imports never collide and the receipt binds
    the exact origin batch (StageSealer and Gate B re-derive the same
    shape from the single batch row).
    """

    if batch_kind == "import":
        token = batch_id[len("import."):]
        return f"snapshot.import.{token[:24]}"
    return f"{snapshot_prefix}.{source_digest[:24]}"


def _validate_reusable_stage(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    preflight: MigrationPreflight,
    batch_kind: str = "migration",
    batch_prefix: str = "migration",
    snapshot_prefix: str = "snapshot.migration",
    batch_id: str | None = None,
) -> None:
    """Validate a completed mutable stage with bounded streaming checks."""

    try:
        if (
            stage.staged_db_path.is_symlink()
            or stage.manifest_temp_path.is_symlink()
            or not stage.staged_db_path.is_file()
            or not stage.manifest_temp_path.is_file()
        ):
            raise ValueError("stage pair is incomplete")
        try:
            schema = inspect_stage_schema(
                stage,
                canonical_store_id=canonical_store_id,
            )
        except SQLiteStoreSchemaError as error:
            if str(error) == "STORE.STAGE_PUBLISHED":
                raise MigrationPreflightError(
                    "MIGRATION.STAGE_SEALED"
                ) from error
            raise
        connection = sqlite3.connect(
            f"{stage.staged_db_path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            batch_rows = connection.execute(
                "SELECT batch_id, source_digest, source_path, status, "
                "valid_count, invalid_count, duplicate_source_count, "
                "completed_revision FROM tm_origin_batch "
                "WHERE kind = ? ORDER BY batch_id",
                (batch_kind,),
            ).fetchall()
            expected_batch_id = (
                batch_id
                if batch_id is not None
                else f"{batch_prefix}.{preflight.source_digest}"
            )
            expected_batch = (
                expected_batch_id,
                preflight.source_digest,
                str(stage.resource_identity.configured_jsonl_path),
                "completed",
                preflight.valid_count,
                preflight.invalid_count,
                preflight.duplicate_source_count,
                1,
            )
            if batch_rows != [expected_batch]:
                raise ValueError("migration batch does not close")
            if connection.execute(
                "SELECT COUNT(*) FROM tm_record"
            ).fetchone() != (preflight.valid_count,):
                raise ValueError("record count does not close")
            receipt_rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version, destination_jsonl_path, "
                "destination_manifest_path, status "
                "FROM tm_snapshot_receipt ORDER BY snapshot_id"
            ).fetchall()
            expected_snapshot_id = _snapshot_id_for_origin(
                batch_id=expected_batch_id,
                batch_kind=batch_kind,
                snapshot_prefix=snapshot_prefix,
                source_digest=preflight.source_digest,
            )
            if len(receipt_rows) != 1:
                raise ValueError("snapshot receipt does not close")
            receipt_row = receipt_rows[0]
            if (
                receipt_row[0] != expected_snapshot_id
                or receipt_row[1] != stage.resource_identity.resource_id
                or receipt_row[2] != canonical_store_id
                or receipt_row[3] != 1
                or receipt_row[4] != preflight.source_digest
                or receipt_row[5] != preflight.valid_count
                or receipt_row[6] != SNAPSHOT_FORMAT_VERSION
                or receipt_row[7]
                != str(stage.resource_identity.configured_jsonl_path)
                or receipt_row[8]
                != str(stage.resource_identity.snapshot_manifest_path)
                or receipt_row[9] != "issued"
            ):
                raise ValueError("snapshot receipt does not close")
            if connection.execute(
                "SELECT COUNT(*) FROM tm_snapshot_binding"
            ).fetchone() != (0,):
                raise ValueError("unpublished stage must not bind snapshot")

            required_sizes = (1, 2) if schema.fts5_available else (1, 2, 3)
            _validate_stage_indexes(
                connection,
                required_sizes=required_sizes,
                fts5_available=schema.fts5_available,
            )
        finally:
            connection.close()

        decoded = contract_from_json(
            stage.manifest_temp_path.read_text(encoding="utf-8")
        )
        if type(decoded) is not SnapshotManifest:
            raise ValueError("manifest temporary is invalid")
        manifest = decoded
        if (
            manifest.snapshot_kind is not SnapshotKind.MIGRATION_SOURCE
            or manifest.receipt.snapshot_id != expected_snapshot_id
            or manifest.receipt.resource_id
            != stage.resource_identity.resource_id
            or manifest.receipt.canonical_store_id != canonical_store_id
            or manifest.receipt.exported_revision != 1
            or manifest.receipt.jsonl_digest != preflight.source_digest
            or manifest.receipt.record_count != preflight.valid_count
            or manifest.receipt_digest
            != snapshot_receipt_digest(manifest.receipt)
        ):
            raise ValueError("manifest temporary does not close")
    except MigrationPreflightError:
        raise
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise MigrationPreflightError("MIGRATION.STAGE_CONFLICT") from error


def _validate_stage_indexes(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
) -> None:
    """Stream-compare record, gram, and FTS rows with bounded per-record sets."""

    folded_cursor = connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
    )
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    fts_cursor = (
        connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts "
            "ORDER BY record_id"
        )
        if fts5_available
        else None
    )
    current_gram = gram_cursor.fetchone()
    current_fts = fts_cursor.fetchone() if fts_cursor is not None else None
    expected_record_id = 1
    for folded_row in folded_cursor:
        record_id = int(folded_row[0])
        folded_source = str(folded_row[1])
        if record_id != expected_record_id:
            raise ValueError("record ids are not contiguous")
        expected_record_id += 1
        actual_grams: set[tuple[int, str]] = set()
        while current_gram is not None and int(current_gram[0]) == record_id:
            actual_grams.add((int(current_gram[1]), str(current_gram[2])))
            current_gram = gram_cursor.fetchone()
        if current_gram is not None and int(current_gram[0]) < record_id:
            raise ValueError("candidate gram index is out of order")
        expected_grams: set[tuple[int, str]] = set()
        for gram_size in required_sizes:
            expected_grams.update(
                (gram_size, gram)
                for gram in unique_character_ngrams(
                    folded_source,
                    gram_size,
                )
            )
        if actual_grams != expected_grams:
            raise ValueError("candidate gram index is incomplete")
        if fts5_available:
            assert fts_cursor is not None
            actual_fts: set[tuple[int, str]] = set()
            while current_fts is not None and int(current_fts[0]) == record_id:
                actual_fts.add((int(current_fts[0]), str(current_fts[1])))
                current_fts = fts_cursor.fetchone()
            if current_fts is not None and int(current_fts[0]) < record_id:
                raise ValueError("candidate FTS index is out of order")
            if actual_fts != {(record_id, folded_source)}:
                raise ValueError("candidate FTS index is incomplete")
    if current_gram is not None:
        raise ValueError("candidate gram index has extra rows")
    if current_fts is not None:
        raise ValueError("candidate FTS index has extra rows")


def _created_file_identity(path: Path) -> _CreatedFileIdentity:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise MigrationPreflightError("MIGRATION.STAGE_FILE_MISSING") from error
    if not stat.S_ISREG(observed.st_mode):
        raise MigrationPreflightError("MIGRATION.STAGE_FILE_UNSAFE")
    return _CreatedFileIdentity(observed.st_dev, observed.st_ino)


def _write_new_file(path: Path, payload: bytes) -> _CreatedFileIdentity:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.MANIFEST_TEMP_CONFLICT"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("manifest write made no progress")
            view = view[written:]
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise MigrationPreflightError(
                "MIGRATION.MANIFEST_TEMP_UNSAFE"
            )
        return _CreatedFileIdentity(observed.st_dev, observed.st_ino)
    finally:
        os.close(descriptor)


def _remove_created_file(
    path: Path,
    expected: _CreatedFileIdentity,
) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(observed.st_mode)
        and observed.st_dev == expected.device
        and observed.st_ino == expected.inode
    ):
        path.unlink()


def _remove_owned_schema_upgrade_backup(
    path: Path,
    identity: tuple[int, int],
) -> None:
    """Safely unlink one exact owned schema-upgrade recovery backup.

    Only a regular single-link file carrying the captured identity is
    removed; a missing file is already gone and a foreign inode is never
    unlinked but fails closed so the caller can stop instead of leaving an
    unaccounted full DB copy.  The parent directory is fsynced after the
    unlink.
    """

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != identity
    ):
        raise MigrationPreflightError("SCHEMA.BACKUP_CLEANUP_UNSAFE")
    try:
        path.unlink()
        _fsync_schema_upgrade_directory(path.parent)
    except OSError as error:
        raise MigrationPreflightError(
            "SCHEMA.BACKUP_CLEANUP_FAILED"
        ) from error


def _snapshot_resource_identity(
    value: object,
) -> CanonicalResourceIdentity:
    if type(value) is not CanonicalResourceIdentity:
        raise TypeError("resource_identity must be exact CanonicalResourceIdentity")
    if (
        type(value.resource_id) is not str
        or type(value.configured_jsonl_path) is not _NATIVE_PATH_TYPE
        or type(value.canonical_sidecar_path) is not _NATIVE_PATH_TYPE
        or type(value.snapshot_manifest_path) is not _NATIVE_PATH_TYPE
        or type(value.target_identity) is not str
        or type(value.identity_version) is not str
    ):
        raise TypeError("resource_identity contains non-native values")
    private_identity = CanonicalResourceIdentity.from_configured_jsonl(
        value.resource_id,
        Path(str(value.configured_jsonl_path)),
    )
    if private_identity != value:
        raise ValueError("resource_identity is not canonical")
    return private_identity


def _require_target_parent_writable(
    identity: CanonicalResourceIdentity,
) -> None:
    parent = identity.canonical_sidecar_path.parent
    try:
        mode = parent.stat().st_mode
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.TARGET_NOT_WRITABLE"
        ) from error
    writable = mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    searchable = mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not stat.S_ISDIR(mode) or not writable or not searchable:
        raise MigrationPreflightError("MIGRATION.TARGET_NOT_WRITABLE")


def _require_source_readable(source: Path) -> None:
    try:
        mode = source.stat().st_mode
    except OSError as error:
        raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE") from error
    readable = mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    if not stat.S_ISREG(mode) or not readable:
        raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE")


def _try_file_digest(path: Path) -> str | None:
    """Hash one file's exact bytes, or return None when unreadable."""

    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _unchanged_preservation(
    asset_kind: AssetKind,
    digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.VERIFIED_UNCHANGED,
        before_digest=digest,
        observed_digest=digest,
    )


def _changed_preservation(
    asset_kind: AssetKind,
    before_digest: str,
    observed_digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.VERIFIED_CHANGED,
        before_digest=before_digest,
        observed_digest=observed_digest,
    )


def _unverified_preservation(
    asset_kind: AssetKind,
    before_digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.UNVERIFIED,
        before_digest=before_digest,
        observed_digest=None,
    )


def _disambiguation_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    return "IMPORT.FAILED"


def _disambiguation_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _find_recovery_backup(
    sidecar: Path,
    *,
    label: str,
    expected_digest: str,
) -> Path | None:
    """Locate one journal-owned recovery backup by its exact bytes."""

    candidates = sorted(
        sidecar.parent.glob(
            f".{sidecar.name}.localcat-recovery.*.{label}.bak"
        ),
        key=str,
    )
    for candidate in candidates:
        if _try_file_digest(candidate) == expected_digest:
            return candidate
    return None


def _schema_upgrade_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    return "SCHEMA.FAILED"


def _schema_upgrade_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _schema_version_of_store(store_path: Path) -> int:
    """Read the meta schema version of one existing store read-only."""

    connection = sqlite3.connect(
        f"{store_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        rows = connection.execute(
            "SELECT value FROM tm_meta WHERE key = 'schema_version'"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise MigrationPreflightError(
            "SCHEMA.SCHEMA_UNREADABLE"
        ) from error
    finally:
        connection.close()
    if len(rows) != 1:
        raise MigrationPreflightError("SCHEMA.SCHEMA_UNREADABLE")
    try:
        version = int(str(rows[0][0]))
    except (TypeError, ValueError) as error:
        raise MigrationPreflightError(
            "SCHEMA.SCHEMA_UNREADABLE"
        ) from error
    return version


def _upgrade_source_ref(
    identity: CanonicalResourceIdentity,
    store_path: Path,
) -> MutableStageRef:
    """One inspection ref over the active old-schema store path.

    The inspection only reads the database, so the structural manifest
    temporary is a distinct adjacent placeholder (the published manifest
    path is deliberately never reused as a stage temporary).
    """

    return MutableStageRef(
        stage_id="stage.schema-upgrade.source",
        resource_identity=identity,
        staged_db_path=store_path,
        manifest_temp_path=store_path.with_name(
            f"{store_path.name}.localcat-schema-upgrade.inspect.tmp"
        ),
    )


def _open_legacy_read_connection(
    database_path: Path,
) -> sqlite3.Connection:
    """One strictly read-only connection that never mutates the source."""

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _read_active_activation_digest(store_path: Path) -> str:
    """Read the ACTIVE old-schema canonical's activation digest.

    The digest expectation is read from the store's own durable meta
    (never guessed) so the strict ACTIVE inspection binds the exact
    published generation; any missing or malformed digest fails closed.
    """

    connection = _open_legacy_read_connection(store_path)
    try:
        rows = connection.execute(
            "SELECT value FROM tm_meta "
            "WHERE key = 'activation_digest'"
        ).fetchall()
    finally:
        connection.close()
    if (
        len(rows) != 1
        or type(rows[0][0]) is not str
        or len(rows[0][0]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in rows[0][0]
        )
    ):
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    return str(rows[0][0])


def _read_legacy_snapshot_facts(
    store_path: Path,
    *,
    canonical_store_id: str,
) -> tuple[SnapshotReceipt, SnapshotKind]:
    """Read the one old-schema receipt and binding kind the copy re-publishes.

    The receipt fields and the binding's ``snapshot_kind`` are read
    exactly as the v1 ledger recorded them; the binding itself is deleted
    from the candidate and re-published identically by activation, so the
    manifest temporary must carry the original kind.
    """

    connection = _open_legacy_read_connection(store_path)
    try:
        receipt_rows = connection.execute(
            "SELECT snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, format_version "
            "FROM tm_snapshot_receipt ORDER BY snapshot_id"
        ).fetchall()
        binding_rows = connection.execute(
            "SELECT snapshot_kind FROM tm_snapshot_binding "
            "WHERE binding_id = 1"
        ).fetchall()
    finally:
        connection.close()
    if len(receipt_rows) != 1:
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    row = receipt_rows[0]
    scalar_values = row[:3] + (row[4], row[6])
    if any(type(value) is not str for value in scalar_values):
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    if type(row[3]) is not int or type(row[5]) is not int:
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    receipt = SnapshotReceipt(
        snapshot_id=str(row[0]),
        resource_id=str(row[1]),
        canonical_store_id=str(row[2]),
        exported_revision=int(row[3]),
        jsonl_digest=str(row[4]),
        record_count=int(row[5]),
        format_version=str(row[6]),
    )
    if receipt.canonical_store_id != canonical_store_id:
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    if len(binding_rows) != 1 or type(binding_rows[0][0]) is not str:
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED")
    try:
        manifest_kind = SnapshotKind(str(binding_rows[0][0]))
    except (TypeError, ValueError) as error:
        raise MigrationPreflightError("SCHEMA.UPGRADE_UNSUPPORTED") from error
    return receipt, manifest_kind


def _fsync_schema_upgrade_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as error:
        raise MigrationPreflightError(
            "SCHEMA.COPY_FAILED"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_store_into_stage(
    source_path: Path,
    destination_path: Path,
) -> _CreatedFileIdentity:
    """One consistent old-schema copy into a fresh exclusively reserved file.

    The source is opened strictly read-only and copied through the SQLite
    backup API into a same-directory fresh ``O_EXCL`` regular file, then
    fsynced (file and parent).  The copy is a valid reopenable old-schema
    store; its byte digest is deliberately not required to equal the live
    DB's digest (``Connection.backup()`` may normalize page layout).  Any
    failure removes the partial copy and never touches the live canonical.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            destination_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
            0o600,
        )
        created = True
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise MigrationPreflightError("SCHEMA.COPY_UNSAFE")
        identity = _CreatedFileIdentity(observed.st_dev, observed.st_ino)
        source = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        try:
            destination = sqlite3.connect(
                f"{destination_path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
            )
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _fsync_schema_upgrade_directory(destination_path.parent)
        final = os.lstat(destination_path)
        if (
            not stat.S_ISREG(final.st_mode)
            or (final.st_dev, final.st_ino) != (identity.device, identity.inode)
        ):
            raise MigrationPreflightError("SCHEMA.COPY_UNSAFE")
        return identity
    except MigrationPreflightError:
        if created:
            try:
                destination_path.unlink()
            except OSError:
                pass
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        if created:
            try:
                destination_path.unlink()
            except OSError:
                pass
        raise MigrationPreflightError("SCHEMA.COPY_FAILED") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ddl_for(
    statements: tuple[str, ...],
    prefix: str,
) -> str:
    matches = [
        statement
        for statement in statements
        if statement.lstrip().startswith(prefix)
    ]
    if len(matches) != 1:
        raise ValueError("schema DDL statement set is inconsistent")
    return matches[0]


def _migrate_schema_copy(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
) -> None:
    """Migrate one copied pre-v2 stage to the current schema in place.

    The rebuild renames the legacy tables aside, recreates the v2 shape,
    derives each completed batch's ``completed_revision`` strictly from
    the proven record-block/origin-ordinal order (never batch-id
    sorting), copies every record verbatim (including provenance,
    context, usage and lineage), rebuilds ``tm_gram`` and, when
    available, ``tm_fts`` from ``tm_record.source_fold_v1``, re-issues
    the single receipt, removes the binding (re-published identically by
    activation), and advances meta to the current schema version and
    approved digest with the durable schema-upgrade origin marker.  The
    whole rebuild is one transaction, so a failed upgrade leaves only
    the untouched original store and its recovery backup.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        try:
            head_rows = connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'head_revision'"
            ).fetchall()
            if len(head_rows) != 1:
                raise SQLiteStoreSchemaError(
                    "STORE.REVISION_ANCESTRY_MISMATCH"
                )
            head_revision = int(str(head_rows[0][0]))
            record_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()[0]
            )
            completed_blocks = _legacy_completed_origin_blocks(connection)
            _ = _legacy_revision_ancestry(
                connection,
                head_revision=head_revision,
                record_count=record_count,
            )
        except SQLiteStoreSchemaError as error:
            raise MigrationPreflightError(
                "SCHEMA.ANCESTRY_UNPROVABLE"
            ) from error
        except (TypeError, ValueError) as error:
            raise MigrationPreflightError(
                "SCHEMA.ANCESTRY_UNPROVABLE"
            ) from error
        revision_by_batch = {
            batch_id: revision
            for batch_id, revision, _count in completed_blocks
        }
        for statement in (
            "DROP INDEX idx_tm_exact",
            "DROP INDEX idx_tm_context_speaker",
            "DROP INDEX idx_tm_gram_lookup",
        ):
            connection.execute(statement)
        connection.execute(
            "ALTER TABLE tm_origin_batch RENAME TO tm_origin_batch_legacy"
        )
        connection.execute(
            "ALTER TABLE tm_record RENAME TO tm_record_legacy"
        )
        connection.execute(
            "ALTER TABLE tm_gram RENAME TO tm_gram_legacy"
        )
        for statement in (
            _ddl_for(
                _SCHEMA_STATEMENTS,
                "CREATE TABLE tm_origin_batch (",
            ),
            _ddl_for(_SCHEMA_STATEMENTS, "CREATE TABLE tm_record ("),
            _ddl_for(_SCHEMA_STATEMENTS, "CREATE TABLE tm_gram ("),
        ):
            connection.execute(statement)
        legacy_batch_cursor = connection.execute(
            "SELECT batch_id, kind, source_digest, source_path, status, "
            "valid_count, invalid_count, duplicate_source_count, "
            "created_at FROM tm_origin_batch_legacy ORDER BY batch_id"
        )
        for batch in legacy_batch_cursor:
            batch_id = str(batch[0])
            connection.execute(
                "INSERT INTO tm_origin_batch("
                "batch_id, kind, source_digest, source_path, status, "
                "valid_count, invalid_count, duplicate_source_count, "
                "completed_revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    str(batch[1]),
                    batch[2],
                    batch[3],
                    str(batch[4]),
                    int(batch[5]),
                    int(batch[6]),
                    int(batch[7]),
                    revision_by_batch.get(batch_id),
                    str(batch[8]),
                ),
            )
        connection.execute(
            "INSERT INTO tm_record("
            "record_id, source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, usage_count, "
            "last_used, origin_batch_id, origin_ordinal) "
            "SELECT record_id, source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, usage_count, "
            "last_used, origin_batch_id, origin_ordinal "
            "FROM tm_record_legacy ORDER BY record_id"
        )
        required_sizes = (1, 2) if fts5_available else (1, 2, 3)
        gram_rows: list[tuple[int, str, int]] = []
        record_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record "
            "ORDER BY record_id"
        )
        for record_id, folded_source in record_cursor:
            for gram_size in required_sizes:
                for gram in unique_character_ngrams(
                    str(folded_source),
                    gram_size,
                ):
                    gram_rows.append((gram_size, gram, int(record_id)))
                    if len(gram_rows) >= 5000:
                        connection.executemany(
                            "INSERT INTO tm_gram(gram_size, gram, record_id) "
                            "VALUES (?, ?, ?)",
                            gram_rows,
                        )
                        gram_rows.clear()
        if gram_rows:
            connection.executemany(
                "INSERT INTO tm_gram(gram_size, gram, record_id) "
                "VALUES (?, ?, ?)",
                gram_rows,
            )
        for table_name in (
            "tm_gram_legacy",
            "tm_record_legacy",
            "tm_origin_batch_legacy",
        ):
            connection.execute(f"DROP TABLE {table_name}")
        for statement in (
            _ddl_for(
                _SCHEMA_STATEMENTS,
                "CREATE INDEX idx_tm_exact",
            ),
            _ddl_for(
                _SCHEMA_STATEMENTS,
                "CREATE INDEX idx_tm_context_speaker",
            ),
            _ddl_for(
                _SCHEMA_STATEMENTS,
                "CREATE INDEX idx_tm_gram_lookup",
            ),
        ):
            connection.execute(statement)
        if fts5_available:
            connection.execute("DROP TABLE tm_fts")
            connection.execute(_FTS5_STATEMENT)
            connection.execute(
                "INSERT INTO tm_fts(source_fold_v1, record_id) "
                "SELECT source_fold_v1, record_id FROM tm_record "
                "ORDER BY record_id"
            )
        connection.execute(
            "UPDATE tm_snapshot_receipt SET status = 'issued'"
        )
        connection.execute("DELETE FROM tm_snapshot_binding")
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'schema_version'",
            (str(TM_SCHEMA_VERSION),),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'schema_digest'",
            (_APPROVED_SCHEMA_DIGESTS[fts5_available],),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'activation_status'",
            ("UNPUBLISHED",),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'generation'",
            ("0",),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'divergence_latched'",
            ("0",),
        )
        connection.execute(
            "DELETE FROM tm_meta WHERE key = 'activation_digest'"
        )
        connection.execute(
            "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
            (_SCHEMA_UPGRADE_META_KEY, _SCHEMA_UPGRADE_META_VALUE),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _rejected_diagnostic(
    line_number: int,
    *,
    code: str,
    stage: str,
    summary: str,
) -> MigrationDiagnostic:
    return MigrationDiagnostic(
        code=code,
        stage=stage,
        line_number=line_number,
        record_id=None,
        disposition=DiagnosticDisposition.REJECTED,
        safe_summary=summary,
    )


__all__ = [
    "MIGRATION_STREAM_CHUNK_SIZE",
    "MigrationPreflightError",
    "MigrationStageBuild",
    "TMMigrationService",
]
