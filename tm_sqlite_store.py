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
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Any, NoReturn, Protocol, cast
import unicodedata
import uuid

import tm_contracts as contract_module
from text_matcher import (
    TEXT_MATCHER_SEMANTICS_VERSION,
    UNICODE_VERSION,
    fold_text_v1,
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
    _rollback_inconsistent_activation,
    _rollback_restored_prior_view,
    _validate_activation_indexes,
    _validate_activation_publication_authority,
    _validate_published_activation_set,
    _validate_replaced_activation_database,
    publish_activation,
    recover_durable_activation,
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
CANDIDATE_INDEX_VERSION = "candidate-index-v1"
CANDIDATE_PROOF_BLOCK_VERSION_V1 = "candidate-proof-block-v1"
CANDIDATE_PROOF_BLOCK_SIZE = 256
BUSY_TIMEOUT_MS = 5000
_CANDIDATE_QUERY_CHUNK_SIZE = 256
_NATIVE_PATH_TYPE = type(Path())

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


class SQLiteStoreSchemaError(RuntimeError):
    """A safe, resource-local schema or connection policy failure."""


class CandidateProofIndexError(ValueError):
    """One canonical candidate-proof fact failed exact recomputation."""


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


@dataclass(frozen=True)
class SQLiteCandidateRecord:
    """Folded source input exposed to a pre-transaction plan builder."""

    origin_ordinal: int
    source_fold_v1: str

    def __post_init__(self) -> None:
        if type(self.origin_ordinal) is not int or self.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(self.source_fold_v1) is not str or not self.source_fold_v1:
            raise ValueError("source_fold_v1 must be a non-empty string")


@dataclass(frozen=True)
class SQLiteGramRow:
    """One declarative gram row for the store-owned transaction."""

    origin_ordinal: int
    gram_size: int
    gram: str
    term_frequency: int = 1

    def __post_init__(self) -> None:
        if type(self.origin_ordinal) is not int or self.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(self.gram_size) is not int or self.gram_size not in {1, 2, 3}:
            raise ValueError("gram_size must be 1, 2, or 3")
        if type(self.gram) is not str or len(self.gram) != self.gram_size:
            raise ValueError("gram length must equal gram_size")
        if type(self.term_frequency) is not int or self.term_frequency < 1:
            raise ValueError("term_frequency must be a positive integer")


@dataclass(frozen=True)
class SQLiteCandidateWritePlan:
    """Closed candidate rows returned without access to SQLite state."""

    gram_rows: tuple[SQLiteGramRow, ...] = ()
    fts_origin_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.gram_rows) is not tuple:
            raise TypeError("gram_rows must contain SQLiteGramRow values")
        gram_keys: list[tuple[int, int, str]] = []
        for row in self.gram_rows:
            if type(row) is not SQLiteGramRow:
                raise TypeError("gram_rows must contain SQLiteGramRow values")
            origin_ordinal = row.origin_ordinal
            gram_size = row.gram_size
            gram = row.gram
            term_frequency = row.term_frequency
            if type(origin_ordinal) is not int or origin_ordinal < 0:
                raise ValueError(
                    "origin_ordinal must be a non-negative integer"
                )
            if type(gram_size) is not int or gram_size not in {1, 2, 3}:
                raise ValueError("gram_size must be 1, 2, or 3")
            if type(gram) is not str or len(gram) != gram_size:
                raise ValueError("gram length must equal gram_size")
            if type(term_frequency) is not int or term_frequency < 1:
                raise ValueError("term_frequency must be a positive integer")
            gram_keys.append((origin_ordinal, gram_size, gram))
        if len(set(gram_keys)) != len(gram_keys):
            raise ValueError("gram_rows must be unique")
        if type(self.fts_origin_ordinals) is not tuple or any(
            type(origin_ordinal) is not int or origin_ordinal < 0
            for origin_ordinal in self.fts_origin_ordinals
        ):
            raise ValueError(
                "fts_origin_ordinals must contain non-negative integers"
            )
        if len(set(self.fts_origin_ordinals)) != len(
            self.fts_origin_ordinals
        ):
            raise ValueError("fts_origin_ordinals must be unique")


def unique_character_ngrams(
    folded_text: str,
    gram_size: int,
) -> tuple[str, ...]:
    """Return first-occurrence unique code-point grams without folding."""

    if type(folded_text) is not str:
        raise TypeError("folded_text must be a built-in string")
    if type(gram_size) is not int:
        raise TypeError("gram_size must be a built-in integer")
    if gram_size not in {1, 2, 3}:
        raise ValueError("gram_size must be 1, 2, or 3")
    seen: set[str] = set()
    grams: list[str] = []
    for offset in range(max(0, len(folded_text) - gram_size + 1)):
        gram = folded_text[offset : offset + gram_size]
        if gram not in seen:
            seen.add(gram)
            grams.append(gram)
    return tuple(grams)


def character_ngram_frequencies(
    folded_text: str,
    gram_size: int,
) -> tuple[tuple[str, int], ...]:
    """Return exact multiset frequencies in first-occurrence gram order."""

    grams = unique_character_ngrams(folded_text, gram_size)
    frequencies = Counter(
        folded_text[offset : offset + gram_size]
        for offset in range(max(0, len(folded_text) - gram_size + 1))
    )
    return tuple((gram, frequencies[gram]) for gram in grams)


def build_candidate_write_plan(
    records: tuple[SQLiteCandidateRecord, ...],
    *,
    fts5_available: bool,
) -> SQLiteCandidateWritePlan:
    """Build the store-owned mandatory candidate plan for one generation."""

    if type(records) is not tuple:
        raise TypeError("records must be a built-in tuple")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    prepared: list[tuple[int, str]] = []
    for record in records:
        if type(record) is not SQLiteCandidateRecord:
            raise TypeError("records must contain exact SQLiteCandidateRecord values")
        if type(record.origin_ordinal) is not int or record.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(record.source_fold_v1) is not str or not record.source_fold_v1:
            raise ValueError("source_fold_v1 must be a non-empty built-in string")
        prepared.append((record.origin_ordinal, record.source_fold_v1))
    gram_sizes = (1, 2) if fts5_available else (1, 2, 3)
    return SQLiteCandidateWritePlan(
        gram_rows=tuple(
            SQLiteGramRow(origin_ordinal, gram_size, gram, term_frequency)
            for origin_ordinal, folded_source in prepared
            for gram_size in gram_sizes
            for gram, term_frequency in character_ngram_frequencies(
                folded_source,
                gram_size,
            )
        ),
        fts_origin_ordinals=(
            tuple(origin_ordinal for origin_ordinal, _source in prepared)
            if fts5_available
            else ()
        ),
    )


