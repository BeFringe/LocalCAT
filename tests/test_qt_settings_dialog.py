from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository


class QtSettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _controller(self, root: Path) -> EditorController:
        repository = ResourceRepository(root / "app-data")
        active = repository.create_resource("Primary TM", ResourceKind.TRANSLATION_MEMORY)
        inactive = repository.create_resource("Archive terms", ResourceKind.TERMBASE)
        repository.update_resource(replace(inactive, active=False))
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(name="Keep me", segments=(EditorSegment(id="1", source="Hello"),))
        )
        self.assertTrue(active.active)
        return controller

    def test_groups_active_and_inactive_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)

            self.assertEqual(dialog.active_table.rowCount(), 1)
            self.assertEqual(dialog.inactive_table.rowCount(), 1)
            self.assertEqual(dialog.active_table.item(0, 3).text(), "Primary TM")
            self.assertEqual(dialog.inactive_table.item(0, 3).text(), "Archive terms")
            dialog.close()

    def test_create_and_checkbox_updates_use_controller_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            original_project = controller.project
            dialog = QtSettingsDialog(controller)
            changed_signals: list[bool] = []
            dialog.resources_changed.connect(lambda: changed_signals.append(True))

            created = dialog.create_resource("Client terms", ResourceKind.TERMBASE)
            lookup = dialog.findChild(QCheckBox, f"lookup_{created.id}")
            self.assertIsNotNone(lookup)
            lookup.setChecked(False)
            self.app.processEvents()
            dialog.refresh_resources()

            restored = next(resource for resource in controller.list_resources() if resource.id == created.id)
            reopened = QtSettingsDialog(controller)

            self.assertFalse(restored.lookup)
            self.assertEqual(reopened.active_table.rowCount(), 2)
            self.assertEqual(controller.project, original_project)
            self.assertGreaterEqual(len(changed_signals), 2)
            reopened.close()
            dialog.close()

    def test_active_checkbox_moves_resource_between_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured for configured in controller.list_resources() if configured.name == "Primary TM"
            )
            active = dialog.findChild(QCheckBox, f"active_{resource.id}")

            active.setChecked(False)
            self.app.processEvents()
            dialog.refresh_resources()

            self.assertEqual(dialog.active_table.rowCount(), 0)
            self.assertEqual(dialog.inactive_table.rowCount(), 2)
            self.assertFalse(
                next(item for item in controller.list_resources() if item.id == resource.id).active
            )
            dialog.close()

    def test_qcombobox_payload_creates_both_resource_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            kind_input = QComboBox()
            kind_input.addItem("翻译记忆库", ResourceKind.TRANSLATION_MEMORY)
            kind_input.addItem("术语表", ResourceKind.TERMBASE)

            tm_payload = kind_input.itemData(0)
            terms_payload = kind_input.itemData(1)
            self.assertIsInstance(tm_payload, str)
            self.assertIsInstance(terms_payload, str)

            tm = dialog.create_resource("UI TM", tm_payload)
            terms = dialog.create_resource("UI terms", terms_payload)

            self.assertIs(tm.kind, ResourceKind.TRANSLATION_MEMORY)
            self.assertEqual(tm.path.suffix, ".jsonl")
            self.assertIs(terms.kind, ResourceKind.TERMBASE)
            self.assertEqual(terms.path.suffix, ".csv")
            dialog.close()

    def test_resource_columns_preserve_chinese_actions_and_share_extra_width(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            dialog.resize(900, 560)
            dialog.show()
            self.app.processEvents()
            table = dialog.active_table
            import_button = table.cellWidget(0, 6)
            small_name_width = table.columnWidth(3)
            small_path_width = table.columnWidth(5)

            self.assertGreaterEqual(table.columnWidth(4), 128)
            self.assertGreaterEqual(import_button.width(), import_button.sizeHint().width())

            dialog.resize(1300, 560)
            self.app.processEvents()

            self.assertGreater(table.columnWidth(3), small_name_width)
            self.assertGreater(table.columnWidth(5), small_path_width)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
