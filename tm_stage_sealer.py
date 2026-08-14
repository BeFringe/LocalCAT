"""Validate, close, fsync, and seal one complete immutable migration artifact."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Any, Protocol, cast
import uuid

import tm_contracts as contract_module
from tm_content_attestation import (
    ContentAttestationError,
    ContentFileProof,
    ContentSemanticFacts,
    SealedContentAttestation,
    _capture_content_file,
    _create_sealed_content_attestation,
    _revalidate_content_file,
)
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
    CandidateProofIndexError,
    SQLiteSchemaSnapshot,
    SQLiteStoreSchemaError,
    _SCHEMA_UPGRADE_META_KEY,
    _SCHEMA_UPGRADE_META_VALUE,
    _legacy_completed_origin_blocks,
    _legacy_revision_ancestry,
    _schema_digest,
    inspect_stage_schema,
    validate_candidate_proof_index,
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
    schema_digest: str
    fold_version: str
    index_version: str
    candidate_index_kind: str
    fts5_available: bool
    sqlite_runtime_version: str
    unicode_runtime_version: str
    journal_mode: str
    synchronous: str
    foreign_keys: bool
    busy_timeout_ms: int
    wal_enabled: bool
    extension_loading_enabled: bool
    record_count: int
    origin_batch_count: int
    origin_batch_id: str
    origin_batch_kind: str
    fts_count: int
    gram_counts: tuple[tuple[int, int], ...]
    receipt: SnapshotReceipt
    exact_parity_digest: str
    closure_digest: str
    schema_upgrade: bool = False
    receipt_boundary_record_count: int | None = None
    fts_boundary_record_count: int | None = None


@dataclass(frozen=True)
class _ArtifactFileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _RegistryReservation:
    """Creation-time identity claim held between reserve and commit."""

    reservation_id: str
    mutable: MutableStageRef
    database_identity: _ArtifactFileIdentity
    manifest_identity: _ArtifactFileIdentity

    @property
    def key(self) -> tuple[str, str]:
        return (
            str(self.mutable.staged_db_path),
            str(self.mutable.manifest_temp_path),
        )


@dataclass(frozen=True)
class _RegistryEntry:
    mutable: MutableStageRef
    stage: SealedStage
    state: ActivationCapabilityState
    database_identity: _ArtifactFileIdentity
    manifest_identity: _ArtifactFileIdentity
    sealed_content_attestation: SealedContentAttestation
    token: contract_module._ActivationToken | None = None


@dataclass(frozen=True)
class _PhysicalReadinessSnapshot:
    """Registry-owned path-bearing facts and sealed claims for Gate B."""

    registry_namespace: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    mutable_stage: MutableStageRef
    evidence: StageValidationEvidence
    generation: GenerationExpectation
    database_identity: _ArtifactFileIdentity
    manifest_identity: _ArtifactFileIdentity
    sealed_content_attestation: SealedContentAttestation


def _content_semantic_facts(facts: _StageFacts) -> ContentSemanticFacts:
    """Freeze the sealer-owned full semantic result for durable reuse."""

    if facts.schema_upgrade:
        if (
            facts.receipt_boundary_record_count is None
            or facts.fts_boundary_record_count is None
        ):
            raise StageSealError("SEALER.STAGE_INVALID")
        receipt_boundary_record_count = (
            facts.receipt_boundary_record_count
        )
        receipt_boundary_fts_count = facts.fts_boundary_record_count
    else:
        receipt_boundary_record_count = facts.record_count
        receipt_boundary_fts_count = facts.fts_count
    return ContentSemanticFacts(
        schema_version=facts.schema_version,
        schema_digest=facts.schema_digest,
        fold_version=facts.fold_version,
        index_version=facts.index_version,
        candidate_index_kind=facts.candidate_index_kind,
        fts5_available=facts.fts5_available,
        sqlite_runtime_version=facts.sqlite_runtime_version,
        unicode_runtime_version=facts.unicode_runtime_version,
        journal_mode=facts.journal_mode,
        synchronous=facts.synchronous,
        foreign_keys=facts.foreign_keys,
        busy_timeout_ms=facts.busy_timeout_ms,
        wal_enabled=facts.wal_enabled,
        extension_loading_enabled=facts.extension_loading_enabled,
        record_count=facts.record_count,
        receipt_boundary_record_count=receipt_boundary_record_count,
        origin_batch_count=facts.origin_batch_count,
        origin_batch_id=facts.origin_batch_id,
        origin_batch_kind=(
            "schema_upgrade" if facts.schema_upgrade else facts.origin_batch_kind
        ),
        exported_revision=facts.receipt.exported_revision,
        fts_count=facts.fts_count,
        receipt_boundary_fts_count=receipt_boundary_fts_count,
        gram_counts=facts.gram_counts,
        exact_parity_digest=facts.exact_parity_digest,
        logical_closure_digest=facts.closure_digest,
    )


def _build_sealed_content_attestation(
    stage: MutableStageRef,
    facts: _StageFacts,
    evidence: StageValidationEvidence,
    generation: GenerationExpectation,
    *,
    database_proof: ContentFileProof | None = None,
    manifest_proof: ContentFileProof | None = None,
    source_proof: ContentFileProof | None = None,
) -> SealedContentAttestation:
    """Construct an attestation only from sealer-owned disk/fact reads."""

    try:
        observed_database = (
            _capture_content_file(stage.staged_db_path)
            if database_proof is None
            else database_proof
        )
        observed_manifest = (
            _capture_content_file(stage.manifest_temp_path)
            if manifest_proof is None
            else manifest_proof
        )
        observed_source = (
            _capture_content_file(
                stage.resource_identity.configured_jsonl_path
            )
            if source_proof is None
            else source_proof
        )
        return _create_sealed_content_attestation(
            resource_id=facts.resource_id,
            target_identity=facts.target_identity,
            canonical_store_id=facts.receipt.canonical_store_id,
            snapshot_receipt_digest=evidence.snapshot_receipt_digest,
            expected_prior_generation=generation.expected_prior_generation,
            evidence_digest=contract_module.stage_validation_evidence_digest(
                evidence
            ),
            database=observed_database,
            manifest=observed_manifest,
            source=observed_source,
            semantic_facts=_content_semantic_facts(facts),
        )
    except ContentAttestationError as error:
        raise StageSealError("SEALER.ATTESTATION_FAILED") from error


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


def _open_stage_write_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=rw",
        uri=True,
        timeout=5.0,
        isolation_level=None,
    )
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
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


def _stage_closure_digests(
    connection: sqlite3.Connection,
    *,
    reconstruct_pre_activation: bool,
) -> tuple[str, str | None]:
    """Bounded digest over every table whose facts enter the evidence.

    The current digest closes every schema, identity, integrity, FK, record,
    index, source-binding, and exact-parity row in one snapshot.  The optional
    second digest counterfactually reverses only the deterministic activation
    receipt/meta transition.  Streaming cursor reads keep peak memory bounded
    regardless of the 100k-row stage size.
    """

    active_digest = hashlib.sha256()
    pre_activation_digest = (
        hashlib.sha256() if reconstruct_pre_activation else None
    )

    def frame(digest: Any, value: object) -> None:
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

    def pre_activation_row(
        table: str,
        row: tuple[object, ...],
    ) -> tuple[object, ...] | None:
        if table == "tm_meta":
            key = row[0]
            if key == "activation_digest":
                return None
            if key == "activation_status":
                return (key, "UNPUBLISHED")
            if key == "generation":
                return (key, "0")
        elif table == "tm_snapshot_receipt":
            return (*row[:9], "issued", *row[10:])
        elif table == "tm_snapshot_binding":
            return None
        return row

    def frame_table(table: str, query: str) -> None:
        active_digest.update(b"table:")
        frame(active_digest, table)
        if pre_activation_digest is not None:
            pre_activation_digest.update(b"table:")
            frame(pre_activation_digest, table)
        cursor = connection.execute(query)
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            for cell in row:
                frame(active_digest, cell)
            active_digest.update(b"\n")
            if pre_activation_digest is not None:
                reconstructed = pre_activation_row(table, row)
                if reconstructed is not None:
                    for cell in reconstructed:
                        frame(pre_activation_digest, cell)
                    pre_activation_digest.update(b"\n")

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
        "source_fold_length, speaker_raw, context_prev_raw, context_next_raw, file_source, "
        "provenance_json, legacy_line_no, usage_count, last_used, "
        "origin_batch_id, origin_ordinal "
        "FROM tm_record ORDER BY record_id",
    )
    frame_table(
        "tm_gram",
        "SELECT gram_size, gram, record_id, term_frequency FROM tm_gram "
        "ORDER BY gram_size, gram, record_id",
    )
    frame_table(
        "tm_candidate_block",
        "SELECT block_id, first_record_id, last_record_id, record_count, "
        "min_source_fold_length, max_source_fold_length "
        "FROM tm_candidate_block ORDER BY block_id",
    )
    frame_table(
        "tm_gram_block_max",
        "SELECT gram_size, gram, block_id, max_term_frequency "
        "FROM tm_gram_block_max ORDER BY gram_size, gram, block_id",
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
    return (
        active_digest.hexdigest(),
        (
            None
            if pre_activation_digest is None
            else pre_activation_digest.hexdigest()
        ),
    )


def _stage_closure_digest(connection: sqlite3.Connection) -> str:
    """Return the one exact logical closure for the current DB snapshot."""

    digest, _pre_activation = _stage_closure_digests(
        connection,
        reconstruct_pre_activation=False,
    )
    return digest


def _active_transition_closure_digests(
    connection: sqlite3.Connection,
) -> tuple[str, str]:
    """Return current and reconstructed pre-activation closures in one pass.

    Receipt publication changes only deterministic ledger/meta rows.  The
    normal and cold SEALED owners reconstruct the pre-activation digest under
    their write lock before changing those rows; cold ACTIVE recovery uses the
    same reconstruction before accepting the current digest as the receipt
    owner's expectation.
    """

    active, pre_activation = _stage_closure_digests(
        connection,
        reconstruct_pre_activation=True,
    )
    if pre_activation is None:
        raise StageSealError("SEALER.STAGE_INVALID")
    return active, pre_activation


def _import_batch_token(batch_id: str) -> str:
    """Return the 32-hex token of one import-shaped origin batch id.

    Explicit import origins (Task 5.10) use a fresh collision-resistant
    ``import.<uuid4-hex>`` batch id that is not derivable from the source
    bytes; the sealer proves the shape and binds the snapshot receipt id
    to the same token so the sealed attestation identifies the exact one
    batch the activation will publish.
    """

    if not batch_id.startswith("import."):
        raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
    token = batch_id[len("import."):]
    if len(token) != 32 or any(
        character not in "0123456789abcdef"
        for character in token
    ):
        raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
    return token


def _validate_stage_facts(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    allow_sealed: bool = False,
    schema_upgrade: bool | None = None,
    seal_stage: bool = False,
) -> _StageFacts:
    """Independently validate every seal-relevant fact from disk once.

    The private sealed mode used by the registry compatibility seal path
    validates the same closure on an already closed SEALED stage; it never
    weakens mutable-stage inspection. The private Task 5.11 schema-upgrade
    mode validates a v2 candidate that
    carries the durable ``schema_upgrade_origin`` marker: multiple proven
    origin batches and head revisions, an issued historical receipt, no
    binding, records with preserved provenance/context/usage, and candidate
    indexes recomputed from ``tm_record.source_fold_v1``.  The marker and
    the explicit mode must agree in both directions; when the mode is left
    as ``None`` it is auto-detected from the durable marker so registry
    compatibility sealing validates the identical closure; ordinary
    migration/import sealing rejects marker-bearing stages unchanged.
    """

    if type(seal_stage) is not bool:
        raise StageSealError("SEALER.TYPE_INVALID")
    if seal_stage and allow_sealed:
        raise StageSealError("SEALER.TYPE_INVALID")
    identity = stage.resource_identity
    try:
        connection = (
            _open_stage_write_connection(stage.staged_db_path)
            if seal_stage
            else _open_stage_read_connection(stage.staged_db_path)
        )
        try:
            connection.execute("BEGIN IMMEDIATE" if seal_stage else "BEGIN")
            try:
                schema = inspect_stage_schema(
                    stage,
                    canonical_store_id=canonical_store_id,
                    _allow_sealed=allow_sealed,
                )
                if type(schema.activation_status) is not str:
                    raise StageSealError("SEALER.STAGE_INVALID")
                marker = _read_upgrade_marker(connection)
                if marker is not None and marker != _SCHEMA_UPGRADE_META_VALUE:
                    raise StageSealError(
                        "SEALER.SCHEMA_UPGRADE_MARKER_INVALID"
                    )
                effective_schema_upgrade = marker is not None
                if (
                    schema_upgrade is not None
                    and schema_upgrade != effective_schema_upgrade
                ):
                    raise StageSealError(
                        "SEALER.SCHEMA_UPGRADE_MARKER_INVALID"
                    )
                _require_stage_meta_unpublished(
                    connection,
                    allow_sealed=allow_sealed,
                    schema_upgrade=effective_schema_upgrade,
                )
                if effective_schema_upgrade:
                    facts = _validate_schema_upgrade_stage_facts(
                        connection,
                        schema,
                        identity,
                    )
                    if seal_stage:
                        _mark_stage_sealed(
                            connection,
                            expected_closure_digest=facts.closure_digest,
                        )
                    connection.commit()
                    return facts
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
                if (
                    batch_kind not in {"migration", "import"}
                    or batch_status != "completed"
                ):
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
                    "source_fold_v1, source_fold_length, speaker_raw, context_prev_raw, "
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
                if batch_kind == "migration":
                    if batch_id != f"migration.{source_digest}":
                        raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
                    expected_snapshot_id = (
                        f"snapshot.migration.{source_digest[:24]}"
                    )
                elif batch_kind == "import":
                    import_token = _import_batch_token(batch_id)
                    expected_snapshot_id = (
                        f"snapshot.import.{import_token[:24]}"
                    )
                else:
                    raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
                if (
                    batch_source_digest != source_digest
                    or batch_valid_count != valid_count
                    or batch_invalid_count != invalid_count
                    or batch_duplicate_count != duplicate_source_count
                ):
                    raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
                if receipt.snapshot_id != expected_snapshot_id:
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
                try:
                    gram_count_rows, fts_count = validate_candidate_proof_index(
                        connection,
                        required_sizes=required_sizes,
                        fts5_available=schema.fts5_available,
                    )
                except CandidateProofIndexError as error:
                    raise StageSealError(
                        "SEALER.FTS_INDEX_INCOMPLETE"
                        if "FTS" in str(error)
                        else "SEALER.CANDIDATE_INDEX_INCOMPLETE"
                    ) from error
                gram_counts = dict(gram_count_rows)
                schema_digest_row = connection.execute(
                    "SELECT value FROM tm_meta "
                    "WHERE key = 'schema_digest'"
                ).fetchone()
                if (
                    schema_digest_row is None
                    or type(schema_digest_row[0]) is not str
                ):
                    raise StageSealError("SEALER.STAGE_INVALID")
                meta_schema_digest = schema_digest_row[0]
                schema_digest = _schema_digest(
                    connection,
                    fts5_available=schema.fts5_available,
                )
                if schema_digest != meta_schema_digest:
                    raise StageSealError("SEALER.STAGE_INVALID")
                closure_digest = _stage_closure_digest(connection)
                facts = _StageFacts(
                    resource_id=identity.resource_id,
                    target_identity=identity.target_identity,
                    schema_version=schema.schema_version,
                    schema_digest=schema_digest,
                    fold_version=schema.fold_version,
                    index_version=schema.candidate_index_version,
                    candidate_index_kind=schema.candidate_index_kind,
                    fts5_available=schema.fts5_available,
                    sqlite_runtime_version=schema.sqlite_runtime_version,
                    unicode_runtime_version=schema.unicode_runtime_version,
                    journal_mode=schema.journal_mode,
                    synchronous=schema.synchronous,
                    foreign_keys=schema.foreign_keys,
                    busy_timeout_ms=schema.busy_timeout_ms,
                    wal_enabled=schema.wal_enabled,
                    extension_loading_enabled=schema.extension_loading_enabled,
                    record_count=valid_count,
                    origin_batch_count=1,
                    origin_batch_id=batch_id,
                    origin_batch_kind=batch_kind,
                    fts_count=fts_count,
                    gram_counts=tuple(
                        (size, gram_counts[size]) for size in required_sizes
                    ),
                    receipt=receipt,
                    exact_parity_digest=exact_parity_digest,
                    closure_digest=closure_digest,
                    schema_upgrade=False,
                )
                if seal_stage:
                    _mark_stage_sealed(
                        connection,
                        expected_closure_digest=closure_digest,
                    )
                connection.commit()
                return facts
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()
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
        if allow_sealed and isinstance(error, SQLiteStoreSchemaError):
            raise StageSealError(str(error)) from error
        raise StageSealError("SEALER.STAGE_INVALID") from error


def _read_upgrade_marker(connection: sqlite3.Connection) -> str | None:
    """Read the durable schema-upgrade origin marker inside one snapshot."""

    rows = connection.execute(
        "SELECT value FROM tm_meta WHERE key = ?",
        (_SCHEMA_UPGRADE_META_KEY,),
    ).fetchall()
    if len(rows) == 0:
        return None
    if len(rows) != 1 or type(rows[0][0]) is not str:
        raise StageSealError("SEALER.SCHEMA_UPGRADE_MARKER_INVALID")
    return str(rows[0][0])


def _legacy_snapshot_id_for_first_batch(
    batch_id: str,
    batch_kind: str,
    jsonl_digest: str,
) -> str:
    """Derive the binding receipt id one migration/import-born store must carry.

    The canonical is born by its first completed batch: a ``migration``
    batch binds ``snapshot.migration.<source digest[:24]>`` and an
    ``import`` batch binds ``snapshot.import.<token[:24]>`` where the
    token is the import batch id's 32-hex suffix.  Any other shape fails
    closed so an externally forged receipt id is never re-published.
    """

    if batch_kind == "migration":
        if batch_id != f"migration.{jsonl_digest}":
            raise StageSealError("SEALER.RECEIPT_INVALID")
        return f"snapshot.migration.{jsonl_digest[:24]}"
    if batch_kind == "import":
        token = batch_id[len("import."):]
        if (
            len(token) != 32
            or any(
                character not in "0123456789abcdef"
                for character in token
            )
        ):
            raise StageSealError("SEALER.RECEIPT_INVALID")
        return f"snapshot.import.{token[:24]}"
    raise StageSealError("SEALER.RECEIPT_INVALID")


def _validate_schema_upgrade_stage_facts(
    connection: sqlite3.Connection,
    schema: SQLiteSchemaSnapshot,
    identity: CanonicalResourceIdentity,
) -> _StageFacts:
    """Validate one marker-bearing v2 schema-upgrade candidate in full.

    The candidate is the migrated copy of a realistic ACTIVE v1 store:
    every completed origin batch carries the ``completed_revision``
    derived from the strict record-block proof (never batch-id order),
    every record is preserved verbatim (provenance, context, usage,
    last-used, lineage), the single receipt is re-issued exactly as the
    legacy ledger described it (an issued historical export is allowed,
    and its JSONL must be a byte-consistent export of the store at that
    revision), the binding is empty until activation re-publishes the
    identical binding, and the candidate gram/FTS indexes are rebuilt
    from ``tm_record.source_fold_v1``.  Divergence, tampering, or any
    unprovable ordering fails closed.
    """

    _require_schema_facts_consistent(connection, schema)
    integrity_rows = connection.execute(
        "PRAGMA integrity_check"
    ).fetchall()
    if integrity_rows != [("ok",)]:
        raise StageSealError("SEALER.INTEGRITY_FAILED")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise StageSealError("SEALER.FOREIGN_KEY_FAILED")
    if schema.head_revision < 1:
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")

    record_count = _int_row(
        connection.execute("SELECT COUNT(*) FROM tm_record").fetchone()[0],
        "record count",
    )
    try:
        completed_blocks = _legacy_completed_origin_blocks(connection)
        cumulative = _legacy_revision_ancestry(
            connection,
            head_revision=schema.head_revision,
            record_count=record_count,
        )
    except SQLiteStoreSchemaError as error:
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID") from error
    if (
        len(completed_blocks) != schema.head_revision
        or tuple(revision for _batch_id, revision, _count in completed_blocks)
        != tuple(range(1, schema.head_revision + 1))
    ):
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")
    revision_by_batch = {
        batch_id: revision for batch_id, revision, _count in completed_blocks
    }
    cumulative_by_revision = dict(cumulative)

    batch_rows = connection.execute(
        "SELECT batch_id, kind, source_digest, source_path, status, "
        "valid_count, invalid_count, duplicate_source_count, "
        "completed_revision FROM tm_origin_batch ORDER BY batch_id"
    ).fetchall()
    if not batch_rows:
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")
    kind_by_batch: dict[str, str] = {}
    for batch in batch_rows:
        batch_id = _text_row(batch[0], "batch_id")
        batch_kind = _text_row(batch[1], "batch kind")
        batch_source_digest = _optional_text_row(
            batch[2],
            "batch source_digest",
        )
        batch_source_path = _optional_text_row(batch[3], "batch source_path")
        batch_status = _text_row(batch[4], "batch status")
        batch_valid_count = _int_row(batch[5], "batch valid_count")
        batch_invalid_count = _int_row(batch[6], "batch invalid_count")
        batch_duplicate_count = _int_row(
            batch[7],
            "batch duplicate_source_count",
        )
        batch_completed_revision = batch[8]
        if batch_kind not in {"migration", "local_write", "import"}:
            raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
        if (
            batch_valid_count < 0
            or batch_invalid_count < 0
            or batch_duplicate_count < 0
        ):
            raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
        if batch_status == "completed":
            if batch_completed_revision is not None:
                batch_completed_revision = _int_row(
                    batch_completed_revision,
                    "batch completed_revision",
                )
            if (
                revision_by_batch.get(batch_id) != batch_completed_revision
            ):
                raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")
        else:
            if batch_status not in {"staged", "failed"}:
                raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
            if batch_completed_revision is not None:
                raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")
        if batch_kind == "local_write":
            if batch_source_digest is not None or batch_source_path is not None:
                raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
        elif (
            type(batch_source_digest) is not str
            or len(batch_source_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in batch_source_digest
            )
            or type(batch_source_path) is not str
            or not batch_source_path
        ):
            raise StageSealError("SEALER.MIGRATION_BATCH_INVALID")
        kind_by_batch[batch_id] = batch_kind

    first_batch_id, first_batch_revision, _first_count = completed_blocks[0]
    if first_batch_revision != 1:
        raise StageSealError("SEALER.REVISION_ANCESTRY_INVALID")

    record_cursor = connection.execute(
        "SELECT record_id, source_raw, target_raw, source_fold_v1, "
        "source_fold_length, speaker_raw, context_prev_raw, context_next_raw, file_source, "
        "provenance_json, legacy_line_no, usage_count, last_used, "
        "origin_batch_id, origin_ordinal "
        "FROM tm_record ORDER BY record_id"
    )
    stage_winners: dict[str, str] = {}
    ordinal = 0
    for record in record_cursor:
        record_id = _int_row(record[0], "record_id")
        if record_id != ordinal + 1:
            raise StageSealError("SEALER.RECORD_IDENTITY_INVALID")
        source_raw = _text_row(record[1], "record source_raw")
        target_raw = _text_row(record[2], "record target_raw")
        stored_fold = _text_row(record[3], "record source_fold_v1")
        stored_fold_length = _int_row(record[4], "record source_fold_length")
        provenance_json = _text_row(record[9], "record provenance_json")
        legacy_line_no = record[10]
        if legacy_line_no is not None:
            legacy_line_no = _int_row(
                legacy_line_no,
                "record legacy_line_no",
            )
            if legacy_line_no < 1:
                raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
        usage_count = _int_row(record[11], "record usage_count")
        if usage_count < 0:
            raise StageSealError("SEALER.RECORD_INVALID")
        last_used = record[12]
        if last_used is not None and type(last_used) is not str:
            raise StageSealError("SEALER.RECORD_INVALID")
        origin_batch_id = _text_row(record[13], "record origin_batch_id")
        origin_ordinal = _int_row(record[14], "record origin_ordinal")
        if origin_ordinal < 0:
            raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
        batch_kind = kind_by_batch.get(origin_batch_id)
        if batch_kind is None:
            raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
        if batch_kind in {"migration", "import"}:
            if legacy_line_no is None:
                raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
        elif legacy_line_no is not None:
            raise StageSealError("SEALER.RECORD_LINEAGE_INVALID")
        projected = fold_text_v1(source_raw)
        if (
            type(projected.folded_text) is not str
            or projected.folded_text != stored_fold
            or stored_fold_length != len(stored_fold)
        ):
            raise StageSealError("SEALER.FOLD_MISMATCH")
        if provenance_json != _EXPECTED_PROVENANCE_JSON:
            raise StageSealError("SEALER.PROVENANCE_MISMATCH")
        stage_winners[source_raw] = target_raw
        ordinal += 1
    if ordinal != record_count:
        raise StageSealError("SEALER.RECORD_COUNT_MISMATCH")

    receipt_rows = connection.execute(
        "SELECT snapshot_id, resource_id, canonical_store_id, "
        "exported_revision, jsonl_digest, record_count, format_version, "
        "destination_jsonl_path, destination_manifest_path, status "
        "FROM tm_snapshot_receipt ORDER BY snapshot_id"
    ).fetchall()
    if len(receipt_rows) != 1:
        raise StageSealError("SEALER.RECEIPT_LEDGER_INVALID")
    receipt_row = receipt_rows[0]
    receipt = _receipt_from_ledger_row(receipt_row)
    if (
        receipt.resource_id != identity.resource_id
        or receipt.canonical_store_id != schema.canonical_store_id
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
        or _text_row(receipt_row[9], "receipt status") != "issued"
    ):
        raise StageSealError("SEALER.RECEIPT_INVALID")
    first_batch_kind = kind_by_batch[first_batch_id]
    first_batch_source_digest = _optional_text_row(
        connection.execute(
            "SELECT source_digest FROM tm_origin_batch WHERE batch_id = ?",
            (first_batch_id,),
        ).fetchone()[0],
        "first batch source_digest",
    )
    if (
        first_batch_kind not in {"migration", "import"}
        or first_batch_source_digest != receipt.jsonl_digest
        or receipt.exported_revision not in cumulative_by_revision
        or receipt.record_count
        != cumulative_by_revision[receipt.exported_revision]
    ):
        raise StageSealError("SEALER.RECEIPT_INVALID")
    receipt_boundary = cumulative_by_revision[receipt.exported_revision]
    fts_boundary_record_count = 0
    if schema.fts5_available:
        fts_boundary_record_count = _int_row(
            connection.execute(
                "SELECT COUNT(*) FROM tm_fts WHERE record_id <= ?",
                (receipt_boundary,),
            ).fetchone()[0],
            "fts boundary record count",
        )

    scan_digest = hashlib.sha256()
    jsonl_winners: dict[str, str] = {}
    valid_count = 0
    matched_count = 0
    boundary_cursor = connection.execute(
        "SELECT record_id, source_raw, target_raw, speaker_raw, "
        "context_prev_raw, context_next_raw, file_source "
        "FROM tm_record WHERE record_id <= ? ORDER BY record_id",
        (receipt_boundary,),
    )
    record = boundary_cursor.fetchone()
    with identity.configured_jsonl_path.open("rb") as stream:
        for _line_number, raw_line in enumerate(stream, start=1):
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
                continue
            accepted = _accepted_jsonl_row(payload)
            if accepted is None:
                continue
            (
                source_raw,
                target_raw,
                speaker_raw,
                context_prev_raw,
                context_next_raw,
                file_source,
            ) = accepted
            jsonl_winners[source_raw] = target_raw
            valid_count += 1
            if record is None:
                raise StageSealError("SEALER.RECORD_COUNT_MISMATCH")
            if (
                _text_row(record[1], "record source_raw") != source_raw
                or _text_row(record[2], "record target_raw") != target_raw
                or _optional_text_row(record[3], "record speaker_raw")
                != speaker_raw
                or _optional_text_row(record[4], "record context_prev_raw")
                != context_prev_raw
                or _optional_text_row(record[5], "record context_next_raw")
                != context_next_raw
                or _optional_text_row(record[6], "record file_source")
                != file_source
            ):
                raise StageSealError("SEALER.RECORD_MISMATCH")
            matched_count += 1
            record = boundary_cursor.fetchone()
    if record is not None:
        raise StageSealError("SEALER.RECORD_COUNT_MISMATCH")
    source_digest = scan_digest.hexdigest()
    if (
        source_digest != receipt.jsonl_digest
        or valid_count != receipt.record_count
        or matched_count != receipt.record_count
    ):
        raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
    restricted_winners: dict[str, str] = {}
    for row in connection.execute(
        "SELECT source_raw, target_raw FROM tm_record "
        "WHERE record_id <= ? ORDER BY record_id",
        (receipt_boundary,),
    ):
        restricted_winners[_text_row(row[0], "source_raw")] = _text_row(
            row[1],
            "target_raw",
        )
    if jsonl_winners != restricted_winners:
        raise StageSealError("SEALER.EXACT_PARITY_MISMATCH")

    if receipt.snapshot_id != _legacy_snapshot_id_for_first_batch(
        first_batch_id,
        first_batch_kind,
        receipt.jsonl_digest,
    ):
        raise StageSealError("SEALER.RECEIPT_INVALID")

    required_sizes = (1, 2) if schema.fts5_available else (1, 2, 3)
    try:
        gram_count_rows, fts_count = validate_candidate_proof_index(
            connection,
            required_sizes=required_sizes,
            fts5_available=schema.fts5_available,
        )
    except CandidateProofIndexError as error:
        raise StageSealError(
            "SEALER.FTS_INDEX_INCOMPLETE"
            if "FTS" in str(error)
            else "SEALER.CANDIDATE_INDEX_INCOMPLETE"
        ) from error
    gram_counts = dict(gram_count_rows)
    schema_digest_row = connection.execute(
        "SELECT value FROM tm_meta "
        "WHERE key = 'schema_digest'"
    ).fetchone()
    if (
        schema_digest_row is None
        or type(schema_digest_row[0]) is not str
    ):
        raise StageSealError("SEALER.STAGE_INVALID")
    schema_digest = _schema_digest(
        connection,
        fts5_available=schema.fts5_available,
    )
    if schema_digest != schema_digest_row[0]:
        raise StageSealError("SEALER.STAGE_INVALID")
    if connection.execute(
        "SELECT COUNT(*) FROM tm_snapshot_binding"
    ).fetchone() != (0,):
        raise StageSealError("SEALER.BINDING_NOT_UNPUBLISHED")
    closure_digest = _stage_closure_digest(connection)
    return _StageFacts(
        resource_id=identity.resource_id,
        target_identity=identity.target_identity,
        schema_version=schema.schema_version,
        schema_digest=schema_digest,
        fold_version=schema.fold_version,
        index_version=schema.candidate_index_version,
        candidate_index_kind=schema.candidate_index_kind,
        fts5_available=schema.fts5_available,
        sqlite_runtime_version=schema.sqlite_runtime_version,
        unicode_runtime_version=schema.unicode_runtime_version,
        journal_mode=schema.journal_mode,
        synchronous=schema.synchronous,
        foreign_keys=schema.foreign_keys,
        busy_timeout_ms=schema.busy_timeout_ms,
        wal_enabled=schema.wal_enabled,
        extension_loading_enabled=schema.extension_loading_enabled,
        record_count=record_count,
        origin_batch_count=len(batch_rows),
        origin_batch_id=first_batch_id,
        origin_batch_kind=first_batch_kind,
        fts_count=fts_count,
        gram_counts=tuple(
            (size, gram_counts[size]) for size in required_sizes
        ),
        receipt=receipt,
        exact_parity_digest=_winners_parity_digest(stage_winners),
        closure_digest=closure_digest,
        schema_upgrade=True,
        receipt_boundary_record_count=receipt_boundary,
        fts_boundary_record_count=fts_boundary_record_count,
    )


def _require_stage_meta_unpublished(
    connection: sqlite3.Connection,
    *,
    allow_sealed: bool = False,
    schema_upgrade: bool = False,
) -> None:
    meta_rows = connection.execute(
        "SELECT key, value FROM tm_meta"
    ).fetchall()
    meta = {str(row[0]): str(row[1]) for row in meta_rows}
    expected_status = "SEALED" if allow_sealed else "UNPUBLISHED"
    if meta.get("activation_status") != expected_status:
        raise StageSealError(
            "SEALER.STAGE_NOT_SEALED"
            if allow_sealed
            else "SEALER.STAGE_NOT_UNPUBLISHED"
        )
    if "activation_digest" in meta:
        raise StageSealError("SEALER.STAGE_ALREADY_ACTIVATED")
    if meta.get("generation") != "0":
        raise StageSealError("SEALER.STAGE_GENERATION_ACTIVE")
    if meta.get("divergence_latched") != "0":
        raise StageSealError("SEALER.STAGE_DIVERGED")
    if not schema_upgrade and meta.get("head_revision") != "1":
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
    stored_fold_length = _int_row(record[4], "record source_fold_length")
    speaker_raw = _optional_text_row(record[5], "record speaker_raw")
    context_prev_raw = _optional_text_row(
        record[6],
        "record context_prev_raw",
    )
    context_next_raw = _optional_text_row(
        record[7],
        "record context_next_raw",
    )
    file_source = _optional_text_row(record[8], "record file_source")
    provenance_json = _text_row(record[9], "record provenance_json")
    legacy_line_no = _int_row(record[10], "record legacy_line_no")
    origin_batch_id = _text_row(record[11], "record origin_batch_id")
    origin_ordinal = _int_row(record[12], "record origin_ordinal")
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
    if (
        projected.folded_text != stored_fold
        or stored_fold_length != len(stored_fold)
    ):
        raise StageSealError("SEALER.FOLD_MISMATCH")


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
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
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
    connection: sqlite3.Connection,
    *,
    expected_closure_digest: str,
) -> None:
    """Write SEALED inside the caller's full-validation write transaction."""

    if type(connection) is not sqlite3.Connection:
        raise StageSealError("SEALER.TYPE_INVALID")
    if (
        type(expected_closure_digest) is not str
        or len(expected_closure_digest) != 64
    ):
        raise StageSealError("SEALER.STAGE_INVALID")
    cursor = connection.execute(
        "UPDATE tm_meta SET value = 'SEALED' "
        "WHERE key = 'activation_status' AND value = 'UNPUBLISHED'"
    )
    if cursor.rowcount != 1:
        raise StageSealError("SEALER.STAGE_NOT_UNPUBLISHED")


