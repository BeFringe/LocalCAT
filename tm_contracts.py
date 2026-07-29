"""Versioned, immutable core contracts for local translation memory.

The contracts in this module intentionally have no storage, parser, matcher, or
UI dependency.  Raw bilingual text is kept only in record/query/result shapes;
resource-local failures expose diagnostic identifiers rather than arbitrary
exception text.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Protocol


TM_CONTRACT_CODEC_VERSION = 1
SCORER_VERSION_V1 = "scorer-v1"
CANONICAL_RESOURCE_IDENTITY_VERSION = "canonical-resource-v1"
SNAPSHOT_FORMAT_VERSION = "localcat-jsonl-v1"
SNAPSHOT_MANIFEST_VERSION = "snapshot-manifest-v1"
SNAPSHOT_BINDING_VERSION = "snapshot-binding-v1"
STAGE_VALIDATION_EVIDENCE_VERSION = "stage-validation-v1"
GENERATION_EXPECTATION_VERSION = "generation-expectation-v1"
ACTIVATION_TOKEN_VERSION = "activation-token-v1"

_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_CAPABILITY_FACTORY_KEY = object()
_CONTEXT_FIELDS = (
    "speaker_raw",
    "context_prev_raw",
    "context_next_raw",
)


class TMMatchType(str, Enum):
    """Stable public TM match categories."""

    EXACT = "EXACT"
    CONTEXT = "CONTEXT"
    FUZZY = "FUZZY"


def _require_tuple(value: object, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    return value


def _require_bool(value: object, field_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")


def _require_int(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_raw_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


def _require_optional_text(value: object, field_name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")


def _require_identity(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_ratio(value: object, field_name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")


def _require_string_tuple(
    value: object,
    field_name: str,
    *,
    identities: bool = False,
) -> tuple[str, ...]:
    items = _require_tuple(value, field_name)
    for item in items:
        if identities:
            _require_identity(item, f"{field_name} item")
        elif not isinstance(item, str):
            raise TypeError(f"{field_name} items must be strings")
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} items must be unique")
    return items


def _require_provenance(value: object) -> None:
    pairs = _require_tuple(value, "provenance")
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError("provenance entries must be two-item tuples")
        key, item_value = pair
        _require_identity(key, "provenance key")
        if not isinstance(item_value, str):
            raise TypeError("provenance values must be strings")


def _require_diagnostic_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if len(value) > 96 or _DIAGNOSTIC_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a safe diagnostic identifier, not message text"
        )


def _require_digest(value: object, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_absolute_path(value: object, field_name: str) -> None:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{field_name} must be an absolute normalized path")


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
class TMRecord:
    """One canonical TM record with raw text, origin, and provenance."""

    record_id: int
    source_raw: str
    target_raw: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    file_source: str | None
    provenance: tuple[tuple[str, str], ...]
    legacy_line_no: int | None
    origin_batch_id: str
    origin_ordinal: int

    def __post_init__(self) -> None:
        _require_int(self.record_id, "record id", minimum=1)
        _require_raw_text(self.source_raw, "record source_raw")
        _require_raw_text(self.target_raw, "record target_raw")
        _require_optional_text(self.speaker_raw, "record speaker_raw")
        _require_optional_text(self.context_prev_raw, "record context_prev_raw")
        _require_optional_text(self.context_next_raw, "record context_next_raw")
        _require_optional_text(self.file_source, "record file_source")
        _require_provenance(self.provenance)
        if self.legacy_line_no is not None:
            _require_int(self.legacy_line_no, "legacy line number", minimum=1)
        _require_identity(self.origin_batch_id, "origin batch id")
        _require_int(self.origin_ordinal, "origin ordinal", minimum=0)


@dataclass(frozen=True)
class TMRecordDraft:
    """A record body accepted by a future canonical store append port."""

    source_raw: str
    target_raw: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    file_source: str | None
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_raw_text(self.source_raw, "record draft source_raw")
        _require_raw_text(self.target_raw, "record draft target_raw")
        _require_optional_text(self.speaker_raw, "record draft speaker_raw")
        _require_optional_text(
            self.context_prev_raw,
            "record draft context_prev_raw",
        )
        _require_optional_text(
            self.context_next_raw,
            "record draft context_next_raw",
        )
        _require_optional_text(self.file_source, "record draft file_source")
        _require_provenance(self.provenance)


class TMStore(Protocol):
    """Minimum runtime port required by a resource handle."""

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]: ...

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]: ...

    def append(self, draft: TMRecordDraft) -> TMRecord: ...

    def export_records(self) -> Iterator[TMRecord]: ...

    def health(self) -> Any: ...


@dataclass(frozen=True)
class TMResourceHandle:
    """One resource selection bound to its required runtime store."""

    resource_id: str
    store: TMStore
    active: bool
    lookup: bool
    update: bool
    order: int

    def __post_init__(self) -> None:
        _require_identity(self.resource_id, "resource id")
        if self.store is None:
            raise ValueError("resource store binding must not be None")
        for method_name in (
            "exact_records",
            "records_by_id",
            "append",
            "export_records",
            "health",
        ):
            if not callable(getattr(self.store, method_name, None)):
                raise TypeError(
                    "resource store binding does not implement TMStore"
                )
        _require_bool(self.active, "resource active")
        _require_bool(self.lookup, "resource lookup")
        _require_bool(self.update, "resource update")
        _require_int(self.order, "resource order", minimum=0)


def validate_resource_handles(
    handles: tuple[TMResourceHandle, ...],
) -> None:
    """Validate the identities and caller order of a resource collection."""

    items = _require_tuple(handles, "resource handles")
    if any(not isinstance(handle, TMResourceHandle) for handle in items):
        raise TypeError("resource handles must contain TMResourceHandle values")
    resource_ids = tuple(handle.resource_id for handle in items)
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("resource ids must be unique")
    resource_orders = tuple(handle.order for handle in items)
    if len(resource_orders) != len(set(resource_orders)):
        raise ValueError("resource orders must be unique")


@dataclass(frozen=True)
class TMQuery:
    """A query and the explicit resource tie order chosen by its caller."""

    query_source: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    minimum_similarity: float
    limit: int
    resource_order: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_raw_text(self.query_source, "query source")
        _require_optional_text(self.speaker_raw, "query speaker_raw")
        _require_optional_text(self.context_prev_raw, "query context_prev_raw")
        _require_optional_text(self.context_next_raw, "query context_next_raw")
        _require_ratio(self.minimum_similarity, "minimum similarity")
        _require_int(self.limit, "query limit", minimum=1)
        _require_string_tuple(
            self.resource_order,
            "query resource_order",
            identities=True,
        )


@dataclass(frozen=True)
class SimilarityEvidence:
    """The scorer-v1 components used for one fuzzy result."""

    levenshtein_ratio: float
    dice_bigram: float
    final_similarity: float
    scorer_version: str = SCORER_VERSION_V1

    def __post_init__(self) -> None:
        _validate_similarity_evidence(self)


def _validate_similarity_evidence(
    evidence: SimilarityEvidence,
) -> None:
    _require_ratio(evidence.levenshtein_ratio, "levenshtein ratio")
    _require_ratio(evidence.dice_bigram, "dice bigram")
    _require_ratio(evidence.final_similarity, "final similarity")
    if evidence.scorer_version != SCORER_VERSION_V1:
        raise ValueError(f"scorer version must be {SCORER_VERSION_V1}")


@dataclass(frozen=True)
class ContextEvidence:
    """Raw context comparison facts for exact-source variants."""

    comparable_fields: tuple[str, ...]
    matched_fields: tuple[str, ...]
    mismatched_fields: tuple[str, ...]
    strength_v1: tuple[int, int, int, int, int]

    def __post_init__(self) -> None:
        _validate_context_evidence(self)


def _validate_context_evidence(evidence: ContextEvidence) -> None:
    """Revalidate all context invariants at each consuming boundary."""

    comparable = _require_string_tuple(
        evidence.comparable_fields,
        "comparable context fields",
        identities=True,
    )
    matched = _require_string_tuple(
        evidence.matched_fields,
        "matched context fields",
        identities=True,
    )
    mismatched = _require_string_tuple(
        evidence.mismatched_fields,
        "mismatched context fields",
        identities=True,
    )
    unsupported = set(comparable).difference(_CONTEXT_FIELDS)
    if unsupported:
        raise ValueError("unsupported context field")
    if not set(matched).issubset(comparable):
        raise ValueError("matched context fields must be comparable")
    if not set(mismatched).issubset(comparable):
        raise ValueError("mismatched context fields must be comparable")
    if set(matched).intersection(mismatched):
        raise ValueError("context fields cannot both match and mismatch")
    if set(matched).union(mismatched) != set(comparable):
        raise ValueError("each comparable context field must be classified")

    strength = _require_tuple(
        evidence.strength_v1,
        "context strength_v1",
    )
    if len(strength) != 5:
        raise ValueError("context strength_v1 must contain five integers")
    for item in strength:
        _require_int(item, "context strength component")
    if strength[0] != len(matched):
        raise ValueError("context matched count does not match evidence")
    if strength[1] != -len(mismatched):
        raise ValueError("context mismatched count does not match evidence")
    if any(flag not in (0, 1) for flag in strength[2:]):
        raise ValueError("context field match flags must be zero or one")
    expected_flags = tuple(
        int(field_name in matched)
        for field_name in _CONTEXT_FIELDS
    )
    if strength[2:] != expected_flags:
        raise ValueError(
            "context strength flags must match matched fields"
        )


@dataclass(frozen=True)
class TMResult:
    """One explainable TM suggestion, including both query and matched source."""

    resource_id: str
    record_id: int
    query_source: str
    matched_source: str
    target: str
    match_type: TMMatchType
    similarity: float
    similarity_evidence: SimilarityEvidence | None
    context_evidence: ContextEvidence
    provenance: tuple[tuple[str, str], ...]
    stable_tie_key: tuple[int, int]

    def __post_init__(self) -> None:
        _require_identity(self.resource_id, "result resource id")
        _require_int(self.record_id, "result record id", minimum=1)
        _require_raw_text(self.query_source, "result query source")
        _require_raw_text(self.matched_source, "result matched source")
        _require_raw_text(self.target, "result target")
        if not isinstance(self.match_type, TMMatchType):
            raise TypeError("result match_type must be a TMMatchType")
        _require_ratio(self.similarity, "result similarity")
        if not isinstance(self.context_evidence, ContextEvidence):
            raise TypeError("result context_evidence must be ContextEvidence")
        _validate_context_evidence(self.context_evidence)
        _require_provenance(self.provenance)

        tie_key = _require_tuple(self.stable_tie_key, "stable tie key")
        if len(tie_key) != 2:
            raise ValueError("stable tie key must contain resource order and record id")
        _require_int(tie_key[0], "stable tie resource order", minimum=0)
        _require_int(tie_key[1], "stable tie record id", minimum=1)
        if tie_key[1] != self.record_id:
            raise ValueError("stable tie record id must equal result record id")

        if self.match_type is TMMatchType.FUZZY:
            similarity_evidence = self.similarity_evidence
            if similarity_evidence is None:
                raise ValueError("fuzzy result must include similarity evidence")
            if not isinstance(similarity_evidence, SimilarityEvidence):
                raise TypeError(
                    "fuzzy result similarity_evidence must be SimilarityEvidence"
                )
            _validate_similarity_evidence(similarity_evidence)
            if self.similarity != similarity_evidence.final_similarity:
                raise ValueError(
                    "fuzzy result similarity must equal evidence final similarity"
                )
            if self.query_source == self.matched_source:
                raise ValueError(
                    "fuzzy result must expose a distinct matched source"
                )
        else:
            if self.similarity_evidence is not None:
                raise ValueError(
                    f"{self.match_type.value} result must not include scorer evidence"
                )
            if self.similarity != 1.0:
                raise ValueError(
                    f"{self.match_type.value} result similarity must be 1.0"
                )
            if self.query_source != self.matched_source:
                raise ValueError(
                    f"{self.match_type.value} result sources must be identical"
                )
            if (
                self.match_type is TMMatchType.CONTEXT
                and not self.context_evidence.matched_fields
            ):
                raise ValueError(
                    "CONTEXT result must include positive context evidence"
                )


@dataclass(frozen=True)
class ResourceQueryFailure:
    """A resource-local failure with no arbitrary body-bearing message field."""

    resource_id: str
    stage: str
    error_code: str
    retryable: bool

    def __post_init__(self) -> None:
        _require_identity(self.resource_id, "failure resource id")
        _require_diagnostic_identifier(self.stage, "failure stage")
        _require_diagnostic_identifier(self.error_code, "failure error code")
        _require_bool(self.retryable, "failure retryable")

    @property
    def safe_summary(self) -> str:
        """Return a deterministic summary composed only of validated codes."""

        retryability = "RETRYABLE" if self.retryable else "NOT_RETRYABLE"
        return f"{self.stage}:{self.error_code}:{retryability}"


@dataclass(frozen=True)
class QueryReport:
    """Aggregated results and isolated resource-local failures."""

    results: tuple[TMResult, ...] = ()
    resource_failures: tuple[ResourceQueryFailure, ...] = ()

    def __post_init__(self) -> None:
        results = _require_tuple(self.results, "query results")
        failures = _require_tuple(
            self.resource_failures,
            "query resource_failures",
        )
        if any(not isinstance(result, TMResult) for result in results):
            raise TypeError("query results must contain TMResult values")
        if any(
            not isinstance(failure, ResourceQueryFailure)
            for failure in failures
        ):
            raise TypeError(
                "query resource_failures must contain ResourceQueryFailure values"
            )
        failure_ids = tuple(failure.resource_id for failure in failures)
        if len(failure_ids) != len(set(failure_ids)):
            raise ValueError("query report may contain one failure per resource")
        result_ids = {result.resource_id for result in results}
        if result_ids.intersection(failure_ids):
            raise ValueError(
                "a failed query resource cannot also contribute results"
            )


@dataclass(frozen=True)
class CanonicalResourceIdentity:
    """Stable resource and deterministic adjacent sidecar identity."""

    resource_id: str
    configured_jsonl_path: Path
    canonical_sidecar_path: Path
    snapshot_manifest_path: Path
    target_identity: str
    identity_version: str = CANONICAL_RESOURCE_IDENTITY_VERSION

    def __post_init__(self) -> None:
        _validate_canonical_resource_identity(self)

    @classmethod
    def from_configured_jsonl(
        cls,
        resource_id: str,
        configured_jsonl_path: Path,
    ) -> CanonicalResourceIdentity:
        _require_identity(resource_id, "resource id")
        _require_absolute_path(
            configured_jsonl_path,
            "configured JSONL path",
        )
        sidecar = configured_jsonl_path.with_name(
            f"{configured_jsonl_path.name}.sqlite3"
        )
        manifest = configured_jsonl_path.with_name(
            f"{configured_jsonl_path.name}.localcat-snapshot.json"
        )
        target_identity = _canonical_target_identity(
            resource_id,
            configured_jsonl_path,
            sidecar,
            manifest,
        )
        return cls(
            resource_id=resource_id,
            configured_jsonl_path=configured_jsonl_path,
            canonical_sidecar_path=sidecar,
            snapshot_manifest_path=manifest,
            target_identity=target_identity,
        )


def _canonical_target_identity(
    resource_id: str,
    configured_jsonl_path: Path,
    canonical_sidecar_path: Path,
    snapshot_manifest_path: Path,
) -> str:
    return _stable_digest(
        {
            "configured_jsonl_path": str(configured_jsonl_path),
            "identity_version": CANONICAL_RESOURCE_IDENTITY_VERSION,
            "resource_id": resource_id,
            "sidecar_path": str(canonical_sidecar_path),
            "snapshot_manifest_path": str(snapshot_manifest_path),
        }
    )


def _validate_canonical_resource_identity(
    identity: CanonicalResourceIdentity,
) -> None:
    _require_identity(identity.resource_id, "resource id")
    _require_absolute_path(
        identity.configured_jsonl_path,
        "configured JSONL path",
    )
    _require_absolute_path(
        identity.canonical_sidecar_path,
        "canonical sidecar path",
    )
    _require_absolute_path(
        identity.snapshot_manifest_path,
        "snapshot manifest path",
    )
    if identity.identity_version != CANONICAL_RESOURCE_IDENTITY_VERSION:
        raise ValueError("unsupported canonical resource identity version")
    expected_sidecar = identity.configured_jsonl_path.with_name(
        f"{identity.configured_jsonl_path.name}.sqlite3"
    )
    if identity.canonical_sidecar_path != expected_sidecar:
        raise ValueError("canonical sidecar path is not deterministic")
    expected_manifest = identity.configured_jsonl_path.with_name(
        f"{identity.configured_jsonl_path.name}.localcat-snapshot.json"
    )
    if identity.snapshot_manifest_path != expected_manifest:
        raise ValueError("snapshot manifest path is not deterministic")
    expected_target = _canonical_target_identity(
        identity.resource_id,
        identity.configured_jsonl_path,
        identity.canonical_sidecar_path,
        identity.snapshot_manifest_path,
    )
    if identity.target_identity != expected_target:
        raise ValueError("canonical target identity does not match resource")


class SnapshotKind(str, Enum):
    """Supported canonical ancestry snapshot purposes."""

    MIGRATION_SOURCE = "MIGRATION_SOURCE"
    EXPLICIT_EXPORT = "EXPLICIT_EXPORT"


class SourceBindingState(str, Enum):
    """Observed relationship between canonical history and configured JSONL."""

    VERIFIED_CURRENT = "VERIFIED_CURRENT"
    VERIFIED_HISTORY = "VERIFIED_HISTORY"
    SOURCE_DIVERGED = "SOURCE_DIVERGED"


@dataclass(frozen=True)
class SnapshotReceipt:
    """Portable ancestry receipt for one immutable JSONL snapshot."""

    snapshot_id: str
    resource_id: str
    canonical_store_id: str
    exported_revision: int
    jsonl_digest: str
    record_count: int
    format_version: str = SNAPSHOT_FORMAT_VERSION

    def __post_init__(self) -> None:
        _validate_snapshot_receipt(self)


def _validate_snapshot_receipt(receipt: SnapshotReceipt) -> None:
    _require_identity(receipt.snapshot_id, "snapshot id")
    _require_identity(receipt.resource_id, "snapshot resource id")
    _require_identity(receipt.canonical_store_id, "canonical store id")
    _require_int(receipt.exported_revision, "exported revision", minimum=0)
    _require_digest(receipt.jsonl_digest, "JSONL digest")
    _require_int(receipt.record_count, "snapshot record count", minimum=0)
    if receipt.format_version != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot format version")


def snapshot_receipt_digest(receipt: SnapshotReceipt) -> str:
    """Return the portable digest shared by receipt, manifest, and ledger."""

    if not isinstance(receipt, SnapshotReceipt):
        raise TypeError("receipt must be SnapshotReceipt")
    _validate_snapshot_receipt(receipt)
    return _stable_digest(
        {
            "canonical_store_id": receipt.canonical_store_id,
            "exported_revision": receipt.exported_revision,
            "format_version": receipt.format_version,
            "jsonl_digest": receipt.jsonl_digest,
            "record_count": receipt.record_count,
            "resource_id": receipt.resource_id,
            "snapshot_id": receipt.snapshot_id,
        }
    )


@dataclass(frozen=True)
class SnapshotManifest:
    """Portable adjacent manifest content for a snapshot receipt."""

    manifest_version: str
    snapshot_kind: SnapshotKind
    receipt: SnapshotReceipt
    receipt_digest: str

    def __post_init__(self) -> None:
        _validate_snapshot_manifest(self)


def _validate_snapshot_manifest(manifest: SnapshotManifest) -> None:
    if manifest.manifest_version != SNAPSHOT_MANIFEST_VERSION:
        raise ValueError("unsupported snapshot manifest version")
    if not isinstance(manifest.snapshot_kind, SnapshotKind):
        raise TypeError("snapshot kind must be SnapshotKind")
    if not isinstance(manifest.receipt, SnapshotReceipt):
        raise TypeError("manifest receipt must be SnapshotReceipt")
    _validate_snapshot_receipt(manifest.receipt)
    _require_digest(manifest.receipt_digest, "receipt digest")
    if manifest.receipt_digest != snapshot_receipt_digest(manifest.receipt):
        raise ValueError("snapshot manifest receipt digest does not match")


@dataclass(frozen=True)
class SnapshotBinding:
    """Configured JSONL, adjacent manifest, and canonical ancestry pair."""

    configured_jsonl_path: Path
    manifest_path: Path
    snapshot_kind: SnapshotKind
    receipt: SnapshotReceipt
    manifest: SnapshotManifest
    binding_version: str = SNAPSHOT_BINDING_VERSION

    def __post_init__(self) -> None:
        _validate_snapshot_binding(self)


def _validate_snapshot_binding(binding: SnapshotBinding) -> None:
    _require_absolute_path(
        binding.configured_jsonl_path,
        "binding configured JSONL path",
    )
    _require_absolute_path(binding.manifest_path, "binding manifest path")
    expected_manifest = binding.configured_jsonl_path.with_name(
        f"{binding.configured_jsonl_path.name}.localcat-snapshot.json"
    )
    if binding.manifest_path != expected_manifest:
        raise ValueError("binding manifest path is not deterministic")
    if binding.binding_version != SNAPSHOT_BINDING_VERSION:
        raise ValueError("unsupported snapshot binding version")
    if not isinstance(binding.snapshot_kind, SnapshotKind):
        raise TypeError("binding snapshot kind must be SnapshotKind")
    if not isinstance(binding.receipt, SnapshotReceipt):
        raise TypeError("binding receipt must be SnapshotReceipt")
    if not isinstance(binding.manifest, SnapshotManifest):
        raise TypeError("binding manifest must be SnapshotManifest")
    _validate_snapshot_receipt(binding.receipt)
    _validate_snapshot_manifest(binding.manifest)
    if binding.snapshot_kind is not binding.manifest.snapshot_kind:
        raise ValueError("binding and manifest snapshot kinds do not match")
    if binding.receipt != binding.manifest.receipt:
        raise ValueError("binding and manifest must carry the same receipt")


@dataclass(frozen=True)
class StageValidationEvidence:
    """Complete, versioned validation facts for a mutable stage."""

    evidence_version: str
    resource_id: str
    target_identity: str
    source_binding: SnapshotBinding
    snapshot_receipt_digest: str
    manifest_temp_digest: str
    schema_version: int
    fold_version: str
    index_version: str
    record_count: int
    origin_batch_count: int
    fts_count: int
    gram_counts: tuple[tuple[int, int], ...]
    exact_parity_digest: str
    integrity_ok: bool
    foreign_keys_ok: bool
    stage_file_digest: str

    def __post_init__(self) -> None:
        _validate_stage_validation_evidence(self)


def _validate_stage_validation_evidence(
    evidence: StageValidationEvidence,
) -> None:
    if evidence.evidence_version != STAGE_VALIDATION_EVIDENCE_VERSION:
        raise ValueError("unsupported stage validation evidence version")
    _require_identity(evidence.resource_id, "stage resource id")
    _require_digest(evidence.target_identity, "stage target identity")
    if not isinstance(evidence.source_binding, SnapshotBinding):
        raise TypeError("stage source binding must be SnapshotBinding")
    _validate_snapshot_binding(evidence.source_binding)
    receipt = evidence.source_binding.receipt
    if evidence.resource_id != receipt.resource_id:
        raise ValueError("stage and source binding resource identities differ")
    _require_digest(evidence.snapshot_receipt_digest, "snapshot receipt digest")
    if evidence.snapshot_receipt_digest != snapshot_receipt_digest(receipt):
        raise ValueError("stage snapshot receipt digest does not match ancestry")
    _require_digest(evidence.manifest_temp_digest, "manifest temporary digest")
    _require_int(evidence.schema_version, "stage schema version", minimum=1)
    _require_identity(evidence.fold_version, "stage fold version")
    _require_identity(evidence.index_version, "stage index version")
    _require_int(evidence.record_count, "stage record count", minimum=0)
    if evidence.record_count != receipt.record_count:
        raise ValueError("stage record count does not match snapshot receipt")
    _require_int(
        evidence.origin_batch_count,
        "stage origin batch count",
        minimum=0,
    )
    _require_int(evidence.fts_count, "stage FTS count", minimum=0)
    if evidence.fts_count > evidence.record_count:
        raise ValueError("stage FTS count cannot exceed record count")
    gram_counts = _require_tuple(evidence.gram_counts, "stage gram counts")
    gram_sizes: list[int] = []
    for pair in gram_counts:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError("stage gram counts must contain integer pairs")
        gram_size, gram_count = pair
        _require_int(gram_size, "stage gram size", minimum=1)
        _require_int(gram_count, "stage gram count", minimum=0)
        gram_sizes.append(gram_size)
    if gram_sizes != sorted(set(gram_sizes)):
        raise ValueError("stage gram sizes must be unique and ordered")
    _require_digest(evidence.exact_parity_digest, "exact parity digest")
    _require_bool(evidence.integrity_ok, "stage integrity status")
    _require_bool(evidence.foreign_keys_ok, "stage foreign-key status")
    if not evidence.integrity_ok:
        raise ValueError("stage integrity validation must pass")
    if not evidence.foreign_keys_ok:
        raise ValueError("stage foreign-key validation must pass")
    _require_digest(evidence.stage_file_digest, "stage file digest")


@dataclass(frozen=True)
class MutableStageRef:
    """Path-bearing working-stage shape that is never activation authority."""

    stage_id: str
    resource_identity: CanonicalResourceIdentity
    staged_db_path: Path
    manifest_temp_path: Path

    def __post_init__(self) -> None:
        _require_identity(self.stage_id, "mutable stage id")
        if not isinstance(self.resource_identity, CanonicalResourceIdentity):
            raise TypeError("mutable stage resource identity is invalid")
        _validate_canonical_resource_identity(self.resource_identity)
        _require_absolute_path(self.staged_db_path, "staged database path")
        _require_absolute_path(
            self.manifest_temp_path,
            "manifest temporary path",
        )
        if (
            self.staged_db_path.parent
            != self.resource_identity.canonical_sidecar_path.parent
        ):
            raise ValueError(
                "staged database must be adjacent to canonical sidecar"
            )
        if (
            self.manifest_temp_path.parent
            != self.resource_identity.snapshot_manifest_path.parent
        ):
            raise ValueError(
                "temporary manifest must be adjacent to final manifest"
            )
        if self.staged_db_path == self.resource_identity.canonical_sidecar_path:
            raise ValueError("mutable stage cannot be the canonical sidecar")
        if (
            self.manifest_temp_path
            == self.resource_identity.snapshot_manifest_path
        ):
            raise ValueError(
                "temporary manifest cannot be the published manifest"
            )


@dataclass(frozen=True, slots=True, init=False)
class _SealedArtifactRef:
    """Module-private opaque reference created only by the sealing seam."""

    registry_namespace: str
    artifact_id: str
    seal_digest: str

    def __init__(
        self,
        *,
        registry_namespace: str,
        artifact_id: str,
        seal_digest: str,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _RUNTIME_CAPABILITY_FACTORY_KEY:
            raise TypeError(
                "sealed artifact refs require the module-private factory"
            )
        _require_identity(registry_namespace, "registry namespace")
        _require_identity(artifact_id, "artifact id")
        _require_digest(seal_digest, "artifact seal digest")
        object.__setattr__(self, "registry_namespace", registry_namespace)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "seal_digest", seal_digest)


@dataclass(frozen=True)
class GenerationExpectation:
    """Expected prior generation and lineage; does not publish a next one."""

    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    expectation_version: str = GENERATION_EXPECTATION_VERSION

    def __post_init__(self) -> None:
        _validate_generation_expectation(self)


def _validate_generation_expectation(
    expectation: GenerationExpectation,
) -> None:
    _require_identity(expectation.resource_id, "generation resource id")
    _require_digest(expectation.target_identity, "generation target identity")
    _require_identity(
        expectation.canonical_store_id,
        "generation canonical store id",
    )
    _require_digest(
        expectation.snapshot_receipt_digest,
        "generation snapshot receipt digest",
    )
    if expectation.expected_prior_generation is not None:
        _require_int(
            expectation.expected_prior_generation,
            "expected prior generation",
            minimum=0,
        )
    if expectation.expectation_version != GENERATION_EXPECTATION_VERSION:
        raise ValueError("unsupported generation expectation version")


def stage_validation_evidence_digest(
    evidence: StageValidationEvidence,
) -> str:
    """Digest the complete portable validation and ancestry facts."""

    _validate_stage_validation_evidence(evidence)
    return _stable_digest(
        {
            "evidence_version": evidence.evidence_version,
            "exact_parity_digest": evidence.exact_parity_digest,
            "fold_version": evidence.fold_version,
            "foreign_keys_ok": evidence.foreign_keys_ok,
            "fts_count": evidence.fts_count,
            "gram_counts": [list(pair) for pair in evidence.gram_counts],
            "index_version": evidence.index_version,
            "integrity_ok": evidence.integrity_ok,
            "manifest_temp_digest": evidence.manifest_temp_digest,
            "origin_batch_count": evidence.origin_batch_count,
            "record_count": evidence.record_count,
            "resource_id": evidence.resource_id,
            "schema_version": evidence.schema_version,
            "snapshot_receipt_digest": evidence.snapshot_receipt_digest,
            "source_binding": {
                "binding_version": (
                    evidence.source_binding.binding_version
                ),
                "configured_jsonl_path": str(
                    evidence.source_binding.configured_jsonl_path
                ),
                "manifest_path": str(evidence.source_binding.manifest_path),
                "manifest_receipt_digest": (
                    evidence.source_binding.manifest.receipt_digest
                ),
                "manifest_version": (
                    evidence.source_binding.manifest.manifest_version
                ),
                "snapshot_kind": evidence.source_binding.snapshot_kind.value,
            },
            "stage_file_digest": evidence.stage_file_digest,
            "target_identity": evidence.target_identity,
        }
    )


def _artifact_seal_digest(
    *,
    registry_namespace: str,
    artifact_id: str,
    mutable_stage: MutableStageRef,
    evidence: StageValidationEvidence,
) -> str:
    """Pure digest helper for a registry's private sealed entry."""

    _require_identity(registry_namespace, "registry namespace")
    _require_identity(artifact_id, "artifact id")
    if not isinstance(mutable_stage, MutableStageRef):
        raise TypeError("artifact sealing requires MutableStageRef")
    if not isinstance(evidence, StageValidationEvidence):
        raise TypeError("artifact sealing requires StageValidationEvidence")
    _validate_stage_validation_evidence(evidence)
    identity = mutable_stage.resource_identity
    if (
        evidence.resource_id != identity.resource_id
        or evidence.target_identity != identity.target_identity
        or evidence.source_binding.configured_jsonl_path
        != identity.configured_jsonl_path
        or evidence.source_binding.manifest_path
        != identity.snapshot_manifest_path
    ):
        raise ValueError("stage evidence does not match resource identity")
    return _stable_digest(
        {
            "artifact_id": artifact_id,
            "canonical_store_id": (
                evidence.source_binding.receipt.canonical_store_id
            ),
            "evidence_digest": stage_validation_evidence_digest(evidence),
            "manifest_temp_path": str(mutable_stage.manifest_temp_path),
            "registry_namespace": registry_namespace,
            "resource_id": evidence.resource_id,
            "staged_db_path": str(mutable_stage.staged_db_path),
            "target_identity": evidence.target_identity,
        }
    )


