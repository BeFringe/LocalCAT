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


TASK_9_2_ROWS: tuple[FaultMatrixRow, ...] = ()
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
    "TASK_9_1_ROWS",
    "TASK_9_2_ROWS",
    "fault_matrix_payload",
    "fault_matrix_registry_digest",
    "fault_matrix_source_fingerprint",
    "fault_matrix_source_paths",
]
