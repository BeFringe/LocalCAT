"""Shared desktop dialog presents paths, overwrites and directory stages."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest import mock
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QFileDialog
from qt_editor_window import QtEditorWindow
from tests import test_controller_rpy_batch_export as fixtures
from tests import test_qt_rpy_export as qt_fixtures


class QtRpyBatchExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        fixtures.ControllerRpyBatchExportTests.setUp(self)
        self.window = QtEditorWindow(self.controller)
        self.window._confirm_unsaved = lambda: True
        self.window.show()
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

    def wait_until(self, predicate, timeout=60):
        return qt_fixtures.QtRpyExportTests.wait_until(self, predicate, timeout)

    def open_export(self):
        self.assertTrue(self.window.tl_export_action.isEnabled())
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.target)) as choose, \
                mock.patch.object(QFileDialog, 'getSaveFileName', return_value=('', '')):
            self.window.tl_export_action.trigger()
        self.assertEqual(choose.call_count, 1)
        dialog = self.window._tl_export_dialog
        self.wait_until(lambda: not dialog.operation_running)
        return dialog

    def test_missing_directories_repreview_but_never_auto_publish(self):
        dialog = self.open_export()
        old = dialog.view
        self.assertIn('准备目录', dialog.confirm_button.text())
        self.assertEqual(dialog.files_table.topLevelItemCount(), 3)
        self.assertEqual(tuple(dialog.files_table.topLevelItem(i).text(0) for i in range(3)), self.refs)
        dialog.confirm_button.click()
        self.wait_until(lambda: not dialog.operation_running and dialog.view is not None and dialog.view is not old)
        self.assertEqual(dialog.view.status, 'ready')
        self.assertEqual(dialog.view.missing_directories, ())
        self.assertEqual(list(self.target.rglob('*.rpy')), [])
        self.assertTrue(dialog.confirm_button.isEnabled())
        self.assertIn('确认导出', dialog.confirm_button.text())
        self.assertIn('已创建', dialog.directory_label.text())
        dialog.confirm_button.click()
        self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'published')
        self.assertTrue(dialog.isVisible())
        self.assertEqual([dialog.files_table.topLevelItem(i).text(1) for i in range(3)], ['已导出'] * 3)

    def test_existing_original_directory_overwrite_visible_and_once(self):
        self.target = self.input
        dialog = self.open_export()
        self.assertEqual([dialog.files_table.topLevelItem(i).text(1) for i in range(3)], ['覆盖'] * 3)
        self.assertIn('覆盖 3', dialog.confirm_button.text())
        self.assertIn('覆盖', dialog.summary_label.text())
        dialog.confirm_button.click()
        self.wait_until(lambda: dialog.result is not None)
        self.assertEqual(dialog.result.outcome, 'published')

    def test_selection_change_invalidates_preview_and_exports_only_checked(self):
        for ref in self.refs:
            (self.target / ref).parent.mkdir(parents=True, exist_ok=True)
        dialog = self.open_export()
        dialog.files_table.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.prepare_target(self.target)
        self.wait_until(lambda: not dialog.operation_running)
        self.assertEqual(tuple(item.source_ref for item in dialog.view.files), (self.refs[0], self.refs[2]))
        dialog.confirm_button.click()
        self.wait_until(lambda: dialog.result is not None)
        self.assertFalse((self.target / self.refs[1]).exists())
        self.assertEqual(dialog.files_table.topLevelItem(1).text(1), '未选择')

    def test_cancel_target_chooser_preserves_overwrite_rows_and_current_preview(self):
        self.target = self.input
        dialog = self.open_export()
        view = dialog.view
        with mock.patch.object(QFileDialog, 'getExistingDirectory', return_value=''):
            dialog.select_target()
        self.assertIs(dialog.view, view)
        self.assertEqual([dialog.files_table.topLevelItem(i).text(1) for i in range(3)], ['覆盖'] * 3)
        self.assertTrue(dialog.confirm_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