def _sealed_stage_contract_digest(
    *,
    artifact: _SealedArtifactRef,
    evidence: StageValidationEvidence,
    generation: GenerationExpectation,
    activation_nonce: str,
) -> str:
    """Bind registry, artifact, lineage, evidence, nonce, and generation."""

    if not isinstance(artifact, _SealedArtifactRef):
        raise TypeError("sealed stage artifact is invalid")
    if not isinstance(evidence, StageValidationEvidence):
        raise TypeError("sealed stage evidence is invalid")
    if not isinstance(generation, GenerationExpectation):
        raise TypeError("sealed stage generation expectation is invalid")
    _validate_stage_validation_evidence(evidence)
    _validate_generation_expectation(generation)
    _require_identity(activation_nonce, "activation nonce")
    receipt = evidence.source_binding.receipt
    if (
        generation.resource_id != evidence.resource_id
        or generation.target_identity != evidence.target_identity
        or generation.canonical_store_id != receipt.canonical_store_id
        or generation.snapshot_receipt_digest
        != evidence.snapshot_receipt_digest
    ):
        raise ValueError("sealed stage generation lineage does not close")
    return _stable_digest(
        {
            "activation_nonce": activation_nonce,
            "artifact_id": artifact.artifact_id,
            "artifact_seal_digest": artifact.seal_digest,
            "canonical_store_id": generation.canonical_store_id,
            "evidence_digest": stage_validation_evidence_digest(evidence),
            "expected_prior_generation": (
                generation.expected_prior_generation
            ),
            "registry_namespace": artifact.registry_namespace,
            "resource_id": generation.resource_id,
            "snapshot_receipt_digest": (
                generation.snapshot_receipt_digest
            ),
            "target_identity": generation.target_identity,
        }
    )


