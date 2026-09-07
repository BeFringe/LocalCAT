from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from qt_startup_window import QtStartupWindow


class QtStartupWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = QtStartupWindow()
        self.addCleanup(self.window.close)
        self.window.show()
        self._events()

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_initial_window_is_honest_loading_state_without_project_actions(self):
        self.assertFalse(self.window.closed)
        self.assertEqual(self.window.size(), QSize(1440, 880))
        self.assertEqual(self.window.minimumSize(), QSize(1080, 700))
        self.assertEqual(self.window.status_label.text(), "正在加载语言资源")
        self.assertFalse(self.window.open_project_button.isEnabled())
        self.assertIn("完成", self.window.open_project_button.toolTip())
        self.assertTrue(self.window.progress_bar.isVisible())
        self.assertEqual(self.window.progress_bar.maximum(), 0)
        self.assertEqual(self.window.styleSheet(), "")
        self.assertEqual(self.window.root.styleSheet(), "")
        self.assertEqual(self.window.card.styleSheet(), "")
        self.assertEqual(self.window.open_project_button.styleSheet(), "")
        self.assertEqual(self.window.retry_button.styleSheet(), "")
        self.assertEqual(self.window.progress_bar.styleSheet(), "")
        self.assertFalse(self.window.retry_button.isVisible())

    def test_failure_and_retry_are_safe_explicit_and_single_shot(self):
        retries = QSignalSpy(self.window.retry_requested)
        self.window.set_loading_failed()
        self.assertEqual(self.window.status_label.text(), "语言资源加载失败")
        self.assertIn("LOCALCAT.STARTUP.RESOURCES_FAILED", self.window.detail_label.text())
        self.assertFalse(self.window.progress_bar.isVisible())
        self.assertTrue(self.window.retry_button.isEnabled())
        self.assertTrue(self.window.retry_button.isVisible())
        self.assertFalse(self.window.open_project_button.isEnabled())
        QTest.mouseClick(self.window.retry_button, Qt.MouseButton.LeftButton)
        self.assertEqual(retries.count(), 1)
        self.assertEqual(self.window.status_label.text(), "正在加载语言资源")
        self.assertFalse(self.window.retry_button.isEnabled())
        self.assertFalse(self.window.retry_button.isVisible())
        self.assertTrue(self.window.progress_bar.isVisible())
        self.window._request_retry()
        self.assertEqual(retries.count(), 1)

    def test_close_marks_closed_and_does_not_request_retry(self):
        retries = QSignalSpy(self.window.retry_requested)
        self.window.set_loading_failed()
        self.window.close()
        self.assertTrue(self.window.closed)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(retries.count(), 0)
        self.window.set_loading()
        self.window.set_loading_failed()
        self.assertTrue(self.window.closed)
        self.assertFalse(self.window.isVisible())

    def test_theme_signal_updates_before_stale_palette_catches_up(self):
        previous_palette = self.app.palette()
        self.addCleanup(self.app.setPalette, previous_palette)
        stale = QPalette(previous_palette)
        stale.setColor(QPalette.ColorRole.Window, QColor("#17191c"))
        stale.setColor(QPalette.ColorRole.WindowText, QColor("#e7edf3"))
        self.app.setPalette(stale)
        self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        self.assertEqual(self.window.property("localcatTheme"), "dark")
        self.assertEqual(self.window.root.palette().color(QPalette.ColorRole.Window), QColor("#17191c"))
        self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        self.assertEqual(self.window.property("localcatTheme"), "light")
        self.assertEqual(self.window.root.palette().color(QPalette.ColorRole.Window), QColor("#edf2f7"))
        self.assertEqual(self.window.card.palette().color(QPalette.ColorRole.Window), QColor("#ffffff"))
        self.assertEqual(self.window.status_label.palette().color(QPalette.ColorRole.WindowText), QColor("#17344d"))

    def test_late_palette_event_rechecks_theme(self):
        with patch("qt_startup_window.system_uses_dark_theme", return_value=True):
            QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.ApplicationPaletteChange))
            self._events()
        self.assertEqual(self.window.property("localcatTheme"), "dark")

    def test_ui_module_has_no_business_or_worker_dependencies(self):
        path = Path(__file__).resolve().parents[1] / "qt_startup_window.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        modules.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertLessEqual(modules, {"__future__", "PySide6", "qt_theme"})


if __name__ == "__main__":
    unittest.main()
