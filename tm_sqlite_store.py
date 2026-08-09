"""Safe schema and connection policy for per-resource canonical TM stores."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import cast
import unicodedata
import uuid

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
class _SQLiteGenerationView:
    stage: MutableStageRef
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


class ResourceStoreCoordinator:
    """Own one resource's operation leases and active generation view."""

    def __init__(
        self,
        stage: MutableStageRef,
        *,
        canonical_store_id: str,
    ) -> None:
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        private_stage = _snapshot_store_stage(stage)
        snapshot = inspect_stage_schema(
            private_stage,
            canonical_store_id=canonical_store_id,
            _allow_diverged_runtime=True,
        )
        self._resource_id = private_stage.resource_identity.resource_id
        self._target_identity = (
            private_stage.resource_identity.target_identity
        )
        self._condition = threading.Condition()
        self._state = "READY"
        self._active_lease_count = 0
        self._view = _SQLiteGenerationView(
            stage=private_stage,
            canonical_store_id=canonical_store_id,
            generation=snapshot.generation,
            fts5_available=snapshot.fts5_available,
        )

    @property
    def resource_id(self) -> str:
        return self._resource_id

    @property
    def current_generation(self) -> int:
        with self._condition:
            return self._view.generation

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
                    generation=view.generation,
                    retryable=True,
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
            raise SQLiteStoreLifecycleError(
                "STORE.IDENTITY_MISMATCH",
                resource_id=self._resource_id,
                generation=self.current_generation,
                retryable=False,
            )

        with self._condition:
            current_view = self._view
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
    ) -> None:
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        self._canonical_store_id = canonical_store_id
        self._coordinator = ResourceStoreCoordinator(
            stage,
            canonical_store_id=self._canonical_store_id,
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
        apply_candidate_write_plan = _apply_candidate_write_plan
        inserted: list[TMRecord] = []
        record_ids_by_ordinal: dict[int, int] = {}
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
                            prepared_batch_id,
                            origin_ordinal,
                        ),
                    )
                    record_id = cursor.lastrowid
                    if record_id is None:
                        raise SQLiteStoreSchemaError(
                            "STORE.RECORD_ID_MISSING"
                        )
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
                            origin_batch_id=prepared_batch_id,
                            origin_ordinal=origin_ordinal,
                        )
                    )
                apply_candidate_write_plan(
                    connection,
                    validated_candidate_plan,
                    record_ids_by_ordinal=record_ids_by_ordinal,
                    folded_sources_by_ordinal={
                        draft[10]: draft[2]
                        for draft in prepared_drafts
                    },
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
        return tuple(inserted)

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
) -> _ValidatedCandidateWritePlan:
    default_plan = _validate_and_copy_candidate_plan(
        build_candidate_write_plan(
            candidate_records,
            fts5_available=fts5_available,
        ),
        batch_size=batch_size,
    )
    if extension is None:
        return default_plan
    additional_plan = _validate_and_copy_candidate_plan(
        extension(candidate_records),
        batch_size=batch_size,
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
) -> tuple[_PreparedRecordDraft, ...]:
    prepared: list[_PreparedRecordDraft] = []
    for ordinal, (draft, legacy_line_no) in enumerate(
        zip(drafts, legacy_line_nos, strict=True)
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
) -> _ValidatedCandidateWritePlan:
    if type(plan) is not SQLiteCandidateWritePlan:
        raise TypeError("extension must return SQLiteCandidateWritePlan")
    if type(plan.gram_rows) is not tuple:
        raise TypeError("gram_rows must be a built-in tuple")
    gram_rows: list[tuple[int, int, str]] = []
    gram_keys: set[tuple[int, int, str]] = set()
    for row in plan.gram_rows:
        if type(row) is not SQLiteGramRow:
            raise TypeError("gram_rows must contain SQLiteGramRow values")
        origin_ordinal = row.origin_ordinal
        gram_size = row.gram_size
        gram = row.gram
        if type(origin_ordinal) is not int or not 0 <= origin_ordinal < batch_size:
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
        if type(origin_ordinal) is not int or not 0 <= origin_ordinal < batch_size:
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
    if type(plan) is not tuple or len(plan) != 2:
        raise TypeError("validated candidate plan is invalid")
    gram_rows, fts_origin_ordinals = plan
    if type(gram_rows) is not tuple or type(fts_origin_ordinals) is not tuple:
        raise TypeError("validated candidate plan is invalid")
    seen_grams: set[tuple[int, int, str]] = set()
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
        connection.execute(
            "INSERT INTO tm_gram(gram_size, gram, record_id) "
            "VALUES (?, ?, ?)",
            (gram_size, gram, record_ids_by_ordinal[origin_ordinal]),
        )
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
        try:
            connection.execute(
                "INSERT INTO tm_fts(source_fold_v1, record_id) VALUES (?, ?)",
                (
                    folded_sources_by_ordinal[origin_ordinal],
                    record_ids_by_ordinal[origin_ordinal],
                ),
            )
        except sqlite3.OperationalError as error:
            raise SQLiteStoreSchemaError("STORE.FTS5_UNAVAILABLE") from error


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
    if type(batch_id) is not str:
        raise TypeError("batch_id must be a built-in string")
    if not batch_id.strip():
        raise ValueError("batch_id must not be empty")
    if type(kind) is not str:
        raise TypeError("kind must be a built-in string")
    if kind not in {"migration", "local_write", "import"}:
        raise ValueError("kind must be migration, local_write, or import")
    if type(drafts) is not tuple:
        raise TypeError("drafts must be a built-in tuple")
    for draft in drafts:
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
    if kind == "local_write":
        if len(drafts) != 1:
            raise ValueError("local_write batch must contain one draft")
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
    if legacy_line_nos is not None:
        if type(legacy_line_nos) is not tuple:
            raise TypeError("legacy_line_nos must be a built-in tuple or None")
        if len(legacy_line_nos) != len(drafts):
            raise ValueError("legacy_line_nos must align with drafts")
        for line_number in legacy_line_nos:
            if line_number is None:
                continue
            if type(line_number) is not int:
                raise TypeError(
                    "legacy line numbers must be built-in integers or None"
                )
            if line_number < 1:
                raise ValueError(
                    "legacy line numbers must be positive integers"
                )
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
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    _allow_diverged_runtime: bool = False,
) -> SQLiteSchemaSnapshot:
    """Strictly inspect one stage without publishing physical readiness."""

    validated_stage = _require_stage(stage)
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
    if meta.get("activation_status") != "UNPUBLISHED":
        raise SQLiteStoreSchemaError("STORE.STAGE_PUBLISHED")
    if "activation_digest" in meta:
        raise SQLiteStoreSchemaError("STORE.STAGE_PUBLISHED")
    if (
        _meta_bool(meta, "divergence_latched")
        and not allow_diverged_runtime
    ):
        raise SQLiteStoreSchemaError("STORE.STAGE_DIVERGED")
    if _meta_int(meta, "generation") != 0:
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
    safe_identity = CanonicalResourceIdentity(
        resource_id=resource_id,
        configured_jsonl_path=_copy_exact_path(configured_jsonl_path),
        canonical_sidecar_path=_copy_exact_path(canonical_sidecar_path),
        snapshot_manifest_path=_copy_exact_path(snapshot_manifest_path),
        target_identity=target_identity,
        identity_version=identity_version,
    )
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