@dataclass(frozen=True)
class SealedStage:
    """Closed stage shape whose authority still depends on registry membership."""

    artifact: _SealedArtifactRef
    evidence: StageValidationEvidence
    generation: GenerationExpectation
    activation_nonce: str
    sealed_stage_digest: str

    def __post_init__(self) -> None:
        _validate_sealed_stage(self)

    @property
    def expected_prior_generation(self) -> int | None:
        return self.generation.expected_prior_generation


def _validate_sealed_stage(stage: SealedStage) -> None:
    _require_digest(stage.sealed_stage_digest, "sealed stage digest")
    expected_digest = _sealed_stage_contract_digest(
        artifact=stage.artifact,
        evidence=stage.evidence,
        generation=stage.generation,
        activation_nonce=stage.activation_nonce,
    )
    if stage.sealed_stage_digest != expected_digest:
        raise ValueError("sealed stage digest does not close")


def _create_sealed_stage(
    *,
    registry_namespace: str,
    artifact_id: str,
    mutable_stage: MutableStageRef,
    evidence: StageValidationEvidence,
    generation: GenerationExpectation,
    activation_nonce: str,
) -> SealedStage:
    """Module-private StageSealer seam; it does not grant registry authority."""

    seal_digest = _artifact_seal_digest(
        registry_namespace=registry_namespace,
        artifact_id=artifact_id,
        mutable_stage=mutable_stage,
        evidence=evidence,
    )
    artifact = _SealedArtifactRef(
        registry_namespace=registry_namespace,
        artifact_id=artifact_id,
        seal_digest=seal_digest,
        _factory_key=_RUNTIME_CAPABILITY_FACTORY_KEY,
    )
    sealed_stage_digest = _sealed_stage_contract_digest(
        artifact=artifact,
        evidence=evidence,
        generation=generation,
        activation_nonce=activation_nonce,
    )
    return SealedStage(
        artifact=artifact,
        evidence=evidence,
        generation=generation,
        activation_nonce=activation_nonce,
        sealed_stage_digest=sealed_stage_digest,
    )


