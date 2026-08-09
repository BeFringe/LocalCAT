"""FTS5 trigram recall primitives for already-folded canonical TM text."""

from __future__ import annotations

from dataclasses import dataclass

from tm_sqlite_store import (
    SQLiteCandidateRecord,
    SQLiteCandidateRecallSnapshot,
    SQLiteCandidateWritePlan,
    SQLiteTMStore,
    SQLiteStoreSchemaError,
    build_candidate_write_plan as _store_build_candidate_write_plan,
    unique_character_ngrams as _store_unique_character_ngrams,
)
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    candidate_budget_v1,
)


FTS5_UNAVAILABLE_CODE = "CANDIDATE.FTS5_UNAVAILABLE"
FTS5_QUERY_TOO_SHORT_CODE = "CANDIDATE.FTS_QUERY_TOO_SHORT"
GRAM_EMPTY_QUERY_CODE = "CANDIDATE.GRAM_QUERY_EMPTY"
GRAM_LONG_QUERY_FTS_SELECTED_CODE = "CANDIDATE.GRAM_LONG_QUERY_FTS_SELECTED"
GRAM_CANDIDATE_HARD_CAP = 8192
CANDIDATE_CONTRACT_FLOOR = candidate_budget_v1(1)


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
        if type(folded_query) is not str:
            raise TypeError("folded_query must be a built-in string")
        if type(result_limit) is not int:
            raise TypeError("result_limit must be a built-in integer")
        if result_limit < 1:
            raise ValueError("result_limit must be positive")
        if store.coordinator.resource_id != resource_id:
            raise ValueError("resource_id must match the store resource")

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

        try:
            snapshot = _copy_candidate_recall_snapshot(
                store.candidate_recall_snapshot(
                    fts_query_trigrams=fts_query_trigrams,
                    query_grams_by_size=query_grams_by_size,
                    candidate_floor=CANDIDATE_CONTRACT_FLOOR,
                    fts_query_degenerate=fts_query_degenerate,
                )
            )
        except (TypeError, ValueError) as error:
            raise SQLiteStoreSchemaError(
                "STORE.CANDIDATE_EVIDENCE_INVALID"
            ) from error
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
                    result_limit=result_limit,
                    candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                    candidate_budget=budget,
                    truncated=False,
                ),
            )

        grams_by_stage = {
            CandidateStage[f"GRAM_{gram_size}"]: grams
            for gram_size, grams in query_grams_by_size
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
            if not fts_ids or fts_query_degenerate or len(fts_ids) < CANDIDATE_CONTRACT_FLOOR:
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
        truncated = union_count > budget
        if truncated:
            stage_metadata.append(
                CandidateStageMetadata(
                    stage=CandidateStage.TRUNCATE,
                    input_count=union_count,
                    added_unique_count=0,
                    output_unique_count=budget,
                    dropped_count=union_count - budget,
                )
            )
            returned_ids = ranked_ids[:budget]
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
                result_limit=result_limit,
                candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                candidate_budget=budget,
                truncated=truncated,
            ),
        )


__all__ = [
    "CANDIDATE_CONTRACT_FLOOR",
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
