from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


ROOT = Path(__file__).resolve().parents[1]


class QtUserJourneyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> QtEditorWindow:
        repository = ResourceRepository(root / "app-data")
        tm = repository.create_resource("Journey TM", ResourceKind.TRANSLATION_MEMORY)
        tm.path.write_text(
            json.dumps({"source": "The office", "target": "该办公室"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        terms = repository.create_resource("Journey terms", ResourceKind.TERMBASE)
        terms.path.write_text("office,办公室\n", encoding="utf-8-sig")
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(
                name="Qt journey",
                segments=(
                    EditorSegment(id="1", source="The office"),
                    EditorSegment(id="2", source="Next segment"),
                ),
            )
        )
        window = QtEditorWindow(controller)
        window.show()
        self._events()
        return window

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_mouse_and_keyboard_complete_editor_resource_journey(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window = self._window(root)
            opened_settings: list[str] = []

            def close_settings() -> None:
                dialog = QApplication.activeModalWidget()
                opened_settings.append(dialog.objectName())
                QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)

            QTimer.singleShot(0, close_settings)
            QTest.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
            self.assertEqual(opened_settings, ["settingsDialog"])

            apply_tm = window.findChild(QPushButton, "applyTm_0")
            QTest.mouseClick(apply_tm, Qt.MouseButton.LeftButton)
            self.assertEqual(window.target_editor.toPlainText(), "该办公室")

            window.target_editor.moveCursor(window.target_editor.textCursor().MoveOperation.End)
            insert_term = window.findChild(QPushButton, "insertTerm_0")
            QTest.mouseClick(insert_term, Qt.MouseButton.LeftButton)
            self.assertTrue(window.target_editor.toPlainText().endswith("办公室"))

            def complete_term_prompt() -> None:
                prompt = QApplication.activeModalWidget()
                inputs = prompt.findChildren(QLineEdit)
                inputs[0].setText("The")
                inputs[1].setText("该")
                buttons = prompt.findChild(QDialogButtonBox)
                QTest.mouseClick(
                    buttons.button(QDialogButtonBox.StandardButton.Ok),
                    Qt.MouseButton.LeftButton,
                )

            QTimer.singleShot(0, complete_term_prompt)
            QTest.mouseClick(window.add_term_button, Qt.MouseButton.LeftButton)
            self.assertIn("The", {term.source_term for term in window.current_suggestions.terms})

            window.raise_()
            window.activateWindow()
            window.target_editor.setFocus()
            self._events()
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Enter,
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.KeypadModifier,
            )
            self._events()

            self.assertTrue(window.controller.project.segments[0].confirmed)
            self.assertEqual(window.controller.current_index, 1)
            window.controller.save_project(root / "journey.json")
            window.close()

    def test_qt_layer_import_boundary_is_ast_guarded(self) -> None:
        forbidden = {
            "resource_repository",
            "tm_engine",
            "glossary_engine",
            "logic_controller",
        }
        for filename in ("qt_editor_window.py", "qt_settings_dialog.py"):
            with self.subTest(filename=filename):
                tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
                imported: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".", 1)[0])
                self.assertTrue(forbidden.isdisjoint(imported), imported & forbidden)


if __name__ == "__main__":
    unittest.main()
