"""Safe schema and connection policy for per-resource canonical TM stores.

Tasks 5.6-5.9 add the durable activation journal (PREPARED -> DB_REPLACED
-> MANIFEST_PUBLISHED -> GENERATION_PUBLISHED), idempotent recovery of one
pending activation, and the deterministic Task 5.9 rollback that restores
one complete prior authority (or the legacy first-activation JSONL) when a
journal-authenticated new set cannot be proven at any phase.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
import hashlib
import importlib
import itertools
import json
import math
import os
from pathlib import Path, PurePath
import sqlite3
import stat
import sys
import threading
import time
from typing import Any, NoReturn, Protocol, cast
import unicodedata
import uuid

import tm_contracts as contract_module
import tm_candidate_store_contracts as candidate_contracts
import tm_sqlite_candidate_projection as candidate_projection
from tm_candidate_store_contracts import (
    CANDIDATE_INDEX_VERSION,
    CANDIDATE_PROOF_BLOCK_SIZE,
    CANDIDATE_PROOF_BLOCK_VERSION_V1,
    CandidateProofIndexError,
    SQLiteCandidateProofBlock,
    SQLiteCandidateProofDensePhase1,
    SQLiteCandidateProofDensePhase2,
    SQLiteCandidateProofRecord,
    SQLiteCandidateProofSnapshot,
    SQLiteCandidateRecallSnapshot,
    SQLiteCandidateRecord,
    SQLiteCandidateWritePlan,
    SQLiteGramRow,
    SQLiteStoreSchemaError,
    _CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY,
    _SQLiteCandidateProofDenseReceipt,
    build_candidate_write_plan,
    character_ngram_frequencies,
    unique_character_ngrams,
)
from text_matcher import (
    TEXT_MATCHER_SEMANTICS_VERSION,
    UNICODE_VERSION,
    fold_text_value_v1,
)
from tm_contracts import (
    SCORER_VERSION_V1,
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    CanonicalResourceIdentity,
    MutableStageRef,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SealedStage,
    SourceBindingState,
    StoreHealth,
    TMRecord,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_content_attestation import (
    ActiveContentAttestation,
    ContentSemanticFacts,
    ContentAttestationError,
    ContentFileProof,
    LOGICAL_CLOSURE_VERSION,
    PortableActiveContentAttestation,
    PortableContentFileProof,
    PortableSealedContentAttestation,
    _capture_content_file,
    _capture_platform_content_file,
    _create_portable_active_content_attestation,
)
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundContentFacts,
    BoundExistingFileMutationGuard,
    BoundRegularFile,
    BoundSynchronizedRegularFile,
    CandidateContentFacts,
    CandidateFile,
    ExistingFileMutationGuard,
    ExistingFileRetirement,
    FileObjectIdentity,
    LedgerEnumerationLimits,
    LockedDescendantNamespaceInspection,
    MutableFileReservation,
    OpaqueAuthority,
    PendingPublication,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PublishMode,
    RootedDirectoryAuthority,
)


from tm_activation_journal import (
    ActivationBackupEvidence,
    ActivationPreparationError,
    ActivationRecoveryReport,
    _ACTIVATION_JOURNAL_DIGEST_FIELDS,
    _ACTIVATION_JOURNAL_ENVELOPE_FIELDS,
    _ACTIVATION_JOURNAL_FACTORY_KEY,
    _ACTIVATION_JOURNAL_IDENTITY_FIELDS,
    _ACTIVATION_JOURNAL_IDENTITY_PAIR_FIELDS,
    _ACTIVATION_JOURNAL_OPTIONAL_DIGEST_FIELDS,
    _ACTIVATION_JOURNAL_OPTIONAL_IDENTITY_PAIR_FIELDS,
    _ACTIVATION_JOURNAL_OPTIONAL_PATH_FIELDS,
    _ACTIVATION_JOURNAL_PATH_FIELDS,
    _ACTIVATION_JOURNAL_PRIOR_OPTIONAL_FIELDS,
    _ACTIVATION_JOURNAL_RECORD_FIELDS,
    _ACTIVATION_JOURNAL_VERSION,
    _ACTIVATION_PREPARATION_FACTORY_KEY,
    _ACTIVATION_RECOVERY_ACTIONS,
    _ActivationCleanupReservation,
    _ActivationFileIdentity,
    _ActivationJournalHandle,
    _ActivationJournalPhase,
    _ActivationJournalRecord,
    _ActivationPreparation,
    _CanonicalStoreRef,
    _OwnedRecoveryPath,
    _PHASE_SEQUENCE,
    _PriorActivationRef,
    _PriorAssetCapture,
    _PortableActivationJournalRecord,
    _PortableActivationJournalUnsigned,
    _PortableCancelledJournalHandle,
    _PortablePreparedJournalHandle,
    _PortablePublicationPhaseHandle,
    _PortablePublicationPhaseRecord,
    _PortablePublicationPhaseUnsigned,
    _PortableReplacementBackupProof,
    _PortableReplacementNamespaceSnapshot,
    _PortableReplacementRecord,
    _PortableReplacementUnsigned,
    _CallerHeldPortableJournalBorrow,
    _ROLLBACK_ELIGIBLE_ERROR_CODES,
    _RecoveryBackupAsset,
    _SQLiteGenerationView,
    _StoreRuntimeRef,
    _activation_file_identity,
    _activation_journal_digest,
    _activation_journal_path,
    _activation_journal_record_payload,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_lineage_marker_state_complete,
    _activation_lineage_marker_temp_path,
    _activation_quarantine_directory,
    _activation_rollback_eligible,
    _activation_terminal_coexistence_valid,
    _activation_terminal_path,
    _activation_terminal_temp_path,
    _capture_activation_file,
    _capture_pre_drain_assets,
    _close_activation_journal,
    _create_recovery_backup,
    _create_recovery_backups,
    _PORTABLE_ACTIVATION_JOURNAL_PHASE,
    _PORTABLE_ACTIVATION_JOURNAL_VERSION,
    _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
    _PORTABLE_PUBLICATION_VERSION,
    _portable_activation_private_directory_name,
    _portable_initial_stage_quarantine_name,
    _portable_replacement_backup_name,
    _parse_portable_replacement_namespace,
    _portable_replacement_phase_name,
    _serialize_portable_replacement_record,
    _PORTABLE_REPLACEMENT_CURRENT_NAME,
    _PORTABLE_REPLACEMENT_OPERATION,
    _PORTABLE_REPLACEMENT_VERSION,
    _WindowsPortablePreparedJournalOwner,
    _WindowsPortableCancelledJournalOwner,
    _WindowsPortableFreshRecoveryOwner,
    _WindowsPortablePublicationPhaseOwner,
    _decode_activation_journal_record,
    _decode_journal_bool,
    _decode_journal_digest,
    _decode_journal_identity_pair,
    _decode_journal_int,
    _decode_journal_optional_identity_pair,
    _decode_journal_optional_int,
    _decode_journal_optional_path,
    _decode_journal_path,
    _decode_journal_phase,
    _decode_journal_string,
    _decode_optional_journal_digest,
    _decode_optional_journal_identity,
    _ensure_activation_lineage_marker,
    _activation_lineage_marker_payload,
    _fsync_activation_directory,
    _fsync_activation_file,
    _fsync_activation_journal,
    _fsync_recovery_backup,
    _fsync_recovery_deletion_directory,
    _fsync_recovery_directory,
    _lstat_activation_journal_identity,
    _lstat_activation_lineage_marker_identity,
    _lstat_activation_terminal_identity,
    _lstat_any_entry,
    _open_activation_journal_temp,
    _open_recovery_backup,
    _parse_activation_journal_bytes,
    _quarantine_failed_activation_artifacts,
    _quarantine_owned_activation_artifact,
    _read_activation_file_bytes,
    _read_activation_journal_file,
    _read_activation_lineage_marker,
    _remove_journal_proven_backups,
    _remove_orphaned_activation_temp,
    _remove_orphaned_rollback_temp,
    _remove_owned_activation_journal_final,
    _remove_owned_activation_journal_temp,
    _remove_owned_activation_terminal_final,
    _remove_recovery_backups,
    _remove_recovery_path,
    _replace_activation_file,
    _replay_activation_journal,
    _require_activation_journal_digest,
    _require_first_activation_absence,
    _require_quarantine_directory,
    _require_recovery_path_absent,
    _require_same_asset_captures,
    _revalidate_prior_assets,
    _rollback_terminal_prior_closes,
    _serialize_activation_journal_record,
    _terminal_new_authority_closes_main_prior,
    _terminal_prior_closure_matches,
    _unlink_recovery_backup,
    _validate_activation_journal_record,
    _validate_journal_native_identity_pair,
    _validate_journal_native_path,
    _write_activation_journal,
    _write_activation_journal_bytes,
    _write_activation_terminal,
    _write_recovery_backup,
)
from tm_activation_recovery import (
    _ActivationGateBGrant,
    _CoordinatorPublishPort,
    _PortableReplacementRecordOwner,
    _StoreValidationPort,
    _activation_exact_parity_digest,
    _activation_publication_digest,
    _advance_activation_journal_after_effect,
    _build_activation_journal_record,
    _canonical_activation_ref,
    _capture_journal_closure_file,
    _capture_prior_assets,
    _coexisting_terminal,
    _complete_prepared_cancellation,
    _complete_recovered_manifest,
    _complete_recovered_receipt,
    _discover_active_canonical,
    _load_activation_transition_record,
    _load_recovery_journal,
    _load_recovery_terminal,
    _preflight_recovered_manifest,
    _publish_activation_journal,
    _publish_activation_manifest,
    _publish_activation_receipt,
    _recover_activation_indexes,
    _recover_generation_publication,
    _recover_manifest_publication,
    _recovery_artifact_seal_digest,
    _recovery_capture_journal_file,
    _recovery_completed_binding,
    _recovery_expected_manifest_bytes,
    _recovery_jsonl_winners_digest,
    _recovery_mismatch,
    _recovery_prior_completed_binding,
    _portable_recovery_activation_digest,
    _recovery_receipt_row,
    _recovery_sealed_stage_digest,
    _replace_activation_database,
    _replay_cancelled_terminal_recovery,
    _require_cancelled_lineage_consistency,
    _replay_terminal_recovery,
    _require_activation_grant_identity,
    _require_activation_grant_identity_replacement,
    _require_activation_token_identity,
    _require_activation_token_identity_replacement,
    _require_rollback_backups,
    _restore_activation_file,
    _retire_coexisting_terminal,
    _revalidate_activation_effect_closure,
    _revalidate_activation_journal_closure,
    _revalidate_discovered_active_set,
    _revalidate_recovered_active_set,
    _revalidate_recovered_prior_set,
    _revalidate_recovered_sealed_database,
    _revalidate_recovery_authority,
    _revalidate_active_content_attestation,
    _require_bound_active_content_attestation,
    _rollback_inconsistent_activation,
    _rollback_restored_prior_view,
    _validate_activation_indexes,
    _validate_activation_publication_authority,
    _validate_published_activation_set,
    _validate_replaced_activation_database,
    publish_activation,
    completed_authority_requires_reattestation,
    reattest_completed_authority,
    recover_durable_activation,
    recover_portable_activation,
    recover_portable_replacement_activation,
    rehydrate_completed_portable_replacement_activation,
    rollback_durable_activation,
)

import tm_schema_upgrade as schema_upgrade_module

import tm_snapshot_recovery as snapshot_recovery_module
import tm_snapshot_artifacts as snapshot_artifacts_module


TM_SCHEMA_VERSION = 2
TM_LEGACY_SCHEMA_VERSION = 1
_SCHEMA_UPGRADE_META_KEY = "schema_upgrade_origin"
_SCHEMA_UPGRADE_META_VALUE = "schema-upgrade-v1"
FOLD_VERSION_V1 = "fold-v1-unicode-16.0.0"
BUSY_TIMEOUT_MS = 5000
_NATIVE_PATH_TYPE = type(Path())
_IMMEDIATE_SEAL_CACHE_KIB = 128 * 1024

_RECORD_COLUMNS = (
    "record_id, source_raw, target_raw, speaker_raw, context_prev_raw, "
    "context_next_raw, file_source, provenance_json, legacy_line_no, "
    "origin_batch_id, origin_ordinal"
)

_EXPORT_RECORD_COLUMNS = (
    "record_id, source_raw, target_raw, speaker_raw, context_prev_raw, "
    "context_next_raw, file_source, provenance_json, legacy_line_no, "
    "origin_batch_id, origin_ordinal, usage_count, last_used"
)

_BASE_TABLES = frozenset(
    {
        "tm_candidate_block",
        "tm_gram",
        "tm_gram_block_max",
        "tm_meta",
        "tm_origin_batch",
        "tm_record",
        "tm_snapshot_binding",
        "tm_snapshot_receipt",
    }
)
_BASE_INDEXES = frozenset(
    {
        "idx_tm_context_speaker",
        "idx_tm_exact",
        "idx_tm_gram_lookup",
        "idx_tm_gram_block_lookup",
    }
)
_LEGACY_BASE_TABLES = _BASE_TABLES - frozenset(
    {"tm_candidate_block", "tm_gram_block_max"}
)
_LEGACY_BASE_INDEXES = _BASE_INDEXES - frozenset(
    {"idx_tm_gram_block_lookup"}
)
_FTS5_SHADOW_TABLES = frozenset(
    {
        "tm_fts_config",
        "tm_fts_content",
        "tm_fts_data",
        "tm_fts_docsize",
        "tm_fts_idx",
    }
)
_APPROVED_SCHEMA_DIGESTS = {
    False: "dc45d70bb6e54d141149305a9e5c99a6d85b1a4b4c2857af57029a96f6279bb4",
    True: "ac0dcfe97473ed0ed71fd3de567f311cb9ef0a4f86e199c9b3994e62e0efe91b",
}
_APPROVED_LEGACY_SCHEMA_DIGESTS = {
    False: "725b94300abd64b5c06824ecb63357e2f737111e9fbd42094796183b924532a7",
    True: "8d093e3e7db360c2b8510ef524ca05170c187a9a2dd4c1f7326dec0a7df89da6",
}
_REQUIRED_META_KEYS = frozenset(
    {
        "activation_status",
        "candidate_index_kind",
        "candidate_index_version",
        "canonical_store_id",
        "divergence_latched",
        "fold_version",
        "fts5_available",
        "generation",
        "head_revision",
        "journal_mode",
        "resource_id",
        "schema_digest",
        "schema_version",
        "scorer_version",
        "sqlite_runtime_version",
        "target_identity",
        "text_semantics_version",
        "unicode_runtime_version",
    }
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE tm_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tm_origin_batch (
        batch_id TEXT PRIMARY KEY CHECK(length(batch_id) > 0),
        kind TEXT NOT NULL
            CHECK(kind IN ('migration', 'local_write', 'import')),
        source_digest TEXT,
        source_path TEXT,
        status TEXT NOT NULL
            CHECK(status IN ('staged', 'completed', 'failed')),
        valid_count INTEGER NOT NULL CHECK(valid_count >= 0),
        invalid_count INTEGER NOT NULL CHECK(invalid_count >= 0),
        duplicate_source_count INTEGER NOT NULL
            CHECK(duplicate_source_count >= 0),
        completed_revision INTEGER UNIQUE,
        created_at TEXT NOT NULL CHECK(length(created_at) > 0),
        CHECK(
            (
                kind = 'local_write'
                AND source_digest IS NULL
                AND source_path IS NULL
            )
            OR
            (
                kind IN ('migration', 'import')
                AND source_digest IS NOT NULL
                AND length(source_digest) = 64
                AND source_path IS NOT NULL
                AND length(source_path) > 0
            )
        ),
        CHECK(
            (
                status = 'completed'
                AND completed_revision IS NOT NULL
                AND completed_revision >= 1
            )
            OR
            (
                status IN ('staged', 'failed')
                AND completed_revision IS NULL
            )
        ),
        UNIQUE(kind, source_digest)
    )
    """,
    """
    CREATE TABLE tm_snapshot_receipt (
        snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id) > 0),
        resource_id TEXT NOT NULL CHECK(length(resource_id) > 0),
        canonical_store_id TEXT NOT NULL
            CHECK(length(canonical_store_id) > 0),
        exported_revision INTEGER NOT NULL CHECK(exported_revision >= 0),
        jsonl_digest TEXT NOT NULL CHECK(length(jsonl_digest) = 64),
        record_count INTEGER NOT NULL CHECK(record_count >= 0),
        format_version TEXT NOT NULL CHECK(length(format_version) > 0),
        destination_jsonl_path TEXT NOT NULL
            CHECK(length(destination_jsonl_path) > 0),
        destination_manifest_path TEXT NOT NULL
            CHECK(length(destination_manifest_path) > 0),
        status TEXT NOT NULL
            CHECK(status IN ('issued', 'completed', 'cancelled')),
        created_at TEXT NOT NULL CHECK(length(created_at) > 0)
    )
    """,
    """
    CREATE TABLE tm_snapshot_binding (
        binding_id INTEGER PRIMARY KEY CHECK(binding_id = 1),
        configured_jsonl_path TEXT NOT NULL
            CHECK(length(configured_jsonl_path) > 0),
        manifest_path TEXT NOT NULL CHECK(length(manifest_path) > 0),
        snapshot_kind TEXT NOT NULL
            CHECK(snapshot_kind IN ('MIGRATION_SOURCE', 'EXPLICIT_EXPORT')),
        snapshot_id TEXT NOT NULL,
        binding_version TEXT NOT NULL CHECK(length(binding_version) > 0),
        FOREIGN KEY(snapshot_id)
            REFERENCES tm_snapshot_receipt(snapshot_id)
    )
    """,
    """
    CREATE TABLE tm_record (
        record_id INTEGER PRIMARY KEY,
        source_raw TEXT NOT NULL CHECK(length(source_raw) > 0),
        target_raw TEXT NOT NULL CHECK(length(target_raw) > 0),
        source_fold_v1 TEXT NOT NULL CHECK(length(source_fold_v1) > 0),
        source_fold_length INTEGER NOT NULL CHECK(source_fold_length >= 0),
        speaker_raw TEXT,
        context_prev_raw TEXT,
        context_next_raw TEXT,
        file_source TEXT,
        provenance_json TEXT NOT NULL CHECK(length(provenance_json) > 0),
        legacy_line_no INTEGER CHECK(
            legacy_line_no IS NULL OR legacy_line_no >= 1
        ),
        usage_count INTEGER NOT NULL DEFAULT 0 CHECK(usage_count >= 0),
        last_used TEXT,
        origin_batch_id TEXT NOT NULL,
        origin_ordinal INTEGER NOT NULL CHECK(origin_ordinal >= 0),
        UNIQUE(origin_batch_id, origin_ordinal),
        FOREIGN KEY(origin_batch_id)
            REFERENCES tm_origin_batch(batch_id)
    )
    """,
    """
    CREATE INDEX idx_tm_exact
    ON tm_record(source_raw, record_id DESC)
    """,
    """
    CREATE INDEX idx_tm_context_speaker
    ON tm_record(source_raw, speaker_raw, record_id DESC)
    """,
    """
    CREATE TABLE tm_gram (
        gram_size INTEGER NOT NULL CHECK(gram_size IN (1, 2, 3)),
        gram TEXT NOT NULL CHECK(length(gram) > 0),
        record_id INTEGER NOT NULL,
        term_frequency INTEGER NOT NULL CHECK(term_frequency > 0),
        PRIMARY KEY(gram_size, gram, record_id),
        FOREIGN KEY(record_id)
            REFERENCES tm_record(record_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_tm_gram_lookup
    ON tm_gram(gram_size, gram, record_id)
    """,
    """
    CREATE TABLE tm_candidate_block (
        block_id INTEGER PRIMARY KEY,
        first_record_id INTEGER NOT NULL,
        last_record_id INTEGER NOT NULL,
        record_count INTEGER NOT NULL CHECK(record_count > 0),
        min_source_fold_length INTEGER NOT NULL,
        max_source_fold_length INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE tm_gram_block_max (
        gram_size INTEGER NOT NULL CHECK(gram_size IN (1, 2)),
        gram TEXT NOT NULL,
        block_id INTEGER NOT NULL,
        max_term_frequency INTEGER NOT NULL CHECK(max_term_frequency > 0),
        PRIMARY KEY(gram_size, gram, block_id),
        FOREIGN KEY(block_id)
            REFERENCES tm_candidate_block(block_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_tm_gram_block_lookup
    ON tm_gram_block_max(gram_size, gram, block_id)
    """,
)

_STREAMED_STAGE_SECONDARY_INDEX_NAMES = (
    "idx_tm_exact",
    "idx_tm_context_speaker",
    "idx_tm_gram_lookup",
    "idx_tm_gram_block_lookup",
)

_LEGACY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE tm_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tm_origin_batch (
        batch_id TEXT PRIMARY KEY CHECK(length(batch_id) > 0),
        kind TEXT NOT NULL
            CHECK(kind IN ('migration', 'local_write', 'import')),
        source_digest TEXT,
        source_path TEXT,
        status TEXT NOT NULL
            CHECK(status IN ('staged', 'completed', 'failed')),
        valid_count INTEGER NOT NULL CHECK(valid_count >= 0),
        invalid_count INTEGER NOT NULL CHECK(invalid_count >= 0),
        duplicate_source_count INTEGER NOT NULL
            CHECK(duplicate_source_count >= 0),
        created_at TEXT NOT NULL CHECK(length(created_at) > 0),
        CHECK(
            (
                kind = 'local_write'
                AND source_digest IS NULL
                AND source_path IS NULL
            )
            OR
            (
                kind IN ('migration', 'import')
                AND source_digest IS NOT NULL
                AND length(source_digest) = 64
                AND source_path IS NOT NULL
                AND length(source_path) > 0
            )
        ),
        UNIQUE(kind, source_digest)
    )
    """,
    """
    CREATE TABLE tm_snapshot_receipt (
        snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id) > 0),
        resource_id TEXT NOT NULL CHECK(length(resource_id) > 0),
        canonical_store_id TEXT NOT NULL
            CHECK(length(canonical_store_id) > 0),
        exported_revision INTEGER NOT NULL CHECK(exported_revision >= 0),
        jsonl_digest TEXT NOT NULL CHECK(length(jsonl_digest) = 64),
        record_count INTEGER NOT NULL CHECK(record_count >= 0),
        format_version TEXT NOT NULL CHECK(length(format_version) > 0),
        destination_jsonl_path TEXT NOT NULL
            CHECK(length(destination_jsonl_path) > 0),
        destination_manifest_path TEXT NOT NULL
            CHECK(length(destination_manifest_path) > 0),
        status TEXT NOT NULL
            CHECK(status IN ('issued', 'completed', 'cancelled')),
        created_at TEXT NOT NULL CHECK(length(created_at) > 0)
    )
    """,
    """
    CREATE TABLE tm_snapshot_binding (
        binding_id INTEGER PRIMARY KEY CHECK(binding_id = 1),
        configured_jsonl_path TEXT NOT NULL
            CHECK(length(configured_jsonl_path) > 0),
        manifest_path TEXT NOT NULL CHECK(length(manifest_path) > 0),
        snapshot_kind TEXT NOT NULL
            CHECK(snapshot_kind IN ('MIGRATION_SOURCE', 'EXPLICIT_EXPORT')),
        snapshot_id TEXT NOT NULL,
        binding_version TEXT NOT NULL CHECK(length(binding_version) > 0),
        FOREIGN KEY(snapshot_id)
            REFERENCES tm_snapshot_receipt(snapshot_id)
    )
    """,
    """
    CREATE TABLE tm_record (
        record_id INTEGER PRIMARY KEY,
        source_raw TEXT NOT NULL CHECK(length(source_raw) > 0),
        target_raw TEXT NOT NULL CHECK(length(target_raw) > 0),
        source_fold_v1 TEXT NOT NULL CHECK(length(source_fold_v1) > 0),
        speaker_raw TEXT,
        context_prev_raw TEXT,
        context_next_raw TEXT,
        file_source TEXT,
        provenance_json TEXT NOT NULL CHECK(length(provenance_json) > 0),
        legacy_line_no INTEGER CHECK(
            legacy_line_no IS NULL OR legacy_line_no >= 1
        ),
        usage_count INTEGER NOT NULL DEFAULT 0 CHECK(usage_count >= 0),
        last_used TEXT,
        origin_batch_id TEXT NOT NULL,
        origin_ordinal INTEGER NOT NULL CHECK(origin_ordinal >= 0),
        UNIQUE(origin_batch_id, origin_ordinal),
        FOREIGN KEY(origin_batch_id)
            REFERENCES tm_origin_batch(batch_id)
    )
    """,
    """
    CREATE INDEX idx_tm_exact
    ON tm_record(source_raw, record_id DESC)
    """,
    """
    CREATE INDEX idx_tm_context_speaker
    ON tm_record(source_raw, speaker_raw, record_id DESC)
    """,
    """
    CREATE TABLE tm_gram (
        gram_size INTEGER NOT NULL CHECK(gram_size IN (1, 2, 3)),
        gram TEXT NOT NULL CHECK(length(gram) > 0),
        record_id INTEGER NOT NULL,
        PRIMARY KEY(gram_size, gram, record_id),
        FOREIGN KEY(record_id)
            REFERENCES tm_record(record_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_tm_gram_lookup
    ON tm_gram(gram_size, gram, record_id)
    """,
)

_FTS5_STATEMENT = """
CREATE VIRTUAL TABLE tm_fts USING fts5(
    source_fold_v1,
    record_id UNINDEXED,
    tokenize='trigram case_sensitive 1'
)
"""


class SQLiteStoreLifecycleError(SQLiteStoreSchemaError):
    """A safe lifecycle failure scoped to one resource generation."""

    def __init__(
        self,
        code: str,
        *,
        resource_id: str,
        generation: int,
        retryable: bool,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.resource_id = resource_id
        self.generation = generation
        self.retryable = retryable


class SchemaUpgradeAncestryError(SQLiteStoreSchemaError):
    """Fail-closed Task 5.11 ancestry failure carrying a stable code."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        if type(code) is not str or not code:
            raise TypeError("schema upgrade ancestry code is invalid")
        if type(retryable) is not bool:
            raise TypeError("schema upgrade ancestry retryable flag is invalid")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


type _PreparedRecordDraft = tuple[
    str,
    str,
    str,
    str | None,
    str | None,
    str | None,
    str | None,
    tuple[tuple[str, str], ...],
    str,
    int | None,
    int,
]


@dataclass(frozen=True)
class CanonicalRevisionSnapshot:
    """One leased observation of a canonical revision and its ancestry."""

    resource_id: str
    canonical_store_id: str
    generation: int
    head_revision: int
    record_count: int


@dataclass(frozen=True)
class CanonicalExportRecord:
    """One record plus its usage facts inside one export snapshot."""

    record: TMRecord
    usage_count: int
    last_used: str | None

    def __post_init__(self) -> None:
        if type(self.record) is not TMRecord:
            raise TypeError("export record must be exact TMRecord")
        if type(self.usage_count) is not int or self.usage_count < 0:
            raise ValueError("export usage count is invalid")
        if self.last_used is not None and type(self.last_used) is not str:
            raise TypeError("export last_used must be a built-in string or None")


@dataclass(frozen=True)
class CanonicalExportSnapshot:
    """Revision and complete record order observed in one read snapshot."""

    revision: CanonicalRevisionSnapshot
    records: tuple[CanonicalExportRecord, ...]

    def __post_init__(self) -> None:
        if type(self.revision) is not CanonicalRevisionSnapshot:
            raise TypeError(
                "export snapshot revision must be CanonicalRevisionSnapshot"
            )
        if type(self.records) is not tuple:
            raise TypeError("export snapshot records must be a built-in tuple")
        for item in self.records:
            if type(item) is not CanonicalExportRecord:
                raise TypeError(
                    "export snapshot records must contain "
                    "CanonicalExportRecord values"
                )
        if self.revision.record_count != len(self.records):
            raise ValueError(
                "export snapshot record count does not match revision"
            )


@dataclass(frozen=True)
class SourceBindingObservation:
    """Safe source-binding state derived from one canonical generation."""

    resource_id: str
    canonical_store_id: str
    generation: int
    head_revision: int
    state: SourceBindingState
    binding_digest: str | None
    diagnostic_codes: tuple[str, ...]


class _PortableActiveSetAuthority(OpaqueAuthority):
    """Process-local protection for one fully revalidated ACTIVE set."""

    __slots__ = (
        "_coordinator",
        "_root",
        "_database_guard",
        "_authorities",
        "_names",
        "_proofs",
        "_attestation",
    )

    def __init__(
        self,
        *,
        coordinator: ResourceStoreCoordinator,
        root: RootedDirectoryAuthority,
        database_guard: BoundExistingFileMutationGuard,
        authorities: tuple[BoundRegularFile, BoundRegularFile, BoundRegularFile],
        names: tuple[str, str, str],
        proofs: tuple[
            PortableContentFileProof,
            PortableContentFileProof,
            PortableContentFileProof,
        ],
        attestation: PortableActiveContentAttestation,
    ) -> None:
        super().__init__()
        if not isinstance(database_guard, BoundExistingFileMutationGuard):
            raise TypeError("database_guard must be a live mutation guard")
        if len(authorities) != 3 or any(
            not isinstance(item, BoundRegularFile) for item in authorities
        ):
            raise TypeError("active-set authorities must contain three files")
        if len(names) != 3 or len(proofs) != 3:
            raise ValueError("active-set authority facts must contain three entries")
        if type(attestation) is not PortableActiveContentAttestation:
            raise TypeError("attestation must be exact portable active attestation")
        self._coordinator = coordinator
        self._root = root
        self._database_guard = database_guard
        self._authorities = authorities
        self._names = names
        self._proofs = proofs
        self._attestation = attestation
        self.reprove()

    def reprove(self) -> PortableActiveContentAttestation:
        self._require_open()
        self._database_guard.reprove()
        for name, authority, expected in zip(
            self._names,
            self._authorities,
            self._proofs,
            strict=True,
        ):
            facts = authority.content_facts()
            if (
                facts.snapshot.identity.kind != "regular"
                or facts.snapshot.identity.link_count != 1
                or self._root.inspect_entry(name) != facts.snapshot
                or self._coordinator._portable_content_proof(facts) != expected
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.ACTIVE_SET_INVALID",
                    retryable=False,
                )
        self._database_guard.reprove()
        return self._attestation

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        for authority in reversed(self._authorities):
            try:
                authority.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        try:
            self._database_guard.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error


@dataclass(frozen=True, slots=True)
class _PortableReplacementBackupPlan:
    """Process-only expected content for one replacement recovery backup."""

    asset_kind: str
    backup_name: str
    content: PortableContentFileProof

    def __post_init__(self) -> None:
        if self.asset_kind not in {"DATABASE", "MANIFEST"}:
            raise ValueError("replacement backup kind is invalid")
        if type(self.backup_name) is not str or not self.backup_name:
            raise TypeError("replacement backup name is invalid")
        if type(self.content) is not PortableContentFileProof:
            raise TypeError("replacement backup content proof is invalid")


@dataclass(frozen=True, slots=True, init=False)
class _PortableReplacementPreparation:
    """Single-use process authority for one Windows replacement attempt.

    This is intentionally distinct from the historical
    ``_ActivationPreparation``.  The latter requires native path-bearing
    recovery assets and backs the v2/POSIX phase machine; admitting portable
    replacement facts there would silently broaden that frozen contract.
    """

    preparation_id: str
    resource_id: str
    target_identity: str
    candidate_store_id: str
    prior_store_id: str
    expected_prior_generation: int
    next_generation: int
    gate_b_grant_digest: str
    lock_payload_digest: str
    prior_authority_digest: str
    backup_plans: tuple[
        _PortableReplacementBackupPlan,
        _PortableReplacementBackupPlan,
    ]
    _token: contract_module._ActivationToken = field(repr=False, compare=False)
    _physical_snapshot: object = field(repr=False, compare=False)
    _prior_view: _SQLiteGenerationView = field(repr=False, compare=False)
    _sealed_stage: SealedStage = field(repr=False, compare=False)
    _prior_authority: _PortableReplacementPriorAuthority = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        *,
        preparation_id: str,
        resource_id: str,
        target_identity: str,
        candidate_store_id: str,
        prior_store_id: str,
        expected_prior_generation: int,
        next_generation: int,
        gate_b_grant_digest: str,
        lock_payload_digest: str,
        prior_authority_digest: str,
        backup_plans: tuple[
            _PortableReplacementBackupPlan,
            _PortableReplacementBackupPlan,
        ],
        _token: contract_module._ActivationToken,
        _physical_snapshot: object,
        _prior_view: _SQLiteGenerationView,
        _sealed_stage: SealedStage,
        _prior_authority: _PortableReplacementPriorAuthority,
    ) -> None:
        for value, label in (
            (preparation_id, "preparation id"),
            (resource_id, "resource id"),
            (target_identity, "target identity"),
            (candidate_store_id, "candidate store id"),
            (prior_store_id, "prior store id"),
            (gate_b_grant_digest, "Gate B digest"),
            (lock_payload_digest, "lock payload digest"),
            (prior_authority_digest, "prior authority digest"),
        ):
            if type(value) is not str or not value:
                raise TypeError(f"replacement {label} is invalid")
        if type(expected_prior_generation) is not int:
            raise TypeError("replacement prior generation is invalid")
        if expected_prior_generation < 0:
            raise ValueError("replacement prior generation is invalid")
        if type(next_generation) is not int:
            raise TypeError("replacement next generation is invalid")
        if next_generation != expected_prior_generation + 1:
            raise ValueError("replacement generation transition is invalid")
        if candidate_store_id == prior_store_id:
            raise ValueError("replacement candidate store must be fresh")
        if (
            type(backup_plans) is not tuple
            or len(backup_plans) != 2
            or any(
                type(plan) is not _PortableReplacementBackupPlan
                for plan in backup_plans
            )
            or {plan.asset_kind for plan in backup_plans}
            != {"DATABASE", "MANIFEST"}
        ):
            raise TypeError("replacement backup plan is incomplete")
        if type(_prior_view) is not _SQLiteGenerationView:
            raise TypeError("replacement prior view is invalid")
        if type(_sealed_stage) is not SealedStage:
            raise TypeError("replacement sealed stage is invalid")
        if type(_prior_authority) is not _PortableReplacementPriorAuthority:
            raise TypeError("replacement prior authority is invalid")
        for name, value in (
            ("preparation_id", preparation_id),
            ("resource_id", resource_id),
            ("target_identity", target_identity),
            ("candidate_store_id", candidate_store_id),
            ("prior_store_id", prior_store_id),
            ("expected_prior_generation", expected_prior_generation),
            ("next_generation", next_generation),
            ("gate_b_grant_digest", gate_b_grant_digest),
            ("lock_payload_digest", lock_payload_digest),
            ("prior_authority_digest", prior_authority_digest),
            ("backup_plans", backup_plans),
            ("_token", _token),
            ("_physical_snapshot", _physical_snapshot),
            ("_prior_view", _prior_view),
            ("_sealed_stage", _sealed_stage),
            ("_prior_authority", _prior_authority),
        ):
            object.__setattr__(self, name, value)

    def __reduce__(self) -> object:
        raise TypeError("portable replacement preparation is code-only")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("portable replacement preparation is code-only")


class _PortableReplacementPriorAuthority(OpaqueAuthority):
    """Live W1/root proof of the prior canonical pair and current source.

    The completed predecessor record remains the generation/lineage authority,
    while this object freezes a fresh content attestation after the coordinator
    has drained.  That distinction is required because healthy canonical writes
    and a latched ``SOURCE_DIVERGED`` observation legitimately change the live
    database bytes after the predecessor READY record was published.  All three
    live handles remain open through backup publication and PREPARED commit.
    """

    __slots__ = (
        "_coordinator",
        "_platform",
        "_root",
        "_caller_borrow",
        "_prior_view",
        "_authorities",
        "_names",
        "_proofs",
        "_lineage_attestation",
        "_prior_attestation",
    )

    def __init__(
        self,
        *,
        coordinator: ResourceStoreCoordinator,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        prior_view: _SQLiteGenerationView,
        authorities: tuple[BoundRegularFile, BoundRegularFile, BoundRegularFile],
        names: tuple[str, str, str],
        proofs: tuple[
            PortableContentFileProof,
            PortableContentFileProof,
            PortableContentFileProof,
        ],
        lineage_attestation: PortableActiveContentAttestation,
        prior_attestation: PortableActiveContentAttestation,
    ) -> None:
        super().__init__()
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("replacement platform is invalid")
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError("replacement root is invalid")
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("replacement caller borrow is invalid")
        if type(prior_view) is not _SQLiteGenerationView:
            raise TypeError("replacement prior view is invalid")
        if len(authorities) != 3 or any(
            not isinstance(authority, BoundRegularFile)
            for authority in authorities
        ):
            raise TypeError("replacement prior authority set is invalid")
        if len(names) != 3 or len(proofs) != 3:
            raise ValueError("replacement prior proof set is incomplete")
        if (
            type(lineage_attestation) is not PortableActiveContentAttestation
            or type(prior_attestation) is not PortableActiveContentAttestation
        ):
            raise TypeError("replacement prior attestation is invalid")
        self._coordinator = coordinator
        self._platform = platform
        self._root = root
        self._caller_borrow = caller_borrow
        self._prior_view = prior_view
        self._authorities = authorities
        self._names = names
        self._proofs = proofs
        self._lineage_attestation = lineage_attestation
        self._prior_attestation = prior_attestation
        self.reprove()

    @property
    def prior_attestation(self) -> PortableActiveContentAttestation:
        self._require_open()
        return self._prior_attestation

    @property
    def database_proof(self) -> PortableContentFileProof:
        self._require_open()
        return self._proofs[0]

    @property
    def manifest_proof(self) -> PortableContentFileProof:
        self._require_open()
        return self._proofs[1]

    @property
    def source_proof(self) -> PortableContentFileProof:
        self._require_open()
        return self._proofs[2]

    def reprove(self) -> PortableActiveContentAttestation:
        self._require_open()
        coordinator = self._coordinator
        prior_view = self._prior_view
        lineage = self._lineage_attestation
        prior = self._prior_attestation
        if (
            coordinator._state not in {"READY", "DRAINING", "ACTIVATING"}
            or coordinator._view is not prior_view
            or prior_view.generation != prior.generation
            or prior_view.canonical_store_id != prior.canonical_store_id
            or prior_view.active_content_attestation != lineage
            or coordinator._canonical_store_id != prior.canonical_store_id
            or self._proofs[0] != prior.database
            or self._proofs[1] != prior.manifest
            or self._proofs[2] != prior.source
            or lineage.journal_id != prior.journal_id
            or lineage.resource_id != prior.resource_id
            or lineage.target_identity != prior.target_identity
            or lineage.canonical_store_id != prior.canonical_store_id
            or lineage.snapshot_receipt_digest
            != prior.snapshot_receipt_digest
            or lineage.generation != prior.generation
            or lineage.activation_digest != prior.activation_digest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        self._caller_borrow.reprove(
            self._platform,
            coordinator._resource_identity,
        )
        for name, authority, expected in zip(
            self._names,
            self._authorities,
            self._proofs,
            strict=True,
        ):
            facts = authority.content_facts()
            if (
                facts.snapshot.identity.kind != "regular"
                or facts.snapshot.identity.link_count != 1
                or self._root.inspect_entry(name) != facts.snapshot
                or coordinator._portable_content_proof(facts) != expected
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PRIOR_ASSET_INVALID",
                    retryable=False,
                )
        self._caller_borrow.reprove(
            self._platform,
            coordinator._resource_identity,
        )
        return prior

    def _stream_asset(self, asset_kind: str) -> tuple[Iterator[bytes], int]:
        self._require_open()
        if asset_kind == "DATABASE":
            authority = self._authorities[0]
        elif asset_kind == "MANIFEST":
            authority = self._authorities[1]
        else:
            raise ValueError("replacement backup kind is invalid")
        expected = authority.snapshot()

        def chunks() -> Iterator[bytes]:
            offset = 0
            while offset < expected.byte_count:
                maximum = min(64 * 1024, expected.byte_count - offset)
                chunk = authority.read_at(offset, maximum, expected)
                if not chunk:
                    raise ActivationPreparationError(
                        "ACTIVATION.BACKUP_FAILED",
                        retryable=True,
                    )
                offset += len(chunk)
                yield chunk
            if authority.snapshot() != expected:
                raise ActivationPreparationError(
                    "ACTIVATION.PRIOR_ASSET_INVALID",
                    retryable=False,
                )

        return chunks(), expected.byte_count

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        for authority in reversed(self._authorities):
            try:
                authority.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


class _PortableReplacementBackupSet(OpaqueAuthority):
    """Two retained backup publications spanning the PREPARED owner commit."""

    __slots__ = (
        "_prior",
        "_private_parent",
        "_pending",
        "_plans",
    )

    def __init__(
        self,
        *,
        prior: _PortableReplacementPriorAuthority,
        private_parent: BoundDirectoryAuthority,
        pending: tuple[PendingPublication, PendingPublication],
        plans: tuple[
            _PortableReplacementBackupPlan,
            _PortableReplacementBackupPlan,
        ],
    ) -> None:
        super().__init__()
        if type(prior) is not _PortableReplacementPriorAuthority:
            raise TypeError("replacement prior authority is invalid")
        if not isinstance(private_parent, BoundDirectoryAuthority):
            raise TypeError("replacement private parent is invalid")
        if len(pending) != 2 or any(
            not isinstance(item, PendingPublication) for item in pending
        ):
            raise TypeError("replacement backup publications are invalid")
        if len(plans) != 2 or {plan.asset_kind for plan in plans} != {
            "DATABASE",
            "MANIFEST",
        }:
            raise ValueError("replacement backup plan is incomplete")
        self._prior = prior
        self._private_parent = private_parent
        self._pending = pending
        self._plans = plans
        self.reprove()

    @property
    def plans(
        self,
    ) -> tuple[
        _PortableReplacementBackupPlan,
        _PortableReplacementBackupPlan,
    ]:
        self._require_open()
        return self._plans

    def reprove(self) -> None:
        self._require_open()
        self._prior.reprove()
        self._private_parent.reprove()
        for publication, plan in zip(self._pending, self._plans, strict=True):
            retained = publication.retained_destination()
            facts = retained.content_facts()
            if (
                facts.snapshot.identity.kind != "regular"
                or facts.snapshot.identity.link_count != 1
                or self._private_parent.inspect_entry(plan.backup_name)
                != facts.snapshot
                or self._prior._coordinator._portable_content_proof(facts)
                != plan.content
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.BACKUP_FAILED",
                    retryable=True,
                )

    def complete_after_prepared(self) -> None:
        """Close both platform handshakes only after durable PREPARED."""

        self._require_open()
        self.reprove()
        for publication in self._pending:
            if publication.terminal_reproof() != publication.preliminary_facts():
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
        self.reprove()
        self.close()

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        for publication in reversed(self._pending):
            try:
                publication.close()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        try:
            self._private_parent.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error




class _CoordinatorStorePort:
    """Narrow store-side adapter exposing coordinator state to recovery.

    Implements the ``tm_activation_recovery`` ``_CoordinatorPublishPort``
    protocol over one ``ResourceStoreCoordinator``.  Keeping the adapter in
    this module means the recovery module never imports ``tm_sqlite_store``.
    """

    def __init__(self, coordinator: ResourceStoreCoordinator) -> None:
        self._coordinator = coordinator

    @property
    def resource_identity(self) -> CanonicalResourceIdentity:
        return self._coordinator._resource_identity

    @property
    def canonical_store_id(self) -> str:
        return self._coordinator._canonical_store_id

    @property
    def view(self) -> _SQLiteGenerationView | None:
        return self._coordinator._view

    @view.setter
    def view(self, value: _SQLiteGenerationView | None) -> None:
        self._coordinator._view = value

    @property
    def state(self) -> str:
        return self._coordinator._state

    @state.setter
    def state(self, value: str) -> None:
        self._coordinator._state = value

    @property
    def preparation(self) -> _ActivationPreparation | None:
        return self._coordinator._preparation

    @preparation.setter
    def preparation(self, value: _ActivationPreparation | None) -> None:
        self._coordinator._preparation = value

    @property
    def cleanup_reservation(self) -> _ActivationCleanupReservation | None:
        return self._coordinator._cleanup_reservation

    @cleanup_reservation.setter
    def cleanup_reservation(
        self,
        value: _ActivationCleanupReservation | None,
    ) -> None:
        self._coordinator._cleanup_reservation = value

    @property
    def cleanup_in_progress(self) -> bool:
        return self._coordinator._cleanup_in_progress

    @property
    def active_lease_count(self) -> int:
        return self._coordinator._active_lease_count

    @property
    def drain_timeout_seconds(self) -> float:
        return self._coordinator._drain_timeout_seconds

    @property
    def sealed_registry(self) -> Any:
        return self._coordinator._sealed_registry

    store_schema_error: type[RuntimeError] = SQLiteStoreSchemaError

    def notify_all(self) -> None:
        self._coordinator._condition.notify_all()

    def rehydrate_completed_portable_base(
        self,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> ActivationRecoveryReport | None:
        return _rehydrate_completed_portable_authority(
            self,
            platform=platform,
            persistent_private=persistent_private,
            descendant_inspection=descendant_inspection,
            caller_borrow=caller_borrow,
        )

    def adopt_portable_replacement_current(
        self,
        record: _PortableReplacementRecord | None,
    ) -> None:
        if record is not None and (
            type(record) is not _PortableReplacementRecord
            or record.unsigned.phase != "READY"
        ):
            raise TypeError("portable replacement current record is invalid")
        self._coordinator._portable_replacement_current_record = record

    def cleanup_portable_replacement_ready_namespace(
        self,
        snapshot: _PortableReplacementNamespaceSnapshot,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> None:
        self._coordinator._cleanup_portable_replacement_ready_namespace(
            snapshot,
            platform=platform,
            persistent_private=persistent_private,
            caller_borrow=caller_borrow,
        )

    def cleanup_portable_replacement_prepared_namespace(
        self,
        snapshot: _PortableReplacementNamespaceSnapshot,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> None:
        self._coordinator._cleanup_portable_replacement_prepared_namespace(
            snapshot,
            platform=platform,
            persistent_private=persistent_private,
            caller_borrow=caller_borrow,
        )

    def portable_content_proof(
        self,
        facts: BoundContentFacts | CandidateContentFacts,
    ) -> PortableContentFileProof:
        return self._coordinator._portable_content_proof(facts)

    def apply_portable_receipt_activation(
        self,
        *,
        binding: SnapshotBinding,
        prepared: _PortableActivationJournalRecord,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        activation_digest: str,
        allow_sealed_transition: bool,
    ) -> tuple[
        PortableContentFileProof,
        str,
        BoundExistingFileMutationGuard,
    ]:
        return self._coordinator._apply_portable_receipt_activation(
            binding=binding,
            prepared=prepared,
            platform=platform,
            root=root,
            activation_digest=activation_digest,
            allow_sealed_transition=allow_sealed_transition,
        )

    def reprove_portable_sealed_database(
        self,
        *,
        prepared: _PortableActivationJournalRecord,
    ) -> None:
        snapshot = inspect_stage_schema(
            _canonical_activation_ref(
                self.resource_identity,
                journal_id=prepared.unsigned.journal_id,
            ),
            canonical_store_id=self.canonical_store_id,
            _allow_sealed=True,
        )
        if snapshot.activation_status != "SEALED":
            raise ActivationPreparationError(
                "ACTIVATION.DB_REOPEN_INVALID",
                retryable=False,
            )

    def reprove_portable_active_set(
        self,
        *,
        binding: SnapshotBinding,
        prepared: _PortableActivationJournalRecord,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        database: PortableContentFileProof,
        manifest: PortableContentFileProof,
        activation_digest: str,
        expected_logical_closure_digest: str,
        database_guard: BoundExistingFileMutationGuard,
        expected_candidate_projection_digest: str | None = None,
    ) -> tuple[
        _CanonicalStoreRef,
        SQLiteSchemaSnapshot,
        PortableActiveContentAttestation,
        _PortableActiveSetAuthority,
    ]:
        return self._coordinator._reprove_portable_active_set(
            binding=binding,
            prepared=prepared,
            platform=platform,
            root=root,
            database=database,
            manifest=manifest,
            activation_digest=activation_digest,
            expected_logical_closure_digest=expected_logical_closure_digest,
            database_guard=database_guard,
            expected_candidate_projection_digest=(
                expected_candidate_projection_digest
            ),
        )

    def ensure_portable_activation_lineage_marker(
        self,
        *,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
    ) -> None:
        self._coordinator._ensure_portable_activation_lineage_marker(
            platform=platform,
            root=root,
        )

    def _activate_candidate_store_id(self, candidate_id: str) -> None:
        """Private replacement seam: switch coordinator authority store id.

        Called by recovery/publication only after the candidate generation
        is durably published (GENERATION_PUBLISHED journal, final active
        set revalidation, token consumption, and the activated-lineage
        marker).  Before that point the coordinator keeps the prior store
        id so cancellation and rollback rehydrate the prior authority.
        The leading underscore keeps this authority mutation off the
        public port surface: call-site discipline is replaced by the
        module-private protocol name.
        """

        if type(candidate_id) is not str:
            raise TypeError("candidate store id must be a built-in string")
        if not candidate_id.strip():
            raise ValueError("candidate store id must not be empty")
        self._coordinator._canonical_store_id = candidate_id

    def drain_for_transition(self) -> None:
        """Stop new leases, drain live leases, prove the view, then ACTIVATING.

        Recovery and rollback call this port operation while the coordinator
        condition is held and before any journal/disk mutation.  ``READY ->
        DRAINING`` makes ``_operation_lease`` reject new leases, the
        condition is released by ``wait`` so in-flight leases can complete
        and decrement, and the view/generation is proven unchanged before
        ``ACTIVATING``.  A drain timeout (or an observed view change)
        restores ``READY`` and notifies waiters, leaving the resource in a
        coherent non-transition state.
        """

        with self._coordinator._condition:
            if (
                self._coordinator._state != "READY"
                or self._coordinator._preparation is not None
                or self._coordinator._cleanup_reservation is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.CONCURRENT_PREPARATION",
                    retryable=True,
                )
            initial_view = self._coordinator._view
            initial_generation = (
                None
                if initial_view is None
                else initial_view.generation
            )
            self._coordinator._state = "DRAINING"
            self._coordinator._condition.notify_all()
            deadline = (
                time.monotonic() + self._coordinator._drain_timeout_seconds
            )
            try:
                while self._coordinator._active_lease_count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActivationPreparationError(
                            "ACTIVATION.DRAIN_TIMEOUT",
                            retryable=True,
                        )
                    self._coordinator._condition.wait(remaining)
                current_view = self._coordinator._view
                current_generation = (
                    None
                    if current_view is None
                    else current_view.generation
                )
                if (
                    current_view is not initial_view
                    or current_generation != initial_generation
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
            except BaseException:
                self._coordinator._state = "READY"
                self._coordinator._condition.notify_all()
                raise
            self._coordinator._state = "ACTIVATING"
            self._coordinator._condition.notify_all()

    def open_configured_connection(
        self,
        database_path: Path,
        *,
        require_existing: bool = False,
    ) -> Any:
        return _open_configured_connection(
            database_path,
            require_existing=require_existing,
        )

    def read_meta(self, connection: sqlite3.Connection) -> dict[str, str]:
        return _read_meta(connection)

    def meta_int(self, meta: dict[str, str], key: str) -> int:
        return _meta_int(meta, key)

    def meta_bool(self, meta: dict[str, str], key: str) -> bool:
        return _meta_bool(meta, key)

    def read_source_binding_facts(
        self,
        connection: sqlite3.Connection,
        lease: _SQLiteGenerationView,
    ) -> Any:
        return _read_source_binding_facts(connection, lease)

    def read_source_binding_facts_in_transaction(
        self,
        connection: sqlite3.Connection,
        lease: _SQLiteGenerationView,
    ) -> Any:
        return _read_source_binding_facts_in_transaction(connection, lease)

    def binding_from_ledger_row(
        self,
        row: tuple[object, ...],
    ) -> SnapshotBinding:
        return _binding_from_ledger_row(row)

    def configured_pair_diagnostics(
        self,
        binding: SnapshotBinding,
        *,
        identity: CanonicalResourceIdentity,
        canonical_store_id: str,
        head_revision: int,
        cumulative_record_counts: tuple[tuple[int, int], ...],
    ) -> tuple[str, ...]:
        diagnostics = _configured_pair_diagnostics(
            binding,
            identity=identity,
            canonical_store_id=canonical_store_id,
            head_revision=head_revision,
            cumulative_record_counts=cumulative_record_counts,
        )
        if any(
            code in {"SOURCE_BINDING.JSONL_UNSAFE", "SOURCE_BINDING.MANIFEST_UNSAFE"}
            for code in diagnostics
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        return diagnostics

    def table_count(self, connection: sqlite3.Connection, table_name: str) -> int:
        return _table_count(connection, table_name)

    def validate_store_identity(
        self,
        connection: sqlite3.Connection,
        *,
        resource_id: str,
        canonical_store_id: str,
        target_identity: str,
    ) -> None:
        _validate_store_identity(
            connection,
            resource_id=resource_id,
            canonical_store_id=canonical_store_id,
            target_identity=target_identity,
        )

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
    ) -> Any:
        return inspect_stage_schema(
            stage_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=_allow_diverged_runtime,
            _allow_sealed=_allow_sealed,
            _allow_active=_allow_active,
            _expected_active_generation=_expected_active_generation,
            _expected_activation_digest=_expected_activation_digest,
        )

    def unique_character_ngrams(
        self,
        folded_text: str,
        gram_size: int,
    ) -> tuple[str, ...]:
        return unique_character_ngrams(folded_text, gram_size)

    def validate_candidate_proof_index(
        self,
        connection: sqlite3.Connection,
        *,
        required_sizes: tuple[int, ...],
        fts5_available: bool,
    ) -> tuple[tuple[tuple[int, int], ...], int]:
        try:
            return validate_candidate_proof_index(
                connection,
                required_sizes=required_sizes,
                fts5_available=fts5_available,
            )
        except (CandidateProofIndexError, sqlite3.DatabaseError) as error:
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_INDEX_INVALID"
            ) from error

    def write_journal(
        self,
        record: _ActivationJournalRecord,
        journal_path: Path,
        *,
        expected_final_identity: _ActivationFileIdentity | None,
    ) -> _ActivationJournalHandle:
        return self._coordinator._write_activation_journal_locked(
            record,
            journal_path,
            expected_final_identity=expected_final_identity,
        )

    def write_terminal(
        self,
        record: _ActivationJournalRecord,
    ) -> _ActivationFileIdentity:
        return self._coordinator._write_activation_terminal_locked(record)

    @property
    def stage_seal_error(self) -> type[RuntimeError]:
        """The registry's StageSealError type used by readiness/token seams."""

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        return stage_seal_error

    def validate_stage_facts(
        self,
        stage_ref: Any,
        *,
        canonical_store_id: str,
    ) -> Any:
        """Re-prove one SEALED stage's seal facts from disk (sealer seam)."""

        stage_sealer = importlib.import_module("tm_stage_sealer")
        return stage_sealer._validate_stage_facts(
            cast(Any, stage_ref),
            canonical_store_id=canonical_store_id,
            allow_sealed=True,
        )

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

        stage_sealer = importlib.import_module("tm_stage_sealer")
        return stage_sealer._accepted_jsonl_row(payload)

    def build_sealed_binding(
        self,
        identity: CanonicalResourceIdentity,
        receipt: SnapshotReceipt,
        manifest: SnapshotManifest,
    ) -> SnapshotBinding:
        """Deterministic MIGRATION_SOURCE snapshot binding construction."""

        stage_sealer = importlib.import_module("tm_stage_sealer")
        return stage_sealer._build_binding(identity, receipt, manifest)

    def build_seal_evidence(
        self,
        facts: Any,
        binding: SnapshotBinding,
        *,
        stage_file_digest: str,
        manifest_temp_digest: str,
    ) -> Any:
        """Reconstruct one sealed stage's validation evidence from facts."""

        stage_sealer = importlib.import_module("tm_stage_sealer")
        return stage_sealer._build_evidence(
            facts,
            binding,
            stage_file_digest=stage_file_digest,
            manifest_temp_digest=manifest_temp_digest,
        )

    def stage_closure_digest(
        self,
        connection: sqlite3.Connection,
    ) -> str:
        stage_sealer = importlib.import_module("tm_stage_sealer")
        return cast(str, stage_sealer._stage_closure_digest(connection))

    def active_transition_closure_digests(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[str, str]:
        stage_sealer = importlib.import_module("tm_stage_sealer")
        return cast(
            tuple[str, str],
            stage_sealer._active_transition_closure_digests(connection),
        )

    def advance_after_effect(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
        *,
        next_generation: int | None = None,
        activation_digest: str | None = None,
        active_content_attestation: Any | None = None,
    ) -> _ActivationJournalHandle:
        return self._coordinator._advance_activation_journal_after_effect_locked(
            preparation,
            handle,
            next_phase,
            next_generation=next_generation,
            activation_digest=activation_digest,
            active_content_attestation=active_content_attestation,
        )


_SCHEMA_UPGRADE_TICKET_FACTORY_KEY = object()


@dataclass(frozen=True)
class _SchemaUpgradeLocatorSnapshot:
    """One coordinator-captured byte-exact prior-store evidence copy.

    Created while the resource is drained beside the Design-required
    ``Connection.backup()`` recovery backup.  The backup is a valid
    reopenable snapshot whose byte digest is deliberately not required to
    equal the active DB digest; this raw byte-exact copy is the only
    artifact whose digest can honestly back a ``RecoveryLocator`` bound to
    the prior active-store digest when a failure contract needs one.  It
    is deleted on success and on any failure that does not expose it, and
    the coordinator holds at most one at a time.
    """

    path: Path
    identity: tuple[int, int]
    digest: str

    def __post_init__(self) -> None:
        if type(self.path) is not _NATIVE_PATH_TYPE:
            raise TypeError("locator snapshot path is invalid")
        if not self.path.is_absolute():
            raise ValueError("locator snapshot path must be absolute")
        if (
            type(self.identity) is not tuple
            or len(self.identity) != 2
            or type(self.identity[0]) is not int
            or type(self.identity[1]) is not int
        ):
            raise TypeError("locator snapshot identity is invalid")
        if type(self.digest) is not str or len(self.digest) != 64:
            raise TypeError("locator snapshot digest is invalid")


@dataclass(frozen=True)
class _SchemaUpgradeSnapshotTicket:
    """Opaque single-use schema-upgrade stabilization ticket (Task 5.11).

    Only :meth:`ResourceStoreCoordinator.prepare_schema_upgrade_ticket`
    can mint one: the factory key is module-private, and the ticket binds
    the coordinator owner nonce, resource, canonical store id, generation,
    head revision, the stabilized prior DB identity/digest, and the
    recovery backup path/identity/digest captured while the coordinator
    held the resource drained.  The activation guard consumes the ticket
    before any journal is written; a stale or foreign ticket can never be
    activated.
    """

    _owner_nonce: str
    resource_id: str
    canonical_store_id: str
    generation: int
    head_revision: int
    db_identity: tuple[int, int]
    db_digest: str
    backup_path: Path
    backup_identity: tuple[int, int]
    backup_digest: str
    _factory_key: object

    def __post_init__(self) -> None:
        if self._factory_key is not _SCHEMA_UPGRADE_TICKET_FACTORY_KEY:
            raise TypeError(
                "schema-upgrade snapshot tickets require the module-private "
                "factory"
            )
        if type(self._owner_nonce) is not str or not self._owner_nonce:
            raise TypeError("ticket owner nonce is invalid")
        if type(self.resource_id) is not str or not self.resource_id.strip():
            raise TypeError("ticket resource id is invalid")
        if (
            type(self.canonical_store_id) is not str
            or not self.canonical_store_id.strip()
        ):
            raise TypeError("ticket canonical store id is invalid")
        if (
            type(self.generation) is not int
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise TypeError("ticket generation is invalid")
        if (
            type(self.head_revision) is not int
            or isinstance(self.head_revision, bool)
            or self.head_revision < 0
        ):
            raise TypeError("ticket head revision is invalid")
        if (
            type(self.db_identity) is not tuple
            or len(self.db_identity) != 2
            or type(self.db_identity[0]) is not int
            or type(self.db_identity[1]) is not int
        ):
            raise TypeError("ticket database identity is invalid")
        if type(self.db_digest) is not str or len(self.db_digest) != 64:
            raise TypeError("ticket database digest is invalid")
        if type(self.backup_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("ticket backup path is invalid")
        if not self.backup_path.is_absolute():
            raise ValueError("ticket backup path must be absolute")
        if (
            type(self.backup_identity) is not tuple
            or len(self.backup_identity) != 2
            or type(self.backup_identity[0]) is not int
            or type(self.backup_identity[1]) is not int
        ):
            raise TypeError("ticket backup identity is invalid")
        if type(self.backup_digest) is not str or len(self.backup_digest) != 64:
            raise TypeError("ticket backup digest is invalid")


def _rehydrate_runtime_authority(
    port: _StoreValidationPort,
) -> ActivationRecoveryReport | None:
    """Rehydrate the durable activation authority for one fresh facade open.

    Task 6.1/6.2 cold-open seam.  A *completed* activation (a durable
    ``GENERATION_PUBLISHED`` journal or terminal) is re-proven at the
    authority, canonical-generation, and lineage level and hydrated as the
    one in-memory view, without re-requiring the activation-time snapshot
    parity: a legitimate canonical append/import advances the head past
    the completed binding (``VERIFIED_HISTORY``) and an externally changed
    configured JSONL/manifest (``SOURCE_DIVERGED``) are both healthy
    runtime states that ``SourceBindingMonitor`` derives only after the
    generation is restored.  A pending activation (``PREPARED`` /
    ``DB_REPLACED`` / ``MANIFEST_PUBLISHED``), an unclosed or tampered
    authority, or any journal/terminal temp or coexistence anomaly still
    goes through the strict Task 5.8/5.9 protocol unchanged, so incomplete
    activations and canonical corruption keep their recovery-or-fail-stop
    semantics and JSONL is never an implicit fallback.
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
    if port.state == "READY":
        port.drain_for_transition()
    try:
        identity = port.resource_identity
        journal_path = _activation_journal_path(identity)
        terminal_path = _activation_terminal_path(identity)
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
        if (
            _lstat_any_entry(_activation_journal_temp_path(journal_path))
            or _lstat_any_entry(_activation_terminal_temp_path(terminal_path))
            or (journal_identity is not None and terminal_identity is not None)
        ):
            return recover_durable_activation(port)
        if journal_identity is not None:
            record = _load_recovery_journal(port,
                journal_path,
                journal_identity,
            )
            _revalidate_recovery_authority(port, record)
            if (
                record.phase
                is not _ActivationJournalPhase.GENERATION_PUBLISHED
            ):
                return recover_durable_activation(port)
            report = _rehydrate_completed_activation(port, record)
            port.state = "READY"
            port.notify_all()
            return report
        if terminal_identity is not None:
            record = _load_recovery_terminal(port,
                terminal_path,
                terminal_identity,
            )
            _revalidate_recovery_authority(port, record)
            if (
                record.phase
                is not _ActivationJournalPhase.GENERATION_PUBLISHED
            ):
                return recover_durable_activation(port)
            report = _rehydrate_completed_activation(port, record)
            port.state = "READY"
            port.notify_all()
            return report
        return recover_durable_activation(port)
    except BaseException:
        port.notify_all()
        raise


def _rehydrate_completed_portable_authority(
    port: _CoordinatorStorePort,
    *,
    platform: PlatformFileBackend,
    persistent_private: PersistentPrivateProof,
    descendant_inspection: LockedDescendantNamespaceInspection,
    caller_borrow: _CallerHeldPortableJournalBorrow,
) -> ActivationRecoveryReport | None:
    """Read-only cold hydration for one complete Windows portable chain.

    This entry cannot advance, cancel, retire, or create any durable fact.  A
    portable namespace is accepted only when its authenticated publication
    chain is already the exact three-record prefix ending at
    ``GENERATION_PUBLISHED``, the stage pair is gone, and the lineage marker
    and current rooted canonical DB are complete.  The configured JSONL and
    adjacent manifest are post-publication observations: absence, replacement,
    or an unsafe shape is classified by ``SourceBindingMonitor`` after the
    canonical authority has been hydrated, rather than revoking that authority.
    """

    identity = port.resource_identity
    snapshot = _WindowsPortableFreshRecoveryOwner.inspect(
        identity=identity,
        canonical_store_id=port.canonical_store_id,
        backend=platform,
        persistent_private=persistent_private,
        descendant_inspection=descendant_inspection,
        caller_borrow=caller_borrow,
    )
    if snapshot.state == "NO_FACTS":
        return None
    port.state = "ACTIVATING"
    port.view = None
    port.notify_all()
    if (
        snapshot.state != "PENDING"
        or snapshot.highest_phase != "GENERATION_PUBLISHED"
        or type(snapshot.pending_record) is not _PortableActivationJournalRecord
        or len(snapshot.phase_records) != 3
        or tuple(
            record.unsigned.phase for record in snapshot.phase_records
        )
        != ("DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED")
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )

    prepared = snapshot.pending_record
    db_phase, manifest_phase, generation_phase = snapshot.phase_records
    unsigned = prepared.unsigned
    active = generation_phase.unsigned.active_content_attestation
    sealed = unsigned.sealed_content_attestation
    if (
        db_phase.unsigned.active_content_attestation is not None
        or type(active) is not PortableActiveContentAttestation
        or manifest_phase.unsigned.active_content_attestation != active
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )

    root, lease = caller_borrow._fresh_recovery_authorities(
        platform,
        persistent_private,
        identity,
    )
    stage_names = (
        unsigned.candidate_stage_db_name,
        unsigned.candidate_manifest_temp_name,
    )
    marker_path = _activation_lineage_marker_path(identity)
    marker_name = marker_path.name
    marker_temp_name = _activation_lineage_marker_temp_path(marker_path).name
    if (
        any(root.inspect_entry(name) is not None for name in stage_names)
        or root.inspect_entry(marker_name) is None
        or root.inspect_entry(marker_temp_name) is not None
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_REQUIRED",
            retryable=True,
        )

    authorities: list[object] = []
    canonical_authorities: dict[str, BoundRegularFile] = {}
    quarantine_authorities: dict[
        str,
        tuple[BoundRegularFile, BoundContentFacts],
    ] = {}
    active_error: BaseException | None = None
    staged_view: _SQLiteGenerationView | None = None
    try:
        quarantine_name = _portable_initial_stage_quarantine_name(
            identity,
            unsigned,
        )
        quarantine_root = platform.bind_parent(
            root,
            PurePath(
                _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                "completed-runtime-placeholder",
            ),
        )
        authorities.append(quarantine_root)
        quarantine_entry = quarantine_root.inspect_entry(quarantine_name)
        if (
            quarantine_entry is None
            or quarantine_entry.identity.kind != "directory"
            or not quarantine_entry.reparse_free
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        quarantine_target = platform.bind_parent(
            root,
            PurePath(
                _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                quarantine_name,
                "completed-runtime-placeholder",
            ),
        )
        authorities.append(quarantine_target)
        expected_quarantine = {
            unsigned.candidate_stage_db_name:
                unsigned.sealed_content_attestation.database,
            unsigned.candidate_manifest_temp_name:
                unsigned.sealed_content_attestation.manifest,
        }
        entries = descendant_inspection.observe_descendant_entries(
            root,
            lease,
            quarantine_target,
            LedgerEnumerationLimits(
                maximum_entries=2,
                maximum_name_bytes=4096,
                maximum_total_bytes=sum(
                    proof.size for proof in expected_quarantine.values()
                ),
            ),
        )
        if {entry.name for entry in entries} != set(expected_quarantine):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        for name, expected in expected_quarantine.items():
            opened = platform.open_regular(
                root,
                PurePath(
                    _PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                    quarantine_name,
                    name,
                ),
            )
            authorities.append(opened)
            facts = opened.content_facts()
            quarantine_authorities[name] = (opened, facts)
            if (
                facts.snapshot.identity.kind != "regular"
                or facts.snapshot.identity.link_count != 1
                or not facts.snapshot.reparse_free
                or port.portable_content_proof(facts) != expected
                or quarantine_target.inspect_entry(name) != facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )

        observed: dict[str, tuple[PortableContentFileProof, BoundContentFacts]] = {}
        for name in (
            identity.canonical_sidecar_path.name,
            marker_name,
        ):
            opened = platform.open_regular(root, PurePath(name))
            authorities.append(opened)
            canonical_authorities[name] = opened
            facts = opened.content_facts()
            if (
                facts.snapshot.identity.kind != "regular"
                or facts.snapshot.identity.link_count != 1
                or not facts.snapshot.reparse_free
                or root.inspect_entry(name) != facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            observed[name] = (port.portable_content_proof(facts), facts)

        reuse_semantic_facts = (
            observed[identity.canonical_sidecar_path.name][0]
            == active.database
        )

        def pair_matches_attestation(
            name: str,
            expected: PortableContentFileProof,
        ) -> bool:
            opened: BoundRegularFile | None = None
            matched = False
            close_failed = False
            try:
                entry = root.inspect_entry(name)
                if (
                    entry is not None
                    and entry.identity.kind == "regular"
                    and entry.identity.link_count == 1
                    and entry.reparse_free
                ):
                    opened = platform.open_regular(root, PurePath(name))
                    facts = opened.content_facts()
                    if (
                        facts.snapshot == entry
                        and root.inspect_entry(name) == facts.snapshot
                    ):
                        observed_proof = port.portable_content_proof(facts)
                        final_facts = opened.content_facts()
                        matched = (
                            final_facts == facts
                            and root.inspect_entry(name)
                            == final_facts.snapshot
                            and observed_proof == expected
                        )
            except (PlatformFileError, OSError):
                matched = False
            finally:
                active_exception = sys.exception()
                if opened is not None:
                    try:
                        opened.close()
                    except (PlatformFileError, OSError):
                        if active_exception is None:
                            close_failed = True
            return matched and not close_failed

        for name, expected in (
            (identity.snapshot_manifest_path.name, active.manifest),
            (identity.configured_jsonl_path.name, active.source),
        ):
            if not pair_matches_attestation(name, expected):
                reuse_semantic_facts = False

        if (
            active.manifest != sealed.manifest
            or active.source != sealed.source
            or canonical_authorities[marker_name].read_all()
            != _activation_lineage_marker_payload(identity)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        activation_digest = _portable_recovery_activation_digest(prepared)
        if (
            active.sealed_attestation_digest
            != unsigned.sealed_content_attestation.attestation_digest
            or active.journal_id != unsigned.journal_id
            or active.resource_id != identity.resource_id
            or active.target_identity != identity.target_identity
            or active.canonical_store_id != port.canonical_store_id
            or active.snapshot_receipt_digest
            != unsigned.snapshot_receipt_digest
            or active.generation != 0
            or active.activation_digest != activation_digest
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )

        active_ref = _canonical_activation_ref(
            identity,
            journal_id=unsigned.journal_id,
        )
        schema = _inspect_completed_active_schema_read_only(
            active_ref,
            canonical_store_id=port.canonical_store_id,
            expected_generation=0,
            expected_activation_digest=activation_digest,
        )
        semantic = active.semantic_facts
        if (
            schema.schema_version != semantic.schema_version
            or schema.fold_version != semantic.fold_version
            or schema.candidate_index_version != semantic.index_version
            or schema.candidate_index_kind != semantic.candidate_index_kind
            or schema.fts5_available != semantic.fts5_available
            or schema.sqlite_runtime_version != semantic.sqlite_runtime_version
            or schema.unicode_runtime_version != semantic.unicode_runtime_version
            or schema.journal_mode != semantic.journal_mode
            or schema.synchronous != semantic.synchronous
            or schema.foreign_keys != semantic.foreign_keys
            or schema.busy_timeout_ms != semantic.busy_timeout_ms
            or schema.wal_enabled != semantic.wal_enabled
            or schema.extension_loading_enabled
            != semantic.extension_loading_enabled
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        staged_view = _SQLiteGenerationView(
            stage=active_ref,
            canonical_store_id=port.canonical_store_id,
            generation=0,
            fts5_available=schema.fts5_available,
            active_content_attestation=active,
        )
        with _open_completed_authority_read_connection(
            identity.canonical_sidecar_path
        ) as connection:
            connection.execute("BEGIN")
            try:
                port.validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=port.canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if not reuse_semantic_facts and (
                    connection.execute("PRAGMA integrity_check").fetchall()
                    != [("ok",)]
                    or connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                ):
                    raise port.store_schema_error("STORE.INTEGRITY_CHECK_FAILED")
                facts = port.read_source_binding_facts_in_transaction(
                    connection,
                    staged_view,
                )
                binding = facts.binding
                if binding is None or facts.diagnostic_codes:
                    raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
                receipt_row, receipt_status = _recovery_receipt_row(
                    port,
                    connection,
                    expected_snapshot_id=binding.receipt.snapshot_id,
                )
                if (
                    receipt_status != "completed"
                    or receipt_row != binding.receipt
                ):
                    raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
                try:
                    _validate_binding_identity(
                        binding,
                        identity=identity,
                        canonical_store_id=port.canonical_store_id,
                    )
                except (TypeError, ValueError):
                    raise port.store_schema_error(
                        "STORE.ACTIVE_BINDING_INVALID"
                    ) from None
                record_count_at_revision = {0: 0}
                record_count_at_revision.update(
                    facts.cumulative_record_counts
                )
                if (
                    binding.receipt.exported_revision > facts.head_revision
                    or record_count_at_revision.get(
                        binding.receipt.exported_revision
                    )
                    != binding.receipt.record_count
                ):
                    raise port.store_schema_error("STORE.ACTIVE_BINDING_INVALID")
                if not reuse_semantic_facts:
                    port.validate_candidate_proof_index(
                        connection,
                        required_sizes=(
                            (1, 2) if schema.fts5_available else (1, 2, 3)
                        ),
                        fts5_available=schema.fts5_available,
                    )
            finally:
                connection.rollback()

        caller_borrow.reprove(platform, identity)
        quarantine_root.reprove()
        quarantine_target.reprove()
        if (
            _WindowsPortableFreshRecoveryOwner.inspect(
                identity=identity,
                canonical_store_id=port.canonical_store_id,
                backend=platform,
                persistent_private=persistent_private,
                descendant_inspection=descendant_inspection,
                caller_borrow=caller_borrow,
            )
            != snapshot
            or any(root.inspect_entry(name) is not None for name in stage_names)
            or root.inspect_entry(marker_temp_name) is not None
            or _completed_authority_sqlite_sidecar_present(
                identity.canonical_sidecar_path
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        for name, authority in canonical_authorities.items():
            facts = authority.content_facts()
            if (
                facts != observed[name][1]
                or root.inspect_entry(name) != facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
        for name, (authority, prior_facts) in quarantine_authorities.items():
            facts = authority.content_facts()
            if (
                facts != prior_facts
                or quarantine_target.inspect_entry(name) != facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
    except BaseException as error:
        active_error = error
    finally:
        close_error: BaseException | None = None
        for authority in reversed(authorities):
            try:
                authority.close()
            except BaseException as error:
                if close_error is None:
                    close_error = error
        if active_error is None and close_error is not None:
            active_error = close_error
    if active_error is not None:
        if isinstance(active_error, (PlatformFileError, OSError)):
            normalized = ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
            normalized.__cause__ = active_error
            active_error = normalized
        port.view = None
        port.state = "ACTIVATING"
        port.notify_all()
        raise active_error
    if staged_view is None:
        raise AssertionError("portable completed hydration produced no view")
    port.view = staged_view
    port.state = "READY"
    port.notify_all()
    return ActivationRecoveryReport(
        phase="GENERATION_PUBLISHED",
        action="COMPLETED",
        generation=0,
    )


@contextmanager
def _open_completed_authority_read_connection(
    database_path: Path,
) -> Iterator[sqlite3.Connection]:
    """Open one target DB without locking or SQLite journal recovery.

    ``immutable=1`` is safe only in this completion-only proof: exact SQLite
    sidecars are rejected before the open and again before in-memory
    publication, while the canonical DB inode+bytes are captured around all
    semantic validation.  A sidecar or DB change in either TOCTOU window
    therefore rejects the proof; SQLite itself is never allowed to recover,
    delete, or otherwise act on transient rollback/WAL state.
    """

    _require_absolute_path(database_path, "database_path")
    if _completed_authority_sqlite_sidecar_present(database_path):
        raise SQLiteStoreSchemaError("STORE.SQLITE_SIDECAR_PRESENT")
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro&immutable=1",
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        uri=True,
    )
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if _pragma_int(connection, "query_only") != 1:
            raise SQLiteStoreSchemaError("STORE.QUERY_ONLY_DISABLED")
        if _pragma_text(connection, "journal_mode").lower() == "wal":
            raise SQLiteStoreSchemaError("STORE.WAL_FORBIDDEN")
        yield connection
    finally:
        connection.close()


def _completed_authority_sqlite_sidecar_present(
    database_path: Path,
) -> bool:
    """Detect exact SQLite rollback/WAL sidecars without opening the DB."""

    return any(
        _lstat_any_entry(Path(f"{database_path}{suffix}"))
        for suffix in ("-journal", "-wal", "-shm")
    )


def _inspect_completed_active_schema_read_only(
    stage: _StoreRuntimeRef,
    *,
    canonical_store_id: str,
    expected_generation: int,
    expected_activation_digest: str,
) -> SQLiteSchemaSnapshot:
    """Validate the active schema without opening the target writable."""

    validated_stage = _require_inspectable_store_ref(stage)
    _require_identity(canonical_store_id, "canonical_store_id")
    if not validated_stage.staged_db_path.is_file():
        raise SQLiteStoreSchemaError("STORE.DATABASE_MISSING")
    with _open_completed_authority_read_connection(
        validated_stage.staged_db_path
    ) as connection:
        connection.execute("BEGIN")
        try:
            meta = _read_meta(connection)
            schema_version = _meta_int(meta, "schema_version")
            if schema_version > TM_SCHEMA_VERSION:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_TOO_NEW")
            if schema_version != TM_SCHEMA_VERSION:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_UNSUPPORTED")
            identity = validated_stage.resource_identity
            if (
                meta["resource_id"] != identity.resource_id
                or meta["canonical_store_id"] != canonical_store_id
                or meta["target_identity"] != identity.target_identity
            ):
                raise SQLiteStoreSchemaError("STORE.IDENTITY_MISMATCH")
            runtime = detect_sqlite_runtime()
            _validate_stage_meta(
                meta,
                runtime=runtime,
                allow_diverged_runtime=True,
                allow_active=True,
                expected_active_generation=expected_generation,
                expected_activation_digest=expected_activation_digest,
            )
            table_names = _schema_object_names(connection, "table")
            index_names = _schema_object_names(connection, "index")
            expected_tables = set(_BASE_TABLES)
            fts5_available = _meta_bool(meta, "fts5_available")
            if fts5_available:
                expected_tables.add("tm_fts")
            expected_physical_tables = set(expected_tables)
            if fts5_available:
                expected_physical_tables.update(_FTS5_SHADOW_TABLES)
            if not expected_physical_tables.issubset(table_names):
                raise SQLiteStoreSchemaError("STORE.SCHEMA_INCOMPLETE")
            if table_names != expected_physical_tables:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_UNEXPECTED")
            if not _BASE_INDEXES.issubset(index_names):
                raise SQLiteStoreSchemaError("STORE.SCHEMA_INCOMPLETE")
            if index_names != _BASE_INDEXES:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_UNEXPECTED")
            _validate_schema_object_types(connection)
            approved_schema_digest = _APPROVED_SCHEMA_DIGESTS[
                fts5_available
            ]
            if (
                meta["schema_digest"] != approved_schema_digest
                or _schema_digest(
                    connection,
                    fts5_available=fts5_available,
                    legacy_schema=False,
                )
                != approved_schema_digest
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.TABLE_SCHEMA_MISMATCH"
                )
            _validate_index_schema(connection, legacy_schema=False)
            _validate_foreign_key_schema(connection, legacy_schema=False)
            if fts5_available != runtime.fts5_available:
                raise SQLiteStoreSchemaError(
                    "STORE.RUNTIME_CAPABILITY_CHANGED"
                )
            if meta["sqlite_runtime_version"] != runtime.sqlite_version:
                raise SQLiteStoreSchemaError("STORE.SQLITE_RUNTIME_CHANGED")
            if meta["unicode_runtime_version"] != runtime.unicode_version:
                raise SQLiteStoreSchemaError(
                    "STORE.UNICODE_RUNTIME_MISMATCH"
                )
            journal_mode = _pragma_text(
                connection,
                "journal_mode",
            ).lower()
            synchronous_value = _pragma_int(connection, "synchronous")
            synchronous = {
                0: "OFF",
                1: "NORMAL",
                2: "FULL",
                3: "EXTRA",
            }.get(synchronous_value, f"UNKNOWN_{synchronous_value}")
            snapshot = SQLiteSchemaSnapshot(
                schema_version=schema_version,
                resource_id=meta["resource_id"],
                canonical_store_id=meta["canonical_store_id"],
                target_identity=meta["target_identity"],
                generation=_meta_int(meta, "generation"),
                head_revision=_meta_int(meta, "head_revision"),
                fold_version=meta["fold_version"],
                scorer_version=meta["scorer_version"],
                text_semantics_version=meta["text_semantics_version"],
                candidate_index_kind=meta["candidate_index_kind"],
                candidate_index_version=meta["candidate_index_version"],
                sqlite_runtime_version=meta["sqlite_runtime_version"],
                unicode_runtime_version=meta["unicode_runtime_version"],
                fts5_available=fts5_available,
                journal_mode=journal_mode,
                synchronous=synchronous,
                foreign_keys=_pragma_int(connection, "foreign_keys") == 1,
                busy_timeout_ms=_pragma_int(connection, "busy_timeout"),
                wal_enabled=journal_mode == "wal",
                extension_loading_enabled=False,
                activation_status=meta["activation_status"],
                activation_digest=meta.get("activation_digest"),
                fuzzy_available=False,
                table_names=tuple(sorted(expected_tables)),
                index_names=tuple(sorted(_BASE_INDEXES)),
            )
        finally:
            connection.rollback()
    return snapshot


def _prove_and_hydrate_completed_activation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
) -> tuple[ActivationRecoveryReport, _SQLiteGenerationView]:
    """Re-prove and stage one completed generation for general recovery.

    Proves the canonical generation and lineage from the authenticated
    record plus disk: the ACTIVE sidecar schema with the record-derived
    generation/activation digest, store identity, integrity, foreign keys,
    the completed ledger binding/receipt closure, and the candidate index
    closure.  The binding receipt is *not* required to equal the current
    head, and the configured JSONL/manifest are *not* compared to the
    ledger here: ``SourceBindingMonitor`` derives ``VERIFIED_CURRENT`` /
    ``VERIFIED_HISTORY`` / ``SOURCE_DIVERGED`` from the restored
    generation on first observation.  Any unproven canonical fact raises
    instead of ever falling back to JSONL.
    """

    identity = port.resource_identity
    if record.phase is not _ActivationJournalPhase.GENERATION_PUBLISHED:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            retryable=False,
        )
    if (
        _lstat_any_entry(record.candidate_stage_db_path)
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
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
    active_attestation = _require_bound_active_content_attestation(
        record,
        canonical_store_id=record.canonical_store_id,
        next_generation=next_generation,
        activation_digest=activation_digest,
    )
    try:
        database = _capture_content_file(identity.canonical_sidecar_path)
    except ContentAttestationError as error:
        code = (
            "ACTIVATION.ACTIVE_ATTESTATION_ASSET_MISSING"
            if error.error_code == "CONTENT_ATTESTATION.FILE_MISSING"
            else "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID"
        )
        raise ActivationPreparationError(code, retryable=False) from error
    if (database.device, database.inode) != (
        active_attestation.database.device,
        active_attestation.database.inode,
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        )
    database_is_attested = database == active_attestation.database
    if database_is_attested:
        active_ref, snapshot = _revalidate_active_content_attestation(
            port,
            record,
            identity=identity,
            canonical_store_id=record.canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
            # A completed canonical generation remains last-known-good
            # authority when its configured JSONL/manifest pair later
            # diverges. SourceBindingMonitor owns that comparison.
            require_configured_pair=False,
        )
    else:
        # A legitimate post-activation append keeps the canonical inode but
        # changes its bytes.  The attestation can no longer authorize the
        # semantic fast path, so cold rehydrate falls back to the complete
        # runtime validator.  A foreign inode was rejected above.
        active_ref = _canonical_activation_ref(
            identity,
            journal_id=record.journal_id,
        )
        snapshot = port.inspect_stage_schema(
            active_ref,
            canonical_store_id=record.canonical_store_id,
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
                canonical_store_id=record.canonical_store_id,
                target_identity=identity.target_identity,
            )
            if not database_is_attested:
                if connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall() != [("ok",)]:
                    raise port.store_schema_error(
                        "STORE.INTEGRITY_CHECK_FAILED"
                    )
                if connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall():
                    raise port.store_schema_error(
                        "STORE.FOREIGN_KEY_CHECK_FAILED"
                    )
            lease = _SQLiteGenerationView(
                stage=active_ref,
                canonical_store_id=record.canonical_store_id,
                generation=next_generation,
                fts5_available=snapshot.fts5_available,
            )
            facts = port.read_source_binding_facts_in_transaction(
                connection,
                lease,
            )
            binding = facts.binding
            if binding is None or facts.diagnostic_codes:
                raise port.store_schema_error(
                    "STORE.ACTIVE_BINDING_INVALID"
                )
            if (
                binding.receipt.snapshot_id != record.new_receipt_id
                or snapshot_receipt_digest(binding.receipt)
                != record.snapshot_receipt_digest
                or binding.receipt.jsonl_digest
                != record.source_jsonl_digest
            ):
                raise port.store_schema_error(
                    "STORE.ACTIVE_BINDING_INVALID"
                )
            if not database_is_attested:
                _recover_activation_indexes(
                    port,
                    connection,
                    fts5_available=snapshot.fts5_available,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    view = _SQLiteGenerationView(
        stage=active_ref,
        canonical_store_id=record.canonical_store_id,
        generation=next_generation,
        fts5_available=snapshot.fts5_available,
        active_content_attestation=record.active_content_attestation,
    )
    return (
        ActivationRecoveryReport(
            phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
            action="COMPLETED",
            generation=next_generation,
        ),
        view,
    )


def _prove_completed_initial_authority_read_only(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
) -> tuple[
    ActivationRecoveryReport,
    _SQLiteGenerationView,
    ContentFileProof,
]:
    """Stage one completed gen0 view through read-only SQLite validation."""

    identity = port.resource_identity
    if record.phase is not _ActivationJournalPhase.GENERATION_PUBLISHED:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            retryable=False,
        )
    if (
        _lstat_any_entry(record.candidate_stage_db_path)
        or _lstat_any_entry(record.candidate_manifest_temp_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        )
    activation_digest = _activation_publication_digest(
        record,
        next_generation=0,
    )
    active_attestation = _require_bound_active_content_attestation(
        record,
        canonical_store_id=record.canonical_store_id,
        next_generation=0,
        activation_digest=activation_digest,
    )
    try:
        database_before = _capture_content_file(
            identity.canonical_sidecar_path
        )
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        ) from error
    if (database_before.device, database_before.inode) != (
        active_attestation.database.device,
        active_attestation.database.inode,
    ):
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        )
    active_ref = _canonical_activation_ref(
        identity,
        journal_id=record.journal_id,
    )
    snapshot = _inspect_completed_active_schema_read_only(
        active_ref,
        canonical_store_id=record.canonical_store_id,
        expected_generation=0,
        expected_activation_digest=activation_digest,
    )
    with _open_completed_authority_read_connection(
        identity.canonical_sidecar_path
    ) as connection:
        connection.execute("BEGIN")
        try:
            port.validate_store_identity(
                connection,
                resource_id=identity.resource_id,
                canonical_store_id=record.canonical_store_id,
                target_identity=identity.target_identity,
            )
            if connection.execute(
                "PRAGMA integrity_check"
            ).fetchall() != [("ok",)]:
                raise port.store_schema_error(
                    "STORE.INTEGRITY_CHECK_FAILED"
                )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise port.store_schema_error(
                    "STORE.FOREIGN_KEY_CHECK_FAILED"
                )
            staged_view = _SQLiteGenerationView(
                stage=active_ref,
                canonical_store_id=record.canonical_store_id,
                generation=0,
                fts5_available=snapshot.fts5_available,
            )
            facts = port.read_source_binding_facts_in_transaction(
                connection,
                staged_view,
            )
            binding = facts.binding
            if binding is None or facts.diagnostic_codes:
                raise port.store_schema_error(
                    "STORE.ACTIVE_BINDING_INVALID"
                )
            if (
                binding.receipt.snapshot_id != record.new_receipt_id
                or snapshot_receipt_digest(binding.receipt)
                != record.snapshot_receipt_digest
                or binding.receipt.jsonl_digest
                != record.source_jsonl_digest
            ):
                raise port.store_schema_error(
                    "STORE.ACTIVE_BINDING_INVALID"
                )
            port.validate_candidate_proof_index(
                connection,
                required_sizes=(
                    (1, 2)
                    if snapshot.fts5_available
                    else (1, 2, 3)
                ),
                fts5_available=snapshot.fts5_available,
            )
        finally:
            connection.rollback()
    try:
        database_after = _capture_content_file(
            identity.canonical_sidecar_path
        )
    except ContentAttestationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_IDENTITY_INVALID",
            retryable=False,
        ) from error
    if database_after != database_before:
        raise ActivationPreparationError(
            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
            retryable=False,
        )
    view = _SQLiteGenerationView(
        stage=active_ref,
        canonical_store_id=record.canonical_store_id,
        generation=0,
        fts5_available=snapshot.fts5_available,
        active_content_attestation=record.active_content_attestation,
    )
    return (
        ActivationRecoveryReport(
            phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
            action="COMPLETED",
            generation=0,
        ),
        view,
        database_after,
    )


def _rehydrate_completed_activation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
) -> ActivationRecoveryReport:
    """Complete cleanup after proving one published canonical generation."""

    report, view = _prove_and_hydrate_completed_activation(port, record)
    port.view = view
    identity = port.resource_identity
    _ensure_activation_lineage_marker(identity)
    port._activate_candidate_store_id(record.canonical_store_id)
    _remove_journal_proven_backups(record)
    return report


def _rehydrate_completed_initial_authority_only(
    port: _StoreValidationPort,
) -> ActivationRecoveryReport | None:
    """Read-only proof and in-memory hydration of completed generation zero.

    This narrow seam exists only for the case where the persistent initial-
    activation reservation cannot be acquired.  It never delegates to the
    general recovery engine: pending phases, temp artifacts, journal/terminal
    coexistence, a missing/unfinished lineage marker, and every non-initial
    generation return ``None`` without advancing, cancelling, cleaning, or
    rolling back durable facts.  Only one already-complete, exactly re-proven
    ``GENERATION_PUBLISHED`` initial record may hydrate the fresh in-memory
    coordinator.
    """

    if (
        port.state != "READY"
        or port.preparation is not None
        or port.cleanup_reservation is not None
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_STATE_INVALID",
            retryable=True,
        )
    identity = port.resource_identity
    journal_path = _activation_journal_path(identity)
    terminal_path = _activation_terminal_path(identity)
    marker_path = _activation_lineage_marker_path(identity)
    marker_temp_path = _activation_lineage_marker_temp_path(marker_path)
    journal_identity = _lstat_activation_journal_identity(journal_path)
    terminal_identity = _lstat_activation_terminal_identity(terminal_path)
    if (
        _lstat_any_entry(_activation_journal_temp_path(journal_path))
        or _lstat_any_entry(_activation_terminal_temp_path(terminal_path))
        or _lstat_any_entry(marker_temp_path)
        or _completed_authority_sqlite_sidecar_present(
            identity.canonical_sidecar_path
        )
        or (journal_identity is not None and terminal_identity is not None)
    ):
        return None
    selected_path: Path
    selected_identity: _ActivationFileIdentity
    if journal_identity is not None:
        selected_path = journal_path
        selected_identity = journal_identity
        record = _load_recovery_journal(
            port,
            selected_path,
            selected_identity,
        )
    elif terminal_identity is not None:
        selected_path = terminal_path
        selected_identity = terminal_identity
        record = _load_recovery_terminal(
            port,
            selected_path,
            selected_identity,
        )
    else:
        return None
    _revalidate_recovery_authority(port, record)
    if (
        record.phase is not _ActivationJournalPhase.GENERATION_PUBLISHED
        or record.expected_prior_generation is not None
        or record.had_prior_canonical
    ):
        return None
    marker_identity = _lstat_activation_lineage_marker_identity(marker_path)
    if marker_identity is None:
        return None
    _read_activation_lineage_marker(marker_path, identity=identity)
    report, staged_view, database_proof = (
        _prove_completed_initial_authority_read_only(
            port,
            record,
        )
    )
    # Re-prove every selected authority path after the potentially long DB
    # validation before exposing the in-memory view.  No cleanup is attempted
    # if any identity, phase, or closed-marker fact changed underneath us.
    if (
        _lstat_any_entry(_activation_journal_temp_path(journal_path))
        or _lstat_any_entry(_activation_terminal_temp_path(terminal_path))
        or _lstat_any_entry(marker_temp_path)
        or _completed_authority_sqlite_sidecar_present(
            identity.canonical_sidecar_path
        )
    ):
        return None
    if selected_path == journal_path:
        if (
            _lstat_activation_journal_identity(journal_path)
            != selected_identity
            or _lstat_activation_terminal_identity(terminal_path) is not None
        ):
            return None
        reproved_record = _load_recovery_journal(
            port,
            selected_path,
            selected_identity,
        )
    else:
        if (
            _lstat_activation_terminal_identity(terminal_path)
            != selected_identity
            or _lstat_activation_journal_identity(journal_path) is not None
        ):
            return None
        reproved_record = _load_recovery_terminal(
            port,
            selected_path,
            selected_identity,
        )
    _revalidate_recovery_authority(port, reproved_record)
    if reproved_record != record:
        return None
    if (
        _lstat_activation_lineage_marker_identity(marker_path)
        != marker_identity
    ):
        return None
    _read_activation_lineage_marker(marker_path, identity=identity)
    if _completed_authority_sqlite_sidecar_present(
        identity.canonical_sidecar_path
    ):
        return None
    try:
        if (
            _capture_content_file(identity.canonical_sidecar_path)
            != database_proof
        ):
            return None
    except ContentAttestationError:
        return None
    # Publish the fully staged in-memory authority only after every durable
    # record, marker, and database proof has closed.  The coordinator method
    # owns its condition lock, so the store-id/view pair becomes observable
    # as one transition and no failure path above can leak a partial gen0.
    port._activate_candidate_store_id(record.canonical_store_id)
    port.view = staged_view
    port.state = "READY"
    port.notify_all()
    return report


class ResourceStoreCoordinator:
    """Own one resource's leases, sealed registry, and activation authority."""

    def __init__(
        self,
        stage: MutableStageRef | None = None,
        *,
        canonical_store_id: str,
        resource_identity: CanonicalResourceIdentity | None = None,
        drain_timeout_seconds: float = 5.0,
        _allow_legacy_schema: bool = False,
        _allow_active: bool = False,
        _expected_active_generation: int | None = None,
        _expected_activation_digest: str | None = None,
    ) -> None:
        if (stage is None) == (resource_identity is None):
            raise TypeError(
                "exactly one active stage or unactivated resource identity "
                "is required"
            )
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        timeout = _require_timeout(drain_timeout_seconds)
        if type(_allow_legacy_schema) is not bool:
            raise TypeError(
                "_allow_legacy_schema must be a built-in bool"
            )
        if type(_allow_active) is not bool:
            raise TypeError("_allow_active must be a built-in bool")
        if _allow_active and (
            type(_expected_active_generation) is not int
            or isinstance(_expected_active_generation, bool)
            or _expected_active_generation < 0
        ):
            raise TypeError("active generation expectation is invalid")
        if _allow_active and (
            type(_expected_activation_digest) is not str
            or len(_expected_activation_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in _expected_activation_digest
            )
        ):
            raise TypeError("activation digest expectation is invalid")
        if stage is not None:
            private_stage = _snapshot_store_stage(stage)
            identity = private_stage.resource_identity
            snapshot = inspect_stage_schema(
                private_stage,
                canonical_store_id=canonical_store_id,
                _allow_diverged_runtime=True,
                _allow_legacy_schema=_allow_legacy_schema,
                _allow_active=_allow_active,
                _expected_active_generation=_expected_active_generation,
                _expected_activation_digest=_expected_activation_digest,
            )
            view: _SQLiteGenerationView | None = _SQLiteGenerationView(
                stage=private_stage,
                canonical_store_id=canonical_store_id,
                generation=snapshot.generation,
                fts5_available=snapshot.fts5_available,
            )
        else:
            identity = _snapshot_store_identity(resource_identity)
            view = None
        self._resource_identity = identity
        self._resource_id = identity.resource_id
        self._target_identity = identity.target_identity
        self._canonical_store_id = canonical_store_id
        self._condition = threading.Condition()
        self._state = "READY"
        self._active_lease_count = 0
        self._view = view
        self._drain_timeout_seconds = timeout
        registry_type = getattr(
            importlib.import_module("tm_stage_sealer"),
            "_SealedArtifactRegistry",
        )
        self._sealed_registry = registry_type(
            registry_namespace=f"coordinator.{identity.target_identity}",
        )
        self._preparation: _ActivationPreparation | None = None
        self._portable_replacement_preparation: (
            _PortableReplacementPreparation | None
        ) = None
        self._portable_replacement_current_record: (
            _PortableReplacementRecord | None
        ) = None
        self._cleanup_reservation: _ActivationCleanupReservation | None = None
        self._cleanup_in_progress = False
        # Process-local fail-stop for an initial activation whose unpublished
        # authority could not be proved clean or published.  Absence from the
        # canonical paths is not sufficient to clear it: a same-byte foreign
        # inode may still occupy a salted stage path outside those locators.
        # Only this coordinator's formal durable recovery paths clear the
        # latch after returning a proven recovery report.
        self._initial_activation_authority_unavailable = False
        self._owner_nonce = uuid.uuid4().hex
        self._schema_upgrade_ticket: _SchemaUpgradeSnapshotTicket | None = None
        self._schema_upgrade_locator_snapshot: (
            _SchemaUpgradeLocatorSnapshot | None
        ) = None
        self._refresh_gate_owner: int | None = None
        self._refresh_gate_depth = 0

    @property
    def resource_id(self) -> str:
        return self._resource_id

    @property
    def canonical_store_id(self) -> str:
        """The coordinator's current (active) canonical store id."""

        with self._condition:
            return self._canonical_store_id

    @property
    def active_store_path(self) -> Path | None:
        """The active canonical DB path, or None before first activation."""

        with self._condition:
            return (
                None
                if self._view is None
                else self._view.stage.staged_db_path
            )

    @property
    def current_generation(self) -> int | None:
        with self._condition:
            return None if self._view is None else self._view.generation

    def _mark_initial_activation_authority_unavailable(self) -> None:
        """Fail-stop later first-activation attempts on this coordinator."""

        with self._condition:
            self._initial_activation_authority_unavailable = True

    def _initial_activation_authority_is_unavailable(self) -> bool:
        """Return the coordinator-owned first-activation fail-stop state."""

        with self._condition:
            return self._initial_activation_authority_unavailable

    def _clear_initial_activation_fail_stop_after_recovery(
        self,
        report: ActivationRecoveryReport | None,
    ) -> None:
        """Clear only after a Core recovery path returned proven authority."""

        with self._condition:
            if report is not None:
                self._initial_activation_authority_unavailable = False

    def _seal_stage(
        self,
        mutable_stage: MutableStageRef,
        *,
        canonical_store_id: str,
        expected_prior_generation: int | None,
        schema_upgrade: bool = False,
        platform: object | None = None,
        caller_borrow: object | None = None,
    ) -> SealedStage:
        """Run the sole verified seal factory and pass caller-held authority."""

        stage_sealer = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealer",
        )
        if os.name == "nt" and (
            platform is None or caller_borrow is None
        ):
            stage_seal_error = getattr(
                importlib.import_module("tm_stage_sealer"),
                "StageSealError",
            )
            raise stage_seal_error("SEALER.ATTESTATION_UNAVAILABLE")
        return cast(SealedStage, stage_sealer(
            registry=self._sealed_registry,
            canonical_store_id=canonical_store_id,
            platform=platform,
        ).seal(
            mutable_stage,
            expected_prior_generation=expected_prior_generation,
            schema_upgrade=schema_upgrade,
            caller_borrow=caller_borrow,
        ))

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def durable_activation_phase(self) -> str | None:
        """The last durable activation journal phase, or None when absent.

        Read-only public seam for callers that must choose between
        Task 5.9 rollback (pending phases) and Task 5.8 recovery (a
        completed ``GENERATION_PUBLISHED`` journal) after a failed
        publication: the journal, when present, is re-read and re-parsed
        so a corrupt or foreign journal fails closed instead of being
        silently treated as absent.
        """

        with self._condition:
            journal_path = _activation_journal_path(
                self._resource_identity
            )
            try:
                journal_identity = _lstat_activation_journal_identity(
                    journal_path
                )
            except ActivationPreparationError:
                raise
            if journal_identity is None:
                return None
            try:
                disk_bytes, _disk_identity = _read_activation_journal_file(
                    journal_path,
                    journal_identity,
                )
                disk_record = _parse_activation_journal_bytes(
                    disk_bytes,
                    expected_journal_path=journal_path,
                )
            except ActivationPreparationError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                    reason_code=error.code,
                ) from error
            return disk_record.phase.value

    def wait_for_state(
        self,
        state: str,
        *,
        timeout_seconds: float,
    ) -> bool:
        if type(state) is not str or state not in {
            "READY",
            "DRAINING",
            "ACTIVATING",
            "FAILED",
        }:
            raise ValueError("state is invalid")
        timeout = _require_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._state != state:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    @contextmanager
    def _operation_lease(self) -> Iterator[_SQLiteGenerationView]:
        with self._condition:
            view = self._view
            if self._state != "READY":
                raise SQLiteStoreLifecycleError(
                    "STORE.RESOURCE_DRAINING",
                    resource_id=self._resource_id,
                    generation=0 if view is None else view.generation,
                    retryable=True,
                )
            if view is None:
                raise SQLiteStoreLifecycleError(
                    "STORE.CANONICAL_UNAVAILABLE",
                    resource_id=self._resource_id,
                    generation=0,
                    retryable=False,
                )
            self._active_lease_count += 1
        try:
            yield view
        finally:
            with self._condition:
                self._active_lease_count -= 1
                if self._active_lease_count < 0:
                    self._active_lease_count = 0
                    self._state = "FAILED"
                    self._condition.notify_all()
                    raise RuntimeError("operation lease count underflow")
                if self._active_lease_count == 0:
                    self._condition.notify_all()

    @contextmanager
    def _refresh_observation_gate(
        self,
        timeout_seconds: float | None = None,
        *,
        require_ready_resource: bool = False,
    ) -> Iterator[None]:
        """Bound configured-pair observation against refresh publication.

        Task 5.13: the configured pair publication intentionally runs a
        JSONL-only window between the JSONL replace and the manifest
        replace, so any ``SourceBindingMonitor.observe()`` read that
        overlaps that window would latch a false ``SOURCE_DIVERGED``.
        Both the refresh reservation and public observations pass
        through this gate, which is scoped to one resource coordinator
        (never a process-global lock).  The owner is the acquiring
        thread identity plus a nesting depth, so the refresh's own
        reentrant preflight observation under its reservation cannot
        deadlock; other threads wait up to the bounded timeout and a
        wedged holder converts to a retryable ``STORE.REFRESH_BUSY``
        failure.  The gate is never acquired while an operation lease
        is held and activation draining never acquires the gate, so it
        cannot deadlock against leases or draining.
        """

        timeout = (
            _REFRESH_RESERVATION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else _require_timeout(timeout_seconds)
        )
        owner = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._condition:
            while (
                self._refresh_gate_owner is not None
                and self._refresh_gate_owner != owner
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SQLiteStoreLifecycleError(
                        "STORE.REFRESH_BUSY",
                        resource_id=self._resource_id,
                        generation=(
                            0
                            if self._view is None
                            else self._view.generation
                        ),
                        retryable=True,
                    )
                self._condition.wait(remaining)
            if require_ready_resource:
                if self._state != "READY":
                    raise SQLiteStoreLifecycleError(
                        "STORE.RESOURCE_DRAINING",
                        resource_id=self._resource_id,
                        generation=(
                            0
                            if self._view is None
                            else self._view.generation
                        ),
                        retryable=True,
                    )
                if self._view is None:
                    raise SQLiteStoreLifecycleError(
                        "STORE.CANONICAL_UNAVAILABLE",
                        resource_id=self._resource_id,
                        generation=0,
                        retryable=False,
                    )
            if self._refresh_gate_owner is None:
                self._refresh_gate_owner = owner
            self._refresh_gate_depth += 1
        try:
            yield
        finally:
            with self._condition:
                self._refresh_gate_depth -= 1
                if self._refresh_gate_depth < 0:
                    self._refresh_gate_depth = 0
                    self._state = "FAILED"
                    self._condition.notify_all()
                    raise RuntimeError("refresh observation gate underflow")
                if self._refresh_gate_depth == 0:
                    self._refresh_gate_owner = None
                    self._condition.notify_all()

    @contextmanager
    def configured_refresh_reservation(
        self,
        timeout_seconds: float | None = None,
    ) -> Iterator[None]:
        """Serialize configured snapshot refreshes for one resource.

        Task 5.13: the configured pair publication intentionally runs a
        JSONL-only window between the JSONL replace and the manifest
        replace.  A second refresh or a public monitor observation
        crossing that window must not treat it as tampering and latch
        ``SOURCE_DIVERGED``, so the whole refresh operation is
        serialized under this reservation through the shared reentrant
        observation gate.  The reservation is held only across the
        operation body, rejects a draining/unavailable resource at
        acquisition, and is never acquired while an operation lease is
        held, so it cannot deadlock against leases or activation
        draining; a bounded wait converts a wedged holder into a
        retryable ``STORE.REFRESH_BUSY`` failure.
        """

        with self._refresh_observation_gate(
            timeout_seconds=timeout_seconds,
            require_ready_resource=True,
        ):
            yield

    def prepare_schema_upgrade_ticket(
        self,
    ) -> _SchemaUpgradeSnapshotTicket:
        """Stabilize the resource and mint one opaque upgrade snapshot ticket.

        Task 5.11 coordinator-owned seam: before draining it proves the
        prior v1 revision ancestry from the strict record-block proof
        (``SCHEMA.ANCESTRY_UNPROVABLE`` on any unprovable order) and
        validates the complete prior binding/manifest/receipt/source/
        divergence closure, then enters DRAINING (new leases are
        rejected), waits for all old leases to drain within the bounded
        timeout, proves the active view is unchanged, and re-proves the
        same ancestry and prior-asset captures.  It then runs
        ``sqlite3.Connection.backup()`` into a fresh same-directory
        exclusively reserved regular file while the resource is stable.
        The file and parent directory are fsynced, the exact prior active
        DB identity/digest and head revision are captured, READY is
        restored, and the single-use ticket is returned.  Any failure
        restores READY and never mutates the live canonical; divergence,
        tampering, or an unprovable order is never repaired.
        """

        self.release_schema_upgrade_locator_snapshot()
        port = _CoordinatorStorePort(self)
        with self._condition:
            if (
                self._state != "READY"
                or self._preparation is not None
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
                or self._schema_upgrade_ticket is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.UPGRADE_BUSY",
                    retryable=True,
                )
            view = self._view
            if view is None:
                raise ActivationPreparationError(
                    "ACTIVATION.UPGRADE_ACTIVE_RESOURCE_REQUIRED",
                    retryable=False,
                )
            _require_no_pending_activation_assets(self._resource_identity)
            # Fresh v1 ticket mint: strictly sweep only unexposed pending
            # schema-upgrade crash orphans of the deterministic naming
            # family (never stable reported backups or exposed failure
            # locators), so a crash after a previous ticket mint or after
            # guard consumption cannot accumulate hidden full DB copies.
            _sweep_pending_schema_upgrade_artifacts(
                view.stage.staged_db_path
            )
            initial_generation = view.generation
            _require_schema_upgrade_ancestry_provable(
                view.stage.staged_db_path
            )
            pre_drain_captures = _capture_prior_assets(
                port,
                view,
                identity=self._resource_identity,
                replacement=False,
            )
            self._state = "DRAINING"
            self._condition.notify_all()
            deadline = time.monotonic() + self._drain_timeout_seconds
            backup_path: Path | None = None
            backup_identity: tuple[int, int] | None = None
            locator_snapshot_path: Path | None = None
            locator_snapshot_identity: tuple[int, int] | None = None
            try:
                while self._active_lease_count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActivationPreparationError(
                            "ACTIVATION.DRAIN_TIMEOUT",
                            retryable=True,
                        )
                    self._condition.wait(remaining)
                if self._view is not view:
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                if view.generation != initial_generation:
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                _require_schema_upgrade_ancestry_provable(
                    view.stage.staged_db_path
                )
                post_drain_captures = _capture_prior_assets(
                    port,
                    view,
                    identity=self._resource_identity,
                    replacement=False,
                )
                try:
                    _require_same_asset_captures(
                        pre_drain_captures,
                        post_drain_captures,
                    )
                except ActivationPreparationError as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.UPGRADE_SNAPSHOT_STALE",
                        retryable=True,
                    ) from error
                database_path = view.stage.staged_db_path
                db_identity, db_digest = _schema_upgrade_db_capture(
                    database_path
                )
                head_revision = _schema_upgrade_head_revision(database_path)
                token = uuid.uuid4().hex
                backup_path = _schema_upgrade_backup_path(
                    database_path,
                    token,
                )
                backup_identity, backup_digest = (
                    _create_schema_upgrade_backup(
                        database_path,
                        backup_path,
                    )
                )
                locator_snapshot_path = _schema_upgrade_locator_snapshot_path(
                    database_path,
                    token,
                )
                locator_snapshot_identity, locator_snapshot_digest = (
                    _create_schema_upgrade_locator_snapshot(
                        database_path,
                        locator_snapshot_path,
                    )
                )
                ticket = _SchemaUpgradeSnapshotTicket(
                    _owner_nonce=self._owner_nonce,
                    resource_id=self._resource_id,
                    canonical_store_id=self._canonical_store_id,
                    generation=view.generation,
                    head_revision=head_revision,
                    db_identity=db_identity,
                    db_digest=db_digest,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    backup_digest=backup_digest,
                    _factory_key=_SCHEMA_UPGRADE_TICKET_FACTORY_KEY,
                )
                self._schema_upgrade_ticket = ticket
                self._schema_upgrade_locator_snapshot = (
                    _SchemaUpgradeLocatorSnapshot(
                        path=locator_snapshot_path,
                        identity=locator_snapshot_identity,
                        digest=locator_snapshot_digest,
                    )
                )
                return ticket
            except BaseException:
                if backup_path is not None and backup_identity is not None:
                    _remove_owned_schema_upgrade_artifact(
                        backup_path,
                        backup_identity,
                    )
                if (
                    locator_snapshot_path is not None
                    and locator_snapshot_identity is not None
                ):
                    _remove_owned_schema_upgrade_artifact(
                        locator_snapshot_path,
                        locator_snapshot_identity,
                    )
                    self._schema_upgrade_locator_snapshot = None
                raise
            finally:
                self._state = "READY"
                self._condition.notify_all()

    def retire_schema_upgrade_ticket(
        self,
        ticket: _SchemaUpgradeSnapshotTicket,
    ) -> None:
        """Safely retire one unused upgrade ticket (Task 5.11).

        Failures between ticket minting and the activation guard must not
        block a later fresh snapshot: this seam consumes the exact live
        ticket.  An already-consumed or foreign ticket is rejected so the
        single-use contract cannot be silently violated.
        """

        if type(ticket) is not _SchemaUpgradeSnapshotTicket:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_INVALID",
                retryable=False,
            )
        remove_backup = False
        with self._condition:
            if self._schema_upgrade_ticket is ticket:
                self._schema_upgrade_ticket = None
                remove_backup = True
            elif self._schema_upgrade_ticket is None:
                return
            else:
                raise ActivationPreparationError(
                    "ACTIVATION.UPGRADE_TICKET_INVALID",
                    retryable=False,
                )
        if remove_backup:
            _remove_schema_upgrade_backup(ticket)

    @property
    def schema_upgrade_locator_snapshot(
        self,
    ) -> _SchemaUpgradeLocatorSnapshot | None:
        """The one coordinator-captured byte-exact locator snapshot, if any."""

        with self._condition:
            return self._schema_upgrade_locator_snapshot

    def release_schema_upgrade_locator_snapshot(self) -> None:
        """Strictly delete the held locator snapshot, if any.

        The snapshot is deleted by its captured identity only (regular
        single-link file); a missing file is already released, and a
        foreign inode is never unlinked but fails closed so the caller can
        stop instead of leaving an unaccounted artifact.  The record is
        cleared only after the deletion is durable.
        """

        with self._condition:
            snapshot = self._schema_upgrade_locator_snapshot
            if snapshot is None:
                return
        _remove_schema_upgrade_locator_snapshot(snapshot)
        with self._condition:
            self._schema_upgrade_locator_snapshot = None

    def _detach_schema_upgrade_locator_snapshot(
        self,
        expected_path: Path,
    ) -> None:
        """Transfer one proven locator snapshot to the failure result.

        Once a ``SchemaUpgradeFailure`` exposes the path, the coordinator
        must stop treating the file as an unexposed temporary: a later retry
        may create fresh evidence, but it must not silently invalidate the
        already-returned recovery locator.  Detachment changes only the
        in-memory ownership record and never deletes or rewrites the file.
        """

        if type(expected_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("expected locator path must be pathlib.Path")
        with self._condition:
            snapshot = self._schema_upgrade_locator_snapshot
            if snapshot is None or snapshot.path != expected_path:
                raise ActivationPreparationError(
                    "ACTIVATION.UPGRADE_LOCATOR_INVALID",
                    retryable=False,
                )
            self._schema_upgrade_locator_snapshot = None

    def activate(
        self,
        sealed_stage: SealedStage,
        *,
        _schema_upgrade_ticket: _SchemaUpgradeSnapshotTicket | None = None,
    ) -> _ActivationPreparation:
        """Prepare one registered same-store-id sealed stage (Task 5.5).

        Ordinary activation retains the same-id rule: the sealed stage's
        canonical store id must equal the coordinator's current id.
        Explicit replacement of an already-active resource (a fresh
        canonical store id) must use :meth:`activate_replacement`.

        The private Task 5.11 schema-upgrade guard accepts exactly one
        coordinator-minted snapshot ticket.  The guard runs after the
        normal drain and before any journal: it proves the ticket is the
        coordinator's live unused ticket bound to this resource/store/
        generation and that the active canonical is byte- and revision-
        identical to the stabilized snapshot, then consumes the ticket.
        A save/import in the window fails the stale candidate before any
        journal, restores READY, and requires a fresh snapshot for retry.
        """

        if _schema_upgrade_ticket is not None and type(
            _schema_upgrade_ticket
        ) is not _SchemaUpgradeSnapshotTicket:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_INVALID",
                retryable=False,
            )
        return self._activate(
            sealed_stage,
            replacement=False,
            schema_upgrade_ticket=_schema_upgrade_ticket,
        )

    def activate_replacement(
        self,
        sealed_stage: SealedStage,
        *,
        _schema_upgrade_ticket: _SchemaUpgradeSnapshotTicket | None = None,
    ) -> _ActivationPreparation:
        """Explicitly prepare a replacement stage with a different store id.

        The Task 5.10 disambiguation seam: the sealed stage must carry a
        fresh canonical store id different from the coordinator's current
        (prior) id, and the resource/target/generation facts must bind
        exactly like an ordinary activation.  Everything else (Gate B,
        token, drain, backups) follows the same preparation pipeline, and
        the coordinator keeps the prior store id until the candidate
        generation is durably published.  Schema-upgrade tickets are
        same-id activations only and are rejected here.
        """

        if _schema_upgrade_ticket is not None:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_INVALID",
                retryable=False,
            )
        return self._activate(sealed_stage, replacement=True)

    def activate_portable_replacement(
        self,
        sealed_stage: SealedStage,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> _PortableReplacementPreparation:
        """Prepare one strict Windows N -> N+1 replacement under W1.

        This path never enters the native/path-bearing backup machinery used
        by ``activate_replacement``.  It authenticates the completed portable
        predecessor, drains the same coordinator, captures the prior canonical
        pair and selected source through live rooted handles, and retains those
        handles for the later PREPARED+backup owner handshake.
        """

        if type(sealed_stage) is not SealedStage:
            raise ActivationPreparationError(
                "ACTIVATION.TYPE_INVALID",
                retryable=False,
            )
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")

        gate_b_evaluator = getattr(
            importlib.import_module("tm_gate_b"),
            "GateBEvaluator",
        )
        stage_sealer = importlib.import_module("tm_stage_sealer")
        stage_seal_error = getattr(stage_sealer, "StageSealError")
        portable_snapshot_type = getattr(
            stage_sealer,
            "_PortablePhysicalReadinessSnapshot",
        )
        registry = cast(Any, self._sealed_registry)
        identity = self._resource_identity
        with self._condition:
            prior_view = self._view
            if (
                self._state != "READY"
                or self._preparation is not None
                or self._portable_replacement_preparation is not None
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
                or type(prior_view) is not _SQLiteGenerationView
                or type(prior_view.active_content_attestation)
                is not PortableActiveContentAttestation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.CONCURRENT_PREPARATION",
                    retryable=True,
                )
            prior_generation = prior_view.generation
            prior_store_id = self._canonical_store_id

        first_report = gate_b_evaluator(
            registry=registry._readiness_view()
        ).evaluate(sealed_stage)
        if not first_report.granted or first_report.grant is None:
            raise ActivationPreparationError(
                "ACTIVATION.GATE_B_DENIED",
                retryable=False,
                reason_code=first_report.error_code,
            )
        first_grant = first_report.grant
        _require_activation_grant_identity_replacement(
            first_grant,
            identity=identity,
            canonical_store_id=prior_store_id,
            prior_view=prior_view,
            current_generation=prior_generation,
        )

        borrow_already_claimed = False
        current_record = self._portable_replacement_current_record
        if current_record is None:
            if not isinstance(platform, LockedDescendantNamespaceInspection):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                    retryable=False,
                )
            base_snapshot = _WindowsPortableFreshRecoveryOwner.inspect(
                identity=identity,
                canonical_store_id=prior_store_id,
                backend=platform,
                persistent_private=persistent_private,
                descendant_inspection=platform,
                caller_borrow=caller_borrow,
            )
            borrow_already_claimed = True
            if (
                base_snapshot.state != "PENDING"
                or base_snapshot.highest_phase != "GENERATION_PUBLISHED"
                or len(base_snapshot.phase_records) != 3
                or base_snapshot.phase_records[-1].unsigned.active_content_attestation
                != prior_view.active_content_attestation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                )
            prior_authority_digest = base_snapshot.phase_records[-1].record_digest
        else:
            current_unsigned = current_record.unsigned
            if (
                current_unsigned.phase != "READY"
                or current_unsigned.resource_id != identity.resource_id
                or current_unsigned.target_identity != identity.target_identity
                or current_unsigned.candidate_canonical_store_id != prior_store_id
                or current_unsigned.next_generation != prior_generation
                or current_unsigned.active_content_attestation
                != prior_view.active_content_attestation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                )
            prior_authority_digest = current_record.record_digest

        token: contract_module._ActivationToken | None = None
        prior_authority: _PortableReplacementPriorAuthority | None = None
        deadline = time.monotonic() + self._drain_timeout_seconds
        try:
            with self._condition:
                if (
                    self._state != "READY"
                    or self._view is not prior_view
                    or self._canonical_store_id != prior_store_id
                    or self._portable_replacement_preparation is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.CONCURRENT_PREPARATION",
                        retryable=True,
                    )
                if (
                    self._schema_upgrade_ticket is not None
                    or self._schema_upgrade_locator_snapshot is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.UPGRADE_BUSY",
                        retryable=True,
                    )
                self._state = "DRAINING"
                self._condition.notify_all()
                try:
                    token = registry.issue_token(
                        sealed_stage,
                        current_generation=prior_generation,
                    )
                except stage_seal_error as error:
                    self._state = "READY"
                    self._condition.notify_all()
                    raise ActivationPreparationError(
                        "ACTIVATION.TOKEN_REJECTED",
                        retryable=False,
                        reason_code=error.error_code,
                    ) from error
                while self._active_lease_count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActivationPreparationError(
                            "ACTIVATION.DRAIN_TIMEOUT",
                            retryable=True,
                        )
                    self._condition.wait(remaining)
                if (
                    self._view is not prior_view
                    or self._canonical_store_id != prior_store_id
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                self._state = "ACTIVATING"
                self._condition.notify_all()

            second_report = gate_b_evaluator(
                registry=registry._readiness_view()
            ).evaluate(sealed_stage)
            if (
                not second_report.granted
                or second_report.grant is None
                or second_report.grant.grant_digest != first_grant.grant_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                    retryable=False,
                    reason_code=second_report.error_code,
                )
            second_grant = second_report.grant
            _require_activation_grant_identity_replacement(
                second_grant,
                identity=identity,
                canonical_store_id=prior_store_id,
                prior_view=prior_view,
                current_generation=prior_generation,
            )
            assert token is not None
            contract_module._validate_activation_token_for_stage(
                token,
                sealed_stage,
            )
            _require_activation_token_identity_replacement(
                token,
                identity=identity,
                canonical_store_id=prior_store_id,
                candidate_store_id=second_grant.canonical_store_id,
                current_generation=prior_generation,
            )
            physical = registry.resolve_physical_readiness(sealed_stage)
            if (
                type(physical) is not portable_snapshot_type
                or physical.resource_id != identity.resource_id
                or physical.target_identity != identity.target_identity
                or physical.canonical_store_id != second_grant.canonical_store_id
                or physical.expected_prior_generation != prior_generation
                or type(physical.sealed_content_attestation)
                is not PortableSealedContentAttestation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.ATTESTATION_UNAVAILABLE",
                    retryable=False,
                )
            physical.live_reproof.reprove()
            caller_borrow.reprove(platform, identity)
            prior_authority = self._capture_portable_replacement_prior_authority(
                prior_view=prior_view,
                candidate_attestation=physical.sealed_content_attestation,
                platform=platform,
                persistent_private=persistent_private,
                caller_borrow=caller_borrow,
                borrow_already_claimed=borrow_already_claimed,
            )
            prior_authority.reprove()
            preparation_id = f"replacement.{uuid.uuid4().hex}"
            backup_plans = (
                _PortableReplacementBackupPlan(
                    asset_kind="DATABASE",
                    backup_name=_portable_replacement_backup_name(
                        preparation_id,
                        "DATABASE",
                    ),
                    content=prior_authority.database_proof,
                ),
                _PortableReplacementBackupPlan(
                    asset_kind="MANIFEST",
                    backup_name=_portable_replacement_backup_name(
                        preparation_id,
                        "MANIFEST",
                    ),
                    content=prior_authority.manifest_proof,
                ),
            )
            preparation = _PortableReplacementPreparation(
                preparation_id=preparation_id,
                resource_id=identity.resource_id,
                target_identity=identity.target_identity,
                candidate_store_id=second_grant.canonical_store_id,
                prior_store_id=prior_store_id,
                expected_prior_generation=prior_generation,
                next_generation=prior_generation + 1,
                gate_b_grant_digest=second_grant.grant_digest,
                lock_payload_digest=caller_borrow.lock_payload_digest(),
                prior_authority_digest=prior_authority_digest,
                backup_plans=backup_plans,
                _token=token,
                _physical_snapshot=physical,
                _prior_view=prior_view,
                _sealed_stage=sealed_stage,
                _prior_authority=prior_authority,
            )
            with self._condition:
                if (
                    self._state != "ACTIVATING"
                    or self._view is not prior_view
                    or self._portable_replacement_preparation is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                self._portable_replacement_preparation = preparation
            prior_authority = None
            return preparation
        except BaseException:
            if prior_authority is not None:
                try:
                    prior_authority.close()
                except BaseException:
                    if type(sys.exception()) in (
                        TypeError,
                        AssertionError,
                        AttributeError,
                    ):
                        raise
            try:
                if token is None:
                    registry.retire_unissued_portable(sealed_stage)
                else:
                    registry.cancel(token)
            except (stage_seal_error, OSError) as cleanup_error:
                with self._condition:
                    self._state = "ACTIVATING"
                    self._condition.notify_all()
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_FAILED",
                    retryable=True,
                ) from cleanup_error
            with self._condition:
                self._portable_replacement_preparation = None
                self._view = prior_view
                self._state = "READY"
                self._condition.notify_all()
            raise

    def _activate(
        self,
        sealed_stage: SealedStage,
        *,
        replacement: bool,
        schema_upgrade_ticket: _SchemaUpgradeSnapshotTicket | None = None,
    ) -> _ActivationPreparation:
        """Shared preparation pipeline for ordinary and replacement stages.

        This Task 5.5 seam performs fresh Gate B evaluation, issues the
        registry token, drains leases, repeats Gate B after the drain, and
        creates a same-directory recovery backup of an existing canonical
        DB/manifest pair.  It deliberately does not replace, journal,
        publish a manifest, mutate DB metadata, or advance generation.
        """

        gate_b_evaluator = getattr(
            importlib.import_module("tm_gate_b"),
            "GateBEvaluator",
        )
        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        registry = cast(Any, self._sealed_registry)

        if type(sealed_stage) is not SealedStage:
            raise ActivationPreparationError(
                "ACTIVATION.TYPE_INVALID",
                retryable=False,
            )
        with self._condition:
            if self._state != "READY" or self._preparation is not None:
                raise ActivationPreparationError(
                    "ACTIVATION.CONCURRENT_PREPARATION",
                    retryable=True,
                )
            journal_path = _activation_journal_path(self._resource_identity)
            try:
                journal_identity = _lstat_activation_journal_identity(
                    journal_path
                )
            except ActivationPreparationError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                    reason_code=error.code,
                ) from error
            if journal_identity is not None:
                try:
                    disk_bytes, _disk_identity = (
                        _read_activation_journal_file(
                            journal_path,
                            journal_identity,
                        )
                    )
                    disk_record = _parse_activation_journal_bytes(
                        disk_bytes,
                        expected_journal_path=journal_path,
                    )
                except ActivationPreparationError as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PENDING",
                        retryable=False,
                        reason_code=error.code,
                    ) from error
                if (
                    disk_record.phase
                    is not _ActivationJournalPhase.GENERATION_PUBLISHED
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PENDING",
                        retryable=False,
                    )
            if _lstat_any_entry(_activation_journal_temp_path(journal_path)):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                )
            terminal_path = _activation_terminal_path(self._resource_identity)
            try:
                terminal_identity = _lstat_activation_terminal_identity(
                    terminal_path
                )
            except ActivationPreparationError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_PENDING",
                    retryable=False,
                    reason_code=error.code,
                ) from error
            if journal_identity is None and terminal_identity is None:
                marker_path = _activation_lineage_marker_path(
                    self._resource_identity
                )
                pair_present = (
                    _lstat_any_entry(
                        self._resource_identity.canonical_sidecar_path
                    )
                    or _lstat_any_entry(
                        self._resource_identity.snapshot_manifest_path
                    )
                    or (
                        self._view is not None
                        and _lstat_any_entry(
                            self._view.stage.staged_db_path
                        )
                    )
                )
                if not pair_present:
                    # The true never-activated legacy state has no pair,
                    # no marker final, and no marker temporary; any
                    # marker-family entry or leftover temporary fails
                    # closed instead of being silently ignored.
                    try:
                        marker_identity = (
                            _lstat_activation_lineage_marker_identity(
                                marker_path
                            )
                        )
                    except ActivationPreparationError as error:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_PENDING",
                            retryable=False,
                            reason_code=error.code,
                        ) from error
                    if marker_identity is not None or _lstat_any_entry(
                        _activation_lineage_marker_temp_path(marker_path)
                    ):
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_PENDING",
                            retryable=False,
                        )
                else:
                    # The final/temp marker state must be complete before a
                    # new preparation: a valid final is accepted only when
                    # the temporary is absent or the exact paired two-link
                    # handoff is finished durably.  Any conflicting
                    # non-paired regular, symlink, directory, extra-link,
                    # wrong-identity, or wrong-byte temporary fails closed
                    # and is never deleted or overwritten.
                    try:
                        marker_identity = (
                            _activation_lineage_marker_state_complete(
                                self._resource_identity
                            )
                        )
                    except ActivationPreparationError as error:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_PENDING",
                            retryable=False,
                            reason_code=error.code,
                        ) from error
                    if marker_identity is None:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_PENDING",
                            retryable=False,
                        )
            initial_view = self._view
            initial_generation = (
                None if initial_view is None else initial_view.generation
            )

        try:
            first_report = gate_b_evaluator(
                registry=registry._readiness_view()
            ).evaluate(sealed_stage)
            if not first_report.granted or first_report.grant is None:
                raise ActivationPreparationError(
                    "ACTIVATION.GATE_B_DENIED",
                    retryable=False,
                    reason_code=first_report.error_code,
                )
            first_grant = first_report.grant
            if replacement:
                _require_activation_grant_identity_replacement(
                    first_grant,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    prior_view=initial_view,
                    current_generation=initial_generation,
                )
            else:
                _require_activation_grant_identity(
                    first_grant,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    prior_view=initial_view,
                    current_generation=initial_generation,
                )
            pre_drain_captures = _capture_pre_drain_assets(
                initial_view,
                identity=self._resource_identity,
                replacement=replacement,
            )
        except BaseException:
            try:
                registry.retire_unissued_portable(sealed_stage)
            except stage_seal_error as cleanup_error:
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_FAILED",
                    retryable=True,
                    reason_code=cleanup_error.error_code,
                ) from cleanup_error
            raise

        token: contract_module._ActivationToken | None = None
        backups: tuple[_RecoveryBackupAsset, ...] = ()
        owned_paths: list[_OwnedRecoveryPath] = []
        deadline = time.monotonic() + self._drain_timeout_seconds
        try:
            with self._condition:
                current_view = self._view
                current_generation = (
                    None if current_view is None else current_view.generation
                )
                if (
                    self._state != "READY"
                    or self._preparation is not None
                    or current_view is not initial_view
                    or current_generation != initial_generation
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.CONCURRENT_PREPARATION",
                        retryable=True,
                    )
                if replacement:
                    # This lock-held check+sweep is the atomic boundary
                    # between Task 5.10 replacement and Task 5.11 ticket
                    # minting.  A live ticket/snapshot is never swept.  An
                    # abandoned deterministic pending family is resolved
                    # immediately before the state becomes DRAINING, so a
                    # later ticket mint cannot recreate it before this
                    # activation owns the coordinator.  Existing operation
                    # leases remain governed by the normal bounded drain
                    # below and do not turn an import into an eager BUSY
                    # failure.
                    if (
                        self._schema_upgrade_ticket is not None
                        or self._schema_upgrade_locator_snapshot is not None
                    ):
                        raise ActivationPreparationError(
                            "ACTIVATION.UPGRADE_BUSY",
                            retryable=True,
                        )
                    if current_view is not None:
                        _sweep_pending_schema_upgrade_artifacts(
                            current_view.stage.staged_db_path
                        )
                self._state = "DRAINING"
                self._condition.notify_all()
                try:
                    token = registry.issue_token(
                        sealed_stage,
                        current_generation=current_generation,
                    )
                except stage_seal_error as error:
                    self._state = "READY"
                    self._condition.notify_all()
                    raise ActivationPreparationError(
                        "ACTIVATION.TOKEN_REJECTED",
                        retryable=False,
                        reason_code=error.error_code,
                    ) from error
                while self._active_lease_count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActivationPreparationError(
                            "ACTIVATION.DRAIN_TIMEOUT",
                            retryable=True,
                        )
                    self._condition.wait(remaining)
                if self._view is not initial_view:
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                observed_generation = (
                    None if self._view is None else self._view.generation
                )
                if observed_generation != initial_generation:
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                self._state = "ACTIVATING"
                self._condition.notify_all()

            second_report = gate_b_evaluator(
                registry=registry._readiness_view()
            ).evaluate(sealed_stage)
            if not second_report.granted or second_report.grant is None:
                raise ActivationPreparationError(
                    "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                    retryable=False,
                    reason_code=second_report.error_code,
                )
            second_grant = second_report.grant
            if second_grant.grant_digest != first_grant.grant_digest:
                raise ActivationPreparationError(
                    "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                    retryable=False,
                )
            if replacement:
                _require_activation_grant_identity_replacement(
                    second_grant,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    prior_view=initial_view,
                    current_generation=initial_generation,
                )
            else:
                _require_activation_grant_identity(
                    second_grant,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    prior_view=initial_view,
                    current_generation=initial_generation,
                )
            assert token is not None
            contract_module._validate_activation_token_for_stage(
                token,
                sealed_stage,
            )
            if replacement:
                _require_activation_token_identity_replacement(
                    token,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    candidate_store_id=second_grant.canonical_store_id,
                    current_generation=initial_generation,
                )
            else:
                _require_activation_token_identity(
                    token,
                    identity=self._resource_identity,
                    canonical_store_id=self._canonical_store_id,
                    current_generation=initial_generation,
                )
            physical_snapshot = registry.resolve_physical_readiness(
                sealed_stage
            )
            expected_candidate_id = (
                second_grant.canonical_store_id
                if replacement
                else self._canonical_store_id
            )
            if (
                physical_snapshot.mutable_stage.resource_identity
                != self._resource_identity
                or physical_snapshot.canonical_store_id
                != expected_candidate_id
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.IDENTITY_MISMATCH",
                    retryable=False,
                )
            _require_schema_upgrade_mode_closure(
                physical_snapshot,
                schema_upgrade_ticket=schema_upgrade_ticket,
            )
            preparation_id = f"preparation.{uuid.uuid4().hex}"
            if initial_view is None:
                _require_first_activation_absence(self._resource_identity)
                source_capture = _capture_activation_file(
                    self._resource_identity.configured_jsonl_path,
                    asset_kind="SOURCE",
                )
                if source_capture.digest != (
                    sealed_stage.evidence.source_binding.receipt.jsonl_digest
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                        retryable=False,
                    )
                _require_first_activation_absence(self._resource_identity)
                _require_same_asset_captures(
                    pre_drain_captures,
                    (source_capture,),
                )
                prior_manifest_absent = False
            else:
                captures = _capture_prior_assets(
                    _CoordinatorStorePort(self),
                    initial_view,
                    identity=self._resource_identity,
                    replacement=replacement,
                )
                _require_same_asset_captures(
                    pre_drain_captures,
                    captures,
                )
                if schema_upgrade_ticket is not None:
                    _require_schema_upgrade_ticket_guard(
                        self,
                        initial_view,
                        schema_upgrade_ticket,
                        captures,
                    )
                prior_manifest_absent = all(
                    asset.asset_kind != "MANIFEST"
                    for asset in captures
                )
                backups = _create_recovery_backups(
                    captures,
                    preparation_id=preparation_id,
                    owned_paths=owned_paths,
                    manifest_absent=prior_manifest_absent,
                )
                _revalidate_prior_assets(captures)
            with self._condition:
                if (
                    self._state != "ACTIVATING"
                    or self._view is not initial_view
                    or self._preparation is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.GENERATION_STALE",
                        retryable=False,
                    )
                preparation = _ActivationPreparation(
                    preparation_id=preparation_id,
                    resource_id=second_grant.resource_id,
                    target_identity=second_grant.target_identity,
                    canonical_store_id=(
                        second_grant.canonical_store_id
                        if replacement
                        else self._canonical_store_id
                    ),
                    prior_canonical_store_id=(
                        self._canonical_store_id if replacement else None
                    ),
                    expected_prior_generation=initial_generation,
                    gate_b_grant_digest=second_grant.grant_digest,
                    had_prior_canonical=initial_view is not None,
                    prior_manifest_absent=(
                        False
                        if initial_view is None
                        else prior_manifest_absent
                    ),
                    backup_evidence=tuple(
                        asset.evidence for asset in backups
                    ),
                    _token=token,
                    _physical_snapshot=physical_snapshot,
                    _prior_view=initial_view,
                    _backup_assets=backups,
                    _sealed_stage=sealed_stage,
                    _factory_key=_ACTIVATION_PREPARATION_FACTORY_KEY,
                )
                self._preparation = preparation
                return preparation
        except BaseException as error:
            failure = (
                error
                if isinstance(error, ActivationPreparationError)
                else ActivationPreparationError(
                    "ACTIVATION.BACKUP_FAILED",
                    retryable=True,
                )
            )
            cleanup_reservation = _ActivationCleanupReservation(
                token=token,
                prior_view=initial_view,
                owned_paths=tuple(owned_paths),
            )
            try:
                _remove_recovery_backups(cleanup_reservation.owned_paths)
                if token is not None:
                    registry.cancel(token)
                else:
                    registry.retire_unissued_portable(sealed_stage)
            except (ActivationPreparationError, stage_seal_error) as cleanup_error:
                with self._condition:
                    self._view = initial_view
                    self._preparation = None
                    self._cleanup_reservation = cleanup_reservation
                    self._state = "ACTIVATING"
                    self._condition.notify_all()
                reason_code = (
                    failure.code
                    if isinstance(cleanup_error, ActivationPreparationError)
                    else cast(str, getattr(cleanup_error, "error_code"))
                )
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_FAILED",
                    retryable=True,
                    reason_code=reason_code,
                ) from cleanup_error
            with self._condition:
                self._view = initial_view
                self._preparation = None
                self._cleanup_reservation = None
                self._state = "READY"
                self._condition.notify_all()
            if failure is error:
                raise
            raise failure from error

    def cancel_prepared_activation(
        self,
        preparation: _ActivationPreparation,
    ) -> None:
        """Fail-safe cancellation before Task 5.6 writes a journal."""

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )

        if type(preparation) is not _ActivationPreparation:
            raise ActivationPreparationError(
                "ACTIVATION.PREPARATION_INVALID",
                retryable=False,
            )
        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._preparation is not preparation
                or self._cleanup_reservation is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PREPARATION_NOT_ACTIVE",
                    retryable=False,
                )
            if self._cleanup_in_progress:
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_IN_PROGRESS",
                    retryable=True,
                )
            self._cleanup_in_progress = True
        owned_paths = tuple(
            _OwnedRecoveryPath(
                path=backup.backup_path,
                identity=backup.backup_identity,
            )
            for backup in preparation._backup_assets
        )
        record: _ActivationJournalRecord | None = None
        try:
            record = _build_activation_journal_record(
                _CoordinatorStorePort(self),
                preparation,
            )
            _require_cancelled_lineage_consistency(
                self._resource_identity,
                had_prior_canonical=preparation.had_prior_canonical,
            )
            _ = self._write_activation_terminal_locked(record)
        except ActivationPreparationError:
            # The live assets no longer prove the PREPARED closure (for
            # example a tampered candidate/source/prior/backup).  The
            # cancellation is still a fail-safe cleanup: no CANCELLED
            # terminal becomes durable (cold recovery fails closed on the
            # unproven state), and the candidates stay owned by the sealed
            # stage.
            record = None
        try:
            _remove_recovery_backups(owned_paths)
            self._sealed_registry.cancel(preparation._token)
        except ActivationPreparationError:
            raise
        except stage_seal_error as error:
            raise ActivationPreparationError(
                "ACTIVATION.CLEANUP_FAILED",
                retryable=True,
                reason_code=error.error_code,
            ) from error
        else:
            with self._condition:
                self._preparation = None
                self._view = preparation._prior_view
                self._state = "READY"
        finally:
            with self._condition:
                self._cleanup_in_progress = False
                self._condition.notify_all()

    def retry_failed_activation_cleanup(self) -> None:
        """Retry strict cleanup for a preparation that failed before return."""

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        with self._condition:
            reservation = self._cleanup_reservation
            if (
                self._state != "ACTIVATING"
                or self._preparation is not None
                or reservation is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_NOT_PENDING",
                    retryable=False,
                )
            if self._cleanup_in_progress:
                raise ActivationPreparationError(
                    "ACTIVATION.CLEANUP_IN_PROGRESS",
                    retryable=True,
                )
            self._cleanup_in_progress = True
        try:
            _remove_recovery_backups(reservation.owned_paths)
            if reservation.token is not None:
                self._sealed_registry.cancel(reservation.token)
        except ActivationPreparationError:
            raise
        except stage_seal_error as error:
            raise ActivationPreparationError(
                "ACTIVATION.CLEANUP_FAILED",
                retryable=True,
                reason_code=error.error_code,
            ) from error
        else:
            with self._condition:
                self._cleanup_reservation = None
                self._view = reservation.prior_view
                self._state = "READY"
        finally:
            with self._condition:
                self._cleanup_in_progress = False
                self._condition.notify_all()

    def publish_prepared_activation(
        self,
        preparation: _ActivationPreparation,
    ) -> _ActivationJournalHandle:
        """Durably publish the PREPARED activation journal (Task 5.6).

        Accepts only the coordinator's exact live ``_ActivationPreparation``
        capability and writes a canonical, fully fsynced journal adjacent to
        the canonical sidecar before any DB/manifest replacement.  On success
        the coordinator stays ACTIVATING, the token stays TOKEN_ISSUED, the
        recovery backups stay present, and no DB/manifest/generation asset
        changes.  An already-durable byte-identical PREPARED journal for the
        same preparation is replayed into a fresh handle without rewriting.
        """

        if type(preparation) is not _ActivationPreparation:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PREPARATION_INVALID",
                retryable=False,
            )
        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._preparation is not preparation
                or self._cleanup_reservation is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_STATE_INVALID",
                    retryable=True,
                )
            if self._cleanup_in_progress:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_STATE_INVALID",
                    retryable=True,
                )
            return _publish_activation_journal(
                _CoordinatorStorePort(self),
                preparation,
            )

    def publish_portable_prepared_activation(
        self,
        preparation: _ActivationPreparation,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> _PortablePreparedJournalHandle:
        """Publish only one Windows v3 PREPARED owner envelope.

        This seam is intentionally separate from the historical v2 writer.
        It leaves the coordinator ACTIVATING and does not publish, cancel, or
        recover the canonical DB/manifest pair.
        """

        if type(preparation) is not _ActivationPreparation:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PREPARATION_INVALID",
                retryable=False,
            )
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if not isinstance(persistent_private, PersistentPrivateProof):
            raise TypeError(
                "persistent_private must satisfy PersistentPrivateProof"
            )
        if (
            persistent_private is not platform
            or descendant_inspection is not platform
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")

        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._preparation is not preparation
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_STATE_INVALID",
                    retryable=True,
                )
            stage = preparation._sealed_stage
            token = preparation._token
            physical = preparation._physical_snapshot
            stage_sealer = importlib.import_module("tm_stage_sealer")
            portable_snapshot_type = getattr(
                stage_sealer,
                "_PortablePhysicalReadinessSnapshot",
            )
            stage_seal_error = getattr(stage_sealer, "StageSealError")
            if type(physical) is not portable_snapshot_type:
                raise ActivationPreparationError(
                    "ACTIVATION.ATTESTATION_UNAVAILABLE",
                    retryable=False,
                )
            try:
                contract_module._validate_activation_token_for_stage(
                    token,
                    stage,
                )
                physical.live_reproof.reprove()
                caller_borrow.reprove(platform, self._resource_identity)
            except stage_seal_error as error:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                ) from error
            except ValueError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                ) from error
            sealed_attestation = physical.sealed_content_attestation
            if type(sealed_attestation) is not PortableSealedContentAttestation:
                raise ActivationPreparationError(
                    "ACTIVATION.ATTESTATION_UNAVAILABLE",
                    retryable=False,
                )
            evidence = physical.evidence
            receipt = evidence.source_binding.receipt
            identity = self._resource_identity
            if (
                preparation.expected_prior_generation is not None
                or preparation.prior_canonical_store_id is not None
                or preparation.had_prior_canonical
                or preparation.prior_manifest_absent
                or preparation._prior_view is not None
                or preparation._backup_assets
                or physical.registry_namespace
                != token.registry_namespace
                or physical.artifact_id != token.artifact_id
                or physical.artifact_seal_digest
                != token.artifact_seal_digest
                or physical.sealed_stage_digest != token.sealed_stage_digest
                or physical.resource_id != identity.resource_id
                or physical.target_identity != identity.target_identity
                or physical.canonical_store_id != self._canonical_store_id
                or physical.canonical_store_id != token.canonical_store_id
                or physical.snapshot_receipt_digest
                != token.snapshot_receipt_digest
                or physical.expected_prior_generation is not None
                or physical.mutable_stage.resource_identity != identity
                or sealed_attestation.resource_id != physical.resource_id
                or sealed_attestation.target_identity
                != physical.target_identity
                or sealed_attestation.canonical_store_id
                != physical.canonical_store_id
                or sealed_attestation.snapshot_receipt_digest
                != physical.snapshot_receipt_digest
                or sealed_attestation.expected_prior_generation is not None
                or sealed_attestation.evidence_digest
                != contract_module.stage_validation_evidence_digest(evidence)
                or sealed_attestation.database.sha256
                != evidence.stage_file_digest
                or sealed_attestation.manifest.sha256
                != evidence.manifest_temp_digest
                or sealed_attestation.source.sha256 != receipt.jsonl_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            private_directory_name = (
                _portable_activation_private_directory_name(identity)
            )
            unsigned = _PortableActivationJournalUnsigned(
                journal_version=_PORTABLE_ACTIVATION_JOURNAL_VERSION,
                closure="PENDING",
                phase=_PORTABLE_ACTIVATION_JOURNAL_PHASE,
                journal_id=f"journal.{preparation.preparation_id}",
                preparation_id=preparation.preparation_id,
                registry_namespace=physical.registry_namespace,
                token_id=token.token_id,
                token_version=token.token_version,
                activation_nonce=token.activation_nonce,
                artifact_id=physical.artifact_id,
                artifact_seal_digest=physical.artifact_seal_digest,
                sealed_stage_digest=physical.sealed_stage_digest,
                resource_id=physical.resource_id,
                target_identity=physical.target_identity,
                canonical_store_id=physical.canonical_store_id,
                expected_prior_generation=None,
                gate_b_grant_digest=preparation.gate_b_grant_digest,
                evidence_digest=sealed_attestation.evidence_digest,
                snapshot_receipt_digest=physical.snapshot_receipt_digest,
                stage_db_digest=evidence.stage_file_digest,
                manifest_temp_digest=evidence.manifest_temp_digest,
                source_jsonl_digest=receipt.jsonl_digest,
                new_receipt_id=receipt.snapshot_id,
                new_manifest_digest=evidence.manifest_temp_digest,
                candidate_stage_db_name=(
                    physical.mutable_stage.staged_db_path.name
                ),
                candidate_manifest_temp_name=(
                    physical.mutable_stage.manifest_temp_path.name
                ),
                private_directory_name=private_directory_name,
                device_key_name="device.key",
                journal_name="activation-journal-v3.json",
                terminal_name="activation-terminal-v3.json",
                lock_payload_digest=caller_borrow.lock_payload_digest(),
                sealed_content_attestation=sealed_attestation,
                active_content_attestation=None,
            )

            def reprove_physical_owner_authority() -> None:
                fresh = self._sealed_registry.resolve_physical_readiness(stage)
                if type(fresh) is not portable_snapshot_type:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                        retryable=False,
                    )
                try:
                    if replace(
                        fresh,
                        live_reproof=physical.live_reproof,
                    ) != physical:
                        raise ActivationPreparationError(
                            "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                            retryable=False,
                        )
                    fresh.live_reproof.reprove()
                finally:
                    fresh.live_reproof._release()

            def owner_reprove() -> None:
                if (
                    self._state != "ACTIVATING"
                    or self._preparation is not preparation
                    or self._cleanup_reservation is not None
                    or self._cleanup_in_progress
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_STATE_INVALID",
                        retryable=True,
                    )
                try:
                    contract_module._validate_activation_token_for_stage(
                        preparation._token,
                        preparation._sealed_stage,
                    )
                    reprove_physical_owner_authority()
                    caller_borrow.reprove(platform, identity)
                except stage_seal_error as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_ASSET_MUTATED",
                        retryable=False,
                    ) from error
                except ValueError as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                        retryable=False,
                    ) from error

            def owner_commit(record: _PortableActivationJournalRecord) -> None:
                if record.unsigned != unsigned:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                        retryable=False,
                    )
                owner_reprove()

            try:
                return _WindowsPortablePreparedJournalOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    descendant_inspection=descendant_inspection,
                    caller_borrow=caller_borrow,
                    unsigned=unsigned,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit,
                )
            except BaseException as error:
                try:
                    self._sealed_registry.release_portable_live_authority_for_recovery(
                        token
                    )
                except BaseException as release_error:
                    if type(error) in (TypeError, AssertionError, AttributeError):
                        raise error
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from release_error
                raise

    @staticmethod
    def _portable_content_proof(
        facts: BoundContentFacts | CandidateContentFacts,
    ) -> PortableContentFileProof:
        if type(facts) is BoundContentFacts:
            size = facts.snapshot.byte_count
        elif type(facts) is CandidateContentFacts:
            size = facts.byte_count
        else:
            raise TypeError("portable content facts are invalid")
        return PortableContentFileProof(
            size=size,
            sha256=facts.content_sha256.hex(),
        )

    def _capture_portable_replacement_prior_authority(
        self,
        *,
        prior_view: _SQLiteGenerationView,
        candidate_attestation: PortableSealedContentAttestation,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        borrow_already_claimed: bool,
    ) -> _PortableReplacementPriorAuthority:
        """Bind the prior pair and selected source without pathname adoption."""

        if type(prior_view) is not _SQLiteGenerationView:
            raise TypeError("portable replacement prior view is invalid")
        if type(candidate_attestation) is not PortableSealedContentAttestation:
            raise TypeError("portable replacement candidate attestation is invalid")
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")
        if type(borrow_already_claimed) is not bool:
            raise TypeError("portable replacement borrow state is invalid")
        lineage_attestation = prior_view.active_content_attestation
        if type(lineage_attestation) is not PortableActiveContentAttestation:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        identity = self._resource_identity
        if (
            candidate_attestation.resource_id != identity.resource_id
            or candidate_attestation.target_identity != identity.target_identity
            or candidate_attestation.expected_prior_generation
            != prior_view.generation
            or candidate_attestation.canonical_store_id
            == lineage_attestation.canonical_store_id
            or lineage_attestation.resource_id != identity.resource_id
            or lineage_attestation.target_identity != identity.target_identity
            or lineage_attestation.canonical_store_id != self._canonical_store_id
            or lineage_attestation.generation != prior_view.generation
        ):
            raise ActivationPreparationError(
                "ACTIVATION.IDENTITY_MISMATCH",
                retryable=False,
            )
        if (
            identity.configured_jsonl_path.parent
            != identity.canonical_sidecar_path.parent
            or identity.snapshot_manifest_path.parent
            != identity.canonical_sidecar_path.parent
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        if borrow_already_claimed:
            root, _lease = caller_borrow._publication_authorities(
                platform,
                persistent_private,
                identity,
            )
        else:
            root, _lease = caller_borrow._authorities(
                platform,
                persistent_private,
                identity,
            )
        names = (
            identity.canonical_sidecar_path.name,
            identity.snapshot_manifest_path.name,
            identity.configured_jsonl_path.name,
        )
        expected_proofs: tuple[
            PortableContentFileProof | None,
            PortableContentFileProof | None,
            PortableContentFileProof,
        ] = (None, None, candidate_attestation.source)
        authorities: list[BoundRegularFile] = []
        try:
            observed: list[PortableContentFileProof] = []
            for name, expected in zip(names, expected_proofs, strict=True):
                authority = platform.open_regular(root, PurePath(name))
                authorities.append(authority)
                facts = authority.content_facts()
                proof = self._portable_content_proof(facts)
                if (
                    facts.snapshot.identity.kind != "regular"
                    or facts.snapshot.identity.link_count != 1
                    or root.inspect_entry(name) != facts.snapshot
                    or (expected is not None and proof != expected)
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.PRIOR_ASSET_INVALID",
                        retryable=False,
                    )
                observed.append(proof)
            active_ref = _canonical_activation_ref(
                identity,
                journal_id=lineage_attestation.journal_id,
            )
            schema = _inspect_completed_active_schema_read_only(
                active_ref,
                canonical_store_id=lineage_attestation.canonical_store_id,
                expected_generation=lineage_attestation.generation,
                expected_activation_digest=lineage_attestation.activation_digest,
            )
            with _open_completed_authority_read_connection(
                identity.canonical_sidecar_path
            ) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lineage_attestation.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    if connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchall() != [("ok",)] or connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall():
                        raise SQLiteStoreSchemaError(
                            "STORE.INTEGRITY_CHECK_FAILED"
                        )
                    runtime_facts = _read_source_binding_facts_in_transaction(
                        connection,
                        prior_view,
                    )
                    if (
                        runtime_facts.binding is None
                        or runtime_facts.diagnostic_codes
                        or snapshot_receipt_digest(
                            runtime_facts.binding.receipt
                        )
                        != lineage_attestation.snapshot_receipt_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_BINDING_INVALID"
                        )
                    _CoordinatorStorePort(self).validate_candidate_proof_index(
                        connection,
                        required_sizes=(
                            (1, 2) if schema.fts5_available else (1, 2, 3)
                        ),
                        fts5_available=schema.fts5_available,
                    )
                finally:
                    connection.rollback()
            prior_attestation = _create_portable_active_content_attestation(
                sealed_attestation_digest=(
                    lineage_attestation.sealed_attestation_digest
                ),
                journal_id=lineage_attestation.journal_id,
                resource_id=lineage_attestation.resource_id,
                target_identity=lineage_attestation.target_identity,
                canonical_store_id=lineage_attestation.canonical_store_id,
                snapshot_receipt_digest=(
                    lineage_attestation.snapshot_receipt_digest
                ),
                generation=lineage_attestation.generation,
                activation_digest=lineage_attestation.activation_digest,
                database=observed[0],
                manifest=observed[1],
                source=observed[2],
                semantic_facts=lineage_attestation.semantic_facts,
            )
            result = _PortableReplacementPriorAuthority(
                coordinator=self,
                platform=platform,
                root=root,
                caller_borrow=caller_borrow,
                prior_view=prior_view,
                authorities=cast(
                    tuple[BoundRegularFile, BoundRegularFile, BoundRegularFile],
                    tuple(authorities),
                ),
                names=names,
                proofs=cast(
                    tuple[
                        PortableContentFileProof,
                        PortableContentFileProof,
                        PortableContentFileProof,
                    ],
                    tuple(observed),
                ),
                lineage_attestation=lineage_attestation,
                prior_attestation=prior_attestation,
            )
            authorities.clear()
            return result
        except ActivationPreparationError:
            raise
        except (
            PlatformFileError,
            OSError,
            sqlite3.Error,
            SQLiteStoreSchemaError,
        ) as error:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            ) from error
        finally:
            active_error = sys.exception()
            close_error: BaseException | None = None
            for authority in reversed(authorities):
                try:
                    authority.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if active_error is None and close_error is not None:
                raise close_error

    def _begin_portable_replacement_backups(
        self,
        preparation: _PortableReplacementPreparation,
        *,
        private_directory_name: str,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> _PortableReplacementBackupSet:
        """Stream both prior assets into retained private publications."""

        if type(preparation) is not _PortableReplacementPreparation:
            raise TypeError("portable replacement preparation is invalid")
        if type(private_directory_name) is not str or not private_directory_name:
            raise TypeError("portable replacement private directory is invalid")
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")
        identity = self._resource_identity
        root, _lease = caller_borrow._publication_authorities(
            platform,
            persistent_private,
            identity,
        )
        private_parent: BoundDirectoryAuthority | None = None
        candidates: list[CandidateFile] = []
        owned_names: list[tuple[str, str, FileObjectIdentity]] = []
        pending: list[PendingPublication] = []
        try:
            preparation._prior_authority.reprove()
            private_parent = platform.bind_parent(
                root,
                PurePath(private_directory_name, "replacement-backup-placeholder"),
            )
            for plan in preparation.backup_plans:
                candidate_name = plan.backup_name + ".candidate"
                if (
                    private_parent.inspect_entry(candidate_name) is not None
                    or private_parent.inspect_entry(plan.backup_name) is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PENDING",
                        retryable=False,
                    )
                candidate = private_parent.create_candidate(
                    candidate_name,
                    private=True,
                )
                candidate_identity = candidate.identity()
                candidates.append(candidate)
                owned_names.append(
                    (candidate_name, plan.backup_name, candidate_identity)
                )
                chunks, maximum_bytes = preparation._prior_authority._stream_asset(
                    plan.asset_kind
                )
                facts = candidate.write_chunks(
                    chunks,
                    maximum_bytes=maximum_bytes,
                )
                if self._portable_content_proof(facts) != plan.content:
                    raise ActivationPreparationError(
                        "ACTIVATION.BACKUP_FAILED",
                        retryable=True,
                    )
                candidate.flush_content()
                try:
                    publication = private_parent.begin_publish(
                        candidate,
                        plan.backup_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                finally:
                    if not candidate.closed:
                        candidate.close()
                    candidates.remove(candidate)
                pending.append(publication)
                retained = publication.retained_destination()
                retained_facts = retained.content_facts()
                if (
                    retained_facts.snapshot.identity.kind != "regular"
                    or retained_facts.snapshot.identity.link_count != 1
                    or private_parent.inspect_entry(plan.backup_name)
                    != retained_facts.snapshot
                    or self._portable_content_proof(retained_facts) != plan.content
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                private_proof = platform.prove_private(retained)
                private_proof.close()
                preparation._prior_authority.reprove()
            if len(pending) != 2:
                raise AssertionError("portable replacement backup pair is incomplete")
            result = _PortableReplacementBackupSet(
                prior=preparation._prior_authority,
                private_parent=private_parent,
                pending=cast(
                    tuple[PendingPublication, PendingPublication],
                    tuple(pending),
                ),
                plans=preparation.backup_plans,
            )
            private_parent = None
            pending.clear()
            owned_names.clear()
            return result
        except ActivationPreparationError:
            raise
        except (PlatformFileError, OSError) as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED" if pending else "ACTIVATION.BACKUP_FAILED",
                retryable=True,
            ) from error
        finally:
            active_error = sys.exception()
            close_error: BaseException | None = None
            for candidate in reversed(candidates):
                try:
                    candidate.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            for publication in reversed(pending):
                try:
                    publication.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if private_parent is not None:
                for candidate_name, backup_name, expected_identity in reversed(
                    owned_names
                ):
                    for owned_name in (candidate_name, backup_name):
                        try:
                            observed = private_parent.inspect_entry(owned_name)
                            if observed is None:
                                continue
                            if observed.identity != expected_identity:
                                raise ActivationPreparationError(
                                    "ACTIVATION.RECOVERY_REQUIRED",
                                    retryable=True,
                                )
                            private_parent.unlink_owned(
                                owned_name,
                                expected_identity,
                            )
                            if private_parent.inspect_entry(owned_name) is not None:
                                raise ActivationPreparationError(
                                    "ACTIVATION.RECOVERY_REQUIRED",
                                    retryable=True,
                                )
                        except BaseException as error:
                            if close_error is None:
                                close_error = error
            if private_parent is not None:
                try:
                    private_parent.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if close_error is not None:
                if active_error is None:
                    raise close_error
                if type(active_error) not in (
                    TypeError,
                    AssertionError,
                    AttributeError,
                ):
                    if isinstance(close_error, ActivationPreparationError) and (
                        close_error.code == "ACTIVATION.RECOVERY_REQUIRED"
                        and close_error.retryable
                    ):
                        raise close_error
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from close_error

    def _cleanup_portable_replacement_prepared_namespace(
        self,
        snapshot: _PortableReplacementNamespaceSnapshot,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> None:
        """Retire only one authenticated PREPARED tail after prior reproof."""

        if (
            type(snapshot) is not _PortableReplacementNamespaceSnapshot
            or snapshot.state != "PENDING"
            or snapshot.highest_pending_phase != "PREPARED"
            or len(snapshot.pending_records) != 1
            or type(snapshot.pending_records[0])
            is not _PortableReplacementRecord
            or snapshot.pending_records[0].unsigned.phase != "PREPARED"
            or len(snapshot.backup_proofs) != 2
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable replacement cleanup borrow is invalid")

        identity = self._resource_identity
        prepared = snapshot.pending_records[0]
        current = snapshot.current_record
        private_name = _portable_activation_private_directory_name(identity)
        if (
            prepared.unsigned.private_directory_name != private_name
            or prepared.unsigned.backup_proofs != snapshot.backup_proofs
            or (
                current is not None
                and (
                    type(current) is not _PortableReplacementRecord
                    or current.unsigned.phase != "READY"
                    or current.unsigned.private_directory_name != private_name
                )
            )
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )

        root, _lease = caller_borrow._publication_authorities(
            platform,
            persistent_private,
            identity,
        )
        private_parent: BoundDirectoryAuthority | None = None
        opened: list[BoundRegularFile] = []
        targets: dict[str, Any] = {}
        current_snapshot: Any = None
        try:
            private_parent = platform.bind_parent(
                root,
                PurePath(private_name, "replacement-prepared-cleanup-placeholder"),
            )
            if current is not None:
                current_file = platform.open_regular(
                    root,
                    PurePath(private_name, _PORTABLE_REPLACEMENT_CURRENT_NAME),
                )
                opened.append(current_file)
                current_facts = current_file.content_facts()
                if (
                    current_file.read_all()
                    != _serialize_portable_replacement_record(current)
                    or current_facts.snapshot.identity.kind != "regular"
                    or current_facts.snapshot.identity.link_count != 1
                    or private_parent.inspect_entry(
                        _PORTABLE_REPLACEMENT_CURRENT_NAME
                    )
                    != current_facts.snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                current_snapshot = current_facts.snapshot

            prepared_name = _portable_replacement_phase_name("PREPARED")
            prepared_file = platform.open_regular(
                root,
                PurePath(private_name, prepared_name),
            )
            opened.append(prepared_file)
            prepared_facts = prepared_file.content_facts()
            if (
                prepared_file.read_all()
                != _serialize_portable_replacement_record(prepared)
                or prepared_facts.snapshot.identity.kind != "regular"
                or prepared_facts.snapshot.identity.link_count != 1
                or private_parent.inspect_entry(prepared_name)
                != prepared_facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            targets[prepared_name] = prepared_facts.snapshot

            for backup in snapshot.backup_proofs:
                backup_file = platform.open_regular(
                    root,
                    PurePath(private_name, backup.backup_name),
                )
                opened.append(backup_file)
                facts = backup_file.content_facts()
                if (
                    self._portable_content_proof(facts) != backup.content
                    or facts.snapshot.identity.kind != "regular"
                    or facts.snapshot.identity.link_count != 1
                    or private_parent.inspect_entry(backup.backup_name)
                    != facts.snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                targets[backup.backup_name] = facts.snapshot

            caller_borrow.reprove(platform, identity)
            target_start = 1 if current is not None else 0
            for authority in reversed(opened[target_start:]):
                authority.close()
            del opened[target_start:]
            for name in (
                *(backup.backup_name for backup in snapshot.backup_proofs),
                prepared_name,
            ):
                expected = targets[name]
                if private_parent.inspect_entry(name) != expected:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                private_parent.unlink_owned(name, expected.identity)
                if private_parent.inspect_entry(name) is not None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                if current is not None and private_parent.inspect_entry(
                    _PORTABLE_REPLACEMENT_CURRENT_NAME
                ) != current_snapshot:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                caller_borrow.reprove(platform, identity)
            if current is not None and (
                opened[0].content_facts().snapshot != current_snapshot
                or opened[0].read_all()
                != _serialize_portable_replacement_record(current)
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
        finally:
            active_error = sys.exception()
            close_error: BaseException | None = None
            for authority in reversed(opened):
                try:
                    authority.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if private_parent is not None:
                try:
                    private_parent.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if active_error is None and close_error is not None:
                raise close_error

    def _cleanup_portable_replacement_ready_namespace(
        self,
        snapshot: _PortableReplacementNamespaceSnapshot,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> None:
        """Delete only the authenticated READY_CLEANUP tail under the same W1."""

        if (
            type(snapshot) is not _PortableReplacementNamespaceSnapshot
            or snapshot.state != "READY_CLEANUP"
            or type(snapshot.current_record) is not _PortableReplacementRecord
            or snapshot.current_record.unsigned.phase != "READY"
            or len(snapshot.pending_records) != 4
            or tuple(
                record.unsigned.phase for record in snapshot.pending_records
            )
            != (
                "PREPARED",
                "DB_REPLACED",
                "MANIFEST_PUBLISHED",
                "GENERATION_PUBLISHED",
            )
            or len(snapshot.backup_proofs) != 2
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("portable replacement cleanup borrow is invalid")

        identity = self._resource_identity
        current = snapshot.current_record
        pending = snapshot.pending_records
        prepared = pending[0]
        if (
            current.unsigned.private_directory_name
            != _portable_activation_private_directory_name(identity)
            or any(
                record.unsigned.private_directory_name
                != current.unsigned.private_directory_name
                for record in pending
            )
            or prepared.unsigned.backup_proofs != snapshot.backup_proofs
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )

        root, _lease = caller_borrow._publication_authorities(
            platform,
            persistent_private,
            identity,
        )
        private_parent: BoundDirectoryAuthority | None = None
        opened: list[BoundRegularFile] = []
        target_snapshots: dict[str, Any] = {}
        current_snapshot: Any = None
        try:
            private_parent = platform.bind_parent(
                root,
                PurePath(
                    current.unsigned.private_directory_name,
                    "replacement-ready-cleanup-placeholder",
                ),
            )
            current_file = platform.open_regular(
                root,
                PurePath(
                    current.unsigned.private_directory_name,
                    _PORTABLE_REPLACEMENT_CURRENT_NAME,
                ),
            )
            opened.append(current_file)
            current_facts = current_file.content_facts()
            if (
                current_file.read_all()
                != _serialize_portable_replacement_record(current)
                or current_facts.snapshot.identity.kind != "regular"
                or current_facts.snapshot.identity.link_count != 1
                or private_parent.inspect_entry(
                    _PORTABLE_REPLACEMENT_CURRENT_NAME
                )
                != current_facts.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
            current_snapshot = current_facts.snapshot

            for record in pending:
                name = _portable_replacement_phase_name(record.unsigned.phase)
                record_file = platform.open_regular(
                    root,
                    PurePath(current.unsigned.private_directory_name, name),
                )
                opened.append(record_file)
                facts = record_file.content_facts()
                if (
                    record_file.read_all()
                    != _serialize_portable_replacement_record(record)
                    or facts.snapshot.identity.kind != "regular"
                    or facts.snapshot.identity.link_count != 1
                    or private_parent.inspect_entry(name) != facts.snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                target_snapshots[name] = facts.snapshot
            for backup in snapshot.backup_proofs:
                backup_file = platform.open_regular(
                    root,
                    PurePath(
                        current.unsigned.private_directory_name,
                        backup.backup_name,
                    ),
                )
                opened.append(backup_file)
                facts = backup_file.content_facts()
                if (
                    self._portable_content_proof(facts) != backup.content
                    or facts.snapshot.identity.kind != "regular"
                    or facts.snapshot.identity.link_count != 1
                    or private_parent.inspect_entry(backup.backup_name)
                    != facts.snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                target_snapshots[backup.backup_name] = facts.snapshot

            caller_borrow.reprove(platform, identity)
            for authority in reversed(opened[1:]):
                authority.close()
            del opened[1:]
            cleanup_names = (
                *(
                    _portable_replacement_phase_name(record.unsigned.phase)
                    for record in reversed(pending)
                ),
                *(backup.backup_name for backup in snapshot.backup_proofs),
            )
            for name in cleanup_names:
                expected = target_snapshots[name]
                if private_parent.inspect_entry(name) != expected:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                private_parent.unlink_owned(name, expected.identity)
                if (
                    private_parent.inspect_entry(name) is not None
                    or private_parent.inspect_entry(
                        _PORTABLE_REPLACEMENT_CURRENT_NAME
                    )
                    != current_snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                caller_borrow.reprove(platform, identity)
            final_current = opened[0].content_facts()
            if (
                final_current.snapshot != current_snapshot
                or opened[0].read_all()
                != _serialize_portable_replacement_record(current)
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
        finally:
            active_error = sys.exception()
            close_error: BaseException | None = None
            for authority in reversed(opened):
                try:
                    authority.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if private_parent is not None:
                try:
                    private_parent.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if active_error is None and close_error is not None:
                raise close_error

    @staticmethod
    def _portable_owner_activation_fields(
        prepared: _PortableActivationJournalRecord | _PortableReplacementRecord,
    ) -> tuple[
        PortableSealedContentAttestation,
        str,
        str,
        str,
        str,
        int,
    ]:
        """Normalize owner facts without widening either persisted codec."""

        if type(prepared) is _PortableActivationJournalRecord:
            unsigned = prepared.unsigned
            return (
                unsigned.sealed_content_attestation,
                unsigned.journal_id,
                unsigned.canonical_store_id,
                unsigned.snapshot_receipt_digest,
                unsigned.new_receipt_id,
                0,
            )
        if type(prepared) is _PortableReplacementRecord:
            unsigned = prepared.unsigned
            return (
                unsigned.sealed_content_attestation,
                unsigned.journal_id,
                unsigned.candidate_canonical_store_id,
                unsigned.sealed_content_attestation.snapshot_receipt_digest,
                unsigned.new_receipt_id,
                unsigned.next_generation,
            )
        raise TypeError("portable activation owner record is invalid")

    def _apply_portable_receipt_activation(
        self,
        *,
        binding: SnapshotBinding,
        prepared: _PortableActivationJournalRecord | _PortableReplacementRecord,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        activation_digest: str,
        allow_sealed_transition: bool,
    ) -> tuple[
        PortableContentFileProof,
        str,
        BoundExistingFileMutationGuard,
    ]:
        """Complete only the first-generation SQLite owner transaction."""

        if type(allow_sealed_transition) is not bool:
            raise TypeError("portable SEALED transition mode must be bool")

        identity = self._resource_identity
        (
            sealed,
            _journal_id,
            owner_store_id,
            owner_receipt_digest,
            owner_receipt_id,
            owner_generation,
        ) = self._portable_owner_activation_fields(prepared)
        database_name = identity.canonical_sidecar_path.name
        opened: BoundRegularFile | None = None
        synchronized: BoundSynchronizedRegularFile | None = None
        mutation_guard: BoundExistingFileMutationGuard | None = None
        try:
            if not isinstance(platform, ExistingFileMutationGuard):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                    retryable=False,
                )
            mutation_guard = platform.guard_existing_for_mutation(
                root,
                PurePath(database_name),
            )
            mutation_guard.reprove()
            opened = platform.open_regular(root, PurePath(database_name))
            before = opened.content_facts()
            before_proof = self._portable_content_proof(before)
            if (
                opened.identity().kind != "regular"
                or opened.identity().link_count != 1
                or root.inspect_entry(database_name) != before.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECEIPT_PUBLICATION_INVALID",
                    retryable=False,
                )
            opened.close()
            opened = None

            receipt = binding.receipt
            with _open_configured_connection(
                identity.canonical_sidecar_path,
                require_existing=True,
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    meta = _read_meta(connection)
                    if (
                        meta.get("resource_id") != identity.resource_id
                        or meta.get("canonical_store_id")
                        != owner_store_id
                        or meta.get("target_identity") != identity.target_identity
                        or (
                            meta.get("activation_status") == "SEALED"
                            and _meta_int(meta, "generation") != 0
                        )
                        or (
                            meta.get("activation_status") == "ACTIVE"
                            and _meta_int(meta, "generation") != owner_generation
                        )
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                    expected_receipt = (
                        receipt.snapshot_id,
                        receipt.resource_id,
                        receipt.canonical_store_id,
                        receipt.exported_revision,
                        receipt.jsonl_digest,
                        receipt.record_count,
                        receipt.format_version,
                        Path.__str__(identity.configured_jsonl_path),
                        Path.__str__(identity.snapshot_manifest_path),
                    )
                    rows = connection.execute(
                        "SELECT snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status "
                        "FROM tm_snapshot_receipt ORDER BY snapshot_id"
                    ).fetchall()
                    binding_rows = connection.execute(
                        "SELECT configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version "
                        "FROM tm_snapshot_binding ORDER BY binding_id"
                    ).fetchall()
                    expected_binding = (
                        Path.__str__(identity.configured_jsonl_path),
                        Path.__str__(identity.snapshot_manifest_path),
                        binding.snapshot_kind.value,
                        receipt.snapshot_id,
                        binding.binding_version,
                    )
                    if (
                        receipt.snapshot_id != owner_receipt_id
                        or snapshot_receipt_digest(receipt)
                        != owner_receipt_digest
                    ):
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                    if meta.get("activation_status") == "SEALED":
                        if not allow_sealed_transition:
                            raise ActivationPreparationError(
                                "ACTIVATION.RECOVERY_REQUIRED",
                                retryable=True,
                            )
                        if (
                            before_proof != sealed.database
                            or
                            "activation_digest" in meta
                            or rows != [expected_receipt + ("issued",)]
                            or binding_rows
                        ):
                            raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                        updated = connection.execute(
                            "UPDATE tm_snapshot_receipt SET status = 'completed' "
                            "WHERE snapshot_id = ? AND status = 'issued'",
                            (receipt.snapshot_id,),
                        )
                        if updated.rowcount != 1:
                            raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                        connection.execute(
                            "INSERT INTO tm_snapshot_binding("
                            "binding_id, configured_jsonl_path, manifest_path, "
                            "snapshot_kind, snapshot_id, binding_version) "
                            "VALUES (1, ?, ?, ?, ?, ?)",
                            expected_binding,
                        )
                        status = connection.execute(
                            "UPDATE tm_meta SET value = 'ACTIVE' "
                            "WHERE key = 'activation_status' AND value = 'SEALED'"
                        )
                        generation = connection.execute(
                            "UPDATE tm_meta SET value = ? "
                            "WHERE key = 'generation' AND value = '0'",
                            (str(owner_generation),),
                        )
                        connection.execute(
                            "INSERT INTO tm_meta(key, value) VALUES "
                            "('activation_digest', ?)",
                            (activation_digest,),
                        )
                        if status.rowcount != 1 or generation.rowcount != 1:
                            raise SQLiteStoreSchemaError(
                                "STORE.ACTIVATION_STATE_INVALID"
                            )
                    elif meta.get("activation_status") == "ACTIVE":
                        if (
                            meta.get("activation_digest") != activation_digest
                            or _meta_int(meta, "generation") != owner_generation
                            or rows != [expected_receipt + ("completed",)]
                            or binding_rows != [expected_binding]
                        ):
                            raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                    else:
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                    closure_digest = cast(
                        str,
                        importlib.import_module(
                            "tm_stage_sealer"
                        )._stage_closure_digest(connection),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

            mutation_guard.reprove()

            # The retained publication handle was deliberately closed before
            # SQLite acquired a writer.  Reopen the exact canonical object and
            # make the committed bytes durable through the platform port.
            synchronized = platform.open_existing_for_synchronization(
                root,
                PurePath(database_name),
            )
            after = synchronized.content_facts()
            synchronized.synchronize_content(after)
            final = synchronized.content_facts()
            if (
                final != after
                or final.snapshot.identity.kind != "regular"
                or final.snapshot.identity.link_count != 1
                or root.inspect_entry(database_name) != final.snapshot
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECEIPT_PUBLICATION_INVALID",
                    retryable=False,
                )
            mutation_guard.reprove()
            synchronized.close()
            synchronized = None
            transferred_guard = mutation_guard
            mutation_guard = None
            return (
                self._portable_content_proof(final),
                closure_digest,
                transferred_guard,
            )
        except ActivationPreparationError:
            raise
        except (PlatformFileError, OSError, sqlite3.Error, SQLiteStoreSchemaError) as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECEIPT_PUBLICATION_FAILED",
                retryable=True,
            ) from error
        finally:
            active_error = sys.exception()
            first_cleanup_error: BaseException | None = None
            for authority in (opened, synchronized, mutation_guard):
                if authority is None:
                    continue
                try:
                    authority.close()
                except BaseException as cleanup_error:
                    if first_cleanup_error is None:
                        first_cleanup_error = cleanup_error
            if active_error is None and first_cleanup_error is not None:
                raise first_cleanup_error

    def _reprove_portable_active_set(
        self,
        *,
        binding: SnapshotBinding,
        prepared: _PortableActivationJournalRecord | _PortableReplacementRecord,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        database: PortableContentFileProof,
        manifest: PortableContentFileProof,
        activation_digest: str,
        expected_logical_closure_digest: str,
        database_guard: BoundExistingFileMutationGuard,
        expected_candidate_projection_digest: str | None = None,
    ) -> tuple[
        _CanonicalStoreRef,
        SQLiteSchemaSnapshot,
        PortableActiveContentAttestation,
        _PortableActiveSetAuthority,
    ]:
        """Rebuild portable ACTIVE facts from live handles and SQLite truth."""

        identity = self._resource_identity
        (
            sealed,
            owner_journal_id,
            owner_store_id,
            owner_receipt_digest,
            owner_receipt_id,
            owner_generation,
        ) = self._portable_owner_activation_fields(prepared)
        authorities: list[BoundRegularFile] = []
        owned_database_guard: BoundExistingFileMutationGuard | None = database_guard
        try:
            if not isinstance(database_guard, BoundExistingFileMutationGuard):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                    retryable=False,
                )
            database_guard.reprove()
            observed_proofs: list[PortableContentFileProof] = []
            for name, expected in (
                (identity.canonical_sidecar_path.name, database),
                (identity.snapshot_manifest_path.name, manifest),
                (identity.configured_jsonl_path.name, sealed.source),
            ):
                authority = platform.open_regular(root, PurePath(name))
                authorities.append(authority)
                facts = authority.content_facts()
                if (
                    facts.snapshot.identity.kind != "regular"
                    or facts.snapshot.identity.link_count != 1
                    or root.inspect_entry(name) != facts.snapshot
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.ACTIVE_SET_INVALID",
                        retryable=False,
                    )
                proof = self._portable_content_proof(facts)
                if proof != expected:
                    raise ActivationPreparationError(
                        "ACTIVATION.ACTIVE_SET_INVALID",
                        retryable=False,
                    )
                observed_proofs.append(proof)
            manifest_bytes = authorities[1].read_all()
            try:
                decoded_manifest = contract_from_json(
                    manifest_bytes.decode("utf-8")
                )
            except (TypeError, ValueError, UnicodeDecodeError) as error:
                raise ActivationPreparationError(
                    "ACTIVATION.ACTIVE_SET_INVALID",
                    retryable=False,
                ) from error
            if type(decoded_manifest) is not SnapshotManifest or (
                decoded_manifest != binding.manifest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.ACTIVE_SET_INVALID",
                    retryable=False,
                )
            # The database read authority denies SQLite's writer.  Keep the
            # manifest and configured source live, but release only that DB
            # reader while the non-deleting mutation guard protects identity.
            database_before = authorities.pop(0)
            database_before.close()
            database_guard.reprove()

            active_ref = _canonical_activation_ref(
                identity,
                journal_id=owner_journal_id,
            )
            snapshot = inspect_stage_schema(
                active_ref,
                canonical_store_id=owner_store_id,
                _allow_diverged_runtime=True,
                _allow_active=True,
                _expected_active_generation=owner_generation,
                _expected_activation_digest=activation_digest,
            )
            sealed_semantic = sealed.semantic_facts
            if (
                snapshot.schema_version != sealed_semantic.schema_version
                or snapshot.fold_version != sealed_semantic.fold_version
                or snapshot.candidate_index_version
                != sealed_semantic.index_version
                or snapshot.candidate_index_kind
                != sealed_semantic.candidate_index_kind
                or snapshot.fts5_available != sealed_semantic.fts5_available
                or snapshot.sqlite_runtime_version
                != sealed_semantic.sqlite_runtime_version
                or snapshot.unicode_runtime_version
                != sealed_semantic.unicode_runtime_version
                or snapshot.journal_mode != sealed_semantic.journal_mode
                or snapshot.synchronous != sealed_semantic.synchronous
                or snapshot.foreign_keys != sealed_semantic.foreign_keys
                or snapshot.busy_timeout_ms != sealed_semantic.busy_timeout_ms
                or snapshot.wal_enabled != sealed_semantic.wal_enabled
                or snapshot.extension_loading_enabled
                != sealed_semantic.extension_loading_enabled
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.ACTIVE_SET_INVALID",
                    retryable=False,
                )

            port = _CoordinatorStorePort(self)
            with _open_configured_connection(
                identity.canonical_sidecar_path,
                require_existing=True,
            ) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=owner_store_id,
                        target_identity=identity.target_identity,
                    )
                    active_meta = _read_meta(connection)
                    if (
                        active_meta.get("schema_digest")
                        != sealed_semantic.schema_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_LOGICAL_CLOSURE_INVALID"
                        )
                    record_count = _table_count(connection, "tm_record")
                    origin_batch_count = _table_count(
                        connection,
                        "tm_origin_batch",
                    )
                    exact_parity_digest = _activation_exact_parity_digest(
                        port,
                        connection,
                    )
                    if (
                        record_count != sealed_semantic.record_count
                        or origin_batch_count
                        != sealed_semantic.origin_batch_count
                        or exact_parity_digest
                        != sealed_semantic.exact_parity_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_COUNT_MISMATCH"
                        )
                    origin_rows = connection.execute(
                        "SELECT batch_id, kind, status FROM tm_origin_batch "
                        "ORDER BY batch_id"
                    ).fetchall()
                    if origin_rows != [
                        (
                            sealed_semantic.origin_batch_id,
                            sealed_semantic.origin_batch_kind,
                            "completed",
                        )
                    ]:
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_COUNT_MISMATCH"
                        )
                    if expected_candidate_projection_digest is None:
                        gram_counts, fts_count = _validate_activation_indexes(
                            port,
                            connection,
                            semantic_facts=sealed_semantic,
                            fts5_available=snapshot.fts5_available,
                        )
                    else:
                        try:
                            candidate_projection_digest = (
                                _candidate_proof_projection_digest(
                                    connection,
                                    fts5_available=snapshot.fts5_available,
                                )
                            )
                        except (
                            CandidateProofIndexError,
                            sqlite3.DatabaseError,
                        ) as error:
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_INDEX_INVALID"
                            ) from error
                        if (
                            candidate_projection_digest
                            != expected_candidate_projection_digest
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_INDEX_INVALID"
                            )
                        gram_counts = sealed_semantic.gram_counts
                        fts_count = sealed_semantic.fts_count
                    if (
                        sealed_semantic.receipt_boundary_record_count
                        == record_count
                        and sealed_semantic.receipt_boundary_fts_count
                        != fts_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_COUNT_MISMATCH"
                        )
                    lease = _SQLiteGenerationView(
                        stage=active_ref,
                        canonical_store_id=owner_store_id,
                        generation=owner_generation,
                        fts5_available=snapshot.fts5_available,
                    )
                    source_facts = _read_source_binding_facts_in_transaction(
                        connection,
                        lease,
                    )
                    receipt = binding.receipt
                    if (
                        source_facts.binding != binding
                        or source_facts.divergence_latched
                        or source_facts.diagnostic_codes
                        or receipt.snapshot_id != owner_receipt_id
                        or snapshot_receipt_digest(receipt)
                        != owner_receipt_digest
                        or receipt.jsonl_digest != sealed.source.sha256
                        or receipt.record_count
                        != sealed_semantic.receipt_boundary_record_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_BINDING_INVALID"
                        )
                    logical_closure_digest = cast(
                        str,
                        importlib.import_module(
                            "tm_stage_sealer"
                        )._stage_closure_digest(connection),
                    )
                    if logical_closure_digest != expected_logical_closure_digest:
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVE_LOGICAL_CLOSURE_INVALID"
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

            database_authority = platform.open_regular(
                root,
                PurePath(identity.canonical_sidecar_path.name),
            )
            database_facts = database_authority.content_facts()
            if (
                self._portable_content_proof(database_facts)
                != observed_proofs[0]
                or root.inspect_entry(identity.canonical_sidecar_path.name)
                != database_facts.snapshot
            ):
                database_authority.close()
                raise ActivationPreparationError(
                    "ACTIVATION.ACTIVE_SET_INVALID",
                    retryable=False,
                )
            authorities.insert(0, database_authority)
            database_guard.reprove()

            semantic = ContentSemanticFacts(
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
                receipt_boundary_record_count=(
                    sealed_semantic.receipt_boundary_record_count
                ),
                origin_batch_count=origin_batch_count,
                origin_batch_id=sealed_semantic.origin_batch_id,
                origin_batch_kind=sealed_semantic.origin_batch_kind,
                exported_revision=binding.receipt.exported_revision,
                fts_count=fts_count,
                receipt_boundary_fts_count=(
                    sealed_semantic.receipt_boundary_fts_count
                ),
                gram_counts=gram_counts,
                exact_parity_digest=exact_parity_digest,
                logical_closure_version=LOGICAL_CLOSURE_VERSION,
                logical_closure_digest=logical_closure_digest,
            )
            active = _create_portable_active_content_attestation(
                sealed_attestation_digest=sealed.attestation_digest,
                journal_id=owner_journal_id,
                resource_id=identity.resource_id,
                target_identity=identity.target_identity,
                canonical_store_id=owner_store_id,
                snapshot_receipt_digest=owner_receipt_digest,
                generation=owner_generation,
                activation_digest=activation_digest,
                database=observed_proofs[0],
                manifest=observed_proofs[1],
                source=observed_proofs[2],
                semantic_facts=semantic,
            )
            live = _PortableActiveSetAuthority(
                coordinator=self,
                root=root,
                database_guard=database_guard,
                authorities=cast(
                    tuple[BoundRegularFile, BoundRegularFile, BoundRegularFile],
                    tuple(authorities),
                ),
                names=(
                    identity.canonical_sidecar_path.name,
                    identity.snapshot_manifest_path.name,
                    identity.configured_jsonl_path.name,
                ),
                proofs=cast(
                    tuple[
                        PortableContentFileProof,
                        PortableContentFileProof,
                        PortableContentFileProof,
                    ],
                    tuple(observed_proofs),
                ),
                attestation=active,
            )
            authorities.clear()
            owned_database_guard = None
            return active_ref, snapshot, active, live
        except ActivationPreparationError:
            raise
        except (PlatformFileError, OSError, sqlite3.Error, SQLiteStoreSchemaError) as error:
            raise ActivationPreparationError(
                "ACTIVATION.ACTIVE_SET_INVALID",
                retryable=False,
            ) from error
        finally:
            active_error = sys.exception()
            first_cleanup_error: BaseException | None = None
            cleanup_authorities: tuple[OpaqueAuthority, ...] = (
                *tuple(reversed(authorities)),
                *((owned_database_guard,) if owned_database_guard is not None else ()),
            )
            for authority in cleanup_authorities:
                try:
                    authority.close()
                except BaseException as cleanup_error:
                    if first_cleanup_error is None:
                        first_cleanup_error = cleanup_error
            if active_error is None and first_cleanup_error is not None:
                raise first_cleanup_error

    def _publish_portable_generation(
        self,
        *,
        preparation: _ActivationPreparation,
        prepared: _PortableActivationJournalRecord,
        predecessor: _PortablePublicationPhaseRecord,
        unsigned: _PortablePublicationPhaseUnsigned,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        owner_reprove: Callable[[], None],
        business_reprove: Callable[[_PortablePublicationPhaseRecord], None],
    ) -> _PortablePublicationPhaseHandle:
        """Withhold generation zero until its durable phase succeeds."""

        def generation_owner_commit(
            record: _PortablePublicationPhaseRecord,
        ) -> None:
            if record.unsigned != unsigned:
                raise ActivationPreparationError(
                    "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                    retryable=False,
                )
        return _WindowsPortablePublicationPhaseOwner.publish(
            identity=self._resource_identity,
            backend=platform,
            persistent_private=persistent_private,
            caller_borrow=caller_borrow,
            prepared=prepared,
            predecessor=predecessor,
            unsigned=unsigned,
            owner_reprove=owner_reprove,
            owner_commit=generation_owner_commit,
            business_reprove=business_reprove,
        )

    def _ensure_portable_activation_lineage_marker(
        self,
        *,
        platform: PlatformFileBackend,
        root: RootedDirectoryAuthority,
    ) -> None:
        identity = self._resource_identity
        marker_name = _activation_lineage_marker_path(identity).name
        candidate_name = _activation_lineage_marker_temp_path(
            _activation_lineage_marker_path(identity)
        ).name
        expected = _activation_lineage_marker_payload(identity)
        if root.inspect_entry(candidate_name) is not None:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_REQUIRED",
                retryable=True,
            )
        existing = root.inspect_entry(marker_name)
        if existing is not None:
            opened = platform.open_regular(root, PurePath(marker_name))
            try:
                if (
                    opened.identity().kind != "regular"
                    or opened.identity().link_count != 1
                    or opened.read_all() != expected
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
            finally:
                opened.close()
            return
        candidate = root.create_candidate(candidate_name, private=False)
        pending: PendingPublication | None = None
        try:
            candidate.write_all(expected)
            candidate.flush_content()
            pending = root.begin_publish(
                candidate,
                marker_name,
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            candidate = None
            retained = pending.retained_destination()
            if (
                retained.identity().kind != "regular"
                or retained.identity().link_count != 1
                or retained.read_all() != expected
                or pending.terminal_reproof() != pending.preliminary_facts()
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                )
        finally:
            if candidate is not None:
                candidate.close()
            if pending is not None:
                pending.close()

    def publish_portable_activation(
        self,
        preparation: _ActivationPreparation,
        prepared_handle: _PortablePreparedJournalHandle,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        retire_unpublished_stage: Callable[[], None],
    ) -> int:
        """Publish one Windows portable first activation through generation zero."""

        if type(preparation) is not _ActivationPreparation:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PREPARATION_INVALID",
                retryable=False,
            )
        if type(prepared_handle) is not _PortablePreparedJournalHandle:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_HANDLE_INVALID",
                retryable=False,
            )
        if not isinstance(platform, PlatformFileBackend) or (
            persistent_private is not platform
        ):
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")
        if not callable(retire_unpublished_stage):
            raise TypeError("retire_unpublished_stage must be callable")

        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._preparation is not preparation
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
                or self._view is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PUBLICATION_STATE_INVALID",
                    retryable=True,
                )
            prepared = prepared_handle._record
            prepared_unsigned = prepared.unsigned
            stage = preparation._sealed_stage
            token = preparation._token
            physical = preparation._physical_snapshot
            stage_sealer = importlib.import_module("tm_stage_sealer")
            portable_snapshot_type = getattr(
                stage_sealer,
                "_PortablePhysicalReadinessSnapshot",
            )
            stage_seal_error = getattr(stage_sealer, "StageSealError")
            if (
                type(physical) is not portable_snapshot_type
                or prepared_handle.preparation_id != preparation.preparation_id
                or prepared_unsigned.closure != "PENDING"
                or prepared_unsigned.phase != "PREPARED"
                or prepared_unsigned.preparation_id != preparation.preparation_id
                or prepared_unsigned.token_id != token.token_id
                or prepared_unsigned.token_version != token.token_version
                or prepared_unsigned.activation_nonce != token.activation_nonce
                or prepared_unsigned.resource_id != self._resource_id
                or prepared_unsigned.target_identity != self._target_identity
                or prepared_unsigned.canonical_store_id
                != self._canonical_store_id
                or prepared_unsigned.expected_prior_generation is not None
                or prepared_unsigned.sealed_content_attestation
                != physical.sealed_content_attestation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                    retryable=False,
                )

            root, _lease = caller_borrow._publication_authorities(
                platform,
                persistent_private,
                self._resource_identity,
            )
            identity = self._resource_identity
            if (
                identity.configured_jsonl_path.parent
                != identity.canonical_sidecar_path.parent
                or identity.snapshot_manifest_path.parent
                != identity.canonical_sidecar_path.parent
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                    retryable=False,
                )
            root.reprove()
            caller_borrow.reprove(platform, identity)
            if (
                root.inspect_entry(identity.canonical_sidecar_path.name)
                is not None
                or root.inspect_entry(identity.snapshot_manifest_path.name)
                is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.CANONICAL_TARGET_OCCUPIED",
                    retryable=False,
                )

            armed = False
            db_candidate: CandidateFile | None = None
            manifest_candidate: CandidateFile | None = None
            database_pending: PendingPublication | None = None
            manifest_pending: PendingPublication | None = None
            database_guard: BoundExistingFileMutationGuard | None = None
            active_set_authority: _PortableActiveSetAuthority | None = None

            def fresh_physical_borrow() -> Any:
                fresh = self._sealed_registry.resolve_physical_readiness(stage)
                if type(fresh) is not portable_snapshot_type or replace(
                    fresh,
                    live_reproof=physical.live_reproof,
                ) != physical:
                    try:
                        fresh.live_reproof._release()
                    except BaseException:
                        pass
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                return fresh.live_reproof

            def owner_reprove() -> None:
                if (
                    self._state != "ACTIVATING"
                    or self._preparation is not preparation
                    or self._cleanup_reservation is not None
                    or self._cleanup_in_progress
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_STATE_INVALID",
                        retryable=True,
                    )
                contract_module._validate_activation_token_for_stage(token, stage)
                borrow = fresh_physical_borrow()
                try:
                    borrow.reprove()
                finally:
                    borrow._release()
                caller_borrow.reprove(platform, identity)

            def phase_unsigned(
                phase: str,
                predecessor_digest: str,
                database: PortableContentFileProof,
                manifest: PortableContentFileProof,
                active: PortableActiveContentAttestation | None,
            ) -> _PortablePublicationPhaseUnsigned:
                return _PortablePublicationPhaseUnsigned(
                    publication_version=_PORTABLE_PUBLICATION_VERSION,
                    phase=phase,
                    predecessor_digest=predecessor_digest,
                    journal_id=prepared_unsigned.journal_id,
                    preparation_id=prepared_unsigned.preparation_id,
                    token_id=prepared_unsigned.token_id,
                    token_version=prepared_unsigned.token_version,
                    activation_nonce=prepared_unsigned.activation_nonce,
                    resource_id=prepared_unsigned.resource_id,
                    target_identity=prepared_unsigned.target_identity,
                    canonical_store_id=prepared_unsigned.canonical_store_id,
                    generation=0,
                    canonical_database_name=identity.canonical_sidecar_path.name,
                    canonical_database_size=database.size,
                    canonical_database_sha256=database.sha256,
                    canonical_manifest_name=identity.snapshot_manifest_path.name,
                    canonical_manifest_size=manifest.size,
                    canonical_manifest_sha256=manifest.sha256,
                    private_directory_name=(
                        prepared_unsigned.private_directory_name
                    ),
                    device_key_name=prepared_unsigned.device_key_name,
                    sealed_content_attestation=(
                        prepared_unsigned.sealed_content_attestation
                    ),
                    active_content_attestation=active,
                )

            def activation_digest() -> str:
                return hashlib.sha256(
                    json.dumps(
                        {
                            "activation_nonce": prepared_unsigned.activation_nonce,
                            "artifact_id": prepared_unsigned.artifact_id,
                            "evidence_digest": prepared_unsigned.evidence_digest,
                            "generation": 0,
                            "journal_id": prepared_unsigned.journal_id,
                            "manifest_digest": (
                                prepared_unsigned.new_manifest_digest
                            ),
                            "sealed_stage_digest": (
                                prepared_unsigned.sealed_stage_digest
                            ),
                            "token_id": prepared_unsigned.token_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()

            try:
                owner_reprove()
                db_borrow = fresh_physical_borrow()
                try:
                    db_candidate, db_candidate_facts = (
                        db_borrow.copy_asset_to_new_candidate(
                            platform=platform,
                            parent=root,
                            asset="database",
                            candidate_name=(
                                identity.canonical_sidecar_path.name
                                + ".activation-candidate"
                            ),
                        )
                    )
                finally:
                    db_borrow._release()
                armed = True
                if (
                    self._portable_content_proof(db_candidate_facts)
                    != prepared_unsigned.sealed_content_attestation.database
                ):
                    db_candidate.close()
                    db_candidate = None
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                db_candidate.flush_content()
                try:
                    database_pending = root.begin_publish(
                        db_candidate,
                        identity.canonical_sidecar_path.name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                finally:
                    db_candidate = None
                db_retained = database_pending.retained_destination()
                sealed_database = self._portable_content_proof(
                    db_retained.content_facts()
                )
                if sealed_database != prepared_unsigned.sealed_content_attestation.database:
                    raise ActivationPreparationError(
                        "ACTIVATION.DB_REPLACE_UNPROVEN",
                        retryable=False,
                    )
                db_unsigned = phase_unsigned(
                    "DB_REPLACED",
                    prepared.record_digest,
                    sealed_database,
                    prepared_unsigned.sealed_content_attestation.manifest,
                    None,
                )

                def db_business(record: _PortablePublicationPhaseRecord) -> None:
                    if record.unsigned != db_unsigned:
                        raise ActivationPreparationError(
                            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                            retryable=False,
                        )
                    owner_reprove()
                    if self._portable_content_proof(
                        db_retained.content_facts()
                    ) != sealed_database:
                        raise ActivationPreparationError(
                            "ACTIVATION.DB_REOPEN_INVALID",
                            retryable=False,
                        )
                    readonly = sqlite3.connect(
                        f"{identity.canonical_sidecar_path.as_uri()}?mode=ro",
                        uri=True,
                        timeout=BUSY_TIMEOUT_MS / 1000,
                        isolation_level=None,
                    )
                    try:
                        readonly.execute("PRAGMA query_only = ON")
                        meta = _read_meta(readonly)
                        if (
                            meta.get("resource_id") != identity.resource_id
                            or meta.get("canonical_store_id")
                            != prepared_unsigned.canonical_store_id
                            or meta.get("target_identity")
                            != identity.target_identity
                            or meta.get("activation_status") != "SEALED"
                            or "activation_digest" in meta
                            or _meta_int(meta, "generation") != 0
                        ):
                            raise ActivationPreparationError(
                                "ACTIVATION.DB_REOPEN_INVALID",
                                retryable=False,
                            )
                    finally:
                        readonly.close()

                def db_owner_commit(
                    record: _PortablePublicationPhaseRecord,
                ) -> None:
                    if record.unsigned != db_unsigned:
                        raise ActivationPreparationError(
                            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                            retryable=False,
                        )
                db_phase = _WindowsPortablePublicationPhaseOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    prepared=prepared,
                    predecessor=prepared,
                    unsigned=db_unsigned,
                    owner_reprove=owner_reprove,
                    owner_commit=db_owner_commit,
                    business_reprove=db_business,
                )
                if (
                    database_pending.terminal_reproof()
                    != database_pending.preliminary_facts()
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                database_pending.close()
                database_pending = None

                sealed_snapshot = inspect_stage_schema(
                    _canonical_activation_ref(
                        identity,
                        journal_id=prepared_unsigned.journal_id,
                    ),
                    canonical_store_id=prepared_unsigned.canonical_store_id,
                    _allow_sealed=True,
                )
                if sealed_snapshot.activation_status != "SEALED":
                    raise ActivationPreparationError(
                        "ACTIVATION.DB_REOPEN_INVALID",
                        retryable=False,
                    )

                digest = activation_digest()
                active_database, logical_closure, database_guard = (
                    self._apply_portable_receipt_activation(
                        binding=physical.evidence.source_binding,
                        prepared=prepared,
                        platform=platform,
                        root=root,
                        activation_digest=digest,
                        allow_sealed_transition=True,
                    )
                )
                database_guard.reprove()

                manifest_borrow = fresh_physical_borrow()
                try:
                    manifest_candidate, manifest_candidate_facts = (
                        manifest_borrow.copy_asset_to_new_candidate(
                            platform=platform,
                            parent=root,
                            asset="manifest",
                            candidate_name=(
                                identity.snapshot_manifest_path.name
                                + ".activation-candidate"
                            ),
                        )
                    )
                finally:
                    manifest_borrow._release()
                if (
                    self._portable_content_proof(manifest_candidate_facts)
                    != prepared_unsigned.sealed_content_attestation.manifest
                ):
                    manifest_candidate.close()
                    manifest_candidate = None
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                manifest_candidate.flush_content()
                try:
                    manifest_pending = root.begin_publish(
                        manifest_candidate,
                        identity.snapshot_manifest_path.name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                finally:
                    manifest_candidate = None
                manifest_retained = manifest_pending.retained_destination()
                active_manifest = self._portable_content_proof(
                    manifest_retained.content_facts()
                )
                if (
                    active_manifest
                    != prepared_unsigned.sealed_content_attestation.manifest
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.MANIFEST_PUBLICATION_UNPROVEN",
                        retryable=False,
                    )
                if database_guard is None:
                    raise AssertionError("portable database mutation guard is missing")
                (
                    active_ref,
                    active_snapshot,
                    active_attestation,
                    active_set_authority,
                ) = (
                    self._reprove_portable_active_set(
                        binding=physical.evidence.source_binding,
                        prepared=prepared,
                        platform=platform,
                        root=root,
                        database=active_database,
                        manifest=active_manifest,
                        activation_digest=digest,
                        expected_logical_closure_digest=logical_closure,
                        database_guard=database_guard,
                        expected_candidate_projection_digest=(
                            physical.candidate_projection_digest
                        ),
                    )
                )
                database_guard = None
                manifest_unsigned = phase_unsigned(
                    "MANIFEST_PUBLISHED",
                    db_phase.record_digest,
                    active_database,
                    active_manifest,
                    active_attestation,
                )

                def active_business(record: _PortablePublicationPhaseRecord) -> None:
                    if record.unsigned.active_content_attestation != active_attestation:
                        raise ActivationPreparationError(
                            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                            retryable=False,
                        )
                    if active_set_authority is None:
                        raise AssertionError("portable active-set authority is missing")
                    observed = active_set_authority.reprove()
                    if observed != active_attestation:
                        raise ActivationPreparationError(
                            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                            retryable=False,
                        )

                def manifest_owner_commit(
                    record: _PortablePublicationPhaseRecord,
                ) -> None:
                    if record.unsigned != manifest_unsigned:
                        raise ActivationPreparationError(
                            "ACTIVATION.PUBLICATION_CHAIN_INVALID",
                            retryable=False,
                        )
                manifest_phase = _WindowsPortablePublicationPhaseOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    prepared=prepared,
                    predecessor=db_phase._record,
                    unsigned=manifest_unsigned,
                    owner_reprove=owner_reprove,
                    owner_commit=manifest_owner_commit,
                    business_reprove=active_business,
                )
                if (
                    manifest_pending.terminal_reproof()
                    != manifest_pending.preliminary_facts()
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                manifest_pending.close()
                manifest_pending = None

                view = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=prepared_unsigned.canonical_store_id,
                    generation=0,
                    fts5_available=active_snapshot.fts5_available,
                    active_content_attestation=active_attestation,
                )
                generation_unsigned = phase_unsigned(
                    "GENERATION_PUBLISHED",
                    manifest_phase.record_digest,
                    active_database,
                    active_manifest,
                    active_attestation,
                )
                _ = self._publish_portable_generation(
                    preparation=preparation,
                    prepared=prepared,
                    predecessor=manifest_phase._record,
                    unsigned=generation_unsigned,
                    platform=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    owner_reprove=owner_reprove,
                    business_reprove=active_business,
                )
                if active_set_authority is None:
                    raise AssertionError("portable active-set authority is missing")
                final_active = active_set_authority.reprove()
                if final_active != active_attestation:
                    raise ActivationPreparationError(
                        "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                        retryable=False,
                    )
                self._sealed_registry.consume(token)
                if retire_unpublished_stage() is not None:
                    raise TypeError(
                        "retire_unpublished_stage must return None"
                    )
                active_set_authority.reprove()
                self._ensure_portable_activation_lineage_marker(
                    platform=platform,
                    root=root,
                )
                active_set_authority.reprove()
                active_set_authority.close()
                active_set_authority = None
                self._view = view
                self._preparation = None
                self._state = "READY"
                self._condition.notify_all()
                return 0
            except (
                ActivationPreparationError,
                PlatformFileError,
                OSError,
                sqlite3.Error,
                SQLiteStoreSchemaError,
                stage_seal_error,
            ) as error:
                try:
                    self._sealed_registry.release_portable_live_authority_for_recovery(
                        token
                    )
                except (stage_seal_error, OSError) as release_error:
                    self._state = "ACTIVATING"
                    self._condition.notify_all()
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from release_error
                self._state = "ACTIVATING"
                self._condition.notify_all()
                if armed:
                    if isinstance(error, ActivationPreparationError) and (
                        error.code == "ACTIVATION.RECOVERY_REQUIRED"
                        and error.retryable
                    ):
                        raise
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from error
                raise
            except BaseException:
                try:
                    self._sealed_registry.release_portable_live_authority_for_recovery(
                        token
                    )
                except BaseException:
                    pass
                self._state = "ACTIVATING"
                self._condition.notify_all()
                raise
            finally:
                active_error = sys.exception()
                first_cleanup_error: BaseException | None = None
                for authority in (
                    db_candidate,
                    manifest_candidate,
                    database_pending,
                    manifest_pending,
                    database_guard,
                    active_set_authority,
                ):
                    if authority is None:
                        continue
                    try:
                        authority.close()
                    except BaseException as cleanup_error:
                        if first_cleanup_error is None:
                            first_cleanup_error = cleanup_error
                if active_error is None and first_cleanup_error is not None:
                    if isinstance(
                        first_cleanup_error,
                        (PlatformFileError, OSError),
                    ):
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        ) from first_cleanup_error
                    raise first_cleanup_error

    def publish_portable_replacement_activation(
        self,
        preparation: _PortableReplacementPreparation,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        caller_borrow: _CallerHeldPortableJournalBorrow,
        retire_replacement_stage: Callable[[], None],
    ) -> int:
        """Publish one Windows replacement and adopt only durable READY."""

        if type(preparation) is not _PortableReplacementPreparation:
            raise TypeError("portable replacement preparation is invalid")
        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if persistent_private is not platform:
            raise ActivationPreparationError(
                "ACTIVATION.PUBLICATION_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")
        if not callable(retire_replacement_stage):
            raise TypeError("retire_replacement_stage must be callable")

        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._portable_replacement_preparation is not preparation
                or self._preparation is not None
                or self._view is not preparation._prior_view
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.PUBLICATION_STATE_INVALID",
                    retryable=True,
                )

            identity = self._resource_identity
            token = preparation._token
            physical = preparation._physical_snapshot
            sealed = physical.sealed_content_attestation
            evidence = physical.evidence
            receipt = evidence.source_binding.receipt
            private_name = _portable_activation_private_directory_name(identity)
            backup_proofs = cast(
                tuple[
                    _PortableReplacementBackupProof,
                    _PortableReplacementBackupProof,
                ],
                tuple(
                    _PortableReplacementBackupProof(
                        asset_kind=plan.asset_kind,
                        backup_name=plan.backup_name,
                        content=plan.content,
                    )
                    for plan in preparation.backup_plans
                ),
            )
            prepared_unsigned = _PortableReplacementUnsigned(
                replacement_version=_PORTABLE_REPLACEMENT_VERSION,
                operation=_PORTABLE_REPLACEMENT_OPERATION,
                phase="PREPARED",
                predecessor_digest=preparation.prior_authority_digest,
                journal_id=f"replacement-journal.{preparation.preparation_id}",
                preparation_id=preparation.preparation_id,
                registry_namespace=physical.registry_namespace,
                token_id=token.token_id,
                token_version=token.token_version,
                activation_nonce=token.activation_nonce,
                artifact_id=physical.artifact_id,
                artifact_seal_digest=physical.artifact_seal_digest,
                sealed_stage_digest=physical.sealed_stage_digest,
                resource_id=identity.resource_id,
                target_identity=identity.target_identity,
                prior_authority_digest=preparation.prior_authority_digest,
                prior_canonical_store_id=preparation.prior_store_id,
                prior_generation=preparation.expected_prior_generation,
                prior_active_content_attestation=(
                    preparation._prior_authority.prior_attestation
                ),
                backup_proofs=backup_proofs,
                candidate_canonical_store_id=preparation.candidate_store_id,
                next_generation=preparation.next_generation,
                gate_b_grant_digest=preparation.gate_b_grant_digest,
                evidence_digest=sealed.evidence_digest,
                source_jsonl_digest=sealed.source.sha256,
                new_receipt_id=receipt.snapshot_id,
                new_manifest_digest=evidence.manifest_temp_digest,
                candidate_stage_db_name=physical.mutable_stage.staged_db_path.name,
                candidate_manifest_temp_name=(
                    physical.mutable_stage.manifest_temp_path.name
                ),
                canonical_database_name=identity.canonical_sidecar_path.name,
                canonical_manifest_name=identity.snapshot_manifest_path.name,
                private_directory_name=private_name,
                device_key_name="device.key",
                lock_payload_digest=preparation.lock_payload_digest,
                sealed_content_attestation=sealed,
                active_content_attestation=None,
            )

            token_consumed = False
            backup_set: _PortableReplacementBackupSet | None = None
            database_candidate: CandidateFile | None = None
            manifest_candidate: CandidateFile | None = None
            database_pending: PendingPublication | None = None
            manifest_pending: PendingPublication | None = None
            database_guard: BoundExistingFileMutationGuard | None = None
            active_set: _PortableActiveSetAuthority | None = None

            def live_physical() -> Any:
                fresh = self._sealed_registry.resolve_physical_readiness(
                    preparation._sealed_stage
                )
                if type(fresh) is not type(physical) or replace(
                    fresh,
                    live_reproof=physical.live_reproof,
                ) != physical:
                    try:
                        fresh.live_reproof._release()
                    except BaseException:
                        pass
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                return fresh.live_reproof

            def owner_reprove() -> None:
                caller_borrow.reprove(platform, identity)
                if token_consumed:
                    if active_set is None:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        )
                    active_set.reprove()
                    return
                contract_module._validate_activation_token_for_stage(
                    token,
                    preparation._sealed_stage,
                )
                borrow = live_physical()
                try:
                    borrow.reprove()
                finally:
                    borrow._release()

            def owner_commit(expected: _PortableReplacementUnsigned) -> Callable[[
                _PortableReplacementRecord
            ], None]:
                def commit(record: _PortableReplacementRecord) -> None:
                    if record.unsigned != expected:
                        raise ActivationPreparationError(
                            "ACTIVATION.REPLACEMENT_CHAIN_INVALID",
                            retryable=False,
                        )

                return commit

            def activation_digest() -> str:
                return hashlib.sha256(
                    json.dumps(
                        {
                            "activation_nonce": token.activation_nonce,
                            "artifact_id": physical.artifact_id,
                            "evidence_digest": sealed.evidence_digest,
                            "generation": preparation.next_generation,
                            "journal_id": prepared_unsigned.journal_id,
                            "manifest_digest": evidence.manifest_temp_digest,
                            "sealed_stage_digest": physical.sealed_stage_digest,
                            "token_id": token.token_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()

            try:
                owner_reprove()
                backup_set = self._begin_portable_replacement_backups(
                    preparation,
                    private_directory_name=private_name,
                    platform=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                )

                def prepared_business(_record: _PortableReplacementRecord) -> None:
                    if backup_set is None:
                        raise AssertionError("replacement backup set is missing")
                    preparation._prior_authority.reprove()
                    backup_set.reprove()

                prepared_record = _PortableReplacementRecordOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    unsigned=prepared_unsigned,
                    predecessor=self._portable_replacement_current_record,
                    replace_current=False,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit(prepared_unsigned),
                    business_reprove=prepared_business,
                )
                backup_set.complete_after_prepared()
                backup_set = None
                preparation._prior_authority.reprove()
                preparation._prior_authority.close()

                root, lease = caller_borrow._publication_authorities(
                    platform,
                    persistent_private,
                    identity,
                )
                db_borrow = live_physical()
                try:
                    database_candidate, database_facts = (
                        db_borrow.copy_asset_to_new_candidate(
                            platform=platform,
                            parent=root,
                            asset="database",
                            candidate_name=(
                                identity.canonical_sidecar_path.name
                                + f".{preparation.preparation_id}.candidate"
                            ),
                        )
                    )
                finally:
                    db_borrow._release()
                if self._portable_content_proof(database_facts) != sealed.database:
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                database_candidate.flush_content()
                database_pending = root.begin_publish(
                    database_candidate,
                    identity.canonical_sidecar_path.name,
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=lease,
                )
                database_candidate = None
                db_retained = database_pending.retained_destination()
                if self._portable_content_proof(
                    db_retained.content_facts()
                ) != sealed.database:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                db_unsigned = replace(
                    prepared_unsigned,
                    phase="DB_REPLACED",
                    predecessor_digest=prepared_record.record_digest,
                )

                def db_business(_record: _PortableReplacementRecord) -> None:
                    if self._portable_content_proof(
                        db_retained.content_facts()
                    ) != sealed.database:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        )
                    snapshot = inspect_stage_schema(
                        _canonical_activation_ref(
                            identity,
                            journal_id=prepared_unsigned.journal_id,
                        ),
                        canonical_store_id=preparation.candidate_store_id,
                        _allow_sealed=True,
                    )
                    if snapshot.activation_status != "SEALED":
                        raise ActivationPreparationError(
                            "ACTIVATION.DB_REOPEN_INVALID",
                            retryable=False,
                        )

                db_record = _PortableReplacementRecordOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    unsigned=db_unsigned,
                    predecessor=prepared_record,
                    replace_current=False,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit(db_unsigned),
                    business_reprove=db_business,
                )
                if (
                    database_pending.terminal_reproof()
                    != database_pending.preliminary_facts()
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                database_pending.close()
                database_pending = None

                digest = activation_digest()
                active_database, logical_closure, database_guard = (
                    self._apply_portable_receipt_activation(
                        binding=evidence.source_binding,
                        prepared=db_record,
                        platform=platform,
                        root=root,
                        activation_digest=digest,
                        allow_sealed_transition=True,
                    )
                )
                manifest_borrow = live_physical()
                try:
                    manifest_candidate, manifest_facts = (
                        manifest_borrow.copy_asset_to_new_candidate(
                            platform=platform,
                            parent=root,
                            asset="manifest",
                            candidate_name=(
                                identity.snapshot_manifest_path.name
                                + f".{preparation.preparation_id}.candidate"
                            ),
                        )
                    )
                finally:
                    manifest_borrow._release()
                if self._portable_content_proof(manifest_facts) != sealed.manifest:
                    raise ActivationPreparationError(
                        "ACTIVATION.PUBLICATION_ASSET_MUTATED",
                        retryable=False,
                    )
                manifest_candidate.flush_content()
                manifest_pending = root.begin_publish(
                    manifest_candidate,
                    identity.snapshot_manifest_path.name,
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=lease,
                )
                manifest_candidate = None
                active_manifest = self._portable_content_proof(
                    manifest_pending.retained_destination().content_facts()
                )
                if active_manifest != sealed.manifest or database_guard is None:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                active_ref, active_snapshot, active_attestation, active_set = (
                    self._reprove_portable_active_set(
                        binding=evidence.source_binding,
                        prepared=db_record,
                        platform=platform,
                        root=root,
                        database=active_database,
                        manifest=active_manifest,
                        activation_digest=digest,
                        expected_logical_closure_digest=logical_closure,
                        database_guard=database_guard,
                        expected_candidate_projection_digest=(
                            physical.candidate_projection_digest
                        ),
                    )
                )
                database_guard = None
                manifest_unsigned = replace(
                    db_unsigned,
                    phase="MANIFEST_PUBLISHED",
                    predecessor_digest=db_record.record_digest,
                    active_content_attestation=active_attestation,
                )

                def active_business(record: _PortableReplacementRecord) -> None:
                    if (
                        record.unsigned.active_content_attestation
                        != active_attestation
                        or active_set is None
                        or active_set.reprove() != active_attestation
                    ):
                        raise ActivationPreparationError(
                            "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                            retryable=False,
                        )

                manifest_record = _PortableReplacementRecordOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    unsigned=manifest_unsigned,
                    predecessor=db_record,
                    replace_current=False,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit(manifest_unsigned),
                    business_reprove=active_business,
                )
                if (
                    manifest_pending.terminal_reproof()
                    != manifest_pending.preliminary_facts()
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                manifest_pending.close()
                manifest_pending = None

                generation_unsigned = replace(
                    manifest_unsigned,
                    phase="GENERATION_PUBLISHED",
                    predecessor_digest=manifest_record.record_digest,
                )
                generation_record = _PortableReplacementRecordOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    unsigned=generation_unsigned,
                    predecessor=manifest_record,
                    replace_current=False,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit(generation_unsigned),
                    business_reprove=active_business,
                )
                if active_set is None or active_set.reprove() != active_attestation:
                    raise ActivationPreparationError(
                        "ACTIVATION.ACTIVE_ATTESTATION_INVALID",
                        retryable=False,
                    )
                self._sealed_registry.consume(token)
                token_consumed = True
                ready_unsigned = replace(
                    generation_unsigned,
                    phase="READY",
                    predecessor_digest=generation_record.record_digest,
                )
                ready_record = _PortableReplacementRecordOwner.publish(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                    unsigned=ready_unsigned,
                    predecessor=generation_record,
                    replace_current=True,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit(ready_unsigned),
                    business_reprove=active_business,
                )
                if retire_replacement_stage() is not None:
                    raise TypeError("retire_replacement_stage must return None")
                ready_cleanup = _parse_portable_replacement_namespace(
                    {
                        _portable_replacement_phase_name(record.unsigned.phase): record
                        for record in (
                            prepared_record,
                            db_record,
                            manifest_record,
                            generation_record,
                            ready_record,
                        )
                    },
                    {
                        backup.backup_name: backup.content
                        for backup in backup_proofs
                    },
                    base_authority_digest=None,
                )
                self._cleanup_portable_replacement_ready_namespace(
                    ready_cleanup,
                    platform=platform,
                    persistent_private=persistent_private,
                    caller_borrow=caller_borrow,
                )
                active_set.reprove()
                active_set.close()
                active_set = None
                self._view = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=preparation.candidate_store_id,
                    generation=preparation.next_generation,
                    fts5_available=active_snapshot.fts5_available,
                    active_content_attestation=active_attestation,
                )
                self._canonical_store_id = preparation.candidate_store_id
                self._portable_replacement_current_record = ready_record
                self._portable_replacement_preparation = None
                self._state = "READY"
                self._condition.notify_all()
                return preparation.next_generation
            except (
                ActivationPreparationError,
                PlatformFileError,
                OSError,
                sqlite3.Error,
                SQLiteStoreSchemaError,
            ) as error:
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                if not token_consumed:
                    try:
                        self._sealed_registry.release_portable_live_authority_for_recovery(
                            token
                        )
                    except BaseException as release_error:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        ) from release_error
                try:
                    preparation._prior_authority.close()
                except BaseException as close_prior_error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from close_prior_error
                if isinstance(error, ActivationPreparationError) and (
                    error.code == "ACTIVATION.RECOVERY_REQUIRED"
                    and error.retryable
                ):
                    raise
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error
            except BaseException:
                error = sys.exception()
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                if not token_consumed:
                    try:
                        self._sealed_registry.release_portable_live_authority_for_recovery(
                            token
                        )
                    except BaseException:
                        if isinstance(
                            error,
                            (TypeError, AssertionError, AttributeError),
                        ):
                            raise error
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        ) from sys.exception()
                try:
                    preparation._prior_authority.close()
                except BaseException:
                    if isinstance(
                        error,
                        (TypeError, AssertionError, AttributeError),
                    ):
                        raise error
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ) from sys.exception()
                raise
            finally:
                active_error = sys.exception()
                close_error: BaseException | None = None
                for authority in (
                    backup_set,
                    database_candidate,
                    manifest_candidate,
                    database_pending,
                    manifest_pending,
                    database_guard,
                    active_set,
                ):
                    if authority is None:
                        continue
                    try:
                        authority.close()
                    except BaseException as error:
                        if close_error is None:
                            close_error = error
                if active_error is None and close_error is not None:
                    raise close_error

    def recover_portable_prepared_cancellation(
        self,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        existing_retirement: ExistingFileRetirement,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> ActivationRecoveryReport:
        """Durably cancel or freshly replay one Windows v3 PREPARED record."""

        if not isinstance(platform, PlatformFileBackend):
            raise TypeError("platform must satisfy PlatformFileBackend")
        if not (
            persistent_private is platform
            and descendant_inspection is platform
            and existing_retirement is platform
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE",
                retryable=False,
            )
        if type(caller_borrow) is not _CallerHeldPortableJournalBorrow:
            raise TypeError("caller_borrow must be the exact portable borrow")

        with self._condition:
            if (
                self._state not in {"READY", "ACTIVATING"}
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_STATE_INVALID",
                    retryable=True,
                )
            live_preparation = self._preparation
            if self._state == "READY":
                if (
                    live_preparation is not None
                    or self._view is not None
                    or self.current_generation is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_STATE_INVALID",
                        retryable=True,
                    )
                self._state = "ACTIVATING"
            elif live_preparation is None:
                # A prior portable recovery failure deliberately remains
                # ACTIVATING and is retried from durable disk authority.
                pass

            stage_sealer = importlib.import_module("tm_stage_sealer")
            portable_snapshot_type = getattr(
                stage_sealer,
                "_PortablePhysicalReadinessSnapshot",
            )
            stage_seal_error = getattr(stage_sealer, "StageSealError")
            identity = self._resource_identity
            committed_record: _PortableActivationJournalRecord | None = None

            def owner_reprove(record: _PortableActivationJournalRecord) -> None:
                if (
                    type(record) is not _PortableActivationJournalRecord
                    or self._state != "ACTIVATING"
                    or self._cleanup_reservation is not None
                    or self._cleanup_in_progress
                    or record.unsigned.resource_id != identity.resource_id
                    or record.unsigned.target_identity != identity.target_identity
                    or record.unsigned.canonical_store_id
                    != self._canonical_store_id
                    or record.unsigned.expected_prior_generation is not None
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                        retryable=False,
                    )
                caller_borrow.reprove(platform, identity)
                if committed_record is not None:
                    if (
                        record != committed_record
                        or self._preparation is not None
                        or self._view is not None
                    ):
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_STATE_INVALID",
                            retryable=True,
                        )
                    return
                if live_preparation is None:
                    if self._preparation is not None or self._view is not None:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_STATE_INVALID",
                            retryable=True,
                        )
                    return
                if self._preparation is not live_preparation:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_STATE_INVALID",
                        retryable=True,
                    )
                stage = live_preparation._sealed_stage
                token = live_preparation._token
                physical = live_preparation._physical_snapshot
                if type(physical) is not portable_snapshot_type:
                    raise ActivationPreparationError(
                        "ACTIVATION.ATTESTATION_UNAVAILABLE",
                        retryable=False,
                    )
                try:
                    contract_module._validate_activation_token_for_stage(
                        token,
                        stage,
                    )
                    fresh = self._sealed_registry.resolve_physical_readiness(stage)
                    if type(fresh) is not portable_snapshot_type:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_ASSET_MUTATED",
                            retryable=False,
                        )
                    try:
                        if replace(
                            fresh,
                            live_reproof=physical.live_reproof,
                        ) != physical:
                            raise ActivationPreparationError(
                                "ACTIVATION.RECOVERY_ASSET_MUTATED",
                                retryable=False,
                            )
                        fresh.live_reproof.reprove()
                    finally:
                        fresh.live_reproof._release()
                except stage_seal_error as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_ASSET_MUTATED",
                        retryable=False,
                    ) from error
                except ValueError as error:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                        retryable=False,
                    ) from error
                evidence = physical.evidence
                receipt = evidence.source_binding.receipt
                unsigned = record.unsigned
                if (
                    unsigned.preparation_id != live_preparation.preparation_id
                    or unsigned.registry_namespace != physical.registry_namespace
                    or unsigned.token_id != token.token_id
                    or unsigned.token_version != token.token_version
                    or unsigned.activation_nonce != token.activation_nonce
                    or unsigned.artifact_id != physical.artifact_id
                    or unsigned.artifact_seal_digest
                    != physical.artifact_seal_digest
                    or unsigned.sealed_stage_digest
                    != physical.sealed_stage_digest
                    or unsigned.gate_b_grant_digest
                    != live_preparation.gate_b_grant_digest
                    or unsigned.evidence_digest
                    != contract_module.stage_validation_evidence_digest(evidence)
                    or unsigned.snapshot_receipt_digest
                    != physical.snapshot_receipt_digest
                    or unsigned.stage_db_digest != evidence.stage_file_digest
                    or unsigned.manifest_temp_digest
                    != evidence.manifest_temp_digest
                    or unsigned.source_jsonl_digest != receipt.jsonl_digest
                    or unsigned.new_receipt_id != receipt.snapshot_id
                    or unsigned.candidate_stage_db_name
                    != physical.mutable_stage.staged_db_path.name
                    or unsigned.candidate_manifest_temp_name
                    != physical.mutable_stage.manifest_temp_path.name
                    or unsigned.sealed_content_attestation
                    != physical.sealed_content_attestation
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_JOURNAL_INVALID",
                        retryable=False,
                    )

            def owner_commit(record: _PortableActivationJournalRecord) -> None:
                nonlocal committed_record
                if record.unsigned.closure != "CANCELLED":
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                        retryable=False,
                    )
                owner_reprove(record)
                if live_preparation is not None:
                    try:
                        self._sealed_registry.cancel(live_preparation._token)
                    except stage_seal_error as error:
                        raise ActivationPreparationError(
                            "ACTIVATION.RECOVERY_REQUIRED",
                            retryable=True,
                        ) from error
                    self._preparation = None
                committed_record = record

            try:
                handle = _WindowsPortableCancelledJournalOwner.cancel(
                    identity=identity,
                    backend=platform,
                    persistent_private=persistent_private,
                    descendant_inspection=descendant_inspection,
                    existing_retirement=existing_retirement,
                    caller_borrow=caller_borrow,
                    owner_reprove=owner_reprove,
                    owner_commit=owner_commit,
                )
                if type(handle) is not _PortableCancelledJournalHandle:
                    raise TypeError("portable recovery returned an invalid handle")
            except BaseException:
                self._condition.notify_all()
                raise
            self._preparation = None
            self._cleanup_reservation = None
            self._view = None
            self._state = "READY"
            self._condition.notify_all()
            return ActivationRecoveryReport(
                phase="PREPARED",
                action="CANCELLED",
                generation=None,
            )

    def recover_portable_activation(
        self,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        existing_retirement: ExistingFileRetirement,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> ActivationRecoveryReport | None:
        """Freshly continue one Windows v3/publication first activation."""

        with self._condition:
            try:
                return recover_portable_activation(
                    _CoordinatorStorePort(self),
                    platform=platform,
                    persistent_private=persistent_private,
                    descendant_inspection=descendant_inspection,
                    existing_retirement=existing_retirement,
                    caller_borrow=caller_borrow,
                )
            except ActivationPreparationError:
                error = sys.exception()
                if (
                    isinstance(error, ActivationPreparationError)
                    and error.code == "ACTIVATION.RECOVERY_REQUIRED"
                ):
                    self._view = None
                    self._state = "ACTIVATING"
                    self._condition.notify_all()
                raise
            except (PlatformFileError, OSError, sqlite3.Error) as error:
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error

    def recover_portable_replacement_activation(
        self,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        existing_retirement: ExistingFileRetirement,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> ActivationRecoveryReport | None:
        """Freshly classify and adopt one strict Windows replacement chain."""

        with self._condition:
            try:
                report = recover_portable_replacement_activation(
                    _CoordinatorStorePort(self),
                    platform=platform,
                    persistent_private=persistent_private,
                    descendant_inspection=descendant_inspection,
                    existing_retirement=existing_retirement,
                    caller_borrow=caller_borrow,
                )
            except ActivationPreparationError:
                error = sys.exception()
                if (
                    isinstance(error, ActivationPreparationError)
                    and error.code == "ACTIVATION.RECOVERY_REQUIRED"
                ):
                    self._view = None
                    self._state = "ACTIVATING"
                    self._condition.notify_all()
                raise
            except (PlatformFileError, OSError, sqlite3.Error) as error:
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error
            if report is not None:
                self._portable_replacement_preparation = None
            return report

    def rehydrate_completed_portable_activation(
        self,
        *,
        platform: PlatformFileBackend,
        persistent_private: PersistentPrivateProof,
        descendant_inspection: LockedDescendantNamespaceInspection,
        caller_borrow: _CallerHeldPortableJournalBorrow,
    ) -> ActivationRecoveryReport | None:
        """Read-only hydrate exactly one complete Windows portable chain."""

        with self._condition:
            try:
                return rehydrate_completed_portable_replacement_activation(
                    _CoordinatorStorePort(self),
                    platform=platform,
                    persistent_private=persistent_private,
                    descendant_inspection=descendant_inspection,
                    caller_borrow=caller_borrow,
                )
            except ActivationPreparationError:
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                raise
            except (PlatformFileError, OSError, sqlite3.Error) as error:
                self._view = None
                self._state = "ACTIVATING"
                self._condition.notify_all()
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_REQUIRED",
                    retryable=True,
                ) from error

    def _advance_activation_journal(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
    ) -> _ActivationJournalHandle:
        """Monotonically advance one durable journal phase (Tasks 5.7-5.9).

        Module-private primitive for the later replacement/publication
        tasks.  It re-reads and strictly validates the durable journal,
        requires the exact live preparation plus the exact handle bound to
        the current file, checks that ``next_phase`` is exactly the next
        phase in the fixed sequence, revalidates token and full closure
        against registry-owned facts, then durably publishes the next phase.
        It never touches DB, manifest, backup, or generation assets.
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
        if type(next_phase) is not _ActivationJournalPhase:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PHASE_INVALID",
                retryable=False,
            )
        with self._condition:
            disk_record = _load_activation_transition_record(
                _CoordinatorStorePort(self),
                preparation,
                handle,
                next_phase,
            )
            _revalidate_activation_journal_closure(
                _CoordinatorStorePort(self),
                preparation,
                disk_record,
            )
            next_record = replace(disk_record, phase=next_phase)
            return self._write_activation_journal_locked(
                next_record,
                handle.journal_path,
                expected_final_identity=handle.file_identity,
            )

    def publish_activation(
        self,
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

        with self._condition:
            return publish_activation(
                _CoordinatorStorePort(self),
                preparation,
                handle,
            )

    def recover_durable_activation(self) -> ActivationRecoveryReport | None:
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

        with self._condition:
            port = _CoordinatorStorePort(self)
            report = recover_durable_activation(port)
            view = self._view
            if report is not None and view is not None:
                # A recovered v2 store will not mint another upgrade
                # ticket, so the cold terminal completion path resolves
                # any pending schema-upgrade artifacts deterministically:
                # completed activations retain one stable reported backup,
                # cancelled/rolled-back outcomes leave no hidden copies.
                # A completed recovery rehydrates the view at the
                # canonical sidecar, so the pending family is resolved
                # against the original store path of the retained
                # terminal closure.
                _finish_cold_schema_upgrade_pending(
                    _recovered_schema_upgrade_pending_root(port, view),
                    completed=(report.action == "COMPLETED"),
                )
            self._clear_initial_activation_fail_stop_after_recovery(report)
            return report

    def reattest_completed_authority(self) -> ActivationRecoveryReport:
        """Repair only a fully proven cross-restart ``st_dev`` drift.

        The ordinary open path remains strict.  Callers must explicitly
        request this maintenance transition; Core then re-proves and
        atomically re-attests the exact same completed generation.
        """

        with self._condition:
            report = reattest_completed_authority(
                _CoordinatorStorePort(self)
            )
            self._clear_initial_activation_fail_stop_after_recovery(report)
            return report

    def completed_authority_requires_reattestation(self) -> bool:
        """Classify the exact repairable ``st_dev`` drift without granting it."""

        with self._condition:
            return completed_authority_requires_reattestation(
                _CoordinatorStorePort(self)
            )

    def rehydrate_runtime_authority(
        self,
    ) -> ActivationRecoveryReport | None:
        """Rehydrate the durable activation authority for a fresh facade.

        Task 6.1/6.2 cold-open seam used by the legacy facade: a completed
        activation (``GENERATION_PUBLISHED`` journal or terminal) is
        re-proven and hydrated as the one in-memory canonical generation
        without re-requiring the activation-time snapshot parity, so a
        legitimate canonical save/import (``VERIFIED_HISTORY``) and a
        latched/observed ``SOURCE_DIVERGED`` both reopen on the same
        canonical lineage.  Incomplete activations and unclosed or tampered
        authorities keep the strict Task 5.8/5.9 recovery or fail-stop
        semantics of :meth:`recover_durable_activation`; JSONL is never an
        implicit fallback.
        """

        with self._condition:
            port = _CoordinatorStorePort(self)
            report = _rehydrate_runtime_authority(port)
            view = self._view
            if report is not None and view is not None:
                # Mirror the cold completion wrapper: a completed runtime
                # open resolves any pending schema-upgrade artifacts
                # deterministically against the retained closure.
                _finish_cold_schema_upgrade_pending(
                    _recovered_schema_upgrade_pending_root(port, view),
                    completed=(report.action == "COMPLETED"),
                )
            self._clear_initial_activation_fail_stop_after_recovery(report)
            return report

    def rehydrate_completed_initial_authority(
        self,
    ) -> ActivationRecoveryReport | None:
        """Hydrate only an already-complete generation zero without recovery.

        This Core-owned fail-safe seam is intentionally narrower than
        :meth:`rehydrate_runtime_authority`: it never advances, cancels,
        rolls back, or cleans durable activation facts.  It is therefore the
        only authority probe permitted when the physical initial-activation
        reservation itself could not be acquired.
        """

        with self._condition:
            report = _rehydrate_completed_initial_authority_only(
                _CoordinatorStorePort(self)
            )
            self._clear_initial_activation_fail_stop_after_recovery(report)
            return report

    def rollback_durable_activation(
        self,
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

        with self._condition:
            port = _CoordinatorStorePort(self)
            report = rollback_durable_activation(port)
            view = self._view
            if report is not None and view is not None:
                # A cold rollback restores the prior authority: any
                # pending schema-upgrade crash orphans are strictly swept
                # (proven-unchanged outcomes leave no pending or stable
                # hidden full-copy artifacts).
                _finish_cold_schema_upgrade_pending(
                    _recovered_schema_upgrade_pending_root(port, view),
                    completed=False,
                )
            self._clear_initial_activation_fail_stop_after_recovery(report)
            return report

    def adopt_recovered_authority(
        self,
        recovered: ResourceStoreCoordinator,
    ) -> str:
        """Adopt a fully recovered completed activation authority.

        Narrow Task 5.10 transition for one fail-stopped coordinator: after
        a fresh coordinator re-proved and completed a durable
        ``GENERATION_PUBLISHED`` journal (crash-window recovery), this
        method adopts only the exact recovered authority - same immutable
        resource identity, recovered ``READY`` state, non-null recovered
        view whose store id/generation/canonical paths are coherent - and
        only while this coordinator is fail-stopped (``ACTIVATING``) with
        zero live leases and no cleanup reservation.  The proven view and
        store id are adopted under the coordinator-owned condition lock,
        the stale live preparation/registry state is retired, and the
        coordinator returns to ``READY`` and notifies waiters.  It is not a
        generic setter: any other type, identity, state, or unproven
        authority is rejected without mutation.
        """

        if type(recovered) is not ResourceStoreCoordinator:
            raise TypeError(
                "recovered authority must be exact ResourceStoreCoordinator"
            )
        if (
            recovered._resource_identity != self._resource_identity
            or recovered._resource_id != self._resource_id
            or recovered._target_identity != self._target_identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_IDENTITY_MISMATCH",
                retryable=False,
            )
        if recovered.state != "READY":
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_STATE_INVALID",
                retryable=False,
            )
        recovered_view = recovered._view
        if type(recovered_view) is not _SQLiteGenerationView:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_VIEW_INVALID",
                retryable=False,
            )
        if (
            recovered_view.canonical_store_id
            != recovered._canonical_store_id
            or recovered_view.generation
            != recovered.current_generation
            or recovered._active_lease_count != 0
            or recovered._preparation is not None
            or recovered._cleanup_reservation is not None
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_AUTHORITY_INVALID",
                retryable=False,
            )
        if (
            recovered_view.stage.resource_identity
            != self._resource_identity
            or recovered_view.stage.staged_db_path
            != self._resource_identity.canonical_sidecar_path
            or recovered_view.stage.manifest_temp_path
            != self._resource_identity.snapshot_manifest_path
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_PATH_MISMATCH",
                retryable=False,
            )
        with self._condition:
            if (
                self._state != "ACTIVATING"
                or self._active_lease_count != 0
                or self._cleanup_reservation is not None
                or self._cleanup_in_progress
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_ADOPTION_INVALID",
                    retryable=True,
                )
            stale_preparation = self._preparation
            self._view = recovered_view
            self._canonical_store_id = recovered._canonical_store_id
            self._preparation = None
            self._cleanup_reservation = None
            self._initial_activation_authority_unavailable = False
            self._state = "READY"
            self._condition.notify_all()
        if stale_preparation is not None:
            self._retire_stale_preparation_registry(stale_preparation)
        return "READY"

    def _retire_stale_preparation_registry(
        self,
        preparation: _ActivationPreparation,
    ) -> None:
        """Best-effort retire of the superseded preparation's registry token.

        The completed durable journal is the recovery authority; the stale
        in-memory token entry is retired only when it is still issued.  Any
        registry refusal is deliberately ignored: after cold-style recovery
        has re-proven the completed journal, an unreachable process-local
        token must not overturn the adopted durable authority.
        """

        token = preparation._token
        stage_seal_error = _CoordinatorStorePort(self).stage_seal_error
        try:
            self._sealed_registry.consume(token)
        except stage_seal_error:
            return

    def _advance_activation_journal_after_effect_locked(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
        *,
        next_generation: int | None = None,
        activation_digest: str | None = None,
        active_content_attestation: Any | None = None,
    ) -> _ActivationJournalHandle:
        """Advance only after re-proving the phase-specific durable effect."""

        return _advance_activation_journal_after_effect(
            _CoordinatorStorePort(self),
            preparation,
            handle,
            next_phase,
            next_generation=next_generation,
            activation_digest=activation_digest,
            active_content_attestation=active_content_attestation,
        )

    def _write_activation_journal_locked(
        self,
        record: _ActivationJournalRecord,
        journal_path: Path,
        *,
        expected_final_identity: _ActivationFileIdentity | None,
    ) -> _ActivationJournalHandle:
        """Publish one journal record with strict exclusive temp + fsync order.

        The exclusive temporary/replace/fsync protocol lives in
        ``tm_activation_journal``; this seam keeps the coordinator's
        historical name so phase-advance callers and fault-injection tests
        target one place.
        """

        try:
            return _write_activation_journal(
                record,
                journal_path,
                expected_final_identity=expected_final_identity,
            )
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_WRITE_FAILED",
                retryable=True,
            ) from error

    def _write_activation_terminal_locked(
        self,
        record: _ActivationJournalRecord,
    ) -> _ActivationFileIdentity:
        """Publish one terminal record with strict exclusive temp + fsync.

        The terminal protocol lives in ``tm_activation_journal``; this seam
        keeps the coordinator's historical name for recovery callers and
        fault-injection tests.
        """

        return _write_activation_terminal(self._resource_identity, record)

    def _transition_generation(
        self,
        stage: MutableStageRef,
        *,
        canonical_store_id: str,
        expected_prior_generation: int,
        timeout_seconds: float,
    ) -> int:
        """Drain and publish an already-prepared view; activation calls this later."""

        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        if type(expected_prior_generation) is not int:
            raise TypeError(
                "expected_prior_generation must be a built-in integer"
            )
        if expected_prior_generation < 0:
            raise ValueError(
                "expected_prior_generation must be non-negative"
            )
        timeout = _require_timeout(timeout_seconds)
        next_stage = _snapshot_store_stage(stage)
        next_identity = next_stage.resource_identity
        if (
            next_identity.resource_id != self._resource_id
            or next_identity.target_identity != self._target_identity
        ):
            observed_generation = self.current_generation
            raise SQLiteStoreLifecycleError(
                "STORE.IDENTITY_MISMATCH",
                resource_id=self._resource_id,
                generation=(
                    0 if observed_generation is None else observed_generation
                ),
                retryable=False,
            )

        with self._condition:
            current_view = self._view
        if current_view is None:
            raise SQLiteStoreLifecycleError(
                "STORE.CANONICAL_UNAVAILABLE",
                resource_id=self._resource_id,
                generation=0,
                retryable=False,
            )
        if canonical_store_id != current_view.canonical_store_id:
            raise SQLiteStoreLifecycleError(
                "STORE.IDENTITY_MISMATCH",
                resource_id=self._resource_id,
                generation=current_view.generation,
                retryable=False,
            )
        if next_stage.staged_db_path != current_view.stage.staged_db_path:
            _ = inspect_stage_schema(
                next_stage,
                canonical_store_id=canonical_store_id,
            )

        deadline = time.monotonic() + timeout
        with self._condition:
            current_view = self._view
            if current_view is None:
                raise SQLiteStoreLifecycleError(
                    "STORE.CANONICAL_UNAVAILABLE",
                    resource_id=self._resource_id,
                    generation=0,
                    retryable=False,
                )
            if self._state != "READY":
                raise SQLiteStoreLifecycleError(
                    "STORE.RESOURCE_DRAINING",
                    resource_id=self._resource_id,
                    generation=current_view.generation,
                    retryable=True,
                )
            if current_view.generation != expected_prior_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_STALE",
                    resource_id=self._resource_id,
                    generation=current_view.generation,
                    retryable=False,
                )
            self._state = "DRAINING"
            self._condition.notify_all()
            while self._active_lease_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._state = "READY"
                    self._condition.notify_all()
                    raise SQLiteStoreLifecycleError(
                        "STORE.DRAIN_TIMEOUT",
                        resource_id=self._resource_id,
                        generation=current_view.generation,
                        retryable=True,
                    )
                self._condition.wait(remaining)
            self._state = "ACTIVATING"
            self._condition.notify_all()
            try:
                next_snapshot = _validate_next_generation_stage(
                    next_stage,
                    canonical_store_id=canonical_store_id,
                )
            except Exception as error:
                self._state = "READY"
                self._condition.notify_all()
                raise SQLiteStoreLifecycleError(
                    "STORE.NEXT_GENERATION_INVALID",
                    resource_id=self._resource_id,
                    generation=current_view.generation,
                    retryable=False,
                ) from error
            next_generation = current_view.generation + 1
            self._view = _SQLiteGenerationView(
                stage=next_stage,
                canonical_store_id=canonical_store_id,
                generation=next_generation,
                fts5_available=next_snapshot.fts5_available,
            )
            self._state = "READY"
            self._condition.notify_all()
            return next_generation


class ReceiptCompletionProbe(str, Enum):
    """Durable completion probe for one ambiguous post-commit window."""

    COMMITTED = "COMMITTED"
    COMMITTED_UNCLEAN = "COMMITTED_UNCLEAN"
    NOT_COMMITTED = "NOT_COMMITTED"


class SourceBindingMonitor:
    """Observe one configured snapshot without publishing either file."""

    def __init__(
        self,
        coordinator: ResourceStoreCoordinator,
        *,
        store: SQLiteTMStore | None = None,
    ) -> None:
        if type(coordinator) is not ResourceStoreCoordinator:
            raise TypeError("coordinator must be ResourceStoreCoordinator")
        if store is not None and type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore or None")
        self._coordinator = coordinator
        self._store = store

    def _recover_configured_refresh(self) -> None:
        """Close one issued refresh crash window before classification.

        Task 5.14: runs under the already-held reentrant refresh
        observation gate so a legitimate issued publication window can
        never be misclassified as external divergence.  Idempotent:
        replay returns the same stable state and a pre-existing
        divergence latch is never cleared.
        """

        if self._store is not None:
            if sys.platform == "win32":
                self._store.require_bound_refresh_observation_ready()
                return
            outcome = self._store.recover_configured_refresh()
            if outcome.state is snapshot_recovery_module.RefreshRecoveryState.BLOCKED:
                raise SQLiteStoreSchemaError(
                    "STORE.REFRESH_RECOVERY_REQUIRED"
                )

    def observe(self) -> SourceBindingObservation:
        """Derive and latch source divergence in one generation lease.

        The observation passes through the coordinator's refresh
        observation gate: when a configured refresh holds its
        reservation the observation waits (bounded) and then reads the
        fully published pair, so the intentional JSONL-only publication
        window can never be read as tampering and latch a false
        ``SOURCE_DIVERGED``.  The owning refresh's own reentrant
        preflight observation under its reservation is not blocked by
        the gate.
        """

        with self._coordinator._refresh_observation_gate():
            self._recover_configured_refresh()
            with self._coordinator._operation_lease() as lease:
                while True:
                    with _open_leased_connection(lease) as connection:
                        facts = _read_source_binding_facts(connection, lease)
                    diagnostics = list(facts.diagnostic_codes)
                    binding = facts.binding
                    if facts.divergence_latched:
                        diagnostics.append(
                            "SOURCE_BINDING.DIVERGENCE_LATCHED"
                        )
                    elif binding is None:
                        diagnostics.append(
                            "SOURCE_BINDING.LEDGER_MISSING"
                        )
                    else:
                        diagnostics.extend(
                            _configured_pair_diagnostics(
                                binding,
                                identity=lease.stage.resource_identity,
                                canonical_store_id=lease.canonical_store_id,
                                head_revision=facts.head_revision,
                                cumulative_record_counts=(
                                    facts.cumulative_record_counts
                                ),
                            )
                        )

                    diagnostic_codes = tuple(sorted(set(diagnostics)))
                    if diagnostic_codes:
                        if (
                            not facts.divergence_latched
                            and not _latch_source_divergence(
                                lease,
                                expected_fingerprint=(
                                    facts.canonical_fingerprint
                                ),
                            )
                        ):
                            continue
                        state = SourceBindingState.SOURCE_DIVERGED
                    else:
                        assert binding is not None
                        state = (
                            SourceBindingState.VERIFIED_CURRENT
                            if binding.receipt.exported_revision
                            == facts.head_revision
                            else SourceBindingState.VERIFIED_HISTORY
                        )
                    return SourceBindingObservation(
                        resource_id=(
                            lease.stage.resource_identity.resource_id
                        ),
                        canonical_store_id=lease.canonical_store_id,
                        generation=lease.generation,
                        head_revision=facts.head_revision,
                        state=state,
                        binding_digest=(
                            None
                            if binding is None
                            else _snapshot_binding_digest(binding)
                        ),
                        diagnostic_codes=diagnostic_codes,
                    )

    def register_completed_binding(self, binding: SnapshotBinding) -> None:
        """Register a pair already published and validated by its owner.

        This deliberately has no issued receipt, file creation, replace,
        fsync, recovery, rebinding, or divergence-clearing behavior.
        """

        private_binding = _snapshot_completed_binding(binding)
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            _validate_binding_identity(
                private_binding,
                identity=identity,
                canonical_store_id=lease.canonical_store_id,
            )
            pair_diagnostics = _configured_pair_diagnostics(
                private_binding,
                identity=identity,
                canonical_store_id=lease.canonical_store_id,
                head_revision=private_binding.receipt.exported_revision,
                cumulative_record_counts=(
                    (
                        private_binding.receipt.exported_revision,
                        private_binding.receipt.record_count,
                    ),
                ),
            )
            if pair_diagnostics:
                if any(code.endswith("_MISSING") for code in pair_diagnostics):
                    raise FileNotFoundError("completed snapshot pair is missing")
                raise ValueError("completed snapshot pair does not match binding")

            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    revision = _canonical_revision_from_connection(
                        connection,
                        lease,
                    )
                    receipt = private_binding.receipt
                    if (
                        receipt.exported_revision != revision.head_revision
                        or receipt.record_count != revision.record_count
                    ):
                        raise ValueError(
                            "completed binding must describe current revision"
                        )
                    existing = connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone()
                    if existing is None or type(existing[0]) is not int:
                        raise SQLiteStoreSchemaError(
                            "STORE.SNAPSHOT_LEDGER_CORRUPT"
                        )
                    if existing[0] != 0:
                        raise ValueError(
                            "completed snapshot binding is already registered"
                        )
                    if _meta_bool(_read_meta(connection), "divergence_latched"):
                        raise ValueError(
                            "diverged source binding cannot be registered"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_receipt("
                        "snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)",
                        (
                            receipt.snapshot_id,
                            receipt.resource_id,
                            receipt.canonical_store_id,
                            receipt.exported_revision,
                            receipt.jsonl_digest,
                            receipt.record_count,
                            receipt.format_version,
                            Path.__str__(private_binding.configured_jsonl_path),
                            Path.__str__(private_binding.manifest_path),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO tm_snapshot_binding("
                        "binding_id, configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version) "
                        "VALUES (1, ?, ?, ?, ?, ?)",
                        (
                            Path.__str__(private_binding.configured_jsonl_path),
                            Path.__str__(private_binding.manifest_path),
                            private_binding.snapshot_kind.value,
                            receipt.snapshot_id,
                            private_binding.binding_version,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise


def _validate_next_generation_stage(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
) -> SQLiteSchemaSnapshot:
    """Reopen and fully validate a drained transition target in rw mode."""

    snapshot = inspect_stage_schema(
        stage,
        canonical_store_id=canonical_store_id,
        _allow_diverged_runtime=True,
    )
    lease = _SQLiteGenerationView(
        stage=stage,
        canonical_store_id=canonical_store_id,
        generation=snapshot.generation,
        fts5_available=snapshot.fts5_available,
    )
    with _open_configured_connection(
        stage.staged_db_path,
        require_existing=True,
    ) as connection:
        identity = stage.resource_identity
        _validate_store_identity(
            connection,
            resource_id=identity.resource_id,
            canonical_store_id=canonical_store_id,
            target_identity=identity.target_identity,
        )
        integrity_rows = connection.execute(
            "PRAGMA integrity_check"
        ).fetchall()
        if integrity_rows != [("ok",)]:
            raise SQLiteStoreSchemaError("STORE.INTEGRITY_CHECK_FAILED")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SQLiteStoreSchemaError("STORE.FOREIGN_KEY_CHECK_FAILED")
        revision = _canonical_revision_from_connection(connection, lease)
        if (
            revision.head_revision != snapshot.head_revision
            or revision.record_count != _table_count(connection, "tm_record")
        ):
            raise SQLiteStoreSchemaError("STORE.NEXT_GENERATION_UNHEALTHY")
    return snapshot


@dataclass(frozen=True)
class _SourceBindingFacts:
    head_revision: int
    record_count: int
    cumulative_record_counts: tuple[tuple[int, int], ...]
    divergence_latched: bool
    binding: SnapshotBinding | None
    diagnostic_codes: tuple[str, ...]
    canonical_fingerprint: str


def _read_source_binding_facts(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> _SourceBindingFacts:
    if connection.in_transaction:
        raise SQLiteStoreSchemaError("STORE.READ_SNAPSHOT_NESTED")
    connection.execute("BEGIN")
    try:
        facts = _read_source_binding_facts_in_transaction(connection, lease)
        connection.commit()
        return facts
    except Exception:
        connection.rollback()
        raise


def _read_source_binding_facts_in_transaction(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> _SourceBindingFacts:
    if not connection.in_transaction:
        raise SQLiteStoreSchemaError("STORE.READ_SNAPSHOT_MISSING")
    identity = lease.stage.resource_identity
    meta = _read_meta(connection)
    if (
        meta["resource_id"] != identity.resource_id
        or meta["canonical_store_id"] != lease.canonical_store_id
        or meta["target_identity"] != identity.target_identity
    ):
        raise SQLiteStoreSchemaError("STORE.IDENTITY_MISMATCH")
    schema_version = _meta_int(meta, "schema_version")
    head_revision = _meta_int(meta, "head_revision")
    record_count = _table_count(connection, "tm_record")
    diagnostics: list[str] = []
    cumulative_record_counts: tuple[tuple[int, int], ...] = ()
    if schema_version == TM_LEGACY_SCHEMA_VERSION:
        ancestry_rows = _legacy_ancestry_fingerprint_rows(connection)
        try:
            cumulative_record_counts = _legacy_revision_ancestry(
                connection,
                head_revision=head_revision,
                record_count=record_count,
            )
        except SQLiteStoreSchemaError:
            diagnostics.append("SOURCE_BINDING.ANCESTRY_INVALID")
    else:
        ancestry_rows = _origin_ancestry_rows(connection)
        try:
            cumulative_record_counts = _validate_revision_ancestry_rows(
                ancestry_rows,
                head_revision=head_revision,
                record_count=record_count,
            )
        except SQLiteStoreSchemaError:
            diagnostics.append("SOURCE_BINDING.ANCESTRY_INVALID")

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
    binding: SnapshotBinding | None = None
    if len(rows) > 1:
        diagnostics.append("SOURCE_BINDING.LEDGER_INVALID")
    elif len(rows) == 1:
        try:
            binding = _binding_from_ledger_row(rows[0])
        except (TypeError, ValueError):
            diagnostics.append("SOURCE_BINDING.LEDGER_INVALID")
        else:
            if rows[0][13] != "completed":
                diagnostics.append("SOURCE_BINDING.LEDGER_NOT_COMPLETED")
            if (
                rows[0][11] != rows[0][0]
                or rows[0][12] != rows[0][1]
            ):
                diagnostics.append("SOURCE_BINDING.LEDGER_PATH_MISMATCH")
    fingerprint_payload = {
        "meta": tuple(sorted(meta.items())),
        "record_count": record_count,
        "ancestry_rows": ancestry_rows,
        "ledger_rows": rows,
    }
    canonical_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _SourceBindingFacts(
        head_revision=head_revision,
        record_count=record_count,
        cumulative_record_counts=cumulative_record_counts,
        divergence_latched=_meta_bool(meta, "divergence_latched"),
        binding=binding,
        diagnostic_codes=tuple(sorted(set(diagnostics))),
        canonical_fingerprint=canonical_fingerprint,
    )


def _origin_ancestry_rows(
    connection: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    """Read one canonical batch ancestry under the current schema version.

    The pre-v2 legacy schema has no ``completed_revision`` column and is
    never a runtime canonical: legacy stores are read through the strict
    record-block proof in :func:`_legacy_revision_ancestry` (Task 5.11)
    instead of this v2-only helper.
    """

    return connection.execute(
        "SELECT b.batch_id, b.status, b.completed_revision, "
        "b.valid_count, COUNT(r.record_id) "
        "FROM tm_origin_batch AS b "
        "LEFT JOIN tm_record AS r ON r.origin_batch_id = b.batch_id "
        "GROUP BY b.batch_id, b.status, b.completed_revision, b.valid_count "
        "ORDER BY b.batch_id"
    ).fetchall()


def _legacy_ancestry_fingerprint_rows(
    connection: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    """Deterministic v1 ancestry rows for binding-fingerprint evidence only.

    The legacy rows expose a NULL revision (the v1 schema has no
    ``completed_revision`` column); ordering is by batch id purely for a
    stable fingerprint payload.  Ordering facts are never derived from
    these rows: :func:`_legacy_revision_ancestry` is the only authority
    for v1 completion order and raises when the order is unprovable.
    """

    return connection.execute(
        "SELECT b.batch_id, b.status, NULL, b.valid_count, "
        "COUNT(r.record_id) "
        "FROM tm_origin_batch AS b "
        "LEFT JOIN tm_record AS r ON r.origin_batch_id = b.batch_id "
        "GROUP BY b.batch_id, b.status, b.valid_count "
        "ORDER BY b.batch_id"
    ).fetchall()


def _legacy_completed_origin_blocks(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, int, int], ...]:
    """Prove one pre-v2 store's true completion order from durable facts.

    Every completed v1 batch is one head-revision advance, but the v1
    schema records no completion revision.  The only durable facts that
    prove actual append/completion order are the strictly contiguous
    record-id blocks and per-batch origin-ordinal blocks: record ids are
    assigned in append order by the single-writer store, so the block
    order (never batch-id sorting) reconstructs the true revision order.

    Returns ``(batch_id, revision, valid_count)`` ordered by the proven
    append order.  Any of the following fails closed with
    ``STORE.REVISION_ANCESTRY_MISMATCH``: a zero-record completed batch
    (its position is unprovable), a non-contiguous or interleaved record
    block, a non-contiguous origin-ordinal block, a non-completed batch
    carrying records, or a block range that leaves gaps.  Deliberately
    scrambled batch ids never change the proven order.
    """

    rows = connection.execute(
        "SELECT b.batch_id, b.status, b.valid_count, "
        "COUNT(r.record_id), MIN(r.record_id), MAX(r.record_id), "
        "MIN(r.origin_ordinal), MAX(r.origin_ordinal), "
        "COUNT(DISTINCT r.origin_ordinal), COUNT(DISTINCT r.record_id) "
        "FROM tm_origin_batch AS b "
        "LEFT JOIN tm_record AS r ON r.origin_batch_id = b.batch_id "
        "GROUP BY b.batch_id, b.status, b.valid_count "
        "ORDER BY b.batch_id"
    ).fetchall()
    blocks: list[tuple[str, int, int, int]] = []
    for row in rows:
        if len(row) != 10:
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        (
            batch_id,
            status,
            valid_count,
            record_count,
            min_record_id,
            max_record_id,
            min_ordinal,
            max_ordinal,
            distinct_ordinals,
            distinct_record_ids,
        ) = row
        if (
            type(batch_id) is not str
            or type(status) is not str
            or type(valid_count) is not int
            or type(record_count) is not int
            or valid_count < 0
            or record_count < 0
        ):
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        if status not in {"staged", "failed", "completed"}:
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        if status != "completed":
            if record_count != 0 or min_record_id is not None:
                raise SQLiteStoreSchemaError(
                    "STORE.REVISION_ANCESTRY_MISMATCH"
                )
            continue
        if (
            record_count < 1
            or record_count != valid_count
            or type(min_record_id) is not int
            or type(max_record_id) is not int
            or type(min_ordinal) is not int
            or type(max_ordinal) is not int
            or type(distinct_ordinals) is not int
            or type(distinct_record_ids) is not int
        ):
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        if (
            min_ordinal != 0
            or max_ordinal != record_count - 1
            or distinct_ordinals != record_count
        ):
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        if (
            max_record_id - min_record_id + 1 != record_count
            or distinct_record_ids != record_count
        ):
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        blocks.append((batch_id, record_count, min_record_id, max_record_id))
    blocks.sort(key=lambda item: item[2])
    cursor = 0
    result: list[tuple[str, int, int]] = []
    for batch_id, block_count, min_record_id, max_record_id in blocks:
        if min_record_id != cursor + 1 or max_record_id != cursor + block_count:
            raise SQLiteStoreSchemaError(
                "STORE.REVISION_ANCESTRY_MISMATCH"
            )
        cursor = max_record_id
        result.append((batch_id, len(result) + 1, block_count))
    return tuple(result)


def _legacy_revision_ancestry(
    connection: sqlite3.Connection,
    *,
    head_revision: int,
    record_count: int,
) -> tuple[tuple[int, int], ...]:
    """Derive v1 cumulative record counts from the strict block proof.

    The completion order and the head/count facts must agree exactly:
    one completed batch per revision up to ``head_revision`` and a total
    record count that matches the proven block partition.
    """

    if type(head_revision) is not int or head_revision < 0:
        raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
    if type(record_count) is not int or record_count < 0:
        raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
    blocks = _legacy_completed_origin_blocks(connection)
    if (
        len(blocks) != head_revision
        or sum(count for _batch_id, _revision, count in blocks)
        != record_count
    ):
        raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
    cumulative = 0
    result: list[tuple[int, int]] = []
    for _batch_id, revision, batch_count in blocks:
        cumulative += batch_count
        result.append((revision, cumulative))
    return tuple(result)


def _validate_revision_ancestry_rows(
    rows: list[tuple[object, ...]],
    *,
    head_revision: int,
    record_count: int,
) -> tuple[tuple[int, int], ...]:
    completed: list[tuple[int, int]] = []
    completed_record_count = 0
    for row in rows:
        if len(row) != 5:
            raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
        batch_id, status, completed_revision, valid_count, batch_record_count = row
        if (
            type(batch_id) is not str
            or type(status) is not str
            or type(valid_count) is not int
            or type(batch_record_count) is not int
            or valid_count < 0
            or batch_record_count < 0
        ):
            raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
        if status == "completed":
            if (
                type(completed_revision) is not int
                or completed_revision < 1
                or valid_count != batch_record_count
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.REVISION_ANCESTRY_MISMATCH"
                )
            completed.append((completed_revision, valid_count))
            completed_record_count += batch_record_count
        elif (
            status not in {"staged", "failed"}
            or completed_revision is not None
            or batch_record_count != 0
        ):
            raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
    completed.sort()
    if (
        tuple(revision for revision, _count in completed)
        != tuple(range(1, head_revision + 1))
        or completed_record_count != record_count
    ):
        raise SQLiteStoreSchemaError("STORE.REVISION_ANCESTRY_MISMATCH")
    cumulative = 0
    result: list[tuple[int, int]] = []
    for revision, batch_count in completed:
        cumulative += batch_count
        result.append((revision, cumulative))
    return tuple(result)


def _binding_from_ledger_row(row: tuple[object, ...]) -> SnapshotBinding:
    if len(row) != 14:
        raise ValueError("snapshot ledger row is invalid")
    string_indexes = tuple(range(0, 7)) + (8, 10, 11, 12, 13)
    if any(type(row[index]) is not str for index in string_indexes):
        raise TypeError("snapshot ledger string is invalid")
    if type(row[7]) is not int or type(row[9]) is not int:
        raise TypeError("snapshot ledger integer is invalid")
    values = cast(
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            str,
            int,
            str,
            str,
            str,
            str,
        ],
        row,
    )
    receipt = SnapshotReceipt(
        snapshot_id=values[4],
        resource_id=values[5],
        canonical_store_id=values[6],
        exported_revision=values[7],
        jsonl_digest=values[8],
        record_count=values[9],
        format_version=values[10],
    )
    kind = SnapshotKind(values[2])
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=kind,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return SnapshotBinding(
        configured_jsonl_path=Path(values[0]),
        manifest_path=Path(values[1]),
        snapshot_kind=kind,
        receipt=receipt,
        manifest=manifest,
        binding_version=values[3],
    )


def _strict_pair_file_state(
    path: Path,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._strict_pair_file_state(
        path=path,
    )

def _artifact_parent_dirfd(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> int:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_parent_dirfd(
        destination=destination,
        expected_identity=expected_identity,
        error_factory=SQLiteStoreSchemaError,
    )

def _after_artifact_parent_dirfd_bound(
    destination: Path,
    parent_identity: tuple[int, int] | None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._after_artifact_parent_dirfd_bound(
        destination=destination,
        parent_identity=parent_identity,
    )

def _artifact_handoff_dirfd_entry(
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
    code: str,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_dirfd_entry(
        name=name,
        descriptor=descriptor,
        expected_identity=expected_identity,
        code=code,
        error_factory=SQLiteStoreSchemaError,
    )

def _artifact_handoff_dirfd_absent(
    name: str,
    descriptor: int,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_dirfd_absent(
        name=name,
        descriptor=descriptor,
    )

def _artifact_handoff_dirfd_reprove(
    destination: Path,
    expected_identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_dirfd_reprove(
        destination=destination,
        expected_identity=expected_identity,
        error_factory=SQLiteStoreSchemaError,
    )

def _configured_pair_diagnostics(
    binding: SnapshotBinding,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    head_revision: int,
    cumulative_record_counts: tuple[tuple[int, int], ...],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    try:
        _validate_binding_identity(
            binding,
            identity=identity,
            canonical_store_id=canonical_store_id,
        )
    except (TypeError, ValueError):
        diagnostics.append("SOURCE_BINDING.IDENTITY_MISMATCH")

    receipt = binding.receipt
    if (
        type(head_revision) is not int
        or head_revision < 0
        or receipt.exported_revision > head_revision
    ):
        diagnostics.append("SOURCE_BINDING.ANCESTRY_INVALID")
    record_count_at_revision = {0: 0}
    record_count_at_revision.update(cumulative_record_counts)
    expected_record_count = record_count_at_revision.get(
        receipt.exported_revision
    )
    if (
        expected_record_count is None
        or receipt.record_count != expected_record_count
    ):
        diagnostics.append("SOURCE_BINDING.ANCESTRY_INVALID")

    jsonl_state, jsonl_digest, _jsonl_identity = _strict_pair_file_state(
        identity.configured_jsonl_path
    )
    if jsonl_state == "absent":
        diagnostics.append("SOURCE_BINDING.JSONL_MISSING")
    elif jsonl_state == "unsafe":
        diagnostics.append("SOURCE_BINDING.JSONL_UNSAFE")
    elif jsonl_digest != receipt.jsonl_digest:
        diagnostics.append("SOURCE_BINDING.JSONL_DIGEST_MISMATCH")

    manifest_state, manifest_digest, _manifest_identity = (
        _strict_pair_file_state(identity.snapshot_manifest_path)
    )
    expected_bytes = contract_to_json(binding.manifest).encode("utf-8")
    if manifest_state == "absent":
        diagnostics.append("SOURCE_BINDING.MANIFEST_MISSING")
    elif manifest_state == "unsafe":
        diagnostics.append("SOURCE_BINDING.MANIFEST_UNSAFE")
    elif manifest_digest != hashlib.sha256(expected_bytes).hexdigest():
        diagnostics.append("SOURCE_BINDING.MANIFEST_MISMATCH")
    else:
        try:
            decoded = contract_from_json(expected_bytes.decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            diagnostics.append("SOURCE_BINDING.MANIFEST_INVALID")
        else:
            if type(decoded) is not SnapshotManifest or decoded != binding.manifest:
                diagnostics.append("SOURCE_BINDING.MANIFEST_MISMATCH")
    return tuple(sorted(set(diagnostics)))


def _validate_binding_identity(
    binding: SnapshotBinding,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> None:
    receipt = binding.receipt
    if (
        binding.configured_jsonl_path != identity.configured_jsonl_path
        or binding.manifest_path != identity.snapshot_manifest_path
        or receipt.resource_id != identity.resource_id
        or receipt.canonical_store_id != canonical_store_id
    ):
        raise ValueError("snapshot binding identity does not match store")


def _snapshot_completed_binding(value: object) -> SnapshotBinding:
    if type(value) is not SnapshotBinding:
        raise TypeError("binding must be exact SnapshotBinding")
    if type(value.receipt) is not SnapshotReceipt:
        raise TypeError("binding receipt must be exact SnapshotReceipt")
    if type(value.manifest) is not SnapshotManifest:
        raise TypeError("binding manifest must be exact SnapshotManifest")
    if type(value.snapshot_kind) is not SnapshotKind:
        raise TypeError("binding snapshot kind must be exact SnapshotKind")
    if type(value.manifest.snapshot_kind) is not SnapshotKind:
        raise TypeError("manifest snapshot kind must be exact SnapshotKind")
    if type(value.manifest.receipt) is not SnapshotReceipt:
        raise TypeError("manifest receipt must be exact SnapshotReceipt")
    for path_value in (value.configured_jsonl_path, value.manifest_path):
        if type(path_value) is not _NATIVE_PATH_TYPE:
            raise TypeError("binding paths must be exact native Path values")
    receipt = value.receipt
    manifest_receipt = value.manifest.receipt
    scalar_values = (
        value.binding_version,
        receipt.snapshot_id,
        receipt.resource_id,
        receipt.canonical_store_id,
        receipt.jsonl_digest,
        receipt.format_version,
        value.manifest.manifest_version,
        value.manifest.receipt_digest,
        manifest_receipt.snapshot_id,
        manifest_receipt.resource_id,
        manifest_receipt.canonical_store_id,
        manifest_receipt.jsonl_digest,
        manifest_receipt.format_version,
    )
    if any(type(item) is not str for item in scalar_values):
        raise TypeError("binding scalar values must use built-in strings")
    if type(receipt.exported_revision) is not int:
        raise TypeError("binding revision must be a built-in integer")
    if type(receipt.record_count) is not int:
        raise TypeError("binding record count must be a built-in integer")
    if type(manifest_receipt.exported_revision) is not int:
        raise TypeError("manifest revision must be a built-in integer")
    if type(manifest_receipt.record_count) is not int:
        raise TypeError("manifest record count must be a built-in integer")
    serialized = contract_to_json(value)
    copied = contract_from_json(serialized)
    if type(copied) is not SnapshotBinding:
        raise TypeError("binding private snapshot is invalid")
    return copied


def _snapshot_receipt(value: object) -> SnapshotReceipt:
    if type(value) is not SnapshotReceipt:
        raise TypeError("receipt must be exact SnapshotReceipt")
    scalar_values = (
        value.snapshot_id,
        value.resource_id,
        value.canonical_store_id,
        value.jsonl_digest,
        value.format_version,
    )
    if any(type(item) is not str for item in scalar_values):
        raise TypeError("receipt scalar values must use built-in strings")
    if type(value.exported_revision) is not int:
        raise TypeError("receipt revision must be a built-in integer")
    if type(value.record_count) is not int:
        raise TypeError("receipt record count must be a built-in integer")
    return SnapshotReceipt(
        snapshot_id=value.snapshot_id,
        resource_id=value.resource_id,
        canonical_store_id=value.canonical_store_id,
        exported_revision=value.exported_revision,
        jsonl_digest=value.jsonl_digest,
        record_count=value.record_count,
        format_version=value.format_version,
    )


def _snapshot_binding_digest(binding: SnapshotBinding) -> str:
    return hashlib.sha256(contract_to_json(binding).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _latch_source_divergence(
    lease: _SQLiteGenerationView,
    *,
    expected_fingerprint: str,
) -> bool:
    with _open_leased_connection(lease) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_facts = _read_source_binding_facts_in_transaction(
                connection,
                lease,
            )
            if (
                current_facts.canonical_fingerprint
                != expected_fingerprint
            ):
                connection.rollback()
                return False
            updated = connection.execute(
                "UPDATE tm_meta SET value = '1' "
                "WHERE key = 'divergence_latched'"
            )
            if updated.rowcount != 1:
                raise SQLiteStoreSchemaError(
                    "STORE.DIVERGENCE_LATCH_MISSING"
                )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise


_REFRESH_RESERVATION_TIMEOUT_SECONDS = 300.0


def _require_timeout(value: object) -> float:
    if type(value) is int:
        timeout = float(value)
    elif type(value) is float:
        timeout = value
    else:
        raise TypeError("timeout_seconds must be a built-in number")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout_seconds must be finite and non-negative")
    return timeout


def _require_artifact_identity_pair(
    value: object,
    field_name: str,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._require_artifact_identity_pair(
        value=value,
        field_name=field_name,
    )

_ARTIFACT_HANDOFF_META_PREFIX = snapshot_artifacts_module._ARTIFACT_HANDOFF_META_PREFIX
"""_ARTIFACT_HANDOFF_META_PREFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""

def _artifact_handoff_meta_key(snapshot_id: str) -> str:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_meta_key(
        snapshot_id=snapshot_id,
    )

def _artifact_handoff_prior_record(
    *,
    identity: tuple[int, int] | None,
    digest: str | None,
    absent: bool | None,
    field_name: str,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_prior_record(
        identity=identity,
        digest=digest,
        absent=absent,
        field_name=field_name,
    )

def _artifact_handoff_meta_value(
    *,
    jsonl_temp_identity: tuple[int, int] | None,
    manifest_temp_identity: tuple[int, int] | None,
    artifact_parent_identity: tuple[int, int] | None = None,
    jsonl_recovery_identity: tuple[int, int] | None = None,
    manifest_recovery_identity: tuple[int, int] | None = None,
    prior_jsonl_identity: tuple[int, int] | None = None,
    prior_jsonl_digest: str | None = None,
    prior_jsonl_absent: bool | None = None,
    prior_manifest_identity: tuple[int, int] | None = None,
    prior_manifest_digest: str | None = None,
    prior_manifest_absent: bool | None = None,
) -> str:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_meta_value(
        jsonl_temp_identity=jsonl_temp_identity,
        manifest_temp_identity=manifest_temp_identity,
        artifact_parent_identity=artifact_parent_identity,
        jsonl_recovery_identity=jsonl_recovery_identity,
        manifest_recovery_identity=manifest_recovery_identity,
        prior_jsonl_identity=prior_jsonl_identity,
        prior_jsonl_digest=prior_jsonl_digest,
        prior_jsonl_absent=prior_jsonl_absent,
        prior_manifest_identity=prior_manifest_identity,
        prior_manifest_digest=prior_manifest_digest,
        prior_manifest_absent=prior_manifest_absent,
    )

def _artifact_handoff_from_meta(
    key: str,
    value: str,
) -> snapshot_recovery_module._ArtifactHandoffFacts | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_handoff_from_meta(
        key=key,
        value=value,
    )

class _SnapshotRecoveryPort:
    """Narrow store-side adapter for Task 5.14 snapshot recovery.

    Implements the ``tm_snapshot_recovery`` ``_SnapshotRecoveryPort``
    protocol over one ``SQLiteTMStore`` and converts the expected store
    failures (lifecycle/schema/database) into the recovery module's
    stable ``RecoveryError`` so the module never imports the store.
    Unexpected programmer errors propagate unchanged.
    """

    def __init__(self, store: SQLiteTMStore) -> None:
        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore")
        self._store = store

    def read_recovery_facts(
        self,
    ) -> snapshot_recovery_module._RefreshRecoveryFacts:
        try:
            return self._store._read_refresh_recovery_facts()
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def cancel_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        try:
            self._store.cancel_issued_refresh_receipt(
                snapshot_id,
                expected_generation=expected_generation,
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def complete_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int] | None = None,
        manifest_identity: tuple[int, int] | None = None,
    ) -> None:
        try:
            self._store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=expected_generation,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
                _allow_history_revision=True,
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def complete_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
    ) -> None:
        try:
            self._store.complete_issued_export_receipt(
                snapshot_id,
                expected_generation=expected_generation,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def cancel_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        try:
            self._store.cancel_issued_export_receipt(
                snapshot_id,
                expected_generation=expected_generation,
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def clear_issued_receipt_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        try:
            self._store.clear_issued_receipt_handoff(
                snapshot_id,
                expected_generation=expected_generation,
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def latch_source_divergence(self, expected_fingerprint: str) -> bool:
        try:
            return self._store.latch_refresh_divergence(
                expected_fingerprint=expected_fingerprint
            )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error


class _BoundSnapshotRecoveryPort(_SnapshotRecoveryPort):
    """Store effects for portable configured-refresh recovery."""

    def cancel_bound_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> None:
        try:
            self._store.cancel_bound_recovered_refresh_receipt(
                snapshot_id,
                expected_generation=expected_generation,
                expected_receipt=expected_receipt,
                expected_handoff=expected_handoff,
                expected_binding=expected_binding,
                family_reprove=family_reprove,
            )
        except (SQLiteStoreLifecycleError, SQLiteStoreSchemaError, sqlite3.DatabaseError) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def complete_bound_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[SnapshotReceipt], None],
    ) -> None:
        try:
            self._store.complete_bound_recovered_refresh_receipt(
                snapshot_id,
                expected_generation=expected_generation,
                expected_receipt=expected_receipt,
                expected_handoff=expected_handoff,
                expected_binding=expected_binding,
                family_reprove=family_reprove,
            )
        except (SQLiteStoreLifecycleError, SQLiteStoreSchemaError, sqlite3.DatabaseError) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def latch_bound_refresh_divergence(
        self,
        expected_fingerprint: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> bool:
        try:
            return self._store.latch_bound_refresh_divergence(
                expected_fingerprint=expected_fingerprint,
                expected_generation=expected_generation,
                expected_receipt=expected_receipt,
                expected_handoff=expected_handoff,
                expected_binding=expected_binding,
                family_reprove=family_reprove,
            )
        except (SQLiteStoreLifecycleError, SQLiteStoreSchemaError, sqlite3.DatabaseError) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error

    def clear_bound_refresh_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> None:
        try:
            self._store.clear_bound_refresh_handoff(
                snapshot_id,
                expected_generation=expected_generation,
                expected_receipt=expected_receipt,
                expected_handoff=expected_handoff,
                expected_binding=expected_binding,
                family_reprove=family_reprove,
            )
        except (SQLiteStoreLifecycleError, SQLiteStoreSchemaError, sqlite3.DatabaseError) as error:
            raise snapshot_recovery_module.RecoveryError(
                _recovery_store_error_code(error),
                retryable=_recovery_store_retryable(error),
            ) from error


def _recovery_store_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    message = getattr(error, "args", None)
    if (
        type(message) is tuple
        and message
        and type(message[0]) is str
        and message[0]
    ):
        return message[0]
    return "RECOVERY.STORE_FAILED"


def _recovery_store_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _require_bound_refresh_effect_snapshot(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
    *,
    expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
    expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
    expected_binding: SnapshotBinding,
) -> _SourceBindingFacts:
    """Reprove the exact classified SQLite owner facts inside one write txn."""

    if type(expected_receipt) is not snapshot_recovery_module.IssuedReceiptFacts:
        raise TypeError("expected_receipt must be exact IssuedReceiptFacts")
    if type(expected_handoff) is not snapshot_recovery_module._BoundRefreshHandoffFacts:
        raise TypeError("expected_handoff must be exact bound refresh facts")
    if type(expected_binding) is not SnapshotBinding:
        raise TypeError("expected_binding must be exact SnapshotBinding")
    if expected_receipt.snapshot_id != expected_handoff.snapshot_id:
        raise ValueError("classified receipt and handoff disagree")
    facts = _read_source_binding_facts_in_transaction(connection, lease)
    receipt_columns = (
        "snapshot_id, resource_id, canonical_store_id, "
        "exported_revision, jsonl_digest, record_count, format_version, "
        "destination_jsonl_path, destination_manifest_path, status"
    )
    receipt_row = connection.execute(
        "SELECT snapshot_id, resource_id, canonical_store_id, "
        "exported_revision, jsonl_digest, record_count, format_version, "
        "destination_jsonl_path, destination_manifest_path, status "
        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
        (expected_receipt.snapshot_id,),
    ).fetchone()
    if receipt_row is None:
        raise SQLiteStoreSchemaError(
            "STORE.BOUND_REFRESH_CLASSIFICATION_CHANGED"
        )
    try:
        observed_receipt = snapshot_recovery_module.IssuedReceiptFacts(
            snapshot_id=str(receipt_row[0]),
            resource_id=str(receipt_row[1]),
            canonical_store_id=str(receipt_row[2]),
            exported_revision=_row_int(receipt_row[3]),
            jsonl_digest=str(receipt_row[4]),
            record_count=_row_int(receipt_row[5]),
            format_version=str(receipt_row[6]),
            destination_jsonl_path=Path(str(receipt_row[7])),
            destination_manifest_path=Path(str(receipt_row[8])),
            status=str(receipt_row[9]),
        )
    except (TypeError, ValueError):
        raise SQLiteStoreSchemaError(
            "STORE.BOUND_REFRESH_CLASSIFICATION_CHANGED"
        ) from None
    configured_issued_rows = connection.execute(
        f"SELECT {receipt_columns} FROM tm_snapshot_receipt "
        "WHERE status = 'issued' AND destination_jsonl_path = ? "
        "AND destination_manifest_path = ? ORDER BY snapshot_id",
        (
            Path.__str__(lease.stage.resource_identity.configured_jsonl_path),
            Path.__str__(lease.stage.resource_identity.snapshot_manifest_path),
        ),
    ).fetchall()
    try:
        configured_issued = tuple(
            snapshot_recovery_module.IssuedReceiptFacts(
                snapshot_id=str(row[0]),
                resource_id=str(row[1]),
                canonical_store_id=str(row[2]),
                exported_revision=_row_int(row[3]),
                jsonl_digest=str(row[4]),
                record_count=_row_int(row[5]),
                format_version=str(row[6]),
                destination_jsonl_path=Path(str(row[7])),
                destination_manifest_path=Path(str(row[8])),
                status=str(row[9]),
            )
            for row in configured_issued_rows
        )
    except (TypeError, ValueError):
        raise SQLiteStoreSchemaError(
            "STORE.BOUND_REFRESH_CLASSIFICATION_CHANGED"
        ) from None
    expected_configured_issued = (
        (expected_receipt,) if expected_receipt.status == "issued" else ()
    )
    ancestry_counts = dict(facts.cumulative_record_counts)
    ancestry_counts[0] = 0
    bound_rows = connection.execute(
        "SELECT key, value FROM tm_meta WHERE key LIKE ? ORDER BY key",
        (
            f"{snapshot_recovery_module._BOUND_REFRESH_HANDOFF_META_PREFIX}%",
        ),
    ).fetchall()
    observed_handoff = None
    if len(bound_rows) == 1:
        observed_handoff = snapshot_recovery_module._bound_refresh_handoff_from_meta(
            str(bound_rows[0][0]),
            str(bound_rows[0][1]),
        )
    configured_legacy_row = connection.execute(
        "SELECT 1 FROM tm_meta AS m "
        "JOIN tm_snapshot_receipt AS r "
        "ON m.key = ? || r.snapshot_id "
        "WHERE r.destination_jsonl_path = ? "
        "AND r.destination_manifest_path = ? LIMIT 1",
        (
            _ARTIFACT_HANDOFF_META_PREFIX,
            Path.__str__(lease.stage.resource_identity.configured_jsonl_path),
            Path.__str__(lease.stage.resource_identity.snapshot_manifest_path),
        ),
    ).fetchone()
    if (
        facts.binding != expected_binding
        or observed_receipt != expected_receipt
        or observed_handoff != expected_handoff
        or configured_issued != expected_configured_issued
        or facts.diagnostic_codes
        or ancestry_counts.get(expected_receipt.exported_revision)
        != expected_receipt.record_count
        or configured_legacy_row is not None
    ):
        raise SQLiteStoreSchemaError(
            "STORE.BOUND_REFRESH_CLASSIFICATION_CHANGED"
        )
    return facts



class SQLiteTMStore:
    """Per-resource store whose public operations use generation leases."""

    def __init__(
        self,
        stage: MutableStageRef,
        *,
        canonical_store_id: str,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        self._canonical_store_id = canonical_store_id
        self._coordinator = ResourceStoreCoordinator(
            stage,
            canonical_store_id=self._canonical_store_id,
            drain_timeout_seconds=drain_timeout_seconds,
        )
        self._source_binding_monitor = SourceBindingMonitor(
            self._coordinator,
            store=self,
        )
        self._query_view_tokens: set[object] = set()

    @classmethod
    def from_coordinator(
        cls,
        coordinator: ResourceStoreCoordinator,
    ) -> SQLiteTMStore:
        """Construct a store bound to an already-recovered canonical coordinator.

        Task 6.2 facade seam: the coordinator must be ``READY`` with a
        live canonical generation view (for example one rehydrated by
        :meth:`ResourceStoreCoordinator.recover_durable_activation`).
        The returned store reuses the coordinator's lease, identity and
        canonical store id instead of minting a new stage-bound
        coordinator, so an active canonical sidecar is never re-opened
        under an unactivated stage contract.
        """

        if type(coordinator) is not ResourceStoreCoordinator:
            raise TypeError("coordinator must be ResourceStoreCoordinator")
        observed_generation = coordinator.current_generation
        if coordinator.state != "READY" or observed_generation is None:
            raise SQLiteStoreLifecycleError(
                "STORE.CANONICAL_UNAVAILABLE",
                resource_id=coordinator.resource_id,
                generation=(
                    0 if observed_generation is None else observed_generation
                ),
                retryable=False,
            )
        store = cls.__new__(cls)
        store._canonical_store_id = coordinator.canonical_store_id
        store._coordinator = coordinator
        store._source_binding_monitor = SourceBindingMonitor(
            coordinator,
            store=store,
        )
        store._query_view_tokens = set()
        return store

    @property
    def coordinator(self) -> ResourceStoreCoordinator:
        return self._coordinator

    @property
    def resource_id(self) -> str:
        """Expose only the immutable resource identity required by recall ports."""

        return self._coordinator.resource_id

    @property
    def candidate_port_scope(self) -> str:
        return "STORE"

    @property
    def source_binding_monitor(self) -> SourceBindingMonitor:
        return self._source_binding_monitor

    def configured_refresh_reservation(
        self,
        timeout_seconds: float | None = None,
    ) -> AbstractContextManager[None]:
        """Serialize one configured snapshot refresh for this resource.

        The reservation is owned by the coordinator and scoped to the
        refresh operation so a second refresh never observes the first
        refresh's intentional JSONL-only publication window.
        """

        return self._coordinator.configured_refresh_reservation(
            timeout_seconds=timeout_seconds,
        )

    def canonical_revision(self) -> CanonicalRevisionSnapshot:
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                return _canonical_revision_from_connection(
                    connection,
                    lease,
                )

    def capture_export_snapshot(self) -> CanonicalExportSnapshot:
        """Capture revision and complete record order in one read snapshot.

        Task 5.12 export seam: the revision (generation, canonical store
        id, head revision, record count) and every canonical record are
        observed inside one operation lease and one read transaction, so
        an export can never mix bytes from two different revisions.
        """

        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN")
                try:
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    rows = connection.execute(
                        f"SELECT {_EXPORT_RECORD_COLUMNS} FROM tm_record "
                        "ORDER BY record_id ASC"
                    ).fetchall()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return CanonicalExportSnapshot(
            revision=revision,
            records=tuple(_export_record_from_row(row) for row in rows),
        )

    def validate_bound_refresh_preflight(
        self,
        *,
        family_matches: Callable[[SnapshotBinding], bool],
    ) -> SourceBindingObservation:
        """Validate the configured binding without reopening either pathname.

        The Windows refresh owner already holds the resource W1 reservation and
        exact rooted handles for the configured JSONL/manifest family.  This
        transaction closes the ledger half of that proof and asks the caller to
        compare those live handle facts with the completed binding.  A mismatch
        latches source divergence before any refresh candidate or receipt can be
        created.  Issued configured receipts are left for the explicit recovery
        entry and therefore block a new refresh here.
        """

        if not callable(family_matches):
            raise TypeError("family_matches must be callable")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    facts = _read_source_binding_facts_in_transaction(
                        connection,
                        lease,
                    )
                    issued = connection.execute(
                        "SELECT 1 FROM tm_snapshot_receipt "
                        "WHERE status = 'issued' AND "
                        "destination_jsonl_path = ? AND "
                        "destination_manifest_path = ? LIMIT 1",
                        (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                        ),
                    ).fetchone()
                    if issued is not None:
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_RECOVERY_REQUIRED"
                        )
                    binding = facts.binding
                    binding_count = (
                        None
                        if binding is None
                        else dict(facts.cumulative_record_counts).get(
                            binding.receipt.exported_revision
                        )
                    )
                    diverged = (
                        facts.divergence_latched
                        or bool(facts.diagnostic_codes)
                        or binding is None
                        or binding_count != binding.receipt.record_count
                    )
                    if not diverged:
                        assert binding is not None
                        try:
                            _validate_binding_identity(
                                binding,
                                identity=identity,
                                canonical_store_id=lease.canonical_store_id,
                            )
                        except (TypeError, ValueError):
                            diverged = True
                    if not diverged:
                        assert binding is not None
                        matched = family_matches(binding)
                        if type(matched) is not bool:
                            raise TypeError(
                                "family_matches must return a built-in bool"
                            )
                        diverged = not matched
                    if diverged:
                        if not facts.divergence_latched:
                            updated = connection.execute(
                                "UPDATE tm_meta SET value = '1' "
                                "WHERE key = 'divergence_latched'"
                            )
                            if updated.rowcount != 1:
                                raise SQLiteStoreSchemaError(
                                    "STORE.DIVERGENCE_LATCH_MISSING"
                                )
                        connection.commit()
                        raise SQLiteStoreSchemaError(
                            "STORE.DIVERGENCE_LATCHED"
                        )
                    assert binding is not None
                    state = (
                        SourceBindingState.VERIFIED_CURRENT
                        if binding.receipt.exported_revision
                        == facts.head_revision
                        else SourceBindingState.VERIFIED_HISTORY
                    )
                    observation = SourceBindingObservation(
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        generation=lease.generation,
                        head_revision=facts.head_revision,
                        state=state,
                        binding_digest=_snapshot_binding_digest(binding),
                        diagnostic_codes=(),
                    )
                    connection.commit()
                    return observation
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

    def register_issued_snapshot_receipt(
        self,
        receipt: SnapshotReceipt,
        *,
        destination_jsonl_path: Path,
        destination_manifest_path: Path,
    ) -> None:
        """Record one unpublished migration receipt inside a mutable stage."""

        private_receipt = _snapshot_receipt(receipt)
        for path_value, field_name in (
            (destination_jsonl_path, "destination_jsonl_path"),
            (destination_manifest_path, "destination_manifest_path"),
        ):
            if type(path_value) is not _NATIVE_PATH_TYPE:
                raise TypeError(f"{field_name} must be an exact native Path")
        private_jsonl_path = _copy_exact_path(destination_jsonl_path)
        private_manifest_path = _copy_exact_path(destination_manifest_path)

        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if (
                private_jsonl_path != identity.configured_jsonl_path
                or private_manifest_path != identity.snapshot_manifest_path
                or private_receipt.resource_id != identity.resource_id
                or private_receipt.canonical_store_id
                != lease.canonical_store_id
            ):
                raise ValueError("issued receipt identity does not match store")
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    revision = _canonical_revision_from_connection(
                        connection,
                        lease,
                    )
                    if (
                        private_receipt.exported_revision
                        != revision.head_revision
                        or private_receipt.record_count
                        != revision.record_count
                    ):
                        raise ValueError(
                            "issued receipt must describe current revision"
                        )
                    receipt_count = connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_receipt"
                    ).fetchone()
                    binding_count = connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone()
                    if receipt_count != (0,) or binding_count != (0,):
                        raise ValueError(
                            "unpublished stage already contains snapshot state"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_receipt("
                        "snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?)",
                        (
                            private_receipt.snapshot_id,
                            private_receipt.resource_id,
                            private_receipt.canonical_store_id,
                            private_receipt.exported_revision,
                            private_receipt.jsonl_digest,
                            private_receipt.record_count,
                            private_receipt.format_version,
                            Path.__str__(private_jsonl_path),
                            Path.__str__(private_manifest_path),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def register_issued_export_receipt(
        self,
        receipt: SnapshotReceipt,
        *,
        destination_jsonl_path: Path,
        destination_manifest_path: Path,
        expected_generation: int,
        jsonl_temp_identity: tuple[int, int] | None = None,
        manifest_temp_identity: tuple[int, int] | None = None,
        artifact_parent_identity: tuple[int, int] | None = None,
        prior_jsonl_identity: tuple[int, int] | None = None,
        prior_jsonl_digest: str | None = None,
        prior_jsonl_absent: bool | None = None,
        prior_manifest_identity: tuple[int, int] | None = None,
        prior_manifest_digest: str | None = None,
        prior_manifest_absent: bool | None = None,
    ) -> None:
        """Atomically register one arbitrary-destination issued receipt.

        Task 5.12 ledger seam: the receipt must describe exactly the
        captured revision (generation, canonical store id, revision
        ancestry and record count) of the live canonical store.  The
        destination paths are caller-chosen arbitrary paths and must not
        alias the configured JSONL or its manifest; they are stored only
        in the ledger and never enter the portable receipt digest.  The
        snapshot binding, divergence latch, head revision and generation
        are never modified.

        Task 5.14 durable ownership seam: when the exclusive JSONL and
        manifest temporary identities are supplied they are recorded in
        the write-once ``tm_meta`` key ``artifact_handoff.<snapshot_id>``
        in the same transaction, so crash recovery can prove ownership of those
        temporaries before any destructive unlink or replace.  When they
        are omitted the receipt is identity-less and recovery fails
        closed on any matching artifact instead of deleting it.

        The exact immediate-parent device/inode proven by a strict real
        non-symlink parent-chain proof is mandatory in the same journal
        (``artifact_parent_identity``): terminal cleanup and handoff
        release re-prove it so a hostile parent rename or replacement
        between registration, cleanup, fsync and release never clears
        the handoff or reports COMPLETED.

        The prior final JSONL/manifest identity and digest (or explicit
        proven absence) are captured before any publication replace and
        recorded in the same handoff journal so reconstruction can
        replace an old manifest only against the durably recorded prior
        identity; unrecorded prior state always fails closed.
        """

        private_receipt = _snapshot_receipt(receipt)
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        _require_artifact_identity_pair(
            jsonl_temp_identity,
            "jsonl_temp_identity",
        )
        _require_artifact_identity_pair(
            manifest_temp_identity,
            "manifest_temp_identity",
        )
        _require_artifact_identity_pair(
            artifact_parent_identity,
            "artifact_parent_identity",
        )
        if (jsonl_temp_identity is None) != (manifest_temp_identity is None):
            raise ValueError(
                "jsonl and manifest temp identities must be supplied together"
            )
        if jsonl_temp_identity is None:
            if artifact_parent_identity is not None:
                raise ValueError(
                    "artifact parent identity requires a durable "
                    "handoff journal"
                )
        elif artifact_parent_identity is None:
            raise ValueError(
                "durable handoff journal requires an artifact "
                "parent identity"
            )
        for value, field_name, absent in (
            (
                prior_jsonl_identity,
                "prior_jsonl_identity",
                prior_jsonl_absent,
            ),
            (
                prior_manifest_identity,
                "prior_manifest_identity",
                prior_manifest_absent,
            ),
        ):
            _require_artifact_identity_pair(value, field_name)
            if type(absent) is not bool and absent is not None:
                raise ValueError(f"{field_name} absent flag is invalid")
        if jsonl_temp_identity is None and any(
            value is not None
            for value in (
                prior_jsonl_identity,
                prior_jsonl_digest,
                prior_jsonl_absent,
                prior_manifest_identity,
                prior_manifest_digest,
                prior_manifest_absent,
            )
        ):
            raise ValueError(
                "prior pair record requires a durable handoff journal"
            )
        _artifact_handoff_prior_record(
            identity=prior_jsonl_identity,
            digest=prior_jsonl_digest,
            absent=prior_jsonl_absent,
            field_name="prior_jsonl",
        )
        _artifact_handoff_prior_record(
            identity=prior_manifest_identity,
            digest=prior_manifest_digest,
            absent=prior_manifest_absent,
            field_name="prior_manifest",
        )
        for path_value, field_name in (
            (destination_jsonl_path, "destination_jsonl_path"),
            (destination_manifest_path, "destination_manifest_path"),
        ):
            if type(path_value) is not _NATIVE_PATH_TYPE:
                raise TypeError(f"{field_name} must be an exact native Path")
        private_jsonl_path = _copy_exact_path(destination_jsonl_path)
        private_manifest_path = _copy_exact_path(destination_manifest_path)
        _require_absolute_path(
            private_jsonl_path,
            "export destination jsonl path",
        )
        _require_absolute_path(
            private_manifest_path,
            "export destination manifest path",
        )
        if private_jsonl_path == private_manifest_path:
            raise ValueError("export destination paths must differ")
        if private_manifest_path != private_jsonl_path.with_name(
            f"{private_jsonl_path.name}.localcat-snapshot.json"
        ):
            raise ValueError(
                "export destination manifest path is not deterministic"
            )

        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if (
                private_jsonl_path == identity.configured_jsonl_path
                or private_jsonl_path == identity.snapshot_manifest_path
                or private_manifest_path == identity.snapshot_manifest_path
                or private_jsonl_path == identity.canonical_sidecar_path
                or private_receipt.resource_id != identity.resource_id
                or private_receipt.canonical_store_id
                != lease.canonical_store_id
            ):
                raise ValueError(
                    "issued export receipt identity does not match store"
                )
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    if (
                        private_receipt.exported_revision
                        > revision.head_revision
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_REVISION_STALE"
                        )
                    counts = _revision_record_counts(
                        connection,
                        head_revision=revision.head_revision,
                        record_count=revision.record_count,
                    )
                    expected_count = counts.get(
                        private_receipt.exported_revision
                    )
                    if (
                        expected_count is None
                        or private_receipt.record_count != expected_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_ANCESTRY_INVALID"
                        )
                    existing = connection.execute(
                        "SELECT 1 FROM tm_snapshot_receipt "
                        "WHERE snapshot_id = ?",
                        (private_receipt.snapshot_id,),
                    ).fetchone()
                    if existing is not None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_DUPLICATE"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_receipt("
                        "snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?)",
                        (
                            private_receipt.snapshot_id,
                            private_receipt.resource_id,
                            private_receipt.canonical_store_id,
                            private_receipt.exported_revision,
                            private_receipt.jsonl_digest,
                            private_receipt.record_count,
                            private_receipt.format_version,
                            Path.__str__(private_jsonl_path),
                            Path.__str__(private_manifest_path),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    if jsonl_temp_identity is not None:
                        assert manifest_temp_identity is not None
                        parent_descriptor = _artifact_parent_dirfd(
                            private_jsonl_path,
                            artifact_parent_identity,
                        )
                        try:
                            _after_artifact_parent_dirfd_bound(
                                private_jsonl_path,
                                artifact_parent_identity,
                            )
                            artifact_paths = (
                                snapshot_recovery_module
                                ._recovery_artifact_paths(
                                    private_jsonl_path
                                )
                            )
                            _artifact_handoff_dirfd_entry(
                                artifact_paths.jsonl_temp.name,
                                parent_descriptor,
                                jsonl_temp_identity,
                                "STORE.HANDOFF_TEMP_UNPROVEN",
                            )
                            _artifact_handoff_dirfd_entry(
                                artifact_paths.manifest_temp.name,
                                parent_descriptor,
                                manifest_temp_identity,
                                "STORE.HANDOFF_TEMP_UNPROVEN",
                            )
                            connection.execute(
                                "INSERT INTO tm_meta(key, value) "
                                "VALUES (?, ?)",
                                (
                                    _artifact_handoff_meta_key(
                                        private_receipt.snapshot_id
                                    ),
                                    _artifact_handoff_meta_value(
                                        jsonl_temp_identity=(
                                            jsonl_temp_identity
                                        ),
                                        manifest_temp_identity=(
                                            manifest_temp_identity
                                        ),
                                        artifact_parent_identity=(
                                            artifact_parent_identity
                                        ),
                                        prior_jsonl_identity=(
                                            prior_jsonl_identity
                                        ),
                                        prior_jsonl_digest=(
                                            prior_jsonl_digest
                                        ),
                                        prior_jsonl_absent=(
                                            prior_jsonl_absent
                                        ),
                                        prior_manifest_identity=(
                                            prior_manifest_identity
                                        ),
                                        prior_manifest_digest=(
                                            prior_manifest_digest
                                        ),
                                        prior_manifest_absent=(
                                            prior_manifest_absent
                                        ),
                                    ),
                                ),
                            )
                        finally:
                            os.close(parent_descriptor)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def register_issued_refresh_receipt(
        self,
        receipt: SnapshotReceipt,
        *,
        expected_generation: int,
        jsonl_temp_identity: tuple[int, int] | None = None,
        manifest_temp_identity: tuple[int, int] | None = None,
        artifact_parent_identity: tuple[int, int] | None = None,
        prior_jsonl_identity: tuple[int, int] | None = None,
        prior_jsonl_digest: str | None = None,
        prior_jsonl_absent: bool | None = None,
        prior_manifest_identity: tuple[int, int] | None = None,
        prior_manifest_digest: str | None = None,
        prior_manifest_absent: bool | None = None,
        bound_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts | None = None,
    ) -> None:
        """Atomically register one configured-path issued refresh receipt.

        Task 5.13 ledger seam: the receipt must describe exactly the
        current canonical revision of the live store (never a history
        revision), the resource must not be divergence-latched, and the
        stored destination paths are the configured JSONL and its
        deterministic adjacent manifest.  The snapshot binding, head
        revision and generation are never modified here.

        Task 5.14 durable ownership seam: same write-once ``tm_meta``
        ``artifact_handoff.<snapshot_id>`` record as the export seam when
        the exclusive temporary identities are supplied, including the
        exact immediate-parent device/inode (``artifact_parent_identity``)
        proven by a strict real non-symlink parent-chain proof and the
        prior final JSONL/manifest identity+digest or explicit proven
        absence captured before any publication replace.
        """

        private_receipt = _snapshot_receipt(receipt)
        if bound_handoff is not None and type(bound_handoff) is not (
            snapshot_recovery_module._BoundRefreshHandoffFacts
        ):
            raise TypeError("bound_handoff must be exact facts or None")
        if bound_handoff is not None and (
            bound_handoff.snapshot_id != private_receipt.snapshot_id
            or bound_handoff.new_jsonl_digest != private_receipt.jsonl_digest
            or jsonl_temp_identity is not None
        ):
            raise ValueError("bound refresh handoff does not match receipt")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        _require_artifact_identity_pair(
            jsonl_temp_identity,
            "jsonl_temp_identity",
        )
        _require_artifact_identity_pair(
            manifest_temp_identity,
            "manifest_temp_identity",
        )
        _require_artifact_identity_pair(
            artifact_parent_identity,
            "artifact_parent_identity",
        )
        if (jsonl_temp_identity is None) != (manifest_temp_identity is None):
            raise ValueError(
                "jsonl and manifest temp identities must be supplied together"
            )
        if jsonl_temp_identity is None:
            if artifact_parent_identity is not None:
                raise ValueError(
                    "artifact parent identity requires a durable "
                    "handoff journal"
                )
        elif artifact_parent_identity is None:
            raise ValueError(
                "durable handoff journal requires an artifact "
                "parent identity"
            )
        for value, field_name, absent in (
            (
                prior_jsonl_identity,
                "prior_jsonl_identity",
                prior_jsonl_absent,
            ),
            (
                prior_manifest_identity,
                "prior_manifest_identity",
                prior_manifest_absent,
            ),
        ):
            _require_artifact_identity_pair(value, field_name)
            if type(absent) is not bool and absent is not None:
                raise ValueError(f"{field_name} absent flag is invalid")
        if jsonl_temp_identity is None and any(
            value is not None
            for value in (
                prior_jsonl_identity,
                prior_jsonl_digest,
                prior_jsonl_absent,
                prior_manifest_identity,
                prior_manifest_digest,
                prior_manifest_absent,
            )
        ):
            raise ValueError(
                "prior pair record requires a durable handoff journal"
            )
        _artifact_handoff_prior_record(
            identity=prior_jsonl_identity,
            digest=prior_jsonl_digest,
            absent=prior_jsonl_absent,
            field_name="prior_jsonl",
        )
        _artifact_handoff_prior_record(
            identity=prior_manifest_identity,
            digest=prior_manifest_digest,
            absent=prior_manifest_absent,
            field_name="prior_manifest",
        )
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if (
                private_receipt.resource_id != identity.resource_id
                or private_receipt.canonical_store_id
                != lease.canonical_store_id
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.RECEIPT_IDENTITY_MISMATCH"
                )
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    if (
                        private_receipt.exported_revision
                        != revision.head_revision
                        or private_receipt.record_count
                        != revision.record_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_REVISION_STALE"
                        )
                    meta = _read_meta(connection)
                    if _meta_bool(meta, "divergence_latched"):
                        raise SQLiteStoreSchemaError(
                            "STORE.DIVERGENCE_LATCHED"
                        )
                    existing = connection.execute(
                        "SELECT 1 FROM tm_snapshot_receipt "
                        "WHERE snapshot_id = ?",
                        (private_receipt.snapshot_id,),
                    ).fetchone()
                    if existing is not None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_DUPLICATE"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_receipt("
                        "snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?)",
                        (
                            private_receipt.snapshot_id,
                            private_receipt.resource_id,
                            private_receipt.canonical_store_id,
                            private_receipt.exported_revision,
                            private_receipt.jsonl_digest,
                            private_receipt.record_count,
                            private_receipt.format_version,
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    if bound_handoff is not None:
                        existing_bound_handoff = connection.execute(
                            "SELECT 1 FROM tm_meta WHERE key LIKE ? LIMIT 1",
                            (
                                f"{snapshot_recovery_module._BOUND_REFRESH_HANDOFF_META_PREFIX}%",
                            ),
                        ).fetchone()
                        if existing_bound_handoff is not None:
                            raise SQLiteStoreSchemaError(
                                "STORE.BOUND_REFRESH_HANDOFF_PENDING"
                            )
                        binding_facts = _read_source_binding_facts_in_transaction(
                            connection, lease
                        )
                        binding = binding_facts.binding
                        if (
                            binding is None
                            or binding.receipt.snapshot_id
                            != bound_handoff.prior_snapshot_id
                            or binding.manifest.snapshot_kind.value
                            != bound_handoff.prior_snapshot_kind
                            or binding.receipt.jsonl_digest
                            != bound_handoff.prior_jsonl_digest
                            or hashlib.sha256(
                                contract_to_json(binding.manifest).encode("utf-8")
                            ).hexdigest()
                            != bound_handoff.prior_manifest_digest
                            or bound_handoff.new_manifest_digest
                            != hashlib.sha256(
                                contract_to_json(
                                    SnapshotManifest(
                                        manifest_version=SNAPSHOT_MANIFEST_VERSION,
                                        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                                        receipt=private_receipt,
                                        receipt_digest=snapshot_receipt_digest(
                                            private_receipt
                                        ),
                                    )
                                ).encode("utf-8")
                            ).hexdigest()
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.BOUND_REFRESH_PRIOR_MISMATCH"
                            )
                        connection.execute(
                            "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
                            (
                                snapshot_recovery_module
                                ._bound_refresh_handoff_meta_key(
                                    private_receipt.snapshot_id
                                ),
                                snapshot_recovery_module
                                ._bound_refresh_handoff_meta_value(
                                    bound_handoff
                                ),
                            ),
                        )
                    if jsonl_temp_identity is not None:
                        assert manifest_temp_identity is not None
                        parent_descriptor = _artifact_parent_dirfd(
                            identity.configured_jsonl_path,
                            artifact_parent_identity,
                        )
                        try:
                            _after_artifact_parent_dirfd_bound(
                                identity.configured_jsonl_path,
                                artifact_parent_identity,
                            )
                            artifact_paths = (
                                snapshot_recovery_module
                                ._recovery_artifact_paths(
                                    identity.configured_jsonl_path
                                )
                            )
                            _artifact_handoff_dirfd_entry(
                                artifact_paths.jsonl_temp.name,
                                parent_descriptor,
                                jsonl_temp_identity,
                                "STORE.HANDOFF_TEMP_UNPROVEN",
                            )
                            _artifact_handoff_dirfd_entry(
                                artifact_paths.manifest_temp.name,
                                parent_descriptor,
                                manifest_temp_identity,
                                "STORE.HANDOFF_TEMP_UNPROVEN",
                            )
                            connection.execute(
                                "INSERT INTO tm_meta(key, value) "
                                "VALUES (?, ?)",
                                (
                                    _artifact_handoff_meta_key(
                                        private_receipt.snapshot_id
                                    ),
                                    _artifact_handoff_meta_value(
                                        jsonl_temp_identity=(
                                            jsonl_temp_identity
                                        ),
                                        manifest_temp_identity=(
                                            manifest_temp_identity
                                        ),
                                        artifact_parent_identity=(
                                            artifact_parent_identity
                                        ),
                                        prior_jsonl_identity=(
                                            prior_jsonl_identity
                                        ),
                                        prior_jsonl_digest=(
                                            prior_jsonl_digest
                                        ),
                                        prior_jsonl_absent=(
                                            prior_jsonl_absent
                                        ),
                                        prior_manifest_identity=(
                                            prior_manifest_identity
                                        ),
                                        prior_manifest_digest=(
                                            prior_manifest_digest
                                        ),
                                        prior_manifest_absent=(
                                            prior_manifest_absent
                                        ),
                                    ),
                                ),
                            )
                        finally:
                            os.close(parent_descriptor)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def record_export_recovery_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_recovery_identity: tuple[int, int] | None = None,
        manifest_recovery_identity: tuple[int, int] | None = None,
    ) -> None:
        """Write-once record of the owned recovery-copy identities.

        Task 5.14 durable ownership seam: after the exclusive recovery
        copies of the prior pair are prepared, their exact identities
        are durably recorded in the receipt's handoff row before any
        publication replace, so crash recovery can prove ownership of
        the copies before any destructive cleanup.  The row must already
        exist (the registration recorded the temporary identities) and
        its recovery slots must still be empty; a second record for the
        same receipt fails closed.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        _require_artifact_identity_pair(
            jsonl_recovery_identity,
            "jsonl_recovery_identity",
        )
        _require_artifact_identity_pair(
            manifest_recovery_identity,
            "manifest_recovery_identity",
        )
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT status, destination_jsonl_path "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if str(row[0]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    destination = Path(str(row[1]))
                    meta_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    if meta_row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_MISSING"
                        )
                    prior = _artifact_handoff_from_meta(
                        _artifact_handoff_meta_key(snapshot_id),
                        str(meta_row[0]),
                    )
                    if (
                        prior is None
                        or prior.jsonl_recovery_identity is not None
                        or prior.manifest_recovery_identity is not None
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_ALREADY_RECORDED"
                        )
                    if (
                        jsonl_recovery_identity is not None
                        or manifest_recovery_identity is not None
                    ):
                        parent_descriptor = _artifact_parent_dirfd(
                            destination,
                            prior.artifact_parent_identity,
                        )
                        try:
                            _after_artifact_parent_dirfd_bound(
                                destination,
                                prior.artifact_parent_identity,
                            )
                            artifact_paths = (
                                snapshot_recovery_module
                                ._recovery_artifact_paths(destination)
                            )
                            if jsonl_recovery_identity is not None:
                                _artifact_handoff_dirfd_entry(
                                    artifact_paths.jsonl_recovery.name,
                                    parent_descriptor,
                                    jsonl_recovery_identity,
                                    "STORE.HANDOFF_RECOVERY_UNPROVEN",
                                )
                            if manifest_recovery_identity is not None:
                                _artifact_handoff_dirfd_entry(
                                    artifact_paths.manifest_recovery.name,
                                    parent_descriptor,
                                    manifest_recovery_identity,
                                    "STORE.HANDOFF_RECOVERY_UNPROVEN",
                                )
                        finally:
                            os.close(parent_descriptor)
                    connection.execute(
                        "UPDATE tm_meta SET value = ? WHERE key = ?",
                        (
                            _artifact_handoff_meta_value(
                                jsonl_temp_identity=(
                                    prior.jsonl_temp_identity
                                ),
                                manifest_temp_identity=(
                                    prior.manifest_temp_identity
                                ),
                                artifact_parent_identity=(
                                    prior.artifact_parent_identity
                                ),
                                jsonl_recovery_identity=(
                                    jsonl_recovery_identity
                                ),
                                manifest_recovery_identity=(
                                    manifest_recovery_identity
                                ),
                                prior_jsonl_identity=(
                                    prior.prior_jsonl_identity
                                ),
                                prior_jsonl_digest=prior.prior_jsonl_digest,
                                prior_jsonl_absent=prior.prior_jsonl_absent,
                                prior_manifest_identity=(
                                    prior.prior_manifest_identity
                                ),
                                prior_manifest_digest=(
                                    prior.prior_manifest_digest
                                ),
                                prior_manifest_absent=(
                                    prior.prior_manifest_absent
                                ),
                            ),
                            _artifact_handoff_meta_key(snapshot_id),
                        )
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def complete_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
    ) -> None:
        """Atomically complete exactly one issued arbitrary-destination receipt.

        Task 5.14 identity closure: the exact captured destination
        identities are mandatory.  The transaction reconstructs the
        receipt and its deterministic manifest from the ledger row and
        strict-captures both destination paths (no-follow, regular,
        single-link, stable identity and digest) before marking the
        receipt completed, so a same-byte foreign inode swap between
        classification and completion fails closed without touching the
        foreign paths, and an absent or unprovable destination can never
        become completed.  The ledger digest, paths and revision
        ancestry must all still match exactly.  Existing
        completed/cancelled receipts and all other ledger history remain
        immutable; a receipt that is unknown, foreign, or no longer
        ``issued`` is rejected.  The receipt's durable artifact handoff
        journal is deliberately kept after the terminal commit: it is
        the cleanup-pending ownership proof and is released only by
        ``clear_issued_receipt_handoff`` after the owned deterministic
        artifacts are durably removed or proven absent.
        """

        _require_artifact_identity_pair(jsonl_identity, "jsonl_identity")
        _require_artifact_identity_pair(
            manifest_identity,
            "manifest_identity",
        )
        if jsonl_identity is None or manifest_identity is None:
            raise ValueError(
                "jsonl and manifest identities are required for completion"
            )
        self._complete_issued_export_receipt_strict(
            snapshot_id,
            expected_generation=expected_generation,
            jsonl_identity=jsonl_identity,
            manifest_identity=manifest_identity,
        )

    def complete_bound_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        destination_jsonl_path: Path,
        destination_manifest_path: Path,
        family_reprove: Callable[[SnapshotReceipt], None],
    ) -> None:
        """Complete an export while its rooted publication family is live.

        The platform-neutral caller retains both destination handles and the
        destination-root lock.  This owner transaction independently closes
        the receipt/revision ancestry, invokes the live family reproof while
        the row is still ``issued``, and only then commits ``completed``.
        It never persists a live platform file identity and never changes the
        active snapshot binding, generation, or divergence state.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        for path_value, field_name in (
            (destination_jsonl_path, "destination_jsonl_path"),
            (destination_manifest_path, "destination_manifest_path"),
        ):
            if type(path_value) is not _NATIVE_PATH_TYPE or not path_value.is_absolute():
                raise TypeError(f"{field_name} must be an absolute exact native Path")
        if destination_manifest_path != destination_jsonl_path.with_name(
            f"{destination_jsonl_path.name}.localcat-snapshot.json"
        ):
            raise ValueError("bound export manifest path is not deterministic")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path FROM tm_snapshot_receipt "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_UNKNOWN")
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_STALE")
                    receipt = SnapshotReceipt(
                        snapshot_id=snapshot_id,
                        resource_id=str(row[0]),
                        canonical_store_id=str(row[1]),
                        exported_revision=_row_int(row[3]),
                        jsonl_digest=str(row[4]),
                        record_count=_row_int(row[5]),
                        format_version=str(row[6]),
                    )
                    if (
                        str(row[7]) != Path.__str__(destination_jsonl_path)
                        or str(row[8]) != Path.__str__(destination_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_DESTINATION_MISMATCH"
                        )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    counts = _revision_record_counts(
                        connection,
                        head_revision=revision.head_revision,
                        record_count=revision.record_count,
                    )
                    if counts.get(receipt.exported_revision) != receipt.record_count:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_ANCESTRY_INVALID"
                        )
                    family_reprove(receipt)
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'completed' "
                        "WHERE snapshot_id = ? AND status = 'issued'",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def complete_bound_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        family_reprove: Callable[[SnapshotReceipt], None],
    ) -> None:
        """Atomically complete and adopt one live rooted refresh family.

        The caller retains the configured JSONL/manifest handles and the W1
        resource reservation.  While this write transaction still owns an
        ``issued`` row, the callback re-proves that live family against the
        reconstructed receipt.  Completion and the singleton binding adoption
        then commit together.  No POSIX file capture or durable pathname
        handoff participates in this Windows owner seam.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, exported_revision, "
                        "jsonl_digest, record_count, format_version "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    if (
                        str(row[3])
                        != Path.__str__(identity.configured_jsonl_path)
                        or str(row[4])
                        != Path.__str__(identity.snapshot_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PATH_MISMATCH"
                        )
                    receipt = SnapshotReceipt(
                        snapshot_id=snapshot_id,
                        resource_id=str(row[0]),
                        canonical_store_id=str(row[1]),
                        exported_revision=_row_int(row[5]),
                        jsonl_digest=str(row[6]),
                        record_count=_row_int(row[7]),
                        format_version=str(row[8]),
                    )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    if (
                        receipt.exported_revision != revision.head_revision
                        or receipt.record_count != revision.record_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_REVISION_STALE"
                        )
                    facts = _read_source_binding_facts_in_transaction(
                        connection,
                        lease,
                    )
                    if facts.divergence_latched:
                        raise SQLiteStoreSchemaError(
                            "STORE.DIVERGENCE_LATCHED"
                        )
                    if facts.diagnostic_codes:
                        raise SQLiteStoreSchemaError(
                            "STORE.SNAPSHOT_LEDGER_CORRUPT"
                        )
                    family_reprove(receipt)
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'completed' "
                        "WHERE snapshot_id = ? AND status = 'issued'",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_binding("
                        "binding_id, configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version) "
                        "VALUES (1, ?, ?, 'EXPLICIT_EXPORT', ?, ?) "
                        "ON CONFLICT(binding_id) DO UPDATE SET "
                        "configured_jsonl_path = excluded.configured_jsonl_path, "
                        "manifest_path = excluded.manifest_path, "
                        "snapshot_kind = excluded.snapshot_kind, "
                        "snapshot_id = excluded.snapshot_id, "
                        "binding_version = excluded.binding_version",
                        (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                            snapshot_id,
                            SNAPSHOT_BINDING_VERSION,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def complete_bound_recovered_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[SnapshotReceipt], None],
    ) -> None:
        """Complete one history-valid refresh through its portable handoff."""

        if type(snapshot_id) is not str or not snapshot_id:
            raise ValueError("snapshot id must be a non-empty string")
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    _require_bound_refresh_effect_snapshot(
                        connection,
                        lease,
                        expected_receipt=expected_receipt,
                        expected_handoff=expected_handoff,
                        expected_binding=expected_binding,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, destination_manifest_path, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version FROM tm_snapshot_receipt "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None or str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_STALE")
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                        or str(row[3])
                        != Path.__str__(identity.configured_jsonl_path)
                        or str(row[4])
                        != Path.__str__(identity.snapshot_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    receipt = SnapshotReceipt(
                        snapshot_id=snapshot_id,
                        resource_id=str(row[0]),
                        canonical_store_id=str(row[1]),
                        exported_revision=_row_int(row[5]),
                        jsonl_digest=str(row[6]),
                        record_count=_row_int(row[7]),
                        format_version=str(row[8]),
                    )
                    revision = _canonical_revision_from_transaction(
                        connection, lease
                    )
                    counts = _revision_record_counts(
                        connection,
                        head_revision=revision.head_revision,
                        record_count=revision.record_count,
                    )
                    if counts.get(receipt.exported_revision) != receipt.record_count:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_ANCESTRY_INVALID"
                        )
                    meta = _read_meta(connection)
                    if _meta_bool(meta, "divergence_latched"):
                        raise SQLiteStoreSchemaError("STORE.DIVERGENCE_LATCHED")
                    handoff_key = (
                        snapshot_recovery_module._bound_refresh_handoff_meta_key(
                            snapshot_id
                        )
                    )
                    handoff_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (handoff_key,),
                    ).fetchone()
                    handoff = (
                        None
                        if handoff_row is None
                        else snapshot_recovery_module
                        ._bound_refresh_handoff_from_meta(
                            handoff_key, str(handoff_row[0])
                        )
                    )
                    manifest = SnapshotManifest(
                        manifest_version=SNAPSHOT_MANIFEST_VERSION,
                        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                        receipt=receipt,
                        receipt_digest=snapshot_receipt_digest(receipt),
                    )
                    if (
                        handoff is None
                        or handoff.new_jsonl_digest != receipt.jsonl_digest
                        or handoff.new_manifest_digest
                        != hashlib.sha256(
                            contract_to_json(manifest).encode("utf-8")
                        ).hexdigest()
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.BOUND_REFRESH_HANDOFF_INVALID"
                        )
                    family_reprove(receipt)
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'completed' "
                        "WHERE snapshot_id = ? AND status = 'issued'",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_binding("
                        "binding_id, configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version) "
                        "VALUES (1, ?, ?, 'EXPLICIT_EXPORT', ?, ?) "
                        "ON CONFLICT(binding_id) DO UPDATE SET "
                        "configured_jsonl_path=excluded.configured_jsonl_path, "
                        "manifest_path=excluded.manifest_path, "
                        "snapshot_kind=excluded.snapshot_kind, "
                        "snapshot_id=excluded.snapshot_id, "
                        "binding_version=excluded.binding_version",
                        (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                            snapshot_id,
                            SNAPSHOT_BINDING_VERSION,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def cancel_bound_recovered_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> None:
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED", resource_id=identity.resource_id,
                    generation=lease.generation, retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection, resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    _require_bound_refresh_effect_snapshot(
                        connection,
                        lease,
                        expected_receipt=expected_receipt,
                        expected_handoff=expected_handoff,
                        expected_binding=expected_binding,
                    )
                    row = connection.execute(
                        "SELECT status, destination_jsonl_path, "
                        "destination_manifest_path FROM tm_snapshot_receipt "
                        "WHERE snapshot_id=?", (snapshot_id,),
                    ).fetchone()
                    if (
                        row is None or str(row[0]) != "issued"
                        or str(row[1]) != str(identity.configured_jsonl_path)
                        or str(row[2]) != str(identity.snapshot_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_STALE")
                    key = snapshot_recovery_module._bound_refresh_handoff_meta_key(
                        snapshot_id
                    )
                    meta_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key=?", (key,)
                    ).fetchone()
                    if meta_row is None or (
                        snapshot_recovery_module._bound_refresh_handoff_from_meta(
                            key, str(meta_row[0])
                        ) is None
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.BOUND_REFRESH_HANDOFF_INVALID"
                        )
                    family_reprove()
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status='cancelled' "
                        "WHERE snapshot_id=? AND status='issued'", (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def clear_bound_refresh_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> None:
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=lease.stage.resource_identity.resource_id,
                    generation=lease.generation, retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _require_bound_refresh_effect_snapshot(
                        connection,
                        lease,
                        expected_receipt=expected_receipt,
                        expected_handoff=expected_handoff,
                        expected_binding=expected_binding,
                    )
                    row = connection.execute(
                        "SELECT status FROM tm_snapshot_receipt WHERE snapshot_id=?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None or str(row[0]) not in {"completed", "cancelled"}:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_RECEIPT_NOT_TERMINAL"
                        )
                    family_reprove()
                    deleted = connection.execute(
                        "DELETE FROM tm_meta WHERE key=?",
                        (snapshot_recovery_module._bound_refresh_handoff_meta_key(snapshot_id),),
                    )
                    if deleted.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.BOUND_REFRESH_HANDOFF_MISSING"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def latch_bound_refresh_divergence(
        self,
        *,
        expected_fingerprint: str,
        expected_generation: int,
        expected_receipt: snapshot_recovery_module.IssuedReceiptFacts,
        expected_handoff: snapshot_recovery_module._BoundRefreshHandoffFacts,
        expected_binding: SnapshotBinding,
        family_reprove: Callable[[], None],
    ) -> bool:
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=lease.stage.resource_identity.resource_id,
                    generation=lease.generation, retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    facts = _require_bound_refresh_effect_snapshot(
                        connection,
                        lease,
                        expected_receipt=expected_receipt,
                        expected_handoff=expected_handoff,
                        expected_binding=expected_binding,
                    )
                    if facts.canonical_fingerprint != expected_fingerprint:
                        connection.rollback()
                        return False
                    family_reprove()
                    if not facts.divergence_latched:
                        updated = connection.execute(
                            "UPDATE tm_meta SET value='1' "
                            "WHERE key='divergence_latched'"
                        )
                        if updated.rowcount != 1:
                            raise SQLiteStoreSchemaError(
                                "STORE.DIVERGENCE_LATCH_MISSING"
                            )
                    connection.commit()
                    return True
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

    def _complete_issued_export_receipt_strict(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
    ) -> None:
        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, exported_revision, "
                        "jsonl_digest, record_count, format_version "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    meta_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    if meta_row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_MISSING"
                        )
                    handoff = _artifact_handoff_from_meta(
                        _artifact_handoff_meta_key(snapshot_id),
                        str(meta_row[0]),
                    )
                    if (
                        handoff is None
                        or handoff.jsonl_temp_identity is None
                        or handoff.manifest_temp_identity is None
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_INVALID"
                        )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    try:
                        receipt = SnapshotReceipt(
                            snapshot_id=snapshot_id,
                            resource_id=str(row[0]),
                            canonical_store_id=str(row[1]),
                            exported_revision=_row_int(row[5]),
                            jsonl_digest=str(row[6]),
                            record_count=_row_int(row[7]),
                            format_version=str(row[8]),
                        )
                    except (TypeError, ValueError):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PAIR_INVALID"
                        ) from None
                    counts = _revision_record_counts(
                        connection,
                        head_revision=revision.head_revision,
                        record_count=revision.record_count,
                    )
                    expected_count = counts.get(receipt.exported_revision)
                    if (
                        expected_count is None
                        or receipt.record_count != expected_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_ANCESTRY_INVALID"
                        )
                    try:
                        jsonl_capture = _capture_activation_file(
                            Path(str(row[3])),
                            asset_kind="EXPORT_DESTINATION",
                        )
                        manifest_capture = _capture_activation_file(
                            Path(str(row[4])),
                            asset_kind="EXPORT_MANIFEST",
                        )
                    except ActivationPreparationError as error:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PAIR_INVALID"
                        ) from error
                    manifest = SnapshotManifest(
                        manifest_version=SNAPSHOT_MANIFEST_VERSION,
                        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                        receipt=receipt,
                        receipt_digest=snapshot_receipt_digest(receipt),
                    )
                    expected_manifest_digest = hashlib.sha256(
                        contract_to_json(manifest).encode("utf-8")
                    ).hexdigest()
                    if (
                        jsonl_capture.digest != receipt.jsonl_digest
                        or manifest_capture.digest
                        != expected_manifest_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PAIR_INVALID"
                        )
                    if (
                        jsonl_capture.identity.device != jsonl_identity[0]
                        or jsonl_capture.identity.inode != jsonl_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PAIR_INVALID"
                        )
                    if (
                        manifest_capture.identity.device
                        != manifest_identity[0]
                        or manifest_capture.identity.inode
                        != manifest_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PAIR_INVALID"
                        )
                    if (
                        jsonl_capture.identity.device
                        != handoff.jsonl_temp_identity[0]
                        or jsonl_capture.identity.inode
                        != handoff.jsonl_temp_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_IDENTITY_MISMATCH"
                        )
                    if (
                        manifest_capture.identity.device
                        != handoff.manifest_temp_identity[0]
                        or manifest_capture.identity.inode
                        != handoff.manifest_temp_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_IDENTITY_MISMATCH"
                        )
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'completed' "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def cancel_issued_export_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        """Mark exactly one issued arbitrary-destination receipt cancelled.

        Cancellation is the only issued->cancelled transition; it never
        completes an absent pair and never rewrites completed history.
        The receipt's durable artifact handoff journal is deliberately
        kept after the terminal commit: it is the cleanup-pending
        ownership proof and is released only by
        ``clear_issued_receipt_handoff`` after the owned deterministic
        artifacts are durably removed or proven absent.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'cancelled' "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def clear_issued_receipt_handoff(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        """Release one terminal receipt's cleanup-pending handoff journal.

        Task 5.14/Cluster F durable cleanup seam: the journal row is
        deleted only after the caller has durably removed (or proven
        absent) every deterministic temp/recovery artifact of this exact
        receipt.  The transaction re-proves the receipt is terminal
        (completed or cancelled), re-reads the journal, independently
        re-proves the exact immediate-parent device/inode recorded at
        registration against the current real non-symlink writable/
        executable parent chain, and requires every one of the four
        deterministic artifact paths to be provably absent before the
        release: a missing legacy parent identity, an unsafe or
        replaced parent chain, or any entry -- the owned inode, a
        foreign regular inode with the same or different bytes, a
        symlink, a hardlink, a directory, or an unreadable/unsafe entry
        -- keeps the journal and fails with a stable store code.  A
        parent rename/replacement between the recovery blocker, the
        cleanup, the fsync and this release therefore never clears the
        handoff and never reports COMPLETED.  The blocking entry is
        never deleted and the release never proceeds on a partial or
        unprovable absence, so an occupied deterministic path can never
        release the cleanup journal while the next refresh/export stays
        blocked.  Cleanup authority is receipt-scoped and can never be
        transferred across snapshot ids.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) not in {"completed", "cancelled"}:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_RECEIPT_NOT_TERMINAL"
                        )
                    meta_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    if meta_row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_MISSING"
                        )
                    handoff = _artifact_handoff_from_meta(
                        _artifact_handoff_meta_key(snapshot_id),
                        str(meta_row[0]),
                    )
                    if handoff is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_INVALID"
                        )
                    destination = Path(str(row[3]))
                    if handoff.artifact_parent_identity is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_PARENT_IDENTITY_MISSING"
                        )
                    parent_descriptor = _artifact_parent_dirfd(
                        destination,
                        handoff.artifact_parent_identity,
                    )
                    try:
                        _after_artifact_parent_dirfd_bound(
                            destination,
                            handoff.artifact_parent_identity,
                        )
                        try:
                            os.fsync(parent_descriptor)
                        except OSError:
                            raise SQLiteStoreSchemaError(
                                "STORE.HANDOFF_PARENT_UNSAFE"
                            ) from None
                        artifact_paths = (
                            snapshot_recovery_module
                            ._recovery_artifact_paths(destination)
                        )
                        for name in (
                            artifact_paths.jsonl_temp.name,
                            artifact_paths.manifest_temp.name,
                            artifact_paths.jsonl_recovery.name,
                            artifact_paths.manifest_recovery.name,
                        ):
                            if not _artifact_handoff_dirfd_absent(
                                name,
                                parent_descriptor,
                            ):
                                raise SQLiteStoreSchemaError(
                                    "STORE.HANDOFF_CLEANUP_PENDING"
                                )
                        _artifact_handoff_dirfd_reprove(
                            destination,
                            handoff.artifact_parent_identity,
                        )
                        connection.execute(
                            "DELETE FROM tm_meta WHERE key = ?",
                            (_artifact_handoff_meta_key(snapshot_id),),
                        )
                        connection.commit()
                    finally:
                        os.close(parent_descriptor)
                except Exception:
                    connection.rollback()
                    raise

    def complete_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        jsonl_identity: tuple[int, int] | None = None,
        manifest_identity: tuple[int, int] | None = None,
        _allow_history_revision: bool = False,
    ) -> None:
        """Atomically complete one issued refresh receipt and rebind.

        Task 5.13 final transaction: one issued configured-path receipt
        is marked completed and the single snapshot binding is pointed
        at that same completed receipt/manifest in one transaction.
        The published pair must still pass the strict no-follow,
        regular, single-link, stable-identity capture, and when the
        exact created/published identities are supplied they must match
        that capture so a same-byte foreign inode swap fails closed.
        Canonical records, head revision, generation and the divergence
        latch are never modified; a latched divergence fails this
        transaction closed.

        ``_allow_history_revision`` is the Task 5.14 recovery seam:
        recovery may complete an issued receipt whose exported revision
        is an older proven completed ancestry revision with the exact
        record count (the result binding is ``VERIFIED_HISTORY``);
        ordinary Task 5.13 callers keep the strict current-revision
        requirement.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        for identity_value, field_name in (
            (jsonl_identity, "jsonl_identity"),
            (manifest_identity, "manifest_identity"),
        ):
            if identity_value is not None and (
                type(identity_value) is not tuple
                or len(identity_value) != 2
                or type(identity_value[0]) is not int
                or type(identity_value[1]) is not int
            ):
                raise ValueError(f"{field_name} is invalid")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, exported_revision, "
                        "jsonl_digest, record_count, format_version "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    if (
                        str(row[3])
                        != Path.__str__(identity.configured_jsonl_path)
                        or str(row[4])
                        != Path.__str__(identity.snapshot_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PATH_MISMATCH"
                        )
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    try:
                        receipt = SnapshotReceipt(
                            snapshot_id=snapshot_id,
                            resource_id=str(row[0]),
                            canonical_store_id=str(row[1]),
                            exported_revision=_row_int(row[5]),
                            jsonl_digest=str(row[6]),
                            record_count=_row_int(row[7]),
                            format_version=str(row[8]),
                        )
                        manifest = SnapshotManifest(
                            manifest_version=SNAPSHOT_MANIFEST_VERSION,
                            snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                            receipt=receipt,
                            receipt_digest=snapshot_receipt_digest(receipt),
                        )
                    except (
                        TypeError,
                        ValueError,
                    ) as error:
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_PAIR_INVALID"
                        ) from error
                    if _allow_history_revision:
                        counts = _revision_record_counts(
                            connection,
                            head_revision=revision.head_revision,
                            record_count=revision.record_count,
                        )
                        expected_count = counts.get(
                            receipt.exported_revision
                        )
                        if (
                            expected_count is None
                            or receipt.record_count != expected_count
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.RECEIPT_ANCESTRY_INVALID"
                            )
                    elif (
                        receipt.exported_revision != revision.head_revision
                        or receipt.record_count != revision.record_count
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_REVISION_STALE"
                        )
                    meta_row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    if meta_row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_MISSING"
                        )
                    handoff = _artifact_handoff_from_meta(
                        _artifact_handoff_meta_key(snapshot_id),
                        str(meta_row[0]),
                    )
                    if (
                        handoff is None
                        or handoff.jsonl_temp_identity is None
                        or handoff.manifest_temp_identity is None
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_INVALID"
                        )
                    try:
                        jsonl_capture = _capture_activation_file(
                            identity.configured_jsonl_path,
                            asset_kind="CONFIGURED_JSONL",
                        )
                        manifest_capture = _capture_activation_file(
                            identity.snapshot_manifest_path,
                            asset_kind="SNAPSHOT_MANIFEST",
                        )
                    except ActivationPreparationError as error:
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_PAIR_INVALID"
                        ) from error
                    expected_manifest_digest = hashlib.sha256(
                        contract_to_json(manifest).encode("utf-8")
                    ).hexdigest()
                    if (
                        jsonl_capture.digest != receipt.jsonl_digest
                        or manifest_capture.digest
                        != expected_manifest_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_PAIR_INVALID"
                        )
                    if jsonl_identity is not None and (
                        jsonl_capture.identity.device != jsonl_identity[0]
                        or jsonl_capture.identity.inode != jsonl_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_PAIR_INVALID"
                        )
                    if manifest_identity is not None and (
                        manifest_capture.identity.device
                        != manifest_identity[0]
                        or manifest_capture.identity.inode
                        != manifest_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.REFRESH_PAIR_INVALID"
                        )
                    if (
                        jsonl_capture.identity.device
                        != handoff.jsonl_temp_identity[0]
                        or jsonl_capture.identity.inode
                        != handoff.jsonl_temp_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_IDENTITY_MISMATCH"
                        )
                    if (
                        manifest_capture.identity.device
                        != handoff.manifest_temp_identity[0]
                        or manifest_capture.identity.inode
                        != handoff.manifest_temp_identity[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.HANDOFF_IDENTITY_MISMATCH"
                        )
                    meta = _read_meta(connection)
                    if _meta_bool(meta, "divergence_latched"):
                        raise SQLiteStoreSchemaError(
                            "STORE.DIVERGENCE_LATCHED"
                        )
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'completed' "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.execute(
                        "INSERT INTO tm_snapshot_binding("
                        "binding_id, configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version) "
                        "VALUES (1, ?, ?, 'EXPLICIT_EXPORT', ?, ?) "
                        "ON CONFLICT(binding_id) DO UPDATE SET "
                        "configured_jsonl_path = "
                        "excluded.configured_jsonl_path, "
                        "manifest_path = excluded.manifest_path, "
                        "snapshot_kind = excluded.snapshot_kind, "
                        "snapshot_id = excluded.snapshot_id, "
                        "binding_version = excluded.binding_version",
                        (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                            snapshot_id,
                            SNAPSHOT_BINDING_VERSION,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def cancel_issued_refresh_receipt(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
    ) -> None:
        """Atomically cancel exactly one issued configured-path receipt.

        Task 5.14 recovery seam: the receipt must be an issued refresh
        receipt of this resource whose stored destination paths are the
        configured JSONL and its deterministic adjacent manifest.  Only
        the receipt status is changed; the old completed binding, the
        pair, canonical records, head revision, generation and the
        divergence latch are never modified.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_UNKNOWN"
                        )
                    if (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_IDENTITY_MISMATCH"
                        )
                    if str(row[2]) != "issued":
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_STALE"
                        )
                    if (
                        str(row[3])
                        != Path.__str__(identity.configured_jsonl_path)
                        or str(row[4])
                        != Path.__str__(identity.snapshot_manifest_path)
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_PATH_MISMATCH"
                        )
                    updated = connection.execute(
                        "UPDATE tm_snapshot_receipt SET status = 'cancelled' "
                        "WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    if updated.rowcount != 1:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECEIPT_TRANSITION_FAILED"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def latch_refresh_divergence(self, *, expected_fingerprint: str) -> bool:
        """Durably latch ``SOURCE_DIVERGED`` under one fingerprint proof.

        Task 5.14 recovery seam: re-reads the canonical facts in one
        transaction and latches divergence only when the canonical
        fingerprint is still the one recovery classified against, so a
        concurrent canonical write between classification and latch
        restarts the classification instead of latching stale facts.
        """

        if type(expected_fingerprint) is not str:
            raise TypeError("expected_fingerprint must be a built-in string")
        if len(expected_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in expected_fingerprint
        ):
            raise ValueError("expected_fingerprint is invalid")
        with self._coordinator._operation_lease() as lease:
            return _latch_source_divergence(
                lease,
                expected_fingerprint=expected_fingerprint,
            )

    def _read_refresh_recovery_facts(
        self,
    ) -> snapshot_recovery_module._RefreshRecoveryFacts:
        """Read ledger/binding facts in one SQLite snapshot (Task 5.14).

        One operation lease and one read transaction capture the
        canonical identity, head revision, proven ancestry record
        counts, divergence latch, the completed binding (with its
        deterministically reconstructed manifest), every ledger receipt
        row and the canonical fingerprint used by the divergence-latch
        race proof.
        """

        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    facts = _read_source_binding_facts_in_transaction(
                        connection,
                        lease,
                    )
                    rows = connection.execute(
                        "SELECT snapshot_id, resource_id, "
                        "canonical_store_id, exported_revision, "
                        "jsonl_digest, record_count, format_version, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, status "
                        "FROM tm_snapshot_receipt ORDER BY snapshot_id"
                    ).fetchall()
                    handoff_rows = connection.execute(
                        "SELECT key, value FROM tm_meta "
                        "WHERE key LIKE ? ORDER BY key",
                        (f"{_ARTIFACT_HANDOFF_META_PREFIX}%",),
                    ).fetchall()
                    bound_handoff_rows = connection.execute(
                        "SELECT key, value FROM tm_meta "
                        "WHERE key LIKE ? ORDER BY key",
                        (
                            f"{snapshot_recovery_module._BOUND_REFRESH_HANDOFF_META_PREFIX}%",
                        ),
                    ).fetchall()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        receipts = tuple(
            snapshot_recovery_module.IssuedReceiptFacts(
                snapshot_id=str(row[0]),
                resource_id=str(row[1]),
                canonical_store_id=str(row[2]),
                exported_revision=_row_int(row[3]),
                jsonl_digest=str(row[4]),
                record_count=_row_int(row[5]),
                format_version=str(row[6]),
                destination_jsonl_path=Path(str(row[7])),
                destination_manifest_path=Path(str(row[8])),
                status=str(row[9]),
            )
            for row in rows
        )
        parsed_handoffs = tuple(
            _artifact_handoff_from_meta(
                str(row[0]),
                str(row[1]),
            )
            for row in handoff_rows
        )
        if any(handoff is None for handoff in parsed_handoffs):
            raise SQLiteStoreSchemaError("STORE.HANDOFF_CORRUPT")
        handoffs = tuple(
            handoff
            for handoff in parsed_handoffs
            if handoff is not None
        )
        parsed_bound_handoffs = tuple(
            snapshot_recovery_module._bound_refresh_handoff_from_meta(
                str(row[0]), str(row[1])
            )
            for row in bound_handoff_rows
        )
        if any(handoff is None for handoff in parsed_bound_handoffs):
            raise SQLiteStoreSchemaError("STORE.BOUND_REFRESH_HANDOFF_CORRUPT")
        bound_handoffs = tuple(
            handoff
            for handoff in parsed_bound_handoffs
            if handoff is not None
        )
        receipt_ids = {receipt.snapshot_id for receipt in receipts}
        if any(
            handoff.snapshot_id not in receipt_ids
            for handoff in handoffs
        ):
            raise SQLiteStoreSchemaError("STORE.HANDOFF_ORPHANED")
        if any(
            handoff.snapshot_id not in receipt_ids
            for handoff in bound_handoffs
        ):
            raise SQLiteStoreSchemaError(
                "STORE.BOUND_REFRESH_HANDOFF_ORPHANED"
            )
        binding_invalid = bool(
            set(facts.diagnostic_codes)
            & {
                "SOURCE_BINDING.LEDGER_INVALID",
                "SOURCE_BINDING.LEDGER_NOT_COMPLETED",
                "SOURCE_BINDING.LEDGER_PATH_MISMATCH",
            }
        )
        if facts.binding is not None:
            try:
                _validate_binding_identity(
                    facts.binding,
                    identity=identity,
                    canonical_store_id=lease.canonical_store_id,
                )
            except (TypeError, ValueError):
                binding_invalid = True
        journal_path = _activation_journal_path(identity)
        marker_path = _activation_lineage_marker_path(identity)
        terminal_path = _activation_terminal_path(identity)
        configured_artifacts = (
            snapshot_recovery_module._recovery_artifact_paths(
                identity.configured_jsonl_path
            )
        )
        authority_paths = frozenset(
            {
                identity.configured_jsonl_path,
                identity.snapshot_manifest_path,
                identity.canonical_sidecar_path,
                journal_path,
                _activation_journal_temp_path(journal_path),
                marker_path,
                _activation_lineage_marker_temp_path(marker_path),
                terminal_path,
                _activation_terminal_temp_path(terminal_path),
                configured_artifacts.jsonl_temp,
                configured_artifacts.manifest_temp,
                configured_artifacts.jsonl_recovery,
                configured_artifacts.manifest_recovery,
            }
        )
        return snapshot_recovery_module._RefreshRecoveryFacts(
            resource_id=identity.resource_id,
            canonical_store_id=lease.canonical_store_id,
            generation=lease.generation,
            configured_jsonl_path=identity.configured_jsonl_path,
            snapshot_manifest_path=identity.snapshot_manifest_path,
            head_revision=facts.head_revision,
            record_count=facts.record_count,
            cumulative_record_counts=facts.cumulative_record_counts,
            divergence_latched=facts.divergence_latched,
            binding=facts.binding,
            binding_invalid=binding_invalid,
            receipts=receipts,
            canonical_fingerprint=facts.canonical_fingerprint,
            handoffs=handoffs,
            authority_paths=authority_paths,
            canonical_sidecar_path=identity.canonical_sidecar_path,
            target_identity_fragment=identity.target_identity[:16],
            bound_refresh_handoffs=bound_handoffs,
        )

    def probe_bound_issued_refresh_receipt_completed(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        family_reprove: Callable[[SnapshotReceipt], None],
    ) -> ReceiptCompletionProbe:
        """Probe a Windows refresh completion through its retained handles.

        A durable ``COMMITTED`` result requires the completed receipt, the
        singleton binding adoption, the unchanged current canonical revision,
        an unlatched source, and the caller's live rooted family reproof.  This
        deliberately avoids ``_capture_activation_file`` and the POSIX
        artifact-handoff journal.  A committed ledger whose owner facts do not
        close is reported as ``COMMITTED_UNCLEAN`` and must never be restored or
        cancelled by the publisher.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        if not callable(family_reprove):
            raise TypeError("family_reprove must be callable")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, exported_revision, "
                        "jsonl_digest, record_count, format_version "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None or (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                        or str(row[2]) != "completed"
                    ):
                        connection.commit()
                        return ReceiptCompletionProbe.NOT_COMMITTED
                    bound = connection.execute(
                        "SELECT configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version "
                        "FROM tm_snapshot_binding WHERE binding_id = 1"
                    ).fetchone()
                    revision = _canonical_revision_from_transaction(
                        connection,
                        lease,
                    )
                    meta = _read_meta(connection)
                    pending_handoff = connection.execute(
                        "SELECT 1 FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    try:
                        receipt = SnapshotReceipt(
                            snapshot_id=snapshot_id,
                            resource_id=str(row[0]),
                            canonical_store_id=str(row[1]),
                            exported_revision=_row_int(row[5]),
                            jsonl_digest=str(row[6]),
                            record_count=_row_int(row[7]),
                            format_version=str(row[8]),
                        )
                    except (TypeError, ValueError):
                        connection.commit()
                        return ReceiptCompletionProbe.COMMITTED_UNCLEAN
                    if (
                        str(row[3])
                        != Path.__str__(identity.configured_jsonl_path)
                        or str(row[4])
                        != Path.__str__(identity.snapshot_manifest_path)
                        or receipt.exported_revision != revision.head_revision
                        or receipt.record_count != revision.record_count
                        or _meta_bool(meta, "divergence_latched")
                        or pending_handoff is not None
                        or bound
                        != (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                            SnapshotKind.EXPLICIT_EXPORT.value,
                            snapshot_id,
                            SNAPSHOT_BINDING_VERSION,
                        )
                    ):
                        connection.commit()
                        return ReceiptCompletionProbe.COMMITTED_UNCLEAN
                    family_reprove(receipt)
                    connection.commit()
                    return ReceiptCompletionProbe.COMMITTED
                except Exception:
                    connection.rollback()
                    raise

    def probe_issued_receipt_completed(
        self,
        snapshot_id: str,
        *,
        expected_generation: int,
        require_bound: bool,
    ) -> ReceiptCompletionProbe:
        """Probe one ambiguous post-commit exception window durably.

        Task 5.14: when the Task 5.13 completion call raised an outward
        exception the commit may already have happened, so before any
        restore/cancel the durable ledger, binding and published pair
        are inspected in one snapshot.  ``COMMITTED`` requires the
        receipt completed (and, for ``require_bound``, the single
        binding pointing at it) plus a strict digest proof of both
        published files and a released cleanup-pending handoff journal;
        ``COMMITTED_UNCLEAN`` means the ledger committed but the
        binding/pair is not cleanly provable or the receipt's artifact
        handoff journal is still pending durable cleanup (the caller
        must never restore or cancel and must never report success);
        ``NOT_COMMITTED`` keeps the ordinary known-before-commit
        restore/cancel behavior.
        """

        if type(snapshot_id) is not str or not snapshot_id.strip():
            raise ValueError("snapshot id must be a non-empty string")
        if (
            type(expected_generation) is not int
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise ValueError("expected_generation is invalid")
        if type(require_bound) is not bool:
            raise TypeError("require_bound must be a built-in bool")
        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            if lease.generation != expected_generation:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=identity.resource_id,
                    generation=lease.generation,
                    retryable=True,
                )
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    row = connection.execute(
                        "SELECT resource_id, canonical_store_id, status, "
                        "destination_jsonl_path, "
                        "destination_manifest_path, exported_revision, "
                        "jsonl_digest, record_count, format_version "
                        "FROM tm_snapshot_receipt WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if row is None or (
                        str(row[0]) != identity.resource_id
                        or str(row[1]) != lease.canonical_store_id
                    ):
                        connection.commit()
                        return ReceiptCompletionProbe.NOT_COMMITTED
                    if str(row[2]) != "completed":
                        connection.commit()
                        return ReceiptCompletionProbe.NOT_COMMITTED
                    pending_handoff = connection.execute(
                        "SELECT 1 FROM tm_meta WHERE key = ?",
                        (_artifact_handoff_meta_key(snapshot_id),),
                    ).fetchone()
                    if pending_handoff is not None:
                        connection.commit()
                        return ReceiptCompletionProbe.COMMITTED_UNCLEAN
                    if require_bound:
                        bound = connection.execute(
                            "SELECT snapshot_id FROM tm_snapshot_binding "
                            "WHERE binding_id = 1"
                        ).fetchone()
                        if (
                            bound is None
                            or str(bound[0]) != snapshot_id
                        ):
                            connection.commit()
                            return (
                                ReceiptCompletionProbe.COMMITTED_UNCLEAN
                            )
                    try:
                        receipt = SnapshotReceipt(
                            snapshot_id=snapshot_id,
                            resource_id=str(row[0]),
                            canonical_store_id=str(row[1]),
                            exported_revision=_row_int(row[5]),
                            jsonl_digest=str(row[6]),
                            record_count=_row_int(row[7]),
                            format_version=str(row[8]),
                        )
                        manifest = SnapshotManifest(
                            manifest_version=SNAPSHOT_MANIFEST_VERSION,
                            snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                            receipt=receipt,
                            receipt_digest=snapshot_receipt_digest(receipt),
                        )
                    except (TypeError, ValueError):
                        connection.commit()
                        return ReceiptCompletionProbe.COMMITTED_UNCLEAN
                    expected_manifest_digest = hashlib.sha256(
                        contract_to_json(manifest).encode("utf-8")
                    ).hexdigest()
                    jsonl_path = Path(str(row[3]))
                    manifest_path = Path(str(row[4]))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        try:
            jsonl_capture = _capture_activation_file(
                jsonl_path,
                asset_kind="RECOVERY_PROBE_JSONL",
            )
            manifest_capture = _capture_activation_file(
                manifest_path,
                asset_kind="RECOVERY_PROBE_MANIFEST",
            )
        except ActivationPreparationError:
            return ReceiptCompletionProbe.COMMITTED_UNCLEAN
        if (
            jsonl_capture.digest != receipt.jsonl_digest
            or manifest_capture.digest != expected_manifest_digest
        ):
            return ReceiptCompletionProbe.COMMITTED_UNCLEAN
        return ReceiptCompletionProbe.COMMITTED

    def recover_configured_refresh(
        self,
    ) -> snapshot_recovery_module.RefreshRecoveryOutcome:
        """Close every issued snapshot publication crash window.

        Task 5.14 explicit store seam: runs the recovery classification
        and effects under the resource-scoped reentrant refresh/
        observation gate so refresh, recovery and monitor observations
        can never interleave on the configured pair.  Idempotent; a
        pre-existing divergence latch is never cleared.
        """

        with self._coordinator._refresh_observation_gate(
            require_ready_resource=True,
        ):
            return snapshot_recovery_module.recover_snapshot_publication(
                _SnapshotRecoveryPort(self)
            )

    def recover_bound_configured_refresh(
        self,
        family: snapshot_recovery_module._BoundConfiguredRefreshFamilyPort,
    ) -> snapshot_recovery_module.RefreshRecoveryOutcome:
        """Run portable configured recovery under the shared refresh gate."""

        with self._coordinator._refresh_observation_gate(
            require_ready_resource=True,
        ):
            return snapshot_recovery_module.recover_bound_configured_snapshot_publication(
                _BoundSnapshotRecoveryPort(self), family
            )

    def require_bound_refresh_observation_ready(self) -> None:
        """Reject a Windows observation until its bound owner is terminal."""

        with self._coordinator._operation_lease() as lease:
            identity = lease.stage.resource_identity
            with _open_leased_connection(lease) as connection:
                connection.execute("BEGIN")
                try:
                    _validate_store_identity(
                        connection,
                        resource_id=identity.resource_id,
                        canonical_store_id=lease.canonical_store_id,
                        target_identity=identity.target_identity,
                    )
                    meta = _read_meta(connection)
                    divergence_latched = _meta_bool(
                        meta, "divergence_latched"
                    )
                    issued = connection.execute(
                        "SELECT 1 FROM tm_snapshot_receipt "
                        "WHERE status='issued' "
                        "AND destination_jsonl_path=? "
                        "AND destination_manifest_path=? LIMIT 1",
                        (
                            Path.__str__(identity.configured_jsonl_path),
                            Path.__str__(identity.snapshot_manifest_path),
                        ),
                    ).fetchone()
                    handoff = connection.execute(
                        "SELECT 1 FROM tm_meta WHERE key LIKE ? LIMIT 1",
                        (
                            f"{snapshot_recovery_module._BOUND_REFRESH_HANDOFF_META_PREFIX}%",
                        ),
                    ).fetchone()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        if not divergence_latched and (
            issued is not None or handoff is not None
        ):
            raise SQLiteStoreSchemaError(
                "STORE.REFRESH_RECOVERY_REQUIRED"
            )

    def register_completed_snapshot_binding(
        self,
        binding: SnapshotBinding,
    ) -> None:
        self._source_binding_monitor.register_completed_binding(binding)

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        _validate_exact_records_input(source_raw)
        with self._coordinator._operation_lease() as lease:
            return _exact_records_body(lease, source_raw)

    def append(self, draft: TMRecordDraft) -> TMRecord:
        if type(draft) is not TMRecordDraft:
            raise TypeError("draft must be TMRecordDraft")
        records = self.append_batch(
            batch_id=f"local_write.{uuid.uuid4().hex}",
            kind="local_write",
            drafts=(draft,),
        )
        return records[0]

    def append_batch(
        self,
        *,
        batch_id: str,
        kind: str,
        drafts: tuple[TMRecordDraft, ...],
        source_digest: str | None = None,
        source_path: Path | None = None,
        legacy_line_nos: tuple[int | None, ...] | None = None,
        invalid_count: int = 0,
        duplicate_source_count: int = 0,
        created_at: str | None = None,
        extension: Callable[
            [tuple[SQLiteCandidateRecord, ...]],
            SQLiteCandidateWritePlan,
        ]
        | None = None,
    ) -> tuple[TMRecord, ...]:
        """Append one ordered origin batch and its extension atomically."""

        (
            prepared_batch_id,
            prepared_kind,
            prepared_source_digest,
            prepared_source_path,
            timestamp,
            prepared_invalid_count,
            prepared_duplicate_source_count,
            prepared_drafts,
            candidate_records,
        ) = _prepare_append_batch_inputs(
            batch_id=batch_id,
            kind=kind,
            drafts=drafts,
            source_digest=source_digest,
            source_path=source_path,
            legacy_line_nos=legacy_line_nos,
            invalid_count=invalid_count,
            duplicate_source_count=duplicate_source_count,
            created_at=created_at,
            extension=extension,
        )
        with self._coordinator._operation_lease() as lease:
            validated_candidate_plan = _prepare_candidate_write_plan(
                extension,
                candidate_records,
                batch_size=len(prepared_drafts),
                fts5_available=lease.fts5_available,
            )
            return self._append_prepared_batch(
                lease=lease,
                prepared_batch_id=prepared_batch_id,
                prepared_kind=prepared_kind,
                prepared_source_digest=prepared_source_digest,
                prepared_source_path=prepared_source_path,
                timestamp=timestamp,
                prepared_invalid_count=prepared_invalid_count,
                prepared_duplicate_source_count=(
                    prepared_duplicate_source_count
                ),
                prepared_drafts=prepared_drafts,
                validated_candidate_plan=validated_candidate_plan,
            )

    def _append_prepared_batch(
        self,
        *,
        lease: _SQLiteGenerationView,
        prepared_batch_id: str,
        prepared_kind: str,
        prepared_source_digest: str | None,
        prepared_source_path: str | None,
        timestamp: str,
        prepared_invalid_count: int,
        prepared_duplicate_source_count: int,
        prepared_drafts: tuple[_PreparedRecordDraft, ...],
        validated_candidate_plan: _ValidatedCandidateWritePlan,
    ) -> tuple[TMRecord, ...]:
        validate_store_identity = _validate_store_identity
        with _open_leased_connection(lease) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_store_identity(
                    connection,
                    resource_id=lease.stage.resource_identity.resource_id,
                    canonical_store_id=lease.canonical_store_id,
                    target_identity=(
                        lease.stage.resource_identity.target_identity
                    ),
                )
                prior_head_revision = _meta_int(
                    _read_meta(connection),
                    "head_revision",
                )
                completed_revision = prior_head_revision + 1
                connection.execute(
                    "INSERT INTO tm_origin_batch("
                    "batch_id, kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "completed_revision, created_at) "
                    "VALUES (?, ?, ?, ?, 'staged', ?, ?, ?, NULL, ?)",
                    (
                        prepared_batch_id,
                        prepared_kind,
                        prepared_source_digest,
                        prepared_source_path,
                        len(prepared_drafts),
                        prepared_invalid_count,
                        prepared_duplicate_source_count,
                        timestamp,
                    ),
                )
                inserted = _insert_prepared_records_and_indexes(
                    connection,
                    prepared_drafts,
                    validated_candidate_plan,
                    batch_id=prepared_batch_id,
                )
                _validate_candidate_index_before_publication(
                    connection,
                    fts5_available=lease.fts5_available,
                )
                completed_batch = connection.execute(
                    "UPDATE tm_origin_batch "
                    "SET status = 'completed', completed_revision = ? "
                    "WHERE batch_id = ?",
                    (completed_revision, prepared_batch_id),
                )
                if completed_batch.rowcount != 1:
                    raise SQLiteStoreSchemaError(
                        "STORE.BATCH_COMPLETION_MISSING"
                    )
                updated = connection.execute(
                    "UPDATE tm_meta SET value = ? "
                    "WHERE key = 'head_revision' AND value = ?",
                    (str(completed_revision), str(prior_head_revision)),
                )
                if updated.rowcount != 1:
                    raise SQLiteStoreSchemaError(
                        "STORE.HEAD_REVISION_MISSING"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def append_streamed_batch(
        self,
        *,
        batch_id: str,
        kind: str,
        drafts: Iterator[tuple[TMRecordDraft, int | None]],
        source_digest: str,
        source_path: Path,
        invalid_count: int,
        duplicate_source_count: int,
        chunk_size: int,
        _defer_secondary_indexes: bool = False,
    ) -> None:
        """Append one ordered origin batch from a bounded draft stream.

        The stream is consumed in fixed-size chunks. Each chunk is committed
        in its own transaction; the batch row starts as ``staged`` and is
        completed with the final counts in the last chunk's transaction. Any
        failure raises without a completed revision, so the caller must
        discard the stage (the migration build deletes both stage files).
        """

        if type(_defer_secondary_indexes) is not bool:
            raise TypeError(
                "_defer_secondary_indexes must be a built-in bool"
            )
        self._append_streamed_batch_body(
            batch_id=batch_id,
            kind=kind,
            drafts=drafts,
            source_digest=source_digest,
            source_path=source_path,
            invalid_count=invalid_count,
            duplicate_source_count=duplicate_source_count,
            chunk_size=chunk_size,
            defer_secondary_indexes=_defer_secondary_indexes,
            immediate_seal=False,
        )

    def _append_streamed_batch_for_immediate_seal(
        self,
        *,
        batch_id: str,
        kind: str,
        drafts: Iterator[tuple[TMRecordDraft, int | None]],
        source_digest: str,
        source_path: Path,
        invalid_count: int,
        duplicate_source_count: int,
        chunk_size: int,
    ) -> None:
        """Build deferred indexes for an owner-held immediate seal handoff."""

        self._append_streamed_batch_body(
            batch_id=batch_id,
            kind=kind,
            drafts=drafts,
            source_digest=source_digest,
            source_path=source_path,
            invalid_count=invalid_count,
            duplicate_source_count=duplicate_source_count,
            chunk_size=chunk_size,
            defer_secondary_indexes=True,
            immediate_seal=True,
        )

    def _append_streamed_batch_body(
        self,
        *,
        batch_id: str,
        kind: str,
        drafts: Iterator[tuple[TMRecordDraft, int | None]],
        source_digest: str,
        source_path: Path,
        invalid_count: int,
        duplicate_source_count: int,
        chunk_size: int,
        defer_secondary_indexes: bool,
        immediate_seal: bool,
    ) -> None:
        """Implement public validation and owner-private immediate sealing."""

        _validate_batch_scalars(
            batch_id=batch_id,
            kind=kind,
            source_digest=source_digest,
            source_path=source_path,
            invalid_count=invalid_count,
            duplicate_source_count=duplicate_source_count,
            created_at=None,
            extension=None,
        )
        if kind == "local_write":
            raise ValueError("streamed batches cannot be local_write")
        if not isinstance(drafts, Iterator):
            raise TypeError("drafts must be an iterator")
        if type(chunk_size) is not int:
            raise TypeError("chunk_size must be a built-in integer")
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        if type(defer_secondary_indexes) is not bool:
            raise TypeError(
                "defer_secondary_indexes must be a built-in bool"
            )
        if type(immediate_seal) is not bool:
            raise TypeError("immediate_seal must be a built-in bool")
        if immediate_seal and not defer_secondary_indexes:
            raise ValueError("immediate seal requires deferred secondary indexes")
        timestamp = datetime.now(UTC).isoformat()

        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                if immediate_seal:
                    _configure_immediate_seal_bulk_cache(connection)
                if defer_secondary_indexes:
                    _suspend_streamed_stage_secondary_indexes(
                        connection,
                        lease,
                    )
                chunk_index = 0
                completed_revision = 0
                prior_head_revision = 0
                while True:
                    chunk = _next_draft_chunk(drafts, chunk_size)
                    is_last = len(chunk) < chunk_size
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if chunk_index == 0:
                            _validate_store_identity(
                                connection,
                                resource_id=(
                                    lease.stage.resource_identity.resource_id
                                ),
                                canonical_store_id=lease.canonical_store_id,
                                target_identity=(
                                    lease.stage.resource_identity.target_identity
                                ),
                            )
                            prior_head_revision = _meta_int(
                                _read_meta(connection),
                                "head_revision",
                            )
                            completed_revision = prior_head_revision + 1
                            connection.execute(
                                "INSERT INTO tm_origin_batch("
                                "batch_id, kind, source_digest, source_path, "
                                "status, valid_count, invalid_count, "
                                "duplicate_source_count, completed_revision, "
                                "created_at) "
                                "VALUES (?, ?, ?, ?, 'staged', 0, 0, 0, "
                                "NULL, ?)",
                                (
                                    batch_id,
                                    kind,
                                    source_digest,
                                    Path.__str__(source_path),
                                    timestamp,
                                ),
                            )
                        if chunk:
                            _insert_streamed_chunk(
                                connection,
                                chunk,
                                batch_id=batch_id,
                                fts5_available=lease.fts5_available,
                                ordinal_offset=(
                                    chunk_index * chunk_size
                                ),
                            )
                        if is_last:
                            if defer_secondary_indexes:
                                _restore_streamed_stage_secondary_indexes(
                                    connection,
                                    lease,
                                    before_publication=True,
                                )
                            if not immediate_seal:
                                _validate_candidate_index_before_publication(
                                    connection,
                                    fts5_available=lease.fts5_available,
                                )
                            _complete_streamed_batch(
                                connection,
                                batch_id=batch_id,
                                completed_revision=completed_revision,
                                prior_head_revision=prior_head_revision,
                                invalid_count=invalid_count,
                                duplicate_source_count=(
                                    duplicate_source_count
                                ),
                            )
                    except Exception:
                        connection.rollback()
                        raise
                    connection.commit()
                    chunk_index += 1
                    if is_last:
                        break

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        prepared_ids = _validate_records_by_id_input(record_ids)
        with self._coordinator._operation_lease() as lease:
            return _records_by_id_body(lease, prepared_ids)

    def fts5_candidate_ids(
        self,
        match_expression: str,
    ) -> tuple[int, ...] | None:
        """Return FTS identities, or None when this generation has no FTS5."""

        if type(match_expression) is not str:
            raise TypeError("match_expression must be a built-in string")
        if not match_expression:
            raise ValueError("match_expression must not be empty")
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                self._validate_identity(connection, lease)
                if not _meta_bool(_read_meta(connection), "fts5_available"):
                    return None
                return candidate_projection.fts5_candidate_ids(
                    connection,
                    match_expression,
                )

    def fts5_candidate_ids_for_trigrams(
        self,
        trigrams: tuple[str, ...],
    ) -> tuple[int, ...] | None:
        """Return the union for all unique trigrams using bounded FTS queries."""

        if type(trigrams) is not tuple:
            raise TypeError("trigrams must be a built-in tuple")
        seen: set[str] = set()
        for trigram in trigrams:
            if type(trigram) is not str:
                raise TypeError("trigrams must contain built-in strings")
            if len(trigram) != 3 or trigram in seen:
                raise ValueError("trigrams must be unique trigrams")
            seen.add(trigram)
        if not trigrams:
            raise ValueError("trigrams must not be empty")
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                try:
                    connection.execute("BEGIN")
                    self._validate_identity(connection, lease)
                    if not lease.fts5_available:
                        connection.rollback()
                        return None
                    record_ids = candidate_projection.fts5_candidate_ids_for_trigrams(
                        connection,
                        trigrams,
                    )
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise SQLiteStoreSchemaError("STORE.FTS5_QUERY_FAILED") from error
                except BaseException:
                    connection.rollback()
                    raise
        return record_ids

    def gram_candidate_overlaps(
        self,
        query_postings: tuple[tuple[int, str], ...],
        *,
        candidate_cap: int,
    ) -> tuple[tuple[int, int], ...]:
        """Return record identities and matched unique posting counts."""

        if type(query_postings) is not tuple:
            raise TypeError("query_postings must be a built-in tuple")
        prepared: list[tuple[int, str]] = []
        for posting in query_postings:
            if type(posting) is not tuple or len(posting) != 2:
                raise TypeError("query_postings must contain built-in pairs")
            gram_size, gram = posting
            if type(gram_size) is not int or type(gram) is not str:
                raise TypeError("query posting values must use built-in types")
            if gram_size not in {1, 2, 3} or len(gram) != gram_size:
                raise ValueError("query posting is invalid")
            prepared.append((gram_size, gram))
        if len(set(prepared)) != len(prepared):
            raise ValueError("query_postings must be unique")
        if not prepared:
            raise ValueError("query_postings must not be empty")
        if type(candidate_cap) is not int:
            raise TypeError("candidate_cap must be a built-in integer")
        if not 1 <= candidate_cap <= 8192:
            raise ValueError("candidate_cap is outside the safe range")

        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                try:
                    connection.execute("BEGIN")
                    self._validate_identity(connection, lease)
                    overlaps = candidate_projection.gram_candidate_overlaps(
                        connection,
                        tuple(prepared),
                        candidate_cap=candidate_cap,
                    )
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise SQLiteStoreSchemaError(
                        "STORE.GRAM_QUERY_FAILED"
                    ) from error
                except BaseException:
                    connection.rollback()
                    raise
        return overlaps

    def candidate_recall_snapshot(
        self,
        *,
        fts_query_trigrams: tuple[str, ...] | None,
        query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...],
        candidate_floor: int,
        fts_query_degenerate: bool,
    ) -> SQLiteCandidateRecallSnapshot:
        """Read one complete candidate-stage snapshot under one generation lease."""

        prepared = _prepare_candidate_recall_inputs(
            fts_query_trigrams=fts_query_trigrams,
            query_grams_by_size=query_grams_by_size,
            candidate_floor=candidate_floor,
            fts_query_degenerate=fts_query_degenerate,
        )
        with self._coordinator._operation_lease() as lease:
            return _candidate_recall_snapshot_body(lease, prepared)

    @contextmanager
    def query_lease(self) -> Iterator[SQLiteTMQueryView]:
        """Yield one read-only query view under exactly one operation lease.

        The view is an exact ``SQLiteTMQueryView`` bound to this exact
        store and to the coordinator's captured generation view.  It is
        invalidated when the lease exits; later calls fail closed and
        never acquire a new lease.  The view exposes only the read-only
        query ports and never receives an append/export/activation or
        update port.
        """

        with self._coordinator._operation_lease() as lease:
            token = object()
            with self._coordinator._condition:
                self._query_view_tokens.add(token)
            try:
                view = SQLiteTMQueryView(self, lease, token=token)
                try:
                    yield view
                finally:
                    view._invalidate()
            finally:
                with self._coordinator._condition:
                    self._query_view_tokens.discard(token)

    def health(self) -> StoreHealth:
        """Return one truthful physical-health observation under one lease.

        Revalidates canonical identity and derives schema/generation/
        count/index and canonical ledger/source-binding facts from the
        same leased canonical view in one read transaction.  It never
        checks or mutates external JSONL, never latches or clears
        divergence, never publishes Gate C, and never writes the DB.
        """

        with self._coordinator._operation_lease() as lease:
            return _store_health_body(lease)

    def export_records(self) -> Iterator[TMRecord]:
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                self._validate_identity(connection, lease)
                rows = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM tm_record "
                    "ORDER BY record_id ASC"
                ).fetchall()
        return iter(tuple(_record_from_row(row) for row in rows))

    def _validate_identity(
        self,
        connection: sqlite3.Connection,
        lease: _SQLiteGenerationView,
    ) -> None:
        _validate_lease_identity(connection, lease)


@dataclass(frozen=True)
class _PreparedCandidateRecallInputs:
    fts_query_trigrams: tuple[str, ...] | None
    query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...]
    candidate_floor: int
    fts_query_degenerate: bool


def _prepare_candidate_recall_inputs(
    *,
    fts_query_trigrams: tuple[str, ...] | None,
    query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...],
    candidate_floor: int,
    fts_query_degenerate: bool,
) -> _PreparedCandidateRecallInputs:
    """Exact-type and privately snapshot one candidate snapshot input set."""

    if fts_query_trigrams is not None:
        if type(fts_query_trigrams) is not tuple:
            raise TypeError("fts_query_trigrams must be a built-in tuple")
        seen_trigrams: set[str] = set()
        for trigram in fts_query_trigrams:
            if type(trigram) is not str:
                raise TypeError("fts query trigrams must be built-in strings")
            if len(trigram) != 3 or trigram in seen_trigrams:
                raise ValueError("fts query trigrams are invalid")
            seen_trigrams.add(trigram)
        if not fts_query_trigrams:
            raise ValueError("fts_query_trigrams must not be empty")
        fts_query_trigrams = tuple(fts_query_trigrams)
    if type(query_grams_by_size) is not tuple:
        raise TypeError("query_grams_by_size must be a built-in tuple")
    prepared_stages: list[tuple[int, tuple[str, ...]]] = []
    seen_sizes: set[int] = set()
    for stage in query_grams_by_size:
        if type(stage) is not tuple or len(stage) != 2:
            raise TypeError("query gram stages must contain built-in pairs")
        gram_size, grams = stage
        if type(gram_size) is not int or type(grams) is not tuple:
            raise TypeError("query gram stage values must use built-in types")
        if gram_size not in {1, 2, 3} or gram_size in seen_sizes:
            raise ValueError("query gram stage size is invalid")
        copied_grams: list[str] = []
        seen_grams: set[str] = set()
        for gram in grams:
            if type(gram) is not str:
                raise TypeError("query grams must contain built-in strings")
            if len(gram) != gram_size:
                raise ValueError("query gram length is invalid")
            if gram in seen_grams:
                raise ValueError("query grams must be unique")
            seen_grams.add(gram)
            copied_grams.append(gram)
        if not copied_grams:
            raise ValueError("query gram stages must not be empty")
        seen_sizes.add(gram_size)
        prepared_stages.append((gram_size, tuple(copied_grams)))
    prepared_sizes = tuple(size for size, _grams in prepared_stages)
    if fts_query_trigrams is None:
        if prepared_sizes not in {(), (1,), (2,)}:
            raise ValueError("short-query gram stage order is invalid")
    elif prepared_sizes != (3, 2, 1):
        raise ValueError("long-query gram stage order is invalid")
    if type(candidate_floor) is not int:
        raise TypeError("candidate_floor must be a built-in integer")
    if not 1 <= candidate_floor <= 8192:
        raise ValueError("candidate_floor is outside the safe range")
    if type(fts_query_degenerate) is not bool:
        raise TypeError("fts_query_degenerate must be a built-in bool")
    return _PreparedCandidateRecallInputs(
        fts_query_trigrams=fts_query_trigrams,
        query_grams_by_size=tuple(prepared_stages),
        candidate_floor=candidate_floor,
        fts_query_degenerate=fts_query_degenerate,
    )


def _candidate_recall_snapshot_body(
    lease: _SQLiteGenerationView,
    prepared: _PreparedCandidateRecallInputs,
) -> SQLiteCandidateRecallSnapshot:
    with _open_leased_connection(lease) as connection:
        try:
            connection.execute("BEGIN")
            _validate_lease_identity(connection, lease)
            meta_fts5_available = _meta_bool(
                _read_meta(connection), "fts5_available"
            )
            if meta_fts5_available != lease.fts5_available:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_CAPABILITY_MISMATCH"
                )
            projected_rows = candidate_projection.candidate_recall_snapshot(
                connection,
                fts5_available=lease.fts5_available,
                fts_query_trigrams=prepared.fts_query_trigrams,
                query_grams_by_size=prepared.query_grams_by_size,
                candidate_floor=prepared.candidate_floor,
                fts_query_degenerate=prepared.fts_query_degenerate,
            )
            if type(projected_rows) is not tuple or len(projected_rows) != 2:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_RESULT_INVALID"
                )
            try:
                snapshot = SQLiteCandidateRecallSnapshot(
                    fts5_available=lease.fts5_available,
                    stage_matches=projected_rows[0],
                    folded_sources=projected_rows[1],
                )
            except (TypeError, ValueError) as error:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_RESULT_INVALID"
                ) from error
            if not lease.fts5_available and any(
                stage_name == "FTS_TRIGRAM"
                for stage_name, _matches in snapshot.stage_matches
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_CAPABILITY_MISMATCH"
                )
            matched_ids = {
                record_id
                for _stage_name, matches in snapshot.stage_matches
                for record_id, _overlap in matches
            }
            if matched_ids != {
                record_id
                for record_id, _folded_source in snapshot.folded_sources
            }:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_RESULT_INVALID"
                )
            connection.commit()
            return snapshot
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise


def _bounded_seed_stages(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    fts5_available: bool,
    seed_limit: int,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Late-bound compatibility wrapper for the unique projection owner."""

    return candidate_projection.bounded_seed_stages(
        connection,
        folded_query=folded_query,
        fts5_available=fts5_available,
        seed_limit=seed_limit,
    )


def _candidate_proof_snapshot_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    seed_limit: int,
    connection: sqlite3.Connection | None = None,
) -> SQLiteCandidateProofSnapshot:
    """Read one real seed plus block frontiers without record-level facts."""

    with _leased_connection_scope(lease, connection) as connection:
        try:
            connection.execute("BEGIN")
            meta = _read_meta(connection)
            _validate_lease_identity_meta(meta, lease)
            head_revision = _meta_int(meta, "head_revision")
            fts5_available = _meta_bool(meta, "fts5_available")
            if fts5_available != lease.fts5_available:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_CAPABILITY_MISMATCH"
                )
            total_record_count = _table_count(connection, "tm_record")
            projected_rows = candidate_projection.candidate_proof_snapshot(
                connection,
                folded_query=folded_query,
                seed_limit=seed_limit,
                fts5_available=fts5_available,
                total_record_count=total_record_count,
            )
            if (
                type(projected_rows) is not tuple
                or len(projected_rows) != 3
                or type(projected_rows[0]) is not tuple
                or type(projected_rows[1]) is not tuple
                or any(
                    type(block) is not SQLiteCandidateProofBlock
                    for block in projected_rows[1]
                )
                or type(projected_rows[2]) is not str
                or len(projected_rows[2]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in projected_rows[2]
                )
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_PROOF_INVALID"
                )
            expected_seed_names = (
                ("FTS_TRIGRAM",)
                if fts5_available and len(folded_query) >= 3
                else tuple(
                    f"GRAM_{size}"
                    for size in (
                        (1,)
                        if len(folded_query) == 1
                        else (2,)
                        if len(folded_query) == 2
                        else (3, 2, 1)
                    )
                )
            )
            seed_names: list[str] = []
            for stage in projected_rows[0]:
                if (
                    type(stage) is not tuple
                    or len(stage) != 2
                    or type(stage[0]) is not str
                    or type(stage[1]) is not tuple
                    or any(
                        type(record_id) is not int
                        or not 1 <= record_id <= total_record_count
                        for record_id in stage[1]
                    )
                    or len(set(stage[1])) != len(stage[1])
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_PROOF_INVALID"
                    )
                seed_names.append(stage[0])
            if tuple(seed_names) != expected_seed_names:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_PROOF_INVALID"
                )
            candidate_projection.validate_candidate_proof_blocks(
                connection,
                blocks=projected_rows[1],
                query_maxima_digest=projected_rows[2],
            )
            snapshot = SQLiteCandidateProofSnapshot(
                index_kind=(
                    "FTS5_TRIGRAM"
                    if fts5_available and len(folded_query) >= 3
                    else "GRAM_FALLBACK"
                ),
                seed_stages=projected_rows[0],
                blocks=projected_rows[1],
                total_record_count=total_record_count,
                head_revision=head_revision,
                query_maxima_digest=projected_rows[2],
            )
            connection.commit()
            return snapshot
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_PROOF_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise


def _candidate_proof_block_records_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    block: SQLiteCandidateProofBlock,
    head_revision: int,
    total_record_count: int,
    connection: sqlite3.Connection | None = None,
) -> tuple[SQLiteCandidateProofRecord, ...]:
    """Read and close exact proof facts for exactly one opened block."""

    with _leased_connection_scope(lease, connection) as connection:
        try:
            connection.execute("BEGIN")
            _validate_lease_identity(connection, lease)
            meta = _read_meta(connection)
            if (
                _meta_int(meta, "head_revision") != head_revision
                or _table_count(connection, "tm_record") != total_record_count
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_STALE")
            records = candidate_projection.candidate_proof_block_records(
                connection,
                folded_query=folded_query,
                block=block,
                total_record_count=total_record_count,
            )
            connection.commit()
            return records
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_PROOF_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise


def _candidate_proof_dense_binding_digest(
    *,
    lease: _SQLiteGenerationView,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    head_revision: int,
    total_record_count: int,
    query_maxima_digest: str,
) -> str:
    identity = lease.stage.resource_identity
    payload = json.dumps(
        (
            contract_module.CANDIDATE_PROOF_TRAVERSAL_VERSION,
            contract_module.SCORER_BOUND_VERSION_V1,
            identity.resource_id,
            lease.canonical_store_id,
            identity.target_identity,
            lease.generation,
            hashlib.sha256(folded_query.encode("utf-8")).hexdigest(),
            head_revision,
            total_record_count,
            query_maxima_digest,
            tuple(tuple(block.__dict__.values()) for block in blocks),
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_candidate_proof_dense_phase1_result(
    value: object,
    *,
    view: SQLiteTMQueryView,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    head_revision: int,
    total_record_count: int,
    query_maxima_digest: str,
) -> None:
    """Accept only the exact store-issued phase-one fact objects."""

    if (
        type(value) is not SQLiteCandidateProofDensePhase1
        or type(view) is not SQLiteTMQueryView
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
    view._check_lifetime()
    expected_binding = _candidate_proof_dense_binding_digest(
        lease=view._lease,
        folded_query=folded_query,
        blocks=blocks,
        head_revision=head_revision,
        total_record_count=total_record_count,
        query_maxima_digest=query_maxima_digest,
    )
    candidate_contracts.validate_candidate_proof_dense_phase1_result(
        value,
        binding_digest=expected_binding,
        total_record_count=total_record_count,
    )


def _validate_candidate_proof_dense_phase2_result(
    value: object,
    *,
    binding_digest: str,
    record_ids: tuple[int, ...],
    source_fold_lengths: tuple[int, ...],
) -> None:
    """Accept only the exact store-issued ordered phase-two projection."""

    candidate_contracts.validate_candidate_proof_dense_phase2_result(
        value,
        binding_digest=binding_digest,
        record_ids=record_ids,
        source_fold_lengths=source_fold_lengths,
    )


def _validate_candidate_proof_dense_binding(
    connection: sqlite3.Connection,
    *,
    lease: _SQLiteGenerationView,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    head_revision: int,
    total_record_count: int,
    query_maxima_digest: str,
) -> str:
    meta = _read_meta(connection)
    _validate_lease_identity_meta(meta, lease)
    if (
        _meta_int(meta, "head_revision") != head_revision
        or _table_count(connection, "tm_record") != total_record_count
    ):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_STALE")
    candidate_projection.validate_candidate_proof_blocks(
        connection,
        blocks=blocks,
        query_maxima_digest=query_maxima_digest,
    )
    return _candidate_proof_dense_binding_digest(
        lease=lease,
        folded_query=folded_query,
        blocks=blocks,
        head_revision=head_revision,
        total_record_count=total_record_count,
        query_maxima_digest=query_maxima_digest,
    )


def _candidate_proof_dense_phase1_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    head_revision: int,
    total_record_count: int,
    query_maxima_digest: str,
    connection: sqlite3.Connection | None = None,
) -> SQLiteCandidateProofDensePhase1:
    """Return all lengths and exact bigram intersections, then commit."""

    with _leased_connection_scope(lease, connection) as connection:
        try:
            connection.execute("BEGIN")
            binding_digest = _validate_candidate_proof_dense_binding(
                connection,
                lease=lease,
                folded_query=folded_query,
                blocks=blocks,
                head_revision=head_revision,
                total_record_count=total_record_count,
                query_maxima_digest=query_maxima_digest,
            )
            projected_rows = candidate_projection.candidate_proof_dense_phase1(
                connection,
                folded_query=folded_query,
                blocks=blocks,
                total_record_count=total_record_count,
            )
            if (
                type(projected_rows) is not tuple
                or len(projected_rows) != 2
                or type(projected_rows[0]) is not tuple
                or type(projected_rows[1]) is not tuple
                or len(projected_rows[0]) != total_record_count
                or len(projected_rows[1]) != total_record_count
                or any(
                    type(length) is not int or length < 1
                    for length in projected_rows[0]
                )
                or any(
                    type(intersection) is not int or intersection < 0
                    for intersection in projected_rows[1]
                )
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_PROOF_INVALID"
                )
            source_fold_lengths = projected_rows[0]
            bigram_intersections = projected_rows[1]
            receipt = _SQLiteCandidateProofDenseReceipt(
                phase="DENSE_PHASE1_V1",
                binding_digest=binding_digest,
                item_count=total_record_count,
                record_ids=None,
                source_folds_v1=None,
                source_fold_lengths=source_fold_lengths,
                bigram_multiset_intersections=bigram_intersections,
                _factory_key=_CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY,
            )
            result = SQLiteCandidateProofDensePhase1(
                source_fold_lengths=source_fold_lengths,
                bigram_multiset_intersections=bigram_intersections,
                binding_digest=binding_digest,
                _receipt=receipt,
            )
            connection.commit()
            return result
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_PROOF_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise


def _candidate_proof_dense_phase2_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    blocks: tuple[SQLiteCandidateProofBlock, ...],
    head_revision: int,
    total_record_count: int,
    query_maxima_digest: str,
    binding_digest: str,
    record_ids: tuple[int, ...],
    source_fold_lengths: tuple[int, ...],
    connection: sqlite3.Connection | None = None,
) -> SQLiteCandidateProofDensePhase2:
    """Return only the ordered folded-source projection for the exact R set."""

    with _leased_connection_scope(lease, connection) as connection:
        try:
            connection.execute("BEGIN")
            actual_binding = _validate_candidate_proof_dense_binding(
                connection,
                lease=lease,
                folded_query=folded_query,
                blocks=blocks,
                head_revision=head_revision,
                total_record_count=total_record_count,
                query_maxima_digest=query_maxima_digest,
            )
            if actual_binding != binding_digest:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_PROOF_INVALID"
                )
            projected_rows = candidate_projection.candidate_proof_dense_phase2(
                connection,
                total_record_count=total_record_count,
                record_ids=record_ids,
                source_fold_lengths=source_fold_lengths,
            )
            if (
                type(projected_rows) is not tuple
                or len(projected_rows) != 3
                or any(type(values) is not tuple for values in projected_rows)
                or projected_rows[0] != record_ids
                or projected_rows[2] != source_fold_lengths
                or len(projected_rows[1]) != len(record_ids)
                or any(
                    type(source) is not str
                    or not source
                    or len(source) != source_fold_lengths[ordinal]
                    for ordinal, source in enumerate(projected_rows[1])
                )
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_PROOF_INVALID"
                )
            projected_record_ids = projected_rows[0]
            projected_source_folds = projected_rows[1]
            projected_source_lengths = projected_rows[2]
            receipt = _SQLiteCandidateProofDenseReceipt(
                phase="DENSE_PHASE2_V1",
                binding_digest=actual_binding,
                item_count=len(projected_record_ids),
                record_ids=projected_record_ids,
                source_folds_v1=projected_source_folds,
                source_fold_lengths=projected_source_lengths,
                bigram_multiset_intersections=None,
                _factory_key=_CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY,
            )
            result = SQLiteCandidateProofDensePhase2(
                record_ids=projected_record_ids,
                source_folds_v1=projected_source_folds,
                source_fold_lengths=projected_source_lengths,
                binding_digest=actual_binding,
                _receipt=receipt,
            )
            connection.commit()
            return result
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_PROOF_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise


def _validate_candidate_proof_generation_body(
    lease: _SQLiteGenerationView,
    *,
    head_revision: int,
    total_record_count: int,
    connection: sqlite3.Connection | None = None,
) -> None:
    with _leased_connection_scope(lease, connection) as connection:
        connection.execute("BEGIN")
        try:
            meta = _read_meta(connection)
            _validate_lease_identity_meta(meta, lease)
            if (
                _meta_int(meta, "head_revision") != head_revision
                or _table_count(connection, "tm_record") != total_record_count
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_STALE")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _validate_store_identity(
    connection: sqlite3.Connection,
    *,
    resource_id: str,
    canonical_store_id: str,
    target_identity: str,
) -> None:
    meta = _read_meta(connection)
    _validate_store_identity_meta(
        meta,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        target_identity=target_identity,
    )


def _validate_store_identity_meta(
    meta: dict[str, str],
    *,
    resource_id: str,
    canonical_store_id: str,
    target_identity: str,
) -> None:
    if (
        meta["resource_id"] != resource_id
        or meta["canonical_store_id"] != canonical_store_id
        or meta["target_identity"] != target_identity
    ):
        raise SQLiteStoreSchemaError("STORE.IDENTITY_MISMATCH")
    if meta.get("activation_status") == "SEALED":
        raise SQLiteStoreSchemaError("STORE.STAGE_SEALED")


def _validate_lease_identity(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> None:
    identity = lease.stage.resource_identity
    _validate_store_identity(
        connection,
        resource_id=identity.resource_id,
        canonical_store_id=lease.canonical_store_id,
        target_identity=identity.target_identity,
    )


def _validate_lease_identity_meta(
    meta: dict[str, str],
    lease: _SQLiteGenerationView,
) -> None:
    identity = lease.stage.resource_identity
    _validate_store_identity_meta(
        meta,
        resource_id=identity.resource_id,
        canonical_store_id=lease.canonical_store_id,
        target_identity=identity.target_identity,
    )


def _validate_exact_records_input(source_raw: object) -> None:
    if type(source_raw) is not str:
        raise TypeError("source_raw must be a built-in string")


def _exact_records_body(
    lease: _SQLiteGenerationView,
    source_raw: str,
) -> tuple[TMRecord, ...]:
    with _open_leased_connection(lease) as connection:
        _validate_lease_identity(connection, lease)
        rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM tm_record "
            "WHERE source_raw = ? ORDER BY record_id DESC",
            (source_raw,),
        ).fetchall()
    return tuple(_record_from_row(row) for row in rows)


def _validate_records_by_id_input(
    record_ids: object,
) -> tuple[int, ...]:
    if type(record_ids) is not tuple:
        raise TypeError("record_ids must be a built-in tuple")
    copied_ids: list[int] = []
    for record_id in record_ids:
        if type(record_id) is not int:
            raise TypeError(
                "record_ids must contain built-in integers"
            )
        if record_id < 1:
            raise ValueError(
                "record_ids must contain positive integers"
            )
        copied_ids.append(record_id)
    return tuple(copied_ids)


def _records_by_id_body(
    lease: _SQLiteGenerationView,
    record_ids: tuple[int, ...],
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[TMRecord, ...]:
    if not record_ids:
        return ()
    placeholders = ",".join("?" for _ in record_ids)
    with _leased_connection_scope(lease, connection) as connection:
        _validate_lease_identity(connection, lease)
        rows = connection.execute(
            f"SELECT {_RECORD_COLUMNS} FROM tm_record "
            f"WHERE record_id IN ({placeholders})",
            record_ids,
        ).fetchall()
    by_id = {
        record.record_id: record
        for record in (_record_from_row(row) for row in rows)
    }
    return tuple(
        by_id[record_id]
        for record_id in record_ids
        if record_id in by_id
    )


def _canonical_revision_from_connection(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> CanonicalRevisionSnapshot:
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        revision = _canonical_revision_from_transaction(connection, lease)
        if owns_transaction:
            connection.commit()
        return revision
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def _canonical_revision_from_transaction(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> CanonicalRevisionSnapshot:
    if not connection.in_transaction:
        raise SQLiteStoreSchemaError("STORE.READ_SNAPSHOT_MISSING")
    identity = lease.stage.resource_identity
    _validate_store_identity(
        connection,
        resource_id=identity.resource_id,
        canonical_store_id=lease.canonical_store_id,
        target_identity=identity.target_identity,
    )
    meta = _read_meta(connection)
    schema_version = _meta_int(meta, "schema_version")
    head_revision = _meta_int(meta, "head_revision")
    record_count = _table_count(connection, "tm_record")
    if schema_version == TM_LEGACY_SCHEMA_VERSION:
        _ = _legacy_revision_ancestry(
            connection,
            head_revision=head_revision,
            record_count=record_count,
        )
    else:
        ancestry_rows = _origin_ancestry_rows(connection)
        _ = _validate_revision_ancestry_rows(
            ancestry_rows,
            head_revision=head_revision,
            record_count=record_count,
        )
    return CanonicalRevisionSnapshot(
        resource_id=identity.resource_id,
        canonical_store_id=lease.canonical_store_id,
        generation=lease.generation,
        head_revision=head_revision,
        record_count=record_count,
    )


def _revision_record_counts(
    connection: sqlite3.Connection,
    *,
    head_revision: int,
    record_count: int,
) -> dict[int, int]:
    """Return the proven cumulative record count for each revision."""

    if not connection.in_transaction:
        raise SQLiteStoreSchemaError("STORE.READ_SNAPSHOT_MISSING")
    meta = _read_meta(connection)
    schema_version = _meta_int(meta, "schema_version")
    if schema_version == TM_LEGACY_SCHEMA_VERSION:
        cumulative = _legacy_revision_ancestry(
            connection,
            head_revision=head_revision,
            record_count=record_count,
        )
    else:
        cumulative = _validate_revision_ancestry_rows(
            _origin_ancestry_rows(connection),
            head_revision=head_revision,
            record_count=record_count,
        )
    return dict(cumulative)


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    if table_name not in _BASE_TABLES:
        raise ValueError("table name is not approved")
    row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise SQLiteStoreSchemaError("STORE.TABLE_COUNT_INVALID")
    return row[0]


def _export_record_from_row(
    row: tuple[object, ...],
) -> CanonicalExportRecord:
    """Build one export record from the 13-column export projection."""

    try:
        if len(row) != 13:
            raise ValueError("export record row is invalid")
        record = _record_from_row(row[:11])
        usage_count = _row_int(row[11])
        if usage_count < 0:
            raise ValueError("export usage count is invalid")
        last_used = None if row[12] is None else str(row[12])
    except (TypeError, ValueError, IndexError) as error:
        raise SQLiteStoreSchemaError("STORE.RECORD_CORRUPT") from error
    return CanonicalExportRecord(
        record=record,
        usage_count=usage_count,
        last_used=last_used,
    )


def _record_from_row(row: tuple[object, ...]) -> TMRecord:
    try:
        provenance_payload = json.loads(str(row[7]))
        if not isinstance(provenance_payload, list):
            raise ValueError("provenance must be a list")
        provenance_items: list[tuple[str, str]] = []
        for item in provenance_payload:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise ValueError("provenance item is invalid")
            provenance_items.append((item[0], item[1]))
        provenance = tuple(provenance_items)
        return TMRecord(
            record_id=_row_int(row[0]),
            source_raw=str(row[1]),
            target_raw=str(row[2]),
            speaker_raw=None if row[3] is None else str(row[3]),
            context_prev_raw=None if row[4] is None else str(row[4]),
            context_next_raw=None if row[5] is None else str(row[5]),
            file_source=None if row[6] is None else str(row[6]),
            provenance=provenance,
            legacy_line_no=(
                None if row[8] is None else _row_int(row[8])
            ),
            origin_batch_id=str(row[9]),
            origin_ordinal=_row_int(row[10]),
        )
    except (TypeError, ValueError, IndexError, json.JSONDecodeError) as error:
        raise SQLiteStoreSchemaError("STORE.RECORD_CORRUPT") from error


def _row_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("record integer column is invalid")
    return value


type _ValidatedCandidateWritePlan = tuple[
    tuple[tuple[int, int, str, int], ...],
    tuple[int, ...],
]


def _prepare_append_batch_inputs(
    *,
    batch_id: str,
    kind: str,
    drafts: tuple[TMRecordDraft, ...],
    source_digest: str | None,
    source_path: Path | None,
    legacy_line_nos: tuple[int | None, ...] | None,
    invalid_count: int,
    duplicate_source_count: int,
    created_at: str | None,
    extension: Callable[
        [tuple[SQLiteCandidateRecord, ...]],
        SQLiteCandidateWritePlan,
    ]
    | None,
) -> tuple[
    str,
    str,
    str | None,
    str | None,
    str,
    int,
    int,
    tuple[_PreparedRecordDraft, ...],
    tuple[SQLiteCandidateRecord, ...],
]:
    _validate_batch_arguments(
        batch_id=batch_id,
        kind=kind,
        drafts=drafts,
        source_digest=source_digest,
        source_path=source_path,
        legacy_line_nos=legacy_line_nos,
        invalid_count=invalid_count,
        duplicate_source_count=duplicate_source_count,
        created_at=created_at,
        extension=extension,
    )
    line_numbers = (
        (None,) * len(drafts)
        if legacy_line_nos is None
        else legacy_line_nos
    )
    prepared_drafts = _prepare_record_drafts(drafts, line_numbers)
    candidate_records = tuple(
        SQLiteCandidateRecord(
            origin_ordinal=draft[10],
            source_fold_v1=draft[2],
        )
        for draft in prepared_drafts
    )
    return (
        batch_id,
        kind,
        source_digest,
        (
            None
            if source_path is None
            else Path.__str__(source_path)
        ),
        created_at if created_at is not None else datetime.now(UTC).isoformat(),
        invalid_count,
        duplicate_source_count,
        prepared_drafts,
        candidate_records,
    )


def _prepare_candidate_write_plan(
    extension: Callable[
        [tuple[SQLiteCandidateRecord, ...]],
        SQLiteCandidateWritePlan,
    ]
    | None,
    candidate_records: tuple[SQLiteCandidateRecord, ...],
    *,
    batch_size: int,
    fts5_available: bool,
    ordinal_base: int = 0,
) -> _ValidatedCandidateWritePlan:
    default_plan = _validate_and_copy_candidate_plan(
        build_candidate_write_plan(
            candidate_records,
            fts5_available=fts5_available,
        ),
        batch_size=batch_size,
        ordinal_base=ordinal_base,
    )
    if extension is None:
        return default_plan
    additional_plan = _validate_and_copy_candidate_plan(
        extension(candidate_records),
        batch_size=batch_size,
        ordinal_base=ordinal_base,
    )
    default_grams, default_fts = default_plan
    additional_grams, additional_fts = additional_plan
    default_by_key = {
        (ordinal, size, gram): term_frequency
        for ordinal, size, gram, term_frequency in default_grams
    }
    for ordinal, size, gram, term_frequency in additional_grams:
        if default_by_key.get((ordinal, size, gram)) != term_frequency:
            raise ValueError("extension candidate gram facts are not canonical")
    if not set(additional_fts).issubset(default_fts):
        raise ValueError("extension FTS facts are not canonical")
    return default_plan


def _prepare_record_drafts(
    drafts: tuple[TMRecordDraft, ...],
    legacy_line_nos: tuple[int | None, ...],
    *,
    ordinal_offset: int = 0,
) -> tuple[_PreparedRecordDraft, ...]:
    if type(ordinal_offset) is not int or ordinal_offset < 0:
        raise ValueError("ordinal_offset must be a non-negative integer")
    prepared: list[_PreparedRecordDraft] = []
    for ordinal, (draft, legacy_line_no) in enumerate(
        zip(drafts, legacy_line_nos, strict=True),
        start=ordinal_offset,
    ):
        source_raw = draft.source_raw
        provenance = draft.provenance
        prepared.append(
            (
                source_raw,
                draft.target_raw,
                fold_text_value_v1(source_raw),
                draft.speaker_raw,
                draft.context_prev_raw,
                draft.context_next_raw,
                draft.file_source,
                provenance,
                json.dumps(
                    provenance,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                legacy_line_no,
                ordinal,
            )
        )
    return tuple(prepared)


def _validate_and_copy_candidate_plan(
    plan: object,
    *,
    batch_size: int,
    ordinal_base: int = 0,
) -> _ValidatedCandidateWritePlan:
    if type(plan) is not SQLiteCandidateWritePlan:
        raise TypeError("extension must return SQLiteCandidateWritePlan")
    if type(plan.gram_rows) is not tuple:
        raise TypeError("gram_rows must be a built-in tuple")
    if type(ordinal_base) is not int or ordinal_base < 0:
        raise ValueError("ordinal_base must be a non-negative integer")
    gram_rows: list[tuple[int, int, str, int]] = []
    gram_keys: set[tuple[int, int, str]] = set()
    for row in plan.gram_rows:
        if type(row) is not SQLiteGramRow:
            raise TypeError("gram_rows must contain SQLiteGramRow values")
        origin_ordinal = row.origin_ordinal
        gram_size = row.gram_size
        gram = row.gram
        term_frequency = row.term_frequency
        if (
            type(origin_ordinal) is not int
            or not ordinal_base
            <= origin_ordinal
            < ordinal_base + batch_size
        ):
            raise ValueError(
                "candidate rows must reference a current batch ordinal"
            )
        if type(gram_size) is not int or gram_size not in {1, 2, 3}:
            raise ValueError("gram_size must be 1, 2, or 3")
        if type(gram) is not str or len(gram) != gram_size:
            raise ValueError("gram length must equal gram_size")
        if type(term_frequency) is not int or term_frequency < 1:
            raise ValueError("term_frequency must be a positive integer")
        key = (origin_ordinal, gram_size, gram)
        if key in gram_keys:
            raise ValueError("gram rows must be unique")
        gram_keys.add(key)
        gram_rows.append((*key, term_frequency))
    if type(plan.fts_origin_ordinals) is not tuple:
        raise TypeError("fts_origin_ordinals must be a built-in tuple")
    fts_origin_ordinals: list[int] = []
    seen_fts_ordinals: set[int] = set()
    for origin_ordinal in plan.fts_origin_ordinals:
        if (
            type(origin_ordinal) is not int
            or not ordinal_base
            <= origin_ordinal
            < ordinal_base + batch_size
        ):
            raise ValueError(
                "FTS rows must reference a current batch ordinal"
            )
        if origin_ordinal in seen_fts_ordinals:
            raise ValueError("FTS rows must be unique")
        seen_fts_ordinals.add(origin_ordinal)
        fts_origin_ordinals.append(origin_ordinal)
    return (tuple(gram_rows), tuple(fts_origin_ordinals))


def _apply_candidate_write_plan(
    connection: sqlite3.Connection,
    plan: _ValidatedCandidateWritePlan,
    *,
    record_ids_by_ordinal: dict[int, int],
    folded_sources_by_ordinal: dict[int, str],
) -> None:
    """Validate the complete bounded plan, then bulk-insert it atomically."""

    if type(plan) is not tuple or len(plan) != 2:
        raise TypeError("validated candidate plan is invalid")
    gram_rows, fts_origin_ordinals = plan
    if type(gram_rows) is not tuple or type(fts_origin_ordinals) is not tuple:
        raise TypeError("validated candidate plan is invalid")
    seen_grams: set[tuple[int, int, str]] = set()
    for row in gram_rows:
        if type(row) is not tuple or len(row) != 4:
            raise TypeError("validated gram row is invalid")
        origin_ordinal, gram_size, gram, term_frequency = row
        key = (origin_ordinal, gram_size, gram)
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids_by_ordinal
            or type(gram_size) is not int
            or gram_size not in {1, 2, 3}
            or type(gram) is not str
            or len(gram) != gram_size
            or type(term_frequency) is not int
            or term_frequency < 1
            or key in seen_grams
        ):
            raise ValueError("validated gram row is invalid")
        seen_grams.add(key)
    seen_fts_ordinals: set[int] = set()
    for origin_ordinal in fts_origin_ordinals:
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids_by_ordinal
            or origin_ordinal not in folded_sources_by_ordinal
            or origin_ordinal in seen_fts_ordinals
        ):
            raise ValueError("validated FTS row is invalid")
        seen_fts_ordinals.add(origin_ordinal)
    copied_record_ids = tuple(record_ids_by_ordinal.items())
    copied_folded_sources = tuple(folded_sources_by_ordinal.items())
    gram_result = candidate_projection.insert_candidate_gram_rows(
        connection,
        plan,
        record_ids_by_ordinal=copied_record_ids,
        folded_sources_by_ordinal=copied_folded_sources,
    )
    if gram_result is not None:
        raise TypeError("candidate write projection returned authority")
    try:
        fts_result = candidate_projection.insert_candidate_fts_rows(
            connection,
            plan,
            record_ids_by_ordinal=copied_record_ids,
            folded_sources_by_ordinal=copied_folded_sources,
        )
    except sqlite3.OperationalError as error:
        raise SQLiteStoreSchemaError("STORE.FTS5_UNAVAILABLE") from error
    if fts_result is not None:
        raise TypeError("candidate write projection returned authority")
    proof_result = _maintain_candidate_proof_summaries(
        connection,
        plan=plan,
        record_ids_by_ordinal=record_ids_by_ordinal,
        folded_sources_by_ordinal=folded_sources_by_ordinal,
    )
    if proof_result is not None:
        raise TypeError("candidate write projection returned authority")


def _maintain_candidate_proof_summaries(
    connection: sqlite3.Connection,
    *,
    plan: _ValidatedCandidateWritePlan,
    record_ids_by_ordinal: dict[int, int],
    folded_sources_by_ordinal: dict[int, str],
) -> object:
    """Late-bound compatibility wrapper for proof-summary projection."""

    return candidate_projection.maintain_candidate_proof_summaries(
        connection,
        plan=plan,
        record_ids_by_ordinal=tuple(record_ids_by_ordinal.items()),
        folded_sources_by_ordinal=tuple(folded_sources_by_ordinal.items()),
    )


_CANDIDATE_PROJECTION_DIGEST_VERSION = "candidate-projection-digest-v2"
_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS = 50_000


def _candidate_proof_projection_digest(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
) -> str:
    return candidate_projection.candidate_proof_projection_digest(
        connection,
        fts5_available=fts5_available,
        gram_chunk_rows=_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
    )


def _validate_candidate_proof_index_core(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
    include_projection_digest: bool,
) -> tuple[tuple[tuple[int, int], ...], int, str | None]:
    return candidate_projection._validate_candidate_proof_index_core(
        connection,
        required_sizes=required_sizes,
        fts5_available=fts5_available,
        include_projection_digest=include_projection_digest,
        gram_chunk_rows=_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS,
    )


def _validate_candidate_proof_index_with_digest(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
) -> tuple[tuple[tuple[int, int], ...], int, str]:
    gram_counts, fts_count, projection_digest = (
        _validate_candidate_proof_index_core(
            connection,
            required_sizes=required_sizes,
            fts5_available=fts5_available,
            include_projection_digest=True,
        )
    )
    if type(projection_digest) is not str:
        raise AssertionError("candidate projection digest is missing")
    return gram_counts, fts_count, projection_digest


def validate_candidate_proof_index(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
) -> tuple[tuple[tuple[int, int], ...], int]:
    """Late-bound compatibility wrapper for proof-index validation."""

    gram_counts, fts_count, projection_digest = (
        _validate_candidate_proof_index_core(
            connection,
            required_sizes=required_sizes,
            fts5_available=fts5_available,
            include_projection_digest=False,
        )
    )
    if projection_digest is not None:
        raise AssertionError("candidate projection digest is unexpected")
    return gram_counts, fts_count


def _validate_candidate_index_before_publication(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
) -> None:
    """Authorize the complete candidate index before batch/head publication."""

    try:
        validate_candidate_proof_index(
            connection,
            required_sizes=(1, 2) if fts5_available else (1, 2, 3),
            fts5_available=fts5_available,
        )
    except (CandidateProofIndexError, sqlite3.DatabaseError) as error:
        raise SQLiteStoreSchemaError(
            "STORE.CANDIDATE_INDEX_INVALID"
        ) from error


def _suspend_streamed_stage_secondary_indexes(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
) -> None:
    """Drop only rebuildable secondary indexes on one empty private stage."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        identity = lease.stage.resource_identity
        _validate_store_identity(
            connection,
            resource_id=identity.resource_id,
            canonical_store_id=lease.canonical_store_id,
            target_identity=identity.target_identity,
        )
        meta = _read_meta(connection)
        if (
            meta.get("activation_status") != "UNPUBLISHED"
            or _meta_int(meta, "head_revision") != 0
            or lease.active_content_attestation is not None
            or connection.execute(
                "SELECT COUNT(*) FROM tm_origin_batch"
            ).fetchone()
            != (0,)
            or connection.execute("SELECT COUNT(*) FROM tm_record").fetchone()
            != (0,)
        ):
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_STATE_INVALID"
            )
        try:
            actual = (
                candidate_projection.streamed_stage_secondary_index_inventory(
                    connection
                )
            )
        except CandidateProofIndexError as error:
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_INDEX_INVALID"
            ) from error
        if actual != tuple(sorted(_STREAMED_STAGE_SECONDARY_INDEX_NAMES)):
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_INDEX_INVALID"
            )
        candidate_projection.suspend_streamed_stage_secondary_indexes(
            connection
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _restore_streamed_stage_secondary_indexes(
    connection: sqlite3.Connection,
    lease: _SQLiteGenerationView,
    *,
    before_publication: bool = False,
) -> None:
    """Bulk-build the exact frozen secondary-index schema before exposure."""

    if type(before_publication) is not bool:
        raise TypeError("before_publication must be a built-in bool")
    if before_publication:
        if not connection.in_transaction:
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_STATE_INVALID"
            )
    else:
        if connection.in_transaction:
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_STATE_INVALID"
            )
        connection.execute("BEGIN IMMEDIATE")
    try:
        identity = lease.stage.resource_identity
        _validate_store_identity(
            connection,
            resource_id=identity.resource_id,
            canonical_store_id=lease.canonical_store_id,
            target_identity=identity.target_identity,
        )
        meta = _read_meta(connection)
        if (
            meta.get("activation_status") != "UNPUBLISHED"
            or _meta_int(meta, "head_revision")
            != (0 if before_publication else 1)
            or lease.active_content_attestation is not None
        ):
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_STATE_INVALID"
            )
        try:
            existing = (
                candidate_projection.streamed_stage_secondary_index_inventory(
                    connection
                )
            )
        except CandidateProofIndexError as error:
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_INDEX_INVALID"
            ) from error
        if existing != ():
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_INDEX_INVALID"
            )
        candidate_projection.restore_streamed_stage_secondary_indexes(
            connection
        )
        fts5_available = _meta_bool(meta, "fts5_available")
        if _schema_digest(
            connection,
            fts5_available=fts5_available,
        ) != _APPROVED_SCHEMA_DIGESTS[fts5_available]:
            raise SQLiteStoreSchemaError(
                "STORE.STREAMED_BUILD_INDEX_INVALID"
            )
        if not before_publication:
            connection.commit()
    except BaseException:
        if not before_publication:
            connection.rollback()
        raise


def _next_draft_chunk(
    drafts: Iterator[tuple[TMRecordDraft, int | None]],
    chunk_size: int,
) -> tuple[tuple[TMRecordDraft, int | None], ...]:
    """Pull one bounded chunk from the draft stream with exact shape checks."""

    chunk = tuple(itertools.islice(drafts, chunk_size))
    for pair in chunk:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError("draft stream entries must be (draft, line) pairs")
        draft, line_number = pair
        if type(draft) is not TMRecordDraft:
            raise TypeError(
                "draft stream must contain exact TMRecordDraft values"
            )
        if line_number is not None and type(line_number) is not int:
            raise TypeError("draft stream line numbers must be int or None")
    return chunk


def _insert_prepared_records_and_indexes(
    connection: sqlite3.Connection,
    prepared_drafts: tuple[_PreparedRecordDraft, ...],
    validated_candidate_plan: _ValidatedCandidateWritePlan,
    *,
    batch_id: str,
) -> tuple[TMRecord, ...]:
    """Insert one bounded prepared batch with its candidate indexes."""

    inserted: list[TMRecord] = []
    record_ids_by_ordinal: dict[int, int] = {}
    for draft in prepared_drafts:
        (
            source_raw,
            target_raw,
            source_fold_v1,
            speaker_raw,
            context_prev_raw,
            context_next_raw,
            file_source,
            provenance,
            provenance_json,
            legacy_line_no,
            origin_ordinal,
        ) = draft
        cursor = connection.execute(
            "INSERT INTO tm_record("
            "source_raw, target_raw, source_fold_v1, source_fold_length, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, "
            "origin_batch_id, origin_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_raw,
                target_raw,
                source_fold_v1,
                len(source_fold_v1),
                speaker_raw,
                context_prev_raw,
                context_next_raw,
                file_source,
                provenance_json,
                legacy_line_no,
                batch_id,
                origin_ordinal,
            ),
        )
        record_id = cursor.lastrowid
        if record_id is None:
            raise SQLiteStoreSchemaError("STORE.RECORD_ID_MISSING")
        record_ids_by_ordinal[origin_ordinal] = record_id
        inserted.append(
            TMRecord(
                record_id=record_id,
                source_raw=source_raw,
                target_raw=target_raw,
                speaker_raw=speaker_raw,
                context_prev_raw=context_prev_raw,
                context_next_raw=context_next_raw,
                file_source=file_source,
                provenance=provenance,
                legacy_line_no=legacy_line_no,
                origin_batch_id=batch_id,
                origin_ordinal=origin_ordinal,
            )
        )
    _apply_candidate_write_plan(
        connection,
        validated_candidate_plan,
        record_ids_by_ordinal=record_ids_by_ordinal,
        folded_sources_by_ordinal={
            draft[10]: draft[2]
            for draft in prepared_drafts
        },
    )
    return tuple(inserted)


def _insert_streamed_chunk(
    connection: sqlite3.Connection,
    chunk: tuple[tuple[TMRecordDraft, int | None], ...],
    *,
    batch_id: str,
    fts5_available: bool,
    ordinal_offset: int,
) -> None:
    """Validate and insert one bounded streamed chunk in its transaction.

    Candidate rows are constructed directly in primary-key order (bounded by
    the chunk) instead of materializing a whole-generation write plan, so
    memory stays flat while the bulk insert stays btree-friendly.
    """

    draft_values = tuple(pair[0] for pair in chunk)
    line_numbers = tuple(pair[1] for pair in chunk)
    for draft in draft_values:
        _validate_draft_exact(draft)
    for line_number in line_numbers:
        _validate_legacy_line_number(line_number)
    prepared_drafts = _prepare_record_drafts(
        draft_values,
        line_numbers,
        ordinal_offset=ordinal_offset,
    )
    record_ids_by_ordinal = _insert_streamed_records(
        connection,
        prepared_drafts,
        batch_id=batch_id,
    )
    _insert_streamed_candidate_index(
        connection,
        prepared_drafts,
        record_ids_by_ordinal,
        fts5_available=fts5_available,
    )


def _insert_streamed_records(
    connection: sqlite3.Connection,
    prepared_drafts: tuple[_PreparedRecordDraft, ...],
    *,
    batch_id: str,
) -> dict[int, int]:
    """Insert one bounded chunk of records and return ordinal-to-id mapping."""

    if not prepared_drafts:
        return {}
    before_row = connection.execute(
        "SELECT COUNT(*), COALESCE(MAX(record_id), 0) FROM tm_record"
    ).fetchone()
    if (
        type(before_row) is not tuple
        or len(before_row) != 2
        or type(before_row[0]) is not int
        or before_row[0] < 0
        or type(before_row[1]) is not int
        or before_row[1] < 0
        or before_row[0] != before_row[1]
    ):
        raise SQLiteStoreSchemaError("STORE.RECORD_ID_SEQUENCE_INVALID")
    prior_count, prior_max_record_id = before_row
    connection.executemany(
        "INSERT INTO tm_record("
        "source_raw, target_raw, source_fold_v1, source_fold_length, "
        "speaker_raw, context_prev_raw, context_next_raw, "
        "file_source, provenance_json, legacy_line_no, "
        "origin_batch_id, origin_ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                draft[0],
                draft[1],
                draft[2],
                len(draft[2]),
                draft[3],
                draft[4],
                draft[5],
                draft[6],
                draft[8],
                draft[9],
                batch_id,
                draft[10],
            )
            for draft in prepared_drafts
        ),
    )
    after_row = connection.execute(
        "SELECT COUNT(*), COALESCE(MAX(record_id), 0), "
        "last_insert_rowid() FROM tm_record"
    ).fetchone()
    expected_last_record_id = prior_max_record_id + len(prepared_drafts)
    if (
        type(after_row) is not tuple
        or len(after_row) != 3
        or type(after_row[0]) is not int
        or type(after_row[1]) is not int
        or type(after_row[2]) is not int
        or after_row[0] != prior_count + len(prepared_drafts)
        or after_row[1] != expected_last_record_id
        or after_row[2] != expected_last_record_id
    ):
        raise SQLiteStoreSchemaError("STORE.RECORD_ID_SEQUENCE_INVALID")
    record_ids_by_ordinal: dict[int, int] = {}
    first_record_id = prior_max_record_id + 1
    for offset, draft in enumerate(prepared_drafts):
        origin_ordinal = draft[10]
        record_id = first_record_id + offset
        if origin_ordinal in record_ids_by_ordinal:
            raise SQLiteStoreSchemaError("STORE.RECORD_ID_SEQUENCE_INVALID")
        record_ids_by_ordinal[origin_ordinal] = record_id
    return record_ids_by_ordinal


def _insert_streamed_candidate_index(
    connection: sqlite3.Connection,
    prepared_drafts: tuple[_PreparedRecordDraft, ...],
    record_ids_by_ordinal: dict[int, int],
    *,
    fts5_available: bool,
) -> None:
    """Late-bound wrapper for one bounded streamed candidate projection."""

    candidate_records = tuple(
        (draft[10], draft[2]) for draft in prepared_drafts
    )
    copied_record_ids = tuple(record_ids_by_ordinal.items())
    try:
        projection_result = (
            candidate_projection._insert_streamed_candidate_projection(
                connection,
                candidate_records,
                copied_record_ids,
                fts5_available=fts5_available,
            )
        )
    except sqlite3.OperationalError as error:
        if (
            type(error) is sqlite3.OperationalError
            and len(error.args) == 1
            and error.args[0] is candidate_projection._STREAMED_FTS_WRITE_FAILED
        ):
            raise SQLiteStoreSchemaError("STORE.FTS5_UNAVAILABLE") from error
        raise
    if projection_result is not None:
        raise TypeError("streamed candidate projection returned authority")


def _complete_streamed_batch(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    completed_revision: int,
    prior_head_revision: int,
    invalid_count: int,
    duplicate_source_count: int,
) -> None:
    """Close the staged batch with final counts and the head revision."""

    completed_batch = connection.execute(
        "UPDATE tm_origin_batch "
        "SET status = 'completed', completed_revision = ?, "
        "valid_count = (SELECT COUNT(*) FROM tm_record "
        "WHERE origin_batch_id = ?), "
        "invalid_count = ?, duplicate_source_count = ? "
        "WHERE batch_id = ? AND status = 'staged'",
        (
            completed_revision,
            batch_id,
            invalid_count,
            duplicate_source_count,
            batch_id,
        ),
    )
    if completed_batch.rowcount != 1:
        raise SQLiteStoreSchemaError("STORE.BATCH_COMPLETION_MISSING")
    updated = connection.execute(
        "UPDATE tm_meta SET value = ? "
        "WHERE key = 'head_revision' AND value = ?",
        (str(completed_revision), str(prior_head_revision)),
    )
    if updated.rowcount != 1:
        raise SQLiteStoreSchemaError("STORE.HEAD_REVISION_MISSING")


def _validate_batch_arguments(
    *,
    batch_id: str,
    kind: str,
    drafts: tuple[TMRecordDraft, ...],
    source_digest: str | None,
    source_path: Path | None,
    legacy_line_nos: tuple[int | None, ...] | None,
    invalid_count: int,
    duplicate_source_count: int,
    created_at: str | None,
    extension: object,
) -> None:
    _validate_batch_scalars(
        batch_id=batch_id,
        kind=kind,
        source_digest=source_digest,
        source_path=source_path,
        invalid_count=invalid_count,
        duplicate_source_count=duplicate_source_count,
        created_at=created_at,
        extension=extension,
    )
    if type(drafts) is not tuple:
        raise TypeError("drafts must be a built-in tuple")
    for draft in drafts:
        _validate_draft_exact(draft)
    if kind == "local_write":
        if len(drafts) != 1:
            raise ValueError("local_write batch must contain one draft")
    if legacy_line_nos is not None:
        if type(legacy_line_nos) is not tuple:
            raise TypeError("legacy_line_nos must be a built-in tuple or None")
        if len(legacy_line_nos) != len(drafts):
            raise ValueError("legacy_line_nos must align with drafts")
        for line_number in legacy_line_nos:
            _validate_legacy_line_number(line_number)


def _validate_batch_scalars(
    *,
    batch_id: str,
    kind: str,
    source_digest: str | None,
    source_path: Path | None,
    invalid_count: int,
    duplicate_source_count: int,
    created_at: str | None,
    extension: object,
) -> None:
    if type(batch_id) is not str:
        raise TypeError("batch_id must be a built-in string")
    if not batch_id.strip():
        raise ValueError("batch_id must not be empty")
    if type(kind) is not str:
        raise TypeError("kind must be a built-in string")
    if kind not in {"migration", "local_write", "import"}:
        raise ValueError("kind must be migration, local_write, or import")
    if kind == "local_write":
        if source_digest is not None or source_path is not None:
            raise ValueError("local_write batch cannot bind a source")
    else:
        if type(source_digest) is not str:
            raise TypeError("source_digest must be a built-in string")
        if (
            len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)
        ):
            raise ValueError("source_digest must be a lowercase SHA-256 digest")
        if type(source_path) is not _NATIVE_PATH_TYPE:
            raise TypeError("source_path must be an exact native Path")
        assert source_path is not None
        if not source_path.is_absolute():
            raise ValueError("source_path must be absolute")
    for value, field_name in (
        (invalid_count, "invalid_count"),
        (duplicate_source_count, "duplicate_source_count"),
    ):
        if type(value) is not int:
            raise TypeError(f"{field_name} must be a built-in integer")
        if value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    if created_at is not None:
        if type(created_at) is not str:
            raise TypeError("created_at must be a built-in string or None")
        if not created_at.strip():
            raise ValueError("created_at must be a non-empty string or None")
    if extension is not None and not callable(extension):
        raise TypeError("extension must be callable or None")


def _validate_draft_exact(draft: object) -> None:
    if type(draft) is not TMRecordDraft:
        raise TypeError("drafts must contain exact TMRecordDraft values")
    if type(draft.source_raw) is not str:
        raise TypeError("draft source_raw must be a built-in string")
    if not draft.source_raw:
        raise ValueError("draft source_raw must not be empty")
    if type(draft.target_raw) is not str:
        raise TypeError("draft target_raw must be a built-in string")
    if not draft.target_raw:
        raise ValueError("draft target_raw must not be empty")
    for value, field_name in (
        (draft.speaker_raw, "speaker_raw"),
        (draft.context_prev_raw, "context_prev_raw"),
        (draft.context_next_raw, "context_next_raw"),
        (draft.file_source, "file_source"),
    ):
        if value is not None and type(value) is not str:
            raise TypeError(
                f"draft {field_name} must be a built-in string or None"
            )
    if type(draft.provenance) is not tuple:
        raise TypeError("draft provenance must be a built-in tuple")
    for pair in draft.provenance:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError(
                "draft provenance entries must be exact two-item tuples"
            )
        key, value = pair
        if type(key) is not str or type(value) is not str:
            raise TypeError(
                "draft provenance keys and values must be built-in strings"
            )
        if not key.strip():
            raise ValueError("draft provenance keys must not be empty")


def _validate_legacy_line_number(line_number: object) -> None:
    if line_number is None:
        return
    if type(line_number) is not int:
        raise TypeError("legacy line numbers must be built-in integers or None")
    if line_number < 1:
        raise ValueError("legacy line numbers must be positive integers")


def _source_binding_health_state(
    facts: _SourceBindingFacts,
) -> tuple[SourceBindingState | None, tuple[str, ...]]:
    """Classify one canonical ledger read without latching or JSONL checks."""

    diagnostics = list(facts.diagnostic_codes)
    if facts.divergence_latched:
        diagnostics.append("SOURCE_BINDING.DIVERGENCE_LATCHED")
    if facts.divergence_latched or facts.diagnostic_codes:
        state = SourceBindingState.SOURCE_DIVERGED
    elif facts.binding is None:
        state = None
    elif facts.binding.receipt.exported_revision == facts.head_revision:
        state = SourceBindingState.VERIFIED_CURRENT
    else:
        state = SourceBindingState.VERIFIED_HISTORY
    return state, tuple(sorted(set(diagnostics)))


def _active_attestation_for_health(
    lease: _SQLiteGenerationView,
) -> ActiveContentAttestation | PortableActiveContentAttestation | None:
    """Re-prove one immutable active byte set for semantic-health reuse.

    A legitimate post-activation write changes the database bytes and falls
    back to the full runtime validator.  Replacing the canonical database
    inode is never such a write and fails closed even when its bytes are
    identical.  The configured JSONL/manifest pair is intentionally outside
    this reuse decision: SourceBindingMonitor owns its divergence semantics.
    """

    active = lease.active_content_attestation
    if active is None:
        return None
    identity = lease.stage.resource_identity
    if type(active) is PortableActiveContentAttestation:
        if (
            active.resource_id != identity.resource_id
            or active.target_identity != identity.target_identity
            or active.canonical_store_id != lease.canonical_store_id
            or active.generation != lease.generation
        ):
            raise SQLiteStoreSchemaError("STORE.ACTIVE_ATTESTATION_INVALID")
        return active
    if type(active) is not ActiveContentAttestation or (
        active.resource_id != identity.resource_id
        or active.target_identity != identity.target_identity
        or active.canonical_store_id != lease.canonical_store_id
        or active.generation != lease.generation
    ):
        raise SQLiteStoreSchemaError("STORE.ACTIVE_ATTESTATION_INVALID")
    try:
        database = _capture_content_file(identity.canonical_sidecar_path)
    except ContentAttestationError as error:
        raise SQLiteStoreSchemaError(
            "STORE.ACTIVE_ATTESTATION_INVALID"
        ) from error
    if (database.device, database.inode) != (
        active.database.device,
        active.database.inode,
    ):
        raise SQLiteStoreSchemaError(
            "STORE.ACTIVE_ATTESTATION_INVALID"
        )
    if database != active.database:
        return None
    return active


def _rebind_portable_attestation_for_health(
    lease: _SQLiteGenerationView,
    active: PortableActiveContentAttestation,
) -> PortableActiveContentAttestation | None:
    """Bind portable semantic facts to the current rooted three-file set.

    Portable attestations deliberately persist only byte count and SHA-256.
    A health call may therefore reuse their semantic facts only after fresh
    platform-rooted handles prove the current database, manifest, and source
    bytes.  File identities are live facts for this capture window only; they
    are never compared with or written into the durable attestation.

    A stable byte difference is an ordinary cache miss (for example, a legal
    canonical append) and falls back to the full SQLite validator.  A rooted
    authority failure or retained-handle race on the canonical database
    invalidates the attestation.  The configured manifest and source remain
    observable inputs, so any failure to prove that optional pair is only a
    cache miss; ``SourceBindingMonitor`` owns its divergence classification.
    """

    identity = lease.stage.resource_identity
    root_path = identity.configured_jsonl_path.parent
    expected = (
        (identity.canonical_sidecar_path, active.database),
        (identity.snapshot_manifest_path, active.manifest),
        (identity.configured_jsonl_path, active.source),
    )
    if any(path.parent != root_path for path, _proof in expected):
        raise SQLiteStoreSchemaError("STORE.ACTIVE_ATTESTATION_INVALID")
    try:
        backend = importlib.import_module(
            "platform_fs"
        ).compose_platform_file_backend(root_path)
    except (PlatformFileError, OSError) as error:
        raise SQLiteStoreSchemaError(
            "STORE.ACTIVE_ATTESTATION_INVALID"
        ) from error

    captures: list[tuple[str, OpaqueAuthority]] = []
    reuse_semantic_facts = True
    try:
        database_path, database_proof = expected[0]
        try:
            database_capture = _capture_platform_content_file(
                backend,
                root_path,
                PurePath(database_path.name),
            )
            captures.append(("database", database_capture))
            database_capture.reprove()
        except (ContentAttestationError, PlatformFileError, OSError) as error:
            raise SQLiteStoreSchemaError(
                "STORE.ACTIVE_ATTESTATION_INVALID"
            ) from error
        if database_capture.persisted_proof() != database_proof:
            reuse_semantic_facts = False

        for path, proof in expected[1:]:
            if not reuse_semantic_facts:
                break
            try:
                capture = _capture_platform_content_file(
                    backend,
                    root_path,
                    PurePath(path.name),
                )
                captures.append(("pair", capture))
                capture.reprove()
            except (ContentAttestationError, PlatformFileError, OSError):
                reuse_semantic_facts = False
                break
            if capture.persisted_proof() != proof:
                reuse_semantic_facts = False
    finally:
        active_error = sys.exception()
        database_close_error: BaseException | None = None
        for role, capture in reversed(captures):
            try:
                capture.close()
            except (ContentAttestationError, PlatformFileError, OSError) as error:
                if role == "database" and database_close_error is None:
                    database_close_error = error
                else:
                    reuse_semantic_facts = False
        if active_error is None and database_close_error is not None:
            raise SQLiteStoreSchemaError(
                "STORE.ACTIVE_ATTESTATION_INVALID"
            ) from database_close_error
    return active if reuse_semantic_facts else None


def _store_health_body(lease: _SQLiteGenerationView) -> StoreHealth:
    """Build one health snapshot from one already-held canonical lease."""

    active_attestation = _active_attestation_for_health(lease)
    with _open_health_connection(lease) as connection:
        connection.execute("BEGIN")
        try:
            identity = lease.stage.resource_identity
            _validate_store_identity(
                connection,
                resource_id=identity.resource_id,
                canonical_store_id=lease.canonical_store_id,
                target_identity=identity.target_identity,
            )
            meta = _read_meta(connection)
            schema_version = _meta_int(meta, "schema_version")
            generation = _meta_int(meta, "generation")
            if generation != lease.generation:
                raise SQLiteStoreSchemaError("STORE.GENERATION_MISMATCH")
            activation_status = meta["activation_status"]
            if type(activation_status) is not str:
                raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE")
            index_kind = meta["candidate_index_kind"]
            if type(index_kind) is not str or not index_kind.strip():
                raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE")
            if type(active_attestation) is PortableActiveContentAttestation:
                active_attestation = _rebind_portable_attestation_for_health(
                    lease,
                    active_attestation,
                )
            if (
                schema_version == TM_SCHEMA_VERSION
                and active_attestation is None
            ):
                fts5_available = _meta_bool(meta, "fts5_available")
                try:
                    validate_candidate_proof_index(
                        connection,
                        required_sizes=(
                            (1, 2) if fts5_available else (1, 2, 3)
                        ),
                        fts5_available=fts5_available,
                    )
                except (CandidateProofIndexError, sqlite3.DatabaseError) as error:
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_INDEX_INVALID"
                    ) from error
            facts = _read_source_binding_facts_in_transaction(
                connection,
                lease,
            )
            if active_attestation is not None:
                semantic = active_attestation.semantic_facts
                binding = facts.binding
                if (
                    schema_version != semantic.schema_version
                    or meta.get("schema_digest") != semantic.schema_digest
                    or meta.get("fold_version") != semantic.fold_version
                    or meta.get("candidate_index_version")
                    != semantic.index_version
                    or index_kind != semantic.candidate_index_kind
                    or _meta_bool(meta, "fts5_available")
                    != semantic.fts5_available
                    or meta.get("activation_digest")
                    != active_attestation.activation_digest
                    or facts.record_count != semantic.record_count
                    or binding is None
                    or snapshot_receipt_digest(binding.receipt)
                    != active_attestation.snapshot_receipt_digest
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_ATTESTATION_INVALID"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    state, binding_codes = _source_binding_health_state(facts)
    exact_available = activation_status == "ACTIVE"
    availability_codes = (
        set()
        if exact_available
        else {"STORE.CANONICAL_NOT_ACTIVE"}
    )
    diagnostic_codes = tuple(
        sorted(
            set(binding_codes)
            | availability_codes
        )
    )
    return StoreHealth(
        healthy=True,
        schema_version=schema_version,
        generation=lease.generation,
        record_count=facts.record_count,
        index_kind=index_kind,
        snapshot_binding_digest=(
            None
            if facts.binding is None
            else _snapshot_binding_digest(facts.binding)
        ),
        source_binding_state=state,
        exact_available=exact_available,
        context_available=False,
        fuzzy_available=False,
        diagnostic_codes=diagnostic_codes,
    )


class SQLiteTMQueryView:
    """Read-only exact-typed query view bound to one operation lease.

    Constructible only by :meth:`SQLiteTMStore.query_lease`; the view
    holds exactly one coordinator operation lease for its lifetime and
    exposes no append/export/activation/update port.  Every operation
    revalidates the view lifetime/identity before touching the DB, and
    the view never acquires a lease itself.
    """

    def __init__(
        self,
        store: SQLiteTMStore,
        lease: _SQLiteGenerationView,
        *,
        token: object,
    ) -> None:
        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore")
        if type(lease) is not _SQLiteGenerationView:
            raise TypeError("lease must be exact _SQLiteGenerationView")
        with store._coordinator._condition:
            if token not in store._query_view_tokens:
                raise TypeError("query view is not directly constructible")
        self._store = store
        self._coordinator = store._coordinator
        self._lease = lease
        self._token = token
        self._expired = False
        self._connection_manager: (
            AbstractContextManager[sqlite3.Connection] | None
        ) = None
        self._connection: sqlite3.Connection | None = None

    def _invalidate(self) -> None:
        manager = self._connection_manager
        self._connection = None
        self._connection_manager = None
        if manager is not None:
            manager.__exit__(None, None, None)
        self._expired = True

    def _candidate_connection(self) -> sqlite3.Connection:
        self._check_lifetime()
        connection = self._connection
        if connection is None:
            manager = _open_leased_connection(self._lease)
            connection = manager.__enter__()
            try:
                worker_row = connection.execute("PRAGMA threads=1").fetchone()
            except sqlite3.Error as error:
                manager.__exit__(None, None, None)
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_THREADS_INVALID"
                ) from error
            except BaseException:
                manager.__exit__(None, None, None)
                raise
            if (
                type(worker_row) is not tuple
                or len(worker_row) != 1
                or type(worker_row[0]) is not int
                or worker_row[0] not in {0, 1}
            ):
                manager.__exit__(None, None, None)
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_THREADS_INVALID"
                )
            self._connection_manager = manager
            self._connection = connection
        if connection.in_transaction:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        return connection

    def _check_lifetime(self) -> None:
        resource_id = self._lease.stage.resource_identity.resource_id
        generation = self._lease.generation
        if self._expired:
            raise SQLiteStoreLifecycleError(
                "STORE.QUERY_VIEW_EXPIRED",
                resource_id=resource_id,
                generation=generation,
                retryable=False,
            )
        if self._store._coordinator is not self._coordinator:
            raise SQLiteStoreSchemaError("STORE.QUERY_VIEW_FOREIGN")
        with self._coordinator._condition:
            if self._token not in self._store._query_view_tokens:
                raise SQLiteStoreLifecycleError(
                    "STORE.QUERY_VIEW_EXPIRED",
                    resource_id=resource_id,
                    generation=generation,
                    retryable=False,
                )
            if self._coordinator._view is not self._lease:
                raise SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id=resource_id,
                    generation=generation,
                    retryable=True,
                )

    @property
    def resource_id(self) -> str:
        self._check_lifetime()
        return self._lease.stage.resource_identity.resource_id

    @property
    def candidate_port_scope(self) -> str:
        self._check_lifetime()
        return "QUERY_VIEW"

    @property
    def generation(self) -> int:
        self._check_lifetime()
        return self._lease.generation

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        self._check_lifetime()
        _validate_exact_records_input(source_raw)
        return _exact_records_body(self._lease, source_raw)

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        self._check_lifetime()
        prepared_ids = _validate_records_by_id_input(record_ids)
        return _records_by_id_body(
            self._lease,
            prepared_ids,
            connection=self._candidate_connection(),
        )

    def candidate_recall_snapshot(
        self,
        *,
        fts_query_trigrams: tuple[str, ...] | None,
        query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...],
        candidate_floor: int,
        fts_query_degenerate: bool,
    ) -> SQLiteCandidateRecallSnapshot:
        """Read one complete candidate-stage snapshot on the captured lease."""

        self._check_lifetime()
        prepared = _prepare_candidate_recall_inputs(
            fts_query_trigrams=fts_query_trigrams,
            query_grams_by_size=query_grams_by_size,
            candidate_floor=candidate_floor,
            fts_query_degenerate=fts_query_degenerate,
        )
        return _candidate_recall_snapshot_body(self._lease, prepared)

    def candidate_proof_snapshot(
        self,
        *,
        folded_query: str,
        seed_limit: int,
    ) -> SQLiteCandidateProofSnapshot:
        """Return one source-free proof snapshot on this captured view."""

        self._check_lifetime()
        if type(folded_query) is not str or not folded_query:
            raise ValueError("folded_query must be a non-empty built-in string")
        if type(seed_limit) is not int or not 1 <= seed_limit <= 8192:
            raise ValueError("seed_limit is outside the safe range")
        return _candidate_proof_snapshot_body(
            self._lease,
            folded_query=folded_query,
            seed_limit=seed_limit,
            connection=self._candidate_connection(),
        )

    def validate_candidate_proof_generation(
        self,
        *,
        head_revision: int,
        total_record_count: int,
    ) -> None:
        """Fail closed if an append changed the proof universe in flight."""

        self._check_lifetime()
        if type(head_revision) is not int or head_revision < 0:
            raise ValueError("head_revision must be a non-negative integer")
        if type(total_record_count) is not int or total_record_count < 0:
            raise ValueError("total_record_count must be non-negative")
        _validate_candidate_proof_generation_body(
            self._lease,
            head_revision=head_revision,
            total_record_count=total_record_count,
            connection=self._candidate_connection(),
        )

    def candidate_proof_block_records(
        self,
        *,
        folded_query: str,
        block: SQLiteCandidateProofBlock,
        head_revision: int,
        total_record_count: int,
    ) -> tuple[SQLiteCandidateProofRecord, ...]:
        """Load exact proof facts only when the proof engine opens one block."""

        self._check_lifetime()
        if type(folded_query) is not str or not folded_query:
            raise ValueError("folded_query must be a non-empty built-in string")
        if type(block) is not SQLiteCandidateProofBlock:
            raise TypeError("block must be an exact SQLiteCandidateProofBlock")
        if type(head_revision) is not int or head_revision < 0:
            raise ValueError("head_revision must be a non-negative integer")
        if type(total_record_count) is not int or total_record_count < 0:
            raise ValueError("total_record_count must be non-negative")
        return _candidate_proof_block_records_body(
            self._lease,
            folded_query=folded_query,
            block=block,
            head_revision=head_revision,
            total_record_count=total_record_count,
            connection=self._candidate_connection(),
        )

    def candidate_proof_dense_phase1(
        self,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
    ) -> SQLiteCandidateProofDensePhase1:
        """Return committed dense phase-one length and bigram facts."""

        self._check_lifetime()
        if type(folded_query) is not str or not folded_query:
            raise ValueError("folded_query must be a non-empty built-in string")
        if type(blocks) is not tuple or any(
            type(block) is not SQLiteCandidateProofBlock for block in blocks
        ):
            raise TypeError("blocks must contain exact proof blocks")
        if type(head_revision) is not int or head_revision < 0:
            raise ValueError("head_revision must be a non-negative integer")
        if type(total_record_count) is not int or total_record_count < 0:
            raise ValueError("total_record_count must be non-negative")
        if (
            type(query_maxima_digest) is not str
            or len(query_maxima_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in query_maxima_digest
            )
        ):
            raise ValueError("query_maxima_digest must be a SHA-256 digest")
        prepared_blocks = tuple(
            SQLiteCandidateProofBlock(
                block_id=block.block_id,
                first_record_id=block.first_record_id,
                last_record_id=block.last_record_id,
                record_count=block.record_count,
                min_source_fold_length=block.min_source_fold_length,
                max_source_fold_length=block.max_source_fold_length,
                character_intersection_upper=(
                    block.character_intersection_upper
                ),
                bigram_intersection_upper=block.bigram_intersection_upper,
            )
            for block in blocks
        )
        return _candidate_proof_dense_phase1_body(
            self._lease,
            folded_query=folded_query,
            blocks=prepared_blocks,
            head_revision=head_revision,
            total_record_count=total_record_count,
            query_maxima_digest=query_maxima_digest,
            connection=self._candidate_connection(),
        )

    def candidate_proof_dense_phase2(
        self,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
        binding_digest: str,
        record_ids: tuple[int, ...],
        source_fold_lengths: tuple[int, ...],
    ) -> SQLiteCandidateProofDensePhase2:
        """Return the proof-only folded projection for the ordered R set."""

        self._check_lifetime()
        if type(folded_query) is not str or not folded_query:
            raise ValueError("folded_query must be a non-empty built-in string")
        if type(blocks) is not tuple or any(
            type(block) is not SQLiteCandidateProofBlock for block in blocks
        ):
            raise TypeError("blocks must contain exact proof blocks")
        if type(head_revision) is not int or head_revision < 0:
            raise ValueError("head_revision must be a non-negative integer")
        if type(total_record_count) is not int or total_record_count < 0:
            raise ValueError("total_record_count must be non-negative")
        for label, digest in (
            ("query maxima", query_maxima_digest),
            ("phase binding", binding_digest),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{label} digest must be a SHA-256 digest")
        if type(record_ids) is not tuple:
            raise ValueError("refinement record ids are invalid")
        if type(source_fold_lengths) is not tuple:
            raise ValueError("refinement source lengths are invalid")
        prepared_blocks = tuple(SQLiteCandidateProofBlock(**block.__dict__) for block in blocks)
        return _candidate_proof_dense_phase2_body(
            self._lease,
            folded_query=folded_query,
            blocks=prepared_blocks,
            head_revision=head_revision,
            total_record_count=total_record_count,
            query_maxima_digest=query_maxima_digest,
            binding_digest=binding_digest,
            record_ids=tuple(record_ids),
            source_fold_lengths=tuple(source_fold_lengths),
            connection=self._candidate_connection(),
        )

    def validate_candidate_proof_dense_phase1_result(
        self,
        value: object,
        *,
        folded_query: str,
        blocks: tuple[SQLiteCandidateProofBlock, ...],
        head_revision: int,
        total_record_count: int,
        query_maxima_digest: str,
    ) -> None:
        """Validate a dense phase-one result on this captured generation."""

        _validate_candidate_proof_dense_phase1_result(
            value,
            view=self,
            folded_query=folded_query,
            blocks=blocks,
            head_revision=head_revision,
            total_record_count=total_record_count,
            query_maxima_digest=query_maxima_digest,
        )

    def validate_candidate_proof_dense_phase2_result(
        self,
        value: object,
        *,
        binding_digest: str,
        record_ids: tuple[int, ...],
        source_fold_lengths: tuple[int, ...],
    ) -> None:
        """Validate a dense phase-two result on this captured generation."""

        self._check_lifetime()
        _validate_candidate_proof_dense_phase2_result(
            value,
            binding_digest=binding_digest,
            record_ids=record_ids,
            source_fold_lengths=source_fold_lengths,
        )

    def health(self) -> StoreHealth:
        """Return the same truthful health snapshot on the captured lease."""

        self._check_lifetime()
        return _store_health_body(self._lease)


@dataclass(frozen=True)
class SQLiteRuntimeCapability:
    sqlite_version: str
    unicode_version: str
    fts5_available: bool


@dataclass(frozen=True)
class SQLiteSchemaSnapshot:
    schema_version: int
    resource_id: str
    canonical_store_id: str
    target_identity: str
    generation: int
    head_revision: int
    fold_version: str
    scorer_version: str
    text_semantics_version: str
    candidate_index_kind: str
    candidate_index_version: str
    sqlite_runtime_version: str
    unicode_runtime_version: str
    fts5_available: bool
    journal_mode: str
    synchronous: str
    foreign_keys: bool
    busy_timeout_ms: int
    wal_enabled: bool
    extension_loading_enabled: bool
    activation_status: str
    activation_digest: str | None
    fuzzy_available: bool
    table_names: tuple[str, ...]
    index_names: tuple[str, ...]


@dataclass(frozen=True)
class _ReservedStageFile:
    device: int
    inode: int
    resolved_parent: Path


def detect_sqlite_runtime() -> SQLiteRuntimeCapability:
    """Probe only local built-in capabilities; never load extensions."""

    if unicodedata.unidata_version != UNICODE_VERSION:
        raise SQLiteStoreSchemaError("STORE.UNICODE_RUNTIME_MISMATCH")
    return SQLiteRuntimeCapability(
        sqlite_version=sqlite3.sqlite_version,
        unicode_version=unicodedata.unidata_version,
        fts5_available=_probe_fts5(),
    )


def _probe_fts5() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(False)
        connection.execute(
            "CREATE VIRTUAL TABLE __localcat_fts_probe USING fts5("
            "source_fold_v1, record_id UNINDEXED, "
            "tokenize='trigram case_sensitive 1')"
        )
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def _configure_immediate_seal_bulk_cache(
    connection: sqlite3.Connection,
) -> None:
    """Apply one connection-local cache hint before private bulk build."""

    if type(connection) is not sqlite3.Connection:
        raise TypeError("connection must be an exact sqlite3 connection")
    if connection.in_transaction:
        raise AssertionError("bulk cache must be configured before transaction")
    try:
        connection.execute(
            f"PRAGMA cache_size={-_IMMEDIATE_SEAL_CACHE_KIB}"
        )
        observed = connection.execute("PRAGMA cache_size").fetchone()
    except sqlite3.Error as error:
        raise SQLiteStoreSchemaError(
            "STORE.BULK_CACHE_UNAVAILABLE"
        ) from error
    if observed != (-_IMMEDIATE_SEAL_CACHE_KIB,):
        raise SQLiteStoreSchemaError("STORE.BULK_CACHE_UNAVAILABLE")


@contextmanager
def _open_configured_connection(
    database_path: Path,
    *,
    expected_file: _ReservedStageFile | None = None,
    require_existing: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Open one short, thread-local connection under the fixed policy."""

    _require_absolute_path(database_path, "database_path")
    database: str | Path = database_path
    uri = False
    if expected_file is not None or require_existing:
        database = f"{database_path.as_uri()}?mode=rw"
        uri = True
    connection = sqlite3.connect(
        database,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        uri=uri,
    )
    try:
        if expected_file is not None:
            _verify_reserved_stage_file(database_path, expected_file)
        connection.enable_load_extension(False)
        current_mode = _pragma_text(connection, "journal_mode").lower()
        if current_mode == "wal":
            raise SQLiteStoreSchemaError("STORE.WAL_FORBIDDEN")
        applied_mode = connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone()
        if (
            applied_mode is None
            or str(applied_mode[0]).lower() != "delete"
        ):
            raise SQLiteStoreSchemaError("STORE.JOURNAL_MODE_UNSAFE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if _pragma_int(connection, "synchronous") != 2:
            raise SQLiteStoreSchemaError("STORE.SYNCHRONOUS_UNSAFE")
        if _pragma_int(connection, "foreign_keys") != 1:
            raise SQLiteStoreSchemaError("STORE.FOREIGN_KEYS_DISABLED")
        if _pragma_int(connection, "busy_timeout") != BUSY_TIMEOUT_MS:
            raise SQLiteStoreSchemaError("STORE.BUSY_TIMEOUT_MISMATCH")
        yield connection
    finally:
        connection.close()


@contextmanager
def _open_leased_connection(
    lease: _SQLiteGenerationView,
) -> Iterator[sqlite3.Connection]:
    """Keep one generation lease around one thread-local connection."""

    try:
        with _open_configured_connection(
            lease.stage.staged_db_path
        ) as connection:
            yield connection
    except sqlite3.OperationalError as error:
        sqlite_code = getattr(error, "sqlite_errorcode", None)
        primary_code = (
            sqlite_code & 0xFF
            if type(sqlite_code) is int
            else None
        )
        message = str(error).lower()
        if (
            primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            or "database is locked" in message
            or "database table is locked" in message
        ):
            raise SQLiteStoreLifecycleError(
                "STORE.BUSY_TIMEOUT",
                resource_id=lease.stage.resource_identity.resource_id,
                generation=lease.generation,
                retryable=True,
            ) from error
        raise


@contextmanager
def _open_health_connection(
    lease: _SQLiteGenerationView,
) -> Iterator[sqlite3.Connection]:
    """Open portable health read-only so rooted byte proof can share the file.

    The ordinary store connection requests write access even for read methods.
    On Windows that conflicts with the intentionally strict rooted content
    handle, whose ``FILE_SHARE_READ`` freezes the exact bytes while hashing.
    A portable health call instead opens SQLite in locking, read-only mode.
    Its established read transaction prevents a writer from changing database
    pages while the retained rooted handle captures the same file.  This is not
    ``immutable=1``: SQLite still participates in the rollback-journal locks.
    """

    if not (
        sys.platform == "win32"
        and type(lease.active_content_attestation)
        is PortableActiveContentAttestation
    ):
        with _open_leased_connection(lease) as connection:
            yield connection
        return

    database_path = lease.stage.staged_db_path
    _require_absolute_path(database_path, "database_path")
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        uri=True,
    )
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if _pragma_int(connection, "query_only") != 1:
            raise SQLiteStoreSchemaError("STORE.QUERY_ONLY_DISABLED")
        if _pragma_text(connection, "journal_mode").lower() == "wal":
            raise SQLiteStoreSchemaError("STORE.WAL_FORBIDDEN")
        yield connection
    except sqlite3.OperationalError as error:
        sqlite_code = getattr(error, "sqlite_errorcode", None)
        primary_code = (
            sqlite_code & 0xFF
            if type(sqlite_code) is int
            else None
        )
        message = str(error).lower()
        if (
            primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            or "database is locked" in message
            or "database table is locked" in message
        ):
            raise SQLiteStoreLifecycleError(
                "STORE.BUSY_TIMEOUT",
                resource_id=lease.stage.resource_identity.resource_id,
                generation=lease.generation,
                retryable=True,
            ) from error
        raise
    finally:
        connection.close()


@contextmanager
def _leased_connection_scope(
    lease: _SQLiteGenerationView,
    connection: sqlite3.Connection | None,
) -> Iterator[sqlite3.Connection]:
    """Reuse one idle query-view connection without spanning transactions."""

    if connection is not None:
        if connection.in_transaction:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        yield connection
        return
    with _open_leased_connection(lease) as opened:
        yield opened


def initialize_stage_schema(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    _legacy_schema: bool = False,
    _caller_reservation: MutableFileReservation | None = None,
    _caller_parent: RootedDirectoryAuthority | None = None,
) -> SQLiteSchemaSnapshot:
    """Create a new unpublished stage; never create the canonical path.

    The private legacy mode creates the pre-v2 schema shape (Task 5.11
    copy-and-switch): a fresh mutable copy starts as the old schema and is
    migrated to the current schema in place before it can be sealed.
    """

    if type(_legacy_schema) is not bool:
        raise TypeError("_legacy_schema must be a built-in bool")
    validated_stage = _require_stage(stage)
    _require_identity(canonical_store_id, "canonical_store_id")
    path = validated_stage.staged_db_path
    runtime = detect_sqlite_runtime()
    if (_caller_reservation is None) != (_caller_parent is None):
        raise TypeError(
            "caller reservation and parent must be supplied together"
        )
    if _caller_reservation is not None:
        if not isinstance(_caller_reservation, MutableFileReservation):
            raise TypeError(
                "_caller_reservation must be MutableFileReservation or None"
            )
        if not isinstance(_caller_parent, RootedDirectoryAuthority):
            raise TypeError(
                "_caller_parent must be RootedDirectoryAuthority or None"
            )
    reservation = None
    if _caller_reservation is None:
        reservation = _reserve_stage_file(
            path,
            expected_parent=(
                validated_stage.resource_identity.canonical_sidecar_path.parent
            ),
        )
    else:
        _reprove_caller_stage_reservation(
            path,
            _caller_parent,
            _caller_reservation,
        )
    try:
        with _open_configured_connection(
            path,
            expected_file=reservation,
            require_existing=_caller_reservation is not None,
        ) as connection:
            if _caller_reservation is not None:
                _reprove_caller_stage_reservation(
                    path,
                    _caller_parent,
                    _caller_reservation,
                )
            connection.execute("BEGIN IMMEDIATE")
            schema_statements = (
                _LEGACY_SCHEMA_STATEMENTS
                if _legacy_schema
                else _SCHEMA_STATEMENTS
            )
            for statement in schema_statements:
                connection.execute(statement)
            if runtime.fts5_available:
                connection.execute(_FTS5_STATEMENT)
            schema_digest = _schema_digest(
                connection,
                fts5_available=runtime.fts5_available,
                legacy_schema=_legacy_schema,
            )
            approved_digests = (
                _APPROVED_LEGACY_SCHEMA_DIGESTS
                if _legacy_schema
                else _APPROVED_SCHEMA_DIGESTS
            )
            if schema_digest != approved_digests[runtime.fts5_available]:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_ROOT_MISMATCH")
            meta = _initial_meta(
                stage=validated_stage,
                canonical_store_id=canonical_store_id,
                runtime=runtime,
                schema_digest=schema_digest,
            )
            if _legacy_schema:
                meta["schema_version"] = str(TM_LEGACY_SCHEMA_VERSION)
            connection.executemany(
                "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
                tuple(sorted(meta.items())),
            )
            connection.commit()
            if _caller_reservation is not None:
                _reprove_caller_stage_reservation(
                    path,
                    _caller_parent,
                    _caller_reservation,
                )
        inspected = inspect_stage_schema(
            validated_stage,
            canonical_store_id=canonical_store_id,
            _allow_legacy_schema=_legacy_schema,
        )
        if _caller_reservation is not None:
            _reprove_caller_stage_reservation(
                path,
                _caller_parent,
                _caller_reservation,
            )
        return inspected
    except Exception:
        if reservation is not None:
            _remove_reserved_stage_file(path, reservation)
        raise


def _reprove_caller_stage_reservation(
    path: Path,
    parent: RootedDirectoryAuthority,
    reservation: MutableFileReservation,
) -> None:
    """Close one owner-private SQLite pathname around a live CREATE_NEW pin."""

    try:
        parent.reprove()
        owned = reservation.identity()
        named = parent.inspect_entry(path.name)
        if named is None or named.identity != owned:
            raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")
        parent.reprove()
    except SQLiteStoreSchemaError:
        raise
    except PlatformFileError as error:
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE") from error


def inspect_stage_schema(
    stage: _StoreRuntimeRef,
    *,
    canonical_store_id: str,
    _allow_diverged_runtime: bool = False,
    _allow_sealed: bool = False,
    _allow_active: bool = False,
    _allow_legacy_schema: bool = False,
    _expected_active_generation: int | None = None,
    _expected_activation_digest: str | None = None,
) -> SQLiteSchemaSnapshot:
    """Strictly inspect one stage without publishing physical readiness.

    The private sealed-inspection mode accepts a closed SEALED stage (Gate B
    recomputation) without weakening normal mutable-stage inspection or the
    future ACTIVE semantics.  The private legacy mode accepts the exact
    pre-v2 schema shape (Task 5.11) so an old-schema canonical can be
    reopened and upgraded; it never accepts an unknown or too-new version
    and still enforces identity, meta, runtime, index, foreign-key and
    digest closure.
    """

    if type(_allow_legacy_schema) is not bool:
        raise TypeError("_allow_legacy_schema must be a built-in bool")
    validated_stage = _require_inspectable_store_ref(stage)
    _require_identity(canonical_store_id, "canonical_store_id")
    if not validated_stage.staged_db_path.is_file():
        raise SQLiteStoreSchemaError("STORE.DATABASE_MISSING")
    with _open_configured_connection(
        validated_stage.staged_db_path,
        require_existing=True,
    ) as connection:
        meta = _read_meta(connection)
        schema_version = _meta_int(meta, "schema_version")
        if schema_version > TM_SCHEMA_VERSION:
            raise SQLiteStoreSchemaError("STORE.SCHEMA_TOO_NEW")
        if schema_version != TM_SCHEMA_VERSION:
            if (
                not _allow_legacy_schema
                or schema_version != TM_LEGACY_SCHEMA_VERSION
            ):
                raise SQLiteStoreSchemaError("STORE.SCHEMA_UNSUPPORTED")
        identity = validated_stage.resource_identity
        if (
            meta["resource_id"] != identity.resource_id
            or meta["canonical_store_id"] != canonical_store_id
            or meta["target_identity"] != identity.target_identity
        ):
            raise SQLiteStoreSchemaError("STORE.IDENTITY_MISMATCH")
        runtime = detect_sqlite_runtime()
        _validate_stage_meta(
            meta,
            runtime=runtime,
            allow_diverged_runtime=_allow_diverged_runtime,
            allow_sealed=_allow_sealed,
            allow_active=_allow_active,
            expected_active_generation=_expected_active_generation,
            expected_activation_digest=_expected_activation_digest,
        )
        table_names = _schema_object_names(connection, "table")
        index_names = _schema_object_names(connection, "index")
        base_tables = (
            _LEGACY_BASE_TABLES
            if schema_version == TM_LEGACY_SCHEMA_VERSION
            else _BASE_TABLES
        )
        base_indexes = (
            _LEGACY_BASE_INDEXES
            if schema_version == TM_LEGACY_SCHEMA_VERSION
            else _BASE_INDEXES
        )
        expected_tables = set(base_tables)
        fts5_available = _meta_bool(meta, "fts5_available")
        if fts5_available:
            expected_tables.add("tm_fts")
        expected_physical_tables = set(expected_tables)
        if fts5_available:
            expected_physical_tables.update(_FTS5_SHADOW_TABLES)
        if not expected_physical_tables.issubset(table_names):
            raise SQLiteStoreSchemaError("STORE.SCHEMA_INCOMPLETE")
        if table_names != expected_physical_tables:
            raise SQLiteStoreSchemaError("STORE.SCHEMA_UNEXPECTED")
        if not base_indexes.issubset(index_names):
            raise SQLiteStoreSchemaError("STORE.SCHEMA_INCOMPLETE")
        if index_names != base_indexes:
            raise SQLiteStoreSchemaError("STORE.SCHEMA_UNEXPECTED")
        _validate_schema_object_types(connection)
        actual_schema_digest = _schema_digest(
            connection,
            fts5_available=fts5_available,
            legacy_schema=schema_version == TM_LEGACY_SCHEMA_VERSION,
        )
        approved_schema_digests = (
            _APPROVED_LEGACY_SCHEMA_DIGESTS
            if schema_version == TM_LEGACY_SCHEMA_VERSION
            else _APPROVED_SCHEMA_DIGESTS
        )
        approved_schema_digest = approved_schema_digests[fts5_available]
        if (
            meta["schema_digest"] != approved_schema_digest
            or actual_schema_digest != approved_schema_digest
        ):
            raise SQLiteStoreSchemaError("STORE.TABLE_SCHEMA_MISMATCH")
        _validate_index_schema(
            connection,
            legacy_schema=schema_version == TM_LEGACY_SCHEMA_VERSION,
        )
        _validate_foreign_key_schema(
            connection,
            legacy_schema=schema_version == TM_LEGACY_SCHEMA_VERSION,
        )
        if fts5_available != runtime.fts5_available:
            raise SQLiteStoreSchemaError("STORE.RUNTIME_CAPABILITY_CHANGED")
        if meta["sqlite_runtime_version"] != runtime.sqlite_version:
            raise SQLiteStoreSchemaError("STORE.SQLITE_RUNTIME_CHANGED")
        if meta["unicode_runtime_version"] != runtime.unicode_version:
            raise SQLiteStoreSchemaError("STORE.UNICODE_RUNTIME_MISMATCH")
        journal_mode = _pragma_text(connection, "journal_mode").lower()
        synchronous_value = _pragma_int(connection, "synchronous")
        synchronous = {
            0: "OFF",
            1: "NORMAL",
            2: "FULL",
            3: "EXTRA",
        }.get(synchronous_value, f"UNKNOWN_{synchronous_value}")
        activation_digest = meta.get("activation_digest")
        return SQLiteSchemaSnapshot(
            schema_version=schema_version,
            resource_id=meta["resource_id"],
            canonical_store_id=meta["canonical_store_id"],
            target_identity=meta["target_identity"],
            generation=_meta_int(meta, "generation"),
            head_revision=_meta_int(meta, "head_revision"),
            fold_version=meta["fold_version"],
            scorer_version=meta["scorer_version"],
            text_semantics_version=meta["text_semantics_version"],
            candidate_index_kind=meta["candidate_index_kind"],
            candidate_index_version=meta["candidate_index_version"],
            sqlite_runtime_version=meta["sqlite_runtime_version"],
            unicode_runtime_version=meta["unicode_runtime_version"],
            fts5_available=fts5_available,
            journal_mode=journal_mode,
            synchronous=synchronous,
            foreign_keys=_pragma_int(connection, "foreign_keys") == 1,
            busy_timeout_ms=_pragma_int(connection, "busy_timeout"),
            wal_enabled=journal_mode == "wal",
            extension_loading_enabled=False,
            activation_status=meta["activation_status"],
            activation_digest=activation_digest,
            fuzzy_available=False,
            table_names=tuple(sorted(expected_tables)),
            index_names=tuple(sorted(base_indexes)),
        )


def _initial_meta(
    *,
    stage: MutableStageRef,
    canonical_store_id: str,
    runtime: SQLiteRuntimeCapability,
    schema_digest: str,
) -> dict[str, str]:
    index_kind = (
        "FTS5_TRIGRAM"
        if runtime.fts5_available
        else "GRAM_FALLBACK"
    )
    return {
        "activation_status": "UNPUBLISHED",
        "candidate_index_kind": index_kind,
        "candidate_index_version": CANDIDATE_INDEX_VERSION,
        "canonical_store_id": canonical_store_id,
        "divergence_latched": "0",
        "fold_version": FOLD_VERSION_V1,
        "fts5_available": "1" if runtime.fts5_available else "0",
        "generation": "0",
        "head_revision": "0",
        "journal_mode": "delete",
        "resource_id": stage.resource_identity.resource_id,
        "schema_digest": schema_digest,
        "schema_version": str(TM_SCHEMA_VERSION),
        "scorer_version": SCORER_VERSION_V1,
        "sqlite_runtime_version": runtime.sqlite_version,
        "target_identity": stage.resource_identity.target_identity,
        "text_semantics_version": TEXT_MATCHER_SEMANTICS_VERSION,
        "unicode_runtime_version": runtime.unicode_version,
    }


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute(
            "SELECT key, value FROM tm_meta ORDER BY key"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE") from error
    meta = {str(row[0]): str(row[1]) for row in rows}
    if not _REQUIRED_META_KEYS.issubset(meta):
        raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE")
    return meta


def _schema_digest(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
    legacy_schema: bool = False,
) -> str:
    if type(legacy_schema) is not bool:
        raise TypeError("legacy_schema must be a built-in bool")
    base_tables = _LEGACY_BASE_TABLES if legacy_schema else _BASE_TABLES
    base_indexes = _LEGACY_BASE_INDEXES if legacy_schema else _BASE_INDEXES
    expected_names = set(base_tables | base_indexes)
    if fts5_available:
        expected_names.add("tm_fts")
        expected_names.update(_FTS5_SHADOW_TABLES)
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    definitions = tuple(
        (str(object_type), str(name), str(sql))
        for object_type, name, sql in rows
        if str(name) in expected_names
    )
    payload = json.dumps(
        definitions,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_schema_object_types(
    connection: sqlite3.Connection,
) -> None:
    rows = connection.execute(
        "SELECT DISTINCT type FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    object_types = {str(row[0]) for row in rows}
    if not object_types.issubset({"table", "index"}):
        raise SQLiteStoreSchemaError("STORE.SCHEMA_UNEXPECTED")


def _validate_stage_meta(
    meta: dict[str, str],
    *,
    runtime: SQLiteRuntimeCapability,
    allow_diverged_runtime: bool = False,
    allow_sealed: bool = False,
    allow_active: bool = False,
    expected_active_generation: int | None = None,
    expected_activation_digest: str | None = None,
) -> None:
    expected_versions = {
        "fold_version": FOLD_VERSION_V1,
        "scorer_version": SCORER_VERSION_V1,
        "text_semantics_version": TEXT_MATCHER_SEMANTICS_VERSION,
        "candidate_index_version": CANDIDATE_INDEX_VERSION,
    }
    if any(meta.get(key) != value for key, value in expected_versions.items()):
        raise SQLiteStoreSchemaError("STORE.META_VERSION_MISMATCH")
    expected_index_kind = (
        "FTS5_TRIGRAM" if runtime.fts5_available else "GRAM_FALLBACK"
    )
    if meta.get("candidate_index_kind") != expected_index_kind:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_MISMATCH")
    if meta.get("journal_mode") != "delete":
        raise SQLiteStoreSchemaError("STORE.JOURNAL_MODE_UNSAFE")
    if allow_sealed and allow_active:
        raise TypeError("sealed and active inspection modes are exclusive")
    expected_status = (
        "ACTIVE"
        if allow_active
        else ("SEALED" if allow_sealed else "UNPUBLISHED")
    )
    if meta.get("activation_status") != expected_status:
        raise SQLiteStoreSchemaError(
            "STORE.STAGE_NOT_SEALED"
            if allow_sealed
            else "STORE.STAGE_PUBLISHED"
        )
    if allow_active:
        if (
            type(expected_active_generation) is not int
            or isinstance(expected_active_generation, bool)
            or expected_active_generation < 0
        ):
            raise TypeError("active generation expectation is invalid")
        if (
            type(expected_activation_digest) is not str
            or len(expected_activation_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_activation_digest
            )
        ):
            raise TypeError("activation digest expectation is invalid")
        if meta.get("activation_digest") != expected_activation_digest:
            raise SQLiteStoreSchemaError("STORE.ACTIVATION_DIGEST_MISMATCH")
        if _meta_int(meta, "generation") != expected_active_generation:
            raise SQLiteStoreSchemaError("STORE.GENERATION_MISMATCH")
    else:
        if "activation_digest" in meta:
            raise SQLiteStoreSchemaError("STORE.STAGE_PUBLISHED")
    if (
        _meta_bool(meta, "divergence_latched")
        and not allow_diverged_runtime
    ):
        raise SQLiteStoreSchemaError("STORE.STAGE_DIVERGED")
    if not allow_active and _meta_int(meta, "generation") != 0:
        raise SQLiteStoreSchemaError("STORE.STAGE_REVISION_INVALID")
    _ = _meta_int(meta, "head_revision")


def _validate_index_schema(
    connection: sqlite3.Connection,
    *,
    legacy_schema: bool,
) -> None:
    expected = {
        "idx_tm_exact": (("source_raw", False), ("record_id", True)),
        "idx_tm_context_speaker": (
            ("source_raw", False),
            ("speaker_raw", False),
            ("record_id", True),
        ),
        "idx_tm_gram_lookup": (
            ("gram_size", False),
            ("gram", False),
            ("record_id", False),
        ),
    }
    if not legacy_schema:
        expected["idx_tm_gram_block_lookup"] = (
            ("gram_size", False),
            ("gram", False),
            ("block_id", False),
        )
    for index_name, expected_columns in expected.items():
        rows = connection.execute(
            f"PRAGMA index_xinfo({index_name})"
        ).fetchall()
        columns = tuple(
            (str(row[2]), bool(row[3]))
            for row in rows
            if row[5] == 1
        )
        if columns != expected_columns:
            raise SQLiteStoreSchemaError("STORE.INDEX_SCHEMA_MISMATCH")


def _validate_foreign_key_schema(
    connection: sqlite3.Connection,
    *,
    legacy_schema: bool,
) -> None:
    expected = {
        "tm_snapshot_binding": (
            ("snapshot_id", "tm_snapshot_receipt", "snapshot_id", "NO ACTION"),
        ),
        "tm_record": (
            ("origin_batch_id", "tm_origin_batch", "batch_id", "NO ACTION"),
        ),
        "tm_gram": (
            ("record_id", "tm_record", "record_id", "CASCADE"),
        ),
    }
    if not legacy_schema:
        expected["tm_gram_block_max"] = (
            ("block_id", "tm_candidate_block", "block_id", "CASCADE"),
        )
    for table_name, expected_keys in expected.items():
        rows = connection.execute(
            f"PRAGMA foreign_key_list({table_name})"
        ).fetchall()
        keys = tuple(
            sorted(
                (
                    str(row[3]),
                    str(row[2]),
                    str(row[4]),
                    str(row[6]),
                )
                for row in rows
            )
        )
        if keys != tuple(sorted(expected_keys)):
            raise SQLiteStoreSchemaError("STORE.FOREIGN_KEY_SCHEMA_MISMATCH")


def _schema_object_names(
    connection: sqlite3.Connection,
    object_type: str,
) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        (object_type,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _snapshot_store_identity(
    value: CanonicalResourceIdentity | None,
) -> CanonicalResourceIdentity:
    if type(value) is not CanonicalResourceIdentity:
        raise TypeError(
            "resource_identity must be exact CanonicalResourceIdentity"
        )
    for path_value, field_name in (
        (value.configured_jsonl_path, "configured_jsonl_path"),
        (value.canonical_sidecar_path, "canonical_sidecar_path"),
        (value.snapshot_manifest_path, "snapshot_manifest_path"),
    ):
        if type(path_value) is not _NATIVE_PATH_TYPE:
            raise TypeError(f"{field_name} must be an exact native Path")
    private = CanonicalResourceIdentity(
        resource_id=value.resource_id,
        configured_jsonl_path=_copy_exact_path(value.configured_jsonl_path),
        canonical_sidecar_path=_copy_exact_path(value.canonical_sidecar_path),
        snapshot_manifest_path=_copy_exact_path(value.snapshot_manifest_path),
        target_identity=value.target_identity,
        identity_version=value.identity_version,
    )
    if private != value:
        raise ValueError("resource_identity is not canonical")
    return private


def _snapshot_store_stage(value: object) -> MutableStageRef:
    if type(value) is not MutableStageRef:
        raise TypeError("stage must be exact MutableStageRef")
    stage_id = value.stage_id
    identity = value.resource_identity
    staged_db_path = value.staged_db_path
    manifest_temp_path = value.manifest_temp_path
    if type(identity) is not CanonicalResourceIdentity:
        raise TypeError("stage resource identity must be exact")
    resource_id = identity.resource_id
    configured_jsonl_path = identity.configured_jsonl_path
    canonical_sidecar_path = identity.canonical_sidecar_path
    snapshot_manifest_path = identity.snapshot_manifest_path
    target_identity = identity.target_identity
    identity_version = identity.identity_version
    for field_value, field_name in (
        (stage_id, "stage_id"),
        (resource_id, "resource_id"),
        (target_identity, "target_identity"),
        (identity_version, "identity_version"),
    ):
        if type(field_value) is not str:
            raise TypeError(f"{field_name} must be a built-in string")
    for path_value, field_name in (
        (configured_jsonl_path, "configured_jsonl_path"),
        (canonical_sidecar_path, "canonical_sidecar_path"),
        (snapshot_manifest_path, "snapshot_manifest_path"),
        (staged_db_path, "staged_db_path"),
        (manifest_temp_path, "manifest_temp_path"),
    ):
        if type(path_value) is not _NATIVE_PATH_TYPE:
            raise TypeError(f"{field_name} must be an exact native Path")
    safe_identity = _snapshot_store_identity(identity)
    safe_stage = MutableStageRef(
        stage_id=stage_id,
        resource_identity=safe_identity,
        staged_db_path=_copy_exact_path(staged_db_path),
        manifest_temp_path=_copy_exact_path(manifest_temp_path),
    )
    return _require_stage(safe_stage)


def _copy_exact_path(value: Path) -> Path:
    raw_path = Path.__str__(value)
    if type(raw_path) is not str:
        raise TypeError("path text must be a built-in string")
    copied = Path(raw_path)
    if type(copied) is not _NATIVE_PATH_TYPE:
        raise TypeError("path copy must be an exact native Path")
    return copied


def _require_stage(value: object) -> MutableStageRef:
    if not isinstance(value, MutableStageRef):
        raise TypeError("stage must be MutableStageRef")
    identity = value.resource_identity
    if not isinstance(identity, CanonicalResourceIdentity):
        raise TypeError("stage resource identity is invalid")
    if value.staged_db_path.is_symlink():
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")
    if value.staged_db_path == identity.canonical_sidecar_path:
        raise SQLiteStoreSchemaError("STORE.CANONICAL_WRITE_FORBIDDEN")
    reserved_paths = {
        identity.configured_jsonl_path,
        identity.snapshot_manifest_path,
        value.manifest_temp_path,
    }
    if value.staged_db_path in reserved_paths:
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_RESERVED")
    if value.manifest_temp_path in {
        identity.configured_jsonl_path,
        identity.canonical_sidecar_path,
        value.staged_db_path,
    }:
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_RESERVED")
    return value


def _require_inspectable_store_ref(value: object) -> _StoreRuntimeRef:
    """Accept a public mutable stage or the exact private canonical view."""

    if type(value) is MutableStageRef:
        return _require_stage(value)
    if type(value) is not _CanonicalStoreRef:
        raise TypeError("store reference is invalid")
    identity = value.resource_identity
    if type(identity) is not CanonicalResourceIdentity:
        raise TypeError("canonical store resource identity is invalid")
    if (
        value.staged_db_path != identity.canonical_sidecar_path
        or value.manifest_temp_path != identity.snapshot_manifest_path
    ):
        raise SQLiteStoreSchemaError("STORE.IDENTITY_MISMATCH")
    if value.staged_db_path.is_symlink():
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")
    return value


def _reserve_stage_file(
    path: Path,
    *,
    expected_parent: Path,
) -> _ReservedStageFile:
    if os.path.lexists(path):
        if path.is_symlink():
            raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")
        raise SQLiteStoreSchemaError("STORE.STAGE_ALREADY_EXISTS")
    try:
        resolved_parent = path.parent.resolve(strict=True)
        required_parent = expected_parent.resolve(strict=True)
    except OSError as error:
        raise SQLiteStoreSchemaError("STORE.STAGE_PARENT_UNSAFE") from error
    if resolved_parent != required_parent:
        raise SQLiteStoreSchemaError("STORE.STAGE_PARENT_UNSAFE")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        code = (
            "STORE.STAGE_PATH_UNSAFE"
            if path.is_symlink()
            else "STORE.STAGE_ALREADY_EXISTS"
        )
        raise SQLiteStoreSchemaError(code) from error
    observed: os.stat_result | None = None
    try:
        os.fchmod(descriptor, 0o600)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")
        reservation = _ReservedStageFile(
            device=observed.st_dev,
            inode=observed.st_ino,
            resolved_parent=resolved_parent,
        )
        _verify_reserved_stage_file(path, reservation)
        return reservation
    except Exception:
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            current = None
        if (
            current is not None
            and observed is not None
            and stat.S_ISREG(current.st_mode)
            and current.st_dev == observed.st_dev
            and current.st_ino == observed.st_ino
        ):
            path.unlink()
        raise
    finally:
        os.close(descriptor)


def _verify_reserved_stage_file(
    path: Path,
    reservation: _ReservedStageFile,
) -> None:
    try:
        observed = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_dev != reservation.device
        or observed.st_ino != reservation.inode
        or resolved.parent != reservation.resolved_parent
        or resolved.name != path.name
    ):
        raise SQLiteStoreSchemaError("STORE.STAGE_PATH_UNSAFE")


def _remove_reserved_stage_file(
    path: Path,
    reservation: _ReservedStageFile,
) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(observed.st_mode)
        and observed.st_dev == reservation.device
        and observed.st_ino == reservation.inode
    ):
        path.unlink()


def _require_absolute_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be Path")
    if not value.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return value


def _require_identity(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _pragma_text(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or not isinstance(row[0], str):
        raise SQLiteStoreSchemaError(f"STORE.PRAGMA_{name.upper()}_INVALID")
    return row[0]


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if (
        row is None
        or not isinstance(row[0], int)
        or isinstance(row[0], bool)
    ):
        raise SQLiteStoreSchemaError(f"STORE.PRAGMA_{name.upper()}_INVALID")
    return row[0]


def _meta_int(meta: dict[str, str], key: str) -> int:
    try:
        value = int(meta[key])
    except (KeyError, ValueError) as error:
        raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE") from error
    if value < 0:
        raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE")
    return value


def _meta_bool(meta: dict[str, str], key: str) -> bool:
    value = meta.get(key)
    if value not in {"0", "1"}:
        raise SQLiteStoreSchemaError("STORE.META_INCOMPLETE")
    return value == "1"


def _schema_upgrade_backup_path(
    store_path: Path,
    token: str,
) -> Path:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._schema_upgrade_backup_path(
        store_path,
        token,
    )


def _fsync_schema_upgrade_directory(path: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._fsync_schema_upgrade_directory(path)


def _create_schema_upgrade_backup(
    source_path: Path,
    backup_path: Path,
) -> tuple[tuple[int, int], str]:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._create_schema_upgrade_backup(
        source_path,
        backup_path,
    )


def _remove_partial_schema_upgrade_backup(backup_path: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._remove_partial_schema_upgrade_backup(
        backup_path
    )


def _schema_upgrade_locator_snapshot_path(
    store_path: Path,
    token: str,
) -> Path:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._schema_upgrade_locator_snapshot_path(
        store_path,
        token,
    )


def _create_schema_upgrade_locator_snapshot(
    source_path: Path,
    snapshot_path: Path,
) -> tuple[tuple[int, int], str]:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._create_schema_upgrade_locator_snapshot(
        source_path,
        snapshot_path,
    )


def _remove_owned_schema_upgrade_artifact(
    path: Path,
    identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._remove_owned_schema_upgrade_artifact(
        path,
        identity,
    )


def _schema_upgrade_reported_path(pending_path: Path) -> Path:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._schema_upgrade_reported_path(
        pending_path
    )


def _promote_schema_upgrade_artifact(
    path: Path,
    identity: tuple[int, int],
) -> Path:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._promote_schema_upgrade_artifact(
        path,
        identity,
    )


def _pending_schema_upgrade_family(store_path: Path) -> list[Path]:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._pending_schema_upgrade_family(
        store_path
    )


def _require_owned_pending_schema_upgrade_name(
    path: Path,
    store_path: Path,
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._require_owned_pending_schema_upgrade_name(
        path,
        store_path,
    )


def _sweep_pending_schema_upgrade_artifacts(store_path: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._sweep_pending_schema_upgrade_artifacts(
        store_path
    )


def _promote_pending_schema_upgrade_backup(
    store_path: Path,
) -> Path | None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._promote_pending_schema_upgrade_backup(
        store_path
    )


def _finish_cold_schema_upgrade_pending(
    store_path: Path,
    *,
    completed: bool,
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._finish_cold_schema_upgrade_pending(
        store_path,
        completed=completed,
    )


def _recovered_schema_upgrade_pending_root(
    port: _CoordinatorStorePort,
    view: _SQLiteGenerationView,
) -> Path:
    """The store path owning the pending family of one cold recovery.

    A completed cold recovery rehydrates the view at the canonical
    sidecar, while the unexposed pending schema-upgrade artifacts were
    minted next to the pre-activation store path captured in the retained
    terminal closure (``prior_db_path``); cancelled/rolled-back outcomes
    already restore that prior path as the view.  Only a recovered view
    sitting at the canonical sidecar consults the terminal, and any
    terminal anomaly falls back to that view path instead of sweeping or
    promoting artifacts next to the wrong store.
    """

    if (
        view.stage.staged_db_path
        != port.resource_identity.canonical_sidecar_path
    ):
        return view.stage.staged_db_path
    identity = port.resource_identity
    prior_db_path: Path | None = None
    journal_path = _activation_journal_path(identity)
    try:
        journal_identity = _lstat_activation_journal_identity(journal_path)
    except ActivationPreparationError:
        journal_identity = None
    if journal_identity is not None:
        try:
            journal_record = _load_recovery_journal(
                port,
                journal_path,
                journal_identity,
            )
        except ActivationPreparationError:
            journal_record = None
        if journal_record is not None:
            prior_db_path = journal_record.prior_db_path
    if prior_db_path is None:
        terminal_path = _activation_terminal_path(identity)
        try:
            terminal_identity = _lstat_activation_terminal_identity(
                terminal_path
            )
        except ActivationPreparationError:
            terminal_identity = None
        if terminal_identity is not None:
            try:
                terminal_record = _load_recovery_terminal(
                    port,
                    terminal_path,
                    terminal_identity,
                )
            except ActivationPreparationError:
                terminal_record = None
            if terminal_record is not None:
                prior_db_path = terminal_record.prior_db_path
    if (
        prior_db_path is not None
        and type(prior_db_path) is _NATIVE_PATH_TYPE
        and prior_db_path.is_absolute()
    ):
        return prior_db_path
    return view.stage.staged_db_path


def _remove_schema_upgrade_backup(
    ticket: _SchemaUpgradeSnapshotTicket,
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._remove_schema_upgrade_backup(ticket)


def _remove_schema_upgrade_locator_snapshot(
    snapshot: _SchemaUpgradeLocatorSnapshot,
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._remove_schema_upgrade_locator_snapshot(
        snapshot
    )


def _file_sha256_of_path(path: Path) -> str:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._file_sha256_of_path(path)


def _schema_upgrade_db_capture(
    database_path: Path,
) -> tuple[tuple[int, int], str]:
    """Capture the exact prior active DB identity and digest (no-follow)."""

    capture = _capture_activation_file(database_path, asset_kind="DATABASE")
    return (
        (capture.identity.device, capture.identity.inode),
        capture.digest,
    )


def _schema_upgrade_head_revision(database_path: Path) -> int:
    """Read the active store's head revision strictly read-only."""

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        rows = connection.execute(
            "SELECT value FROM tm_meta WHERE key = 'head_revision'"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_ACTIVE_UNREADABLE",
            retryable=False,
        ) from error
    finally:
        connection.close()
    if len(rows) != 1:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_ACTIVE_UNREADABLE",
            retryable=False,
        )
    try:
        value = int(str(rows[0][0]))
    except (TypeError, ValueError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_ACTIVE_UNREADABLE",
            retryable=False,
        ) from error
    if value < 0:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_ACTIVE_UNREADABLE",
            retryable=False,
        )
    return value


def _read_schema_upgrade_marker(database_path: Path) -> str | None:
    """Read the durable schema-upgrade origin marker, or None when absent."""

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        rows = connection.execute(
            "SELECT value FROM tm_meta "
            "WHERE key = 'schema_upgrade_origin'"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_CANDIDATE_UNREADABLE",
            retryable=False,
        ) from error
    finally:
        connection.close()
    if len(rows) == 0:
        return None
    if len(rows) != 1 or type(rows[0][0]) is not str:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_CANDIDATE_INVALID",
            retryable=False,
        )
    return str(rows[0][0])



def _require_schema_upgrade_ancestry_provable(database_path: Path) -> None:
    """Prove one pre-v2 store's completion order from the strict block proof.

    Task 5.11 fail-closed preflight: the legacy schema records no
    ``completed_revision``, so revision order is derived only from the
    strictly contiguous record-id blocks and per-batch origin ordinals.
    Any zero-record completed batch, interleaved block, non-contiguous
    ordinal block, or head/count mismatch raises the specific
    ``SCHEMA.ANCESTRY_UNPROVABLE`` code and never falls back to batch-id
    sorting.  The store is opened strictly read-only and never mutated.
    """

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        try:
            meta_rows = connection.execute(
                "SELECT key, value FROM tm_meta"
            ).fetchall()
            meta = {str(row[0]): str(row[1]) for row in meta_rows}
            head_revision = int(str(meta["head_revision"]))
            record_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()[0]
            )
            _legacy_revision_ancestry(
                connection,
                head_revision=head_revision,
                record_count=record_count,
            )
        except SQLiteStoreSchemaError as error:
            raise SchemaUpgradeAncestryError(
                "SCHEMA.ANCESTRY_UNPROVABLE",
                retryable=False,
            ) from error
        except (TypeError, ValueError, KeyError, sqlite3.DatabaseError) as error:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_ACTIVE_UNREADABLE",
                retryable=False,
            ) from error
    finally:
        connection.close()


def _require_no_pending_activation_assets(
    identity: CanonicalResourceIdentity,
) -> None:
    """Fail closed when any durable activation asset is pending.

    Mirrors the preparation prechecks for the Task 5.11 ticket seam: a
    journal, journal temporary, or incomplete lineage marker means a
    pending activation authority exists and a new stabilization must not
    proceed.  A valid regular terminal is the closed record of a finished
    cancellation or rollback (the prior authority is already closed, so a
    fresh stabilization may proceed); a malformed or foreign terminal
    fails closed exactly like the preparation prechecks.
    """

    journal_path = _activation_journal_path(identity)
    try:
        journal_identity = _lstat_activation_journal_identity(journal_path)
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PENDING",
            retryable=False,
            reason_code=error.code,
        ) from error
    if journal_identity is not None or _lstat_any_entry(
        _activation_journal_temp_path(journal_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PENDING",
            retryable=False,
        )
    terminal_path = _activation_terminal_path(identity)
    try:
        terminal_identity = _lstat_activation_terminal_identity(terminal_path)
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PENDING",
            retryable=False,
            reason_code=error.code,
        ) from error
    try:
        marker_identity = _activation_lineage_marker_state_complete(identity)
    except ActivationPreparationError as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PENDING",
            retryable=False,
            reason_code=error.code,
        ) from error
    if marker_identity is None:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PENDING",
            retryable=False,
        )


def _require_schema_upgrade_mode_closure(
    physical_snapshot: Any,
    *,
    schema_upgrade_ticket: _SchemaUpgradeSnapshotTicket | None,
) -> None:
    """Bind the durable candidate marker to the activation guard.

    A schema-upgrade candidate (meta carries the durable upgrade-origin
    marker that its sealed closure covers) may only be activated with the
    coordinator-minted ticket; an ordinary candidate may never be
    activated under a ticket.  Either mismatch fails closed before any
    journal.
    """

    candidate_path = physical_snapshot.mutable_stage.staged_db_path
    marker = _read_schema_upgrade_marker(candidate_path)
    if schema_upgrade_ticket is None:
        if marker is not None:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_REQUIRED",
                retryable=False,
            )
        return
    if marker != _SCHEMA_UPGRADE_META_VALUE:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_TICKET_INVALID",
            retryable=False,
        )


def _retire_schema_upgrade_ticket(
    coordinator: ResourceStoreCoordinator,
    ticket: _SchemaUpgradeSnapshotTicket,
) -> None:
    with coordinator._condition:
        if coordinator._schema_upgrade_ticket is ticket:
            coordinator._schema_upgrade_ticket = None
        else:
            return
    _remove_schema_upgrade_backup(ticket)


def _require_schema_upgrade_ticket_guard(
    coordinator: ResourceStoreCoordinator,
    view: _SQLiteGenerationView,
    ticket: _SchemaUpgradeSnapshotTicket,
    captures: tuple[_PriorAssetCapture, ...],
) -> None:
    """Prove the ticket and the stabilized prior active canonical unchanged.

    Runs after the normal drain and before any journal.  The ticket must
    be the coordinator's live unused ticket bound to this resource,
    canonical store id, and generation; the active DB must still carry
    the exact identity/digest/head revision captured while drained; and
    the recovery backup must still be intact.  Any stale/foreign state
    consumes the ticket and fails the stale candidate before journal so
    the coordinator restores READY and a later fresh snapshot can retry.
    """

    def fail(code: str, *, retryable: bool) -> NoReturn:
        _retire_schema_upgrade_ticket(coordinator, ticket)
        raise ActivationPreparationError(code, retryable=retryable)

    with coordinator._condition:
        if coordinator._schema_upgrade_ticket is not ticket:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_STALE",
                retryable=False,
            )
        if (
            ticket._owner_nonce != coordinator._owner_nonce
            or ticket.resource_id != coordinator._resource_id
            or ticket.canonical_store_id != coordinator._canonical_store_id
            or ticket.generation != view.generation
        ):
            fail("ACTIVATION.UPGRADE_TICKET_INVALID", retryable=False)
        database_capture: _PriorAssetCapture | None = None
        for capture in captures:
            if capture.asset_kind == "DATABASE":
                database_capture = capture
                break
        if database_capture is None:
            fail("ACTIVATION.UPGRADE_TICKET_INVALID", retryable=False)
        if (
            (
                database_capture.identity.device,
                database_capture.identity.inode,
            )
            != ticket.db_identity
            or database_capture.digest != ticket.db_digest
        ):
            fail("ACTIVATION.UPGRADE_SNAPSHOT_STALE", retryable=True)
    live_head = _schema_upgrade_head_revision(view.stage.staged_db_path)
    if live_head != ticket.head_revision:
        fail("ACTIVATION.UPGRADE_SNAPSHOT_STALE", retryable=True)
    try:
        backup_capture = _capture_activation_file(
            ticket.backup_path,
            asset_kind="DATABASE",
        )
    except ActivationPreparationError:
        fail("ACTIVATION.UPGRADE_BACKUP_INVALID", retryable=False)
    if backup_capture.digest != ticket.backup_digest:
        fail("ACTIVATION.UPGRADE_BACKUP_INVALID", retryable=False)
    with coordinator._condition:
        if coordinator._schema_upgrade_ticket is not ticket:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_TICKET_STALE",
                retryable=False,
            )
        coordinator._schema_upgrade_ticket = None
    # The stabilized prior is now proven byte-identical to the live
    # canonical and the activation pipeline's own byte-exact recovery
    # backup takes over as the honest locator evidence, so the extra
    # locator snapshot is no longer needed and is strictly removed.
    coordinator.release_schema_upgrade_locator_snapshot()


__all__ = [
    "ActivationBackupEvidence",
    "ActivationPreparationError",
    "ActivationRecoveryReport",
    "BUSY_TIMEOUT_MS",
    "CANDIDATE_INDEX_VERSION",
    "CANDIDATE_PROOF_BLOCK_SIZE",
    "CANDIDATE_PROOF_BLOCK_VERSION_V1",
    "CandidateProofIndexError",
    "CanonicalRevisionSnapshot",
    "FOLD_VERSION_V1",
    "ResourceStoreCoordinator",
    "SQLiteCandidateRecord",
    "SQLiteCandidateProofBlock",
    "SQLiteCandidateProofRecord",
    "SQLiteCandidateProofSnapshot",
    "SQLiteCandidateRecallSnapshot",
    "SQLiteCandidateWritePlan",
    "SQLiteGramRow",
    "SQLiteRuntimeCapability",
    "SQLiteSchemaSnapshot",
    "SQLiteStoreLifecycleError",
    "SQLiteStoreSchemaError",
    "SQLiteTMStore",
    "SQLiteTMQueryView",
    "SourceBindingMonitor",
    "SourceBindingObservation",
    "TM_LEGACY_SCHEMA_VERSION",
    "TM_SCHEMA_VERSION",
    "detect_sqlite_runtime",
    "initialize_stage_schema",
    "inspect_stage_schema",
    "validate_candidate_proof_index",
]
