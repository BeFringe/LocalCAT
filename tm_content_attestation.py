"""Strict durable content attestations for sealed and active TM artifacts.

This module is deliberately a leaf: it knows only filesystem and scalar
content facts.  Stage sealing, activation, and recovery owners derive the
facts from their own SQLite/manifest/source reads and use this module only to
freeze, encode, decode, and re-prove the exact files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping, TypedDict


CONTENT_ATTESTATION_VERSION = "tm-content-attestation-v2"
LOGICAL_CLOSURE_VERSION = "tm-logical-closure-v2"
SEALED_CONTENT_PHASE = "SEALED"
ACTIVE_CONTENT_PHASE = "ACTIVE"

_NATIVE_PATH_TYPE = type(Path())
_READ_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ContentAttestationError(RuntimeError):
    """Stable code-only failure raised by filesystem re-proofs."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


def _require_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty built-in string")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_int(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a built-in integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in boolean")
    return value


def _require_optional_generation(value: object) -> int | None:
    if value is None:
        return None
    return _require_int(value, "expected_prior_generation")


def _stable_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ContentFileProof:
    """One no-follow regular/single-link file identity and complete hash."""

    device: int
    inode: int
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _require_int(self.device, "device")
        _require_int(self.inode, "inode", minimum=1)
        _require_int(self.size, "size")
        _require_digest(self.sha256, "sha256")


_CONTENT_FILE_PROOF_FIELDS = frozenset(
    {"device", "inode", "sha256", "size"}
)


def _content_file_proof_to_mapping(
    proof: ContentFileProof,
) -> dict[str, object]:
    if type(proof) is not ContentFileProof:
        raise TypeError("proof must be exact ContentFileProof")
    return {
        "device": proof.device,
        "inode": proof.inode,
        "sha256": proof.sha256,
        "size": proof.size,
    }


def _content_file_proof_from_mapping(
    mapping: object,
) -> ContentFileProof:
    if type(mapping) is not dict:
        raise TypeError("file proof must be an exact object")
    values: dict[object, object] = mapping
    if set(values) != _CONTENT_FILE_PROOF_FIELDS:
        raise ValueError("file proof fields do not match the strict codec")
    return ContentFileProof(
        device=_require_int(values["device"], "device"),
        inode=_require_int(values["inode"], "inode", minimum=1),
        size=_require_int(values["size"], "size"),
        sha256=_require_digest(values["sha256"], "sha256"),
    )


