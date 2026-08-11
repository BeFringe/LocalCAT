"""Exact/context classification and fuzzy scoring seams for TM retrieval.

Task 7.1 production slice: a per-resource exact/context classifier that
Task 7.3 composes into the full query pipeline.  It consumes only the
leased ``TMStore.exact_records`` port and performs no persistence, scoring,
capability or limit side effects.

Task 7.2 production slice: a storage-independent fuzzy scoring seam over an
already coherent per-resource recall report and its batch-loaded canonical
records.  It retains both query and matched source, applies minimum
similarity before any ordering, deduplicates by (resource_id, record_id),
and performs no store lease, persistence, apply/confirm, capability
publication or cross-resource global limiting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, cast

from tm_contracts import (
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    ContextEvidence,
    SCORER_VERSION_V1,
    SimilarityEvidence,
    SimilarityScorer,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMResourceHandle,
    TMResult,
)
from tm_similarity import SimilarityScorerV1


_CONTEXT_FIELD_NAMES = (
    "speaker_raw",
    "context_prev_raw",
    "context_next_raw",
)

_EMPTY_CONTEXT_EVIDENCE = ContextEvidence(
    comparable_fields=(),
    matched_fields=(),
    mismatched_fields=(),
    strength_v1=(0, 0, 0, 0, 0),
)


def _require_exact_type(value: object, expected_type: type[Any], label: str) -> None:
    if type(value) is not expected_type:
        raise TypeError(f"{label} must be an exact {expected_type.__name__}")


def _require_exact_tuple(value: object, label: str) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an exact tuple")
    return value


def _require_builtin_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")


def _require_builtin_str(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if value == "":
        raise ValueError(f"{label} must not be empty")


def _require_builtin_ratio(value: object, label: str) -> None:
    if type(value) not in (int, float):
        raise TypeError(f"{label} must be a number")
    numeric_value = cast(Any, value)
    if not math.isfinite(numeric_value) or not 0.0 <= numeric_value <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")


def _require_snapshot_provenance(value: object) -> None:
    pairs = _require_exact_tuple(value, "provenance")
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError("provenance entries must be two-item tuples")
        key, item_value = pair
        _require_builtin_str(key, "provenance key")
        if not key.strip():
            raise ValueError("provenance keys must not be empty")
        if type(item_value) is not str:
            raise TypeError("provenance values must be strings")


@dataclass(frozen=True)
class _FuzzyQuerySnapshot:
    """Private pre-callback copy of every query scalar used by scoring."""

    query_source: str
    minimum_similarity: float
    limit: int


@dataclass(frozen=True)
class _FuzzyRecordSnapshot:
    """Private pre-callback copy of every record field used after scoring."""

    record_id: int
    source_raw: str
    target_raw: str
    provenance: tuple[tuple[str, str], ...]


def _snapshot_query(query: TMQuery) -> _FuzzyQuerySnapshot:
    _require_exact_type(query, TMQuery, "query")
    query_source = query.query_source
    _require_builtin_str(query_source, "query source")
    minimum_similarity = query.minimum_similarity
    _require_builtin_ratio(minimum_similarity, "minimum similarity")
    limit = query.limit
    _require_builtin_int(limit, "query limit", minimum=1)
    return _FuzzyQuerySnapshot(
        query_source=query_source,
        minimum_similarity=minimum_similarity,
        limit=limit,
    )


def _snapshot_candidate_stage_metadata(
    stage_metadata: CandidateStageMetadata,
) -> CandidateStageMetadata:
    _require_exact_type(
        stage_metadata,
        CandidateStageMetadata,
        "candidate stage metadata",
    )
    stage = stage_metadata.stage
    _require_exact_type(stage, CandidateStage, "candidate stage")
    return CandidateStageMetadata(
        stage=stage,
        input_count=stage_metadata.input_count,
        added_unique_count=stage_metadata.added_unique_count,
        output_unique_count=stage_metadata.output_unique_count,
        dropped_count=stage_metadata.dropped_count,
    )


def _snapshot_recall_metadata(
    metadata: CandidateRecallMetadata,
) -> CandidateRecallMetadata:
    _require_exact_type(
        metadata,
        CandidateRecallMetadata,
        "report metadata",
    )
    stages = _require_exact_tuple(metadata.stages, "candidate stages")
    stage_snapshots = tuple(
        _snapshot_candidate_stage_metadata(stage_metadata)
        for stage_metadata in stages
    )
    return CandidateRecallMetadata(
        resource_id=metadata.resource_id,
        index_kind=metadata.index_kind,
        fuzzy_available=metadata.fuzzy_available,
        fuzzy_unavailable_code=metadata.fuzzy_unavailable_code,
        stages=stage_snapshots,
        union_unique_count=metadata.union_unique_count,
        deduplicated_count=metadata.deduplicated_count,
        result_limit=metadata.result_limit,
        candidate_budget_version=metadata.candidate_budget_version,
        candidate_budget=metadata.candidate_budget,
        truncated=metadata.truncated,
    )


def _snapshot_candidate_evidence(
    candidate: CandidateEvidence,
) -> CandidateEvidence:
    _require_exact_type(candidate, CandidateEvidence, "candidate evidence")
    recall_stages = _require_exact_tuple(
        candidate.recall_stages,
        "candidate recall stages",
    )
    for stage in recall_stages:
        _require_exact_type(stage, CandidateStage, "candidate recall stage")
    return CandidateEvidence(
        record_id=candidate.record_id,
        recall_stages=tuple(recall_stages),
        matched_grams=candidate.matched_grams,
        query_grams=candidate.query_grams,
        overlap_ratio=candidate.overlap_ratio,
        pretruncate_rank=candidate.pretruncate_rank,
    )


def _snapshot_candidate_report(
    report: CandidateRetrievalReport,
) -> CandidateRetrievalReport:
    _require_exact_type(report, CandidateRetrievalReport, "report")
    metadata_snapshot = _snapshot_recall_metadata(report.metadata)
    candidates = _require_exact_tuple(report.candidates, "candidate values")
    candidate_snapshots = tuple(
        _snapshot_candidate_evidence(candidate)
        for candidate in candidates
    )
    return CandidateRetrievalReport(
        candidates=candidate_snapshots,
        metadata=metadata_snapshot,
    )


def _snapshot_record(record: TMRecord) -> _FuzzyRecordSnapshot:
    _require_exact_type(record, TMRecord, "record")
    record_id = record.record_id
    _require_builtin_int(record_id, "record id", minimum=1)
    source_raw = record.source_raw
    _require_builtin_str(source_raw, "record source_raw")
    target_raw = record.target_raw
    _require_builtin_str(target_raw, "record target_raw")
    provenance = record.provenance
    _require_snapshot_provenance(provenance)
    provenance_snapshot = tuple(
        (key, item_value) for key, item_value in provenance
    )
    return _FuzzyRecordSnapshot(
        record_id=record_id,
        source_raw=source_raw,
        target_raw=target_raw,
        provenance=provenance_snapshot,
    )


def _snapshot_evidence(evidence: SimilarityEvidence) -> SimilarityEvidence:
    _require_exact_type(evidence, SimilarityEvidence, "scorer evidence")
    levenshtein_ratio = evidence.levenshtein_ratio
    dice_bigram = evidence.dice_bigram
    final_similarity = evidence.final_similarity
    scorer_version = evidence.scorer_version
    _require_builtin_ratio(levenshtein_ratio, "levenshtein ratio")
    _require_builtin_ratio(dice_bigram, "dice bigram")
    _require_builtin_ratio(final_similarity, "final similarity")
    if type(scorer_version) is not str:
        raise TypeError("scorer version must be a string")
    if scorer_version != SCORER_VERSION_V1:
        raise ValueError(f"scorer version must be {SCORER_VERSION_V1}")
    return SimilarityEvidence(
        levenshtein_ratio=levenshtein_ratio,
        dice_bigram=dice_bigram,
        final_similarity=final_similarity,
        scorer_version=scorer_version,
    )


def _empty_context_evidence() -> ContextEvidence:
    return ContextEvidence(
        comparable_fields=(),
        matched_fields=(),
        mismatched_fields=(),
        strength_v1=(0, 0, 0, 0, 0),
    )


def _is_nonempty_builtin_string(value: object) -> bool:
    return type(value) is str and value != ""


def compare_context_v1(*, query: TMQuery, record: TMRecord) -> ContextEvidence:
    """Compare one record's raw context facts against a query under context-v1.

    A field is comparable only when both sides are non-empty built-in strings.
    Comparable fields are compared by raw full-string equality; case and
    whitespace differences are mismatches.  Missing facts are never invented.
    """

    _require_exact_type(query, TMQuery, "query")
    _require_exact_type(record, TMRecord, "record")

    comparable: list[str] = []
    matched: list[str] = []
    mismatched: list[str] = []
    for field_name in _CONTEXT_FIELD_NAMES:
        query_value = getattr(query, field_name)
        record_value = getattr(record, field_name)
        if not (
            _is_nonempty_builtin_string(query_value)
            and _is_nonempty_builtin_string(record_value)
        ):
            continue
        comparable.append(field_name)
        if query_value == record_value:
            matched.append(field_name)
        else:
            mismatched.append(field_name)

    speaker_match = int("speaker_raw" in matched)
    prev_match = int("context_prev_raw" in matched)
    next_match = int("context_next_raw" in matched)
    return ContextEvidence(
        comparable_fields=tuple(comparable),
        matched_fields=tuple(matched),
        mismatched_fields=tuple(mismatched),
        strength_v1=(
            len(matched),
            -len(mismatched),
            speaker_match,
            prev_match,
            next_match,
        ),
    )


@dataclass(frozen=True)
class ExactContextClassification:
    """Per-resource exact/context classification of one exact-source group.

    ``winner`` is the sole EXACT result (maximum valid record id).
    ``context_results`` are same-source variants with positive raw context
    evidence, ordered by record id descending.  ``retained_only_variants``
    are same-source records without positive context evidence: retained for
    export/audit but omitted from returned suggestions.
    """

    resource_id: str
    winner: TMResult | None
    context_results: tuple[TMResult, ...]
    retained_only_variants: tuple[TMRecord, ...]

    @property
    def returned_results(self) -> tuple[TMResult, ...]:
        """Return suggestions in the per-resource EXACT then CONTEXT order."""

        if self.winner is None:
            return self.context_results
        return (self.winner,) + self.context_results


def classify_exact_context(
    *,
    resource_id: str,
    resource_order: int,
    query: TMQuery,
    records: tuple[TMRecord, ...],
) -> ExactContextClassification:
    """Classify one resource's exact-source records into winner and variants.

    The maximum valid ``record_id`` is the sole compatibility EXACT winner,
    independent of the order in which records are supplied.  Other
    same-source variants become CONTEXT only when they carry at least one
    matched raw context fact; otherwise they are retained but omitted.
    """

    _require_exact_type(resource_id, str, "resource_id")
    _require_exact_type(resource_order, int, "resource_order")
    _require_exact_type(query, TMQuery, "query")
    _require_exact_type(records, tuple, "records")
    if not resource_id.strip():
        raise ValueError("resource_id must not be empty")
    if resource_order < 0:
        raise ValueError("resource_order must be non-negative")
    record_ids: set[int] = set()
    for record in records:
        _require_exact_type(record, TMRecord, "record")
        if record.source_raw != query.query_source:
            raise ValueError("records must belong to the raw exact source")
        if record.record_id in record_ids:
            raise ValueError("records must have unique record ids")
        record_ids.add(record.record_id)

    if not records:
        return ExactContextClassification(
            resource_id=resource_id,
            winner=None,
            context_results=(),
            retained_only_variants=(),
        )

    winner_record = max(records, key=lambda record: record.record_id)
    winner = TMResult(
        resource_id=resource_id,
        record_id=winner_record.record_id,
        query_source=query.query_source,
        matched_source=winner_record.source_raw,
        target=winner_record.target_raw,
        match_type=TMMatchType.EXACT,
        similarity=1.0,
        similarity_evidence=None,
        context_evidence=_EMPTY_CONTEXT_EVIDENCE,
        provenance=winner_record.provenance,
        stable_tie_key=(resource_order, winner_record.record_id),
    )

    context_results: list[TMResult] = []
    retained_only: list[TMRecord] = []
    for record in records:
        if record.record_id == winner_record.record_id:
            continue
        evidence = compare_context_v1(query=query, record=record)
        if evidence.matched_fields:
            context_results.append(
                TMResult(
                    resource_id=resource_id,
                    record_id=record.record_id,
                    query_source=query.query_source,
                    matched_source=record.source_raw,
                    target=record.target_raw,
                    match_type=TMMatchType.CONTEXT,
                    similarity=1.0,
                    similarity_evidence=None,
                    context_evidence=evidence,
                    provenance=record.provenance,
                    stable_tie_key=(resource_order, record.record_id),
                )
            )
        else:
            retained_only.append(record)

    context_results.sort(key=lambda result: result.record_id, reverse=True)
    retained_only.sort(key=lambda record: record.record_id, reverse=True)
    return ExactContextClassification(
        resource_id=resource_id,
        winner=winner,
        context_results=tuple(context_results),
        retained_only_variants=tuple(retained_only),
    )


def query_resource_exact(
    *,
    handle: TMResourceHandle,
    query: TMQuery,
) -> ExactContextClassification:
    """Classify one resource through its leased ``exact_records`` store port.

    Callers must only pass handles selected for lookup; the Active+Lookup
    resource gate is owned by the Task 7.3 aggregation layer.  This seam
    performs no persistence, scoring, capability publication or limit
    side effects.
    """

    _require_exact_type(handle, TMResourceHandle, "handle")
    _require_exact_type(query, TMQuery, "query")
    records = handle.store.exact_records(query.query_source)
    return classify_exact_context(
        resource_id=handle.resource_id,
        resource_order=handle.order,
        query=query,
        records=records,
    )


@dataclass(frozen=True)
class FuzzyScoringResult:
    """One resource's accepted fuzzy suggestions for Task 7.3 composition.

    ``accepted`` is the deterministic per-resource fuzzy order and is
    intentionally unbounded by any cross-resource limit: the final global
    slice belongs to Task 7.3.  ``scored_count`` is the measured number of
    candidate records scored by scorer-v1; it never fabricates recall,
    threshold or global-limit claims.
    """

    resource_id: str
    resource_order: int
    accepted: tuple[TMResult, ...]
    scored_count: int

    def __post_init__(self) -> None:
        _require_exact_type(self.resource_id, str, "resource_id")
        _require_exact_type(self.resource_order, int, "resource_order")
        _require_exact_type(self.accepted, tuple, "accepted")
        _require_exact_type(self.scored_count, int, "scored_count")
        if not self.resource_id.strip():
            raise ValueError("resource_id must not be empty")
        if self.resource_order < 0:
            raise ValueError("resource_order must be non-negative")
        if self.scored_count < 0:
            raise ValueError("scored_count must be non-negative")
        if len(self.accepted) > self.scored_count:
            raise ValueError("accepted count must not exceed scored count")
        seen: set[tuple[str, int]] = set()
        previous: TMResult | None = None
        for result in self.accepted:
            _require_exact_type(result, TMResult, "result")
            if result.match_type is not TMMatchType.FUZZY:
                raise ValueError("accepted results must be fuzzy")
            if result.resource_id != self.resource_id:
                raise ValueError(
                    "accepted result resource id must match the result"
                )
            tie_key = _require_exact_tuple(
                result.stable_tie_key,
                "stable tie key",
            )
            if len(tie_key) != 2:
                raise ValueError(
                    "stable tie key must contain resource order and record id"
                )
            if type(tie_key[0]) is not int or type(tie_key[1]) is not int:
                raise TypeError("stable tie key must contain integers")
            if tie_key[1] != result.record_id:
                raise ValueError(
                    "stable tie record id must equal result record id"
                )
            if tie_key[0] != self.resource_order:
                raise ValueError(
                    "accepted result tie order must match resource_order"
                )
            identity = (result.resource_id, result.record_id)
            if identity in seen:
                raise ValueError(
                    "accepted results must be deduplicated by "
                    "resource id and record id"
                )
            seen.add(identity)
            if previous is not None and (
                result.similarity > previous.similarity
                or (
                    result.similarity == previous.similarity
                    and result.record_id > previous.record_id
                )
            ):
                raise ValueError(
                    "accepted results must be in deterministic fuzzy order"
                )
            previous = result


def score_fuzzy_candidates(
    *,
    resource_id: str,
    resource_order: int,
    query: TMQuery,
    report: CandidateRetrievalReport,
    records: tuple[TMRecord, ...],
    scorer: SimilarityScorer | None = None,
) -> FuzzyScoringResult:
    """Score one resource's bounded candidates into accepted fuzzy results.

    Consumes an already coherent per-resource recall report and the
    batch-loaded canonical records for its candidate ids; it never opens a
    store or candidate lease.  Same-source records belong to Task 7.1
    EXACT/CONTEXT handling and are omitted without scoring.  Each accepted
    record is scored by scorer-v1 on ``query.query_source`` and the record's
    raw source; the frozen evidence is kept unrounded.  Threshold filtering
    (boundary equality included) happens before the stable per-resource
    order, and no cross-resource global limit is applied here.  All query
    scalars, record fields, candidate identities and resource/result-limit
    bindings are validated and rebuilt into a private pre-callback snapshot,
    and each scorer evidence is rebuilt into a private validated value
    before result construction, so a scorer mutating caller-owned aliases
    cannot corrupt the returned results.
    """

    _require_exact_type(resource_id, str, "resource_id")
    _require_exact_type(resource_order, int, "resource_order")
    _require_exact_type(query, TMQuery, "query")
    _require_exact_type(report, CandidateRetrievalReport, "report")
    _require_exact_type(records, tuple, "records")
    if not resource_id.strip():
        raise ValueError("resource_id must not be empty")
    if resource_order < 0:
        raise ValueError("resource_order must be non-negative")

    report_snapshot = _snapshot_candidate_report(report)
    report_resource_id = report_snapshot.metadata.resource_id
    report_result_limit = report_snapshot.metadata.result_limit
    query_snapshot = _snapshot_query(query)
    if report_resource_id != resource_id:
        raise ValueError("report must belong to resource_id")
    if report_result_limit != query_snapshot.limit:
        raise ValueError("report result limit must equal query limit")
    candidate_ids = tuple(
        candidate.record_id for candidate in report_snapshot.candidates
    )
    records_by_id: dict[int, _FuzzyRecordSnapshot] = {}
    for record in records:
        record_snapshot = _snapshot_record(record)
        if record_snapshot.record_id in records_by_id:
            raise ValueError("records must have unique record ids")
        records_by_id[record_snapshot.record_id] = record_snapshot
    if set(candidate_ids) != set(records_by_id):
        raise ValueError(
            "records must correspond exactly to candidate ids"
        )

    score_callable: Callable[[str, str], SimilarityEvidence]
    if scorer is None:
        score_callable = SimilarityScorerV1().score
    else:
        scorer_score = getattr(scorer, "score", None)
        if not callable(scorer_score):
            raise TypeError(
                "scorer must implement the SimilarityScorer score port"
            )
        score_callable = cast(
            Callable[[str, str], SimilarityEvidence],
            scorer_score,
        )

    accepted: list[TMResult] = []
    scored_count = 0
    for record_id in candidate_ids:
        record = records_by_id[record_id]
        if record.source_raw == query_snapshot.query_source:
            continue
        evidence = score_callable(
            query_snapshot.query_source,
            record.source_raw,
        )
        evidence_snapshot = _snapshot_evidence(evidence)
        scored_count += 1
        if (
            evidence_snapshot.final_similarity
            < query_snapshot.minimum_similarity
        ):
            continue
        accepted.append(
            TMResult(
                resource_id=resource_id,
                record_id=record.record_id,
                query_source=query_snapshot.query_source,
                matched_source=record.source_raw,
                target=record.target_raw,
                match_type=TMMatchType.FUZZY,
                similarity=evidence_snapshot.final_similarity,
                similarity_evidence=evidence_snapshot,
                context_evidence=_empty_context_evidence(),
                provenance=record.provenance,
                stable_tie_key=(resource_order, record.record_id),
            )
        )

    accepted.sort(
        key=lambda result: (
            -result.similarity,
            -result.record_id,
        ),
    )
    return FuzzyScoringResult(
        resource_id=resource_id,
        resource_order=resource_order,
        accepted=tuple(accepted),
        scored_count=scored_count,
    )
