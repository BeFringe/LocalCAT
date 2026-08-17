from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TermSuggestion,
)
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow, render_highlighted_source
from resource_repository import ResourceRepository


class QtEditorSuggestionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(
        self,
        root: Path,
        *,
        writable_terms: bool = True,
    ) -> QtEditorWindow:
        repository = ResourceRepository(root / "app-data")
        tm = repository.create_resource("Client TM", ResourceKind.TRANSLATION_MEMORY)
        source = "<script>alert(1)</script> The office"
        tm.path.write_text(
            json.dumps({"source": source, "target": "脚本 The 办公室"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        terms = repository.create_resource("Client terms", ResourceKind.TERMBASE)
        terms.path.write_text("office,办公室\n", encoding="utf-8-sig")
        if not writable_terms:
            from dataclasses import replace

            repository.update_resource(replace(terms, update=False))
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(name="Suggestions", segments=(EditorSegment(id="1", source=source),))
        )
        return QtEditorWindow(controller)

    def _events(self) -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_renders_provenanced_cards_and_escaped_highlight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            window.show()
            self._events()
            rendered = window.source_display.toHtml().lower()
            labels = [label.text() for label in window.findChildren(QLabel)]

            self.assertNotIn("<script>", rendered)
            self.assertEqual(window.source_display.toPlainText(), "<script>alert(1)</script> The office")
            self.assertIn("background-color", rendered)
            self.assertTrue(any("Client TM" in label for label in labels))
            self.assertIn("Client terms", labels)
            self.assertIsNotNone(window.findChild(QPushButton, "applyTm_0"))
            self.assertIsNotNone(window.findChild(QPushButton, "insertTerm_0"))
            window.close()

    def test_apply_tm_and_insert_term_do_not_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(Path(temp_dir))
            tm = window.current_suggestions.tm_matches[0]
            term = window.current_suggestions.terms[0]

            self.assertTrue(window.apply_tm_suggestion(tm))
            self.assertEqual(window.target_editor.toPlainText(), "脚本 The 办公室")
            window.target_editor.moveCursor(window.target_editor.textCursor().MoveOperation.End)
            self.assertTrue(window.insert_term_suggestion(term))

            self.assertTrue(window.target_editor.toPlainText().endswith("办公室"))
            self.assertFalse(window.controller.current_segment.confirmed)
            window.controller.save_project(Path(temp_dir) / "cleanup.json")
            window.close()

    def test_add_term_refreshes_current_sentence_and_reports_missing_writable_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            window = self._window(root)

            self.assertTrue(window.add_term("The", "该"))
            self.assertEqual(
                {term.source_term for term in window.current_suggestions.terms},
                {"The", "office"},
            )
            window.close()

            blocked = self._window(root / "blocked", writable_terms=False)
            errors: list[tuple[str, str]] = []
            blocked._show_error = lambda title, message: errors.append((title, message))
            self.assertFalse(blocked.add_term("The", "该"))
            self.assertEqual(errors[0][0], "无法添加术语")
            self.assertRegex(
                errors[0][1],
                r"语言资源设置.*术语表.*Active.*Update",
            )

            errors.clear()
            self.assertFalse(blocked.add_term("", "该"))
            self.assertEqual(errors[0][0], "无法添加术语")
            self.assertIn("不能为空", errors[0][1])
            self.assertNotIn("语言资源设置", errors[0][1])
            blocked.close()

    def test_longest_non_overlapping_term_highlight_is_deterministic(self) -> None:
        terms = (
            TermSuggestion(
                source_term="office",
                target_term="办公室",
                start_index=4,
                end_index=10,
                resource_id="terms",
                resource_name="Terms",
            ),
            TermSuggestion(
                source_term="The office",
                target_term="该办公室",
                start_index=0,
                end_index=10,
                resource_id="terms",
                resource_name="Terms",
            ),
        )

        rendered = render_highlighted_source("The office", terms)

        self.assertEqual(rendered.count("background-color"), 1)
        self.assertIn("The office", rendered)


if __name__ == "__main__":
    unittest.main()
