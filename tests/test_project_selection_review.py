"""File-picker classification and review over real ProjectPackages."""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication
from editor_controller import compose_project_enabled_editor_controller
from resource_repository import ResourceRepository
from project_codec_settings import CodecSettingsRepository, CodecSettings, CodecProviderSetting
from parser_composition import ParserApplicationSurface


class ProjectSelectionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.controller = compose_project_enabled_editor_controller(ResourceRepository(self.root / 'data'))
        self.addCleanup(self.controller.close_project)

    def file(self, name, body=b'Hello\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_classifies_without_reading_bodies_and_keeps_original_root_and_order(self):
        first = self.file('a/10.txt')
        skipped = self.file('b/script.rpyc', b'not a parseable body')
        last = self.file('a/2.txt')
        with mock.patch.object(ParserApplicationSurface, 'open_input', side_effect=AssertionError('preview body read')):
            review = self.controller.review_selected_project_files((first, skipped, last))
        self.assertEqual(review.root, self.root)
        self.assertEqual(review.selected_paths, (first, last))
        self.assertEqual(tuple(item.path for item in review.skipped), (skipped,))
        self.assertIn('不支持', review.skipped[0].reason)
        self.assertEqual(review.blocking_reasons, ())

    def test_zero_supported_and_project_package_mix_are_distinct(self):
        unknown = self.file('a.rpyc')
        other = self.file('b.bin')
        review = self.controller.review_selected_project_files((unknown, other))
        self.assertEqual(review.selected_paths, ())
        self.assertEqual(len(review.skipped), 2)
        for extension in ('.zip', '.localcat-project'):
            with self.subTest(extension=extension):
                package = self.file('saved' + extension)
                blocked = self.controller.review_selected_project_files((unknown, package))
                self.assertTrue(any('单独打开' in reason for reason in blocked.blocking_reasons))

    def test_known_disabled_rpy_is_blocked_and_not_skipped(self):
        rpy = self.file('chapter.rpy')
        ignored = self.file('chapter.rpyc')
        CodecSettingsRepository(self.root / 'data').save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        review = self.controller.review_selected_project_files((rpy, ignored))
        self.assertTrue(any('PROVIDER_DISABLED' in reason for reason in review.blocking_reasons))
        self.assertNotIn(rpy, tuple(item.path for item in review.skipped))

    def test_invalid_input_is_not_classified_as_unsupported(self):
        missing = self.root / 'missing.rpyc'
        supported = self.file('valid.txt')
        review = self.controller.review_selected_project_files((missing, supported))
        self.assertTrue(review.blocking_reasons)
        self.assertNotIn(missing, tuple(item.path for item in review.skipped))

    def test_missing_incompatible_provider_invalid_settings_and_ambiguous_are_blocked(self):
        rpy = self.file('chapter.rpy')
        supported = self.file('other.txt')
        for provider, code in ((None, 'PROVIDER_MISSING'), (object(), 'PROVIDER_INCOMPATIBLE')):
            with self.subTest(code=code), mock.patch('project_codec_settings._bundled_rpy_provider', return_value=provider):
                review = self.controller.review_selected_project_files((supported, rpy))
                self.assertTrue(any(code in reason for reason in review.blocking_reasons))
                self.assertEqual(review.skipped, ())
        config = self.root / 'data' / 'project-codecs.json'
        config.parent.mkdir(exist_ok=True)
        config.write_text('{invalid', encoding='utf-8')
        review = self.controller.review_selected_project_files((supported, rpy))
        self.assertTrue(any('PROVIDER_CONFIGURATION_INVALID' in reason for reason in review.blocking_reasons))
        self.assertEqual(review.skipped, ())
        config.unlink()
        from parser_contracts import SelectionFailure, EffectivePurpose
        failure = SelectionFailure('PARSER.SELECTION.AMBIGUOUS', EffectivePurpose.PROJECT_DOCUMENT,
                                   None, None, (), 0, False)
        with mock.patch.object(ParserApplicationSurface, 'select', return_value=failure):
            review = self.controller.review_selected_project_files((supported, rpy))
        self.assertEqual(review.skipped, ())
        self.assertEqual(len(review.blocking_reasons), 2)
        self.assertTrue(all('AMBIGUOUS' in reason for reason in review.blocking_reasons))

    def test_tmx_is_not_a_project_document_and_strict_intake_stays_strict(self):
        from project_workspace_intake import stage_selected_project_documents, SelectedProjectDocumentsRequest
        from project_workspace_contracts import ProjectWorkspaceError
        supported, tmx = self.file('valid.txt'), self.file('memory.tmx')
        review = self.controller.review_selected_project_files((supported, tmx))
        self.assertEqual(review.selected_paths, (supported,))
        self.assertEqual(tuple(item.path for item in review.skipped), (tmx,))
        with self.assertRaises(ProjectWorkspaceError):
            stage_selected_project_documents(self.root, (supported, tmx),
                                             SelectedProjectDocumentsRequest('Strict', 'en', 'zh-CN'))


from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QLabel, QListWidget, QToolButton
from project_package import ProjectPackageService
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow, QtWorkspaceCreationDialog


class QtProjectSelectionReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.controller, _ = _compose_editor_controller(ResourceRepository(self.root / 'data'))
        self.window = QtEditorWindow(self.controller)
        self.window._confirm_unsaved = lambda: True
        self.errors = []
        self.window._show_error = lambda *args: self.errors.append(args)
        self.destination = self.root / 'result.localcat-project'
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.cancel_file_operation()
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def file(self, name, body=b'Hello\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def choose(self, paths, review=None):
        review = review or (lambda dialog: QDialog.DialogCode.Accepted)
        with mock.patch.object(QtWorkspaceCreationDialog, 'exec', review), \
             mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.destination), '')):
            return self.window._review_and_create_workspace_from_selected_paths(paths)

    def test_filtered_one_and_many_save_real_packages_with_original_root_and_identity(self):
        for count in (1, 2):
            with self.subTest(count=count):
                selected = (self.file(f'source{count}/a/10.txt'), self.file(f'source{count}/a/2.txt'))[:count]
                ignored = self.file(f'source{count}/b/chapter.rpyc', b'not text')
                original = selected[:1] + (ignored,) + selected[1:]
                source_bytes = tuple(path.read_bytes() for path in original)
                self.destination = self.root / f'result{count}.localcat-project'
                def accept(dialog):
                    self.assertEqual(dialog.ordered_paths, selected)
                    hint = dialog.findChild(QLabel, 'workspaceCreationHint')
                    self.assertIn(f'将导入 {count} 个，跳过 1 个', hint.text())
                    details = dialog.findChild(QListWidget, 'workspaceSkippedFiles')
                    toggle = dialog.findChild(QToolButton, 'workspaceSkippedDetailsToggle')
                    self.assertTrue(details.isHidden())
                    toggle.click()
                    self.assertFalse(details.isHidden())
                    self.assertIn('rpyc', details.item(0).text())
                    self.assertNotIn('已验证', hint.text())
                    return QDialog.DialogCode.Accepted
                opened_sources = []
                original_open = ParserApplicationSurface.open_input
                def record(surface, reference, *args, **kwargs):
                    opened_sources.append(reference.selected_path)
                    return original_open(surface, reference, *args, **kwargs)
                with mock.patch.object(ParserApplicationSurface, 'open_input', record):
                    self.assertTrue(self.choose(original, accept), self.errors)
                self.assertNotIn(str(ignored), opened_sources)
                active = self.controller.workspace_view
                self.controller.close_project()
                (self.root / f'source{count}').rename(self.root / f'moved{count}')
                cold = ProjectPackageService().open(self.destination).workspace
                self.assertEqual(tuple(item.source_ref for item in cold.documents),
                                 tuple('a/' + path.name for path in selected))
                self.assertEqual(cold.origin.profile_version,
                                 'explicit-single-file-v1' if count == 1 else 'explicit-selected-files-v1')
                self.assertEqual(cold.project_id, active.project.project_id)
                self.assertEqual(tuple(item.document_id for item in cold.documents),
                                 tuple(item.identity.document_id for item in active.documents))
                self.assertEqual(tuple((self.root / f'moved{count}' / path.relative_to(self.root / f'source{count}')).read_bytes()
                                       for path in original), source_bytes)

    def test_rpy_and_rpyc_one_retained_uses_real_tl_package_route(self):
        rpy = self.file('source/a/intro.rpy', b'translate chinese start_abc:\n    # "Hello"\n    "Hi"\n')
        skipped = self.file('source/b/intro.rpyc')
        self.assertTrue(self.choose((rpy, skipped)), self.errors)
        active = self.controller.workspace_view
        cold = ProjectPackageService().open(self.destination).workspace
        self.assertEqual(cold.origin.profile_version, 'explicit-single-file-v1')
        self.assertEqual(cold.documents[0].source_ref, 'a/intro.rpy')
        self.assertEqual(cold.project_id, active.project.project_id)
        self.assertEqual(cold.documents[0].document_id, active.documents[0].identity.document_id)

    def test_zero_supported_and_package_mix_disable_confirmation(self):
        unsupported = (self.file('first.rpyc'), self.file('second.bin'))
        for paths in (unsupported, (unsupported[0], self.file('project.zip'))):
            def inspect(dialog):
                self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
                dialog._accept_if_valid()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                return QDialog.DialogCode.Accepted  # Even a mistaken caller cannot publish.
            with mock.patch.object(QFileDialog, 'getSaveFileName') as destination:
                with mock.patch.object(QtWorkspaceCreationDialog, 'exec', inspect):
                    self.assertFalse(self.window._review_and_create_workspace_from_selected_paths(paths))
                destination.assert_not_called()
        self.assertFalse(self.controller.has_workspace)
        self.assertFalse(self.destination.exists())

    def test_cancel_and_invalid_retained_bodies_preserve_current_dirty_project(self):
        first, second = self.file('old/first.txt'), self.file('old/second.txt')
        old = self.root / 'old.localcat-project'
        self.controller.create_workspace_package(self.root / 'old', (first, second), old,
                                                 name='Old', source_locale='en', target_locale='zh-CN')
        self.controller.update_workspace_target('unsaved')
        old_id = self.controller.workspace_view.project.project_id
        paths = (self.file('new/bad.json', b'{invalid'), self.file('new/unused.rpyc'))
        self.assertFalse(self.choose(paths, lambda dialog: QDialog.DialogCode.Rejected))
        self.assertFalse(self.choose(paths))
        self.assertTrue(self.errors)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.controller.workspace_view.project.project_id, old_id)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertEqual(self.controller.current_segment.target, 'unsaved')

    def test_retained_file_disappearing_during_review_blocks_publication(self):
        selected = self.file('source/valid.txt')
        ignored = self.file('source/ignored.rpyc')
        def accept(dialog):
            selected.unlink()
            return QDialog.DialogCode.Accepted
        self.assertFalse(self.choose((selected, ignored), accept))
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.errors)

    def test_review_does_not_publish_after_window_close(self):
        paths = (self.file('first.txt'), self.file('second.rpyc'))
        def close(dialog):
            self.window.close()
            return QDialog.DialogCode.Accepted
        self.assertFalse(self.choose(paths, close))
        self.assertFalse(self.destination.exists())

    def test_original_single_file_or_package_keeps_open_route(self):
        for suffix in ('.txt', '.json', '.rpy', '.localcat-project', '.zip'):
            path = self.file('single' + suffix)
            with self.subTest(suffix=suffix), \
                 mock.patch.object(self.window, 'open_project_path', return_value=True) as opened, \
                 mock.patch.object(self.controller, 'review_selected_project_files', side_effect=AssertionError('single filtered')):
                self.assertTrue(self.window._open_or_review_home_selection((path,)))
                opened.assert_called_once_with(path)
