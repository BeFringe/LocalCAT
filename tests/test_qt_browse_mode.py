from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from editor_contracts import SegmentDensity, WorkspaceMode
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtBrowseModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _window(self, root: Path) -> tuple[QtEditorWindow, Path]:
        project_path = root / "long-project.json"
        project_path.write_text(
            json.dumps(
                {
                    "name": "Long project",
                    "segments": [
                        {
                            "id": "one",
                            "source": "A very long source sentence " * 12,
                            "target": "第一段译文",
                        },
                        {
                            "id": "two",
                            "source": "Another long source sentence " * 10,
                            "target": "第二段译文",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        controller = EditorController(ResourceRepository(root / "app-data"))
        controller.open_project(project_path)
        window = QtEditorWindow(controller)
        window.resize(1280, 760)
        window.show()
        self._events()
        return window, project_path

    def test_compact_and_wrapped_density_preserve_current_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir))
            window.target_editor.setPlainText("尚未保存的最新译文")
            self._events()
            compact_height = window.segment_list.item(0).sizeHint().height()

            self.assertTrue(window.set_segment_density(SegmentDensity.WRAPPED))
            self._events()
            wrapped_item = window.segment_list.item(0)

            self.assertGreater(wrapped_item.sizeHint().height(), compact_height)
            self.assertIn("A very long source sentence", wrapped_item.text())
            self.assertEqual(window.controller.current_index, 0)
            self.assertEqual(window.controller.current_segment.target, "尚未保存的最新译文")
            self.assertEqual(
                window.controller.display_preferences().segment_density,
                SegmentDensity.WRAPPED,
            )
            narrow_height = wrapped_item.sizeHint().height()
            window.main_splitter.setSizes([520, 430, 300])
            window._refresh_segment_item_sizes()
            self._events()
            self.assertLessEqual(
                window.segment_list.item(0).sizeHint().height(),
                narrow_height,
            )
            window._confirm_unsaved = lambda: True
            window.close()

            restored_controller = EditorController(
                ResourceRepository(Path(temp_dir) / "app-data")
            )
            restored_controller.open_project(Path(temp_dir) / "long-project.json")
            restored_window = QtEditorWindow(restored_controller)
            self.assertIs(restored_window.segment_density, SegmentDensity.WRAPPED)
            restored_window.close()

    def test_browse_shows_latest_bilingual_rows_and_double_click_returns_to_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir))
            window.target_editor.setPlainText("浏览页应显示的最新译文")
            self._events()

            self.assertTrue(window.set_workspace_mode(WorkspaceMode.BROWSE))
            self._events()

            self.assertEqual(window.workspace_pages.currentIndex(), 1)
            self.assertEqual(window.browse_table.rowCount(), 2)
            self.assertEqual(window.browse_table.item(0, 2).text(), "浏览页应显示的最新译文")
            self.assertTrue(window.browse_table.wordWrap())
            self.assertGreater(window.browse_table.rowHeight(0), 44)

            window.browse_table.cellDoubleClicked.emit(1, 1)
            self._events()

            self.assertEqual(window.workspace_pages.currentIndex(), 0)
            self.assertEqual(window.controller.current_index, 1)
            self.assertEqual(
                window.controller.display_preferences().workspace_mode,
                WorkspaceMode.EDIT,
            )
            self.assertTrue(window.set_workspace_mode(WorkspaceMode.BROWSE))
            window._confirm_unsaved = lambda: True
            window.close()

            restored_controller = EditorController(
                ResourceRepository(Path(temp_dir) / "app-data")
            )
            restored_controller.open_project(Path(temp_dir) / "long-project.json")
            restored_window = QtEditorWindow(restored_controller)
            self.assertIs(restored_window.workspace_mode, WorkspaceMode.BROWSE)
            self.assertEqual(restored_window.workspace_pages.currentIndex(), 1)
            restored_window.close()


if __name__ == "__main__":
    unittest.main()
