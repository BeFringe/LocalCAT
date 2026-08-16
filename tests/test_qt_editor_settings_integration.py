from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication, QCheckBox

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtEditorSettingsIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(
        self,
        root: Path,
        *,
        populated_tm: bool,
    ) -> tuple[QtEditorWindow, str]:
        repository = ResourceRepository(root / "app-data")
        tm = repository.create_resource("Settings TM", ResourceKind.TRANSLATION_MEMORY)
        if populated_tm:
            tm.path.write_text(
                json.dumps(
                    {"source": "The office is ready.", "target": "办公室准备好了。"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(
                name="Settings integration",
                segments=(EditorSegment(id="1", source="The office is ready."),),
            )
        )
        return QtEditorWindow(controller), tm.id

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _wait_for_import(self, dialog) -> None:
        worker = dialog.import_worker
        if worker is not None:
            self.assertTrue(worker.wait(5000))
        self._events()
        self.assertIsNotNone(dialog.last_import_report)

    def test_lookup_change_refreshes_suggestions_without_losing_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, resource_id = self._window(Path(temp_dir), populated_tm=True)
            window.target_editor.setPlainText("我的未保存译文")
            self._events()
            dialog = window.create_settings_dialog()
            lookup = dialog.findChild(QCheckBox, f"lookup_{resource_id}")

            self.assertEqual(len(window.current_suggestions.tm_matches), 1)
            lookup.setChecked(False)
            self._events()

            self.assertEqual(window.current_suggestions.tm_matches, ())
            self.assertEqual(window.controller.current_segment.target, "我的未保存译文")
            self.assertTrue(window.controller.dirty)
            self.assertIn("建议已刷新", window.statusBar().currentMessage())
            dialog.close()
            window._confirm_unsaved = lambda: True
            window.close()

    def test_settings_import_refreshes_main_window_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window, resource_id = self._window(root, populated_tm=False)
            source = root / "memory.tmx"
            source.write_text(
                """<tmx version="1.4"><body>
                <tu><tuv xml:lang="en-US"><seg>The office is ready.</seg></tuv>
                    <tuv xml:lang="zh-CN"><seg>导入后出现。</seg></tuv></tu>
                </body></tmx>""",
                encoding="utf-8",
            )
            dialog = window.create_settings_dialog()

            self.assertEqual(window.current_suggestions.tm_matches, ())
            self.assertTrue(dialog.start_import(resource_id, source, "en-US", "zh-CN"))
            self._wait_for_import(dialog)

            self.assertEqual(dialog.last_import_report.imported, 1)
            self.assertEqual(window.current_suggestions.tm_matches[0].target, "导入后出现。")
            self.assertEqual(window.controller.current_index, 0)
            dialog.close()
            window.close()

    def test_settings_resource_creation_keeps_project_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window, _ = self._window(Path(temp_dir), populated_tm=False)
            project = window.controller.project
            dialog = window.create_settings_dialog()

            created = dialog.create_resource("New termbase", ResourceKind.TERMBASE)
            self._events()

            self.assertEqual(window.controller.project, project)
            self.assertIn(created, window.controller.list_resources())
            self.assertIs(window.settings_dialog, dialog)
            dialog.close()
            window.close()


if __name__ == "__main__":
    unittest.main()
