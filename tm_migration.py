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
    ResourceStoreCoordinator,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
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
        stage = self._build_stage(
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
            stage = self._build_stage(
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
    ) -> MutableStageRef:
        """Build or reuse one complete unpublished stage (shared builder).

        The stage is deterministic per (identity, source digest, prefix)
        and is never the canonical sidecar: reuse is only accepted after
        the full stage closure is re-proven from disk, and any build
        failure removes exactly the created stage files.  Migration
        origins use the deterministic ``migration.<source digest>`` batch
        id; explicit imports pass a fresh collision-resistant
        ``import.<uuid>`` batch id whose token also shapes the snapshot
        receipt id.
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
            return stage

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
        return stage

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
