"""Validate, close, fsync, and seal one complete immutable migration artifact."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import cast
import uuid

import tm_contracts as contract_module
from tm_contracts import (
    GENERATION_EXPECTATION_VERSION,
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    STAGE_VALIDATION_EVIDENCE_VERSION,
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    GenerationExpectation,
    MutableStageRef,
    SealedStage,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    StageValidationEvidence,
    contract_from_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
    SQLiteSchemaSnapshot,
    SQLiteStoreSchemaError,
    inspect_stage_schema,
    unique_character_ngrams,
)
from text_matcher import fold_text_v1


_NATIVE_PATH_TYPE = type(Path())
_READ_CHUNK_BYTES = 1024 * 1024
_EXPECTED_PROVENANCE_JSON = (
    '[["source","legacy-jsonl"]]'
)


class StageSealError(RuntimeError):
    """Stable code-only seal failure; never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class _StageFacts:
    resource_id: str
    target_identity: str
    schema_version: int
    fold_version: str
    index_version: str
    fts5_available: bool
    record_count: int
    origin_batch_count: int
    fts_count: int
    gram_counts: tuple[tuple[int, int], ...]
    receipt: SnapshotReceipt
    exact_parity_digest: str
    closure_digest: str


@dataclass(frozen=True)
class _ArtifactFileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _RegistryEntry:
    mutable: MutableStageRef
    stage: SealedStage
    state: ActivationCapabilityState


