from __future__ import annotations

from collections import Counter
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QModelIndex, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QToolButton,
)

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository


class QtSettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        application = QApplication.instance()
        cls.app = (
            application if isinstance(application, QApplication) else QApplication([])
        )

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
            active = next(
                resource
                for resource in controller.list_resources()
                if resource.name == "Primary TM"
            )

            self.assertEqual(dialog.active_table.rowCount(), 1)
            self.assertEqual(dialog.inactive_table.rowCount(), 1)
            active_name = dialog.active_table.item(0, 3)
            inactive_name = dialog.inactive_table.item(0, 3)
            assert inactive_name is not None
            self.assertIsNone(active_name)
            tm_name = dialog.findChild(QLabel, f"resourceName_{active.id}")
            self.assertIsNotNone(tm_name)
            assert tm_name is not None
            self.assertEqual(tm_name.text(), "Primary TM")
            self.assertEqual(inactive_name.text(), "Archive terms")
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
            assert lookup is not None
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
            self.assertIsNotNone(active)
            assert active is not None

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

    def test_new_resource_kind_combo_renders_readable_popup_states(self) -> None:
        hostile_palette = QPalette(self.app.palette())
        for role, color in (
            (QPalette.ColorRole.Text, QColor("#ffffff")),
            (QPalette.ColorRole.ButtonText, QColor("#ffffff")),
            (QPalette.ColorRole.Base, QColor("#ffffff")),
            (QPalette.ColorRole.Button, QColor("#ffffff")),
            (QPalette.ColorRole.Highlight, QColor("#f4f6f8")),
            (QPalette.ColorRole.HighlightedText, QColor("#ffffff")),
        ):
            hostile_palette.setColor(role, color)

        original_palette = QPalette(self.app.palette())
        rendered: dict[str, object] = {}
        try:
            self.app.setPalette(hostile_palette)
            with tempfile.TemporaryDirectory() as temp_dir:
                controller = self._controller(Path(temp_dir))
                dialog = QtSettingsDialog(controller)

                def capture_prompt(prompt: QDialog) -> QDialog.DialogCode:
                    kind_input = prompt.findChild(QComboBox)
                    self.assertIsNotNone(kind_input)
                    assert kind_input is not None
                    prompt.show()
                    kind_input.resize(240, 36)
                    closed_image = kind_input.grab().toImage()
                    closed_option = QStyleOptionComboBox()
                    closed_option.initFrom(kind_input)
                    closed_option.currentText = kind_input.currentText()
                    closed_option.rect = kind_input.rect()
                    closed_text_rect = kind_input.style().subControlRect(
                        QStyle.ComplexControl.CC_ComboBox,
                        closed_option,
                        QStyle.SubControl.SC_ComboBoxEditField,
                        kind_input,
                    )
                    rendered["closed"] = (
                        closed_image,
                        self._to_device_rect(closed_image, closed_text_rect),
                    )
                    kind_input.showPopup()
                    self.app.processEvents()
                    popup = kind_input.view()

                    rendered["combo"] = kind_input
                    rendered["popup"] = popup
                    rendered["data"] = tuple(
                        kind_input.itemData(index) for index in range(kind_input.count())
                    )
                    states = (
                        QStyle.StateFlag.State_Enabled,
                        QStyle.StateFlag.State_Enabled
                        | QStyle.StateFlag.State_MouseOver,
                        QStyle.StateFlag.State_Enabled
                        | QStyle.StateFlag.State_Selected,
                    )
                    rendered["states"] = tuple(
                        self._render_popup_item(
                            popup,
                            kind_input.model().index(row, 0),
                            state,
                        )
                        for row in range(kind_input.count())
                        for state in states
                    )
                    kind_input.hidePopup()
                    prompt.close()
                    return QDialog.DialogCode.Rejected

                with patch.object(QDialog, "exec", new=capture_prompt):
                    dialog._prompt_create_resource()

                kind_input = rendered["combo"]
                popup = rendered["popup"]
                assert isinstance(kind_input, QComboBox)
                assert isinstance(popup, QAbstractItemView)
                self.assertEqual(kind_input.objectName(), "newResourceKind")
                self.assertEqual(kind_input.accessibleName(), "资源类型")
                self.assertEqual(popup.objectName(), "newResourceKindPopup")
                self.assertEqual(
                    rendered["data"],
                    (
                        ResourceKind.TRANSLATION_MEMORY.value,
                        ResourceKind.TERMBASE.value,
                    ),
                )
                self.assertGreaterEqual(
                    self._contrast_ratio(
                        kind_input.palette().color(QPalette.ColorRole.Text),
                        kind_input.palette().color(QPalette.ColorRole.Base),
                    ),
                    4.5,
                )
                closed_image, closed_text_rect = cast(
                    tuple[QImage, QRect], rendered["closed"]
                )
                closed_background = self._dominant_background_outside(
                    closed_image,
                    closed_text_rect,
                )
                self.assertTrue(
                    self._has_readable_glyph_population(
                        closed_image,
                        closed_text_rect,
                        closed_background,
                    )
                )
                self.assertGreaterEqual(
                    self._contrast_ratio(
                        popup.palette().color(QPalette.ColorRole.Text),
                        popup.palette().color(QPalette.ColorRole.Base),
                    ),
                    4.5,
                )
                self.assertGreaterEqual(
                    self._contrast_ratio(
                        popup.palette().color(QPalette.ColorRole.HighlightedText),
                        popup.palette().color(QPalette.ColorRole.Highlight),
                    ),
                    4.5,
                )

                state_images = rendered["states"]
                assert isinstance(state_images, tuple)
                self.assertTrue(
                    all(
                        isinstance(image, QImage) and isinstance(text_rect, QRect)
                        for image, text_rect in state_images
                    )
                )
                backgrounds = tuple(
                    self._dominant_background_outside(image, text_rect)
                    for image, text_rect in state_images
                )
                self.assertEqual(len(backgrounds), 6)
                self.assertEqual(
                    len({color.name() for color in backgrounds[:3]}),
                    3,
                )
                self.assertEqual(
                    len({color.name() for color in backgrounds[3:]}),
                    3,
                )
                for (image, text_rect), background in zip(state_images, backgrounds):
                    self.assertTrue(
                        self._has_readable_glyph_population(
                            image,
                            text_rect,
                            background,
                        )
                    )
                dialog.close()
        finally:
            self.app.setPalette(original_palette)

    def test_contrast_oracle_rejects_one_dark_artifact(self) -> None:
        image = QImage(120, 32, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#ffffff"))
        text_rect = QRect(8, 4, 84, 24)
        image.setPixelColor(text_rect.center(), QColor("#10243b"))
        background = self._dominant_background_outside(image, text_rect)

        self.assertFalse(
            self._has_readable_glyph_population(image, text_rect, background)
        )

    @staticmethod
    def _render_popup_item(
        popup: QAbstractItemView,
        index: QModelIndex,
        state: QStyle.StateFlag,
    ) -> tuple[QImage, QRect]:
        image = QImage(240, 36, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(popup.palette().color(QPalette.ColorRole.Base))
        option = QStyleOptionViewItem()
        option.initFrom(popup)
        delegate = popup.itemDelegateForIndex(index)
        if not isinstance(delegate, QStyledItemDelegate):
            raise AssertionError("resource kind popup must use QStyledItemDelegate")
        delegate.initStyleOption(option, index)
        option.rect = QRect(0, 0, image.width(), image.height())
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
    def _to_device_rect(image: QImage, logical_rect: QRect) -> QRect:
        ratio = image.devicePixelRatio()
        return QRect(
            round(logical_rect.x() * ratio),
            round(logical_rect.y() * ratio),
            round(logical_rect.width() * ratio),
            round(logical_rect.height() * ratio),
        ).intersected(image.rect())

    @staticmethod
    def _dominant_background_outside(image: QImage, text_rect: QRect) -> QColor:
        protected_text = text_rect.adjusted(-1, -1, 1, 1)
        rgba_values = (
            image.pixelColor(x, y).rgba()
            for y in range(3, image.height() - 3)
            for x in range(3, image.width() - 3)
            if not protected_text.contains(x, y)
        )
        counts = Counter(rgba_values)
        if not counts:
            raise AssertionError("no rendered background exists outside the text rect")
        return QColor.fromRgba(counts.most_common(1)[0][0])

    @classmethod
    def _has_readable_glyph_population(
        cls,
        image: QImage,
        text_rect: QRect,
        background: QColor,
    ) -> bool:
        sample_rect = text_rect.intersected(image.rect()).adjusted(1, 1, -1, -1)
        glyph_pixels = tuple(
            (x, y)
            for y in range(sample_rect.top(), sample_rect.bottom() + 1)
            for x in range(sample_rect.left(), sample_rect.right() + 1)
            if cls._contrast_ratio(image.pixelColor(x, y), background) >= 4.5
        )
        if len(glyph_pixels) < 10:
            return False
        x_values = tuple(x for x, _y in glyph_pixels)
        y_values = tuple(y for _x, y in glyph_pixels)
        return max(x_values) - min(x_values) >= 6 and max(y_values) - min(y_values) >= 4

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
            assert button is not None
            expected_button_width = min(
                40,
                max(32, button.sizeHint().width() + 8),
            )

            dialog.show()
            self.app.processEvents()

            self.assertTrue(button.autoRaise())
            self.assertEqual(
                button.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Fixed,
            )
            self.assertGreaterEqual(button.width(), 32)
            self.assertLessEqual(button.width(), 40)
            self.assertEqual(button.width(), expected_button_width)
            self.assertLessEqual(table.columnWidth(7), 40)
            self.assertIn(
                table.horizontalHeader().sectionResizeMode(7),
                (
                    QHeaderView.ResizeMode.ResizeToContents,
                    QHeaderView.ResizeMode.Fixed,
                ),
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
            self.assertIsNotNone(button)
            assert button is not None

            dialog.show()
            for width in (860, 1320):
                dialog.resize(width, 560)
                self.app.processEvents()
                cell_left = table.columnViewportPosition(7)
                cell_right = cell_left + table.columnWidth(7)
                button_left = button.mapTo(
                    table.viewport(),
                    button.rect().topLeft(),
                ).x()
                button_right = button_left + button.width()
                import_right = (
                    table.columnViewportPosition(6) + table.columnWidth(6)
                )

                self.assertTrue(button.isVisible())
                self.assertLessEqual(table.columnWidth(7), 40)
                self.assertEqual(
                    button.width(),
                    min(40, max(32, button.sizeHint().width() + 8)),
                )
                self.assertGreaterEqual(cell_left, import_right)
                self.assertGreaterEqual(button_left, cell_left)
                self.assertLessEqual(button_right, cell_right)
                self.assertLessEqual(cell_right, table.viewport().width())

            dialog.close()

    def test_more_button_keeps_transparent_unframed_edges_in_all_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured
                for configured in controller.list_resources()
                if configured.name == "Primary TM"
            )
            button = dialog.findChild(QToolButton, f"more_{resource.id}")
            self.assertIsNotNone(button)
            assert button is not None
            dialog.show()
            self.app.processEvents()

            def rendered_state() -> QImage:
                return button.grab().toImage()

            def edge_colors(image: QImage) -> tuple[QColor, ...]:
                return tuple(
                    image.pixelColor(x, y)
                    for x, y in (
                        (1, 1),
                        (image.width() - 2, 1),
                        (1, image.height() - 2),
                        (image.width() - 2, image.height() - 2),
                    )
                )

            normal_image = rendered_state()
            button.setFocus()
            self.app.processEvents()
            focus_image = rendered_state()
            QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
            self.app.processEvents()
            hover_image = rendered_state()
            button.setDown(True)
            self.app.processEvents()
            pressed_image = rendered_state()

            button.setDown(False)
            QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
            self.app.processEvents()

            normal_edges = edge_colors(normal_image)
            for image in (normal_image, focus_image, hover_image, pressed_image):
                self.assertEqual(edge_colors(image), normal_edges)
                self.assertTrue(
                    all(color.alpha() == 0 for color in edge_colors(image))
                )
                glyph_pixels = tuple(
                    (x, y)
                    for y in range(image.height())
                    for x in range(image.width())
                    if image.pixelColor(x, y).alpha() > 0
                )
                self.assertTrue(glyph_pixels)
                glyph_left = min(x for x, _y in glyph_pixels)
                glyph_right = max(x for x, _y in glyph_pixels)
                glyph_top = min(y for _x, y in glyph_pixels)
                glyph_bottom = max(y for _x, y in glyph_pixels)
                self.assertAlmostEqual(
                    (glyph_left + glyph_right + 1) / 2,
                    image.width() / 2,
                    delta=1,
                )
                self.assertAlmostEqual(
                    (glyph_top + glyph_bottom + 1) / 2,
                    image.height() / 2,
                    delta=max(1.0, image.devicePixelRatio() * 1.5),
                )
            self.assertIn("background: transparent", button.styleSheet())
            self.assertIn("border: none", button.styleSheet())
            self.assertIn("QToolButton::menu-indicator", button.styleSheet())
            dialog.close()

    def test_import_and_more_hit_areas_are_tightly_adjacent_after_show(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            dialog = QtSettingsDialog(controller)
            resource = next(
                configured
                for configured in controller.list_resources()
                if configured.name == "Primary TM"
            )
            table = dialog.active_table
            import_button = dialog.findChild(QPushButton, f"import_{resource.id}")
            more_button = dialog.findChild(QToolButton, f"more_{resource.id}")
            self.assertIsNotNone(import_button)
            self.assertIsNotNone(more_button)
            assert import_button is not None
            assert more_button is not None

            dialog.show()
            for width, height in ((860, 560), (1180, 680), (1320, 680)):
                dialog.resize(width, height)
                self.app.processEvents()
                import_left = import_button.mapTo(
                    table.viewport(),
                    import_button.rect().topLeft(),
                ).x()
                more_left = more_button.mapTo(
                    table.viewport(),
                    more_button.rect().topLeft(),
                ).x()
                gap = more_left - (import_left + import_button.width())

                self.assertEqual(table.columnWidth(7), 32)
                self.assertEqual(more_button.width(), 32)
                self.assertEqual(gap, 0)
                self.assertLessEqual(
                    import_button.width() + more_button.width(),
                    160,
                )
                self.assertFalse(import_button.grab().toImage().isNull())
                self.assertFalse(more_button.grab().toImage().isNull())

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
            self.assertIsNotNone(button)
            assert button is not None
            menu = button.menu()
            self.assertIsNotNone(menu)
            assert menu is not None
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
            assert more_button is not None
            menu = more_button.menu()
            self.assertIsNotNone(menu)
            assert menu is not None
            delete_action = next(
                action for action in menu.actions() if action.text() == "删除资源"
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
            self.assertIsNotNone(more_button)
            assert more_button is not None
            menu = more_button.menu()
            self.assertIsNotNone(menu)
            assert menu is not None
            delete_action = next(
                action for action in menu.actions() if action.text() == "删除资源"
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