@dataclass(frozen=True)
class ContentSemanticFacts:
    """Complete semantic closure produced by an owning SQLite validator."""

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
    receipt_boundary_record_count: int
    origin_batch_count: int
    origin_batch_id: str
    origin_batch_kind: str
    exported_revision: int
    fts_count: int
    receipt_boundary_fts_count: int
    gram_counts: tuple[tuple[int, int], ...]
    exact_parity_digest: str
    logical_closure_version: str
    logical_closure_digest: str

    def __post_init__(self) -> None:
        _require_int(self.schema_version, "schema_version", minimum=1)
        _require_digest(self.schema_digest, "schema_digest")
        _require_string(self.fold_version, "fold_version")
        _require_string(self.index_version, "index_version")
        _require_string(self.candidate_index_kind, "candidate_index_kind")
        _require_bool(self.fts5_available, "fts5_available")
        _require_string(self.sqlite_runtime_version, "sqlite_runtime_version")
        _require_string(self.unicode_runtime_version, "unicode_runtime_version")
        _require_string(self.journal_mode, "journal_mode")
        _require_string(self.synchronous, "synchronous")
        _require_bool(self.foreign_keys, "foreign_keys")
        _require_int(self.busy_timeout_ms, "busy_timeout_ms")
        _require_bool(self.wal_enabled, "wal_enabled")
        _require_bool(
            self.extension_loading_enabled,
            "extension_loading_enabled",
        )
        record_count = _require_int(self.record_count, "record_count")
        boundary_record_count = _require_int(
            self.receipt_boundary_record_count,
            "receipt_boundary_record_count",
        )
        if boundary_record_count > record_count:
            raise ValueError(
                "receipt_boundary_record_count cannot exceed record_count"
            )
        _require_int(self.origin_batch_count, "origin_batch_count")
        _require_string(self.origin_batch_id, "origin_batch_id")
        origin_batch_kind = _require_string(
            self.origin_batch_kind,
            "origin_batch_kind",
        )
        if origin_batch_kind not in {"migration", "import", "schema_upgrade"}:
            raise ValueError("origin_batch_kind is not supported")
        _require_int(self.exported_revision, "exported_revision")
        fts_count = _require_int(self.fts_count, "fts_count")
        boundary_fts_count = _require_int(
            self.receipt_boundary_fts_count,
            "receipt_boundary_fts_count",
        )
        if fts_count > record_count:
            raise ValueError("fts_count cannot exceed record_count")
        if boundary_fts_count > boundary_record_count:
            raise ValueError(
                "receipt_boundary_fts_count cannot exceed "
                "receipt_boundary_record_count"
            )
        if boundary_fts_count > fts_count:
            raise ValueError(
                "receipt_boundary_fts_count cannot exceed fts_count"
            )
        if not self.fts5_available and (
            fts_count != 0 or boundary_fts_count != 0
        ):
            raise ValueError("fallback semantic facts cannot carry FTS rows")
        if type(self.gram_counts) is not tuple:
            raise TypeError("gram_counts must be an exact tuple")
        sizes: list[int] = []
        for pair in self.gram_counts:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError("gram_counts must contain exact integer pairs")
            size = _require_int(pair[0], "gram_size", minimum=1)
            _require_int(pair[1], "gram_count")
            sizes.append(size)
        if sizes != sorted(set(sizes)):
            raise ValueError("gram_counts must have unique ordered sizes")
        _require_digest(self.exact_parity_digest, "exact_parity_digest")
        if self.logical_closure_version != LOGICAL_CLOSURE_VERSION:
            raise ValueError("unsupported logical closure version")
        _require_digest(self.logical_closure_digest, "logical_closure_digest")


_SEMANTIC_FIELDS = frozenset(
    {
        "busy_timeout_ms",
        "candidate_index_kind",
        "exact_parity_digest",
        "exported_revision",
        "extension_loading_enabled",
        "fold_version",
        "foreign_keys",
        "fts5_available",
        "fts_count",
        "gram_counts",
        "index_version",
        "journal_mode",
        "logical_closure_digest",
        "logical_closure_version",
        "origin_batch_count",
        "origin_batch_id",
        "origin_batch_kind",
        "record_count",
        "receipt_boundary_fts_count",
        "receipt_boundary_record_count",
        "schema_digest",
        "schema_version",
        "sqlite_runtime_version",
        "synchronous",
        "unicode_runtime_version",
        "wal_enabled",
    }
)


def _semantic_facts_to_mapping(
    facts: ContentSemanticFacts,
) -> dict[str, object]:
    if type(facts) is not ContentSemanticFacts:
        raise TypeError("facts must be exact ContentSemanticFacts")
    return {
        "busy_timeout_ms": facts.busy_timeout_ms,
        "candidate_index_kind": facts.candidate_index_kind,
        "exact_parity_digest": facts.exact_parity_digest,
        "exported_revision": facts.exported_revision,
        "extension_loading_enabled": facts.extension_loading_enabled,
        "fold_version": facts.fold_version,
        "foreign_keys": facts.foreign_keys,
        "fts5_available": facts.fts5_available,
        "fts_count": facts.fts_count,
        "gram_counts": [list(pair) for pair in facts.gram_counts],
        "index_version": facts.index_version,
        "journal_mode": facts.journal_mode,
        "logical_closure_digest": facts.logical_closure_digest,
        "logical_closure_version": facts.logical_closure_version,
        "origin_batch_count": facts.origin_batch_count,
        "origin_batch_id": facts.origin_batch_id,
        "origin_batch_kind": facts.origin_batch_kind,
        "record_count": facts.record_count,
        "receipt_boundary_fts_count": facts.receipt_boundary_fts_count,
        "receipt_boundary_record_count": (
            facts.receipt_boundary_record_count
        ),
        "schema_digest": facts.schema_digest,
        "schema_version": facts.schema_version,
        "sqlite_runtime_version": facts.sqlite_runtime_version,
        "synchronous": facts.synchronous,
        "unicode_runtime_version": facts.unicode_runtime_version,
        "wal_enabled": facts.wal_enabled,
    }