def _restore_stage_unpublished(
    path: Path,
    expected: _ArtifactFileIdentity,
) -> None:
    """Durably roll the SEALED marker back to UNPUBLISHED after a failed seal.

    Only the StageSealer recovery path calls this, before any registry
    publication: the stage bytes are unchanged, so a deterministic retry can
    re-validate and re-seal the same completed stage.  Identity is enforced
    exactly like the marker write, so a swapped path is never rewritten.
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
            status_rows = connection.execute(
                "SELECT value FROM tm_meta "
                "WHERE key = 'activation_status'"
            ).fetchall()
            if status_rows == [("UNPUBLISHED",)]:
                connection.commit()
                return
            if status_rows != [("SEALED",)]:
                raise StageSealError("SEALER.STAGE_INVALID")
            cursor = connection.execute(
                "UPDATE tm_meta SET value = 'UNPUBLISHED' "
                "WHERE key = 'activation_status' AND value = 'SEALED'"
            )
            if cursor.rowcount != 1:
                raise StageSealError("SEALER.STAGE_INVALID")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    except sqlite3.DatabaseError as error:
        raise StageSealError("SEALER.STAGE_INVALID") from error
    finally:
        connection.close()
    _fsync_file(path, expected)
    _fsync_directory(path.parent)


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
    *,
    upgrade_manifest: bool = False,
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
        or manifest.receipt != receipt
        or manifest.receipt_digest
        != snapshot_receipt_digest(manifest.receipt)
    ):
        raise StageSealError("SEALER.MANIFEST_MISMATCH")
    if not upgrade_manifest and manifest.snapshot_kind is not (
        SnapshotKind.MIGRATION_SOURCE
    ):
        raise StageSealError("SEALER.MANIFEST_MISMATCH")
    if upgrade_manifest and manifest.snapshot_kind not in {
        SnapshotKind.MIGRATION_SOURCE,
        SnapshotKind.EXPLICIT_EXPORT,
    }:
        raise StageSealError("SEALER.MANIFEST_MISMATCH")
    return hashlib.sha256(bytes(payload)).hexdigest(), manifest


def _verify_sealed_stage(
    stage: MutableStageRef,
    *,
    record_count: int,
    origin_batch_count: int,
    receipt_count: int,
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
            if counts != (
                record_count,
                origin_batch_count,
                receipt_count,
            ):
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
        snapshot_kind=manifest.snapshot_kind,
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
    if facts.schema_upgrade:
        if (
            facts.receipt_boundary_record_count is None
            or facts.fts_boundary_record_count is None
        ):
            raise StageSealError("SEALER.STAGE_INVALID")
        record_count = facts.receipt_boundary_record_count
        fts_count = facts.fts_boundary_record_count
    else:
        record_count = facts.record_count
        fts_count = facts.fts_count
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
        record_count=record_count,
        origin_batch_count=facts.origin_batch_count,
        fts_count=fts_count,
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


def _require_linearization_closure(
    snapshot: _PhysicalReadinessSnapshot,
    attestation: SealedContentAttestation,
) -> None:
    """Terminal closure at the Gate B linearization point, before the grant.

    Rehashes all three files after claim closure and immediately before Gate B
    mints its grant, comparing the no-follow file proofs with the
    registry-owned sealed attestation. Any mutation or swap in or after claim
    closure denies here, so a successful grant proves the exact artifact and
    configured-source bytes at this linearization point. Path-bearing facts
    stay private to this module.
    """

    if type(attestation) is not SealedContentAttestation:
        raise StageSealError("SEALER.ATTESTATION_INVALID")
    try:
        observed_database = _capture_content_file(
            snapshot.mutable_stage.staged_db_path
        )
    except ContentAttestationError as error:
        raise StageSealError("SEALER.STAGE_DATABASE_UNSAFE") from error
    try:
        observed_manifest = _capture_content_file(
            snapshot.mutable_stage.manifest_temp_path
        )
    except ContentAttestationError as error:
        raise StageSealError("SEALER.STAGE_MANIFEST_UNSAFE") from error
    try:
        observed_source = _capture_content_file(
            snapshot.mutable_stage.resource_identity.configured_jsonl_path
        )
    except ContentAttestationError as error:
        raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH") from error
    if (
        observed_database.device,
        observed_database.inode,
    ) != (attestation.database.device, attestation.database.inode):
        raise StageSealError("SEALER.STAGE_DATABASE_UNSAFE")
    if (
        observed_manifest.device,
        observed_manifest.inode,
    ) != (attestation.manifest.device, attestation.manifest.inode):
        raise StageSealError("SEALER.STAGE_MANIFEST_UNSAFE")
    if (
        observed_source.device,
        observed_source.inode,
    ) != (attestation.source.device, attestation.source.inode):
        raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")
    if (
        observed_database.sha256 != attestation.database.sha256
        or observed_database.size != attestation.database.size
        or observed_manifest.sha256 != attestation.manifest.sha256
        or observed_manifest.size != attestation.manifest.size
    ):
        raise StageSealError("SEALER.ARTIFACT_MUTATED")
    if (
        observed_source.sha256 != attestation.source.sha256
        or observed_source.size != attestation.source.size
    ):
        raise StageSealError("SEALER.SOURCE_DIGEST_MISMATCH")


class _SealLifecycleRegistry(
    contract_module._SealedArtifactRegistryPort,
    Protocol,
):
    """Reservation/commit/release seam the StageSealer requires."""

    @property
    def registry_namespace(self) -> str: ...

    def reserve(
        self,
        mutable_stage: MutableStageRef,
        *,
        database_identity: _ArtifactFileIdentity,
        manifest_identity: _ArtifactFileIdentity,
    ) -> _RegistryReservation: ...

    def commit(
        self,
        reservation: _RegistryReservation,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
        attestation: SealedContentAttestation | None = None,
    ) -> SealedStage: ...

    def release(self, reservation: _RegistryReservation) -> None: ...


class SealedArtifactRegistry:
    """Coordinator-owned sealed artifact authority for one namespace.

    The registry is created and owned by the coordinator; the StageSealer
    registers only through the reservation/commit/release seam, which
    carries the sealer's creation-time no-follow file identities so a
    byte-identical path swap can never be registered.  Task 5.5 adds the
    exact SEALED -> TOKEN_ISSUED -> CONSUMED/CANCELLED lifecycle here, with
    namespace-global nonce replay protection that is never released by a
    terminal transition.
    """

    def __init__(self, *, registry_namespace: str) -> None:
        if type(registry_namespace) is not str:
            raise TypeError("registry_namespace must be a built-in string")
        if not registry_namespace.strip():
            raise ValueError("registry_namespace must not be empty")
        self._registry_namespace = registry_namespace
        self._entries: dict[str, _RegistryEntry] = {}
        self._reservations: dict[tuple[str, str], _RegistryReservation] = {}
        self._sealed_paths: dict[tuple[str, str], str] = {}
        self._tokens: dict[
            str,
            tuple[str, contract_module._ActivationToken],
        ] = {}
        self._claimed_nonces: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def registry_namespace(self) -> str:
        return self._registry_namespace

    def seal(
        self,
        mutable_stage: MutableStageRef,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
    ) -> SealedStage:
        """Single-shot convenience registration for port callers.

        The identities captured here are the ones visible at call time; the
        StageSealer's creation-time identities enter only through reserve(),
        so the sealer flow stays immune to swaps before registration.
        """

        try:
            stage = _snapshot_stage(mutable_stage)
        except (TypeError, ValueError) as error:
            raise StageSealError("SEALER.TYPE_INVALID") from error
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
        reservation = self.reserve(
            stage,
            database_identity=database_identity,
            manifest_identity=manifest_identity,
        )
        try:
            facts = _validate_stage_facts(
                stage,
                canonical_store_id=(
                    evidence.source_binding.receipt.canonical_store_id
                ),
                allow_sealed=True,
            )
            attestation = _build_sealed_content_attestation(
                stage,
                facts,
                evidence,
                generation,
            )
            return self.commit(
                reservation,
                evidence,
                generation,
                attestation,
            )
        except BaseException:
            self.release(reservation)
            raise

    def reserve(
        self,
        mutable_stage: MutableStageRef,
        *,
        database_identity: _ArtifactFileIdentity,
        manifest_identity: _ArtifactFileIdentity,
    ) -> _RegistryReservation:
        """Reserve one stage before any irreversible transition.

        The identities are the sealer's creation-time no-follow captures;
        the registry re-observes the paths immediately so a swap before
        reservation denies instead of being registered.
        """

        with self._lock:
            try:
                stage = _snapshot_stage(mutable_stage)
            except (TypeError, ValueError) as error:
                raise StageSealError("SEALER.TYPE_INVALID") from error
            if (
                type(database_identity) is not _ArtifactFileIdentity
                or type(manifest_identity) is not _ArtifactFileIdentity
            ):
                raise StageSealError("SEALER.TYPE_INVALID")
            self._reject_second_seal(stage)
            key = (str(stage.staged_db_path), str(stage.manifest_temp_path))
            if key in self._reservations:
                raise StageSealError("SEALER.ALREADY_RESERVED")
            observed_database = _artifact_file_identity(
                stage.staged_db_path,
                missing_code="SEALER.STAGE_DATABASE_MISSING",
                unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
            )
            if (observed_database.device, observed_database.inode) != (
                database_identity.device,
                database_identity.inode,
            ):
                raise StageSealError("SEALER.STAGE_DATABASE_UNSAFE")
            observed_manifest = _artifact_file_identity(
                stage.manifest_temp_path,
                missing_code="SEALER.STAGE_MANIFEST_MISSING",
                unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
            )
            if (observed_manifest.device, observed_manifest.inode) != (
                manifest_identity.device,
                manifest_identity.inode,
            ):
                raise StageSealError("SEALER.STAGE_MANIFEST_UNSAFE")
            reservation = _RegistryReservation(
                reservation_id=f"reservation.{uuid.uuid4().hex}",
                mutable=stage,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
            self._reservations[reservation.key] = reservation
            return reservation

    def commit(
        self,
        reservation: _RegistryReservation,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
        attestation: SealedContentAttestation | None = None,
    ) -> SealedStage:
        """Finalize one reservation into the authoritative sealed entry.

        Runs only after the SEALED marker committed and its fsync completed.
        The registry re-observes the paths against the reservation's
        creation-time identities and re-proves the sealed digests before
        recording the entry; any mismatch denies without recording anything.
        """

        with self._lock:
            if type(reservation) is not _RegistryReservation:
                raise StageSealError("SEALER.TYPE_INVALID")
            existing = self._reservations.get(reservation.key)
            if (
                existing is None
                or existing.reservation_id != reservation.reservation_id
            ):
                raise StageSealError("SEALER.RESERVATION_MISMATCH")
            try:
                claim = _snapshot_evidence(evidence)
                expected_generation = _snapshot_generation(generation)
            except (TypeError, ValueError) as error:
                raise StageSealError("SEALER.TYPE_INVALID") from error
            if type(attestation) is not SealedContentAttestation:
                raise StageSealError("SEALER.TYPE_INVALID")
            stage = reservation.mutable
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
                _require_identity_unchanged(
                    stage.staged_db_path,
                    reservation.database_identity,
                    unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
                )
                _require_identity_unchanged(
                    stage.manifest_temp_path,
                    reservation.manifest_identity,
                    unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
                )
                _require_stage_sealed_marker(stage.staged_db_path)
                observed_database = _revalidate_content_file(
                    stage.staged_db_path,
                    attestation.database,
                )
                observed_manifest = _revalidate_content_file(
                    stage.manifest_temp_path,
                    attestation.manifest,
                )
                observed_source = _revalidate_content_file(
                    stage.resource_identity.configured_jsonl_path,
                    attestation.source,
                )
                semantic = attestation.semantic_facts
                if (
                    observed_database.sha256 != claim.stage_file_digest
                    or observed_manifest.sha256 != claim.manifest_temp_digest
                    or observed_source.sha256
                    != claim.source_binding.receipt.jsonl_digest
                    or attestation.resource_id != claim.resource_id
                    or attestation.target_identity != claim.target_identity
                    or attestation.canonical_store_id
                    != claim.source_binding.receipt.canonical_store_id
                    or attestation.snapshot_receipt_digest
                    != claim.snapshot_receipt_digest
                    or attestation.expected_prior_generation
                    != expected_generation.expected_prior_generation
                    or attestation.evidence_digest
                    != contract_module.stage_validation_evidence_digest(claim)
                    or semantic.schema_version != claim.schema_version
                    or semantic.fold_version != claim.fold_version
                    or semantic.index_version != claim.index_version
                    or semantic.receipt_boundary_record_count
                    != claim.record_count
                    or semantic.origin_batch_count != claim.origin_batch_count
                    or semantic.receipt_boundary_fts_count
                    != claim.fts_count
                    or semantic.gram_counts != claim.gram_counts
                    or semantic.exact_parity_digest
                    != claim.exact_parity_digest
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
                ContentAttestationError,
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
                database_identity=reservation.database_identity,
                manifest_identity=reservation.manifest_identity,
                sealed_content_attestation=attestation,
            )
            self._sealed_paths[reservation.key] = artifact_id
            del self._reservations[reservation.key]
            return sealed_stage

    def release(self, reservation: _RegistryReservation) -> None:
        """Release one uncommitted reservation; never touches committed entries."""

        with self._lock:
            if type(reservation) is not _RegistryReservation:
                return
            existing = self._reservations.get(reservation.key)
            if (
                existing is not None
                and existing.reservation_id == reservation.reservation_id
            ):
                del self._reservations[reservation.key]

    def _reject_second_seal(self, stage: MutableStageRef) -> None:
        key = (str(stage.staged_db_path), str(stage.manifest_temp_path))
        if key in self._sealed_paths:
            raise StageSealError("SEALER.ALREADY_SEALED")

    def _match_entry(self, stage: SealedStage) -> _RegistryEntry:
        """Registry membership and full contract chain, without file effects.

        Gate B resolves path-bearing facts only through this narrow seam; the
        caller-provided stage is never a source of paths or evidence claims.
        """

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
        return entry

    def _entry(self, stage: SealedStage) -> _RegistryEntry:
        """Registry authority check that re-proves identities and digests."""

        entry = self._match_entry(stage)
        evidence = entry.stage.evidence
        observed_database = _artifact_file_identity(
            entry.mutable.staged_db_path,
            missing_code="SEALER.STAGE_DATABASE_MISSING",
            unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
        )
        if (observed_database.device, observed_database.inode) != (
            entry.database_identity.device,
            entry.database_identity.inode,
        ):
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        observed_manifest = _artifact_file_identity(
            entry.mutable.manifest_temp_path,
            missing_code="SEALER.STAGE_MANIFEST_MISSING",
            unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
        )
        if (observed_manifest.device, observed_manifest.inode) != (
            entry.manifest_identity.device,
            entry.manifest_identity.inode,
        ):
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        if _file_sha256(
            entry.mutable.staged_db_path,
            entry.database_identity,
        ) != evidence.stage_file_digest:
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        if _file_sha256(
            entry.mutable.manifest_temp_path,
            entry.manifest_identity,
        ) != evidence.manifest_temp_digest:
            raise StageSealError("SEALER.ARTIFACT_MUTATED")
        return entry

    def resolve_physical_readiness(
        self,
        stage: SealedStage,
    ) -> _PhysicalReadinessSnapshot:
        """Resolve one registered sealed artifact for Gate B revalidation.

        Returns only registry-owned paths and sealed claims; Gate B never
        accepts caller-provided paths or evidence objects.
        """

        with self._lock:
            entry = self._match_entry(stage)
            artifact = entry.stage.artifact
            receipt = entry.stage.evidence.source_binding.receipt
            return _PhysicalReadinessSnapshot(
                registry_namespace=self._registry_namespace,
                artifact_id=artifact.artifact_id,
                artifact_seal_digest=artifact.seal_digest,
                sealed_stage_digest=entry.stage.sealed_stage_digest,
                resource_id=entry.stage.evidence.resource_id,
                target_identity=entry.stage.evidence.target_identity,
                canonical_store_id=receipt.canonical_store_id,
                snapshot_receipt_digest=(
                    entry.stage.evidence.snapshot_receipt_digest
                ),
                expected_prior_generation=(
                    entry.stage.generation.expected_prior_generation
                ),
                mutable_stage=entry.mutable,
                evidence=entry.stage.evidence,
                generation=entry.stage.generation,
                database_identity=entry.database_identity,
                manifest_identity=entry.manifest_identity,
                sealed_content_attestation=entry.sealed_content_attestation,
            )

    def contains(self, stage: SealedStage) -> bool:
        with self._lock:
            try:
                self._entry(stage)
            except (StageSealError, TypeError, ValueError, AttributeError):
                return False
            return True

    def state(self, stage: SealedStage) -> ActivationCapabilityState:
        with self._lock:
            return self._entry(stage).state

    def issue_token(
        self,
        stage: SealedStage,
        *,
        current_generation: int | None,
    ) -> contract_module._ActivationToken:
        with self._lock:
            if current_generation is not None and (
                type(current_generation) is not int
                or isinstance(current_generation, bool)
            ):
                raise StageSealError("SEALER.GENERATION_INVALID")
            if current_generation is not None and current_generation < 0:
                raise StageSealError("SEALER.GENERATION_INVALID")
            entry = self._entry(stage)
            if entry.state is not ActivationCapabilityState.SEALED:
                raise StageSealError("SEALER.TOKEN_ALREADY_ISSUED")
            if current_generation != entry.stage.expected_prior_generation:
                raise StageSealError("SEALER.GENERATION_MISMATCH")
            nonce = entry.stage.activation_nonce
            if nonce in self._claimed_nonces:
                raise StageSealError("SEALER.NONCE_REPLAY")
            token = contract_module._create_activation_token(
                token_id=f"token.{uuid.uuid4().hex}",
                stage=entry.stage,
            )
            self._claimed_nonces[nonce] = entry.stage.artifact.artifact_id
            self._tokens[token.token_id] = (
                entry.stage.artifact.artifact_id,
                token,
            )
            self._entries[entry.stage.artifact.artifact_id] = replace(
                entry,
                state=ActivationCapabilityState.TOKEN_ISSUED,
                token=token,
            )
            return token

    def _token_entry(
        self,
        token: contract_module._ActivationToken,
    ) -> _RegistryEntry:
        if type(token) is not contract_module._ActivationToken:
            raise StageSealError("SEALER.TOKEN_INVALID")
        registered = self._tokens.get(token.token_id)
        if registered is None or registered[1] is not token:
            raise StageSealError("SEALER.TOKEN_INVALID")
        entry = self._entries.get(registered[0])
        if entry is None or entry.token is not token:
            raise StageSealError("SEALER.TOKEN_INVALID")
        try:
            contract_module._validate_activation_token_for_stage(
                token,
                entry.stage,
            )
        except (TypeError, ValueError) as error:
            raise StageSealError("SEALER.TOKEN_INVALID") from error
        return entry

    def consume(self, token: contract_module._ActivationToken) -> None:
        with self._lock:
            entry = self._token_entry(token)
            if entry.state is not ActivationCapabilityState.TOKEN_ISSUED:
                raise StageSealError("SEALER.TOKEN_NOT_ACTIVE")
            self._entries[entry.stage.artifact.artifact_id] = replace(
                entry,
                state=ActivationCapabilityState.CONSUMED,
            )

    def cancel(self, token: contract_module._ActivationToken) -> None:
        with self._lock:
            entry = self._token_entry(token)
            if entry.state is not ActivationCapabilityState.TOKEN_ISSUED:
                raise StageSealError("SEALER.TOKEN_NOT_ACTIVE")
            self._entries[entry.stage.artifact.artifact_id] = replace(
                entry,
                state=ActivationCapabilityState.CANCELLED,
            )


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
        for seam_name in ("reserve", "commit", "release"):
            if not callable(getattr(registry, seam_name, None)):
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

    def _lifecycle_registry(self) -> _SealLifecycleRegistry:
        return cast(_SealLifecycleRegistry, self._registry)

    def seal(
        self,
        mutable_stage: MutableStageRef,
        *,
        expected_prior_generation: int | None = None,
        schema_upgrade: bool = False,
    ) -> SealedStage:
        """Seal one complete migration stage into one opaque artifact.

        Ordering is validate (capturing a closure digest), fsync content,
        reserve the artifact in the registry, then recheck the closure under
        the write lock before writing the SEALED marker.  The marker commit
        is itself fsynced (DB and parent) before any digest work or registry
        publication, so registration only ever follows the final fsynced
        sealed state.  Any post-marker failure durably restores UNPUBLISHED
        and releases the reservation, so the same completed stage
        deterministically retries without rebuilding or duplicating records.
        A path swap during the seal denies and never mints an entry.

        The private Task 5.11 ``schema_upgrade`` mode seals a marker-bearing
        v2 candidate through the upgrade fact validation; the marker and
        the explicit mode must agree.
        """

        if type(schema_upgrade) is not bool:
            raise StageSealError("SEALER.TYPE_INVALID")
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
        try:
            _require_stage_sealed_marker(stage.staged_db_path)
        except (StageSealError, sqlite3.DatabaseError):
            pass
        else:
            raise StageSealError("SEALER.STAGE_INVALID")
        lifecycle = self._lifecycle_registry()
        reservation = lifecycle.reserve(
            stage,
            database_identity=database_identity,
            manifest_identity=manifest_identity,
        )
        marker_written = False
        try:
            facts = _validate_stage_facts(
                stage,
                canonical_store_id=self._canonical_store_id,
                schema_upgrade=schema_upgrade,
                seal_stage=True,
            )
            marker_written = True
            try:
                database_proof = _capture_content_file(
                    stage.staged_db_path
                )
                manifest_proof = _capture_content_file(
                    stage.manifest_temp_path
                )
                source_proof = _capture_content_file(
                    stage.resource_identity.configured_jsonl_path
                )
            except ContentAttestationError as error:
                raise StageSealError("SEALER.ATTESTATION_FAILED") from error
            _fsync_stage_assets(stage, database_identity, manifest_identity)
            try:
                _revalidate_content_file(
                    stage.staged_db_path,
                    database_proof,
                )
                _revalidate_content_file(
                    stage.manifest_temp_path,
                    manifest_proof,
                )
                _revalidate_content_file(
                    stage.resource_identity.configured_jsonl_path,
                    source_proof,
                )
            except ContentAttestationError as error:
                raise StageSealError(
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION"
                ) from error
            stage_file_digest = database_proof.sha256
            manifest_temp_digest, manifest = _verify_manifest_at_digest(
                stage.manifest_temp_path,
                manifest_identity,
                facts.receipt,
                upgrade_manifest=facts.schema_upgrade,
            )
            if (
                manifest_temp_digest != manifest_proof.sha256
                or source_proof.sha256 != facts.receipt.jsonl_digest
            ):
                raise StageSealError(
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION"
                )
            _verify_sealed_stage(
                stage,
                record_count=facts.record_count,
                origin_batch_count=facts.origin_batch_count,
                receipt_count=1,
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
            attestation = _build_sealed_content_attestation(
                stage,
                facts,
                evidence,
                generation,
                database_proof=database_proof,
                manifest_proof=manifest_proof,
                source_proof=source_proof,
            )
            return lifecycle.commit(
                reservation,
                evidence,
                generation,
                attestation,
            )
        except BaseException as error:
            try:
                if marker_written:
                    _restore_stage_unpublished(
                        stage.staged_db_path,
                        database_identity,
                    )
            finally:
                lifecycle.release(reservation)
            if isinstance(error, StageSealError):
                raise
            raise StageSealError("SEALER.STAGE_INVALID") from error


__all__ = ["SealedArtifactRegistry", "StageSealError", "StageSealer"]
