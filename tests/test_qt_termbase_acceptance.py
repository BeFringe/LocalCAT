"""Fresh current-source acceptance for Qt Requirement 7."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import ClassVar, override
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from capability_host import CapabilityHostComposition
from editor_contracts import (
    ImportRequest,
    ResourceKind,
    TermCommitOutcome,
    TermCommitState,
    TermRowKind,
    TextMatcherState,
)
from editor_controller import EditorController
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from qt_termbase_dialog import QtTermbaseDialog
from resource_repository import ResourceRepository


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class QtTermbaseAcceptanceTests(unittest.TestCase):
    app: ClassVar[QApplication]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        existing = QApplication.instance()
        cls.app = existing if isinstance(existing, QApplication) else QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    @staticmethod
    def _validate_text_v1(composition: CapabilityHostComposition) -> None:
        _ = composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    @staticmethod
    def _write_project(path: Path, source: str) -> None:
        _ = path.write_text(
            json.dumps(
                {
                    "name": "Requirement 7 acceptance",
                    "source_locale": "en-US",
                    "target_locale": "zh-CN",
                    "segments": [
                        {
                            "id": "segment-1",
                            "source": source,
                            "target": "",
                            "speaker": "",
                            "confirmed": False,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _close_window(window: QtEditorWindow) -> None:
        window._confirm_unsaved = lambda: True
        window.close()

    @staticmethod
    def _row_for_source(dialog: QtTermbaseDialog, source: str) -> int:
        for row in range(dialog.term_table.rowCount()):
            item = dialog.term_table.item(row, 0)
            if item is not None and item.text() == source:
                return row
        raise AssertionError(f"missing term row for {source!r}")

    def test_mixed_pre_gate_and_text_v1_cjk_use_the_same_core_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource(
                "Mixed terms",
                ResourceKind.TERMBASE,
            )
            payload = (
                b"Cat,legacy-target\n"
                b"localcat-term-v1,term-cjk,\xe7\x8c\xab,"
                b"\xe7\x8c\xab-target,false,true\n"
                b"localcat-term-v1,term-bird,Bird,bird-target,true,false\n"
            )
            resource.path.write_bytes(payload)
            project_path = root / "project.json"
            self._write_project(
                project_path,
                "cat Catapult 小猫咪和猫 bird Bird",
            )
            controller, composition = _compose_editor_controller(repository)
            controller.open_project(project_path)
            window = QtEditorWindow(controller)
            window.show()
            self._events()

            self.assertIs(
                controller.term_matcher_display().state,
                TextMatcherState.UNAVAILABLE,
            )
            pre_gate = tuple(
                (
                    item.source_term,
                    item.target_term,
                    item.start_index,
                    item.end_index,
                )
                for item in window.current_suggestions.terms
            )
            self.assertEqual(
                pre_gate,
                (
                    ("Cat", "legacy-target", 4, 7),
                    ("猫", "猫-target", 14, 15),
                    ("猫", "猫-target", 17, 18),
                    ("Bird", "bird-target", 24, 28),
                ),
            )
            pre_gate_dialog = QtTermbaseDialog(
                controller,
                resource.id,
                resource.name,
            )
            cjk_row = self._row_for_source(pre_gate_dialog, "猫")
            pre_gate_dialog.term_table.setCurrentCell(cjk_row, 0)
            self._events()
            self.assertFalse(pre_gate_dialog.match_case_checkbox.isEnabled())
            self.assertFalse(pre_gate_dialog.whole_word_checkbox.isEnabled())
            self.assertIn(
                "已保存但尚不参与匹配",
                pre_gate_dialog.capability_label.text(),
            )
            pre_gate_dialog.close()

            self._validate_text_v1(composition)
            controller.reload_resources()
            window.refresh_suggestions()
            self._events()
            self.assertIs(
                controller.term_matcher_display().state,
                TextMatcherState.TEXT_V1_VALIDATED,
            )
            text_v1 = tuple(
                (
                    item.source_term,
                    item.target_term,
                    item.start_index,
                    item.end_index,
                )
                for item in window.current_suggestions.terms
            )
            self.assertEqual(text_v1, pre_gate)
            self.assertEqual(
                tuple(
                    (start, end)
                    for source, _target, start, end in text_v1
                    if source == "猫"
                ),
                ((14, 15), (17, 18)),
            )

            text_v1_dialog = QtTermbaseDialog(
                controller,
                resource.id,
                resource.name,
            )
            cjk_row = self._row_for_source(text_v1_dialog, "猫")
            text_v1_dialog.term_table.setCurrentCell(cjk_row, 0)
            self._events()
            self.assertTrue(text_v1_dialog.match_case_checkbox.isEnabled())
            self.assertTrue(text_v1_dialog.whole_word_checkbox.isEnabled())
            self.assertFalse(text_v1_dialog.match_case_checkbox.isChecked())
            self.assertTrue(text_v1_dialog.whole_word_checkbox.isChecked())
            self.assertTrue(resource.path.read_bytes().startswith(b"Cat,legacy-target\n"))
            self.assertEqual(resource.path.read_bytes(), payload)
            text_v1_dialog.close()
            self._close_window(window)

    def test_committed_qt_crud_refreshes_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource(
                "Project terms",
                ResourceKind.TERMBASE,
            )
            resource.path.write_bytes(b"Seed,seed-target\n")
            project_path = root / "project.json"
            self._write_project(project_path, "Fresh")
            controller, composition = _compose_editor_controller(repository)
            self._validate_text_v1(composition)
            controller.reload_resources()
            controller.open_project(project_path)
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            self.assertEqual(window.current_suggestions.terms, ())

            dialog = QtTermbaseDialog(
                controller,
                resource.id,
                resource.name,
            )
            dialog.terms_committed.connect(window.refresh_suggestions)
            QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
            dialog.source_input.setText("Fresh")
            dialog.target_input.setText("first-target")
            dialog.match_case_checkbox.setChecked(True)
            dialog.whole_word_checkbox.setChecked(False)
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._events()
            self.assertEqual(
                tuple(
                    item.target_term
                    for item in window.current_suggestions.terms
                ),
                ("first-target",),
            )
            dialog.target_input.setText("second-target")
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._events()
            self.assertEqual(
                tuple(
                    item.target_term
                    for item in window.current_suggestions.terms
                ),
                ("second-target",),
            )
            dialog.close()
            self._close_window(window)

            restarted, restarted_composition = _compose_editor_controller(
                repository
            )
            self._validate_text_v1(restarted_composition)
            restarted.reload_resources()
            restarted.open_project(project_path)
            restarted_window = QtEditorWindow(restarted)
            restarted_window.show()
            self._events()
            self.assertEqual(
                tuple(
                    item.target_term
                    for item in restarted_window.current_suggestions.terms
                ),
                ("second-target",),
            )
            restarted_dialog = QtTermbaseDialog(
                restarted,
                resource.id,
                resource.name,
            )
            fresh_row = self._row_for_source(restarted_dialog, "Fresh")
            restarted_dialog.term_table.setCurrentCell(fresh_row, 0)
            self._events()
            self.assertTrue(restarted_dialog.match_case_checkbox.isChecked())
            self.assertFalse(restarted_dialog.whole_word_checkbox.isChecked())
            restarted_dialog.terms_committed.connect(
                restarted_window.refresh_suggestions
            )
            with patch.object(
                restarted_dialog,
                "_confirm_delete",
                return_value=True,
            ):
                QTest.mouseClick(
                    restarted_dialog.delete_button,
                    Qt.MouseButton.LeftButton,
                )
            self._events()
            self.assertEqual(restarted_window.current_suggestions.terms, ())
            restarted_dialog.close()
            self._close_window(restarted_window)

            final_controller, final_composition = _compose_editor_controller(
                repository
            )
            self._validate_text_v1(final_composition)
            final_controller.reload_resources()
            final_controller.open_project(project_path)
            self.assertEqual(final_controller.term_suggestions(), ())
            self.assertNotIn(
                "Fresh",
                {
                    record.source
                    for record in final_controller.list_terms(resource.id)
                },
            )

    def test_noncommitted_states_never_refresh_or_replace_visible_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource(
                "Project terms",
                ResourceKind.TERMBASE,
            )
            resource.path.write_bytes(b"Legacy,old\n")
            controller, _composition = _compose_editor_controller(repository)
            recovery = root / "recovery.csv"
            outcomes = (
                TermCommitOutcome(
                    state=TermCommitState.NOT_COMMITTED,
                    report=None,
                    error_code="SOURCE_CHANGED",
                    retryable=True,
                    recovery_path=None,
                    quarantined=False,
                    safe_detail="Prepare the termbase change again before retrying.",
                ),
                TermCommitOutcome(
                    state=TermCommitState.ROLLED_BACK,
                    report=None,
                    error_code="DIRECTORY_FSYNC_FAILED",
                    retryable=True,
                    recovery_path=recovery,
                    quarantined=False,
                    safe_detail="The previous bytes were restored; retry the change.",
                ),
                TermCommitOutcome(
                    state=TermCommitState.INDETERMINATE,
                    report=None,
                    error_code="ROLLBACK_FAILED",
                    retryable=False,
                    recovery_path=recovery,
                    quarantined=True,
                    safe_detail=(
                        "Quarantine the resource and restore it from the recovery "
                        "file before retrying."
                    ),
                ),
            )
            for outcome in outcomes:
                with self.subTest(state=outcome.state):
                    dialog = QtTermbaseDialog(
                        controller,
                        resource.id,
                        resource.name,
                    )
                    rows_before = tuple(controller.list_terms(resource.id))
                    bytes_before = resource.path.read_bytes()
                    emissions: list[None] = []
                    dialog.terms_committed.connect(
                        lambda: emissions.append(None)
                    )
                    dialog.create_button.click()
                    dialog.source_input.setText("Attempted")
                    dialog.target_input.setText("new")
                    with patch.object(
                        controller,
                        "create_term",
                        return_value=outcome,
                    ):
                        dialog.save_button.click()
                    self.assertEqual(emissions, [])
                    self.assertEqual(
                        tuple(controller.list_terms(resource.id)),
                        rows_before,
                    )
                    self.assertEqual(resource.path.read_bytes(), bytes_before)
                    self.assertIn(
                        outcome.state.value,
                        dialog.feedback_label.text(),
                    )
                    self.assertNotIn(
                        outcome.safe_detail or "private-body",
                        dialog.feedback_label.text(),
                    )
                    if outcome.quarantined:
                        self.assertIn("隔离", dialog.feedback_label.text())
                    dialog.close()

    def test_import_preserves_v1_metadata_counts_and_restart_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource(
                "Project terms",
                ResourceKind.TERMBASE,
            )
            resource.path.write_bytes(
                b"Legacy,old\n"
                b"localcat-term-v1,term-1,Configured,configured-old,true,false\n"
            )
            controller, composition = _compose_editor_controller(repository)
            self._validate_text_v1(composition)
            controller.reload_resources()
            incoming = root / "incoming.csv"
            incoming.write_text(
                "Source,Target\n"
                "Configured,configured-first\n"
                "Fresh,fresh-new\n"
                "Configured,configured-final\n",
                encoding="utf-8-sig",
            )
            report = controller.import_resource(
                ImportRequest(
                    resource_id=resource.id,
                    input_path=incoming,
                )
            )
            records = controller.list_terms(resource.id)
            self.assertEqual(
                (
                    report.imported,
                    report.skipped,
                    report.overwritten,
                    report.errors,
                ),
                (2, 1, 2, ()),
            )
            self.assertEqual(
                tuple(
                    (
                        record.source,
                        record.target,
                        record.locator.row_kind,
                        record.record_id,
                        record.match_case,
                        record.whole_word,
                    )
                    for record in records
                ),
                (
                    ("Legacy", "old", TermRowKind.LEGACY, None, None, None),
                    (
                        "Configured",
                        "configured-final",
                        TermRowKind.V1,
                        "term-1",
                        True,
                        False,
                    ),
                    ("Fresh", "fresh-new", TermRowKind.LEGACY, None, None, None),
                ),
            )
            restarted, _ = _compose_editor_controller(repository)
            self.assertEqual(restarted.list_terms(resource.id), records)


if __name__ == "__main__":
    unittest.main()