def _semantic_facts_from_mapping(mapping: object) -> ContentSemanticFacts:
    if type(mapping) is not dict:
        raise TypeError("semantic facts must be an exact object")
    values: dict[object, object] = mapping
    if set(values) != _SEMANTIC_FIELDS:
        raise ValueError("semantic fact fields do not match the strict codec")
    encoded_counts = values["gram_counts"]
    if type(encoded_counts) is not list:
        raise TypeError("gram_counts must be an exact array")
    gram_counts: list[tuple[int, int]] = []
    for item in encoded_counts:
        if type(item) is not list or len(item) != 2:
            raise TypeError("gram_counts must contain two-item arrays")
        gram_counts.append(
            (
                _require_int(item[0], "gram_size", minimum=1),
                _require_int(item[1], "gram_count"),
            )
        )
    return ContentSemanticFacts(
        schema_version=_require_int(
            values["schema_version"], "schema_version", minimum=1
        ),
        schema_digest=_require_digest(values["schema_digest"], "schema_digest"),
        fold_version=_require_string(values["fold_version"], "fold_version"),
        index_version=_require_string(values["index_version"], "index_version"),
        candidate_index_kind=_require_string(
            values["candidate_index_kind"], "candidate_index_kind"
        ),
        fts5_available=_require_bool(values["fts5_available"], "fts5_available"),
        sqlite_runtime_version=_require_string(
            values["sqlite_runtime_version"], "sqlite_runtime_version"
        ),
        unicode_runtime_version=_require_string(
            values["unicode_runtime_version"], "unicode_runtime_version"
        ),
        journal_mode=_require_string(values["journal_mode"], "journal_mode"),
        synchronous=_require_string(values["synchronous"], "synchronous"),
        foreign_keys=_require_bool(values["foreign_keys"], "foreign_keys"),
        busy_timeout_ms=_require_int(
            values["busy_timeout_ms"], "busy_timeout_ms"
        ),
        wal_enabled=_require_bool(values["wal_enabled"], "wal_enabled"),
        extension_loading_enabled=_require_bool(
            values["extension_loading_enabled"], "extension_loading_enabled"
        ),
        record_count=_require_int(values["record_count"], "record_count"),
        receipt_boundary_record_count=_require_int(
            values["receipt_boundary_record_count"],
            "receipt_boundary_record_count",
        ),
        origin_batch_count=_require_int(
            values["origin_batch_count"], "origin_batch_count"
        ),
        origin_batch_id=_require_string(
            values["origin_batch_id"], "origin_batch_id"
        ),
        origin_batch_kind=_require_string(
            values["origin_batch_kind"], "origin_batch_kind"
        ),
        exported_revision=_require_int(
            values["exported_revision"], "exported_revision"
        ),
        fts_count=_require_int(values["fts_count"], "fts_count"),
        receipt_boundary_fts_count=_require_int(
            values["receipt_boundary_fts_count"],
            "receipt_boundary_fts_count",
        ),
        gram_counts=tuple(gram_counts),
        exact_parity_digest=_require_digest(
            values["exact_parity_digest"], "exact_parity_digest"
        ),
        logical_closure_version=_require_string(
            values["logical_closure_version"],
            "logical_closure_version",
        ),
        logical_closure_digest=_require_digest(
            values["logical_closure_digest"], "logical_closure_digest"
        ),
    )


