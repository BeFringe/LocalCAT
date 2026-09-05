"""Regression coverage for independent and dynamically created theme surfaces."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QLineEdit, QMenu, QVBoxLayout, QWidget

from editor_contracts import ResourceKind, WorkspaceMode
from editor_controller import EditorController
from qt_control_styles import configure_combo_popup, configure_menu
from qt_editor_window import _InlineMenuButton
from qt_settings_dialog import (
    QtSettingsDialog,
    ResourcePackageApplyDialog,
    ResourcePackageImportOptionsDialog,
    _ResourceMoreButton,
)
from resource_package_contracts import ResourceImportMode
from resource_repository import ResourceRepository
from tests import test_multi_document_cluster4_qt as workspace_fixture
from tests import test_qt_settings_dialog as settings_fixture


class RemainingThemeSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.palette = self.app.palette()
        dark = QPalette(self.palette)
        for role in (QPalette.Window, QPalette.Base, QPalette.Button):
            dark.setColor(role, QColor("#202020"))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            dark.setColor(role, QColor("#ffffff"))
        self.app.setPalette(dark)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.setPalette(self.palette)
        self.app.processEvents()

    def _signal(self, dark):
        self.app.styleHints().colorSchemeChanged.emit(
            Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light,
        )

    def test_package_dialogs_switch_real_colors_without_losing_import_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(b"\xef\xbb\xbfhello,world\n")
            controller = EditorController(repository)
            package = root / "terms.localcat-resource"
            controller.export_resource_package(resource.id, package)
            report = controller.validate_resource_package(package)
            parent = QtSettingsDialog(controller)
            options = ResourcePackageImportOptionsDialog(report, controller.list_resources(), parent)
            preview = controller.preview_resource_package_import(package, ResourceImportMode.CREATE_NEW)
            apply = ResourcePackageApplyDialog(preview, resource.name, parent)
            self.widgets.extend((apply, options, parent))
            options.name_input.setText("unsaved import name")
            try:
                for dark in (True, False, True):
                    self._signal(dark)
                    for dialog in (options, apply):
                        self.assertEqual(dialog.palette().color(QPalette.Window).lightnessF() < .5, dark)
                    palette = options.name_input.palette()
                    self.assertEqual(palette.color(QPalette.Base).lightnessF() < .5, dark)
                    self.assertGreater(abs(palette.color(QPalette.Text).lightnessF() - palette.color(QPalette.Base).lightnessF()), .5)
                    self.assertEqual(options.name_input.text(), "unsaved import name")
                    self.assertIs(options.selection()[0], ResourceImportMode.CREATE_NEW)
            finally:
                controller.cancel_resource_package_import(preview)

    def test_open_create_prompt_rethemes_closed_field_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as raw:
            dialog = QtSettingsDialog(settings_fixture.QtSettingsDialogTest()._controller(Path(raw)))
            self.widgets.append(dialog)
            dialog._apply_system_theme(Qt.ColorScheme.Dark)

            def inspect(prompt):
                self.widgets.insert(0, prompt)
                combo = prompt.findChild(QComboBox, "newResourceKind")
                name = prompt.findChild(QLineEdit, "newResourceName")
                name.setText("unsaved resource")
                combo.setCurrentIndex(1)
                before = combo.currentData()
                for dark in (True, False, True):
                    self._signal(dark)
                    self.assertEqual(combo.palette().color(QPalette.Base).lightnessF() < .5, dark)
                    self.assertEqual(combo.view().palette().color(QPalette.Base).lightnessF() < .5, dark)
                    self.assertEqual(combo.currentData(), before)
                    self.assertEqual(name.text(), "unsaved resource")
                return QDialog.Rejected

            with patch.object(QDialog, "exec", inspect):
                dialog._prompt_create_resource()

    def test_settings_popups_use_new_owner_scheme_while_platform_is_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            dialog = QtSettingsDialog(settings_fixture.QtSettingsDialogTest()._controller(Path(raw)))
            self.widgets.append(dialog)
            dialog._apply_system_theme(Qt.ColorScheme.Dark)
            with patch("qt_theme.system_uses_dark_theme", return_value=True):
                dialog._apply_system_theme(Qt.ColorScheme.Light)
                QApplication.sendEvent(dialog, QEvent(QEvent.PaletteChange))
                QTest.qWait(20)
                self.assertFalse(dialog._dark_theme)
            for combo in dialog.findChildren(QComboBox):
                self.assertGreater(combo.view().palette().color(QPalette.Base).lightnessF(), .8)
            menus = dialog.findChildren(QMenu)
            self.assertTrue(menus)
            for menu in menus:
                self.assertGreater(menu.palette().color(QPalette.Window).lightnessF(), .8)
            # A popup created after the signal must inherit the owner's already
            # projected theme rather than the still-dark application palette.
            late_combo = QComboBox(dialog)
            late_combo.addItem("fixture")
            configure_combo_popup(late_combo, object_name="latePopup", accessible_name="fixture")
            late_menu = QMenu(dialog)
            late_menu.addAction("fixture")
            configure_menu(late_menu)
            late_menu.ensurePolished()
            self.assertGreater(late_combo.view().palette().color(QPalette.Base).lightnessF(), .8)
            self.assertGreater(late_menu.palette().color(QPalette.Window).lightnessF(), .8)

    def test_parent_owned_plain_prompt_has_symmetric_light_foreground(self):
        with tempfile.TemporaryDirectory() as raw:
            dialog = QtSettingsDialog(settings_fixture.QtSettingsDialogTest()._controller(Path(raw)))
            prompt = QDialog(dialog)
            layout = QVBoxLayout(prompt)
            label = QLabel("fixture")
            field = QLineEdit("unsaved")
            layout.addWidget(label)
            layout.addWidget(field)
            self.widgets.extend((prompt, dialog))
            dialog._apply_system_theme(Qt.ColorScheme.Dark)
            dialog._apply_system_theme(Qt.ColorScheme.Light)
            self.assertGreater(prompt.palette().color(QPalette.Window).lightnessF(), .8)
            self.assertLess(label.palette().color(QPalette.WindowText).lightnessF(), .4)
            self.assertLess(field.palette().color(QPalette.Text).lightnessF(), .4)
            self.assertEqual(field.text(), "unsaved")

    def test_workspace_browse_dividers_follow_current_and_changed_theme(self):
        case = workspace_fixture.Cluster4QtAcceptanceTests()
        case.setUpClass()
        case.setUp()
        try:
            case._open_workspace()
            case.window._apply_system_theme(Qt.ColorScheme.Dark)
            case.window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
            table = case.window.browse_table
            dividers = [table.item(row, 0) for row in range(table.rowCount())
                        if table.item(row, 0).data(Qt.ItemDataRole.UserRole) is None]
            self.assertEqual(len(dividers), 2)
            for dark in (True, False, True):
                case.window._apply_system_theme(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
                for item in dividers:
                    self.assertEqual(item.background().color().lightnessF() < .5, dark)
                    self.assertGreater(abs(item.foreground().color().lightnessF() - item.background().color().lightnessF()), .3)
            case.window._apply_system_theme(Qt.ColorScheme.Light)
            with patch("qt_theme.system_uses_dark_theme", return_value=True):
                QApplication.sendEvent(case.window, QEvent(QEvent.PaletteChange))
                QTest.qWait(20)
            self.assertFalse(case.window._dark_theme)
        finally:
            case.tearDown()

    def test_settings_theme_queue_is_cancelled_on_destruction(self):
        with tempfile.TemporaryDirectory() as raw:
            dialog = QtSettingsDialog(settings_fixture.QtSettingsDialogTest()._controller(Path(raw)))
            errors = []
            with patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args)):
                QApplication.sendEvent(dialog, QEvent(QEvent.ThemeChange))
                self.assertTrue(dialog._theme_refresh_pending)
                dialog.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                QTest.qWait(20)
            self.assertEqual(errors, [])

    def test_custom_menu_indicators_follow_owner_projection(self):
        owner = QWidget()
        more = _ResourceMoreButton(owner)
        inline = _InlineMenuButton(owner)
        self.widgets.append(owner)

        owner.setProperty("localcatTheme", "dark")
        self.assertEqual(more._dot_color().name(), "#b8dce8")
        self.assertEqual(inline._chevron_color().name(), "#c5d3df")
        inline.setEnabled(False)
        self.assertEqual(inline._chevron_color().name(), "#718291")

        owner.setProperty("localcatTheme", "light")
        self.assertEqual(more._dot_color().name(), "#26435e")
        inline.setEnabled(True)
        self.assertEqual(inline._chevron_color().name(), "#244b68")


if __name__ == "__main__":
    unittest.main()
