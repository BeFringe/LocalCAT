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
import json
import math
import re
from typing import Any, Mapping, Protocol


TM_CONTRACT_CODEC_VERSION = 1
SCORER_VERSION_V1 = "scorer-v1"

_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")
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


type TMContract = (
    TMRecord
    | TMRecordDraft
    | TMQuery
    | SimilarityEvidence
    | ContextEvidence
    | TMResult
    | ResourceQueryFailure
    | QueryReport
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
    "SCORER_VERSION_V1",
    "TM_CONTRACT_CODEC_VERSION",
    "ContextEvidence",
    "QueryReport",
    "ResourceQueryFailure",
    "SimilarityEvidence",
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
    "validate_resource_handles",
]
