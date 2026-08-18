"""Task 6.3 Qt journeys for the two Controller-owned fuzzy thresholds."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from editor_contracts import EditorProject, EditorSegment, TMPreferences
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository
from tests.test_editor_controller_tm_apply import (
    _canonical_controller,
    _legacy_fixture,
)
from tm_application_composition import TMResourceResolver, TMRuntimeHost


class QtTMThresholdIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _threshold_entries(
        self,
        window: QtEditorWindow,
        dialog: QtSettingsDialog,
    ) -> tuple[QPushButton, QLabel, QPushButton, QLabel]:
        window_chip = window.findChild(QPushButton, "tmThresholdChip")
        window_state = window.findChild(QLabel, "tmThresholdState")
        settings_chip = dialog.findChild(QPushButton, "settingsTmThresholdChip")
        settings_state = dialog.findChild(QLabel, "settingsTmThresholdState")
        self.assertIsNotNone(window_chip)
        self.assertIsNotNone(window_state)
        self.assertIsNotNone(settings_chip)
        self.assertIsNotNone(settings_state)
        assert window_chip is not None
        assert window_state is not None
        assert settings_chip is not None
        assert settings_state is not None
        return window_chip, window_state, settings_chip, settings_state

    def test_two_available_entries_share_value_state_and_keyboard_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            dialog = window.create_settings_dialog()
            window.show()
            self._events()
            window_chip, window_state, settings_chip, settings_state = (
                self._threshold_entries(window, dialog)
            )

            window.confirm_button.setFocus()
            QTest.keyClick(window.confirm_button, Qt.Key.Key_Tab)
            self.assertTrue(window_chip.hasFocus())
            dialog.show()
            self._events()

            for chip in (window_chip, settings_chip):
                self.assertTrue(chip.isVisible())
                self.assertTrue(chip.isEnabled())
                self.assertEqual(chip.focusPolicy(), Qt.FocusPolicy.StrongFocus)
                self.assertIn("60%", chip.text())
                self.assertIn("Fuzzy 阈值", chip.accessibleName())
                self.assertTrue(chip.toolTip())
            self.assertEqual(window_state.text(), settings_state.text())
            self.assertIn("Fuzzy 可用", window_state.text())

            dialog.new_resource_button.setFocus()
            QTest.keyClick(dialog.new_resource_button, Qt.Key.Key_Tab)
            self.assertTrue(settings_chip.hasFocus())

            with patch(
                "qt_tm_threshold.QInputDialog.getDouble",
                return_value=(78, True),
            ):
                settings_chip.setFocus()
                QTest.keyClick(settings_chip, Qt.Key.Key_Space)
            self._events()

            self.assertEqual(controller.tm_preferences(), TMPreferences(0.78))
            self.assertIn("78%", settings_chip.text())
            self.assertIn("78%", window_chip.text())
            self.assertIn("78%", dialog.status_label.text())
            self.assertIn("78%", window.statusBar().currentMessage())

            dialog.close()
            self._events()
            with patch(
                "qt_tm_threshold.QInputDialog.getDouble",
                return_value=(82, True),
            ):
                window_chip.setFocus()
                QTest.keyClick(window_chip, Qt.Key.Key_Return)
            self._events()

            self.assertEqual(controller.tm_preferences(), TMPreferences(0.82))
            self.assertIn("82%", window_chip.text())
            self.assertIn("82%", window.statusBar().currentMessage())
            window.close()

    def test_unavailable_entries_remain_visible_with_one_disabled_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = _legacy_fixture(
                Path(temporary)
            )
            window = QtEditorWindow(controller)
            with patch.object(
                controller,
                "tm_suggestion_report",
                side_effect=AssertionError("settings threshold must not query"),
            ) as query:
                dialog = window.create_settings_dialog()
                dialog.refresh_resources()

            self.assertEqual(query.call_count, 0)
            window.show()
            self._events()
            window_chip, window_state, settings_chip, settings_state = (
                self._threshold_entries(window, dialog)
            )
            window.confirm_button.setFocus()
            QTest.keyClick(window.confirm_button, Qt.Key.Key_Tab)
            self.assertTrue(window_chip.hasFocus())
            dialog.show()
            self._events()
            dialog.new_resource_button.setFocus()
            QTest.keyClick(dialog.new_resource_button, Qt.Key.Key_Tab)
            self.assertTrue(settings_chip.hasFocus())
            for chip in (window_chip, settings_chip):
                self.assertTrue(chip.isVisible())
                self.assertTrue(chip.isEnabled())
                self.assertEqual(chip.focusPolicy(), Qt.FocusPolicy.StrongFocus)
                self.assertFalse(bool(chip.property("fuzzyAvailable")))
                self.assertIn("60%", chip.text())
            self.assertEqual(window_state.text(), settings_state.text())
            self.assertIn("Fuzzy 不可用", window_state.text())
            self.assertEqual(window_chip.toolTip(), settings_chip.toolTip())
            self.assertIn(window_state.text(), window_chip.accessibleName())

            with (
                patch(
                    "qt_tm_threshold.QInputDialog.getDouble",
                    side_effect=AssertionError(
                        "unavailable threshold must not open the prompt"
                    ),
                ) as prompt,
                patch.object(
                    controller,
                    "update_tm_minimum_similarity",
                    side_effect=AssertionError(
                        "unavailable threshold must not update the Controller"
                    ),
                ) as update,
            ):
                for chip in (window_chip, settings_chip):
                    chip.click()
                    chip.setFocus()
                    QTest.keyClick(chip, Qt.Key.Key_Return)
                    QTest.keyClick(chip, Qt.Key.Key_Space)
            self.assertEqual(prompt.call_count, 0)
            self.assertEqual(update.call_count, 0)
            dialog.close()
            window.close()

    def test_retrieval_status_projection_is_query_free_and_defensive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                autospec=True,
                side_effect=AssertionError("status projection must not query"),
            ) as query:
                first = controller.tm_retrieval_status()
                object.__setattr__(first, "fuzzy_available", False)
                second = controller.tm_retrieval_status()

            self.assertEqual(query.call_count, 0)
            self.assertFalse(first.fuzzy_available)
            self.assertTrue(second.fuzzy_available)

    def test_persistence_failures_keep_both_entries_and_feedback_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            dialog = window.create_settings_dialog()
            window.show()
            dialog.show()
            self._events()
            window_chip, _window_state, settings_chip, _settings_state = (
                self._threshold_entries(window, dialog)
            )

            with (
                patch(
                    "qt_tm_threshold.QInputDialog.getDouble",
                    return_value=(84, True),
                ),
                patch(
                    "workspace_state.os.replace",
                    side_effect=OSError("/private/customer/threshold"),
                ),
            ):
                window_chip.click()
            self._events()
            self.assertEqual(controller.tm_preferences(), TMPreferences())
            self.assertIn("60%", window_chip.text())
            self.assertIn("保存失败", window.statusBar().currentMessage())
            self.assertNotIn("/private", window.statusBar().currentMessage())

            with (
                patch(
                    "qt_tm_threshold.QInputDialog.getDouble",
                    return_value=(86, True),
                ),
                patch(
                    "workspace_state.os.replace",
                    side_effect=OSError("proof body"),
                ),
            ):
                settings_chip.click()
            self._events()
            self.assertEqual(controller.tm_preferences(), TMPreferences())
            self.assertIn("60%", settings_chip.text())
            self.assertIn("保存失败", dialog.status_label.text())
            self.assertNotIn("proof body", dialog.status_label.text())
            dialog.close()
            window.close()

    def test_project_switch_and_new_controller_restore_the_same_visible_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _runtime, composition = _canonical_controller(self, root)
            self.assertTrue(
                controller.update_tm_minimum_similarity(0.83).succeeded
            )
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            chip = window.findChild(QPushButton, "tmThresholdChip")
            self.assertIsNotNone(chip)
            assert chip is not None
            self.assertIn("83%", chip.text())

            controller.set_project(
                EditorProject(
                    name="Second project",
                    segments=(EditorSegment(id="second", source="aabba"),),
                )
            )
            window.refresh_suggestions()
            self.assertIn("83%", chip.text())
            window.close()

            repository = ResourceRepository(controller.repository.config_dir)
            restarted = EditorController(
                repository,
                tm_adapter=EditorTMAdapter(
                    runtime_host=TMRuntimeHost(
                        resolver=TMResourceResolver(),
                        configs=repository.list_resources(),
                    ),
                    capability_host=composition.host,
                ),
            )
            restarted.set_project(
                EditorProject(
                    name="Restarted",
                    segments=(EditorSegment(id="restart", source="aabba"),),
                )
            )
            restarted_window = QtEditorWindow(restarted)
            restarted_dialog = restarted_window.create_settings_dialog()
            restarted_window.show()
            restarted_dialog.show()
            self._events()
            restarted_window_chip, _state, restarted_settings_chip, _other = (
                self._threshold_entries(restarted_window, restarted_dialog)
            )
            self.assertIn("83%", restarted_window_chip.text())
            self.assertIn("83%", restarted_settings_chip.text())
            restarted_dialog.close()
            restarted_window.close()


if __name__ == "__main__":
    unittest.main()
