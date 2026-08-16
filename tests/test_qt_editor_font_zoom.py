from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from editor_contracts import (
    MAX_EDITOR_FONT_SIZE,
    MIN_EDITOR_FONT_SIZE,
    DisplayPreferences,
    EditorProject,
    EditorSegment,
    WorkspaceMode,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor_window import (
    QtEditorWindow,
    _EDITOR_STYLE,
    render_highlighted_source,
)
from resource_repository import ResourceRepository


class QtEditorFontZoomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _window(self, root: Path) -> QtEditorWindow:
        controller = EditorController(ResourceRepository(root / "app-data"))
        controller.set_project(
            EditorProject(
                name="Font zoom",
                segments=(
                    EditorSegment(
                        id="one",
                        source="Narrated source " * 120,
                        target="第一段译文",
                        speaker="NVLHED",
                        confirmed=True,
                    ),
                    EditorSegment(
                        id="two",
                        source="Second source",
                        target="第二段译文",
                        speaker="alice",
                    ),
                ),
            )
        )
        window = QtEditorWindow(controller)
        window.resize(1280, 760)
        window.show()
        self._events()
        return window

    def _wheel(
        self,
        receiver: object,
        *,
        vertical: int = 120,
        horizontal: int = 0,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.ControlModifier,
    ) -> QWheelEvent:
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(),
            QPoint(horizontal, vertical),
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(receiver, event)
        self._events()
        return event

    def assert_editor_font_size(self, window: QtEditorWindow, expected: int) -> None:
        for widget in (window.source_display, window.target_editor):
            self.assertEqual(widget.font().pixelSize(), expected)
            self.assertEqual(widget.document().defaultFont().pixelSize(), expected)

    def test_ctrl_wheel_on_each_editor_viewport_syncs_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window = self._window(root)

            self._wheel(window.source_display.viewport(), vertical=120)
            self.assert_editor_font_size(window, 16)
            self.assertEqual(
                window.controller.display_preferences().editor_font_size,
                16,
            )
            payload = json.loads(
                (root / "app-data" / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["display"]["editor_font_size"], 16)

            self._wheel(window.target_editor.viewport(), vertical=-120)
            self.assert_editor_font_size(window, 15)
            self._wheel(window.target_editor.viewport(), vertical=120)
            self.assert_editor_font_size(window, 16)
            window.close()

            restored = self._window(root)
            self.assert_editor_font_size(restored, 16)
            restored.close()

    def test_non_zoom_wheel_inputs_and_outside_widgets_pass_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            initial = window.editor_font_size
            source_viewport = window.source_display.viewport()

            cases = (
                QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(),
                    QPoint(0, 120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.ScrollUpdate,
                    False,
                ),
                QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(),
                    QPoint(0, 120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.ShiftModifier,
                    Qt.ScrollPhase.ScrollUpdate,
                    False,
                ),
                QWheelEvent(
                    QPointF(10, 10),
                    QPointF(10, 10),
                    QPoint(),
                    QPoint(120, 0),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.ControlModifier,
                    Qt.ScrollPhase.ScrollUpdate,
                    False,
                ),
            )
            for event in cases:
                with self.subTest(modifiers=event.modifiers(), delta=event.angleDelta()):
                    self.assertFalse(window.eventFilter(source_viewport, event))

            scroll_bar = window.source_display.verticalScrollBar()
            self.assertGreater(scroll_bar.maximum(), 0)
            scroll_bar.setValue(0)
            self._wheel(
                source_viewport,
                vertical=-120,
                modifiers=Qt.KeyboardModifier.NoModifier,
            )
            self.assertGreater(scroll_bar.value(), 0)

            self.assertTrue(window.set_workspace_mode(WorkspaceMode.BROWSE))
            self._wheel(
                source_viewport,
                vertical=120,
                modifiers=Qt.KeyboardModifier.ControlModifier,
            )
            self.assertEqual(window.editor_font_size, initial)

            self._wheel(
                window.segment_list.viewport(),
                vertical=120,
                modifiers=Qt.KeyboardModifier.ControlModifier,
            )
            self.assertEqual(window.editor_font_size, initial)
            self.assert_editor_font_size(window, initial)
            window.close()

    def test_boundaries_consume_ctrl_wheel_without_redundant_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.set_editor_font_size(MAX_EDITOR_FONT_SIZE, persist=False)

            with mock.patch.object(
                window.controller,
                "update_display_preferences",
                wraps=window.controller.update_display_preferences,
            ) as update:
                event = self._wheel(window.source_display.viewport(), vertical=120)

            self.assertTrue(event.isAccepted())
            self.assertEqual(window.editor_font_size, MAX_EDITOR_FONT_SIZE)
            update.assert_not_called()

            window.set_editor_font_size(MIN_EDITOR_FONT_SIZE, persist=False)
            with mock.patch.object(
                window.controller,
                "update_display_preferences",
                wraps=window.controller.update_display_preferences,
            ) as update:
                self._wheel(window.target_editor.viewport(), vertical=-120)
            self.assertEqual(window.editor_font_size, MIN_EDITOR_FONT_SIZE)
            update.assert_not_called()
            window.close()

    def test_font_survives_content_project_and_mode_refresh_without_domain_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            original_project = window.controller.project
            segment_font = window.segment_list.font().toString()
            browse_font = window.browse_table.font().toString()
            menu_font = window.project_menu.font().toString()

            self.assertTrue(window.set_editor_font_size(21))
            window.refresh_suggestions()
            window.controller.go_to(1)
            window._render_current_segment()
            self.assertTrue(window.set_workspace_mode(WorkspaceMode.BROWSE))
            self.assertTrue(window.set_workspace_mode(WorkspaceMode.EDIT))
            self.assert_editor_font_size(window, 21)
            self.assertEqual(window.controller.project, original_project)
            self.assertEqual(window.segment_list.font().toString(), segment_font)
            self.assertEqual(window.browse_table.font().toString(), browse_font)
            self.assertEqual(window.project_menu.font().toString(), menu_font)

            replacement = EditorProject(
                name="Replacement",
                segments=(
                    EditorSegment(
                        id="replacement",
                        source="Replacement source",
                        speaker="NVLHED",
                    ),
                ),
            )
            window.controller.set_project(replacement)
            window._render_project()
            self.assert_editor_font_size(window, 21)
            self.assertEqual(window.controller.project, replacement)
            window.close()

    def test_persistence_failure_keeps_visible_font_and_previous_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ResourceRepository(root / "app-data")
            controller = EditorController(repository)
            controller.update_display_preferences(
                DisplayPreferences(editor_font_size=17)
            )
            controller.set_project(
                EditorProject(
                    name="Failure",
                    segments=(EditorSegment(id="one", source="Source"),),
                )
            )
            window = QtEditorWindow(controller)
            state_path = root / "app-data" / "workspace.json"
            previous_state = state_path.read_bytes()
            errors: list[tuple[str, str]] = []
            window._show_error = lambda title, message: errors.append((title, message))

            with mock.patch.object(
                controller,
                "update_display_preferences",
                side_effect=EditorControllerError("disk full"),
            ):
                self.assertFalse(window.set_editor_font_size(18))

            self.assert_editor_font_size(window, 18)
            self.assertEqual(window.editor_font_size, 18)
            self.assertEqual(window._display_preferences.editor_font_size, 17)
            self.assertEqual(controller.display_preferences().editor_font_size, 17)
            self.assertEqual(state_path.read_bytes(), previous_state)
            self.assertTrue(errors)
            self.assertIn("字号偏好未保存", errors[0][0])
            window.close()

    def test_source_html_and_editor_styles_do_not_pin_font_size(self) -> None:
        self.assertNotIn("font-size", render_highlighted_source("Source", ()))
        selector = "QTextBrowser#sourceDisplay, QTextEdit#targetEditor {"
        start = _EDITOR_STYLE.index(selector)
        end = _EDITOR_STYLE.index("}", start)
        self.assertNotIn("font-size", _EDITOR_STYLE[start:end])


if __name__ == "__main__":
    unittest.main()