@dataclass(frozen=True, slots=True, init=False)
class _ActivationToken:
    """Module-private token shape created only by the coordinator seam."""

    token_id: str
    registry_namespace: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    activation_nonce: str
    token_version: str = ACTIVATION_TOKEN_VERSION

    def __init__(
        self,
        *,
        token_id: str,
        registry_namespace: str,
        resource_id: str,
        target_identity: str,
        canonical_store_id: str,
        artifact_id: str,
        artifact_seal_digest: str,
        sealed_stage_digest: str,
        snapshot_receipt_digest: str,
        expected_prior_generation: int | None,
        activation_nonce: str,
        token_version: str = ACTIVATION_TOKEN_VERSION,
        _factory_key: object | None = None,
    ) -> None:
        if _factory_key is not _RUNTIME_CAPABILITY_FACTORY_KEY:
            raise TypeError(
                "activation tokens require the module-private factory"
            )
        object.__setattr__(self, "token_id", token_id)
        object.__setattr__(
            self,
            "registry_namespace",
            registry_namespace,
        )
        object.__setattr__(self, "resource_id", resource_id)
        object.__setattr__(self, "target_identity", target_identity)
        object.__setattr__(
            self,
            "canonical_store_id",
            canonical_store_id,
        )
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(
            self,
            "artifact_seal_digest",
            artifact_seal_digest,
        )
        object.__setattr__(
            self,
            "sealed_stage_digest",
            sealed_stage_digest,
        )
        object.__setattr__(
            self,
            "snapshot_receipt_digest",
            snapshot_receipt_digest,
        )
        object.__setattr__(
            self,
            "expected_prior_generation",
            expected_prior_generation,
        )
        object.__setattr__(self, "activation_nonce", activation_nonce)
        object.__setattr__(self, "token_version", token_version)
        _validate_activation_token(self)


