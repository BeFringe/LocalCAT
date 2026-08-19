"""Fresh current-source acceptance journeys for Qt Requirement 3."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import ClassVar, cast, override
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from capability_host import CapabilityHostComposition
from editor_contracts import (
    ProjectSearchRequest,
    SearchField,
    SearchOptions,
    SegmentTranslationStatus,
    TextMatcherDisplayState,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_REAL_PROJECT_PATH = _REPOSITORY_ROOT / "po" / "卷二_引.json"


def _write_project(path: Path) -> None:
    _ = path.write_text(
        json.dumps(
            {
                "name": "Requirement 3 acceptance",
                "source_locale": "en-US",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "seg-a",
                        "source": "Alpha CAT cat 中文词",
                        "target": "draft CAT cat",
                        "speaker": "Guide cat",
                        "confirmed": False,
                    },
                    {
                        "id": "seg-b",
                        "source": "catapult cat",
                        "target": "中文词条 中文词",
                        "speaker": "CAT",
                        "confirmed": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class QtProjectSearchAcceptanceTests(unittest.TestCase):
    app: ClassVar[QApplication]
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str], cast(object, None)
    )
    controller: EditorController = cast(EditorController, cast(object, None))
    composition: CapabilityHostComposition = cast(
        CapabilityHostComposition, cast(object, None)
    )
    project_path: Path = cast(Path, cast(object, None))
    window: QtEditorWindow = cast(QtEditorWindow, cast(object, None))

    @classmethod
    @override
    def setUpClass(cls) -> None:
        existing = QApplication.instance()
        cls.app = existing if isinstance(existing, QApplication) else QApplication([])

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-q1-acceptance-"
        )
        root = Path(self.temporary.name)
        self.controller, self.composition = _compose_editor_controller(
            ResourceRepository(root / "app-data")
        )
        self.project_path = root / "acceptance.json"
        _write_project(self.project_path)

    @override
    def tearDown(self) -> None:
        window = getattr(self, "window", None)
        if isinstance(window, QtEditorWindow):
            window._confirm_unsaved = lambda: True
            window.close()
            self._events()
        self.temporary.cleanup()

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _validate_basic(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def _validate_text_v1(self) -> None:
        _ = self.composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def _open_window(
        self,
        project_path: Path | None = None,
    ) -> QtEditorWindow:
        self.controller.open_project(project_path or self.project_path)
        self.window = QtEditorWindow(self.controller)
        self.window.show()
        self._events()
        return self.window

    @staticmethod
    def _request(
        query: str,
        *,
        fields: tuple[SearchField, ...] = (
            SearchField.SOURCE,
            SearchField.TARGET,
            SearchField.SPEAKER,
        ),
        match_case: bool = False,
        whole_word: bool = False,
    ) -> ProjectSearchRequest:
        return ProjectSearchRequest(
            query=query,
            fields=fields,
            options=SearchOptions(
                match_case=match_case,
                whole_word=whole_word,
            ),
        )

    def test_basic_real_composition_keeps_offsets_order_and_unsaved_navigation(
        self,
    ) -> None:
        self._validate_basic()
        window = self._open_window()
        window.target_editor.setPlainText("unsaved CAT cat")
        window.project_search_input.setText("cat")

        QTest.mouseClick(
            window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()

        report = window.current_project_search_report
        assert report is not None
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
                    6,
                    9,
                    "Alpha CAT cat 中文词",
                ),
                (
                    "seg-a",
                    0,
                    SearchField.SOURCE,
                    10,
                    13,
                    "Alpha CAT cat 中文词",
                ),
                (
                    "seg-a",
                    0,
                    SearchField.TARGET,
                    8,
                    11,
                    "unsaved CAT cat",
                ),
                (
                    "seg-a",
                    0,
                    SearchField.TARGET,
                    12,
                    15,
                    "unsaved CAT cat",
                ),
                (
                    "seg-a",
                    0,
                    SearchField.SPEAKER,
                    6,
                    9,
                    "Guide cat",
                ),
                (
                    "seg-b",
                    1,
                    SearchField.SOURCE,
                    0,
                    3,
                    "catapult cat",
                ),
                (
                    "seg-b",
                    1,
                    SearchField.SOURCE,
                    9,
                    12,
                    "catapult cat",
                ),
                (
                    "seg-b",
                    1,
                    SearchField.SPEAKER,
                    0,
                    3,
                    "CAT",
                ),
            ),
        )
        self.assertIn("共 8 个结果", window.project_search_result.text())
        self.assertIn("SOURCE", window.project_search_result.text())
        self.assertIn("Alpha CAT cat", window.project_search_preview.text())

        for _ in range(5):
            QTest.mouseClick(
                window.project_search_next,
                Qt.MouseButton.LeftButton,
            )
        self._events()
        self.assertEqual(self.controller.current_index, 1)
        self.assertIn("第 6 个", window.project_search_result.text())
        self.assertEqual(
            self.controller.project.segments[0].target,
            "unsaved CAT cat",
        )
        self.assertTrue(self.controller.dirty)

        QTest.mouseClick(
            window.project_search_previous,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(self.controller.current_index, 0)
        self.assertEqual(
            self.controller.current_segment.target,
            "unsaved CAT cat",
        )

    def test_text_v1_real_core_honours_case_word_and_pure_cjk_profiles(
        self,
    ) -> None:
        self._validate_text_v1()
        self.controller.open_project(self.project_path)

        case_sensitive = self.controller.search_project(
            self._request("cat", match_case=True)
        )
        self.assertEqual(
            tuple(
                (hit.segment_id, hit.field, hit.start_index, hit.end_index)
                for hit in case_sensitive.hits
            ),
            (
                ("seg-a", SearchField.SOURCE, 10, 13),
                ("seg-a", SearchField.TARGET, 10, 13),
                ("seg-a", SearchField.SPEAKER, 6, 9),
                ("seg-b", SearchField.SOURCE, 0, 3),
                ("seg-b", SearchField.SOURCE, 9, 12),
            ),
        )

        whole_word = self.controller.search_project(
            self._request("cat", whole_word=True)
        )
        self.assertEqual(
            tuple(
                (hit.segment_id, hit.field, hit.start_index, hit.end_index)
                for hit in whole_word.hits
            ),
            (
                ("seg-a", SearchField.SOURCE, 6, 9),
                ("seg-a", SearchField.SOURCE, 10, 13),
                ("seg-a", SearchField.TARGET, 6, 9),
                ("seg-a", SearchField.TARGET, 10, 13),
                ("seg-a", SearchField.SPEAKER, 6, 9),
                ("seg-b", SearchField.SOURCE, 9, 12),
                ("seg-b", SearchField.SPEAKER, 0, 3),
            ),
        )

        cjk_fields = (SearchField.SOURCE, SearchField.TARGET)
        cjk_plain = self.controller.search_project(
            self._request("中文词", fields=cjk_fields)
        )
        cjk_whole = self.controller.search_project(
            self._request("中文词", fields=cjk_fields, whole_word=True)
        )
        self.assertEqual(cjk_whole.hits, cjk_plain.hits)
        self.assertEqual(
            tuple(
                (hit.segment_id, hit.field, hit.start_index, hit.end_index)
                for hit in cjk_whole.hits
            ),
            (
                ("seg-a", SearchField.SOURCE, 14, 17),
                ("seg-b", SearchField.TARGET, 0, 3),
                ("seg-b", SearchField.TARGET, 5, 8),
            ),
        )

    def test_empty_no_result_and_stale_hit_keep_the_current_project(self) -> None:
        self._validate_basic()
        window = self._open_window()
        self.controller.go_to(1)
        window._render_current_segment()
        before_project = self.controller.project
        before_index = self.controller.current_index

        window.project_search_input.setText("   ")
        QTest.keyClick(window.project_search_input, Qt.Key.Key_Return)
        self._events()
        self.assertIn("有效关键词", window.project_search_result.text())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

        window.project_search_input.setText("absent-token")
        QTest.keyClick(window.project_search_input, Qt.Key.Key_Return)
        self._events()
        self.assertIn("没有找到", window.project_search_result.text())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)

        window.project_search_input.setText("cat")
        QTest.keyClick(window.project_search_input, Qt.Key.Key_Return)
        self._events()
        self.assertIsNotNone(window.current_project_search_report)
        index_before_stale_navigation = self.controller.current_index
        self._validate_text_v1()
        QTest.mouseClick(
            window.project_search_next,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertIsNone(window.current_project_search_report)
        self.assertIn("已过期", window.project_search_result.text())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(
            self.controller.current_index,
            index_before_stale_navigation,
        )

    def test_unvalidated_and_foreign_handoffs_reject_execution_without_fallback(
        self,
    ) -> None:
        self.controller.open_project(self.project_path)
        before_project = self.controller.project
        before_index = self.controller.current_index
        request = self._request("cat")

        with self.assertRaisesRegex(
            EditorControllerError,
            "^MATCHER\\.VALIDATION_UNAVAILABLE$",
        ):
            self.controller.search_project(request)
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertIsNone(self.controller.current_project_search_report)

        self._validate_basic()
        with patch.object(
            self.controller,
            "text_matcher_handoff",
            return_value=object(),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.HANDOFF_INVALID$",
        ):
            self.controller.search_project(request)
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertIsNone(self.controller.current_project_search_report)

    def test_foreign_handoff_refresh_fails_closed_without_qt_fallback(self) -> None:
        self._validate_basic()
        window = self._open_window()
        self.assertTrue(window.project_search_button.isEnabled())

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            return_value=object(),
        ):
            window.refresh_suggestions()
            self._events()

        self.assertFalse(window.project_search_button.isEnabled())
        self.assertIn(
            "PROJECT_SEARCH.HANDOFF_INVALID",
            window.project_search_capability.text(),
        )
        self.assertIsNone(window.current_project_search_report)

    def test_exact_display_programmer_type_error_is_not_laundered(self) -> None:
        self._validate_basic()
        window = self._open_window()

        with patch.object(
            TextMatcherDisplayState,
            "__post_init__",
            side_effect=TypeError("display programmer fault"),
        ), self.assertRaisesRegex(
            TypeError,
            "^display programmer fault$",
        ):
            window.refresh_suggestions()

    def test_exact_current_validator_value_error_is_not_laundered(self) -> None:
        self._validate_basic()
        _ = self._open_window()
        request = ProjectSearchRequest(
            query="cat",
            fields=(SearchField.SOURCE,),
            options=SearchOptions(match_case=False, whole_word=False),
        )

        with patch.object(
            TextMatcherDisplayState,
            "__post_init__",
            side_effect=ValueError("display programmer fault"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "^display programmer fault$",
            ):
                self.controller.project_search_matcher_display()
            with self.assertRaisesRegex(
                ValueError,
                "^display programmer fault$",
            ):
                self.controller.search_project(request)

    @unittest.skipUnless(
        _REAL_PROJECT_PATH.is_file(),
        "real 卷二_引.json acceptance project is not present",
    )
    def test_real_project_search_surface_amendment_journey(self) -> None:
        """Exercise Requirement 3.1–3.16 through the real Qt surface."""

        project_bytes = _REAL_PROJECT_PATH.read_bytes()
        self._validate_basic()
        window = self._open_window(_REAL_PROJECT_PATH)

        self.assertFalse(window.project_search_panel.isVisible())
        self.assertFalse(window.project_search_toggle.isChecked())
        window.target_editor.setFocus()
        QTest.keyClick(
            window.target_editor,
            Qt.Key.Key_F,
            Qt.KeyboardModifier.ControlModifier,
        )
        self._events()
        self.assertTrue(window.project_search_panel.isVisible())
        self.assertTrue(window.project_search_input.hasFocus())

        window.project_search_source.setChecked(False)
        window.project_search_target.setChecked(False)
        window.project_search_speaker.setChecked(True)
        unfilled_index = window.project_search_status.findData(
            SegmentTranslationStatus.UNFILLED.value
        )
        self.assertGreaterEqual(unfilled_index, 0)
        window.project_search_status.setCurrentIndex(unfilled_index)
        window.project_search_input.setText("littleoldme")
        QTest.keyClick(window.project_search_input, Qt.Key.Key_Return)
        self._events()

        unfilled_report = window.current_project_search_report
        assert unfilled_report is not None
        self.assertEqual(unfilled_report.total, 99)
        self.assertTrue(
            all(hit.field is SearchField.SPEAKER for hit in unfilled_report.hits)
        )
        first_hit = unfilled_report.hits[0]
        self.assertEqual(
            (
                first_hit.segment_id,
                first_hit.segment_index,
                first_hit.start_index,
                first_hit.end_index,
                first_hit.preview,
            ),
            ("segment-1", 0, 0, 11, "littleoldme"),
        )
        self.assertEqual(self.controller.current_index, 0)
        self.assertIn("SPEAKER", window.project_search_result.text())
        self.assertIn("段落 1", window.project_search_result.text())
        self.assertFalse(window.project_search_match_case.isEnabled())
        self.assertFalse(window.project_search_whole_word.isEnabled())

        window.target_editor.setPlainText("draft translation")
        self._events()
        self.assertEqual(
            self.controller.project.segments[0].target,
            "draft translation",
        )
        self.assertIsNone(window.current_project_search_report)
        self.assertIn("已过期", window.project_search_result.text())
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.NO_ISSUED_REPORT$",
        ):
            self.controller.go_to_search_hit(first_hit)
        self.assertEqual(self.controller.current_index, 0)

        self._validate_text_v1()
        window.refresh_suggestions()
        self._events()
        self.assertTrue(window.project_search_match_case.isEnabled())
        self.assertTrue(window.project_search_whole_word.isEnabled())
        draft_index = window.project_search_status.findData(
            SegmentTranslationStatus.DRAFT.value
        )
        self.assertGreaterEqual(draft_index, 0)
        window.project_search_status.setCurrentIndex(draft_index)
        QTest.mouseClick(
            window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()

        draft_report = window.current_project_search_report
        assert draft_report is not None
        self.assertEqual(draft_report.total, 1)
        self.assertEqual(draft_report.hits[0].segment_id, "segment-1")
        draft_hit = draft_report.hits[0]
        confirmed = self.controller.confirm_current()
        self.assertTrue(confirmed.write_report.succeeded)
        self.assertTrue(self.controller.project.segments[0].confirmed)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "draft translation",
        )
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.STALE_PROJECT$",
        ):
            self.controller.go_to_search_hit(draft_hit)

        translated_index = window.project_search_status.findData(
            SegmentTranslationStatus.TRANSLATED.value
        )
        self.assertGreaterEqual(translated_index, 0)
        window.project_search_status.setCurrentIndex(translated_index)
        QTest.mouseClick(
            window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        translated_report = window.current_project_search_report
        assert translated_report is not None
        self.assertEqual(translated_report.total, 1)
        self.assertEqual(translated_report.hits[0].segment_id, "segment-1")
        matcher_stale_hit = translated_report.hits[0]
        self._validate_basic()
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.STALE_MATCHER_GENERATION$",
        ):
            self.controller.go_to_search_hit(matcher_stale_hit)

        before_clear_project = self.controller.project
        before_clear_index = self.controller.current_index
        before_clear_dirty = self.controller.dirty
        QTest.mouseClick(
            window.project_search_clear,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(window.project_search_input.text(), "")
        self.assertIsNone(window.current_project_search_report)
        self.assertIsNone(self.controller.current_project_search_report)
        self.assertFalse(window.project_search_source.isChecked())
        self.assertFalse(window.project_search_target.isChecked())
        self.assertTrue(window.project_search_speaker.isChecked())
        self.assertEqual(
            window.project_search_status.currentData(),
            SegmentTranslationStatus.TRANSLATED.value,
        )
        self.assertTrue(window.project_search_panel.isVisible())
        self.assertIs(self.controller.project, before_clear_project)
        self.assertEqual(self.controller.current_index, before_clear_index)
        self.assertEqual(self.controller.dirty, before_clear_dirty)

        with patch.object(
            self.controller,
            "search_project",
            wraps=self.controller.search_project,
        ) as status_only:
            QTest.mouseClick(
                window.project_search_button,
                Qt.MouseButton.LeftButton,
            )
            self._events()
        status_only.assert_not_called()
        self.assertIn("有效关键词", window.project_search_result.text())
        self.assertEqual(_REAL_PROJECT_PATH.read_bytes(), project_bytes)

    def test_search_surface_amendment_does_not_claim_deferred_features(
        self,
    ) -> None:
        source = (_REPOSITORY_ROOT / "qt_editor_window.py").read_text(
            encoding="utf-8"
        )
        for deferred in (
            "projectSearchReplace",
            "replace_project_search",
            "Replace All",
            "Approved",
            "Revise",
        ):
            with self.subTest(deferred=deferred):
                self.assertNotIn(deferred, source)


if __name__ == "__main__":
    unittest.main()
