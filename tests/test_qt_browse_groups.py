from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QWidget,
)
from PySide6.QtTest import QTest

from editor_contracts import (
    BrowseGroupDisplayMode,
    BrowseGroupPreferences,
    EditorProject,
    EditorSegment,
    WorkspaceMode,
)
from editor_controller import EditorController
from qt_browse_group_dialog import (
    BrowseGroupCard,
    BrowseGroupPreview,
    BrowseGroupTurnBar,
    QtBrowseGroupDialog,
)
from qt_editor_window import QtEditorWindow, _localcat_document_icon
from resource_repository import ResourceRepository


class QtBrowseGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_default_threshold_is_strict_per_document_or_logic(self) -> None:
        preferences = BrowseGroupPreferences()

        self.assertEqual(preferences.segments_per_group, 20)
        self.assertEqual(preferences.group_count(100), 5)
        self.assertFalse(preferences.should_show(100))
        self.assertTrue(preferences.should_show(101))
        self.assertTrue(
            BrowseGroupPreferences(
                segments_per_group=200,
                activation_group_threshold=99,
                activation_segment_threshold=100,
            ).should_show(101)
        )
        self.assertFalse(
            BrowseGroupPreferences(enabled=False).should_show(10_000)
        )
        with self.assertRaises(ValueError):
            BrowseGroupPreferences(segments_per_group=25)

    def test_turn_bar_supports_auto_collapse_and_fixed_display(self) -> None:
        preferences = BrowseGroupPreferences()
        previews = tuple(
            BrowseGroupPreview(
                ordinal=index + 1,
                total_groups=8,
                start_index=index * 20,
                end_index=(index + 1) * 20,
                source=("A very long source preview " * 12) + str(index),
                target=("A translated preview " * 14) if index == 0 else "",
                issued_identity=index * 20,
                selected=index == 7,
            )
            for index in range(8)
        )
        turn_bar = BrowseGroupTurnBar()
        host = QWidget()
        host_layout = QHBoxLayout(host)
        host_layout.addWidget(turn_bar)
        other_control = QPushButton("other")
        host_layout.addWidget(other_control)
        host.resize(240, 120)
        turn_bar.set_previews(previews, document_name="volume-02.txt")
        host.show()
        other_control.setFocus()
        self._events()

        self.assertEqual(turn_bar.stack.currentIndex(), 0)
        self.assertEqual(turn_bar.maximumWidth(), 42)
        self.assertEqual(len(turn_bar.ticks), 8)
        self.assertGreater(
            turn_bar.indicator_scroll.verticalScrollBar().maximum(),
            0,
        )
        self.assertGreater(
            turn_bar.indicator_scroll.verticalScrollBar().value(),
            0,
        )
        QTest.mouseMove(other_control, QPoint(2, 2))
        QTest.mouseMove(turn_bar.ticks[-1], QPoint(3, 3))
        self._events()
        self.assertTrue(turn_bar._preview_popup.isVisible())
        QTest.mouseMove(other_control, QPoint(2, 2))
        QTest.qWait(180)
        self._events()
        self.assertFalse(turn_bar._preview_popup.isVisible())

        turn_bar.ticks[0].setFocus()
        self._events()
        self.assertTrue(turn_bar._preview_popup.isVisible())
        self.assertEqual(
            turn_bar._preview_popup._card.preview_line_limits,
            (1, 3),
        )
        other_control.setFocus()
        QTest.qWait(180)
        self._events()
        self.assertFalse(turn_bar._preview_popup.isVisible())

        turn_bar.set_display_mode(BrowseGroupDisplayMode.FIXED)
        turn_bar.resize(330, 470)
        self._events()

        self.assertEqual(turn_bar.stack.currentIndex(), 1)
        self.assertGreaterEqual(turn_bar.minimumWidth(), 280)
        self.assertEqual(turn_bar.cards[0].preview_line_limits, (1, 3))
        self.assertEqual(turn_bar.cards[1].preview_line_limits, (4, 0))
        self.assertGreater(
            turn_bar.group_scroll.verticalScrollBar().maximum(),
            0,
        )
        self.assertGreater(
            turn_bar.group_scroll.verticalScrollBar().value(),
            0,
        )
        selected: list[object] = []
        turn_bar.groupSelected.connect(selected.append)
        turn_bar.cards[1].click()
        self.assertEqual(selected, [previews[1].issued_identity])

        host.close()

    def test_dialog_contains_only_settings_and_localized_controls(self) -> None:
        preferences = BrowseGroupPreferences()

        dialog = QtBrowseGroupDialog(
            preferences=preferences,
            document_name="volume-02.txt",
            segment_count=160,
        )
        dialog.show()
        self._events()

        self.assertEqual(dialog.findChildren(BrowseGroupCard), [])
        self.assertEqual(dialog.enabled_checkbox.text(), "启用中")
        self.assertEqual(
            dialog.display_mode_combo.currentData(),
            BrowseGroupDisplayMode.AUTO_COLLAPSE.value,
        )
        dialog.enabled_checkbox.setChecked(False)
        dialog.display_mode_combo.setCurrentIndex(1)
        self.assertEqual(dialog.enabled_checkbox.text(), "不显示")
        self.assertEqual(dialog.cancel_button.text(), "取消")

        dialog.group_size_spin.setValue(25)
        dialog._save_and_accept()
        self.assertEqual(
            dialog.saved_preferences,
            BrowseGroupPreferences(
                enabled=False,
                segments_per_group=30,
                display_mode=BrowseGroupDisplayMode.FIXED,
            ),
        )
        dialog.close()

    def test_browse_header_has_one_entry_and_group_selection_jumps_to_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ResourceRepository(Path(temporary) / "app-data")
            controller = EditorController(repository)
            controller.set_project(
                EditorProject(
                    name="long-document",
                    segments=tuple(
                        EditorSegment(
                            id=f"segment-{index + 1}",
                            source=f"Source sentence {index + 1}",
                            target=(
                                f"Target sentence {index + 1}"
                                if index % 3
                                else ""
                            ),
                        )
                        for index in range(121)
                    ),
                )
            )
            window = QtEditorWindow(controller)
            window.show()
            self.assertTrue(window.set_workspace_mode(WorkspaceMode.BROWSE))
            self._events()

            self.assertEqual(window.browse_group_button.text(), "轮次 1 / 7")
            position_item = window.browse_table.item(37, 0)
            self.assertIsNotNone(position_item)
            assert position_item is not None
            self.assertEqual(position_item.text(), "038")
            self.assertGreaterEqual(
                window.browse_table.columnWidth(0),
                window.browse_table.fontMetrics().horizontalAdvance("038")
                + 40,
            )
            self.assertLessEqual(window.browse_table.columnWidth(0), 90)
            self.assertTrue(window.browse_group_turn_bar.isVisible())
            self.assertEqual(
                len(window.browse_group_turn_bar.ticks),
                7,
            )
            self.assertEqual(
                window.browse_group_turn_bar.stack.currentIndex(),
                0,
            )
            self.assertEqual(
                window.findChild(type(window.browse_group_button), "browseGroupNavigatorButton"),
                window.browse_group_button,
            )
            browse_panel = window.findChild(QFrame, "browsePanel")
            self.assertIsNotNone(browse_panel)
            self.assertEqual(browse_panel.findChildren(QSpinBox), [])

            window.browse_group_turn_bar.ticks[2].click()
            self._events()

            self.assertEqual(controller.current_index, 40)
            self.assertIs(window.workspace_mode, WorkspaceMode.BROWSE)
            self.assertEqual(window.browse_group_button.text(), "轮次 3 / 7")
            window.close()

    def test_document_icon_keeps_dark_pixels_on_white(self) -> None:
        image = _localcat_document_icon(24).pixmap(24, 24).toImage()
        white = QColor("#ffffff")
        contrasts: list[float] = []
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.alpha() == 0:
                    continue
                contrasts.append(self._contrast_ratio(color, white))
        self.assertTrue(contrasts)
        self.assertGreater(max(contrasts), 7.0)
        self.assertGreaterEqual(
            sum(contrast >= 3.0 for contrast in contrasts),
            80,
        )

    @staticmethod
    def _contrast_ratio(first: QColor, second: QColor) -> float:
        def luminance(color: QColor) -> float:
            channels = (color.redF(), color.greenF(), color.blueF())
            linear = tuple(
                channel / 12.92
                if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            )
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
        return (lighter + 0.05) / (darker + 0.05)


if __name__ == "__main__":
    unittest.main()
