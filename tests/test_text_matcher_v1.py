from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest
from typing import Any, cast

from text_matcher import (
    TEXT_MATCHER_SEMANTICS_VERSION,
    TextMatcherV1,
)
from tm_contracts import SearchHit, SearchOptions, TextMatchProfile


_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "text_matcher_v1_vectors.json"
)


def _load_fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")),
    )


_FIXTURE = _load_fixture()


class TextMatcherV1Tests(unittest.TestCase):
    matcher = TextMatcherV1()

    def test_versioned_golden_vectors_return_original_offsets(self) -> None:
        self.assertEqual(
            self.matcher.semantics_version,
            TEXT_MATCHER_SEMANTICS_VERSION,
        )
        self.assertEqual(
            _FIXTURE["semantics_version"],
            TEXT_MATCHER_SEMANTICS_VERSION,
        )
        self.assertEqual(
            _FIXTURE["fixture_version"],
            "text-matcher-v1-vectors-v1",
        )
        for raw_vector in cast(
            list[dict[str, object]],
            _FIXTURE["vectors"],
        ):
            with self.subTest(vector=raw_vector["id"]):
                raw_options = cast(
                    dict[str, bool],
                    raw_vector["options"],
                )
                actual = self.matcher.match(
                    text=cast(str, raw_vector["text"]),
                    query=cast(str, raw_vector["query"]),
                    profile=TextMatchProfile(
                        cast(str, raw_vector["profile"])
                    ),
                    options=SearchOptions(**raw_options),
                )
                expected = tuple(
                    SearchHit(start_index=start, end_index=end)
                    for start, end in cast(
                        list[list[int]],
                        raw_vector["hits"],
                    )
                )
                self.assertEqual(actual, expected)
                self.assertEqual(
                    actual,
                    tuple(
                        sorted(
                            set(actual),
                            key=lambda hit: (
                                hit.start_index,
                                hit.end_index,
                            ),
                        )
                    ),
                )

    def test_fixture_covers_profiles_and_all_configurable_options(self) -> None:
        vectors = cast(
            list[dict[str, object]],
            _FIXTURE["vectors"],
        )
        profiles = {
            cast(str, vector["profile"]) for vector in vectors
        }
        self.assertEqual(
            profiles,
            {profile.value for profile in TextMatchProfile},
        )
        configurable_options = {
            (
                cast(dict[str, bool], vector["options"])["match_case"],
                cast(dict[str, bool], vector["options"])["whole_word"],
            )
            for vector in vectors
            if vector["profile"]
            == TextMatchProfile.CONFIGURABLE_TEXT_V1.value
        }
        self.assertEqual(
            configurable_options,
            {
                (False, False),
                (False, True),
                (True, False),
                (True, True),
            },
        )

    def test_pure_cjk_whole_word_equals_contiguous_matching(self) -> None:
        text = "甲中文乙 中文"
        query = "中文"
        whole_word = self.matcher.match(
            text=text,
            query=query,
            profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
            options=SearchOptions(
                match_case=False,
                whole_word=True,
            ),
        )
        continuous = self.matcher.match(
            text=text,
            query=query,
            profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
            options=SearchOptions(
                match_case=False,
                whole_word=False,
            ),
        )
        self.assertEqual(whole_word, continuous)
        self.assertEqual(
            whole_word,
            (
                SearchHit(start_index=1, end_index=3),
                SearchHit(start_index=5, end_index=7),
            ),
        )

    def test_repeat_execution_is_deterministic_and_hits_are_frozen(self) -> None:
        options = SearchOptions(
            match_case=False,
            whole_word=False,
        )
        first = self.matcher.match(
            text="ßß SS",
            query="s",
            profile=TextMatchProfile.BASIC_CONTIGUOUS,
            options=options,
        )
        for _ in range(20):
            self.assertEqual(
                self.matcher.match(
                    text="ßß SS",
                    query="s",
                    profile=TextMatchProfile.BASIC_CONTIGUOUS,
                    options=options,
                ),
                first,
            )
        self.assertEqual(
            first,
            (
                SearchHit(start_index=0, end_index=1),
                SearchHit(start_index=1, end_index=2),
                SearchHit(start_index=3, end_index=4),
                SearchHit(start_index=4, end_index=5),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(first[0], "start_index", 99)

    def test_invalid_inputs_and_profile_options_fail_closed(self) -> None:
        valid: dict[str, Any] = {
            "text": "text",
            "query": "t",
            "profile": TextMatchProfile.LEGACY_COMPAT,
            "options": SearchOptions(
                match_case=True,
                whole_word=False,
            ),
        }
        for field, invalid in (
            ("text", None),
            ("query", 1),
            ("profile", "LEGACY_COMPAT"),
            ("options", (True, False)),
        ):
            arguments: dict[str, Any] = dict(valid)
            arguments[field] = invalid
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    cast(Any, self.matcher.match)(**arguments)

        incompatible = (
            (
                TextMatchProfile.LEGACY_COMPAT,
                SearchOptions(match_case=False, whole_word=False),
            ),
            (
                TextMatchProfile.LEGACY_COMPAT,
                SearchOptions(match_case=True, whole_word=True),
            ),
            (
                TextMatchProfile.BASIC_CONTIGUOUS,
                SearchOptions(match_case=True, whole_word=False),
            ),
            (
                TextMatchProfile.BASIC_CONTIGUOUS,
                SearchOptions(match_case=False, whole_word=True),
            ),
        )
        for profile, options in incompatible:
            with self.subTest(profile=profile, options=options):
                with self.assertRaisesRegex(
                    ValueError,
                    "^options are not allowed for text match profile$",
                ):
                    self.matcher.match(
                        text="text",
                        query="t",
                        profile=profile,
                        options=options,
                    )

    def test_internal_algorithm_does_not_implement_capability_gating(self) -> None:
        source = (
            Path(__file__).parents[1] / "text_matcher.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
        self.assertNotIn("TextMatcherCapability", names)
        self.assertNotIn("MatcherCapabilityEvaluator", names)
        self.assertNotIn("CapabilityGatedTextMatcher", names)
        self.assertNotIn("TextMatchSuccess", names)
        self.assertNotIn("TextMatchRejected", names)


if __name__ == "__main__":
    unittest.main()
