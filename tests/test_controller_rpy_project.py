"""Single-TL controller jobs use real Parser and Project owners."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from editor_controller import EditorControllerError, compose_project_enabled_editor_controller
from project_codec_settings import CodecSettingsRepository, CodecSettings, CodecProviderSetting
from resource_repository import ResourceRepository
from tests.test_rpy_provider import FIXTURES
from tests.test_project_single_file_profile import _PACKAGE_PORT


class ControllerRpyProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'single.rpy'
        self.source.write_bytes((FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes())
        self.controller = compose_project_enabled_editor_controller(ResourceRepository(self.root / 'data'))
        self.addCleanup(self.controller.close_project)
        self.destination = self.root / 'single.localcat-project'

    def open(self, path=None):
        job = self.controller.begin_file_open(path or self.source)
        job.run()
        outcome = self.controller.finish_file_job(job)
        self.assertTrue(outcome.accepted)
        return job

    def save(self):
        job = self.controller.begin_file_save(self.destination)
        job.run()
        outcome = self.controller.finish_file_job(job)
        self.assertTrue(outcome.accepted)
        self.assertTrue(outcome.result.receipt.durable)
        return outcome

    def incoming_package(self):
        other = compose_project_enabled_editor_controller(ResourceRepository(self.root / 'incoming-data'))
        destination = self.root / 'incoming.localcat-project'
        try:
            opened = other.begin_file_open(self.source)
            opened.run()
            self.assertTrue(other.finish_file_job(opened).accepted)
            other.update_workspace_target('导入包中的译文')
            saved = other.begin_file_save(destination)
            saved.run()
            self.assertTrue(other.finish_file_job(saved).result.receipt.durable)
        finally:
            other.close_project()
        return destination

    def test_draft_edit_confirm_save_source_absent_cold_open(self):
        self.open()
        controller = self.controller
        self.assertEqual(len(controller.workspace_view.segments), 6)
        self.assertEqual(controller.current_segment.speaker, 'guide')
        self.assertTrue(controller.active_project_dirty)
        self.assertIsNone(controller.workspace_save_state.package_path)
        ids = tuple(s.identity.segment_identity for s in controller.workspace_view.segments)
        controller.update_workspace_target('新的译文')
        controller.confirm_current()
        self.save()
        self.assertFalse(controller.active_project_dirty)
        self.source.unlink()
        controller.close_project()
        self.open(self.destination)
        self.assertEqual(tuple(s.identity.segment_identity for s in controller.workspace_view.segments), ids)
        self.assertEqual(controller.workspace_view.segments[0].target, '新的译文')
        self.assertTrue(controller.workspace_view.segments[0].confirmed)

    def test_disabled_codec_package_neutral_save_and_new_source_rejected(self):
        self.open()
        self.save()
        CodecSettingsRepository(self.controller.repository.config_dir).save(
            CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        self.open(self.destination)
        self.assertIn('禁用', self.controller.tl_export_unavailable_reason)
        self.controller.update_workspace_target('仍然可以保存')
        self.save()
        old = self.controller.workspace_view.project
        job = self.controller.begin_file_open(self.source)
        job.run()
        with self.assertRaisesRegex(EditorControllerError, 'PROVIDER_DISABLED'):
            self.controller.finish_file_job(job)
        self.assertIs(self.controller.workspace_view.project, old)

    def test_invalid_input_has_safe_location_and_retains_current(self):
        self.controller.load_sample()
        old = self.controller.project
        self.source.write_text('translate zh bad:\n    python:\n        pass\n')
        job = self.controller.begin_file_open(self.source)
        job.run()
        with self.assertRaisesRegex(EditorControllerError, '行 2'):
            self.controller.finish_file_job(job)
        self.assertIs(self.controller.project, old)

    def test_import_replaces_rpy_owner_then_edits_save_and_cold_reopen(self):
        incoming = self.incoming_package()
        self.open()
        self.save()
        previous = self.controller._rpy_project_session
        preview = self.controller.preview_workspace_package_import(incoming)
        result = self.controller.apply_workspace_package_import(preview)
        self.assertTrue(result.active_session_changed)
        self.assertTrue(result.receipt.durable)
        self.assertTrue(previous.closed)
        self.assertIsNone(self.controller._rpy_project_session)
        self.assertEqual(self.controller.current_segment.target, '导入包中的译文')
        self.controller.update_workspace_target('导入后实际保存的译文')
        self.controller.confirm_current()
        self.save()
        self.controller.close_project()
        self.open(self.destination)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '导入后实际保存的译文')
        self.assertTrue(self.controller.workspace_view.segments[0].confirmed)

    def test_failed_or_independent_import_keeps_original_rpy_session_saveable(self):
        incoming = self.incoming_package()
        self.open()
        self.save()
        previous = self.controller._rpy_project_session
        before_bytes = self.destination.read_bytes()
        preview = self.controller.preview_workspace_package_import(incoming)
        with mock.patch.object(self.controller, '_prepare_workspace_install',
                               side_effect=RuntimeError('candidate-projection-fault')):
            with self.assertRaisesRegex(RuntimeError, 'candidate-projection-fault'):
                self.controller.apply_workspace_package_import(preview)
        self.assertIs(self.controller._rpy_project_session, previous)
        self.assertFalse(previous.closed)
        self.assertEqual(self.destination.read_bytes(), before_bytes)
        independent = self.root / 'independent.localcat-project'
        preview = self.controller.preview_workspace_package_import(incoming, destination=independent)
        result = self.controller.apply_workspace_package_import(preview)
        self.assertFalse(result.active_session_changed)
        self.assertIs(self.controller._rpy_project_session, previous)
        self.assertFalse(previous.closed)
        self.controller.update_workspace_target('仍由原项目保存')
        self.save()
        self.controller.close_project()
        self.open(self.destination)
        self.assertEqual(self.controller.workspace_view.segments[0].target, '仍由原项目保存')

    def test_stale_edit_cancel_and_close_release_candidate(self):
        self.controller.load_sample()
        for mode in ('edit', 'cancel', 'close'):
            job = self.controller.begin_file_open(self.source)
            job.run()
            candidate = job._value
            if mode == 'edit':
                self.controller.update_target('edited')
            elif mode == 'cancel':
                job.cancel()
            else:
                self.controller.close_project()
            outcome = self.controller.finish_file_job(job)
            self.assertFalse(outcome.accepted)
            self.assertTrue(candidate.session.closed)
            if not self.controller.has_active_project:
                self.controller.load_sample()

    def test_save_freezes_mutation_not_navigation_and_late_receipt_is_truthful(self):
        self.open()
        job = self.controller.begin_file_save(self.destination)
        with self.assertRaisesRegex(EditorControllerError, 'SAVE_IN_PROGRESS'):
            self.controller.update_workspace_target('racing')
        with self.assertRaisesRegex(EditorControllerError, 'SAVE_IN_PROGRESS'):
            self.controller.confirm_current()
        with self.assertRaisesRegex(EditorControllerError, 'SAVE_IN_PROGRESS'):
            self.controller._workspace_owner_for_chunk_controller()
        self.controller.move_workspace(1)
        old = job.session
        self.controller.load_sample()
        job.cancel()
        job.run()
        outcome = self.controller.finish_file_job(job)
        self.assertFalse(outcome.accepted)
        # Cancel before the owner starts proves no save, with no fake success.
        self.assertIsNone(outcome.result)
        self.assertTrue(old.closed)
        self.assertFalse(self.destination.exists())

    def test_completed_save_cancel_still_reports_receipt_without_old_install(self):
        self.open()
        job = self.controller.begin_file_save(self.destination)
        job.run()
        self.controller.load_sample()
        old = self.controller.project
        job.cancel()
        outcome = self.controller.finish_file_job(job)
        self.assertFalse(outcome.accepted)
        self.assertTrue(outcome.result.receipt.durable)
        self.assertIs(self.controller.project, old)
        self.assertTrue(job.session.closed)

    def test_one_shot_job_and_stale_reopen(self):
        first = self.controller.begin_file_open(self.source)
        first.run()
        candidate = first._value
        self.open()
        self.assertFalse(self.controller.finish_file_job(first).accepted)
        self.assertTrue(candidate.session.closed)
        with self.assertRaisesRegex(EditorControllerError, 'NOT_ISSUED'):
            self.controller.finish_file_job(first)

    def test_late_recovery_result_does_not_block_new_workspace(self):
        self.open()
        job = self.controller.begin_file_save(self.destination)
        with mock.patch(_PACKAGE_PORT + '.commit_candidate', side_effect=OSError('cleanup failure')):
            job.run()
        self.open()
        outcome = self.controller.finish_file_job(job)
        self.assertFalse(outcome.accepted)
        self.assertTrue(outcome.result.save_report.recovery_required)
        self.assertIsNone(self.controller.workspace_recovery_target)
        self.assertTrue(self.controller.active_project_dirty)

    def test_save_projection_remains_coherent_until_gui_accepts_owner_result(self):
        self.open()
        before = self.controller.workspace_save_state
        job = self.controller.begin_file_save(self.destination)
        with self.assertRaises(AttributeError):
            job.context = ('forged',)
        job.run()
        self.assertTrue(self.destination.exists())
        self.assertEqual(self.controller.workspace_save_state, before)
        self.controller.move_workspace(1)
        self.assertTrue(self.controller.workspace_save_state.project_dirty)
        outcome = self.controller.finish_file_job(job)
        self.assertTrue(outcome.result.receipt.durable)
        self.assertFalse(self.controller.workspace_save_state.project_dirty)

    def test_failed_first_save_preserves_draft_and_recovery_retains_exact_target(self):
        self.open()
        self.controller.update_workspace_target('保留编辑')
        failed = self.controller.begin_file_save(self.destination)
        with mock.patch(_PACKAGE_PORT + '.stage_candidate', side_effect=OSError('failure')):
            failed.run()
        result = self.controller.finish_file_job(failed)
        self.assertIsNone(result.result.receipt)
        self.assertIsNone(self.controller.workspace_save_state.package_path)
        self.assertTrue(self.controller.active_project_dirty)
        self.assertEqual(self.controller.current_segment.target, '保留编辑')
        recovery = self.controller.begin_file_save(self.destination)
        with mock.patch(_PACKAGE_PORT + '.commit_candidate', side_effect=OSError('cleanup failure')):
            recovery.run()
        result = self.controller.finish_file_job(recovery)
        self.assertTrue(result.result.save_report.recovery_required)
        self.assertIsNone(result.result.receipt)
        self.assertEqual(self.controller.workspace_recovery_target, self.destination)
        with self.assertRaisesRegex(EditorControllerError, 'RECOVERY_REQUIRED'):
            self.controller.begin_file_save(self.root / 'wrong-retry.localcat-project')
        self.assertEqual(self.controller.current_segment.target, '保留编辑')
        self.open()
        self.assertIsNone(self.controller.workspace_recovery_target)
        next_save = self.controller.begin_file_save(self.root / 'new-project.localcat-project')
        next_save.run()
        self.assertTrue(self.controller.finish_file_job(next_save).result.receipt.durable)

    def test_legacy_json_txt_still_work(self):
        text = self.root / 'old.txt'
        text.write_text('One\nTwo\n')
        self.controller.open_project(text)
        self.assertEqual(len(self.controller.project.segments), 2)
