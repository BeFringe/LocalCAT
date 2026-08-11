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
MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION = "matcher-validation-v2"
MATCHER_VALIDATION_MANIFEST_CODEC_VERSION = 2
CANDIDATE_BUDGET_VERSION = "candidate-budget-v1"
BENCHMARK_CONTRACT_VERSION = "benchmark-v1"
BENCHMARK_SUITE_VERSION = "benchmark-suite-v1"
BENCHMARK_PERCENTILE_METHOD = "nearest-rank"
BENCHMARK_RSS_SCOPE = "child-process-lifetime-v1"

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


def _require_nonnegative_number(value: object, field_name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


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

    def health(self) -> StoreHealth: ...


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


class SimilarityScorer(Protocol):
    """Storage-independent scorer port for one query/candidate pair."""

    def score(
        self,
        query: str,
        candidate: str,
    ) -> SimilarityEvidence: ...


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


class CandidateStage(str, Enum):
    """Ordered recall and candidate-pool transformation stages."""

    FTS_TRIGRAM = "FTS_TRIGRAM"
    GRAM_3 = "GRAM_3"
    GRAM_2 = "GRAM_2"
    GRAM_1 = "GRAM_1"
    UNION = "UNION"
    DEDUPLICATE = "DEDUPLICATE"
    TRUNCATE = "TRUNCATE"


_CANDIDATE_SOURCE_STAGES = (
    CandidateStage.FTS_TRIGRAM,
    CandidateStage.GRAM_3,
    CandidateStage.GRAM_2,
    CandidateStage.GRAM_1,
)
_CANDIDATE_STAGE_ORDER = {
    stage: position
    for position, stage in enumerate(
        (
            *_CANDIDATE_SOURCE_STAGES,
            CandidateStage.UNION,
            CandidateStage.DEDUPLICATE,
            CandidateStage.TRUNCATE,
        )
    )
}
_CANDIDATE_INDEX_KINDS = ("FTS5_TRIGRAM", "GRAM_FALLBACK")


def candidate_budget_v1(result_limit: int) -> int:
    """Return the frozen candidate-budget-v1 pool limit."""

    _require_int(result_limit, "result limit", minimum=1)
    return min(8192, max(2048, result_limit * 128))


@dataclass(frozen=True)
class CandidateStageMetadata:
    """Conserved counts for one executed recall or pool stage."""

    stage: CandidateStage
    input_count: int
    added_unique_count: int
    output_unique_count: int
    dropped_count: int

    def __post_init__(self) -> None:
        _validate_candidate_stage_metadata(self)


def _validate_candidate_stage_metadata(
    metadata: CandidateStageMetadata,
) -> None:
    if not isinstance(metadata.stage, CandidateStage):
        raise TypeError("candidate stage must be CandidateStage")
    for field_name, value in (
        ("input count", metadata.input_count),
        ("added unique count", metadata.added_unique_count),
        ("output unique count", metadata.output_unique_count),
        ("dropped count", metadata.dropped_count),
    ):
        _require_int(value, f"candidate stage {field_name}", minimum=0)
    if (
        metadata.input_count
        + metadata.added_unique_count
        - metadata.dropped_count
        != metadata.output_unique_count
    ):
        raise ValueError("candidate stage counts must conserve input and output")
    if metadata.stage in _CANDIDATE_SOURCE_STAGES:
        if (
            metadata.dropped_count != 0
            or metadata.output_unique_count
            != metadata.input_count + metadata.added_unique_count
        ):
            raise ValueError(
                "candidate source stage must only grow the cumulative unique pool"
            )
    elif metadata.stage is CandidateStage.UNION:
        if (
            metadata.added_unique_count != 0
            or metadata.dropped_count != 0
            or metadata.output_unique_count != metadata.input_count
        ):
            raise ValueError(
                "UNION must preserve the cumulative unique pool"
            )
    if (
        metadata.stage
        in (CandidateStage.DEDUPLICATE, CandidateStage.TRUNCATE)
        and metadata.added_unique_count != 0
    ):
        raise ValueError(
            f"{metadata.stage.value} must not add unique candidates"
        )


@dataclass(frozen=True)
class CandidateRecallMetadata:
    """Recall-only metadata, excluding scorer and global-limit claims."""

    resource_id: str
    index_kind: str
    fuzzy_available: bool
    fuzzy_unavailable_code: str | None
    stages: tuple[CandidateStageMetadata, ...]
    union_unique_count: int
    deduplicated_count: int
    result_limit: int
    candidate_budget_version: str
    candidate_budget: int
    truncated: bool

    def __post_init__(self) -> None:
        _validate_candidate_recall_metadata(self)


def _candidate_final_count(metadata: CandidateRecallMetadata) -> int:
    if not metadata.stages:
        return 0
    return metadata.stages[-1].output_unique_count


def _validate_candidate_recall_metadata(
    metadata: CandidateRecallMetadata,
) -> None:
    _require_identity(metadata.resource_id, "candidate resource id")
    _require_identity(metadata.index_kind, "candidate index kind")
    if metadata.index_kind not in _CANDIDATE_INDEX_KINDS:
        raise ValueError("candidate index kind is unsupported")
    _require_bool(metadata.fuzzy_available, "fuzzy available")
    _require_int(
        metadata.union_unique_count,
        "union unique count",
        minimum=0,
    )
    _require_int(
        metadata.deduplicated_count,
        "deduplicated count",
        minimum=0,
    )
    _require_int(metadata.result_limit, "result limit", minimum=1)
    if metadata.candidate_budget_version != CANDIDATE_BUDGET_VERSION:
        raise ValueError(
            f"candidate budget version must be {CANDIDATE_BUDGET_VERSION}"
        )
    _require_int(metadata.candidate_budget, "candidate budget", minimum=1)
    expected_budget = candidate_budget_v1(metadata.result_limit)
    if metadata.candidate_budget != expected_budget:
        raise ValueError(
            "candidate budget must equal candidate-budget-v1 for result limit"
        )
    _require_bool(metadata.truncated, "candidate truncated")

    stages = _require_tuple(metadata.stages, "candidate stages")
    if any(
        not isinstance(stage_metadata, CandidateStageMetadata)
        for stage_metadata in stages
    ):
        raise TypeError(
            "candidate stages must contain CandidateStageMetadata values"
        )
    for stage_metadata in stages:
        _validate_candidate_stage_metadata(stage_metadata)

    if metadata.fuzzy_available:
        if metadata.fuzzy_unavailable_code is not None:
            raise ValueError(
                "fuzzy unavailable code must be empty when fuzzy is available"
            )
        if not stages:
            raise ValueError(
                "available fuzzy recall must include executed stages"
            )
    else:
        if metadata.fuzzy_unavailable_code is None:
            raise ValueError(
                "fuzzy unavailable code is required when fuzzy is unavailable"
            )
        _require_diagnostic_identifier(
            metadata.fuzzy_unavailable_code,
            "fuzzy unavailable code",
        )
        if stages:
            raise ValueError(
                "fuzzy unavailable recall must have empty stages"
            )
        if (
            metadata.union_unique_count != 0
            or metadata.deduplicated_count != 0
            or metadata.truncated
        ):
            raise ValueError(
                "fuzzy unavailable recall must have zero counts and not truncate"
            )
        return

    stage_values = tuple(stage_metadata.stage for stage_metadata in stages)
    if len(stage_values) != len(set(stage_values)):
        raise ValueError("candidate stage order must not repeat stages")
    stage_positions = tuple(
        _CANDIDATE_STAGE_ORDER[stage]
        for stage in stage_values
    )
    if stage_positions != tuple(sorted(stage_positions)):
        raise ValueError("candidate stage order is invalid")
    if (
        CandidateStage.TRUNCATE in stage_values
    ) != metadata.truncated:
        raise ValueError(
            "candidate truncated flag must match TRUNCATE stage"
        )

    expected_suffix = (
        CandidateStage.UNION,
        CandidateStage.DEDUPLICATE,
    )
    if metadata.truncated:
        expected_suffix += (CandidateStage.TRUNCATE,)
    if stage_values[-len(expected_suffix):] != expected_suffix:
        raise ValueError(
            "candidate stage order must end with UNION, DEDUPLICATE"
            + (", TRUNCATE" if metadata.truncated else "")
        )
    source_stages = stage_values[:-len(expected_suffix)]
    if not source_stages or any(
        stage not in _CANDIDATE_SOURCE_STAGES
        for stage in source_stages
    ):
        raise ValueError(
            "candidate stage order must start with recall source stages"
        )
    if metadata.index_kind == "FTS5_TRIGRAM":
        if CandidateStage.FTS_TRIGRAM not in source_stages:
            raise ValueError(
                "FTS5_TRIGRAM index kind must execute FTS_TRIGRAM"
            )
    elif CandidateStage.FTS_TRIGRAM in source_stages:
        raise ValueError(
            "GRAM_FALLBACK index kind must not execute FTS_TRIGRAM"
        )

    prior_output = 0
    for stage_metadata in stages:
        if stage_metadata.input_count != prior_output:
            raise ValueError(
                "candidate stage input counts must be continuous"
            )
        prior_output = stage_metadata.output_unique_count

    union_metadata = stages[stage_values.index(CandidateStage.UNION)]
    if union_metadata.output_unique_count != metadata.union_unique_count:
        raise ValueError(
            "UNION output must equal union unique count"
        )
    deduplicate_metadata = stages[
        stage_values.index(CandidateStage.DEDUPLICATE)
    ]
    if (
        deduplicate_metadata.output_unique_count
        != metadata.deduplicated_count
    ):
        raise ValueError(
            "DEDUPLICATE output must equal deduplicated count"
        )

    if metadata.truncated:
        truncate_metadata = stages[-1]
        if (
            truncate_metadata.input_count
            != metadata.deduplicated_count
            or truncate_metadata.output_unique_count
            != metadata.candidate_budget
            or truncate_metadata.dropped_count <= 0
        ):
            raise ValueError(
                "TRUNCATE counts must reduce deduplicated candidates "
                "to candidate budget"
            )
    elif metadata.deduplicated_count > metadata.candidate_budget:
        raise ValueError(
            "candidate pool above budget must include TRUNCATE"
        )


@dataclass(frozen=True)
class CandidateEvidence:
    """Recall evidence for one candidate id before scoring."""

    record_id: int
    recall_stages: tuple[CandidateStage, ...]
    matched_grams: int
    query_grams: int
    overlap_ratio: float
    pretruncate_rank: int | None

    def __post_init__(self) -> None:
        _validate_candidate_evidence(self)


def _validate_candidate_evidence(evidence: CandidateEvidence) -> None:
    _require_int(evidence.record_id, "candidate record id", minimum=1)
    recall_stages = _require_tuple(
        evidence.recall_stages,
        "candidate recall stages",
    )
    if not recall_stages:
        raise ValueError("candidate recall stages must not be empty")
    if any(
        not isinstance(stage, CandidateStage)
        for stage in recall_stages
    ):
        raise TypeError(
            "candidate recall stages must contain CandidateStage values"
        )
    if any(
        stage not in _CANDIDATE_SOURCE_STAGES
        for stage in recall_stages
    ):
        raise ValueError(
            "candidate recall stages may contain only recall source stages"
        )
    if len(recall_stages) != len(set(recall_stages)):
        raise ValueError("candidate recall stages must be unique")
    positions = tuple(
        _CANDIDATE_STAGE_ORDER[stage]
        for stage in recall_stages
    )
    if positions != tuple(sorted(positions)):
        raise ValueError("candidate recall stages must be in stable order")
    _require_int(evidence.matched_grams, "matched grams", minimum=1)
    _require_int(evidence.query_grams, "query grams", minimum=1)
    if evidence.matched_grams > evidence.query_grams:
        raise ValueError("matched grams must not exceed query grams")
    _require_ratio(evidence.overlap_ratio, "candidate overlap ratio")
    expected_overlap = evidence.matched_grams / evidence.query_grams
    if not math.isclose(
        evidence.overlap_ratio,
        expected_overlap,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            "candidate overlap ratio must equal matched/query grams"
        )
    if evidence.pretruncate_rank is not None:
        _require_int(
            evidence.pretruncate_rank,
            "candidate pretruncate rank",
            minimum=1,
        )


@dataclass(frozen=True)
class CandidateRetrievalReport:
    """Candidate ids and recall-only metadata from one resource."""

    candidates: tuple[CandidateEvidence, ...]
    metadata: CandidateRecallMetadata

    def __post_init__(self) -> None:
        _validate_candidate_retrieval_report(self)


def _validate_candidate_retrieval_report(
    report: CandidateRetrievalReport,
) -> None:
    if not isinstance(report.metadata, CandidateRecallMetadata):
        raise TypeError(
            "candidate retrieval metadata must be CandidateRecallMetadata"
        )
    _validate_candidate_recall_metadata(report.metadata)
    candidates = _require_tuple(report.candidates, "candidate values")
    if any(
        not isinstance(candidate, CandidateEvidence)
        for candidate in candidates
    ):
        raise TypeError(
            "candidate values must contain CandidateEvidence values"
        )
    for candidate in candidates:
        _validate_candidate_evidence(candidate)
    if not report.metadata.fuzzy_available:
        if candidates:
            raise ValueError(
                "fuzzy unavailable recall must return empty candidates"
            )
        return
    if len(candidates) != _candidate_final_count(report.metadata):
        raise ValueError(
            "candidate count must equal final recall stage output"
        )
    record_ids = tuple(candidate.record_id for candidate in candidates)
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("candidate values must have unique record ids")
    executed_source_stages = {
        stage_metadata.stage
        for stage_metadata in report.metadata.stages
        if stage_metadata.stage in _CANDIDATE_SOURCE_STAGES
    }
    for candidate in candidates:
        if not set(candidate.recall_stages).issubset(
            executed_source_stages
        ):
            raise ValueError(
                "candidate recall stages must be executed by metadata"
            )
    if report.metadata.truncated:
        ranks = tuple(
            candidate.pretruncate_rank
            for candidate in candidates
        )
        if any(rank is None for rank in ranks):
            raise ValueError(
                "truncated candidates require pretruncate ranks"
            )
        if set(ranks) != set(range(1, len(candidates) + 1)):
            raise ValueError(
                "truncated candidate pretruncate ranks must be contiguous"
            )


@dataclass(frozen=True)
class ResourceQueryMetadata:
    """Post-recall scorer/filter/global-limit counts for one resource."""

    resource_id: str
    context_available: bool
    context_unavailable_code: str | None
    recall: CandidateRecallMetadata
    scored_count: int
    returned_count: int

    def __post_init__(self) -> None:
        _validate_resource_query_metadata(self)


def _validate_resource_query_metadata(
    metadata: ResourceQueryMetadata,
) -> None:
    _require_identity(metadata.resource_id, "resource query metadata id")
    _require_bool(metadata.context_available, "context available")
    if metadata.context_available:
        if metadata.context_unavailable_code is not None:
            raise ValueError(
                "context unavailable code must be empty when context is available"
            )
    else:
        if metadata.context_unavailable_code is None:
            raise ValueError(
                "context unavailable code is required when context is unavailable"
            )
        _require_diagnostic_identifier(
            metadata.context_unavailable_code,
            "context unavailable code",
        )
    if not isinstance(metadata.recall, CandidateRecallMetadata):
        raise TypeError(
            "resource recall must be CandidateRecallMetadata"
        )
    _validate_candidate_recall_metadata(metadata.recall)
    if metadata.resource_id != metadata.recall.resource_id:
        raise ValueError(
            "resource query metadata resource id must match recall resource id"
        )
    _require_int(metadata.scored_count, "scored count", minimum=0)
    _require_int(metadata.returned_count, "returned count", minimum=0)
    final_candidate_count = _candidate_final_count(metadata.recall)
    if metadata.scored_count > final_candidate_count:
        raise ValueError(
            "scored count must not exceed recalled candidate count"
        )
    if (
        not metadata.recall.fuzzy_available
        and metadata.scored_count != 0
    ):
        raise ValueError(
            "fuzzy unavailable resource must not score candidates"
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

    results: tuple[TMResult, ...]
    resource_failures: tuple[ResourceQueryFailure, ...]
    resource_metadata: tuple[ResourceQueryMetadata, ...]

    def __post_init__(self) -> None:
        results = _require_tuple(self.results, "query results")
        failures = _require_tuple(
            self.resource_failures,
            "query resource_failures",
        )
        resource_metadata = _require_tuple(
            self.resource_metadata,
            "query resource_metadata",
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
        if any(
            not isinstance(metadata, ResourceQueryMetadata)
            for metadata in resource_metadata
        ):
            raise TypeError(
                "query resource_metadata must contain "
                "ResourceQueryMetadata values"
            )
        for metadata in resource_metadata:
            _validate_resource_query_metadata(metadata)
        failure_ids = tuple(failure.resource_id for failure in failures)
        if len(failure_ids) != len(set(failure_ids)):
            raise ValueError("query report may contain one failure per resource")
        metadata_ids = tuple(
            metadata.resource_id
            for metadata in resource_metadata
        )
        if len(metadata_ids) != len(set(metadata_ids)):
            raise ValueError(
                "query report may contain one metadata entry per resource"
            )
        result_limits = tuple(
            metadata.recall.result_limit
            for metadata in resource_metadata
        )
        if len(set(result_limits)) > 1:
            raise ValueError(
                "resource metadata must declare the same global result limit"
            )
        if (
            result_limits
            and sum(
                metadata.returned_count
                for metadata in resource_metadata
            )
            > result_limits[0]
        ):
            raise ValueError(
                "resource returned counts must not exceed global result limit"
            )
        result_ids = {result.resource_id for result in results}
        if result_ids.intersection(failure_ids):
            raise ValueError(
                "a failed query resource cannot also contribute results"
            )
        if set(metadata_ids).intersection(failure_ids):
            raise ValueError(
                "a failed query resource cannot also contribute metadata"
            )
        if result_ids.difference(metadata_ids):
            raise ValueError(
                "each result resource must have resource metadata"
            )
        for metadata in resource_metadata:
            resource_result_count = sum(
                result.resource_id == metadata.resource_id
                for result in results
            )
            if metadata.returned_count != resource_result_count:
                raise ValueError(
                    "resource returned count must equal its query result count"
                )
        if sum(
            metadata.returned_count
            for metadata in resource_metadata
        ) != len(results):
            raise ValueError(
                "resource returned counts must equal query result count"
            )


class BenchmarkExecutionPath(str, Enum):
    """Closed benchmark paths that must never share one capability result."""

    FTS5_TRIGRAM = "FTS5_TRIGRAM"
    GRAM_FALLBACK = "GRAM_FALLBACK"


_BENCHMARK_GATE_ORDER = (
    "CANDIDATE_RECALL",
    "EXACT_P95",
    "FUZZY_P95",
    "MIGRATION",
    "PEAK_RSS",
)
_BENCHMARK_REQUIRED_ENVIRONMENT_FIELDS = (
    "cpu",
    "fts5_enabled",
    "os",
    "python_version",
    "ram_mib",
    "sqlite_version",
    "unicode_version",
)


@dataclass(frozen=True)
class BenchmarkContract:
    """Fixed benchmark-v1 corpus, cohort, statistic, and hard-gate contract."""

    contract_version: str
    corpus_generator_version: str
    corpus_seed: int
    corpus_record_count: int
    corpus_digest: str
    corpus_composition_version: str
    corpus_composition_digest: str
    exact_cohort_digest: str
    exact_min_samples: int
    exact_cohort_count: int
    fuzzy_cohort_digest: str
    fuzzy_min_samples: int
    fuzzy_cohort_count: int
    oracle_subset_digest: str
    oracle_subset_record_count: int
    oracle_query_count: int
    top_k: int
    minimum_similarity: float
    warmup_queries_per_cohort: int
    measured_repeats: int
    percentile_method: str
    rss_scope: str
    candidate_budget_version: str
    scorer_config_digest: str
    fast_path_config_digest: str
    fallback_path_config_digest: str
    exact_p95_gate_ms: float
    fuzzy_p95_gate_ms: float
    migration_gate_seconds: float
    peak_rss_gate_mib: float
    candidate_recall_gate: float

    def __post_init__(self) -> None:
        _validate_benchmark_contract(self)


def _require_fixed_number(
    value: object,
    expected: int | float,
    field_name: str,
) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    if not math.isfinite(value) or value != expected:
        raise ValueError(f"{field_name} must be {expected}")


def _validate_benchmark_contract(contract: BenchmarkContract) -> None:
    if contract.contract_version != BENCHMARK_CONTRACT_VERSION:
        raise ValueError(
            f"contract version must be {BENCHMARK_CONTRACT_VERSION}"
        )
    _require_identity(
        contract.corpus_generator_version,
        "corpus generator version",
    )
    _require_int(contract.corpus_seed, "corpus seed", minimum=0)
    _require_int(
        contract.corpus_record_count,
        "corpus record count",
        minimum=0,
    )
    if contract.corpus_record_count != 100_000:
        raise ValueError("corpus record count must be 100000")
    for field_name, digest in (
        ("corpus digest", contract.corpus_digest),
        (
            "corpus composition digest",
            contract.corpus_composition_digest,
        ),
        ("exact cohort digest", contract.exact_cohort_digest),
        ("fuzzy cohort digest", contract.fuzzy_cohort_digest),
        ("oracle subset digest", contract.oracle_subset_digest),
        ("scorer config digest", contract.scorer_config_digest),
        ("fast path config digest", contract.fast_path_config_digest),
        ("fallback path config digest", contract.fallback_path_config_digest),
    ):
        _require_digest(digest, field_name)
    _require_identity(
        contract.corpus_composition_version,
        "corpus composition version",
    )
    _require_int(
        contract.exact_min_samples,
        "exact minimum samples",
        minimum=0,
    )
    if contract.exact_min_samples != 1_000:
        raise ValueError("exact minimum samples must be 1000")
    _require_int(
        contract.exact_cohort_count,
        "exact cohort count",
        minimum=contract.exact_min_samples,
    )
    _require_int(
        contract.fuzzy_min_samples,
        "fuzzy minimum samples",
        minimum=0,
    )
    if contract.fuzzy_min_samples != 200:
        raise ValueError("fuzzy minimum samples must be 200")
    _require_int(
        contract.fuzzy_cohort_count,
        "fuzzy cohort count",
        minimum=contract.fuzzy_min_samples,
    )
    _require_int(
        contract.oracle_subset_record_count,
        "oracle record count",
        minimum=0,
    )
    if contract.oracle_subset_record_count != 5_000:
        raise ValueError("oracle record count must be 5000")
    _require_int(
        contract.oracle_query_count,
        "oracle query count",
        minimum=0,
    )
    if contract.oracle_query_count != 200:
        raise ValueError("oracle query count must be 200")
    _require_int(contract.top_k, "top_k", minimum=1)
    if contract.top_k != 10:
        raise ValueError("top_k must be 10")
    _require_fixed_number(
        contract.minimum_similarity,
        0.60,
        "minimum similarity",
    )
    _require_int(
        contract.warmup_queries_per_cohort,
        "warmup queries per cohort",
        minimum=0,
    )
    if contract.warmup_queries_per_cohort != 100:
        raise ValueError("warmup queries per cohort must be 100")
    _require_int(
        contract.measured_repeats,
        "measured repeats",
        minimum=1,
    )
    if contract.measured_repeats != 1:
        raise ValueError("measured repeats must be 1")
    if contract.percentile_method != BENCHMARK_PERCENTILE_METHOD:
        raise ValueError(
            "percentile method must be nearest-rank"
        )
    if contract.rss_scope != BENCHMARK_RSS_SCOPE:
        raise ValueError(
            f"RSS scope must be {BENCHMARK_RSS_SCOPE}"
        )
    if contract.candidate_budget_version != CANDIDATE_BUDGET_VERSION:
        raise ValueError(
            "candidate budget version must be candidate-budget-v1"
        )
    _require_fixed_number(
        contract.exact_p95_gate_ms,
        50.0,
        "exact p95 gate",
    )
    _require_fixed_number(
        contract.fuzzy_p95_gate_ms,
        500.0,
        "fuzzy p95 gate",
    )
    _require_fixed_number(
        contract.migration_gate_seconds,
        120.0,
        "migration gate",
    )
    _require_fixed_number(
        contract.peak_rss_gate_mib,
        512.0,
        "peak RSS gate",
    )
    _require_fixed_number(
        contract.candidate_recall_gate,
        1.0,
        "candidate recall gate",
    )


def _benchmark_contract_payload(
    contract: BenchmarkContract,
) -> dict[str, Any]:
    return {
        "candidate_budget_version": contract.candidate_budget_version,
        "candidate_recall_gate": contract.candidate_recall_gate,
        "contract_version": contract.contract_version,
        "corpus_composition_digest": (
            contract.corpus_composition_digest
        ),
        "corpus_composition_version": (
            contract.corpus_composition_version
        ),
        "corpus_digest": contract.corpus_digest,
        "corpus_generator_version": contract.corpus_generator_version,
        "corpus_record_count": contract.corpus_record_count,
        "corpus_seed": contract.corpus_seed,
        "exact_cohort_digest": contract.exact_cohort_digest,
        "exact_cohort_count": contract.exact_cohort_count,
        "exact_min_samples": contract.exact_min_samples,
        "exact_p95_gate_ms": contract.exact_p95_gate_ms,
        "fallback_path_config_digest": (
            contract.fallback_path_config_digest
        ),
        "fast_path_config_digest": contract.fast_path_config_digest,
        "fuzzy_cohort_digest": contract.fuzzy_cohort_digest,
        "fuzzy_cohort_count": contract.fuzzy_cohort_count,
        "fuzzy_min_samples": contract.fuzzy_min_samples,
        "fuzzy_p95_gate_ms": contract.fuzzy_p95_gate_ms,
        "measured_repeats": contract.measured_repeats,
        "migration_gate_seconds": contract.migration_gate_seconds,
        "minimum_similarity": contract.minimum_similarity,
        "oracle_query_count": contract.oracle_query_count,
        "oracle_subset_digest": contract.oracle_subset_digest,
        "oracle_subset_record_count": contract.oracle_subset_record_count,
        "peak_rss_gate_mib": contract.peak_rss_gate_mib,
        "percentile_method": contract.percentile_method,
        "rss_scope": contract.rss_scope,
        "scorer_config_digest": contract.scorer_config_digest,
        "top_k": contract.top_k,
        "warmup_queries_per_cohort": (
            contract.warmup_queries_per_cohort
        ),
    }


def benchmark_contract_digest(contract: BenchmarkContract) -> str:
    """Digest every approved benchmark-v1 parameter."""

    if not isinstance(contract, BenchmarkContract):
        raise TypeError("benchmark contract must be BenchmarkContract")
    _validate_benchmark_contract(contract)
    return _stable_digest(_benchmark_contract_payload(contract))


def _validate_benchmark_environment(
    environment: tuple[tuple[str, str], ...],
) -> None:
    pairs = _require_tuple(environment, "benchmark environment")
    keys: list[str] = []
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(
                "benchmark environment entries must be two-item tuples"
            )
        key, value = pair
        _require_identity(key, "benchmark environment key")
        _require_identity(value, "benchmark environment value")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError("benchmark environment keys must be unique")
    if tuple(keys) != tuple(sorted(keys)):
        raise ValueError(
            "benchmark environment keys must be in stable order"
        )
    missing = set(_BENCHMARK_REQUIRED_ENVIRONMENT_FIELDS).difference(keys)
    if missing:
        raise ValueError(
            "benchmark environment is missing required fields"
        )


def benchmark_environment_digest(
    environment: tuple[tuple[str, str], ...],
) -> str:
    """Digest a strict environment tuple without accepting free-form messages."""

    _validate_benchmark_environment(environment)
    return _stable_digest(
        {"environment": [list(pair) for pair in environment]}
    )


@dataclass(frozen=True)
class BenchmarkReport:
    """One path-specific benchmark-v1 result bound to all input facts."""

    contract: BenchmarkContract
    contract_digest: str
    corpus_digest: str
    corpus_composition_version: str
    corpus_composition_digest: str
    exact_cohort_digest: str
    fuzzy_cohort_digest: str
    oracle_subset_digest: str
    scorer_config_digest: str
    execution_path: BenchmarkExecutionPath
    path_config_digest: str
    exact_sample_count: int
    fuzzy_sample_count: int
    oracle_query_count: int
    percentile_method: str
    candidate_recall: float
    exact_p50_ms: float
    exact_p95_ms: float
    exact_max_ms: float
    fuzzy_top10_p50_ms: float
    fuzzy_top10_p95_ms: float
    fuzzy_top10_max_ms: float
    migration_seconds: float
    peak_rss_mib: float
    passed: bool
    failed_gates: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    environment_digest: str

    def __post_init__(self) -> None:
        _validate_benchmark_report(self)


def _validate_percentile_triplet(
    p50: float,
    p95: float,
    maximum: float,
    field_prefix: str,
) -> None:
    _require_nonnegative_number(p50, f"{field_prefix} p50")
    _require_nonnegative_number(p95, f"{field_prefix} p95")
    _require_nonnegative_number(maximum, f"{field_prefix} max")
    if not p50 <= p95 <= maximum:
        raise ValueError(
            f"{field_prefix} p50/p95/max must be monotonic"
        )


def _validate_benchmark_report(report: BenchmarkReport) -> None:
    if not isinstance(report.contract, BenchmarkContract):
        raise TypeError("report contract must be BenchmarkContract")
    _validate_benchmark_contract(report.contract)
    expected_contract_digest = benchmark_contract_digest(report.contract)
    _require_digest(report.contract_digest, "contract digest")
    if report.contract_digest != expected_contract_digest:
        raise ValueError(
            "report contract digest must bind the benchmark contract"
        )
    if (
        report.corpus_composition_version
        != report.contract.corpus_composition_version
    ):
        raise ValueError(
            "report corpus composition version must match benchmark contract"
        )
    for field_name, actual, expected in (
        ("corpus digest", report.corpus_digest, report.contract.corpus_digest),
        (
            "corpus composition digest",
            report.corpus_composition_digest,
            report.contract.corpus_composition_digest,
        ),
        (
            "exact cohort digest",
            report.exact_cohort_digest,
            report.contract.exact_cohort_digest,
        ),
        (
            "fuzzy cohort digest",
            report.fuzzy_cohort_digest,
            report.contract.fuzzy_cohort_digest,
        ),
        (
            "oracle subset digest",
            report.oracle_subset_digest,
            report.contract.oracle_subset_digest,
        ),
        (
            "scorer config digest",
            report.scorer_config_digest,
            report.contract.scorer_config_digest,
        ),
    ):
        _require_digest(actual, field_name)
        if actual != expected:
            raise ValueError(
                f"report {field_name} must match benchmark contract"
            )
    if not isinstance(report.execution_path, BenchmarkExecutionPath):
        raise TypeError(
            "report execution path must be BenchmarkExecutionPath"
        )
    expected_path_config_digest = (
        report.contract.fast_path_config_digest
        if report.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else report.contract.fallback_path_config_digest
    )
    _require_digest(report.path_config_digest, "path config digest")
    if report.path_config_digest != expected_path_config_digest:
        raise ValueError(
            "report path config digest must match execution path"
        )
    _require_int(
        report.exact_sample_count,
        "exact sample count",
        minimum=0,
    )
    if report.exact_sample_count != report.contract.exact_cohort_count:
        raise ValueError(
            "exact sample count must equal exact cohort count"
        )
    _require_int(
        report.fuzzy_sample_count,
        "fuzzy sample count",
        minimum=0,
    )
    if report.fuzzy_sample_count != report.contract.fuzzy_cohort_count:
        raise ValueError(
            "fuzzy sample count must equal fuzzy cohort count"
        )
    _require_int(
        report.oracle_query_count,
        "oracle query count",
        minimum=0,
    )
    if report.oracle_query_count != report.contract.oracle_query_count:
        raise ValueError(
            "oracle query count must match benchmark contract"
        )
    if report.percentile_method != report.contract.percentile_method:
        raise ValueError(
            "report percentile method must match benchmark contract"
        )
    _require_ratio(report.candidate_recall, "candidate recall")
    _validate_percentile_triplet(
        report.exact_p50_ms,
        report.exact_p95_ms,
        report.exact_max_ms,
        "exact",
    )
    _validate_percentile_triplet(
        report.fuzzy_top10_p50_ms,
        report.fuzzy_top10_p95_ms,
        report.fuzzy_top10_max_ms,
        "fuzzy top-10",
    )
    _require_nonnegative_number(
        report.migration_seconds,
        "migration seconds",
    )
    _require_nonnegative_number(
        report.peak_rss_mib,
        "peak RSS MiB",
    )
    _require_bool(report.passed, "benchmark passed")
    failed_gates = _require_string_tuple(
        report.failed_gates,
        "failed gates",
        identities=True,
    )
    unknown_gates = set(failed_gates).difference(_BENCHMARK_GATE_ORDER)
    if unknown_gates:
        raise ValueError("failed gates contain unsupported gate")
    expected_failed_gates = tuple(
        gate
        for gate, failed in (
            (
                "CANDIDATE_RECALL",
                report.candidate_recall
                < report.contract.candidate_recall_gate,
            ),
            (
                "EXACT_P95",
                report.exact_p95_ms
                > report.contract.exact_p95_gate_ms,
            ),
            (
                "FUZZY_P95",
                report.fuzzy_top10_p95_ms
                > report.contract.fuzzy_p95_gate_ms,
            ),
            (
                "MIGRATION",
                report.migration_seconds
                > report.contract.migration_gate_seconds,
            ),
            (
                "PEAK_RSS",
                report.peak_rss_mib
                > report.contract.peak_rss_gate_mib,
            ),
        )
        if failed
    )
    if failed_gates != expected_failed_gates:
        raise ValueError(
            "failed gates must exactly match measured hard-gate failures"
        )
    if report.passed != (not expected_failed_gates):
        raise ValueError(
            "benchmark passed must derive from failed gates"
        )
    _validate_benchmark_environment(report.environment)
    _require_digest(report.environment_digest, "environment digest")
    if report.environment_digest != benchmark_environment_digest(
        report.environment
    ):
        raise ValueError(
            "report environment digest must bind environment"
        )
    environment = dict(report.environment)
    fts5_enabled = environment["fts5_enabled"]
    if report.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        if fts5_enabled != "true":
            raise ValueError(
                "FTS5 environment must enable FTS5 for fast path"
            )
    elif fts5_enabled != "false":
        raise ValueError(
            "fallback environment must disable FTS5"
        )


@dataclass(frozen=True)
class BenchmarkSuiteContract:
    """Aggregate contract requiring both benchmark-v1 execution paths."""

    suite_version: str
    benchmark_contract: BenchmarkContract
    benchmark_contract_digest: str
    required_paths: tuple[BenchmarkExecutionPath, ...]

    def __post_init__(self) -> None:
        _validate_benchmark_suite_contract(self)


def _validate_benchmark_suite_contract(
    contract: BenchmarkSuiteContract,
) -> None:
    if contract.suite_version != BENCHMARK_SUITE_VERSION:
        raise ValueError(
            f"benchmark suite version must be {BENCHMARK_SUITE_VERSION}"
        )
    if not isinstance(contract.benchmark_contract, BenchmarkContract):
        raise TypeError(
            "suite benchmark contract must be BenchmarkContract"
        )
    _validate_benchmark_contract(contract.benchmark_contract)
    _require_digest(
        contract.benchmark_contract_digest,
        "suite benchmark contract digest",
    )
    if contract.benchmark_contract_digest != benchmark_contract_digest(
        contract.benchmark_contract
    ):
        raise ValueError(
            "suite benchmark contract digest must bind benchmark contract"
        )
    required_paths = _require_tuple(
        contract.required_paths,
        "suite required paths",
    )
    if any(
        not isinstance(path, BenchmarkExecutionPath)
        for path in required_paths
    ):
        raise TypeError(
            "suite required paths must contain BenchmarkExecutionPath values"
        )
    if required_paths != tuple(BenchmarkExecutionPath):
        raise ValueError(
            "suite required paths must contain both benchmark paths "
            "in stable order"
        )


def _benchmark_suite_contract_payload(
    contract: BenchmarkSuiteContract,
) -> dict[str, Any]:
    return {
        "benchmark_contract": _benchmark_contract_payload(
            contract.benchmark_contract
        ),
        "benchmark_contract_digest": (
            contract.benchmark_contract_digest
        ),
        "required_paths": [
            path.value for path in contract.required_paths
        ],
        "suite_version": contract.suite_version,
    }


def benchmark_suite_contract_digest(
    contract: BenchmarkSuiteContract,
) -> str:
    """Digest the two-path aggregate contract and nested benchmark contract."""

    if not isinstance(contract, BenchmarkSuiteContract):
        raise TypeError(
            "benchmark suite contract must be BenchmarkSuiteContract"
        )
    _validate_benchmark_suite_contract(contract)
    return _stable_digest(_benchmark_suite_contract_payload(contract))


@dataclass(frozen=True)
class BenchmarkSuiteReport:
    """Both path reports with aggregate pass/failure derived from facts."""

    suite_contract: BenchmarkSuiteContract
    suite_contract_digest: str
    corpus_composition_version: str
    corpus_composition_digest: str
    path_reports: tuple[BenchmarkReport, ...]
    passed: bool
    failed_paths: tuple[BenchmarkExecutionPath, ...]

    def __post_init__(self) -> None:
        _validate_benchmark_suite_report(self)


def _validate_benchmark_suite_report(
    report: BenchmarkSuiteReport,
) -> None:
    if not isinstance(report.suite_contract, BenchmarkSuiteContract):
        raise TypeError(
            "suite report contract must be BenchmarkSuiteContract"
        )
    _validate_benchmark_suite_contract(report.suite_contract)
    _require_digest(
        report.suite_contract_digest,
        "suite contract digest",
    )
    if report.suite_contract_digest != benchmark_suite_contract_digest(
        report.suite_contract
    ):
        raise ValueError(
            "suite contract digest must bind suite contract"
        )
    benchmark_contract = report.suite_contract.benchmark_contract
    if (
        report.corpus_composition_version
        != benchmark_contract.corpus_composition_version
    ):
        raise ValueError(
            "suite corpus composition version must match benchmark contract"
        )
    _require_digest(
        report.corpus_composition_digest,
        "suite corpus composition digest",
    )
    if (
        report.corpus_composition_digest
        != benchmark_contract.corpus_composition_digest
    ):
        raise ValueError(
            "suite corpus composition digest must match benchmark contract"
        )
    path_reports = _require_tuple(
        report.path_reports,
        "suite path reports",
    )
    if any(
        not isinstance(path_report, BenchmarkReport)
        for path_report in path_reports
    ):
        raise TypeError(
            "suite path reports must contain BenchmarkReport values"
        )
    for path_report in path_reports:
        _validate_benchmark_report(path_report)
        if path_report.contract != benchmark_contract:
            raise ValueError(
                "suite path report contract must match suite contract"
            )
        if (
            path_report.corpus_composition_version
            != report.corpus_composition_version
        ):
            raise ValueError(
                "suite path corpus composition version must match suite"
            )
        if (
            path_report.corpus_composition_digest
            != report.corpus_composition_digest
        ):
            raise ValueError(
                "suite path corpus composition digest must match suite"
            )
    execution_paths = tuple(
        path_report.execution_path
        for path_report in path_reports
    )
    required_paths = report.suite_contract.required_paths
    if (
        len(execution_paths) != len(required_paths)
        or set(execution_paths) != set(required_paths)
    ):
        raise ValueError(
            "suite must report both benchmark paths exactly once"
        )
    if execution_paths != required_paths:
        raise ValueError(
            "suite benchmark path order must match required paths"
        )
    _require_bool(report.passed, "aggregate passed")
    failed_paths = _require_tuple(
        report.failed_paths,
        "suite failed paths",
    )
    if any(
        not isinstance(path, BenchmarkExecutionPath)
        for path in failed_paths
    ):
        raise TypeError(
            "suite failed paths must contain BenchmarkExecutionPath values"
        )
    expected_failed_paths = tuple(
        path_report.execution_path
        for path_report in path_reports
        if not path_report.passed
    )
    if failed_paths != expected_failed_paths:
        raise ValueError(
            "suite failed paths must derive from path reports"
        )
    if report.passed != (not expected_failed_paths):
        raise ValueError(
            "aggregate passed must derive from both path reports"
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
    publication_committed: bool = False,
    publication_commit_ambiguous: bool = False,
) -> None:
    if type(publication_committed) is not bool:
        raise TypeError(f"{field_name} publication committed must be a bool")
    if type(publication_commit_ambiguous) is not bool:
        raise TypeError(
            f"{field_name} publication commit ambiguous must be a bool"
        )
    if publication_committed and publication_commit_ambiguous:
        raise ValueError(
            f"{field_name} publication commit state is contradictory"
        )
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
    if publication_committed:
        if retryable:
            raise ValueError(
                "committed publication failure must be fail-stop and "
                "not retryable"
            )
        if locators:
            raise ValueError(
                "committed publication failure cannot restore the "
                "prior destination"
            )
        return
    if publication_commit_ambiguous:
        if retryable:
            raise ValueError(
                "ambiguous publication failure must be fail-stop and "
                "not retryable"
            )
        if locators:
            raise ValueError(
                "ambiguous publication failure cannot fabricate a "
                "recovery locator"
            )
        return
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
    """Fail-stop export result with destination preservation evidence.

    ``publication_committed`` distinguishes a failure whose destination
    publication is the durable truth (the canonical/ledger rollback is
    forbidden and only cleanup/handoff remains pending) from an ordinary
    fail-stop export failure where the prior destination may still be
    restored.  When True the failure must be fail-stop (``retryable``
    False) and must not carry any recovery locator: the old destination
    is never restored after a committed publication.

    ``publication_commit_ambiguous`` marks a failure whose ledger commit
    state is unknown (the completion probe itself failed), so neither
    committed truth nor rollback authority may be claimed: it must be
    fail-stop, must not carry any recovery locator, and may preserve
    truthful VERIFIED_CHANGED/UNVERIFIED destination evidence without a
    locator because automatic rollback is forbidden while the commit
    state is unknown.  The two publication-state flags are mutually
    exclusive.  Both defaults are False and preserve the original
    contract unchanged.
    """

    stage: str
    error_code: str
    retryable: bool
    diagnostics: tuple[ExportDiagnostic, ...]
    previous_destination_preservation: AssetPreservationEvidence
    recovery_locators: tuple[RecoveryLocator, ...]
    publication_committed: bool = False
    publication_commit_ambiguous: bool = False

    def __post_init__(self) -> None:
        _require_diagnostic_identifier(self.stage, "export failure stage")
        _require_diagnostic_identifier(
            self.error_code,
            "export failure error code",
        )
        _require_bool(self.retryable, "export failure retryable")
        _require_bool(
            self.publication_committed,
            "export failure publication committed",
        )
        _require_bool(
            self.publication_commit_ambiguous,
            "export failure publication commit ambiguous",
        )
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
            publication_committed=self.publication_committed,
            publication_commit_ambiguous=self.publication_commit_ambiguous,
        )


def export_cleanup_pending_failure(
    *,
    stage: str,
    destination_before: str | None,
    destination_observed: str | None,
    diagnostics: tuple[ExportDiagnostic, ...] = (),
) -> ExportFailure:
    """Build the dedicated cleanup-pending failure for a committed export.

    The destination publication is durably committed and the canonical/
    ledger rollback is forbidden, so the failure is fail-stop
    (``retryable`` False), carries ``publication_committed`` True and
    never fabricates a recovery locator for the old destination.  The
    evidence keeps every known digest: a fresh prior-absent destination
    is ``NOT_APPLICABLE``, an unchanged destination is
    ``VERIFIED_UNCHANGED``, a changed destination is
    ``VERIFIED_CHANGED`` with both digests, and an unreadable
    destination is ``UNVERIFIED`` with the known before digest.  Callers
    pass code-only diagnostics (stable identifiers, no paths or text).
    """

    if destination_before is None:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.NOT_APPLICABLE,
            before_digest=None,
            observed_digest=None,
        )
    elif destination_observed == destination_before:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.VERIFIED_UNCHANGED,
            before_digest=destination_before,
            observed_digest=destination_before,
        )
    elif destination_observed is not None:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.VERIFIED_CHANGED,
            before_digest=destination_before,
            observed_digest=destination_observed,
        )
    else:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.UNVERIFIED,
            before_digest=destination_before,
            observed_digest=None,
        )
    return ExportFailure(
        stage=stage,
        error_code="EXPORT.CLEANUP_PENDING",
        retryable=False,
        diagnostics=diagnostics,
        previous_destination_preservation=evidence,
        recovery_locators=(),
        publication_committed=True,
    )


