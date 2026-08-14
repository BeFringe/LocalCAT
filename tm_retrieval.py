"""Exact/context classification, fuzzy scoring and retrieval composition.

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

Task 7.3 production slice: the multi-resource ``TMRetrievalService`` that
composes Task 7.1 and Task 7.2 under exactly one read-only query lease per
Active+Lookup resource, aggregates in stable EXACT/CONTEXT/FUZZY order, and
applies the global limit only after cross-resource aggregation.  It performs
no persistence, apply/confirm, activation/update, capability publication or
write side effects, and isolates every resource-local failure.

Task 7.4 production slice: the same service captures the publisher's
immutable ``RetrievalCapabilitySnapshot`` exactly once before reading any
participating resource and threads that snapshot through the whole query, so
a refresh during an in-flight multi-resource query affects only the next
query.  Query-effective CONTEXT/FUZZY availability is combined in memory from
physical store health and the captured snapshot; closed gates never touch
retriever, candidate record or scorer ports.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, cast

from text_matcher import fold_text_v1
from tm_candidate_index import (
    CandidateProofBudgetExhausted,
    CandidateProofSession,
    CandidateRetriever,
)
from tm_retrieval_capability import (
    RetrievalCapabilityEvidenceSummary,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalContextDecision,
    RetrievalFuzzyCoreDecision,
    RetrievalFuzzyPathDecision,
    default_retrieval_capability_publisher,
)
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CandidateEvidence,
    CandidateProofMetadata,
    CandidateProofRefinementMetadata,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    ContextEvidence,
    QueryReport,
    ResourceQueryFailure,
    ResourceQueryMetadata,
    SCORER_VERSION_V1,
    SimilarityEvidence,
    SimilarityScorer,
    StoreHealth,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMResourceHandle,
    TMResult,
    candidate_budget_v1,
)
from tm_similarity import SimilarityScorerV1
from tm_sqlite_store import SQLiteStoreLifecycleError, SQLiteStoreSchemaError


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
    proof = metadata.proof
    proof_snapshot = (
        None
        if proof is None
        else _snapshot_candidate_proof_metadata(proof)
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
        proof=proof_snapshot,
    )


def _snapshot_candidate_proof_metadata(
    metadata: CandidateProofMetadata,
) -> CandidateProofMetadata:
    _require_exact_type(
        metadata,
        CandidateProofMetadata,
        "candidate proof metadata",
    )
    refinement = metadata.refinement
    if refinement is not None:
        _require_exact_type(
            refinement,
            CandidateProofRefinementMetadata,
            "candidate proof refinement metadata",
        )
        refinement = CandidateProofRefinementMetadata(**refinement.__dict__)
    return CandidateProofMetadata(
        proof_version=metadata.proof_version,
        bound_version=metadata.bound_version,
        block_version=metadata.block_version,
        traversal_version=metadata.traversal_version,
        traversal_mode=metadata.traversal_mode,
        total_block_count=metadata.total_block_count,
        total_record_count=metadata.total_record_count,
        scanned_block_count=metadata.scanned_block_count,
        opened_block_count=metadata.opened_block_count,
        inspected_record_count=metadata.inspected_record_count,
        seed_unique_count=metadata.seed_unique_count,
        scorer_invocation_count=metadata.scorer_invocation_count,
        accounted_identity_count=metadata.accounted_identity_count,
        unscored_identity_count=metadata.unscored_identity_count,
        unscored_max_upper_bound=metadata.unscored_max_upper_bound,
        unscored_possible_record_id=metadata.unscored_possible_record_id,
        minimum_similarity=metadata.minimum_similarity,
        threshold_closed=metadata.threshold_closed,
        top_k=metadata.top_k,
        kth_score=metadata.kth_score,
        kth_record_id=metadata.kth_record_id,
        top_k_closed=metadata.top_k_closed,
        refinement=refinement,
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


def prove_and_score_fuzzy_candidates(
    *,
    resource_id: str,
    resource_order: int,
    query: TMQuery,
    view: object,
    retriever: CandidateRetriever | None = None,
    scorer: SimilarityScorer | None = None,
    proof_session_port: Callable[..., CandidateProofSession] | None = None,
) -> tuple[FuzzyScoringResult, CandidateRetrievalReport]:
    """Alternate conservative proof expansion with scorer-v1 execution.

    Candidate ownership supplies only bounded seed/proof facts and receives
    exact scorer observations.  Retrieval derives query-local equivalence
    only from complete fold-v1 equality on loaded records, invokes its own
    concrete scorer-v1 once per new class, and fans immutable evidence out to
    every independently retained record identity.
    """

    _require_exact_type(resource_id, str, "resource_id")
    _require_exact_type(resource_order, int, "resource_order")
    _require_exact_type(query, TMQuery, "query")
    if not resource_id.strip():
        raise ValueError("resource_id must not be empty")
    if resource_order < 0:
        raise ValueError("resource_order must be non-negative")
    if scorer is not None:
        raise ValueError(
            "proof-query-v2 requires the production scorer-v1 owner"
        )
    query_snapshot = _snapshot_query(query)
    folded_query = fold_text_v1(query_snapshot.query_source).folded_text
    if type(folded_query) is not str or not folded_query:
        raise ValueError("proof query must fold to non-empty text")
    if proof_session_port is None:
        if retriever is None:
            retriever = CandidateRetriever()
        candidate_port = getattr(retriever, "proof_session_from_view", None)
        if not callable(candidate_port):
            raise TypeError("retriever must implement proof_session_from_view")
        proof_session_port = cast(
            Callable[..., CandidateProofSession],
            candidate_port,
        )
    session = cast(
        CandidateProofSession,
        proof_session_port(
            resource_id,
            view,
            folded_query,
            minimum_similarity=float(query_snapshot.minimum_similarity),
            result_limit=query_snapshot.limit,
        ),
    )
    records_port = getattr(view, "records_by_id", None)
    if not callable(records_port):
        raise TypeError("query view must implement records_by_id")
    score_callable = SimilarityScorerV1().score

    observed: dict[int, tuple[_FuzzyRecordSnapshot, SimilarityEvidence]] = {}
    evidence_by_fold: dict[str, SimilarityEvidence] = {}
    fold_by_raw_source: dict[str, str] = {}
    scorer_invocation_count = 0
    scorer_budget = candidate_budget_v1(query_snapshot.limit)
    while True:
        record_ids = session.next_batch()
        _require_exact_type(record_ids, tuple, "proof record ids")
        if not record_ids:
            break
        raw_records = records_port(record_ids)
        _require_exact_type(raw_records, tuple, "proof records")
        records = cast(tuple[TMRecord, ...], raw_records)
        if len(records) != len(record_ids):
            raise ValueError("proof records must close the requested batch")
        snapshots = tuple(_snapshot_record(record) for record in records)
        if tuple(record.record_id for record in snapshots) != record_ids:
            raise ValueError("proof records must preserve requested identity order")
        observations: list[tuple[int, SimilarityEvidence, bool]] = []
        for record in snapshots:
            if record.record_id in observed:
                raise ValueError("proof identity must be scored exactly once")
            candidate_fold = fold_by_raw_source.get(record.source_raw)
            if candidate_fold is None:
                candidate_fold = fold_text_v1(record.source_raw).folded_text
                fold_by_raw_source[record.source_raw] = candidate_fold
            if type(candidate_fold) is not str or not candidate_fold:
                raise ValueError("proof candidate must fold to non-empty text")
            evidence = evidence_by_fold.get(candidate_fold)
            scorer_invoked = evidence is None
            if scorer_invoked:
                if scorer_invocation_count >= scorer_budget:
                    raise CandidateProofBudgetExhausted()
                evidence = _snapshot_evidence(
                    score_callable(
                        query_snapshot.query_source,
                        record.source_raw,
                    )
                )
                scorer_invocation_count += 1
                evidence_by_fold[candidate_fold] = evidence
            assert evidence is not None
            if evidence.final_similarity != (
                evidence.levenshtein_ratio + evidence.dice_bigram
            ) / 2.0:
                raise ValueError("scorer-v1 evidence components do not close")
            observed[record.record_id] = (record, evidence)
            observations.append((record.record_id, evidence, scorer_invoked))
        session.observe(tuple(observations))

    report = _snapshot_candidate_report(session.finish())
    candidate_ids = tuple(candidate.record_id for candidate in report.candidates)
    if candidate_ids != tuple(observed):
        raise ValueError("proof report identities must equal scorer observations")
    proof = report.metadata.proof
    if (
        proof is None
        or not proof.threshold_closed
        or not proof.top_k_closed
        or proof.scorer_invocation_count != scorer_invocation_count
        or proof.scorer_invocation_count != len(evidence_by_fold)
        or proof.accounted_identity_count != len(observed)
        or (
            proof.accounted_identity_count + proof.unscored_identity_count
            != proof.total_record_count
        )
    ):
        raise ValueError("proof report must publish closed scorer conservation")

    accepted: list[TMResult] = []
    for record, evidence in observed.values():
        if record.source_raw == query_snapshot.query_source:
            continue
        if evidence.final_similarity < query_snapshot.minimum_similarity:
            continue
        accepted.append(TMResult(
            resource_id=resource_id,
            record_id=record.record_id,
            query_source=query_snapshot.query_source,
            matched_source=record.source_raw,
            target=record.target_raw,
            match_type=TMMatchType.FUZZY,
            similarity=evidence.final_similarity,
            similarity_evidence=evidence,
            context_evidence=_empty_context_evidence(),
            provenance=record.provenance,
            stable_tie_key=(resource_order, record.record_id),
        ))
    accepted.sort(key=lambda result: (-result.similarity, -result.record_id))
    return (
        FuzzyScoringResult(
            resource_id=resource_id,
            resource_order=resource_order,
            accepted=tuple(accepted),
            scored_count=len(observed),
        ),
        report,
    )


# --- Task 7.3 service composition -------------------------------------------

_UNHEALTHY_CODE = "RETRIEVAL.STORE_UNHEALTHY"
_EXACT_UNAVAILABLE_CODE = "RETRIEVAL.EXACT_UNAVAILABLE"
_LEASE_UNAVAILABLE_CODE = "STORE.QUERY_LEASE_UNAVAILABLE"
_QUERY_VIEW_INVALID_CODE = "STORE.QUERY_VIEW_INVALID"
_QUERY_VIEW_FOREIGN_CODE = "STORE.QUERY_VIEW_FOREIGN"
_GENERATION_MISMATCH_CODE = "STORE.GENERATION_MISMATCH"
_RECALL_PATH_MISMATCH_CODE = "RETRIEVAL.RECALL_PATH_MISMATCH"
_NORMALIZED_FAILURE_CODE = "RETRIEVAL.QUERY_FAILED"
_FALLBACK_STAGE = "QUERY"

_TYPE_RANK = {
    TMMatchType.EXACT: 0,
    TMMatchType.CONTEXT: 1,
    TMMatchType.FUZZY: 2,
}

_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")


@dataclass(frozen=True)
class _ServiceHandleSnapshot:
    """Private pre-callback copy of every caller-owned handle scalar/ref."""

    resource_id: str
    store: object
    active: bool
    lookup: bool
    update: bool
    order: int


class _ResourcePipelineFailure(Exception):
    """Private per-resource stage failure carrying only stable safe codes."""

    def __init__(
        self,
        *,
        stage: str,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(error_code)
        self.stage = stage
        self.error_code = error_code
        self.retryable = retryable


def _snapshot_handle(handle: TMResourceHandle) -> _ServiceHandleSnapshot:
    _require_exact_type(handle, TMResourceHandle, "handle")
    resource_id = handle.resource_id
    _require_builtin_str(resource_id, "resource id")
    if not resource_id.strip():
        raise ValueError("resource id must not be empty")
    store = handle.store
    if store is None:
        raise ValueError("resource store binding must not be None")
    active = handle.active
    lookup = handle.lookup
    update = handle.update
    _require_exact_type(active, bool, "resource active")
    _require_exact_type(lookup, bool, "resource lookup")
    _require_exact_type(update, bool, "resource update")
    order = handle.order
    _require_builtin_int(order, "resource order", minimum=0)
    return _ServiceHandleSnapshot(
        resource_id=resource_id,
        store=store,
        active=active,
        lookup=lookup,
        update=update,
        order=order,
    )


def _snapshot_handles(
    handles: object,
) -> tuple[_ServiceHandleSnapshot, ...]:
    items = _require_exact_tuple(handles, "resource handles")
    snapshots = tuple(_snapshot_handle(item) for item in items)
    resource_ids = tuple(item.resource_id for item in snapshots)
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("resource ids must be unique")
    orders = tuple(item.order for item in snapshots)
    if len(orders) != len(set(orders)):
        raise ValueError("resource orders must be unique")
    return snapshots


def _snapshot_service_query(query: TMQuery) -> TMQuery:
    _require_exact_type(query, TMQuery, "query")
    query_source = query.query_source
    _require_builtin_str(query_source, "query source")
    if not query_source.strip():
        raise ValueError("query source must not be empty")
    speaker_raw = query.speaker_raw
    context_prev_raw = query.context_prev_raw
    context_next_raw = query.context_next_raw
    for label, value in (
        ("query speaker_raw", speaker_raw),
        ("query context_prev_raw", context_prev_raw),
        ("query context_next_raw", context_next_raw),
    ):
        if value is not None:
            _require_builtin_str(value, label)
    minimum_similarity = query.minimum_similarity
    _require_builtin_ratio(minimum_similarity, "minimum similarity")
    limit = query.limit
    _require_builtin_int(limit, "query limit", minimum=1)
    resource_order = _require_exact_tuple(
        query.resource_order,
        "query resource_order",
    )
    for item in resource_order:
        _require_builtin_str(item, "resource_order id")
        if not item.strip():
            raise ValueError("resource_order ids must not be empty")
    if len(resource_order) != len(set(resource_order)):
        raise ValueError("resource_order ids must be unique")
    return TMQuery(
        query_source=query_source,
        speaker_raw=speaker_raw,
        context_prev_raw=context_prev_raw,
        context_next_raw=context_next_raw,
        minimum_similarity=minimum_similarity,
        limit=limit,
        resource_order=resource_order,
    )


def _validate_service_mapping(
    snapshots: tuple[_ServiceHandleSnapshot, ...],
    query: TMQuery,
) -> None:
    """Enforce the one-to-one handle/query.resource_order tie mapping."""

    resource_ids = tuple(item.resource_id for item in snapshots)
    query_ids = query.resource_order
    if set(query_ids) != set(resource_ids):
        raise ValueError(
            "query resource_order must map one-to-one to handle ids"
        )
    position_by_id = {
        resource_id: position
        for position, resource_id in enumerate(query_ids)
    }
    for snapshot in snapshots:
        if snapshot.order != position_by_id[snapshot.resource_id]:
            raise ValueError(
                "handle order must match its query resource_order position"
            )


def _snapshot_store_health(health: StoreHealth) -> StoreHealth:
    _require_exact_type(health, StoreHealth, "store health")
    healthy = health.healthy
    schema_version = health.schema_version
    generation = health.generation
    record_count = health.record_count
    index_kind = health.index_kind
    snapshot_binding_digest = health.snapshot_binding_digest
    source_binding_state = health.source_binding_state
    exact_available = health.exact_available
    context_available = health.context_available
    fuzzy_available = health.fuzzy_available
    diagnostic_codes = health.diagnostic_codes
    return StoreHealth(
        healthy=healthy,
        schema_version=schema_version,
        generation=generation,
        record_count=record_count,
        index_kind=index_kind,
        snapshot_binding_digest=snapshot_binding_digest,
        source_binding_state=source_binding_state,
        exact_available=exact_available,
        context_available=context_available,
        fuzzy_available=fuzzy_available,
        diagnostic_codes=diagnostic_codes,
    )


def _snapshot_capability_snapshot(
    snapshot: RetrievalCapabilitySnapshot,
) -> RetrievalCapabilitySnapshot:
    """Clone one capability snapshot into fresh exact-typed values.

    The query consumes its own private copy so a publisher refresh or a
    tampered frozen alias during in-flight callbacks can never rebind or
    corrupt the decisions that already closed this query.
    """

    _require_exact_type(
        snapshot,
        RetrievalCapabilitySnapshot,
        "capability snapshot",
    )
    return RetrievalCapabilitySnapshot(
        semantics_version=snapshot.semantics_version,
        context=RetrievalContextDecision(
            available=snapshot.context.available,
            unavailable_code=snapshot.context.unavailable_code,
        ),
        fuzzy_core=RetrievalFuzzyCoreDecision(
            available=snapshot.fuzzy_core.available,
            unavailable_code=snapshot.fuzzy_core.unavailable_code,
        ),
        fts5_trigram=RetrievalFuzzyPathDecision(
            path=snapshot.fts5_trigram.path,
            available=snapshot.fts5_trigram.available,
            unavailable_code=snapshot.fts5_trigram.unavailable_code,
        ),
        gram_fallback=RetrievalFuzzyPathDecision(
            path=snapshot.gram_fallback.path,
            available=snapshot.gram_fallback.available,
            unavailable_code=snapshot.gram_fallback.unavailable_code,
        ),
        summary=RetrievalCapabilityEvidenceSummary(
            summary_version=snapshot.summary.summary_version,
            evidence_digest=snapshot.summary.evidence_digest,
            evaluated_at_utc=snapshot.summary.evaluated_at_utc,
            unavailable_codes=snapshot.summary.unavailable_codes,
        ),
    )


def _snapshot_exact_records(
    records: tuple[TMRecord, ...],
) -> tuple[TMRecord, ...]:
    items = _require_exact_tuple(records, "exact records")
    for record in items:
        _require_exact_type(record, TMRecord, "record")
    return items


def _unavailable_recall_metadata(
    *,
    resource_id: str,
    index_kind: str,
    result_limit: int,
    fuzzy_unavailable_code: str,
) -> CandidateRecallMetadata:
    return CandidateRecallMetadata(
        resource_id=resource_id,
        index_kind=index_kind,
        fuzzy_available=False,
        fuzzy_unavailable_code=fuzzy_unavailable_code,
        stages=(),
        union_unique_count=0,
        deduplicated_count=0,
        result_limit=result_limit,
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        candidate_budget=candidate_budget_v1(result_limit),
        truncated=False,
    )


def _safe_attr(error: object, name: str) -> object:
    try:
        return getattr(error, name, None)
    except Exception:
        return None


def _is_stable_code(value: object) -> bool:
    if type(value) is not str or value == "":
        return False
    return _DIAGNOSTIC_IDENTIFIER.fullmatch(value) is not None


def _stable_error_code(error: Exception) -> str:
    code = _safe_attr(error, "error_code")
    if _is_stable_code(code):
        return cast(str, code)
    code = _safe_attr(error, "code")
    if _is_stable_code(code):
        return cast(str, code)
    if isinstance(error, SQLiteStoreSchemaError):
        message = error.args[0] if error.args else None
        if _is_stable_code(message):
            return cast(str, message)
    return _NORMALIZED_FAILURE_CODE


def _stable_error_retryable(error: Exception) -> bool:
    retryable = _safe_attr(error, "retryable")
    return retryable if type(retryable) is bool else False


def _normalize_stage_error(
    error: Exception,
    stage: str,
) -> _ResourcePipelineFailure:
    return _ResourcePipelineFailure(
        stage=stage,
        error_code=_stable_error_code(error),
        retryable=_stable_error_retryable(error),
    )


@contextmanager
def _stage_guard(stage: str) -> Iterator[None]:
    try:
        yield
    except _ResourcePipelineFailure:
        raise
    except Exception as error:
        raise _normalize_stage_error(error, stage) from error


def _fold_query(query: TMQuery) -> str:
    folded_query = fold_text_v1(query.query_source).folded_text
    if type(folded_query) is not str:
        raise TypeError("folded query must be a built-in string")
    return folded_query


def _result_sort_key(result: TMResult) -> tuple[int, float, tuple[int, ...], int, int]:
    return (
        _TYPE_RANK[result.match_type],
        -result.similarity,
        tuple(-value for value in result.context_evidence.strength_v1),
        result.stable_tie_key[0],
        -result.record_id,
    )


class _LazyRetrieverPort:
    """Query-scoped retriever port captured once on first fuzzy use."""

    def __init__(self, retriever: object) -> None:
        self._retriever = retriever
        self._port: Callable[..., CandidateRetrievalReport] | None = None
        self._proof_port: Callable[..., CandidateProofSession] | None = None
        self._proof_checked = False

    def port(self) -> Callable[..., CandidateRetrievalReport]:
        if self._port is None:
            port = _safe_attr(self._retriever, "candidates_from_view")
            if not callable(port):
                raise TypeError(
                    "retriever must implement the candidates_from_view port"
                )
            self._port = cast(
                Callable[..., CandidateRetrievalReport],
                port,
            )
        return self._port

    def proof_port(self) -> Callable[..., CandidateProofSession] | None:
        if not self._proof_checked:
            port = _safe_attr(self._retriever, "proof_session_from_view")
            if callable(port):
                self._proof_port = cast(
                    Callable[..., CandidateProofSession],
                    port,
                )
            self._proof_checked = True
        return self._proof_port


class _LazyScorerPort:
    """Query-scoped scorer facade capturing the original score port once."""

    def __init__(self, scorer: object) -> None:
        self._scorer = scorer
        self._score: Callable[[str, str], SimilarityEvidence] | None = None

    @property
    def score(self) -> Callable[[str, str], SimilarityEvidence]:
        if self._score is None:
            port = getattr(self._scorer, "score", None)
            if not callable(port):
                raise TypeError(
                    "scorer must implement the SimilarityScorer score port"
                )
            self._score = cast(
                Callable[[str, str], SimilarityEvidence],
                port,
            )
        return self._score


class TMRetrievalService:
    """Deterministic multi-resource exact/context/fuzzy retrieval service.

    ``query`` snapshots every caller-owned handle and query value before any
    controllable callback, then processes each Active+Lookup resource in the
    declared ``TMQuery.resource_order``.  Each participating resource obtains
    exactly one ``store.query_lease`` context and consumes only the leased
    read-only view for health, raw exact records, candidate recall and the
    candidate record batch; public store query and write ports are never
    called.  Resource-local exceptions become one stable
    ``ResourceQueryFailure`` per resource, and other resources survive.
    Successful results are aggregated, stable-sorted by EXACT, CONTEXT,
    FUZZY / similarity / context strength / caller order / record id,
    deduplicated by (resource_id, record_id), and the global limit is
    applied only after cross-resource aggregation.  Dynamic retriever and
    scorer ports are captured lazily once per query on first fuzzy use and
    reused for every later fuzzy resource.  The capability publisher snapshot
    is captured once per query and shared by every participating resource;
    physical health provides only physical/canonical facts while CONTEXT and
    FUZZY query-effective availability come from that snapshot.
    """

    def __init__(
        self,
        *,
        retriever: CandidateRetriever | None = None,
        scorer: SimilarityScorer | None = None,
        capability_publisher: RetrievalCapabilityPublisher | None = None,
    ) -> None:
        self._proof_query_v2_authority = retriever is None and scorer is None
        if retriever is None:
            retriever = CandidateRetriever()
        if scorer is None:
            scorer = SimilarityScorerV1()
        if capability_publisher is None:
            capability_publisher = default_retrieval_capability_publisher(
                datetime.now(timezone.utc)
            )
        else:
            _require_exact_type(
                capability_publisher,
                RetrievalCapabilityPublisher,
                "capability publisher",
            )
        self._retriever = retriever
        self._scorer = scorer
        self._capability_publisher = capability_publisher

    def query(
        self,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> QueryReport:
        """Run one deterministic multi-resource query without side effects."""

        capability_snapshot = _snapshot_capability_snapshot(
            self._capability_publisher.snapshot()
        )
        snapshots = _snapshot_handles(resources)
        query_snapshot = _snapshot_service_query(query)
        _validate_service_mapping(snapshots, query_snapshot)
        lazy_retriever = _LazyRetrieverPort(self._retriever)
        lazy_scorer = _LazyScorerPort(self._scorer)
        folded_query = _fold_query(query_snapshot)

        snapshot_by_id = {
            snapshot.resource_id: snapshot for snapshot in snapshots
        }
        ordered_snapshots = tuple(
            snapshot_by_id[resource_id]
            for resource_id in query_snapshot.resource_order
        )

        local_outcomes: list[
            tuple[tuple[TMResult, ...], ResourceQueryMetadata]
        ] = []
        failures: list[ResourceQueryFailure] = []
        for snapshot in ordered_snapshots:
            if not (snapshot.active and snapshot.lookup):
                continue
            try:
                local_results, metadata, local_failure = self._query_resource(
                    snapshot,
                    query_snapshot,
                    capability_snapshot,
                    lazy_retriever,
                    lazy_scorer,
                    folded_query,
                    self._proof_query_v2_authority,
                )
            except _ResourcePipelineFailure as failure:
                failures.append(
                    ResourceQueryFailure(
                        resource_id=snapshot.resource_id,
                        stage=failure.stage,
                        error_code=failure.error_code,
                        retryable=failure.retryable,
                    )
                )
                continue
            except Exception as error:
                failures.append(
                    ResourceQueryFailure(
                        resource_id=snapshot.resource_id,
                        stage=_FALLBACK_STAGE,
                        error_code=_stable_error_code(error),
                        retryable=_stable_error_retryable(error),
                    )
                )
                continue
            local_outcomes.append((local_results, metadata))
            if local_failure is not None:
                failures.append(ResourceQueryFailure(
                    resource_id=snapshot.resource_id,
                    stage=local_failure.stage,
                    error_code=local_failure.error_code,
                    retryable=local_failure.retryable,
                ))

        all_results = [
            result
            for local_results, _metadata in local_outcomes
            for result in local_results
        ]
        ordered_results = sorted(all_results, key=_result_sort_key)
        deduplicated: list[TMResult] = []
        seen: set[tuple[str, int]] = set()
        for result in ordered_results:
            identity = (result.resource_id, result.record_id)
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(result)
        limited_results = deduplicated[: query_snapshot.limit]

        returned_by_resource: dict[str, int] = {}
        for result in limited_results:
            returned_by_resource[result.resource_id] = (
                returned_by_resource.get(result.resource_id, 0) + 1
            )
        resource_metadata = tuple(
            replace(
                metadata,
                returned_count=returned_by_resource.get(
                    metadata.resource_id,
                    0,
                ),
            )
            for _local_results, metadata in local_outcomes
        )
        return QueryReport(
            results=tuple(limited_results),
            resource_failures=tuple(failures),
            resource_metadata=resource_metadata,
        )

    def _query_resource(
        self,
        snapshot: _ServiceHandleSnapshot,
        query: TMQuery,
        capability_snapshot: RetrievalCapabilitySnapshot,
        lazy_retriever: _LazyRetrieverPort,
        lazy_scorer: _LazyScorerPort,
        folded_query: str,
        proof_query_v2_authority: bool,
    ) -> tuple[
        tuple[TMResult, ...],
        ResourceQueryMetadata,
        _ResourcePipelineFailure | None,
    ]:
        store = snapshot.store
        query_lease_port = _safe_attr(store, "query_lease")
        if not callable(query_lease_port):
            raise _ResourcePipelineFailure(
                stage="LEASE",
                error_code=_LEASE_UNAVAILABLE_CODE,
            )
        try:
            with cast(Any, query_lease_port)() as view:
                with _stage_guard("LEASE"):
                    health_port = _safe_attr(view, "health")
                    view_resource_id = _safe_attr(view, "resource_id")
                    view_generation = _safe_attr(view, "generation")
                    if not callable(health_port):
                        raise _ResourcePipelineFailure(
                            stage="LEASE",
                            error_code=_QUERY_VIEW_INVALID_CODE,
                        )
                    if type(view_resource_id) is not str:
                        raise _ResourcePipelineFailure(
                            stage="LEASE",
                            error_code=_QUERY_VIEW_INVALID_CODE,
                        )
                    if view_resource_id != snapshot.resource_id:
                        raise _ResourcePipelineFailure(
                            stage="LEASE",
                            error_code=_QUERY_VIEW_FOREIGN_CODE,
                        )
                    if (
                        type(view_generation) is not int
                        or isinstance(view_generation, bool)
                        or view_generation < 0
                    ):
                        raise _ResourcePipelineFailure(
                            stage="LEASE",
                            error_code=_QUERY_VIEW_INVALID_CODE,
                        )

                with _stage_guard("HEALTH"):
                    health_snapshot = _snapshot_store_health(
                        cast(Any, health_port)()
                    )
                    if not health_snapshot.healthy:
                        raise _ResourcePipelineFailure(
                            stage="HEALTH",
                            error_code=_UNHEALTHY_CODE,
                        )
                    if not health_snapshot.exact_available:
                        raise _ResourcePipelineFailure(
                            stage="HEALTH",
                            error_code=_EXACT_UNAVAILABLE_CODE,
                        )
                    if health_snapshot.generation != view_generation:
                        raise _ResourcePipelineFailure(
                            stage="HEALTH",
                            error_code=_GENERATION_MISMATCH_CODE,
                        )
                    intended_path = (
                        "FTS5_TRIGRAM"
                        if (
                            health_snapshot.index_kind == "FTS5_TRIGRAM"
                            and len(folded_query) >= 3
                        )
                        else "GRAM_FALLBACK"
                    )
                    context_available = (
                        capability_snapshot.context.available
                    )
                    context_unavailable_code = (
                        capability_snapshot.context.unavailable_code
                    )
                    fuzzy_available, fuzzy_unavailable_code = (
                        capability_snapshot.fuzzy_available_for(
                            intended_path
                        )
                    )

                with _stage_guard("EXACT"):
                    exact_port = _safe_attr(view, "exact_records")
                    if not callable(exact_port):
                        raise _ResourcePipelineFailure(
                            stage="EXACT",
                            error_code=_QUERY_VIEW_INVALID_CODE,
                        )
                    exact_records = _snapshot_exact_records(
                        cast(Any, exact_port)(query.query_source)
                    )
                    classification = classify_exact_context(
                        resource_id=snapshot.resource_id,
                        resource_order=snapshot.order,
                        query=query,
                        records=exact_records,
                    )

                if fuzzy_available:
                    proof_session_port = lazy_retriever.proof_port()
                    if proof_session_port is not None and proof_query_v2_authority:
                        try:
                            with _stage_guard("PROOF"):
                                fuzzy, report = prove_and_score_fuzzy_candidates(
                                    resource_id=snapshot.resource_id,
                                    resource_order=snapshot.order,
                                    query=query,
                                    view=view,
                                    proof_session_port=proof_session_port,
                                )
                                report_snapshot = _snapshot_candidate_report(report)
                                if (
                                    report_snapshot.metadata.resource_id
                                    != snapshot.resource_id
                                ):
                                    raise ValueError(
                                        "proof report resource binding drift"
                                    )
                                if report_snapshot.metadata.result_limit != query.limit:
                                    raise ValueError(
                                        "proof report result-limit binding drift"
                                    )
                                if report_snapshot.metadata.index_kind != intended_path:
                                    raise _ResourcePipelineFailure(
                                        stage="PROOF",
                                        error_code=_RECALL_PATH_MISMATCH_CODE,
                                    )
                        except _ResourcePipelineFailure as proof_failure:
                            recall_metadata = _unavailable_recall_metadata(
                                resource_id=snapshot.resource_id,
                                index_kind=intended_path,
                                result_limit=query.limit,
                                fuzzy_unavailable_code=proof_failure.error_code,
                            )
                            scored_count = 0
                            fuzzy_results = ()
                            local_failure: _ResourcePipelineFailure | None = proof_failure
                        else:
                            recall_metadata = report_snapshot.metadata
                            scored_count = fuzzy.scored_count
                            fuzzy_results = fuzzy.accepted
                            local_failure = None
                    else:
                        with _stage_guard("RECALL"):
                            retriever_port = lazy_retriever.port()
                            report = retriever_port(
                                snapshot.resource_id,
                                view,
                                folded_query,
                                result_limit=query.limit,
                            )
                            report_snapshot = _snapshot_candidate_report(report)
                            if (
                                report_snapshot.metadata.resource_id
                                != snapshot.resource_id
                            ):
                                raise _ResourcePipelineFailure(
                                    stage="RECALL",
                                    error_code=_NORMALIZED_FAILURE_CODE,
                                )
                            if report_snapshot.metadata.result_limit != query.limit:
                                raise _ResourcePipelineFailure(
                                    stage="RECALL",
                                    error_code=_NORMALIZED_FAILURE_CODE,
                                )
                            if report_snapshot.metadata.index_kind != intended_path:
                                raise _ResourcePipelineFailure(
                                    stage="RECALL",
                                    error_code=_RECALL_PATH_MISMATCH_CODE,
                                )
                            candidate_ids = tuple(
                                candidate.record_id
                                for candidate in report_snapshot.candidates
                            )
                        with _stage_guard("RECORDS"):
                            records_port = _safe_attr(view, "records_by_id")
                            if not callable(records_port):
                                raise _ResourcePipelineFailure(
                                    stage="RECORDS",
                                    error_code=_QUERY_VIEW_INVALID_CODE,
                                )
                            batch_records = cast(Any, records_port)(candidate_ids)
                        with _stage_guard("SCORE"):
                            fuzzy = score_fuzzy_candidates(
                                resource_id=snapshot.resource_id,
                                resource_order=snapshot.order,
                                query=query,
                                report=report_snapshot,
                                records=batch_records,
                                scorer=cast(Any, lazy_scorer),
                            )
                        recall_metadata = report_snapshot.metadata
                        scored_count = fuzzy.scored_count
                        fuzzy_results = fuzzy.accepted
                        local_failure = None
                else:
                    with _stage_guard("RECALL"):
                        recall_metadata = _unavailable_recall_metadata(
                            resource_id=snapshot.resource_id,
                            index_kind=intended_path,
                            result_limit=query.limit,
                            fuzzy_unavailable_code=cast(
                                str,
                                fuzzy_unavailable_code,
                            ),
                        )
                    scored_count = 0
                    fuzzy_results = ()
                    local_failure = None

                with _stage_guard("QUERY"):
                    if context_available:
                        local_results = classification.returned_results
                    else:
                        local_results = (
                            (classification.winner,)
                            if classification.winner is not None
                            else ()
                        )
                    local_results = local_results + fuzzy_results
                    metadata = ResourceQueryMetadata(
                        resource_id=snapshot.resource_id,
                        context_available=context_available,
                        context_unavailable_code=context_unavailable_code,
                        recall=recall_metadata,
                        scored_count=scored_count,
                        returned_count=0,
                    )
                return local_results, metadata, local_failure
        except _ResourcePipelineFailure:
            raise
        except Exception as error:
            raise _normalize_stage_error(error, "LEASE") from error
