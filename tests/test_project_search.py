"""Domain tests for Core-owned project text matching orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

from capability_gated_text_matcher import CapabilityGatedTextMatcherV1
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ProjectSearchRequest,
    SearchField,
    SegmentTranslationStatus,
)
from matcher_validation import build_validated_matcher_v1
from project_search import ProjectSearchError, ProjectSearchService
from tm_contracts import (
    SearchOptions,
    TextMatchProfile,
    TextMatchRequest,
    TextMatcherState,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _validated_matcher(*, include_full: bool) -> CapabilityGatedTextMatcherV1:
    return build_validated_matcher_v1(
        repository_root=_REPOSITORY_ROOT,
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
        evaluated_at_utc=_EVALUATED_AT,
        include_full=include_full,
    )


class ProjectSearchServiceTests(unittest.TestCase):
    basic_matcher: CapabilityGatedTextMatcherV1 = cast(
        CapabilityGatedTextMatcherV1,
        cast(object, None),
    )
    full_matcher: CapabilityGatedTextMatcherV1 = cast(
        CapabilityGatedTextMatcherV1,
        cast(object, None),
    )

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.basic_matcher = _validated_matcher(include_full=False)
        cls.full_matcher = _validated_matcher(include_full=True)
        if (
            cls.basic_matcher.capability().state
            is not TextMatcherState.BASIC_VALIDATED
        ):
            raise AssertionError("real Core BASIC matcher fixture did not validate")
        if (
            cls.full_matcher.capability().state
            is not TextMatcherState.TEXT_V1_VALIDATED
        ):
            raise AssertionError("real Core TEXT_V1 matcher fixture did not validate")

    def test_basic_search_uses_real_core_offsets_and_stable_project_field_order(
        self,
    ) -> None:
        project = EditorProject(
            name="Offsets",
            segments=(
                EditorSegment(
                    id="seg-a",
                    source="Straße STRASSE",
                    target="STRASSE",
                    speaker="Straße keeper",
                    confirmed=True,
                ),
                EditorSegment(
                    id="seg-b",
                    source="Nothing here",
                    target="strasse",
                    speaker="",
                ),
            ),
        )
        request = ProjectSearchRequest(
            query="strasse",
            fields=(
                SearchField.SOURCE,
                SearchField.TARGET,
                SearchField.SPEAKER,
            ),
            options=SearchOptions(match_case=False, whole_word=False),
        )
        original_project = project

        report = ProjectSearchService(self.basic_matcher).search(project, request)

        self.assertIs(project, original_project)
        self.assertEqual(report.total, 5)
        self.assertEqual(
            tuple(
                (
                    hit.segment_id,
                    hit.segment_index,
                    hit.field,
                    hit.start_index,
                    hit.end_index,
                    hit.preview,
                )
                for hit in report.hits
            ),
            (
                (
                    "seg-a",
                    0,
                    SearchField.SOURCE,
                    0,
                    6,
                    "Straße STRASSE",
                ),
                (
                    "seg-a",
                    0,
                    SearchField.SOURCE,
                    7,
                    14,
                    "Straße STRASSE",
                ),
                ("seg-a", 0, SearchField.TARGET, 0, 7, "STRASSE"),
                (
                    "seg-a",
                    0,
                    SearchField.SPEAKER,
                    0,
                    6,
                    "Straße keeper",
                ),
                ("seg-b", 1, SearchField.TARGET, 0, 7, "strasse"),
            ),
        )
        self.assertIs(
            report.capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        self.assertEqual(project.segments[0].target, "STRASSE")
        self.assertTrue(project.segments[0].confirmed)

    def test_selected_fields_call_the_same_core_port_in_fixed_order(self) -> None:
        project = EditorProject(
            name="Call order",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="cat source",
                    target="cat target",
                    speaker="cat speaker",
                ),
                EditorSegment(
                    id="seg-2",
                    source="second cat",
                    target="other",
                    speaker="",
                ),
            ),
        )
        options = SearchOptions(match_case=False, whole_word=False)
        request = ProjectSearchRequest(
            query="cat",
            fields=(SearchField.SOURCE, SearchField.SPEAKER),
            options=options,
        )
        observed: list[tuple[CapabilityGatedTextMatcherV1, TextMatchRequest]] = []
        core_match = CapabilityGatedTextMatcherV1.match

        def observe_match(
            matcher: CapabilityGatedTextMatcherV1,
            match_request: TextMatchRequest,
        ):
            observed.append((matcher, match_request))
            return core_match(matcher, match_request)

        with patch.object(
            CapabilityGatedTextMatcherV1,
            "match",
            new=observe_match,
        ):
            report = ProjectSearchService(self.basic_matcher).search(
                project,
                request,
            )

        self.assertEqual(report.total, 3)
        self.assertEqual(
            tuple(match_request.text for _, match_request in observed),
            ("cat source", "cat speaker", "second cat", ""),
        )
        self.assertTrue(
            all(matcher is self.basic_matcher for matcher, _ in observed)
        )
        self.assertTrue(
            all(
                match_request.profile is TextMatchProfile.BASIC_CONTIGUOUS
                for _, match_request in observed
            )
        )
        self.assertTrue(
            all(match_request.options is options for _, match_request in observed)
        )

    def test_status_filter_is_derived_before_the_same_core_matcher_runs(
        self,
    ) -> None:
        project = EditorProject(
            name="Translation states",
            segments=(
                EditorSegment(
                    id="unfilled",
                    source="needle unfilled",
                    target="  \t",
                    speaker="needle unfilled speaker",
                    confirmed=False,
                ),
                EditorSegment(
                    id="draft",
                    source="needle draft",
                    target="needle draft target",
                    speaker="needle draft speaker",
                    confirmed=False,
                ),
                EditorSegment(
                    id="translated",
                    source="needle translated",
                    target="",
                    speaker="needle translated speaker",
                    confirmed=True,
                ),
            ),
        )
        core_match = CapabilityGatedTextMatcherV1.match

        for status, expected_texts, expected_hit_ids in (
            (
                SegmentTranslationStatus.UNFILLED,
                ("needle unfilled", "  \t", "needle unfilled speaker"),
                ("unfilled", "unfilled"),
            ),
            (
                SegmentTranslationStatus.DRAFT,
                (
                    "needle draft",
                    "needle draft target",
                    "needle draft speaker",
                ),
                ("draft", "draft", "draft"),
            ),
            (
                SegmentTranslationStatus.TRANSLATED,
                ("needle translated", "", "needle translated speaker"),
                ("translated", "translated"),
            ),
        ):
            with self.subTest(status=status):
                observed: list[
                    tuple[CapabilityGatedTextMatcherV1, TextMatchRequest]
                ] = []

                def observe_match(
                    matcher: CapabilityGatedTextMatcherV1,
                    match_request: TextMatchRequest,
                ):
                    observed.append((matcher, match_request))
                    return core_match(matcher, match_request)

                request = ProjectSearchRequest(
                    query="needle",
                    fields=(
                        SearchField.SOURCE,
                        SearchField.TARGET,
                        SearchField.SPEAKER,
                    ),
                    options=SearchOptions(
                        match_case=False,
                        whole_word=False,
                    ),
                    status=status,
                )
                with patch.object(
                    CapabilityGatedTextMatcherV1,
                    "match",
                    new=observe_match,
                ):
                    report = ProjectSearchService(self.basic_matcher).search(
                        project,
                        request,
                    )

                self.assertEqual(
                    tuple(match_request.text for _, match_request in observed),
                    expected_texts,
                )
                self.assertTrue(
                    all(matcher is self.basic_matcher for matcher, _ in observed)
                )
                self.assertEqual(
                    tuple(hit.segment_id for hit in report.hits),
                    expected_hit_ids,
                )

    def test_advanced_options_are_passed_unchanged_to_real_text_v1_matcher(
        self,
    ) -> None:
        project = EditorProject(
            name="CJK",
            segments=(
                EditorSegment(
                    id="cjk-1",
                    source="小猫咪猫",
                    target="",
                    speaker="",
                ),
            ),
        )
        options = SearchOptions(match_case=True, whole_word=True)
        request = ProjectSearchRequest(
            query="猫",
            fields=(SearchField.SOURCE,),
            options=options,
        )
        observed: list[TextMatchRequest] = []
        core_match = CapabilityGatedTextMatcherV1.match

        def observe_match(
            matcher: CapabilityGatedTextMatcherV1,
            match_request: TextMatchRequest,
        ):
            observed.append(match_request)
            return core_match(matcher, match_request)

        with patch.object(
            CapabilityGatedTextMatcherV1,
            "match",
            new=observe_match,
        ):
            report = ProjectSearchService(self.full_matcher).search(project, request)

        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in report.hits),
            ((1, 2), (3, 4)),
        )
        self.assertEqual(len(observed), 1)
        self.assertIs(
            observed[0].profile,
            TextMatchProfile.CONFIGURABLE_TEXT_V1,
        )
        self.assertIs(observed[0].options, options)
        self.assertIs(
            report.capability.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )

    def test_empty_query_and_no_result_do_not_modify_the_project(self) -> None:
        project = EditorProject(
            name="No result",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="Alpha",
                    target="Beta",
                    speaker="Gamma",
                    confirmed=True,
                ),
            ),
        )
        request = ProjectSearchRequest(
            query="missing",
            fields=(SearchField.SOURCE, SearchField.TARGET),
            options=SearchOptions(match_case=False, whole_word=False),
        )
        original_segments = project.segments

        report = ProjectSearchService(self.basic_matcher).search(project, request)

        self.assertEqual(report.hits, ())
        self.assertEqual(report.total, 0)
        self.assertIs(project.segments, original_segments)

        malformed = object.__new__(ProjectSearchRequest)
        object.__setattr__(malformed, "query", "   ")
        object.__setattr__(malformed, "fields", (SearchField.SOURCE,))
        object.__setattr__(
            malformed,
            "options",
            SearchOptions(match_case=False, whole_word=False),
        )
        with self.assertRaisesRegex(ValueError, "query must not be empty"):
            _ = ProjectSearchService(self.basic_matcher).search(
                project,
                malformed,
            )
        self.assertIs(project.segments, original_segments)

    def test_missing_or_core_rejected_matcher_fails_without_local_fallback(
        self,
    ) -> None:
        with self.assertRaises(ProjectSearchError) as missing:
            _ = ProjectSearchService(None)
        self.assertEqual(missing.exception.code, "MATCHER.PORT_UNAVAILABLE")

        with tempfile.TemporaryDirectory(
            prefix="localcat-unavailable-matcher-",
        ) as temporary:
            unavailable_matcher = build_validated_matcher_v1(
                repository_root=Path(temporary),
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
                include_full=False,
            )
        self.assertIs(
            unavailable_matcher.capability().state,
            TextMatcherState.UNAVAILABLE,
        )
        project = EditorProject(
            name="Rejected",
            segments=(EditorSegment(id="seg-1", source="cat"),),
        )
        request = ProjectSearchRequest(
            query="cat",
            fields=(SearchField.SOURCE,),
            options=SearchOptions(match_case=False, whole_word=False),
        )

        with self.assertRaises(ProjectSearchError) as rejected:
            _ = ProjectSearchService(unavailable_matcher).search(
                project,
                request,
            )

        self.assertEqual(rejected.exception.code, "MATCHER.CAPABILITY_UNAVAILABLE")
        self.assertEqual(project.segments[0].source, "cat")

    def test_service_source_contains_no_competing_matcher_semantics(self) -> None:
        source = inspect.getsource(ProjectSearchService)

        self.assertNotIn("casefold", source)
        self.assertNotIn(".lower(", source)
        self.assertNotIn("TextMatcherV1", source)
        self.assertNotIn("import re", source)
        self.assertNotIn("whole_word_boundary", source)


if __name__ == "__main__":
    _ = unittest.main()