@dataclass(frozen=True)
class SealedContentAttestation:
    attestation_version: str
    phase: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    evidence_digest: str
    database: ContentFileProof
    manifest: ContentFileProof
    source: ContentFileProof
    semantic_facts: ContentSemanticFacts
    attestation_digest: str

    def __post_init__(self) -> None:
        if self.attestation_version != CONTENT_ATTESTATION_VERSION:
            raise ValueError("unsupported sealed content attestation version")
        if self.phase != SEALED_CONTENT_PHASE:
            raise ValueError("sealed content attestation phase is invalid")
        _require_string(self.resource_id, "resource_id")
        _require_digest(self.target_identity, "target_identity")
        _require_string(self.canonical_store_id, "canonical_store_id")
        _require_digest(self.snapshot_receipt_digest, "snapshot_receipt_digest")
        _require_optional_generation(self.expected_prior_generation)
        _require_digest(self.evidence_digest, "evidence_digest")
        for value in (self.database, self.manifest, self.source):
            if type(value) is not ContentFileProof:
                raise TypeError("attestation files must be exact ContentFileProof")
        if type(self.semantic_facts) is not ContentSemanticFacts:
            raise TypeError("semantic_facts must be exact ContentSemanticFacts")
        _require_digest(self.attestation_digest, "attestation_digest")
        if self.attestation_digest != _stable_digest(
            _sealed_content_payload(self, include_digest=False)
        ):
            raise ValueError("sealed content attestation digest does not close")


class _SealedContentAttestationValues(TypedDict):
    attestation_version: str
    phase: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    evidence_digest: str
    database: ContentFileProof
    manifest: ContentFileProof
    source: ContentFileProof
    semantic_facts: ContentSemanticFacts


_SEALED_FIELDS = frozenset(
    {
        "attestation_digest",
        "attestation_version",
        "canonical_store_id",
        "database",
        "evidence_digest",
        "expected_prior_generation",
        "manifest",
        "phase",
        "resource_id",
        "semantic_facts",
        "snapshot_receipt_digest",
        "source",
        "target_identity",
    }
)


def _sealed_content_payload(
    attestation: SealedContentAttestation,
    *,
    include_digest: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "attestation_version": attestation.attestation_version,
        "canonical_store_id": attestation.canonical_store_id,
        "database": _content_file_proof_to_mapping(attestation.database),
        "evidence_digest": attestation.evidence_digest,
        "expected_prior_generation": attestation.expected_prior_generation,
        "manifest": _content_file_proof_to_mapping(attestation.manifest),
        "phase": attestation.phase,
        "resource_id": attestation.resource_id,
        "semantic_facts": _semantic_facts_to_mapping(
            attestation.semantic_facts
        ),
        "snapshot_receipt_digest": attestation.snapshot_receipt_digest,
        "source": _content_file_proof_to_mapping(attestation.source),
        "target_identity": attestation.target_identity,
    }
    if include_digest:
        payload["attestation_digest"] = attestation.attestation_digest
    return payload


def _create_sealed_content_attestation(
    *,
    resource_id: str,
    target_identity: str,
    canonical_store_id: str,
    snapshot_receipt_digest: str,
    expected_prior_generation: int | None,
    evidence_digest: str,
    database: ContentFileProof,
    manifest: ContentFileProof,
    source: ContentFileProof,
    semantic_facts: ContentSemanticFacts,
) -> SealedContentAttestation:
    values: _SealedContentAttestationValues = {
        "attestation_version": CONTENT_ATTESTATION_VERSION,
        "phase": SEALED_CONTENT_PHASE,
        "resource_id": resource_id,
        "target_identity": target_identity,
        "canonical_store_id": canonical_store_id,
        "snapshot_receipt_digest": snapshot_receipt_digest,
        "expected_prior_generation": expected_prior_generation,
        "evidence_digest": evidence_digest,
        "database": database,
        "manifest": manifest,
        "source": source,
        "semantic_facts": semantic_facts,
    }
    provisional = object.__new__(SealedContentAttestation)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    digest = _stable_digest(_sealed_content_payload(provisional, include_digest=False))
    return SealedContentAttestation(**values, attestation_digest=digest)


