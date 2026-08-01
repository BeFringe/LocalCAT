"""Safe schema and connection policy for per-resource canonical TM stores."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import unicodedata
import uuid

from text_matcher import (
    TEXT_MATCHER_SEMANTICS_VERSION,
    UNICODE_VERSION,
    fold_text_v1,
)
from tm_contracts import (
    SCORER_VERSION_V1,
    CanonicalResourceIdentity,
    MutableStageRef,
    TMRecord,
    TMRecordDraft,
)


TM_SCHEMA_VERSION = 1
FOLD_VERSION_V1 = "fold-v1-unicode-16.0.0"
CANDIDATE_INDEX_VERSION = "candidate-index-v1"
BUSY_TIMEOUT_MS = 5000
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


class SQLiteTMStore:
    """Task 3.2 store port, gated until its behavioral tests are green."""

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
        self._stage = _snapshot_store_stage(stage)
        self._canonical_store_id = canonical_store_id
        _ = inspect_stage_schema(
            self._stage,
            canonical_store_id=self._canonical_store_id,
        )

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        if type(source_raw) is not str:
            raise TypeError("source_raw must be a built-in string")
        with _open_configured_connection(
            self._stage.staged_db_path
        ) as connection:
            self._validate_identity(connection)
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
            validated_candidate_plan,
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
        stage_identity = self._stage.resource_identity
        database_path = self._stage.staged_db_path
        expected_resource_id = stage_identity.resource_id
        expected_canonical_store_id = self._canonical_store_id
        expected_target_identity = stage_identity.target_identity
        open_connection = _open_configured_connection
        validate_store_identity = _validate_store_identity
        apply_candidate_write_plan = _apply_candidate_write_plan
        inserted: list[TMRecord] = []
        record_ids_by_ordinal: dict[int, int] = {}
        with open_connection(
            database_path
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                validate_store_identity(
                    connection,
                    resource_id=expected_resource_id,
                    canonical_store_id=expected_canonical_store_id,
                    target_identity=expected_target_identity,
                )
                connection.execute(
                    "INSERT INTO tm_origin_batch("
                    "batch_id, kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "created_at) VALUES (?, ?, ?, ?, 'staged', ?, ?, ?, ?)",
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
                connection.execute(
                    "UPDATE tm_origin_batch SET status = 'completed' "
                    "WHERE batch_id = ?",
                    (prepared_batch_id,),
                )
                updated = connection.execute(
                    "UPDATE tm_meta SET value = CAST(value AS INTEGER) + 1 "
                    "WHERE key = 'head_revision'"
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
        if not record_ids:
            return ()
        placeholders = ",".join("?" for _ in record_ids)
        with _open_configured_connection(
            self._stage.staged_db_path
        ) as connection:
            self._validate_identity(connection)
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

    def export_records(self) -> Iterator[TMRecord]:
        with _open_configured_connection(
            self._stage.staged_db_path
        ) as connection:
            self._validate_identity(connection)
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM tm_record "
                "ORDER BY record_id ASC"
            ).fetchall()
        return iter(tuple(_record_from_row(row) for row in rows))

    def _validate_identity(self, connection: sqlite3.Connection) -> None:
        identity = self._stage.resource_identity
        _validate_store_identity(
            connection,
            resource_id=identity.resource_id,
            canonical_store_id=self._canonical_store_id,
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
    _ValidatedCandidateWritePlan,
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
    candidate_plan = (
        SQLiteCandidateWritePlan()
        if extension is None
        else extension(candidate_records)
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
        _validate_and_copy_candidate_plan(
            candidate_plan,
            batch_size=len(prepared_drafts),
        ),
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
) -> Iterator[sqlite3.Connection]:
    """Open one short, thread-local connection under the fixed policy."""

    _require_absolute_path(database_path, "database_path")
    database: str | Path = database_path
    uri = False
    if expected_file is not None:
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
) -> SQLiteSchemaSnapshot:
    """Strictly inspect one stage without publishing physical readiness."""

    validated_stage = _require_stage(stage)
    _require_identity(canonical_store_id, "canonical_store_id")
    if not validated_stage.staged_db_path.is_file():
        raise SQLiteStoreSchemaError("STORE.DATABASE_MISSING")
    with _open_configured_connection(
        validated_stage.staged_db_path
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
        _validate_stage_meta(meta, runtime=runtime)
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
    if _meta_bool(meta, "divergence_latched"):
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
    "FOLD_VERSION_V1",
    "SQLiteCandidateRecord",
    "SQLiteCandidateWritePlan",
    "SQLiteGramRow",
    "SQLiteRuntimeCapability",
    "SQLiteSchemaSnapshot",
    "SQLiteStoreSchemaError",
    "SQLiteTMStore",
    "TM_SCHEMA_VERSION",
    "detect_sqlite_runtime",
    "initialize_stage_schema",
    "inspect_stage_schema",
]
