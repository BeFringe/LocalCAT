"""Safe schema and connection policy for per-resource canonical TM stores.

Tasks 5.6-5.9 add the durable activation journal (PREPARED -> DB_REPLACED
-> MANIFEST_PUBLISHED -> GENERATION_PUBLISHED), idempotent recovery of one
pending activation, and the deterministic Task 5.9 rollback that restores
one complete prior authority (or the legacy first-activation JSONL) when a
journal-authenticated new set cannot be proven at any phase.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from typing import Any, Protocol, cast
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
    SNAPSHOT_MANIFEST_VERSION,
    CanonicalResourceIdentity,
    MutableStageRef,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SealedStage,
    SourceBindingState,
    TMRecord,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)

TM_SCHEMA_VERSION = 2
FOLD_VERSION_V1 = "fold-v1-unicode-16.0.0"
CANDIDATE_INDEX_VERSION = "candidate-index-v1"
BUSY_TIMEOUT_MS = 5000
_CANDIDATE_QUERY_CHUNK_SIZE = 256
_NATIVE_PATH_TYPE = type(Path())

_RECORD_COLUMNS = (
    "record_id, source_raw, target_raw, speaker_raw, context_prev_raw, "
    "context_next_raw, file_source, provenance_json, legacy_line_no, "
    "origin_batch_id, origin_ordinal"
)

_BASE_TABLES = frozenset(
    {
        "tm_gram",
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
    }
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
    False: "d807116c449da67b6186a64c500def04923eaecc9eed8edf47aa9cebeac3d751",
    True: "a8925fe918c9394684eeb61b74052719480f7259cf2fa2ad3322704db21c5b1d",
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

    def __post_init__(self) -> None:
        if type(self.origin_ordinal) is not int or self.origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(self.gram_size) is not int or self.gram_size not in {1, 2, 3}:
            raise ValueError("gram_size must be 1, 2, or 3")
        if type(self.gram) is not str or len(self.gram) != self.gram_size:
            raise ValueError("gram length must equal gram_size")


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
            if type(origin_ordinal) is not int or origin_ordinal < 0:
                raise ValueError(
                    "origin_ordinal must be a non-negative integer"
                )
            if type(gram_size) is not int or gram_size not in {1, 2, 3}:
                raise ValueError("gram_size must be 1, 2, or 3")
            if type(gram) is not str or len(gram) != gram_size:
                raise ValueError("gram length must equal gram_size")
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
            SQLiteGramRow(origin_ordinal, gram_size, gram)
            for origin_ordinal, folded_source in prepared
            for gram_size in gram_sizes
            for gram in unique_character_ngrams(folded_source, gram_size)
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


@dataclass(frozen=True)
class CanonicalRevisionSnapshot:
    """One leased observation of a canonical revision and its ancestry."""

    resource_id: str
    canonical_store_id: str
    generation: int
    head_revision: int
    record_count: int


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
    expected_prior_generation: int | None
    gate_b_grant_digest: str
    had_prior_canonical: bool
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
        expected_prior_generation: int | None,
        gate_b_grant_digest: str,
        had_prior_canonical: bool,
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
        if had_prior_canonical != bool(_backup_assets):
            raise ValueError("activation prior asset state is inconsistent")
        if had_prior_canonical and len(_backup_assets) != 2:
            raise ValueError("activation prior asset set is incomplete")
        if backup_evidence != tuple(
            asset.evidence for asset in _backup_assets
        ):
            raise ValueError("activation backup evidence is inconsistent")
        if type(_sealed_stage) is not SealedStage:
            raise TypeError("activation sealed stage is invalid")
        for name, value in (
            ("preparation_id", preparation_id),
            ("resource_id", resource_id),
            ("target_identity", target_identity),
            ("canonical_store_id", canonical_store_id),
            ("expected_prior_generation", expected_prior_generation),
            ("gate_b_grant_digest", gate_b_grant_digest),
            ("had_prior_canonical", had_prior_canonical),
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

_ACTIVATION_JOURNAL_VERSION = "activation-journal-v1"
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
        "expected_prior_generation",
        "had_prior_canonical",
        "phase",
        "prior_generation",
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


class ResourceStoreCoordinator:
    """Own one resource's leases, sealed registry, and activation authority."""

    def __init__(
        self,
        stage: MutableStageRef | None = None,
        *,
        canonical_store_id: str,
        resource_identity: CanonicalResourceIdentity | None = None,
        drain_timeout_seconds: float = 5.0,
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
        if stage is not None:
            private_stage = _snapshot_store_stage(stage)
            identity = private_stage.resource_identity
            snapshot = inspect_stage_schema(
                private_stage,
                canonical_store_id=canonical_store_id,
                _allow_diverged_runtime=True,
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

    @property
    def resource_id(self) -> str:
        return self._resource_id

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

    def activate(self, sealed_stage: SealedStage) -> _ActivationPreparation:
        """Prepare exactly one registered sealed stage for later activation.

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
            _require_activation_token_identity(
                token,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
                current_generation=initial_generation,
            )
            physical_snapshot = registry.resolve_physical_readiness(
                sealed_stage
            )
            if (
                physical_snapshot.mutable_stage.resource_identity
                != self._resource_identity
                or physical_snapshot.canonical_store_id
                != self._canonical_store_id
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.IDENTITY_MISMATCH",
                    retryable=False,
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
            else:
                captures = _capture_prior_assets(
                    initial_view,
                    identity=self._resource_identity,
                )
                _require_same_asset_captures(
                    pre_drain_captures,
                    captures,
                )
                backups = _create_recovery_backups(
                    captures,
                    preparation_id=preparation_id,
                    owned_paths=owned_paths,
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
                    canonical_store_id=self._canonical_store_id,
                    expected_prior_generation=initial_generation,
                    gate_b_grant_digest=second_grant.grant_digest,
                    had_prior_canonical=initial_view is not None,
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
            return self._publish_activation_journal_locked(preparation)

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
            disk_record = self._load_activation_transition_record_locked(
                preparation,
                handle,
                next_phase,
            )
            self._revalidate_activation_journal_closure(
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
        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        with self._condition:
            prepared_record = self._load_activation_transition_record_locked(
                preparation,
                handle,
                _ActivationJournalPhase.DB_REPLACED,
            )
            # PREPARED keeps the full candidate/prior closure.  It is the
            # last point at which that validation is meaningful because the
            # following atomic replace consumes the candidate path.
            self._revalidate_activation_journal_closure(
                preparation,
                prepared_record,
            )

            _replace_activation_database(
                prepared_record,
                identity=self._resource_identity,
            )
            _validate_replaced_activation_database(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
            )
            handle = self._advance_activation_journal_after_effect_locked(
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
            _publish_activation_receipt(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
                next_generation=next_generation,
                activation_digest=activation_digest,
            )
            _publish_activation_manifest(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
            )
            active_ref, active_snapshot = _validate_published_activation_set(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
                next_generation=next_generation,
                activation_digest=activation_digest,
            )
            handle = self._advance_activation_journal_after_effect_locked(
                preparation,
                handle,
                _ActivationJournalPhase.MANIFEST_PUBLISHED,
                next_generation=next_generation,
                activation_digest=activation_digest,
            )

            # Revalidate the same full active set immediately before the one
            # in-memory generation switch.  State remains ACTIVATING, so no
            # operation can observe this view until the final phase and token
            # consumption are both durable/complete.
            active_ref, active_snapshot = _validate_published_activation_set(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
                next_generation=next_generation,
                activation_digest=activation_digest,
            )
            prior_view = self._view
            self._view = _SQLiteGenerationView(
                stage=active_ref,
                canonical_store_id=self._canonical_store_id,
                generation=next_generation,
                fts5_available=active_snapshot.fts5_available,
            )
            try:
                self._retire_coexisting_terminal_locked(prepared_record)
                handle = self._advance_activation_journal_after_effect_locked(
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
                self._view = prior_view
                raise
            final_ref, _final_snapshot = _validate_published_activation_set(
                prepared_record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
                next_generation=next_generation,
                activation_digest=activation_digest,
            )
            if self._view is None or self._view.stage != final_ref:
                raise ActivationPreparationError(
                    "ACTIVATION.GENERATION_PUBLICATION_INVALID",
                    retryable=False,
                )
            try:
                self._sealed_registry.consume(preparation._token)
            except stage_seal_error as error:
                raise ActivationPreparationError(
                    "ACTIVATION.TOKEN_CONSUME_FAILED",
                    retryable=True,
                    reason_code=error.error_code,
                ) from error
            self._preparation = None
            self._state = "READY"
            self._condition.notify_all()
            return next_generation

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
            if (
                self._state != "READY"
                or self._preparation is not None
                or self._cleanup_reservation is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_STATE_INVALID",
                    retryable=True,
                )
            journal_path = _activation_journal_path(self._resource_identity)
            terminal_path = _activation_terminal_path(self._resource_identity)
            self._state = "ACTIVATING"
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
                        if self._view is not None:
                            self._state = "READY"
                            self._condition.notify_all()
                            return None
                        report = self._discover_active_canonical_locked()
                    else:
                        terminal_record = self._load_recovery_terminal_locked(
                            terminal_path,
                            terminal_identity,
                        )
                        self._revalidate_recovery_authority_locked(
                            terminal_record
                        )
                        if (
                            terminal_record.phase
                            is _ActivationJournalPhase.GENERATION_PUBLISHED
                        ):
                            report = self._replay_terminal_recovery_locked(
                                terminal_record,
                                terminal_identity,
                            )
                        elif (
                            terminal_record.phase
                            is _ActivationJournalPhase.PREPARED
                        ):
                            report = (
                                self._replay_cancelled_terminal_recovery_locked(
                                    terminal_record,
                                )
                            )
                        else:
                            raise ActivationPreparationError(
                                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                                retryable=False,
                            )
                    self._preparation = None
                    self._cleanup_reservation = None
                    self._state = "READY"
                    self._condition.notify_all()
                    return report
                record = self._load_recovery_journal_locked(
                    journal_path,
                    journal_identity,
                )
                self._revalidate_recovery_authority_locked(record)
                rollback_terminal_coexists = False
                if terminal_identity is not None:
                    terminal_record = self._load_recovery_terminal_locked(
                        terminal_path,
                        terminal_identity,
                    )
                    self._revalidate_recovery_authority_locked(
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
                        report = self._complete_prepared_cancellation_locked(
                            record,
                            journal_identity,
                        )
                    elif record.phase is _ActivationJournalPhase.DB_REPLACED:
                        report = self._recover_manifest_publication_locked(
                            record,
                            journal_identity,
                        )
                    elif (
                        record.phase
                        is _ActivationJournalPhase.MANIFEST_PUBLISHED
                    ):
                        report = self._recover_generation_publication_locked(
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
                                _revalidate_recovered_active_set(
                                    record,
                                    identity=self._resource_identity,
                                    canonical_store_id=self._canonical_store_id,
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
                        report = self._replay_terminal_recovery_locked(
                            record,
                            journal_identity,
                        )
                except ActivationPreparationError as error:
                    if not _activation_rollback_eligible(error):
                        raise
                    # Task 5.9: completion cannot be proven, so restore one
                    # complete prior authority (or the legacy first-activation
                    # state) instead of leaving the resource fail-stopped.
                    report = self._rollback_inconsistent_activation_locked(
                        record,
                        journal_identity,
                    )
                self._preparation = None
                self._cleanup_reservation = None
                self._state = "READY"
                self._condition.notify_all()
                return report
            except BaseException:
                self._condition.notify_all()
                raise


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

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        with self._condition:
            if (
                self._state not in {"READY", "ACTIVATING"}
                or self._cleanup_in_progress
                or self._cleanup_reservation is not None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.ROLLBACK_STATE_INVALID",
                    retryable=True,
                )
            journal_path = _activation_journal_path(self._resource_identity)
            terminal_path = _activation_terminal_path(self._resource_identity)
            self._state = "ACTIVATING"
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
                        if self._view is not None:
                            self._state = "READY"
                            self._condition.notify_all()
                            return None
                        report = self._discover_active_canonical_locked()
                    else:
                        terminal_record = self._load_recovery_terminal_locked(
                            terminal_path,
                            terminal_identity,
                        )
                        self._revalidate_recovery_authority_locked(
                            terminal_record
                        )
                        if (
                            terminal_record.phase
                            is _ActivationJournalPhase.GENERATION_PUBLISHED
                        ):
                            report = self._replay_terminal_recovery_locked(
                                terminal_record,
                                terminal_identity,
                            )
                        elif (
                            terminal_record.phase
                            is _ActivationJournalPhase.PREPARED
                        ):
                            report = (
                                self._replay_cancelled_terminal_recovery_locked(
                                    terminal_record,
                                )
                            )
                        else:
                            raise ActivationPreparationError(
                                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                                retryable=False,
                            )
                    self._preparation = None
                    self._cleanup_reservation = None
                    self._state = "READY"
                    self._condition.notify_all()
                    return report
                record = self._load_recovery_journal_locked(
                    journal_path,
                    journal_identity,
                )
                self._revalidate_recovery_authority_locked(record)
                if terminal_identity is not None:
                    terminal_record = self._load_recovery_terminal_locked(
                        terminal_path,
                        terminal_identity,
                    )
                    self._revalidate_recovery_authority_locked(
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
                        _revalidate_recovered_active_set(
                            record,
                            identity=self._resource_identity,
                            canonical_store_id=self._canonical_store_id,
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
                if self._preparation is not None:
                    try:
                        entry = self._sealed_registry._token_entry(
                            self._preparation._token
                        )
                    except stage_seal_error:
                        entry = None
                    if entry is not None and (
                        entry.state
                        is contract_module.ActivationCapabilityState.TOKEN_ISSUED
                    ):
                        try:
                            self._sealed_registry.cancel(
                                self._preparation._token
                            )
                        except stage_seal_error as error:
                            raise ActivationPreparationError(
                                "ACTIVATION.ROLLBACK_TOKEN_CANCEL_FAILED",
                                retryable=True,
                                reason_code=error.error_code,
                            ) from error
                report = self._rollback_inconsistent_activation_locked(
                    record,
                    journal_identity,
                )
                self._preparation = None
                self._cleanup_reservation = None
                self._state = "READY"
                self._condition.notify_all()
                return report
            except BaseException:
                self._condition.notify_all()
                raise

    def _rollback_inconsistent_activation_locked(
        self,
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

        identity = self._resource_identity
        canonical_store_id = self._canonical_store_id
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
            )
        else:
            if (
                record.prior_db_path is None
                or record.prior_manifest_path is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_MISMATCH",
                    retryable=False,
                )
            try:
                fts5_available = _revalidate_recovered_prior_set(
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
                _restore_activation_file(
                    record,
                    manifest_backup[0],
                    manifest_backup[1],
                    record.prior_manifest_path,
                )
                fts5_available = _revalidate_recovered_prior_set(
                    record,
                    identity=identity,
                    canonical_store_id=canonical_store_id,
                    allow_restored_identities=True,
                )
            db_identity = _recovery_capture_journal_file(
                record.prior_db_path
            )[0]
            manifest_identity = _recovery_capture_journal_file(
                record.prior_manifest_path
            )[0]
            terminal_record = replace(
                record,
                phase=_ActivationJournalPhase.PREPARED,
                prior_db_identity=(db_identity.device, db_identity.inode),
                prior_manifest_identity=(
                    manifest_identity.device,
                    manifest_identity.inode,
                ),
            )
            prior_generation = record.prior_generation
        self._retire_coexisting_terminal_locked(record)
        _ = self._write_activation_terminal_locked(terminal_record)
        _remove_owned_activation_journal_final(
            _activation_journal_path(identity),
            journal_identity,
        )
        _remove_journal_proven_backups(record)
        if record.had_prior_canonical:
            self._view = _rollback_restored_prior_view(
                record,
                identity=identity,
                canonical_store_id=canonical_store_id,
                fts5_available=fts5_available,
            )
        else:
            self._view = None
        return ActivationRecoveryReport(
            phase=record.phase.value,
            action="ROLLED_BACK",
            generation=prior_generation,
        )

    def _discover_active_canonical_locked(
        self,
    ) -> ActivationRecoveryReport | None:
        """Re-prove and rehydrate the active canonical generation (no journal).

        With no journal on disk the deterministic canonical sidecar/manifest
        pair is the only surviving authority.  A fully validated completed
        activation is hydrated into the one in-memory view and reported as a
        terminal COMPLETED; an absent pair is the unchanged legacy state
        (``None``); anything partial, foreign, or tampered fails closed in
        ``ACTIVATING`` and never authorizes a store.
        """

        identity = self._resource_identity
        if (
            not _lstat_any_entry(identity.canonical_sidecar_path)
            and not _lstat_any_entry(identity.snapshot_manifest_path)
        ):
            return None
        try:
            generation, fts5_available = _revalidate_discovered_active_set(
                identity,
                canonical_store_id=self._canonical_store_id,
            )
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_DISCOVERY_FAILED",
                retryable=True,
            ) from error
        self._view = _SQLiteGenerationView(
            stage=_canonical_activation_ref(
                identity,
                journal_id="discovery",
            ),
            canonical_store_id=self._canonical_store_id,
            generation=generation,
            fts5_available=fts5_available,
        )
        return ActivationRecoveryReport(
            phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
            action="COMPLETED",
            generation=generation,
        )

    def _load_recovery_journal_locked(
        self,
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

    def _load_recovery_terminal_locked(
        self,
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
            return self._load_recovery_journal_locked(
                terminal_path,
                terminal_identity,
                expected_record_journal_path=_activation_journal_path(
                    self._resource_identity
                ),
            )
        except ActivationPreparationError as error:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                retryable=False,
                reason_code=error.code,
            ) from error

    def _coexisting_terminal_locked(
        self,
        main_record: _ActivationJournalRecord,
    ) -> tuple[_ActivationFileIdentity, _ActivationJournalRecord] | None:
        """Load and rule-validate a coexisting terminal record, if any.

        Returns ``None`` when no terminal file exists; otherwise the exact
        terminal identity plus its fully re-proven record.  A foreign or
        tampered terminal (or any coexistence that does not close the same
        canonical state) fails closed and is never used or overwritten.
        """

        terminal_path = _activation_terminal_path(self._resource_identity)
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
        terminal_record = self._load_recovery_terminal_locked(
            terminal_path,
            terminal_identity,
        )
        self._revalidate_recovery_authority_locked(
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

    def _retire_coexisting_terminal_locked(
        self,
        main_record: _ActivationJournalRecord,
    ) -> None:
        """Strictly retire a validated coexisting terminal record.

        Called only after the pending main journal (or the new CANCELLED
        terminal) is durable and revalidated, so the prior terminal may be
        retired without ever leaving the resource without a valid authority.
        """

        coexisting = self._coexisting_terminal_locked(main_record)
        if coexisting is None:
            return
        terminal_identity, _terminal_record = coexisting
        _remove_owned_activation_terminal_final(
            _activation_terminal_path(self._resource_identity),
            terminal_identity,
        )

    def _revalidate_recovery_authority_locked(
        self,
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

        identity = self._resource_identity
        if (
            record.journal_version != _ACTIVATION_JOURNAL_VERSION
            or record.journal_id != f"journal.{record.preparation_id}"
            or record.journal_path != _activation_journal_path(identity)
            or record.resource_id != identity.resource_id
            or record.target_identity != identity.target_identity
            or record.canonical_store_id != self._canonical_store_id
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
            if (
                record.expected_prior_generation is None
                or record.prior_db_path is None
                or record.prior_db_digest is None
                or record.prior_db_identity is None
                or record.prior_manifest_path is None
                or record.prior_manifest_digest is None
                or record.prior_manifest_identity is None
                or record.prior_binding_snapshot_id is None
                or record.prior_receipt_digest is None
                or record.prior_db_backup_path is None
                or record.prior_db_backup_digest is None
                or record.prior_db_backup_identity is None
                or record.prior_manifest_backup_path is None
                or record.prior_manifest_backup_digest is None
                or record.prior_manifest_backup_identity is None
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_MISMATCH",
                    retryable=False,
                )
        else:
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
        if check_view and self._view is not None:
            if record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
                expected_view_generation = (
                    0
                    if record.expected_prior_generation is None
                    else record.expected_prior_generation + 1
                )
                if (
                    self._view.stage.staged_db_path
                    != identity.canonical_sidecar_path
                    or self._view.generation != expected_view_generation
                ):
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_VIEW_MISMATCH",
                        retryable=False,
                    )
            elif (
                not record.had_prior_canonical
                or record.prior_db_path is None
                or self._view.stage.staged_db_path != record.prior_db_path
                or self._view.generation != record.prior_generation
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.RECOVERY_VIEW_MISMATCH",
                    retryable=False,
                )

    def _complete_prepared_cancellation_locked(
        self,
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

        identity = self._resource_identity
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
            _revalidate_recovered_sealed_database(
                record,
                stage_ref=candidate_ref,
                database_path=record.candidate_stage_db_path,
                identity=identity,
                canonical_store_id=self._canonical_store_id,
            )
        except ActivationPreparationError as error:
            raise _recovery_mismatch(error) from error
        if record.had_prior_canonical:
            fts5_available = _revalidate_recovered_prior_set(
                record,
                identity=identity,
                canonical_store_id=self._canonical_store_id,
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
        _remove_journal_proven_backups(record)
        self._retire_coexisting_terminal_locked(record)
        _ = self._write_activation_terminal_locked(record)
        _remove_owned_activation_journal_final(
            _activation_journal_path(identity),
            journal_identity,
        )
        if record.had_prior_canonical:
            prior_db_path = record.prior_db_path
            prior_manifest_path = record.prior_manifest_path
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
            self._view = _SQLiteGenerationView(
                stage=prior_ref,
                canonical_store_id=self._canonical_store_id,
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

    def _recover_manifest_publication_locked(
        self,
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

        identity = self._resource_identity
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
        _complete_recovered_receipt(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
        )
        _complete_recovered_manifest(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
        )
        _ = _revalidate_recovered_active_set(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
            require_manifest_published=True,
        )
        journal_path = _activation_journal_path(identity)
        manifest_record = replace(record, phase=_ActivationJournalPhase.MANIFEST_PUBLISHED)
        journal_identity = self._write_activation_journal_locked(
            manifest_record,
            journal_path,
            expected_final_identity=journal_identity,
        ).file_identity
        return self._recover_generation_publication_locked(
            manifest_record,
            journal_identity,
            report_phase=_ActivationJournalPhase.DB_REPLACED,
        )

    def _recover_generation_publication_locked(
        self,
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

        identity = self._resource_identity
        next_generation = (
            0
            if record.expected_prior_generation is None
            else record.expected_prior_generation + 1
        )
        activation_digest = _activation_publication_digest(
            record,
            next_generation=next_generation,
        )
        snapshot = _revalidate_recovered_active_set(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
            require_manifest_published=True,
        )
        self._view = _SQLiteGenerationView(
            stage=_canonical_activation_ref(
                identity,
                journal_id=record.journal_id,
            ),
            canonical_store_id=self._canonical_store_id,
            generation=next_generation,
            fts5_available=snapshot.fts5_available,
        )
        try:
            self._retire_coexisting_terminal_locked(record)
            terminal_record = replace(
                record,
                phase=_ActivationJournalPhase.GENERATION_PUBLISHED,
            )
            journal_path = _activation_journal_path(identity)
            journal_identity = self._write_activation_journal_locked(
                terminal_record,
                journal_path,
                expected_final_identity=journal_identity,
            ).file_identity
        except BaseException:
            self._view = None
            raise
        _ = _revalidate_recovered_active_set(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
            require_manifest_published=True,
        )
        _remove_journal_proven_backups(record)
        return ActivationRecoveryReport(
            phase=report_phase.value,
            action="COMPLETED",
            generation=next_generation,
        )

    def _replay_terminal_recovery_locked(
        self,
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

        identity = self._resource_identity
        next_generation = (
            0
            if record.expected_prior_generation is None
            else record.expected_prior_generation + 1
        )
        activation_digest = _activation_publication_digest(
            record,
            next_generation=next_generation,
        )
        snapshot = _revalidate_recovered_active_set(
            record,
            identity=identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
            require_manifest_published=True,
        )
        self._view = _SQLiteGenerationView(
            stage=_canonical_activation_ref(
                identity,
                journal_id=record.journal_id,
            ),
            canonical_store_id=self._canonical_store_id,
            generation=next_generation,
            fts5_available=snapshot.fts5_available,
        )
        _remove_journal_proven_backups(record)
        return ActivationRecoveryReport(
            phase=_ActivationJournalPhase.GENERATION_PUBLISHED.value,
            action="COMPLETED",
            generation=next_generation,
        )

    def _replay_cancelled_terminal_recovery_locked(
        self,
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

        identity = self._resource_identity
        if record.phase is not _ActivationJournalPhase.PREPARED:
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                retryable=False,
            )
        if not record.had_prior_canonical:
            try:
                _require_first_activation_absence(identity)
            except ActivationPreparationError as error:
                raise _recovery_mismatch(error) from error
            return None
        try:
            fts5_available = _revalidate_recovered_prior_set(
                record,
                identity=identity,
                canonical_store_id=self._canonical_store_id,
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
        if (
            record.prior_db_path != identity.canonical_sidecar_path
            and _lstat_any_entry(identity.canonical_sidecar_path)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_MISMATCH",
                retryable=False,
            )
        prior_db_path = record.prior_db_path
        prior_manifest_path = record.prior_manifest_path
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
        self._view = _SQLiteGenerationView(
            stage=prior_ref,
            canonical_store_id=self._canonical_store_id,
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

    def _load_activation_transition_record_locked(
        self,
        preparation: _ActivationPreparation,
        handle: _ActivationJournalHandle,
        next_phase: _ActivationJournalPhase,
    ) -> _ActivationJournalRecord:
        """Load one exact current journal record and enforce phase order."""

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
        if (
            handle.journal_path
            != _activation_journal_path(self._resource_identity)
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

        disk_record = self._load_activation_transition_record_locked(
            preparation,
            handle,
            next_phase,
        )
        self._revalidate_activation_effect_closure(
            preparation,
            disk_record,
            next_phase=next_phase,
            next_generation=next_generation,
            activation_digest=activation_digest,
        )
        next_record = replace(disk_record, phase=next_phase)
        return self._write_activation_journal_locked(
            next_record,
            handle.journal_path,
            expected_final_identity=handle.file_identity,
        )

    def _publish_activation_journal_locked(
        self,
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

        journal_path = _activation_journal_path(self._resource_identity)
        terminal_path = _activation_terminal_path(self._resource_identity)
        record = self._build_activation_journal_record(preparation)
        existing = _lstat_activation_journal_identity(journal_path)
        if existing is None:
            coexisting = self._coexisting_terminal_locked(record)
            handle = self._write_activation_journal_locked(
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
            coexisting = self._coexisting_terminal_locked(record)
            handle = self._replay_activation_journal_locked(
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
        identity = self._resource_identity
        if (
            disk_record.phase is not _ActivationJournalPhase.GENERATION_PUBLISHED
            or disk_record.journal_path != journal_path
            or disk_record.resource_id != identity.resource_id
            or disk_record.target_identity != identity.target_identity
            or disk_record.canonical_store_id != self._canonical_store_id
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
                retryable=False,
            )
        coexisting = self._coexisting_terminal_locked(disk_record)
        if coexisting is None:
            terminal_identity = self._write_activation_terminal_locked(
                disk_record
            )
        else:
            terminal_identity = coexisting[0]
        _remove_owned_activation_journal_final(journal_path, existing)
        handle = self._write_activation_journal_locked(
            record,
            journal_path,
            expected_final_identity=None,
        )
        _remove_owned_activation_terminal_final(
            terminal_path,
            terminal_identity,
        )
        return handle

    def _replay_activation_journal_locked(
        self,
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

    def _write_activation_journal_locked(
        self,
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

    def _write_activation_terminal_locked(
        self,
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

        terminal_path = _activation_terminal_path(self._resource_identity)
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

    def _build_activation_journal_record(
        self,
        preparation: _ActivationPreparation,
    ) -> _ActivationJournalRecord:
        """Derive the complete PREPARED closure from live registry facts.

        Every fact comes from coordinator-owned state: the registry's sealed
        entry and physical readiness snapshot, the registry token, the
        preparation's backups, the prior generation view, and the canonical
        resource identity.  No caller-supplied path, grant, token, nonce,
        phase, or mapping is accepted as authority.
        """

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
        stage = preparation._sealed_stage
        token = preparation._token
        registry = cast(Any, self._sealed_registry)
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
        identity = self._resource_identity
        evidence = physical.evidence
        receipt = evidence.source_binding.receipt
        if (
            physical.registry_namespace != registry.registry_namespace
            or physical.resource_id != identity.resource_id
            or physical.target_identity != identity.target_identity
            or physical.canonical_store_id != self._canonical_store_id
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
        if source_capture[1] != receipt.jsonl_digest:
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
        if had_prior:
            if prior_view is None or self._view is not prior_view:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            try:
                captures = _capture_prior_assets(
                    prior_view,
                    identity=identity,
                )
                with _open_configured_connection(
                    prior_view.stage.staged_db_path,
                    require_existing=True,
                ) as connection:
                    facts = _read_source_binding_facts(connection, prior_view)
            except ActivationPreparationError as error:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                    reason_code=error.code,
                ) from error
            database, manifest, source = captures
            if (
                facts.binding is None
                or facts.divergence_latched
                or facts.diagnostic_codes
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            binding = facts.binding
            backups_by_kind = {
                asset.asset_kind: asset for asset in preparation._backup_assets
            }
            if set(backups_by_kind) != {"DATABASE", "MANIFEST"}:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            prior_db_asset = backups_by_kind["DATABASE"]
            prior_manifest_asset = backups_by_kind["MANIFEST"]
            if (
                database.identity != prior_db_asset.original_identity
                or database.digest
                != prior_db_asset.evidence.original_digest
                or manifest.identity != prior_manifest_asset.original_identity
                or manifest.digest
                != prior_manifest_asset.evidence.original_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
            prior_db_backup_capture = _capture_journal_closure_file(
                prior_db_asset.backup_path
            )
            prior_manifest_backup_capture = _capture_journal_closure_file(
                prior_manifest_asset.backup_path
            )
            if (
                prior_db_backup_capture
                != (
                    prior_db_asset.backup_identity,
                    prior_db_asset.evidence.backup_digest,
                )
                or prior_manifest_backup_capture
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
            prior_manifest_digest_value = manifest.digest
            prior_db_path = database.path
            prior_manifest_path = identity.snapshot_manifest_path
            prior_db_digest_value = database.digest
            prior_db_identity_value = (
                database.identity.device,
                database.identity.inode,
            )
            prior_manifest_identity_value = (
                manifest.identity.device,
                manifest.identity.inode,
            )
            prior_db_backup_path = prior_db_asset.backup_path
            prior_manifest_backup_path = prior_manifest_asset.backup_path
            prior_db_backup_digest_value = (
                prior_db_asset.evidence.backup_digest
            )
            prior_manifest_backup_digest_value = (
                prior_manifest_asset.evidence.backup_digest
            )
            prior_db_backup_identity_value = (
                prior_db_asset.backup_identity.device,
                prior_db_asset.backup_identity.inode,
            )
            prior_manifest_backup_identity_value = (
                prior_manifest_asset.backup_identity.device,
                prior_manifest_asset.backup_identity.inode,
            )
        else:
            if self._view is not None or prior_view is not None:
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
        )
        return record

    def _revalidate_activation_journal_closure(
        self,
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

        stage_seal_error = getattr(
            importlib.import_module("tm_stage_sealer"),
            "StageSealError",
        )
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
        registry = cast(Any, self._sealed_registry)
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
        identity = self._resource_identity
        if (
            record.new_manifest_path != identity.snapshot_manifest_path
            or record.journal_path != _activation_journal_path(identity)
            or record.preparation_id != preparation.preparation_id
            or record.gate_b_grant_digest != preparation.gate_b_grant_digest
            or record.had_prior_canonical != preparation.had_prior_canonical
            or record.resource_id != preparation.resource_id
            or record.target_identity != preparation.target_identity
            or record.canonical_store_id != preparation.canonical_store_id
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
        view = self._view
        if record.had_prior_canonical:
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
                manifest = _capture_activation_file(
                    identity.snapshot_manifest_path,
                    asset_kind="MANIFEST",
                )
                with _open_configured_connection(
                    view.stage.staged_db_path,
                    require_existing=True,
                ) as connection:
                    facts = _read_source_binding_facts(connection, view)
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
                or (manifest.identity.device, manifest.identity.inode)
                != record.prior_manifest_identity
                or manifest.digest != record.prior_manifest_digest
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    retryable=False,
                )
            if (
                facts.binding is None
                or facts.divergence_latched
                or facts.diagnostic_codes
                or _configured_pair_diagnostics(
                    facts.binding,
                    identity=identity,
                    canonical_store_id=view.canonical_store_id,
                    head_revision=facts.head_revision,
                    cumulative_record_counts=facts.cumulative_record_counts,
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
                or manifest.path != record.prior_manifest_path
            ):
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
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
            if set(backups_by_kind) != {"DATABASE", "MANIFEST"}:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            prior_db_asset = backups_by_kind["DATABASE"]
            prior_manifest_asset = backups_by_kind["MANIFEST"]
            db_backup_capture = _capture_journal_closure_file(
                prior_db_asset.backup_path
            )
            manifest_backup_capture = _capture_journal_closure_file(
                prior_manifest_asset.backup_path
            )
            if (
                (
                    db_backup_capture[0].device,
                    db_backup_capture[0].inode,
                )
                != record.prior_db_backup_identity
                or db_backup_capture[1] != record.prior_db_backup_digest
                or (
                    manifest_backup_capture[0].device,
                    manifest_backup_capture[0].inode,
                )
                != record.prior_manifest_backup_identity
                or manifest_backup_capture[1]
                != record.prior_manifest_backup_digest
                or db_backup_capture
                != (
                    prior_db_asset.backup_identity,
                    prior_db_asset.evidence.backup_digest,
                )
                or manifest_backup_capture
                != (
                    prior_manifest_asset.backup_identity,
                    prior_manifest_asset.evidence.backup_digest,
                )
                or prior_db_asset.backup_path != record.prior_db_backup_path
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
        self,
        preparation: _ActivationPreparation,
        record: _ActivationJournalRecord,
        *,
        next_phase: _ActivationJournalPhase,
        next_generation: int | None,
        activation_digest: str | None,
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
            registry=cast(Any, self._sealed_registry),
            identity=self._resource_identity,
            canonical_store_id=self._canonical_store_id,
        )
        if next_phase is _ActivationJournalPhase.DB_REPLACED:
            if next_generation is not None or activation_digest is not None:
                raise ActivationPreparationError(
                    "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                    retryable=False,
                )
            _validate_replaced_activation_database(
                record,
                preparation=preparation,
                identity=self._resource_identity,
                canonical_store_id=self._canonical_store_id,
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
        active_ref, _snapshot = _validate_published_activation_set(
            record,
            preparation=preparation,
            identity=self._resource_identity,
            canonical_store_id=self._canonical_store_id,
            next_generation=next_generation,
            activation_digest=activation_digest,
        )
        if next_phase is _ActivationJournalPhase.MANIFEST_PUBLISHED:
            return
        if next_phase is not _ActivationJournalPhase.GENERATION_PUBLISHED:
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_PHASE_INVALID",
                retryable=False,
            )
        view = self._view
        if (
            view is None
            or type(view.stage) is not _CanonicalStoreRef
            or view.stage != active_ref
            or view.canonical_store_id != self._canonical_store_id
            or view.generation != next_generation
        ):
            raise ActivationPreparationError(
                "ACTIVATION.GENERATION_PUBLICATION_INVALID",
                retryable=False,
            )

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


def _activation_file_identity(path: Path) -> _ActivationFileIdentity:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if not stat.S_ISREG(observed.st_mode):
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
        if not stat.S_ISREG(observed.st_mode):
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
        return bytes(payload)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    finally:
        os.close(descriptor)


def _capture_pre_drain_assets(
    view: _SQLiteGenerationView | None,
    *,
    identity: CanonicalResourceIdentity,
) -> tuple[_PriorAssetCapture, ...]:
    if view is None:
        _require_first_activation_absence(identity)
        return (
            _capture_activation_file(
                identity.configured_jsonl_path,
                asset_kind="SOURCE",
            ),
        )
    return (
        _capture_activation_file(
            view.stage.staged_db_path,
            asset_kind="DATABASE",
        ),
        _capture_activation_file(
            identity.snapshot_manifest_path,
            asset_kind="MANIFEST",
        ),
        _capture_activation_file(
            identity.configured_jsonl_path,
            asset_kind="SOURCE",
        ),
    )


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


def _capture_prior_assets(
    view: _SQLiteGenerationView,
    *,
    identity: CanonicalResourceIdentity,
) -> tuple[_PriorAssetCapture, ...]:
    if (
        view.stage.resource_identity != identity
        or view.stage.resource_identity.resource_id != identity.resource_id
        or view.stage.resource_identity.target_identity != identity.target_identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.IDENTITY_MISMATCH",
            retryable=False,
        )
    with _open_configured_connection(
        view.stage.staged_db_path,
        require_existing=True,
    ) as connection:
        facts = _read_source_binding_facts(connection, view)
    binding = facts.binding
    if (
        facts.divergence_latched
        or facts.diagnostic_codes
        or binding is None
        or _configured_pair_diagnostics(
            binding,
            identity=identity,
            canonical_store_id=view.canonical_store_id,
            head_revision=facts.head_revision,
            cumulative_record_counts=facts.cumulative_record_counts,
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_BINDING_INVALID",
            retryable=False,
        )
    database = _capture_activation_file(
        view.stage.staged_db_path,
        asset_kind="DATABASE",
    )
    manifest = _capture_activation_file(
        identity.snapshot_manifest_path,
        asset_kind="MANIFEST",
    )
    source = _capture_activation_file(
        identity.configured_jsonl_path,
        asset_kind="SOURCE",
    )
    if source.digest != binding.receipt.jsonl_digest:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_BINDING_INVALID",
            retryable=False,
        )
    manifest_payload = _read_activation_file_bytes(manifest)
    if manifest_payload != contract_to_json(binding.manifest).encode("utf-8"):
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
    if type(decoded) is not SnapshotManifest or decoded != binding.manifest:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_BINDING_INVALID",
            retryable=False,
        )
    return database, manifest, source


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
) -> tuple[_RecoveryBackupAsset, ...]:
    backup_captures = tuple(
        capture
        for capture in captures
        if capture.asset_kind in {"DATABASE", "MANIFEST"}
    )
    if tuple(capture.asset_kind for capture in backup_captures) != (
        "DATABASE",
        "MANIFEST",
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
            or (observed.st_dev, observed.st_ino)
            != (expected_identity.device, expected_identity.inode)
        ):
            raise OSError("activation file identity changed")
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
        if (
            len(preparation._backup_assets) != 2
            or set(backups) != {"DATABASE", "MANIFEST"}
        ):
            raise ActivationPreparationError(
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
                retryable=False,
            )
        for kind, path, expected_identity, expected_digest in (
            (
                "DATABASE",
                record.prior_db_backup_path,
                record.prior_db_backup_identity,
                record.prior_db_backup_digest,
            ),
            (
                "MANIFEST",
                record.prior_manifest_backup_path,
                record.prior_manifest_backup_identity,
                record.prior_manifest_backup_digest,
            ),
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
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> SQLiteSchemaSnapshot:
    """Reopen and independently revalidate the still-SEALED canonical DB."""

    canonical = _capture_journal_closure_file(identity.canonical_sidecar_path)
    if (
        (canonical[0].device, canonical[0].inode)
        != record.candidate_stage_db_identity
        or canonical[1] != record.stage_db_digest
        or _lstat_any_entry(record.candidate_stage_db_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        )
    active_ref = _canonical_activation_ref(identity, journal_id=record.journal_id)
    try:
        stage_sealer = importlib.import_module("tm_stage_sealer")
        facts = stage_sealer._validate_stage_facts(
            cast(MutableStageRef, cast(object, active_ref)),
            canonical_store_id=canonical_store_id,
            allow_sealed=True,
        )
        snapshot = inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_sealed=True,
        )
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.DB_REOPEN_INVALID",
            retryable=False,
        ) from error
    evidence = preparation._sealed_stage.evidence
    if (
        facts.resource_id != evidence.resource_id
        or facts.target_identity != evidence.target_identity
        or facts.schema_version != evidence.schema_version
        or facts.fold_version != evidence.fold_version
        or facts.index_version != evidence.index_version
        or facts.record_count != evidence.record_count
        or facts.origin_batch_count != evidence.origin_batch_count
        or facts.fts_count != evidence.fts_count
        or facts.gram_counts != evidence.gram_counts
        or facts.exact_parity_digest != evidence.exact_parity_digest
        or snapshot.activation_status != "SEALED"
    ):
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
        with _open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                meta = _read_meta(connection)
                if (
                    meta.get("resource_id") != identity.resource_id
                    or meta.get("canonical_store_id") != canonical_store_id
                    or meta.get("target_identity") != identity.target_identity
                    or meta.get("activation_status") != "SEALED"
                    or "activation_digest" in meta
                    or _meta_int(meta, "generation") != 0
                ):
                    raise SQLiteStoreSchemaError(
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
                    raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                if connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_binding"
                ).fetchone() != (0,):
                    raise SQLiteStoreSchemaError("STORE.BINDING_INVALID")
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
                    raise SQLiteStoreSchemaError(
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


def _activation_exact_parity_digest(connection: sqlite3.Connection) -> str:
    winners: dict[str, str] = {}
    for source_raw, target_raw in connection.execute(
        "SELECT source_raw, target_raw FROM tm_record ORDER BY record_id"
    ):
        if type(source_raw) is not str or type(target_raw) is not str:
            raise SQLiteStoreSchemaError("STORE.RECORD_INVALID")
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


def _validate_activation_indexes(
    connection: sqlite3.Connection,
    *,
    evidence: contract_module.StageValidationEvidence,
    fts5_available: bool,
) -> None:
    required_sizes = tuple(size for size, _count in evidence.gram_counts)
    expected_counts = dict(evidence.gram_counts)
    actual_counts = {size: 0 for size in required_sizes}
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    current_gram = gram_cursor.fetchone()
    for record_id, folded_source in connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
    ):
        if type(record_id) is not int or type(folded_source) is not str:
            raise SQLiteStoreSchemaError("STORE.RECORD_INVALID")
        actual: set[tuple[int, str]] = set()
        while current_gram is not None and current_gram[0] == record_id:
            gram_size, gram = current_gram[1], current_gram[2]
            if type(gram_size) is not int or type(gram) is not str:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
            actual.add((gram_size, gram))
            current_gram = gram_cursor.fetchone()
        expected = {
            (size, gram)
            for size in required_sizes
            for gram in unique_character_ngrams(folded_source, size)
        }
        if actual != expected:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
        for size, _gram in actual:
            if size not in actual_counts:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
            actual_counts[size] += 1
    if current_gram is not None or actual_counts != expected_counts:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
    if fts5_available:
        record_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
        )
        fts_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts ORDER BY record_id"
        )
        fts_count = 0
        while True:
            record_row = record_cursor.fetchone()
            fts_row = fts_cursor.fetchone()
            if record_row is None or fts_row is None:
                if record_row != fts_row:
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_INDEX_INVALID"
                    )
                break
            if fts_row != record_row:
                raise SQLiteStoreSchemaError(
                    "STORE.CANDIDATE_INDEX_INVALID"
                )
            fts_count += 1
        if fts_count != evidence.fts_count:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
    elif evidence.fts_count != 0:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")


def _validate_published_activation_set(
    record: _ActivationJournalRecord,
    *,
    preparation: _ActivationPreparation,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
) -> tuple[_CanonicalStoreRef, SQLiteSchemaSnapshot]:
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
        snapshot = inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=next_generation,
            _expected_activation_digest=activation_digest,
        )
        with _open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                _validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if connection.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise SQLiteStoreSchemaError("STORE.INTEGRITY_CHECK_FAILED")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise SQLiteStoreSchemaError("STORE.FOREIGN_KEY_CHECK_FAILED")
                evidence = preparation._sealed_stage.evidence
                if (
                    _table_count(connection, "tm_record")
                    != evidence.record_count
                    or _table_count(connection, "tm_origin_batch")
                    != evidence.origin_batch_count
                    or _activation_exact_parity_digest(connection)
                    != evidence.exact_parity_digest
                ):
                    raise SQLiteStoreSchemaError("STORE.ACTIVE_COUNT_MISMATCH")
                _validate_activation_indexes(
                    connection,
                    evidence=evidence,
                    fts5_available=snapshot.fts5_available,
                )
                lease = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=canonical_store_id,
                    generation=next_generation,
                    fts5_available=snapshot.fts5_available,
                )
                facts = _read_source_binding_facts_in_transaction(
                    connection,
                    lease,
                )
                if (
                    facts.binding != evidence.source_binding
                    or facts.divergence_latched
                    or facts.diagnostic_codes
                    or _configured_pair_diagnostics(
                        evidence.source_binding,
                        identity=identity,
                        canonical_store_id=canonical_store_id,
                        head_revision=facts.head_revision,
                        cumulative_record_counts=facts.cumulative_record_counts,
                    )
                ):
                    raise SQLiteStoreSchemaError("STORE.ACTIVE_BINDING_INVALID")
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
    return active_ref, snapshot


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
        or terminal_record.canonical_store_id
        != main_record.canonical_store_id
    ):
        return False
    if main_record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
        if (
            terminal_record.phase
            is not _ActivationJournalPhase.GENERATION_PUBLISHED
        ):
            return _rollback_terminal_prior_closes(
                terminal_record,
                main_record,
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
        _terminal_prior_closure_matches(terminal_record, main_record)
        or _rollback_terminal_prior_closes(terminal_record, main_record)
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
) -> tuple[tuple[Path, str], tuple[Path, str]]:
    """Verify both journal-owned prior backups are present and intact.

    The durable journal is the only surviving ownership locator, so each
    backup path, identity, and digest comes from the journal record itself.
    A missing or mutated backup (or a foreign/hardlinked entry) fails closed
    before any mutation: without a restorable prior authority the pending
    journal must stay recoverable for manual intervention.
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
    prior_manifest_path = record.prior_manifest_path
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


def _recovery_jsonl_winners_digest(path: Path) -> str:
    """Last-valid-wins exact winners digest over the configured JSONL."""

    stage_sealer = importlib.import_module("tm_stage_sealer")
    accepted_row = stage_sealer._accepted_jsonl_row

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
    expected_counts = {size: 0 for size in required_sizes}
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    current_gram = gram_cursor.fetchone()
    for record_id, folded_source in connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
    ):
        if type(record_id) is not int or type(folded_source) is not str:
            raise SQLiteStoreSchemaError("STORE.RECORD_INVALID")
        actual: set[tuple[int, str]] = set()
        while current_gram is not None and current_gram[0] == record_id:
            gram_size, gram = current_gram[1], current_gram[2]
            if type(gram_size) is not int or type(gram) is not str:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
            actual.add((gram_size, gram))
            current_gram = gram_cursor.fetchone()
        expected = {
            (size, gram)
            for size in required_sizes
            for gram in unique_character_ngrams(folded_source, size)
        }
        if actual != expected:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
        for size, _gram in actual:
            if size not in expected_counts:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
            expected_counts[size] += 1
    if current_gram is not None:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")
    if fts5_available:
        record_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
        )
        fts_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts ORDER BY record_id"
        )
        while True:
            record_row = record_cursor.fetchone()
            fts_row = fts_cursor.fetchone()
            if record_row is None or fts_row is None:
                if record_row != fts_row:
                    raise SQLiteStoreSchemaError(
                        "STORE.CANDIDATE_INDEX_INVALID"
                    )
                break
            if fts_row != record_row:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_INDEX_INVALID")


