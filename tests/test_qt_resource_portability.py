from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QLineEdit, QPushButton, QToolButton

from editor_contracts import ResourceKind
from editor_controller import EditorController, EditorControllerError
from qt_settings_dialog import (
    QtSettingsDialog,
    ResourcePackageApplyDialog,
    ResourcePackageImportOptionsDialog,
)
from resource_package_contracts import ResourceImportMode
from resource_package_contracts import (
    ResourcePackageValidationReport,
    ResourcePayloadProfile,
)
from resource_repository import ResourceRepository


class QtResourcePortabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        current = QApplication.instance()
        cls.app = current if isinstance(current, QApplication) else QApplication([])

    def test_resource_menus_and_global_import_entry_are_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            tm = repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
            terms = repository.create_resource("Terms", ResourceKind.TERMBASE)
            dialog = QtSettingsDialog(EditorController(repository))
            tm_menu = dialog.findChild(QToolButton, f"more_{tm.id}").menu()
            term_menu = dialog.findChild(QToolButton, f"more_{terms.id}").menu()
            self.assertIn("导出兼容 JSONL", [action.text() for action in tm_menu.actions()])
            self.assertIn("导出 TMX", [action.text() for action in tm_menu.actions()])
            self.assertIn("导出 CSV/v1", [action.text() for action in term_menu.actions()])
            self.assertIn("导出资源包", [action.text() for action in tm_menu.actions()])
            self.assertTrue(dialog.resource_package_button.isEnabled())
            self.assertEqual(dialog.resource_package_button.text(), "导入资源包")
            dialog.close()

    def test_import_options_and_sealed_apply_dialog_keep_create_replace_boundaries_clear(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            source = repository.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(b"\xef\xbb\xbfhello,world\n")
            controller = EditorController(repository)
            package = root / "terms.localcat-resource"
            controller.export_resource_package(source.id, package)
            validation = controller.validate_resource_package(package)
            options = ResourcePackageImportOptionsDialog(
                validation,
                controller.list_resources(),
            )
            self.assertIs(
                ResourceImportMode(
                    options.findChild(QComboBox, "resourcePackageImportMode").currentData()
                ),
                ResourceImportMode.CREATE_NEW,
            )
            self.assertFalse(
                options.findChild(QLineEdit, "resourcePackageNewResourceName").isHidden()
            )
            mode_combo = options.findChild(QComboBox, "resourcePackageImportMode")
            mode_combo.setCurrentIndex(1)
            self.assertIs(
                ResourceImportMode(mode_combo.currentData()),
                ResourceImportMode.REPLACE_SELECTED,
            )
            preview = controller.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=source.id,
            )
            apply_dialog = ResourcePackageApplyDialog(preview, source.name)
            self.assertEqual(apply_dialog.windowTitle(), "预览并导入 ResourcePackage")
            controller.cancel_resource_package_import(preview)
            apply_dialog.close()
            options.close()

    def test_pending_import_recovery_is_visible_on_resource_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            source = repository.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(b"\xef\xbb\xbfhello,world\n")
            controller = EditorController(repository)
            package = root / "terms.localcat-resource"
            controller.export_resource_package(source.id, package)
            preview = controller.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
            )
            with patch.object(
                controller,
                "_reload_resources_after_persisted_mutation",
                side_effect=EditorControllerError("injected reload failure"),
            ):
                with self.assertRaises(EditorControllerError):
                    controller.apply_resource_package_import(preview)

            dialog = QtSettingsDialog(controller)
            panel = dialog.findChild(QFrame, "resourceRecoveryPanel")
            self.assertIsNotNone(panel)
            self.assertFalse(panel.isHidden())
            buttons = [
                button
                for button in panel.findChildren(QPushButton)
                if button.objectName().startswith("recoverResource_")
            ]
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0].text(), "完成恢复")
            dialog.close()

    def test_termbase_package_export_executes_operation_not_nested_lambda(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            terms = repository.create_resource("Terms", ResourceKind.TERMBASE)
            terms.path.write_bytes(b"\xef\xbb\xbfhello,world\n")
            controller = EditorController(repository)
            dialog = QtSettingsDialog(controller)
            destination = root / "terms-export"
            with (
                patch(
                    "qt_settings_dialog.QFileDialog.getSaveFileName",
                    return_value=(str(destination), ""),
                ),
                patch.object(dialog, "_start_portability_operation") as start,
            ):
                dialog._prompt_export_package(terms)
            operation = start.call_args.args[0]
            outcome = operation()
            self.assertEqual(outcome.receipt.record_count, 1)
            self.assertTrue(
                destination.with_name(
                    f"{destination.name}.localcat-resource"
                ).is_file()
            )
            dialog.close()

    def test_tmx_resource_package_validation_explains_export_only_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            dialog = QtSettingsDialog(EditorController(ResourceRepository(root / "app")))
            report = object.__new__(ResourcePackageValidationReport)
            object.__setattr__(
                report,
                "payload_profile",
                ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )
            with patch.object(dialog, "_show_import_feedback") as feedback:
                dialog._after_resource_package_validation(
                    root / "tm.localcat-resource",
                    report,
                    None,
                )
            self.assertIn("仅支持导出", feedback.call_args.args[0])
            self.assertIn("不能导入 LocalCAT", feedback.call_args.args[0])
            self.assertTrue(feedback.call_args.kwargs["failed"])
            dialog.close()


if __name__ == "__main__":
    unittest.main()
