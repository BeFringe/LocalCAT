from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHeaderView,
    QMessageBox,
    QSizePolicy,
    QToolButton,
)

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

    def test_more_button_is_compact_accessible_and_action_column_does_not_stretch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured
                for configured in controller.list_resources()
                if configured.name == "Primary TM"
            )
            table = dialog.active_table
            button = dialog.findChild(QToolButton, f"more_{resource.id}")

            self.assertIsNotNone(button)
            self.assertTrue(button.autoRaise())
            self.assertEqual(
                button.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Fixed,
            )
            self.assertGreaterEqual(button.width(), 32)
            self.assertLessEqual(button.width(), 40)
            self.assertEqual(
                button.width(),
                min(40, max(32, button.sizeHint().width() + 8)),
            )
            self.assertEqual(
                table.horizontalHeader().sectionResizeMode(7),
                QHeaderView.ResizeMode.ResizeToContents,
            )
            self.assertEqual(
                table.horizontalHeader().sectionResizeMode(6),
                QHeaderView.ResizeMode.Fixed,
            )
            self.assertEqual(
                table.horizontalHeader().sectionResizeMode(3),
                QHeaderView.ResizeMode.Stretch,
            )
            self.assertEqual(
                table.horizontalHeader().sectionResizeMode(5),
                QHeaderView.ResizeMode.Stretch,
            )
            self.assertEqual(button.toolTip(), "Primary TM 的更多操作")
            self.assertEqual(button.accessibleName(), "Primary TM 的更多操作")
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
            dialog.close()

    def test_more_button_stays_visible_without_overlap_at_narrow_and_wide_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured
                for configured in controller.list_resources()
                if configured.name == "Primary TM"
            )
            table = dialog.active_table
            button = dialog.findChild(QToolButton, f"more_{resource.id}")

            dialog.show()
            for width in (860, 1320):
                dialog.resize(width, 560)
                self.app.processEvents()
                cell_left = table.columnViewportPosition(7)
                cell_right = cell_left + table.columnWidth(7)
                button_left = button.geometry().left()
                button_right = button.geometry().right() + 1
                import_right = (
                    table.columnViewportPosition(6) + table.columnWidth(6)
                )

                self.assertTrue(button.isVisible())
                self.assertGreaterEqual(cell_left, import_right)
                self.assertGreaterEqual(button_left, cell_left)
                self.assertLessEqual(button_right, cell_right)
                self.assertLessEqual(cell_right, table.viewport().width())

            dialog.close()

    def test_more_button_opens_its_menu_with_pointer_enter_and_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured
                for configured in controller.list_resources()
                if configured.name == "Primary TM"
            )
            button = dialog.findChild(QToolButton, f"more_{resource.id}")
            menu = button.menu()
            opened: list[bool] = []
            menu.aboutToShow.connect(lambda: opened.append(True))
            dialog.show()
            self.app.processEvents()

            button.setFocus()
            self.app.processEvents()
            self.assertTrue(button.hasFocus())
            close_timer = QTimer(menu)
            close_timer.setSingleShot(True)
            close_timer.timeout.connect(menu.close)
            close_timer.start(25)
            QTest.keyClick(button, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(len(opened), 1)

            close_timer.start(25)
            QTest.keyClick(button, Qt.Key.Key_Space)
            self.app.processEvents()
            self.assertEqual(len(opened), 2)

            close_timer.start(25)
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertEqual(len(opened), 3)
            dialog.close()

    def test_more_menu_confirms_and_deletes_managed_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            resource = controller.create_resource("Delete me", ResourceKind.TERMBASE)
            dialog = QtSettingsDialog(controller)
            more_button = dialog.findChild(QToolButton, f"more_{resource.id}")

            self.assertIsNotNone(more_button)
            delete_action = next(
                action for action in more_button.menu().actions() if action.text() == "删除资源"
            )
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                delete_action.trigger()

            self.assertNotIn(
                resource.id,
                {configured.id for configured in controller.list_resources()},
            )
            self.assertFalse(resource.path.exists())
            dialog.close()

    def test_cancelled_delete_keeps_resource_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            resource = controller.create_resource("Keep me", ResourceKind.TERMBASE)
            dialog = QtSettingsDialog(controller)
            more_button = dialog.findChild(QToolButton, f"more_{resource.id}")
            delete_action = next(
                action for action in more_button.menu().actions() if action.text() == "删除资源"
            )

            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Cancel,
            ):
                delete_action.trigger()

            self.assertIn(
                resource.id,
                {configured.id for configured in controller.list_resources()},
            )
            self.assertTrue(resource.path.exists())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