def export_ledger_ambiguous_failure(
    *,
    stage: str,
    error_code: str,
    destination_before: str | None,
    destination_observed: str | None,
    diagnostics: tuple[ExportDiagnostic, ...] = (),
) -> ExportFailure:
    """Build the dedicated ledger-ambiguous failure for a lost probe.

    When the completion probe itself raises, the ledger commit state is
    unknown: this is never success, never ``publication_committed``, and
    automatic rollback is forbidden, so the failure is fail-stop
    (``retryable`` False), sets ``publication_commit_ambiguous`` True and
    never fabricates a recovery locator.  The evidence keeps every known
    digest: a fresh prior-absent destination is ``NOT_APPLICABLE``, an
    unchanged destination is ``VERIFIED_UNCHANGED``, a changed
    destination is ``VERIFIED_CHANGED`` with both digests, and an
    unreadable destination is ``UNVERIFIED`` with the known before
    digest.  Callers pass code-only diagnostics (stable identifiers, no
    paths or text).
    """

    if destination_before is None:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.NOT_APPLICABLE,
            before_digest=None,
            observed_digest=None,
        )
    elif destination_observed == destination_before:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.VERIFIED_UNCHANGED,
            before_digest=destination_before,
            observed_digest=destination_before,
        )
    elif destination_observed is not None:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.VERIFIED_CHANGED,
            before_digest=destination_before,
            observed_digest=destination_observed,
        )
    else:
        evidence = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.UNVERIFIED,
            before_digest=destination_before,
            observed_digest=None,
        )
    return ExportFailure(
        stage=stage,
        error_code=error_code,
        retryable=False,
        diagnostics=diagnostics,
        previous_destination_preservation=evidence,
        recovery_locators=(),
        publication_committed=False,
        publication_commit_ambiguous=True,
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
    generated_at_utc: str
    valid_until_utc: str

    def __post_init__(self) -> None:
        _validate_matcher_validation_cohort_evidence(self)


def _validate_matcher_validation_cohort_evidence(
    evidence: MatcherValidationCohortEvidence,
) -> None:
    _require_identity(evidence.cohort_id, "matcher cohort id")
    _require_digest(evidence.cohort_digest, "matcher cohort digest")
    _require_bool(evidence.passed, "matcher cohort passed")
    generated = _parse_strict_utc_timestamp(
        evidence.generated_at_utc,
        "matcher cohort generated_at_utc",
    )
    valid_until = _parse_strict_utc_timestamp(
        evidence.valid_until_utc,
        "matcher cohort valid_until_utc",
    )
    if valid_until <= generated:
        raise ValueError(
            "matcher cohort valid_until_utc must be later than "
            "generated_at_utc"
        )


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
class StoreHealth:
    """One immutable observation of physical and feature availability."""

    healthy: bool
    schema_version: int
    generation: int
    record_count: int
    index_kind: str
    snapshot_binding_digest: str | None
    source_binding_state: SourceBindingState | None
    exact_available: bool
    context_available: bool
    fuzzy_available: bool
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_bool(self.healthy, "store health status")
        _require_int(
            self.schema_version,
            "store health schema version",
            minimum=1,
        )
        _require_int(
            self.generation,
            "store health generation",
            minimum=0,
        )
        _require_int(
            self.record_count,
            "store health record count",
            minimum=0,
        )
        _require_identity(self.index_kind, "store health index kind")
        if self.snapshot_binding_digest is not None:
            _require_digest(
                self.snapshot_binding_digest,
                "store health snapshot binding digest",
            )
        if (
            self.source_binding_state is not None
            and not isinstance(
                self.source_binding_state,
                SourceBindingState,
            )
        ):
            raise TypeError(
                "store health source binding state must be "
                "SourceBindingState or None"
            )
        _require_bool(self.exact_available, "store exact availability")
        _require_bool(self.context_available, "store context availability")
        _require_bool(self.fuzzy_available, "store fuzzy availability")
        if (
            self.context_available or self.fuzzy_available
        ) and not self.exact_available:
            raise ValueError(
                "context or fuzzy availability requires exact availability"
            )
        if self.exact_available and not self.healthy:
            raise ValueError(
                "an unhealthy store cannot advertise exact availability"
            )
        codes = _require_string_tuple(
            self.diagnostic_codes,
            "store diagnostic codes",
            identities=True,
        )
        if codes != tuple(sorted(codes)):
            raise ValueError(
                "store diagnostic codes must use stable sorted order"
            )


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
    | CandidateStageMetadata
    | CandidateRecallMetadata
    | CandidateEvidence
    | CandidateRetrievalReport
    | ResourceQueryMetadata
    | ResourceQueryFailure
    | QueryReport
    | BenchmarkContract
    | BenchmarkReport
    | BenchmarkSuiteContract
    | BenchmarkSuiteReport
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


def _encode_candidate_stage_metadata(
    metadata: CandidateStageMetadata,
) -> dict[str, Any]:
    return {
        "added_unique_count": metadata.added_unique_count,
        "dropped_count": metadata.dropped_count,
        "input_count": metadata.input_count,
        "output_unique_count": metadata.output_unique_count,
        "stage": metadata.stage.value,
    }


def _encode_candidate_recall_metadata(
    metadata: CandidateRecallMetadata,
) -> dict[str, Any]:
    return {
        "candidate_budget": metadata.candidate_budget,
        "candidate_budget_version": metadata.candidate_budget_version,
        "deduplicated_count": metadata.deduplicated_count,
        "fuzzy_available": metadata.fuzzy_available,
        "fuzzy_unavailable_code": metadata.fuzzy_unavailable_code,
        "index_kind": metadata.index_kind,
        "resource_id": metadata.resource_id,
        "result_limit": metadata.result_limit,
        "stages": [
            _encode_candidate_stage_metadata(stage)
            for stage in metadata.stages
        ],
        "truncated": metadata.truncated,
        "union_unique_count": metadata.union_unique_count,
    }


def _encode_candidate_evidence(
    evidence: CandidateEvidence,
) -> dict[str, Any]:
    return {
        "matched_grams": evidence.matched_grams,
        "overlap_ratio": evidence.overlap_ratio,
        "pretruncate_rank": evidence.pretruncate_rank,
        "query_grams": evidence.query_grams,
        "recall_stages": [
            stage.value for stage in evidence.recall_stages
        ],
        "record_id": evidence.record_id,
    }


def _encode_candidate_retrieval_report(
    report: CandidateRetrievalReport,
) -> dict[str, Any]:
    return {
        "candidates": [
            _encode_candidate_evidence(candidate)
            for candidate in report.candidates
        ],
        "metadata": _encode_candidate_recall_metadata(report.metadata),
    }


def _encode_resource_query_metadata(
    metadata: ResourceQueryMetadata,
) -> dict[str, Any]:
    return {
        "context_available": metadata.context_available,
        "context_unavailable_code": metadata.context_unavailable_code,
        "recall": _encode_candidate_recall_metadata(metadata.recall),
        "resource_id": metadata.resource_id,
        "returned_count": metadata.returned_count,
        "scored_count": metadata.scored_count,
    }


def _encode_benchmark_report(
    report: BenchmarkReport,
) -> dict[str, Any]:
    return {
        "candidate_recall": report.candidate_recall,
        "contract": _benchmark_contract_payload(report.contract),
        "contract_digest": report.contract_digest,
        "corpus_composition_digest": report.corpus_composition_digest,
        "corpus_composition_version": report.corpus_composition_version,
        "corpus_digest": report.corpus_digest,
        "environment": [list(pair) for pair in report.environment],
        "environment_digest": report.environment_digest,
        "exact_cohort_digest": report.exact_cohort_digest,
        "exact_max_ms": report.exact_max_ms,
        "exact_p50_ms": report.exact_p50_ms,
        "exact_p95_ms": report.exact_p95_ms,
        "exact_sample_count": report.exact_sample_count,
        "execution_path": report.execution_path.value,
        "failed_gates": list(report.failed_gates),
        "fuzzy_cohort_digest": report.fuzzy_cohort_digest,
        "fuzzy_sample_count": report.fuzzy_sample_count,
        "fuzzy_top10_max_ms": report.fuzzy_top10_max_ms,
        "fuzzy_top10_p50_ms": report.fuzzy_top10_p50_ms,
        "fuzzy_top10_p95_ms": report.fuzzy_top10_p95_ms,
        "migration_seconds": report.migration_seconds,
        "oracle_query_count": report.oracle_query_count,
        "oracle_subset_digest": report.oracle_subset_digest,
        "passed": report.passed,
        "path_config_digest": report.path_config_digest,
        "peak_rss_mib": report.peak_rss_mib,
        "percentile_method": report.percentile_method,
        "scorer_config_digest": report.scorer_config_digest,
    }


def _encode_benchmark_suite_report(
    report: BenchmarkSuiteReport,
) -> dict[str, Any]:
    return {
        "corpus_composition_digest": report.corpus_composition_digest,
        "corpus_composition_version": report.corpus_composition_version,
        "failed_paths": [
            path.value for path in report.failed_paths
        ],
        "passed": report.passed,
        "path_reports": [
            _encode_benchmark_report(path_report)
            for path_report in report.path_reports
        ],
        "suite_contract": _benchmark_suite_contract_payload(
            report.suite_contract
        ),
        "suite_contract_digest": report.suite_contract_digest,
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
        "publication_commit_ambiguous": (
            failure.publication_commit_ambiguous
        ),
        "publication_committed": failure.publication_committed,
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
        "generated_at_utc": evidence.generated_at_utc,
        "passed": evidence.passed,
        "valid_until_utc": evidence.valid_until_utc,
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
    if isinstance(contract, CandidateStageMetadata):
        _validate_candidate_stage_metadata(contract)
        return (
            "CandidateStageMetadata",
            _encode_candidate_stage_metadata(contract),
        )
    if isinstance(contract, CandidateRecallMetadata):
        _validate_candidate_recall_metadata(contract)
        return (
            "CandidateRecallMetadata",
            _encode_candidate_recall_metadata(contract),
        )
    if isinstance(contract, CandidateEvidence):
        _validate_candidate_evidence(contract)
        return "CandidateEvidence", _encode_candidate_evidence(contract)
    if isinstance(contract, CandidateRetrievalReport):
        _validate_candidate_retrieval_report(contract)
        return (
            "CandidateRetrievalReport",
            _encode_candidate_retrieval_report(contract),
        )
    if isinstance(contract, ResourceQueryMetadata):
        _validate_resource_query_metadata(contract)
        return (
            "ResourceQueryMetadata",
            _encode_resource_query_metadata(contract),
        )
    if isinstance(contract, ResourceQueryFailure):
        return "ResourceQueryFailure", _encode_failure(contract)
    if isinstance(contract, QueryReport):
        contract.__post_init__()
        return "QueryReport", {
            "resource_metadata": [
                _encode_resource_query_metadata(metadata)
                for metadata in contract.resource_metadata
            ],
            "resource_failures": [
                _encode_failure(failure)
                for failure in contract.resource_failures
            ],
            "results": [_encode_result(result) for result in contract.results],
        }
    if isinstance(contract, BenchmarkContract):
        _validate_benchmark_contract(contract)
        return "BenchmarkContract", _benchmark_contract_payload(contract)
    if isinstance(contract, BenchmarkReport):
        _validate_benchmark_report(contract)
        return "BenchmarkReport", _encode_benchmark_report(contract)
    if isinstance(contract, BenchmarkSuiteContract):
        _validate_benchmark_suite_contract(contract)
        return (
            "BenchmarkSuiteContract",
            _benchmark_suite_contract_payload(contract),
        )
    if isinstance(contract, BenchmarkSuiteReport):
        _validate_benchmark_suite_report(contract)
        return (
            "BenchmarkSuiteReport",
            _encode_benchmark_suite_report(contract),
        )
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
        (
            "cohort_digest",
            "cohort_id",
            "generated_at_utc",
            "passed",
            "valid_until_utc",
        ),
    )
    return MatcherValidationCohortEvidence(
        cohort_id=payload["cohort_id"],
        cohort_digest=payload["cohort_digest"],
        passed=payload["passed"],
        generated_at_utc=payload["generated_at_utc"],
        valid_until_utc=payload["valid_until_utc"],
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
    mapping = _as_mapping(value, "ExportFailure payload")
    present_optional = tuple(
        key
        for key in ("publication_committed", "publication_commit_ambiguous")
        if key in mapping
    )
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
        )
        + present_optional,
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
        publication_committed=payload.get("publication_committed", False),
        publication_commit_ambiguous=payload.get(
            "publication_commit_ambiguous",
            False,
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


def _decode_candidate_stage(
    value: object,
    field_name: str,
) -> CandidateStage:
    try:
        return CandidateStage(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} is invalid") from None


def _decode_candidate_stage_metadata(
    value: object,
) -> CandidateStageMetadata:
    payload = _strict_fields(
        value,
        "CandidateStageMetadata payload",
        (
            "added_unique_count",
            "dropped_count",
            "input_count",
            "output_unique_count",
            "stage",
        ),
    )
    return CandidateStageMetadata(
        stage=_decode_candidate_stage(
            payload["stage"],
            "candidate stage",
        ),
        input_count=payload["input_count"],
        added_unique_count=payload["added_unique_count"],
        output_unique_count=payload["output_unique_count"],
        dropped_count=payload["dropped_count"],
    )


def _decode_candidate_recall_metadata(
    value: object,
) -> CandidateRecallMetadata:
    payload = _strict_fields(
        value,
        "CandidateRecallMetadata payload",
        (
            "candidate_budget",
            "candidate_budget_version",
            "deduplicated_count",
            "fuzzy_available",
            "fuzzy_unavailable_code",
            "index_kind",
            "resource_id",
            "result_limit",
            "stages",
            "truncated",
            "union_unique_count",
        ),
    )
    return CandidateRecallMetadata(
        resource_id=payload["resource_id"],
        index_kind=payload["index_kind"],
        fuzzy_available=payload["fuzzy_available"],
        fuzzy_unavailable_code=payload["fuzzy_unavailable_code"],
        stages=tuple(
            _decode_candidate_stage_metadata(stage)
            for stage in _as_list(payload["stages"], "candidate stages")
        ),
        union_unique_count=payload["union_unique_count"],
        deduplicated_count=payload["deduplicated_count"],
        result_limit=payload["result_limit"],
        candidate_budget_version=payload["candidate_budget_version"],
        candidate_budget=payload["candidate_budget"],
        truncated=payload["truncated"],
    )


def _decode_candidate_evidence(value: object) -> CandidateEvidence:
    payload = _strict_fields(
        value,
        "CandidateEvidence payload",
        (
            "matched_grams",
            "overlap_ratio",
            "pretruncate_rank",
            "query_grams",
            "recall_stages",
            "record_id",
        ),
    )
    return CandidateEvidence(
        record_id=payload["record_id"],
        recall_stages=tuple(
            _decode_candidate_stage(stage, "candidate recall stage")
            for stage in _as_list(
                payload["recall_stages"],
                "candidate recall stages",
            )
        ),
        matched_grams=payload["matched_grams"],
        query_grams=payload["query_grams"],
        overlap_ratio=payload["overlap_ratio"],
        pretruncate_rank=payload["pretruncate_rank"],
    )


def _decode_candidate_retrieval_report(
    value: object,
) -> CandidateRetrievalReport:
    payload = _strict_fields(
        value,
        "CandidateRetrievalReport payload",
        ("candidates", "metadata"),
    )
    return CandidateRetrievalReport(
        candidates=tuple(
            _decode_candidate_evidence(candidate)
            for candidate in _as_list(
                payload["candidates"],
                "candidate values",
            )
        ),
        metadata=_decode_candidate_recall_metadata(payload["metadata"]),
    )


def _decode_resource_query_metadata(
    value: object,
) -> ResourceQueryMetadata:
    payload = _strict_fields(
        value,
        "ResourceQueryMetadata payload",
        (
            "context_available",
            "context_unavailable_code",
            "recall",
            "resource_id",
            "returned_count",
            "scored_count",
        ),
    )
    return ResourceQueryMetadata(
        resource_id=payload["resource_id"],
        context_available=payload["context_available"],
        context_unavailable_code=payload["context_unavailable_code"],
        recall=_decode_candidate_recall_metadata(payload["recall"]),
        scored_count=payload["scored_count"],
        returned_count=payload["returned_count"],
    )


def _decode_benchmark_contract(value: object) -> BenchmarkContract:
    payload = _strict_fields(
        value,
        "BenchmarkContract payload",
        (
            "candidate_budget_version",
            "candidate_recall_gate",
            "contract_version",
            "corpus_composition_digest",
            "corpus_composition_version",
            "corpus_digest",
            "corpus_generator_version",
            "corpus_record_count",
            "corpus_seed",
            "exact_cohort_digest",
            "exact_cohort_count",
            "exact_min_samples",
            "exact_p95_gate_ms",
            "fallback_path_config_digest",
            "fast_path_config_digest",
            "fuzzy_cohort_digest",
            "fuzzy_cohort_count",
            "fuzzy_min_samples",
            "fuzzy_p95_gate_ms",
            "measured_repeats",
            "migration_gate_seconds",
            "minimum_similarity",
            "oracle_query_count",
            "oracle_subset_digest",
            "oracle_subset_record_count",
            "peak_rss_gate_mib",
            "percentile_method",
            "rss_scope",
            "scorer_config_digest",
            "top_k",
            "warmup_queries_per_cohort",
        ),
    )
    return BenchmarkContract(
        contract_version=payload["contract_version"],
        corpus_generator_version=payload["corpus_generator_version"],
        corpus_seed=payload["corpus_seed"],
        corpus_record_count=payload["corpus_record_count"],
        corpus_digest=payload["corpus_digest"],
        corpus_composition_version=payload[
            "corpus_composition_version"
        ],
        corpus_composition_digest=payload[
            "corpus_composition_digest"
        ],
        exact_cohort_digest=payload["exact_cohort_digest"],
        exact_min_samples=payload["exact_min_samples"],
        exact_cohort_count=payload["exact_cohort_count"],
        fuzzy_cohort_digest=payload["fuzzy_cohort_digest"],
        fuzzy_min_samples=payload["fuzzy_min_samples"],
        fuzzy_cohort_count=payload["fuzzy_cohort_count"],
        oracle_subset_digest=payload["oracle_subset_digest"],
        oracle_subset_record_count=payload["oracle_subset_record_count"],
        oracle_query_count=payload["oracle_query_count"],
        top_k=payload["top_k"],
        minimum_similarity=payload["minimum_similarity"],
        warmup_queries_per_cohort=payload[
            "warmup_queries_per_cohort"
        ],
        measured_repeats=payload["measured_repeats"],
        percentile_method=payload["percentile_method"],
        rss_scope=payload["rss_scope"],
        candidate_budget_version=payload["candidate_budget_version"],
        scorer_config_digest=payload["scorer_config_digest"],
        fast_path_config_digest=payload["fast_path_config_digest"],
        fallback_path_config_digest=payload[
            "fallback_path_config_digest"
        ],
        exact_p95_gate_ms=payload["exact_p95_gate_ms"],
        fuzzy_p95_gate_ms=payload["fuzzy_p95_gate_ms"],
        migration_gate_seconds=payload["migration_gate_seconds"],
        peak_rss_gate_mib=payload["peak_rss_gate_mib"],
        candidate_recall_gate=payload["candidate_recall_gate"],
    )


def _decode_benchmark_execution_path(
    value: object,
) -> BenchmarkExecutionPath:
    try:
        return BenchmarkExecutionPath(value)
    except (TypeError, ValueError):
        raise ValueError("benchmark execution path is invalid") from None


def _decode_benchmark_environment(
    value: object,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for entry in _as_list(value, "benchmark environment"):
        pair = _as_list(entry, "benchmark environment entry")
        if len(pair) != 2:
            raise ValueError(
                "benchmark environment entries must contain two values"
            )
        key, item_value = pair
        if not isinstance(key, str) or not isinstance(item_value, str):
            raise TypeError(
                "benchmark environment entries must contain strings"
            )
        pairs.append((key, item_value))
    return tuple(pairs)


def _decode_benchmark_report(value: object) -> BenchmarkReport:
    payload = _strict_fields(
        value,
        "BenchmarkReport payload",
        (
            "candidate_recall",
            "contract",
            "contract_digest",
            "corpus_composition_digest",
            "corpus_composition_version",
            "corpus_digest",
            "environment",
            "environment_digest",
            "exact_cohort_digest",
            "exact_max_ms",
            "exact_p50_ms",
            "exact_p95_ms",
            "exact_sample_count",
            "execution_path",
            "failed_gates",
            "fuzzy_cohort_digest",
            "fuzzy_sample_count",
            "fuzzy_top10_max_ms",
            "fuzzy_top10_p50_ms",
            "fuzzy_top10_p95_ms",
            "migration_seconds",
            "oracle_query_count",
            "oracle_subset_digest",
            "passed",
            "path_config_digest",
            "peak_rss_mib",
            "percentile_method",
            "scorer_config_digest",
        ),
    )
    return BenchmarkReport(
        contract=_decode_benchmark_contract(payload["contract"]),
        contract_digest=payload["contract_digest"],
        corpus_digest=payload["corpus_digest"],
        corpus_composition_version=payload[
            "corpus_composition_version"
        ],
        corpus_composition_digest=payload[
            "corpus_composition_digest"
        ],
        exact_cohort_digest=payload["exact_cohort_digest"],
        fuzzy_cohort_digest=payload["fuzzy_cohort_digest"],
        oracle_subset_digest=payload["oracle_subset_digest"],
        scorer_config_digest=payload["scorer_config_digest"],
        execution_path=_decode_benchmark_execution_path(
            payload["execution_path"]
        ),
        path_config_digest=payload["path_config_digest"],
        exact_sample_count=payload["exact_sample_count"],
        fuzzy_sample_count=payload["fuzzy_sample_count"],
        oracle_query_count=payload["oracle_query_count"],
        percentile_method=payload["percentile_method"],
        candidate_recall=payload["candidate_recall"],
        exact_p50_ms=payload["exact_p50_ms"],
        exact_p95_ms=payload["exact_p95_ms"],
        exact_max_ms=payload["exact_max_ms"],
        fuzzy_top10_p50_ms=payload["fuzzy_top10_p50_ms"],
        fuzzy_top10_p95_ms=payload["fuzzy_top10_p95_ms"],
        fuzzy_top10_max_ms=payload["fuzzy_top10_max_ms"],
        migration_seconds=payload["migration_seconds"],
        peak_rss_mib=payload["peak_rss_mib"],
        passed=payload["passed"],
        failed_gates=_decode_string_tuple(
            payload["failed_gates"],
            "failed gates",
        ),
        environment=_decode_benchmark_environment(
            payload["environment"]
        ),
        environment_digest=payload["environment_digest"],
    )


def _decode_benchmark_suite_contract(
    value: object,
) -> BenchmarkSuiteContract:
    payload = _strict_fields(
        value,
        "BenchmarkSuiteContract payload",
        (
            "benchmark_contract",
            "benchmark_contract_digest",
            "required_paths",
            "suite_version",
        ),
    )
    return BenchmarkSuiteContract(
        suite_version=payload["suite_version"],
        benchmark_contract=_decode_benchmark_contract(
            payload["benchmark_contract"]
        ),
        benchmark_contract_digest=payload[
            "benchmark_contract_digest"
        ],
        required_paths=tuple(
            _decode_benchmark_execution_path(path)
            for path in _as_list(
                payload["required_paths"],
                "suite required paths",
            )
        ),
    )


def _decode_benchmark_suite_report(
    value: object,
) -> BenchmarkSuiteReport:
    payload = _strict_fields(
        value,
        "BenchmarkSuiteReport payload",
        (
            "corpus_composition_digest",
            "corpus_composition_version",
            "failed_paths",
            "passed",
            "path_reports",
            "suite_contract",
            "suite_contract_digest",
        ),
    )
    return BenchmarkSuiteReport(
        suite_contract=_decode_benchmark_suite_contract(
            payload["suite_contract"]
        ),
        suite_contract_digest=payload["suite_contract_digest"],
        corpus_composition_version=payload[
            "corpus_composition_version"
        ],
        corpus_composition_digest=payload[
            "corpus_composition_digest"
        ],
        path_reports=tuple(
            _decode_benchmark_report(path_report)
            for path_report in _as_list(
                payload["path_reports"],
                "suite path reports",
            )
        ),
        passed=payload["passed"],
        failed_paths=tuple(
            _decode_benchmark_execution_path(path)
            for path in _as_list(
                payload["failed_paths"],
                "suite failed paths",
            )
        ),
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
    if contract_type == "CandidateStageMetadata":
        return _decode_candidate_stage_metadata(value)
    if contract_type == "CandidateRecallMetadata":
        return _decode_candidate_recall_metadata(value)
    if contract_type == "CandidateEvidence":
        return _decode_candidate_evidence(value)
    if contract_type == "CandidateRetrievalReport":
        return _decode_candidate_retrieval_report(value)
    if contract_type == "ResourceQueryMetadata":
        return _decode_resource_query_metadata(value)
    if contract_type == "ResourceQueryFailure":
        return _decode_failure(value)
    if contract_type == "QueryReport":
        payload = _strict_fields(
            value,
            "QueryReport payload",
            ("resource_failures", "resource_metadata", "results"),
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
            resource_metadata=tuple(
                _decode_resource_query_metadata(metadata)
                for metadata in _as_list(
                    payload["resource_metadata"],
                    "resource_metadata",
                )
            ),
        )
    if contract_type == "BenchmarkContract":
        return _decode_benchmark_contract(value)
    if contract_type == "BenchmarkReport":
        return _decode_benchmark_report(value)
    if contract_type == "BenchmarkSuiteContract":
        return _decode_benchmark_suite_contract(value)
    if contract_type == "BenchmarkSuiteReport":
        return _decode_benchmark_suite_report(value)
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
    "BENCHMARK_CONTRACT_VERSION",
    "BENCHMARK_PERCENTILE_METHOD",
    "BENCHMARK_RSS_SCOPE",
    "BENCHMARK_SUITE_VERSION",
    "CANONICAL_RESOURCE_IDENTITY_VERSION",
    "CANDIDATE_BUDGET_VERSION",
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
    "BenchmarkContract",
    "BenchmarkExecutionPath",
    "BenchmarkReport",
    "BenchmarkSuiteContract",
    "BenchmarkSuiteReport",
    "CanonicalResourceIdentity",
    "CandidateEvidence",
    "CandidateRecallMetadata",
    "CandidateRetrievalReport",
    "CandidateStage",
    "CandidateStageMetadata",
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
    "ResourceQueryMetadata",
    "SchemaUpgradeFailure",
    "SchemaUpgradeOutcome",
    "SchemaUpgradeReport",
    "SearchHit",
    "SearchOptions",
    "SealedStage",
    "SimilarityEvidence",
    "SimilarityScorer",
    "SnapshotBinding",
    "SnapshotKind",
    "SnapshotManifest",
    "SnapshotReceipt",
    "SourceBindingState",
    "StageValidationEvidence",
    "StoreHealth",
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
    "benchmark_contract_digest",
    "benchmark_environment_digest",
    "benchmark_suite_contract_digest",
    "candidate_budget_v1",
    "contract_from_json",
    "contract_to_json",
    "snapshot_receipt_digest",
    "stage_validation_evidence_digest",
    "validate_resource_handles",
]
