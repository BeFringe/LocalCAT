"""Offline manifest navigation; grouping never changes document authority/order."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from editor_contracts import SearchField, SearchOptions, WorkspaceSearchRequest
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from project_workspace_identity import normalize_portable_ref_v1


def document_actions(menu):
    return tuple(action for item in menu.actions()
                 for action in (document_actions(item.menu()) if item.menu() else (item,))
                 if action.data() is not None)


class QtDirectoryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'sources'
        self.refs = ('b/extra/ending.txt', 'a/same.txt', 'b/same.txt')
        for index, ref in enumerate(self.refs):
            path = self.root / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'needle {index}\n', encoding='utf-8')
        self.controller, self.composition = _compose_editor_controller(
            ResourceRepository(self.base / 'data'))
        self.window = QtEditorWindow(self.controller)
        self.window._confirm_unsaved = lambda: True
        self.errors = []
        self.window._show_error = lambda *args: self.errors.append(args)

    def tearDown(self):
        self.controller.close_project()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def package(self, refs):
        job = self.controller.begin_directory_open(self.root)
        job.run()
        preview = self.controller.finish_file_job(job).result
        entries = {entry.source_ref: entry.entry_id for entry in preview.preview.entries}
        destination = self.base / 'navigation.localcat-project'
        job = self.controller.begin_directory_publish(
            preview, tuple(entries[ref] for ref in refs), destination,
            name='Navigation', source_locale='en', target_locale='zh-CN')
        job.run()
        result = self.controller.finish_file_job(job)
        self.assertTrue(result.accepted, result)
        self.controller.close_project()
        self.root.rename(self.base / 'offline')
        self.assertTrue(self.window.open_project_package_path(destination), self.errors)
        return destination

    def test_offline_interleaved_tree_keeps_manifest_reading_search_and_exact_identities(self):
        self.package(self.refs)
        original = self.controller.workspace_view
        tree = self.controller.workspace_document_tree
        self.assertIsInstance(tree, tuple)
        self.assertEqual(tuple(group.name for group in tree), ('b', 'a'))
        self.assertEqual(tuple(item.name if hasattr(item, 'children') else item.source_ref
                               for item in tree[0].children), ('extra', 'b/same.txt'))
        self.assertIs(tree[0].children[0].children[0], original.documents[0])
        self.assertIs(tree[1].children[0].identity, original.documents[1].identity)
        with self.assertRaises(FrozenInstanceError):
            tree[0].name = 'changed'
        menu = self.window.workspace_documents_menu
        self.assertEqual(tuple(action.text() for action in menu.actions()), ('b', 'a'))
        leaves = document_actions(menu)
        self.assertEqual(tuple(action.toolTip().split(' · ')[0] for action in leaves),
                         (self.refs[0], self.refs[2], self.refs[1]))
        self.assertTrue(all(not action.icon().isNull() for action in leaves))
        self.assertIs(leaves[0].data(), original.documents[0].identity)
        self.assertIs(leaves[2].data(), original.documents[1].identity)
        self.assertEqual(tuple(doc.source_ref for doc in self.controller.workspace_view.documents), self.refs)
        self.assertEqual(tuple(segment.project_global_index for segment in original.segments), (0, 1, 2))
        self.controller.move_workspace(1)
        self.assertEqual(self.controller.current_workspace_document_id, original.documents[1].identity.document_id)
        self.controller.move_workspace(1)
        self.assertEqual(self.controller.current_workspace_document_id, original.documents[2].identity.document_id)
        self.composition.matcher_validation_owner.validate_basic(
            generated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            valid_until_utc=datetime(2030, 1, 2, tzinfo=timezone.utc),
            evaluated_at_utc=datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
        report = self.controller.search_workspace(WorkspaceSearchRequest(
            query='needle', fields=(SearchField.SOURCE,), options=SearchOptions(False, False)))
        self.assertEqual(tuple(hit.document_id for hit in report.hits),
                         tuple(doc.identity.document_id for doc in original.documents))
        self.assertFalse(self.controller.workspace_save_state.project_dirty)

    def test_keyboard_leaf_current_dirty_and_chunk_pruning(self):
        self.package(self.refs)
        menu = self.window.workspace_documents_menu
        before = self.controller.current_workspace_identity
        menu.actions()[0].trigger()
        self.assertIs(self.controller.current_workspace_identity, before)
        self.assertEqual(self.errors, [])
        # Right opens the directory; Return on the nested file performs normal navigation.
        self.window.show()
        menu.popup(self.window.mapToGlobal(self.window.rect().center()))
        directory = menu.actions()[1]
        menu.setActiveAction(directory)
        QTest.keyClick(menu, Qt.Key.Key_Right)
        submenu = directory.menu()
        submenu.setActiveAction(submenu.actions()[0])
        QTest.keyClick(submenu, Qt.Key.Key_Return)
        self.assertEqual(self.controller.workspace_global_index, 1)
        self.window.target_editor.setPlainText('edited target')
        self.window._refresh_workspace_documents_menu()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(len(menu.findChildren(QMenu)), 3)
        leaf = next(action for action in document_actions(menu) if action.isChecked())
        self.assertIn('未保存', leaf.text())
        self.assertIn('✓', leaf.text())
        self.assertIn('a/same.txt', leaf.toolTip())
        dirty_before = self.controller.workspace_save_state
        selected = self.controller.workspace_view.documents[2]
        with mock.patch.object(self.window, '_current_chunk_identity_keys',
                               return_value={(selected.identity.document_id, 'unused')}):
            self.window._refresh_workspace_documents_menu()
        self.assertEqual(tuple(action.text() for action in menu.actions()), ('b',))
        self.assertEqual(len(menu.actions()[0].menu().actions()), 1)
        only = document_actions(menu)
        self.assertEqual(len(only), 1)
        self.assertIs(only[0].data(), selected.identity)
        self.assertEqual(self.controller.workspace_save_state, dirty_before)
        with mock.patch.object(self.window, '_current_chunk_identity_keys', return_value=set()):
            self.window._refresh_workspace_documents_menu()
        self.assertFalse(menu.actions())

    def test_single_deep_document_keeps_full_path_and_literal_ampersands(self):
        ref = 'a&b/deep&more/same&name.txt'
        path = self.root / ref
        path.parent.mkdir(parents=True)
        path.write_text('single\n', encoding='utf-8')
        self.package((ref,))
        menu = self.window.workspace_documents_menu
        first = menu.actions()[0]
        self.assertEqual(first.text(), 'a&&b')
        second = first.menu().actions()[0]
        self.assertEqual(second.text(), 'deep&&more')
        leaf = second.menu().actions()[0]
        self.assertEqual(leaf.text(), 'same&&name.txt    ✓')
        self.assertTrue(leaf.isChecked())
        self.assertIn(ref, leaf.toolTip())
        self.assertIs(leaf.data(), self.controller.workspace_view.documents[0].identity)
        self.assertTrue(self.window.chapter_progress_label.isHidden())
        self.assertTrue(self.window.workspace_browse_chapter_title.isHidden())

    def test_maximum_portable_ref_depth_builds_and_prunes_without_recursion(self):
        self.package((self.refs[0],))
        # Existing packages permit 1024-byte refs independently of discovery's
        # scan-depth limit. Project these legal saved facts without OS mkdirs.
        ref = 'a/' * 511 + 'ff'
        self.assertEqual(len(ref.encode('utf-8')), 1024)
        self.assertEqual(normalize_portable_ref_v1(ref), ref)
        original = self.controller.workspace_view
        document = replace(original.documents[0], source_ref=ref)
        view = replace(original, documents=(document,))
        with mock.patch.object(type(self.controller), 'workspace_view',
                               new_callable=mock.PropertyMock, return_value=view):
            nodes = self.controller.workspace_document_tree
            for _ in range(511):
                self.assertEqual(len(nodes), 1)
                self.assertEqual(nodes[0].name, 'a')
                nodes = nodes[0].children
            self.assertEqual(len(nodes), 1)
            self.assertIs(nodes[0], document)
            self.window._refresh_workspace_documents_menu()
            menu = self.window.workspace_documents_menu
            for _ in range(511):
                self.assertEqual(len(menu.actions()), 1)
                menu = menu.actions()[0].menu()
                self.assertIsNotNone(menu)
            self.assertEqual(len(menu.actions()), 1)
            self.assertIs(menu.actions()[0].data(), document.identity)
            self.assertIn(ref, menu.actions()[0].toolTip())
            with mock.patch.object(self.window, '_current_chunk_identity_keys', return_value=set()):
                self.window._refresh_workspace_documents_menu()
            self.assertFalse(self.window.workspace_documents_menu.actions())
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(self.window.workspace_documents_menu.findChildren(QMenu))


if __name__ == '__main__':
    unittest.main()
