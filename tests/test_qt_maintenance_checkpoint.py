from __future__ import annotations

from collections import Counter
from dataclasses import replace
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
)

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository


class QtMaintenanceCheckpointTest(unittest.TestCase):
    """One cumulative user journey for the three Checkpoint M repairs."""

    @classmethod
    def setUpClass(cls) -> None:
        application = QApplication.instance()
        cls.app = (
            application if isinstance(application, QApplication) else QApplication([])
        )

    def _events(self) -> None:
        self.app.processEvents()

    @staticmethod
    def _render_popup_item(
        popup: QAbstractItemView,
        index: QModelIndex,
        state: QStyle.StateFlag,
    ) -> tuple[QImage, QRect]:
        image = QImage(240, 36, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#ffffff"))
        option = QStyleOptionViewItem()
        option.initFrom(popup)
        delegate = popup.itemDelegateForIndex(index)
        if not isinstance(delegate, QStyledItemDelegate):
            raise AssertionError("resource kind popup must use QStyledItemDelegate")
        delegate.initStyleOption(option, index)
        option.rect = image.rect()
        option.state = state
        option.widget = popup
        text_rect = popup.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText,
            option,
            popup,
        ).intersected(image.rect())
        painter = QPainter(image)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()
        return image, text_rect

    @staticmethod
    def _contrast_ratio(first: QColor, second: QColor) -> float:
        def luminance(color: QColor) -> float:
            linear = tuple(
                channel / 12.92
                if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4
                for channel in (color.redF(), color.greenF(), color.blueF())
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
    def _assert_rendered_text_readable(
        cls,
        image: QImage,
        text_rect: QRect,
    ) -> QColor:
        protected_text = text_rect.adjusted(-1, -1, 1, 1)
        background_values = Counter(
            image.pixelColor(x, y).rgba()
            for y in range(3, image.height() - 3)
            for x in range(3, image.width() - 3)
            if not protected_text.contains(x, y)
        )
        if not background_values:
            raise AssertionError("popup render has no background outside its text")
        background = QColor.fromRgba(background_values.most_common(1)[0][0])
        sample_rect = text_rect.intersected(image.rect()).adjusted(1, 1, -1, -1)
        glyph_pixels = tuple(
            (x, y)
            for y in range(sample_rect.top(), sample_rect.bottom() + 1)
            for x in range(sample_rect.left(), sample_rect.right() + 1)
            if cls._contrast_ratio(image.pixelColor(x, y), background) >= 4.5
        )
        if len(glyph_pixels) < 10:
            raise AssertionError("popup text does not have a readable glyph population")
        x_values = tuple(x for x, _y in glyph_pixels)
        y_values = tuple(y for _x, y in glyph_pixels)
        if max(x_values) - min(x_values) < 6 or max(y_values) - min(y_values) < 4:
            raise AssertionError("isolated contrast artifact is not readable text")
        return background

    def test_workspace_mode_combo_renders_distinct_readable_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResourceRepository(Path(temp_dir) / "app-data")
            controller = EditorController(repository)
            controller.set_project(
                EditorProject(
                    name="Workspace mode contrast",
                    segments=(EditorSegment(id="1", source="One"),),
                )
            )
            window = QtEditorWindow(controller)
            window.show()
            self._events()

            mode_combo = window.workspace_mode_combo
            mode_before = mode_combo.currentData()
            preferences_before = controller.display_preferences()
            closed_image = mode_combo.grab().toImage()
            closed_option = QStyleOptionComboBox()
            closed_option.initFrom(mode_combo)
            closed_option.currentText = mode_combo.currentText()
            closed_option.rect = mode_combo.rect()
            closed_text_rect = mode_combo.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox,
                closed_option,
                QStyle.SubControl.SC_ComboBoxEditField,
                mode_combo,
            )
            closed_background = self._assert_rendered_text_readable(
                closed_image,
                self._to_device_rect(closed_image, closed_text_rect),
            )
            mode_combo.showPopup()
            self._events()
            mode_popup = mode_combo.view()
            self.assertEqual(mode_popup.objectName(), "workspaceModePopup")
            mode_states = (
                QStyle.StateFlag.State_Enabled,
                QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver,
                QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected,
            )
            mode_state_images = tuple(
                self._render_popup_item(
                    mode_popup,
                    mode_combo.model().index(0, 0),
                    state,
                )
                for state in mode_states
            )
            mode_combo.hidePopup()
            mode_backgrounds = tuple(
                self._assert_rendered_text_readable(image, text_rect)
                for image, text_rect in mode_state_images
            )
            self.assertEqual(
                len(
                    {
                        closed_background.name(),
                        *(color.name() for color in mode_backgrounds),
                    }
                ),
                4,
            )
            self.assertEqual(
                tuple(mode_combo.itemData(index) for index in range(mode_combo.count())),
                ("edit", "browse"),
            )
            self.assertEqual(mode_combo.currentData(), mode_before)
            self.assertEqual(controller.display_preferences(), preferences_before)

            window._confirm_unsaved = lambda: True
            window.close()

    def test_shortcut_popup_and_writable_termbase_recovery_share_one_journey(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ResourceRepository(root / "app-data")
            repository.create_resource("Writable TM", ResourceKind.TRANSLATION_MEMORY)
            termbase = repository.create_resource("Review terms", ResourceKind.TERMBASE)
            termbase = repository.update_resource(replace(termbase, update=False))
            controller = EditorController(repository)
            controller.set_project(
                EditorProject(
                    name="Maintenance journey",
                    segments=(
                        EditorSegment(id="1", source="One"),
                        EditorSegment(id="2", source="Two"),
                        EditorSegment(id="3", source="Three"),
                    ),
                )
            )
            window = QtEditorWindow(controller)
            window.show()
            window.activateWindow()
            window.target_editor.setFocus()
            window.target_editor.setPlainText("一")
            self._events()

            # Qt's ControlModifier is the platform primary modifier, so this is
            # Command+Return on macOS and Ctrl+Return elsewhere.
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier,
            )
            self._events()
            self.assertTrue(controller.project.segments[0].confirmed)
            self.assertEqual(controller.current_index, 1)

            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Down,
                Qt.KeyboardModifier.AltModifier,
            )
            self._events()
            self.assertEqual(controller.current_index, 2)
            QTest.keyClick(
                window.target_editor,
                Qt.Key.Key_Up,
                Qt.KeyboardModifier.AltModifier,
            )
            self._events()
            self.assertEqual(controller.current_index, 1)

            settings = QtSettingsDialog(controller, window)
            rendered_states: list[tuple[QImage, QRect]] = []

            def inspect_create_prompt(prompt: QDialog) -> QDialog.DialogCode:
                kind_input = prompt.findChild(QComboBox, "newResourceKind")
                if kind_input is None:
                    raise AssertionError("new resource type selector is missing")
                prompt.show()
                kind_input.showPopup()
                self._events()
                popup = kind_input.view()
                states = (
                    QStyle.StateFlag.State_Enabled,
                    QStyle.StateFlag.State_Enabled
                    | QStyle.StateFlag.State_MouseOver,
                    QStyle.StateFlag.State_Enabled
                    | QStyle.StateFlag.State_Selected,
                )
                rendered_states.extend(
                    self._render_popup_item(
                        popup,
                        kind_input.model().index(0, 0),
                        state,
                    )
                    for state in states
                )
                kind_input.hidePopup()
                prompt.close()
                return QDialog.DialogCode.Rejected

            with patch.object(QDialog, "exec", new=inspect_create_prompt):
                settings._prompt_create_resource()

            backgrounds = tuple(
                self._assert_rendered_text_readable(image, text_rect)
                for image, text_rect in rendered_states
            )
            self.assertEqual(len(backgrounds), 3)
            self.assertEqual(len({color.name() for color in backgrounds}), 3)

            registry_before = repository.registry_path.read_bytes()
            terms_before = termbase.path.read_bytes()
            project_before = controller.project
            errors: list[tuple[str, str]] = []
            window._show_error = lambda title, message: errors.append((title, message))

            self.assertFalse(window.add_term("Two", "二"))
            self.assertEqual(errors[0][0], "无法添加术语")
            self.assertRegex(
                errors[0][1],
                r"语言资源设置.*术语表.*Active.*Update",
            )
            self.assertEqual(repository.registry_path.read_bytes(), registry_before)
            self.assertEqual(termbase.path.read_bytes(), terms_before)
            self.assertEqual(controller.project, project_before)

            update_checkbox = settings.findChild(QCheckBox, f"update_{termbase.id}")
            if update_checkbox is None:
                raise AssertionError("termbase Update checkbox is missing")
            self.assertFalse(update_checkbox.isChecked())
            update_checkbox.setChecked(True)
            self._events()
            enabled = next(
                resource
                for resource in controller.list_resources()
                if resource.id == termbase.id
            )
            self.assertTrue(enabled.active)
            self.assertTrue(enabled.update)

            self.assertTrue(window.add_term("Two", "二"))
            self.assertNotEqual(termbase.path.read_bytes(), terms_before)
            self.assertIn(
                ("Two", "二"),
                {
                    (term.source_term, term.target_term)
                    for term in window.current_suggestions.terms
                },
            )

            settings.close()
            window._confirm_unsaved = lambda: True
            window.close()


if __name__ == "__main__":
    unittest.main()