def _recovery_receipt_row(
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
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
    binding = _binding_from_ledger_row(rows[0])
    if rows[0][13] != "completed":
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
    binding = _binding_from_ledger_row(rows[0])
    if rows[0][13] != "completed":
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
    if (
        binding.configured_jsonl_path != identity.configured_jsonl_path
        or binding.manifest_path != identity.snapshot_manifest_path
        or binding.receipt.resource_id != identity.resource_id
        or binding.receipt.canonical_store_id != canonical_store_id
        or binding.receipt.snapshot_id != record.prior_binding_snapshot_id
        or snapshot_receipt_digest(binding.receipt)
        != record.prior_receipt_digest
    ):
        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
    if record.prior_manifest_path != identity.snapshot_manifest_path:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_MISMATCH",
            retryable=False,
        )
    prior_db_path = record.prior_db_path
    assert record.prior_manifest_path is not None
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
            manifest_temp_path=record.prior_manifest_path,
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
        database, manifest, source = _capture_prior_assets(
            prior_view,
            identity=identity,
        )
        with _open_configured_connection(
            prior_db_path,
            require_existing=True,
        ) as connection:
            meta = _read_meta(connection)
            prior_status = meta.get("activation_status")
            if prior_status == "UNPUBLISHED":
                if (
                    record.prior_generation != 0
                    or "activation_digest" in meta
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
            elif prior_status == "ACTIVE":
                if (
                    _meta_int(meta, "generation")
                    != record.prior_generation
                    or meta.get("activation_digest") is None
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVATION_STATE_INVALID"
                    )
            else:
                raise SQLiteStoreSchemaError(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
            _recovery_prior_completed_binding(
                connection,
                identity=identity,
                canonical_store_id=canonical_store_id,
                record=record,
            )
            fts5_available = _meta_bool(meta, "fts5_available")
    except ActivationPreparationError as error:
        raise _recovery_mismatch(error) from error
    except (OSError, sqlite3.Error, SQLiteStoreSchemaError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
            retryable=False,
            reason_code=getattr(error, "error_code", None),
        ) from error
    if (
        database.digest != record.prior_db_digest
        or manifest.digest != record.prior_manifest_digest
        or (source.identity.device, source.identity.inode)
        != record.source_jsonl_identity
        or source.digest != record.source_jsonl_digest
        or (
            not allow_restored_identities
            and (
                (database.identity.device, database.identity.inode)
                != record.prior_db_identity
                or (manifest.identity.device, manifest.identity.inode)
                != record.prior_manifest_identity
            )
        )
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
            retryable=False,
        )
    return fts5_available


def _revalidate_recovered_sealed_database(
    record: _ActivationJournalRecord,
    *,
    stage_ref: _StoreRuntimeRef,
    database_path: Path,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
) -> None:
    """Re-prove the full sealed evidence digest from disk for one DB.

    The candidate (PREPARED) or freshly replaced (DB_REPLACED, still SEALED)
    database is reopened and every seal-relevant fact is recomputed, the
    receipt is read from the ledger, the temporary manifest bytes are
    re-derived, and the complete evidence digest must equal the journal's
    durable ``evidence_digest``.  This anchors record counts, indexes, exact
    parity, and the source binding to the journal without any in-memory
    sealed stage.
    """

    stage_sealer = importlib.import_module("tm_stage_sealer")
    database_capture = _capture_journal_closure_file(database_path)
    if (
        (database_capture[0].device, database_capture[0].inode)
        != record.candidate_stage_db_identity
        or database_capture[1] != record.stage_db_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )
    try:
        facts = stage_sealer._validate_stage_facts(
            cast(MutableStageRef, cast(object, stage_ref)),
            canonical_store_id=canonical_store_id,
            allow_sealed=True,
        )
    except Exception as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
            reason_code=getattr(error, "error_code", None),
        ) from error
    receipt = facts.receipt
    if (
        receipt.snapshot_id != record.new_receipt_id
        or snapshot_receipt_digest(receipt) != record.snapshot_receipt_digest
        or receipt.jsonl_digest != record.source_jsonl_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )
    manifest_capture = _capture_activation_file(
        record.candidate_manifest_temp_path,
        asset_kind="MANIFEST",
    )
    if (
        (
            manifest_capture.identity.device,
            manifest_capture.identity.inode,
        )
        != record.candidate_manifest_temp_identity
        or manifest_capture.digest != record.manifest_temp_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ASSET_MUTATED",
            retryable=False,
        )
    manifest_bytes = _read_activation_file_bytes(manifest_capture)
    expected_manifest_bytes = _recovery_expected_manifest_bytes(receipt)
    if manifest_bytes != expected_manifest_bytes:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )
    try:
        manifest = contract_from_json(manifest_bytes.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        ) from error
    if type(manifest) is not SnapshotManifest:
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )
    binding = stage_sealer._build_binding(identity, receipt, manifest)
    evidence = stage_sealer._build_evidence(
        facts,
        binding,
        stage_file_digest=record.stage_db_digest,
        manifest_temp_digest=record.manifest_temp_digest,
    )
    if (
        contract_module.stage_validation_evidence_digest(evidence)
        != record.evidence_digest
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
            retryable=False,
        )


