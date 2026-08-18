from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

import editor_controller as editor_controller_module
from capability_host import CapabilityHostComposition, MatcherHandoffSnapshot
from editor_contracts import (
    ProjectSearchHit,
    ProjectSearchReport,
    ProjectSearchRequest,
    ProjectToolCapability,
    SearchField,
    TextMatcherDisplayState,
)
from editor_controller import EditorController, EditorControllerError
from project_search import ProjectSearchError, ProjectSearchService
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from tm_contracts import (
    SearchOptions,
    TextMatchProfile,
    TextMatcherState,
)


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_BASIC_OPTIONS = SearchOptions(match_case=False, whole_word=False)


def _request(
    query: str = "needle",
    *,
    options: SearchOptions = _BASIC_OPTIONS,
) -> ProjectSearchRequest:
    return ProjectSearchRequest(
        query=query,
        fields=(
            SearchField.SOURCE,
            SearchField.TARGET,
            SearchField.SPEAKER,
        ),
        options=options,
    )


def _write_project(path: Path) -> None:
    _ = path.write_text(
        json.dumps(
            {
                "name": "Search controller",
                "segments": [
                    {
                        "id": "seg-a",
                        "source": "A needle in source",
                        "target": "draft before search",
                        "speaker": "Alice",
                        "confirmed": False,
                    },
                    {
                        "id": "seg-b",
                        "source": "Other source",
                        "target": "Needle in target",
                        "speaker": "Needle keeper",
                        "confirmed": True,
                    },
                    {
                        "id": "seg-c",
                        "source": "Final needle",
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class EditorControllerProjectSearchTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    controller: EditorController = cast(EditorController, cast(object, None))
    composition: CapabilityHostComposition = cast(
        CapabilityHostComposition,
        cast(object, None),
    )
    project_path: Path = cast(Path, cast(object, None))

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-controller-project-search-",
        )
        root = Path(self.temporary.name)
        repository = ResourceRepository(root / "app-data")
        self.controller, self.composition = _compose_editor_controller(repository)
        self.project_path = root / "project.JsOn"
        _write_project(self.project_path)

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _validate_basic(self) -> MatcherHandoffSnapshot:
        return self.composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def _validate_text_v1(self) -> MatcherHandoffSnapshot:
        return self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def test_basic_search_uses_one_handoff_and_navigates_report_order(self) -> None:
        handoff = self._validate_basic()
        self.controller.open_project(self.project_path)
        self.controller.update_target("unsaved needle target")
        before_epoch = self.controller.query_epoch

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            wraps=self.controller.text_matcher_handoff,
        ) as capture:
            report = self.controller.search_project(_request())

        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.controller.current_index, 0)
        self.assertTrue(self.controller.dirty)
        self.assertEqual(self.controller.query_epoch, before_epoch)
        self.assertIs(report.capability.state, TextMatcherState.BASIC_VALIDATED)
        self.assertEqual(report.capability, handoff.display)
        self.assertEqual(
            tuple(
                (hit.segment_id, hit.segment_index, hit.field)
                for hit in report.hits
            ),
            (
                ("seg-a", 0, SearchField.SOURCE),
                ("seg-a", 0, SearchField.TARGET),
                ("seg-b", 1, SearchField.TARGET),
                ("seg-b", 1, SearchField.SPEAKER),
                ("seg-c", 2, SearchField.SOURCE),
            ),
        )

        self.controller.go_to_search_hit(report.hits[2])
        self.assertEqual(self.controller.current_index, 1)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "unsaved needle target",
        )
        self.controller.go_to_search_hit(report.hits[0])
        self.assertEqual(self.controller.current_index, 0)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "unsaved needle target",
        )

    def test_basic_gate_rejects_advanced_until_text_v1_then_passes_options(
        self,
    ) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        advanced = _request(
            "needle",
            options=SearchOptions(match_case=True, whole_word=True),
        )
        before_project = self.controller.project
        before_index = self.controller.current_index
        before_epoch = self.controller.query_epoch

        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.ADVANCED_OPTIONS_UNAVAILABLE$",
        ):
            self.controller.search_project(advanced)

        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertEqual(self.controller.query_epoch, before_epoch)
        self.assertIsNone(self.controller.current_project_search_report)

        handoff = self._validate_text_v1()
        report = self.controller.search_project(advanced)
        self.assertIs(
            report.capability.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )
        self.assertEqual(report.capability, handoff.display)

    def test_single_json_gate_precedes_handoff_and_preserves_session(self) -> None:
        self._validate_basic()
        text_path = self.project_path.with_suffix(".txt")
        text_path.write_text("needle\n", encoding="utf-8")
        self.controller.open_project(text_path)
        before_project = self.controller.project
        before_index = self.controller.current_index
        before_epoch = self.controller.query_epoch

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            wraps=self.controller.text_matcher_handoff,
        ) as capture, self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_TOOLS\\.JSON_REQUIRED$",
        ):
            self.controller.search_project(_request())

        capture.assert_not_called()
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertEqual(self.controller.query_epoch, before_epoch)

        foreign_gate = ProjectToolCapability(
            project_session_id="foreign-session",
            single_json_tools_available=True,
            project_kind="json",
            unavailable_reason=None,
        )
        with patch.object(
            self.controller,
            "project_tool_capability",
            return_value=foreign_gate,
        ), patch.object(
            self.controller,
            "text_matcher_handoff",
            wraps=self.controller.text_matcher_handoff,
        ) as foreign_capture, self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.PROJECT_GATE_INVALID$",
        ):
            self.controller.search_project(_request())
        foreign_capture.assert_not_called()

    def test_unavailable_foreign_or_tampered_handoff_fails_closed(self) -> None:
        valid = self._validate_basic()
        self.controller.open_project(self.project_path)
        before_project = self.controller.project
        before_index = self.controller.current_index

        unavailable = MatcherHandoffSnapshot(
            generation=valid.generation + 1,
            matcher=None,
            display=TextMatcherDisplayState(
                state=TextMatcherState.UNAVAILABLE,
                supported_profiles=(),
                safe_reason="MATCHER.VALIDATION_UNAVAILABLE",
            ),
        )
        malformed = object.__new__(MatcherHandoffSnapshot)
        object.__setattr__(malformed, "generation", valid.generation)
        object.__setattr__(malformed, "matcher", valid.matcher)
        object.__setattr__(
            malformed,
            "display",
            TextMatcherDisplayState(
                state=TextMatcherState.TEXT_V1_VALIDATED,
                supported_profiles=(
                    TextMatchProfile.LEGACY_COMPAT,
                    TextMatchProfile.BASIC_CONTIGUOUS,
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                ),
                safe_reason=None,
            ),
        )
        missing_matcher = object.__new__(MatcherHandoffSnapshot)
        object.__setattr__(
            missing_matcher,
            "generation",
            valid.generation,
        )
        object.__setattr__(missing_matcher, "matcher", None)
        object.__setattr__(missing_matcher, "display", valid.display)
        cases = (
            (unavailable, "PROJECT_SEARCH.HANDOFF_INVALID"),
            (object(), "PROJECT_SEARCH.HANDOFF_INVALID"),
            (malformed, "PROJECT_SEARCH.HANDOFF_INVALID"),
            (missing_matcher, "PROJECT_SEARCH.HANDOFF_INVALID"),
        )

        for returned, code in cases:
            with self.subTest(code=code), patch.object(
                self.controller,
                "text_matcher_handoff",
                return_value=returned,
            ), self.assertRaisesRegex(
                EditorControllerError,
                f"^{re.escape(code)}$",
            ):
                self.controller.search_project(_request())
            self.assertIs(self.controller.project, before_project)
            self.assertEqual(self.controller.current_index, before_index)
            self.assertIsNone(self.controller.current_project_search_report)

    def test_exact_handoff_with_foreign_matcher_identity_fails_closed(self) -> None:
        valid = self._validate_basic()
        self.controller.open_project(self.project_path)
        foreign_copy = MatcherHandoffSnapshot(
            generation=valid.generation,
            matcher=valid.matcher,
            display=valid.display,
        )
        tampered = object.__new__(MatcherHandoffSnapshot)
        object.__setattr__(tampered, "generation", valid.generation)
        object.__setattr__(tampered, "matcher", object())
        object.__setattr__(tampered, "display", valid.display)

        for candidate in (foreign_copy, tampered):
            with self.subTest(candidate=candidate), patch.object(
                self.controller,
                "text_matcher_handoff",
                return_value=candidate,
            ), self.assertRaisesRegex(
                EditorControllerError,
                "^PROJECT_SEARCH\\.HANDOFF_INVALID$",
            ):
                self.controller.search_project(_request())

            self.assertIsNone(self.controller.current_project_search_report)

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            side_effect=EditorControllerError("PRIVATE.PROOF.TOKEN"),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.HANDOFF_INVALID$",
        ) as raised:
            self.controller.search_project(_request())

        self.assertNotIn("PRIVATE.PROOF.TOKEN", str(raised.exception))
        self.assertIsNone(self.controller.current_project_search_report)

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            side_effect=EditorControllerError("MATCHER.HANDOFF_UNAVAILABLE"),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^MATCHER\\.HANDOFF_UNAVAILABLE$",
        ):
            self.controller.search_project(_request())

        ordinary = self.controller.search_project(_request())
        self.assertEqual(ordinary.capability, valid.display)

    def test_report_is_defensive_and_tampered_hit_is_not_membership(self) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        report = self.controller.search_project(_request())
        fresh = self.controller.current_project_search_report

        self.assertIs(type(fresh), ProjectSearchReport)
        assert fresh is not None
        self.assertEqual(fresh, report)
        self.assertIsNot(fresh, report)
        self.assertIsNot(fresh.hits[0], report.hits[0])
        object.__setattr__(report.hits[0], "segment_index", 2)
        before_project = self.controller.project
        before_index = self.controller.current_index

        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.HIT_NOT_ISSUED$",
        ):
            self.controller.go_to_search_hit(report.hits[0])

        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        unchanged = self.controller.current_project_search_report
        assert unchanged is not None
        self.assertEqual(unchanged.hits[0].segment_index, 0)

    def test_generation_session_and_project_field_changes_reject_stale_hits(
        self,
    ) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        first = self.controller.search_project(_request())
        before_project = self.controller.project
        before_index = self.controller.current_index
        _ = self._validate_text_v1()

        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.STALE_MATCHER_GENERATION$",
        ):
            self.controller.go_to_search_hit(first.hits[-1])
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

        second = self.controller.search_project(_request())
        self.controller.update_target("field replacement remains unsaved")
        replaced_project = self.controller.project
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.STALE_PROJECT$",
        ):
            self.controller.go_to_search_hit(second.hits[-1])
        self.assertIs(self.controller.project, replaced_project)
        self.assertEqual(self.controller.current_index, before_index)

        third = self.controller.search_project(_request("needle"))
        replacement_path = self.project_path.with_name("replacement.json")
        _write_project(replacement_path)
        self.controller.open_project(replacement_path)
        replacement_project = self.controller.project
        replacement_session = self.controller.project_session_id
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.NO_ISSUED_REPORT$",
        ):
            self.controller.go_to_search_hit(third.hits[-1])
        self.assertIs(self.controller.project, replacement_project)
        self.assertEqual(self.controller.project_session_id, replacement_session)
        self.assertEqual(self.controller.current_index, 0)

    def test_empty_no_result_and_service_rejection_never_move_or_mutate(self) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        self.controller.go_to(1)
        before_project = self.controller.project
        before_index = self.controller.current_index
        before_epoch = self.controller.query_epoch

        no_result = self.controller.search_project(_request("absent-token"))
        self.assertEqual(no_result.hits, ())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertEqual(self.controller.query_epoch, before_epoch)

        malformed = object.__new__(ProjectSearchRequest)
        object.__setattr__(malformed, "query", "   ")
        object.__setattr__(malformed, "fields", (SearchField.SOURCE,))
        object.__setattr__(malformed, "options", _BASIC_OPTIONS)
        with self.assertRaisesRegex(ValueError, "query must not be empty"):
            self.controller.search_project(malformed)
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

        with patch.object(
            ProjectSearchService,
            "search",
            side_effect=ProjectSearchError("MATCHER.CAPABILITY_CHANGED"),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^MATCHER\\.CAPABILITY_CHANGED$",
        ):
            self.controller.search_project(_request())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

    def test_replaced_service_report_cannot_publish_mismatched_display(self) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        before_project = self.controller.project
        mismatched = ProjectSearchReport(
            hits=(),
            capability=TextMatcherDisplayState(
                state=TextMatcherState.TEXT_V1_VALIDATED,
                supported_profiles=(
                    TextMatchProfile.LEGACY_COMPAT,
                    TextMatchProfile.BASIC_CONTIGUOUS,
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                ),
                safe_reason=None,
            ),
        )

        with patch.object(
            ProjectSearchService,
            "search",
            return_value=mismatched,
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.REPORT_MISMATCH$",
        ):
            self.controller.search_project(_request())

        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, 0)
        self.assertIsNone(self.controller.current_project_search_report)

    def test_replaced_service_hits_must_bind_to_current_project_fields(self) -> None:
        handoff = self._validate_basic()
        self.controller.open_project(self.project_path)
        source = self.controller.project.segments[0].source
        cases = (
            ProjectSearchHit(
                segment_id="foreign-segment",
                segment_index=0,
                field=SearchField.SOURCE,
                start_index=2,
                end_index=8,
                preview=source,
            ),
            ProjectSearchHit(
                segment_id="seg-a",
                segment_index=99,
                field=SearchField.SOURCE,
                start_index=2,
                end_index=8,
                preview=source,
            ),
            ProjectSearchHit(
                segment_id="seg-a",
                segment_index=0,
                field=SearchField.TARGET,
                start_index=2,
                end_index=8,
                preview=source,
            ),
        )

        for hit in cases:
            report = ProjectSearchReport(
                hits=(hit,),
                capability=handoff.display,
            )
            with self.subTest(hit=hit), patch.object(
                ProjectSearchService,
                "search",
                return_value=report,
            ), self.assertRaisesRegex(
                EditorControllerError,
                "^PROJECT_SEARCH\\.REPORT_INVALID$",
            ):
                self.controller.search_project(_request())
            self.assertIsNone(self.controller.current_project_search_report)

    def test_internal_programmer_type_errors_propagate_before_publication(
        self,
    ) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        before_project = self.controller.project
        before_index = self.controller.current_index

        cases = (
            (
                "project-gate-revalidation",
                patch.object(
                    ProjectToolCapability,
                    "__post_init__",
                    side_effect=TypeError("gate programmer fault"),
                ),
                "gate programmer fault",
            ),
            (
                "handoff-revalidation",
                patch.object(
                    MatcherHandoffSnapshot,
                    "__post_init__",
                    side_effect=TypeError("handoff programmer fault"),
                ),
                "handoff programmer fault",
            ),
            (
                "service-report-clone",
                patch(
                    "editor_controller._clone_project_search_report",
                    side_effect=TypeError("clone programmer fault"),
                ),
                "clone programmer fault",
            ),
        )
        for name, injected, message in cases:
            with self.subTest(case=name), injected, self.assertRaisesRegex(
                TypeError,
                f"^{re.escape(message)}$",
            ):
                self.controller.search_project(_request())
            self.assertIs(self.controller.project, before_project)
            self.assertEqual(self.controller.current_index, before_index)
            self.assertIsNone(self.controller.current_project_search_report)

        with patch.object(
            ProjectSearchService,
            "search",
            side_effect=AssertionError("service programmer assertion"),
        ), self.assertRaisesRegex(
            AssertionError,
            "^service programmer assertion$",
        ):
            self.controller.search_project(_request())
        self.assertIsNone(self.controller.current_project_search_report)

        report = self.controller.search_project(_request())
        with patch.object(
            type(report.hits[0]),
            "__post_init__",
            side_effect=TypeError("hit programmer fault"),
        ), self.assertRaisesRegex(
            TypeError,
            "^hit programmer fault$",
        ):
            self.controller.go_to_search_hit(report.hits[-1])
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

    def test_return_clone_programmer_error_cannot_partially_publish(self) -> None:
        self._validate_basic()
        self.controller.open_project(self.project_path)
        original_clone = editor_controller_module._clone_project_search_report
        clone_count = 0

        def fail_second_clone(
            report: ProjectSearchReport,
        ) -> ProjectSearchReport:
            nonlocal clone_count
            clone_count += 1
            if clone_count == 2:
                raise TypeError("return clone programmer fault")
            return original_clone(report)

        with patch(
            "editor_controller._clone_project_search_report",
            side_effect=fail_second_clone,
        ), self.assertRaisesRegex(
            TypeError,
            "^return clone programmer fault$",
        ):
            self.controller.search_project(_request())

        self.assertEqual(clone_count, 2)
        self.assertIsNone(self.controller.current_project_search_report)
        self.assertEqual(self.controller.current_index, 0)


if __name__ == "__main__":
    unittest.main()
