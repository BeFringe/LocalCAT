"""Task 6.4 Qt Layer 4 boundary and accessibility journeys."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMessageBox,
    QPushButton,
    QToolButton,
)

from editor_contracts import EditorProject, EditorSegment
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from tests.test_editor_controller_tm_apply import (
    _canonical_controller,
    _legacy_fixture,
)
from tests.test_qt_settings_tm_lifecycle import _controller as _lifecycle_controller


ROOT = Path(__file__).resolve().parents[1]
QT_LAYER_FILES = (
    "qt_editor_window.py",
    "qt_settings_dialog.py",
    "qt_tm_threshold.py",
)
FORBIDDEN_IMPLEMENTATION_IMPORTS = {
    "capability_host",
    "capability_gated_text_matcher",
    "editor_tm_adapter",
    "glossary_engine",
    "matcher_capability",
    "matcher_validation",
    "resource_importer",
    "resource_repository",
    "termbase_store",
    "text_matcher",
    "tm_activation_journal",
    "tm_activation_recovery",
    "tm_application_composition",
    "tm_benchmark",
    "tm_benchmark_gate",
    "tm_candidate_index",
    "tm_content_attestation",
    "tm_engine",
    "tm_gate_a",
    "tm_gate_b",
    "tm_json_importer",
    "tm_migration",
    "tm_retrieval",
    "tm_retrieval_capability",
    "tm_retrieval_validation",
    "tm_schema_upgrade",
    "tm_similarity",
    "tm_snapshot_artifacts",
    "tm_snapshot_recovery",
    "tm_sqlite_store",
    "tm_stage_sealer",
    "workspace_state",
}
FORBIDDEN_IMPLEMENTATION_MARKERS = (
    "evaluator",
    "migration",
    "proof",
    "retrieval",
    "store",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            imported.add(node.args[0].value.split(".", 1)[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            imported.add(node.args[0].value.split(".", 1)[0])
    return imported


class QtTMLayerBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_qt_layer_has_no_core_implementation_imports(self) -> None:
        for relative in QT_LAYER_FILES:
            with self.subTest(relative=relative):
                imported = _imports(ROOT / relative)
                self.assertEqual(
                    imported & FORBIDDEN_IMPLEMENTATION_IMPORTS,
                    set(),
                )
                marked = {
                    name
                    for name in imported
                    if any(
                        marker in name
                        for marker in FORBIDDEN_IMPLEMENTATION_MARKERS
                    )
                }
                self.assertEqual(marked, set())

    def test_tm_cards_and_persistent_states_are_accessible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            window.show()
            self._events()

            threshold_state = window.findChild(QLabel, "tmThresholdState")
            self.assertIsNotNone(threshold_state)
            assert threshold_state is not None
            self.assertTrue(threshold_state.accessibleName())
            self.assertTrue(threshold_state.toolTip())

            report = window.current_tm_report
            self.assertIsNotNone(report)
            assert report is not None
            buttons = tuple(
                window.findChild(QPushButton, f"applyTm_{index}")
                for index in range(len(report.suggestions))
            )
            self.assertTrue(buttons)
            self.assertTrue(all(button is not None for button in buttons))
            for button in buttons:
                assert button is not None
                self.assertTrue(button.objectName())
                self.assertTrue(button.accessibleName())
                self.assertTrue(button.toolTip())
                self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)

            first, second = buttons[:2]
            assert first is not None
            assert second is not None
            window.tm_threshold_chip.setFocus()
            for _step in range(8):
                if first.hasFocus():
                    break
                focused = QApplication.focusWidget()
                self.assertIsNotNone(focused)
                assert focused is not None
                QTest.keyClick(focused, Qt.Key.Key_Tab)
                self._events()
            self.assertTrue(first.hasFocus())
            with patch.object(
                window,
                "apply_tm_suggestion",
                return_value=True,
            ) as apply:
                first.setFocus()
                QTest.keyClick(first, Qt.Key.Key_Return)
                second.setFocus()
                QTest.keyClick(second, Qt.Key.Key_Space)
            self.assertEqual(apply.call_count, 2)
            window.close()

    def test_suggestion_state_exposes_persistent_accessible_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = _legacy_fixture(
                Path(temporary)
            )
            controller.set_project(
                EditorProject(
                    name="No TM match",
                    segments=(
                        EditorSegment(id="none", source="No reusable translation"),
                    ),
                )
            )
            window = QtEditorWindow(controller)
            state = window.findChild(QLabel, "tmSuggestionState")
            self.assertIsNotNone(state)
            assert state is not None
            self.assertTrue(state.text())
            self.assertEqual(state.accessibleName(), state.text())
            self.assertEqual(state.toolTip(), state.text())
            window.close()

    def test_lifecycle_state_and_more_menu_have_keyboard_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _lifecycle_controller(Path(temporary))
            dialog = QtSettingsDialog(controller)
            dialog.show()
            self._events()

            state = dialog.findChild(QLabel, f"tmStatus_{resource_id}")
            more = dialog.findChild(QToolButton, f"more_{resource_id}")
            action = dialog.findChild(QAction, f"tmLifecycleAction_{resource_id}")
            self.assertIsNotNone(state)
            self.assertIsNotNone(more)
            self.assertIsNotNone(action)
            assert state is not None
            assert more is not None
            assert action is not None
            self.assertTrue(state.accessibleName())
            self.assertTrue(state.toolTip())
            self.assertTrue(more.accessibleName())
            self.assertTrue(more.toolTip())
            self.assertTrue(action.objectName())
            self.assertTrue(action.toolTip())
            self.assertEqual(more.focusPolicy(), Qt.FocusPolicy.StrongFocus)

            dialog.tm_threshold_chip.setFocus()
            for _step in range(16):
                if more.hasFocus():
                    break
                focused = QApplication.focusWidget()
                self.assertIsNotNone(focused)
                assert focused is not None
                QTest.keyClick(focused, Qt.Key.Key_Tab)
                self._events()
            self.assertTrue(more.hasFocus())

            persistent_state = state.text()
            menu = more.menu()
            self.assertIsNotNone(menu)
            assert menu is not None

            def activate_lifecycle_action() -> None:
                menu.setActiveAction(action)
                QTest.keyClick(menu, Qt.Key.Key_Return)

            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as confirm:
                for key in (Qt.Key.Key_Return, Qt.Key.Key_Space):
                    more.setFocus()
                    QTimer.singleShot(0, activate_lifecycle_action)
                    QTest.keyClick(more, key)
                    self._events()
            self.assertEqual(confirm.call_count, 2)
            self.assertEqual(state.text(), persistent_state)
            self.assertIn("已取消", dialog.status_label.text())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