def _build_fts5_match_expression(trigrams: tuple[str, ...]) -> str:
    return " OR ".join(
        f'"{trigram.replace(chr(34), chr(34) * 2)}"'
        for trigram in trigrams
    )


def _chunked(values: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    for offset in range(0, len(values), _CANDIDATE_QUERY_CHUNK_SIZE):
        yield values[offset : offset + _CANDIDATE_QUERY_CHUNK_SIZE]


@dataclass(frozen=True)
class SQLiteCandidateRecallSnapshot:
    """Private-value snapshot returned by the leased candidate read seam."""

    fts5_available: bool
    stage_matches: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    folded_sources: tuple[tuple[int, str], ...]

    def __post_init__(self) -> None:
        if type(self.fts5_available) is not bool:
            raise TypeError("fts5_available must be a built-in bool")
        if type(self.stage_matches) is not tuple:
            raise TypeError("stage_matches must be a built-in tuple")
        stage_names: list[str] = []
        for stage_entry in self.stage_matches:
            if type(stage_entry) is not tuple or len(stage_entry) != 2:
                raise TypeError("stage_matches must contain built-in pairs")
            stage_name, matches = stage_entry
            if type(stage_name) is not str or type(matches) is not tuple:
                raise TypeError("candidate stage snapshot values are invalid")
            if stage_name not in {"FTS_TRIGRAM", "GRAM_3", "GRAM_2", "GRAM_1"}:
                raise ValueError("candidate stage snapshot name is invalid")
            stage_names.append(stage_name)
            record_ids: list[int] = []
            for match in matches:
                if type(match) is not tuple or len(match) != 2:
                    raise TypeError("candidate stage matches must be pairs")
                record_id, matched_count = match
                if (
                    type(record_id) is not int
                    or record_id < 1
                    or type(matched_count) is not int
                    or matched_count < 0
                ):
                    raise ValueError("candidate stage match is invalid")
                if matched_count < 1:
                    raise ValueError("candidate stage overlap is invalid")
                record_ids.append(record_id)
            if len(set(record_ids)) != len(record_ids):
                raise ValueError("candidate stage matches must be unique")
        if len(set(stage_names)) != len(stage_names):
            raise ValueError("candidate stage snapshot names must be unique")
        if type(self.folded_sources) is not tuple:
            raise TypeError("folded_sources must be a built-in tuple")
        source_ids: list[int] = []
        for source_entry in self.folded_sources:
            if type(source_entry) is not tuple or len(source_entry) != 2:
                raise TypeError("folded_sources must contain built-in pairs")
            record_id, folded_source = source_entry
            if (
                type(record_id) is not int
                or record_id < 1
                or type(folded_source) is not str
                or not folded_source
            ):
                raise ValueError("candidate folded source is invalid")
            source_ids.append(record_id)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("candidate folded sources must be unique")


@dataclass(frozen=True)
class SQLiteCandidateProofBlock:
    """One verified block bound input without any record source text."""

    block_id: int
    first_record_id: int
    last_record_id: int
    record_count: int
    min_source_fold_length: int
    max_source_fold_length: int
    character_intersection_upper: int
    bigram_intersection_upper: int


@dataclass(frozen=True)
class SQLiteCandidateProofRecord:
    """Exact record-bound facts derived from relevant proof rows only."""

    record_id: int
    block_id: int
    source_fold_length: int
    character_multiset_intersection: int
    bigram_multiset_intersection: int


@dataclass(frozen=True)
class SQLiteCandidateProofSnapshot:
    """Seed and block frontiers for one query generation, without record facts."""

    index_kind: str
    seed_stages: tuple[tuple[str, tuple[int, ...]], ...]
    blocks: tuple[SQLiteCandidateProofBlock, ...]
    total_record_count: int
    head_revision: int


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

    def advance_after_effect(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
        *,
        next_generation: int | None = None,
        activation_digest: str | None = None,
    ) -> _ActivationJournalHandle:
        return self._coordinator._advance_activation_journal_after_effect_locked(
            preparation,
            handle,
            next_phase,
            next_generation=next_generation,
            activation_digest=activation_digest,
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


def _rehydrate_completed_activation(
    port: _StoreValidationPort,
    record: _ActivationJournalRecord,
) -> ActivationRecoveryReport:
    """Re-prove and hydrate exactly one completed canonical generation.

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
    database = _recovery_capture_journal_file(
        identity.canonical_sidecar_path
    )
    if (
        (database[0].device, database[0].inode)
        != record.candidate_stage_db_identity
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
            _recover_activation_indexes(port,
                connection,
                fts5_available=snapshot.fts5_available,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    port.view = _SQLiteGenerationView(
        stage=active_ref,
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
            "SealedArtifactRegistry",
        )
        self._sealed_registry = registry_type(
            registry_namespace=f"coordinator.{identity.target_identity}",
        )
        self._preparation: _ActivationPreparation | None = None
        self._cleanup_reservation: _ActivationCleanupReservation | None = None
        self._cleanup_in_progress = False
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

    @property
    def sealed_registry(self) -> contract_module._SealedArtifactRegistryPort:
        return self._sealed_registry

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

        first_report = gate_b_evaluator(
            registry=registry
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
                registry=registry
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
            return report

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
    ) -> _ActivationJournalHandle:
        """Advance only after re-proving the phase-specific durable effect."""

        return _advance_activation_journal_after_effect(
            _CoordinatorStorePort(self),
            preparation,
            handle,
            next_phase,
            next_generation=next_generation,
            activation_digest=activation_digest,
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
            self._store.recover_configured_refresh()

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
        receipt_ids = {receipt.snapshot_id for receipt in receipts}
        if any(
            handoff.snapshot_id not in receipt_ids
            for handoff in handoffs
        ):
            raise SQLiteStoreSchemaError("STORE.HANDOFF_ORPHANED")
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
        )

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
    ) -> None:
        """Append one ordered origin batch from a bounded draft stream.

        The stream is consumed in fixed-size chunks. Each chunk is committed
        in its own transaction; the batch row starts as ``staged`` and is
        completed with the final counts in the last chunk's transaction. Any
        failure raises without a completed revision, so the caller must
        discard the stage (the migration build deletes both stage files).
        """

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
        timestamp = datetime.now(UTC).isoformat()

        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
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
                        return

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
                rows = connection.execute(
                    "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
                    (match_expression,),
                ).fetchall()
        record_ids: set[int] = set()
        for row in rows:
            if type(row) is not tuple or len(row) != 1:
                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
            value = row[0]
            if type(value) is int:
                record_id = value
            elif type(value) is str and value.isdecimal():
                record_id = int(value)
            else:
                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
            if record_id < 1:
                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
            record_ids.add(record_id)
        return tuple(sorted(record_ids))

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
                record_ids: set[int] = set()
                try:
                    connection.execute("BEGIN")
                    self._validate_identity(connection, lease)
                    if not lease.fts5_available:
                        connection.rollback()
                        return None
                    for chunk in _chunked(trigrams):
                        rows = connection.execute(
                            "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
                            (_build_fts5_match_expression(chunk),),
                        ).fetchall()
                        for row in rows:
                            if type(row) is not tuple or len(row) != 1:
                                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
                            value = row[0]
                            if type(value) is int:
                                record_id = value
                            elif type(value) is str and value.isdecimal():
                                record_id = int(value)
                            else:
                                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
                            if record_id < 1:
                                raise SQLiteStoreSchemaError("STORE.FTS5_RESULT_INVALID")
                            record_ids.add(record_id)
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise SQLiteStoreSchemaError("STORE.FTS5_QUERY_FAILED") from error
                except BaseException:
                    connection.rollback()
                    raise
        return tuple(sorted(record_ids))

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

        matched_by_id: dict[int, int] = {}
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                try:
                    connection.execute("BEGIN")
                    self._validate_identity(connection, lease)
                    for offset in range(0, len(prepared), _CANDIDATE_QUERY_CHUNK_SIZE):
                        chunk = prepared[offset : offset + _CANDIDATE_QUERY_CHUNK_SIZE]
                        values_sql = ",".join("(?, ?)" for _ in chunk)
                        parameters: list[int | str] = []
                        for gram_size, gram in chunk:
                            parameters.extend((gram_size, gram))
                        rows = connection.execute(
                            "WITH query_grams(gram_size, gram) AS (VALUES "
                            f"{values_sql}) "
                            "SELECT postings.record_id, COUNT(*) AS matched_count "
                            "FROM tm_gram AS postings "
                            "JOIN query_grams AS query "
                            "ON query.gram_size = postings.gram_size "
                            "AND query.gram = postings.gram "
                            "GROUP BY postings.record_id",
                            tuple(parameters),
                        ).fetchall()
                        for row in rows:
                            if (
                                type(row) is not tuple
                                or len(row) != 2
                                or type(row[0]) is not int
                                or row[0] < 1
                                or type(row[1]) is not int
                                or not 1 <= row[1] <= len(chunk)
                            ):
                                raise SQLiteStoreSchemaError("STORE.GRAM_RESULT_INVALID")
                            matched_by_id[row[0]] = matched_by_id.get(row[0], 0) + row[1]
                    connection.commit()
                except sqlite3.Error as error:
                    connection.rollback()
                    raise SQLiteStoreSchemaError(
                        "STORE.GRAM_QUERY_FAILED"
                    ) from error
                except BaseException:
                    connection.rollback()
                    raise
        return tuple(
            sorted(matched_by_id.items(), key=lambda item: (-item[1], item[0]))[
                :candidate_cap
            ]
        )

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
    stage_matches: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    cumulative_ids: set[int] = set()
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
            fts5_available = lease.fts5_available

            fts_ids: set[int] = set()
            if prepared.fts_query_trigrams is not None and fts5_available:
                for chunk in _chunked(prepared.fts_query_trigrams):
                    fts_rows = connection.execute(
                        "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ?",
                        (_build_fts5_match_expression(chunk),),
                    ).fetchall()
                    for row in fts_rows:
                        if type(row) is not tuple or len(row) != 1:
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_RESULT_INVALID"
                            )
                        value = row[0]
                        if type(value) is int:
                            record_id = value
                        elif type(value) is str and value.isdecimal():
                            record_id = int(value)
                        else:
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_RESULT_INVALID"
                            )
                        if record_id < 1:
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_RESULT_INVALID"
                            )
                        fts_ids.add(record_id)
                        cumulative_ids.add(record_id)

            if prepared.fts_query_trigrams is None:
                selected_stages = prepared.query_grams_by_size
            elif not fts5_available:
                selected_stages = prepared.query_grams_by_size
            elif (
                not cumulative_ids
                or prepared.fts_query_degenerate
                or len(cumulative_ids) < prepared.candidate_floor
            ):
                selected_stages = [
                    stage for stage in prepared.query_grams_by_size if stage[0] in {1, 2}
                ]
            else:
                selected_stages = []

            for gram_size, grams in selected_stages:
                if (
                    prepared.fts_query_trigrams is not None
                    and fts5_available
                    and gram_size == 1
                    and len(cumulative_ids) >= prepared.candidate_floor
                ):
                    continue
                matched_by_id: dict[int, int] = {}
                for chunk in _chunked(grams):
                    placeholders = ",".join("?" for _ in chunk)
                    rows = connection.execute(
                        "SELECT record_id, COUNT(*) AS matched_count "
                        "FROM tm_gram WHERE gram_size = ? "
                        f"AND gram IN ({placeholders}) "
                        "GROUP BY record_id",
                        (gram_size, *chunk),
                    ).fetchall()
                    for row in rows:
                        if (
                            type(row) is not tuple
                            or len(row) != 2
                            or type(row[0]) is not int
                            or row[0] < 1
                            or type(row[1]) is not int
                            or not 1 <= row[1] <= len(chunk)
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.CANDIDATE_RESULT_INVALID"
                            )
                        matched_by_id[row[0]] = matched_by_id.get(row[0], 0) + row[1]
                        cumulative_ids.add(row[0])
                stage_matches.append(
                    (f"GRAM_{gram_size}", tuple(matched_by_id.items()))
                )

            folded_sources: list[tuple[int, str]] = []
            candidate_ids = tuple(cumulative_ids)
            for offset in range(0, len(candidate_ids), 512):
                chunk = candidate_ids[offset : offset + 512]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT record_id, source_fold_v1 FROM tm_record "
                    f"WHERE record_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    if (
                        type(row) is not tuple
                        or len(row) != 2
                        or type(row[0]) is not int
                        or row[0] < 1
                        or type(row[1]) is not str
                        or not row[1]
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.CANDIDATE_RESULT_INVALID"
                        )
                    folded_sources.append((row[0], row[1]))
            if {record_id for record_id, _source in folded_sources} != cumulative_ids:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_SOURCE_MISSING"
                )
            if prepared.fts_query_trigrams is not None and fts5_available:
                sources_by_id = dict(folded_sources)
                query_gram_set = set(prepared.fts_query_trigrams)
                fts_matches = tuple(
                    (
                        record_id,
                        len(
                            query_gram_set.intersection(
                                unique_character_ngrams(
                                    sources_by_id[record_id], 3
                                )
                            )
                        ),
                    )
                    for record_id in sorted(fts_ids)
                )
                if any(count < 1 for _record_id, count in fts_matches):
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_RESULT_INVALID"
                    )
                stage_matches.insert(0, ("FTS_TRIGRAM", fts_matches))
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_QUERY_FAILED"
            ) from error
        except Exception:
            connection.rollback()
            raise
    return SQLiteCandidateRecallSnapshot(
        fts5_available=fts5_available,
        stage_matches=tuple(stage_matches),
        folded_sources=tuple(folded_sources),
    )


def _proof_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise SQLiteStoreSchemaError(code)
    return value


def _proof_text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise SQLiteStoreSchemaError(code)
    return value


def _bounded_seed_stages(
    connection: sqlite3.Connection,
    *,
    folded_query: str,
    fts5_available: bool,
    seed_limit: int,
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Execute one real bounded seed on the generation's selected index."""

    if len(folded_query) >= 3 and fts5_available:
        trigrams = unique_character_ngrams(folded_query, 3)
        if not trigrams:
            return (("FTS_TRIGRAM", ()),)
        ids: list[int] = []
        seen: set[int] = set()
        for chunk in _chunked(trigrams):
            rows = connection.execute(
                "SELECT record_id FROM tm_fts WHERE tm_fts MATCH ? "
                "ORDER BY rank, CAST(record_id AS INTEGER) DESC LIMIT ?",
                (_build_fts5_match_expression(chunk), seed_limit),
            ).fetchall()
            for row in rows:
                if type(row) is not tuple or len(row) != 1:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_SEED_INVALID")
                raw_id = row[0]
                if type(raw_id) is int:
                    record_id = raw_id
                elif type(raw_id) is str and raw_id.isdecimal():
                    record_id = int(raw_id)
                else:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_SEED_INVALID")
                if record_id < 1:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_SEED_INVALID")
                if record_id not in seen and len(ids) < seed_limit:
                    seen.add(record_id)
                    ids.append(record_id)
        return (("FTS_TRIGRAM", tuple(ids)),)

    if not folded_query:
        return (("GRAM_1", ()),)
    if len(folded_query) == 1:
        sizes = (1,)
    elif len(folded_query) == 2:
        sizes = (2,)
    else:
        sizes = (3, 2, 1)
    stages: list[tuple[str, tuple[int, ...]]] = []
    for size in sizes:
        grams = unique_character_ngrams(folded_query, size)
        matched: dict[int, int] = {}
        for chunk in _chunked(grams):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT record_id, COUNT(*) FROM tm_gram "
                "WHERE gram_size = ? "
                f"AND gram IN ({placeholders}) GROUP BY record_id",
                (size, *chunk),
            ).fetchall()
            for row in rows:
                if (
                    type(row) is not tuple
                    or len(row) != 2
                    or type(row[0]) is not int
                    or row[0] < 1
                    or type(row[1]) is not int
                    or row[1] < 1
                ):
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_SEED_INVALID")
                matched[row[0]] = matched.get(row[0], 0) + row[1]
        ordered = tuple(
            record_id
            for record_id, _count in sorted(
                matched.items(), key=lambda item: (-item[1], -item[0])
            )[:seed_limit]
        )
        stages.append((f"GRAM_{size}", ordered))
    return tuple(stages)


def _candidate_proof_snapshot_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    seed_limit: int,
) -> SQLiteCandidateProofSnapshot:
    """Read one real seed plus block frontiers without record-level facts."""

    query_characters = Counter(folded_query)
    query_bigrams = Counter(
        folded_query[offset : offset + 2]
        for offset in range(max(0, len(folded_query) - 1))
    )
    query_terms = tuple(
        (size, gram, frequency)
        for size, frequencies in ((1, query_characters), (2, query_bigrams))
        for gram, frequency in frequencies.items()
    )
    with _open_leased_connection(lease) as connection:
        try:
            connection.execute("BEGIN")
            _validate_lease_identity(connection, lease)
            meta = _read_meta(connection)
            head_revision = _meta_int(meta, "head_revision")
            fts5_available = _meta_bool(meta, "fts5_available")
            if fts5_available != lease.fts5_available:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_CAPABILITY_MISMATCH"
                )
            index_kind = (
                "FTS5_TRIGRAM"
                if fts5_available and len(folded_query) >= 3
                else "GRAM_FALLBACK"
            )
            seed_stages = _bounded_seed_stages(
                connection,
                folded_query=folded_query,
                fts5_available=fts5_available,
                seed_limit=seed_limit,
            )

            persisted_maxima: dict[tuple[int, int, str], int] = {}
            if query_terms:
                for offset in range(0, len(query_terms), _CANDIDATE_QUERY_CHUNK_SIZE):
                    chunk = query_terms[offset : offset + _CANDIDATE_QUERY_CHUNK_SIZE]
                    values_sql = ",".join("(?, ?)" for _ in chunk)
                    parameters = []
                    for size, gram, _frequency in chunk:
                        parameters.extend((size, gram))
                    rows = connection.execute(
                        "WITH query_terms(gram_size, gram) AS "
                        f"(VALUES {values_sql}) "
                        "SELECT facts.block_id, facts.gram_size, facts.gram, "
                        "facts.max_term_frequency FROM tm_gram_block_max AS facts "
                        "JOIN query_terms ON query_terms.gram_size = facts.gram_size "
                        "AND query_terms.gram = facts.gram "
                        "ORDER BY facts.block_id, facts.gram_size, facts.gram",
                        tuple(parameters),
                    ).fetchall()
                    for row in rows:
                        if type(row) is not tuple or len(row) != 4:
                            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                        block_id = _proof_int(row[0], "STORE.CANDIDATE_PROOF_INVALID")
                        size = _proof_int(row[1], "STORE.CANDIDATE_PROOF_INVALID")
                        gram = _proof_text(row[2], "STORE.CANDIDATE_PROOF_INVALID")
                        frequency = _proof_int(row[3], "STORE.CANDIDATE_PROOF_INVALID")
                        key = (block_id, size, gram)
                        if size not in {1, 2} or frequency < 1 or key in persisted_maxima:
                            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                        persisted_maxima[key] = frequency
            block_rows = connection.execute(
                "SELECT block_id, first_record_id, last_record_id, record_count, "
                "min_source_fold_length, max_source_fold_length "
                "FROM tm_candidate_block ORDER BY block_id"
            ).fetchall()
            total_record_count = _table_count(connection, "tm_record")
            expected_block_count = (
                total_record_count + CANDIDATE_PROOF_BLOCK_SIZE - 1
            ) // CANDIDATE_PROOF_BLOCK_SIZE
            blocks: list[SQLiteCandidateProofBlock] = []
            covered_records = 0
            for expected_block_id, row in enumerate(block_rows):
                if type(row) is not tuple or len(row) != 6:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                values = tuple(
                    _proof_int(value, "STORE.CANDIDATE_PROOF_INVALID")
                    for value in row
                )
                block_id, first_id, last_id, count, minimum, maximum = values
                expected_first = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
                expected_count = min(
                    CANDIDATE_PROOF_BLOCK_SIZE,
                    total_record_count - expected_first + 1,
                )
                if (
                    block_id != expected_block_id
                    or expected_count < 1
                    or first_id != expected_first
                    or last_id != expected_first + CANDIDATE_PROOF_BLOCK_SIZE - 1
                    or count != expected_count
                    or minimum < 1
                    or maximum < minimum
                ):
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                character_upper = sum(
                    min(
                        query_frequency,
                        persisted_maxima.get((block_id, 1, gram), 0),
                    )
                    for gram, query_frequency in query_characters.items()
                )
                bigram_upper = sum(
                    min(
                        query_frequency,
                        persisted_maxima.get((block_id, 2, gram), 0),
                    )
                    for gram, query_frequency in query_bigrams.items()
                )
                blocks.append(
                    SQLiteCandidateProofBlock(
                        block_id=block_id,
                        first_record_id=first_id,
                        last_record_id=last_id,
                        record_count=count,
                        min_source_fold_length=minimum,
                        max_source_fold_length=maximum,
                        character_intersection_upper=character_upper,
                        bigram_intersection_upper=bigram_upper,
                    )
                )
                covered_records += count
            if (
                len(blocks) != expected_block_count
                or covered_records != total_record_count
                or any(key[0] >= expected_block_count for key in persisted_maxima)
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_QUERY_FAILED") from error
        except Exception:
            connection.rollback()
            raise
    return SQLiteCandidateProofSnapshot(
        index_kind=index_kind,
        seed_stages=seed_stages,
        blocks=tuple(blocks),
        total_record_count=total_record_count,
        head_revision=head_revision,
    )


def _candidate_proof_block_records_body(
    lease: _SQLiteGenerationView,
    *,
    folded_query: str,
    block: SQLiteCandidateProofBlock,
    head_revision: int,
    total_record_count: int,
) -> tuple[SQLiteCandidateProofRecord, ...]:
    """Read and close exact proof facts for exactly one opened 256-slot block."""

    query_characters = Counter(folded_query)
    query_bigrams = Counter(
        folded_query[offset : offset + 2]
        for offset in range(max(0, len(folded_query) - 1))
    )
    query_terms = tuple(
        (size, gram, frequency)
        for size, frequencies in ((1, query_characters), (2, query_bigrams))
        for gram, frequency in frequencies.items()
    )
    final_record_id = min(block.last_record_id, total_record_count)
    with _open_leased_connection(lease) as connection:
        try:
            connection.execute("BEGIN")
            _validate_lease_identity(connection, lease)
            meta = _read_meta(connection)
            if (
                _meta_int(meta, "head_revision") != head_revision
                or _table_count(connection, "tm_record") != total_record_count
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_STALE")
            row = connection.execute(
                "SELECT block_id, first_record_id, last_record_id, record_count, "
                "min_source_fold_length, max_source_fold_length "
                "FROM tm_candidate_block WHERE block_id = ?",
                (block.block_id,),
            ).fetchone()
            expected_block = (
                block.block_id,
                block.first_record_id,
                block.last_record_id,
                block.record_count,
                block.min_source_fold_length,
                block.max_source_fold_length,
            )
            if row != expected_block:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            record_rows = connection.execute(
                "SELECT record_id, source_fold_length FROM tm_record "
                "WHERE record_id BETWEEN ? AND ? ORDER BY record_id",
                (block.first_record_id, final_record_id),
            ).fetchall()
            lengths: dict[int, int] = {}
            for offset, row in enumerate(record_rows):
                if type(row) is not tuple or len(row) != 2:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                record_id = _proof_int(row[0], "STORE.CANDIDATE_PROOF_INVALID")
                length = _proof_int(row[1], "STORE.CANDIDATE_PROOF_INVALID")
                if record_id != block.first_record_id + offset or length < 1:
                    raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                lengths[record_id] = length
            if (
                len(lengths) != block.record_count
                or not lengths
                or min(lengths.values()) != block.min_source_fold_length
                or max(lengths.values()) != block.max_source_fold_length
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")

            intersections = {record_id: [0, 0] for record_id in lengths}
            exact_maxima: dict[tuple[int, str], int] = {}
            persisted_maxima: dict[tuple[int, str], int] = {}
            for offset in range(0, len(query_terms), _CANDIDATE_QUERY_CHUNK_SIZE):
                chunk = query_terms[offset : offset + _CANDIDATE_QUERY_CHUNK_SIZE]
                if not chunk:
                    continue
                values_sql = ",".join("(?, ?, ?)" for _ in chunk)
                parameters: list[int | str] = []
                for size, gram, frequency in chunk:
                    parameters.extend((size, gram, frequency))
                rows = connection.execute(
                    "WITH query_terms(gram_size, gram, query_frequency) "
                    f"AS (VALUES {values_sql}) "
                    "SELECT facts.record_id, facts.gram_size, facts.gram, "
                    "facts.term_frequency, query_terms.query_frequency "
                    "FROM tm_gram AS facts JOIN query_terms "
                    "ON query_terms.gram_size = facts.gram_size "
                    "AND query_terms.gram = facts.gram "
                    "WHERE facts.record_id BETWEEN ? AND ? "
                    "ORDER BY facts.record_id, facts.gram_size, facts.gram",
                    (*parameters, block.first_record_id, final_record_id),
                ).fetchall()
                for row in rows:
                    if type(row) is not tuple or len(row) != 5:
                        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                    record_id = _proof_int(row[0], "STORE.CANDIDATE_PROOF_INVALID")
                    size = _proof_int(row[1], "STORE.CANDIDATE_PROOF_INVALID")
                    gram = _proof_text(row[2], "STORE.CANDIDATE_PROOF_INVALID")
                    frequency = _proof_int(row[3], "STORE.CANDIDATE_PROOF_INVALID")
                    query_frequency = _proof_int(row[4], "STORE.CANDIDATE_PROOF_INVALID")
                    if record_id not in lengths or size not in {1, 2} or frequency < 1:
                        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                    intersections[record_id][size - 1] += min(
                        frequency, query_frequency
                    )
                    exact_maxima[(size, gram)] = max(
                        exact_maxima.get((size, gram), 0), frequency
                    )
                maxima_rows = connection.execute(
                    "WITH query_terms(gram_size, gram, query_frequency) "
                    f"AS (VALUES {values_sql}) "
                    "SELECT facts.gram_size, facts.gram, facts.max_term_frequency "
                    "FROM tm_gram_block_max AS facts JOIN query_terms "
                    "ON query_terms.gram_size = facts.gram_size "
                    "AND query_terms.gram = facts.gram "
                    "WHERE facts.block_id = ? ORDER BY facts.gram_size, facts.gram",
                    (*parameters, block.block_id),
                ).fetchall()
                for row in maxima_rows:
                    if type(row) is not tuple or len(row) != 3:
                        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                    key = (
                        _proof_int(row[0], "STORE.CANDIDATE_PROOF_INVALID"),
                        _proof_text(row[1], "STORE.CANDIDATE_PROOF_INVALID"),
                    )
                    frequency = _proof_int(row[2], "STORE.CANDIDATE_PROOF_INVALID")
                    if key in persisted_maxima or frequency < 1:
                        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                    persisted_maxima[key] = frequency
            if persisted_maxima != exact_maxima:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            character_upper = sum(
                min(query_frequency, persisted_maxima.get((1, gram), 0))
                for gram, query_frequency in query_characters.items()
            )
            bigram_upper = sum(
                min(query_frequency, persisted_maxima.get((2, gram), 0))
                for gram, query_frequency in query_bigrams.items()
            )
            if (
                character_upper != block.character_intersection_upper
                or bigram_upper != block.bigram_intersection_upper
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            records = tuple(
                SQLiteCandidateProofRecord(
                    record_id=record_id,
                    block_id=block.block_id,
                    source_fold_length=length,
                    character_multiset_intersection=intersections[record_id][0],
                    bigram_multiset_intersection=intersections[record_id][1],
                )
                for record_id, length in lengths.items()
            )
            connection.commit()
            return records
        except sqlite3.Error as error:
            connection.rollback()
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_QUERY_FAILED") from error
        except Exception:
            connection.rollback()
            raise


def _validate_candidate_proof_generation_body(
    lease: _SQLiteGenerationView,
    *,
    head_revision: int,
    total_record_count: int,
) -> None:
    with _open_leased_connection(lease) as connection:
        connection.execute("BEGIN")
        try:
            _validate_lease_identity(connection, lease)
            meta = _read_meta(connection)
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
) -> tuple[TMRecord, ...]:
    if not record_ids:
        return ()
    placeholders = ",".join("?" for _ in record_ids)
    with _open_leased_connection(lease) as connection:
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
                fold_text_v1(source_raw).folded_text,
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
    validated_gram_rows: list[tuple[int, str, int, int]] = []
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
        validated_gram_rows.append(
            (
                gram_size,
                gram,
                record_ids_by_ordinal[origin_ordinal],
                term_frequency,
            )
        )
    connection.executemany(
        "INSERT INTO tm_gram(gram_size, gram, record_id, term_frequency) "
        "VALUES (?, ?, ?, ?)",
        validated_gram_rows,
    )
    seen_fts_ordinals: set[int] = set()
    validated_fts_rows: list[tuple[str, int]] = []
    for origin_ordinal in fts_origin_ordinals:
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids_by_ordinal
            or origin_ordinal not in folded_sources_by_ordinal
            or origin_ordinal in seen_fts_ordinals
        ):
            raise ValueError("validated FTS row is invalid")
        seen_fts_ordinals.add(origin_ordinal)
        validated_fts_rows.append(
            (
                folded_sources_by_ordinal[origin_ordinal],
                record_ids_by_ordinal[origin_ordinal],
            )
        )
    if validated_fts_rows:
        try:
            connection.executemany(
                "INSERT INTO tm_fts(source_fold_v1, record_id) VALUES (?, ?)",
                validated_fts_rows,
            )
        except sqlite3.OperationalError as error:
            raise SQLiteStoreSchemaError("STORE.FTS5_UNAVAILABLE") from error
    _maintain_candidate_proof_summaries(
        connection,
        record_ids_by_ordinal=record_ids_by_ordinal,
        folded_sources_by_ordinal=folded_sources_by_ordinal,
        gram_rows=gram_rows,
    )


def _maintain_candidate_proof_summaries(
    connection: sqlite3.Connection,
    *,
    record_ids_by_ordinal: dict[int, int],
    folded_sources_by_ordinal: dict[int, str],
    gram_rows: tuple[tuple[int, int, str, int], ...],
) -> None:
    """Maintain exact proof blocks inside the owning record transaction."""

    for origin_ordinal, record_id in sorted(
        record_ids_by_ordinal.items(), key=lambda pair: pair[1]
    ):
        folded_source = folded_sources_by_ordinal[origin_ordinal]
        source_fold_length = len(folded_source)
        block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        first_record_id = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        last_record_id = first_record_id + CANDIDATE_PROOF_BLOCK_SIZE - 1
        connection.execute(
            "INSERT INTO tm_candidate_block("
            "block_id, first_record_id, last_record_id, record_count, "
            "min_source_fold_length, max_source_fold_length) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(block_id) DO UPDATE SET "
            "record_count = record_count + 1, "
            "min_source_fold_length = min(min_source_fold_length, excluded.min_source_fold_length), "
            "max_source_fold_length = max(max_source_fold_length, excluded.max_source_fold_length)",
            (
                block_id,
                first_record_id,
                last_record_id,
                source_fold_length,
                source_fold_length,
            ),
        )
    for origin_ordinal, gram_size, gram, term_frequency in gram_rows:
        if gram_size not in {1, 2}:
            continue
        record_id = record_ids_by_ordinal[origin_ordinal]
        block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        connection.execute(
            "INSERT INTO tm_gram_block_max("
            "gram_size, gram, block_id, max_term_frequency) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(gram_size, gram, block_id) DO UPDATE SET "
            "max_term_frequency = max(max_term_frequency, excluded.max_term_frequency)",
            (gram_size, gram, block_id, term_frequency),
        )


def validate_candidate_proof_index(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
) -> tuple[tuple[tuple[int, int], ...], int]:
    """Stream-recompute exact length, TF, block and optional FTS facts."""

    if type(connection) is not sqlite3.Connection:
        raise TypeError("connection must be an exact sqlite3 connection")
    if type(required_sizes) is not tuple or any(
        type(size) is not int for size in required_sizes
    ):
        raise TypeError("required_sizes must be a tuple of built-in integers")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    expected_sizes = (1, 2) if fts5_available else (1, 2, 3)
    if required_sizes != expected_sizes:
        raise ValueError("candidate gram sizes do not match the index path")

    def proof_int(value: object) -> int:
        if type(value) is not int:
            raise CandidateProofIndexError("candidate integer fact is invalid")
        return value

    def proof_text(value: object) -> str:
        if type(value) is not str or not value:
            raise CandidateProofIndexError("candidate text fact is invalid")
        return value

    record_cursor = connection.execute(
        "SELECT record_id, source_raw, source_fold_v1, source_fold_length "
        "FROM tm_record ORDER BY record_id"
    )
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram, term_frequency FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    block_cursor = connection.execute(
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id"
    )
    maximum_cursor = connection.execute(
        "SELECT block_id, gram_size, gram, max_term_frequency "
        "FROM tm_gram_block_max ORDER BY block_id, gram_size, gram"
    )
    fts_cursor = (
        connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts ORDER BY record_id"
        )
        if fts5_available
        else None
    )
    current_gram = gram_cursor.fetchone()
    current_block = block_cursor.fetchone()
    current_maximum = maximum_cursor.fetchone()
    current_fts = fts_cursor.fetchone() if fts_cursor is not None else None
    gram_counts = {size: 0 for size in required_sizes}
    fts_count = 0
    block_id: int | None = None
    block_lengths: list[int] = []
    block_maxima: dict[tuple[int, str], int] = {}

    def flush_block() -> None:
        nonlocal current_block, current_maximum
        if block_id is None:
            return
        first_record_id = block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        expected_block = (
            block_id,
            first_record_id,
            first_record_id + CANDIDATE_PROOF_BLOCK_SIZE - 1,
            len(block_lengths),
            min(block_lengths),
            max(block_lengths),
        )
        if current_block is None or tuple(
            proof_int(value) for value in current_block
        ) != expected_block:
            raise CandidateProofIndexError("candidate block fact is invalid")
        current_block = block_cursor.fetchone()
        actual_maxima: dict[tuple[int, str], int] = {}
        while current_maximum is not None:
            maximum_block_id = proof_int(current_maximum[0])
            if maximum_block_id != block_id:
                break
            size = proof_int(current_maximum[1])
            gram = proof_text(current_maximum[2])
            frequency = proof_int(current_maximum[3])
            key = (size, gram)
            if size not in {1, 2} or frequency < 1 or key in actual_maxima:
                raise CandidateProofIndexError(
                    "candidate block maximum is invalid"
                )
            actual_maxima[key] = frequency
            current_maximum = maximum_cursor.fetchone()
        if actual_maxima != block_maxima:
            raise CandidateProofIndexError("candidate block maximum is invalid")

    expected_record_id = 1
    for record_row in record_cursor:
        record_id = proof_int(record_row[0])
        source_raw = proof_text(record_row[1])
        stored_folded_source = proof_text(record_row[2])
        source_fold_length = proof_int(record_row[3])
        folded_source = fold_text_v1(source_raw).folded_text
        if (
            not folded_source
            or record_id != expected_record_id
            or stored_folded_source != folded_source
            or source_fold_length != len(folded_source)
        ):
            raise CandidateProofIndexError("candidate record fact is invalid")
        expected_record_id += 1
        next_block_id = (record_id - 1) // CANDIDATE_PROOF_BLOCK_SIZE
        if block_id is not None and next_block_id != block_id:
            flush_block()
            block_lengths.clear()
            block_maxima.clear()
        block_id = next_block_id
        block_lengths.append(source_fold_length)

        actual_grams: dict[tuple[int, str], int] = {}
        while current_gram is not None:
            gram_record_id = proof_int(current_gram[0])
            if gram_record_id != record_id:
                break
            size = proof_int(current_gram[1])
            gram = proof_text(current_gram[2])
            frequency = proof_int(current_gram[3])
            key = (size, gram)
            if size not in required_sizes or frequency < 1 or key in actual_grams:
                raise CandidateProofIndexError("candidate gram fact is invalid")
            actual_grams[key] = frequency
            current_gram = gram_cursor.fetchone()
        expected_grams = {
            (size, gram): frequency
            for size in required_sizes
            for gram, frequency in character_ngram_frequencies(
                folded_source, size
            )
        }
        if actual_grams != expected_grams:
            raise CandidateProofIndexError("candidate gram fact is invalid")
        for (size, gram), frequency in actual_grams.items():
            gram_counts[size] += 1
            if size in {1, 2}:
                key = (size, gram)
                block_maxima[key] = max(block_maxima.get(key, 0), frequency)

        if fts5_available:
            if current_fts is None:
                raise CandidateProofIndexError("candidate FTS fact is invalid")
            fts_record_id = proof_int(current_fts[0])
            fts_source = proof_text(current_fts[1])
            if (fts_record_id, fts_source) != (record_id, folded_source):
                raise CandidateProofIndexError("candidate FTS fact is invalid")
            fts_count += 1
            current_fts = fts_cursor.fetchone() if fts_cursor is not None else None

    flush_block()
    if any(row is not None for row in (current_gram, current_block, current_maximum)):
        raise CandidateProofIndexError("candidate proof index has extra rows")
    if current_fts is not None:
        raise CandidateProofIndexError("candidate FTS fact is invalid")
    return tuple(sorted(gram_counts.items())), fts_count


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
            _provenance_json,
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
            _provenance_json,
            legacy_line_no,
            batch_id,
            origin_ordinal,
            ),
        )
        record_id = cursor.lastrowid
        if record_id is None:
            raise SQLiteStoreSchemaError("STORE.RECORD_ID_MISSING")
        record_ids_by_ordinal[origin_ordinal] = record_id
    return record_ids_by_ordinal


