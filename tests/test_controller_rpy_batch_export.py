"""Controller-issued multi-document export and the two confirmation stages."""
from dataclasses import replace
import unittest
from unittest import mock

from editor_controller import EditorControllerError, compose_project_enabled_editor_controller
from resource_repository import ResourceRepository
from tests import test_rpy_batch_export as fixtures


class ControllerRpyBatchExportTests(unittest.TestCase):
    def setUp(self):
        fixtures.RpyBatchExportTests.setUp(self)
        self.controller = compose_project_enabled_editor_controller(ResourceRepository(self.base / 'desktop-data'))
        self.addCleanup(self.controller.close_project)
        self.addCleanup(self.controller.abandon_file_jobs)
        self.controller.open_project_package(self.package_path)

    def preview(self, selection=None):
        job = self.controller.begin_tl_export_preview(self.target, selection=selection)
        job.run()
        result = self.controller.finish_tl_export_job(job)
        self.assertTrue(result.accepted)
        return result.result

    def finish(self, job):
        job.run()
        return self.controller.finish_tl_export_job(job)

    def test_multi_available_issued_subset_and_full_relative_paths(self):
        self.assertIsNone(self.controller.tl_export_unavailable_reason)
        self.assertTrue(self.controller.tl_export_uses_directory)
        documents = self.controller.tl_export_documents
        view = self.preview((documents[2].identity, documents[0].identity))
        self.assertEqual(tuple(item.source_ref for item in view.files), (self.refs[0], self.refs[2]))
        for selection in ((), (replace(documents[0].identity),), (documents[0].identity,) * 2):
            with self.assertRaises((EditorControllerError, TypeError, ValueError)):
                self.controller.begin_tl_export_preview(self.target, selection=selection)

    def test_directory_preparation_requires_a_new_issued_preview(self):
        view = self.preview()
        with self.assertRaisesRegex(EditorControllerError, 'DIRECTORIES_REQUIRED'):
            self.controller.begin_tl_export_publish(view, self.target)
        job = self.controller.begin_tl_export_directory_preparation(view, self.target)
        with self.assertRaisesRegex(EditorControllerError, 'EXPORT_IN_PROGRESS'):
            self.controller.update_workspace_target('during prep')
        result = self.finish(job)
        self.assertTrue(result.accepted)
        self.assertEqual(result.result.outcome, 'prepared')
        self.assertEqual(list(self.target.rglob('*.rpy')), [])
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.controller.begin_tl_export_publish(view, self.target)
        fresh = self.preview()
        self.assertNotEqual(view.preview_id, fresh.preview_id)
        self.assertEqual(fresh.missing_directories, ())
        result = self.finish(self.controller.begin_tl_export_publish(fresh, self.target))
        self.assertEqual([item.outcome for item in result.result.files], ['published'] * 3)
        for index, ref in enumerate(self.refs):
            self.assertEqual((self.target / ref).read_bytes(), fixtures.tl(index))

    def test_batch_diagnostic_keeps_document_and_segment_identity(self):
        doc = self.controller.workspace_view.documents[2]
        self.controller.select_workspace_document(doc.identity)
        self.controller.update_workspace_target('[new_expression()]')
        view = self.preview()
        self.assertEqual(view.status, 'blocked')
        issue = view.diagnostics[0]
        self.assertEqual(issue.source_ref, self.refs[2])
        self.assertEqual(self.controller.tl_export_diagnostic_segment_number(view, issue), 3)

    def test_mixed_workspace_lists_only_tl_and_single_selection_keeps_path(self):
        from project_workspace_intake import SelectedProjectDocumentsRequest
        note = self.input / 'notes.txt'
        note.write_text('Plain text document', encoding='utf-8')
        mixed = self.base / 'mixed.localcat-project'
        with self.adapter.prepare_selected(self.input,
                (self.input / self.refs[0], note, self.input / self.refs[1]),
                SelectedProjectDocumentsRequest('Mixed', 'en', 'zh'), session_id='mixed') as session:
            self.assertTrue(session.save(mixed).receipt.durable)
        self.controller.close_project()
        self.controller.open_project_package(mixed)
        documents = self.controller.tl_export_documents
        self.assertEqual(tuple(item.source_ref for item in documents), self.refs[:2])
        self.assertIsNone(self.controller.tl_export_unavailable_reason)
        view = self.preview((documents[1].identity,))
        self.assertEqual(tuple(item.source_ref for item in view.files), (self.refs[1],))
        self.finish(self.controller.begin_tl_export_directory_preparation(view, self.target))
        view = self.preview((documents[1].identity,))
        outcome = self.finish(self.controller.begin_tl_export_publish(view, self.target))
        self.assertEqual(outcome.result.files[0].outcome, 'published')
        self.assertEqual((self.target / self.refs[1]).read_bytes(), fixtures.tl(1))
        self.assertFalse((self.target / 'notes.txt').exists())

    def test_identical_multitl_releases_templates_before_in_place_export(self):
        self.controller.update_workspace_target('覆盖后仍能保存')
        job = self.controller.begin_source_update_preview(self.input, self.refs)
        job.run()
        view = self.controller.finish_source_update_job(job).result
        self.assertTrue(view.templates_identical)
        job = self.controller.begin_source_update_apply(view, ())
        job.run()
        self.assertTrue(self.controller.finish_source_update_job(job).accepted)
        self.assertIsNone(self.controller.tl_export_unavailable_reason)
        job = self.controller.begin_tl_export_preview(self.input)
        view = self.finish(job).result
        self.assertEqual(view.status, 'ready')
        result = self.finish(self.controller.begin_tl_export_publish(view, self.input)).result
        self.assertEqual([item.outcome for item in result.files], ['published'] * 3)
        self.assertTrue(self.controller.save_workspace_package().receipt.durable)
        self.controller.close_project()
        self.controller.open_project_package(self.package_path)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '覆盖后仍能保存')

    def test_runtime_failure_before_owner_call_has_no_uncertain_file(self):
        for directories in (True, False):
            with self.subTest(directories=directories):
                if not directories:
                    for ref in self.refs:
                        (self.target / ref).parent.mkdir(parents=True, exist_ok=True)
                view = self.preview()
                method = (self.controller.begin_tl_export_directory_preparation if directories
                          else self.controller.begin_tl_export_publish)
                job = method(view, self.target)
                with mock.patch('editor_controller.compose_project_codec_runtime', side_effect=OSError('runtime fault')):
                    result = self.finish(job).result
                self.assertEqual(result.outcome, 'failed')
                if not directories:
                    self.assertEqual([item.outcome for item in result.files], ['not_attempted'] * 3)
                self.assertEqual(list(self.target.rglob('*.rpy')), [])


if __name__ == '__main__':
    unittest.main()
