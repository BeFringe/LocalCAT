"""Versioned, immutable core contracts for local translation memory.

The contracts in this module intentionally have no storage, parser, matcher, or
UI dependency.  Raw bilingual text is kept only in record/query/result shapes;
resource-local failures expose diagnostic identifiers rather than arbitrary
exception text.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
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
MATCHER_VALIDATION_SUMMARY_VERSION = "matcher-validation-summary-v1"
MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION = "matcher-validation-v1"
MATCHER_VALIDATION_MANIFEST_CODEC_VERSION = 1

_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STRICT_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z"
)
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


class AssetKind(str, Enum):
    """Portable operation assets whose preservation can be proven."""

    ORIGINAL_SOURCE = "ORIGINAL_SOURCE"
    ACTIVE_STORE = "ACTIVE_STORE"
    EXPORT_DESTINATION = "EXPORT_DESTINATION"


class AssetPreservationState(str, Enum):
    """Closed relationship between before and observed asset digests."""

    VERIFIED_UNCHANGED = "VERIFIED_UNCHANGED"
    VERIFIED_CHANGED = "VERIFIED_CHANGED"
    UNVERIFIED = "UNVERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DiagnosticDisposition(str, Enum):
    """Closed effect of a safe row/record diagnostic."""

    REJECTED = "REJECTED"
    WARNING = "WARNING"


@dataclass(frozen=True)
class AssetPreservationEvidence:
    """Digest-backed preservation fact, not a caller-supplied success flag."""

    asset_kind: AssetKind
    state: AssetPreservationState
    before_digest: str | None
    observed_digest: str | None

    def __post_init__(self) -> None:
        _validate_asset_preservation_evidence(self)


def _validate_asset_preservation_evidence(
    evidence: AssetPreservationEvidence,
) -> None:
    if not isinstance(evidence.asset_kind, AssetKind):
        raise TypeError("asset preservation kind must be AssetKind")
    if not isinstance(evidence.state, AssetPreservationState):
        raise TypeError(
            "asset preservation state must be AssetPreservationState"
        )
    if evidence.state is AssetPreservationState.NOT_APPLICABLE:
        if (
            evidence.before_digest is not None
            or evidence.observed_digest is not None
        ):
            raise ValueError(
                "not-applicable asset preservation must omit digests"
            )
        return
    if evidence.before_digest is None:
        raise ValueError("asset preservation requires a before digest")
    _require_digest(evidence.before_digest, "asset before digest")
    if evidence.state is AssetPreservationState.UNVERIFIED:
        if evidence.observed_digest is not None:
            raise ValueError(
                "unverified asset preservation must omit observed digest"
            )
        return
    if evidence.observed_digest is None:
        raise ValueError("verified asset preservation requires observed digest")
    _require_digest(evidence.observed_digest, "asset observed digest")
    if (
        evidence.state is AssetPreservationState.VERIFIED_UNCHANGED
        and evidence.observed_digest != evidence.before_digest
    ):
        raise ValueError("verified unchanged asset digests must match")
    if (
        evidence.state is AssetPreservationState.VERIFIED_CHANGED
        and evidence.observed_digest == evidence.before_digest
    ):
        raise ValueError("verified changed asset digests must differ")


@dataclass(frozen=True)
class RecoveryLocator:
    """Portable recovery location without filesystem authority."""

    path: Path
    asset_kind: AssetKind
    expected_digest: str

    def __post_init__(self) -> None:
        _validate_recovery_locator(self)


def _validate_recovery_locator(locator: RecoveryLocator) -> None:
    _require_absolute_path(locator.path, "recovery locator path")
    if not isinstance(locator.asset_kind, AssetKind):
        raise TypeError("recovery locator asset kind must be AssetKind")
    _require_digest(locator.expected_digest, "recovery expected digest")


@dataclass(frozen=True)
class MigrationDiagnostic:
    """Safe, locatable migration fact without TM text."""

    code: str
    stage: str
    line_number: int | None
    record_id: int | None
    disposition: DiagnosticDisposition
    safe_summary: str

    def __post_init__(self) -> None:
        _validate_migration_diagnostic(self)


def _validate_migration_diagnostic(
    diagnostic: MigrationDiagnostic,
) -> None:
    _require_diagnostic_identifier(diagnostic.code, "migration diagnostic code")
    _require_diagnostic_identifier(
        diagnostic.stage,
        "migration diagnostic stage",
    )
    if diagnostic.line_number is not None:
        _require_int(
            diagnostic.line_number,
            "migration diagnostic line number",
            minimum=1,
        )
    if diagnostic.record_id is not None:
        _require_int(
            diagnostic.record_id,
            "migration diagnostic record id",
            minimum=1,
        )
    if diagnostic.line_number is None and diagnostic.record_id is None:
        raise ValueError(
            "migration diagnostic requires a line or record location"
        )
    if not isinstance(diagnostic.disposition, DiagnosticDisposition):
        raise TypeError(
            "migration diagnostic disposition must be DiagnosticDisposition"
        )
    _require_diagnostic_identifier(
        diagnostic.safe_summary,
        "migration diagnostic safe summary",
    )


@dataclass(frozen=True)
class ExportDiagnostic:
    """Safe, record-local export fact without TM text."""

    code: str
    record_id: int | None
    disposition: DiagnosticDisposition
    safe_summary: str

    def __post_init__(self) -> None:
        _validate_export_diagnostic(self)


def _validate_export_diagnostic(diagnostic: ExportDiagnostic) -> None:
    _require_diagnostic_identifier(diagnostic.code, "export diagnostic code")
    if diagnostic.record_id is not None:
        _require_int(
            diagnostic.record_id,
            "export diagnostic record id",
            minimum=1,
        )
    if not isinstance(diagnostic.disposition, DiagnosticDisposition):
        raise TypeError(
            "export diagnostic disposition must be DiagnosticDisposition"
        )
    if (
        diagnostic.disposition is DiagnosticDisposition.REJECTED
        and diagnostic.record_id is None
    ):
        raise ValueError("rejected export diagnostic requires a record id")
    _require_diagnostic_identifier(
        diagnostic.safe_summary,
        "export diagnostic safe summary",
    )


def _migration_diagnostic_key(
    diagnostic: MigrationDiagnostic,
) -> tuple[int, int, str, str, str, str]:
    return (
        diagnostic.line_number
        if diagnostic.line_number is not None
        else 2**63 - 1,
        diagnostic.record_id if diagnostic.record_id is not None else 2**63 - 1,
        diagnostic.stage,
        diagnostic.code,
        diagnostic.disposition.value,
        diagnostic.safe_summary,
    )


def _export_diagnostic_key(
    diagnostic: ExportDiagnostic,
) -> tuple[int, str, str, str]:
    return (
        diagnostic.record_id if diagnostic.record_id is not None else 2**63 - 1,
        diagnostic.code,
        diagnostic.disposition.value,
        diagnostic.safe_summary,
    )


def _migration_diagnostic_location(
    diagnostic: MigrationDiagnostic,
) -> tuple[str, int]:
    if diagnostic.line_number is not None:
        return ("LINE", diagnostic.line_number)
    if diagnostic.record_id is None:
        raise ValueError(
            "migration diagnostic requires a line or record location"
        )
    return ("RECORD", diagnostic.record_id)


def _export_diagnostic_location(
    diagnostic: ExportDiagnostic,
) -> tuple[str, int]:
    if diagnostic.record_id is None:
        raise ValueError("export diagnostic requires a record location")
    return ("RECORD", diagnostic.record_id)


def _require_migration_diagnostics(
    value: object,
) -> tuple[MigrationDiagnostic, ...]:
    diagnostics = _require_tuple(value, "migration diagnostics")
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, MigrationDiagnostic):
            raise TypeError(
                "migration diagnostics must contain MigrationDiagnostic values"
            )
        _validate_migration_diagnostic(diagnostic)
    keys = tuple(_migration_diagnostic_key(item) for item in diagnostics)
    if len(keys) != len(set(keys)):
        raise ValueError("migration diagnostics must be unique")
    if keys != tuple(sorted(keys)):
        raise ValueError("migration diagnostics must use stable order")
    rejected_locations = tuple(
        _migration_diagnostic_location(item)
        for item in diagnostics
        if item.disposition is DiagnosticDisposition.REJECTED
    )
    if len(rejected_locations) != len(set(rejected_locations)):
        raise ValueError(
            "rejected migration diagnostic locations must be unique"
        )
    return diagnostics


def _require_export_diagnostics(
    value: object,
) -> tuple[ExportDiagnostic, ...]:
    diagnostics = _require_tuple(value, "export diagnostics")
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, ExportDiagnostic):
            raise TypeError(
                "export diagnostics must contain ExportDiagnostic values"
            )
        _validate_export_diagnostic(diagnostic)
    keys = tuple(_export_diagnostic_key(item) for item in diagnostics)
    if len(keys) != len(set(keys)):
        raise ValueError("export diagnostics must be unique")
    if keys != tuple(sorted(keys)):
        raise ValueError("export diagnostics must use stable order")
    rejected_locations = tuple(
        _export_diagnostic_location(item)
        for item in diagnostics
        if item.disposition is DiagnosticDisposition.REJECTED
    )
    if len(rejected_locations) != len(set(rejected_locations)):
        raise ValueError(
            "rejected export diagnostic locations must be unique"
        )
    return diagnostics


def _rejected_migration_location_count(
    diagnostics: tuple[MigrationDiagnostic, ...],
) -> int:
    return len(
        {
            _migration_diagnostic_location(item)
            for item in diagnostics
            if item.disposition is DiagnosticDisposition.REJECTED
        }
    )


def _rejected_export_location_count(
    diagnostics: tuple[ExportDiagnostic, ...],
) -> int:
    return len(
        {
            _export_diagnostic_location(item)
            for item in diagnostics
            if item.disposition is DiagnosticDisposition.REJECTED
        }
    )


def _validate_preservation_and_recovery(
    *,
    evidence: tuple[AssetPreservationEvidence, ...],
    recovery_locators: tuple[RecoveryLocator, ...],
    retryable: bool,
    field_name: str,
) -> None:
    for item in evidence:
        if not isinstance(item, AssetPreservationEvidence):
            raise TypeError(
                f"{field_name} must contain AssetPreservationEvidence values"
            )
        _validate_asset_preservation_evidence(item)
    evidence_kinds = tuple(item.asset_kind for item in evidence)
    if len(evidence_kinds) != len(set(evidence_kinds)):
        raise ValueError(f"{field_name} asset kinds must be unique")

    locators = _require_tuple(recovery_locators, "recovery locators")
    for locator in locators:
        if not isinstance(locator, RecoveryLocator):
            raise TypeError(
                "recovery locators must contain RecoveryLocator values"
            )
        _validate_recovery_locator(locator)
    locator_kinds = tuple(locator.asset_kind for locator in locators)
    if len(locator_kinds) != len(set(locator_kinds)):
        raise ValueError("recovery locator asset kinds must be unique")
    if locator_kinds != tuple(sorted(locator_kinds, key=lambda item: item.value)):
        raise ValueError("recovery locators must use stable asset order")

    by_kind = {item.asset_kind: item for item in evidence}
    for locator in locators:
        preserved = by_kind.get(locator.asset_kind)
        if preserved is None or preserved.before_digest is None:
            raise ValueError(
                "recovery locator must correspond to a prior asset digest"
            )
        if locator.expected_digest != preserved.before_digest:
            raise ValueError(
                "recovery locator expected digest must match prior asset"
            )

    needs_recovery = tuple(
        item
        for item in evidence
        if item.state
        in (
            AssetPreservationState.VERIFIED_CHANGED,
            AssetPreservationState.UNVERIFIED,
        )
    )
    recovery_kinds = tuple(
        sorted(
            (item.asset_kind for item in needs_recovery),
            key=lambda item: item.value,
        )
    )
    if locator_kinds != recovery_kinds:
        if not recovery_kinds:
            raise ValueError(
                "recovery locators must be empty when preservation is verified"
            )
        raise ValueError(
            "recovery locator asset kinds must exactly match assets "
            "requiring recovery"
        )
    if needs_recovery and retryable:
        raise ValueError(
            "unproven asset preservation must be fail-stop and not retryable"
        )


@dataclass(frozen=True)
class MigrationPreflight:
    """Portable validation counts and safe row diagnostics."""

    source_digest: str
    valid_count: int
    invalid_count: int
    duplicate_source_count: int
    variant_count: int
    diagnostics: tuple[MigrationDiagnostic, ...]

    def __post_init__(self) -> None:
        _require_digest(self.source_digest, "migration source digest")
        _require_int(self.valid_count, "migration valid count", minimum=0)
        _require_int(self.invalid_count, "migration invalid count", minimum=0)
        _require_int(
            self.duplicate_source_count,
            "migration duplicate source count",
            minimum=0,
        )
        _require_int(self.variant_count, "migration variant count", minimum=0)
        if self.valid_count + self.invalid_count == 0:
            raise ValueError("migration preflight must describe at least one row")
        if self.duplicate_source_count > self.variant_count:
            raise ValueError(
                "migration duplicate source count cannot exceed variant count"
            )
        if self.variant_count > self.valid_count:
            raise ValueError(
                "migration variant count cannot exceed valid count"
            )
        diagnostics = _require_migration_diagnostics(self.diagnostics)
        if (
            _rejected_migration_location_count(diagnostics)
            != self.invalid_count
        ):
            raise ValueError(
                "migration invalid count must match rejected diagnostics"
            )


@dataclass(frozen=True)
class MigrationReport:
    """Successful migration evidence for one complete active generation."""

    resource_id: str
    canonical_store_id: str
    source_digest: str
    snapshot_receipt: SnapshotReceipt
    migrated_count: int
    variant_count: int
    skipped_count: int
    diagnostics: tuple[MigrationDiagnostic, ...]
    activated_generation: int
    canonical_exact_available: bool
    context_available: bool
    fuzzy_available: bool

    def __post_init__(self) -> None:
        _require_identity(self.resource_id, "migration resource id")
        _require_identity(
            self.canonical_store_id,
            "migration canonical store id",
        )
        _require_digest(self.source_digest, "migration source digest")
        if not isinstance(self.snapshot_receipt, SnapshotReceipt):
            raise TypeError("migration snapshot receipt must be SnapshotReceipt")
        _validate_snapshot_receipt(self.snapshot_receipt)
        if self.snapshot_receipt.resource_id != self.resource_id:
            raise ValueError("migration resource does not match receipt")
        if (
            self.snapshot_receipt.canonical_store_id
            != self.canonical_store_id
        ):
            raise ValueError("migration canonical store does not match receipt")
        if self.snapshot_receipt.jsonl_digest != self.source_digest:
            raise ValueError("migration source digest does not match receipt")
        _require_int(self.migrated_count, "migrated count", minimum=0)
        if self.snapshot_receipt.record_count != self.migrated_count:
            raise ValueError("migration record count does not match receipt")
        _require_int(self.variant_count, "migrated variant count", minimum=0)
        _require_int(self.skipped_count, "migration skipped count", minimum=0)
        if self.variant_count > self.migrated_count:
            raise ValueError(
                "migration variant count cannot exceed migrated count"
            )
        diagnostics = _require_migration_diagnostics(self.diagnostics)
        if (
            _rejected_migration_location_count(diagnostics)
            != self.skipped_count
        ):
            raise ValueError(
                "migration skipped count must match rejected diagnostics"
            )
        _require_int(
            self.activated_generation,
            "migration activated generation",
            minimum=0,
        )
        _require_bool(
            self.canonical_exact_available,
            "migration canonical exact availability",
        )
        _require_bool(self.context_available, "migration context availability")
        _require_bool(self.fuzzy_available, "migration fuzzy availability")
        if not self.canonical_exact_available:
            raise ValueError(
                "successful migration requires canonical exact availability"
            )


@dataclass(frozen=True)
class MigrationFailure:
    """Fail-stop migration result with explicit asset preservation facts."""

    stage: str
    error_code: str
    retryable: bool
    diagnostics: tuple[MigrationDiagnostic, ...]
    active_generation: int | None
    original_source_preservation: AssetPreservationEvidence
    active_store_preservation: AssetPreservationEvidence
    recovery_locators: tuple[RecoveryLocator, ...]

    def __post_init__(self) -> None:
        _require_diagnostic_identifier(self.stage, "migration failure stage")
        _require_diagnostic_identifier(
            self.error_code,
            "migration failure error code",
        )
        _require_bool(self.retryable, "migration failure retryable")
        _require_migration_diagnostics(self.diagnostics)
        if self.active_generation is not None:
            _require_int(
                self.active_generation,
                "migration active generation",
                minimum=0,
            )
        if not isinstance(
            self.original_source_preservation,
            AssetPreservationEvidence,
        ):
            raise TypeError(
                "migration source preservation must be "
                "AssetPreservationEvidence"
            )
        if not isinstance(
            self.active_store_preservation,
            AssetPreservationEvidence,
        ):
            raise TypeError(
                "migration store preservation must be "
                "AssetPreservationEvidence"
            )
        if (
            self.original_source_preservation.asset_kind
            is not AssetKind.ORIGINAL_SOURCE
        ):
            raise ValueError(
                "migration source preservation has the wrong asset kind"
            )
        if (
            self.original_source_preservation.state
            is AssetPreservationState.NOT_APPLICABLE
        ):
            raise ValueError(
                "migration requires original source preservation evidence"
            )
        if (
            self.active_store_preservation.asset_kind
            is not AssetKind.ACTIVE_STORE
        ):
            raise ValueError(
                "migration store preservation has the wrong asset kind"
            )
        if (
            self.active_generation is None
            and self.active_store_preservation.state
            is not AssetPreservationState.NOT_APPLICABLE
        ):
            raise ValueError(
                "first migration failure has no prior active store"
            )
        if (
            self.active_generation is not None
            and self.active_store_preservation.state
            is AssetPreservationState.NOT_APPLICABLE
        ):
            raise ValueError(
                "existing generation requires active store preservation"
            )
        _validate_preservation_and_recovery(
            evidence=(
                self.original_source_preservation,
                self.active_store_preservation,
            ),
            recovery_locators=self.recovery_locators,
            retryable=self.retryable,
            field_name="migration preservation evidence",
        )


@dataclass(frozen=True)
class ExportReport:
    """Successful compatible export and immutable snapshot evidence."""

    exported_count: int
    skipped_count: int
    destination_digest: str
    canonical_generation: int
    exported_revision: int
    snapshot_id: str
    snapshot_receipt_digest: str
    snapshot_receipt: SnapshotReceipt
    diagnostics: tuple[ExportDiagnostic, ...]

    def __post_init__(self) -> None:
        _require_int(self.exported_count, "exported count", minimum=0)
        _require_int(self.skipped_count, "export skipped count", minimum=0)
        _require_digest(self.destination_digest, "export destination digest")
        _require_int(
            self.canonical_generation,
            "export canonical generation",
            minimum=0,
        )
        _require_int(
            self.exported_revision,
            "exported revision",
            minimum=0,
        )
        _require_identity(self.snapshot_id, "export snapshot id")
        _require_digest(
            self.snapshot_receipt_digest,
            "export snapshot receipt digest",
        )
        if not isinstance(self.snapshot_receipt, SnapshotReceipt):
            raise TypeError("export snapshot receipt must be SnapshotReceipt")
        _validate_snapshot_receipt(self.snapshot_receipt)
        if self.snapshot_receipt.jsonl_digest != self.destination_digest:
            raise ValueError("export destination digest does not match receipt")
        if self.snapshot_receipt.record_count != self.exported_count:
            raise ValueError("exported count does not match receipt")
        if self.snapshot_receipt.exported_revision != self.exported_revision:
            raise ValueError("exported revision does not match receipt")
        if self.snapshot_receipt.snapshot_id != self.snapshot_id:
            raise ValueError("export snapshot id does not match receipt")
        if (
            snapshot_receipt_digest(self.snapshot_receipt)
            != self.snapshot_receipt_digest
        ):
            raise ValueError("export snapshot receipt digest does not match")
        diagnostics = _require_export_diagnostics(self.diagnostics)
        if _rejected_export_location_count(diagnostics) != self.skipped_count:
            raise ValueError(
                "export skipped count must match rejected diagnostics"
            )


@dataclass(frozen=True)
class ExportFailure:
    """Fail-stop export result with destination preservation evidence."""

    stage: str
    error_code: str
    retryable: bool
    diagnostics: tuple[ExportDiagnostic, ...]
    previous_destination_preservation: AssetPreservationEvidence
    recovery_locators: tuple[RecoveryLocator, ...]

    def __post_init__(self) -> None:
        _require_diagnostic_identifier(self.stage, "export failure stage")
        _require_diagnostic_identifier(
            self.error_code,
            "export failure error code",
        )
        _require_bool(self.retryable, "export failure retryable")
        _require_export_diagnostics(self.diagnostics)
        if not isinstance(
            self.previous_destination_preservation,
            AssetPreservationEvidence,
        ):
            raise TypeError(
                "export destination preservation must be "
                "AssetPreservationEvidence"
            )
        if (
            self.previous_destination_preservation.asset_kind
            is not AssetKind.EXPORT_DESTINATION
        ):
            raise ValueError(
                "export destination preservation has the wrong asset kind"
            )
        _validate_preservation_and_recovery(
            evidence=(self.previous_destination_preservation,),
            recovery_locators=self.recovery_locators,
            retryable=self.retryable,
            field_name="export preservation evidence",
        )


type MigrationOutcome = MigrationReport | MigrationFailure
type ExportOutcome = ExportReport | ExportFailure


@dataclass(frozen=True)
class SchemaUpgradeReport:
    """Successful monotonic schema upgrade with lineage and digest proof."""

    canonical_store_id: str
    from_version: int
    to_version: int
    backup_path: Path
    backup_digest: str
    success_digest: str
    activated_generation: int

    def __post_init__(self) -> None:
        _require_identity(
            self.canonical_store_id,
            "schema canonical store id",
        )
        _require_int(self.from_version, "schema from version", minimum=1)
        _require_int(self.to_version, "schema to version", minimum=1)
        if self.to_version <= self.from_version:
            raise ValueError(
                "schema to version must be greater than from version"
            )
        _require_absolute_path(self.backup_path, "schema backup path")
        _require_digest(self.backup_digest, "schema backup digest")
        _require_digest(self.success_digest, "schema success digest")
        if self.backup_digest == self.success_digest:
            raise ValueError("schema success and backup digests must differ")
        _require_int(
            self.activated_generation,
            "schema activated generation",
            minimum=0,
        )


@dataclass(frozen=True)
class SchemaUpgradeFailure:
    """Fail-stop schema upgrade result with active-store preservation."""

    stage: str
    error_code: str
    retryable: bool
    active_generation: int
    active_store_preservation: AssetPreservationEvidence
    recovery_locators: tuple[RecoveryLocator, ...]

    def __post_init__(self) -> None:
        _require_diagnostic_identifier(
            self.stage,
            "schema upgrade failure stage",
        )
        _require_diagnostic_identifier(
            self.error_code,
            "schema upgrade failure error code",
        )
        _require_bool(self.retryable, "schema upgrade failure retryable")
        _require_int(
            self.active_generation,
            "schema upgrade active generation",
            minimum=0,
        )
        if not isinstance(
            self.active_store_preservation,
            AssetPreservationEvidence,
        ):
            raise TypeError(
                "schema store preservation must be "
                "AssetPreservationEvidence"
            )
        if (
            self.active_store_preservation.asset_kind
            is not AssetKind.ACTIVE_STORE
        ):
            raise ValueError(
                "schema store preservation has the wrong asset kind"
            )
        if (
            self.active_store_preservation.state
            is AssetPreservationState.NOT_APPLICABLE
        ):
            raise ValueError(
                "schema upgrade requires active store preservation"
            )
        _validate_preservation_and_recovery(
            evidence=(self.active_store_preservation,),
            recovery_locators=self.recovery_locators,
            retryable=self.retryable,
            field_name="schema preservation evidence",
        )


type SchemaUpgradeOutcome = SchemaUpgradeReport | SchemaUpgradeFailure


class TextMatcherState(str, Enum):
    """Closed public readiness states derived by the Core evaluator."""

    UNAVAILABLE = "UNAVAILABLE"
    BASIC_VALIDATED = "BASIC_VALIDATED"
    TEXT_V1_VALIDATED = "TEXT_V1_VALIDATED"


class TextMatchProfile(str, Enum):
    """Explicit caller purposes; options alone never imply a purpose."""

    LEGACY_COMPAT = "LEGACY_COMPAT"
    BASIC_CONTIGUOUS = "BASIC_CONTIGUOUS"
    CONFIGURABLE_TEXT_V1 = "CONFIGURABLE_TEXT_V1"


class TextMatchRejectCode(str, Enum):
    """Closed, content-free reasons for fail-closed matcher outcomes."""

    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PROFILE_NOT_VALIDATED = "PROFILE_NOT_VALIDATED"
    OPTIONS_NOT_ALLOWED = "OPTIONS_NOT_ALLOWED"


@dataclass(frozen=True)
class SearchOptions:
    """Qt-neutral text match options."""

    match_case: bool
    whole_word: bool

    def __post_init__(self) -> None:
        _validate_search_options(self)


def _validate_search_options(options: SearchOptions) -> None:
    _require_bool(options.match_case, "search match_case")
    _require_bool(options.whole_word, "search whole_word")


@dataclass(frozen=True)
class SearchHit:
    """Non-empty half-open range into the original text."""

    start_index: int
    end_index: int

    def __post_init__(self) -> None:
        _validate_search_hit(self)


def _validate_search_hit(hit: SearchHit) -> None:
    _require_int(hit.start_index, "search hit start index", minimum=0)
    _require_int(hit.end_index, "search hit end index", minimum=0)
    if hit.end_index <= hit.start_index:
        raise ValueError(
            "search hit end index must be greater than start index"
        )


@dataclass(frozen=True)
class MatcherValidationSummary:
    """Opaque public checksum; it never carries readiness inputs."""

    summary_version: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_matcher_validation_summary(self)


def _validate_matcher_validation_summary(
    summary: MatcherValidationSummary,
) -> None:
    if summary.summary_version != MATCHER_VALIDATION_SUMMARY_VERSION:
        raise ValueError("unsupported matcher validation summary version")
    _require_digest(
        summary.evidence_digest,
        "matcher validation summary evidence digest",
    )


_BASIC_MATCHER_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
)
_TEXT_V1_MATCHER_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
    TextMatchProfile.CONFIGURABLE_TEXT_V1,
)


@dataclass(frozen=True)
class TextMatcherCapability:
    """One immutable Core capability decision exposed to consumers."""

    state: TextMatcherState
    semantics_version: str | None
    supported_profiles: tuple[TextMatchProfile, ...]
    validation_summary: MatcherValidationSummary | None
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        _validate_text_matcher_capability(self)


def _validate_text_matcher_capability(
    capability: TextMatcherCapability,
) -> None:
    if not isinstance(capability.state, TextMatcherState):
        raise TypeError("matcher capability state must be TextMatcherState")
    profiles = _require_tuple(
        capability.supported_profiles,
        "matcher supported profiles",
    )
    for profile in profiles:
        if not isinstance(profile, TextMatchProfile):
            raise TypeError(
                "matcher supported profiles must contain TextMatchProfile"
            )
    expected_profiles: tuple[TextMatchProfile, ...]
    if capability.state is TextMatcherState.UNAVAILABLE:
        expected_profiles = ()
    elif capability.state is TextMatcherState.BASIC_VALIDATED:
        expected_profiles = _BASIC_MATCHER_PROFILES
    else:
        expected_profiles = _TEXT_V1_MATCHER_PROFILES
    if profiles != expected_profiles:
        raise ValueError(
            "matcher supported profiles must exactly match capability state"
        )

    if capability.state is TextMatcherState.UNAVAILABLE:
        if (
            capability.semantics_version is not None
            or capability.validation_summary is not None
        ):
            raise ValueError(
                "unavailable matcher capability must omit semantics and "
                "validation summary"
            )
        if capability.unavailable_reason is None:
            raise ValueError(
                "unavailable matcher capability requires unavailable reason"
            )
        _require_diagnostic_identifier(
            capability.unavailable_reason,
            "matcher unavailable reason",
        )
        return

    if capability.semantics_version is None:
        raise ValueError(
            "available matcher capability requires semantics version"
        )
    _require_identity(
        capability.semantics_version,
        "matcher semantics version",
    )
    if not isinstance(
        capability.validation_summary,
        MatcherValidationSummary,
    ):
        raise ValueError(
            "available matcher capability requires validation summary"
        )
    _validate_matcher_validation_summary(capability.validation_summary)
    if capability.unavailable_reason is not None:
        raise ValueError(
            "available matcher capability must omit unavailable reason"
        )


@dataclass(frozen=True)
class TextMatchRequest:
    """Raw match input plus an explicit purpose and options."""

    text: str
    query: str
    profile: TextMatchProfile
    options: SearchOptions

    def __post_init__(self) -> None:
        _validate_text_match_request(self)

    @property
    def request_digest(self) -> str:
        """Opaque identity used to bind an outcome without echoing content."""

        _validate_text_match_request(self)
        return _stable_digest(
            {
                "options": {
                    "match_case": self.options.match_case,
                    "whole_word": self.options.whole_word,
                },
                "profile": self.profile.value,
                "query": self.query,
                "request_version": "text-match-request-v1",
                "text": self.text,
            }
        )


def _validate_text_match_request(request: TextMatchRequest) -> None:
    if not isinstance(request.text, str):
        raise TypeError("text must be a string")
    if not isinstance(request.query, str):
        raise TypeError("query must be a string")
    if not isinstance(request.profile, TextMatchProfile):
        raise TypeError("text match profile must be TextMatchProfile")
    if not isinstance(request.options, SearchOptions):
        raise TypeError("text match request options must be SearchOptions")
    _validate_search_options(request.options)


def _text_match_matrix_reject_code(
    *,
    capability: TextMatcherCapability,
    profile: TextMatchProfile,
    options: SearchOptions,
) -> TextMatchRejectCode | None:
    _validate_text_matcher_capability(capability)
    if not isinstance(profile, TextMatchProfile):
        raise TypeError("text match outcome profile must be TextMatchProfile")
    if not isinstance(options, SearchOptions):
        raise TypeError("text match outcome options must be SearchOptions")
    _validate_search_options(options)
    if capability.state is TextMatcherState.UNAVAILABLE:
        return TextMatchRejectCode.CAPABILITY_UNAVAILABLE
    if profile not in capability.supported_profiles:
        return TextMatchRejectCode.PROFILE_NOT_VALIDATED
    expected_options: SearchOptions | None
    if profile is TextMatchProfile.LEGACY_COMPAT:
        expected_options = SearchOptions(match_case=True, whole_word=False)
    elif profile is TextMatchProfile.BASIC_CONTIGUOUS:
        expected_options = SearchOptions(match_case=False, whole_word=False)
    else:
        expected_options = None
    if expected_options is not None and options != expected_options:
        return TextMatchRejectCode.OPTIONS_NOT_ALLOWED
    return None


def _require_search_hits(value: object) -> tuple[SearchHit, ...]:
    hits = _require_tuple(value, "text match success hits")
    for hit in hits:
        if not isinstance(hit, SearchHit):
            raise TypeError(
                "text match success hits must contain SearchHit values"
            )
        _validate_search_hit(hit)
    keys = tuple((hit.start_index, hit.end_index) for hit in hits)
    if len(keys) != len(set(keys)):
        raise ValueError("text match success hits must be unique")
    if keys != tuple(sorted(keys)):
        raise ValueError("text match success hits must use stable order")
    return hits


@dataclass(frozen=True)
class TextMatchSuccess:
    """Authorized hits bound to one request and capability snapshot."""

    hits: tuple[SearchHit, ...]
    request_profile: TextMatchProfile
    request_options: SearchOptions
    request_digest: str
    capability: TextMatcherCapability

    def __post_init__(self) -> None:
        _validate_text_match_success(self)


def _validate_text_match_success(success: TextMatchSuccess) -> None:
    _require_search_hits(success.hits)
    _require_digest(success.request_digest, "text match request digest")
    if not isinstance(success.capability, TextMatcherCapability):
        raise TypeError(
            "text match success capability must be TextMatcherCapability"
        )
    reject_code = _text_match_matrix_reject_code(
        capability=success.capability,
        profile=success.request_profile,
        options=success.request_options,
    )
    if reject_code is not None:
        raise ValueError(
            "text match success request is not authorized by capability"
        )


@dataclass(frozen=True)
class TextMatchRejected:
    """Content-free rejection bound to one request and capability snapshot."""

    code: TextMatchRejectCode
    safe_reason: str
    request_profile: TextMatchProfile
    request_options: SearchOptions
    request_digest: str
    capability: TextMatcherCapability

    def __post_init__(self) -> None:
        _validate_text_match_rejected(self)


def _validate_text_match_rejected(rejected: TextMatchRejected) -> None:
    if not isinstance(rejected.code, TextMatchRejectCode):
        raise TypeError("text match reject code must be TextMatchRejectCode")
    _require_digest(rejected.request_digest, "text match request digest")
    if not isinstance(rejected.capability, TextMatcherCapability):
        raise TypeError(
            "text match rejection capability must be TextMatcherCapability"
        )
    expected_code = _text_match_matrix_reject_code(
        capability=rejected.capability,
        profile=rejected.request_profile,
        options=rejected.request_options,
    )
    if expected_code is None:
        raise ValueError("authorized text match request cannot be rejected")
    if rejected.code is not expected_code:
        raise ValueError(
            "text match reject code does not match capability decision"
        )
    _require_diagnostic_identifier(
        rejected.safe_reason,
        "text match safe reason",
    )
    if rejected.safe_reason != f"MATCHER.{rejected.code.value}":
        raise ValueError("text match safe reason must derive from reject code")


type TextMatchOutcome = TextMatchSuccess | TextMatchRejected


class CapabilityGatedTextMatcher(Protocol):
    """Runtime-only public matcher port; implementations arrive in task 2.5."""

    def capability(self) -> TextMatcherCapability: ...

    def match(self, request: TextMatchRequest) -> TextMatchOutcome: ...


@dataclass(frozen=True)
class MatcherValidationCohortEvidence:
    """Internal portable evidence for one required validation cohort."""

    cohort_id: str
    cohort_digest: str
    passed: bool

    def __post_init__(self) -> None:
        _validate_matcher_validation_cohort_evidence(self)


def _validate_matcher_validation_cohort_evidence(
    evidence: MatcherValidationCohortEvidence,
) -> None:
    _require_identity(evidence.cohort_id, "matcher cohort id")
    _require_digest(evidence.cohort_digest, "matcher cohort digest")
    _require_bool(evidence.passed, "matcher cohort passed")


def _parse_strict_utc_timestamp(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, str)
        or _STRICT_UTC_TIMESTAMP.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a strict UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError(f"{field_name} must be a strict UTC timestamp") from None


@dataclass(frozen=True)
class MatcherValidationManifest:
    """Internal evidence input; it does not itself grant capability."""

    evidence_schema_version: str
    matcher_artifact_digest: str
    matcher_build_digest: str
    semantics_version: str
    required_cohort_ids: tuple[str, ...]
    cohort_evidence: tuple[MatcherValidationCohortEvidence, ...]
    fixture_digest: str
    evaluator_digest: str
    generated_at_utc: str
    valid_until_utc: str

    def __post_init__(self) -> None:
        _validate_matcher_validation_manifest(self)


def _validate_matcher_validation_manifest(
    manifest: MatcherValidationManifest,
) -> None:
    if (
        manifest.evidence_schema_version
        != MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported matcher evidence schema version")
    _require_digest(
        manifest.matcher_artifact_digest,
        "matcher artifact digest",
    )
    _require_digest(manifest.matcher_build_digest, "matcher build digest")
    _require_identity(manifest.semantics_version, "matcher semantics version")
    required_ids = _require_string_tuple(
        manifest.required_cohort_ids,
        "matcher required cohort ids",
        identities=True,
    )
    if not required_ids:
        raise ValueError("matcher required cohort ids must not be empty")
    if required_ids != tuple(sorted(required_ids)):
        raise ValueError("matcher required cohort ids must use stable order")

    evidence_items = _require_tuple(
        manifest.cohort_evidence,
        "matcher cohort evidence",
    )
    for evidence in evidence_items:
        if not isinstance(evidence, MatcherValidationCohortEvidence):
            raise TypeError(
                "matcher cohort evidence must contain "
                "MatcherValidationCohortEvidence values"
            )
        _validate_matcher_validation_cohort_evidence(evidence)
    evidence_ids = tuple(item.cohort_id for item in evidence_items)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("matcher cohort evidence ids must be unique")
    if evidence_ids != tuple(sorted(evidence_ids)):
        raise ValueError("matcher cohort evidence must use stable order")
    if evidence_ids != required_ids:
        raise ValueError(
            "matcher required cohort ids must exactly match cohort evidence ids"
        )

    _require_digest(manifest.fixture_digest, "matcher fixture digest")
    _require_digest(manifest.evaluator_digest, "matcher evaluator digest")
    generated = _parse_strict_utc_timestamp(
        manifest.generated_at_utc,
        "matcher generated_at_utc",
    )
    valid_until = _parse_strict_utc_timestamp(
        manifest.valid_until_utc,
        "matcher valid_until_utc",
    )
    if valid_until <= generated:
        raise ValueError(
            "matcher valid_until_utc must be later than generated_at_utc"
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
    | AssetPreservationEvidence
    | RecoveryLocator
    | MigrationDiagnostic
    | ExportDiagnostic
    | MigrationPreflight
    | MigrationReport
    | MigrationFailure
    | ExportReport
    | ExportFailure
    | SchemaUpgradeReport
    | SchemaUpgradeFailure
    | SearchOptions
    | SearchHit
    | MatcherValidationSummary
    | TextMatcherCapability
    | TextMatchRequest
    | TextMatchSuccess
    | TextMatchRejected
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


def _encode_asset_preservation_evidence(
    evidence: AssetPreservationEvidence,
) -> dict[str, Any]:
    return {
        "asset_kind": evidence.asset_kind.value,
        "before_digest": evidence.before_digest,
        "observed_digest": evidence.observed_digest,
        "state": evidence.state.value,
    }


def _encode_recovery_locator(locator: RecoveryLocator) -> dict[str, Any]:
    return {
        "asset_kind": locator.asset_kind.value,
        "expected_digest": locator.expected_digest,
        "path": str(locator.path),
    }


def _encode_migration_diagnostic(
    diagnostic: MigrationDiagnostic,
) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "disposition": diagnostic.disposition.value,
        "line_number": diagnostic.line_number,
        "record_id": diagnostic.record_id,
        "safe_summary": diagnostic.safe_summary,
        "stage": diagnostic.stage,
    }


def _encode_export_diagnostic(
    diagnostic: ExportDiagnostic,
) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "disposition": diagnostic.disposition.value,
        "record_id": diagnostic.record_id,
        "safe_summary": diagnostic.safe_summary,
    }


def _encode_migration_preflight(
    preflight: MigrationPreflight,
) -> dict[str, Any]:
    return {
        "diagnostics": [
            _encode_migration_diagnostic(diagnostic)
            for diagnostic in preflight.diagnostics
        ],
        "duplicate_source_count": preflight.duplicate_source_count,
        "invalid_count": preflight.invalid_count,
        "source_digest": preflight.source_digest,
        "valid_count": preflight.valid_count,
        "variant_count": preflight.variant_count,
    }


def _encode_migration_report(report: MigrationReport) -> dict[str, Any]:
    return {
        "activated_generation": report.activated_generation,
        "canonical_store_id": report.canonical_store_id,
        "canonical_exact_available": report.canonical_exact_available,
        "context_available": report.context_available,
        "diagnostics": [
            _encode_migration_diagnostic(diagnostic)
            for diagnostic in report.diagnostics
        ],
        "fuzzy_available": report.fuzzy_available,
        "migrated_count": report.migrated_count,
        "resource_id": report.resource_id,
        "skipped_count": report.skipped_count,
        "snapshot_receipt": _encode_snapshot_receipt(
            report.snapshot_receipt
        ),
        "source_digest": report.source_digest,
        "variant_count": report.variant_count,
    }


def _encode_migration_failure(
    failure: MigrationFailure,
) -> dict[str, Any]:
    return {
        "active_generation": failure.active_generation,
        "active_store_preservation": (
            _encode_asset_preservation_evidence(
                failure.active_store_preservation
            )
        ),
        "diagnostics": [
            _encode_migration_diagnostic(diagnostic)
            for diagnostic in failure.diagnostics
        ],
        "error_code": failure.error_code,
        "original_source_preservation": (
            _encode_asset_preservation_evidence(
                failure.original_source_preservation
            )
        ),
        "recovery_locators": [
            _encode_recovery_locator(locator)
            for locator in failure.recovery_locators
        ],
        "retryable": failure.retryable,
        "stage": failure.stage,
    }


def _encode_export_report(report: ExportReport) -> dict[str, Any]:
    return {
        "canonical_generation": report.canonical_generation,
        "destination_digest": report.destination_digest,
        "diagnostics": [
            _encode_export_diagnostic(diagnostic)
            for diagnostic in report.diagnostics
        ],
        "exported_count": report.exported_count,
        "exported_revision": report.exported_revision,
        "skipped_count": report.skipped_count,
        "snapshot_id": report.snapshot_id,
        "snapshot_receipt": _encode_snapshot_receipt(
            report.snapshot_receipt
        ),
        "snapshot_receipt_digest": report.snapshot_receipt_digest,
    }


def _encode_export_failure(failure: ExportFailure) -> dict[str, Any]:
    return {
        "diagnostics": [
            _encode_export_diagnostic(diagnostic)
            for diagnostic in failure.diagnostics
        ],
        "error_code": failure.error_code,
        "previous_destination_preservation": (
            _encode_asset_preservation_evidence(
                failure.previous_destination_preservation
            )
        ),
        "recovery_locators": [
            _encode_recovery_locator(locator)
            for locator in failure.recovery_locators
        ],
        "retryable": failure.retryable,
        "stage": failure.stage,
    }


def _encode_schema_upgrade_report(
    report: SchemaUpgradeReport,
) -> dict[str, Any]:
    return {
        "activated_generation": report.activated_generation,
        "backup_digest": report.backup_digest,
        "backup_path": str(report.backup_path),
        "canonical_store_id": report.canonical_store_id,
        "from_version": report.from_version,
        "success_digest": report.success_digest,
        "to_version": report.to_version,
    }


def _encode_schema_upgrade_failure(
    failure: SchemaUpgradeFailure,
) -> dict[str, Any]:
    return {
        "active_generation": failure.active_generation,
        "active_store_preservation": (
            _encode_asset_preservation_evidence(
                failure.active_store_preservation
            )
        ),
        "error_code": failure.error_code,
        "recovery_locators": [
            _encode_recovery_locator(locator)
            for locator in failure.recovery_locators
        ],
        "retryable": failure.retryable,
        "stage": failure.stage,
    }


def _encode_search_options(options: SearchOptions) -> dict[str, Any]:
    return {
        "match_case": options.match_case,
        "whole_word": options.whole_word,
    }


def _encode_search_hit(hit: SearchHit) -> dict[str, Any]:
    return {
        "end_index": hit.end_index,
        "start_index": hit.start_index,
    }


def _encode_matcher_validation_summary(
    summary: MatcherValidationSummary,
) -> dict[str, Any]:
    return {
        "evidence_digest": summary.evidence_digest,
        "summary_version": summary.summary_version,
    }


def _encode_text_matcher_capability(
    capability: TextMatcherCapability,
) -> dict[str, Any]:
    return {
        "semantics_version": capability.semantics_version,
        "state": capability.state.value,
        "supported_profiles": [
            profile.value for profile in capability.supported_profiles
        ],
        "unavailable_reason": capability.unavailable_reason,
        "validation_summary": (
            None
            if capability.validation_summary is None
            else _encode_matcher_validation_summary(
                capability.validation_summary
            )
        ),
    }


def _encode_text_match_request(
    request: TextMatchRequest,
) -> dict[str, Any]:
    return {
        "options": _encode_search_options(request.options),
        "profile": request.profile.value,
        "query": request.query,
        "text": request.text,
    }


def _encode_text_match_success(
    success: TextMatchSuccess,
) -> dict[str, Any]:
    return {
        "capability": _encode_text_matcher_capability(success.capability),
        "hits": [_encode_search_hit(hit) for hit in success.hits],
        "request_digest": success.request_digest,
        "request_options": _encode_search_options(success.request_options),
        "request_profile": success.request_profile.value,
    }


def _encode_text_match_rejected(
    rejected: TextMatchRejected,
) -> dict[str, Any]:
    return {
        "capability": _encode_text_matcher_capability(rejected.capability),
        "code": rejected.code.value,
        "request_digest": rejected.request_digest,
        "request_options": _encode_search_options(rejected.request_options),
        "request_profile": rejected.request_profile.value,
        "safe_reason": rejected.safe_reason,
    }


def _encode_matcher_validation_cohort_evidence(
    evidence: MatcherValidationCohortEvidence,
) -> dict[str, Any]:
    return {
        "cohort_digest": evidence.cohort_digest,
        "cohort_id": evidence.cohort_id,
        "passed": evidence.passed,
    }


def _encode_matcher_validation_manifest(
    manifest: MatcherValidationManifest,
) -> dict[str, Any]:
    return {
        "cohort_evidence": [
            _encode_matcher_validation_cohort_evidence(evidence)
            for evidence in manifest.cohort_evidence
        ],
        "evaluator_digest": manifest.evaluator_digest,
        "evidence_schema_version": manifest.evidence_schema_version,
        "fixture_digest": manifest.fixture_digest,
        "generated_at_utc": manifest.generated_at_utc,
        "matcher_artifact_digest": manifest.matcher_artifact_digest,
        "matcher_build_digest": manifest.matcher_build_digest,
        "required_cohort_ids": list(manifest.required_cohort_ids),
        "semantics_version": manifest.semantics_version,
        "valid_until_utc": manifest.valid_until_utc,
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
    if isinstance(contract, AssetPreservationEvidence):
        _validate_asset_preservation_evidence(contract)
        return (
            "AssetPreservationEvidence",
            _encode_asset_preservation_evidence(contract),
        )
    if isinstance(contract, RecoveryLocator):
        _validate_recovery_locator(contract)
        return "RecoveryLocator", _encode_recovery_locator(contract)
    if isinstance(contract, MigrationDiagnostic):
        _validate_migration_diagnostic(contract)
        return (
            "MigrationDiagnostic",
            _encode_migration_diagnostic(contract),
        )
    if isinstance(contract, ExportDiagnostic):
        _validate_export_diagnostic(contract)
        return "ExportDiagnostic", _encode_export_diagnostic(contract)
    if isinstance(contract, MigrationPreflight):
        contract.__post_init__()
        return "MigrationPreflight", _encode_migration_preflight(contract)
    if isinstance(contract, MigrationReport):
        contract.__post_init__()
        return "MigrationReport", _encode_migration_report(contract)
    if isinstance(contract, MigrationFailure):
        contract.__post_init__()
        return "MigrationFailure", _encode_migration_failure(contract)
    if isinstance(contract, ExportReport):
        contract.__post_init__()
        return "ExportReport", _encode_export_report(contract)
    if isinstance(contract, ExportFailure):
        contract.__post_init__()
        return "ExportFailure", _encode_export_failure(contract)
    if isinstance(contract, SchemaUpgradeReport):
        contract.__post_init__()
        return (
            "SchemaUpgradeReport",
            _encode_schema_upgrade_report(contract),
        )
    if isinstance(contract, SchemaUpgradeFailure):
        contract.__post_init__()
        return (
            "SchemaUpgradeFailure",
            _encode_schema_upgrade_failure(contract),
        )
    if isinstance(contract, SearchOptions):
        _validate_search_options(contract)
        return "SearchOptions", _encode_search_options(contract)
    if isinstance(contract, SearchHit):
        _validate_search_hit(contract)
        return "SearchHit", _encode_search_hit(contract)
    if isinstance(contract, MatcherValidationSummary):
        _validate_matcher_validation_summary(contract)
        return (
            "MatcherValidationSummary",
            _encode_matcher_validation_summary(contract),
        )
    if isinstance(contract, TextMatcherCapability):
        _validate_text_matcher_capability(contract)
        return (
            "TextMatcherCapability",
            _encode_text_matcher_capability(contract),
        )
    if isinstance(contract, TextMatchRequest):
        _validate_text_match_request(contract)
        return "TextMatchRequest", _encode_text_match_request(contract)
    if isinstance(contract, TextMatchSuccess):
        _validate_text_match_success(contract)
        return "TextMatchSuccess", _encode_text_match_success(contract)
    if isinstance(contract, TextMatchRejected):
        _validate_text_match_rejected(contract)
        return (
            "TextMatchRejected",
            _encode_text_match_rejected(contract),
        )
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


def _decode_text_matcher_state(value: object) -> TextMatcherState:
    try:
        return TextMatcherState(value)
    except (TypeError, ValueError):
        raise ValueError("text matcher state is invalid") from None


def _decode_text_match_profile(value: object) -> TextMatchProfile:
    try:
        return TextMatchProfile(value)
    except (TypeError, ValueError):
        raise ValueError("text match profile is invalid") from None


def _decode_text_match_reject_code(value: object) -> TextMatchRejectCode:
    try:
        return TextMatchRejectCode(value)
    except (TypeError, ValueError):
        raise ValueError("text match reject code is invalid") from None


def _decode_search_options(value: object) -> SearchOptions:
    payload = _strict_fields(
        value,
        "SearchOptions payload",
        ("match_case", "whole_word"),
    )
    return SearchOptions(
        match_case=payload["match_case"],
        whole_word=payload["whole_word"],
    )


def _decode_search_hit(value: object) -> SearchHit:
    payload = _strict_fields(
        value,
        "SearchHit payload",
        ("end_index", "start_index"),
    )
    return SearchHit(
        start_index=payload["start_index"],
        end_index=payload["end_index"],
    )


def _decode_matcher_validation_summary(
    value: object,
) -> MatcherValidationSummary:
    payload = _strict_fields(
        value,
        "MatcherValidationSummary payload",
        ("evidence_digest", "summary_version"),
    )
    return MatcherValidationSummary(
        summary_version=payload["summary_version"],
        evidence_digest=payload["evidence_digest"],
    )


def _decode_text_matcher_capability(
    value: object,
) -> TextMatcherCapability:
    payload = _strict_fields(
        value,
        "TextMatcherCapability payload",
        (
            "semantics_version",
            "state",
            "supported_profiles",
            "unavailable_reason",
            "validation_summary",
        ),
    )
    summary_payload = payload["validation_summary"]
    return TextMatcherCapability(
        state=_decode_text_matcher_state(payload["state"]),
        semantics_version=payload["semantics_version"],
        supported_profiles=tuple(
            _decode_text_match_profile(profile)
            for profile in _as_list(
                payload["supported_profiles"],
                "matcher supported profiles",
            )
        ),
        validation_summary=(
            None
            if summary_payload is None
            else _decode_matcher_validation_summary(summary_payload)
        ),
        unavailable_reason=payload["unavailable_reason"],
    )


def _decode_text_match_request(value: object) -> TextMatchRequest:
    payload = _strict_fields(
        value,
        "TextMatchRequest payload",
        ("options", "profile", "query", "text"),
    )
    return TextMatchRequest(
        text=payload["text"],
        query=payload["query"],
        profile=_decode_text_match_profile(payload["profile"]),
        options=_decode_search_options(payload["options"]),
    )


def _decode_text_match_success(value: object) -> TextMatchSuccess:
    payload = _strict_fields(
        value,
        "TextMatchSuccess payload",
        (
            "capability",
            "hits",
            "request_digest",
            "request_options",
            "request_profile",
        ),
    )
    return TextMatchSuccess(
        hits=tuple(
            _decode_search_hit(hit)
            for hit in _as_list(payload["hits"], "text match success hits")
        ),
        request_profile=_decode_text_match_profile(
            payload["request_profile"]
        ),
        request_options=_decode_search_options(payload["request_options"]),
        request_digest=payload["request_digest"],
        capability=_decode_text_matcher_capability(payload["capability"]),
    )


def _decode_text_match_rejected(value: object) -> TextMatchRejected:
    payload = _strict_fields(
        value,
        "TextMatchRejected payload",
        (
            "capability",
            "code",
            "request_digest",
            "request_options",
            "request_profile",
            "safe_reason",
        ),
    )
    return TextMatchRejected(
        code=_decode_text_match_reject_code(payload["code"]),
        safe_reason=payload["safe_reason"],
        request_profile=_decode_text_match_profile(
            payload["request_profile"]
        ),
        request_options=_decode_search_options(payload["request_options"]),
        request_digest=payload["request_digest"],
        capability=_decode_text_matcher_capability(payload["capability"]),
    )


def _decode_matcher_validation_cohort_evidence(
    value: object,
) -> MatcherValidationCohortEvidence:
    payload = _strict_fields(
        value,
        "matcher cohort evidence",
        ("cohort_digest", "cohort_id", "passed"),
    )
    return MatcherValidationCohortEvidence(
        cohort_id=payload["cohort_id"],
        cohort_digest=payload["cohort_digest"],
        passed=payload["passed"],
    )


def _decode_matcher_validation_manifest(
    value: object,
) -> MatcherValidationManifest:
    payload = _strict_fields(
        value,
        "matcher validation manifest",
        (
            "cohort_evidence",
            "evaluator_digest",
            "evidence_schema_version",
            "fixture_digest",
            "generated_at_utc",
            "matcher_artifact_digest",
            "matcher_build_digest",
            "required_cohort_ids",
            "semantics_version",
            "valid_until_utc",
        ),
    )
    return MatcherValidationManifest(
        evidence_schema_version=payload["evidence_schema_version"],
        matcher_artifact_digest=payload["matcher_artifact_digest"],
        matcher_build_digest=payload["matcher_build_digest"],
        semantics_version=payload["semantics_version"],
        required_cohort_ids=_decode_string_tuple(
            payload["required_cohort_ids"],
            "matcher required cohort ids",
        ),
        cohort_evidence=tuple(
            _decode_matcher_validation_cohort_evidence(evidence)
            for evidence in _as_list(
                payload["cohort_evidence"],
                "matcher cohort evidence",
            )
        ),
        fixture_digest=payload["fixture_digest"],
        evaluator_digest=payload["evaluator_digest"],
        generated_at_utc=payload["generated_at_utc"],
        valid_until_utc=payload["valid_until_utc"],
    )


def _decode_asset_kind(value: object, field_name: str) -> AssetKind:
    try:
        return AssetKind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} is invalid") from None


def _decode_asset_preservation_state(
    value: object,
) -> AssetPreservationState:
    try:
        return AssetPreservationState(value)
    except (TypeError, ValueError):
        raise ValueError("asset preservation state is invalid") from None


def _decode_diagnostic_disposition(
    value: object,
) -> DiagnosticDisposition:
    try:
        return DiagnosticDisposition(value)
    except (TypeError, ValueError):
        raise ValueError("diagnostic disposition is invalid") from None


def _decode_asset_preservation_evidence(
    value: object,
) -> AssetPreservationEvidence:
    payload = _strict_fields(
        value,
        "AssetPreservationEvidence payload",
        ("asset_kind", "before_digest", "observed_digest", "state"),
    )
    return AssetPreservationEvidence(
        asset_kind=_decode_asset_kind(
            payload["asset_kind"],
            "asset preservation kind",
        ),
        state=_decode_asset_preservation_state(payload["state"]),
        before_digest=payload["before_digest"],
        observed_digest=payload["observed_digest"],
    )


def _decode_recovery_locator(value: object) -> RecoveryLocator:
    payload = _strict_fields(
        value,
        "RecoveryLocator payload",
        ("asset_kind", "expected_digest", "path"),
    )
    return RecoveryLocator(
        path=_decode_path(payload["path"], "RecoveryLocator path"),
        asset_kind=_decode_asset_kind(
            payload["asset_kind"],
            "recovery locator asset kind",
        ),
        expected_digest=payload["expected_digest"],
    )


def _decode_recovery_locators(
    value: object,
) -> tuple[RecoveryLocator, ...]:
    return tuple(
        _decode_recovery_locator(locator)
        for locator in _as_list(value, "recovery locators")
    )


def _decode_migration_diagnostic(value: object) -> MigrationDiagnostic:
    payload = _strict_fields(
        value,
        "MigrationDiagnostic payload",
        (
            "code",
            "disposition",
            "line_number",
            "record_id",
            "safe_summary",
            "stage",
        ),
    )
    return MigrationDiagnostic(
        code=payload["code"],
        stage=payload["stage"],
        line_number=payload["line_number"],
        record_id=payload["record_id"],
        disposition=_decode_diagnostic_disposition(
            payload["disposition"]
        ),
        safe_summary=payload["safe_summary"],
    )


def _decode_export_diagnostic(value: object) -> ExportDiagnostic:
    payload = _strict_fields(
        value,
        "ExportDiagnostic payload",
        ("code", "disposition", "record_id", "safe_summary"),
    )
    return ExportDiagnostic(
        code=payload["code"],
        record_id=payload["record_id"],
        disposition=_decode_diagnostic_disposition(
            payload["disposition"]
        ),
        safe_summary=payload["safe_summary"],
    )


def _decode_migration_diagnostics(
    value: object,
) -> tuple[MigrationDiagnostic, ...]:
    return tuple(
        _decode_migration_diagnostic(diagnostic)
        for diagnostic in _as_list(value, "migration diagnostics")
    )


def _decode_export_diagnostics(
    value: object,
) -> tuple[ExportDiagnostic, ...]:
    return tuple(
        _decode_export_diagnostic(diagnostic)
        for diagnostic in _as_list(value, "export diagnostics")
    )


def _decode_migration_preflight(value: object) -> MigrationPreflight:
    payload = _strict_fields(
        value,
        "MigrationPreflight payload",
        (
            "diagnostics",
            "duplicate_source_count",
            "invalid_count",
            "source_digest",
            "valid_count",
            "variant_count",
        ),
    )
    return MigrationPreflight(
        source_digest=payload["source_digest"],
        valid_count=payload["valid_count"],
        invalid_count=payload["invalid_count"],
        duplicate_source_count=payload["duplicate_source_count"],
        variant_count=payload["variant_count"],
        diagnostics=_decode_migration_diagnostics(payload["diagnostics"]),
    )


def _decode_migration_report(value: object) -> MigrationReport:
    payload = _strict_fields(
        value,
        "MigrationReport payload",
        (
            "activated_generation",
            "canonical_store_id",
            "canonical_exact_available",
            "context_available",
            "diagnostics",
            "fuzzy_available",
            "migrated_count",
            "resource_id",
            "skipped_count",
            "snapshot_receipt",
            "source_digest",
            "variant_count",
        ),
    )
    return MigrationReport(
        resource_id=payload["resource_id"],
        canonical_store_id=payload["canonical_store_id"],
        source_digest=payload["source_digest"],
        snapshot_receipt=_decode_snapshot_receipt(
            payload["snapshot_receipt"]
        ),
        migrated_count=payload["migrated_count"],
        variant_count=payload["variant_count"],
        skipped_count=payload["skipped_count"],
        diagnostics=_decode_migration_diagnostics(payload["diagnostics"]),
        activated_generation=payload["activated_generation"],
        canonical_exact_available=payload["canonical_exact_available"],
        context_available=payload["context_available"],
        fuzzy_available=payload["fuzzy_available"],
    )


def _decode_migration_failure(value: object) -> MigrationFailure:
    payload = _strict_fields(
        value,
        "MigrationFailure payload",
        (
            "active_generation",
            "active_store_preservation",
            "diagnostics",
            "error_code",
            "original_source_preservation",
            "recovery_locators",
            "retryable",
            "stage",
        ),
    )
    return MigrationFailure(
        stage=payload["stage"],
        error_code=payload["error_code"],
        retryable=payload["retryable"],
        diagnostics=_decode_migration_diagnostics(payload["diagnostics"]),
        active_generation=payload["active_generation"],
        original_source_preservation=(
            _decode_asset_preservation_evidence(
                payload["original_source_preservation"]
            )
        ),
        active_store_preservation=_decode_asset_preservation_evidence(
            payload["active_store_preservation"]
        ),
        recovery_locators=_decode_recovery_locators(
            payload["recovery_locators"]
        ),
    )


def _decode_export_report(value: object) -> ExportReport:
    payload = _strict_fields(
        value,
        "ExportReport payload",
        (
            "canonical_generation",
            "destination_digest",
            "diagnostics",
            "exported_count",
            "exported_revision",
            "skipped_count",
            "snapshot_id",
            "snapshot_receipt",
            "snapshot_receipt_digest",
        ),
    )
    return ExportReport(
        exported_count=payload["exported_count"],
        skipped_count=payload["skipped_count"],
        destination_digest=payload["destination_digest"],
        canonical_generation=payload["canonical_generation"],
        exported_revision=payload["exported_revision"],
        snapshot_id=payload["snapshot_id"],
        snapshot_receipt_digest=payload["snapshot_receipt_digest"],
        snapshot_receipt=_decode_snapshot_receipt(
            payload["snapshot_receipt"]
        ),
        diagnostics=_decode_export_diagnostics(payload["diagnostics"]),
    )


def _decode_export_failure(value: object) -> ExportFailure:
    payload = _strict_fields(
        value,
        "ExportFailure payload",
        (
            "diagnostics",
            "error_code",
            "previous_destination_preservation",
            "recovery_locators",
            "retryable",
            "stage",
        ),
    )
    return ExportFailure(
        stage=payload["stage"],
        error_code=payload["error_code"],
        retryable=payload["retryable"],
        diagnostics=_decode_export_diagnostics(payload["diagnostics"]),
        previous_destination_preservation=(
            _decode_asset_preservation_evidence(
                payload["previous_destination_preservation"]
            )
        ),
        recovery_locators=_decode_recovery_locators(
            payload["recovery_locators"]
        ),
    )


def _decode_schema_upgrade_report(value: object) -> SchemaUpgradeReport:
    payload = _strict_fields(
        value,
        "SchemaUpgradeReport payload",
        (
            "activated_generation",
            "backup_digest",
            "backup_path",
            "canonical_store_id",
            "from_version",
            "success_digest",
            "to_version",
        ),
    )
    return SchemaUpgradeReport(
        canonical_store_id=payload["canonical_store_id"],
        from_version=payload["from_version"],
        to_version=payload["to_version"],
        backup_path=_decode_path(
            payload["backup_path"],
            "SchemaUpgradeReport backup_path",
        ),
        backup_digest=payload["backup_digest"],
        success_digest=payload["success_digest"],
        activated_generation=payload["activated_generation"],
    )


def _decode_schema_upgrade_failure(
    value: object,
) -> SchemaUpgradeFailure:
    payload = _strict_fields(
        value,
        "SchemaUpgradeFailure payload",
        (
            "active_generation",
            "active_store_preservation",
            "error_code",
            "recovery_locators",
            "retryable",
            "stage",
        ),
    )
    return SchemaUpgradeFailure(
        stage=payload["stage"],
        error_code=payload["error_code"],
        retryable=payload["retryable"],
        active_generation=payload["active_generation"],
        active_store_preservation=_decode_asset_preservation_evidence(
            payload["active_store_preservation"]
        ),
        recovery_locators=_decode_recovery_locators(
            payload["recovery_locators"]
        ),
    )


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
    if contract_type == "AssetPreservationEvidence":
        return _decode_asset_preservation_evidence(value)
    if contract_type == "RecoveryLocator":
        return _decode_recovery_locator(value)
    if contract_type == "MigrationDiagnostic":
        return _decode_migration_diagnostic(value)
    if contract_type == "ExportDiagnostic":
        return _decode_export_diagnostic(value)
    if contract_type == "MigrationPreflight":
        return _decode_migration_preflight(value)
    if contract_type == "MigrationReport":
        return _decode_migration_report(value)
    if contract_type == "MigrationFailure":
        return _decode_migration_failure(value)
    if contract_type == "ExportReport":
        return _decode_export_report(value)
    if contract_type == "ExportFailure":
        return _decode_export_failure(value)
    if contract_type == "SchemaUpgradeReport":
        return _decode_schema_upgrade_report(value)
    if contract_type == "SchemaUpgradeFailure":
        return _decode_schema_upgrade_failure(value)
    if contract_type == "SearchOptions":
        return _decode_search_options(value)
    if contract_type == "SearchHit":
        return _decode_search_hit(value)
    if contract_type == "MatcherValidationSummary":
        return _decode_matcher_validation_summary(value)
    if contract_type == "TextMatcherCapability":
        return _decode_text_matcher_capability(value)
    if contract_type == "TextMatchRequest":
        return _decode_text_match_request(value)
    if contract_type == "TextMatchSuccess":
        return _decode_text_match_success(value)
    if contract_type == "TextMatchRejected":
        return _decode_text_match_rejected(value)
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


def matcher_validation_manifest_to_json(
    manifest: MatcherValidationManifest,
) -> str:
    """Encode the Core-internal validation manifest independently."""

    if not isinstance(manifest, MatcherValidationManifest):
        raise TypeError(
            "matcher validation manifest must be MatcherValidationManifest"
        )
    _validate_matcher_validation_manifest(manifest)
    envelope = {
        "manifest": _encode_matcher_validation_manifest(manifest),
        "manifest_codec_version": MATCHER_VALIDATION_MANIFEST_CODEC_VERSION,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def matcher_validation_manifest_from_json(
    serialized: str,
) -> MatcherValidationManifest:
    """Decode one strict Core-internal validation manifest envelope."""

    if not isinstance(serialized, str):
        raise TypeError("serialized matcher manifest must be a string")

    def reject_non_finite(value: str) -> None:
        raise ValueError(
            f"non-finite JSON number is not allowed: {value}"
        )

    try:
        value = json.loads(serialized, parse_constant=reject_non_finite)
    except (json.JSONDecodeError, TypeError):
        raise ValueError(
            "serialized matcher manifest is not valid JSON"
        ) from None
    envelope = _strict_fields(
        value,
        "matcher validation manifest envelope",
        ("manifest", "manifest_codec_version"),
    )
    version = envelope["manifest_codec_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("manifest_codec_version must be an integer")
    if version != MATCHER_VALIDATION_MANIFEST_CODEC_VERSION:
        raise ValueError(
            f"unsupported matcher manifest codec version: {version}"
        )
    return _decode_matcher_validation_manifest(envelope["manifest"])


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
    "AssetKind",
    "AssetPreservationEvidence",
    "AssetPreservationState",
    "CanonicalResourceIdentity",
    "CapabilityGatedTextMatcher",
    "ContextEvidence",
    "DiagnosticDisposition",
    "ExportDiagnostic",
    "ExportFailure",
    "ExportOutcome",
    "ExportReport",
    "GenerationExpectation",
    "MigrationDiagnostic",
    "MigrationFailure",
    "MigrationOutcome",
    "MigrationPreflight",
    "MigrationReport",
    "MatcherValidationSummary",
    "MutableStageRef",
    "QueryReport",
    "RecoveryLocator",
    "ResourceStoreCoordinatorPort",
    "ResourceQueryFailure",
    "SchemaUpgradeFailure",
    "SchemaUpgradeOutcome",
    "SchemaUpgradeReport",
    "SearchHit",
    "SearchOptions",
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
    "TextMatchOutcome",
    "TextMatchProfile",
    "TextMatchRejectCode",
    "TextMatchRejected",
    "TextMatchRequest",
    "TextMatchSuccess",
    "TextMatcherCapability",
    "TextMatcherState",
    "contract_from_json",
    "contract_to_json",
    "snapshot_receipt_digest",
    "stage_validation_evidence_digest",
    "validate_resource_handles",
]