def _insert_streamed_candidate_index(
    connection: sqlite3.Connection,
    prepared_drafts: tuple[_PreparedRecordDraft, ...],
    record_ids_by_ordinal: dict[int, int],
    *,
    fts5_available: bool,
) -> None:
    """Bulk-insert one bounded chunk's grams and FTS rows in PK order."""

    gram_sizes = (1, 2) if fts5_available else (1, 2, 3)
    grams_by_size: dict[int, dict[str, list[tuple[int, int]]]] = {
        gram_size: {} for gram_size in gram_sizes
    }
    fts_rows: list[tuple[str, int]] = []
    for draft in prepared_drafts:
        origin_ordinal = draft[10]
        folded_source = draft[2]
        record_id = record_ids_by_ordinal[origin_ordinal]
        for gram_size in gram_sizes:
            bucket = grams_by_size[gram_size]
            for gram, term_frequency in character_ngram_frequencies(
                folded_source,
                gram_size,
            ):
                bucket.setdefault(gram, []).append(
                    (record_id, term_frequency)
                )
        if fts5_available:
            fts_rows.append((folded_source, record_id))
    for gram_size in gram_sizes:
        bucket = grams_by_size[gram_size]
        rows = [
            (gram_size, gram, record_id, term_frequency)
            for gram in sorted(bucket)
            for record_id, term_frequency in bucket[gram]
        ]
        connection.executemany(
            "INSERT INTO tm_gram("
            "gram_size, gram, record_id, term_frequency) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    if fts_rows:
        try:
            connection.executemany(
                "INSERT INTO tm_fts(source_fold_v1, record_id) "
                "VALUES (?, ?)",
                fts_rows,
            )
        except sqlite3.OperationalError as error:
            raise SQLiteStoreSchemaError("STORE.FTS5_UNAVAILABLE") from error
    _maintain_candidate_proof_summaries(
        connection,
        record_ids_by_ordinal=record_ids_by_ordinal,
        folded_sources_by_ordinal={draft[10]: draft[2] for draft in prepared_drafts},
        gram_rows=tuple(
            (draft[10], gram_size, gram, term_frequency)
            for draft in prepared_drafts
            for gram_size in gram_sizes
            for gram, term_frequency in character_ngram_frequencies(
                draft[2], gram_size
            )
        ),
    )


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


def _store_health_body(lease: _SQLiteGenerationView) -> StoreHealth:
    """Build one health snapshot from one already-held canonical lease."""

    with _open_leased_connection(lease) as connection:
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
            if schema_version == TM_SCHEMA_VERSION:
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

    def _invalidate(self) -> None:
        self._expired = True

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
        return _records_by_id_body(self._lease, prepared_ids)

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


def initialize_stage_schema(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    _legacy_schema: bool = False,
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
    reservation = _reserve_stage_file(
        path,
        expected_parent=(
            validated_stage.resource_identity.canonical_sidecar_path.parent
        ),
    )
    try:
        with _open_configured_connection(
            path,
            expected_file=reservation,
        ) as connection:
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
        return inspect_stage_schema(
            validated_stage,
            canonical_store_id=canonical_store_id,
            _allow_legacy_schema=_legacy_schema,
        )
    except Exception:
        _remove_reserved_stage_file(path, reservation)
        raise


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