def _validate_activation_token(token: _ActivationToken) -> None:
    _require_identity(token.token_id, "activation token id")
    _require_identity(token.registry_namespace, "token registry namespace")
    _require_identity(token.resource_id, "token resource id")
    _require_digest(token.target_identity, "token target identity")
    _require_identity(token.canonical_store_id, "token canonical store id")
    _require_identity(token.artifact_id, "token artifact id")
    _require_digest(token.artifact_seal_digest, "token artifact seal digest")
    _require_digest(token.sealed_stage_digest, "token sealed stage digest")
    _require_digest(
        token.snapshot_receipt_digest,
        "token snapshot receipt digest",
    )
    if token.expected_prior_generation is not None:
        _require_int(
            token.expected_prior_generation,
            "token expected prior generation",
            minimum=0,
        )
    _require_identity(token.activation_nonce, "token activation nonce")
    if token.token_version != ACTIVATION_TOKEN_VERSION:
        raise ValueError("unsupported activation token version")


def _validate_activation_token_for_stage(
    token: _ActivationToken,
    stage: SealedStage,
) -> None:
    """Revalidate the full immutable token/stage chain."""

    if not isinstance(token, _ActivationToken):
        raise TypeError("token must be a private activation token")
    if not isinstance(stage, SealedStage):
        raise TypeError("stage must be SealedStage")
    _validate_activation_token(token)
    _validate_sealed_stage(stage)
    receipt = stage.evidence.source_binding.receipt
    expected = (
        stage.artifact.registry_namespace,
        stage.evidence.resource_id,
        stage.evidence.target_identity,
        receipt.canonical_store_id,
        stage.artifact.artifact_id,
        stage.artifact.seal_digest,
        stage.sealed_stage_digest,
        stage.evidence.snapshot_receipt_digest,
        stage.expected_prior_generation,
        stage.activation_nonce,
    )
    actual = (
        token.registry_namespace,
        token.resource_id,
        token.target_identity,
        token.canonical_store_id,
        token.artifact_id,
        token.artifact_seal_digest,
        token.sealed_stage_digest,
        token.snapshot_receipt_digest,
        token.expected_prior_generation,
        token.activation_nonce,
    )
    if actual != expected:
        raise ValueError("activation token does not close over sealed stage")


def _create_activation_token(
    *,
    token_id: str,
    stage: SealedStage,
) -> _ActivationToken:
    """Module-private coordinator seam; registry state grants single use."""

    if not isinstance(stage, SealedStage):
        raise TypeError("activation token stage must be SealedStage")
    _validate_sealed_stage(stage)
    receipt = stage.evidence.source_binding.receipt
    token = _ActivationToken(
        token_id=token_id,
        registry_namespace=stage.artifact.registry_namespace,
        resource_id=stage.evidence.resource_id,
        target_identity=stage.evidence.target_identity,
        canonical_store_id=receipt.canonical_store_id,
        artifact_id=stage.artifact.artifact_id,
        artifact_seal_digest=stage.artifact.seal_digest,
        sealed_stage_digest=stage.sealed_stage_digest,
        snapshot_receipt_digest=stage.evidence.snapshot_receipt_digest,
        expected_prior_generation=stage.expected_prior_generation,
        activation_nonce=stage.activation_nonce,
        _factory_key=_RUNTIME_CAPABILITY_FACTORY_KEY,
    )
    _validate_activation_token_for_stage(token, stage)
    return token


