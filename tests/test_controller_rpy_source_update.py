"""Source-template review keeps the Project owner and exact candidate together."""
from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest

from editor_controller import EditorControllerError, compose_project_enabled_editor_controller
from resource_repository import ResourceRepository
from project_codec_settings import CodecSettingsRepository, CodecSettings, CodecProviderSetting


def template(source='Old source', label='intro_a', old='Open'):
    return (f'translate chinese {label}:\n    # guide "{source}"\n'
            f'    guide "原译文"\n\ntranslate chinese strings:\n'
            f'    old "{old}"\n    new "打开"\n')


class ControllerRpySourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'intro.rpy'
        self.source.write_text(template(), encoding='utf-8')
        self.package = self.root / 'project.localcat-project'
        self.controller = compose_project_enabled_editor_controller(ResourceRepository(self.root / 'data'))
        self.addCleanup(self.controller.abandon_file_jobs)
        self.addCleanup(self.controller.close_project)
        job = self.controller.begin_file_open(self.source)
        job.run()
        self.controller.finish_file_job(job)
        job = self.controller.begin_file_save(self.package)
        job.run()
        self.assertTrue(self.controller.finish_file_job(job).result.receipt.durable)
        self.controller.update_workspace_target('保留的译文')
        self.controller.confirm_current()
        self.controller.save_workspace_package()
        self.controller.close_project()
        self.controller.open_project_package(self.package)
        self.controller.go_to_workspace_segment(self.controller.workspace_view.segments[0].identity)

    def preview(self, refs=('intro.rpy',)):
        job = self.controller.begin_source_update_preview(self.root, refs)
        job.run()
        outcome = self.controller.finish_source_update_job(job)
        self.assertTrue(outcome.accepted)
        return outcome.result

    def apply(self, view, decisions=()):
        job = self.controller.begin_source_update_apply(view, decisions)
        job.run()
        return self.controller.finish_source_update_job(job)

    def mutate_source_or_verify_native_pin(self, view, text, job=None):
        before = self.source.read_bytes()
        workspace = self.controller.workspace_view
        try:
            self.source.write_text(text, encoding='utf-8')
        except PermissionError:
            if os.name != 'nt':
                raise
            self.assertEqual(self.source.read_bytes(), before)
            self.assertEqual(self.controller.workspace_view, workspace)
            if job is not None:
                job.dispose()
            self.controller.cancel_source_update_preview(view)
            if job is not None:
                self.assertTrue(job.disposed)
                with self.assertRaisesRegex(EditorControllerError, 'PROJECT.FILE.NOT_ISSUED'):
                    self.controller.finish_source_update_job(job)
            self.assertFalse(self.controller.source_update_preview_current(view))
            self.source.write_text(text, encoding='utf-8')
            self.assertEqual(self.source.read_text(encoding='utf-8'), text)
            self.assertEqual(self.controller.workspace_view, workspace)
            return False
        return True

    def test_changed_source_save_cold_reopen_and_export(self):
        self.source.write_text(template('New source'), encoding='utf-8')
        view = self.preview()
        self.assertEqual(view.preview.source_changed_count, 1)
        self.assertEqual(view.preview.unchanged_count, 1)
        self.assertTrue(self.apply(view).accepted)
        segment = self.controller.workspace_view.segments[0]
        self.assertEqual((segment.source, segment.target, segment.confirmed),
                         ('New source', '保留的译文', False))
        self.assertTrue(self.controller.active_project_dirty)
        job = self.controller.begin_file_save()
        job.run()
        self.assertTrue(self.controller.finish_file_job(job).result.receipt.durable)
        self.source.unlink()
        self.controller.close_project()
        self.controller.open_project_package(self.package)
        target = self.root / 'export.rpy'
        job = self.controller.begin_tl_export_preview(target)
        job.run()
        export = self.controller.finish_tl_export_job(job).result
        self.assertEqual(export.status, 'ready')
        job = self.controller.begin_tl_export_publish(export, target)
        job.run()
        self.assertEqual(self.controller.finish_tl_export_job(job).result.outcome, 'published')
        self.assertIn('# guide "New source"', target.read_text(encoding='utf-8'))
        self.assertIn('guide "保留的译文"', target.read_text(encoding='utf-8'))

    def test_rename_new_removed_require_explicit_decisions(self):
        renamed = self.root / 'renamed.rpy'
        self.source.rename(renamed)
        renamed.write_text(template(label='new_label', old='Close'), encoding='utf-8')
        view = self.preview(('renamed.rpy',))
        self.assertEqual((view.preview.new_count, view.preview.removed_count), (2, 2))
        with self.assertRaises(EditorControllerError):
            self.controller.begin_source_update_apply(view, ())
        self.assertTrue(self.apply(view, ('remove', 'remove')).accepted)
        self.assertEqual(self.controller.workspace_view.documents[0].source_ref, 'renamed.rpy')
        self.assertTrue(all(not s.confirmed for s in self.controller.workspace_view.segments))

    def test_cancel_edit_source_drift_and_forgery_preserve_state(self):
        self.source.write_text(template('New source'), encoding='utf-8')
        view = self.preview()
        with self.assertRaises(EditorControllerError):
            self.controller.begin_source_update_apply(replace(view), ())
        self.controller.cancel_source_update_preview(view)
        self.assertFalse(self.controller.source_update_preview_current(view))
        view = self.preview()
        self.controller.update_workspace_target('之后编辑')
        self.assertFalse(self.controller.source_update_preview_current(view))
        view = self.preview()
        if self.mutate_source_or_verify_native_pin(view, template('Changed again')):
            with self.assertRaises(EditorControllerError):
                self.apply(view)
        self.assertEqual(self.controller.workspace_view.segments[0].source, 'Old source')
        self.assertEqual(self.controller.workspace_view.segments[0].target, '之后编辑')

    def test_new_preview_retires_abandoned_apply_plan(self):
        view = self.preview()
        job = self.controller.begin_source_update_apply(view, ())
        job.run()
        replacement = self.preview()
        self.assertTrue(job.disposed)
        self.assertEqual(len(self.controller._workspace_service._plans), 1)
        self.controller.cancel_source_update_preview(replacement)
        self.assertEqual(len(self.controller._workspace_service._plans), 0)

    def test_late_preview_after_close_is_discarded(self):
        job = self.controller.begin_source_update_preview(self.root, ('intro.rpy',))
        job.run()
        self.controller.close_project()
        self.assertFalse(self.controller.finish_source_update_job(job).accepted)

    def test_worker_completion_does_not_authorize_changed_source_or_codec(self):
        self.source.write_text(template('Preview source'), encoding='utf-8')
        for change in ('source', 'codec'):
            with self.subTest(change=change):
                view = self.preview()
                job = self.controller.begin_source_update_apply(view, ())
                job.run()
                if change == 'source':
                    if not self.mutate_source_or_verify_native_pin(
                            view, template('Late changed source'), job):
                        continue
                else:
                    CodecSettingsRepository(self.root / 'data').save(
                        CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
                with self.assertRaises(EditorControllerError):
                    self.controller.finish_source_update_job(job)
                self.assertEqual(self.controller.workspace_view.segments[0].source, 'Old source')

    def test_close_during_apply_releases_plans_and_jobs(self):
        self.source.write_text(template('Preview source'), encoding='utf-8')
        service = self.controller._workspace_service
        for _ in range(3):
            view = self.preview()
            job = self.controller.begin_source_update_apply(view, ())
            job.run()
            job.dispose()
            self.controller.cancel_source_update_preview(view)
            self.assertEqual(len(service._plans), 0)
            self.assertEqual(len(self.controller._issued_source_update_jobs), 0)
        self.assertEqual(self.controller.workspace_view.segments[0].source, 'Old source')


if __name__ == '__main__':
    unittest.main()
