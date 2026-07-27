from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QPushButton

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_settings_dialog import (
    TERMBASE_FILE_FILTER,
    TMX_FILE_FILTER,
    QtSettingsDialog,
)
from resource_repository import ResourceRepository


class RecordingController(EditorController):
    def __init__(self, repository: ResourceRepository) -> None:
        self.import_thread: QThread | None = None
        super().__init__(repository)

    def import_resource(self, request):
        self.import_thread = QThread.currentThread()
        return super().import_resource(request)


class QtSettingsImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(
        self,
        root: Path,
        kind: ResourceKind,
    ) -> tuple[QtSettingsDialog, RecordingController, str]:
        controller = RecordingController(ResourceRepository(root / "app-data"))
        controller.set_project(
            EditorProject(
                name="Import",
                segments=(EditorSegment(id="1", source="The office is ready."),),
            )
        )
        resource = controller.create_resource("Import target", kind)
        return QtSettingsDialog(controller), controller, resource.id

    def _write_tmx(self, path: Path, *, unsafe: bool = False) -> None:
        if unsafe:
            path.write_text(
                '<!DOCTYPE tmx SYSTEM "tmx14.dtd"><tmx><body/></tmx>',
                encoding="utf-8",
            )
            return
        path.write_text(
            """<tmx version="1.4"><body>
            <tu><tuv xml:lang="en-US"><seg>The office is ready.</seg></tuv>
                <tuv xml:lang="zh-CN"><seg>办公室准备好了。</seg></tuv></tu>
            </body></tmx>""",
            encoding="utf-8",
        )

    @staticmethod
    def _wait_for_import(dialog: QtSettingsDialog) -> bool:
        if dialog.last_import_report is not None:
            return True
        worker = dialog.import_worker
        if worker is None or not worker.wait(5000):
            return False
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        return dialog.last_import_report is not None

    def test_tmx_import_runs_off_main_thread_and_reports_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(
                root, ResourceKind.TRANSLATION_MEMORY
            )
            source = root / "memory.tmx"
            self._write_tmx(source)
            event_seen: list[bool] = []

            self.assertTrue(dialog.start_import(resource_id, source, "en-US", "zh-CN"))
            self.assertFalse(dialog.active_table.isEnabled())
            QTimer.singleShot(0, lambda: event_seen.append(True))
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            self.assertTrue(event_seen)
            self.assertTrue(self._wait_for_import(dialog))

            self.assertIsNot(controller.import_thread, self.app.thread())
            self.assertEqual(dialog.last_import_report.imported, 1)
            self.assertIn("已导入 1", dialog.import_feedback.text())
            self.assertTrue(dialog.active_table.isEnabled())
            self.assertEqual(controller.suggestions().tm_matches[0].target, "办公室准备好了。")
            dialog.close()

    def test_failed_tmx_import_can_retry_without_reopening_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, _, resource_id = self._dialog(root, ResourceKind.TRANSLATION_MEMORY)
            unsafe = root / "unsafe.tmx"
            valid = root / "valid.tmx"
            self._write_tmx(unsafe, unsafe=True)
            self._write_tmx(valid)
            self.assertTrue(dialog.start_import(resource_id, unsafe, "en-US", "zh-CN"))
            self.assertTrue(self._wait_for_import(dialog))
            self.assertTrue(dialog.last_import_report.errors)
            self.assertTrue(dialog.start_import(resource_id, valid, "en-US", "zh-CN"))
            self.assertTrue(self._wait_for_import(dialog))

            self.assertEqual(dialog.last_import_report.imported, 1)
            self.assertEqual(dialog.last_import_report.errors, ())
            dialog.close()

    def test_csv_import_and_resource_specific_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,办公室\n", encoding="utf-8-sig")
            button = dialog.findChild(QPushButton, f"import_{resource_id}")
            self.assertEqual(button.text(), "导入术语表")
            self.assertTrue(dialog.start_import(resource_id, source))
            self.assertTrue(self._wait_for_import(dialog))

            self.assertEqual(dialog.last_import_report.imported, 1)
            self.assertEqual(controller.suggestions().terms[0].target_term, "办公室")
            self.assertEqual(TMX_FILE_FILTER, "TMX files (*.tmx)")
            self.assertEqual(TERMBASE_FILE_FILTER, "Termbase files (*.csv *.xlsx)")
            dialog.close()


if __name__ == "__main__":
    unittest.main()