class ActivationCapabilityState(str, Enum):
    """Registry-owned linear activation states."""

    SEALED = "SEALED"
    TOKEN_ISSUED = "TOKEN_ISSUED"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"


class _SealedArtifactRegistryPort(Protocol):
    """Private registry lifecycle boundary for the future coordinator."""

    @property
    def registry_namespace(self) -> str: ...

    def seal(
        self,
        mutable_stage: MutableStageRef,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
    ) -> SealedStage: ...

    def contains(self, stage: SealedStage) -> bool: ...

    def state(self, stage: SealedStage) -> ActivationCapabilityState: ...

    def issue_token(
        self,
        stage: SealedStage,
        *,
        current_generation: int | None,
    ) -> _ActivationToken: ...

    def consume(self, token: _ActivationToken) -> None: ...

    def cancel(self, token: _ActivationToken) -> None: ...


class ResourceStoreCoordinatorPort(Protocol):
    """Public activation boundary; implementation arrives in task 5.5."""

    def activate(
        self,
        sealed_stage: SealedStage,
    ) -> None: ...


type TMContract = (
    TMRecord
    | TMRecordDraft
    | TMQuery
    | SimilarityEvidence
    | ContextEvidence
    | TMResult
    | ResourceQueryFailure
    | QueryReport
    | CanonicalResourceIdentity
    | SnapshotReceipt
    | SnapshotManifest
    | SnapshotBinding
    | StageValidationEvidence
)


def _encode_provenance(
    provenance: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in provenance]


def _encode_context_evidence(evidence: ContextEvidence) -> dict[str, Any]:
    return {
        "comparable_fields": list(evidence.comparable_fields),
        "matched_fields": list(evidence.matched_fields),
        "mismatched_fields": list(evidence.mismatched_fields),
        "strength_v1": list(evidence.strength_v1),
    }


def _encode_similarity_evidence(
    evidence: SimilarityEvidence,
) -> dict[str, Any]:
    return {
        "dice_bigram": evidence.dice_bigram,
        "final_similarity": evidence.final_similarity,
        "levenshtein_ratio": evidence.levenshtein_ratio,
        "scorer_version": evidence.scorer_version,
    }


def _encode_result(result: TMResult) -> dict[str, Any]:
    return {
        "context_evidence": _encode_context_evidence(result.context_evidence),
        "match_type": result.match_type.value,
        "matched_source": result.matched_source,
        "provenance": _encode_provenance(result.provenance),
        "query_source": result.query_source,
        "record_id": result.record_id,
        "resource_id": result.resource_id,
        "similarity": result.similarity,
        "similarity_evidence": (
            None
            if result.similarity_evidence is None
            else _encode_similarity_evidence(result.similarity_evidence)
        ),
        "stable_tie_key": list(result.stable_tie_key),
        "target": result.target,
    }


def _encode_failure(failure: ResourceQueryFailure) -> dict[str, Any]:
    return {
        "error_code": failure.error_code,
        "resource_id": failure.resource_id,
        "retryable": failure.retryable,
        "stage": failure.stage,
    }


def _encode_canonical_resource_identity(
    identity: CanonicalResourceIdentity,
) -> dict[str, Any]:
    return {
        "canonical_sidecar_path": str(identity.canonical_sidecar_path),
        "configured_jsonl_path": str(identity.configured_jsonl_path),
        "identity_version": identity.identity_version,
        "resource_id": identity.resource_id,
        "snapshot_manifest_path": str(identity.snapshot_manifest_path),
        "target_identity": identity.target_identity,
    }


def _encode_snapshot_receipt(receipt: SnapshotReceipt) -> dict[str, Any]:
    return {
        "canonical_store_id": receipt.canonical_store_id,
        "exported_revision": receipt.exported_revision,
        "format_version": receipt.format_version,
        "jsonl_digest": receipt.jsonl_digest,
        "record_count": receipt.record_count,
        "resource_id": receipt.resource_id,
        "snapshot_id": receipt.snapshot_id,
    }


def _encode_snapshot_manifest(manifest: SnapshotManifest) -> dict[str, Any]:
    return {
        "manifest_version": manifest.manifest_version,
        "receipt": _encode_snapshot_receipt(manifest.receipt),
        "receipt_digest": manifest.receipt_digest,
        "snapshot_kind": manifest.snapshot_kind.value,
    }


def _encode_snapshot_binding(binding: SnapshotBinding) -> dict[str, Any]:
    return {
        "binding_version": binding.binding_version,
        "configured_jsonl_path": str(binding.configured_jsonl_path),
        "manifest": _encode_snapshot_manifest(binding.manifest),
        "manifest_path": str(binding.manifest_path),
        "receipt": _encode_snapshot_receipt(binding.receipt),
        "snapshot_kind": binding.snapshot_kind.value,
    }


def _encode_stage_validation_evidence(
    evidence: StageValidationEvidence,
) -> dict[str, Any]:
    return {
        "evidence_version": evidence.evidence_version,
        "exact_parity_digest": evidence.exact_parity_digest,
        "fold_version": evidence.fold_version,
        "foreign_keys_ok": evidence.foreign_keys_ok,
        "fts_count": evidence.fts_count,
        "gram_counts": [list(pair) for pair in evidence.gram_counts],
        "index_version": evidence.index_version,
        "integrity_ok": evidence.integrity_ok,
        "manifest_temp_digest": evidence.manifest_temp_digest,
        "origin_batch_count": evidence.origin_batch_count,
        "record_count": evidence.record_count,
        "resource_id": evidence.resource_id,
        "schema_version": evidence.schema_version,
        "snapshot_receipt_digest": evidence.snapshot_receipt_digest,
        "source_binding": _encode_snapshot_binding(evidence.source_binding),
        "stage_file_digest": evidence.stage_file_digest,
        "target_identity": evidence.target_identity,
    }


def _contract_payload(contract: TMContract) -> tuple[str, dict[str, Any]]:
    if isinstance(contract, TMResourceHandle):
        raise TypeError(
            "TMResourceHandle is runtime-only and cannot use the stable codec"
        )
    if isinstance(contract, TMRecord):
        return "TMRecord", {
            "context_next_raw": contract.context_next_raw,
            "context_prev_raw": contract.context_prev_raw,
            "file_source": contract.file_source,
            "legacy_line_no": contract.legacy_line_no,
            "origin_batch_id": contract.origin_batch_id,
            "origin_ordinal": contract.origin_ordinal,
            "provenance": _encode_provenance(contract.provenance),
            "record_id": contract.record_id,
            "source_raw": contract.source_raw,
            "speaker_raw": contract.speaker_raw,
            "target_raw": contract.target_raw,
        }
    if isinstance(contract, TMRecordDraft):
        return "TMRecordDraft", {
            "context_next_raw": contract.context_next_raw,
            "context_prev_raw": contract.context_prev_raw,
            "file_source": contract.file_source,
            "provenance": _encode_provenance(contract.provenance),
            "source_raw": contract.source_raw,
            "speaker_raw": contract.speaker_raw,
            "target_raw": contract.target_raw,
        }
    if isinstance(contract, TMQuery):
        return "TMQuery", {
            "context_next_raw": contract.context_next_raw,
            "context_prev_raw": contract.context_prev_raw,
            "limit": contract.limit,
            "minimum_similarity": contract.minimum_similarity,
            "query_source": contract.query_source,
            "resource_order": list(contract.resource_order),
            "speaker_raw": contract.speaker_raw,
        }
    if isinstance(contract, SimilarityEvidence):
        return "SimilarityEvidence", _encode_similarity_evidence(contract)
    if isinstance(contract, ContextEvidence):
        return "ContextEvidence", _encode_context_evidence(contract)
    if isinstance(contract, TMResult):
        return "TMResult", _encode_result(contract)
    if isinstance(contract, ResourceQueryFailure):
        return "ResourceQueryFailure", _encode_failure(contract)
    if isinstance(contract, QueryReport):
        return "QueryReport", {
            "resource_failures": [
                _encode_failure(failure)
                for failure in contract.resource_failures
            ],
            "results": [_encode_result(result) for result in contract.results],
        }
    if isinstance(contract, CanonicalResourceIdentity):
        return (
            "CanonicalResourceIdentity",
            _encode_canonical_resource_identity(contract),
        )
    if isinstance(contract, SnapshotReceipt):
        return "SnapshotReceipt", _encode_snapshot_receipt(contract)
    if isinstance(contract, SnapshotManifest):
        return "SnapshotManifest", _encode_snapshot_manifest(contract)
    if isinstance(contract, SnapshotBinding):
        return "SnapshotBinding", _encode_snapshot_binding(contract)
    if isinstance(contract, StageValidationEvidence):
        return (
            "StageValidationEvidence",
            _encode_stage_validation_evidence(contract),
        )
    raise TypeError(f"unsupported TM contract type: {type(contract).__name__}")


