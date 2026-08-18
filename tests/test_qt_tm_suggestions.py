"""Task 6.1 Qt current-segment TM suggestion surface journeys."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from editor_contracts import EditorProject, EditorSegment
from qt_editor_window import QtEditorWindow
from tests.test_editor_controller_tm_apply import (
    _canonical_controller,
    _legacy_fixture,
)
from tm_contracts import TMMatchType


class QtTMSuggestionSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_authentic_cards_show_three_match_types_without_repeating_query_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            with patch.object(
                controller,
                "suggestions",
                side_effect=AssertionError("legacy TM query must not run"),
            ):
                window = QtEditorWindow(controller)
            window.show()
            self._events()

            report = window.current_tm_report
            self.assertIsNotNone(report)
            assert report is not None
            by_type = {item.match_type: item for item in report.suggestions}
            self.assertTrue(
                {
                    TMMatchType.EXACT,
                    TMMatchType.CONTEXT,
                    TMMatchType.FUZZY,
                }.issubset(by_type)
            )

            cards = {
                label.text(): label.parentWidget()
                for label in window.findChildren(QLabel)
                if label.objectName().startswith("tmMatchType_")
            }
            self.assertTrue({"EXACT", "CONTEXT", "FUZZY"}.issubset(cards))
            for match_type in (
                TMMatchType.EXACT,
                TMMatchType.CONTEXT,
                TMMatchType.FUZZY,
            ):
                suggestion = by_type[match_type]
                card = cards[match_type.value]
                self.assertIsNotNone(card)
                assert card is not None
                labels = [label.text() for label in card.findChildren(QLabel)]
                self.assertIn(suggestion.target, labels)
                self.assertIn(suggestion.provenance.resource_name, labels)
                self.assertNotIn(suggestion.query_source, labels)
                self.assertIsNotNone(
                    card.findChild(QPushButton, f"applyTm_{report.suggestions.index(suggestion)}")
                )
                percentage = f"{round(suggestion.final_similarity * 100)}%"
                self.assertIn(percentage, labels)

            fuzzy = by_type[TMMatchType.FUZZY]
            fuzzy_card = cards["FUZZY"]
            assert fuzzy_card is not None
            fuzzy_labels = [label.text() for label in fuzzy_card.findChildren(QLabel)]
            self.assertIn(f"实际命中原文：{fuzzy.matched_source}", fuzzy_labels)
            self.assertNotEqual(fuzzy.query_source, fuzzy.matched_source)
            window.close()

    def test_each_card_applies_explicitly_and_rejection_stays_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            report = window.current_tm_report
            self.assertIsNotNone(report)
            assert report is not None
            fuzzy_index = next(
                index
                for index, suggestion in enumerate(report.suggestions)
                if suggestion.match_type is TMMatchType.FUZZY
            )
            fuzzy = report.suggestions[fuzzy_index]
            button = window.findChild(QPushButton, f"applyTm_{fuzzy_index}")
            self.assertIsNotNone(button)
            assert button is not None

            button.click()
            self._events()

            self.assertEqual(window.target_editor.toPlainText(), fuzzy.target)
            self.assertFalse(controller.current_segment.confirmed)
            self.assertIn("已应用", window.statusBar().currentMessage())

            old_exact = next(
                suggestion
                for suggestion in report.suggestions
                if suggestion.match_type is TMMatchType.EXACT
            )
            outcome = controller.update_tm_minimum_similarity(0.83)
            self.assertTrue(outcome.succeeded)

            self.assertFalse(window.apply_tm_suggestion(old_exact))
            self.assertIn("未应用", window.statusBar().currentMessage())
            controller.save_project(Path(temporary) / "cleanup.json")
            window.close()

    def test_no_match_capability_closed_and_resource_failure_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            no_match_root = Path(temporary) / "no-match"
            no_match_root.mkdir()
            no_match_controller, _runtime, _composition = _canonical_controller(
                self,
                no_match_root,
            )
            no_match_controller.set_project(
                EditorProject(
                    name="No match",
                    segments=(
                        EditorSegment(
                            id="no-match",
                            source="unrelated sentence with no reusable translation",
                        ),
                    ),
                )
            )
            no_match = QtEditorWindow(no_match_controller)
            no_match_state = no_match.findChild(QLabel, "tmSuggestionState")
            self.assertIsNotNone(no_match_state)
            assert no_match_state is not None
            self.assertEqual(
                no_match_state.text(),
                "当前段暂无翻译记忆建议。",
            )
            no_match.close()

        with tempfile.TemporaryDirectory() as temporary:
            capability_controller, _adapter, _runtime, _repository = _legacy_fixture(
                Path(temporary) / "capability"
            )
            capability_controller.set_project(
                EditorProject(
                    name="Capability closed",
                    segments=(EditorSegment(id="closed", source="No exact match"),),
                )
            )
            capability_closed = QtEditorWindow(capability_controller)
            capability_state = capability_closed.findChild(
                QLabel,
                "tmSuggestionState",
            )
            self.assertIsNotNone(capability_state)
            assert capability_state is not None
            capability_text = capability_state.text()
            self.assertIn("匹配能力", capability_text)
            self.assertNotIn("暂无", capability_text)
            capability_closed.close()

        with tempfile.TemporaryDirectory() as temporary:
            resource_controller, _adapter, runtime, _repository = _legacy_fixture(
                Path(temporary) / "resource"
            )
            backend = runtime.snapshot().legacy_ports[0].backend
            with patch.object(backend, "query_exact", side_effect=OSError):
                resource_failure = QtEditorWindow(resource_controller)
                resource_state = resource_failure.findChild(
                    QLabel,
                    "tmSuggestionState",
                )
                self.assertIsNotNone(resource_state)
                assert resource_state is not None
                resource_text = resource_state.text()
            self.assertIn("资源查询失败", resource_text)
            self.assertNotIn("暂无", resource_text)
            resource_failure.close()


if __name__ == "__main__":
    unittest.main()
