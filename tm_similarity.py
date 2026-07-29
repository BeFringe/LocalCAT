"""Deterministic similarity-v1 scoring for translation-memory candidates."""

from __future__ import annotations

from collections import Counter

from text_matcher import fold_text_v1
from tm_contracts import SCORER_VERSION_V1, SimilarityEvidence


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


__all__ = ["SimilarityScorerV1"]