def _sealed_content_attestation_to_mapping(
    attestation: SealedContentAttestation,
) -> dict[str, object]:
    if type(attestation) is not SealedContentAttestation:
        raise TypeError("attestation must be exact SealedContentAttestation")
    return _sealed_content_payload(attestation, include_digest=True)


def _sealed_content_attestation_from_mapping(
    mapping: object,
) -> SealedContentAttestation:
    if type(mapping) is not dict:
        raise TypeError("sealed content attestation must be an exact object")
    values: dict[object, object] = mapping
    if set(values) != _SEALED_FIELDS:
        raise ValueError("sealed attestation fields do not match strict codec")
    return SealedContentAttestation(
        attestation_version=_require_string(
            values["attestation_version"], "attestation_version"
        ),
        phase=_require_string(values["phase"], "phase"),
        resource_id=_require_string(values["resource_id"], "resource_id"),
        target_identity=_require_digest(
            values["target_identity"], "target_identity"
        ),
        canonical_store_id=_require_string(
            values["canonical_store_id"], "canonical_store_id"
        ),
        snapshot_receipt_digest=_require_digest(
            values["snapshot_receipt_digest"], "snapshot_receipt_digest"
        ),
        expected_prior_generation=_require_optional_generation(
            values["expected_prior_generation"]
        ),
        evidence_digest=_require_digest(
            values["evidence_digest"], "evidence_digest"
        ),
        database=_content_file_proof_from_mapping(values["database"]),
        manifest=_content_file_proof_from_mapping(values["manifest"]),
        source=_content_file_proof_from_mapping(values["source"]),
        semantic_facts=_semantic_facts_from_mapping(values["semantic_facts"]),
        attestation_digest=_require_digest(
            values["attestation_digest"], "attestation_digest"
        ),
    )


@dataclass(frozen=True)
class ActiveContentAttestation:
    attestation_version: str
    phase: str
    attested_journal_phase: str
    sealed_attestation_digest: str
    journal_id: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    generation: int
    activation_digest: str
    database: ContentFileProof
    manifest: ContentFileProof
    source: ContentFileProof
    semantic_facts: ContentSemanticFacts
    attestation_digest: str

    def __post_init__(self) -> None:
        if self.attestation_version != CONTENT_ATTESTATION_VERSION:
            raise ValueError("unsupported active content attestation version")
        if self.phase != ACTIVE_CONTENT_PHASE:
            raise ValueError("active content attestation phase is invalid")
        if self.attested_journal_phase != "MANIFEST_PUBLISHED":
            raise ValueError("active attestation journal phase is invalid")
        _require_digest(self.sealed_attestation_digest, "sealed_attestation_digest")
        _require_string(self.journal_id, "journal_id")
        _require_string(self.resource_id, "resource_id")
        _require_digest(self.target_identity, "target_identity")
        _require_string(self.canonical_store_id, "canonical_store_id")
        _require_digest(self.snapshot_receipt_digest, "snapshot_receipt_digest")
        _require_int(self.generation, "generation")
        _require_digest(self.activation_digest, "activation_digest")
        for value in (self.database, self.manifest, self.source):
            if type(value) is not ContentFileProof:
                raise TypeError("attestation files must be exact ContentFileProof")
        if type(self.semantic_facts) is not ContentSemanticFacts:
            raise TypeError("semantic_facts must be exact ContentSemanticFacts")
        _require_digest(self.attestation_digest, "attestation_digest")
        if self.attestation_digest != _stable_digest(
            _active_content_payload(self, include_digest=False)
        ):
            raise ValueError("active content attestation digest does not close")


