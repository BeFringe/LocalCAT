"""Operation notice lifetimes through real Qt event delivery."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QTextBrowser, QToolButton

from editor_contracts import EditorProject, EditorSegment, ResourceKind, WorkspaceMode
from editor_controller import EditorController, EditorControllerError
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from qt_preprocess_dialog import QtPreprocessDialog
from qt_termbase_dialog import QtTermbaseDialog
from qt_chunk_manager_dialog import QtChunkManagerDialog
from resource_repository import ResourceRepository
from resource_package_contracts import ResourceImportMode
from tests.test_multi_document_cluster4_qt import _export_package, _write_json
from tests.test_collaborative_chunks_cluster4_dialog import _view, _Facade


class QtTransientNoticesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = ResourceRepository(self.root / "data")
        self.controller = EditorController(self.repository)

    def _show(self, widget):
        self.addCleanup(widget.close)
        if isinstance(widget, QtEditorWindow):
            widget._confirm_unsaved = lambda: True
        widget.show()
        self.app.processEvents()
        return widget

    def _shorten(self, widget, attribute):
        timer = getattr(widget, attribute, None)
        self.assertIsNotNone(timer)
        self.assertEqual(timer.interval(), 7000)
        timer.setInterval(40)

    def _wait(self):
        QTest.qWait(90)

    def _workspace(self):
        root = self.root / "source"
        paths = tuple(root / f"{n}.json" for n in (1, 2))
        for n, path in enumerate(paths):
            _write_json(path, name=str(n), segments=((str(n), "source", "target", False),))
        package = self.root / "project.localcat-project"
        _export_package(root, paths, package, name="Notices")
        window = self._show(QtEditorWindow(self.controller))
        self.assertTrue(window.open_workspace_package_path(package))
        self.assertEqual(window._workspace_feedback_timeout_ms, 7000)
        window._workspace_feedback_timeout_ms = 40
        return window

    def test_real_save_expires_across_modes_chapters_and_redraw_without_mutation(self):
        window = self._workspace()
        self.controller.update_workspace_target("edited")
        self.assertTrue(window.save_workspace_project_package())
        saved = self.controller.workspace_save_state
        self.assertIn("已保存", window.statusBar().currentMessage())
        window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        self._wait()
        self.assertEqual(window.workspace_browse_save_feedback.text(), "")
        self.controller.select_workspace_document(self.controller.workspace_view.documents[1].identity)
        window._render_project()
        window.set_workspace_mode(WorkspaceMode.EDIT, persist=False)
        self.assertTrue(window.workspace_save_feedback.isHidden())
        self.assertTrue(window.workspace_browse_save_feedback.isHidden())
        self.assertEqual(window.workspace_save_feedback.text(), "")
        self.assertEqual(self.controller.workspace_save_state, saved)
        self.assertFalse(self.controller.active_project_dirty)

    def test_later_failure_survives_old_timer_then_project_switch_clears_it(self):
        window = self._workspace()
        self.assertTrue(window.save_workspace_project_package())
        window._set_workspace_save_feedback("保存失败；需要恢复。")
        self._wait()
        self.assertEqual(window.workspace_save_feedback.text(), "保存失败；需要恢复。")
        self.controller.set_project(EditorProject(name="Next", segments=(EditorSegment(id="next", source="next"),)))
        window._render_project()
        self.assertEqual(window.workspace_save_feedback.text(), "")
        window.close()
        self.assertEqual(window.statusBar().currentMessage(), "")

    def test_settings_rename_expires_without_rebuilding_editor_or_undoing_name(self):
        resource = self.controller.create_resource("Original", ResourceKind.TERMBASE)
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_status_feedback_timer")
        editor = dialog.findChild(QLineEdit, f"resourceName_{resource.id}")
        dialog._set_resource_name(resource.id, "Renamed")
        self.assertIn("名称已保存", dialog.status_label.text())
        self._wait()
        self.assertNotIn("已保存", dialog.status_label.text())
        self.assertIn("活动资源", dialog.status_label.text())
        self.assertEqual(self.repository.get(resource.id).name, "Renamed")
        self.assertIs(dialog.findChild(type(editor), f"resourceName_{resource.id}"), editor)

    def test_settings_busy_does_not_restore_old_success_and_failed_import_persists(self):
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_status_feedback_timer")
        dialog.create_resource("Created", ResourceKind.TERMBASE)
        dialog._set_import_busy(True, "正在导入…")
        self._wait()
        self.assertEqual(dialog.status_label.text(), "正在导入…")
        dialog._set_import_busy(False)
        self.assertNotIn("已创建", dialog.status_label.text())
        self.assertIn("活动资源", dialog.status_label.text())
        dialog._show_import_feedback("已导入 2 · 错误 1\n行 3 无效", failed=False)
        self._wait()
        self.assertIn("错误 1", dialog.import_feedback.text())
        dialog.reject()

    def test_resource_feedback_summary_expires_and_new_failure_stays(self):
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_import_feedback_timer")
        dialog._show_import_feedback("已取消资源包导入。", failed=False, transient=True)
        self._wait()
        self.assertEqual(dialog.import_feedback.text(), "")
        self.assertTrue(dialog.import_feedback.isHidden())
        dialog._show_import_feedback("完成", failed=False, transient=True)
        dialog._show_import_feedback("需要恢复", failed=True)
        self._wait()
        self.assertEqual(dialog.import_feedback.text(), "需要恢复")
        dialog.reject()
        self.assertFalse(dialog._import_feedback_timer.isActive())

    def test_terms_saved_feedback_expires_without_changing_records(self):
        resource = self.controller.create_resource("Terms", ResourceKind.TERMBASE)
        dialog = self._show(QtTermbaseDialog(self.controller, resource.id, resource.name))
        self._shorten(dialog, "_feedback_timer")
        dialog._begin_create()
        dialog.source_input.setText("hello")
        dialog.target_input.setText("你好")
        dialog._save_term()
        self.assertIn("已保存", dialog.feedback_label.text())
        saved = resource.path.read_bytes()
        self._wait()
        self.assertEqual(dialog.feedback_label.text(), "")
        self.assertTrue(dialog.feedback_label.isHidden())
        self.assertEqual(resource.path.read_bytes(), saved)
        dialog._show_feedback("资源已隔离；需要恢复", failed=True)
        self._wait()
        self.assertIn("需要恢复", dialog.feedback_label.text())

    def test_preprocess_save_cancel_apply_and_undo_restore_current_explanation(self):
        self.controller.set_project(EditorProject(name="Text", path=self.root / "text.json", segments=(EditorSegment(id="a", source="source", target="foo"),)))
        dialog = self._show(QtPreprocessDialog(self.controller))
        self._shorten(dialog, "_status_timer")
        dialog.rules_table.item(0, 1).setText("foo")
        dialog.rules_table.item(0, 2).setText("bar")
        self.assertTrue(dialog.save_preferences())
        self._wait()
        self.assertEqual(dialog.status_label.text(), "预览不会修改项目。")
        self.assertTrue(dialog.preview_rules())
        with patch.object(dialog, "_confirm_apply", return_value=False):
            self.assertFalse(dialog.apply_preview())
        self._wait()
        self.assertIn("预览已生成", dialog.status_label.text())
        self.assertTrue(dialog.apply_button.isEnabled())
        with patch.object(dialog, "_confirm_apply", return_value=True):
            self.assertTrue(dialog.apply_preview())
        dirty = self.controller.active_project_dirty
        self._wait()
        self.assertEqual(dialog.status_label.text(), "预览不会修改项目。")
        self.assertEqual(self.controller.current_segment.target, "bar")
        self.assertEqual(self.controller.active_project_dirty, dirty)
        self.assertTrue(dialog.undo_latest())
        self._wait()
        self.assertEqual(dialog.status_label.text(), "预览不会修改项目。")
        self.assertEqual(self.controller.current_segment.target, "foo")
        self.assertTrue(dialog.preview_rules())
        dialog.cancel_preview()
        self._wait()
        self.assertEqual(dialog.status_label.text(), "预览不会修改项目。")

    def test_chunk_publish_expires_but_next_preview_or_failure_stays(self):
        facade = _Facade(_view())
        dialog = self._show(QtChunkManagerDialog(facade, facade.view))
        self._shorten(dialog, "_feedback_timer")
        preview = facade.preview_rename_chunk("chunk-a", "Renamed")
        dialog._preview = preview
        dialog.confirm_check.setChecked(True)
        dialog._apply_preview()
        self.assertIn("已发布", dialog.preview_panel.text())
        self._wait()
        self.assertEqual(dialog.preview_panel.text(), "选择范围并生成预览。")
        self.assertEqual(len(facade.applied), 1)
        dialog._render_preview(preview)
        self._wait()
        self.assertIn("影响", dialog.preview_panel.text())
        dialog._show_error("CHUNK.RECOVERY_REQUIRED")
        self._wait()
        self.assertIn("CHUNK.RECOVERY_REQUIRED", dialog.preview_panel.text())

    def test_replacement_gets_own_interval_and_refused_close_keeps_notice(self):
        window = self._workspace()
        window._workspace_feedback_timeout_ms = 160
        window._set_workspace_save_feedback("First", transient=True)
        QTest.qWait(100)
        window._set_workspace_save_feedback("Second", transient=True)
        QTest.qWait(90)
        self.assertEqual(window.statusBar().currentMessage(), "Second")
        self.controller.update_workspace_target("unsaved")
        window._confirm_unsaved = lambda: False
        self.assertFalse(window.close())
        self.assertEqual(window.statusBar().currentMessage(), "Second")
        self._wait()
        self.assertEqual(window.workspace_save_feedback.text(), "")
        self.assertTrue(self.controller.active_project_dirty)
        window._confirm_unsaved = lambda: True
        window._set_workspace_save_feedback("Closing", transient=True)
        window.close()
        self.assertEqual(window.statusBar().currentMessage(), "")
        self.assertEqual(window.workspace_save_feedback.text(), "")

    def test_dialog_close_cancels_timer_but_rejected_busy_close_does_not(self):
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_status_feedback_timer")
        dialog.create_resource("Pending", ResourceKind.TERMBASE)
        dialog._import_busy = True
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog._status_feedback_timer.isActive())
        dialog._import_busy = False
        dialog.reject()
        self.assertFalse(dialog._status_feedback_timer.isActive())
        self.assertEqual(dialog.status_label.text(), "")
        self.controller.set_project(EditorProject(name="Text", path=self.root / "text.json", segments=(EditorSegment(id="a", source="source", target="foo"),)))
        preprocess = self._show(QtPreprocessDialog(self.controller))
        self._shorten(preprocess, "_status_timer")
        preprocess.cancel_preview()
        self.assertTrue(preprocess._status_timer.isActive())
        preprocess.reject()
        self.assertFalse(preprocess._status_timer.isActive())
        self.assertEqual(preprocess.status_label.text(), "预览不会修改项目。")

    def test_preprocess_later_error_and_terms_later_error_keep_feedback(self):
        self.controller.set_project(EditorProject(name="Text", path=self.root / "text.json", segments=(EditorSegment(id="a", source="source", target="foo"),)))
        preprocess = self._show(QtPreprocessDialog(self.controller))
        self._shorten(preprocess, "_status_timer")
        preprocess.cancel_preview()
        preprocess._set_status("当前预览过期，请重新预览。", error=True)
        self._wait()
        self.assertEqual(preprocess.status_label.text(), "当前预览过期，请重新预览。")
        resource = self.controller.create_resource("Terms", ResourceKind.TERMBASE)
        terms = self._show(QtTermbaseDialog(self.controller, resource.id, resource.name))
        self._shorten(terms, "_feedback_timer")
        terms._show_feedback("Saved", failed=False, transient=True)
        terms._show_feedback("Term recovery required", failed=True)
        self._wait()
        self.assertEqual(terms.feedback_label.text(), "Term recovery required")
        terms._show_feedback("Saved again", failed=False, transient=True)
        terms.reject()
        self.assertFalse(terms._feedback_timer.isActive())
        self.assertEqual(terms.feedback_label.text(), "")

    def test_resource_package_success_expires_but_failed_cancel_is_not_success(self):
        resource = self.controller.create_resource("Terms", ResourceKind.TERMBASE)
        resource.path.write_text("hello,你好\n", encoding="utf-8")
        package = self.root / "terms.localcat-resource"
        exported = self.controller.export_resource_package(resource.id, package)
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_import_feedback_timer")
        dialog._finish_resource_export(exported, None, package=True)
        self.assertIn("已导出", dialog.import_feedback.text())
        self._wait()
        self.assertEqual(dialog.import_feedback.text(), "")
        preview = self.controller.preview_resource_package_import(
            package, ResourceImportMode.CREATE_NEW, new_resource_name="Imported")
        with patch("qt_settings_dialog.ResourcePackageApplyDialog.exec", return_value=QDialog.DialogCode.Rejected):
            with patch.object(self.controller, "cancel_resource_package_import", side_effect=EditorControllerError("RESOURCE.IMPORT.RECOVERY_REQUIRED")):
                dialog._after_resource_package_preview(preview, None, "Imported")
        self._wait()
        self.assertIn("RESOURCE.IMPORT.RECOVERY_REQUIRED", dialog.import_feedback.text())
        self.assertTrue(dialog.import_feedback.property("failed"))
        self.controller.cancel_resource_package_import(preview)

    def test_mode_switch_does_not_restart_remaining_save_notice_interval(self):
        window = self._workspace()
        window._workspace_feedback_timeout_ms = 160
        self.assertTrue(window.save_workspace_project_package())
        QTest.qWait(100)
        window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        QTest.qWait(90)
        self.assertEqual(window.workspace_save_feedback.text(), "")
        self.assertEqual(window.workspace_browse_save_feedback.text(), "")

    def _read_save_result_via_button(self, window):
        button = window.findChild(QToolButton, "workspaceSaveResultButton")
        self.assertIsNotNone(button, "左下状态栏应保留逐章节保存结果入口")
        self.assertTrue(button.isVisible())
        captured = []
        def inspect_dialog():
            dialog = window.findChild(QDialog, "workspaceSaveResultDialog")
            if dialog is not None:
                body = dialog.findChild(QTextBrowser, "workspaceSaveResultText")
                captured.append(body.toPlainText() if body is not None else None)
                dialog.accept()
        QTimer.singleShot(0, inspect_dialog)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        self.assertEqual(len(captured), 1, "键盘应实际打开保存结果对话框")
        self.assertIsNotNone(captured[0])
        return captured[0]

    def test_full_save_result_remains_accessible_after_success_notice_expires(self):
        window = self._workspace()
        self.controller.update_workspace_target("Changed")
        self.assertTrue(window.save_workspace_project_package())
        self._wait()
        self.assertTrue(window.workspace_save_feedback.isHidden())
        text = self._read_save_result_via_button(window)
        self.assertIn("0:saved", text)
        self.assertIn("1:unchanged", text)
        self.assertIn("该次保存", text)
        self.assertNotIn("Changed", text)
        window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        self.controller.select_workspace_document(self.controller.workspace_view.documents[1].identity)
        window._render_project()
        self.assertEqual(self._read_save_result_via_button(window), text)
        self.assertEqual(window.workspace_browse_save_feedback.text(), "")
        self.controller.set_project(EditorProject(name="Next", segments=(EditorSegment(id="next", source="next"),)))
        window._render_project()
        self.assertTrue(window.findChild(QToolButton, "workspaceSaveResultButton").isHidden())

    def test_current_chapter_save_result_preserves_then_replaces_historical_dirty_note(self):
        window = self._workspace()
        button = window.findChild(QToolButton, "workspaceSaveResultButton")
        self.assertIsNotNone(button)
        self.assertTrue(button.isHidden(), "无报告时隐藏次级入口")
        self.controller.update_workspace_target("Chapter A new")
        self.controller.select_workspace_document(self.controller.workspace_view.documents[1].identity)
        self.controller.update_workspace_target("Chapter B new")
        self.controller.select_workspace_document(self.controller.workspace_view.documents[0].identity)
        self.assertTrue(window.save_workspace_current_document())
        state = self.controller.workspace_save_state
        self._wait()
        self.assertEqual(window.workspace_save_feedback.text(), "")
        text = self._read_save_result_via_button(window)
        self.assertIn("0:saved", text)
        self.assertIn("1:unchanged·该次保存后仍未保存", text)
        self.assertEqual(self.controller.workspace_save_state, state)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertTrue(window.save_workspace_project_package())
        self._wait()
        latest = self._read_save_result_via_button(window)
        self.assertIn("1:saved", latest)
        self.assertNotIn("该次保存后仍未保存", latest)
        self.assertFalse(self.controller.active_project_dirty)
        window.close()
        self.assertTrue(button.isHidden())

    def test_real_termbase_column_preview_cancel_expires_without_import(self):
        resource = self.controller.create_resource("Terms", ResourceKind.TERMBASE)
        original = resource.path.read_bytes()
        csv = self.root / "columns.csv"
        csv.write_text("Source,Target\nhello,你好\n", encoding="utf-8")
        dialog = self._show(QtSettingsDialog(self.controller))
        self._shorten(dialog, "_import_feedback_timer")
        with patch("qt_settings_dialog.TermbaseColumnSelectionDialog.exec", return_value=QDialog.DialogCode.Rejected):
            self.assertTrue(dialog.start_termbase_preview(resource.id, csv))
            worker = dialog.preview_worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()
        self.assertEqual(dialog.import_feedback.text(), "已取消术语表导入。")
        self.assertFalse(dialog.is_importing)
        self._wait()
        self.assertEqual(dialog.import_feedback.text(), "")
        self.assertTrue(dialog.import_feedback.isHidden())
        self.assertIsNone(dialog.last_import_report)
        self.assertEqual(resource.path.read_bytes(), original)

    def test_project_success_uses_bottom_left_status_and_no_result_menu_item(self):
        window = self._workspace()
        self.assertTrue(window.save_workspace_project_package())
        self.assertTrue(window.workspace_save_feedback.isHidden(), "成功摘要不应挤压正文")
        self.assertTrue(window.workspace_browse_save_feedback.isHidden())
        self.assertEqual(window.workspace_save_feedback.text(), "")
        self.assertIn("已保存", window.statusBar().currentMessage())
        self.assertIsNone(window.findChild(QAction, "workspaceSaveResultAction"))
        self.assertFalse(any("保存结果" in action.text() for action in window.project_menu.actions()))
        button = window.findChild(QToolButton, "workspaceSaveResultButton")
        self.assertIsNotNone(button)
        self._wait()
        self.assertEqual(window.statusBar().currentMessage(), "")
        self.assertTrue(button.isVisible())
        self.assertLess(button.mapTo(window, QPoint(0, 0)).x(), 40)
        self.assertGreater(button.mapTo(window, QPoint(0, 0)).y(), window.height() - 80)
        self.assertIn("保存", button.toolTip())
        self.assertNotEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_later_status_message_survives_previous_project_success_timeout(self):
        window = self._workspace()
        self.assertTrue(window.save_workspace_project_package())
        window.statusBar().showMessage("正在处理另一个操作")
        self._wait()
        self.assertEqual(window.statusBar().currentMessage(), "正在处理另一个操作")
        self.assertEqual(window.workspace_save_feedback.text(), "")
