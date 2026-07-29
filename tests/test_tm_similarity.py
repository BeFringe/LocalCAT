from __future__ import annotations

import json
from pathlib import Path
import unicodedata
import unittest
from typing import Any, cast

from tm_contracts import (
    SCORER_VERSION_V1,
    SimilarityEvidence,
    SimilarityScorer,
)
from tm_similarity import SimilarityScorerV1


_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "tm_similarity_vectors.json"
)


def _load_fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def _valid_vectors(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], vector)
        for vector in cast(list[object], fixture["vectors"])
        if cast(dict[str, Any], vector)["expected_error"] is None
    ]


_FIXTURE = _load_fixture()
_SCORER: SimilarityScorer = SimilarityScorerV1()


class SimilarityScorerV1Tests(unittest.TestCase):
    def test_versioned_golden_vectors_match_every_score_component(
        self,
    ) -> None:
        self.assertEqual(
            _FIXTURE["fixture_version"],
            "tm-similarity-vectors-v1",
        )
        self.assertEqual(_FIXTURE["algorithm_version"], "similarity-v1")
        self.assertEqual(_FIXTURE["fold_version"], "fold-v1")
        self.assertEqual(
            _FIXTURE["scorer_version"],
            SCORER_VERSION_V1,
        )

        for vector in _valid_vectors(_FIXTURE):
            with self.subTest(vector=vector["id"]):
                query_raw = cast(str, vector["query_raw"])
                candidate_raw = cast(str, vector["candidate_raw"])
                query_folded = unicodedata.normalize(
                    "NFC",
                    query_raw,
                ).casefold()
                candidate_folded = unicodedata.normalize(
                    "NFC",
                    candidate_raw,
                ).casefold()
                self.assertEqual(query_folded, vector["query_folded"])
                self.assertEqual(candidate_folded, vector["candidate_folded"])

                query_bigram_count = max(len(query_folded) - 1, 0)
                candidate_bigram_count = max(
                    len(candidate_folded) - 1,
                    0,
                )
                self.assertEqual(
                    query_bigram_count,
                    vector["query_bigram_count"],
                )
                self.assertEqual(
                    candidate_bigram_count,
                    vector["candidate_bigram_count"],
                )

                distance = cast(int, vector["levenshtein_distance"])
                expected_levenshtein = 1.0 - (
                    distance / max(len(query_folded), len(candidate_folded))
                )
                self.assertEqual(
                    expected_levenshtein,
                    vector["levenshtein_ratio"],
                )

                shared_bigram_count = cast(
                    int,
                    vector["shared_bigram_count"],
                )
                if len(query_folded) == len(candidate_folded) == 1:
                    expected_dice = float(query_folded == candidate_folded)
                else:
                    gram_count = query_bigram_count + candidate_bigram_count
                    expected_dice = (
                        2.0 * shared_bigram_count / gram_count
                        if gram_count
                        else 0.0
                    )
                self.assertEqual(expected_dice, vector["dice_bigram"])

                expected_final = (
                    expected_levenshtein + expected_dice
                ) / 2.0
                self.assertEqual(
                    expected_final,
                    vector["final_similarity"],
                )

                evidence = _SCORER.score(query_raw, candidate_raw)
                self.assertIsInstance(evidence, SimilarityEvidence)
                self.assertEqual(
                    evidence.levenshtein_ratio,
                    vector["levenshtein_ratio"],
                )
                self.assertEqual(
                    evidence.dice_bigram,
                    vector["dice_bigram"],
                )
                self.assertEqual(
                    evidence.final_similarity,
                    vector["final_similarity"],
                )
                self.assertEqual(evidence.scorer_version, SCORER_VERSION_V1)

    def test_both_empty_fails_closed_with_stable_value_error(self) -> None:
        vector = cast(
            dict[str, Any],
            next(
                item
                for item in cast(list[dict[str, Any]], _FIXTURE["vectors"])
                if item["id"] == "both-empty-rejected"
            ),
        )
        expected_error = cast(dict[str, str], vector["expected_error"])
        self.assertEqual(expected_error["type"], "ValueError")
        with self.assertRaisesRegex(
            ValueError,
            f"^{expected_error['message']}$",
        ):
            _SCORER.score(
                cast(str, vector["query_raw"]),
                cast(str, vector["candidate_raw"]),
            )

    def test_one_sided_empty_inputs_produce_zero_evidence(self) -> None:
        vectors = {
            cast(str, vector["id"]): vector
            for vector in _valid_vectors(_FIXTURE)
            if cast(str, vector["id"]).startswith("one-sided-empty")
        }
        self.assertEqual(
            set(vectors),
            {"one-sided-empty-query", "one-sided-empty-candidate"},
        )
        for vector in vectors.values():
            with self.subTest(vector=vector["id"]):
                evidence = _SCORER.score(
                    cast(str, vector["query_raw"]),
                    cast(str, vector["candidate_raw"]),
                )
                self.assertEqual(
                    evidence,
                    SimilarityEvidence(0.0, 0.0, 0.0),
                )

    def test_threshold_boundary_is_preserved_without_filtering_or_rounding(
        self,
    ) -> None:
        vector = next(
            vector
            for vector in _valid_vectors(_FIXTURE)
            if vector["threshold_relation"] == "AT"
        )
        evidence = _SCORER.score(
            cast(str, vector["query_raw"]),
            cast(str, vector["candidate_raw"]),
        )
        self.assertEqual(
            evidence.final_similarity,
            _FIXTURE["reference_threshold"],
        )
        self.assertEqual(
            evidence.levenshtein_ratio,
            0.19999999999999996,
        )

    def test_repeated_execution_returns_identical_frozen_evidence(self) -> None:
        repeat_count = cast(int, _FIXTURE["repeat_count"])
        for vector in _valid_vectors(_FIXTURE):
            with self.subTest(vector=vector["id"]):
                query_raw = cast(str, vector["query_raw"])
                candidate_raw = cast(str, vector["candidate_raw"])
                expected = _SCORER.score(query_raw, candidate_raw)
                repeated = tuple(
                    _SCORER.score(query_raw, candidate_raw)
                    for _ in range(repeat_count)
                )
                self.assertEqual(repeated, (expected,) * repeat_count)

    def test_non_string_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "^query must be a string$"):
            _SCORER.score(cast(Any, 1), "candidate")
        with self.assertRaisesRegex(TypeError, "^candidate must be a string$"):
            _SCORER.score("query", cast(Any, None))


if __name__ == "__main__":
    unittest.main()
