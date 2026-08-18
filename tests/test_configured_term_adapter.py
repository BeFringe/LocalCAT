from __future__ import annotations

from capability_host import CapabilityHostComposition, MatcherHandoffSnapshot
from datetime import datetime, timezone
from editor_controller import EditorController
from pathlib import Path
import tempfile
from typing import cast, override
import unittest

from configured_term_adapter import (
    ConfiguredTermAdapter,
    extract_terms_from_resources,
)
from editor_contracts import (
    TermMatchPolicy,
    TermRecord,
    TermRecordLocator,
    TermRowKind,
)
from glossary_engine import TermHighlighter
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_DIGEST = "1" * 64


def _record(
    row_ordinal: int,
    source: str,
    target: str,
    *,
    match_case: bool | None = None,
    whole_word: bool | None = None,
) -> TermRecord:
    configured = match_case is not None or whole_word is not None
    if configured and (match_case is None or whole_word is None):
        raise AssertionError("configured fixture flags must be complete")
    record_id = f"term-{row_ordinal}" if configured else None
    row_kind = TermRowKind.V1 if configured else TermRowKind.LEGACY
    return TermRecord(
        locator=TermRecordLocator(
            row_kind=row_kind,
            file_digest=_DIGEST,
            row_ordinal=row_ordinal,
            row_digest=f"{row_ordinal:064x}",
            record_id=record_id,
        ),
        record_id=record_id,
        source=source,
        target=target,
        policy=(
            TermMatchPolicy.CONFIGURED
            if configured
            else TermMatchPolicy.LEGACY
        ),
        match_case=match_case,
        whole_word=whole_word,
    )


