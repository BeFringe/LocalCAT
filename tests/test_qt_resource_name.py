"""Qt increment 6.1: inline names through the existing resource update path."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QToolButton, QWidget

from capability_host import CapabilityHost
from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController, _initial_tm_activation_service
from editor_tm_adapter import EditorTMAdapter
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceError, ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import MigrationReport


class QtResourceNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _controller(self, root):
        repository = ResourceRepository(root / "app-data")
        for kind in (ResourceKind.TRANSLATION_MEMORY, ResourceKind.TERMBASE):
            for active in (True, False):
                resource = repository.create_resource(f"{kind.value} {active}", kind)
                resource.path.write_text(
                    '{"source":"Hello.","target":"Original"}\n'
                    if kind is ResourceKind.TRANSLATION_MEMORY else "Hello,你好\n",
                    encoding="utf-8",
                )
                repository.update_resource(replace(resource, active=active))
        controller = EditorController(repository)
        controller.set_project(EditorProject(name="Names", segments=(EditorSegment(id="1", source="Hello."),)))
        return controller, repository

    def _dialog(self, controller):
        dialog = QtSettingsDialog(controller)
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        return dialog

    def _editor(self, dialog, resource_id):
        editor = dialog.findChild(QLineEdit, f"resourceName_{resource_id}")
        self.assertIsNotNone(editor, "名称列必须提供行内输入")
        return editor

    def _draft(self, dialog, resource_id, text):
        editor = self._editor(dialog, resource_id)
        editor.setFocus()
        self.app.processEvents()
        editor.selectAll()
        QTest.keyClicks(editor, text)
        return editor

    def _enter(self, dialog, resource_id, text):
        editor = self._draft(dialog, resource_id, text)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertTrue(dialog.isVisible(), "Enter 不应关闭设置")

    def test_all_kinds_and_states_rename_trim_preserve_and_cold_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            dialog = self._dialog(controller)
            before = repository.list_resources()
            contents = {item.id: item.path.read_bytes() for item in before}
            for resource in before:
                self._enter(dialog, resource.id, "  Shared name  ")
                self.assertEqual(repository.get(resource.id), replace(resource, name="Shared name"))
                self.assertEqual(resource.path.read_bytes(), contents[resource.id])
                editor = self._editor(dialog, resource.id)
                self.assertEqual(editor.text(), "Shared name")
                self.assertIn("编辑资源名称", editor.toolTip())
                self.assertIn("名称", editor.accessibleName())
            self.assertEqual({item.resource_name for item in controller.suggestions().tm_matches}, {"Shared name"})
            self.assertEqual({item.resource_name for item in controller.term_suggestions()}, {"Shared name"})
            dialog.close()
            cold = EditorController(ResourceRepository(Path(temporary) / "app-data"))
            reopened = self._dialog(cold)
            for resource in before:
                self.assertEqual(self._editor(reopened, resource.id).text(), "Shared name")

    def test_names_are_reachable_with_tab_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            dialog = self._dialog(controller)
            expected = {f"resourceName_{item.id}" for item in repository.list_resources()}
            visited = set()
            dialog.new_resource_button.setFocus()
            for _ in range(100):
                focused = self.app.focusWidget()
                self.assertIsNotNone(focused)
                visited.add(focused.objectName())
                QTest.keyClick(focused, Qt.Key.Key_Tab)
                self.app.processEvents()
                if expected <= visited:
                    break
            self.assertTrue(expected <= visited, expected - visited)

    def test_focus_loss_save_keeps_clicked_next_name_ready_to_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            first, second = tuple(item for item in repository.list_resources() if item.active)
            dialog = self._dialog(controller)
            next_editor = self._editor(dialog, second.id)
            first_editor = self._draft(dialog, first.id, "First name")
            QTest.mouseClick(next_editor, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertEqual(repository.get(first.id).name, "First name")
            self.assertIs(self.app.focusWidget(), next_editor)
            next_editor.selectAll()
            QTest.keyClicks(next_editor, "Second name")
            QTest.keyClick(next_editor, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(repository.get(second.id).name, "Second name")
            self.assertIs(self._editor(dialog, first.id), first_editor)

    def test_focus_loss_save_keeps_tab_destination_and_continuous_editing(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            first = repository.list_resources()[0]
            dialog = self._dialog(controller)
            editor = self._editor(dialog, first.id)
            editor.setFocus()
            QTest.keyClick(editor, Qt.Key.Key_Tab)
            self.app.processEvents()
            expected_next = self.app.focusWidget()
            self._draft(dialog, first.id, "Tab first")
            QTest.keyClick(editor, Qt.Key.Key_Tab)
            self.app.processEvents()
            self.assertEqual(repository.get(first.id).name, "Tab first")
            self.assertIs(self.app.focusWidget(), expected_next)
            for _ in range(50):
                focused = self.app.focusWidget()
                if isinstance(focused, QLineEdit) and focused is not editor:
                    break
                QTest.keyClick(focused, Qt.Key.Key_Tab)
                self.app.processEvents()
            else:
                self.fail("Tab 应接续到另一资源名称")
            second_id = focused.objectName().removeprefix("resourceName_")
            focused.selectAll()
            QTest.keyClicks(focused, "Tab second")
            QTest.keyClick(focused, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(repository.get(second_id).name, "Tab second")

    def test_name_projection_updates_tooltips_menus_and_action_resource(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            dialog = self._dialog(controller)
            for resource in repository.list_resources():
                self._enter(dialog, resource.id, "Current display name")
                more = dialog.findChild(QToolButton, f"more_{resource.id}")
                self.assertIn("Current display name", more.toolTip())
                self.assertIn("Current display name", more.accessibleName())
                self.assertIn("Current display name", more.menu().accessibleName())
                if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                    for name in (f"tmResource_{resource.id}", f"tmStatus_{resource.id}"):
                        item = dialog.findChild(QWidget, name)
                        self.assertIn("Current display name", item.toolTip())
                        self.assertIn("Current display name", item.accessibleName())
                else:
                    action = dialog.findChild(QAction, f"manageTerms_{resource.id}")
                    self.assertIn("Current display name", action.toolTip())
                with patch.object(dialog, "_prompt_export_direct") as prompt:
                    dialog.findChild(QAction, f"exportDirect_{resource.id}").trigger()
                self.assertEqual(prompt.call_args.args[0], repository.get(resource.id))

    def test_focus_loss_saves_once_and_unchanged_enter_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            with patch.object(controller, "update_resource", wraps=controller.update_resource) as update:
                self._draft(dialog, resource.id, "Focus name")
                dialog.close_button.setFocus()
                self.app.processEvents()
                self.assertEqual(repository.get(resource.id).name, "Focus name")
                self._enter(dialog, resource.id, "Focus name")
                dialog.close_button.setFocus()
                self.app.processEvents()
                self.assertEqual(update.call_count, 1)

    def test_escape_cancels_without_closing_and_empty_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            with patch.object(controller, "update_resource", wraps=controller.update_resource) as update:
                editor = self._draft(dialog, resource.id, "Discard")
                QTest.keyClick(editor, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertEqual(editor.text(), resource.name)
                dialog.close_button.setFocus()
                self.app.processEvents()
                self._enter(dialog, resource.id, "   ")
                self.assertEqual(self._editor(dialog, resource.id).text(), resource.name)
                self.assertIn("不能为空", dialog.status_label.text())
                self.assertEqual(repository.get(resource.id), resource)
                update.assert_not_called()

    def test_refresh_discards_draft_and_submit_uses_latest_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            with patch.object(controller, "update_resource", wraps=controller.update_resource) as update:
                self._draft(dialog, resource.id, "Never publish")
                dialog._render_cached_resources()
                self.app.processEvents()
                update.assert_not_called()
                self.assertEqual(self._editor(dialog, resource.id).text(), resource.name)
            editor = self._draft(dialog, resource.id, "Current flags")
            current = controller.update_resource(replace(resource, lookup=False, update=False))
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(repository.get(resource.id), replace(current, name="Current flags"))

    def test_import_busy_and_canonical_busy_disable_names_and_discard_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            with patch.object(controller, "update_resource", wraps=controller.update_resource) as update:
                self._draft(dialog, resource.id, "Busy draft")
                dialog._set_import_busy(True, "正在导入")
                self.app.processEvents()
                self.assertFalse(self._editor(dialog, resource.id).isEnabled())
                update.assert_not_called()
                dialog._render_cached_resources()
                self.assertFalse(self._editor(dialog, resource.id).isEnabled())
                dialog._set_import_busy(False)
                self.assertTrue(self._editor(dialog, resource.id).isEnabled())
                self.assertEqual(self._editor(dialog, resource.id).text(), resource.name)
                operation = SimpleNamespace(completed=False, operation_id="busy", resource_id=resource.id)
                with patch.object(controller, "tm_activation_operation", return_value=operation):
                    dialog._render_cached_resources()
                    self.assertTrue(all(not self._editor(dialog, item.id).isEnabled() for item in repository.list_resources()))
                dialog._tm_operation_timer.stop()

    def test_pre_persist_failure_keeps_old_name_and_reports_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            registry_before = repository.registry_path.read_bytes()
            with patch.object(repository, "_write_registry_bound", side_effect=ResourceError("写入失败")):
                self._enter(dialog, resource.id, "Not saved")
            self.assertEqual(repository.registry_path.read_bytes(), registry_before)
            self.assertEqual(repository.get(resource.id), resource)
            self.assertEqual(self._editor(dialog, resource.id).text(), resource.name)
            self.assertIn("写入失败", dialog.status_label.text())

    def test_post_persist_runtime_failure_shows_saved_name_and_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, repository = self._controller(Path(temporary))
            resource = repository.list_resources()[0]
            dialog = self._dialog(controller)
            with patch.object(controller, "_load_glossary_engine", side_effect=ValueError("private detail")):
                self._enter(dialog, resource.id, "Saved name")
            self.assertEqual(repository.get(resource.id).name, "Saved name")
            self.assertEqual(self._editor(dialog, resource.id).text(), "Saved name")
            self.assertIn("已保存", dialog.status_label.text())
            self.assertIn("刷新失败", dialog.status_label.text())
            self.assertNotIn("回滚", dialog.status_label.text())
            self.assertNotIn("private detail", dialog.status_label.text())

    def test_canonical_rename_keeps_coordinator_records_query_and_writeback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository = ResourceRepository(root / "data")
            resource = repository.create_resource("Canonical", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text('{"source":"Hello.","target":"Original"}\n', encoding="utf-8")
            outcome = _initial_tm_activation_service(resource).activate_initial(resource.path, resource.id)
            self.assertIs(type(outcome), MigrationReport)
            runtime = TMRuntimeHost(resolver=TMResourceResolver(), configs=repository.list_resources())
            adapter = EditorTMAdapter(runtime_host=runtime, capability_host=CapabilityHost(evaluated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc)))
            controller = EditorController(repository, tm_adapter=adapter)
            controller.set_project(EditorProject(name="Canonical", segments=(EditorSegment(id="1", source="Hello."),)))
            store = runtime.snapshot().canonical_ports[0].handle.store
            coordinator = store.coordinator
            records = store.exact_records("Hello.")
            original = resource.path.read_bytes()
            dialog = self._dialog(controller)
            with patch.object(controller, "prepare_tm_activation", side_effect=AssertionError("rename must not activate")):
                self._enter(dialog, resource.id, "Renamed canonical")
            current_store = runtime.snapshot().canonical_ports[0].handle.store
            self.assertIs(current_store.coordinator, coordinator)
            self.assertEqual(current_store.exact_records("Hello."), records)
            self.assertEqual(resource.path.read_bytes(), original)
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].provenance.resource_name, "Renamed canonical")
            controller.set_project(EditorProject(name="Write", segments=(EditorSegment(id="2", source="New source"),)))
            controller.update_target("New target")
            controller.confirm_current()
            self.assertEqual(current_store.exact_records("New source")[0].target_raw, "New target")
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].provenance.resource_name, "Renamed canonical")
