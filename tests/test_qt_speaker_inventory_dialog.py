from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QHeaderView

from editor_contracts import (
    EditorProject,
    EditorSegment,
    SpeakerInventory,
    SpeakerInventoryItem,
)
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from qt_speaker_avatar import SpeakerAvatarCatalog
from qt_speaker_inventory_dialog import QtSpeakerInventoryDialog
from resource_repository import ResourceRepository


class _InventoryController:
    def __init__(self, inventory: SpeakerInventory) -> None:
        self.inventory = inventory
        self.calls = 0

    def speaker_inventory(self) -> SpeakerInventory:
        self.calls += 1
        return self.inventory


class QtSpeakerInventoryDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _inventory(self) -> SpeakerInventory:
        return SpeakerInventory(
            items=(
                SpeakerInventoryItem("adela", 2, "one", 0),
                SpeakerInventoryItem("Unknown", 1, "three", 2),
            ),
            empty_count=1,
            segment_count=4,
        )

    def test_dialog_uses_controller_inventory_and_inventory_only_avatars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            avatar = QImage(16, 16, QImage.Format.Format_ARGB32)
            avatar.fill(0xFF336699)
            self.assertTrue(avatar.save(str(root / "adelaHalf.png")))

            controller = _InventoryController(self._inventory())
            dialog = QtSpeakerInventoryDialog(  # type: ignore[arg-type]
                controller,
                avatar_catalog=SpeakerAvatarCatalog(root),
            )
            self.addCleanup(dialog.close)

            self.assertEqual(controller.calls, 1)
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.table.item(0, 1).text(), "adela")
            self.assertEqual(dialog.table.item(0, 2).text(), "2")
            matched_avatar = dialog.table.cellWidget(0, 0)
            missing_avatar = dialog.table.cellWidget(1, 0)
            self.assertIsNotNone(matched_avatar.pixmap())
            self.assertLessEqual(matched_avatar.pixmap().width(), 48)
            self.assertLessEqual(matched_avatar.pixmap().height(), 48)
            self.assertIn("内置 speaker 头像", matched_avatar.accessibleName())
            self.assertEqual(missing_avatar.text(), "—")
            self.assertIn("无内置头像", missing_avatar.accessibleName())
            self.assertIn("1 段无 speaker", dialog.summary_label.text())

    def test_count_column_keeps_complete_header_visible_at_minimum_size(self) -> None:
        dialog = QtSpeakerInventoryDialog(  # type: ignore[arg-type]
            _InventoryController(self._inventory())
        )
        self.addCleanup(dialog.close)
        dialog.resize(dialog.minimumSize())
        dialog.show()
        QApplication.processEvents()

        header = dialog.table.horizontalHeader()
        required = header.fontMetrics().horizontalAdvance("出现次数") + 32
        self.assertEqual(
            header.sectionResizeMode(2),
            QHeaderView.ResizeMode.Fixed,
        )
        self.assertGreaterEqual(dialog.table.columnWidth(2), required)
        self.assertEqual(dialog.table.horizontalHeaderItem(2).text(), "出现次数")

    def test_catalog_is_casefolded_and_fails_closed_for_collision_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = QImage(16, 16, QImage.Format.Format_ARGB32)
            valid.fill(0xFF336699)
            self.assertTrue(valid.save(str(root / "AliceHalf.png"), "PNG"))
            catalog = SpeakerAvatarCatalog(root)
            self.assertIsNotNone(catalog.avatar_pixmap("alice"))
            self.assertIsNone(catalog.avatar_pixmap("../Alice"))

            collided = SpeakerAvatarCatalog(root)
            collided._paths["alice"] = None
            self.assertIsNone(collided.avatar_pixmap("ALICE"))

            (root / "BrokenHalf.png").write_bytes(b"not an image")
            invalid = SpeakerAvatarCatalog(root)
            self.assertIsNone(invalid.avatar_pixmap("Broken"))

    def test_empty_inventory_has_explicit_empty_state(self) -> None:
        controller = _InventoryController(
            SpeakerInventory(items=(), empty_count=2, segment_count=2)
        )
        dialog = QtSpeakerInventoryDialog(controller)  # type: ignore[arg-type]
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertTrue(dialog.empty_label.isVisible() or not dialog.isVisible())
        self.assertIn("没有非空", dialog.empty_label.text())

    def test_main_window_entry_uses_single_json_gate_and_remains_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = EditorController(ResourceRepository(root / "data"))
            project_path = root / "project.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Window inventory",
                        "segments": [
                            {
                                "id": "one",
                                "source": "Source",
                                "speaker": "Adela",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller.open_project(project_path)
            window = QtEditorWindow(controller)
            self.addCleanup(window.close)
            project_before = controller.project
            index_before = controller.current_index
            dirty_before = controller.dirty

            self.assertTrue(window.speaker_inventory_action.isEnabled())
            self.assertEqual(
                window.speaker_inventory_action.text(),
                "Raw speaker 盘点",
            )
            with patch.object(
                QtSpeakerInventoryDialog,
                "exec",
                autospec=True,
                return_value=0,
            ) as execute:
                window._open_speaker_inventory_dialog()

            self.assertEqual(execute.call_count, 1)
            self.assertIs(controller.project, project_before)
            self.assertEqual(controller.current_index, index_before)
            self.assertEqual(controller.dirty, dirty_before)
            self.assertFalse(
                any(
                    "avatar" in child.objectName().casefold()
                    for child in (window.speaker_display, window.browse_table)
                )
            )

            controller.set_project(
                EditorProject(
                    name="Text boundary",
                    path=root / "notes.txt",
                    segments=(EditorSegment(id="one", source="Source"),),
                )
            )
            window._render_project()
            self.assertFalse(window.speaker_inventory_action.isEnabled())


if __name__ == "__main__":
    unittest.main()
