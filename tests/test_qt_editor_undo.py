from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtGui import QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtEditorUndoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _window(self, root: Path) -> QtEditorWindow:
        repository = ResourceRepository(root / "app-data")
        tm = repository.create_resource(
            "Undo TM",
            ResourceKind.TRANSLATION_MEMORY,
        )
        tm.path.write_text(
            json.dumps(
                {"source": "office", "target": "办公室译文"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        terms = repository.create_resource("Undo terms", ResourceKind.TERMBASE)
        terms.path.write_text("office,办公室\n", encoding="utf-8-sig")
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(
                name="Undo",
                segments=(
                    EditorSegment(
                        id="1",
                        source="office",
                        target="已有",
                        confirmed=True,
                    ),
                    EditorSegment(
                        id="2",
                        source="next",
                        target="下一段",
                    ),
                ),
            )
        )
        window = QtEditorWindow(controller)
        window.show()
        window.raise_()
        window.activateWindow()
        self._events()
        return window

    def _focus_target(self, window: QtEditorWindow) -> None:
        window.target_editor.setFocus()
        self._events()
        self.assertTrue(window.target_editor.hasFocus())

    @staticmethod
    def _close(window: QtEditorWindow) -> None:
        window._confirm_unsaved = lambda: True
        window.close()

    def test_typing_undo_and_both_redo_shortcuts_sync_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            self._focus_target(window)
            window.target_editor.moveCursor(
                window.target_editor.textCursor().MoveOperation.End
            )

            QTest.keyClicks(window.target_editor, " changed")
            self._events()

            changed = "已有 changed"
            self.assertEqual(window.target_editor.toPlainText(), changed)
            self.assertEqual(window.controller.current_segment.target, changed)
            self.assertFalse(window.controller.current_segment.confirmed)
            self.assertTrue(window.controller.dirty)

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "已有")
            self.assertEqual(window.controller.current_segment.target, "已有")
            self.assertFalse(window.controller.current_segment.confirmed)

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Y,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), changed)
            self.assertEqual(window.controller.current_segment.target, changed)

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), changed)
            self.assertEqual(window.controller.current_segment.target, changed)
            self._close(window)

    def test_shortcuts_are_target_scoped_and_empty_stack_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            initial = window.target_editor.toPlainText()
            shortcuts = (
                (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier),
                (Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier),
                (
                    Qt.Key.Key_Z,
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.ShiftModifier,
                ),
            )

            self._focus_target(window)
            for key, modifiers in shortcuts:
                with self.subTest(empty_stack=(key, modifiers)):
                    QTest.keyClick(window.target_editor, key, modifiers)
                    self._events()
                    self.assertEqual(window.target_editor.toPlainText(), initial)

            window.target_editor.moveCursor(
                window.target_editor.textCursor().MoveOperation.End
            )
            QTest.keyClicks(window.target_editor, " changed")
            self._events()
            changed = window.target_editor.toPlainText()

            window.previous_button.setFocus()
            self._events()
            self.assertFalse(window.target_editor.hasFocus())
            for key, modifiers in shortcuts:
                with self.subTest(focus_elsewhere=(key, modifiers)):
                    QTest.keyClick(window.previous_button, key, modifiers)
                    self._events()
                    self.assertEqual(window.target_editor.toPlainText(), changed)
                    self.assertEqual(window.controller.current_segment.target, changed)
            self._close(window)

    def test_shortcuts_have_stable_target_scoped_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            expected = {
                "targetUndoShortcut": "Ctrl+Z",
                "targetRedoShortcut": "Ctrl+Y",
                "targetAlternateRedoShortcut": "Ctrl+Shift+Z",
            }

            for object_name, sequence in expected.items():
                with self.subTest(object_name=object_name):
                    shortcut = window.findChild(QShortcut, object_name)
                    self.assertIsNotNone(shortcut)
                    self.assertEqual(shortcut.key().toString(), sequence)
                    self.assertEqual(
                        shortcut.context(),
                        Qt.ShortcutContext.WidgetShortcut,
                    )
                    self.assertTrue(shortcut.whatsThis())
            self._close(window)

    def test_term_and_tm_suggestions_each_undo_in_one_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            term = window.current_suggestions.terms[0]
            tm = window.current_suggestions.tm_matches[0]
            self._focus_target(window)
            window.target_editor.moveCursor(
                window.target_editor.textCursor().MoveOperation.End
            )

            self.assertTrue(window.insert_term_suggestion(term))
            self._events()
            inserted = "已有办公室"
            self.assertEqual(window.target_editor.toPlainText(), inserted)
            self.assertEqual(window.controller.current_segment.target, inserted)

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "已有")
            self.assertEqual(window.controller.current_segment.target, "已有")

            self.assertTrue(window.apply_tm_suggestion(tm))
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "办公室译文")

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "已有")
            self.assertEqual(window.controller.current_segment.target, "已有")
            self._close(window)

    def test_segment_switch_clears_prior_document_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            self._focus_target(window)
            window.target_editor.moveCursor(
                window.target_editor.textCursor().MoveOperation.End
            )
            QTest.keyClicks(window.target_editor, " changed")
            self._events()
            first_target = window.controller.current_segment.target

            window.next_button.click()
            self._events()
            self.assertEqual(window.controller.current_index, 1)
            self.assertFalse(window.target_editor.document().isUndoAvailable())

            window.previous_button.click()
            self._events()
            self.assertEqual(window.controller.current_index, 0)
            self.assertEqual(window.target_editor.toPlainText(), first_target)
            self.assertFalse(window.target_editor.document().isUndoAvailable())

            self._focus_target(window)
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), first_target)
            self.assertEqual(window.controller.current_segment.target, first_target)
            self._close(window)

    def test_project_and_programmatic_refresh_clear_document_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            self._focus_target(window)
            window.target_editor.moveCursor(
                window.target_editor.textCursor().MoveOperation.End
            )
            QTest.keyClicks(window.target_editor, " changed")
            self._events()
            self.assertTrue(window.target_editor.document().isUndoAvailable())

            window.controller.update_target("批量刷新")
            window._refresh_target_from_controller()
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "批量刷新")
            self.assertFalse(window.target_editor.document().isUndoAvailable())

            window.controller.set_project(
                EditorProject(
                    name="Replacement",
                    segments=(
                        EditorSegment(
                            id="replacement",
                            source="replacement",
                            target="替代项目",
                        ),
                    ),
                )
            )
            window._render_project()
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "替代项目")
            self.assertFalse(window.target_editor.document().isUndoAvailable())

            self._focus_target(window)
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Z,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertEqual(window.target_editor.toPlainText(), "替代项目")
            self.assertEqual(window.controller.current_segment.target, "替代项目")
            self._close(window)


if __name__ == "__main__":
    unittest.main()