class _ActiveContentAttestationValues(TypedDict):
    attestation_version: str
    phase: str
    attested_journal_phase: str
    sealed_attestation_digest: str
    journal_id: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    generation: int
    activation_digest: str
    database: ContentFileProof
    manifest: ContentFileProof
    source: ContentFileProof
    semantic_facts: ContentSemanticFacts


_ACTIVE_FIELDS = frozenset(
    {
        "activation_digest",
        "attestation_digest",
        "attestation_version",
        "attested_journal_phase",
        "canonical_store_id",
        "database",
        "generation",
        "journal_id",
        "manifest",
        "phase",
        "resource_id",
        "sealed_attestation_digest",
        "semantic_facts",
        "snapshot_receipt_digest",
        "source",
        "target_identity",
    }
)


def _active_content_payload(
    attestation: ActiveContentAttestation,
    *,
    include_digest: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "activation_digest": attestation.activation_digest,
        "attestation_version": attestation.attestation_version,
        "attested_journal_phase": attestation.attested_journal_phase,
        "canonical_store_id": attestation.canonical_store_id,
        "database": _content_file_proof_to_mapping(attestation.database),
        "generation": attestation.generation,
        "journal_id": attestation.journal_id,
        "manifest": _content_file_proof_to_mapping(attestation.manifest),
        "phase": attestation.phase,
        "resource_id": attestation.resource_id,
        "sealed_attestation_digest": attestation.sealed_attestation_digest,
        "semantic_facts": _semantic_facts_to_mapping(attestation.semantic_facts),
        "snapshot_receipt_digest": attestation.snapshot_receipt_digest,
        "source": _content_file_proof_to_mapping(attestation.source),
        "target_identity": attestation.target_identity,
    }
    if include_digest:
        payload["attestation_digest"] = attestation.attestation_digest
    return payload


def _create_active_content_attestation(
    *,
    sealed_attestation_digest: str,
    journal_id: str,
    resource_id: str,
    target_identity: str,
    canonical_store_id: str,
    snapshot_receipt_digest: str,
    generation: int,
    activation_digest: str,
    database: ContentFileProof,
    manifest: ContentFileProof,
    source: ContentFileProof,
    semantic_facts: ContentSemanticFacts,
) -> ActiveContentAttestation:
    values: _ActiveContentAttestationValues = {
        "attestation_version": CONTENT_ATTESTATION_VERSION,
        "phase": ACTIVE_CONTENT_PHASE,
        "attested_journal_phase": "MANIFEST_PUBLISHED",
        "sealed_attestation_digest": sealed_attestation_digest,
        "journal_id": journal_id,
        "resource_id": resource_id,
        "target_identity": target_identity,
        "canonical_store_id": canonical_store_id,
        "snapshot_receipt_digest": snapshot_receipt_digest,
        "generation": generation,
        "activation_digest": activation_digest,
        "database": database,
        "manifest": manifest,
        "source": source,
        "semantic_facts": semantic_facts,
    }
    provisional = object.__new__(ActiveContentAttestation)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    digest = _stable_digest(_active_content_payload(provisional, include_digest=False))
    return ActiveContentAttestation(**values, attestation_digest=digest)


def _active_content_attestation_to_mapping(
    attestation: ActiveContentAttestation,
) -> dict[str, object]:
    if type(attestation) is not ActiveContentAttestation:
        raise TypeError("attestation must be exact ActiveContentAttestation")
    return _active_content_payload(attestation, include_digest=True)


