"""Real batch workers retain physical facts through cancellation and late delivery."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from threading import Event, Thread
import unittest
from unittest import mock
from PySide6.QtCore import QTimer

from editor_controller import EditorControllerError
from parser_composition import ParserApplicationSurface
from platform_export_directory import ExportDirectoryBatch
from rpy_project_export import _TargetObservation
from tests import test_controller_rpy_batch_export as controller_fixtures
from tests import test_qt_rpy_batch_export as qt_fixtures
from tests import test_rpy_batch_export as export_fixtures
from tests.test_parser_roundtrip import _CandidateFile, _PlatformAdapter, _POST_NAMING_FAULTS


class ControllerBatchLifecycleTests(unittest.TestCase):
    setUp = controller_fixtures.ControllerRpyBatchExportTests.setUp
    preview = controller_fixtures.ControllerRpyBatchExportTests.preview
    finish = controller_fixtures.ControllerRpyBatchExportTests.finish

    def parents(self):
        for ref in self.refs:
            (self.target / ref).parent.mkdir(parents=True, exist_ok=True)

    def test_preview_late_delivery_after_edit_cancel_supersede_and_project_switch(self):
        for action in ('edit', 'cancel', 'supersede', 'switch'):
            with self.subTest(action=action):
                job = self.controller.begin_tl_export_preview(self.target)
                job.run()
                prepared = job._value.prepared
                if action == 'edit':
                    self.controller.update_workspace_target('later edit')
                elif action == 'cancel':
                    job.cancel()
                elif action == 'supersede':
                    self.preview()
                else:
                    self.controller.load_sample()
                result = self.controller.finish_tl_export_job(job)
                self.assertFalse(result.accepted)
                self.assertTrue(prepared.closed)
        self.assertEqual(list(self.target.rglob('*.rpy')), [])

    def test_cancel_directory_preparation_keeps_created_prefix_and_consumes_old_view(self):
        view = self.preview()
        job = self.controller.begin_tl_export_directory_preparation(view, self.target)
        original = _PlatformAdapter.open_export_directory
        def create(backend, parent, name, *, create=False):
            result = original(backend, parent, name, create=create)
            if create:
                job.cancel()
            return result
        with mock.patch.object(_PlatformAdapter, 'open_export_directory', create):
            outcome = self.finish(job)
        self.assertEqual(outcome.result.outcome, 'cancelled')
        self.assertEqual(outcome.result.created_directories, ('b',))
        self.assertTrue(outcome.cancelled)
        self.assertEqual(list(self.target.rglob('*.rpy')), [])
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.controller.begin_tl_export_publish(view, self.target)
        self.assertFalse(self.controller.tl_export_publish_running)

    def test_cancel_after_first_publication_keeps_exact_prefix(self):
        self.parents()
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        original = ParserApplicationSurface.write_prepared
        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            job.cancel()
            return result
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', write):
            outcome = self.finish(job)
        self.assertTrue(outcome.accepted)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(outcome.result.outcome, 'partial')
        self.assertEqual([item.outcome for item in outcome.result.files], ['published', 'not_attempted', 'not_attempted'])
        self.assertEqual((self.target / self.refs[0]).read_bytes(), export_fixtures.tl(0))
        self.assertFalse((self.target / self.refs[1]).exists())

    def test_second_prepublication_failure_keeps_old_file_and_later_unattempted(self):
        self.parents()
        for ref in self.refs:
            (self.target / ref).write_bytes(b'previous target')
        self.controller.update_workspace_target('dirty translation')
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        original = _CandidateFile._flush_content
        calls = []
        def flush(candidate, *args, **kwargs):
            calls.append(candidate)
            if len(calls) == 2:
                raise OSError('synthetic flush failure')
            return original(candidate, *args, **kwargs)
        with mock.patch.object(_CandidateFile, '_flush_content', flush):
            result = self.finish(job).result
        self.assertEqual([item.outcome for item in result.files], ['published', 'failed', 'not_attempted'])
        self.assertEqual((self.target / self.refs[1]).read_bytes(), b'previous target')
        self.assertEqual((self.target / self.refs[2]).read_bytes(), b'previous target')
        self.assertTrue(self.controller.active_project_dirty)
        self.assertEqual(self.package_path.read_bytes(), self.package_bytes)

    def test_second_post_naming_fault_retains_uncertain_file_and_published_prefix(self):
        self.parents()
        calls = []
        def fault(point):
            if point == _POST_NAMING_FAULTS[0]:
                calls.append(point)
                if len(calls) == 2:
                    raise OSError('synthetic post naming failure')
        with mock.patch('rpy_project_batch_export.compose_platform_file_backend',
                        side_effect=lambda root: _PlatformAdapter(_fault_injector=fault)):
            view = self.preview()
            result = self.finish(self.controller.begin_tl_export_publish(view, self.target)).result
        self.assertEqual([item.outcome for item in result.files], ['published', 'uncertain', 'not_attempted'])
        self.assertEqual((self.target / self.refs[1]).read_bytes(), export_fixtures.tl(1))
        self.assertIsNone(result.files[1].output_sha256)
        self.assertFalse((self.target / self.refs[2]).exists())

    def test_cleanup_interrupt_owner_facts_reach_controller(self):
        self.parents()
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        original = _TargetObservation.close
        armed = [True]
        class Interrupted(BaseException):
            pass
        def close(observation):
            original(observation)
            if armed[0]:
                armed[0] = False
                raise Interrupted()
        with mock.patch.object(_TargetObservation, 'close', close):
            result = self.finish(job).result
        self.assertEqual([item.outcome for item in result.files], ['uncertain', 'not_attempted', 'not_attempted'])
        self.assertEqual((self.target / self.refs[0]).read_bytes(), export_fixtures.tl(0))
        self.assertFalse(self.controller.tl_export_publish_running)

    def test_finished_publication_late_delivery_never_installs_old_session(self):
        self.parents()
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        job.run()
        self.controller.load_sample()
        current = self.controller.project
        outcome = self.controller.finish_tl_export_job(job)
        self.assertFalse(outcome.accepted)
        self.assertIs(self.controller.project, current)
        self.assertEqual([item.outcome for item in outcome.result.files], ['published'] * 3)
        with self.assertRaisesRegex(EditorControllerError, 'NOT_ISSUED'):
            self.controller.finish_tl_export_job(job)

    def test_active_preview_disposal_does_not_wait_and_releases_every_source(self):
        entered, release = Event(), Event()
        original = ParserApplicationSurface.prepare_round_trip
        owners = []
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            owners.append(result)
            entered.set()
            if not release.wait(60):
                raise RuntimeError('test gate timeout')
            return result
        job = self.controller.begin_tl_export_preview(self.target)
        with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', prepare):
            worker = Thread(target=job.run)
            worker.start()
            try:
                self.assertTrue(entered.wait(60))
                self.controller.close_project()
                self.controller.abandon_file_jobs()
                self.assertTrue(worker.is_alive())
                self.assertTrue(job.disposed)
            finally:
                release.set()
                worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertTrue(all(owner.closed for owner in owners))
        self.assertEqual(list(self.controller.repository.config_dir.glob('rpy-export-*')), [])
        self.assertEqual(list(self.target.rglob('*.rpy')), [])


class QtBatchLifecycleTests(unittest.TestCase):
    setUpClass = classmethod(qt_fixtures.QtRpyBatchExportTests.setUpClass.__func__)
    setUp = qt_fixtures.QtRpyBatchExportTests.setUp
    tearDown = qt_fixtures.QtRpyBatchExportTests.tearDown
    wait_until = qt_fixtures.QtRpyBatchExportTests.wait_until
    open_export = qt_fixtures.QtRpyBatchExportTests.open_export

    def test_closed_directory_stage_does_not_restart_preview_or_publish(self):
        from rpy_project_batch_export import PreparedRpyBatchExport
        dialog = self.open_export()
        entered, release = Event(), Event()
        original = PreparedRpyBatchExport.prepare_directories
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(60):
                raise RuntimeError('test gate timeout')
            return result
        with mock.patch.object(PreparedRpyBatchExport, 'prepare_directories', prepare), \
                mock.patch.object(self.controller, 'begin_tl_export_preview',
                                  wraps=self.controller.begin_tl_export_preview) as previews:
            dialog.confirm_button.click()
            try:
                self.wait_until(entered.is_set)
                dialog.close()
                self.assertTrue(dialog.operation_running)
                self.assertFalse(dialog.isVisible())
            finally:
                release.set()
                self.wait_until(lambda: not dialog.operation_running)
            self.assertEqual(previews.call_count, 0)
        self.assertEqual(dialog.result.outcome, 'prepared')
        self.assertEqual(set(dialog.result.created_directories), {'a', 'b', 'b/extra'})
        self.assertEqual(list(self.target.rglob('*.rpy')), [])
        self.assertFalse(dialog.isVisible())

    def test_publish_close_keeps_event_loop_live_and_shows_partial_facts(self):
        for ref in self.refs:
            (self.target / ref).parent.mkdir(parents=True, exist_ok=True)
        dialog = self.open_export()
        entered, release = Event(), Event()
        original = ParserApplicationSurface.write_prepared
        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(60):
                raise RuntimeError('test gate timeout')
            return result
        ticks = []
        timer = QTimer(self.window)
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', write):
            dialog.confirm_button.click()
            try:
                self.wait_until(lambda: entered.is_set() and len(ticks) >= 2)
                dialog.close()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(dialog.operation_running)
            finally:
                release.set()
                self.wait_until(lambda: not dialog.operation_running)
                timer.stop()
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual(dialog.result.outcome, 'partial')
        self.assertEqual([item.outcome for item in dialog.result.files], ['published', 'not_attempted', 'not_attempted'])
        self.assertEqual([dialog.files_table.topLevelItem(i).text(1) for i in range(3)], ['已导出', '未执行', '未执行'])
        self.assertFalse(dialog.isVisible())
        self.assertIn('已导出 1', self.window.statusBar().currentMessage())


if __name__ == '__main__':
    unittest.main()
