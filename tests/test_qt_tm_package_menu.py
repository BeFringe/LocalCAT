from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import tempfile
from typing import ClassVar
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QWidget

from editor_contracts import ResourceConfig, ResourceKind
from editor_controller import (
    EditorController,
    EditorControllerError,
    _initial_tm_activation_service,
)
from qt_settings_dialog import QtSettingsDialog, ResourcePortabilityWorker
from resource_package_contracts import ResourceImportMode, ResourcePayloadProfile
from resource_repository import ResourceRepository
from tm_contracts import MigrationReport


class QtTmPackageMenuTests(unittest.TestCase):
    app: ClassVar[QApplication]

    @classmethod
    def setUpClass(cls) -> None:
        current = QApplication.instance()
        cls.app = current if isinstance(current, QApplication) else QApplication([])

    def _canonical_resource(self, root: Path) -> tuple[ResourceRepository, ResourceConfig]:
        repository = ResourceRepository(root / "app")
        resource = repository.create_resource("Canonical", ResourceKind.TRANSLATION_MEMORY)
        _ = resource.path.write_text(
            '{"source":"Hello","target":"你好"}\n'
            '{"source":"Goodbye","target":"再见"}\n',
            encoding="utf-8",
        )
        outcome = _initial_tm_activation_service(resource).activate_initial(
            resource.path, resource.id,
        )
        self.assertIs(type(outcome), MigrationReport)
        return repository, resource

    def _package_action(self, dialog: QtSettingsDialog, resource: ResourceConfig) -> QAction:
        action = dialog.findChild(QAction, f"exportPackage_{resource.id}")
        if not isinstance(action, QAction):
            raise AssertionError("资源菜单缺少导出资源包 QAction")
        return action

    def _trigger_format(
        self,
        dialog: QtSettingsDialog,
        resource: ResourceConfig,
        destination: Path | None,
        *,
        tmx: bool = False,
        cancel_format: bool = False,
    ) -> None:
        errors: list[Exception] = []
        visited: list[bool] = []

        def choose() -> None:
            modal = QApplication.activeModalWidget()
            if not isinstance(modal, QDialog):
                errors.append(AssertionError("未打开资源包格式窗口"))
                return
            try:
                visited.append(True)
                combo = modal.findChild(QComboBox, "tmPackageFormat")
                if not isinstance(combo, QComboBox):
                    raise AssertionError("TM 资源包缺少可导入 JSONL 格式选择")
                self.assertEqual(combo.currentData(), "jsonl")
                self.assertEqual(combo.currentText(), "LocalCAT JSONL（可导入）")
                locales = modal.findChild(QWidget, "tmPackageLocales")
                if not isinstance(locales, QWidget):
                    raise AssertionError("缺少 TMX 语言对控件组")
                self.assertFalse(locales.isVisible())
                if tmx:
                    combo.setCurrentIndex(combo.findData("tmx"))
                    self.assertEqual(combo.currentText(), "TMX（仅导出）")
                    self.assertTrue(locales.isVisible())
                    source = modal.findChild(QLineEdit, "tmxPackageSourceLocale")
                    target = modal.findChild(QLineEdit, "tmxPackageTargetLocale")
                    if not isinstance(source, QLineEdit) or not isinstance(target, QLineEdit):
                        raise AssertionError("缺少 TMX 源语言或目标语言输入")
                    source.setText("en")
                    target.setText("zh-CN")
                modal.reject() if cancel_format else modal.accept()
            except Exception as error:
                errors.append(error)
                modal.reject()

        action = self._package_action(dialog, resource)
        self.assertTrue(action.isEnabled())
        with patch(
            "qt_settings_dialog.QFileDialog.getSaveFileName",
            return_value=(str(destination) if destination is not None else "", ""),
        ) as save:
            QTimer.singleShot(0, choose)
            action.trigger()
            self.assertEqual(save.call_count, 0 if cancel_format or errors else 1)
        if errors:
            raise errors[0]
        self.assertEqual(visited, [True])

    def _wait_for_worker(self, dialog: QtSettingsDialog) -> None:
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        _ = poll.timeout.connect(lambda: None if dialog.is_importing else loop.quit())
        deadline = QTimer()
        deadline.setSingleShot(True)
        _ = deadline.timeout.connect(loop.quit)
        poll.start()
        deadline.start(20_000)
        try:
            if dialog.is_importing:
                _ = loop.exec()
        finally:
            poll.stop()
            deadline.stop()
        self.assertFalse(dialog.is_importing)
        self.assertIsNone(dialog.portability_worker)
        self.assertTrue(dialog.close_button.isEnabled())

    def _with_tm(self, run: Callable[[Path, ResourceConfig, QtSettingsDialog], None]) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._canonical_resource(root)
            dialog = QtSettingsDialog(EditorController(repository))
            try:
                run(root, resource, dialog)
            finally:
                self._wait_for_worker(dialog)
                _ = dialog.close()

    def test_default_jsonl_menu_worker_cold_create_and_records(self) -> None:
        def run(root: Path, resource: ResourceConfig, dialog: QtSettingsDialog) -> None:
            original_status = dialog.status_label.text()
            self._trigger_format(dialog, resource, root / "backup")
            self.assertIsInstance(dialog.portability_worker, ResourcePortabilityWorker)
            self.assertTrue(dialog.is_importing)
            self.assertFalse(dialog.close_button.isEnabled())
            self.assertFalse(dialog.active_table.isEnabled())
            self.assertFalse(dialog.resource_package_button.isEnabled())
            self.assertFalse(dialog._start_portability_operation(
                lambda: self.fail("忙碌时不得开始第二个导出"),
                "第二个导出", lambda _result, _error: None,
            ))
            self._wait_for_worker(dialog)
            self.assertEqual(dialog.status_label.text(), original_status)
            self.assertFalse(dialog.import_feedback.property("failed"))
            self.assertIn("ResourcePackage已导出 · 2", dialog.import_feedback.text())
            package = root / "backup.localcat-resource"
            cold = EditorController(ResourceRepository(root / "imported"))
            validation = cold.validate_resource_package(package)
            self.assertIs(validation.payload_profile, ResourcePayloadProfile.TM_JSONL_V1)
            self.assertEqual(validation.record_count, 2)
            preview = cold.preview_resource_package_import(
                package, ResourceImportMode.CREATE_NEW, new_resource_name="Restored",
            )
            result = cold.apply_resource_package_import(preview)
            reopened = EditorController(ResourceRepository(root / "imported"))
            self.assertIn(result.destination_resource_id,
                          {item.id for item in reopened.list_resources()})
            exported = root / "restored.jsonl"
            outcome = reopened.export_resource_direct(result.destination_resource_id, exported)
            self.assertEqual(outcome.receipt.record_count, 2)
            records = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({(row["source"], row["target"]) for row in records},
                             {("Hello", "你好"), ("Goodbye", "再见")})
        self._with_tm(run)

    def test_explicit_tmx_menu_worker_cold_validate_remains_export_only(self) -> None:
        def run(root: Path, resource: ResourceConfig, dialog: QtSettingsDialog) -> None:
            package = root / "backup.localcat-resource"
            self._trigger_format(dialog, resource, package, tmx=True)
            self.assertIsInstance(dialog.portability_worker, ResourcePortabilityWorker)
            self._wait_for_worker(dialog)
            self.assertFalse(dialog.import_feedback.property("failed"))
            cold = EditorController(ResourceRepository(root / "imported"))
            validation = cold.validate_resource_package(package)
            self.assertIs(validation.payload_profile, ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1)
            self.assertEqual(validation.record_count, 2)
            self.assertFalse(cold.resource_package_import_supported(validation))
            with self.assertRaisesRegex(EditorControllerError, "RESOURCE.IMPORT.PROFILE_UNSUPPORTED"):
                _ = cold.preview_resource_package_import(package, ResourceImportMode.CREATE_NEW)
            self.assertEqual(cold.list_resources(), ())
        self._with_tm(run)

    def test_cancel_format_or_path_does_not_start_export(self) -> None:
        for cancel_format in (True, False):
            with self.subTest(cancel_format=cancel_format):
                def run(root: Path, resource: ResourceConfig, dialog: QtSettingsDialog) -> None:
                    original_status = dialog.status_label.text()
                    self._trigger_format(dialog, resource, None, cancel_format=cancel_format)
                    self.assertIsNone(dialog.portability_worker)
                    self.assertFalse(dialog.is_importing)
                    self.assertEqual(dialog.status_label.text(), original_status)
                    self.assertEqual(tuple(root.glob("*.localcat-resource")), ())
                self._with_tm(run)

    def test_real_export_failure_restores_interaction(self) -> None:
        def run(root: Path, resource: ResourceConfig, dialog: QtSettingsDialog) -> None:
            original_status = dialog.status_label.text()
            destination = root / "directory.localcat-resource"
            destination.mkdir()
            self._trigger_format(dialog, resource, destination)
            self._wait_for_worker(dialog)
            self.assertTrue(destination.is_dir())
            self.assertTrue(dialog.import_feedback.property("failed"))
            self.assertIn("导出未完成", dialog.import_feedback.text())
            self.assertEqual(dialog.status_label.text(), original_status)
        self._with_tm(run)

    def test_legacy_tm_package_stays_disabled_and_direct_tmx_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = ResourceRepository(Path(raw).resolve() / "app")
            resource = repository.create_resource("Legacy", ResourceKind.TRANSLATION_MEMORY)
            dialog = QtSettingsDialog(EditorController(repository))
            try:
                self.assertFalse(self._package_action(dialog, resource).isEnabled())
                direct = dialog.findChild(QAction, f"exportTmx_{resource.id}")
                if not isinstance(direct, QAction):
                    raise AssertionError("独立导出 TMX 入口丢失")
                self.assertEqual(direct.text(), "导出 TMX")
            finally:
                _ = dialog.close()

    def test_termbase_menu_uses_original_worker_without_format_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            _ = resource.path.write_bytes(b"\xef\xbb\xbfhello,world\n")
            dialog = QtSettingsDialog(EditorController(repository))
            package = root / "terms.localcat-resource"
            try:
                with patch("qt_settings_dialog.QFileDialog.getSaveFileName",
                           return_value=(str(package), "")):
                    self._package_action(dialog, resource).trigger()
                self.assertIsInstance(dialog.portability_worker, ResourcePortabilityWorker)
                self._wait_for_worker(dialog)
                cold = EditorController(ResourceRepository(root / "cold"))
                report = cold.validate_resource_package(package)
                self.assertIs(report.payload_profile, ResourcePayloadProfile.TERMBASE_CSV_V1)
                self.assertEqual(report.record_count, 1)
            finally:
                self._wait_for_worker(dialog)
                _ = dialog.close()


if __name__ == "__main__":
    unittest.main()
