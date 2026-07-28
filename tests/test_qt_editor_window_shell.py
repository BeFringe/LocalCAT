from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from editor_contracts import ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtEditorWindowShellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> QtEditorWindow:
        repository = ResourceRepository(root / "app-data")
        repository.create_resource("Local TM", ResourceKind.TRANSLATION_MEMORY)
        repository.create_resource("Local terms", ResourceKind.TERMBASE)
        return QtEditorWindow(EditorController(repository))

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _schedule_message_box_click(
        self,
        standard_button: QMessageBox.StandardButton,
        clicked: list[str],
    ) -> None:
        def click_button() -> None:
            for widget in QApplication.topLevelWidgets():
                if not isinstance(widget, QMessageBox):
                    continue
                button = widget.button(standard_button)
                if button is not None:
                    clicked.append(button.text())
                    button.click()
                    return

        QTimer.singleShot(25, click_button)

    def test_empty_state_and_sample_reach_first_editor_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.show()
            self._events()

            self.assertEqual(window.pages.currentWidget().objectName(), "emptyPage")
            self.assertTrue(window.open_button.isVisible())
            self.assertTrue(window.settings_button.isVisible())

            window.load_sample()
            self._events()

            self.assertEqual(window.pages.currentWidget().objectName(), "editorPage")
            self.assertEqual(window.segment_list.count(), 3)
            self.assertEqual(window.project_name_label.text(), "LocalCAT Welcome")
            self.assertEqual(window.language_label.text(), "en-US  →  zh-CN")
            window.close()

    def test_open_edit_and_save_json_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "input.json"
            save_path = root / "saved.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Client project",
                        "source_locale": "en-US",
                        "target_locale": "zh-CN",
                        "segments": [
                            {"id": "1", "source": "Hello"},
                            {"id": "2", "source": "World"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            window = self._window(root)

            self.assertTrue(window.open_project_path(project_path))
            window.target_editor.setPlainText("你好")
            self._events()
            self.assertTrue(window.controller.dirty)
            self.assertTrue(window.save_project_path(save_path))
            payload = json.loads(save_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["segments"][0]["target"], "你好")
            self.assertFalse(window.controller.dirty)
            self.assertIn("Client project", window.windowTitle())
            self.assertIn("已保存", window.statusBar().currentMessage())
            window.close()

    def test_invalid_open_and_cancelled_unsaved_guard_preserve_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.txt"
            second = root / "second.txt"
            invalid = root / "invalid.json"
            first.write_text("First source\n", encoding="utf-8")
            second.write_text("Second source\n", encoding="utf-8")
            invalid.write_text("{not-json", encoding="utf-8")
            window = self._window(root)
            errors: list[str] = []
            window._show_error = lambda _title, message: errors.append(message)

            self.assertTrue(window.open_project_path(first))
            self.assertFalse(window.open_project_path(invalid))
            self.assertEqual(window.controller.current_segment.source, "First source")
            self.assertTrue(errors)
            window.target_editor.setPlainText("未保存")
            self._events()
            window._confirm_unsaved = lambda: False

            self.assertFalse(window.open_project_path(second))
            self.assertEqual(window.controller.current_segment.source, "First source")
            window.controller.save_project(root / "cleanup.json")
            window.close()

    def test_discard_button_closes_dirty_project_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.load_sample()
            window.target_editor.setPlainText("未保存")
            self._events()
            clicked: list[str] = []

            self._schedule_message_box_click(
                QMessageBox.StandardButton.Discard,
                clicked,
            )
            self.assertTrue(window.close_current_project())
            self.assertFalse(window.controller.has_project)

            window.load_sample()
            window.target_editor.setPlainText("再次未保存")
            window.show()
            self._events()
            self._schedule_message_box_click(
                QMessageBox.StandardButton.Discard,
                clicked,
            )
            self.assertTrue(window.close())
            self._events()

            self.assertEqual(len(clicked), 2)
            self.assertFalse(window.isVisible())

    def test_three_columns_keep_usable_sizes_after_resize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.load_sample()
            window.resize(1280, 760)
            window.show()
            self._events()
            sizes = window.main_splitter.sizes()

            self.assertEqual(len(sizes), 3)
            self.assertGreaterEqual(sizes[0], 200)
            self.assertGreaterEqual(sizes[1], 360)
            self.assertGreaterEqual(sizes[2], 250)
            self.assertEqual(window.main_splitter.stretchFactor(0), 2)
            self.assertEqual(window.main_splitter.stretchFactor(1), 5)
            self.assertEqual(window.main_splitter.stretchFactor(2), 3)
            window.close()

    def test_project_menu_lists_recent_and_can_exit_to_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "recent.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Recent",
                        "segments": [
                            {"id": "1", "source": "First"},
                            {"id": "2", "source": "Second"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            window = self._window(root)
            self.assertTrue(window.open_project_path(project_path))
            window.controller.go_to(1)
            window.refresh_recent_projects()

            actions = window.recent_projects_menu.actions()
            self.assertEqual(len(actions), 1)
            self.assertIn("recent.json", actions[0].text())
            self.assertEqual(Path(actions[0].data()), project_path.resolve())

            window.target_editor.setPlainText("未保存")
            self._events()
            window._confirm_unsaved = lambda: False
            self.assertFalse(window.close_current_project())
            self.assertTrue(window.controller.has_project)
            window._confirm_unsaved = lambda: True
            self.assertTrue(window.close_current_project())

            self.assertFalse(window.controller.has_project)
            self.assertEqual(window.pages.currentWidget().objectName(), "emptyPage")
            self.assertFalse(window.save_button.isEnabled())
            self.assertFalse(window.close_project_action.isEnabled())
            window.close()

    def test_missing_recent_project_is_pruned_with_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "gone.txt"
            project_path.write_text("Temporary project\n", encoding="utf-8")
            window = self._window(root)
            self.assertTrue(window.open_project_path(project_path))
            self.assertTrue(window.close_current_project())
            project_path.unlink()
            errors: list[str] = []
            window._show_error = lambda _title, message: errors.append(message)

            self.assertFalse(window.open_recent_project(project_path))

            self.assertTrue(errors)
            self.assertIn("不存在", errors[0])
            self.assertEqual(window.recent_projects_menu.actions()[0].text(), "暂无最近项目")
            window.close()


if __name__ == "__main__":
    unittest.main()