def _active_content_attestation_from_mapping(
    mapping: object,
) -> ActiveContentAttestation:
    if type(mapping) is not dict:
        raise TypeError("active content attestation must be an exact object")
    values: dict[object, object] = mapping
    if set(values) != _ACTIVE_FIELDS:
        raise ValueError("active attestation fields do not match strict codec")
    return ActiveContentAttestation(
        attestation_version=_require_string(
            values["attestation_version"], "attestation_version"
        ),
        phase=_require_string(values["phase"], "phase"),
        attested_journal_phase=_require_string(
            values["attested_journal_phase"], "attested_journal_phase"
        ),
        sealed_attestation_digest=_require_digest(
            values["sealed_attestation_digest"], "sealed_attestation_digest"
        ),
        journal_id=_require_string(values["journal_id"], "journal_id"),
        resource_id=_require_string(values["resource_id"], "resource_id"),
        target_identity=_require_digest(
            values["target_identity"], "target_identity"
        ),
        canonical_store_id=_require_string(
            values["canonical_store_id"], "canonical_store_id"
        ),
        snapshot_receipt_digest=_require_digest(
            values["snapshot_receipt_digest"], "snapshot_receipt_digest"
        ),
        generation=_require_int(values["generation"], "generation"),
        activation_digest=_require_digest(
            values["activation_digest"], "activation_digest"
        ),
        database=_content_file_proof_from_mapping(values["database"]),
        manifest=_content_file_proof_from_mapping(values["manifest"]),
        source=_content_file_proof_from_mapping(values["source"]),
        semantic_facts=_semantic_facts_from_mapping(values["semantic_facts"]),
        attestation_digest=_require_digest(
            values["attestation_digest"], "attestation_digest"
        ),
    )


def _normalized_absolute_path(path: Path) -> Path:
    if type(path) is not _NATIVE_PATH_TYPE:
        raise TypeError("path must be an exact native Path")
    if not path.is_absolute() or ".." in path.parts:
        raise ContentAttestationError("CONTENT_ATTESTATION.PATH_INVALID")
    normalized = Path(os.path.normpath(str(path)))
    if normalized != path:
        raise ContentAttestationError("CONTENT_ATTESTATION.PATH_INVALID")
    return path


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _safe_directory(result: os.stat_result) -> bool:
    return stat.S_ISDIR(result.st_mode) and result.st_nlink >= 1


def _safe_regular(result: os.stat_result) -> bool:
    return stat.S_ISREG(result.st_mode) and result.st_nlink == 1


