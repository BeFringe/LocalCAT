from __future__ import annotations

from collections import Counter
from itertools import product
import json
from pathlib import Path
import random
import unicodedata
import unittest
from typing import Any, cast

from text_matcher import fold_text_v1
from tm_contracts import (
    SCORER_BOUND_VERSION_V1,
    SCORER_VERSION_V1,
    SimilarityEvidence,
    SimilarityScorer,
)
from tm_similarity import SimilarityScorerV1, scorer_upper_bound_v1


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
    def _assert_score_bounded(self, query: str, candidate: str) -> None:
        query_folded = fold_text_v1(query).folded_text
        candidate_folded = fold_text_v1(candidate).folded_text
        if not query_folded and not candidate_folded:
            return
        query_characters = Counter(query_folded)
        candidate_characters = Counter(candidate_folded)
        query_bigrams = Counter(zip(query_folded, query_folded[1:]))
        candidate_bigrams = Counter(
            zip(candidate_folded, candidate_folded[1:])
        )
        bound = scorer_upper_bound_v1(
            query_fold_length=len(query_folded),
            record_fold_length=len(candidate_folded),
            character_multiset_intersection=(
                query_characters & candidate_characters
            ).total(),
            bigram_multiset_intersection=(
                query_bigrams & candidate_bigrams
            ).total(),
            query_bigram_count=query_bigrams.total(),
            record_bigram_count=candidate_bigrams.total(),
        )
        self.assertEqual(bound.bound_version, SCORER_BOUND_VERSION_V1)
        self.assertGreaterEqual(
            bound.final_similarity_upper_bound,
            _SCORER.score(query, candidate).final_similarity,
        )

    def test_bound_proves_exhaustive_small_alphabet_scores(self) -> None:
        values = [
            "".join(characters)
            for length in range(6)
            for characters in product("ab", repeat=length)
        ]
        for query in values:
            for candidate in values:
                with self.subTest(query=query, candidate=candidate):
                    self._assert_score_bounded(query, candidate)

    def test_bound_proves_deterministic_unicode_random_scores(self) -> None:
        randomizer = random.Random(0x8_6)
        alphabet = ("A", "a", "ß", "é", "\u0301", "İ", "中", "🙂")
        pairs = [
            ("Straße", "STRASSE"),
            ("e\u0301", "é"),
            ("İ", "i\u0307"),
            ("ﬀ", "ff"),
            ("中🙂中", "中🙂"),
        ]
        pairs.extend(
            (
                "".join(randomizer.choices(alphabet, k=randomizer.randrange(9))),
                "".join(randomizer.choices(alphabet, k=randomizer.randrange(9))),
            )
            for _ in range(1_000)
        )
        for query, candidate in pairs:
            if query or candidate:
                self._assert_score_bounded(query, candidate)

    def test_bound_closes_exact_single_character_dice(self) -> None:
        equal = scorer_upper_bound_v1(
            query_fold_length=1,
            record_fold_length=1,
            character_multiset_intersection=1,
            bigram_multiset_intersection=0,
            query_bigram_count=0,
            record_bigram_count=0,
        )
        unequal = scorer_upper_bound_v1(
            query_fold_length=1,
            record_fold_length=1,
            character_multiset_intersection=0,
            bigram_multiset_intersection=0,
            query_bigram_count=0,
            record_bigram_count=0,
        )
        self.assertEqual(equal.dice_bigram_exact, 1.0)
        self.assertEqual(equal.final_similarity_upper_bound, 1.0)
        self.assertEqual(unequal.dice_bigram_exact, 0.0)
        self.assertEqual(unequal.final_similarity_upper_bound, 0.0)

    def test_bound_rejects_non_exact_or_inconsistent_facts_before_math(
        self,
    ) -> None:
        class IntSubclass(int):
            def __sub__(self, other: object) -> int:
                raise AssertionError("subclass arithmetic was dispatched")

        valid = {
            "query_fold_length": 2,
            "record_fold_length": 2,
            "character_multiset_intersection": 2,
            "bigram_multiset_intersection": 1,
            "query_bigram_count": 1,
            "record_bigram_count": 1,
        }
        for field, value in (
            ("query_fold_length", True),
            ("record_fold_length", IntSubclass(2)),
            ("character_multiset_intersection", 3),
            ("bigram_multiset_intersection", 2),
            ("query_bigram_count", 0),
        ):
            facts = dict(valid)
            facts[field] = value
            with self.subTest(field=field), self.assertRaises(
                (TypeError, ValueError)
            ):
                scorer_upper_bound_v1(**facts)

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

    def test_blocked_canonical_composition_self_score_is_exact(self) -> None:
        raw = "\u1E9B\u0323"
        evidence = _SCORER.score(raw, raw)
        self.assertEqual(
            evidence,
            SimilarityEvidence(1.0, 1.0, 1.0),
        )

    def test_non_string_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "^query must be a string$"):
            _SCORER.score(cast(Any, 1), "candidate")
        with self.assertRaisesRegex(TypeError, "^candidate must be a string$"):
            _SCORER.score("query", cast(Any, None))


if __name__ == "__main__":
    unittest.main()
