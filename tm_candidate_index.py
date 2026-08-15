"""FTS5 trigram recall primitives for already-folded canonical TM text."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
import re

from tm_sqlite_store import (
    SQLiteCandidateRecord,
    SQLiteCandidateProofBlock,
    SQLiteCandidateProofDensePhase1,
    SQLiteCandidateProofDensePhase2,
    SQLiteCandidateProofRecord,
    SQLiteCandidateProofSnapshot,
    SQLiteCandidateRecallSnapshot,
    SQLiteCandidateWritePlan,
    SQLiteTMStore,
    SQLiteTMQueryView,
    SQLiteStoreSchemaError,
    CANDIDATE_PROOF_BLOCK_SIZE,
    CANDIDATE_PROOF_BLOCK_VERSION_V1,
    _validate_candidate_proof_dense_phase1_result,
    _validate_candidate_proof_dense_phase2_result,
    build_candidate_write_plan as _store_build_candidate_write_plan,
    unique_character_ngrams as _store_unique_character_ngrams,
)
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION,
    CANDIDATE_PROOF_TRAVERSAL_VERSION,
    CandidateEvidence,
    CandidateProofMetadata,
    CandidateProofRefinementMetadata,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    candidate_budget_v1,
    SCORER_BOUND_VERSION_V1,
    SimilarityEvidence,
)
from tm_similarity import scorer_upper_bound_v1


FTS5_UNAVAILABLE_CODE = "CANDIDATE.FTS5_UNAVAILABLE"
FTS5_QUERY_TOO_SHORT_CODE = "CANDIDATE.FTS_QUERY_TOO_SHORT"
GRAM_EMPTY_QUERY_CODE = "CANDIDATE.GRAM_QUERY_EMPTY"
GRAM_LONG_QUERY_FTS_SELECTED_CODE = "CANDIDATE.GRAM_LONG_QUERY_FTS_SELECTED"
GRAM_CANDIDATE_HARD_CAP = 8192
CANDIDATE_CONTRACT_FLOOR = candidate_budget_v1(1)
CANDIDATE_PROOF_BATCH_SIZE = 32
CANDIDATE_PROOF_BUDGET_EXHAUSTED = "CANDIDATE.PROOF_BUDGET_EXHAUSTED"
_DENSE_CROSSOVER_MIN_BLOCKS = 8
_ASCII_LCS_TRANSITION_STATE_LIMIT = 4_096


class CandidateProofBudgetExhausted(RuntimeError):
    """The frozen scorer budget ended before both proof closures."""

    code = CANDIDATE_PROOF_BUDGET_EXHAUSTED

    def __init__(self) -> None:
        super().__init__(self.code)


def _copy_candidate_recall_snapshot(
    value: object,
) -> SQLiteCandidateRecallSnapshot:
    """Reject forged nested store values before hashing or ordering them."""

    if type(value) is not SQLiteCandidateRecallSnapshot:
        raise TypeError("store returned an invalid candidate snapshot")
    if type(value.fts5_available) is not bool:
        raise TypeError("candidate snapshot capability is invalid")
    if type(value.stage_matches) is not tuple:
        raise TypeError("candidate snapshot stages are invalid")
    copied_stages: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    for stage_entry in value.stage_matches:
        if type(stage_entry) is not tuple or len(stage_entry) != 2:
            raise TypeError("candidate snapshot stage is invalid")
        stage_name, matches = stage_entry
        if type(stage_name) is not str or type(matches) is not tuple:
            raise TypeError("candidate snapshot stage values are invalid")
        copied_matches: list[tuple[int, int]] = []
        for match in matches:
            if type(match) is not tuple or len(match) != 2:
                raise TypeError("candidate snapshot match is invalid")
            record_id, matched_count = match
            if type(record_id) is not int or type(matched_count) is not int:
                raise TypeError("candidate snapshot match values are invalid")
            if record_id < 1 or matched_count < 0:
                raise ValueError("candidate snapshot match values are invalid")
            copied_matches.append((record_id, matched_count))
        copied_stages.append((stage_name, tuple(copied_matches)))
    if type(value.folded_sources) is not tuple:
        raise TypeError("candidate snapshot sources are invalid")
    copied_sources: list[tuple[int, str]] = []
    for source_entry in value.folded_sources:
        if type(source_entry) is not tuple or len(source_entry) != 2:
            raise TypeError("candidate snapshot source is invalid")
        record_id, folded_source = source_entry
        if type(record_id) is not int or type(folded_source) is not str:
            raise TypeError("candidate snapshot source values are invalid")
        if record_id < 1 or not folded_source:
            raise ValueError("candidate snapshot source values are invalid")
        copied_sources.append((record_id, folded_source))
    return SQLiteCandidateRecallSnapshot(
        fts5_available=value.fts5_available,
        stage_matches=tuple(copied_stages),
        folded_sources=tuple(copied_sources),
    )


def unique_character_ngrams(folded_text: str, gram_size: int) -> tuple[str, ...]:
    """Delegate to the store-owned canonical gram builder."""

    return _store_unique_character_ngrams(folded_text, gram_size)


def unique_character_trigrams(folded_query: str) -> tuple[str, ...]:
    """Return first-occurrence ordered unique code-point trigrams."""

    return unique_character_ngrams(folded_query, 3)


def build_candidate_write_plan(
    records: tuple[SQLiteCandidateRecord, ...],
    *,
    fts5_available: bool,
) -> SQLiteCandidateWritePlan:
    """Delegate to the store-owned mandatory candidate-plan builder."""

    return _store_build_candidate_write_plan(
        records,
        fts5_available=fts5_available,
    )


def build_fts5_match_expression(trigrams: tuple[str, ...]) -> str:
    """Build a parameter-bound OR union of correctly escaped FTS phrases."""

    if type(trigrams) is not tuple:
        raise TypeError("trigrams must be a built-in tuple")
    seen: set[str] = set()
    phrases: list[str] = []
    for trigram in trigrams:
        if type(trigram) is not str:
            raise TypeError("trigrams must contain built-in strings")
        if len(trigram) != 3:
            raise ValueError("each trigram must contain three characters")
        if trigram in seen:
            raise ValueError("trigrams must be unique")
        seen.add(trigram)
        phrases.append(f'"{trigram.replace(chr(34), chr(34) * 2)}"')
    return " OR ".join(phrases)


@dataclass(frozen=True)
class FTS5CandidateResult:
    """Task 4.1 fast-path seam; final phased metadata belongs to Task 4.3."""

    record_ids: tuple[int, ...]
    query_trigrams: tuple[str, ...]
    available: bool
    unavailable_code: str | None

    def __post_init__(self) -> None:
        if type(self.record_ids) is not tuple or any(
            type(record_id) is not int or record_id < 1
            for record_id in self.record_ids
        ):
            raise ValueError("record_ids must contain positive integers")
        if tuple(sorted(set(self.record_ids))) != self.record_ids:
            raise ValueError("record_ids must be unique and sorted")
        if type(self.query_trigrams) is not tuple or any(
            type(trigram) is not str or len(trigram) != 3
            for trigram in self.query_trigrams
        ):
            raise ValueError("query_trigrams must contain trigrams")
        if len(set(self.query_trigrams)) != len(self.query_trigrams):
            raise ValueError("query_trigrams must be unique")
        if type(self.available) is not bool:
            raise TypeError("available must be a built-in bool")
        if self.available:
            if self.unavailable_code is not None:
                raise ValueError("available result cannot have unavailable_code")
        elif (
            type(self.unavailable_code) is not str
            or not self.unavailable_code.strip()
            or self.record_ids
        ):
            raise ValueError(
                "unavailable result needs a code and cannot contain candidates"
            )


@dataclass(frozen=True)
class GramPostingEvidence:
    """Minimal posting overlap evidence; scoring metadata belongs to Task 4.3."""

    record_id: int
    matched_postings: int
    query_postings: int
    overlap_ratio: float

    def __post_init__(self) -> None:
        if type(self.record_id) is not int or self.record_id < 1:
            raise ValueError("record_id must be a positive integer")
        if type(self.matched_postings) is not int or self.matched_postings < 1:
            raise ValueError("matched_postings must be a positive integer")
        if type(self.query_postings) is not int or self.query_postings < 1:
            raise ValueError("query_postings must be a positive integer")
        if self.matched_postings > self.query_postings:
            raise ValueError("matched_postings cannot exceed query_postings")
        if type(self.overlap_ratio) is not float or not 0.0 <= self.overlap_ratio <= 1.0:
            raise ValueError("overlap_ratio must be a float in [0.0, 1.0]")
        if self.overlap_ratio != self.matched_postings / self.query_postings:
            raise ValueError("overlap_ratio must match posting counts")


@dataclass(frozen=True)
class GramCandidateResult:
    """Task 4.2 gram seam without Task 4.3 phased recall accounting."""

    record_ids: tuple[int, ...]
    evidence: tuple[GramPostingEvidence, ...]
    query_postings: tuple[tuple[int, str], ...]
    path: str
    available: bool
    unavailable_code: str | None

    def __post_init__(self) -> None:
        if type(self.record_ids) is not tuple or any(
            type(record_id) is not int or record_id < 1 for record_id in self.record_ids
        ):
            raise ValueError("record_ids must contain positive integers")
        if len(set(self.record_ids)) != len(self.record_ids):
            raise ValueError("record_ids must be unique")
        if type(self.evidence) is not tuple or any(
            type(item) is not GramPostingEvidence for item in self.evidence
        ):
            raise TypeError("evidence must contain GramPostingEvidence values")
        if tuple(item.record_id for item in self.evidence) != self.record_ids:
            raise ValueError("evidence must align with record_ids")
        if type(self.query_postings) is not tuple:
            raise TypeError("query_postings must be a built-in tuple")
        posting_keys: list[tuple[int, str]] = []
        for posting in self.query_postings:
            if type(posting) is not tuple or len(posting) != 2:
                raise TypeError("query_postings must contain built-in pairs")
            gram_size, gram = posting
            if (
                type(gram_size) is not int
                or gram_size not in {1, 2, 3}
                or type(gram) is not str
                or len(gram) != gram_size
            ):
                raise ValueError("query posting is invalid")
            posting_keys.append((gram_size, gram))
        if len(set(posting_keys)) != len(posting_keys):
            raise ValueError("query_postings must be unique")
        if type(self.path) is not str or not self.path.strip():
            raise ValueError("path must be a non-empty built-in string")
        if type(self.available) is not bool:
            raise TypeError("available must be a built-in bool")
        if self.available:
            if self.unavailable_code is not None:
                raise ValueError("available result cannot have unavailable_code")
        elif (
            type(self.unavailable_code) is not str
            or not self.unavailable_code.strip()
            or self.record_ids
            or self.evidence
        ):
            raise ValueError("unavailable result needs a code and no candidates")


class GramPostingIndex:
    """Short-query postings and no-FTS deterministic fallback recall."""

    def __init__(
        self,
        *,
        fts5_available: bool,
        hard_cap: int = GRAM_CANDIDATE_HARD_CAP,
    ) -> None:
        if type(fts5_available) is not bool:
            raise TypeError("fts5_available must be a built-in bool")
        if type(hard_cap) is not int:
            raise TypeError("hard_cap must be a built-in integer")
        if not 1 <= hard_cap <= GRAM_CANDIDATE_HARD_CAP:
            raise ValueError("hard_cap is outside the safe range")
        self._fts5_available = fts5_available
        self._hard_cap = hard_cap

    def write_plan(
        self,
        records: tuple[SQLiteCandidateRecord, ...],
    ) -> SQLiteCandidateWritePlan:
        return build_candidate_write_plan(
            records,
            fts5_available=self._fts5_available,
        )

    def candidates(
        self,
        store: SQLiteTMStore,
        folded_query: str,
        *,
        limit: int,
    ) -> GramCandidateResult:
        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be SQLiteTMStore")
        if type(folded_query) is not str:
            raise TypeError("folded_query must be a built-in string")
        if type(limit) is not int:
            raise TypeError("limit must be a built-in integer")
        if limit < 1:
            raise ValueError("limit must be positive")

        if not folded_query:
            return GramCandidateResult(
                record_ids=(),
                evidence=(),
                query_postings=(),
                path="GRAM_NONE",
                available=False,
                unavailable_code=GRAM_EMPTY_QUERY_CODE,
            )
        if len(folded_query) == 1:
            gram_sizes = (1,)
            path = "GRAM_1_SHORT"
        elif len(folded_query) == 2:
            gram_sizes = (2,)
            path = "GRAM_2_SHORT"
        elif self._fts5_available:
            return GramCandidateResult(
                record_ids=(),
                evidence=(),
                query_postings=(),
                path="FTS_TRIGRAM",
                available=False,
                unavailable_code=GRAM_LONG_QUERY_FTS_SELECTED_CODE,
            )
        else:
            gram_sizes = (3, 2, 1)
            path = "GRAM_123_FALLBACK"

        query_postings = tuple(
            (gram_size, gram)
            for gram_size in gram_sizes
            for gram in unique_character_ngrams(folded_query, gram_size)
        )
        cap = min(limit, self._hard_cap)
        overlaps = store.gram_candidate_overlaps(
            query_postings,
            candidate_cap=cap,
        )
        query_count = len(query_postings)
        evidence = tuple(
            GramPostingEvidence(
                record_id=record_id,
                matched_postings=matched_postings,
                query_postings=query_count,
                overlap_ratio=matched_postings / query_count,
            )
            for record_id, matched_postings in sorted(
                overlaps,
                key=lambda item: (-item[1], item[0]),
            )[:cap]
        )
        return GramCandidateResult(
            record_ids=tuple(item.record_id for item in evidence),
            evidence=evidence,
            query_postings=query_postings,
            path=path,
            available=True,
            unavailable_code=None,
        )


class FTS5TrigramIndex:
    """Contentful FTS5 write-plan and recall implementation for fold-v1 text."""

    def __init__(self, *, available: bool) -> None:
        if type(available) is not bool:
            raise TypeError("available must be a built-in bool")
        self._available = available

    def write_plan(
        self,
        records: tuple[SQLiteCandidateRecord, ...],
    ) -> SQLiteCandidateWritePlan:
        """Select rows for the store-owned transaction without re-folding."""

        return build_candidate_write_plan(
            records,
            fts5_available=self._available,
        )

    def candidates(
        self,
        store: SQLiteTMStore,
        folded_query: str,
    ) -> FTS5CandidateResult:
        """Return deterministic candidate identities; never fold or fallback."""

        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be SQLiteTMStore")
        trigrams = unique_character_trigrams(folded_query)
        if not self._available:
            return FTS5CandidateResult(
                record_ids=(),
                query_trigrams=trigrams,
                available=False,
                unavailable_code=FTS5_UNAVAILABLE_CODE,
            )
        if not trigrams:
            return FTS5CandidateResult(
                record_ids=(),
                query_trigrams=(),
                available=False,
                unavailable_code=FTS5_QUERY_TOO_SHORT_CODE,
            )
        record_ids = (
            store.fts5_candidate_ids(build_fts5_match_expression(trigrams))
            if len(trigrams) <= 256
            else store.fts5_candidate_ids_for_trigrams(trigrams)
        )
        if record_ids is None:
            return FTS5CandidateResult(
                record_ids=(),
                query_trigrams=trigrams,
                available=False,
                unavailable_code=FTS5_UNAVAILABLE_CODE,
            )
        return FTS5CandidateResult(
            record_ids=tuple(sorted(set(record_ids))),
            query_trigrams=trigrams,
            available=True,
            unavailable_code=None,
        )


@dataclass(frozen=True)
class _PreparedCandidateQuery:
    result_limit: int
    budget: int
    query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...]
    fts_query_trigrams: tuple[str, ...] | None
    fts_query_degenerate: bool


def _validate_candidate_scalars(
    folded_query: object,
    result_limit: object,
) -> None:
    if type(folded_query) is not str:
        raise TypeError("folded_query must be a built-in string")
    if type(result_limit) is not int:
        raise TypeError("result_limit must be a built-in integer")
    if result_limit < 1:
        raise ValueError("result_limit must be positive")


def _prepare_candidate_query(
    folded_query: str,
    result_limit: int,
) -> _PreparedCandidateQuery:
    budget = candidate_budget_v1(result_limit)
    query_grams_by_size: tuple[tuple[int, tuple[str, ...]], ...]
    fts_query_trigrams: tuple[str, ...] | None = None
    fts_query_degenerate = False
    if not folded_query:
        query_grams_by_size = ()
    elif len(folded_query) == 1:
        query_grams_by_size = (
            (1, unique_character_ngrams(folded_query, 1)),
        )
    elif len(folded_query) == 2:
        query_grams_by_size = (
            (2, unique_character_ngrams(folded_query, 2)),
        )
    else:
        query_grams_by_size = tuple(
            (
                gram_size,
                unique_character_ngrams(folded_query, gram_size),
            )
            for gram_size in (3, 2, 1)
        )
        query_trigrams = query_grams_by_size[0][1]
        fts_query_trigrams = query_trigrams
        fts_query_degenerate = len(query_trigrams) <= 1
    return _PreparedCandidateQuery(
        result_limit=result_limit,
        budget=budget,
        query_grams_by_size=query_grams_by_size,
        fts_query_trigrams=fts_query_trigrams,
        fts_query_degenerate=fts_query_degenerate,
    )


def _candidate_recall_snapshot_or_fail(
    source: SQLiteTMStore | SQLiteTMQueryView,
    prepared: _PreparedCandidateQuery,
) -> SQLiteCandidateRecallSnapshot:
    try:
        snapshot = _copy_candidate_recall_snapshot(
            source.candidate_recall_snapshot(
                fts_query_trigrams=prepared.fts_query_trigrams,
                query_grams_by_size=prepared.query_grams_by_size,
                candidate_floor=CANDIDATE_CONTRACT_FLOOR,
                fts_query_degenerate=prepared.fts_query_degenerate,
            )
        )
    except (TypeError, ValueError) as error:
        raise SQLiteStoreSchemaError(
            "STORE.CANDIDATE_EVIDENCE_INVALID"
        ) from error
    return snapshot


def _build_candidate_report(
    resource_id: str,
    folded_query: str,
    prepared: _PreparedCandidateQuery,
    snapshot: SQLiteCandidateRecallSnapshot,
) -> CandidateRetrievalReport:
    index_kind = (
        "FTS5_TRIGRAM"
        if snapshot.fts5_available and len(folded_query) >= 3
        else "GRAM_FALLBACK"
    )
    if not folded_query:
        return CandidateRetrievalReport(
            candidates=(),
            metadata=CandidateRecallMetadata(
                resource_id=resource_id,
                index_kind=index_kind,
                fuzzy_available=False,
                fuzzy_unavailable_code=GRAM_EMPTY_QUERY_CODE,
                stages=(),
                union_unique_count=0,
                deduplicated_count=0,
                result_limit=prepared.result_limit,
                candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                candidate_budget=prepared.budget,
                truncated=False,
            ),
        )

    grams_by_stage = {
        CandidateStage[f"GRAM_{gram_size}"]: grams
        for gram_size, grams in prepared.query_grams_by_size
    }
    if len(folded_query) >= 3:
        grams_by_stage[CandidateStage.FTS_TRIGRAM] = (
            unique_character_trigrams(folded_query)
        )
    sources_by_id = dict(snapshot.folded_sources)
    if len(sources_by_id) != len(snapshot.folded_sources):
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
    try:
        raw_stages = tuple(
            (CandidateStage(stage_name), matches)
            for stage_name, matches in snapshot.stage_matches
        )
    except ValueError as error:
        raise SQLiteStoreSchemaError(
            "STORE.CANDIDATE_EVIDENCE_INVALID"
        ) from error
    actual_stage_sequence = tuple(stage for stage, _matches in raw_stages)
    if not folded_query:
        expected_stage_sequence: tuple[CandidateStage, ...] = ()
    elif len(folded_query) == 1:
        expected_stage_sequence = (CandidateStage.GRAM_1,)
    elif len(folded_query) == 2:
        expected_stage_sequence = (CandidateStage.GRAM_2,)
    elif not snapshot.fts5_available:
        expected_stage_sequence = (
            CandidateStage.GRAM_3,
            CandidateStage.GRAM_2,
            CandidateStage.GRAM_1,
        )
    else:
        expected: list[CandidateStage] = [CandidateStage.FTS_TRIGRAM]
        fts_ids = {
            record_id
            for record_id, _count in (
                raw_stages[0][1]
                if raw_stages and raw_stages[0][0] is CandidateStage.FTS_TRIGRAM
                else ()
            )
        }
        if not fts_ids or prepared.fts_query_degenerate or len(fts_ids) < CANDIDATE_CONTRACT_FLOOR:
            expected.append(CandidateStage.GRAM_2)
            gram_2_ids = {
                record_id
                for stage, matches in raw_stages
                if stage is CandidateStage.GRAM_2
                for record_id, _count in matches
            }
            if len(fts_ids | gram_2_ids) < CANDIDATE_CONTRACT_FLOOR:
                expected.append(CandidateStage.GRAM_1)
        expected_stage_sequence = tuple(expected)
    if actual_stage_sequence != expected_stage_sequence:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
    cumulative_ids: set[int] = set()
    recall_stages_by_id: dict[int, list[CandidateStage]] = {}
    matched_by_id: dict[int, int] = {}
    stage_metadata: list[CandidateStageMetadata] = []
    executed_query_grams = 0

    for stage, raw_matches in raw_stages:
        if stage not in grams_by_stage:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
        query_grams = grams_by_stage[stage]
        executed_query_grams += len(query_grams)
        input_count = len(cumulative_ids)
        stage_ids: set[int] = set()
        for record_id, store_matched_count in raw_matches:
            if record_id in stage_ids:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
            stage_ids.add(record_id)
            source = sources_by_id.get(record_id)
            if source is None:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
            gram_size = 3 if stage is CandidateStage.FTS_TRIGRAM else int(stage.value[-1])
            source_grams = set(unique_character_ngrams(source, gram_size))
            matched_count = sum(gram in source_grams for gram in query_grams)
            if (
                not 1 <= matched_count <= len(query_grams)
                or store_matched_count != matched_count
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")
            recall_stages_by_id.setdefault(record_id, []).append(stage)
            matched_by_id[record_id] = matched_by_id.get(record_id, 0) + matched_count
        cumulative_ids.update(stage_ids)
        stage_metadata.append(
            CandidateStageMetadata(
                stage=stage,
                input_count=input_count,
                added_unique_count=len(cumulative_ids) - input_count,
                output_unique_count=len(cumulative_ids),
                dropped_count=0,
            )
        )

    if set(sources_by_id) != cumulative_ids:
        raise SQLiteStoreSchemaError("STORE.CANDIDATE_EVIDENCE_INVALID")

    union_count = len(cumulative_ids)
    stage_metadata.extend(
        (
            CandidateStageMetadata(
                stage=CandidateStage.UNION,
                input_count=union_count,
                added_unique_count=0,
                output_unique_count=union_count,
                dropped_count=0,
            ),
            CandidateStageMetadata(
                stage=CandidateStage.DEDUPLICATE,
                input_count=union_count,
                added_unique_count=0,
                output_unique_count=union_count,
                dropped_count=0,
            ),
        )
    )
    ranked_ids = tuple(
        sorted(
            cumulative_ids,
            key=lambda record_id: (
                -(matched_by_id[record_id] / executed_query_grams),
                abs(len(sources_by_id[record_id]) - len(folded_query)),
                record_id,
            ),
        )
    )
    truncated = union_count > prepared.budget
    if truncated:
        stage_metadata.append(
            CandidateStageMetadata(
                stage=CandidateStage.TRUNCATE,
                input_count=union_count,
                added_unique_count=0,
                output_unique_count=prepared.budget,
                dropped_count=union_count - prepared.budget,
            )
        )
        returned_ids = ranked_ids[:prepared.budget]
    else:
        returned_ids = ranked_ids
    rank_by_id = {
        record_id: rank
        for rank, record_id in enumerate(ranked_ids, start=1)
    }
    candidates = tuple(
        CandidateEvidence(
            record_id=record_id,
            recall_stages=tuple(recall_stages_by_id[record_id]),
            matched_grams=matched_by_id[record_id],
            query_grams=executed_query_grams,
            overlap_ratio=(
                matched_by_id[record_id] / executed_query_grams
            ),
            pretruncate_rank=rank_by_id[record_id],
        )
        for record_id in returned_ids
    )
    return CandidateRetrievalReport(
        candidates=candidates,
        metadata=CandidateRecallMetadata(
            resource_id=resource_id,
            index_kind=index_kind,
            fuzzy_available=True,
            fuzzy_unavailable_code=None,
            stages=tuple(stage_metadata),
            union_unique_count=union_count,
            deduplicated_count=union_count,
            result_limit=prepared.result_limit,
            candidate_budget_version=CANDIDATE_BUDGET_VERSION,
            candidate_budget=prepared.budget,
            truncated=truncated,
        ),
    )


def _copy_proof_snapshot(value: object) -> SQLiteCandidateProofSnapshot:
    """Close every store-owned proof value before heap/order operations."""

    if type(value) is not SQLiteCandidateProofSnapshot:
        raise TypeError("store returned an invalid proof snapshot")
    if type(value.index_kind) is not str or value.index_kind not in {
        "FTS5_TRIGRAM",
        "GRAM_FALLBACK",
    }:
        raise ValueError("proof snapshot index kind is invalid")
    if type(value.seed_stages) is not tuple:
        raise TypeError("proof seed stages must be a tuple")
    stages: list[tuple[str, tuple[int, ...]]] = []
    seen_names: set[str] = set()
    for entry in value.seed_stages:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("proof seed stage is invalid")
        name, ids = entry
        if type(name) is not str or name not in {
            "FTS_TRIGRAM", "GRAM_3", "GRAM_2", "GRAM_1"
        }:
            raise ValueError("proof seed stage name is invalid")
        if name in seen_names or type(ids) is not tuple:
            raise TypeError("proof seed stage values are invalid")
        copied_ids: list[int] = []
        for record_id in ids:
            if type(record_id) is not int or record_id < 1:
                raise ValueError("proof seed record id is invalid")
            copied_ids.append(record_id)
        if len(copied_ids) != len(set(copied_ids)):
            raise ValueError("proof seed ids must be unique per stage")
        seen_names.add(name)
        stages.append((name, tuple(copied_ids)))

    if type(value.blocks) is not tuple:
        raise TypeError("proof blocks must be a tuple")
    blocks: list[SQLiteCandidateProofBlock] = []
    block_ids: set[int] = set()
    for block in value.blocks:
        if type(block) is not SQLiteCandidateProofBlock:
            raise TypeError("proof blocks contain an invalid value")
        integers = (
            block.block_id,
            block.first_record_id,
            block.last_record_id,
            block.record_count,
            block.min_source_fold_length,
            block.max_source_fold_length,
            block.character_intersection_upper,
            block.bigram_intersection_upper,
        )
        if any(type(item) is not int or item < 0 for item in integers):
            raise ValueError("proof block integer fact is invalid")
        if (
            block.block_id in block_ids
            or block.record_count < 1
            or block.first_record_id < 1
            or block.last_record_id < block.first_record_id
            or block.min_source_fold_length < 1
            or block.max_source_fold_length < block.min_source_fold_length
        ):
            raise ValueError("proof block fact is invalid")
        block_ids.add(block.block_id)
        blocks.append(SQLiteCandidateProofBlock(**block.__dict__))
    if type(value.total_record_count) is not int or value.total_record_count < 0:
        raise ValueError("proof total record count is invalid")
    if type(value.head_revision) is not int or value.head_revision < 0:
        raise ValueError("proof head revision is invalid")
    if (
        type(value.query_maxima_digest) is not str
        or len(value.query_maxima_digest) != 64
        or any(character not in "0123456789abcdef" for character in value.query_maxima_digest)
    ):
        raise ValueError("proof query maxima digest is invalid")
    expected_block_ids = set(range(
        (value.total_record_count + CANDIDATE_PROOF_BLOCK_SIZE - 1)
        // CANDIDATE_PROOF_BLOCK_SIZE
    ))
    if block_ids != expected_block_ids:
        raise ValueError("proof block identities do not close the universe")
    for block in blocks:
        expected_first = block.block_id * CANDIDATE_PROOF_BLOCK_SIZE + 1
        expected_count = min(
            CANDIDATE_PROOF_BLOCK_SIZE,
            value.total_record_count - expected_first + 1,
        )
        if (
            block.first_record_id != expected_first
            or block.last_record_id
            != expected_first + CANDIDATE_PROOF_BLOCK_SIZE - 1
            or block.record_count != expected_count
        ):
            raise ValueError("proof block slot facts do not close")
    if any(
        record_id > value.total_record_count
        for _name, ids in stages
        for record_id in ids
    ):
        raise ValueError("proof seed identity is outside the proof universe")
    return SQLiteCandidateProofSnapshot(
        index_kind=value.index_kind,
        seed_stages=tuple(stages),
        blocks=tuple(blocks),
        total_record_count=value.total_record_count,
        head_revision=value.head_revision,
        query_maxima_digest=value.query_maxima_digest,
    )


def _block_upper_bound(
    block: SQLiteCandidateProofBlock,
    *,
    query_length: int,
) -> float:
    query_bigram_count = max(query_length - 1, 0)
    best = 0.0
    length_span = block.max_source_fold_length - block.min_source_fold_length
    if length_span > 4096:
        return 1.0
    for record_length in range(
        block.min_source_fold_length,
        block.max_source_fold_length + 1,
    ):
        bound = scorer_upper_bound_v1(
            query_fold_length=query_length,
            record_fold_length=record_length,
            character_multiset_intersection=min(
                block.character_intersection_upper,
                query_length,
                record_length,
            ),
            bigram_multiset_intersection=min(
                block.bigram_intersection_upper,
                query_bigram_count,
                max(record_length - 1, 0),
            ),
            query_bigram_count=query_bigram_count,
            record_bigram_count=max(record_length - 1, 0),
        ).final_similarity_upper_bound
        best = max(best, bound)
    return best


def _record_upper_bound(
    record: SQLiteCandidateProofRecord,
    *,
    query_length: int,
) -> float:
    return scorer_upper_bound_v1(
        query_fold_length=query_length,
        record_fold_length=record.source_fold_length,
        character_multiset_intersection=record.character_multiset_intersection,
        bigram_multiset_intersection=record.bigram_multiset_intersection,
        query_bigram_count=max(query_length - 1, 0),
        record_bigram_count=max(record.source_fold_length - 1, 0),
    ).final_similarity_upper_bound


def _dense_phase1_upper_bound(
    *,
    query_length: int,
    record_length: int,
    bigram_intersection: int,
) -> float:
    """Return U1 with exact bigrams and optimistic character intersection."""

    return scorer_upper_bound_v1(
        query_fold_length=query_length,
        record_fold_length=record_length,
        character_multiset_intersection=min(query_length, record_length),
        bigram_multiset_intersection=bigram_intersection,
        query_bigram_count=max(query_length - 1, 0),
        record_bigram_count=max(record_length - 1, 0),
    ).final_similarity_upper_bound


def _dense_u2_upper_bound(
    *,
    query_length: int,
    record_length: int,
    character_intersection: int,
    bigram_intersection: int,
) -> float:
    """Return the algebraic U2 comparator used by bound verification."""

    return scorer_upper_bound_v1(
        query_fold_length=query_length,
        record_fold_length=record_length,
        character_multiset_intersection=character_intersection,
        bigram_multiset_intersection=bigram_intersection,
        query_bigram_count=max(query_length - 1, 0),
        record_bigram_count=max(record_length - 1, 0),
    ).final_similarity_upper_bound


def _exact_lcs_query_projection(
    query: str,
) -> tuple[dict[str, int], re.Pattern[str]]:
    """Precompute an exact fixed-query bit projection for repeated LCS."""

    if type(query) is not str:
        raise TypeError("LCS query must be a built-in string")
    positions: dict[str, int] = {}
    for offset, code_point in enumerate(query):
        positions[code_point] = positions.get(code_point, 0) | (1 << offset)
    irrelevant = re.compile(f"[^{re.escape(''.join(positions))}]+")
    return positions, irrelevant


def _exact_lcs_ascii_query_projection(
    query: str,
) -> tuple[tuple[int, ...], bytes, bytes]:
    """Precompute the same exact LCS projection for an ASCII query."""

    masks_by_code_point = [0] * 256
    for offset, code_point in enumerate(query.encode("ascii")):
        masks_by_code_point[code_point] |= 1 << offset
    relevant_code_points = tuple(
        code_point
        for code_point, mask in enumerate(masks_by_code_point)
        if mask
    )
    translation = bytearray(256)
    for symbol, code_point in enumerate(relevant_code_points):
        translation[code_point] = symbol
    return (
        tuple(masks_by_code_point[code_point] for code_point in relevant_code_points),
        bytes(translation),
        bytes(
            code_point
            for code_point, mask in enumerate(masks_by_code_point)
            if not mask
        ),
    )


class _ExactLCSQueryProjection:
    """Compute one exact Unicode code-point LCS fact per identity."""

    def __init__(self, query: str) -> None:
        self._positions, self._irrelevant_pattern = _exact_lcs_query_projection(
            query
        )
        self._ascii_projection = (
            _exact_lcs_ascii_query_projection(query) if query.isascii() else None
        )
        self._ascii_frontiers = [0]
        self._ascii_frontier_bit_counts = [0]
        self._ascii_state_by_frontier = {0: 0}
        self._ascii_cache_saturated = False
        self._ascii_transitions = (
            []
            if self._ascii_projection is None
            else [[-1] * len(self._ascii_projection[0])]
        )

    def facts(
        self,
        candidate: str,
        source_length: int,
    ) -> int:
        """Return the exact LCS length for one ordered projection identity."""

        if (
            type(candidate) is not str
            or not candidate
            or type(source_length) is not int
            or len(candidate) != source_length
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        if self._ascii_projection is not None:
            return self._ascii_facts(candidate)

        frontier = 0
        relevant_candidate = self._irrelevant_pattern.sub("", candidate)
        for code_point in relevant_candidate:
            matches = self._positions[code_point]
            union = frontier | matches
            frontier = union & ~(union - ((frontier << 1) | 1))
        return frontier.bit_count()

    def _ascii_facts(self, candidate: str) -> int:
        projection = self._ascii_projection
        if projection is None:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        masks, translation, irrelevant = projection
        symbols = candidate.encode("ascii", "ignore").translate(
            translation,
            irrelevant,
        )
        if self._ascii_cache_saturated:
            return _exact_ascii_lcs_frontier(symbols, masks).bit_count()
        state = 0
        # Transition memoization only accelerates this exact automaton.  Every
        # identity still invokes ``facts`` and traverses its own folded source;
        # this state cannot authorize fold equivalence or scorer reuse.
        for offset, symbol in enumerate(symbols):
            next_state = self._ascii_transitions[state][symbol]
            if next_state < 0:
                frontier = self._ascii_frontiers[state]
                union = frontier | masks[symbol]
                updated = union & ~(union - ((frontier << 1) | 1))
                known_state = self._ascii_state_by_frontier.get(updated)
                if known_state is None:
                    if (
                        len(self._ascii_frontiers)
                        >= _ASCII_LCS_TRANSITION_STATE_LIMIT
                    ):
                        self._ascii_cache_saturated = True
                        return _exact_ascii_lcs_frontier(
                            symbols[offset + 1 :],
                            masks,
                            frontier=updated,
                        ).bit_count()
                    known_state = len(self._ascii_frontiers)
                    self._ascii_state_by_frontier[updated] = known_state
                    self._ascii_frontiers.append(updated)
                    self._ascii_frontier_bit_counts.append(updated.bit_count())
                    self._ascii_transitions.append([-1] * len(masks))
                next_state = known_state
                self._ascii_transitions[state][symbol] = next_state
            state = next_state
        return self._ascii_frontier_bit_counts[state]


def _exact_ascii_lcs_frontier(
    symbols: bytes,
    masks: tuple[int, ...],
    *,
    frontier: int = 0,
) -> int:
    """Advance exact bit-LCS without retaining identity-dependent states."""

    for symbol in symbols:
        matches = masks[symbol]
        union = frontier | matches
        frontier = union & ~(union - ((frontier << 1) | 1))
    return frontier


def _exact_lcs_length(left: str, right: str) -> int:
    """Return exact Unicode code-point LCS length without scoring or edits."""

    if type(left) is not str or type(right) is not str:
        raise TypeError("LCS inputs must be built-in strings")
    if not left or not right:
        return 0
    lcs_length = _ExactLCSQueryProjection(left).facts(
        right,
        len(right),
    )
    return lcs_length


def _dense_phase2_upper_bound(
    *,
    query_length: int,
    record_length: int,
    lcs_length: int,
    bigram_intersection: int,
) -> float:
    """Return U3 from exact LCS and exact bigram facts after phase two."""

    return scorer_upper_bound_v1(
        query_fold_length=query_length,
        record_fold_length=record_length,
        character_multiset_intersection=lcs_length,
        bigram_multiset_intersection=bigram_intersection,
        query_bigram_count=max(query_length - 1, 0),
        record_bigram_count=max(record_length - 1, 0),
    ).final_similarity_upper_bound


def _should_use_dense_traversal(
    block_upper_bounds: tuple[float, ...],
    *,
    minimum_similarity: float,
) -> bool:
    """Cross over when coarse maxima leave at least three quarters open."""

    if len(block_upper_bounds) < _DENSE_CROSSOVER_MIN_BLOCKS:
        return False
    threshold_competitive = sum(
        upper >= minimum_similarity for upper in block_upper_bounds
    )
    maximum = max(block_upper_bounds, default=0.0)
    coarse_competitive = (
        sum(upper >= maximum * 0.75 for upper in block_upper_bounds)
        if maximum > 0.0
        else 0
    )
    return (
        threshold_competitive * 4 >= len(block_upper_bounds) * 3
        or coarse_competitive * 4 >= len(block_upper_bounds) * 3
    )


class CandidateProofSession:
    """Private alternating proof port; Retrieval alone executes scorer-v1."""

    def __init__(
        self,
        *,
        resource_id: str,
        view: SQLiteTMQueryView,
        folded_query: str,
        minimum_similarity: float,
        result_limit: int,
    ) -> None:
        _validate_candidate_scalars(folded_query, result_limit)
        if not folded_query:
            raise ValueError("proof query must not be empty")
        if type(minimum_similarity) is not float or not math.isfinite(minimum_similarity):
            raise TypeError("minimum_similarity must be a finite float")
        if not 0.0 <= minimum_similarity <= 1.0:
            raise ValueError("minimum_similarity must be in [0, 1]")
        if type(view) is not SQLiteTMQueryView or view.resource_id != resource_id:
            raise TypeError("proof view must be the resource's exact query view")
        self._view = view
        self._resource_id = resource_id
        self._folded_query = folded_query
        self._minimum_similarity = minimum_similarity
        self._result_limit = result_limit
        self._budget = candidate_budget_v1(result_limit)
        try:
            snapshot = _copy_proof_snapshot(
                view.candidate_proof_snapshot(
                    folded_query=folded_query,
                    seed_limit=min(256, self._budget),
                )
            )
            expected_seed_names = (
                ("FTS_TRIGRAM",)
                if snapshot.index_kind == "FTS5_TRIGRAM"
                else (
                    ("GRAM_1",)
                    if len(folded_query) == 1
                    else (
                        ("GRAM_2",)
                        if len(folded_query) == 2
                        else ("GRAM_3", "GRAM_2", "GRAM_1")
                    )
                )
            )
            if tuple(name for name, _ids in snapshot.seed_stages) != expected_seed_names:
                raise ValueError("proof seed stages do not match the actual path")
        except (TypeError, ValueError) as error:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID") from error
        self._snapshot = snapshot
        self._blocks_by_id = {block.block_id: block for block in snapshot.blocks}
        self._record_by_id: dict[int, SQLiteCandidateProofRecord] = {}
        self._upper_by_id: dict[int, float] = {}
        self._block_heap: list[tuple[float, int, int]] = []
        block_bound_cache: dict[tuple[int, int, int, int], float] = {}
        block_upper_bounds: list[float] = []
        for block in snapshot.blocks:
            bound_key = (
                block.min_source_fold_length,
                block.max_source_fold_length,
                block.character_intersection_upper,
                block.bigram_intersection_upper,
            )
            upper = block_bound_cache.get(bound_key)
            if upper is None:
                upper = _block_upper_bound(
                    block,
                    query_length=len(folded_query),
                )
                block_bound_cache[bound_key] = upper
            block_upper_bounds.append(upper)
            possible_record_id = min(
                block.last_record_id,
                snapshot.total_record_count,
            )
            heapq.heappush(
                self._block_heap,
                (-upper, -possible_record_id, block.block_id),
            )
        self._record_heap: list[tuple[float, int, int]] = []
        self._dense_phase1: SQLiteCandidateProofDensePhase1 | None = None
        self._dense_phase1_uppers: tuple[float, ...] = ()
        self._dense_lcs_by_id: dict[int, int] = {}
        self._dense_p2_floor_frontier: tuple[float, int] | None = None
        self._dense_frontier_groups: list[tuple[float, list[int]]] | None = None
        self._dense_refined = False
        self._dense_a0_count = 0
        self._dense_p1_count = 0
        self._dense_r_count = 0
        self._dense_phase2_returned_count = 0
        self._dense_k0: tuple[float, int] | None = None
        self._dense_p1_frontier: tuple[float, int] | None = None
        self._opened_blocks: set[int] = set()
        self._outstanding: set[int] = set()
        self._scores: dict[int, float] = {}
        self._observation_order: list[int] = []
        self._scorer_invocation_count = 0
        self._traversal_mode = "SPARSE"
        if _should_use_dense_traversal(
            tuple(block_upper_bounds),
            minimum_similarity=minimum_similarity,
        ):
            self._load_dense_frontier()

    @property
    def index_kind(self) -> str:
        return self._snapshot.index_kind

    def _load_dense_frontier(self) -> None:
        phase1 = self._view.candidate_proof_dense_phase1(
            folded_query=self._folded_query,
            blocks=self._snapshot.blocks,
            head_revision=self._snapshot.head_revision,
            total_record_count=self._snapshot.total_record_count,
            query_maxima_digest=self._snapshot.query_maxima_digest,
        )
        _validate_candidate_proof_dense_phase1_result(
            phase1,
            view=self._view,
            folded_query=self._folded_query,
            blocks=self._snapshot.blocks,
            head_revision=self._snapshot.head_revision,
            total_record_count=self._snapshot.total_record_count,
            query_maxima_digest=self._snapshot.query_maxima_digest,
        )
        if (
            type(phase1) is not SQLiteCandidateProofDensePhase1
            or type(phase1.source_fold_lengths) is not tuple
            or type(phase1.bigram_multiset_intersections) is not tuple
            or len(phase1.source_fold_lengths) != self._snapshot.total_record_count
            or len(phase1.bigram_multiset_intersections)
            != self._snapshot.total_record_count
            or type(phase1.binding_digest) is not str
            or len(phase1.binding_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in phase1.binding_digest
            )
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        bound_cache: dict[tuple[int, int], float] = {}
        uppers: list[float] = []
        initial_limit = min(
            self._result_limit,
            self._snapshot.total_record_count,
        )
        initial_frontier: list[tuple[float, int]] = []
        query_length = len(self._folded_query)
        query_bigram_count = max(query_length - 1, 0)
        for record_id, (source_fold_length, bigram_intersection) in enumerate(
            zip(
                phase1.source_fold_lengths,
                phase1.bigram_multiset_intersections,
                strict=True,
            ),
            start=1,
        ):
            if (
                type(source_fold_length) is not int
                or type(bigram_intersection) is not int
                or source_fold_length < 1
                or bigram_intersection < 0
                or bigram_intersection
                > min(query_bigram_count, source_fold_length - 1)
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            bound_key = (
                source_fold_length,
                bigram_intersection,
            )
            upper = bound_cache.get(bound_key)
            if upper is None:
                upper = _dense_phase1_upper_bound(
                    query_length=query_length,
                    record_length=source_fold_length,
                    bigram_intersection=bigram_intersection,
                )
                bound_cache[bound_key] = upper
            uppers.append(upper)
            pair = (upper, record_id)
            if len(initial_frontier) < initial_limit:
                heapq.heappush(initial_frontier, pair)
            elif pair > initial_frontier[0]:
                heapq.heapreplace(initial_frontier, pair)
        for block in self._snapshot.blocks:
            start = block.first_record_id - 1
            stop = block.last_record_id
            block_lengths = phase1.source_fold_lengths[start:stop]
            block_bigrams = phase1.bigram_multiset_intersections[start:stop]
            if (
                len(block_lengths) != block.record_count
                or not block_lengths
                or min(block_lengths) != block.min_source_fold_length
                or max(block_lengths) != block.max_source_fold_length
                or max(block_bigrams, default=0) > block.bigram_intersection_upper
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        initial_ids_by_upper: dict[float, list[int]] = {}
        for upper, record_id in initial_frontier:
            initial_ids_by_upper.setdefault(upper, []).append(record_id)
        self._dense_frontier_groups = [
            (upper, sorted(initial_ids_by_upper[upper]))
            for upper in sorted(initial_ids_by_upper)
        ]
        self._dense_phase1 = phase1
        self._dense_phase1_uppers = tuple(uppers)
        self._block_heap.clear()
        self._traversal_mode = "DENSE"

    def _refine_dense_frontier(self) -> None:
        phase1 = self._dense_phase1
        groups = self._dense_frontier_groups
        if phase1 is None or groups is None or self._dense_refined:
            return
        required_prefix = min(
            self._result_limit,
            self._snapshot.total_record_count,
        )
        if len(self._scores) < required_prefix or self._outstanding:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        k0 = (
            heapq.nlargest(
                self._result_limit,
                (
                    (score, record_id)
                    for record_id, score in self._scores.items()
                ),
            )[-1]
            if self._snapshot.total_record_count >= self._result_limit
            else None
        )
        refinement_ids: list[int] = []
        refinement_source_lengths: list[int] = []
        p1_frontier: tuple[float, int] | None = None
        for record_id, upper in enumerate(
            self._dense_phase1_uppers,
            start=1,
        ):
            if record_id in self._scores:
                continue
            if upper >= self._minimum_similarity or (
                k0 is not None and (upper, record_id) >= k0
            ):
                refinement_ids.append(record_id)
                refinement_source_lengths.append(
                    phase1.source_fold_lengths[record_id - 1]
                )
            else:
                pair = (upper, record_id)
                if p1_frontier is None or pair > p1_frontier:
                    p1_frontier = pair
        request = tuple(refinement_ids)
        requested_source_lengths = tuple(refinement_source_lengths)
        response = self._view.candidate_proof_dense_phase2(
            folded_query=self._folded_query,
            blocks=self._snapshot.blocks,
            head_revision=self._snapshot.head_revision,
            total_record_count=self._snapshot.total_record_count,
            query_maxima_digest=self._snapshot.query_maxima_digest,
            binding_digest=phase1.binding_digest,
            record_ids=request,
            source_fold_lengths=requested_source_lengths,
        )
        _validate_candidate_proof_dense_phase2_result(
            response,
            binding_digest=phase1.binding_digest,
            record_ids=request,
            source_fold_lengths=requested_source_lengths,
        )
        if (
            type(response) is not SQLiteCandidateProofDensePhase2
            or type(response.record_ids) is not tuple
            or response.record_ids != request
            or type(response.source_folds_v1) is not tuple
            or len(response.source_folds_v1) != len(request)
            or type(response.source_fold_lengths) is not tuple
            or response.source_fold_lengths != requested_source_lengths
            or response.binding_digest != phase1.binding_digest
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        phase2_ids_by_upper: dict[float, list[int]] = {}
        bound_cache: dict[tuple[int, int, int], float] = {}
        query_length = len(self._folded_query)
        lcs_by_id: dict[int, int] = {}
        p2_floor_frontier: tuple[float, int] | None = None
        p2_floor_count = 0
        lcs_projection = _ExactLCSQueryProjection(self._folded_query)
        dense_u1_uppers = self._dense_phase1_uppers
        phase1_bigrams = phase1.bigram_multiset_intersections
        for (
            record_id,
            source_fold_v1,
            source_fold_length,
        ) in zip(
            request,
            response.source_folds_v1,
            requested_source_lengths,
            strict=True,
        ):
            offset = record_id - 1
            bigram_intersection = phase1_bigrams[offset]
            lcs_length = lcs_projection.facts(
                source_fold_v1,
                source_fold_length,
            )
            if not 0 <= lcs_length <= min(query_length, source_fold_length):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            bound_key = (
                source_fold_length,
                lcs_length,
                bigram_intersection,
            )
            upper = bound_cache.get(bound_key)
            if upper is None:
                upper = _dense_phase2_upper_bound(
                    query_length=query_length,
                    record_length=source_fold_length,
                    lcs_length=lcs_length,
                    bigram_intersection=bigram_intersection,
                )
                bound_cache[bound_key] = upper
            if upper > dense_u1_uppers[offset] + 1e-12:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            if upper >= self._minimum_similarity or (
                k0 is not None and (upper, record_id) >= k0
            ):
                lcs_by_id[record_id] = lcs_length
                phase2_ids_by_upper.setdefault(upper, []).append(record_id)
            else:
                p2_floor_count += 1
                pair = (upper, record_id)
                if p2_floor_frontier is None or pair > p2_floor_frontier:
                    p2_floor_frontier = pair
        self._dense_frontier_groups = [
            (upper, sorted(phase2_ids_by_upper[upper]))
            for upper in sorted(phase2_ids_by_upper)
        ]
        if p2_floor_count + sum(
            len(record_ids) for record_ids in phase2_ids_by_upper.values()
        ) != len(request):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        self._dense_lcs_by_id = lcs_by_id
        self._dense_p2_floor_frontier = p2_floor_frontier
        self._dense_a0_count = len(self._scores)
        self._dense_r_count = len(request)
        self._dense_p1_count = (
            self._snapshot.total_record_count
            - self._dense_a0_count
            - self._dense_r_count
        )
        if self._dense_p1_count < 0 or (
            self._dense_p1_count == 0 and p1_frontier is not None
        ) or (
            self._dense_p1_count > 0 and p1_frontier is None
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        self._dense_phase2_returned_count = len(response.record_ids)
        self._dense_k0 = k0
        self._dense_p1_frontier = p1_frontier
        self._dense_refined = True

    def _open_block(self, block_id: int) -> None:
        if block_id in self._opened_blocks:
            return
        block = self._blocks_by_id.get(block_id)
        if block is None:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        records = self._view.candidate_proof_block_records(
            folded_query=self._folded_query,
            block=block,
            head_revision=self._snapshot.head_revision,
            total_record_count=self._snapshot.total_record_count,
        )
        if type(records) is not tuple or len(records) != block.record_count:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        expected_record_id = block.first_record_id
        copied_records: list[SQLiteCandidateProofRecord] = []
        for record in records:
            if type(record) is not SQLiteCandidateProofRecord:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            integers = (
                record.record_id,
                record.block_id,
                record.source_fold_length,
                record.character_multiset_intersection,
                record.bigram_multiset_intersection,
            )
            if (
                any(type(item) is not int or item < 0 for item in integers)
                or record.record_id != expected_record_id
                or record.block_id != block_id
                or record.source_fold_length < 1
                or record.character_multiset_intersection
                > min(len(self._folded_query), record.source_fold_length)
                or record.bigram_multiset_intersection
                > min(
                    max(len(self._folded_query) - 1, 0),
                    max(record.source_fold_length - 1, 0),
                )
            ):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            copied_records.append(SQLiteCandidateProofRecord(**record.__dict__))
            expected_record_id += 1
        if (
            min(record.source_fold_length for record in copied_records)
            != block.min_source_fold_length
            or max(record.source_fold_length for record in copied_records)
            != block.max_source_fold_length
            or max(
                record.character_multiset_intersection
                for record in copied_records
            ) > block.character_intersection_upper
            or max(
                record.bigram_multiset_intersection
                for record in copied_records
            ) > block.bigram_intersection_upper
        ):
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        self._opened_blocks.add(block_id)
        for record in copied_records:
            if record.record_id in self._record_by_id:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            self._record_by_id[record.record_id] = record
            upper = _record_upper_bound(record, query_length=len(self._folded_query))
            self._upper_by_id[record.record_id] = upper
            heapq.heappush(self._record_heap, (-upper, -record.record_id, record.record_id))

    def _discard_opened_block_heads(self) -> None:
        while self._block_heap and self._block_heap[0][2] in self._opened_blocks:
            heapq.heappop(self._block_heap)

    def _record_frontier(self) -> tuple[float, int] | None:
        if self._dense_frontier_groups is not None:
            frontier = None
            if self._dense_frontier_groups:
                upper, record_ids = self._dense_frontier_groups[-1]
                frontier = (upper, record_ids[-1])
            if self._dense_refined and self._dense_p2_floor_frontier is not None:
                frontier = (
                    self._dense_p2_floor_frontier
                    if frontier is None
                    else max(frontier, self._dense_p2_floor_frontier)
                )
            return frontier
        return (
            (-self._record_heap[0][0], -self._record_heap[0][1])
            if self._record_heap
            else None
        )

    def _pop_record_frontier(self) -> tuple[float, int]:
        if self._dense_frontier_groups is not None:
            if not self._dense_frontier_groups:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            upper, record_ids = self._dense_frontier_groups[-1]
            record_id = record_ids.pop()
            if not record_ids:
                self._dense_frontier_groups.pop()
            return upper, record_id
        if not self._record_heap:
            raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        negative_upper, _negative_record_id, record_id = heapq.heappop(
            self._record_heap
        )
        return -negative_upper, record_id

    def _frontier(self) -> tuple[float, int] | None:
        self._discard_opened_block_heads()
        block_frontier = (
            (-self._block_heap[0][0], -self._block_heap[0][1])
            if self._block_heap
            else None
        )
        record_frontier = self._record_frontier()
        if block_frontier is None:
            frontier = record_frontier
            if self._dense_refined and self._dense_p1_frontier is not None:
                frontier = (
                    self._dense_p1_frontier
                    if frontier is None
                    else max(frontier, self._dense_p1_frontier)
                )
            return frontier
        if record_frontier is None:
            return block_frontier
        return max(block_frontier, record_frontier)

    def _closure(self) -> tuple[bool, bool, tuple[float, int] | None]:
        frontier = self._frontier()
        threshold_closed = (
            frontier is None or frontier[0] < self._minimum_similarity
        )
        total = self._snapshot.total_record_count
        if total < self._result_limit:
            top_k_closed = frontier is None
        elif len(self._scores) < self._result_limit:
            top_k_closed = False
        else:
            kth = heapq.nlargest(
                self._result_limit,
                (
                    (score, record_id)
                    for record_id, score in self._scores.items()
                ),
            )[-1]
            top_k_closed = frontier is None or frontier < kth
        return threshold_closed, top_k_closed, frontier

    def next_batch(self) -> tuple[int, ...]:
        if (
            self._dense_phase1 is not None
            and not self._dense_refined
            and len(self._scores)
            >= min(self._result_limit, self._snapshot.total_record_count)
        ):
            self._refine_dense_frontier()
        threshold_closed, top_k_closed, _frontier = self._closure()
        if threshold_closed and top_k_closed:
            return ()
        batch: list[int] = []
        batch_limit = CANDIDATE_PROOF_BATCH_SIZE
        if len(self._scores) < self._result_limit:
            batch_limit = min(
                batch_limit,
                self._result_limit - len(self._scores),
            )
        known_kth = (
            heapq.nlargest(
                self._result_limit,
                (
                    (score, record_id)
                    for record_id, score in self._scores.items()
                ),
            )[-1]
            if len(self._scores) >= self._result_limit
            else None
        )
        while len(batch) < batch_limit:
            if batch and known_kth is not None:
                frontier = self._frontier()
                if frontier is None or (
                    frontier[0] < self._minimum_similarity
                    and frontier < known_kth
                ):
                    break
            self._discard_opened_block_heads()
            block_key = (
                (-self._block_heap[0][0], -self._block_heap[0][1])
                if self._block_heap
                else None
            )
            record_key = self._record_frontier()
            if block_key is not None and (
                record_key is None or block_key >= record_key
            ):
                _neg_upper, _neg_id, block_id = heapq.heappop(self._block_heap)
                self._open_block(block_id)
                if not batch:
                    threshold_closed, top_k_closed, _frontier = self._closure()
                    if threshold_closed and top_k_closed:
                        return ()
                continue
            if record_key is None:
                break
            upper, record_id = self._pop_record_frontier()
            if record_id in self._scores or record_id in self._outstanding:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            if self._dense_phase1 is not None and record_id not in self._record_by_id:
                offset = record_id - 1
                character_intersection = min(
                    len(self._folded_query),
                    self._dense_phase1.source_fold_lengths[offset],
                )
                if self._dense_refined:
                    lcs_length = self._dense_lcs_by_id.get(record_id)
                    if lcs_length is None:
                        raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
                    character_intersection = lcs_length
                record = SQLiteCandidateProofRecord(
                    record_id=record_id,
                    block_id=offset // CANDIDATE_PROOF_BLOCK_SIZE,
                    source_fold_length=(
                        self._dense_phase1.source_fold_lengths[offset]
                    ),
                    character_multiset_intersection=character_intersection,
                    bigram_multiset_intersection=(
                        self._dense_phase1.bigram_multiset_intersections[offset]
                    ),
                )
                self._record_by_id[record_id] = record
                self._upper_by_id[record_id] = upper
            self._outstanding.add(record_id)
            batch.append(record_id)
        if not batch:
            threshold_closed, top_k_closed, _frontier = self._closure()
            if not (threshold_closed and top_k_closed):
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
        return tuple(batch)

    def observe(
        self,
        observations: tuple[tuple[int, SimilarityEvidence, bool], ...],
    ) -> None:
        if type(observations) is not tuple:
            raise TypeError("proof observations must be a tuple")
        seen: set[int] = set()
        for item in observations:
            if type(item) is not tuple or len(item) != 3:
                raise TypeError("proof observation is invalid")
            record_id, evidence, scorer_invoked = item
            if type(record_id) is not int or record_id in seen or record_id not in self._outstanding:
                raise ValueError("proof score identity is invalid")
            if type(evidence) is not SimilarityEvidence:
                raise TypeError("proof score evidence is invalid")
            if type(scorer_invoked) is not bool:
                raise TypeError("proof scorer invocation fact is invalid")
            score = evidence.final_similarity
            if (
                evidence.scorer_version != "scorer-v1"
                or type(score) is not float
                or not math.isfinite(score)
                or not 0.0 <= score <= 1.0
                or score > self._upper_by_id[record_id] + 1e-12
            ):
                raise ValueError("proof score evidence does not close")
            seen.add(record_id)
            self._scores[record_id] = score
            self._observation_order.append(record_id)
            if scorer_invoked:
                self._scorer_invocation_count += 1
                if self._scorer_invocation_count > self._budget:
                    raise CandidateProofBudgetExhausted()
        if seen != self._outstanding:
            raise ValueError("proof observations must close the outstanding batch")
        self._outstanding.clear()

    def finish(self) -> CandidateRetrievalReport:
        if self._outstanding:
            raise ValueError("proof has an outstanding scorer batch")
        if self._dense_phase1 is not None and not self._dense_refined:
            raise ValueError("dense proof must complete phase-two refinement")
        threshold_closed, top_k_closed, frontier = self._closure()
        if not (threshold_closed and top_k_closed):
            if self._scorer_invocation_count >= self._budget:
                raise CandidateProofBudgetExhausted()
            raise ValueError("candidate proof is not closed")
        self._view.validate_candidate_proof_generation(
            head_revision=self._snapshot.head_revision,
            total_record_count=self._snapshot.total_record_count,
        )
        kth = (
            heapq.nlargest(
                self._result_limit,
                (
                    (score, record_id)
                    for record_id, score in self._scores.items()
                ),
            )[-1]
            if self._snapshot.total_record_count >= self._result_limit
            else None
        )
        refinement: CandidateProofRefinementMetadata | None = None
        if self._traversal_mode == "DENSE":
            phase2_frontier = self._record_frontier()
            a1_count = len(self._scores) - self._dense_a0_count
            p2_count = self._dense_r_count - a1_count
            if a1_count < 0 or p2_count < 0:
                raise SQLiteStoreSchemaError("STORE.CANDIDATE_PROOF_INVALID")
            refinement = CandidateProofRefinementMetadata(
                phase="PHASE_2_COMPLETE",
                refined=True,
                a0_accounted_identity_count=self._dense_a0_count,
                p1_unscored_identity_count=self._dense_p1_count,
                r_refinement_identity_count=self._dense_r_count,
                a1_accounted_identity_count=a1_count,
                p2_unscored_identity_count=p2_count,
                refinement_request_count=self._dense_r_count,
                refinement_returned_count=self._dense_phase2_returned_count,
                k0_score=(None if self._dense_k0 is None else self._dense_k0[0]),
                k0_record_id=(
                    None if self._dense_k0 is None else self._dense_k0[1]
                ),
                p1_max_upper_bound=(
                    None
                    if self._dense_p1_frontier is None
                    else self._dense_p1_frontier[0]
                ),
                p1_possible_record_id=(
                    None
                    if self._dense_p1_frontier is None
                    else self._dense_p1_frontier[1]
                ),
                p2_max_upper_bound=(
                    None if phase2_frontier is None else phase2_frontier[0]
                ),
                p2_possible_record_id=(
                    None if phase2_frontier is None else phase2_frontier[1]
                ),
            )
        proof = CandidateProofMetadata(
            proof_version=CANDIDATE_PROOF_QUERY_VERSION,
            bound_version=SCORER_BOUND_VERSION_V1,
            block_version=CANDIDATE_PROOF_BLOCK_VERSION_V1,
            traversal_version=CANDIDATE_PROOF_TRAVERSAL_VERSION,
            traversal_mode=self._traversal_mode,
            total_block_count=len(self._snapshot.blocks),
            total_record_count=self._snapshot.total_record_count,
            scanned_block_count=(
                len(self._snapshot.blocks)
                if self._traversal_mode == "DENSE"
                else len(self._opened_blocks)
            ),
            opened_block_count=len(self._opened_blocks),
            inspected_record_count=(
                self._snapshot.total_record_count
                if self._traversal_mode == "DENSE"
                else sum(
                    self._blocks_by_id[block_id].record_count
                    for block_id in self._opened_blocks
                )
            ),
            seed_unique_count=len({
                record_id
                for _stage, ids in self._snapshot.seed_stages
                for record_id in ids
            }),
            scorer_invocation_count=self._scorer_invocation_count,
            accounted_identity_count=len(self._scores),
            unscored_identity_count=(
                self._snapshot.total_record_count - len(self._scores)
            ),
            unscored_max_upper_bound=(None if frontier is None else frontier[0]),
            unscored_possible_record_id=(None if frontier is None else frontier[1]),
            minimum_similarity=self._minimum_similarity,
            threshold_closed=threshold_closed,
            top_k=self._result_limit,
            kth_score=(None if kth is None else kth[0]),
            kth_record_id=(None if kth is None else kth[1]),
            top_k_closed=top_k_closed,
            refinement=refinement,
        )
        seed_pool: set[int] = set()
        stages: list[CandidateStageMetadata] = []
        for stage_name, ids in self._snapshot.seed_stages:
            before = len(seed_pool)
            seed_pool.update(ids)
            stages.append(CandidateStageMetadata(
                stage=CandidateStage(stage_name),
                input_count=before,
                added_unique_count=len(seed_pool) - before,
                output_unique_count=len(seed_pool),
                dropped_count=0,
            ))
        scored_ids = set(self._scores)
        stages.append(CandidateStageMetadata(
            stage=CandidateStage.BOUND_PROOF,
            input_count=len(seed_pool),
            added_unique_count=len(scored_ids - seed_pool),
            output_unique_count=len(scored_ids),
            dropped_count=len(seed_pool - scored_ids),
        ))
        for stage in (CandidateStage.UNION, CandidateStage.DEDUPLICATE):
            stages.append(CandidateStageMetadata(
                stage=stage,
                input_count=len(scored_ids),
                added_unique_count=0,
                output_unique_count=len(scored_ids),
                dropped_count=0,
            ))
        query_gram_count = max(len(self._folded_query) - 1, 1)
        candidates = tuple(
            CandidateEvidence(
                record_id=record_id,
                recall_stages=(CandidateStage.BOUND_PROOF,),
                matched_grams=(
                    self._record_by_id[record_id].bigram_multiset_intersection
                ),
                query_grams=query_gram_count,
                overlap_ratio=(
                    self._record_by_id[record_id].bigram_multiset_intersection
                    / query_gram_count
                ),
                pretruncate_rank=index,
            )
            for index, record_id in enumerate(self._observation_order, start=1)
        )
        return CandidateRetrievalReport(
            candidates=candidates,
            metadata=CandidateRecallMetadata(
                resource_id=self._resource_id,
                index_kind=self._snapshot.index_kind,
                fuzzy_available=True,
                fuzzy_unavailable_code=None,
                stages=tuple(stages),
                union_unique_count=len(scored_ids),
                deduplicated_count=len(scored_ids),
                result_limit=self._result_limit,
                candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                candidate_budget=self._budget,
                truncated=False,
                proof=proof,
            ),
        )



class CandidateRetriever:
    """Sole recall orchestrator for candidate-budget-v1 evidence."""

    def candidates(
        self,
        resource_id: str,
        store: SQLiteTMStore,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport:
        if type(resource_id) is not str:
            raise TypeError("resource_id must be a built-in string")
        if not resource_id.strip():
            raise ValueError("resource_id must not be empty")
        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be SQLiteTMStore")
        _validate_candidate_scalars(folded_query, result_limit)
        if store.coordinator.resource_id != resource_id:
            raise ValueError("resource_id must match the store resource")
        prepared = _prepare_candidate_query(folded_query, result_limit)
        snapshot = _candidate_recall_snapshot_or_fail(store, prepared)
        return _build_candidate_report(
            resource_id,
            folded_query,
            prepared,
            snapshot,
        )

    def candidates_from_view(
        self,
        resource_id: str,
        view: SQLiteTMQueryView,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport:
        if type(resource_id) is not str:
            raise TypeError("resource_id must be a built-in string")
        if not resource_id.strip():
            raise ValueError("resource_id must not be empty")
        if type(view) is not SQLiteTMQueryView:
            raise TypeError("view must be SQLiteTMQueryView")
        _validate_candidate_scalars(folded_query, result_limit)
        if view.resource_id != resource_id:
            raise ValueError("resource_id must match the view resource")
        prepared = _prepare_candidate_query(folded_query, result_limit)
        snapshot = _candidate_recall_snapshot_or_fail(view, prepared)
        return _build_candidate_report(
            resource_id,
            folded_query,
            prepared,
            snapshot,
        )

    def proof_session_from_view(
        self,
        resource_id: str,
        view: SQLiteTMQueryView,
        folded_query: str,
        *,
        minimum_similarity: float,
        result_limit: int,
    ) -> CandidateProofSession:
        """Create the private alternating proof port on one query view."""

        if type(resource_id) is not str or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty built-in string")
        return CandidateProofSession(
            resource_id=resource_id,
            view=view,
            folded_query=folded_query,
            minimum_similarity=minimum_similarity,
            result_limit=result_limit,
        )



__all__ = [
    "CANDIDATE_CONTRACT_FLOOR",
    "CANDIDATE_PROOF_BUDGET_EXHAUSTED",
    "CandidateProofBudgetExhausted",
    "CandidateProofSession",
    "CandidateRetriever",
    "FTS5CandidateResult",
    "FTS5TrigramIndex",
    "FTS5_QUERY_TOO_SHORT_CODE",
    "FTS5_UNAVAILABLE_CODE",
    "GRAM_CANDIDATE_HARD_CAP",
    "GRAM_EMPTY_QUERY_CODE",
    "GRAM_LONG_QUERY_FTS_SELECTED_CODE",
    "GramCandidateResult",
    "GramPostingEvidence",
    "GramPostingIndex",
    "build_candidate_write_plan",
    "build_fts5_match_expression",
    "unique_character_ngrams",
    "unique_character_trigrams",
]
