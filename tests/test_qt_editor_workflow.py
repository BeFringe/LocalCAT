from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tm_engine import TMEngine


class QtEditorWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(
        self,
        root: Path,
        *,
        with_tm: bool = True,
    ) -> tuple[QtEditorWindow, object | None]:
        repository = ResourceRepository(root / "app-data")
        tm = (
            repository.create_resource("Writable TM", ResourceKind.TRANSLATION_MEMORY)
            if with_tm
            else None
        )
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(
                name="Workflow",
                segments=(
                    EditorSegment(id="1", source="One", target="一", confirmed=True),
                    EditorSegment(id="2", source="Two"),
                    EditorSegment(id="3", source="Three", target="三", confirmed=True),
                    EditorSegment(id="4", source="Four"),
                ),
            )
        )
        return QtEditorWindow(controller), tm

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_confirm_button_writes_tm_updates_progress_and_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window, tm = self._window(root)
            window.controller.go_to(1)
            window._render_project()
            window.target_editor.setPlainText("二")
            self._events()

            window.confirm_button.click()
            self._events()
            persisted = TMEngine(str(tm.path)).query_exact("Two")

            self.assertTrue(window.controller.project.segments[1].confirmed)
            self.assertEqual(window.controller.current_index, 3)
            self.assertEqual(window.progress_bar.value(), 3)
            self.assertEqual(persisted.target, "二")
            self.assertIn("译文已确认", window.statusBar().currentMessage())
            window._confirm_unsaved = lambda: True
            window.close()

    def test_confirm_shortcut_uses_same_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir), with_tm=False)
            window.controller.go_to(1)
            window._render_project()
            window.target_editor.setPlainText("二")
            self._events()

            window.shortcuts["confirm"].activated.emit()
            self._events()

            self.assertTrue(window.controller.project.segments[1].confirmed)
            self.assertEqual(window.controller.current_index, 3)
            window._confirm_unsaved = lambda: True
            window.close()

    def test_write_failure_stays_on_current_unconfirmed_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, tm = self._window(Path(temp_dir))
            window.controller.go_to(1)
            window._render_project()
            window.target_editor.setPlainText("二")
            self._events()
            tm.path.unlink()
            tm.path.mkdir()
            errors: list[str] = []
            window._show_error = lambda _title, message: errors.append(message)

            with redirect_stdout(io.StringIO()):
                window.confirm_button.click()
                self._events()

            self.assertFalse(window.controller.current_segment.confirmed)
            self.assertEqual(window.controller.current_index, 1)
            self.assertTrue(errors)
            window._confirm_unsaved = lambda: True
            window.close()

    def test_unconfirmed_filter_controls_list_and_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir), with_tm=False)

            window.unconfirmed_filter.setChecked(True)
            self._events()
            self.assertEqual(window.segment_list.count(), 2)
            self.assertEqual(window.controller.current_index, 1)

            window.next_button.click()
            self.assertEqual(window.controller.current_index, 3)
            window.previous_button.click()
            self.assertEqual(window.controller.current_index, 1)
            window.close()

    def test_required_shortcuts_are_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir), with_tm=False)
            sequences = {
                name: shortcut.key().toString()
                for name, shortcut in window.shortcuts.items()
            }

            self.assertEqual(
                sequences,
                {
                    "open": "Ctrl+O",
                    "save": "Ctrl+S",
                    "confirm": "Ctrl+Enter",
                    "previous": "Alt+Up",
                    "next": "Alt+Down",
                    "settings": "Ctrl+,",
                    "close_project": "Ctrl+Shift+W",
                    "quit": "Ctrl+Q",
                },
            )
            self.assertIn("Ctrl+O", window.open_button.toolTip())
            self.assertIn("Ctrl+S", window.save_button.toolTip())
            self.assertIn("Ctrl+,", window.settings_button.toolTip())
            self.assertIn("Ctrl+Enter", window.confirm_button.toolTip())
            self.assertIn("Alt+Up", window.previous_button.toolTip())
            self.assertIn("Alt+Down", window.next_button.toolTip())
            window.close()

    def test_editing_confirmed_segment_reappears_in_unconfirmed_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir), with_tm=False)
            window.controller.set_project(
                EditorProject(
                    name="All confirmed",
                    segments=(
                        EditorSegment(id="1", source="Only", target="已有", confirmed=True),
                    ),
                )
            )
            window._render_project()
            window.unconfirmed_filter.setChecked(True)
            self._events()
            self.assertEqual(window.segment_list.count(), 0)

            window.target_editor.setPlainText("已修改")
            self._events()

            self.assertEqual(window.segment_list.count(), 1)
            self.assertFalse(window.controller.current_segment.confirmed)
            self.assertEqual(window.confirmation_label.property("confirmed"), False)
            window._confirm_unsaved = lambda: True
            window.close()


if __name__ == "__main__":
    unittest.main()
