"""Read-only diff navigation over Controller projections and cancellable jobs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest import mock

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from qt_project_source_update_dialog import QtProjectSourceUpdateDialog
from editor_source_update import SourceUpdateBlock, SourceUpdateItem, SourceUpdateReview, SourceUpdateText


def _wait_until(predicate):
    if not predicate():
        # Run Qt's event loop while the Python file worker makes progress.
        loop = QEventLoop()
        poll, deadline = QTimer(loop), QTimer(loop)
        deadline.setSingleShot(True)
        failure = None

        def check():
            nonlocal failure
            try:
                if predicate():
                    loop.quit()
            except BaseException as error:
                failure = error
                loop.quit()

        poll.timeout.connect(check)
        deadline.timeout.connect(loop.quit)
        try:
            poll.start(10)
            deadline.start(10000)
            loop.exec()
        finally:
            poll.stop()
            deadline.stop()
            poll.timeout.disconnect(check)
            deadline.timeout.disconnect(loop.quit)
        if failure is not None:
            raise failure
    return predicate()


class Job:
    def __init__(self, kind, result, gate=None):
        self.kind, self.result, self.gate = kind, result, gate
        self.done = self.cancelled = self.disposed = False

    def run(self):
        if self.gate is not None:
            self.gate.wait(3)
        self.done = True

    def cancel(self):
        self.cancelled = True

    def dispose(self):
        self.cancel()
        self.disposed = True

    def fail_before_start(self, error):
        self.done = self.cancelled = True


class Controller:
    def __init__(self):
        self.source_update_documents = tuple(SimpleNamespace(
            identity=SimpleNamespace(project=SimpleNamespace(session_id='test'), document_id=ref),
            source_ref=ref, display_name='intro.rpy')
            for ref in ('a/intro.rpy', 'b/intro.rpy'))
        self.current = True
        self.source_update_root = None
        self.items = tuple(SourceUpdateItem(category=category, source_ref='a/intro.rpy',
            segment_number=number, required=category in ('removed', 'ambiguous', 'unresolved'),
            old=None if category == 'new' else SourceUpdateText('a/intro.rpy', number, 'Old sentence.', '旧译文'),
            new=None if category in ('removed', 'ambiguous', 'unresolved') else
                SourceUpdateText('a/intro.rpy', number, 'New sentence.', '模板译文'))
            for number, category in enumerate(('unchanged', 'source_changed', 'new', 'removed',
                                               'ambiguous', 'unresolved'), 1))
        self.review = SourceUpdateReview(None, self.items, tuple(
            SourceUpdateBlock(indices, tuple(self.items[i].old for i in indices if self.items[i].old),
                              tuple(self.items[i].new for i in indices if self.items[i].new))
            for indices in ((1,), (2, 3), (4,), (5,))))
        self.gate = None
        self.applied = None
        self.error = None

    def begin_source_update_preview(self, root, refs):
        self.selection = (root, refs)
        self.current = True
        return Job('preview_source_update', self.review, self.gate)

    def finish_source_update_job(self, job):
        if self.error:
            raise ValueError(self.error)
        accepted = self.current and not job.cancelled and not job.disposed
        if accepted and job.kind == 'validate_source_update':
            self.applied = self.decisions
        return SimpleNamespace(kind=job.kind, result=job.result, accepted=accepted,
                               cancelled=job.cancelled)

    def source_update_preview_current(self, review):
        return self.current

    def cancel_source_update_preview(self, review=None):
        self.current = False

    def begin_source_update_apply(self, review, dispositions):
        self.decisions = dispositions
        return Job('validate_source_update', object(), self.gate)


class QtRpySourceUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controller = Controller()
        self.dialog = QtProjectSourceUpdateDialog(self.controller)
        self.dialog.show()

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.deleteLater()
        self.app.processEvents()

    def wait(self, predicate):
        self.assertTrue(_wait_until(predicate))

    def preview(self):
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value='/updated/tl'):
            self.dialog.root_button.click()
        self.dialog.preview_button.click()
        self.wait(lambda: not self.dialog.operation_running)

    def test_explicit_paths_preview_six_categories_and_required_decisions(self):
        self.dialog.paths.item(1, 1).setText('renamed/intro.rpy')
        self.preview()
        self.assertEqual(self.controller.selection,
                         (Path('/updated/tl'), ('a/intro.rpy', 'renamed/intro.rpy')))
        self.assertTrue(self.dialog.cancel_button.isDefault())
        self.assertEqual(self.dialog.results.rowCount(), 1)
        self.assertIn('源文变更：1', self.dialog.summary_label.text())
        self.assertIn('第 2 段', self.dialog.results.item(0, 2).text())
        self.assertIn('Old sentence.', self.dialog.diff_view.toPlainText())
        self.assertIn('New sentence.', self.dialog.diff_view.toPlainText())
        self.assertFalse(self.dialog.previous_button.isEnabled())
        self.assertEqual(self.dialog.position_label.text(), '1 / 4')
        self.assertFalse(self.dialog.apply_button.isEnabled())
        for index, row in ((1, 1), (2, 0), (3, 0)):
            self.dialog.next_button.click()
            choice = self.dialog.results.cellWidget(row, 3)
            self.assertIsNone(choice.currentData())
            self.assertEqual(choice.findData('remove') >= 0, index == 1)
            choice.setCurrentIndex(choice.findData('remove' if index == 1 else 'keep_detached'))
        self.assertEqual(self.dialog.position_label.text(), '4 / 4')
        self.assertFalse(self.dialog.next_button.isEnabled())
        self.dialog.previous_button.click()
        self.dialog.previous_button.click()
        self.assertEqual(self.dialog.results.cellWidget(1, 3).currentData(), 'remove')
        self.assertTrue(self.dialog.apply_button.isEnabled())
        self.dialog.apply_button.click()
        self.wait(lambda: not self.dialog.operation_running)
        self.assertEqual(self.controller.applied, ('remove', 'keep_detached', 'keep_detached'))
        self.assertIn('保存项目包', self.dialog.status_label.text())
        self.assertFalse(self.dialog.apply_button.isEnabled())

    def test_editing_input_or_context_invalidates_preview(self):
        self.preview()
        self.dialog.paths.item(0, 1).setText('new.rpy')
        self.assertIsNone(self.dialog.view)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertEqual(self.dialog.diff_view.toPlainText(), '')
        self.assertFalse(self.dialog.next_button.isVisible())
        self.preview()
        self.controller.current = False
        self.wait(lambda: self.dialog.view is None)
        self.assertIn('过期', self.dialog.status_label.text())

    def test_cancel_and_close_do_not_deliver_late_application(self):
        self.preview()
        for row in (1, 0, 0):
            self.dialog.next_button.click()
            self.dialog.results.cellWidget(row, 3).setCurrentIndex(1)
        self.controller.gate = Event()
        self.dialog.apply_button.click()
        runner = self.dialog._runner
        self.dialog.close()
        self.assertTrue(runner.job.disposed)
        self.controller.gate.set()
        runner._thread.join(3)
        self.app.processEvents()
        self.assertIsNone(self.controller.applied)
        self.assertFalse(self.dialog.isVisible())

    def test_cancel_running_preview_remains_responsive(self):
        self.controller.gate = Event()
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value='/updated/tl'):
            self.dialog.root_button.click()
        self.dialog.preview_button.click()
        self.dialog.cancel_button.click()
        self.assertTrue(self.dialog._runner.job.cancelled)
        self.controller.gate.set()
        self.wait(lambda: not self.dialog.operation_running)
        self.assertIsNone(self.dialog.view)
        self.assertIn('取消', self.dialog.status_label.text())

    def test_failed_preview_keeps_choices_and_reports_failure(self):
        self.controller.error = 'PARSER.RPY.UNSUPPORTED_SYNTAX'
        self.preview()
        self.assertIsNone(self.dialog.view)
        self.assertIn('失败', self.dialog.status_label.text())
        self.assertEqual(self.dialog.paths.item(1, 1).text(), 'b/intro.rpy')

    def test_no_diff_hides_navigation_and_distinguishes_template_bytes(self):
        for identical in (True, False):
            self.controller.review = SourceUpdateReview(None, (), (), identical)
            self.preview()
            self.assertFalse(self.dialog.next_button.isVisible())
            self.assertFalse(self.dialog.previous_button.isVisible())
            self.assertIn('模板一致' if identical else '模板其他内容有变化', self.dialog.diff_view.toPlainText())

    def test_visual_block_order_does_not_reorder_required_dispositions(self):
        original = self.controller.review
        self.controller.review = SourceUpdateReview(None, original.items, tuple(reversed(original.blocks)))
        self.preview()
        while True:
            for row in range(self.dialog.results.rowCount()):
                choice = self.dialog.results.cellWidget(row, 3)
                if choice is not None:
                    choice.setCurrentIndex(choice.findData('remove') if choice.findData('remove') >= 0 else 1)
            if not self.dialog.next_button.isEnabled():
                break
            self.dialog.next_button.click()
        self.assertTrue(self.dialog.apply_button.isEnabled())
        self.dialog.apply_button.click()
        self.wait(lambda: not self.dialog.operation_running)
        self.assertEqual(self.controller.applied, ('remove', 'keep_detached', 'keep_detached'))

    def test_body_is_escaped_read_only_and_long_text_is_complete(self):
        old = SourceUpdateText('a/<intro>.rpy', 1, '<img src="file:///secret"> & old', '')
        new = SourceUpdateText('a/<intro>.rpy', 1, '<script>new</script> & "text"', '')
        self.controller.review = SourceUpdateReview(None, (), (SourceUpdateBlock((), (old,), (new,)),))
        self.preview()
        body = self.dialog.diff_view.toPlainText()
        self.assertIn(old.source, body)
        self.assertIn(new.source, body)
        self.assertIn('a/<intro>.rpy', body)
        self.assertTrue(self.dialog.diff_view.isReadOnly())
        self.assertNotIn('<img ', self.dialog.diff_view.toHtml())
        long = SourceUpdateText('intro.rpy', 1, 'word ' * 3000, '')
        self.controller.review = SourceUpdateReview(None, (), (SourceUpdateBlock((), (long,), (new,)),))
        self.preview()
        self.assertIn(long.source, self.dialog.diff_view.toPlainText())
        self.assertIn('未做词级高亮', self.dialog.diff_view.toPlainText())


class QtRpySourceUpdateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        import tempfile
        from qt_editor import _compose_editor_controller, _compose_chunk_controller
        from qt_editor_window import QtEditorWindow
        from resource_repository import ResourceRepository
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'original.rpy'
        self.raw = ('translate zh_Hans intro_aa1:\n    # "Old source"\n    "已有译文"\n\n'
                    'translate zh_Hans strings:\n    old "Open"\n    new "打开"\n')
        self.source.write_text(self.raw, encoding='utf-8')
        self.package = self.root / 'project.localcat-project'
        repository = ResourceRepository(self.root / 'data')
        self.controller, _ = _compose_editor_controller(repository)
        chunks = _compose_chunk_controller(self.controller, repository)
        job = self.controller.begin_file_open(self.source)
        job.run()
        self.controller.finish_file_job(job)
        self.controller.confirm_current()
        job = self.controller.begin_file_save(self.package)
        job.run()
        self.controller.finish_file_job(job)
        self.original_package = self.package.read_bytes()
        self.controller.go_to_workspace_segment(self.controller.workspace_view.segments[0].identity)
        self.updated = self.root / 'updated'
        self.updated.mkdir()
        self.new_source = self.updated / 'original.rpy'
        self.new_source.write_text(self.raw.replace('Old source', 'Changed source').replace('old "Open"', 'old "Start"'), encoding='utf-8')
        self.window = QtEditorWindow(self.controller, chunk_controller=chunks)
        self.window.show()
        self.window._render_project()
        self.app.processEvents()

    def tearDown(self):
        if self.window._source_update_dialog:
            self.window._source_update_dialog.shutdown()
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, predicate):
        self.assertTrue(_wait_until(predicate))

    def open_preview(self, rename=False):
        self.assertTrue(self.window.source_update_action.isEnabled())
        if rename:
            self.controller.close_project()
            self.controller.open_project_package(self.package)
            self.window._render_project()
        else:
            self.source.write_bytes(self.new_source.read_bytes())
        self.window.source_update_action.trigger()
        dialog = self.window._source_update_dialog
        if rename:
            self.new_source.rename(self.updated / 'renamed.rpy')
            dialog.paths.item(0, 1).setText('renamed.rpy')
        with mock.patch.object(QFileDialog, 'getExistingDirectory',
                               return_value=str(self.updated if rename else self.root)):
            dialog.root_button.click()
        dialog.preview_button.click()
        self.wait(lambda: not dialog.operation_running)
        self.assertIsNotNone(dialog.view, dialog.status_label.text())
        return dialog

    def test_real_menu_rename_apply_updates_editor_and_keeps_package_unsaved(self):
        dialog = self.open_preview(rename=True)
        categories = [item.category for item in dialog.view.items]
        self.assertCountEqual(categories, ['source_changed', 'new', 'removed'])
        while True:
            for row in range(dialog.results.rowCount()):
                choice = dialog.results.cellWidget(row, 3)
                if choice is not None:
                    choice.setCurrentIndex(choice.findData('remove'))
            if not dialog.next_button.isEnabled():
                break
            dialog.next_button.click()
        dialog.apply_button.click()
        self.wait(lambda: not dialog.operation_running)
        self.assertIsNotNone(dialog.result_receipt, dialog.status_label.text())
        segment = self.controller.workspace_view.segments[0]
        self.assertEqual(segment.source, 'Changed source')
        self.assertEqual(segment.target, '已有译文')
        self.assertFalse(segment.confirmed)
        self.assertEqual(self.controller.workspace_view.documents[0].source_ref, 'renamed.rpy')
        self.assertTrue(self.controller.active_project_dirty)
        self.assertIn('*', self.window.windowTitle())
        self.assertIn('保存项目包', self.window.statusBar().currentMessage())
        self.assertEqual(self.package.read_bytes(), self.original_package)

    def test_real_edit_and_project_switch_stale_preview(self):
        dialog = self.open_preview()
        self.window.target_editor.setPlainText('预览后的编辑')
        self.wait(lambda: dialog.view is None)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '预览后的编辑')
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertTrue(dialog.preview_button.isEnabled())
        self.controller.close_project()
        self.window.load_sample()
        self.wait(lambda: not dialog.preview_button.isEnabled())
        self.window._refresh_tl_export_action()
        self.assertFalse(self.window.source_update_action.isVisible())
        self.assertEqual(self.package.read_bytes(), self.original_package)

    def test_warm_other_root_explains_reopen_requirement(self):
        self.window.source_update_action.trigger()
        dialog = self.window._source_update_dialog
        self.assertEqual(dialog._root, self.root)
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.updated)):
            dialog.root_button.click()
        dialog.preview_button.click()
        self.wait(lambda: not dialog.operation_running)
        self.assertIsNone(dialog.view)
        self.assertIn('请先保存项目包并重新打开', dialog.status_label.text())
        self.assertFalse(self.controller.active_project_dirty)
        self.assertEqual(self.package.read_bytes(), self.original_package)

    def test_real_invalid_template_failure_keeps_project(self):
        self.source.write_text('translate zh_Hans intro_aa1:\n    python:\n        pass\n', encoding='utf-8')
        self.window.source_update_action.trigger()
        dialog = self.window._source_update_dialog
        before = self.controller.workspace_view
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.root)):
            dialog.root_button.click()
        dialog.preview_button.click()
        self.wait(lambda: not dialog.operation_running)
        self.assertIsNone(dialog.view)
        self.assertIn('失败', dialog.status_label.text())
        self.assertEqual(self.controller.workspace_view, before)
        self.assertFalse(self.controller.active_project_dirty)
        self.assertEqual(self.package.read_bytes(), self.original_package)


if __name__ == '__main__':
    unittest.main()
