from __future__ import annotations

import dataclasses
import unittest

from editor_contracts import (
    ProjectSearchHit,
    ProjectSearchReport,
    ProjectSearchRequest,
    ProjectToolCapability,
    SearchField,
    SegmentTranslationStatus,
    TextMatcherDisplayState,
)
from tm_contracts import (
    SearchOptions,
    TextMatchProfile,
    TextMatcherState,
)


def _display(state: TextMatcherState) -> TextMatcherDisplayState:
    if state is TextMatcherState.UNAVAILABLE:
        return TextMatcherDisplayState(
            state=state,
            supported_profiles=(),
            safe_reason="MATCHER.VALIDATION_UNAVAILABLE",
        )
    profiles = (
        TextMatchProfile.LEGACY_COMPAT,
        TextMatchProfile.BASIC_CONTIGUOUS,
    )
    if state is TextMatcherState.TEXT_V1_VALIDATED:
        profiles += (TextMatchProfile.CONFIGURABLE_TEXT_V1,)
    return TextMatcherDisplayState(
        state=state,
        supported_profiles=profiles,
        safe_reason=None,
    )


def _hit(
    *,
    segment_id: str = "seg-1",
    segment_index: int = 0,
    field: SearchField = SearchField.SOURCE,
    start_index: int = 1,
    end_index: int = 4,
    preview: str = "A cat sat here.",
) -> ProjectSearchHit:
    return ProjectSearchHit(
        segment_id=segment_id,
        segment_index=segment_index,
        field=field,
        start_index=start_index,
        end_index=end_index,
        preview=preview,
    )


