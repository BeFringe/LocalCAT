from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import Workbook
from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from editor_contracts import ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtEndToEndResourceLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _wait_import(self, dialog) -> None:
        worker = dialog.import_worker
        if worker is not None:
            self.assertTrue(worker.wait(5000))
        self._events()
        self.assertIsNotNone(dialog.last_import_report)
        self.assertGreater(dialog.last_import_report.imported, 0)

    def test_settings_import_confirm_save_and_reload_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            project_input = root / "project.json"
            project_output = root / "saved.json"
            project_input.write_text(
                json.dumps(
                    {
                        "name": "End-to-end",
                        "source_locale": "en-US",
                        "target_locale": "zh-CN",
                        "segments": [{"id": "1", "source": "The office memory"}],
                    }
                ),
                encoding="utf-8",
            )
            tmx_path = root / "memory.tmx"
            tmx_path.write_text(
                """<tmx version="1.4"><body>
                <tu><tuv xml:lang="en-US"><seg>The office memory</seg></tuv>
                    <tuv xml:lang="zh-CN"><seg>该办公室记忆库</seg></tuv></tu>
                </body></tmx>""",
                encoding="utf-8",
            )
            csv_path = root / "terms.csv"
            csv_path.write_text("Source,Target\noffice,办公室\n", encoding="utf-8-sig")
            xlsx_path = root / "terms.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Source term", "Target term"])
            sheet.append(["memory", "记忆库"])
            workbook.save(xlsx_path)
            workbook.close()

            repository = ResourceRepository(config_dir)
            tm = repository.create_resource("E2E TM", ResourceKind.TRANSLATION_MEMORY)
            csv_terms = repository.create_resource("E2E CSV terms", ResourceKind.TERMBASE)
            xlsx_terms = repository.create_resource("E2E XLSX terms", ResourceKind.TERMBASE)
            controller = EditorController(repository)
            window = QtEditorWindow(controller)
            self.assertTrue(window.open_project_path(project_input))
            dialog = window.create_settings_dialog()

            self.assertTrue(dialog.start_import(tm.id, tmx_path, "en-US", "zh-CN"))
            self._wait_import(dialog)
            self.assertTrue(dialog.start_import(csv_terms.id, csv_path))
            self._wait_import(dialog)
            self.assertTrue(dialog.start_import(xlsx_terms.id, xlsx_path))
            self._wait_import(dialog)

            bundle = window.current_suggestions
            self.assertEqual(bundle.tm_matches[0].target, "该办公室记忆库")
            self.assertEqual(
                {term.source_term for term in bundle.terms},
                {"office", "memory"},
            )
            self.assertTrue(window.apply_tm_suggestion(bundle.tm_matches[0]))
            self.assertTrue(window.confirm_current())
            self.assertTrue(window.save_project_path(project_output))
            dialog.close()
            window.close()

            restored_repository = ResourceRepository(config_dir)
            restored = EditorController(restored_repository)
            restored.open_project(project_output)
            restored_bundle = restored.suggestions()

            self.assertTrue(restored.current_segment.confirmed)
            self.assertEqual(restored.current_segment.target, "该办公室记忆库")
            self.assertEqual(restored_bundle.tm_matches[0].target, "该办公室记忆库")
            self.assertEqual(
                {term.source_term for term in restored_bundle.terms},
                {"office", "memory"},
            )


if __name__ == "__main__":
    unittest.main()
