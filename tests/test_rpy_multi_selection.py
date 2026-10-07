"""Configured TL selection through the real directory and explicit-file UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog
from editor_controller import EditorControllerError
from project_codec_settings import CodecProviderSetting, CodecSettings, CodecSettingsRepository
from project_package import ProjectPackageService
from qt_directory_open_dialog import QtDirectoryOpenDialog
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


def tl(language='chinese', target='已存在'):
    # Identical local keys across documents must remain distinct project identities.
    return (f'translate {language} chapter_001:\n'
            f'    # guide "Needle"\n    guide "{target}"\n\n'
            f'translate {language} strings:\n'
            '    old "Return"\n    new "返回"\n')


class RpyMultiSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'tl'
        self.refs = ('b/extra/ending.rpy', 'a/intro.rpy', 'b/intro.rpy')
        for ref in self.refs:
            self.write(ref, tl())
        self.write('unchecked.rpy', 'translate chinese python:\n    evil()\n')
        self.controller, _ = _compose_editor_controller(ResourceRepository(self.base / 'data'))
        self.window = QtEditorWindow(self.controller)
        self.window._confirm_unsaved = lambda: True
        self.errors = []
        self.window._show_error = lambda *args: self.errors.append(args)
        self.destination = self.base / 'chosen.localcat-project'
        self.window.show()

    def write(self, ref, text):
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def tearDown(self):
        self.window.cancel_file_operation()
        self.wait(lambda: not self.window.file_operation_running)
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def wait(self, predicate):
        if not predicate():
            loop = QEventLoop()
            poll, deadline = QTimer(loop), QTimer(loop)
            deadline.setSingleShot(True)
            poll.timeout.connect(lambda: loop.quit() if predicate() else None)
            deadline.timeout.connect(loop.quit)
            poll.start(10)
            deadline.start(10000)
            loop.exec()
        self.assertTrue(predicate())

    def preview(self):
        job = self.controller.begin_directory_open(self.root)
        job.run()
        return self.controller.finish_file_job(job).result

    def publish_job(self, refs):
        review = self.preview()
        entries = {entry.source_ref: entry.entry_id for entry in review.preview.entries}
        return self.controller.begin_directory_publish(
            review, tuple(entries[ref] for ref in refs), self.destination,
            name='Chapters', source_locale='en', target_locale='zh-CN')

    def open_directory(self, refs):
        def choose(dialog):
            entries = {entry.source_ref: entry.entry_id for entry in dialog.review.preview.entries}
            for ref in reversed(refs):
                dialog.entry_items[entries[ref]].setCheckState(0, Qt.CheckState.Checked)
            # Explicit order is a user choice, independent of preview/tree sorting.
            for wanted_row, ref in enumerate(refs):
                entry_id = entries[ref]
                row = dialog.ordered_entry_ids.index(entry_id)
                dialog.selected_files.setCurrentRow(row)
                while row > wanted_row:
                    dialog.move_selected(-1)
                    row -= 1
            return QDialog.DialogCode.Accepted
        with mock.patch.object(QtDirectoryOpenDialog, 'exec', choose), \
             mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')):
            return self.window.open_directory_async(self.root, wait=True)

    def test_nested_order_same_names_edit_save_offline_and_multi_export_guard(self):
        self.assertTrue(self.open_directory(self.refs), self.errors)
        docs = self.controller._workspace_service.workspace.documents
        self.assertEqual(tuple(d.source_ref for d in docs), self.refs)
        self.assertEqual(tuple(d.display_name for d in docs), ('ending.rpy', 'intro.rpy', 'intro.rpy'))
        self.assertEqual(len({d.document_id for d in docs}), 3)
        self.assertEqual(len({s.identity for s in self.controller.workspace_view.segments}), 6)
        self.assertTrue(all(not s.confirmed for s in self.controller.workspace_view.segments))
        self.assertFalse(self.controller.active_project_dirty)
        self.assertFalse(self.window.tl_export_action.isEnabled())
        with self.assertRaises(EditorControllerError):
            self.controller.begin_tl_export_preview(self.base / 'must-not-export.rpy')
        self.assertEqual(tuple(group.name for group in self.controller.workspace_document_tree), ('b', 'a'))
        self.window.target_editor.setPlainText('保存新译文')
        self.window.confirm_current()
        self.assertTrue(self.window.save_workspace_package(wait=True), self.errors)
        identities = tuple(s.identity.segment_identity for s in self.controller.workspace_view.segments)
        saved = ProjectPackageService().open(self.destination).workspace
        self.controller.close_project()
        self.root.rename(self.base / 'source-offline')
        self.assertTrue(self.window.open_project_package_path(self.destination), self.errors)
        self.assertEqual(self.controller._workspace_service.workspace, saved)
        self.assertEqual(tuple(s.identity.segment_identity for s in self.controller.workspace_view.segments),
                         identities)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '保存新译文')
        self.assertTrue(self.controller.workspace_view.segments[0].confirmed)
        self.assertFalse(self.controller.active_project_dirty)
        self.assertEqual(tuple(d.source_ref for d in self.controller.workspace_view.documents), self.refs)

    def test_nested_single_keeps_original_root_and_can_prepare_offline_export(self):
        self.assertTrue(self.open_directory(self.refs[:1]), self.errors)
        workspace = self.controller._workspace_service.workspace
        self.assertEqual(workspace.origin.profile_version, 'explicit-single-file-v1')
        self.assertEqual(workspace.documents[0].source_ref, self.refs[0])
        self.assertEqual(workspace.documents[0].display_name, 'ending.rpy')
        self.controller.close_project()
        self.root.rename(self.base / 'source-offline')
        self.assertTrue(self.window.open_project_package_path(self.destination), self.errors)
        self.assertTrue(self.window.tl_export_action.isEnabled())
        job = self.controller.begin_tl_export_preview(self.base / 'export.rpy')
        job.run()
        outcome = self.controller.finish_tl_export_job(job)
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.result.status, 'ready')
        self.controller.cancel_tl_export_preview()

    def test_mixed_language_and_selected_bad_file_preserve_dirty_project_and_sources(self):
        other = self.write('japanese.rpy', tl('japanese'))
        self.window.load_sample()
        self.window.target_editor.setPlainText('保留旧修改')
        old = self.controller.project
        for refs, code, source in (
                ((self.refs[0], 'japanese.rpy'), 'RPY.IMPORT.MIXED_LANGUAGE', 'japanese.rpy'),
                ((self.refs[0], 'unchecked.rpy'), 'PARSER.RPY.UNSUPPORTED_SYNTAX', 'unchecked.rpy')):
            with self.subTest(refs=refs):
                job = self.publish_job(refs)
                job.run()
                with self.assertRaises(EditorControllerError) as caught:
                    self.controller.finish_file_job(job)
                self.assertIn(code, str(caught.exception))
                self.assertIn(source, str(caught.exception))
                self.assertIs(self.controller.project, old)
                self.assertTrue(self.controller.active_project_dirty)
                self.assertFalse(self.destination.exists())
        self.assertEqual(other.read_text(), tl('japanese'))

    def test_limit_rejects_257_before_intake_and_accepts_256(self):
        refs = tuple(f'bounded/{i:03}.rpy' for i in range(257))
        for ref in refs:
            self.write(ref, tl())
        import editor_directory_open
        original = editor_directory_open.prepare_directory_project_documents
        job = self.publish_job(refs)
        with mock.patch.object(editor_directory_open, 'prepare_directory_project_documents', wraps=original) as intake:
            job.run()
            with self.assertRaises(EditorControllerError) as caught:
                self.controller.finish_file_job(job)
            self.assertIn('RPY.IMPORT.DOCUMENT_LIMIT_EXCEEDED', str(caught.exception))
            self.assertIn('256', str(caught.exception))
            intake.assert_not_called()
        self.assertFalse(self.destination.exists())
        import rpy_project_adapter
        with mock.patch.object(rpy_project_adapter, 'prepare_selected_project_documents') as intake:
            self.assertFalse(self.window.create_workspace_from_selected_files(
                self.root, tuple(self.root / ref for ref in refs), self.destination,
                name='Over limit', source_locale='en', target_locale='zh-CN'))
            self.assertIn('RPY.IMPORT.DOCUMENT_LIMIT_EXCEEDED', self.errors[-1][1])
            intake.assert_not_called()
        job = self.publish_job(refs[:256])
        job.run()
        self.assertTrue(self.controller.finish_file_job(job).accepted)
        self.assertEqual(len(self.controller.workspace_view.documents), 256)

    def test_explicit_selection_uses_same_validation_and_retained_private_handoff(self):
        self.write('plain.txt', 'Neutral text\n')
        refs = (self.refs[2], 'plain.txt', self.refs[1])
        self.assertTrue(self.window.create_workspace_from_selected_files(
            self.root, tuple(self.root / ref for ref in refs), self.destination,
            name='Selected', source_locale='en', target_locale='zh-CN'), self.errors)
        docs = self.controller._workspace_service.workspace.documents
        self.assertEqual(tuple(d.source_ref for d in docs), refs)
        self.assertEqual(tuple(d.display_name for d in (docs[0], docs[2])), ('intro.rpy', 'intro.rpy'))
        self.assertIsNotNone(docs[0].codec_private_member)
        self.assertIsNone(docs[1].codec_private_member)
        self.assertEqual(ProjectPackageService().open(self.destination).workspace,
                         self.controller._workspace_service.workspace)
        self.window.target_editor.setPlainText('保留已导入项目的修改')
        before = self.controller._workspace_service
        self.write('japanese.rpy', tl('japanese'))
        rejected = self.base / 'rejected.localcat-project'
        for source, code in (('japanese.rpy', 'RPY.IMPORT.MIXED_LANGUAGE'),
                             ('unchecked.rpy', 'PARSER.RPY.UNSUPPORTED_SYNTAX')):
            with self.subTest(source=source):
                self.assertFalse(self.window.create_workspace_from_selected_files(
                    self.root, (self.root / self.refs[0], self.root / source), rejected,
                    name='Mixed', source_locale='en', target_locale='und'))
                self.assertIn(source, self.errors[-1][1])
                self.assertIn(code, self.errors[-1][1])
                self.assertIs(self.controller._workspace_service, before)
                self.assertTrue(self.controller.active_project_dirty)
                self.assertFalse(rejected.exists())

    def test_explicit_worker_cancel_and_disabled_codec_keep_old_session(self):
        import rpy_project_adapter
        self.window.load_sample()
        self.window.target_editor.setPlainText('旧稿')
        old = self.controller.project
        entered, release = Event(), Event()
        heartbeat = []
        original = rpy_project_adapter.prepare_selected_project_documents
        def delayed(*args, **kwargs):
            entered.set()
            # Released by the GUI timer; a synchronous UI implementation cannot progress.
            release.wait(3)
            return original(*args, **kwargs)
        timer = QTimer()
        def cancel():
            heartbeat.append(True)
            if entered.is_set():
                self.window.cancel_file_operation()
                release.set()
        timer.timeout.connect(cancel)
        timer.start(10)
        with mock.patch.object(rpy_project_adapter, 'prepare_selected_project_documents', delayed):
            self.assertFalse(self.window.create_workspace_from_selected_files(
                self.root, tuple(self.root / ref for ref in self.refs[:2]), self.destination,
                name='Cancelled', source_locale='en', target_locale='zh-CN'), self.errors)
        timer.stop()
        self.assertTrue(entered.is_set())
        self.assertTrue(heartbeat)
        self.assertIs(self.controller.project, old)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertFalse(self.destination.exists())
        CodecSettingsRepository(self.controller.repository.config_dir).save(
            CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        review = self.preview()
        self.assertFalse(any(entry.selectable and entry.source_ref.endswith('.rpy')
                             for entry in review.preview.entries))
        self.controller.cancel_directory_open(review)
        self.assertFalse(self.window.create_workspace_from_selected_files(
            self.root, tuple(self.root / ref for ref in self.refs[:2]), self.destination,
            name='Disabled', source_locale='en', target_locale='zh-CN'))
        self.assertIn('PROVIDER_DISABLED', self.errors[-1][1])
        self.assertIs(self.controller.project, old)


if __name__ == '__main__':
    unittest.main()