def contract_to_json(contract: TMContract) -> str:
    """Encode one supported contract to deterministic, versioned JSON."""

    contract_type, payload = _contract_payload(contract)
    envelope = {
        "contract_type": contract_type,
        "contract_version": TM_CONTRACT_CODEC_VERSION,
        "payload": payload,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _as_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    return value


def _strict_fields(
    value: object,
    field_name: str,
    expected_fields: tuple[str, ...],
) -> Mapping[str, Any]:
    mapping = _as_mapping(value, field_name)
    expected = set(expected_fields)
    actual = set(mapping)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise ValueError(f"{field_name} missing fields: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"{field_name} has unexpected fields")
    return mapping


def _as_list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return value


def _decode_provenance(value: object) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for entry in _as_list(value, "provenance"):
        pair = _as_list(entry, "provenance entry")
        if len(pair) != 2:
            raise ValueError("provenance entries must contain two values")
        pairs.append((pair[0], pair[1]))
    return tuple(pairs)


def _decode_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(_as_list(value, field_name))


def _decode_int_tuple(value: object, field_name: str) -> tuple[int, ...]:
    return tuple(_as_list(value, field_name))


def _decode_context_strength(
    value: object,
) -> tuple[int, int, int, int, int]:
    items = _decode_int_tuple(value, "strength_v1")
    if len(items) != 5:
        raise ValueError("strength_v1 must contain five values")
    first, second, third, fourth, fifth = items
    return first, second, third, fourth, fifth


def _decode_stable_tie_key(value: object) -> tuple[int, int]:
    items = _decode_int_tuple(value, "stable_tie_key")
    if len(items) != 2:
        raise ValueError("stable_tie_key must contain two values")
    first, second = items
    return first, second


def _decode_similarity_evidence(value: object) -> SimilarityEvidence:
    payload = _strict_fields(
        value,
        "similarity evidence payload",
        (
            "dice_bigram",
            "final_similarity",
            "levenshtein_ratio",
            "scorer_version",
        ),
    )
    return SimilarityEvidence(
        levenshtein_ratio=payload["levenshtein_ratio"],
        dice_bigram=payload["dice_bigram"],
        final_similarity=payload["final_similarity"],
        scorer_version=payload["scorer_version"],
    )


def _decode_context_evidence(value: object) -> ContextEvidence:
    payload = _strict_fields(
        value,
        "context evidence payload",
        (
            "comparable_fields",
            "matched_fields",
            "mismatched_fields",
            "strength_v1",
        ),
    )
    return ContextEvidence(
        comparable_fields=_decode_string_tuple(
            payload["comparable_fields"],
            "comparable_fields",
        ),
        matched_fields=_decode_string_tuple(
            payload["matched_fields"],
            "matched_fields",
        ),
        mismatched_fields=_decode_string_tuple(
            payload["mismatched_fields"],
            "mismatched_fields",
        ),
        strength_v1=_decode_context_strength(payload["strength_v1"]),
    )


def _decode_result(value: object) -> TMResult:
    payload = _strict_fields(
        value,
        "TMResult payload",
        (
            "context_evidence",
            "match_type",
            "matched_source",
            "provenance",
            "query_source",
            "record_id",
            "resource_id",
            "similarity",
            "similarity_evidence",
            "stable_tie_key",
            "target",
        ),
    )
    try:
        match_type = TMMatchType(payload["match_type"])
    except (TypeError, ValueError):
        raise ValueError("TMResult match_type is invalid") from None
    similarity_payload = payload["similarity_evidence"]
    return TMResult(
        resource_id=payload["resource_id"],
        record_id=payload["record_id"],
        query_source=payload["query_source"],
        matched_source=payload["matched_source"],
        target=payload["target"],
        match_type=match_type,
        similarity=payload["similarity"],
        similarity_evidence=(
            None
            if similarity_payload is None
            else _decode_similarity_evidence(similarity_payload)
        ),
        context_evidence=_decode_context_evidence(
            payload["context_evidence"]
        ),
        provenance=_decode_provenance(payload["provenance"]),
        stable_tie_key=_decode_stable_tie_key(payload["stable_tie_key"]),
    )


def _decode_failure(value: object) -> ResourceQueryFailure:
    payload = _strict_fields(
        value,
        "ResourceQueryFailure payload",
        ("error_code", "resource_id", "retryable", "stage"),
    )
    return ResourceQueryFailure(
        resource_id=payload["resource_id"],
        stage=payload["stage"],
        error_code=payload["error_code"],
        retryable=payload["retryable"],
    )


def _decode_path(value: object, field_name: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string path")
    return Path(value)


def _decode_snapshot_kind(value: object, field_name: str) -> SnapshotKind:
    try:
        return SnapshotKind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} is invalid") from None


def _decode_canonical_resource_identity(
    value: object,
) -> CanonicalResourceIdentity:
    payload = _strict_fields(
        value,
        "CanonicalResourceIdentity payload",
        (
            "canonical_sidecar_path",
            "configured_jsonl_path",
            "identity_version",
            "resource_id",
            "snapshot_manifest_path",
            "target_identity",
        ),
    )
    return CanonicalResourceIdentity(
        resource_id=payload["resource_id"],
        configured_jsonl_path=_decode_path(
            payload["configured_jsonl_path"],
            "configured_jsonl_path",
        ),
        canonical_sidecar_path=_decode_path(
            payload["canonical_sidecar_path"],
            "canonical_sidecar_path",
        ),
        snapshot_manifest_path=_decode_path(
            payload["snapshot_manifest_path"],
            "snapshot_manifest_path",
        ),
        target_identity=payload["target_identity"],
        identity_version=payload["identity_version"],
    )


def _decode_snapshot_receipt(value: object) -> SnapshotReceipt:
    payload = _strict_fields(
        value,
        "SnapshotReceipt payload",
        (
            "canonical_store_id",
            "exported_revision",
            "format_version",
            "jsonl_digest",
            "record_count",
            "resource_id",
            "snapshot_id",
        ),
    )
    return SnapshotReceipt(
        snapshot_id=payload["snapshot_id"],
        resource_id=payload["resource_id"],
        canonical_store_id=payload["canonical_store_id"],
        exported_revision=payload["exported_revision"],
        jsonl_digest=payload["jsonl_digest"],
        record_count=payload["record_count"],
        format_version=payload["format_version"],
    )


def _decode_snapshot_manifest(value: object) -> SnapshotManifest:
    payload = _strict_fields(
        value,
        "SnapshotManifest payload",
        (
            "manifest_version",
            "receipt",
            "receipt_digest",
            "snapshot_kind",
        ),
    )
    return SnapshotManifest(
        manifest_version=payload["manifest_version"],
        snapshot_kind=_decode_snapshot_kind(
            payload["snapshot_kind"],
            "SnapshotManifest snapshot_kind",
        ),
        receipt=_decode_snapshot_receipt(payload["receipt"]),
        receipt_digest=payload["receipt_digest"],
    )


def _decode_snapshot_binding(value: object) -> SnapshotBinding:
    payload = _strict_fields(
        value,
        "SnapshotBinding payload",
        (
            "binding_version",
            "configured_jsonl_path",
            "manifest",
            "manifest_path",
            "receipt",
            "snapshot_kind",
        ),
    )
    return SnapshotBinding(
        configured_jsonl_path=_decode_path(
            payload["configured_jsonl_path"],
            "SnapshotBinding configured_jsonl_path",
        ),
        manifest_path=_decode_path(
            payload["manifest_path"],
            "SnapshotBinding manifest_path",
        ),
        snapshot_kind=_decode_snapshot_kind(
            payload["snapshot_kind"],
            "SnapshotBinding snapshot_kind",
        ),
        receipt=_decode_snapshot_receipt(payload["receipt"]),
        manifest=_decode_snapshot_manifest(payload["manifest"]),
        binding_version=payload["binding_version"],
    )


def _decode_gram_counts(value: object) -> tuple[tuple[int, int], ...]:
    pairs: list[tuple[int, int]] = []
    for entry in _as_list(value, "gram_counts"):
        pair = _as_list(entry, "gram_counts entry")
        if len(pair) != 2:
            raise ValueError("gram_counts entries must contain two values")
        gram_size, gram_count = pair
        if (
            not isinstance(gram_size, int)
            or isinstance(gram_size, bool)
            or not isinstance(gram_count, int)
            or isinstance(gram_count, bool)
        ):
            raise ValueError("gram_counts entries must contain integers")
        pairs.append((gram_size, gram_count))
    return tuple(pairs)


def _decode_stage_validation_evidence(
    value: object,
) -> StageValidationEvidence:
    payload = _strict_fields(
        value,
        "StageValidationEvidence payload",
        (
            "evidence_version",
            "exact_parity_digest",
            "fold_version",
            "foreign_keys_ok",
            "fts_count",
            "gram_counts",
            "index_version",
            "integrity_ok",
            "manifest_temp_digest",
            "origin_batch_count",
            "record_count",
            "resource_id",
            "schema_version",
            "snapshot_receipt_digest",
            "source_binding",
            "stage_file_digest",
            "target_identity",
        ),
    )
    return StageValidationEvidence(
        evidence_version=payload["evidence_version"],
        resource_id=payload["resource_id"],
        target_identity=payload["target_identity"],
        source_binding=_decode_snapshot_binding(payload["source_binding"]),
        snapshot_receipt_digest=payload["snapshot_receipt_digest"],
        manifest_temp_digest=payload["manifest_temp_digest"],
        schema_version=payload["schema_version"],
        fold_version=payload["fold_version"],
        index_version=payload["index_version"],
        record_count=payload["record_count"],
        origin_batch_count=payload["origin_batch_count"],
        fts_count=payload["fts_count"],
        gram_counts=_decode_gram_counts(payload["gram_counts"]),
        exact_parity_digest=payload["exact_parity_digest"],
        integrity_ok=payload["integrity_ok"],
        foreign_keys_ok=payload["foreign_keys_ok"],
        stage_file_digest=payload["stage_file_digest"],
    )


def _decode_payload(contract_type: str, value: object) -> TMContract:
    if contract_type == "TMRecord":
        payload = _strict_fields(
            value,
            "TMRecord payload",
            (
                "context_next_raw",
                "context_prev_raw",
                "file_source",
                "legacy_line_no",
                "origin_batch_id",
                "origin_ordinal",
                "provenance",
                "record_id",
                "source_raw",
                "speaker_raw",
                "target_raw",
            ),
        )
        return TMRecord(
            record_id=payload["record_id"],
            source_raw=payload["source_raw"],
            target_raw=payload["target_raw"],
            speaker_raw=payload["speaker_raw"],
            context_prev_raw=payload["context_prev_raw"],
            context_next_raw=payload["context_next_raw"],
            file_source=payload["file_source"],
            provenance=_decode_provenance(payload["provenance"]),
            legacy_line_no=payload["legacy_line_no"],
            origin_batch_id=payload["origin_batch_id"],
            origin_ordinal=payload["origin_ordinal"],
        )
    if contract_type == "TMRecordDraft":
        payload = _strict_fields(
            value,
            "TMRecordDraft payload",
            (
                "context_next_raw",
                "context_prev_raw",
                "file_source",
                "provenance",
                "source_raw",
                "speaker_raw",
                "target_raw",
            ),
        )
        return TMRecordDraft(
            source_raw=payload["source_raw"],
            target_raw=payload["target_raw"],
            speaker_raw=payload["speaker_raw"],
            context_prev_raw=payload["context_prev_raw"],
            context_next_raw=payload["context_next_raw"],
            file_source=payload["file_source"],
            provenance=_decode_provenance(payload["provenance"]),
        )
    if contract_type == "TMQuery":
        payload = _strict_fields(
            value,
            "TMQuery payload",
            (
                "context_next_raw",
                "context_prev_raw",
                "limit",
                "minimum_similarity",
                "query_source",
                "resource_order",
                "speaker_raw",
            ),
        )
        return TMQuery(
            query_source=payload["query_source"],
            speaker_raw=payload["speaker_raw"],
            context_prev_raw=payload["context_prev_raw"],
            context_next_raw=payload["context_next_raw"],
            minimum_similarity=payload["minimum_similarity"],
            limit=payload["limit"],
            resource_order=_decode_string_tuple(
                payload["resource_order"],
                "resource_order",
            ),
        )
    if contract_type == "SimilarityEvidence":
        return _decode_similarity_evidence(value)
    if contract_type == "ContextEvidence":
        return _decode_context_evidence(value)
    if contract_type == "TMResult":
        return _decode_result(value)
    if contract_type == "ResourceQueryFailure":
        return _decode_failure(value)
    if contract_type == "QueryReport":
        payload = _strict_fields(
            value,
            "QueryReport payload",
            ("resource_failures", "results"),
        )
        return QueryReport(
            results=tuple(
                _decode_result(result)
                for result in _as_list(payload["results"], "results")
            ),
            resource_failures=tuple(
                _decode_failure(failure)
                for failure in _as_list(
                    payload["resource_failures"],
                    "resource_failures",
                )
            ),
        )
    if contract_type == "CanonicalResourceIdentity":
        return _decode_canonical_resource_identity(value)
    if contract_type == "SnapshotReceipt":
        return _decode_snapshot_receipt(value)
    if contract_type == "SnapshotManifest":
        return _decode_snapshot_manifest(value)
    if contract_type == "SnapshotBinding":
        return _decode_snapshot_binding(value)
    if contract_type == "StageValidationEvidence":
        return _decode_stage_validation_evidence(value)
    raise ValueError("unsupported contract type")


def contract_from_json(serialized: str) -> TMContract:
    """Decode a strict supported-version envelope into an immutable contract."""

    if not isinstance(serialized, str):
        raise TypeError("serialized contract must be a string")

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        value = json.loads(serialized, parse_constant=reject_non_finite)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("serialized contract is not valid JSON") from None
    envelope = _strict_fields(
        value,
        "contract envelope",
        ("contract_type", "contract_version", "payload"),
    )
    version = envelope["contract_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("contract_version must be an integer")
    if version != TM_CONTRACT_CODEC_VERSION:
        raise ValueError(f"unsupported contract version: {version}")
    contract_type = envelope["contract_type"]
    _require_identity(contract_type, "contract_type")
    return _decode_payload(contract_type, envelope["payload"])


__all__ = [
    "ACTIVATION_TOKEN_VERSION",
    "CANONICAL_RESOURCE_IDENTITY_VERSION",
    "GENERATION_EXPECTATION_VERSION",
    "SCORER_VERSION_V1",
    "SNAPSHOT_BINDING_VERSION",
    "SNAPSHOT_FORMAT_VERSION",
    "SNAPSHOT_MANIFEST_VERSION",
    "STAGE_VALIDATION_EVIDENCE_VERSION",
    "TM_CONTRACT_CODEC_VERSION",
    "ActivationCapabilityState",
    "CanonicalResourceIdentity",
    "ContextEvidence",
    "GenerationExpectation",
    "MutableStageRef",
    "QueryReport",
    "ResourceStoreCoordinatorPort",
    "ResourceQueryFailure",
    "SealedStage",
    "SimilarityEvidence",
    "SnapshotBinding",
    "SnapshotKind",
    "SnapshotManifest",
    "SnapshotReceipt",
    "SourceBindingState",
    "StageValidationEvidence",
    "TMContract",
    "TMMatchType",
    "TMQuery",
    "TMRecord",
    "TMRecordDraft",
    "TMResourceHandle",
    "TMResult",
    "TMStore",
    "contract_from_json",
    "contract_to_json",
    "snapshot_receipt_digest",
    "stage_validation_evidence_digest",
    "validate_resource_handles",
]