def _revalidate_recovered_active_set(
    record: _ActivationJournalRecord,
    *,
    identity: CanonicalResourceIdentity,
    canonical_store_id: str,
    next_generation: int,
    activation_digest: str,
    require_manifest_published: bool,
) -> SQLiteSchemaSnapshot:
    """Re-prove one complete published new DB/receipt/binding/manifest set.

    Every expectation is re-derived from the durable journal and the active
    ledger (receipt/binding), the published manifest file, and the configured
    JSONL; record counts, exact winners parity, indexes, ancestry, and the
    configured pair are recomputed from disk.  A manifest still waiting to be
    published (DB_REPLACED recovery window) only skips the manifest-file
    diagnostics.
    """

    database = _recovery_capture_journal_file(
        identity.canonical_sidecar_path
    )
    if (
        (database[0].device, database[0].inode)
        != record.candidate_stage_db_identity
        or _lstat_any_entry(record.candidate_stage_db_path)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        )
    if require_manifest_published:
        manifest = _recovery_capture_journal_file(
            identity.snapshot_manifest_path
        )
        if (
            (manifest[0].device, manifest[0].inode)
            != record.candidate_manifest_temp_identity
            or manifest[1] != record.new_manifest_digest
            or _lstat_any_entry(record.candidate_manifest_temp_path)
        ):
            raise ActivationPreparationError(
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                retryable=False,
            )
    active_ref = _canonical_activation_ref(
        identity,
        journal_id=record.journal_id,
    )
    try:
        snapshot = inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=next_generation,
            _expected_activation_digest=activation_digest,
        )
        with _open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                _validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if connection.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise SQLiteStoreSchemaError(
                        "STORE.INTEGRITY_CHECK_FAILED"
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise SQLiteStoreSchemaError(
                        "STORE.FOREIGN_KEY_CHECK_FAILED"
                    )
                lease = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=canonical_store_id,
                    generation=next_generation,
                    fts5_available=snapshot.fts5_available,
                )
                facts = _read_source_binding_facts_in_transaction(
                    connection,
                    lease,
                )
                if (
                    facts.binding is None
                    or facts.divergence_latched
                    or facts.diagnostic_codes
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                binding = facts.binding
                if (
                    binding.receipt.snapshot_id != record.new_receipt_id
                    or snapshot_receipt_digest(binding.receipt)
                    != record.snapshot_receipt_digest
                    or binding.receipt.jsonl_digest
                    != record.source_jsonl_digest
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                if _table_count(connection, "tm_record") != (
                    binding.receipt.record_count
                ):
                    raise SQLiteStoreSchemaError("STORE.ACTIVE_COUNT_MISMATCH")
                jsonl_parity = _recovery_jsonl_winners_digest(
                    identity.configured_jsonl_path
                )
                if (
                    _activation_exact_parity_digest(connection)
                    != jsonl_parity
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_COUNT_MISMATCH"
                    )
                _recover_activation_indexes(
                    connection,
                    fts5_available=snapshot.fts5_available,
                )
                pair_diagnostics = _configured_pair_diagnostics(
                    binding,
                    identity=identity,
                    canonical_store_id=canonical_store_id,
                    head_revision=facts.head_revision,
                    cumulative_record_counts=facts.cumulative_record_counts,
                )
                allowed_manifest_codes = (
                    frozenset()
                    if require_manifest_published
                    else frozenset(
                        {
                            "SOURCE_BINDING.MANIFEST_MISSING",
                            "SOURCE_BINDING.MANIFEST_UNREADABLE",
                            "SOURCE_BINDING.MANIFEST_MISMATCH",
                            "SOURCE_BINDING.MANIFEST_INVALID",
                        }
                    )
                )
                unexpected = tuple(
                    code
                    for code in pair_diagnostics
                    if code not in allowed_manifest_codes
                )
                if unexpected:
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except Exception as error:
        if isinstance(error, ActivationPreparationError):
            raise
        raise ActivationPreparationError(
            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            retryable=False,
        ) from error
    return snapshot


def _revalidate_discovered_active_set(
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
        with _open_configured_connection(
            db_path,
            require_existing=True,
        ) as connection:
            meta = _read_meta(connection)
            if meta.get("activation_status") != "ACTIVE":
                raise SQLiteStoreSchemaError(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
            generation = _meta_int(meta, "generation")
            activation_digest = meta.get("activation_digest")
            if (
                type(activation_digest) is not str
                or len(activation_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in activation_digest
                )
            ):
                raise SQLiteStoreSchemaError(
                    "STORE.ACTIVATION_STATE_INVALID"
                )
        snapshot = inspect_stage_schema(
            active_ref,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
            _allow_active=True,
            _expected_active_generation=generation,
            _expected_activation_digest=activation_digest,
        )
        with _open_configured_connection(
            db_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                _validate_store_identity(
                    connection,
                    resource_id=identity.resource_id,
                    canonical_store_id=canonical_store_id,
                    target_identity=identity.target_identity,
                )
                if connection.execute("PRAGMA integrity_check").fetchall() != [
                    ("ok",)
                ]:
                    raise SQLiteStoreSchemaError(
                        "STORE.INTEGRITY_CHECK_FAILED"
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise SQLiteStoreSchemaError(
                        "STORE.FOREIGN_KEY_CHECK_FAILED"
                    )
                lease = _SQLiteGenerationView(
                    stage=active_ref,
                    canonical_store_id=canonical_store_id,
                    generation=generation,
                    fts5_available=snapshot.fts5_available,
                )
                facts = _read_source_binding_facts_in_transaction(
                    connection,
                    lease,
                )
                if (
                    facts.binding is None
                    or facts.divergence_latched
                    or facts.diagnostic_codes
                ):
                    raise SQLiteStoreSchemaError(
                        "STORE.ACTIVE_BINDING_INVALID"
                    )
                binding = facts.binding
                receipt_row, receipt_status = _recovery_receipt_row(
                    connection
                )
                if (
                    receipt_status != "completed"
                    or receipt_row != binding.receipt
                ):
                    raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                if _table_count(connection, "tm_record") != (
                    binding.receipt.record_count
                ):
                    raise SQLiteStoreSchemaError("STORE.ACTIVE_COUNT_MISMATCH")
                if _recovery_jsonl_winners_digest(
                    identity.configured_jsonl_path
                ) != _activation_exact_parity_digest(connection):
                    raise SQLiteStoreSchemaError("STORE.ACTIVE_COUNT_MISMATCH")
                _recover_activation_indexes(
                    connection,
                    fts5_available=snapshot.fts5_available,
                )
                pair_diagnostics = _configured_pair_diagnostics(
                    binding,
                    identity=identity,
                    canonical_store_id=canonical_store_id,
                    head_revision=facts.head_revision,
                    cumulative_record_counts=facts.cumulative_record_counts,
                )
                if pair_diagnostics:
                    raise SQLiteStoreSchemaError(
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
        with _open_configured_connection(
            identity.canonical_sidecar_path,
            require_existing=True,
        ) as connection:
            connection.execute("BEGIN")
            try:
                meta = _read_meta(connection)
                status = meta.get("activation_status")
                if status == "SEALED":
                    if canonical_capture[1] != record.stage_db_digest:
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                    receipt, receipt_status = _recovery_receipt_row(
                        connection
                    )
                    if receipt_status != "issued":
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                    if (
                        receipt.snapshot_id != record.new_receipt_id
                        or snapshot_receipt_digest(receipt)
                        != record.snapshot_receipt_digest
                        or receipt.jsonl_digest != record.source_jsonl_digest
                        or receipt.resource_id != identity.resource_id
                        or receipt.canonical_store_id != canonical_store_id
                    ):
                        raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
                    if connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone() != (0,):
                        raise SQLiteStoreSchemaError("STORE.BINDING_INVALID")
                    if (
                        _meta_int(meta, "generation") != 0
                        or "activation_digest" in meta
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                    connection.commit()
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        current_meta = _read_meta(connection)
                        if (
                            current_meta.get("activation_status")
                            != "SEALED"
                            or _meta_int(current_meta, "generation") != 0
                            or "activation_digest" in current_meta
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.ACTIVATION_STATE_INVALID"
                            )
                        current_receipt, current_status = (
                            _recovery_receipt_row(connection)
                        )
                        if (
                            current_receipt != receipt
                            or current_status != "issued"
                        ):
                            raise SQLiteStoreSchemaError(
                                "STORE.RECEIPT_INVALID"
                            )
                        updated = connection.execute(
                            "UPDATE tm_snapshot_receipt SET status = "
                            "'completed' WHERE snapshot_id = ? "
                            "AND status = 'issued'",
                            (receipt.snapshot_id,),
                        )
                        if updated.rowcount != 1:
                            raise SQLiteStoreSchemaError(
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
                            raise SQLiteStoreSchemaError(
                                "STORE.ACTIVATION_STATE_INVALID"
                            )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                elif status == "ACTIVE":
                    _ = _recovery_completed_binding(
                        connection,
                        identity=identity,
                        canonical_store_id=canonical_store_id,
                        record=record,
                    )
                    if (
                        _meta_int(meta, "generation") != next_generation
                        or meta.get("activation_digest") != activation_digest
                    ):
                        raise SQLiteStoreSchemaError(
                            "STORE.ACTIVATION_STATE_INVALID"
                        )
                else:
                    raise SQLiteStoreSchemaError(
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

    with _open_configured_connection(
        identity.canonical_sidecar_path,
        require_existing=True,
    ) as connection:
        connection.execute("BEGIN")
        try:
            receipt, receipt_status = _recovery_receipt_row(connection)
            if receipt_status != "completed":
                raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
            if (
                receipt.snapshot_id != record.new_receipt_id
                or snapshot_receipt_digest(receipt)
                != record.snapshot_receipt_digest
                or receipt.jsonl_digest != record.source_jsonl_digest
            ):
                raise SQLiteStoreSchemaError("STORE.RECEIPT_INVALID")
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
        if record.had_prior_canonical:
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
    elif record.had_prior_canonical:
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
        "evidence_digest": record.evidence_digest,
        "expected_prior_generation": record.expected_prior_generation,
        "gate_b_grant_digest": record.gate_b_grant_digest,
        "had_prior_canonical": record.had_prior_canonical,
        "journal_id": record.journal_id,
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
            or record.prior_manifest_digest is None
            or record.prior_db_path is None
            or record.prior_manifest_path is None
            or record.prior_db_digest is None
            or record.prior_db_identity is None
            or record.prior_manifest_identity is None
            or record.prior_db_backup_path is None
            or record.prior_manifest_backup_path is None
            or record.prior_db_backup_digest is None
            or record.prior_manifest_backup_digest is None
            or record.prior_db_backup_identity is None
            or record.prior_manifest_backup_identity is None
        ):
            raise ValueError(
                "activation journal prior facts are incomplete"
            )
        if record.prior_db_digest != record.prior_db_backup_digest:
            raise ValueError(
                "activation journal prior database digest does not close"
            )
        if (
            record.prior_manifest_digest
            != record.prior_manifest_backup_digest
        ):
            raise ValueError(
                "activation journal prior manifest digest does not close"
            )
    else:
        if (
            record.prior_generation is not None
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


class SourceBindingMonitor:
    """Observe one configured snapshot without publishing either file."""

    def __init__(self, coordinator: ResourceStoreCoordinator) -> None:
        if type(coordinator) is not ResourceStoreCoordinator:
            raise TypeError("coordinator must be ResourceStoreCoordinator")
        self._coordinator = coordinator

    def observe(self) -> SourceBindingObservation:
        """Derive and latch source divergence in one generation lease."""

        with self._coordinator._operation_lease() as lease:
            while True:
                with _open_leased_connection(lease) as connection:
                    facts = _read_source_binding_facts(connection, lease)
                diagnostics = list(facts.diagnostic_codes)
                binding = facts.binding
                if facts.divergence_latched:
                    diagnostics.append("SOURCE_BINDING.DIVERGENCE_LATCHED")
                elif binding is None:
                    diagnostics.append("SOURCE_BINDING.LEDGER_MISSING")
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
                    resource_id=lease.stage.resource_identity.resource_id,
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
    head_revision = _meta_int(meta, "head_revision")
    record_count = _table_count(connection, "tm_record")
    ancestry_rows = connection.execute(
        "SELECT b.batch_id, b.status, b.completed_revision, "
        "b.valid_count, COUNT(r.record_id) "
        "FROM tm_origin_batch AS b "
        "LEFT JOIN tm_record AS r ON r.origin_batch_id = b.batch_id "
        "GROUP BY b.batch_id, b.status, b.completed_revision, b.valid_count "
        "ORDER BY b.batch_id"
    ).fetchall()
    diagnostics: list[str] = []
    cumulative_record_counts: tuple[tuple[int, int], ...] = ()
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

    try:
        jsonl_digest = _file_sha256(identity.configured_jsonl_path)
    except FileNotFoundError:
        diagnostics.append("SOURCE_BINDING.JSONL_MISSING")
    except OSError:
        diagnostics.append("SOURCE_BINDING.JSONL_UNREADABLE")
    else:
        if jsonl_digest != receipt.jsonl_digest:
            diagnostics.append("SOURCE_BINDING.JSONL_DIGEST_MISMATCH")

    try:
        manifest_bytes = identity.snapshot_manifest_path.read_bytes()
    except FileNotFoundError:
        diagnostics.append("SOURCE_BINDING.MANIFEST_MISSING")
    except OSError:
        diagnostics.append("SOURCE_BINDING.MANIFEST_UNREADABLE")
    else:
        expected_bytes = contract_to_json(binding.manifest).encode("utf-8")
        if manifest_bytes != expected_bytes:
            diagnostics.append("SOURCE_BINDING.MANIFEST_MISMATCH")
        else:
            try:
                decoded = contract_from_json(manifest_bytes.decode("utf-8"))
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
            self._coordinator
        )

    @property
    def coordinator(self) -> ResourceStoreCoordinator:
        return self._coordinator

    @property
    def source_binding_monitor(self) -> SourceBindingMonitor:
        return self._source_binding_monitor

    def canonical_revision(self) -> CanonicalRevisionSnapshot:
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                return _canonical_revision_from_connection(
                    connection,
                    lease,
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

    def register_completed_snapshot_binding(
        self,
        binding: SnapshotBinding,
    ) -> None:
        self._source_binding_monitor.register_completed_binding(binding)

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        if type(source_raw) is not str:
            raise TypeError("source_raw must be a built-in string")
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                self._validate_identity(connection, lease)
                rows = connection.execute(
                    f"SELECT {_RECORD_COLUMNS} FROM tm_record "
                    "WHERE source_raw = ? ORDER BY record_id DESC",
                    (source_raw,),
                ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

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
        if type(record_ids) is not tuple:
            raise TypeError("record_ids must be a built-in tuple")
        for record_id in record_ids:
            if type(record_id) is not int:
                raise TypeError(
                    "record_ids must contain built-in integers"
                )
            if record_id < 1:
                raise ValueError(
                    "record_ids must contain positive integers"
                )
        with self._coordinator._operation_lease() as lease:
            if not record_ids:
                return ()
            placeholders = ",".join("?" for _ in record_ids)
            with _open_leased_connection(lease) as connection:
                self._validate_identity(connection, lease)
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

        stage_matches: list[tuple[str, tuple[tuple[int, int], ...]]] = []
        cumulative_ids: set[int] = set()
        with self._coordinator._operation_lease() as lease:
            with _open_leased_connection(lease) as connection:
                try:
                    connection.execute("BEGIN")
                    self._validate_identity(connection, lease)
                    meta_fts5_available = _meta_bool(
                        _read_meta(connection), "fts5_available"
                    )
                    if meta_fts5_available != lease.fts5_available:
                        raise SQLiteStoreSchemaError(
                            "STORE.CANDIDATE_CAPABILITY_MISMATCH"
                        )
                    fts5_available = lease.fts5_available

                    fts_ids: set[int] = set()
                    if fts_query_trigrams is not None and fts5_available:
                        for chunk in _chunked(fts_query_trigrams):
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

                    if fts_query_trigrams is None:
                        selected_stages = prepared_stages
                    elif not fts5_available:
                        selected_stages = prepared_stages
                    elif (
                        not cumulative_ids
                        or fts_query_degenerate
                        or len(cumulative_ids) < candidate_floor
                    ):
                        selected_stages = [
                            stage for stage in prepared_stages if stage[0] in {1, 2}
                        ]
                    else:
                        selected_stages = []

                    for gram_size, grams in selected_stages:
                        if (
                            fts_query_trigrams is not None
                            and fts5_available
                            and gram_size == 1
                            and len(cumulative_ids) >= candidate_floor
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
                    if fts_query_trigrams is not None and fts5_available:
                        sources_by_id = dict(folded_sources)
                        query_gram_set = set(fts_query_trigrams)
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
        identity = lease.stage.resource_identity
        _validate_store_identity(
            connection,
            resource_id=identity.resource_id,
            canonical_store_id=lease.canonical_store_id,
            target_identity=identity.target_identity,
        )


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
    head_revision = _meta_int(meta, "head_revision")
    record_count = _table_count(connection, "tm_record")
    ancestry_rows = connection.execute(
        "SELECT b.batch_id, b.status, b.completed_revision, "
        "b.valid_count, COUNT(r.record_id) "
        "FROM tm_origin_batch AS b "
        "LEFT JOIN tm_record AS r ON r.origin_batch_id = b.batch_id "
        "GROUP BY b.batch_id, b.status, b.completed_revision, b.valid_count "
        "ORDER BY b.batch_id"
    ).fetchall()
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


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    if table_name not in _BASE_TABLES:
        raise ValueError("table name is not approved")
    row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise SQLiteStoreSchemaError("STORE.TABLE_COUNT_INVALID")
    return row[0]


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
    tuple[tuple[int, int, str], ...],
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
    return (
        tuple(dict.fromkeys((*default_grams, *additional_grams))),
        tuple(dict.fromkeys((*default_fts, *additional_fts))),
    )


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
    gram_rows: list[tuple[int, int, str]] = []
    gram_keys: set[tuple[int, int, str]] = set()
    for row in plan.gram_rows:
        if type(row) is not SQLiteGramRow:
            raise TypeError("gram_rows must contain SQLiteGramRow values")
        origin_ordinal = row.origin_ordinal
        gram_size = row.gram_size
        gram = row.gram
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
        key = (origin_ordinal, gram_size, gram)
        if key in gram_keys:
            raise ValueError("gram rows must be unique")
        gram_keys.add(key)
        gram_rows.append(key)
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
    validated_gram_rows: list[tuple[int, str, int]] = []
    for row in gram_rows:
        if type(row) is not tuple or len(row) != 3:
            raise TypeError("validated gram row is invalid")
        origin_ordinal, gram_size, gram = row
        if (
            type(origin_ordinal) is not int
            or origin_ordinal not in record_ids_by_ordinal
            or type(gram_size) is not int
            or gram_size not in {1, 2, 3}
            or type(gram) is not str
            or len(gram) != gram_size
            or row in seen_grams
        ):
            raise ValueError("validated gram row is invalid")
        seen_grams.add(row)
        validated_gram_rows.append(
            (gram_size, gram, record_ids_by_ordinal[origin_ordinal])
        )
    connection.executemany(
        "INSERT INTO tm_gram(gram_size, gram, record_id) VALUES (?, ?, ?)",
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
            "source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, "
            "origin_batch_id, origin_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_raw,
                target_raw,
                source_fold_v1,
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
            "source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, "
            "origin_batch_id, origin_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_raw,
                target_raw,
                source_fold_v1,
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
    grams_by_size: dict[int, dict[str, list[int]]] = {
        gram_size: {} for gram_size in gram_sizes
    }
    fts_rows: list[tuple[str, int]] = []
    for draft in prepared_drafts:
        origin_ordinal = draft[10]
        folded_source = draft[2]
        record_id = record_ids_by_ordinal[origin_ordinal]
        for gram_size in gram_sizes:
            bucket = grams_by_size[gram_size]
            for gram in unique_character_ngrams(folded_source, gram_size):
                bucket.setdefault(gram, []).append(record_id)
        if fts5_available:
            fts_rows.append((folded_source, record_id))
    for gram_size in gram_sizes:
        bucket = grams_by_size[gram_size]
        rows = [
            (gram_size, gram, record_id)
            for gram in sorted(bucket)
            for record_id in bucket[gram]
        ]
        connection.executemany(
            "INSERT INTO tm_gram(gram_size, gram, record_id) "
            "VALUES (?, ?, ?)",
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
) -> SQLiteSchemaSnapshot:
    """Create a new unpublished stage; never create the canonical path."""

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
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            if runtime.fts5_available:
                connection.execute(_FTS5_STATEMENT)
            schema_digest = _schema_digest(
                connection,
                fts5_available=runtime.fts5_available,
            )
            if schema_digest != _APPROVED_SCHEMA_DIGESTS[
                runtime.fts5_available
            ]:
                raise SQLiteStoreSchemaError("STORE.SCHEMA_ROOT_MISMATCH")
            meta = _initial_meta(
                stage=validated_stage,
                canonical_store_id=canonical_store_id,
                runtime=runtime,
                schema_digest=schema_digest,
            )
            connection.executemany(
                "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
                tuple(sorted(meta.items())),
            )
            connection.commit()
        return inspect_stage_schema(
            validated_stage,
            canonical_store_id=canonical_store_id,
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
    _expected_active_generation: int | None = None,
    _expected_activation_digest: str | None = None,
) -> SQLiteSchemaSnapshot:
    """Strictly inspect one stage without publishing physical readiness.

    The private sealed-inspection mode accepts a closed SEALED stage (Gate B
    recomputation) without weakening normal mutable-stage inspection or the
    future ACTIVE semantics.
    """

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
        actual_schema_digest = _schema_digest(
            connection,
            fts5_available=fts5_available,
        )
        approved_schema_digest = _APPROVED_SCHEMA_DIGESTS[fts5_available]
        if (
            meta["schema_digest"] != approved_schema_digest
            or actual_schema_digest != approved_schema_digest
        ):
            raise SQLiteStoreSchemaError("STORE.TABLE_SCHEMA_MISMATCH")
        _validate_index_schema(connection)
        _validate_foreign_key_schema(connection)
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
            index_names=tuple(sorted(_BASE_INDEXES)),
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
) -> str:
    expected_names = set(_BASE_TABLES | _BASE_INDEXES)
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


def _validate_index_schema(connection: sqlite3.Connection) -> None:
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


def _validate_foreign_key_schema(connection: sqlite3.Connection) -> None:
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


__all__ = [
    "ActivationBackupEvidence",
    "ActivationPreparationError",
    "ActivationRecoveryReport",
    "BUSY_TIMEOUT_MS",
    "CANDIDATE_INDEX_VERSION",
    "CanonicalRevisionSnapshot",
    "FOLD_VERSION_V1",
    "ResourceStoreCoordinator",
    "SQLiteCandidateRecord",
    "SQLiteCandidateRecallSnapshot",
    "SQLiteCandidateWritePlan",
    "SQLiteGramRow",
    "SQLiteRuntimeCapability",
    "SQLiteSchemaSnapshot",
    "SQLiteStoreLifecycleError",
    "SQLiteStoreSchemaError",
    "SQLiteTMStore",
    "SourceBindingMonitor",
    "SourceBindingObservation",
    "TM_SCHEMA_VERSION",
    "detect_sqlite_runtime",
    "initialize_stage_schema",
    "inspect_stage_schema",
]
