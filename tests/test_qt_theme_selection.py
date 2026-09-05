"""Explicit OS scheme facts survive stale asynchronous palette notifications."""
import unittest
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from qt_theme import ThemeBinding


class ThemeSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_signal_survives_queued_stale_palette_and_unknown_releases_it(self):
        dialog = QDialog()
        binding = ThemeBinding(dialog, "QDialog {background:#ffffff;}", "QDialog {background:#202020;}")
        try:
            with patch("qt_theme.system_uses_dark_theme", return_value=True):
                self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
                for event in (QEvent.PaletteChange, QEvent.ThemeChange, QEvent.ApplicationPaletteChange):
                    QApplication.sendEvent(dialog, QEvent(event))
                    QTest.qWait(20)
                    self.assertEqual(dialog.property("localcatTheme"), "light")
                    self.assertGreater(dialog.palette().color(QPalette.Window).lightnessF(), .8)
                self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Unknown)
                QTest.qWait(20)
                self.assertEqual(dialog.property("localcatTheme"), "dark")
        finally:
            dialog.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_new_child_inherits_projected_owner_before_system_catches_up(self):
        parent = QDialog()
        parent.setProperty("localcatTheme", "light")
        try:
            with patch("qt_theme.system_uses_dark_theme", return_value=True):
                child = QDialog(parent)
                binding = ThemeBinding(child, "QDialog {background:#ffffff;}", "QDialog {background:#202020;}")
                QTest.qWait(20)
                self.assertEqual(child.property("localcatTheme"), "light")
                binding.refresh(Qt.ColorScheme.Unknown)
                self.assertEqual(child.property("localcatTheme"), "light")
        finally:
            parent.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_real_style_hints_switch_survives_event_loop(self):
        if self.app.platformName() != "windows":
            if sys.platform != "win32":
                self.skipTest("native Windows style-hints verification")
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "windows"
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest",
                 "tests.test_qt_theme_selection.ThemeSelectionTests.test_real_style_hints_switch_survives_event_loop"],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return
        dialog = QDialog()
        binding = ThemeBinding(dialog, "QDialog {background:#ffffff;}", "QDialog {background:#202020;}")
        hints = self.app.styleHints()
        previous = hints.colorScheme()
        try:
            for scheme, expected in ((Qt.ColorScheme.Dark, "dark"), (Qt.ColorScheme.Light, "light")):
                hints.setColorScheme(scheme)
                QTest.qWait(30)
                self.assertEqual(dialog.property("localcatTheme"), expected)
        finally:
            dialog.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            hints.setColorScheme(previous)
