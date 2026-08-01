"""FTS5 trigram recall primitives for already-folded canonical TM text."""

from __future__ import annotations

from dataclasses import dataclass

from tm_sqlite_store import (
    SQLiteCandidateRecord,
    SQLiteCandidateWritePlan,
    SQLiteGramRow,
    SQLiteTMStore,
)


FTS5_UNAVAILABLE_CODE = "CANDIDATE.FTS5_UNAVAILABLE"
FTS5_QUERY_TOO_SHORT_CODE = "CANDIDATE.FTS_QUERY_TOO_SHORT"
GRAM_EMPTY_QUERY_CODE = "CANDIDATE.GRAM_QUERY_EMPTY"
GRAM_LONG_QUERY_FTS_SELECTED_CODE = "CANDIDATE.GRAM_LONG_QUERY_FTS_SELECTED"
GRAM_CANDIDATE_HARD_CAP = 8192
GRAM_QUERY_POSTING_HARD_CAP = 4096


def unique_character_ngrams(folded_text: str, gram_size: int) -> tuple[str, ...]:
    """Return first-occurrence unique code-point grams without folding input."""

    if type(folded_text) is not str:
        raise TypeError("folded_text must be a built-in string")
    if type(gram_size) is not int:
        raise TypeError("gram_size must be a built-in integer")
    if gram_size not in {1, 2, 3}:
        raise ValueError("gram_size must be 1, 2, or 3")
    seen: set[str] = set()
    grams: list[str] = []
    for offset in range(max(0, len(folded_text) - gram_size + 1)):
        gram = folded_text[offset : offset + gram_size]
        if gram not in seen:
            seen.add(gram)
            grams.append(gram)
    return tuple(grams)


def unique_character_trigrams(folded_query: str) -> tuple[str, ...]:
    """Return first-occurrence ordered unique code-point trigrams."""

    return unique_character_ngrams(folded_query, 3)


def build_candidate_write_plan(
    records: tuple[SQLiteCandidateRecord, ...],
    *,
    fts5_available: bool,
) -> SQLiteCandidateWritePlan:
    """Build the complete per-configuration candidate plan exactly once."""

    if type(records) is not tuple:
        raise TypeError("records must be a built-in tuple")
    if type(fts5_available) is not bool:
        raise TypeError("fts5_available must be a built-in bool")
    prepared: list[tuple[int, str]] = []
    for record in records:
        if type(record) is not SQLiteCandidateRecord:
            raise TypeError("records must contain exact SQLiteCandidateRecord values")
        origin_ordinal = record.origin_ordinal
        folded_source = record.source_fold_v1
        if type(origin_ordinal) is not int or origin_ordinal < 0:
            raise ValueError("origin_ordinal must be a non-negative integer")
        if type(folded_source) is not str or not folded_source:
            raise ValueError("source_fold_v1 must be a non-empty built-in string")
        prepared.append((origin_ordinal, folded_source))

    gram_sizes = (1, 2) if fts5_available else (1, 2, 3)
    gram_rows: list[SQLiteGramRow] = []
    for origin_ordinal, folded_source in prepared:
        for gram_size in gram_sizes:
            gram_rows.extend(
                SQLiteGramRow(
                    origin_ordinal=origin_ordinal,
                    gram_size=gram_size,
                    gram=gram,
                )
                for gram in unique_character_ngrams(folded_source, gram_size)
            )
    return SQLiteCandidateWritePlan(
        gram_rows=tuple(gram_rows),
        fts_origin_ordinals=(
            tuple(origin_ordinal for origin_ordinal, _source in prepared)
            if fts5_available
            else ()
        ),
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
        )[:GRAM_QUERY_POSTING_HARD_CAP]
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
        record_ids = store.fts5_candidate_ids(
            build_fts5_match_expression(trigrams)
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


__all__ = [
    "FTS5CandidateResult",
    "FTS5TrigramIndex",
    "FTS5_QUERY_TOO_SHORT_CODE",
    "FTS5_UNAVAILABLE_CODE",
    "GRAM_CANDIDATE_HARD_CAP",
    "GRAM_EMPTY_QUERY_CODE",
    "GRAM_LONG_QUERY_FTS_SELECTED_CODE",
    "GRAM_QUERY_POSTING_HARD_CAP",
    "GramCandidateResult",
    "GramPostingEvidence",
    "GramPostingIndex",
    "build_candidate_write_plan",
    "build_fts5_match_expression",
    "unique_character_ngrams",
    "unique_character_trigrams",
]
