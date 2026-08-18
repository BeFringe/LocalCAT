from __future__ import annotations

from collections import Counter
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEventLoop, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionToolButton,
)

from editor_contracts import ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


class QtEditorWindowShellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> QtEditorWindow:
        repository = ResourceRepository(root / "app-data")
        repository.create_resource("Local TM", ResourceKind.TRANSLATION_MEMORY)
        repository.create_resource("Local terms", ResourceKind.TERMBASE)
        return QtEditorWindow(EditorController(repository))

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

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

    @staticmethod
    def _to_device_rect(image: QImage, logical_rect: QRect) -> QRect:
        ratio = image.devicePixelRatio()
        return QRect(
            round(logical_rect.x() * ratio),
            round(logical_rect.y() * ratio),
            round(logical_rect.width() * ratio),
            round(logical_rect.height() * ratio),
        ).intersected(image.rect())

    @classmethod
    def _assert_chevron_visible(cls, image: QImage, logical_rect: QRect) -> None:
        rect = cls._to_device_rect(image, logical_rect).adjusted(4, 5, -4, -5)
        colors = Counter(
            image.pixelColor(x, y).rgba()
            for y in range(rect.top(), rect.bottom() + 1)
            for x in range(rect.left(), rect.right() + 1)
        )
        background = QColor.fromRgba(colors.most_common(1)[0][0])
        glyph = tuple(
            (x, y)
            for y in range(rect.top(), rect.bottom() + 1)
            for x in range(rect.left(), rect.right() + 1)
            if cls._contrast_ratio(image.pixelColor(x, y), background) >= 3.0
        )
        glyph_width = max((x for x, _y in glyph), default=0) - min(
            (x for x, _y in glyph),
            default=0,
        )
        glyph_height = max((y for _x, y in glyph), default=0) - min(
            (y for _x, y in glyph),
            default=0,
        )
        if len(glyph) < 8 or glyph_width < 6 or glyph_height < 3:
            raise AssertionError(
                "top-bar arrow has no readable chevron glyph "
                f"(pixels={len(glyph)}, span={glyph_width}x{glyph_height})"
            )

    @staticmethod
    def _mode_arrow_rect(window: QtEditorWindow) -> QRect:
        combo = window.workspace_mode_combo
        option = QStyleOptionComboBox()
        option.initFrom(combo)
        option.rect = combo.rect()
        option.currentText = combo.currentText()
        return combo.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            combo,
        )

    @staticmethod
    def _project_menu_rect(window: QtEditorWindow) -> QRect:
        button = window.open_button
        option = QStyleOptionToolButton()
        option.initFrom(button)
        option.rect = button.rect()
        option.features = (
            QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup
            | QStyleOptionToolButton.ToolButtonFeature.HasMenu
        )
        return button.style().subControlRect(
            QStyle.ComplexControl.CC_ToolButton,
            option,
            QStyle.SubControl.SC_ToolButtonMenu,
            button,
        )

    def _schedule_message_box_click(
        self,
        standard_button: QMessageBox.StandardButton,
        clicked: list[str],
    ) -> None:
        def click_button() -> None:
            for widget in QApplication.topLevelWidgets():
                if not isinstance(widget, QMessageBox):
                    continue
                button = widget.button(standard_button)
                if button is not None:
                    clicked.append(button.text())
                    button.click()
                    return

        QTimer.singleShot(25, click_button)

    def test_empty_state_and_sample_reach_first_editor_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.show()
            self._events()

            self.assertEqual(window.pages.currentWidget().objectName(), "emptyPage")
            self.assertTrue(window.open_button.isVisible())
            self.assertTrue(window.settings_button.isVisible())

            window.load_sample()
            self._events()

            self.assertEqual(window.pages.currentWidget().objectName(), "editorPage")
            self.assertEqual(window.segment_list.count(), 3)
            self.assertEqual(window.project_name_label.text(), "LocalCAT Welcome")
            self.assertEqual(window.language_label.text(), "en-US  →  zh-CN")
            window.close()

    def test_top_bar_arrows_render_and_keep_compact_hit_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.resize(1180, 680)
            window.show()
            self._events()

            mode = window.workspace_mode_combo
            disabled_rect = self._mode_arrow_rect(window)
            disabled_image = mode.grab().toImage()
            self.assertFalse(mode.isEnabled())
            self._assert_chevron_visible(disabled_image, disabled_rect)

            window.load_sample()
            self._events()
            enabled_rect = self._mode_arrow_rect(window)
            enabled_image = mode.grab().toImage()
            self.assertTrue(mode.isEnabled())
            self._assert_chevron_visible(enabled_image, enabled_rect)
            self.assertGreaterEqual(enabled_rect.width(), 24)
            self.assertTrue(mode.accessibleName())

            project_rect = self._project_menu_rect(window)
            project_image = window.open_button.grab().toImage()
            self.assertGreaterEqual(project_rect.width(), 24)
            self._assert_chevron_visible(project_image, project_rect)
            self.assertLessEqual(window.open_button.width(), 96)
            self.assertTrue(window.open_button.accessibleName())
            window._confirm_unsaved = lambda: True
            window.close()

    def test_project_button_preserves_main_action_and_opens_existing_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.show()
            self._events()
            button = window.open_button
            menu_rect = self._project_menu_rect(window)
            main_rect = button.rect().adjusted(0, 0, -menu_rect.width(), 0)

            with patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=("", ""),
            ) as choose:
                QTest.mouseClick(
                    button,
                    Qt.MouseButton.LeftButton,
                    pos=main_rect.center(),
                )
                button.setFocus()
                QTest.keyClick(button, Qt.Key.Key_Space)
                QTest.keyClick(button, Qt.Key.Key_Return)
                QTest.keyClick(button, Qt.Key.Key_Enter)
            self.assertEqual(choose.call_count, 4)

            opened: list[bool] = []

            def close_existing_menu() -> None:
                opened.append(True)
                QTimer.singleShot(0, window.project_menu.close)

            window.project_menu.aboutToShow.connect(close_existing_menu)
            QTest.mouseClick(
                button,
                Qt.MouseButton.LeftButton,
                pos=menu_rect.center(),
            )
            button.setFocus()
            QTest.keyClick(
                button,
                Qt.Key.Key_Down,
                Qt.KeyboardModifier.AltModifier,
            )
            self._events()
            self.assertEqual(opened, [True, True])
            window.close()

    def test_open_edit_and_save_json_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "input.json"
            save_path = root / "saved.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Client project",
                        "source_locale": "en-US",
                        "target_locale": "zh-CN",
                        "segments": [
                            {"id": "1", "source": "Hello"},
                            {"id": "2", "source": "World"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            window = self._window(root)

            self.assertTrue(window.open_project_path(project_path))
            window.target_editor.setPlainText("你好")
            self._events()
            self.assertTrue(window.controller.dirty)
            self.assertTrue(window.save_project_path(save_path))
            payload = json.loads(save_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["segments"][0]["target"], "你好")
            self.assertFalse(window.controller.dirty)
            self.assertIn("Client project", window.windowTitle())
            self.assertIn("已保存", window.statusBar().currentMessage())
            window.close()

    def test_invalid_open_and_cancelled_unsaved_guard_preserve_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.txt"
            second = root / "second.txt"
            invalid = root / "invalid.json"
            first.write_text("First source\n", encoding="utf-8")
            second.write_text("Second source\n", encoding="utf-8")
            invalid.write_text("{not-json", encoding="utf-8")
            window = self._window(root)
            errors: list[str] = []
            window._show_error = lambda title, message: errors.append(message)

            self.assertTrue(window.open_project_path(first))
            self.assertFalse(window.open_project_path(invalid))
            self.assertEqual(window.controller.current_segment.source, "First source")
            self.assertTrue(errors)
            window.target_editor.setPlainText("未保存")
            self._events()
            window._confirm_unsaved = lambda: False

            self.assertFalse(window.open_project_path(second))
            self.assertEqual(window.controller.current_segment.source, "First source")
            window.controller.save_project(root / "cleanup.json")
            window.close()

    def test_discard_button_closes_dirty_project_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.load_sample()
            window.target_editor.setPlainText("未保存")
            self._events()
            clicked: list[str] = []

            self._schedule_message_box_click(
                QMessageBox.StandardButton.Discard,
                clicked,
            )
            self.assertTrue(window.close_current_project())
            self.assertFalse(window.controller.has_project)

            window.load_sample()
            window.target_editor.setPlainText("再次未保存")
            window.show()
            self._events()
            self._schedule_message_box_click(
                QMessageBox.StandardButton.Discard,
                clicked,
            )
            self.assertTrue(window.close())
            self._events()

            self.assertEqual(len(clicked), 2)
            self.assertFalse(window.isVisible())

    def test_three_columns_keep_usable_sizes_after_resize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.load_sample()
            window.resize(1280, 760)
            window.show()
            self._events()
            sizes = window.main_splitter.sizes()

            self.assertEqual(len(sizes), 3)
            self.assertGreaterEqual(sizes[0], 200)
            self.assertGreaterEqual(sizes[1], 360)
            self.assertGreaterEqual(sizes[2], 250)
            self.assertEqual(window.main_splitter.stretchFactor(0), 2)
            self.assertEqual(window.main_splitter.stretchFactor(1), 5)
            self.assertEqual(window.main_splitter.stretchFactor(2), 3)
            window.close()

    def test_project_menu_lists_recent_and_can_exit_to_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "recent.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Recent",
                        "segments": [
                            {"id": "1", "source": "First"},
                            {"id": "2", "source": "Second"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            window = self._window(root)
            self.assertTrue(window.open_project_path(project_path))
            window.controller.go_to(1)
            window.refresh_recent_projects()

            actions = window.recent_projects_menu.actions()
            self.assertEqual(len(actions), 1)
            self.assertIn("recent.json", actions[0].text())
            self.assertEqual(Path(actions[0].data()), project_path.resolve())

            window.target_editor.setPlainText("未保存")
            self._events()
            window._confirm_unsaved = lambda: False
            self.assertFalse(window.close_current_project())
            self.assertTrue(window.controller.has_project)
            window._confirm_unsaved = lambda: True
            self.assertTrue(window.close_current_project())

            self.assertFalse(window.controller.has_project)
            self.assertEqual(window.pages.currentWidget().objectName(), "emptyPage")
            self.assertFalse(window.save_button.isEnabled())
            self.assertFalse(window.close_project_action.isEnabled())
            window.close()

    def test_missing_recent_project_is_pruned_with_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_path = root / "gone.txt"
            project_path.write_text("Temporary project\n", encoding="utf-8")
            window = self._window(root)
            self.assertTrue(window.open_project_path(project_path))
            self.assertTrue(window.close_current_project())
            project_path.unlink()
            errors: list[str] = []
            window._show_error = lambda title, message: errors.append(message)

            self.assertFalse(window.open_recent_project(project_path))

            self.assertTrue(errors)
            self.assertIn("不存在", errors[0])
            self.assertEqual(window.recent_projects_menu.actions()[0].text(), "暂无最近项目")
            window.close()


if __name__ == "__main__":
    unittest.main()
