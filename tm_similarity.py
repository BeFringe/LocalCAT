"""Deterministic similarity-v1 scoring for translation-memory candidates."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

from text_matcher import fold_text_v1
from tm_contracts import (
    SCORER_BOUND_VERSION_V1,
    SCORER_VERSION_V1,
    SimilarityEvidence,
)


@dataclass(frozen=True)
class SimilarityUpperBoundV1:
    """Closed proof facts and the safe scorer-v1 similarity upper bound."""

    bound_version: str
    edit_distance_lower_bound: int
    levenshtein_ratio_upper_bound: float
    dice_bigram_exact: float
    final_similarity_upper_bound: float


def scorer_upper_bound_v1(
    *,
    query_fold_length: int,
    record_fold_length: int,
    character_multiset_intersection: int,
    bigram_multiset_intersection: int,
    query_bigram_count: int,
    record_bigram_count: int,
) -> SimilarityUpperBoundV1:
    """Return a pure safe upper bound for one already-folded scorer pair."""

    facts = (
        ("query_fold_length", query_fold_length),
        ("record_fold_length", record_fold_length),
        ("character_multiset_intersection", character_multiset_intersection),
        ("bigram_multiset_intersection", bigram_multiset_intersection),
        ("query_bigram_count", query_bigram_count),
        ("record_bigram_count", record_bigram_count),
    )
    for name, value in facts:
        if type(value) is not int:
            raise TypeError(f"{name} must be a built-in integer")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")

    m = query_fold_length
    n = record_fold_length
    character_intersection = character_multiset_intersection
    bigram_intersection = bigram_multiset_intersection
    if m == 0 and n == 0:
        raise ValueError("folded inputs cannot both be empty")
    if character_intersection > min(m, n):
        raise ValueError("character intersection exceeds folded lengths")
    if query_bigram_count != max(m - 1, 0):
        raise ValueError("query bigram count does not close")
    if record_bigram_count != max(n - 1, 0):
        raise ValueError("record bigram count does not close")
    if bigram_intersection > min(query_bigram_count, record_bigram_count):
        raise ValueError("bigram intersection exceeds bigram counts")

    bigram_delta = (
        query_bigram_count
        + record_bigram_count
        - (2 * bigram_intersection)
    )
    edit_distance_lower_bound = max(
        abs(m - n),
        max(m, n) - character_intersection,
        (bigram_delta + 3) // 4,
    )
    longest_length = max(m, n)
    levenshtein_ratio_upper_bound = 1.0 - (
        edit_distance_lower_bound / longest_length
    )
    if m == n == 1:
        dice_bigram_exact = float(character_intersection == 1)
    else:
        total_bigrams = query_bigram_count + record_bigram_count
        dice_bigram_exact = (
            (2.0 * bigram_intersection / total_bigrams)
            if total_bigrams
            else 0.0
        )
    final_similarity_upper_bound = (
        levenshtein_ratio_upper_bound + dice_bigram_exact
    ) / 2.0
    numeric_closure = (
        levenshtein_ratio_upper_bound,
        dice_bigram_exact,
        final_similarity_upper_bound,
    )
    if edit_distance_lower_bound > longest_length or any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0
        for value in numeric_closure
    ):
        raise ValueError("scorer upper bound does not close")
    return SimilarityUpperBoundV1(
        bound_version=SCORER_BOUND_VERSION_V1,
        edit_distance_lower_bound=edit_distance_lower_bound,
        levenshtein_ratio_upper_bound=levenshtein_ratio_upper_bound,
        dice_bigram_exact=dice_bigram_exact,
        final_similarity_upper_bound=final_similarity_upper_bound,
    )


class SimilarityScorerV1:
    """Score folded Unicode code points with Levenshtein and bigram Dice."""

    def score(
        self,
        query: str,
        candidate: str,
    ) -> SimilarityEvidence:
        query_folded = _fold_v1(query, "query")
        candidate_folded = _fold_v1(candidate, "candidate")
        if not query_folded and not candidate_folded:
            raise ValueError("query and candidate cannot both be empty")

        distance = _levenshtein_distance(
            query_folded,
            candidate_folded,
        )
        longest_length = max(len(query_folded), len(candidate_folded))
        levenshtein_ratio = 1.0 - (distance / longest_length)
        dice_bigram = _multiset_bigram_dice(
            query_folded,
            candidate_folded,
        )
        final_similarity = (levenshtein_ratio + dice_bigram) / 2.0
        return SimilarityEvidence(
            levenshtein_ratio=levenshtein_ratio,
            dice_bigram=dice_bigram,
            final_similarity=final_similarity,
            scorer_version=SCORER_VERSION_V1,
        )


def _fold_v1(raw: str, label: str) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"{label} must be a string")
    return fold_text_v1(raw).folded_text


def _levenshtein_distance(query: str, candidate: str) -> int:
    if query == candidate:
        return 0
    if not query:
        return len(candidate)
    if not candidate:
        return len(query)
    if len(query) < len(candidate):
        query, candidate = candidate, query

    previous_row = list(range(len(candidate) + 1))
    for query_index, query_code_point in enumerate(query, start=1):
        current_row = [query_index]
        for candidate_index, candidate_code_point in enumerate(
            candidate,
            start=1,
        ):
            current_row.append(
                min(
                    current_row[-1] + 1,
                    previous_row[candidate_index] + 1,
                    previous_row[candidate_index - 1]
                    + (query_code_point != candidate_code_point),
                )
            )
        previous_row = current_row
    return previous_row[-1]


def _multiset_bigram_dice(query: str, candidate: str) -> float:
    if len(query) == len(candidate) == 1:
        return float(query == candidate)

    query_bigrams = Counter(zip(query, query[1:]))
    candidate_bigrams = Counter(zip(candidate, candidate[1:]))
    gram_count = query_bigrams.total() + candidate_bigrams.total()
    if gram_count == 0:
        return 0.0
    shared_count = (query_bigrams & candidate_bigrams).total()
    return 2.0 * shared_count / gram_count


__all__ = [
    "SimilarityScorerV1",
    "SimilarityUpperBoundV1",
    "scorer_upper_bound_v1",
]