class ConfiguredTermAdapterTests(unittest.TestCase):
    _temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    controller: EditorController = cast(EditorController, cast(object, None))
    composition: CapabilityHostComposition = cast(
        CapabilityHostComposition,
        cast(object, None),
    )

    @override
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-configured-terms-",
        )
        repository = ResourceRepository(Path(self._temporary.name))
        self.controller, self.composition = _compose_editor_controller(repository)

    @override
    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_pre_gate_v1_flags_do_not_change_legacy_trie_results(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        records = (
            _record(1, "cat", "猫"),
            _record(
                2,
                "Dog",
                "狗",
                match_case=False,
                whole_word=True,
            ),
        )
        adapter = ConfiguredTermAdapter(
            records,
            "mixed.csv",
            self.controller.text_matcher_handoff(),
        )

        hits = adapter.extract_terms("cat catalog dog Dogmatic")

        self.assertEqual(
            tuple(
                (hit.source_term, hit.target_term, hit.start_index, hit.end_index)
                for hit in hits
            ),
            (
                ("cat", "猫", 0, 3),
                ("cat", "猫", 4, 7),
                ("Dog", "狗", 16, 19),
            ),
        )

    def test_text_v1_applies_options_only_to_v1_cohort(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        records = (
            _record(1, "cat", "legacy"),
            _record(
                2,
                "Dog",
                "configured",
                match_case=False,
                whole_word=True,
            ),
            _record(
                3,
                "Bird",
                "configured-case-substring",
                match_case=True,
                whole_word=False,
            ),
        )
        adapter = ConfiguredTermAdapter(
            records,
            "mixed.csv",
            self.controller.text_matcher_handoff(),
        )

        hits = adapter.extract_terms(
            "Cat catalog dog Dogmatic bird Birdhouse"
        )

        self.assertEqual(
            tuple(
                (hit.source_term, hit.target_term, hit.start_index, hit.end_index)
                for hit in hits
            ),
            (
                ("cat", "legacy", 4, 7),
                ("dog", "configured", 12, 15),
                ("Bird", "configured-case-substring", 30, 34),
            ),
        )

    def test_text_v1_pure_cjk_whole_word_matches_contiguous_reference(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        handoff = self.controller.text_matcher_handoff()
        whole_word = ConfiguredTermAdapter(
            (
                _record(
                    1,
                    "猫",
                    "cat",
                    match_case=False,
                    whole_word=True,
                ),
            ),
            "whole-word.csv",
            handoff,
        )
        contiguous = ConfiguredTermAdapter(
            (
                _record(
                    1,
                    "猫",
                    "cat",
                    match_case=False,
                    whole_word=False,
                ),
            ),
            "contiguous.csv",
            handoff,
        )

        whole_word_hits = whole_word.extract_terms("小猫咪和猫")
        contiguous_hits = contiguous.extract_terms("小猫咪和猫")

        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in whole_word_hits),
            tuple((hit.start_index, hit.end_index) for hit in contiguous_hits),
        )
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in whole_word_hits),
            ((1, 2), (4, 5)),
        )

    def test_mixed_hits_use_start_length_and_record_order_before_selection(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        adapter = ConfiguredTermAdapter(
            (
                _record(1, "catalog", "legacy-long"),
                _record(2, "cat", "legacy-short"),
                _record(
                    3,
                    "Catalog",
                    "configured-long",
                    match_case=False,
                    whole_word=True,
                ),
            ),
            "mixed.csv",
            self.controller.text_matcher_handoff(),
        )

        hits = adapter.extract_terms("catalog cat")

        self.assertEqual(
            tuple(
                (hit.target_term, hit.start_index, hit.end_index)
                for hit in hits
            ),
            (
                ("legacy-long", 0, 7),
                ("configured-long", 0, 7),
                ("legacy-short", 0, 3),
                ("legacy-short", 8, 11),
            ),
        )
        self.assertEqual(
            TermHighlighter.highlight("catalog cat", hits),
            "[catalog|legacy-long] [cat|legacy-short]",
        )

    def test_resource_batch_globally_merges_interleaved_hits_and_ties(
        self,
    ) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        handoff = self.controller.text_matcher_handoff()
        first = ConfiguredTermAdapter(
            (
                _record(1, "catalog", "first-r1-long"),
                _record(2, "cat", "first-r2-short"),
                _record(
                    3,
                    "Catalog",
                    "first-r3-long-configured",
                    match_case=False,
                    whole_word=True,
                ),
            ),
            "first.csv",
            handoff,
        )
        second = ConfiguredTermAdapter(
            (
                _record(
                    1,
                    "Catalog",
                    "second-r1-long-configured",
                    match_case=False,
                    whole_word=True,
                ),
                _record(2, "cat", "second-r2-short"),
            ),
            "second.csv",
            handoff,
        )

        hits = extract_terms_from_resources(
            "catalog cat",
            (first, second),
        )

        self.assertEqual(
            tuple(
                (
                    hit.target_term,
                    hit.start_index,
                    hit.end_index,
                    hit.glossary_source,
                )
                for hit in hits
            ),
            (
                ("first-r1-long", 0, 7, "first.csv"),
                ("first-r3-long-configured", 0, 7, "first.csv"),
                ("second-r1-long-configured", 0, 7, "second.csv"),
                ("first-r2-short", 0, 3, "first.csv"),
                ("second-r2-short", 0, 3, "second.csv"),
                ("first-r2-short", 8, 11, "first.csv"),
                ("second-r2-short", 8, 11, "second.csv"),
            ),
        )
        self.assertEqual(
            TermHighlighter.highlight("catalog cat", hits),
            "[catalog|first-r1-long] [cat|first-r2-short]",
        )

    def test_unavailable_closes_configured_path_and_foreign_handoff_is_rejected(
        self,
    ) -> None:
        records = (
            _record(
                1,
                "cat",
                "猫",
                match_case=False,
                whole_word=True,
            ),
        )
        adapter = ConfiguredTermAdapter(
            records,
            "mixed.csv",
            self.controller.text_matcher_handoff(),
        )

        self.assertEqual(
            tuple(
                (hit.source_term, hit.start_index, hit.end_index)
                for hit in adapter.extract_terms("cat Catalog")
            ),
            (("cat", 0, 3),),
        )
        with self.assertRaisesRegex(TypeError, "handoff"):
            _ = ConfiguredTermAdapter(
                records,
                "mixed.csv",
                cast(MatcherHandoffSnapshot, object()),
            )


if __name__ == "__main__":
    unittest.main()