class ProjectToolCapabilityContractTests(unittest.TestCase):
    def test_available_and_unavailable_single_json_tool_states_are_explicit(self) -> None:
        available = ProjectToolCapability(
            project_session_id="session-json",
            single_json_tools_available=True,
            project_kind="json",
            unavailable_reason=None,
        )
        unavailable = ProjectToolCapability(
            project_session_id="session-txt",
            single_json_tools_available=False,
            project_kind="txt",
            unavailable_reason="PROJECT_TOOLS.JSON_REQUIRED",
        )
        no_project = ProjectToolCapability(
            project_session_id=None,
            single_json_tools_available=False,
            project_kind="none",
            unavailable_reason="PROJECT_TOOLS.NO_PROJECT",
        )

        self.assertTrue(available.single_json_tools_available)
        self.assertFalse(unavailable.single_json_tools_available)
        self.assertIsNone(no_project.project_session_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(available, "project_kind", "txt")

    def test_project_tool_capability_rejects_incoherent_combinations(self) -> None:
        invalid_calls = (
            lambda: ProjectToolCapability(
                project_session_id=1,  # pyright: ignore[reportArgumentType]
                single_json_tools_available=True,
                project_kind="json",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id="",
                single_json_tools_available=True,
                project_kind="json",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id="session",
                single_json_tools_available=1,  # pyright: ignore[reportArgumentType]
                project_kind="json",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id="session",
                single_json_tools_available=True,
                project_kind="txt",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id=None,
                single_json_tools_available=True,
                project_kind="json",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id="session",
                single_json_tools_available=True,
                project_kind="json",
                unavailable_reason="PROJECT_TOOLS.JSON_REQUIRED",
            ),
            lambda: ProjectToolCapability(
                project_session_id="session",
                single_json_tools_available=False,
                project_kind="txt",
                unavailable_reason=None,
            ),
            lambda: ProjectToolCapability(
                project_session_id="session",
                single_json_tools_available=False,
                project_kind="txt",
                unavailable_reason="contains body text",
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                _ = invalid_call()


class ProjectSearchContractTests(unittest.TestCase):
    def test_request_hit_and_report_preserve_frozen_search_identity(self) -> None:
        request = ProjectSearchRequest(
            query="cat",
            fields=(
                SearchField.SOURCE,
                SearchField.TARGET,
                SearchField.SPEAKER,
            ),
            options=SearchOptions(match_case=False, whole_word=False),
            status=SegmentTranslationStatus.DRAFT,
        )
        hits = (
            _hit(),
            _hit(
                field=SearchField.TARGET,
                start_index=3,
                end_index=6,
                preview="一只 cat 坐在这里。",
            ),
            _hit(
                segment_id="seg-2",
                segment_index=1,
                field=SearchField.SPEAKER,
                start_index=0,
                end_index=3,
                preview="Cat",
            ),
        )
        report = ProjectSearchReport(
            hits=hits,
            capability=_display(TextMatcherState.BASIC_VALIDATED),
        )

        self.assertEqual(request.query, "cat")
        self.assertEqual(request.fields[2], SearchField.SPEAKER)
        self.assertIs(request.status, SegmentTranslationStatus.DRAFT)
        self.assertIs(type(request.options), SearchOptions)
        self.assertEqual(report.hits, hits)
        self.assertEqual(report.total, 3)
        self.assertIs(
            report.capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(report, "hits", ())

    def test_request_rejects_invalid_query_field_tuple_or_options(self) -> None:
        valid_options = SearchOptions(match_case=False, whole_word=False)
        invalid_calls = (
            lambda: ProjectSearchRequest(
                query=1,  # pyright: ignore[reportArgumentType]
                fields=(SearchField.SOURCE,),
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="   ",
                fields=(SearchField.SOURCE,),
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=[],  # pyright: ignore[reportArgumentType]
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(),
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SOURCE, SearchField.SOURCE),
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SPEAKER, SearchField.SOURCE),
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=("source",),  # pyright: ignore[reportArgumentType]
                options=valid_options,
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SOURCE,),
                options=object(),  # pyright: ignore[reportArgumentType]
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SOURCE,),
                options=valid_options,
                status="draft",  # pyright: ignore[reportArgumentType]
            ),
            lambda: ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SOURCE,),
                options=valid_options,
                status=SearchField.TARGET,  # pyright: ignore[reportArgumentType]
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                _ = invalid_call()

    def test_status_filter_is_optional_and_cannot_replace_a_text_hit(self) -> None:
        valid_options = SearchOptions(match_case=False, whole_word=False)

        unfiltered = ProjectSearchRequest(
            query="cat",
            fields=(SearchField.SOURCE,),
            options=valid_options,
        )
        filtered = tuple(
            ProjectSearchRequest(
                query="cat",
                fields=(SearchField.SOURCE,),
                options=valid_options,
                status=status,
            )
            for status in SegmentTranslationStatus
        )

        self.assertIsNone(unfiltered.status)
        self.assertEqual(
            tuple(request.status for request in filtered),
            (
                SegmentTranslationStatus.UNFILLED,
                SegmentTranslationStatus.DRAFT,
                SegmentTranslationStatus.TRANSLATED,
            ),
        )
        with self.assertRaises(TypeError):
            _ = ProjectSearchHit(
                segment_id="seg-1",
                segment_index=0,
                field=None,  # pyright: ignore[reportArgumentType]
                start_index=0,
                end_index=1,
                preview="",
            )

    def test_hit_rejects_invalid_exact_types_offsets_fields_or_preview(self) -> None:
        invalid_calls = (
            lambda: _hit(segment_id=""),
            lambda: _hit(segment_id=1),  # pyright: ignore[reportArgumentType]
            lambda: _hit(segment_index=True),  # type: ignore[arg-type]
            lambda: _hit(segment_index=-1),
            lambda: _hit(field="source"),  # pyright: ignore[reportArgumentType]
            lambda: _hit(start_index=True),  # type: ignore[arg-type]
            lambda: _hit(start_index=-1),
            lambda: _hit(start_index=3, end_index=3),
            lambda: _hit(start_index=4, end_index=3),
            lambda: _hit(start_index=1, end_index=16, preview="too short"),
            lambda: _hit(preview=""),
            lambda: _hit(preview=1),  # pyright: ignore[reportArgumentType]
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                _ = invalid_call()

    def test_report_rejects_invalid_tuple_order_identity_or_capability(self) -> None:
        first = _hit()
        second = _hit(
            segment_id="seg-2",
            segment_index=1,
            field=SearchField.TARGET,
            start_index=0,
            end_index=2,
            preview="ok",
        )
        basic = _display(TextMatcherState.BASIC_VALIDATED)
        unavailable = _display(TextMatcherState.UNAVAILABLE)
        invalid_calls = (
            lambda: ProjectSearchReport(
                hits=[first],  # pyright: ignore[reportArgumentType]
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(object(),),  # pyright: ignore[reportArgumentType]
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(first, first),
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(second, first),
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(first, _hit(segment_id="seg-1", segment_index=1)),
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(first, _hit(segment_id="seg-2", segment_index=0)),
                capability=basic,
            ),
            lambda: ProjectSearchReport(
                hits=(first,),
                capability=object(),  # pyright: ignore[reportArgumentType]
            ),
            lambda: ProjectSearchReport(
                hits=(first,),
                capability=unavailable,
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                _ = invalid_call()

    def test_empty_report_preserves_each_handoff_state_without_forging_authority(
        self,
    ) -> None:
        for state in TextMatcherState:
            with self.subTest(state=state):
                report = ProjectSearchReport(hits=(), capability=_display(state))
                self.assertEqual(report.total, 0)
                self.assertIs(report.capability.state, state)


if __name__ == "__main__":
    _ = unittest.main()
