"""The empty home is usable as a loading surface before Core is ready."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow

import qt_editor
from qt_startup_window import QtStartupWindow


class QtStartupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_during_first_paint_does_not_start_preload_or_empty_event_loop(self):
        class ImmediatelyClosedHome(QtStartupWindow):
            def show(self):
                super().show()
                QTimer.singleShot(0, self.close)

        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(self.app.quit)
        try:
            with (
                patch("qt_startup_window.QtStartupWindow", ImmediatelyClosedHome),
                patch.object(qt_editor, "_compose_editor_controller") as compose,
                patch("qt_speaker_avatar.resource_png_pixmap", return_value=QPixmap(2, 2)),
            ):
                safety.start(2000)
                result = qt_editor._run_empty_home_startup(
                    object(), object(), SimpleNamespace(logo=None, speaker_avatars=()),
                    qt_editor._StartupTrace("fixture"),
                )
                self.assertEqual(result, 0)
                compose.assert_not_called()
        finally:
            safety.stop()

    def test_validation_bridge_failure_closes_unpublished_editor_and_keeps_home(self):
        observations, windows = [], []

        class UnpublishedWindow(QMainWindow):
            def __init__(self, *_args, **_kwargs):
                super().__init__()
                windows.append(self)

        def inspect_failure():
            for widget in self.app.topLevelWidgets():
                if isinstance(widget, QtStartupWindow) and widget.isVisible():
                    observations.append(widget.status_label.text())
                    widget.close()

        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(self.app.quit)
        try:
            with (
                patch.object(qt_editor, "_compose_editor_controller", return_value=(object(), object())),
                patch.object(qt_editor, "_compose_chunk_controller", return_value=object()),
                patch.object(qt_editor, "_compose_tmx_export_service", return_value=object()),
                patch.object(qt_editor, "_start_capability_validation", side_effect=ValueError("private bridge failure")),
                patch("qt_editor_window.QtEditorWindow", UnpublishedWindow),
                patch("qt_speaker_avatar.resource_png_pixmap", return_value=QPixmap(2, 2)),
            ):
                QTimer.singleShot(200, inspect_failure)
                safety.start(3000)
                qt_editor._run_empty_home_startup(
                    object(), object(), SimpleNamespace(logo=None, speaker_avatars=()),
                    qt_editor._StartupTrace("fixture"),
                )
            self.assertEqual(observations, ["语言资源加载失败"])
            self.assertEqual(len(windows), 1)
            self.assertFalse(windows[0].isVisible())
        finally:
            safety.stop()
            for widget in self.app.topLevelWidgets():
                widget.close()

    def test_home_paints_while_preload_is_blocked_then_publishes_on_gui(self):
        entered, release = threading.Event(), threading.Event()
        events, windows = [], []
        controller, capabilities = object(), object()
        gui_thread = threading.get_ident()

        def compose(*_args, **_kwargs):
            events.append(("build", threading.get_ident()))
            entered.set()
            release.wait(5)
            return controller, capabilities

        class ReadyWindow(QMainWindow):
            def __init__(self, received_controller, **_kwargs):
                super().__init__()
                events.append(("ready", threading.get_ident(), received_controller))
                windows.append(self)
                QTimer.singleShot(100, self.close)

        def inspect_home():
            homes = [widget for widget in self.app.topLevelWidgets()
                     if isinstance(widget, QtStartupWindow) and widget.isVisible()]
            events.append(("home", len(homes), entered.is_set(), len(windows)))
            if homes:
                homes[0].resize(1200, 760)
                events.append(("loading", homes[0].status_label.text()))
            release.set()

        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(self.app.quit)
        try:
            with (
                patch.object(qt_editor, "_compose_editor_controller", side_effect=compose),
                patch.object(qt_editor, "_compose_chunk_controller", return_value=object()),
                patch.object(qt_editor, "_compose_tmx_export_service", return_value=object()),
                patch.object(qt_editor, "_start_capability_validation", return_value=object()),
                patch("qt_editor_window.QtEditorWindow", ReadyWindow),
                patch("qt_speaker_avatar.resource_png_pixmap", return_value=QPixmap(2, 2)),
            ):
                QTimer.singleShot(200, inspect_home)
                safety.start(5000)
                code = qt_editor._run_empty_home_startup(
                    object(), object(), SimpleNamespace(logo=None, speaker_avatars=()),
                    qt_editor._StartupTrace("fixture"),
                )
            self.assertEqual(code, 0)
            self.assertIn(("home", 1, True, 0), events)
            self.assertIn(("loading", "正在加载语言资源"), events)
            self.assertIn(("ready", gui_thread, controller), events)
            self.assertNotEqual(events[0][1], gui_thread)
            self.assertEqual(len(windows), 1)
            self.assertEqual((windows[0].width(), windows[0].height()), (1200, 760))
        finally:
            safety.stop()
            release.set()
            for widget in self.app.topLevelWidgets():
                widget.close()

    def test_failed_preload_keeps_home_and_does_not_publish_editor(self):
        observations = []

        def inspect_failure():
            for widget in self.app.topLevelWidgets():
                if isinstance(widget, QtStartupWindow) and widget.isVisible():
                    observations.append((widget.status_label.text(), widget.detail_label.text()))
                    widget.close()

        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(self.app.quit)
        try:
            with (
                patch.object(qt_editor, "_compose_editor_controller", side_effect=ValueError("private fixture")),
                patch("qt_editor_window.QtEditorWindow") as ready,
                patch("qt_speaker_avatar.resource_png_pixmap", return_value=QPixmap(2, 2)),
            ):
                QTimer.singleShot(200, inspect_failure)
                safety.start(5000)
                qt_editor._run_empty_home_startup(
                    object(), object(), SimpleNamespace(logo=None, speaker_avatars=()),
                    qt_editor._StartupTrace("fixture"),
                )
                ready.assert_not_called()
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0][0], "语言资源加载失败")
            self.assertNotIn("private fixture", observations[0][1])
            self.assertIn("LOCALCAT.STARTUP.RESOURCES_FAILED", observations[0][1])
        finally:
            safety.stop()
            for widget in self.app.topLevelWidgets():
                widget.close()