def _capture_content_file(path: Path) -> ContentFileProof:
    """Hash one exact file through an anchored, no-follow parent chain.

    The proof requires every root-to-parent directory entry and the final
    regular/single-link file entry to retain the same identity before and
    after the complete SHA-256 read.  Content metadata must also remain
    unchanged during the read, preventing an in-place concurrent mutation
    from producing an authorization proof for a transient byte stream.
    """

    exact_path = _normalized_absolute_path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0) | nofollow
    file_flags = flags | nofollow | getattr(os, "O_NONBLOCK", 0)
    directory_fds: list[int] = []
    directory_stats: list[os.stat_result] = []
    descriptor = -1
    try:
        root_fd = os.open(os.path.sep, directory_flags)
        directory_fds.append(root_fd)
        root_stat = os.fstat(root_fd)
        if not _safe_directory(root_stat):
            raise ContentAttestationError(
                "CONTENT_ATTESTATION.PARENT_UNSAFE"
            )
        directory_stats.append(root_stat)
        for component in exact_path.parts[1:-1]:
            try:
                before = os.stat(
                    component,
                    dir_fd=directory_fds[-1],
                    follow_symlinks=False,
                )
                if not _safe_directory(before):
                    raise ContentAttestationError(
                        "CONTENT_ATTESTATION.PARENT_UNSAFE"
                    )
                child_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fds[-1],
                )
                opened = os.fstat(child_fd)
            except ContentAttestationError:
                raise
            except OSError as error:
                raise ContentAttestationError(
                    "CONTENT_ATTESTATION.PARENT_UNSAFE"
                ) from error
            if not _safe_directory(opened) or not _same_identity(before, opened):
                os.close(child_fd)
                raise ContentAttestationError(
                    "CONTENT_ATTESTATION.PARENT_UNSAFE"
                )
            directory_fds.append(child_fd)
            directory_stats.append(opened)

        name = exact_path.name
        if not name:
            raise ContentAttestationError("CONTENT_ATTESTATION.PATH_INVALID")
        try:
            before_file = os.stat(
                name,
                dir_fd=directory_fds[-1],
                follow_symlinks=False,
            )
            if not _safe_regular(before_file):
                raise ContentAttestationError(
                    "CONTENT_ATTESTATION.FILE_UNSAFE"
                )
            descriptor = os.open(name, file_flags, dir_fd=directory_fds[-1])
            opened_file = os.fstat(descriptor)
        except ContentAttestationError:
            raise
        except FileNotFoundError as error:
            raise ContentAttestationError(
                "CONTENT_ATTESTATION.FILE_MISSING"
            ) from error
        except OSError as error:
            raise ContentAttestationError(
                "CONTENT_ATTESTATION.FILE_UNSAFE"
            ) from error
        if not _safe_regular(opened_file) or not _same_identity(
            before_file, opened_file
        ):
            raise ContentAttestationError("CONTENT_ATTESTATION.FILE_UNSAFE")

        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after_file = os.fstat(descriptor)
        final_entry = os.stat(
            name,
            dir_fd=directory_fds[-1],
            follow_symlinks=False,
        )
        stable_metadata = (
            opened_file.st_size,
            opened_file.st_mtime_ns,
            opened_file.st_ctime_ns,
        ) == (
            after_file.st_size,
            after_file.st_mtime_ns,
            after_file.st_ctime_ns,
        )
        if (
            not stable_metadata
            or not _safe_regular(after_file)
            or not _safe_regular(final_entry)
            or not _same_identity(opened_file, after_file)
            or not _same_identity(opened_file, final_entry)
        ):
            raise ContentAttestationError(
                "CONTENT_ATTESTATION.FILE_MUTATED"
            )
        for index, expected in enumerate(directory_stats):
            observed = os.fstat(directory_fds[index])
            if not _safe_directory(observed) or not _same_identity(
                expected, observed
            ):
                raise ContentAttestationError(
                    "CONTENT_ATTESTATION.PARENT_MUTATED"
                )
            if index > 0:
                rebound = os.stat(
                    exact_path.parts[index],
                    dir_fd=directory_fds[index - 1],
                    follow_symlinks=False,
                )
                if not _safe_directory(rebound) or not _same_identity(
                    expected, rebound
                ):
                    raise ContentAttestationError(
                        "CONTENT_ATTESTATION.PARENT_MUTATED"
                    )
        return ContentFileProof(
            device=after_file.st_dev,
            inode=after_file.st_ino,
            size=after_file.st_size,
            sha256=digest.hexdigest(),
        )
    except ContentAttestationError:
        raise
    except OSError as error:
        raise ContentAttestationError(
            "CONTENT_ATTESTATION.FILE_UNSAFE"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _revalidate_content_file(
    path: Path,
    expected: ContentFileProof,
) -> ContentFileProof:
    if type(expected) is not ContentFileProof:
        raise TypeError("expected must be exact ContentFileProof")
    observed = _capture_content_file(path)
    if observed != expected:
        raise ContentAttestationError(
            "CONTENT_ATTESTATION.CONTENT_MISMATCH"
        )
    return observed


__all__ = [
    "ACTIVE_CONTENT_PHASE",
    "CONTENT_ATTESTATION_VERSION",
    "LOGICAL_CLOSURE_VERSION",
    "SEALED_CONTENT_PHASE",
    "ActiveContentAttestation",
    "ContentAttestationError",
    "ContentFileProof",
    "ContentSemanticFacts",
    "SealedContentAttestation",
]
