"""Directory intake through the real background Controller/Qt open workflow."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import nullcontext
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock

from PySide6.QtCore import QEventLoop, QTimer, Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox
from editor_controller import EditorControllerError
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from qt_directory_open_dialog import QtDirectoryOpenDialog, QtLocalProjectOpenDialog
from project_package import ProjectPackageService
from project_workspace_discovery import ProjectDirectoryDiscoveryService
from resource_repository import ResourceRepository
from tests.test_project_single_file_profile import _PACKAGE_PORT


class QtDirectoryOpenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'tl'
        (self.root / 'a').mkdir(parents=True)
        (self.root / 'b' / 'deep').mkdir(parents=True)
        (self.root / 'a' / 'same.txt').write_text('First\n')
        (self.root / 'b' / 'deep' / 'same.txt').write_text('Second\n')
        (self.root / 'bad.json').write_text('invalid unselected')
        (self.root / 'unknown.xyz').write_text('unsupported')
        self.destination = self.root / 'created.localcat-project'
        self.controller, _ = _compose_editor_controller(ResourceRepository(self.base / 'data'))
        self.window = QtEditorWindow(self.controller)
        self.window._confirm_unsaved = lambda: True
        self.errors = []
        self.window._show_error = lambda *args: self.errors.append(args)
        self.window.show()

    def tearDown(self):
        self.window.cancel_file_operation()
        self.wait_until(lambda: not self.window.file_operation_running)
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def wait_until(self, predicate):
        if not predicate():
            loop = QEventLoop()
            poll, deadline = QTimer(loop), QTimer(loop)
            deadline.setSingleShot(True)
            deadline.setTimerType(Qt.TimerType.PreciseTimer)
            poll.timeout.connect(lambda: loop.quit() if predicate() else None)
            deadline.timeout.connect(loop.quit)
            poll.start(10)
            deadline.start(5000)
            loop.exec()
        self.assertTrue(predicate())

    def preview(self):
        job = self.controller.begin_directory_open(self.root)
        job.run()
        result = self.controller.finish_file_job(job)
        self.assertTrue(result.accepted)
        return result.result

    def ids(self, review, refs):
        entries = {entry.source_ref: entry.entry_id for entry in review.preview.entries}
        return tuple(entries[ref] for ref in refs)

    def publish(self, review, refs):
        return self.controller.begin_directory_publish(
            review, self.ids(review, refs), self.destination,
            name='目录项目', source_locale='en', target_locale='zh-CN')

    def test_real_nested_single_and_multiple_clean_package_and_cold_open(self):
        for refs in (('b/deep/same.txt',), ('b/deep/same.txt', 'a/same.txt')):
            with self.subTest(refs=refs):
                self.destination = self.root / (str(len(refs)) + '.localcat-project')
                def choose(dialog):
                    for entry_id in self.ids(dialog.review, tuple(reversed(refs))):
                        dialog.entry_items[entry_id].setCheckState(0, Qt.CheckState.Checked)
                    if len(refs) > 1:
                        dialog.selected_files.setCurrentRow(1)
                        dialog.move_selected(-1)
                    return QDialog.DialogCode.Accepted
                with mock.patch.object(QtDirectoryOpenDialog, 'exec', choose), \
                     mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')):
                    self.assertTrue(self.window.open_directory_async(self.root, wait=True), self.errors)
                workspace = self.controller._workspace_service.workspace
                self.assertEqual(tuple(doc.source_ref for doc in workspace.documents), refs)
                self.assertFalse(self.controller.active_project_dirty)
                self.assertIsNone(self.controller._rpy_project_session)
                opened = ProjectPackageService().open(self.destination)
                self.assertEqual(opened.workspace, workspace)

    def test_tree_defaults_none_unavailable_reasons_order_and_incomplete_disabled(self):
        review = self.preview()
        dialog = QtDirectoryOpenDialog(review)
        try:
            self.assertEqual(dialog.ordered_entry_ids, ())
            self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            unavailable = next(item for item in review.preview.entries if item.source_ref == 'unknown.xyz')
            self.assertIn('不支持', dialog.entry_items[unavailable.entry_id].text(1))
            self.assertFalse(dialog.entry_items[unavailable.entry_id].flags() & Qt.ItemFlag.ItemIsUserCheckable)
            for entry_id in self.ids(review, ('a/same.txt', 'b/deep/same.txt')):
                dialog.entry_items[entry_id].setCheckState(0, Qt.CheckState.Checked)
            self.assertTrue(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            dialog.selected_files.setCurrentRow(1)
            dialog.move_selected(-1)
            self.assertEqual(dialog.ordered_entry_ids, self.ids(review, ('b/deep/same.txt', 'a/same.txt')))
        finally:
            dialog.close()
        from dataclasses import replace
        from project_directory_contracts import DirectoryDiscoveryRejection, DirectoryRejectionCode
        incomplete = replace(review, preview=replace(review.preview, complete=False,
            rejection=DirectoryDiscoveryRejection(DirectoryRejectionCode.INCOMPLETE)))
        blocked = QtDirectoryOpenDialog(incomplete)
        self.assertFalse(blocked.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        blocked.close()
        self.controller.cancel_directory_open(review)

    def test_folder_bulk_selection_partial_state_order_and_stale_controls(self):
        (self.root / 'b' / 'other.txt').write_text('Third\n')
        (self.root / 'blocked').mkdir()
        (self.root / 'blocked' / 'unknown.xyz').write_text('unsupported')
        review = self.preview()
        current = True
        dialog = QtDirectoryOpenDialog(review, current=lambda: current)
        items = {entry.source_ref: dialog.entry_items[entry.entry_id]
                 for entry in review.preview.entries}
        try:
            self.assertEqual(dialog.ordered_entry_ids, ())
            self.assertTrue(items['b'].flags() & Qt.ItemFlag.ItemIsUserCheckable)
            self.assertFalse(items['blocked'].flags() & Qt.ItemFlag.ItemIsUserCheckable)
            items['a/same.txt'].setCheckState(0, Qt.CheckState.Checked)
            items['b/deep/same.txt'].setCheckState(0, Qt.CheckState.Checked)
            self.assertEqual(items['b/deep'].checkState(0), Qt.CheckState.Checked)
            self.assertEqual(items['b'].checkState(0), Qt.CheckState.PartiallyChecked)
            dialog.selected_files.setCurrentRow(1)
            dialog.move_selected(-1)
            items['b'].setCheckState(0, Qt.CheckState.Checked)
            self.assertEqual(dialog.ordered_entry_ids,
                             self.ids(review, ('b/deep/same.txt', 'a/same.txt', 'b/other.txt')))
            items['b/other.txt'].setCheckState(0, Qt.CheckState.Unchecked)
            self.assertEqual(items['b'].checkState(0), Qt.CheckState.PartiallyChecked)
            items['b'].setCheckState(0, Qt.CheckState.Unchecked)
            self.assertEqual(dialog.ordered_entry_ids, self.ids(review, ('a/same.txt',)))
            self.assertEqual(items['b/deep'].checkState(0), Qt.CheckState.Unchecked)
            dialog.select_all_button.click()
            selectable = tuple(entry.entry_id for entry in review.preview.entries if entry.selectable)
            self.assertEqual(dialog.ordered_entry_ids, selectable)
            self.assertIn(self.ids(review, ('bad.json',))[0], dialog.ordered_entry_ids)
            self.assertEqual(items['b'].checkState(0), Qt.CheckState.Checked)
            self.assertEqual(items['unknown.xyz'].checkState(0), Qt.CheckState.Unchecked)
            dialog.clear_selection_button.click()
            self.assertEqual(dialog.ordered_entry_ids, ())
            self.assertEqual(items['b'].checkState(0), Qt.CheckState.Unchecked)
            self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
            current = False
            dialog._update_ready()
            self.assertFalse(dialog.select_all_button.isEnabled())
            self.assertFalse(dialog.clear_selection_button.isEnabled())
            self.assertFalse(dialog.tree.isEnabled())
        finally:
            dialog.close()
            self.controller.cancel_directory_open(review)

    def test_cancel_review_save_picker_failure_and_bad_selected_keep_dirty_old_project(self):
        self.window.load_sample()
        self.window.target_editor.setPlainText('旧项目修改')
        old = self.controller.project
        with mock.patch.object(QtDirectoryOpenDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            self.assertFalse(self.window.open_directory_async(self.root, wait=True))
        def choose(dialog):
            dialog.entry_items[self.ids(dialog.review, ('a/same.txt',))[0]].setCheckState(0, Qt.CheckState.Checked)
            return QDialog.DialogCode.Accepted
        with mock.patch.object(QtDirectoryOpenDialog, 'exec', choose), \
             mock.patch.object(QFileDialog, 'getSaveFileName', return_value=('', '')):
            self.assertFalse(self.window.open_directory_async(self.root, wait=True))
        for refs, failure in ((('bad.json',), False), (('a/same.txt',), True)):
            review = self.preview()
            job = self.publish(review, refs)
            with mock.patch(_PACKAGE_PORT + '.stage_candidate', side_effect=OSError('failure')) if failure else nullcontext():
                job.run()
            try:
                outcome = self.controller.finish_file_job(job)
                self.assertFalse(outcome.accepted)
            except EditorControllerError as error:
                if not failure:
                    self.assertIn('bad.json', str(error))
            self.assertIs(self.controller.project, old)
            self.assertTrue(self.controller.active_project_dirty)
            self.assertFalse(self.destination.exists())

    def test_zero_forged_stale_changed_file_and_cancel_before_start_do_not_publish(self):
        review = self.preview()
        with self.assertRaises((EditorControllerError, ValueError)):
            self.controller.begin_directory_publish(review, (), self.destination,
                name='Empty', source_locale='en', target_locale='zh-CN')
        review = self.preview()
        from dataclasses import replace
        with self.assertRaises(EditorControllerError):
            self.publish(replace(review), ('a/same.txt',))
        stale = review
        newer = self.preview()
        with self.assertRaises(EditorControllerError):
            self.publish(stale, ('a/same.txt',))
        self.controller.cancel_directory_open(newer)
        review = self.preview()
        (self.root / 'a' / 'same.txt').write_text('Changed\n')
        job = self.publish(review, ('a/same.txt',))
        job.run()
        with self.assertRaises(EditorControllerError):
            self.controller.finish_file_job(job)
        review = self.preview()
        job = self.publish(review, ('a/same.txt',))
        job.cancel()
        job.run()
        self.assertFalse(self.controller.finish_file_job(job).accepted)
        self.assertFalse(self.destination.exists())

    def test_inflight_scan_cancel_and_window_abandon_release_owner(self):
        entered, release = Event(), Event()
        original = ProjectDirectoryDiscoveryService.preview
        owners = []
        def delayed(owner, root):
            owners.append(owner)
            result = original(owner, root)
            entered.set()
            release.wait(3)
            return result
        with mock.patch.object(ProjectDirectoryDiscoveryService, 'preview', delayed):
            job = self.controller.begin_directory_open(self.root)
            thread = Thread(target=job.run)
            thread.start()
            self.assertTrue(entered.wait(3))
            self.controller.abandon_file_jobs()
            release.set()
            thread.join(3)
        self.assertTrue(job.done)
        self.assertTrue(job.disposed)
        with self.assertRaises(Exception):
            owners[0].preview(self.root)
        self.assertFalse(self.controller.has_workspace)

    def test_cancel_inflight_intake_preserves_dirty_session_and_releases_candidates(self):
        import editor_directory_open
        self.window.load_sample()
        self.window.target_editor.setPlainText('仍然保留')
        old = self.controller.project
        review = self.preview()
        job = self.publish(review, ('a/same.txt',))
        entered, release = Event(), Event()
        original = editor_directory_open.prepare_directory_project_documents
        prepared = []
        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            prepared.append(value)
            entered.set()
            release.wait(3)
            return value
        with mock.patch.object(editor_directory_open, 'prepare_directory_project_documents', delayed):
            thread = Thread(target=job.run)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                job.cancel()
            finally:
                release.set()
                thread.join(3)
        outcome = self.controller.finish_file_job(job)
        self.assertFalse(outcome.accepted)
        self.assertTrue(prepared[0].closed)
        self.assertIs(self.controller.project, old)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertFalse(self.destination.exists())

    def test_session_switch_disables_review_and_close_during_review_does_not_publish(self):
        review = self.preview()
        dialog = QtDirectoryOpenDialog(review,
            current=lambda: self.controller.directory_review_current(review))
        dialog.entry_items[self.ids(review, ('a/same.txt',))[0]].setCheckState(0, Qt.CheckState.Checked)
        self.assertTrue(dialog._update_ready())
        self.controller.load_sample()
        self.assertFalse(dialog._update_ready())
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        dialog.close()
        def close_review(dialog):
            self.window.close()
            return QDialog.DialogCode.Accepted
        with mock.patch.object(QtDirectoryOpenDialog, 'exec', close_review), \
             mock.patch.object(QFileDialog, 'getSaveFileName', side_effect=AssertionError('closed review')):
            self.assertFalse(self.window.open_directory_async(self.root, wait=True))
        self.assertFalse(self.destination.exists())

    def test_late_cancel_after_durable_publication_keeps_receipt_without_switching(self):
        self.window.load_sample()
        old = self.controller.project
        review = self.preview()
        job = self.publish(review, ('a/same.txt',))
        job.run()
        job.cancel()
        outcome = self.controller.finish_file_job(job)
        self.assertFalse(outcome.accepted)
        self.assertTrue(outcome.result.receipt.durable)
        self.assertIs(self.controller.project, old)
        self.assertTrue(self.destination.exists())

    def test_close_during_directory_publication_clean_cancel_then_close_keeps_real_package(self):
        import editor_directory_open
        review = self.preview()
        job = self.publish(review, ('a/same.txt',))
        published, release = Event(), Event()
        original = editor_directory_open.DirectoryOpenCandidate.publish
        def delayed(owner, *args):
            result = original(owner, *args)
            self.assertTrue(result.result.receipt.durable)
            published.set()
            release.wait(3)
            return result
        with mock.patch.object(editor_directory_open.DirectoryOpenCandidate, 'publish', delayed):
            self.window._run_file_operation(job, wait=False)
            try:
                self.wait_until(published.is_set)
                self.assertTrue(self.destination.exists())
                with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel) as question:
                    self.assertFalse(self.window.close())
                    question.assert_called_once()
                self.assertTrue(self.window.isVisible())
                self.assertFalse(job.disposed)
                with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Close) as question:
                    self.assertTrue(self.window.close())
                    question.assert_called_once()
                self.assertTrue(job.disposed)
                self.assertFalse(self.controller.has_workspace)
            finally:
                release.set()
                self.wait_until(lambda: job.done)
        self.assertEqual(len(ProjectPackageService().open(self.destination).workspace.documents), 1)

    def test_close_during_directory_publication_dirty_preserves_old_edits_without_save_prompt(self):
        import editor_directory_open
        self.window.load_sample()
        self.window.target_editor.setPlainText('旧项目仍需保存')
        old = self.controller.project
        review = self.preview()
        job = self.publish(review, ('a/same.txt',))
        published, release = Event(), Event()
        original = editor_directory_open.DirectoryOpenCandidate.publish
        def delayed(owner, *args):
            result = original(owner, *args)
            published.set()
            release.wait(3)
            return result
        with mock.patch.object(editor_directory_open.DirectoryOpenCandidate, 'publish', delayed):
            self.window._run_file_operation(job, wait=False)
            try:
                self.wait_until(published.is_set)
                with mock.patch.object(QMessageBox, 'question') as question, \
                     mock.patch.object(self.window, '_confirm_unsaved') as confirm:
                    self.assertFalse(self.window.close())
                    question.assert_not_called()
                    confirm.assert_not_called()
                self.assertTrue(self.window.isVisible())
                self.assertIs(self.controller.project, old)
                self.assertTrue(self.controller.active_project_dirty)
                self.assertIn('请先等待建包结束或取消导入', self.window.statusBar().currentMessage())
            finally:
                job.cancel()
                release.set()
                self.wait_until(lambda: not self.window.file_operation_running)
        self.assertIs(self.controller.project, old)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertIn('项目包已建立', self.window.statusBar().currentMessage())

    def test_unified_open_files_and_folder_branch_keep_existing_picker(self):
        with mock.patch.object(QtLocalProjectOpenDialog, 'exec', return_value=QDialog.DialogCode.Accepted), \
             mock.patch.object(QFileDialog, 'getOpenFileNames', return_value=([str(self.root / 'a' / 'same.txt')], '')):
            self.assertTrue(self.window._choose_open_home())
        self.assertFalse(self.controller.has_workspace)
        def folder(dialog):
            dialog.selection_kind = 'folder'
            return QDialog.DialogCode.Accepted
        with mock.patch.object(QtLocalProjectOpenDialog, 'exec', folder), \
             mock.patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.root)), \
             mock.patch.object(self.window, 'open_directory_async', return_value=True) as opened:
            self.assertTrue(self.window._choose_open_home())
            opened.assert_called_once_with(self.root)


if __name__ == '__main__':
    unittest.main()
