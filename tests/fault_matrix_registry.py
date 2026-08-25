"""Closed Feature 5 fault-matrix registry used by release validation.

This module is test support, not a runtime capability owner.  Rows point to
existing behavioral tests and production files so the release evidence can
prove that every claimed fault has an executable assertion and fresh source
bytes without making the D/E/F owners depend on validation code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re


FAULT_MATRIX_SCHEMA_VERSION = "tm-fault-matrix-v1"

_ROW_ID = re.compile(r"9\.[12]\.[A-Z_]+\.\d{2}\Z")
_TEST_ID = re.compile(
    r"tests\.test_[a-z0-9_]+\.[A-Za-z0-9_]+\.test_[a-z0-9_]+\Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class FaultClass(str, Enum):
    CORRUPT_INPUT = "CORRUPT_INPUT"
    WRITE_FAULT = "WRITE_FAULT"
    LEASE = "LEASE"
    BUSY = "BUSY"
    TOKEN_REPLAY = "TOKEN_REPLAY"
    PHASE_CRASH = "PHASE_CRASH"
    PRESERVATION = "PRESERVATION"
    EXPORT_CRASH = "EXPORT_CRASH"
    EXTERNAL_CHANGE = "EXTERNAL_CHANGE"
    MISMATCH = "MISMATCH"
    IMPORT_REBUILD = "IMPORT_REBUILD"
    SCHEMA_UPGRADE = "SCHEMA_UPGRADE"
    REFRESH_RECOVERY = "REFRESH_RECOVERY"
    MUTATION_PROOF = "MUTATION_PROOF"
    TERMINAL_REPLAY = "TERMINAL_REPLAY"
    FOREIGN_INODE = "FOREIGN_INODE"


@dataclass(frozen=True)
class FaultMatrixRow:
    """One closed fault claim backed by exact executable unittest ids."""

    row_id: str
    task: str
    fault_class: FaultClass
    fault_row: str
    production_seam: str
    test_ids: tuple[str, ...]
    assertion_contract: str

    def __post_init__(self) -> None:
        if type(self.row_id) is not str or _ROW_ID.fullmatch(self.row_id) is None:
            raise ValueError("fault row id must use the closed 9.1/9.2 format")
        if type(self.task) is not str or self.task not in ("9.1", "9.2"):
            raise ValueError("fault task must be 9.1 or 9.2")
        if not self.row_id.startswith(f"{self.task}."):
            raise ValueError("fault row id must bind its task")
        if type(self.fault_class) is not FaultClass:
            raise TypeError("fault class must be FaultClass")
        if f".{self.fault_class.value}." not in self.row_id:
            raise ValueError("fault row id must bind its fault class")
        for name, value in (
            ("fault row", self.fault_row),
            ("production seam", self.production_seam),
            ("assertion contract", self.assertion_contract),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        source_path = self.production_seam.partition(":")[0]
        if not source_path.endswith(".py") or Path(source_path).is_absolute():
            raise ValueError("production seam must begin with a relative .py path")
        if type(self.test_ids) is not tuple or not self.test_ids:
            raise ValueError("fault row must reference at least one test")
        if len(set(self.test_ids)) != len(self.test_ids):
            raise ValueError("fault row test ids must be unique")
        for test_id in self.test_ids:
            if type(test_id) is not str or _TEST_ID.fullmatch(test_id) is None:
                raise ValueError("fault row contains an invalid unittest id")


@dataclass(frozen=True)
class SnapshotProcessDeathBoundary:
    """One exact shared export/refresh boundary killed by ``os._exit``."""

    boundary_id: str
    seam: str
    ordinal: int
    expected_resolution: str

    def __post_init__(self) -> None:
        if type(self.boundary_id) is not str or not self.boundary_id:
            raise TypeError("process-death boundary id must be a string")
        if self.seam not in {
            "file_fsync",
            "register",
            "handoff",
            "replace",
            "directory_fsync",
            "complete",
            "cleanup_unlink",
            "clear",
        }:
            raise ValueError("process-death boundary seam is invalid")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("process-death boundary ordinal is invalid")
        if self.expected_resolution not in {
            "UNJOURNALED",
            "BLOCKED",
            "CANCELLED",
            "COMPLETED",
            "TERMINAL_NOOP",
        }:
            raise ValueError("process-death resolution is invalid")


SNAPSHOT_PROCESS_DEATH_BOUNDARIES = (
    SnapshotProcessDeathBoundary(
        "JSONL_TEMP_FILE_FSYNC", "file_fsync", 1, "UNJOURNALED"
    ),
    SnapshotProcessDeathBoundary(
        "MANIFEST_TEMP_FILE_FSYNC", "file_fsync", 2, "UNJOURNALED"
    ),
    SnapshotProcessDeathBoundary(
        "ISSUED_COMMIT", "register", 1, "CANCELLED"
    ),
    SnapshotProcessDeathBoundary(
        "JSONL_RECOVERY_FILE_FSYNC", "file_fsync", 3, "BLOCKED"
    ),
    SnapshotProcessDeathBoundary(
        "MANIFEST_RECOVERY_FILE_FSYNC", "file_fsync", 4, "BLOCKED"
    ),
    SnapshotProcessDeathBoundary(
        "RECOVERY_HANDOFF_COMMIT", "handoff", 1, "CANCELLED"
    ),
    SnapshotProcessDeathBoundary(
        "JSONL_REPLACE", "replace", 1, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "JSONL_PARENT_FSYNC", "directory_fsync", 1, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "MANIFEST_REPLACE", "replace", 2, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "MANIFEST_PARENT_FSYNC", "directory_fsync", 2, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "RECEIPT_COMPLETION", "complete", 1, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "MANIFEST_RECOVERY_CLEANUP", "cleanup_unlink", 1, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "JSONL_RECOVERY_CLEANUP", "cleanup_unlink", 2, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "CLEANUP_PARENT_FSYNC", "directory_fsync", 3, "COMPLETED"
    ),
    SnapshotProcessDeathBoundary(
        "HANDOFF_CLEAR_COMMIT", "clear", 1, "TERMINAL_NOOP"
    ),
)


def _row(
    row_id: str,
    fault_class: FaultClass,
    fault_row: str,
    production_seam: str,
    test_ids: tuple[str, ...],
    assertion_contract: str,
) -> FaultMatrixRow:
    return FaultMatrixRow(
        row_id=row_id,
        task=".".join(row_id.split(".")[:2]),
        fault_class=fault_class,
        fault_row=fault_row,
        production_seam=production_seam,
        test_ids=test_ids,
        assertion_contract=assertion_contract,
    )


TASK_9_1_ROWS: tuple[FaultMatrixRow, ...] = (
    _row(
        "9.1.CORRUPT_INPUT.01",
        FaultClass.CORRUPT_INPUT,
        "Malformed or non-UTF-8 JSONL is rejected without source mutation",
        "tm_migration.py:preflight input boundary",
        (
            "tests.test_tm_migration.TMMigrationPreflightTests."
            "test_preflight_rejects_non_utf8_and_non_object_rows_safely",
        ),
        "Invalid input yields safe diagnostics and leaves the original JSONL unchanged.",
    ),
    _row(
        "9.1.CORRUPT_INPUT.02",
        FaultClass.CORRUPT_INPUT,
        "Naked or incomplete staged sidecar never becomes reusable authority",
        "tm_migration.py:mutable stage reuse boundary",
        (
            "tests.test_tm_migration.TMMigrationStageBuildTests."
            "test_naked_sidecar_is_not_authority_and_never_reports_reuse",
            "tests.test_tm_migration.TMMigrationStageBuildTests."
            "test_incomplete_sidecar_schema_is_rejected_as_invalid",
        ),
        "Only a complete receipt/manifest/schema/index set can authorize stage reuse.",
    ),
    _row(
        "9.1.CORRUPT_INPUT.03",
        FaultClass.CORRUPT_INPUT,
        "Corrupt canonical sidecar fails stop and never falls back to legacy",
        "tm_engine.py:cold canonical authority selection",
        (
            "tests.test_tm_engine_compat.CanonicalFailStopTests."
            "test_corrupt_sidecar_fail_stops_never_legacy",
            "tests.test_tm_engine_compat.CanonicalFailStopTests."
            "test_same_bytes_foreign_sidecar_inode_fail_stops",
        ),
        "A previously activated but unprovable canonical store cannot be replaced by JSONL authority.",
    ),
    _row(
        "9.1.CORRUPT_INPUT.04",
        FaultClass.CORRUPT_INPUT,
        "Seal rejects integrity and foreign-key corruption",
        "tm_stage_sealer.py:physical validation",
        (
            "tests.test_tm_stage_sealer.StageSealerPhysicalFailureTests."
            "test_integrity_corruption_rejected",
            "tests.test_tm_stage_sealer.StageSealerPhysicalFailureTests."
            "test_foreign_key_violation_rejected",
        ),
        "A physically inconsistent mutable stage cannot mint a sealed capability.",
    ),
    _row(
        "9.1.CORRUPT_INPUT.05",
        FaultClass.CORRUPT_INPUT,
        "Too-new, incomplete, or semantics-tampered schema is rejected",
        "tm_sqlite_store.py:schema validation",
        (
            "tests.test_tm_sqlite_store.SQLiteSchemaTests."
            "test_schema_rejects_too_new_or_incomplete_metadata",
            "tests.test_tm_sqlite_store.SQLiteSchemaTests."
            "test_schema_rejects_tampered_semantics_and_stage_state",
        ),
        "Opening a store requires the exact approved schema and semantics facts.",
    ),
    _row(
        "9.1.CORRUPT_INPUT.06",
        FaultClass.CORRUPT_INPUT,
        "Sealed and active attestations reject codec, byte, inode, and semantic drift",
        "tm_content_attestation.py:sealed and active content proof boundary",
        (
            "tests.test_tm_content_attestation.ContentAttestationCodecTests."
            "test_sealed_and_active_codecs_are_exact_and_digest_closed",
            "tests.test_tm_content_attestation.ContentFileProofTests."
            "test_capture_binds_exact_bytes_and_inode",
            "tests.test_tm_activation_recovery.ActivationRecoveryFailStopTests."
            "test_terminal_active_attestation_rejects_bytes_and_inode_drift",
            "tests.test_tm_activation_publication.ActivationPublicationFailureTests."
            "test_semantic_mutation_after_receipt_owner_never_self_attests",
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_second_fresh_coordinator_rehydrates_after_terminal_completion",
        ),
        "Only exact, digest-closed content identities and semantic facts can be reused; drift fails before health or generation publication.",
    ),
    _row(
        "9.1.WRITE_FAULT.01",
        FaultClass.WRITE_FAULT,
        "Origin or record write failure leaves no partial batch",
        "tm_sqlite_store.py:append transaction",
        (
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_origin_and_record_stage_failures_leave_no_partial_batch",
        ),
        "Origin, records, indexes, and completion facts commit or roll back together.",
    ),
    _row(
        "9.1.WRITE_FAULT.02",
        FaultClass.WRITE_FAULT,
        "Candidate index failure rolls back records and revision",
        "tm_sqlite_store.py:candidate extension transaction",
        (
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_candidate_sql_failure_rolls_back_entire_batch",
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_extension_cannot_commit_partial_batch_before_failure",
        ),
        "Candidate SQL cannot commit independently from canonical record and revision facts.",
    ),
    _row(
        "9.1.WRITE_FAULT.03",
        FaultClass.WRITE_FAULT,
        "Commit failure rolls back origin, record, and candidate rows",
        "tm_sqlite_store.py:commit boundary",
        (
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_commit_failure_rolls_back_origin_record_and_candidate_rows",
        ),
        "A failed SQLite commit exposes none of the candidate batch.",
    ),
    _row(
        "9.1.WRITE_FAULT.04",
        FaultClass.WRITE_FAULT,
        "Mid-stream migration failure discards the partial stage",
        "tm_migration.py:streaming stage build",
        (
            "tests.test_tm_migration.TMMigrationStageBuildTests."
            "test_stream_failure_mid_build_discards_partial_stage",
            "tests.test_tm_migration.TMMigrationStageBuildTests."
            "test_digest_change_or_write_failure_leaves_no_stage_artifacts",
        ),
        "A partial stream never becomes a reusable or sealable stage.",
    ),
    _row(
        "9.1.WRITE_FAULT.05",
        FaultClass.WRITE_FAULT,
        "Stage fsync failure fails closed and is retryable",
        "tm_stage_sealer.py:durable seal boundary",
        (
            "tests.test_tm_stage_sealer.StageSealerPhysicalFailureTests."
            "test_fsync_failure_fails_closed",
            "tests.test_tm_stage_sealer.StageSealerPhysicalFailureTests."
            "test_one_shot_fsync_failure_is_deterministically_retryable",
        ),
        "The registry is not committed before stage and parent durability are proven.",
    ),
    _row(
        "9.1.WRITE_FAULT.06",
        FaultClass.WRITE_FAULT,
        "Journal short-write, replace, and directory-fsync failures preserve replay",
        "tm_activation_journal.py:durable journal publication",
        (
            "tests.test_tm_activation_journal.ActivationJournalFaultInjectionTests."
            "test_short_write_no_progress_cleans_temp_and_retries",
            "tests.test_tm_activation_journal.ActivationJournalFaultInjectionTests."
            "test_replace_failure_cleans_temp_and_retries",
            "tests.test_tm_activation_journal.ActivationJournalFaultInjectionTests."
            "test_directory_fsync_failure_after_publish_fail_stops_with_replay",
        ),
        "Journal phase is claimed only after durable publication or recoverable fail-stop evidence.",
    ),
    _row(
        "9.1.WRITE_FAULT.07",
        FaultClass.WRITE_FAULT,
        "Activation replace and parent-fsync faults never advance visibility",
        "tm_activation_recovery.py:publication phase boundaries",
        (
            "tests.test_tm_activation_publication.ActivationPublicationFailureTests."
            "test_database_parent_fsync_failure_does_not_advance_or_publish_view",
            "tests.test_tm_activation_publication.ActivationPublicationFailureTests."
            "test_manifest_parent_fsync_failure_does_not_claim_manifest_phase",
        ),
        "DB, manifest, and generation phases advance only after their own durable effects.",
    ),
    _row(
        "9.1.LEASE.01",
        FaultClass.LEASE,
        "Concurrent prepare has one winner and no token ABA",
        "tm_sqlite_store.py:activation preparation guard",
        (
            "tests.test_tm_activation.ActivationDrainAndFailureTests."
            "test_concurrent_prepare_is_rejected_without_second_token_or_aba",
        ),
        "One coordinator owns one activation preparation at a time.",
    ),
    _row(
        "9.1.LEASE.02",
        FaultClass.LEASE,
        "Drain blocks new leases until the old view exits",
        "tm_sqlite_store.py:generation drain",
        (
            "tests.test_tm_activation.ActivationDrainAndFailureTests."
            "test_held_lease_drains_then_prepares_and_rejects_new_lease",
            "tests.test_tm_sqlite_store.SQLiteTMQueryViewTests."
            "test_query_lease_blocks_generation_publication_until_exit",
        ),
        "Readers observe a complete old or new generation, never a transitional mix.",
    ),
    _row(
        "9.1.LEASE.03",
        FaultClass.LEASE,
        "Drain timeout restores READY and the prior generation",
        "tm_sqlite_store.py:bounded drain timeout",
        (
            "tests.test_tm_activation.ActivationDrainAndFailureTests."
            "test_drain_timeout_restores_ready_and_cancels_token",
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_drain_timeout_keeps_prior_generation_and_recovers_ready_state",
        ),
        "A timed-out drain publishes nothing and cancels only the pending token.",
    ),
    _row(
        "9.1.BUSY.01",
        FaultClass.BUSY,
        "SQLite busy timeout is resource-local and retryable",
        "tm_sqlite_store.py:busy error normalization",
        (
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_busy_timeout_is_resource_local_and_retryable",
        ),
        "One locked resource reports STORE.BUSY_TIMEOUT without poisoning another resource.",
    ),
    _row(
        "9.1.TOKEN_REPLAY.01",
        FaultClass.TOKEN_REPLAY,
        "Activation token is exact single-use and terminal",
        "tm_stage_sealer.py:sealed-stage registry",
        (
            "tests.test_tm_stage_sealer.StageSealerRegistryTests."
            "test_token_lifecycle_is_exact_single_use_and_terminal",
        ),
        "A consumed or cancelled sealed token can never authorize another activation.",
    ),
    _row(
        "9.1.TOKEN_REPLAY.02",
        FaultClass.TOKEN_REPLAY,
        "Activation nonce replay is rejected across registry artifacts",
        "tm_stage_sealer.py:activation nonce registry",
        (
            "tests.test_tm_stage_sealer.StageSealerRegistryTests."
            "test_activation_nonce_replay_is_global_across_registry_artifacts",
        ),
        "A nonce identifies one activation attempt globally within the registry.",
    ),
    _row(
        "9.1.TOKEN_REPLAY.03",
        FaultClass.TOKEN_REPLAY,
        "Cancelled or consumed token cannot publish or advance journal",
        "tm_activation_journal.py:phase authorization",
        (
            "tests.test_tm_activation_journal.ActivationJournalPhaseBoundaryTests."
            "test_cancelled_or_consumed_token_denies_publish_and_advance",
        ),
        "Journal transitions require the same live token and preparation capability.",
    ),
    _row(
        "9.1.TOKEN_REPLAY.04",
        FaultClass.TOKEN_REPLAY,
        "Terminal generation replay is idempotent",
        "tm_activation_recovery.py:terminal replay",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_generation_published_replay_is_idempotent_completed",
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_existing_canonical_generation_replay_keeps_generation",
        ),
        "Replaying a completed token confirms the same generation without a second publication.",
    ),
    _row(
        "9.1.PHASE_CRASH.01",
        FaultClass.PHASE_CRASH,
        "PREPARED crash cancels first activation or restores prior authority",
        "tm_activation_recovery.py:PREPARED recovery",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryCancelTests."
            "test_first_activation_prepared_cancel_restores_no_view",
            "tests.test_tm_activation_recovery.ActivationRecoveryCancelTests."
            "test_existing_canonical_prepared_cancel_restores_prior_generation",
        ),
        "PREPARED recovery leaves legacy first-use or the complete prior canonical generation.",
    ),
    _row(
        "9.1.PHASE_CRASH.02",
        FaultClass.PHASE_CRASH,
        "DB_REPLACED crash completes the token or restores the prior pair",
        "tm_activation_recovery.py:DB_REPLACED recovery",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_first_activation_db_replaced_recovery_publishes_one_generation",
            "tests.test_tm_activation_rollback.RollbackExistingCanonicalTests."
            "test_db_replaced_phase_restores_prior_pair_from_backups",
        ),
        "Recovery never exposes the candidate DB with a prior manifest or binding.",
    ),
    _row(
        "9.1.PHASE_CRASH.03",
        FaultClass.PHASE_CRASH,
        "MANIFEST_PUBLISHED crash completes or restores the prior pair",
        "tm_activation_recovery.py:MANIFEST_PUBLISHED recovery",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_manifest_published_recovery_publishes_unique_generation",
            "tests.test_tm_activation_rollback.RollbackExistingCanonicalTests."
            "test_manifest_published_phase_restores_prior_pair_from_backups",
        ),
        "A published manifest becomes visible only with its matching DB, binding, and generation.",
    ),
    _row(
        "9.1.PHASE_CRASH.04",
        FaultClass.PHASE_CRASH,
        "GENERATION_PUBLISHED crash replays one completed generation",
        "tm_activation_recovery.py:GENERATION_PUBLISHED recovery",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryCompletionTests."
            "test_generation_published_replay_is_idempotent_completed",
            "tests.test_tm_activation_recovery.ActivationRecoveryTerminalProtocolTests."
            "test_live_published_terminal_replayed_by_fresh_coordinators",
        ),
        "Terminal recovery confirms the already durable generation and never republishes it.",
    ),
    _row(
        "9.1.PHASE_CRASH.05",
        FaultClass.PHASE_CRASH,
        "Journal advance failure preserves the truthful durable phase",
        "tm_activation_recovery.py:phase truth reconciliation",
        (
            "tests.test_tm_activation_recovery.ActivationRecoveryPhaseTruthfulnessTests."
            "test_advance_failure_before_manifest_journal_keeps_db_replaced",
            "tests.test_tm_activation_recovery.ActivationRecoveryPhaseTruthfulnessTests."
            "test_advance_failure_before_generation_journal_keeps_manifest_published",
        ),
        "Recovery resumes from observed durable effects instead of a caller-reported phase.",
    ),
    _row(
        "9.1.PHASE_CRASH.06",
        FaultClass.PHASE_CRASH,
        "Rollback quarantine, restore, terminal, and cleanup crashes resume",
        "tm_activation_recovery.py:rollback terminal protocol",
        (
            "tests.test_tm_activation_rollback.RollbackCrashResumeTests."
            "test_quarantine_fsync_crash_resumes_idempotently",
            "tests.test_tm_activation_rollback.RollbackCrashResumeTests."
            "test_restore_fsync_crash_resumes_idempotently",
            "tests.test_tm_activation_rollback.RollbackCrashResumeTests."
            "test_terminal_write_crash_resumes_idempotently",
        ),
        "Each rollback death boundary retains enough durable evidence for an idempotent replay.",
    ),
    _row(
        "9.1.PRESERVATION.01",
        FaultClass.PRESERVATION,
        "Failure preserves the original JSONL bytes and canonical authority",
        "tm_engine.py:diverged canonical facade",
        (
            "tests.test_tm_engine_compat.SourceDivergedTests."
            "test_diverged_canonical_query_and_save_continue_and_keep_jsonl",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportValidationTests."
            "test_validation_failures_never_mutate",
        ),
        "A failed migration or disambiguation does not rewrite source JSONL or demote canonical SQLite.",
    ),
    _row(
        "9.1.PRESERVATION.02",
        FaultClass.PRESERVATION,
        "Last-known-good DB, manifest, and binding restore as one set",
        "tm_activation_recovery.py:existing-canonical rollback",
        (
            "tests.test_tm_activation_rollback.RollbackExistingCanonicalTests."
            "test_prepared_phase_restores_prior_pair_from_backups",
            "tests.test_tm_activation_rollback.RollbackExistingCanonicalTests."
            "test_manifest_published_phase_restores_prior_pair_from_backups",
            "tests.test_tm_activation_recovery.ActivationRecoveryFailStopTests."
            "test_manifest_tamper_rolls_back_before_receipt_mutation",
        ),
        "Rollback cannot restore only one member of the prior authority set.",
    ),
    _row(
        "9.1.PRESERVATION.03",
        FaultClass.PRESERVATION,
        "No partial generation is visible during activation or recovery",
        "tm_sqlite_store.py:generation publication",
        (
            "tests.test_tm_activation_publication.ActivationPublicationVisibilityTests."
            "test_leases_are_blocked_until_manifest_and_generation_are_complete",
            "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
            "test_generation_switch_exposes_only_complete_old_or_new_version",
            "tests.test_tm_activation_recovery.ActivationRecoveryGateTests."
            "test_no_premature_visibility_before_recovery",
        ),
        "Every operation observes one complete pre- or post-activation generation.",
    ),
)


TASK_9_2_ROWS: tuple[FaultMatrixRow, ...] = (
    _row(
        "9.2.EXPORT_CRASH.01",
        FaultClass.EXPORT_CRASH,
        "Crash after issued receipt cancels against the intact old pair",
        "tm_snapshot_recovery.py:issued receipt cancellation",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_old_pair_plus_issued_cancels_receipt",
        ),
        "An issued receipt with the untouched old pair cancels without changing binding or canonical state.",
    ),
    _row(
        "9.2.EXPORT_CRASH.02",
        FaultClass.EXPORT_CRASH,
        "Crash after JSONL replace reconstructs and publishes its manifest",
        "tm_snapshot_recovery.py:JSONL-only recovery",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_crash_after_jsonl_replace_reconstructs_on_recovery",
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_process_death_after_reconstruction_temp_fsync_replays_to_completion",
        ),
        "Recovery completes the same snapshot from durable JSONL and handoff facts without a second export.",
    ),
    _row(
        "9.2.EXPORT_CRASH.03",
        FaultClass.EXPORT_CRASH,
        "Crash after manifest replace completes the issued receipt",
        "tm_snapshot_recovery.py:manifest-published recovery",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_crash_after_manifest_replace_completes_on_recovery",
        ),
        "A strictly matching published pair completes exactly its issued receipt.",
    ),
    _row(
        "9.2.EXPORT_CRASH.04",
        FaultClass.EXPORT_CRASH,
        "Crash after completion commit fails closed then finishes cleanup",
        "tm_snapshot_recovery.py:post-completion replay",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_crash_after_complete_commit_fails_closed_then_recovery_finishes",
        ),
        "A committed receipt is not reported complete until its durable artifact cleanup is reconciled.",
    ),
    _row(
        "9.2.EXPORT_CRASH.05",
        FaultClass.EXPORT_CRASH,
        "Arbitrary-path export recovery never changes configured binding",
        "tm_snapshot_recovery.py:export receipt reconciliation",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryExportTests."
            "test_export_full_pair_completes_without_touching_binding",
            "tests.test_tm_export.TMExportDivergenceTests."
            "test_export_leaves_divergence_latch_and_configured_pair_unchanged",
            "tests.test_tm_export.TMExportDivergenceTests."
            "test_damaged_export_destination_never_affects_configured_binding",
        ),
        "An export destination is never promoted into configured source authority.",
    ),
    _row(
        "9.2.EXPORT_CRASH.06",
        FaultClass.EXPORT_CRASH,
        "Export and recovery pin a revision without mutating canonical state",
        "tm_migration.py:export read snapshot",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryCanonicalInvariantTests."
            "test_recovery_never_changes_canonical_state",
            "tests.test_tm_export.TMExportSnapshotIsolationTests."
            "test_export_pins_one_stable_revision_under_concurrent_append",
        ),
        "Concurrent canonical writes produce a coherent exported revision and recovery never rolls canonical back.",
    ),
    _row(
        "9.2.EXPORT_CRASH.07",
        FaultClass.EXPORT_CRASH,
        "Real process death closes every shared fsync, replace, completion, and cleanup boundary",
        "tm_migration.py:shared export/refresh durable publication protocol",
        (
            "tests.test_tm_snapshot_recovery.TMProcessDeathBoundaryTests."
            "test_export_and_refresh_process_death_boundary_catalog",
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_failure_injection_restores_prior_pair_and_cancels_ledger",
            "tests.test_tm_export.TMExportHandoffDurabilityTests."
            "test_cleanup_fsync_failure_before_handoff_clear_returns_pending",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_terminal_replay_fsync_failure_keeps_handoff_then_replays",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_export_release_fsync_failure_blocks_then_retries",
        ),
        "Each boundary is killed by os._exit for both publication modes; replay either preserves unowned evidence, blocks on unprovable ownership, cancels the intact old pair, or converges on one completed pair.",
    ),
    _row(
        "9.2.EXTERNAL_CHANGE.01",
        FaultClass.EXTERNAL_CHANGE,
        "External JSONL or manifest change latches divergence",
        "tm_sqlite_store.py:source binding observation",
        (
            "tests.test_tm_source_binding.SourceBindingMonitorTests."
            "test_external_jsonl_or_manifest_change_latches_divergence",
        ),
        "An external configured-pair change becomes SOURCE_DIVERGED and is never silently imported.",
    ),
    _row(
        "9.2.EXTERNAL_CHANGE.02",
        FaultClass.EXTERNAL_CHANGE,
        "Ordinary canonical write creates history and cannot clear divergence",
        "tm_sqlite_store.py:canonical revision and divergence latch",
        (
            "tests.test_tm_source_binding.SourceBindingMonitorTests."
            "test_completed_binding_is_current_then_append_makes_history_only",
            "tests.test_tm_source_binding.SourceBindingMonitorTests."
            "test_diverged_store_remains_canonical_and_append_cannot_clear_latch",
            "tests.test_tm_source_binding.SourceBindingMonitorTests."
            "test_concurrent_append_during_observation_never_latches_divergence",
        ),
        "A local canonical append is VERIFIED_HISTORY, while an existing divergence remains latched.",
    ),
    _row(
        "9.2.MISMATCH.01",
        FaultClass.MISMATCH,
        "Receipt, binding, ledger, or ancestry mismatch diverges",
        "tm_snapshot_recovery.py:configured receipt adjudication",
        (
            "tests.test_tm_source_binding.SourceBindingMonitorTests."
            "test_ledger_identity_digest_and_ancestry_mismatch_diverge",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_ancestry_invalid_receipt_latches_divergence",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_binding_tamper_never_cancels_old_pair",
        ),
        "Recovery cannot complete or cancel from mismatched authority facts.",
    ),
    _row(
        "9.2.MISMATCH.02",
        FaultClass.MISMATCH,
        "Foreign, missing, symlinked, hardlinked, or directory pair diverges",
        "tm_snapshot_recovery.py:configured pair proof",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_foreign_manifest_latches_divergence",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_missing_pair_latches_divergence",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_symlink_pair_latches_divergence",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_hardlink_pair_latches_divergence",
            "tests.test_tm_snapshot_recovery.TMRecoveryConfiguredDecisionTests."
            "test_directory_manifest_latches_divergence",
        ),
        "Only regular single-link files with the issued identities can form a configured pair.",
    ),
    _row(
        "9.2.MISMATCH.03",
        FaultClass.MISMATCH,
        "Export ledger enforces ancestry and immutable completion history",
        "tm_sqlite_store.py:export receipt ledger",
        (
            "tests.test_tm_export.TMExportLedgerTests."
            "test_receipt_revision_ancestry_enforced",
            "tests.test_tm_export.TMExportLedgerTests."
            "test_completed_export_history_is_immutable",
            "tests.test_tm_export.TMExportLedgerTests."
            "test_stale_and_generation_transitions_rejected",
        ),
        "A completed export receipt cannot be rebound to another revision, path, or generation.",
    ),
    _row(
        "9.2.MISMATCH.04",
        FaultClass.MISMATCH,
        "Corrupt, missing, or orphaned durable handoff relations block without mutation",
        "tm_sqlite_store.py:receipt-to-handoff relation closure",
        (
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_corrupt_issued_and_terminal_handoffs_block_without_mutation",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_issued_deleted_or_orphaned_handoff_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_configured_issued_missing_handoff_and_all_artifacts_absent_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_diverged_configured_issued_missing_handoff_absent_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_diverged_configured_issued_missing_handoff_present_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_export_issued_missing_handoff_and_all_artifacts_absent_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_completed_deleted_or_orphaned_handoff_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_cancelled_deleted_or_orphaned_handoff_blocks",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_terminal_without_handoff_and_artifacts_is_legitimate_noop",
        ),
        "Malformed values, orphan rows, and missing issued or cleanup-pending relations are explicit fail-stop evidence; only a terminal receipt with an absent artifact family may remain post-clear NOOP.",
    ),
    _row(
        "9.2.IMPORT_REBUILD.01",
        FaultClass.IMPORT_REBUILD,
        "Explicit import and rebuild publish fresh canonical generations",
        "tm_migration.py:explicit import/rebuild",
        (
            "tests.test_tm_explicit_import_rebuild.ExplicitImportRebuildSuccessTests."
            "test_import_and_rebuild_replace_the_active_canonical",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportRebuildSuccessTests."
            "test_identical_snapshot_import_twice_succeeds_with_new_ids",
        ),
        "Each verified explicit disambiguation uses the normal sealed activation path and a fresh store identity.",
    ),
    _row(
        "9.2.IMPORT_REBUILD.02",
        FaultClass.IMPORT_REBUILD,
        "Import validation, seal, and publication failure preserve three assets",
        "tm_migration.py:explicit import failure reconciliation",
        (
            "tests.test_tm_explicit_import_rebuild.ExplicitImportValidationTests."
            "test_validation_failures_never_mutate",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportValidationTests."
            "test_seal_failure_removes_exactly_the_fresh_stage_pair",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportPublicationFailureTests."
            "test_db_replace_failure_auto_restores_ready_old_service",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportPublicationFailureTests."
            "test_manifest_publish_failure_auto_restores_ready_old_service",
        ),
        "Failed disambiguation preserves configured JSONL, active canonical, and matching manifest/binding.",
    ),
    _row(
        "9.2.IMPORT_REBUILD.03",
        FaultClass.IMPORT_REBUILD,
        "Only successful verified activation clears divergence",
        "tm_migration.py:divergence disambiguation",
        (
            "tests.test_tm_explicit_import_rebuild.ExplicitImportManifestDivergenceTests."
            "test_missing_prior_manifest_import_succeeds",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportManifestDivergenceTests."
            "test_missing_prior_manifest_failure_preserves_absence",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportManifestDivergenceTests."
            "test_externally_altered_manifest_failure_preserves_bytes",
            "tests.test_tm_explicit_import_rebuild.ExplicitImportManifestDivergenceTests."
            "test_foreign_manifest_entries_fail_closed",
        ),
        "Divergence clears only after a complete explicit import/rebuild publishes one new authority set.",
    ),
    _row(
        "9.2.SCHEMA_UPGRADE.01",
        FaultClass.SCHEMA_UPGRADE,
        "Upgrade copy, seal, journal, or publication failure keeps old READY schema",
        "tm_schema_upgrade.py:upgrade failure reconciliation",
        (
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailureReconciliationTests."
            "test_copy_failure_removes_owned_backup_and_keeps_old_schema",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailureReconciliationTests."
            "test_seal_failure_keeps_old_schema_and_ready_service",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailureReconciliationTests."
            "test_journal_write_failure_cancels_and_restores_ready",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailureReconciliationTests."
            "test_db_replace_failure_auto_restores_ready_old_service",
        ),
        "Before durable completion every upgrade failure restores the prior schema and service authority.",
    ),
    _row(
        "9.2.SCHEMA_UPGRADE.02",
        FaultClass.SCHEMA_UPGRADE,
        "Upgrade crash-window replay completes the exact durable candidate",
        "tm_schema_upgrade.py:upgrade backup locator replay",
        (
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailureReconciliationTests."
            "test_crash_window_recovery_completes_durable_upgrade",
            "tests.test_tm_schema_upgrade.SchemaUpgradeRecoveryLocatorStrictnessTests."
            "test_crash_after_publish_cold_recovery_promotes_exactly_one_backup",
        ),
        "Cold recovery promotes only the identity-bound reported upgrade backup and completes one generation.",
    ),
    _row(
        "9.2.SCHEMA_UPGRADE.03",
        FaultClass.SCHEMA_UPGRADE,
        "Upgrade rejects tampered, missing, symlinked, or multilink authority",
        "tm_schema_upgrade.py:old-store and manifest proof",
        (
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailClosedTests."
            "test_tampered_old_store_fails_closed",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailClosedTests."
            "test_missing_manifest_fails_closed_and_is_never_repaired",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailClosedTests."
            "test_symlink_manifest_fails_closed_and_is_never_repaired",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailClosedTests."
            "test_multilink_manifest_fails_closed_and_is_never_repaired",
            "tests.test_tm_schema_upgrade.SchemaUpgradeFailClosedTests."
            "test_mismatched_source_digest_fails_closed_and_is_never_repaired",
        ),
        "Schema upgrade cannot repair or overwrite an authority set it cannot prove.",
    ),
    _row(
        "9.2.REFRESH_RECOVERY.01",
        FaultClass.REFRESH_RECOVERY,
        "Refresh recovery survives copy, handoff, and temp-fsync crash windows",
        "tm_snapshot_recovery.py:configured refresh crash replay",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_crash_after_recovery_copies_cancels_owned",
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_crash_before_recovery_handoff_preserves_copies",
            "tests.test_tm_snapshot_recovery.TMRecoveryCrashBoundaryTests."
            "test_observe_recovers_before_misclassifying_divergence",
            "tests.test_tm_snapshot_refresh.TMRefreshFailureInjectionTests."
            "test_failure_injection_restores_pair_and_cancels_ledger",
        ),
        "Issued refresh recovery completes, cancels, or diverges from durable artifact facts before observation.",
    ),
    _row(
        "9.2.REFRESH_RECOVERY.02",
        FaultClass.REFRESH_RECOVERY,
        "Refresh publishes one complete pair and VERIFIED_CURRENT binding",
        "tm_migration.py:configured snapshot refresh",
        (
            "tests.test_tm_snapshot_refresh.TMRefreshSuccessTests."
            "test_refresh_publishes_complete_pair_and_reports_verified_current",
            "tests.test_tm_snapshot_refresh.TMRefreshSuccessTests."
            "test_refresh_ledger_and_binding_share_same_completed_receipt",
            "tests.test_tm_snapshot_refresh.TMRefreshSuccessTests."
            "test_refresh_publication_order_jsonl_before_manifest",
            "tests.test_tm_snapshot_refresh.TMRefreshSuccessTests."
            "test_refresh_pins_stable_snapshot_under_concurrent_append",
        ),
        "JSONL, manifest, receipt, and binding close over one revision without changing generation.",
    ),
    _row(
        "9.2.REFRESH_RECOVERY.03",
        FaultClass.REFRESH_RECOVERY,
        "Refresh while diverged has zero side effects",
        "tm_migration.py:refresh divergence gate",
        (
            "tests.test_tm_snapshot_refresh.TMRefreshDivergenceTests."
            "test_refresh_rejected_when_diverged_with_zero_side_effects",
        ),
        "A latched divergence requires explicit import/rebuild and cannot be cleared by refresh.",
    ),
    _row(
        "9.2.MUTATION_PROOF.01",
        FaultClass.MUTATION_PROOF,
        "Ancestor symlink and direct-parent rename or ABA block mutation",
        "tm_snapshot_artifacts.py:root-to-parent descriptor binding",
        (
            "tests.test_tm_export.TMExportPreflightTests."
            "test_export_bind_ancestor_symlink_swap_fails_closed",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_store_clear_parent_renamed_with_foreign_temp_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_export_reconcile_parent_renamed_with_owned_temp_moved_blocks",
        ),
        "A path string cannot authorize mutation after any bound ancestor or direct parent changes identity.",
    ),
    _row(
        "9.2.MUTATION_PROOF.02",
        FaultClass.MUTATION_PROOF,
        "Parent replacement or symlink after blocker or fsync fails closed",
        "tm_snapshot_recovery.py:terminal parent revalidation",
        (
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_terminal_replay_parent_replaced_after_blocker_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_terminal_replay_parent_replaced_between_fsync_and_clear_blocks",
        ),
        "Terminal cleanup keeps handoff evidence when the bound parent changes before release.",
    ),
    _row(
        "9.2.MUTATION_PROOF.03",
        FaultClass.MUTATION_PROOF,
        "Source inode swap at the pre-mutation seam fails closed",
        "tm_snapshot_artifacts.py:source proof before replace",
        (
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_source_swap_at_pre_mutation_seam_fails_closed",
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_hostile_swap_during_restore_fails_closed_with_locator",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_reconstructed_manifest_source_different_byte_swap_blocks",
        ),
        "Only the exact handed-off source inode may be moved, restored, or cleaned.",
    ),
    _row(
        "9.2.MUTATION_PROOF.04",
        FaultClass.MUTATION_PROOF,
        "Destination inode swap at the pre-mutation seam is never overwritten",
        "tm_snapshot_artifacts.py:destination proof before replace",
        (
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_destination_swap_at_pre_mutation_seam_fails_closed",
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_foreign_destination_created_at_replace_is_not_overwritten",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_reconstructed_manifest_destination_swap_blocks",
        ),
        "A foreign final created after the last absence or identity proof survives unchanged.",
    ),
    _row(
        "9.2.MUTATION_PROOF.05",
        FaultClass.MUTATION_PROOF,
        "Same-byte foreign inode swap after proof is rejected",
        "tm_snapshot_artifacts.py:terminal identity proof",
        (
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_same_bytes_swap_before_identity_proof_is_not_overwritten",
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_same_byte_foreign_swap_at_completion_seam_fails_closed",
            "tests.test_tm_snapshot_refresh.TMRefreshHostileSwapTests."
            "test_same_bytes_foreign_swap_at_identity_proof_fails_closed",
            "tests.test_tm_stage_sealer.StageSealerIdentitySwapTests."
            "test_byte_identical_db_inode_swap_before_registration_denied",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_reconstructed_manifest_source_same_byte_swap_blocks",
        ),
        "Matching bytes never substitute for durable inode ownership and a fresh terminal proof.",
    ),
    _row(
        "9.2.MUTATION_PROOF.06",
        FaultClass.MUTATION_PROOF,
        "Different-byte or missing handed-off temp/recovery blocks replay",
        "tm_snapshot_recovery.py:durable handoff artifact proof",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_terminal_replay_different_byte_foreign_temp_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_terminal_replay_different_byte_foreign_recovery_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_missing_handed_off_manifest_temp_blocks_closed",
        ),
        "Missing or content-drifted durable handoff members remain for manual recovery and are never fabricated.",
    ),
    _row(
        "9.2.MUTATION_PROOF.07",
        FaultClass.MUTATION_PROOF,
        "Symlink, hardlink, multilink, dotdot, and authority aliases are rejected",
        "tm_snapshot_artifacts.py:namespace safety",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryExportTests."
            "test_export_destination_parent_symlink_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMRecoveryExportTests."
            "test_export_destination_dotdot_blocks_closed",
            "tests.test_tm_snapshot_recovery.TMRecoveryExportTests."
            "test_export_destination_authority_alias_blocks_closed",
            "tests.test_tm_activation_cluster_d_corrections.ActivationSingleLinkClosureTests."
            "test_hardlinked_source_denies_first_activation",
        ),
        "Snapshot and activation mutations require direct regular single-link files outside authority aliases.",
    ),
    _row(
        "9.2.TERMINAL_REPLAY.01",
        FaultClass.TERMINAL_REPLAY,
        "Terminal replay is idempotent across fresh stores and cleanup retries",
        "tm_snapshot_recovery.py:terminal handoff replay",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryIdempotencyTests."
            "test_repeated_recovery_is_idempotent",
            "tests.test_tm_snapshot_recovery.TMRecoveryIdempotencyTests."
            "test_fresh_store_replay_reaches_same_durable_state",
            "tests.test_tm_snapshot_recovery.TMClusterFRegressionTests."
            "test_terminal_handoff_replay_cleans_and_releases",
        ),
        "Repeated replay converges on the same receipt, binding, pair, and released handoff state.",
    ),
    _row(
        "9.2.FOREIGN_INODE.01",
        FaultClass.FOREIGN_INODE,
        "Foreign temp, recovery, manifest, and final inodes are never deleted or overwritten",
        "tm_snapshot_artifacts.py:identity-bound cleanup",
        (
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_foreign_temp_never_deleted",
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_foreign_recovery_copy_never_deleted",
            "tests.test_tm_snapshot_recovery.TMRecoveryArtifactSafetyTests."
            "test_foreign_manifest_temp_blocks_reconstruction",
            "tests.test_tm_export.TMExportFailureInjectionTests."
            "test_foreign_manifest_created_at_replace_is_not_overwritten",
        ),
        "Cleanup and publication mutate only the exact inode recorded by their durable ownership proof.",
    ),
)
FAULT_MATRIX_ROWS = TASK_9_1_ROWS + TASK_9_2_ROWS


def fault_matrix_payload(
    rows: tuple[FaultMatrixRow, ...] = FAULT_MATRIX_ROWS,
) -> dict[str, object]:
    return {
        "schema_version": FAULT_MATRIX_SCHEMA_VERSION,
        "rows": [
            {
                "assertion_contract": row.assertion_contract,
                "fault_class": row.fault_class.value,
                "fault_row": row.fault_row,
                "production_seam": row.production_seam,
                "row_id": row.row_id,
                "task": row.task,
                "test_ids": list(row.test_ids),
            }
            for row in rows
        ],
    }


def fault_matrix_registry_digest(
    rows: tuple[FaultMatrixRow, ...] = FAULT_MATRIX_ROWS,
) -> str:
    encoded = json.dumps(
        fault_matrix_payload(rows),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fault_matrix_source_paths(
    rows: tuple[FaultMatrixRow, ...] = FAULT_MATRIX_ROWS,
) -> tuple[str, ...]:
    paths = {
        "tm_candidate_index.py",
        "tm_candidate_store_contracts.py",
        "tm_sqlite_candidate_projection.py",
        "tm_sqlite_store.py",
        "tests/fault_matrix_registry.py",
        "tests/test_tm_fault_matrix.py",
        "tools/validate_tm_fault_matrix.py",
    }
    for row in rows:
        paths.add(row.production_seam.partition(":")[0])
        for test_id in row.test_ids:
            module_name = ".".join(test_id.split(".")[:2])
            paths.add(module_name.replace(".", "/") + ".py")
    return tuple(sorted(paths))


def fault_matrix_source_fingerprint(
    registry_digest: str,
    source_files: tuple[tuple[str, str], ...],
) -> str:
    if type(registry_digest) is not str or _SHA256.fullmatch(registry_digest) is None:
        raise ValueError("registry digest must be SHA-256")
    for path, digest in source_files:
        if type(path) is not str or not path or Path(path).is_absolute():
            raise ValueError("source file path must be relative")
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise ValueError("source file digest must be SHA-256")
    encoded = json.dumps(
        {
            "registry_digest": registry_digest,
            "source_files": [list(item) for item in source_files],
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "FAULT_MATRIX_ROWS",
    "FAULT_MATRIX_SCHEMA_VERSION",
    "FaultClass",
    "FaultMatrixRow",
    "SNAPSHOT_PROCESS_DEATH_BOUNDARIES",
    "SnapshotProcessDeathBoundary",
    "TASK_9_1_ROWS",
    "TASK_9_2_ROWS",
    "fault_matrix_payload",
    "fault_matrix_registry_digest",
    "fault_matrix_source_fingerprint",
    "fault_matrix_source_paths",
]
