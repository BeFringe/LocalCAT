"""Qt acceptance tests for preprocessing dialog and four-view refresh."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import ClassVar, cast, override
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QMessageBox

from editor_contracts import EditorProject, EditorSegment
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from qt_preprocess_dialog import QtPreprocessDialog
from resource_repository import ResourceRepository


class QtPreprocessingTests(unittest.TestCase):
    app: ClassVar[QApplication]
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str], cast(object, None)
    )
    controller: EditorController = cast(EditorController, cast(object, None))
    window: QtEditorWindow = cast(QtEditorWindow, cast(object, None))
    dialog: QtPreprocessDialog = cast(QtPreprocessDialog, cast(object, None))

    @classmethod
    @override
    def setUpClass(cls) -> None:
        existing = QApplication.instance()
        cls.app = existing if isinstance(existing, QApplication) else QApplication([])

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-qt-preprocess-"
        )
        root = Path(self.temporary.name)
        self.controller = EditorController(
            ResourceRepository(root / "app-data")
        )
        self.controller.set_project(
            EditorProject(
                name="Qt preprocessing",
                path=root / "project.json",
                segments=(
                    EditorSegment(
                        id="seg-1",
                        source="Source one",
                        target="foo target",
                        speaker="Alice",
                        confirmed=True,
                    ),
                    EditorSegment(
                        id="seg-2",
                        source="Source two",
                        target="keep",
                        speaker="Bob",
                        confirmed=True,
                    ),
                ),
            )
        )
        self.window = QtEditorWindow(self.controller)
        self.window.show()
        self.dialog = QtPreprocessDialog(self.controller, self.window)
        self.dialog.show()
        self._events()

    @override
    def tearDown(self) -> None:
        dialog = getattr(self, "dialog", None)
        if isinstance(dialog, QtPreprocessDialog):
            dialog.close()
        window = getattr(self, "window", None)
        if isinstance(window, QtEditorWindow):
            window._confirm_unsaved = lambda: True
            window.close()
        self._events()
        self.temporary.cleanup()

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _set_rule(
        self,
        row: int,
        *,
        find: str,
        replacement: str,
        enabled: bool = True,
    ) -> None:
        enabled_item = self.dialog.rules_table.item(row, 0)
        find_item = self.dialog.rules_table.item(row, 1)
        replacement_item = self.dialog.rules_table.item(row, 2)
        assert enabled_item is not None
        assert find_item is not None
        assert replacement_item is not None
        enabled_item.setCheckState(
            Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        )
        find_item.setText(find)
        replacement_item.setText(replacement)
        self._events()

    def test_ordered_rules_preview_and_cancel_are_read_only(self) -> None:
        self._set_rule(0, find="foo", replacement="bar")
        self.dialog.add_rule_button.click()
        self._set_rule(1, find="bar", replacement="baz")
        project_before = self.controller.project
        revision_before = self.controller.project_revision

        self.assertTrue(self.dialog.preview_rules())

        self.assertIs(self.controller.project, project_before)
        self.assertEqual(self.controller.project_revision, revision_before)
        self.assertFalse(self.controller.dirty)
        self.assertEqual(self.dialog.preview_table.rowCount(), 1)
        self.assertEqual(
            self.dialog.preview_table.item(0, 2).text(),
            "baz target",
        )
        self.assertIn("1", self.dialog.preview_count_label.text())
        self.assertTrue(self.dialog.apply_button.isEnabled())

        self.dialog.cancel_preview()

        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertEqual(self.dialog.preview_table.rowCount(), 0)
        self.assertIs(self.controller.project, project_before)
        self.assertFalse(self.controller.dirty)

    def test_rule_enable_and_reorder_follow_visible_order(self) -> None:
        self._set_rule(0, find="foo", replacement="bar", enabled=False)
        self.dialog.add_rule_button.click()
        self._set_rule(1, find="bar", replacement="baz", enabled=True)
        self.dialog.rules_table.setCurrentCell(1, 1)

        self.dialog.move_rule_up_button.click()

        rules = self.dialog.rules()
        self.assertEqual(
            tuple((rule.find, rule.replacement, rule.enabled) for rule in rules),
            (("bar", "baz", True), ("foo", "bar", False)),
        )
        self.assertFalse(self.dialog.preview_rules())
        self.assertIn("没有产生", self.dialog.status_label.text())

    def test_apply_and_undo_refresh_edit_browse_progress_dirty_and_suggestions(
        self,
    ) -> None:
        self._set_rule(0, find="foo", replacement="bar")
        self.assertTrue(self.dialog.preview_rules())
        self.dialog._confirm_apply = lambda _count, _includes_confirmed: True
        self.dialog.mutation_committed.connect(
            self.window._preprocessing_changed
        )

        self.assertTrue(self.dialog.apply_preview())
        self._events()

        self.assertEqual(self.window.target_editor.toPlainText(), "bar target")
        self.assertEqual(self.window.browse_table.item(0, 2).text(), "bar target")
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.assertEqual(self.window.confirmation_label.text(), "待确认")
        self.assertTrue(self.controller.dirty)
        self.assertIn("*", self.window.windowTitle())
        self.assertEqual(
            self.window.source_display.toPlainText(),
            self.controller.current_segment.source,
        )
        self.assertTrue(self.dialog.undo_button.isEnabled())

        self.assertTrue(self.dialog.undo_latest())
        self._events()

        self.assertEqual(self.window.target_editor.toPlainText(), "foo target")
        self.assertEqual(self.window.browse_table.item(0, 2).text(), "foo target")
        self.assertEqual(self.window.progress_bar.value(), 2)
        self.assertEqual(self.window.confirmation_label.text(), "已确认")
        self.assertFalse(self.controller.dirty)
        self.assertNotIn("*", self.window.windowTitle())
        self.assertFalse(self.dialog.undo_button.isEnabled())

    def test_stale_apply_and_no_undo_show_clear_error_without_partial_refresh(self) -> None:
        self._set_rule(0, find="foo", replacement="bar")
        self.assertTrue(self.dialog.preview_rules())
        self.dialog._confirm_apply = lambda _count, _includes_confirmed: True
        self.controller.update_target("manual")
        current_project = self.controller.project

        self.assertFalse(self.dialog.apply_preview())

        self.assertIs(self.controller.project, current_project)
        self.assertIn("重新预览", self.dialog.status_label.text())
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertFalse(self.dialog.undo_latest())
        self.assertIn("没有可撤销", self.dialog.status_label.text())

    def test_project_action_obeys_single_json_capability(self) -> None:
        self.assertTrue(self.window.preprocess_action.isEnabled())
        self.assertEqual(
            self.window.preprocess_action.objectName(),
            "preprocessProjectAction",
        )
        self.assertEqual(self.window.preprocess_action.text(), "Target 文字预处理")
        self.controller.set_project(
            EditorProject(
                name="Text",
                path=Path(self.temporary.name) / "project.txt",
                segments=(
                    EditorSegment(id="text", source="Text", target="foo"),
                ),
            )
        )

        self.window._render_project()

        self.assertFalse(self.window.preprocess_action.isEnabled())
        self.assertIn("JSON", self.window.preprocess_action.toolTip())

    def test_status_filters_are_two_checkboxes_and_preview_reports_distribution(
        self,
    ) -> None:
        self.assertEqual(self.dialog.findChildren(QComboBox), [])
        checkboxes = self.dialog.findChildren(QCheckBox)
        self.assertEqual(
            {checkbox.text() for checkbox in checkboxes},
            {"草稿", "已确认"},
        )
        self.assertTrue(self.dialog.include_draft_checkbox.isChecked())
        self.assertTrue(self.dialog.include_confirmed_checkbox.isChecked())
        self.controller.set_project(
            EditorProject(
                name="Status filters",
                path=Path(self.temporary.name) / "filters.json",
                segments=(
                    EditorSegment(
                        id="draft",
                        source="Draft",
                        target="foo draft",
                        confirmed=False,
                    ),
                    EditorSegment(
                        id="confirmed",
                        source="Confirmed",
                        target="foo confirmed",
                        confirmed=True,
                    ),
                ),
            )
        )
        self._set_rule(0, find="foo", replacement="bar")

        self.assertTrue(self.dialog.preview_rules())
        self.assertEqual(self.dialog.preview_table.rowCount(), 2)
        self.assertIn("草稿 1", self.dialog.preview_count_label.text())
        self.assertIn("已确认 1", self.dialog.preview_count_label.text())

        self.dialog.include_confirmed_checkbox.setChecked(False)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertTrue(self.dialog.preview_rules())
        self.assertEqual(self.dialog.preview_table.rowCount(), 1)
        self.assertIn("草稿 1", self.dialog.preview_count_label.text())
        self.assertIn("已确认 0", self.dialog.preview_count_label.text())

        self.dialog.include_draft_checkbox.setChecked(False)
        self.assertFalse(self.dialog.preview_rules())
        self.assertIn("至少选择", self.dialog.status_label.text())

    def test_save_rules_restores_order_filters_and_never_runs_rules(self) -> None:
        self._set_rule(0, find="foo", replacement="bar")
        self.dialog.add_rule_button.click()
        self._set_rule(1, find="bar", replacement="baz", enabled=False)
        self.dialog.include_confirmed_checkbox.setChecked(False)
        project_before = self.controller.project
        revision_before = self.controller.project_revision

        self.assertTrue(self.dialog.save_preferences())
        self.assertIs(self.controller.project, project_before)
        self.assertEqual(self.controller.project_revision, revision_before)
        self.assertFalse(self.controller.dirty)
        self.assertEqual(self.controller.project.segments[0].target, "foo target")

        reopened = QtPreprocessDialog(self.controller, self.window)
        self.addCleanup(reopened.close)
        self.assertEqual(
            tuple(
                (rule.find, rule.replacement, rule.enabled)
                for rule in reopened.rules()
            ),
            (("foo", "bar", True), ("bar", "baz", False)),
        )
        self.assertTrue(reopened.include_draft_checkbox.isChecked())
        self.assertFalse(reopened.include_confirmed_checkbox.isChecked())

        root = Path(self.temporary.name)
        restarted_controller = EditorController(
            ResourceRepository(root / "app-data")
        )
        restarted = QtPreprocessDialog(restarted_controller)
        self.addCleanup(restarted.close)
        self.assertEqual(
            tuple(
                (rule.find, rule.replacement, rule.enabled)
                for rule in restarted.rules()
            ),
            (("foo", "bar", True), ("bar", "baz", False)),
        )
        self.assertTrue(restarted.include_draft_checkbox.isChecked())
        self.assertFalse(restarted.include_confirmed_checkbox.isChecked())

    def test_confirmed_preview_keeps_warning_and_draft_only_stays_explicit(
        self,
    ) -> None:
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as question:
            self.assertFalse(self.dialog._confirm_apply(2, True))
            confirmed_prompt = question.call_args.args[2]
            self.assertIn("设为待确认", confirmed_prompt)

            self.assertFalse(self.dialog._confirm_apply(1, False))
            draft_prompt = question.call_args.args[2]
            self.assertIn("草稿", draft_prompt)
            self.assertNotIn("设为待确认", draft_prompt)


if __name__ == "__main__":
    unittest.main()
