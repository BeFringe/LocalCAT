"""Offscreen desktop export; native source acceptance remains a separate task."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
from qt_editor import _compose_editor_controller, _compose_chunk_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tests.test_rpy_provider import FIXTURES


class QtRpyExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original.rpy'
        self.raw = (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes()
        self.source.write_bytes(self.raw)
        self.package = self.root / 'package.localcat-project'
        self.target = self.root / 'chosen.rpy'
        repository = ResourceRepository(self.root / 'data')
        self.controller, self.capabilities = _compose_editor_controller(repository)
        self.chunks = _compose_chunk_controller(self.controller, repository)
        opened = self.controller.begin_file_open(self.source)
        opened.run()
        self.controller.finish_file_job(opened)
        saved = self.controller.begin_file_save(self.package)
        saved.run()
        self.controller.finish_file_job(saved)
        self.controller.close_project()
        self.source.unlink()
        self.controller.open_project_package(self.package)
        self.window = QtEditorWindow(self.controller, chunk_controller=self.chunks)
        self.window.show()
        self.window._render_project()
        self.app.processEvents()

    def tearDown(self):
        dialog = self.window._tl_export_dialog
        if dialog is not None:
            dialog.cancel_operation()
            self.wait_until(lambda: not dialog.operation_running)
            dialog.close()
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def wait_until(self, predicate, timeout=8):
        if timeout > 0 and not predicate():
            loop = QEventLoop()
            poll = QTimer(loop)
            deadline = QTimer(loop)
            deadline.setSingleShot(True)
            deadline.setTimerType(Qt.TimerType.PreciseTimer)
            failure = None

            def check():
                nonlocal failure
                try:
                    if predicate():
                        loop.quit()
                except BaseException as error:
                    # Qt callbacks must not swallow a failing test predicate.
                    failure = error
                    loop.quit()

            poll.timeout.connect(check)
            deadline.timeout.connect(loop.quit)
            try:
                poll.start(10)
                deadline.start(int(timeout * 1000))
                loop.exec()
            finally:
                poll.stop()
                deadline.stop()
                poll.timeout.disconnect(check)
                deadline.timeout.disconnect(loop.quit)
            if failure is not None:
                raise failure
        self.assertTrue(predicate())

    def open_export(self):
        self.assertTrue(self.window.tl_export_action.isEnabled())
        with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.target), '')) as choose:
            self.window.tl_export_action.trigger()
        self.assertEqual(choose.call_args.args[2], str(self.root / 'original.rpy'))
        dialog = self.window._tl_export_dialog
        self.assertIsNotNone(dialog)
        self.wait_until(lambda: not dialog.operation_running)
        return dialog

    def test_existing_single_target_warns_in_preview_and_confirmation(self):
        self.target.write_bytes(b'previous output')
        dialog = self.open_export()
        self.assertEqual(dialog.view.target_action, 'overwrite')
        self.assertIn('覆盖 1', dialog.summary_label.text())
        self.assertIn('覆盖', dialog.confirm_button.text())
        self.assertEqual(self.target.read_bytes(), b'previous output')

    def test_menu_preview_confirm_empty_target_and_dirty_preserved(self):
        self.window.target_editor.setPlainText('')
        dialog = self.open_export()
        self.assertEqual(dialog.view.modified_count, 1)
        self.assertIn(str(self.target), dialog.target_label.text())
        for text in ('修改', '空译文', '未确认'):
            self.assertIn(text, dialog.summary_label.text())
        self.assertTrue(dialog.confirm_button.isEnabled())
        dialog.confirm_button.click()
        self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'published')
        self.assertIn('导出成功', dialog.status_label.text())
        self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode(), b''))
        self.assertTrue(self.controller.active_project_dirty)
        self.assertIn('*', self.window.windowTitle())

    def test_published_closes_panel_and_reopening_prepares_current_edit(self):
        dialog = self.open_export()
        first_view = dialog.view
        accepted, rejected = mock.Mock(), mock.Mock()
        dialog.accepted.connect(accepted)
        dialog.rejected.connect(rejected)
        with mock.patch.object(self.controller, 'cancel_tl_export_preview',
                               wraps=self.controller.cancel_tl_export_preview) as cancel:
            dialog.confirm_button.click()
            job = dialog._runner.job
            self.wait_until(lambda: dialog.result is not None)
            self.assertEqual(dialog.result.outcome, 'published')
            self.assertFalse(dialog.isVisible())
            self.assertEqual(QDialog.result(dialog), QDialog.DialogCode.Accepted)
            accepted.assert_called_once_with()
            rejected.assert_not_called()
            cancel.assert_not_called()
            self.assertFalse(job.cancellation.cancelled)
        self.assertIn('导出成功', self.window.statusBar().currentMessage())
        self.assertIn(str(self.target), self.window.statusBar().currentMessage())
        self.assertIn('导出不会替代项目包保存', self.window.statusBar().currentMessage())
        self.assertEqual(self.target.read_bytes(), self.raw)
        self.assertFalse(self.controller.tl_export_publish_running)
        self.window.target_editor.setPlainText('再次导出时的译文')
        reopened = self.open_export()
        self.assertTrue(reopened.isVisible())
        self.assertIsNone(reopened.result)
        self.assertNotEqual(reopened.view.preview_id, first_view.preview_id)
        self.assertEqual(reopened.view.modified_count, 1)
        self.assertTrue(reopened.confirm_button.isEnabled())

    def test_edit_invalidates_preview_then_target_reselection_prepares_again(self):
        dialog = self.open_export()
        old = dialog.view
        self.window.target_editor.setPlainText('later')
        self.wait_until(lambda: not dialog.confirm_button.isEnabled())
        self.assertIn('过期', dialog.status_label.text())
        self.assertTrue(dialog.isVisible())
        next_target = self.root / 'elsewhere.rpy'
        with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(next_target), '')):
            dialog.target_button.click()
        self.wait_until(lambda: not dialog.operation_running)
        self.assertIsNot(dialog.view, old)
        self.assertEqual(dialog.view.target_path, str(next_target))
        dialog.confirm_button.click()
        self.wait_until(lambda: dialog.result is not None)
        self.assertTrue(next_target.exists())
        self.assertFalse(self.target.exists())

    def test_blocking_diagnostics_include_file_line_and_segment(self):
        self.window.target_editor.setPlainText('错误 [new_expression]')
        dialog = self.open_export()
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertTrue(dialog.isVisible())
        issue = dialog.view.diagnostics[0]
        text = dialog.diagnostics.toPlainText()
        self.assertIn('original.rpy', text)
        self.assertIn(str(issue.line_number), text)
        self.assertNotIn(issue.local_segment_id, text)
        self.assertIn('第 1 段', text)
        self.controller.move_workspace(1)
        self.assertTrue(dialog.locate_button.isEnabled())
        dialog.locate_button.click()
        self.assertEqual(self.controller.current_workspace_identity, self.controller.workspace_view.segments[0].identity)
        self.assertEqual(self.window.target_editor.toPlainText(), '错误 [new_expression]')
        self.assertFalse(self.target.exists())

    def test_cancel_prepare_and_close_dialog_release_late_candidate(self):
        from threading import Event
        from rpy_project_adapter import RpyProjectAdapter
        entered, release = Event(), Event()
        original = RpyProjectAdapter.prepare_export
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(RpyProjectAdapter, 'prepare_export', autospec=True, side_effect=delayed):
            with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.target), '')):
                self.window.tl_export_action.trigger()
            dialog = self.window._tl_export_dialog
            try:
                self.wait_until(entered.is_set)
                dialog.close()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(dialog.operation_running)
            finally:
                release.set()
            self.wait_until(lambda: not dialog.operation_running)
        self.assertFalse(self.target.exists())
        self.assertIsNone(self.controller._pending_tl_export)
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))

    def test_dirty_window_close_during_publish_cannot_skip_unsaved_protection(self):
        from threading import Event
        from parser_composition import ParserApplicationSurface
        self.window.target_editor.setPlainText('unsaved target')
        dialog = self.open_export()
        entered, release = Event(), Event()
        original = ParserApplicationSurface.write_prepared
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', autospec=True, side_effect=delayed):
            dialog.confirm_button.click()
            try:
                self.wait_until(entered.is_set)
                self.assertTrue(self.window.target_editor.isReadOnly())
                self.assertFalse(self.window.save_button.isEnabled())
                self.assertFalse(self.window.chunk_manage_action.isEnabled())
                started = time.monotonic()
                self.assertFalse(self.window.close())
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(self.controller.active_project_dirty)
                self.assertTrue(self.window.isVisible())
            finally:
                release.set()
            self.wait_until(lambda: dialog.result is not None)
        self.assertNotEqual(dialog.result.outcome, 'published')
        self.assertTrue(self.controller.active_project_dirty)
        self.assertFalse(self.window.target_editor.isReadOnly())
        with mock.patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.window.close())
        self.assertTrue(self.controller.active_project_dirty)
        with mock.patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.close())

    def test_clean_window_close_releases_late_preparation_without_qt_delivery(self):
        from threading import Event
        from rpy_project_adapter import RpyProjectAdapter
        entered, release = Event(), Event()
        original = RpyProjectAdapter.prepare_export
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(RpyProjectAdapter, 'prepare_export', autospec=True, side_effect=delayed):
            with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.target), '')):
                self.window.tl_export_action.trigger()
            dialog = self.window._tl_export_dialog
            job = dialog._runner.job
            thread = dialog._runner._thread
            try:
                self.wait_until(entered.is_set)
                self.assertTrue(self.window.close())
                self.assertTrue(job.disposed)
                self.assertFalse(self.controller.has_active_project)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))
        self.assertIsNone(dialog.result)

    def test_failed_and_uncertain_results_are_never_success_or_automatic_retry(self):
        from tests.test_parser_roundtrip import _CandidateFile, _PlatformAdapter, _POST_NAMING_FAULTS
        from project_codec_settings import compose_project_codec_runtime
        dialog = self.open_export()
        with mock.patch.object(_CandidateFile, '_flush_content', side_effect=OSError('prepublish fault')):
            dialog.confirm_button.click()
            self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'failed')
        self.assertTrue(dialog.isVisible())
        self.assertIn('导出失败', dialog.status_label.text())
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertFalse(self.target.exists())
        def fault(point):
            if point == _POST_NAMING_FAULTS[0]:
                raise OSError('post naming fault')
        def runtime(config):
            composed = compose_project_codec_runtime(config)
            composed.surface._platform_backend_factory = lambda root: _PlatformAdapter(_fault_injector=fault)
            return composed
        with mock.patch('editor_controller.compose_project_codec_runtime', side_effect=runtime):
            dialog.prepare_target(self.target)
            self.wait_until(lambda: not dialog.operation_running)
            dialog.confirm_button.click()
            self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'uncertain')
        self.assertTrue(dialog.isVisible())
        self.assertIn('结果不确定', dialog.status_label.text())
        self.assertNotIn('导出成功', dialog.status_label.text())
        self.assertEqual(self.target.read_bytes(), self.raw)
        self.assertFalse(dialog.confirm_button.isEnabled())

    def test_project_switch_stales_preview_and_next_menu_uses_new_package_directory(self):
        dialog = self.open_export()
        self.window.load_sample()
        self.wait_until(lambda: dialog.view.status == 'stale')
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.window._refresh_tl_export_action()
        self.assertFalse(self.window.tl_export_action.isVisible())
        self.controller.open_project_package(self.package)
        self.window._render_project()
        with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=('', '')) as choose:
            self.window.tl_export_action.trigger()
        self.assertEqual(choose.call_args.args[2], str(self.root / 'original.rpy'))

    def test_thread_start_failure_releases_export_without_reporting_uncertain(self):
        dialog = self.open_export()
        with mock.patch('qt_project_file_job.Thread.start', side_effect=RuntimeError('start failed')):
            dialog.confirm_button.click()
            self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'failed')
        self.assertFalse(self.controller.tl_export_publish_running)
        self.assertFalse(self.window.target_editor.isReadOnly())
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))


    def test_escape_reject_and_close_release_ready_preview(self):
        for action in ('escape', 'reject', 'close'):
            with self.subTest(action=action):
                dialog = self.open_export()
                candidate = self.controller._pending_tl_export[1]
                if action == 'escape':
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                elif action == 'reject':
                    dialog.reject()
                else:
                    dialog.close()
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertIsNone(self.controller._pending_tl_export)
                self.assertTrue(candidate.prepared.closed)
                self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))

    def test_escape_cancels_pending_preview_and_releases_late_candidate(self):
        from threading import Event
        from rpy_project_adapter import RpyProjectAdapter
        entered, release = Event(), Event()
        candidates = []
        original = RpyProjectAdapter.prepare_export
        def delayed(*args, **kwargs):
            candidate = original(*args, **kwargs)
            candidates.append(candidate)
            entered.set()
            release.wait(5)
            return candidate
        with mock.patch.object(RpyProjectAdapter, 'prepare_export', autospec=True, side_effect=delayed):
            with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.target), '')):
                self.window.tl_export_action.trigger()
            dialog = self.window._tl_export_dialog
            job = dialog._runner.job
            try:
                self.wait_until(entered.is_set)
                started = time.monotonic()
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(dialog.isVisible())
                cancelled = job.cancellation.cancelled
                self.assertTrue(dialog.operation_running)
            finally:
                release.set()
            self.wait_until(lambda: not dialog.operation_running)
        self.assertTrue(cancelled)
        self.assertTrue(candidates[0].closed)
        self.assertIsNone(self.controller._pending_tl_export)
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))
        self.assertFalse(self.target.exists())

    def test_escape_cancels_pending_publish_without_waiting_for_owner(self):
        from threading import Event
        from parser_composition import ParserApplicationSurface
        dialog = self.open_export()
        entered, release = Event(), Event()
        original = ParserApplicationSurface.write_prepared
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', autospec=True, side_effect=delayed):
            dialog.confirm_button.click()
            job = dialog._runner.job
            try:
                self.wait_until(entered.is_set)
                started = time.monotonic()
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(dialog.isVisible())
                cancelled = job.cancellation.cancelled
                self.assertTrue(dialog.operation_running)
            finally:
                release.set()
            self.wait_until(lambda: dialog.result is not None)
        self.assertTrue(cancelled)
        self.assertEqual(dialog.result.outcome, 'failed')
        self.assertIn('PARSER.SOURCE.CANCELLED', [item.code for item in dialog.result.diagnostics])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.controller.tl_export_publish_running)
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))

    def test_escape_after_naming_preserves_real_published_or_uncertain_result(self):
        from threading import Event
        from tests.test_parser_roundtrip import _PlatformAdapter, _POST_NAMING_FAULTS
        from project_codec_settings import compose_project_codec_runtime
        for fails in (False, True):
            with self.subTest(fails=fails):
                entered, release = Event(), Event()
                def delay_after_naming(point):
                    if point == _POST_NAMING_FAULTS[0]:
                        entered.set()
                        release.wait(5)
                        if fails:
                            raise OSError('post naming fault')
                def runtime(config):
                    composed = compose_project_codec_runtime(config)
                    composed.surface._platform_backend_factory = lambda root: _PlatformAdapter(
                        _fault_injector=delay_after_naming)
                    return composed
                with mock.patch('editor_controller.compose_project_codec_runtime', side_effect=runtime):
                    dialog = self.open_export()
                    dialog.confirm_button.click()
                    job = dialog._runner.job
                    try:
                        self.wait_until(entered.is_set)
                        self.assertTrue(self.target.exists())
                        started = time.monotonic()
                        QTest.keyClick(dialog, Qt.Key.Key_Escape)
                        self.assertLess(time.monotonic() - started, 0.5)
                        self.assertFalse(dialog.isVisible())
                        cancelled = job.cancellation.cancelled
                    finally:
                        release.set()
                    self.wait_until(lambda: dialog.result is not None)
                self.assertTrue(cancelled)
                self.assertFalse(dialog.isVisible())
                self.assertEqual(QDialog.result(dialog), QDialog.DialogCode.Rejected)
                self.assertEqual(dialog.result.outcome, 'uncertain' if fails else 'published')
                self.assertEqual(dialog.result.target_path, str(self.target))
                self.assertEqual(self.target.read_bytes(), self.raw)
                self.assertIn('结果不确定' if fails else '导出成功', self.window.statusBar().currentMessage())
                self.assertNotIn('导出已取消', self.window.statusBar().currentMessage())
                self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))

    def test_chooser_return_restores_preview_panel_after_native_window_handoff(self):
        for selected in (str(self.target), ''):
            with self.subTest(selected=selected):
                def choose(dialog, *_args, **_kwargs):
                    # A native chooser can return after moving its parent out
                    # of view. Simulate that GUI handoff, not publication.
                    dialog.hide()
                    return selected, ''
                with mock.patch.object(QFileDialog, 'getSaveFileName', side_effect=choose):
                    self.window.tl_export_action.trigger()
                dialog = self.window._tl_export_dialog
                self.wait_until(lambda: not dialog.operation_running)
                self.assertTrue(dialog.isVisible())
                dialog.close()

    def test_chooser_return_does_not_reopen_panel_after_window_shutdown(self):
        def choose(dialog, *_args, **_kwargs):
            dialog.shutdown()
            return str(self.target), ''
        with mock.patch.object(QFileDialog, 'getSaveFileName', side_effect=choose):
            self.window.tl_export_action.trigger()
        dialog = self.window._tl_export_dialog
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.operation_running)
        self.assertIsNone(self.controller._pending_tl_export)
        self.assertFalse(self.target.exists())
