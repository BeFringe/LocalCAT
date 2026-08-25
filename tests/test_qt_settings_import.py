from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QPushButton,
)

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TermbaseImportHeaderMode,
    TermbaseImportSelection,
)
from editor_controller import EditorController
from qt_settings_dialog import (
    TERMBASE_FILE_FILTER,
    TMX_FILE_FILTER,
    QtSettingsDialog,
    TermbaseColumnSelectionDialog,
)
from resource_repository import ResourceRepository


class RecordingController(EditorController):
    def __init__(self, repository: ResourceRepository) -> None:
        self.import_thread: QThread | None = None
        self.preview_thread: QThread | None = None
        self.preview_paths: list[Path] = []
        self.import_requests = []
        super().__init__(repository)

    def preview_termbase_import(self, input_path: Path):
        self.preview_thread = QThread.currentThread()
        self.preview_paths.append(input_path)
        return super().preview_termbase_import(input_path)

    def import_resource(self, request):
        self.import_thread = QThread.currentThread()
        self.import_requests.append(request)
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

    @staticmethod
    def _wait_for_preview(dialog: QtSettingsDialog) -> bool:
        worker = dialog.preview_worker
        if worker is None or not worker.wait(5000):
            return False
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        return dialog.preview_worker is None

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
            report = dialog.last_import_report
            assert report is not None
            self.assertEqual(report.imported, 1)
            self.assertIn("已导入 1", dialog.import_feedback.text())
            self.assertIn("内部 JSONL", dialog.import_feedback.text())
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
            failed_report = dialog.last_import_report
            assert failed_report is not None
            self.assertTrue(failed_report.errors)
            self.assertTrue(dialog.start_import(resource_id, valid, "en-US", "zh-CN"))
            self.assertTrue(self._wait_for_import(dialog))

            report = dialog.last_import_report
            assert report is not None
            self.assertEqual(report.imported, 1)
            self.assertEqual(report.errors, ())
            dialog.close()

    def test_csv_import_and_resource_specific_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,办公室\n", encoding="utf-8-sig")
            button = dialog.findChild(QPushButton, f"import_{resource_id}")
            assert button is not None
            self.assertEqual(button.text(), "导入术语表")
            self.assertTrue(dialog.start_import(resource_id, source))
            self.assertTrue(self._wait_for_import(dialog))

            report = dialog.last_import_report
            assert report is not None
            self.assertEqual(report.imported, 1)
            self.assertEqual(controller.suggestions().terms[0].target_term, "办公室")
            self.assertEqual(TMX_FILE_FILTER, "TMX files (*.tmx)")
            self.assertEqual(TERMBASE_FILE_FILTER, "Termbase files (*.csv *.xlsx)")
            dialog.close()

    def test_column_dialog_projects_preview_and_builds_index_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _settings, controller, _resource_id = self._dialog(
                root,
                ResourceKind.TERMBASE,
            )
            source = root / "long-header.csv"
            source.write_text(
                f"Source,Target,{'N' * 300}\noffice,\u529e\u516c\u5ba4,note\n",
                encoding="utf-8-sig",
            )
            preview = controller.preview_termbase_import(source)
            prompt = TermbaseColumnSelectionDialog(preview)

            source_combo = prompt.findChild(QComboBox, "termbaseSourceColumn")
            target_combo = prompt.findChild(QComboBox, "termbaseTargetColumn")
            header = prompt.findChild(QCheckBox, "termbaseFirstRowHeader")
            buttons = prompt.findChild(QDialogButtonBox, "termbaseColumnButtons")
            self.assertIsNotNone(source_combo)
            self.assertIsNotNone(target_combo)
            self.assertIsNotNone(header)
            self.assertIsNotNone(buttons)
            assert source_combo is not None
            assert target_combo is not None
            assert header is not None
            assert buttons is not None

            self.assertEqual(source_combo.currentData(), 0)
            self.assertEqual(target_combo.currentData(), 1)
            self.assertTrue(preview.legacy_header_detected)
            self.assertTrue(header.isChecked())
            self.assertIn("\u7b2c 1 \u5217", source_combo.itemText(0))
            self.assertTrue(source_combo.itemText(2).endswith("\u2026"))

            target_combo.setCurrentIndex(0)
            self.assertFalse(
                buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
            )
            target_combo.setCurrentIndex(2)
            header.setChecked(False)
            selection = prompt.selection()
            self.assertEqual(selection.source_zero_based_index, 0)
            self.assertEqual(selection.target_zero_based_index, 2)
            self.assertIs(selection.header_mode, TermbaseImportHeaderMode.NO_HEADER)
            self.assertEqual(selection.preview_column_count, len(preview.columns))
            self.assertEqual(selection.preview_source_identity, preview.source_identity)
            prompt.close()
            _settings.close()

    def test_prompted_termbase_import_previews_then_imports_selected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text(
                "Notes,Target,Source\nnote,\u529e\u516c\u5ba4,office\n",
                encoding="utf-8-sig",
            )
            resource = next(
                item for item in controller.list_resources() if item.id == resource_id
            )

            def reordered_selection(
                prompt: TermbaseColumnSelectionDialog,
            ) -> TermbaseImportSelection:
                return TermbaseImportSelection(
                    source_zero_based_index=2,
                    target_zero_based_index=1,
                    header_mode=TermbaseImportHeaderMode.FIRST_ROW,
                    preview_column_count=len(prompt.preview.columns),
                    preview_source_identity=prompt.preview.source_identity,
                )

            with (
                patch.object(
                    QFileDialog,
                    "getOpenFileName",
                    return_value=(str(source), TERMBASE_FILE_FILTER),
                ),
                patch.object(
                    TermbaseColumnSelectionDialog,
                    "exec",
                    return_value=int(QDialog.DialogCode.Accepted),
                ),
                patch.object(
                    TermbaseColumnSelectionDialog,
                    "selection",
                    new=reordered_selection,
                ),
            ):
                dialog._prompt_import(resource)
                preview_worker = dialog.preview_worker
                self.assertIsNotNone(preview_worker)
                assert preview_worker is not None
                self.assertTrue(preview_worker.wait(5000))
                QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
                self.assertTrue(self._wait_for_import(dialog))

            self.assertIsNot(controller.preview_thread, self.app.thread())
            self.assertIsNot(controller.import_thread, self.app.thread())
            self.assertEqual(controller.preview_paths, [source])
            self.assertEqual(len(controller.import_requests), 1)
            selection = controller.import_requests[0].termbase_selection
            self.assertIsNotNone(selection)
            assert selection is not None
            self.assertEqual(selection.source_zero_based_index, 2)
            self.assertEqual(selection.target_zero_based_index, 1)
            report = dialog.last_import_report
            assert report is not None
            self.assertEqual(report.imported, 1)
            self.assertEqual(controller.suggestions().terms[0].source_term, "office")
            dialog.close()

    def test_cancelled_column_dialog_never_starts_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,\u529e\u516c\u5ba4\n", encoding="utf-8-sig")

            with patch.object(
                TermbaseColumnSelectionDialog,
                "exec",
                return_value=int(QDialog.DialogCode.Rejected),
            ):
                self.assertTrue(dialog.start_termbase_preview(resource_id, source))
                self.assertTrue(self._wait_for_preview(dialog))

            self.assertEqual(controller.import_requests, [])
            self.assertIsNone(dialog.last_import_report)
            self.assertFalse(dialog.is_importing)
            dialog.close()

    def test_preview_with_fewer_than_two_columns_fails_without_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "one-column.csv"
            source.write_text("Only\nvalue\n", encoding="utf-8-sig")

            self.assertTrue(dialog.start_termbase_preview(resource_id, source))
            self.assertTrue(self._wait_for_preview(dialog))

            self.assertEqual(controller.import_requests, [])
            self.assertFalse(dialog.is_importing)
            self.assertIn("\u81f3\u5c11 2 \u5217", dialog.import_feedback.text())
            dialog.close()

    def test_unexpected_preview_failure_never_leaks_exception_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,\u529e\u516c\u5ba4\n", encoding="utf-8-sig")

            with patch.object(
                controller,
                "preview_termbase_import",
                side_effect=RuntimeError("SECRET HEADER BODY"),
            ):
                self.assertTrue(dialog.start_termbase_preview(resource_id, source))
                self.assertTrue(self._wait_for_preview(dialog))

            feedback = dialog.import_feedback.text()
            self.assertIn("\u672a\u80fd\u5b89\u5168\u5b8c\u6210", feedback)
            self.assertNotIn("SECRET", feedback)
            self.assertEqual(controller.import_requests, [])
            dialog.close()

    def test_preview_and_import_share_one_busy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dialog, controller, resource_id = self._dialog(root, ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,\u529e\u516c\u5ba4\n", encoding="utf-8-sig")
            release = threading.Event()
            entered = threading.Event()
            original = controller.preview_termbase_import

            def blocked_preview(input_path: Path):
                entered.set()
                release.wait(5)
                return original(input_path)

            with patch.object(
                controller,
                "preview_termbase_import",
                side_effect=blocked_preview,
            ):
                self.assertTrue(dialog.start_termbase_preview(resource_id, source))
                self.assertTrue(entered.wait(2))
                self.assertFalse(dialog.start_import(resource_id, source))
                release.set()
                worker = dialog.preview_worker
                assert worker is not None
                self.assertTrue(worker.wait(5000))
                with patch.object(
                    TermbaseColumnSelectionDialog,
                    "exec",
                    return_value=int(QDialog.DialogCode.Rejected),
                ):
                    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

            self.assertFalse(dialog.is_importing)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
