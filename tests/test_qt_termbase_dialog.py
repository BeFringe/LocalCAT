from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QMenu, QToolButton

from capability_host import CapabilityHostComposition
from editor_contracts import (
    ResourceKind,
    TermCommitOutcome,
    TermCommitState,
    TermRowKind,
    TextMatcherState,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from qt_termbase_dialog import QtTermbaseDialog
from resource_repository import ResourceRepository


ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class QtTermbaseDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        application = QApplication.instance()
        cls.app = (
            application if isinstance(application, QApplication) else QApplication([])
        )

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _controller(
        self,
        root: Path,
    ) -> tuple[
        EditorController,
        CapabilityHostComposition,
        ResourceRepository,
        str,
        str,
    ]:
        repository = ResourceRepository(root / "app-data")
        termbase = repository.create_resource("Project terminology", ResourceKind.TERMBASE)
        termbase.path.write_bytes(
            b"Legacy,legacy-target\n"
            b"localcat-term-v1,term-1,Configured,configured-target,true,false\n"
        )
        tm = repository.create_resource("Not a termbase", ResourceKind.TRANSLATION_MEMORY)
        controller, composition = _compose_editor_controller(repository)
        return controller, composition, repository, termbase.id, tm.id

    @staticmethod
    def _row_values(dialog: QtTermbaseDialog) -> tuple[tuple[str, ...], ...]:
        def cell_text(row: int, column: int) -> str:
            item = dialog.term_table.item(row, column)
            if item is None:
                raise AssertionError("term table cell must be populated")
            return item.text()

        return tuple(
            tuple(
                cell_text(row, column)
                for column in range(dialog.term_table.columnCount())
            )
            for row in range(dialog.term_table.rowCount())
        )

    def test_mixed_rows_preserve_legacy_policy_and_gate_configured_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, composition, _repository, resource_id, _tm_id = self._controller(
                Path(temporary)
            )
            dialog = QtTermbaseDialog(controller, resource_id, "Project terminology")
            dialog.show()
            self._events()

            self.assertIs(
                controller.term_matcher_display().state,
                TextMatcherState.UNAVAILABLE,
            )
            self.assertEqual(dialog.term_table.rowCount(), 2)
            self.assertEqual(
                self._row_values(dialog),
                (
                    (
                        "Legacy",
                        "legacy-target",
                        "Legacy",
                        "—",
                        "—",
                    ),
                    (
                        "Configured",
                        "configured-target",
                        "Configured",
                        "是",
                        "否",
                    ),
                ),
            )

            dialog.term_table.setCurrentCell(0, 0)
            self._events()
            self.assertFalse(dialog.match_case_checkbox.isVisible())
            self.assertFalse(dialog.whole_word_checkbox.isVisible())
            self.assertIn("Legacy", dialog.policy_label.text())
            self.assertIn("两列", dialog.policy_label.text())

            dialog.term_table.setCurrentCell(1, 0)
            self._events()
            self.assertTrue(dialog.match_case_checkbox.isVisible())
            self.assertTrue(dialog.whole_word_checkbox.isVisible())
            self.assertTrue(dialog.match_case_checkbox.isChecked())
            self.assertFalse(dialog.whole_word_checkbox.isChecked())
            self.assertFalse(dialog.match_case_checkbox.isEnabled())
            self.assertFalse(dialog.whole_word_checkbox.isEnabled())
            self.assertIn("已保存但尚不参与匹配", dialog.capability_label.text())

            _ = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            basic = QtTermbaseDialog(
                controller,
                resource_id,
                "Project terminology",
            )
            basic.term_table.setCurrentCell(1, 0)
            self._events()
            self.assertIs(
                controller.term_matcher_display().state,
                TextMatcherState.BASIC_VALIDATED,
            )
            self.assertFalse(basic.match_case_checkbox.isEnabled())
            self.assertFalse(basic.whole_word_checkbox.isEnabled())
            self.assertIn("已保存但尚不参与匹配", basic.capability_label.text())
            basic.close()

            _ = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            reopened = QtTermbaseDialog(
                controller,
                resource_id,
                "Project terminology",
            )
            reopened.term_table.setCurrentCell(1, 0)
            self._events()

            self.assertIs(
                controller.term_matcher_display().state,
                TextMatcherState.TEXT_V1_VALIDATED,
            )
            self.assertTrue(reopened.match_case_checkbox.isEnabled())
            self.assertTrue(reopened.whole_word_checkbox.isEnabled())
            self.assertIn("参与匹配", reopened.capability_label.text())
            reopened.close()
            dialog.close()

    def test_real_controller_crud_defaults_legacy_locator_and_restart_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _composition, repository, resource_id, _tm_id = self._controller(
                root
            )
            dialog = QtTermbaseDialog(controller, resource_id, "Project terminology")

            QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
            self.assertFalse(dialog.match_case_checkbox.isChecked())
            self.assertTrue(dialog.whole_word_checkbox.isChecked())
            dialog.source_input.setText("Fresh")
            dialog.target_input.setText("fresh-target")
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._events()

            created = next(
                record
                for record in controller.list_terms(resource_id)
                if record.source == "Fresh"
            )
            self.assertIs(created.locator.row_kind, TermRowKind.V1)
            self.assertFalse(created.match_case)
            self.assertTrue(created.whole_word)
            self.assertEqual(dialog.term_table.rowCount(), 3)
            self.assertIn("已保存", dialog.feedback_label.text())

            legacy_row = next(
                row
                for row in range(dialog.term_table.rowCount())
                if self._row_values(dialog)[row][0] == "Legacy"
            )
            dialog.term_table.setCurrentCell(legacy_row, 0)
            self._events()
            dialog.target_input.setText("legacy-updated")
            QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            self._events()
            legacy = next(
                record
                for record in controller.list_terms(resource_id)
                if record.source == "Legacy"
            )
            self.assertIs(legacy.locator.row_kind, TermRowKind.LEGACY)
            self.assertIsNone(legacy.match_case)
            self.assertIsNone(legacy.whole_word)

            fresh_row = next(
                row
                for row in range(dialog.term_table.rowCount())
                if self._row_values(dialog)[row][0] == "Fresh"
            )
            dialog.term_table.setCurrentCell(fresh_row, 0)
            self._events()
            with patch.object(
                dialog,
                "_confirm_delete",
                return_value=True,
            ):
                QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
            self._events()
            self.assertNotIn(
                "Fresh",
                {record.source for record in controller.list_terms(resource_id)},
            )
            committed_bytes = next(
                resource.path
                for resource in repository.list_resources()
                if resource.id == resource_id
            ).read_bytes()
            dialog.close()

            reopened_controller, _ = _compose_editor_controller(repository)
            reopened = QtTermbaseDialog(
                reopened_controller,
                resource_id,
                "Project terminology",
            )
            self.assertEqual(
                tuple(
                    (record.source, record.target, record.locator.row_kind)
                    for record in reopened_controller.list_terms(resource_id)
                ),
                (
                    ("Legacy", "legacy-updated", TermRowKind.LEGACY),
                    ("Configured", "configured-target", TermRowKind.V1),
                ),
            )
            resource_path = next(
                resource.path
                for resource in repository.list_resources()
                if resource.id == resource_id
            )
            self.assertEqual(resource_path.read_bytes(), committed_bytes)
            reopened.close()

    def test_noncommitted_outcomes_preserve_rows_selection_and_show_safe_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _composition, _repository, resource_id, _tm_id = self._controller(
                root
            )
            recovery = root / "term-recovery.csv"
            cases = (
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
            for outcome in cases:
                with self.subTest(state=outcome.state):
                    dialog = QtTermbaseDialog(
                        controller,
                        resource_id,
                        "Project terminology",
                    )
                    dialog.term_table.setCurrentCell(1, 0)
                    self._events()
                    rows_before = self._row_values(dialog)
                    selected_before = dialog.term_table.currentRow()
                    dialog.source_input.setText("Configured")
                    dialog.target_input.setText("attempted-change")

                    with (
                        patch.object(
                            controller,
                            "list_terms",
                            wraps=controller.list_terms,
                        ) as list_terms,
                        patch.object(
                            controller,
                            "update_term",
                            return_value=outcome,
                        ),
                    ):
                        dialog._save_term()

                    self.assertEqual(list_terms.call_count, 0)
                    self.assertEqual(self._row_values(dialog), rows_before)
                    self.assertEqual(dialog.term_table.currentRow(), selected_before)
                    self.assertIn(outcome.state.value, dialog.feedback_label.text())
                    self.assertIn(outcome.error_code or "", dialog.feedback_label.text())
                    self.assertNotIn(
                        outcome.safe_detail or "unreachable-private-body",
                        dialog.feedback_label.text(),
                    )
                    if outcome.recovery_path is not None:
                        self.assertIn(str(outcome.recovery_path), dialog.feedback_label.text())
                    if outcome.quarantined:
                        self.assertIn("隔离", dialog.feedback_label.text())
                    dialog.close()

    def test_keyboard_tab_space_and_enter_complete_configured_create(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, composition, _repository, resource_id, _tm_id = self._controller(
                Path(temporary)
            )
            _ = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            dialog = QtTermbaseDialog(controller, resource_id, "Project terminology")
            dialog.show()
            dialog.activateWindow()
            self._events()

            dialog.term_table.setFocus()
            QTest.keyClick(dialog.term_table, Qt.Key.Key_Tab)
            self._events()
            self.assertTrue(dialog.source_input.hasFocus())

            dialog.create_button.setFocus()
            QTest.keyClick(dialog.create_button, Qt.Key.Key_Space)
            self._events()
            self.assertTrue(dialog.source_input.hasFocus())
            QTest.keyClicks(dialog.source_input, "Keyboard")
            dialog.target_input.setFocus()
            QTest.keyClicks(dialog.target_input, "keyboard-target")

            self.assertTrue(dialog.match_case_checkbox.isEnabled())
            self.assertTrue(dialog.whole_word_checkbox.isEnabled())
            self.assertFalse(dialog.match_case_checkbox.isChecked())
            self.assertTrue(dialog.whole_word_checkbox.isChecked())

            dialog.match_case_checkbox.setFocus()
            QTest.keyClick(dialog.match_case_checkbox, Qt.Key.Key_Space)
            dialog.whole_word_checkbox.setFocus()
            QTest.keyClick(dialog.whole_word_checkbox, Qt.Key.Key_Space)
            self.assertTrue(dialog.match_case_checkbox.isChecked())
            self.assertFalse(dialog.whole_word_checkbox.isChecked())

            dialog.save_button.setFocus()
            with (
                patch.object(
                    controller,
                    "create_term",
                    wraps=controller.create_term,
                ) as create_term,
                patch.object(
                    controller,
                    "update_term",
                    side_effect=AssertionError("create must not require a second update"),
                ) as update_term,
            ):
                QTest.keyClick(dialog.save_button, Qt.Key.Key_Return)
            self._events()

            created = next(
                record
                for record in controller.list_terms(resource_id)
                if record.source == "Keyboard"
            )
            self.assertEqual(create_term.call_count, 1)
            self.assertEqual(update_term.call_count, 0)
            self.assertTrue(created.match_case)
            self.assertFalse(created.whole_word)
            self.assertIn("已保存", dialog.feedback_label.text())
            dialog.close()

    def test_pre_gate_create_is_one_default_save_with_flags_disabled(self) -> None:
        for state in (
            TextMatcherState.UNAVAILABLE,
            TextMatcherState.BASIC_VALIDATED,
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                (
                    controller,
                    composition,
                    _repository,
                    resource_id,
                    _tm_id,
                ) = self._controller(Path(temporary))
                if state is TextMatcherState.BASIC_VALIDATED:
                    _ = composition.matcher_validation_owner.validate_basic(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                    controller.reload_resources()
                dialog = QtTermbaseDialog(
                    controller,
                    resource_id,
                    "Project terminology",
                )
                dialog._begin_create()
                self.assertFalse(dialog.match_case_checkbox.isEnabled())
                self.assertFalse(dialog.whole_word_checkbox.isEnabled())
                self.assertFalse(dialog.match_case_checkbox.isChecked())
                self.assertTrue(dialog.whole_word_checkbox.isChecked())
                dialog.source_input.setText(f"PreGate-{state.value}")
                dialog.target_input.setText("default-target")

                with patch.object(
                    controller,
                    "create_term",
                    wraps=controller.create_term,
                ) as create_term:
                    dialog._save_term()

                self.assertEqual(create_term.call_count, 1)
                created = next(
                    record
                    for record in controller.list_terms(resource_id)
                    if record.source == f"PreGate-{state.value}"
                )
                self.assertFalse(created.match_case)
                self.assertTrue(created.whole_word)
                dialog.close()

    def test_invalid_duplicate_and_programmer_faults_are_not_washed_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _composition, _repository, resource_id, _tm_id = self._controller(
                Path(temporary)
            )
            dialog = QtTermbaseDialog(controller, resource_id, "Project terminology")
            rows_before = self._row_values(dialog)

            dialog._begin_create()
            dialog.source_input.setText("  ")
            dialog.target_input.setText("target")
            dialog._save_term()
            self.assertEqual(self._row_values(dialog), rows_before)
            self.assertIn("不能为空", dialog.feedback_label.text())

            dialog._begin_create()
            dialog.source_input.setText("Legacy")
            dialog.target_input.setText("duplicate")
            dialog._save_term()
            self.assertEqual(self._row_values(dialog), rows_before)
            self.assertIn("重复", dialog.feedback_label.text())

            dialog.term_table.setCurrentCell(1, 0)
            self._events()
            dialog.source_input.setText("Legacy")
            dialog.target_input.setText("conflicting update")
            dialog._save_term()
            self.assertEqual(self._row_values(dialog), rows_before)
            self.assertIn("冲突", dialog.feedback_label.text())

            dialog._begin_create()
            dialog.source_input.setText("Fresh")
            dialog.target_input.setText("fresh")
            with patch.object(
                controller,
                "create_term",
                side_effect=TypeError("programmer contract fault"),
            ):
                with self.assertRaisesRegex(TypeError, "programmer contract fault"):
                    dialog._save_term()
            with patch.object(
                controller,
                "create_term",
                side_effect=AssertionError("validator fault"),
            ):
                with self.assertRaisesRegex(AssertionError, "validator fault"):
                    dialog._save_term()

            dialog._begin_create()
            dialog.source_input.setText("Fresh")
            dialog.target_input.setText("fresh")
            with patch.object(
                controller,
                "create_term",
                side_effect=EditorControllerError("private source body"),
            ):
                dialog._save_term()
            self.assertNotIn("private source body", dialog.feedback_label.text())
            dialog.close()

    def test_settings_menu_exposes_keyboard_reachable_action_only_for_termbase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _composition, _repository, resource_id, tm_id = self._controller(
                Path(temporary)
            )
            settings = QtSettingsDialog(controller)
            term_more = settings.findChild(QToolButton, f"more_{resource_id}")
            tm_more = settings.findChild(QToolButton, f"more_{tm_id}")
            self.assertIsNotNone(term_more)
            self.assertIsNotNone(tm_more)
            assert term_more is not None and tm_more is not None
            term_menu = term_more.menu()
            tm_menu = tm_more.menu()
            self.assertIsInstance(term_menu, QMenu)
            self.assertIsInstance(tm_menu, QMenu)
            assert term_menu is not None and tm_menu is not None
            manage = next(
                (
                    action
                    for action in term_menu.actions()
                    if action.objectName() == f"manageTerms_{resource_id}"
                ),
                None,
            )
            self.assertIsNotNone(manage)
            assert manage is not None
            self.assertEqual(manage.text(), "管理术语")
            self.assertTrue(manage.isEnabled())
            self.assertFalse(
                any(
                    action.objectName() == f"manageTerms_{tm_id}"
                    for action in tm_menu.actions()
                )
            )

            opened: list[QtTermbaseDialog] = []

            def inspect_and_close() -> None:
                active = QApplication.activeModalWidget()
                self.assertIsInstance(active, QtTermbaseDialog)
                assert isinstance(active, QtTermbaseDialog)
                opened.append(active)
                self.assertEqual(active.accessibleName(), "Project terminology 术语管理")
                self.assertTrue(active.term_table.focusPolicy() & Qt.FocusPolicy.TabFocus)
                self.assertIsNotNone(
                    active.findChild(QCheckBox, "termMatchCase")
                )
                QTest.mouseClick(active.close_button, Qt.MouseButton.LeftButton)

            QTimer.singleShot(0, inspect_and_close)
            manage.trigger()
            self.assertEqual(len(opened), 1)
            settings.close()

    def test_main_termbase_tab_exposes_the_second_management_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _composition, _repository, resource_id, tm_id = (
                self._controller(root)
            )
            project_path = root / "project.json"
            _ = project_path.write_text(
                json.dumps(
                    {
                        "name": "Main term management entry",
                        "source_locale": "en-US",
                        "target_locale": "zh-CN",
                        "segments": [
                            {
                                "id": "segment-1",
                                "source": "Legacy appears here.",
                                "target": "",
                                "speaker": "",
                                "confirmed": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller.open_project(project_path)
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            window.suggestion_tabs.setCurrentWidget(window.termbase_page)

            button = window.manage_terms_button
            self.assertTrue(button.isVisible())
            self.assertEqual(button.text(), "管理术语")
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.toolTip())
            menu = button.menu()
            self.assertIsInstance(menu, QMenu)
            assert menu is not None
            actions = {
                action.objectName(): action for action in menu.actions()
            }
            self.assertIn(f"manageTermsMain_{resource_id}", actions)
            self.assertNotIn(f"manageTermsMain_{tm_id}", actions)
            action = actions[f"manageTermsMain_{resource_id}"]
            self.assertTrue(action.isEnabled())

            opened: list[QtTermbaseDialog] = []

            def inspect_and_close() -> None:
                active = QApplication.activeModalWidget()
                self.assertIsInstance(active, QtTermbaseDialog)
                assert isinstance(active, QtTermbaseDialog)
                opened.append(active)
                self.assertEqual(active.resource_id, resource_id)
                active.terms_committed.emit()
                QTest.mouseClick(active.close_button, Qt.MouseButton.LeftButton)

            with patch.object(
                window,
                "_term_suggestions_changed",
                wraps=window._term_suggestions_changed,
            ) as refreshed:
                QTimer.singleShot(0, inspect_and_close)
                action.trigger()
            self.assertEqual(refreshed.call_count, 1)
            self.assertEqual(len(opened), 1)
            window._confirm_unsaved = lambda: True
            window.close()

    def test_committed_crud_refreshes_current_window_suggestions_immediately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, composition, _repository, resource_id, _tm_id = (
                self._controller(root)
            )
            _ = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            project_path = root / "project.json"
            _ = project_path.write_text(
                json.dumps(
                    {
                        "name": "Term refresh journey",
                        "source_locale": "en-US",
                        "target_locale": "zh-CN",
                        "segments": [
                            {
                                "id": "segment-1",
                                "source": "Fresh appears here.",
                                "target": "",
                                "speaker": "",
                                "confirmed": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller.open_project(project_path)
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            self.assertEqual(window.current_suggestions.terms, ())
            settings = window.create_settings_dialog()
            resource = next(
                item
                for item in controller.list_resources()
                if item.id == resource_id
            )
            more = settings.findChild(QToolButton, f"more_{resource_id}")
            self.assertIsNotNone(more)
            assert more is not None and more.menu() is not None
            manage = next(
                action
                for action in more.menu().actions()
                if action.objectName() == f"manageTerms_{resource_id}"
            )
            observed: list[tuple[str, ...]] = []

            def operate_dialog() -> None:
                active = QApplication.activeModalWidget()
                if not isinstance(active, QtTermbaseDialog):
                    raise AssertionError("term dialog must be active")
                QTest.mouseClick(
                    active.create_button,
                    Qt.MouseButton.LeftButton,
                )
                active.source_input.setText("Fresh")
                active.target_input.setText("first-target")
                QTest.mouseClick(
                    active.save_button,
                    Qt.MouseButton.LeftButton,
                )
                self._events()
                observed.append(
                    tuple(
                        item.target_term
                        for item in window.current_suggestions.terms
                    )
                )

                active.target_input.setText("second-target")
                QTest.mouseClick(
                    active.save_button,
                    Qt.MouseButton.LeftButton,
                )
                self._events()
                observed.append(
                    tuple(
                        item.target_term
                        for item in window.current_suggestions.terms
                    )
                )

                with patch.object(
                    active,
                    "_confirm_delete",
                    return_value=True,
                ):
                    QTest.mouseClick(
                        active.delete_button,
                        Qt.MouseButton.LeftButton,
                    )
                self._events()
                observed.append(
                    tuple(
                        item.target_term
                        for item in window.current_suggestions.terms
                    )
                )
                active.close()

            with patch.object(
                window,
                "refresh_suggestions",
                wraps=window.refresh_suggestions,
            ) as refresh:
                QTimer.singleShot(0, operate_dialog)
                manage.trigger()

            self.assertEqual(
                observed,
                [("first-target",), ("second-target",), ()],
            )
            self.assertEqual(refresh.call_count, 3)
            self.assertIsNone(QApplication.activeModalWidget())
            settings.close()
            window._confirm_unsaved = lambda: True
            window.close()

    def test_noncommitted_term_outcome_emits_no_refresh_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _composition, _repository, resource_id, _tm_id = (
                self._controller(root)
            )
            dialog = QtTermbaseDialog(
                controller,
                resource_id,
                "Project terminology",
            )
            emissions: list[None] = []
            dialog.terms_committed.connect(lambda: emissions.append(None))
            dialog.create_button.click()
            dialog.source_input.setText("Uncommitted")
            dialog.target_input.setText("unchanged")
            outcome = TermCommitOutcome(
                state=TermCommitState.NOT_COMMITTED,
                report=None,
                error_code="SOURCE_CHANGED",
                retryable=True,
                recovery_path=None,
                quarantined=False,
                safe_detail=(
                    "Prepare the termbase change again before retrying."
                ),
            )
            with patch.object(
                controller,
                "create_term",
                return_value=outcome,
            ):
                dialog.save_button.click()
            self.assertEqual(emissions, [])
            self.assertIn(
                TermCommitState.NOT_COMMITTED.value,
                dialog.feedback_label.text(),
            )
            dialog.close()

    def test_layer4_ast_boundary_forbids_store_engine_and_core_authorities(self) -> None:
        forbidden = {
            "capability_host",
            "configured_term_adapter",
            "glossary_engine",
            "resource_repository",
            "termbase_store",
            "tm_contracts",
            "tm_engine",
            "tm_sqlite_store",
        }
        for filename in ("qt_termbase_dialog.py", "qt_settings_dialog.py"):
            with self.subTest(filename=filename):
                tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
                imported: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".", 1)[0])
                self.assertTrue(forbidden.isdisjoint(imported), imported & forbidden)

    def test_term_dialog_never_calls_legacy_add_term_or_keeps_store_authority(self) -> None:
        source = (ROOT / "qt_termbase_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            {"add_term", "import_resource"}.intersection(called_attributes)
        )
        assigned_names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
        }
        self.assertFalse(
            {"store", "engine", "records", "resource_repository"}.intersection(
                assigned_names
            )
        )


if __name__ == "__main__":
    unittest.main()
