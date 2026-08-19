from __future__ import annotations

import ast
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
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from capability_host import CapabilityHostComposition
from editor_contracts import (
    ProjectSearchReport,
    ProjectSearchRequest,
    SearchField,
    SearchOptions as EditorSearchOptions,
    SegmentTranslationStatus,
    TextMatcherState as EditorTextMatcherState,
    WorkspaceMode,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tm_contracts import (
    SearchOptions as CoreSearchOptions,
    TextMatcherState,
    TextMatcherState as CoreTextMatcherState,
)


ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _write_project(path: Path) -> None:
    _ = path.write_text(
        json.dumps(
            {
                "name": "Qt project search",
                "source_locale": "en-US",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "seg-a",
                        "source": "Needle in source and 中文词",
                        "target": "draft before search",
                        "speaker": "Alice",
                        "confirmed": False,
                    },
                    {
                        "id": "seg-b",
                        "source": "Other source with 中文词条",
                        "target": "needle in target",
                        "speaker": "Needle keeper",
                        "confirmed": True,
                    },
                    {
                        "id": "seg-c",
                        "source": "Final NEEDLE",
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


class QtProjectSearchTests(unittest.TestCase):
    app: ClassVar[QApplication]
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
    window: QtEditorWindow = cast(QtEditorWindow, cast(object, None))

    @classmethod
    def setUpClass(cls) -> None:
        existing = QApplication.instance()
        cls.app = existing if isinstance(existing, QApplication) else QApplication([])

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-qt-project-search-",
        )
        root = Path(self.temporary.name)
        repository = ResourceRepository(root / "app-data")
        self.controller, self.composition = _compose_editor_controller(repository)
        self.project_path = root / "project.json"
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

    def _open_window(self, *, expand_search: bool = True) -> QtEditorWindow:
        self.controller.open_project(self.project_path)
        self.window = QtEditorWindow(self.controller)
        self.window.show()
        self._events()
        if expand_search:
            QTest.mouseClick(
                self.window.project_search_toggle,
                Qt.MouseButton.LeftButton,
            )
            self._events()
        return self.window

    def test_basic_search_renders_fields_preview_and_issued_navigation(self) -> None:
        self._validate_basic()
        window = self._open_window()

        self.assertTrue(window.project_search_source.isChecked())
        self.assertTrue(window.project_search_target.isChecked())
        self.assertTrue(window.project_search_speaker.isChecked())
        self.assertTrue(window.project_search_button.isEnabled())
        self.assertFalse(window.project_search_match_case.isEnabled())
        self.assertFalse(window.project_search_whole_word.isEnabled())
        self.assertIn("第二阶段", window.project_search_capability.text())

        window.target_editor.setPlainText("unsaved needle target")
        window.project_search_match_case.setChecked(True)
        window.project_search_whole_word.setChecked(True)
        window.project_search_input.setText("needle")
        with patch.object(
            self.controller,
            "search_project",
            wraps=self.controller.search_project,
        ) as search, patch.object(
            self.controller,
            "go_to_search_hit",
            wraps=self.controller.go_to_search_hit,
        ) as navigate:
            QTest.mouseClick(
                window.project_search_button,
                Qt.MouseButton.LeftButton,
            )
            self._events()

            request = cast(ProjectSearchRequest, search.call_args.args[0])
            self.assertFalse(request.options.match_case)
            self.assertFalse(request.options.whole_word)
            self.assertEqual(
                request.fields,
                (SearchField.SOURCE, SearchField.TARGET, SearchField.SPEAKER),
            )
            self.assertEqual(navigate.call_count, 1)
            report = cast(
                ProjectSearchReport,
                window.current_project_search_report,
            )
            self.assertIs(
                report.capability.state,
                TextMatcherState.BASIC_VALIDATED,
            )
            self.assertEqual(
                {hit.field for hit in report.hits},
                {SearchField.SOURCE, SearchField.TARGET, SearchField.SPEAKER},
            )
            self.assertIn(
                f"共 {report.total} 个结果",
                window.project_search_result.text(),
            )
            self.assertIn("SOURCE", window.project_search_result.text())
            self.assertIn("Needle in source", window.project_search_preview.text())
            self.assertFalse(window.project_search_previous.isEnabled())
            self.assertTrue(window.project_search_next.isEnabled())

            QTest.mouseClick(
                window.project_search_next,
                Qt.MouseButton.LeftButton,
            )
            QTest.mouseClick(
                window.project_search_next,
                Qt.MouseButton.LeftButton,
            )
            self._events()
            self.assertGreaterEqual(navigate.call_count, 3)

        self.assertEqual(self.controller.current_index, 1)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "unsaved needle target",
        )
        self.assertTrue(self.controller.dirty)

        while window.project_search_next.isEnabled():
            QTest.mouseClick(
                window.project_search_next,
                Qt.MouseButton.LeftButton,
            )
            self._events()
        self.assertIn("最后一个", window.project_search_next.toolTip())
        self.assertTrue(window.project_search_previous.isEnabled())

    def test_empty_and_no_result_feedback_never_move_the_current_segment(self) -> None:
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
        self.assertFalse(window.project_search_previous.isEnabled())
        self.assertFalse(window.project_search_next.isEnabled())

    def test_text_v1_enables_options_and_pure_cjk_uses_core_semantics(self) -> None:
        self._validate_basic()
        window = self._open_window()
        self._validate_text_v1()
        window.refresh_suggestions()
        self._events()

        self.assertTrue(window.project_search_match_case.isEnabled())
        self.assertTrue(window.project_search_whole_word.isEnabled())
        self.assertIn("TEXT_V1", window.project_search_capability.text())
        window.project_search_target.setChecked(False)
        window.project_search_speaker.setChecked(False)
        window.project_search_match_case.setChecked(False)
        window.project_search_whole_word.setChecked(True)
        window.project_search_input.setText("中文词")

        with patch.object(
            self.controller,
            "search_project",
            wraps=self.controller.search_project,
        ) as search:
            QTest.mouseClick(
                window.project_search_button,
                Qt.MouseButton.LeftButton,
            )
            self._events()
            report = cast(
                ProjectSearchReport,
                window.current_project_search_report,
            )
            whole_word_total = report.total
            request = cast(ProjectSearchRequest, search.call_args.args[0])
            self.assertTrue(request.options.whole_word)
            self.assertEqual(request.fields, (SearchField.SOURCE,))

            window.project_search_whole_word.setChecked(False)
            QTest.mouseClick(
                window.project_search_button,
                Qt.MouseButton.LeftButton,
            )
            self._events()

        report = cast(ProjectSearchReport, window.current_project_search_report)
        self.assertEqual(report.total, whole_word_total)
        self.assertEqual(whole_word_total, 2)

    def test_capability_and_project_gate_refresh_without_reopening_window(self) -> None:
        window = self._open_window()
        self.assertFalse(window.project_search_button.isEnabled())
        self.assertIn(
            "MATCHER.VALIDATION_UNAVAILABLE",
            window.project_search_capability.text(),
        )

        self._validate_basic()
        window.refresh_suggestions()
        self._events()
        self.assertTrue(window.project_search_button.isEnabled())
        self.assertFalse(window.project_search_match_case.isEnabled())

        self._validate_text_v1()
        window.refresh_suggestions()
        self._events()
        self.assertTrue(window.project_search_match_case.isEnabled())

        text_path = self.project_path.with_suffix(".txt")
        text_path.write_text("needle\n", encoding="utf-8")
        self.assertTrue(window.open_project_path(text_path))
        self._events()
        self.assertFalse(window.project_search_button.isEnabled())
        self.assertIn(
            "PROJECT_TOOLS.JSON_REQUIRED",
            window.project_search_capability.text(),
        )

        window.load_sample()
        self._events()
        self.assertFalse(window.project_search_button.isEnabled())
        self.assertIn(
            "PROJECT_TOOLS.JSON_REQUIRED",
            window.project_search_capability.text(),
        )

    def test_foreign_display_wrapper_cannot_enable_qt_search(self) -> None:
        self._validate_basic()
        display = self.controller.text_matcher_handoff().display

        class ForeignDisplayWrapper:
            display: object

            def __init__(self) -> None:
                self.display = display

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            return_value=ForeignDisplayWrapper(),
        ):
            window = self._open_window()

        self.assertFalse(window.project_search_button.isEnabled())
        self.assertIn(
            "PROJECT_SEARCH.HANDOFF_INVALID",
            window.project_search_capability.text(),
        )

        window_source = (ROOT / "qt_editor_window.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("controller.text_matcher_handoff", window_source)

    def test_private_handoff_error_body_is_not_exposed_by_qt(self) -> None:
        self.controller.open_project(self.project_path)
        with patch.object(
            self.controller,
            "text_matcher_handoff",
            side_effect=EditorControllerError("PRIVATE.PROOF.TOKEN"),
        ):
            polluted = self.controller.project_search_matcher_display()
            object.__setattr__(
                polluted,
                "state",
                TextMatcherState.BASIC_VALIDATED,
            )
            display = self.controller.project_search_matcher_display()
            self.window = QtEditorWindow(self.controller)
            self.window.show()
            self._events()

        message = self.window.project_search_capability.text()
        self.assertIsNot(display, polluted)
        self.assertIs(display.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(
            display.safe_reason,
            "PROJECT_SEARCH.HANDOFF_INVALID",
        )
        self.assertNotIn("PRIVATE.PROOF.TOKEN", display.safe_reason or "")
        self.assertFalse(self.window.project_search_button.isEnabled())
        self.assertIn("PROJECT_SEARCH.HANDOFF_INVALID", message)
        self.assertNotIn("PRIVATE.PROOF.TOKEN", message)

        with patch.object(
            self.controller,
            "text_matcher_handoff",
            side_effect=EditorControllerError("MATCHER.HANDOFF_UNAVAILABLE"),
        ):
            missing_first = self.controller.project_search_matcher_display()
            object.__setattr__(
                missing_first,
                "state",
                TextMatcherState.TEXT_V1_VALIDATED,
            )
            missing_second = self.controller.project_search_matcher_display()

        self.assertIsNot(missing_first, missing_second)
        self.assertIs(missing_second.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(
            missing_second.safe_reason,
            "MATCHER.HANDOFF_UNAVAILABLE",
        )

    def test_keyboard_accessibility_and_layer4_boundary(self) -> None:
        self._validate_basic()
        window = self._open_window()

        search_shortcut = window.project_search_shortcut
        self.assertEqual(
            search_shortcut.key().toString(QKeySequence.SequenceFormat.PortableText),
            "Ctrl+F",
        )
        window.target_editor.setFocus()
        QTest.keyClick(
            window.target_editor,
            Qt.Key.Key_F,
            Qt.KeyboardModifier.ControlModifier,
        )
        self._events()
        self.assertTrue(window.project_search_input.hasFocus())

        controls = (
            window.project_search_toggle,
            window.project_search_input,
            window.project_search_source,
            window.project_search_target,
            window.project_search_speaker,
            window.project_search_status,
            window.project_search_match_case,
            window.project_search_whole_word,
            window.project_search_clear,
            window.project_search_button,
            window.project_search_previous,
            window.project_search_next,
            window.project_search_capability,
            window.project_search_result,
            window.project_search_preview,
        )
        for control in controls:
            with self.subTest(control=control.objectName()):
                self.assertTrue(control.objectName())
                self.assertTrue(control.accessibleName())
                self.assertTrue(control.toolTip())

        tree = ast.parse(
            (ROOT / "qt_editor_window.py").read_text(encoding="utf-8"),
            filename="qt_editor_window.py",
        )
        imported = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertTrue(
            {"tm_contracts", "project_search", "text_matcher"}.isdisjoint(imported),
            imported,
        )

    def test_search_surface_defaults_collapsed_and_has_two_discovery_paths(
        self,
    ) -> None:
        self._validate_basic()
        window = self._open_window(expand_search=False)

        self.assertFalse(window.project_search_panel.isVisible())
        self.assertFalse(window.project_search_toggle.isChecked())
        self.assertTrue(window.project_search_toggle.isEnabled())
        window.project_search_toggle.setFocus()
        QTest.keyClick(window.project_search_toggle, Qt.Key.Key_Space)
        self._events()
        self.assertTrue(window.project_search_panel.isVisible())
        self.assertTrue(window.project_search_input.hasFocus())
        window.project_search_input.setText("needle remains")

        window.set_workspace_mode(WorkspaceMode.BROWSE)
        window.set_workspace_mode(WorkspaceMode.EDIT)
        self.assertTrue(window.project_search_panel.isVisible())
        self.assertEqual(window.project_search_input.text(), "needle remains")

        QTest.mouseClick(
            window.project_search_toggle,
            Qt.MouseButton.LeftButton,
        )
        self.assertFalse(window.project_search_panel.isVisible())
        window.target_editor.setFocus()
        QTest.keyClick(
            window.target_editor,
            Qt.Key.Key_F,
            Qt.KeyboardModifier.ControlModifier,
        )
        self._events()
        self.assertTrue(window.project_search_panel.isVisible())
        self.assertTrue(window.project_search_input.hasFocus())
        self.assertTrue(window.project_search_input.hasSelectedText())

        self.assertTrue(window.open_project_path(self.project_path))
        self._events()
        self.assertFalse(window.project_search_panel.isVisible())
        self.assertFalse(window.project_search_toggle.isChecked())
        self.assertTrue(window.project_search_toggle.isEnabled())
        self.assertTrue(window.close_current_project())
        self.assertFalse(window.project_search_panel.isVisible())
        self.assertFalse(window.project_search_toggle.isEnabled())

    def test_explicit_clear_and_status_filter_share_controller_issuance(
        self,
    ) -> None:
        self._validate_basic()
        window = self._open_window()
        window.project_search_status.setCurrentIndex(2)
        window.project_search_source.setChecked(False)
        window.project_search_match_case.setChecked(True)
        window.project_search_whole_word.setChecked(True)
        window.project_search_input.setText("needle")
        before_project = self.controller.project
        before_index = self.controller.current_index
        before_dirty = self.controller.dirty

        with patch.object(
            self.controller,
            "search_project",
            wraps=self.controller.search_project,
        ) as search:
            QTest.mouseClick(
                window.project_search_button,
                Qt.MouseButton.LeftButton,
            )
            self._events()

        request = cast(ProjectSearchRequest, search.call_args.args[0])
        self.assertIs(request.status, SegmentTranslationStatus.DRAFT)
        self.assertEqual(
            request.fields,
            (SearchField.TARGET, SearchField.SPEAKER),
        )
        self.assertIsNotNone(window.current_project_search_report)

        with patch.object(
            self.controller,
            "clear_project_search",
            wraps=self.controller.clear_project_search,
        ) as criteria_clear:
            window.project_search_status.setCurrentIndex(3)
            self._events()
        criteria_clear.assert_called_once_with()
        self.assertIsNone(window.current_project_search_report)
        self.assertIsNone(self.controller.current_project_search_report)

        window.project_search_status.setCurrentIndex(2)
        QTest.mouseClick(
            window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertIsNotNone(window.current_project_search_report)

        with patch.object(
            self.controller,
            "clear_project_search",
            wraps=self.controller.clear_project_search,
        ) as clear:
            QTest.mouseClick(
                window.project_search_clear,
                Qt.MouseButton.LeftButton,
            )
            self._events()

        clear.assert_called_once_with()
        self.assertEqual(window.project_search_input.text(), "")
        self.assertIsNone(window.current_project_search_report)
        self.assertIsNone(self.controller.current_project_search_report)
        self.assertFalse(window.project_search_source.isChecked())
        self.assertEqual(window.project_search_status.currentIndex(), 2)
        self.assertTrue(window.project_search_match_case.isChecked())
        self.assertTrue(window.project_search_whole_word.isChecked())
        self.assertIs(self.controller.project, before_project)
        self.assertEqual(self.controller.current_index, before_index)
        self.assertEqual(self.controller.dirty, before_dirty)
        self.assertTrue(window.project_search_panel.isVisible())

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

    def test_editor_contracts_explicitly_reexports_exact_core_search_types(
        self,
    ) -> None:
        contracts_source = (ROOT / "editor_contracts.py").read_text(
            encoding="utf-8"
        )
        contracts_tree = ast.parse(
            contracts_source,
            filename="editor_contracts.py",
        )
        explicit_exports = {
            alias.name
            for node in ast.walk(contracts_tree)
            if isinstance(node, ast.ImportFrom) and node.module == "tm_contracts"
            for alias in node.names
            if alias.asname == alias.name
        }
        self.assertTrue(
            {"SearchOptions", "TextMatcherState"}.issubset(explicit_exports),
            explicit_exports,
        )
        self.assertIs(EditorSearchOptions, CoreSearchOptions)
        self.assertIs(EditorTextMatcherState, CoreTextMatcherState)
        window_source = (ROOT / "qt_editor_window.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("reportPrivateLocalImportUsage", window_source)


if __name__ == "__main__":
    unittest.main()
