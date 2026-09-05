"""Real-widget theme transitions preserve the TMX preparation and inputs."""

import unittest
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qt_tmx_export_dialog import (
    TmxExportDialog, TmxExportDialogPreview, TmxExportScopeChoice,
)


class TmxExportThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.palette = self.app.palette()
        self.dialog = TmxExportDialog(
            title="导出 TMX", scopes=(TmxExportScopeChoice("project", "项目"),),
            source_locale="en", target_locale="zh-CN",
            prepare=mock.Mock(), publish=mock.Mock(),
        )
        self.dialog.ensurePolished()

    def tearDown(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        self.app.setPalette(self.palette)
        self.app.processEvents()

    def _signal(self, scheme):
        self.app.styleHints().colorSchemeChanged.emit(scheme)
        self.dialog.ensurePolished()

    def test_dark_light_real_palette_render_and_preparation_are_preserved(self):
        dialog = self.dialog
        dialog.destination.setText("fixture.tmx")
        preview = TmxExportDialogPreview(object(), "TMX", "预览", "已绑定", 1, 1, 2, 0, 0, "p")
        dialog._preview = preview
        dark_palette = QPalette(self.palette)
        for role in (QPalette.Window, QPalette.Base, QPalette.Button):
            dark_palette.setColor(role, QColor("#202020"))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            dark_palette.setColor(role, QColor("#ffffff"))
        self.app.setPalette(dark_palette)
        self._signal(Qt.ColorScheme.Dark)
        self.assertLess(dialog.palette().color(QPalette.Window).lightnessF(), .3)
        self.assertGreater(dialog.source_locale.palette().color(QPalette.Text).lightnessF(), .7)
        self.assertLess(dialog.source_locale.palette().color(QPalette.Base).lightnessF(), .3)
        self.assertGreater(dialog.source_locale.palette().color(QPalette.PlaceholderText).lightnessF(), .4)
        self.assertLess(dialog.scope_combo.view().palette().color(QPalette.Base).lightnessF(), .3)
        dark_pixel = dialog.grab().toImage().pixelColor(5, 5)
        self.assertLess(dark_pixel.lightnessF(), .3)
        # Light arrives before the application palette catches up.
        self._signal(Qt.ColorScheme.Light)
        self.assertGreater(dialog.palette().color(QPalette.Window).lightnessF(), .8)
        self.assertLess(dialog.source_locale.palette().color(QPalette.Text).lightnessF(), .4)
        self.assertGreater(dialog.source_locale.palette().color(QPalette.Base).lightnessF(), .8)
        self.assertGreater(dialog.scope_combo.view().palette().color(QPalette.Base).lightnessF(), .8)
        self.assertGreater(dialog.grab().toImage().pixelColor(5, 5).lightnessF(), .8)
        self.assertIs(dialog._preview, preview)
        self.assertEqual(dialog.destination.text(), "fixture.tmx")
        self.assertEqual(dialog.source_locale.text(), "en")
        dialog._prepare.assert_not_called()
        dialog._publish.assert_not_called()

    def test_failed_status_resets_real_palette_when_preview_succeeds(self):
        dialog = self.dialog
        self._signal(Qt.ColorScheme.Light)
        dialog._set_error("TMX.NO_INCLUDED_UNITS")
        failed = dialog.status.palette().color(QPalette.WindowText)
        dialog._worker = mock.Mock(error_message=None, result=TmxExportDialogPreview(
            object(), "TMX", "预览", "已绑定", 1, 1, 2, 0, 0, "p",
        ))
        dialog._preview_finished()
        self.assertFalse(dialog.status.property("failed"))
        self.assertNotEqual(dialog.status.palette().color(QPalette.WindowText), failed)

    def test_late_theme_event_refreshes_disabled_controls_and_popup(self):
        dialog = self.dialog
        dialog._set_busy(True)
        with mock.patch("qt_theme.system_uses_dark_theme", return_value=True):
            QApplication.sendEvent(dialog, QEvent(QEvent.ThemeChange))
            QTest.qWait(20)
            palette = dialog.source_locale.palette()
            self.assertLess(palette.color(QPalette.Disabled, QPalette.Base).lightnessF(), .3)
            self.assertGreater(palette.color(QPalette.Disabled, QPalette.Text).lightnessF(), .5)
            self.assertLess(dialog.scope_combo.view().grab().toImage().pixelColor(2, 2).lightnessF(), .5)
        with mock.patch("qt_theme.system_uses_dark_theme", return_value=False):
            QApplication.sendEvent(dialog, QEvent(QEvent.PaletteChange))
            QTest.qWait(20)
            palette = dialog.source_locale.palette()
            self.assertGreater(palette.color(QPalette.Disabled, QPalette.Base).lightnessF(), .8,
                               (dialog.property("localcatTheme"), dialog._theme_refresh_pending,
                                dialog._theme_refresh_in_progress))
            self.assertLess(palette.color(QPalette.Disabled, QPalette.Text).lightnessF(), .6)
        self.assertFalse(dialog.source_locale.isEnabled())

    def test_queued_theme_refresh_is_cancelled_with_dialog_destruction(self):
        import sys
        from shiboken6 import isValid
        errors = []
        dialog = self.dialog
        try:
            with mock.patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args)):
                QApplication.sendEvent(dialog, QEvent(QEvent.ThemeChange))
                dialog.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                QTest.qWait(30)
        finally:
            if not isValid(dialog):
                self.dialog = None
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
