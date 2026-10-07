"""Desktop export issuance over real Parser and durable ProjectPackage."""
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock

from editor_controller import EditorControllerError, compose_project_enabled_editor_controller
from project_codec_settings import CodecSettingsRepository, CodecSettings, CodecProviderSetting
from resource_repository import ResourceRepository
from rpy_project_adapter import RpyProjectAdapter
from parser_composition import ParserApplicationSurface
from platform_fs_contracts import CandidateFile
from tests.test_parser_roundtrip import _CandidateFile, _PlatformAdapter, _POST_NAMING_FAULTS
from tests.test_rpy_provider import FIXTURES


class ControllerRpyExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.raw = (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes()
        self.source = self.root / 'single.rpy'
        self.source.write_bytes(self.raw)
        self.package = self.root / 'project.localcat-project'
        self.target = self.root / 'chosen.rpy'
        self.controller = compose_project_enabled_editor_controller(ResourceRepository(self.root / 'data'))
        self.addCleanup(self.controller.abandon_file_jobs)
        self.addCleanup(self.controller.close_project)
        opened = self.controller.begin_file_open(self.source)
        opened.run()
        self.assertTrue(self.controller.finish_file_job(opened).accepted)
        saved = self.controller.begin_file_save(self.package)
        saved.run()
        self.assertTrue(self.controller.finish_file_job(saved).result.receipt.durable)
        self.package_bytes = self.package.read_bytes()
        self.controller.close_project()
        self.source.unlink()
        # Ordinary package open intentionally has no RpyProjectSession.
        self.controller.open_project_package(self.package)
        self.assertIsNone(self.controller._rpy_project_session)

    def preview(self, target=None):
        job = self.controller.begin_tl_export_preview(target or self.target)
        job.run()
        outcome = self.controller.finish_tl_export_job(job)
        self.assertTrue(outcome.accepted)
        return outcome.result

    def publish(self, view, target=None):
        job = self.controller.begin_tl_export_publish(view, target or self.target)
        job.run()
        return self.controller.finish_tl_export_job(job)

    def test_cold_package_exports_edited_empty_targets_without_saving_package(self):
        self.assertIsNone(self.controller.tl_export_unavailable_reason)
        self.assertEqual(self.controller.tl_export_default_path, self.root / 'single.rpy')
        self.controller.update_workspace_target('')
        view = self.preview()
        self.assertEqual((view.status, view.modified_count), ('ready', 1))
        self.assertGreater(view.empty_count, 0)
        self.assertEqual(view.unconfirmed_count, 6)
        outcome = self.publish(view)
        self.assertEqual(outcome.result.outcome, 'published')
        self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode(), b''))
        self.assertTrue(self.controller.active_project_dirty)
        self.assertEqual(self.package.read_bytes(), self.package_bytes)

    def test_issued_view_identity_and_selected_target_are_required(self):
        view = self.preview()
        for forged, target in ((replace(view), self.target), (view, self.root / 'other.rpy')):
            with self.assertRaisesRegex(EditorControllerError, 'STALE'):
                self.controller.begin_tl_export_publish(forged, target)
        self.assertFalse(self.target.exists())

    def test_diagnostic_location_and_no_publication_for_invalid_target_text(self):
        self.controller.update_workspace_target('新增 [unsafe_expression]')
        view = self.preview()
        self.assertEqual(view.status, 'blocked')
        issue = view.diagnostics[0]
        self.assertEqual(issue.source_ref, 'single.rpy')
        self.assertIsNotNone(issue.line_number)
        self.assertIsNotNone(issue.local_segment_id)
        self.assertEqual(self.controller.tl_export_diagnostic_segment_number(view, issue), 1)
        self.assertIsNone(self.controller.tl_export_diagnostic_segment_number(replace(view), issue))
        self.assertIsNone(self.controller.tl_export_diagnostic_segment_number(view, replace(issue)))
        self.controller.move_workspace(1)
        self.controller.locate_tl_export_diagnostic(view, issue)
        self.assertEqual(self.controller.current_workspace_identity, self.controller.workspace_view.segments[0].identity)
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.publish(view)
        self.assertFalse(self.target.exists())

    def test_edit_project_and_target_change_invalidate_preview(self):
        view = self.preview()
        self.controller.update_workspace_target('编辑后')
        self.assertFalse(self.controller.tl_export_preview_current(view, self.target))
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.publish(view)
        old = self.preview()
        next_target = self.root / 'next.rpy'
        current = self.preview(next_target)
        self.assertFalse(self.controller.tl_export_preview_current(old, self.target))
        self.assertEqual(self.publish(current, next_target).result.outcome, 'published')
        view = self.preview()
        self.controller.load_sample()
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.publish(view)

    def test_codec_and_target_replacement_are_revalidated_on_worker(self):
        view = self.preview()
        self.target.write_bytes(b'competitor')
        self.assertEqual(self.publish(view).result.outcome, 'stale')
        self.assertEqual(self.target.read_bytes(), b'competitor')
        view = self.preview()
        CodecSettingsRepository(self.controller.repository.config_dir).save(
            CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        outcome = self.publish(view)
        self.assertEqual(outcome.result.outcome, 'stale')
        self.assertIn('禁用', self.controller.tl_export_unavailable_reason)

    def test_prepare_edit_cancel_close_and_superseding_results_are_not_accepted(self):
        for action in ('edit', 'cancel', 'supersede', 'close'):
            with self.subTest(action=action):
                job = self.controller.begin_tl_export_preview(self.target)
                job.run()
                candidate = job._value
                if action == 'edit':
                    self.controller.update_workspace_target('later')
                elif action == 'cancel':
                    job.cancel()
                elif action == 'supersede':
                    newer = self.controller.begin_tl_export_preview(self.root / 'new.rpy')
                    newer.run()
                    self.controller.finish_tl_export_job(newer)
                else:
                    self.controller.close_project()
                outcome = self.controller.finish_tl_export_job(job)
                self.assertFalse(outcome.accepted)
                self.assertTrue(candidate.prepared.closed)
        self.assertFalse(self.target.exists())

    def test_publish_freezes_mutation_and_save_but_preview_does_not(self):
        view = self.preview()
        self.controller.update_workspace_target('edited')
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        for operation in (lambda: self.controller.update_workspace_target('racing'),
                          self.controller.confirm_current, self.controller.begin_file_save,
                          self.controller._workspace_owner_for_chunk_controller):
            with self.assertRaisesRegex(EditorControllerError, 'EXPORT_IN_PROGRESS'):
                operation()
        self.controller.move_workspace(1)
        job.run()
        self.assertEqual(self.controller.finish_tl_export_job(job).result.outcome, 'published')
        self.controller.update_workspace_target('later edit')

    def test_cancel_inside_parser_uses_the_preparation_source_token(self):
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        original = CandidateFile.write_all
        def cancel_after_write(*args, **kwargs):
            result = original(*args, **kwargs)
            job.cancel()
            return result
        with mock.patch.object(CandidateFile, 'write_all', autospec=True, side_effect=cancel_after_write):
            job.run()
        result = self.controller.finish_tl_export_job(job).result
        self.assertEqual(result.outcome, 'failed')
        self.assertIn('PARSER.SOURCE.CANCELLED', [item.code for item in result.diagnostics])
        self.assertFalse(self.target.exists())

    def test_late_issued_publication_reports_true_target_without_new_session_install(self):
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        job.run()
        self.controller.load_sample()
        current = self.controller.project
        outcome = self.controller.finish_tl_export_job(job)
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.result.outcome, 'published')
        self.assertEqual(outcome.result.target_path, str(self.target))
        self.assertIs(self.controller.project, current)
        with self.assertRaisesRegex(EditorControllerError, 'NOT_ISSUED'):
            self.controller.finish_tl_export_job(job)

    def test_prepublication_failure_projects_failed_without_retry(self):
        self.target.write_bytes(b'old')
        view = self.preview()
        with mock.patch.object(_CandidateFile, '_flush_content', side_effect=OSError('fault')):
            result = self.publish(view).result
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(self.target.read_bytes(), b'old')
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.publish(view)

    def test_cancel_before_publish_has_terminal_result_and_releases_owner(self):
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        job.cancel()
        job.run()
        self.assertEqual(self.controller.finish_tl_export_job(job).result.outcome, 'cancelled')
        self.assertFalse(self.target.exists())

    def test_post_naming_fault_keeps_uncertain_actual_output_and_dirty(self):
        from project_codec_settings import compose_project_codec_runtime
        self.controller.update_workspace_target('changed')
        original = compose_project_codec_runtime
        def fault(point):
            if point == _POST_NAMING_FAULTS[0]:
                raise OSError('post naming fault')
        def runtime(config):
            composed = original(config)
            composed.surface._platform_backend_factory = lambda root: _PlatformAdapter(_fault_injector=fault)
            return composed
        with mock.patch('editor_controller.compose_project_codec_runtime', side_effect=runtime):
            view = self.preview()
            result = self.publish(view).result
        self.assertEqual(result.outcome, 'uncertain')
        self.assertIsNone(result.output_sha256)
        self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode(), b'changed'))
        self.assertTrue(self.controller.active_project_dirty)
        self.assertEqual(self.package.read_bytes(), self.package_bytes)

    def test_dispose_active_publish_never_waits_for_owner_lock(self):
        view = self.preview()
        job = self.controller.begin_tl_export_publish(view, self.target)
        entered, release = Event(), Event()
        original = ParserApplicationSurface.write_prepared
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', autospec=True, side_effect=delayed):
            thread = Thread(target=job.run)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.controller.close_project()
                self.controller.abandon_file_jobs()
                self.assertTrue(thread.is_alive())
                self.assertTrue(job.disposed)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(list(self.controller.repository.config_dir.glob('rpy-export-*')))

    def test_save_during_preview_invalidates_binding_before_confirmation(self):
        view = self.preview()
        self.controller.update_workspace_target('saved later')
        job = self.controller.begin_file_save()
        job.run()
        self.controller.finish_file_job(job)
        self.assertFalse(self.controller.tl_export_preview_current(view, self.target))
        with self.assertRaisesRegex(EditorControllerError, 'STALE'):
            self.publish(view)

    def test_unissued_job_cannot_deliver_and_draft_cannot_export(self):
        from editor_file_jobs import ControllerFileJob
        forged = ControllerFileJob('prepare_export', self.target, (), lambda cancellation: None)
        forged.run()
        with self.assertRaisesRegex(EditorControllerError, 'NOT_ISSUED'):
            self.controller.finish_tl_export_job(forged)
        self.source.write_bytes(self.raw)
        opened = self.controller.begin_file_open(self.source)
        opened.run()
        self.controller.finish_file_job(opened)
        self.assertIn('先保存', self.controller.tl_export_unavailable_reason)
        with self.assertRaisesRegex(EditorControllerError, 'UNAVAILABLE'):
            self.preview()
