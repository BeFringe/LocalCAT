"""Real asynchronous Qt single-TL workflows and late-result ownership."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from datetime import datetime, timedelta, timezone
import tempfile
import time
from threading import Event
import unittest
from unittest import mock

from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from editor_controller import compose_project_enabled_editor_controller
from resource_repository import ResourceRepository
from rpy_project_adapter import RpyProjectAdapter, RpyProjectSession
from qt_editor_window import QtEditorWindow
from qt_editor import _compose_editor_controller, _compose_chunk_controller
from tests.test_rpy_provider import FIXTURES
from tests.test_project_single_file_profile import _PACKAGE_PORT


class QtRpyProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'single.rpy'
        self.source.write_bytes((FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes())
        self.destination = self.root / 'single.localcat-project'
        repository = ResourceRepository(self.root / 'data')
        self.controller, self.capabilities = _compose_editor_controller(repository)
        self.chunks = _compose_chunk_controller(self.controller, repository)
        self.window = QtEditorWindow(self.controller, chunk_controller=self.chunks)
        self.window.show()
        self.errors = []
        self.window._show_error = lambda *args: self.errors.append(args)
        self.app.processEvents()

    def tearDown(self):
        self.window.cancel_file_operation()
        self.wait_until(lambda: not self.window.file_operation_running)
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def wait_until(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(predicate())

    def open(self):
        self.assertTrue(self.window.open_project_path(self.source))
        self.wait_until(lambda: not self.window.file_operation_running)
        self.assertTrue(self.controller.has_workspace, self.errors)

    def test_normal_open_edit_save_cold_reopen_browse(self):
        self.open()
        self.assertIn('guide', self.window.speaker_display.text())
        self.assertFalse(self.window.tl_export_action.isEnabled())
        self.assertIn('暂不可用', self.window.tl_export_action.toolTip())
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.capabilities.matcher_validation_owner.validate_basic(
            generated_at_utc=now, valid_until_utc=now + timedelta(days=1),
            evaluated_at_utc=now)
        self.window._refresh_project_search_controls()
        self.window.project_search_input.setText('guide')
        self.window.project_search_source.setChecked(False)
        self.window.project_search_target.setChecked(False)
        self.window.project_search_speaker.setChecked(True)
        self.window._submit_project_search()
        self.assertIsNotNone(self.window.current_project_search_report, (self.errors, self.window.project_search_result.text()))
        self.assertEqual(self.window.current_project_search_report.total, 2)
        self.assertTrue(self.window._activate_project_search_ordinal(0))
        self.window.target_editor.setPlainText('新的译文')
        self.window.confirm_current()
        with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')):
            self.assertTrue(self.window._choose_save())
        self.wait_until(lambda: not self.window.file_operation_running)
        self.assertFalse(self.controller.active_project_dirty, self.errors)
        self.source.unlink()
        self.assertTrue(self.window.open_project_path(self.destination))
        self.wait_until(lambda: not self.window.file_operation_running)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '新的译文')
        self.assertTrue(self.controller.workspace_view.segments[0].confirmed)
        self.assertEqual(len(self.controller.workspace_view.segments), 6)
        self.assertGreaterEqual(self.window.segment_list.count(), 6)
        self.assertFalse(self.errors)

    def test_open_worker_heartbeat_and_cancel_releases_late_real_candidate(self):
        entered, release = Event(), Event()
        prepared = []
        original = RpyProjectAdapter.prepare_single
        def delayed(adapter, *args, **kwargs):
            session = original(adapter, *args, **kwargs)
            prepared.append(session)
            entered.set()
            release.wait(3)
            return session
        beats = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: beats.append(1))
        timer.start()
        with mock.patch.object(RpyProjectAdapter, 'prepare_single', delayed):
            self.window.open_project_path(self.source)
            self.wait_until(entered.is_set)
            QTest.qWait(50)
            self.assertGreater(len(beats), 2)
            self.window.cancel_file_operation()
            release.set()
            self.wait_until(lambda: not self.window.file_operation_running)
        timer.stop()
        self.assertTrue(prepared[0].closed)
        self.assertFalse(self.controller.has_active_project)

    def test_save_worker_heartbeat_and_cancellation_keeps_real_receipt(self):
        self.open()
        entered, release = Event(), Event()
        original = RpyProjectSession.save
        def delayed(session, *args, **kwargs):
            entered.set()
            release.wait(3)
            return original(session, *args, **kwargs)
        with mock.patch.object(RpyProjectSession, 'save', delayed):
            self.assertTrue(self.window.save_workspace_package(self.destination, wait=False))
            self.wait_until(entered.is_set)
            self.assertTrue(self.window.target_editor.isReadOnly())
            self.window._navigate(1)
            self.assertEqual(self.controller.workspace_global_index, 1)
            self.assertTrue(self.window.target_editor.isReadOnly())
            self.window.cancel_file_operation()
            release.set()
            self.wait_until(lambda: not self.window.file_operation_running)
        self.assertTrue(self.destination.exists())
        self.assertFalse(self.controller.active_project_dirty)
        self.assertIn('已保存', self.window.statusBar().currentMessage())

    def test_unsaved_save_continuation_waits_for_durable_receipt(self):
        self.open()
        self.window.target_editor.setPlainText('保存后离开')
        with mock.patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Save), \
             mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')):
            self.assertTrue(self.window.load_sample())
        self.assertTrue(self.destination.exists())
        self.assertTrue(self.controller.has_project)
        self.assertFalse(self.controller.has_workspace)

    def test_failed_unsaved_save_does_not_continue_or_lose_draft(self):
        self.open()
        self.window.target_editor.setPlainText('失败仍保留')
        old = self.controller.workspace_view.project.project_id
        with mock.patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Save), \
             mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')), \
             mock.patch(_PACKAGE_PORT + '.stage_candidate', side_effect=OSError('failure')):
            self.assertFalse(self.window.load_sample())
        self.assertEqual(self.controller.workspace_view.project.project_id, old)
        self.assertEqual(self.controller.current_segment.target, '失败仍保留')
        self.assertTrue(self.controller.active_project_dirty)
        self.assertFalse(self.destination.exists())

    def test_close_during_save_does_not_close_live_owner_or_fake_rollback(self):
        self.open()
        entered, release = Event(), Event()
        sessions = []
        original = RpyProjectSession.save
        def delayed(session, *args, **kwargs):
            sessions.append(session)
            entered.set()
            release.wait(3)
            self.assertFalse(session.closed)
            return original(session, *args, **kwargs)
        with mock.patch.object(RpyProjectSession, 'save', delayed):
            self.window.save_workspace_package(self.destination, wait=False)
            self.wait_until(entered.is_set)
            with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Close):
                self.window.close()
            self.assertFalse(sessions[0].closed)
            release.set()
            self.wait_until(lambda: sessions[0].closed)
        self.assertTrue(self.destination.exists())
        self.assertFalse(self.controller.has_active_project)

    def test_thread_start_failure_does_not_leave_save_reserved(self):
        self.open()
        self.assertTrue(self.window.chunk_manage_action.isEnabled())
        with mock.patch('qt_project_file_job.Thread.start', side_effect=RuntimeError('start failed')):
            self.assertFalse(self.window.save_workspace_package(self.destination, wait=True))
        self.assertFalse(self.controller.workspace_save_running)
        self.assertFalse(self.window.file_operation_running)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.window.chunk_manage_action.isEnabled())
        self.assertTrue(self.window.save_workspace_package(self.destination, wait=True))

    def test_close_during_preparation_releases_without_gui_delivery(self):
        entered, release = Event(), Event()
        prepared = []
        original = RpyProjectAdapter.prepare_single
        def delayed(adapter, *args, **kwargs):
            session = original(adapter, *args, **kwargs)
            prepared.append(session)
            entered.set()
            release.wait(3)
            return session
        with mock.patch.object(RpyProjectAdapter, 'prepare_single', delayed):
            self.window.open_project_path(self.source)
            self.wait_until(entered.is_set)
            self.window.close()
            release.set()
            self.wait_until(lambda: bool(prepared) and prepared[0].closed)
        self.assertFalse(self.controller.has_active_project)