def _snapshot_str(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a built-in string")
    return value


def _snapshot_int(value: object, field_name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a built-in integer")
    return value


def _snapshot_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in boolean")
    return value


def _snapshot_path(value: object, field_name: str) -> Path:
    if type(value) is not _NATIVE_PATH_TYPE:
        raise TypeError(f"{field_name} must be an exact native Path")
    copied = Path(Path.__str__(value))
    if type(copied) is not _NATIVE_PATH_TYPE:
        raise TypeError(f"{field_name} copy must be an exact native Path")
    return copied


def _snapshot_identity(value: object) -> CanonicalResourceIdentity:
    if type(value) is not CanonicalResourceIdentity:
        raise TypeError(
            "resource identity must be exact CanonicalResourceIdentity"
        )
    return CanonicalResourceIdentity(
        resource_id=_snapshot_str(value.resource_id, "resource_id"),
        configured_jsonl_path=_snapshot_path(
            value.configured_jsonl_path,
            "configured_jsonl_path",
        ),
        canonical_sidecar_path=_snapshot_path(
            value.canonical_sidecar_path,
            "canonical_sidecar_path",
        ),
        snapshot_manifest_path=_snapshot_path(
            value.snapshot_manifest_path,
            "snapshot_manifest_path",
        ),
        target_identity=_snapshot_str(value.target_identity, "target_identity"),
        identity_version=_snapshot_str(
            value.identity_version,
            "identity_version",
        ),
    )


def _snapshot_stage(value: object) -> MutableStageRef:
    if type(value) is not MutableStageRef:
        raise TypeError("stage must be exact MutableStageRef")
    return MutableStageRef(
        stage_id=_snapshot_str(value.stage_id, "stage_id"),
        resource_identity=_snapshot_identity(value.resource_identity),
        staged_db_path=_snapshot_path(value.staged_db_path, "staged_db_path"),
        manifest_temp_path=_snapshot_path(
            value.manifest_temp_path,
            "manifest_temp_path",
        ),
    )


def _snapshot_receipt(value: object) -> SnapshotReceipt:
    if type(value) is not SnapshotReceipt:
        raise TypeError("snapshot receipt must be exact SnapshotReceipt")
    return SnapshotReceipt(
        snapshot_id=_snapshot_str(value.snapshot_id, "snapshot_id"),
        resource_id=_snapshot_str(value.resource_id, "resource_id"),
        canonical_store_id=_snapshot_str(
            value.canonical_store_id,
            "canonical_store_id",
        ),
        exported_revision=_snapshot_int(
            value.exported_revision,
            "exported_revision",
        ),
        jsonl_digest=_snapshot_str(value.jsonl_digest, "jsonl_digest"),
        record_count=_snapshot_int(value.record_count, "record_count"),
        format_version=_snapshot_str(value.format_version, "format_version"),
    )


def _snapshot_manifest(value: object) -> SnapshotManifest:
    if type(value) is not SnapshotManifest:
        raise TypeError("snapshot manifest must be exact SnapshotManifest")
    kind = value.snapshot_kind
    if type(kind) is not SnapshotKind:
        raise TypeError("manifest snapshot kind must be exact SnapshotKind")
    return SnapshotManifest(
        manifest_version=_snapshot_str(
            value.manifest_version,
            "manifest_version",
        ),
        snapshot_kind=kind,
        receipt=_snapshot_receipt(value.receipt),
        receipt_digest=_snapshot_str(value.receipt_digest, "receipt_digest"),
    )


def _snapshot_binding(value: object) -> SnapshotBinding:
    if type(value) is not SnapshotBinding:
        raise TypeError("snapshot binding must be exact SnapshotBinding")
    kind = value.snapshot_kind
    if type(kind) is not SnapshotKind:
        raise TypeError("binding snapshot kind must be exact SnapshotKind")
    return SnapshotBinding(
        configured_jsonl_path=_snapshot_path(
            value.configured_jsonl_path,
            "configured_jsonl_path",
        ),
        manifest_path=_snapshot_path(value.manifest_path, "manifest_path"),
        snapshot_kind=kind,
        receipt=_snapshot_receipt(value.receipt),
        manifest=_snapshot_manifest(value.manifest),
        binding_version=_snapshot_str(
            value.binding_version,
            "binding_version",
        ),
    )


def _snapshot_gram_counts(
    value: object,
) -> tuple[tuple[int, int], ...]:
    if type(value) is not tuple:
        raise TypeError("gram_counts must be a built-in tuple")
    pairs: list[tuple[int, int]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("gram counts must contain exact integer pairs")
        pairs.append(
            (
                _snapshot_int(item[0], "gram size"),
                _snapshot_int(item[1], "gram count"),
            )
        )
    return tuple(pairs)


def _snapshot_evidence(value: object) -> StageValidationEvidence:
    if type(value) is not StageValidationEvidence:
        raise TypeError("evidence must be exact StageValidationEvidence")
    return StageValidationEvidence(
        evidence_version=_snapshot_str(
            value.evidence_version,
            "evidence_version",
        ),
        resource_id=_snapshot_str(value.resource_id, "resource_id"),
        target_identity=_snapshot_str(
            value.target_identity,
            "target_identity",
        ),
        source_binding=_snapshot_binding(value.source_binding),
        snapshot_receipt_digest=_snapshot_str(
            value.snapshot_receipt_digest,
            "snapshot_receipt_digest",
        ),
        manifest_temp_digest=_snapshot_str(
            value.manifest_temp_digest,
            "manifest_temp_digest",
        ),
        schema_version=_snapshot_int(value.schema_version, "schema_version"),
        fold_version=_snapshot_str(value.fold_version, "fold_version"),
        index_version=_snapshot_str(value.index_version, "index_version"),
        record_count=_snapshot_int(value.record_count, "record_count"),
        origin_batch_count=_snapshot_int(
            value.origin_batch_count,
            "origin_batch_count",
        ),
        fts_count=_snapshot_int(value.fts_count, "fts_count"),
        gram_counts=_snapshot_gram_counts(value.gram_counts),
        exact_parity_digest=_snapshot_str(
            value.exact_parity_digest,
            "exact_parity_digest",
        ),
        integrity_ok=_snapshot_bool(value.integrity_ok, "integrity_ok"),
        foreign_keys_ok=_snapshot_bool(
            value.foreign_keys_ok,
            "foreign_keys_ok",
        ),
        stage_file_digest=_snapshot_str(
            value.stage_file_digest,
            "stage_file_digest",
        ),
    )


def _snapshot_generation(value: object) -> GenerationExpectation:
    if type(value) is not GenerationExpectation:
        raise TypeError("generation must be exact GenerationExpectation")
    expected = value.expected_prior_generation
    if expected is not None:
        _snapshot_int(expected, "expected prior generation")
    return GenerationExpectation(
        resource_id=_snapshot_str(value.resource_id, "resource_id"),
        target_identity=_snapshot_str(
            value.target_identity,
            "target_identity",
        ),
        canonical_store_id=_snapshot_str(
            value.canonical_store_id,
            "canonical_store_id",
        ),
        snapshot_receipt_digest=_snapshot_str(
            value.snapshot_receipt_digest,
            "snapshot_receipt_digest",
        ),
        expected_prior_generation=expected,
        expectation_version=_snapshot_str(
            value.expectation_version,
            "expectation_version",
        ),
    )


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _accepted_jsonl_row(
    payload: object,
) -> tuple[str, str, str | None, str | None, str | None, str | None] | None:
    if type(payload) is not dict:
        return None
    row = cast(dict[str, object], payload)
    source_raw = row.get("source")
    target_raw = row.get("target")
    if (
        type(source_raw) is not str
        or source_raw == ""
        or type(target_raw) is not str
        or target_raw == ""
    ):
        return None

    def optional_text(field_name: str) -> str | None:
        value = row.get(field_name)
        return value if type(value) is str else None

    return (
        source_raw,
        target_raw,
        optional_text("speaker"),
        optional_text("context_prev"),
        optional_text("context_next"),
        optional_text("file_source"),
    )


def _open_stage_read_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _text_row(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a built-in string")
    return value


def _int_row(value: object, field_name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a built-in integer")
    return value


def _optional_text_row(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text_row(value, field_name)


def _receipt_from_ledger_row(row: tuple[object, ...]) -> SnapshotReceipt:
    return SnapshotReceipt(
        snapshot_id=_text_row(row[0], "snapshot_id"),
        resource_id=_text_row(row[1], "receipt resource_id"),
        canonical_store_id=_text_row(row[2], "canonical_store_id"),
        exported_revision=_int_row(row[3], "exported_revision"),
        jsonl_digest=_text_row(row[4], "jsonl_digest"),
        record_count=_int_row(row[5], "record_count"),
        format_version=_text_row(row[6], "format_version"),
    )


def _winners_parity_digest(winners: dict[str, str]) -> str:
    """Deterministic bounded digest over the compatibility winner mapping."""

    digest = hashlib.sha256()

    def frame(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return (
            str(len(encoded)).encode("ascii")
            + b":"
            + encoded
            + b";"
        )

    for source_raw in sorted(winners):
        digest.update(frame(source_raw))
        digest.update(frame(winners[source_raw]))
    return digest.hexdigest()


def _stage_closure_digest(connection: sqlite3.Connection) -> str:
    """Bounded digest over every table whose facts enter the evidence.

    Captured inside the validation read snapshot and rechecked under the
    marker write lock, this proves every schema, identity, integrity, FK,
    record, index, source-binding, and exact-parity fact describes the same
    committed bytes that transition to SEALED. Streaming cursor reads keep
    peak memory bounded regardless of the 100k-row stage size.
    """

    digest = hashlib.sha256()

    def frame(value: object) -> None:
        if value is None:
            digest.update(b"n;")
            return
        if type(value) is str:
            encoded = value.encode("utf-8")
            tag = b"s"
        elif type(value) is int and not isinstance(value, bool):
            encoded = str(value).encode("ascii")
            tag = b"i"
        else:
            raise StageSealError("SEALER.STAGE_INVALID")
        digest.update(tag)
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
        digest.update(b";")

    def frame_table(table: str, query: str) -> None:
        digest.update(b"table:")
        frame(table)
        cursor = connection.execute(query)
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            for cell in row:
                frame(cell)
            digest.update(b"\n")

    def has_table(table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row == (1,)

    frame_table(
        "tm_meta",
        "SELECT key, value FROM tm_meta ORDER BY key",
    )
    frame_table(
        "tm_origin_batch",
        "SELECT batch_id, kind, source_digest, source_path, status, "
        "valid_count, invalid_count, duplicate_source_count, "
        "completed_revision, created_at "
        "FROM tm_origin_batch ORDER BY batch_id",
    )
    frame_table(
        "tm_snapshot_receipt",
        "SELECT snapshot_id, resource_id, canonical_store_id, "
        "exported_revision, jsonl_digest, record_count, format_version, "
        "destination_jsonl_path, destination_manifest_path, status, "
        "created_at FROM tm_snapshot_receipt ORDER BY snapshot_id",
    )
    frame_table(
        "tm_snapshot_binding",
        "SELECT binding_id, configured_jsonl_path, manifest_path, "
        "snapshot_kind, snapshot_id, binding_version "
        "FROM tm_snapshot_binding ORDER BY binding_id",
    )
    frame_table(
        "tm_record",
        "SELECT record_id, source_raw, target_raw, source_fold_v1, "
        "speaker_raw, context_prev_raw, context_next_raw, file_source, "
        "provenance_json, legacy_line_no, usage_count, last_used, "
        "origin_batch_id, origin_ordinal "
        "FROM tm_record ORDER BY record_id",
    )
    frame_table(
        "tm_gram",
        "SELECT gram_size, gram, record_id FROM tm_gram "
        "ORDER BY gram_size, gram, record_id",
    )
    if has_table("tm_fts"):
        frame_table(
            "tm_fts",
            "SELECT record_id, source_fold_v1 FROM tm_fts "
            "ORDER BY record_id",
        )
    frame_table(
        "sqlite_master",
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
        "ORDER BY type, name",
    )
    return digest.hexdigest()


def _validate_stage_facts(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
) -> _StageFacts:
    """Independently validate every seal-relevant fact from disk once."""

    identity = stage.resource_identity
    try:
        schema = inspect_stage_schema(
            stage,
            canonical_store_id=canonical_store_id,
        )
        if type(schema.activation_status) is not str:
            raise StageSealError("SEALER.STAGE_INVALID")

        connection = _open_stage_read_connection(stage.staged_db_path)
        try:
            connection.execute("BEGIN")
            try:
                _require_stage_meta_unpublished(connection)
                _require_schema_facts_consistent(connection, schema)
                integrity_rows = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                if integrity_rows != [("ok",)]:
                    raise StageSealError("SEALER.INTEGRITY_FAILED")
                if connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall():
                    raise StageSealError("SEALER.FOREIGN_KEY_FAILED")

                batch_rows = connection.execute(
                    "SELECT batch_id, kind, source_digest, source_path, "
                    "status, valid_count, invalid_count, "
                    "duplicate_source_count, completed_revision "
                    "FROM tm_origin_batch ORDER BY batch_id"
                ).fetchall()
                if len(batch_rows) != 1:
                    raise StageSealError("SEALER.ORIGIN_ANCESTRY_INVALID")
                batch = batch_rows[0]
                batch_id = _text_row(batch[0], "batch_id")
                batch_kind = _text_row(batch[1], "batch kind")
                batch_source_digest = _text_row(
                    batch[2],
                    "batch source_digest",
                )
                batch_source_path = _text_row(batch[3], "batch source_path")
                batch_status = _text_row(batch[4], "batch status")
                batch_valid_count = _int_row(batch[5], "batch valid_count")
                batch_invalid_count = _int_row(
                    batch[6],
                    "batch invalid_count",
                )
                batch_duplicate_count = _int_row(
                    batch[7],
                    "batch duplicate_source_count",
                )
                batch_completed_revision = _int_row(
                    batch[8],
                    "batch completed_revision",
                )
                if batch_kind != "migration" or batch_status != "completed":
                    raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
                if batch_completed_revision != 1:
                    raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")
                if batch_source_path != str(identity.configured_jsonl_path):
                    raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")

                receipt_rows = connection.execute(
                    "SELECT snapshot_id, resource_id, canonical_store_id, "
                    "exported_revision, jsonl_digest, record_count, "
                    "format_version, destination_jsonl_path, "
                    "destination_manifest_path, status "
                    "FROM tm_snapshot_receipt ORDER BY snapshot_id"
                ).fetchall()
                if len(receipt_rows) != 1:
                    raise StageSealError("SEALER.RECEIPT_LEDGER_INVALID")
                receipt_row = receipt_rows[0]
                receipt = _receipt_from_ledger_row(receipt_row)
                if (
                    receipt.resource_id != identity.resource_id
                    or receipt.canonical_store_id != canonical_store_id
                    or receipt.exported_revision != 1
                    or receipt.format_version != SNAPSHOT_FORMAT_VERSION
                    or _text_row(
                        receipt_row[7],
                        "destination_jsonl_path",
                    )
                    != str(identity.configured_jsonl_path)
                    or _text_row(
                        receipt_row[8],
                        "destination_manifest_path",
                    )
                    != str(identity.snapshot_manifest_path)
                    or _text_row(receipt_row[9], "receipt status")
                    != "issued"
                ):
                    raise StageSealError("SEALER.RECEIPT_INVALID")
                if connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_binding"
                ).fetchone() != (0,):
                    raise StageSealError("SEALER.BINDING_NOT_UNPUBLISHED")

                record_cursor = connection.execute(
                    "SELECT record_id, source_raw, target_raw, "
                    "source_fold_v1, speaker_raw, context_prev_raw, "
                    "context_next_raw, file_source, provenance_json, "
                    "legacy_line_no, origin_batch_id, origin_ordinal "
                    "FROM tm_record ORDER BY record_id"
                )
                scan_digest = hashlib.sha256()
                jsonl_winners: dict[str, str] = {}
                stage_winners: dict[str, str] = {}
                valid_count = 0
                invalid_count = 0
                duplicate_source_count = 0
                source_counts: dict[str, int] = {}
                ordinal = 0
                record = record_cursor.fetchone()
                with identity.configured_jsonl_path.open("rb") as stream:
                    for line_number, raw_line in enumerate(
                        stream,
                        start=1,
                    ):
                        scan_digest.update(raw_line)
                        try:
                            decoded_line = raw_line.decode("utf-8")
                            payload = json.loads(
                                decoded_line,
                                parse_constant=_reject_json_constant,
                            )
                        except (
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                            ValueError,
                        ):
                            invalid_count += 1
                            continue
                        accepted = _accepted_jsonl_row(payload)
                        if accepted is None:
                            invalid_count += 1
                            continue
                        (
                            source_raw,
                            target_raw,
                            speaker_raw,
                            context_prev_raw,
                            context_next_raw,
                            file_source,
                        ) = accepted
                        prior_count = source_counts.get(source_raw, 0)
                        source_counts[source_raw] = prior_count + 1
                        if prior_count == 1:
                            duplicate_source_count += 1
                        jsonl_winners[source_raw] = target_raw
                        valid_count += 1
                        if record is None:
                            raise StageSealError(
                                "SEALER.RECORD_COUNT_MISMATCH"
                            )
                        _validate_record_row(
                            record,
                            ordinal=ordinal,
                            line_number=line_number,
                            batch_id=batch_id,
                            accepted=accepted,
                        )
                        stage_winners[source_raw] = target_raw
                        ordinal += 1
                        record = record_cursor.fetchone()
                if record is not None:
                    raise StageSealError("SEALER.RECORD_COUNT_MISMATCH")
                source_digest = scan_digest.hexdigest()
                if (
                    batch_id != f"migration.{source_digest}"
                    or batch_source_digest != source_digest
                    or batch_valid_count != valid_count
                    or batch_invalid_count != invalid_count
                    or batch_duplicate_count != duplicate_source_count
                ):
                    raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
                if receipt.snapshot_id != (
                    f"snapshot.migration.{source_digest[:24]}"
                ):
                    raise StageSealError("SEALER.RECEIPT_INVALID")
                if receipt.jsonl_digest != source_digest:
                    raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
                if receipt.record_count != valid_count:
                    raise StageSealError("SEALER.RECEIPT_INVALID")
                if jsonl_winners != stage_winners:
                    raise StageSealError("SEALER.EXACT_PARITY_MISMATCH")
                exact_parity_digest = _winners_parity_digest(stage_winners)

                required_sizes = (
                    (1, 2) if schema.fts5_available else (1, 2, 3)
                )
                gram_counts = _validate_gram_index(
                    connection,
                    required_sizes=required_sizes,
                )
                fts_count = 0
                if schema.fts5_available:
                    fts_count = _validate_fts_index(connection)
                closure_digest = _stage_closure_digest(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

        return _StageFacts(
            resource_id=identity.resource_id,
            target_identity=identity.target_identity,
            schema_version=schema.schema_version,
            fold_version=schema.fold_version,
            index_version=schema.candidate_index_version,
            fts5_available=schema.fts5_available,
            record_count=valid_count,
            origin_batch_count=1,
            fts_count=fts_count,
            gram_counts=tuple(
                (size, gram_counts[size]) for size in required_sizes
            ),
            receipt=receipt,
            exact_parity_digest=exact_parity_digest,
            closure_digest=closure_digest,
        )
    except StageSealError:
        raise
    except (
        OSError,
        sqlite3.DatabaseError,
        SQLiteStoreSchemaError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as error:
        raise StageSealError("SEALER.STAGE_INVALID") from error


def _require_stage_meta_unpublished(
    connection: sqlite3.Connection,
) -> None:
    meta_rows = connection.execute(
        "SELECT key, value FROM tm_meta"
    ).fetchall()
    meta = {str(row[0]): str(row[1]) for row in meta_rows}
    if meta.get("activation_status") != "UNPUBLISHED":
        raise StageSealError("SEALER.STAGE_NOT_UNPUBLISHED")
    if "activation_digest" in meta:
        raise StageSealError("SEALER.STAGE_ALREADY_ACTIVATED")
    if meta.get("generation") != "0":
        raise StageSealError("SEALER.STAGE_GENERATION_ACTIVE")
    if meta.get("divergence_latched") != "0":
        raise StageSealError("SEALER.STAGE_DIVERGED")
    if meta.get("head_revision") != "1":
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")


def _require_schema_facts_consistent(
    connection: sqlite3.Connection,
    schema: SQLiteSchemaSnapshot,
) -> None:
    """Bind the inspected schema facts to the validation read snapshot."""

    meta_rows = connection.execute(
        "SELECT key, value FROM tm_meta"
    ).fetchall()
    meta = {str(row[0]): str(row[1]) for row in meta_rows}
    if (
        meta.get("schema_version") != str(schema.schema_version)
        or meta.get("fold_version") != schema.fold_version
        or meta.get("candidate_index_version")
        != schema.candidate_index_version
        or meta.get("fts5_available")
        != ("1" if schema.fts5_available else "0")
        or meta.get("resource_id") != schema.resource_id
        or meta.get("canonical_store_id") != schema.canonical_store_id
        or meta.get("target_identity") != schema.target_identity
    ):
        raise StageSealError("SEALER.STAGE_INVALID")


def _validate_record_row(
    record: tuple[object, ...],
    *,
    ordinal: int,
    line_number: int,
    batch_id: str,
    accepted: tuple[
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
    ],
) -> None:
    record_id = _int_row(record[0], "record_id")
    if record_id != ordinal + 1:
        raise StageSealError("SEALER.RECORD_IDENTITY_INVALID")
    source_raw = _text_row(record[1], "record source_raw")
    target_raw = _text_row(record[2], "record target_raw")
    stored_fold = _text_row(record[3], "record source_fold_v1")
    speaker_raw = _optional_text_row(record[4], "record speaker_raw")
    context_prev_raw = _optional_text_row(
        record[5],
        "record context_prev_raw",
    )
    context_next_raw = _optional_text_row(
        record[6],
        "record context_next_raw",
    )
    file_source = _optional_text_row(record[7], "record file_source")
    provenance_json = _text_row(record[8], "record provenance_json")
    legacy_line_no = _int_row(record[9], "record legacy_line_no")
    origin_batch_id = _text_row(record[10], "record origin_batch_id")
    origin_ordinal = _int_row(record[11], "record origin_ordinal")
    if (
        source_raw != accepted[0]
        or target_raw != accepted[1]
        or speaker_raw != accepted[2]
        or context_prev_raw != accepted[3]
        or context_next_raw != accepted[4]
        or file_source != accepted[5]
    ):
        raise StageSealError("SEALER.RECORD_MISMATCH")
    if provenance_json != _EXPECTED_PROVENANCE_JSON:
        raise StageSealError("SEALER.PROVENANCE_MISMATCH")
    if legacy_line_no != line_number or origin_ordinal != ordinal:
        raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
    if origin_batch_id != batch_id:
        raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
    projected = fold_text_v1(source_raw)
    if type(projected.folded_text) is not str:
        raise StageSealError("SEALER.STAGE_INVALID")
    if projected.folded_text != stored_fold:
        raise StageSealError("SEALER.FOLD_MISMATCH")


def _validate_gram_index(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
) -> dict[int, int]:
    folded_cursor = connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record "
        "ORDER BY record_id"
    )
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    gram_counts: dict[int, int] = {size: 0 for size in required_sizes}
    current = gram_cursor.fetchone()
    for folded_row in folded_cursor:
        record_id = _int_row(folded_row[0], "fold record_id")
        folded_source = _text_row(
            folded_row[1],
            "fold source_fold_v1",
        )
        actual: set[tuple[int, str]] = set()
        while current is not None:
            gram_record_id = _int_row(current[0], "gram record_id")
            if gram_record_id != record_id:
                break
            gram_size = _int_row(current[1], "gram_size")
            gram = _text_row(current[2], "gram")
            actual.add((gram_size, gram))
            current = gram_cursor.fetchone()
        expected: set[tuple[int, str]] = set()
        for gram_size in required_sizes:
            expected.update(
                (gram_size, gram)
                for gram in unique_character_ngrams(
                    folded_source,
                    gram_size,
                )
            )
        if actual != expected:
            raise StageSealError("SEALER.CANDIDATE_INDEX_INCOMPLETE")
        for gram_size in required_sizes:
            gram_counts[gram_size] += sum(
                1 for size, _gram in actual if size == gram_size
            )
    if current is not None:
        raise StageSealError("SEALER.CANDIDATE_INDEX_INCOMPLETE")
    return gram_counts


def _validate_fts_index(connection: sqlite3.Connection) -> int:
    folded_cursor = connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record "
        "ORDER BY record_id"
    )
    fts_cursor = connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_fts ORDER BY record_id"
    )
    fts_count = 0
    current = fts_cursor.fetchone()
    for folded_row in folded_cursor:
        record_id = _int_row(folded_row[0], "fold record_id")
        folded_source = _text_row(
            folded_row[1],
            "fold source_fold_v1",
        )
        actual: set[tuple[int, str]] = set()
        while current is not None:
            fts_record_id = _int_row(current[0], "fts record_id")
            if fts_record_id != record_id:
                break
            fts_folded = _text_row(current[1], "fts source_fold_v1")
            actual.add((fts_record_id, fts_folded))
            current = fts_cursor.fetchone()
        if actual != {(record_id, folded_source)}:
            raise StageSealError("SEALER.FTS_INDEX_INCOMPLETE")
        fts_count += 1
    if current is not None:
        raise StageSealError("SEALER.FTS_INDEX_INCOMPLETE")
    return fts_count


def _artifact_file_identity(
    path: Path,
    *,
    missing_code: str,
    unsafe_code: str,
) -> _ArtifactFileIdentity:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise StageSealError(missing_code) from error
    if not stat.S_ISREG(observed.st_mode):
        raise StageSealError(unsafe_code)
    return _ArtifactFileIdentity(observed.st_dev, observed.st_ino)


def _require_identity_unchanged(
    path: Path,
    expected: _ArtifactFileIdentity,
    *,
    unsafe_code: str,
) -> None:
    observed = _artifact_file_identity(
        path,
        missing_code=unsafe_code,
        unsafe_code=unsafe_code,
    )
    if (observed.device, observed.inode) != (
        expected.device,
        expected.inode,
    ):
        raise StageSealError(unsafe_code)


def _mark_stage_sealed(
    path: Path,
    expected: _ArtifactFileIdentity,
    *,
    expected_closure_digest: str,
) -> None:
    """Recheck the validation closure, then write the SEALED marker.

    Content fsync has already completed and registration happens only
    afterwards; the closure recheck runs under the write lock before the
    marker changes, so a writer that committed after validation cannot
    slip undetected into the sealed bytes.
    """

    _require_identity_unchanged(
        path,
        expected,
        unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
    )
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=rw",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
    except sqlite3.DatabaseError as error:
        raise StageSealError("SEALER.STAGE_INVALID") from error
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            observed_closure_digest = _stage_closure_digest(connection)
            if observed_closure_digest != expected_closure_digest:
                raise StageSealError(
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION"
                )
            cursor = connection.execute(
                "UPDATE tm_meta SET value = 'SEALED' "
                "WHERE key = 'activation_status' AND value = 'UNPUBLISHED'"
            )
            if cursor.rowcount != 1:
                raise StageSealError("SEALER.STAGE_NOT_UNPUBLISHED")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    except sqlite3.DatabaseError as error:
        raise StageSealError("SEALER.STAGE_INVALID") from error
    finally:
        connection.close()


def _fsync_file(path: Path, expected: _ArtifactFileIdentity) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StageSealError("SEALER.FSYNC_FAILED") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or (observed.st_dev, observed.st_ino)
            != (expected.device, expected.inode)
        ):
            raise StageSealError("SEALER.STAGE_PATH_UNSAFE")
        os.fsync(descriptor)
    except OSError as error:
        raise StageSealError("SEALER.FSYNC_FAILED") from error
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StageSealError("SEALER.FSYNC_FAILED") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise StageSealError("SEALER.FSYNC_FAILED") from error
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _fsync_stage_assets(
    stage: MutableStageRef,
    database_identity: _ArtifactFileIdentity,
    manifest_identity: _ArtifactFileIdentity,
) -> None:
    """Close-then-fsync the staged DB, temp manifest, then parent directory."""

    _fsync_file(stage.staged_db_path, database_identity)
    _fsync_file(stage.manifest_temp_path, manifest_identity)
    _fsync_directory(stage.staged_db_path.parent)


def _file_sha256(
    path: Path,
    expected: _ArtifactFileIdentity | None = None,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StageSealError("SEALER.DIGEST_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise StageSealError("SEALER.STAGE_PATH_UNSAFE")
        if expected is not None and (
            observed.st_dev,
            observed.st_ino,
        ) != (expected.device, expected.inode):
            raise StageSealError("SEALER.STAGE_PATH_UNSAFE")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise StageSealError("SEALER.DIGEST_UNREADABLE") from error
    finally:
        os.close(descriptor)


def _verify_manifest_at_digest(
    path: Path,
    expected: _ArtifactFileIdentity,
    receipt: SnapshotReceipt,
) -> tuple[str, SnapshotManifest]:
    """Read, digest, and close the sealed manifest temporary in one pass."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StageSealError("SEALER.DIGEST_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or (observed.st_dev, observed.st_ino)
            != (expected.device, expected.inode)
        ):
            raise StageSealError("SEALER.STAGE_PATH_UNSAFE")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            payload.extend(chunk)
    except OSError as error:
        raise StageSealError("SEALER.DIGEST_UNREADABLE") from error
    finally:
        os.close(descriptor)
    try:
        decoded = contract_from_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise StageSealError("SEALER.MANIFEST_INVALID") from error
    if type(decoded) is not SnapshotManifest:
        raise StageSealError("SEALER.MANIFEST_INVALID")
    manifest = decoded
    if (
        manifest.manifest_version != SNAPSHOT_MANIFEST_VERSION
        or type(manifest.snapshot_kind) is not SnapshotKind
        or manifest.snapshot_kind is not SnapshotKind.MIGRATION_SOURCE
        or manifest.receipt != receipt
        or manifest.receipt_digest
        != snapshot_receipt_digest(manifest.receipt)
    ):
        raise StageSealError("SEALER.MANIFEST_MISMATCH")
    return hashlib.sha256(bytes(payload)).hexdigest(), manifest


def _verify_sealed_stage(
    stage: MutableStageRef,
    *,
    record_count: int,
    database_identity: _ArtifactFileIdentity,
) -> None:
    """Reopen after fsync and confirm the sealed state closed on disk."""

    _require_identity_unchanged(
        stage.staged_db_path,
        database_identity,
        unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
    )
    connection = _open_stage_read_connection(stage.staged_db_path)
    try:
        connection.execute("BEGIN")
        try:
            integrity_rows = connection.execute(
                "PRAGMA integrity_check"
            ).fetchall()
            if integrity_rows != [("ok",)]:
                raise StageSealError("SEALER.INTEGRITY_FAILED")
            status_rows = connection.execute(
                "SELECT value FROM tm_meta "
                "WHERE key = 'activation_status'"
            ).fetchall()
            if status_rows != [("SEALED",)]:
                raise StageSealError("SEALER.STAGE_NOT_UNPUBLISHED")
            counts = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM tm_record), "
                "(SELECT COUNT(*) FROM tm_origin_batch), "
                "(SELECT COUNT(*) FROM tm_snapshot_receipt)"
            ).fetchone()
            if counts != (record_count, 1, 1):
                raise StageSealError("SEALER.RECORD_COUNT_MISMATCH")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()


def _build_binding(
    identity: CanonicalResourceIdentity,
    receipt: SnapshotReceipt,
    manifest: SnapshotManifest,
) -> SnapshotBinding:
    return SnapshotBinding(
        configured_jsonl_path=identity.configured_jsonl_path,
        manifest_path=identity.snapshot_manifest_path,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        manifest=manifest,
        binding_version=SNAPSHOT_BINDING_VERSION,
    )


def _build_evidence(
    facts: _StageFacts,
    binding: SnapshotBinding,
    *,
    stage_file_digest: str,
    manifest_temp_digest: str,
) -> StageValidationEvidence:
    return StageValidationEvidence(
        evidence_version=STAGE_VALIDATION_EVIDENCE_VERSION,
        resource_id=facts.resource_id,
        target_identity=facts.target_identity,
        source_binding=binding,
        snapshot_receipt_digest=snapshot_receipt_digest(facts.receipt),
        manifest_temp_digest=manifest_temp_digest,
        schema_version=facts.schema_version,
        fold_version=facts.fold_version,
        index_version=facts.index_version,
        record_count=facts.record_count,
        origin_batch_count=facts.origin_batch_count,
        fts_count=facts.fts_count,
        gram_counts=facts.gram_counts,
        exact_parity_digest=facts.exact_parity_digest,
        integrity_ok=True,
        foreign_keys_ok=True,
        stage_file_digest=stage_file_digest,
    )


def _build_generation(
    facts: _StageFacts,
    *,
    canonical_store_id: str,
    expected_prior_generation: int | None,
) -> GenerationExpectation:
    return GenerationExpectation(
        resource_id=facts.resource_id,
        target_identity=facts.target_identity,
        canonical_store_id=canonical_store_id,
        snapshot_receipt_digest=snapshot_receipt_digest(facts.receipt),
        expected_prior_generation=expected_prior_generation,
        expectation_version=GENERATION_EXPECTATION_VERSION,
    )


def _require_generation_closure(
    generation: GenerationExpectation,
    evidence: StageValidationEvidence,
) -> None:
    receipt = evidence.source_binding.receipt
    if (
        generation.resource_id != evidence.resource_id
        or generation.target_identity != evidence.target_identity
        or generation.canonical_store_id != receipt.canonical_store_id
        or generation.snapshot_receipt_digest
        != evidence.snapshot_receipt_digest
    ):
        raise StageSealError("SEALER.GENERATION_MISMATCH")


class SealedArtifactRegistry:
    """Coordinator-owned sealed artifact authority for one namespace.

    The registry is created and owned by the coordinator; the StageSealer
    only registers through it. Token issuance, consumption, and
    cancellation belong to Task 5.5 and fail closed until that exists.
    """

    def __init__(self, *, registry_namespace: str) -> None:
        if type(registry_namespace) is not str:
            raise TypeError("registry_namespace must be a built-in string")
        if not registry_namespace.strip():
            raise ValueError("registry_namespace must not be empty")
        self._registry_namespace = registry_namespace
        self._entries: dict[str, _RegistryEntry] = {}
        self._sealed_paths: dict[tuple[str, str], str] = {}

    @property
    def registry_namespace(self) -> str:
        return self._registry_namespace

    def seal(
        self,
        mutable_stage: MutableStageRef,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
    ) -> SealedStage:
        try:
            stage = _snapshot_stage(mutable_stage)
            claim = _snapshot_evidence(evidence)
            expected_generation = _snapshot_generation(generation)
        except (TypeError, ValueError) as error:
            raise StageSealError("SEALER.TYPE_INVALID") from error
        self._reject_second_seal(stage)
        try:
            contract_module._validate_stage_validation_evidence(claim)
            identity = stage.resource_identity
            if (
                claim.resource_id != identity.resource_id
                or claim.target_identity != identity.target_identity
                or claim.source_binding.configured_jsonl_path
                != identity.configured_jsonl_path
                or claim.source_binding.manifest_path
                != identity.snapshot_manifest_path
            ):
                raise StageSealError("SEALER.EVIDENCE_MISMATCH")
            _require_generation_closure(expected_generation, claim)
            _require_stage_sealed_marker(stage.staged_db_path)
            if _file_sha256(stage.staged_db_path) != claim.stage_file_digest:
                raise StageSealError("SEALER.EVIDENCE_MISMATCH")
            if _file_sha256(stage.manifest_temp_path) != (
                claim.manifest_temp_digest
            ):
                raise StageSealError("SEALER.EVIDENCE_MISMATCH")
            artifact_id = f"artifact.{uuid.uuid4().hex}"
            sealed_stage = contract_module._create_sealed_stage(
                registry_namespace=self._registry_namespace,
                artifact_id=artifact_id,
                mutable_stage=stage,
                evidence=claim,
                generation=expected_generation,
                activation_nonce=f"nonce.{uuid.uuid4().hex}",
            )
        except StageSealError:
            raise
        except (
            OSError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
        ) as error:
            raise StageSealError("SEALER.STAGE_INVALID") from error
        self._entries[artifact_id] = _RegistryEntry(
            mutable=stage,
            stage=sealed_stage,
            state=ActivationCapabilityState.SEALED,
        )
        self._sealed_paths[
            (str(stage.staged_db_path), str(stage.manifest_temp_path))
        ] = artifact_id
        return sealed_stage

    def _reject_second_seal(self, stage: MutableStageRef) -> None:
        key = (str(stage.staged_db_path), str(stage.manifest_temp_path))
        if key in self._sealed_paths:
            raise StageSealError("SEALER.ALREADY_SEALED")

    def _entry(self, stage: SealedStage) -> _RegistryEntry:
        if type(stage) is not SealedStage:
            raise StageSealError("SEALER.REGISTRY_MISMATCH")
        artifact = stage.artifact
        if artifact.registry_namespace != self._registry_namespace:
            raise StageSealError("SEALER.REGISTRY_MISMATCH")
        entry = self._entries.get(artifact.artifact_id)
        if entry is None or entry.stage is not stage:
            raise StageSealError("SEALER.REGISTRY_MISMATCH")
        try:
            contract_module._validate_sealed_stage(stage)
            if (
                contract_module._artifact_seal_digest(
                    registry_namespace=self._registry_namespace,
                    artifact_id=artifact.artifact_id,
                    mutable_stage=entry.mutable,
                    evidence=stage.evidence,
                )
                != artifact.seal_digest
            ):
                raise StageSealError("SEALER.REGISTRY_MISMATCH")
        except (TypeError, ValueError) as error:
            raise StageSealError("SEALER.REGISTRY_MISMATCH") from error
        evidence = stage.evidence
        identity = entry.mutable.resource_identity
        if (
            evidence.resource_id != identity.resource_id
            or evidence.target_identity != identity.target_identity
            or evidence.source_binding.configured_jsonl_path
            != identity.configured_jsonl_path
            or evidence.source_binding.manifest_path
            != identity.snapshot_manifest_path
        ):
            raise StageSealError("SEALER.REGISTRY_MISMATCH")
        if _file_sha256(entry.mutable.staged_db_path) != (
            evidence.stage_file_digest
        ):
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        if _file_sha256(entry.mutable.manifest_temp_path) != (
            evidence.manifest_temp_digest
        ):
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        return entry

    def contains(self, stage: SealedStage) -> bool:
        try:
            self._entry(stage)
        except (StageSealError, TypeError, ValueError, AttributeError):
            return False
        return True

    def state(self, stage: SealedStage) -> ActivationCapabilityState:
        return self._entry(stage).state

    def issue_token(
        self,
        stage: SealedStage,
        *,
        current_generation: int | None,
    ) -> contract_module._ActivationToken:
        _ = stage
        _ = current_generation
        raise StageSealError("SEALER.TOKEN_LIFECYCLE_PENDING")

    def consume(self, token: contract_module._ActivationToken) -> None:
        _ = token
        raise StageSealError("SEALER.TOKEN_LIFECYCLE_PENDING")

    def cancel(self, token: contract_module._ActivationToken) -> None:
        _ = token
        raise StageSealError("SEALER.TOKEN_LIFECYCLE_PENDING")


def _require_stage_sealed_marker(path: Path) -> None:
    """Confirm the on-disk seal marker that only the sealer can set."""

    connection = _open_stage_read_connection(path)
    try:
        rows = connection.execute(
            "SELECT value FROM tm_meta WHERE key = 'activation_status'"
        ).fetchall()
        if rows != [("SEALED",)]:
            raise StageSealError("SEALER.STAGE_NOT_SEALED")
    finally:
        connection.close()


class StageSealer:
    """Validate, close, fsync, and seal one complete immutable stage.

    The sealed-artifact registry is injected and coordinator-owned; this
    class never traps registry authority inside its own instance.
    """

    def __init__(
        self,
        *,
        registry: contract_module._SealedArtifactRegistryPort,
        canonical_store_id: str,
    ) -> None:
        if not callable(getattr(registry, "seal", None)):
            raise StageSealError("SEALER.TYPE_INVALID")
        namespace = registry.registry_namespace
        if type(namespace) is not str or not namespace.strip():
            raise StageSealError("SEALER.TYPE_INVALID")
        if type(canonical_store_id) is not str:
            raise StageSealError("SEALER.TYPE_INVALID")
        if not canonical_store_id.strip():
            raise StageSealError("SEALER.TYPE_INVALID")
        self._registry = registry
        self._canonical_store_id = canonical_store_id

    @property
    def registry(self) -> contract_module._SealedArtifactRegistryPort:
        return self._registry

    @property
    def canonical_store_id(self) -> str:
        return self._canonical_store_id

    def seal(
        self,
        mutable_stage: MutableStageRef,
        *,
        expected_prior_generation: int | None = None,
    ) -> SealedStage:
        """Seal one complete migration stage into one opaque artifact.

        Ordering is validate (capturing a closure digest), fsync content,
        then recheck the closure under the write lock before writing the
        SEALED marker and registering: a reported fsync failure happens
        before any state transition, so the same completed stage can
        deterministically retry without rebuilding or duplicating records.
        """

        if expected_prior_generation is not None:
            if (
                type(expected_prior_generation) is not int
                or isinstance(expected_prior_generation, bool)
            ):
                raise StageSealError("SEALER.TYPE_INVALID")
            if expected_prior_generation < 0:
                raise StageSealError("SEALER.GENERATION_INVALID")
        stage = _snapshot_stage(mutable_stage)
        database_identity = _artifact_file_identity(
            stage.staged_db_path,
            missing_code="SEALER.STAGE_DATABASE_MISSING",
            unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
        )
        manifest_identity = _artifact_file_identity(
            stage.manifest_temp_path,
            missing_code="SEALER.STAGE_MANIFEST_MISSING",
            unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
        )
        facts = _validate_stage_facts(
            stage,
            canonical_store_id=self._canonical_store_id,
        )
        _fsync_stage_assets(stage, database_identity, manifest_identity)
        _mark_stage_sealed(
            stage.staged_db_path,
            database_identity,
            expected_closure_digest=facts.closure_digest,
        )
        stage_file_digest = _file_sha256(
            stage.staged_db_path,
            database_identity,
        )
        manifest_temp_digest, manifest = _verify_manifest_at_digest(
            stage.manifest_temp_path,
            manifest_identity,
            facts.receipt,
        )
        _verify_sealed_stage(
            stage,
            record_count=facts.record_count,
            database_identity=database_identity,
        )
        binding = _build_binding(
            stage.resource_identity,
            facts.receipt,
            manifest,
        )
        evidence = _build_evidence(
            facts,
            binding,
            stage_file_digest=stage_file_digest,
            manifest_temp_digest=manifest_temp_digest,
        )
        generation = _build_generation(
            facts,
            canonical_store_id=self._canonical_store_id,
            expected_prior_generation=expected_prior_generation,
        )
        return self._registry.seal(stage, evidence, generation)


__all__ = ["SealedArtifactRegistry", "StageSealError", "StageSealer"]
