from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

from capability_host import CapabilityHostComposition
from editor_controller import EditorController, EditorControllerError
from glossary_engine import GlossaryEngine
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from tm_contracts import (
    SearchHit,
    SearchOptions,
    TextMatchProfile,
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherState,
)


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_SHARED_VECTORS_PATH = (
    Path(__file__).parent / "fixtures" / "text_matcher_v1_vectors.json"
)


def _request_from_vector(vector: dict[str, object]) -> TextMatchRequest:
    options = cast(dict[str, bool], vector["options"])
    return TextMatchRequest(
        text=cast(str, vector["text"]),
        query=cast(str, vector["query"]),
        profile=TextMatchProfile(cast(str, vector["profile"])),
        options=SearchOptions(
            match_case=options["match_case"],
            whole_word=options["whole_word"],
        ),
    )


class TextMatcherApplicationHandoffTests(unittest.TestCase):
    _temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    controller: EditorController = cast(
        EditorController,
        cast(object, None),
    )
    composition: CapabilityHostComposition = cast(
        CapabilityHostComposition,
        cast(object, None),
    )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-text-matcher-handoff-",
        )
        repository = ResourceRepository(Path(self._temporary.name))
        controller, composition = _compose_editor_controller(repository)
        self.controller = controller
        self.composition = composition

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_production_composition_exposes_the_exact_host_handoff(self) -> None:
        handoff = self.controller.text_matcher_handoff()

        self.assertIs(handoff, self.composition.host.matcher_snapshot())
        self.assertIs(handoff.display.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(handoff.display.supported_profiles, ())
        self.assertIsNone(handoff.matcher)

    def test_basic_handoff_executes_core_contiguous_profile_only(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        handoff = self.controller.text_matcher_handoff()
        matcher = handoff.matcher

        self.assertIs(handoff.display.state, TextMatcherState.BASIC_VALIDATED)
        self.assertIsNotNone(matcher)
        assert matcher is not None
        outcome = matcher.match(
            TextMatchRequest(
                text="Straße STRASSE",
                query="strasse",
                profile=TextMatchProfile.BASIC_CONTIGUOUS,
                options=SearchOptions(match_case=False, whole_word=False),
            )
        )
        self.assertIsInstance(outcome, TextMatchSuccess)
        assert isinstance(outcome, TextMatchSuccess)
        self.assertEqual(
            outcome.hits,
            (
                SearchHit(start_index=0, end_index=6),
                SearchHit(start_index=7, end_index=14),
            ),
        )
        advanced = matcher.match(
            TextMatchRequest(
                text="cat catalog",
                query="cat",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                options=SearchOptions(match_case=False, whole_word=True),
            )
        )
        self.assertIsInstance(advanced, TextMatchRejected)

    def test_text_v1_handoff_replays_all_shared_match_vectors(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        handoff = self.controller.text_matcher_handoff()
        matcher = handoff.matcher
        fixture = cast(
            dict[str, Any],
            json.loads(_SHARED_VECTORS_PATH.read_text(encoding="utf-8")),
        )

        self.assertIs(handoff.display.state, TextMatcherState.TEXT_V1_VALIDATED)
        self.assertIsNotNone(matcher)
        assert matcher is not None
        vector_ids = {
            cast(str, vector["id"])
            for vector in cast(list[dict[str, object]], fixture["vectors"])
        }
        self.assertTrue(
            {
                "sharp-s-expansion-original-offsets",
                "whole-word-rejects-digit-adjacency",
                "whole-word-rejects-underscore-adjacency",
                "whole-word-accepts-punctuation-boundaries",
                "pure-cjk-whole-word-tailors-to-contiguous",
                "pure-cjk-contiguous-reference",
            }.issubset(vector_ids)
        )
        for vector in cast(list[dict[str, object]], fixture["vectors"]):
            with self.subTest(vector=vector["id"]):
                outcome = matcher.match(_request_from_vector(vector))
                self.assertIsInstance(outcome, TextMatchSuccess)
                assert isinstance(outcome, TextMatchSuccess)
                expected = tuple(
                    SearchHit(start_index=start, end_index=end)
                    for start, end in cast(list[list[int]], vector["hits"])
                )
                self.assertEqual(outcome.hits, expected)

    def test_removed_core_matcher_stays_unavailable_without_fallback(self) -> None:
        with patch("capability_host.build_validated_matcher_v1", None):
            published = self.composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        handoff = self.controller.text_matcher_handoff()
        self.assertIs(handoff, published)
        self.assertIs(handoff.display.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(handoff.display.supported_profiles, ())
        self.assertEqual(
            handoff.display.safe_reason,
            "MATCHER.VALIDATION_UNAVAILABLE",
        )
        self.assertIsNone(handoff.matcher)

    def test_uncomposed_controller_has_no_fallback_matcher(self) -> None:
        repository = ResourceRepository(Path(self._temporary.name) / "bare")
        controller = EditorController(repository)

        with self.assertRaisesRegex(
            EditorControllerError,
            "^MATCHER\\.HANDOFF_UNAVAILABLE$",
        ):
            controller.text_matcher_handoff()

    def test_handoff_methods_only_delegate_the_existing_core_port(self) -> None:
        controller_source = inspect.getsource(
            EditorController.text_matcher_handoff
        )
        adapter_source = inspect.getsource(
            type(cast(Any, self.controller)._tm_adapter)
            ._text_matcher_handoff_for_controller
        )
        combined = controller_source + adapter_source

        self.assertNotIn("casefold", combined)
        self.assertNotIn("whole_word", combined)
        self.assertNotIn("CJK", combined)
        self.assertNotIn("TextMatcherV1", combined)
        self.assertEqual(combined.count("matcher_snapshot"), 1)


class LegacyTrieHandoffCompatibilityTests(unittest.TestCase):
    def test_legacy_trie_remains_case_sensitive_contiguous_and_long_first(
        self,
    ) -> None:
        engine = GlossaryEngine()
        engine.add_term("cat", "猫", "legacy.csv")
        engine.add_term("catalog", "目录", "legacy.csv")

        self.assertEqual(engine.extract_terms("Catalog"), [])
        hits = engine.extract_terms("catalog cat")
        self.assertEqual(
            tuple(
                (hit.source_term, hit.start_index, hit.end_index)
                for hit in hits
            ),
            (
                ("catalog", 0, 7),
                ("cat", 0, 3),
                ("cat", 8, 11),
            ),
        )


if __name__ == "__main__":
    unittest.main()
