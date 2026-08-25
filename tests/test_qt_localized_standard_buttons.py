from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QInputDialog,
    QMessageBox,
    QWidget,
)

from editor_contracts import EditorProject, EditorSegment, TMPreferences
from editor_controller import EditorController
from qt_localized_message_box import (
    ask_localized_question,
    show_localized_critical,
)
from qt_speaker_inventory_dialog import QtSpeakerInventoryDialog
from qt_tm_threshold import prompt_tm_threshold
from resource_repository import ResourceRepository


_ROOT = Path(__file__).resolve().parents[1]


class QtLocalizedStandardButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_question_and_critical_buttons_are_application_owned(self) -> None:
        observed: dict[str, tuple[str, ...]] = {}

        def cancel_question() -> None:
            prompt = next(
                widget
                for widget in QApplication.topLevelWidgets()
                if isinstance(widget, QMessageBox)
            )
            observed["question"] = (
                prompt.button(QMessageBox.StandardButton.Apply).text(),
                prompt.button(QMessageBox.StandardButton.Cancel).text(),
            )
            prompt.button(QMessageBox.StandardButton.Cancel).click()

        QTimer.singleShot(0, cancel_question)
        decision = ask_localized_question(
            None,
            title="确认",
            text="是否应用？",
            buttons=(
                QMessageBox.StandardButton.Apply
                | QMessageBox.StandardButton.Cancel
            ),
            default_button=QMessageBox.StandardButton.Cancel,
            button_labels={
                QMessageBox.StandardButton.Apply: "应用",
                QMessageBox.StandardButton.Cancel: "取消",
            },
        )
        self.assertEqual(decision, QMessageBox.StandardButton.Cancel)
        self.assertEqual(observed["question"], ("应用", "取消"))

        def accept_critical() -> None:
            prompt = next(
                widget
                for widget in QApplication.topLevelWidgets()
                if isinstance(widget, QMessageBox)
            )
            observed["critical"] = (
                prompt.button(QMessageBox.StandardButton.Ok).text(),
            )
            prompt.button(QMessageBox.StandardButton.Ok).click()

        QTimer.singleShot(0, accept_critical)
        show_localized_critical(None, title="错误", text="失败")
        self.assertEqual(observed["critical"], ("确定",))

    def test_fuzzy_threshold_and_speaker_close_are_localized(self) -> None:
        observed: dict[str, tuple[str, str]] = {}
        parent = QWidget()

        def cancel_threshold() -> None:
            dialog = next(
                widget
                for widget in QApplication.topLevelWidgets()
                if isinstance(widget, QInputDialog)
            )
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            observed["threshold"] = (
                buttons.button(QDialogButtonBox.StandardButton.Ok).text(),
                buttons.button(QDialogButtonBox.StandardButton.Cancel).text(),
            )
            buttons.button(QDialogButtonBox.StandardButton.Cancel).click()

        QTimer.singleShot(0, cancel_threshold)
        self.assertIsNone(prompt_tm_threshold(parent, TMPreferences()))
        self.assertEqual(observed["threshold"], ("确定", "取消"))
        parent.close()

        with tempfile.TemporaryDirectory() as temporary:
            controller = EditorController(
                ResourceRepository(Path(temporary) / "app-data")
            )
            controller.set_project(
                EditorProject(
                    name="speaker",
                    segments=(EditorSegment(id="1", source="hello"),),
                    path=(Path(temporary) / "speaker.json").resolve(),
                )
            )
            dialog = QtSpeakerInventoryDialog(controller)
            buttons = dialog.findChild(QDialogButtonBox, "speakerInventoryButtons")
            self.assertIsNotNone(buttons)
            assert buttons is not None
            self.assertEqual(
                buttons.button(QDialogButtonBox.StandardButton.Close).text(),
                "关闭",
            )
            dialog.close()

    def test_production_does_not_use_static_platform_labeled_prompts(self) -> None:
        forbidden = {
            ("QMessageBox", "question"),
            ("QMessageBox", "critical"),
            ("QMessageBox", "warning"),
            ("QMessageBox", "information"),
            ("QInputDialog", "getDouble"),
        }
        findings: list[str] = []
        for path in sorted(_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func,
                    ast.Attribute,
                ):
                    continue
                owner = node.func.value
                if not isinstance(owner, ast.Name):
                    continue
                if (owner.id, node.func.attr) in forbidden:
                    findings.append(f"{path.name}:{node.lineno}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
