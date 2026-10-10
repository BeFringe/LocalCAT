"""Real offline packages and platform targets for batch export coordination."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_composition import ParserApplicationSurface
from parser_source import CancellationToken
from platform_export_directory import ExportDirectoryBatch
from project_codec_settings import compose_project_codec_runtime
from project_package import ProjectPackageService
from project_workspace_intake import SelectedProjectDocumentsRequest
from rpy_project_adapter import RpyProjectAdapter
from rpy_project_export import RpyExportContext
import rpy_project_export as export


def tl(number):
    return (f'translate chinese chapter_{number}:\n'
            f'    # guide "Source {number}"\n'
            f'    guide "目标 {number}"\n').encode('utf-8')


class RpyBatchExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.config = self.base / 'config'
        self.config.mkdir()
        self.input = self.base / 'input'
        self.input.mkdir()
        self.refs = ('b/extra/ending.rpy', 'a/intro.rpy', 'b/intro.rpy')
        sources = tuple(self.input / ref for ref in self.refs)
        for index, path in enumerate(sources):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tl(index))
        self.runtime = compose_project_codec_runtime(self.config)
        self.package = ProjectPackageService()
        self.adapter = RpyProjectAdapter(self.runtime, package_service=self.package)
        with self.adapter.prepare_selected(self.input, sources,
                SelectedProjectDocumentsRequest('多章节', 'en', 'zh'), session_id='intake') as session:
            self.package_path = self.base / 'multi.localcat-project'
            self.assertTrue(session.save(self.package_path).receipt.durable)
        opened = self.package.open(self.package_path)
        self.save = opened.create_save_service(session_id='editor', revision=0)
        self.service = self.save.workspace_service
        self.context = RpyExportContext(self.service, opened.persistence_binding, self.runtime, 9)
        self.ids = tuple(document.document_id for document in self.service.workspace.documents)
        self.target = self.base / 'output'
        self.target.mkdir()
        self.package_bytes = self.package_path.read_bytes()
        self.baseline = self.save.saved_workspace_snapshot

    def prepare(self, ids=None, **kwargs):
        candidate = export.prepare_rpy_batch_export(self.context, self.target,
            document_ids=self.ids if ids is None else ids, config_dir=self.config,
            package_service=self.package, **kwargs)
        self.addCleanup(candidate.close)
        return candidate

    def parents(self):
        for ref in self.refs:
            (self.target / ref).parent.mkdir(parents=True, exist_ok=True)

    def assert_no_files(self):
        self.assertEqual(list(self.target.rglob('*.rpy')), [])

    def test_preview_readonly_all_paths_and_missing_directories(self):
        candidate = self.prepare()
        self.assertEqual(candidate.view.status, 'ready')
        self.assertEqual(tuple(item.source_ref for item in candidate.view.files), self.refs)
        self.assertEqual(set(candidate.view.missing_directories), {'b', 'b/extra', 'a'})
        self.assertEqual([item.target_action for item in candidate.view.files], ['new'] * 3)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(list(self.config.iterdir()), [])
        candidate.close()
        self.assertTrue(candidate.closed)

    def test_missing_directories_require_consumption_then_fresh_preview(self):
        candidate = self.prepare()
        result = candidate.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'blocked')
        self.assertEqual([item.outcome for item in result.files], ['not_attempted'] * 3)
        self.assertEqual(list(self.target.iterdir()), [])
        candidate = self.prepare()
        result = candidate.prepare_directories(self.context, self.target)
        self.assertEqual(result.outcome, 'prepared')
        self.assertEqual(set(result.created_directories), {'a', 'b', 'b/extra'})
        self.assertTrue(candidate.closed)
        self.assert_no_files()
        self.assertEqual(candidate.publish(self.context, self.target).outcome, 'not_attempted')
        fresh = self.prepare()
        self.assertEqual(fresh.view.missing_directories, ())
        result = fresh.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'published')
        self.assertEqual([item.outcome for item in result.files], ['published'] * 3)
        for index, ref in enumerate(self.refs):
            self.assertEqual((self.target / ref).read_bytes(), tl(index))
            self.assertEqual(result.files[index].output_sha256, hashlib.sha256(tl(index)).hexdigest())
        self.assertIs(fresh.publish(self.context, self.target), result)

    def test_existing_original_directory_overwrite_current_edits_keeps_dirty(self):
        self.target = self.input
        self.service.update_segment_edit(self.service.flat_segments[1].identity, target='新增译文',
            confirmed=True, session_id=self.service.session_id, base_revision=self.service.revision)
        candidate = self.prepare()
        self.assertEqual([item.target_action for item in candidate.view.files], ['overwrite'] * 3)
        self.assertEqual(candidate.view.missing_directories, ())
        result = candidate.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'published')
        self.assertEqual((self.target / self.refs[1]).read_bytes(), tl(1).replace('目标 1'.encode(), '新增译文'.encode()))
        self.assertTrue(self.save.project_dirty)
        self.assertEqual(self.save.saved_workspace_snapshot, self.baseline)
        self.assertEqual(self.package_path.read_bytes(), self.package_bytes)

    def test_selected_subset_preserves_workspace_order_and_offline_template(self):
        self.parents()
        for ref in self.refs:
            (self.input / ref).unlink()
        candidate = self.prepare((self.ids[2], self.ids[0]))
        self.assertEqual(tuple(item.source_ref for item in candidate.view.files), (self.refs[0], self.refs[2]))
        self.assertEqual(candidate.publish(self.context, self.target).outcome, 'published')
        self.assertFalse((self.target / self.refs[1]).exists())

    def test_invalid_selection_rejected_without_io(self):
        for ids in ((), (self.ids[0], self.ids[0]), ('foreign',)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.prepare(ids)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_last_invalid_content_blocks_every_file(self):
        self.parents()
        self.service.update_segment_edit(self.service.flat_segments[2].identity, target='[new_call()]',
            confirmed=False, session_id=self.service.session_id, base_revision=self.service.revision)
        candidate = self.prepare()
        self.assertEqual(candidate.view.status, 'blocked')
        self.assertEqual(candidate.view.diagnostics[0].source_ref, self.refs[2])
        self.assertEqual(candidate.publish(self.context, self.target).outcome, 'blocked')
        self.assert_no_files()
        self.assertEqual(list(self.config.iterdir()), [])

    def test_second_dispatch_exception_preserves_published_and_unattempted(self):
        self.parents()
        candidate = self.prepare()
        original = ParserApplicationSurface.write_prepared
        calls = []
        def write(surface, prepared):
            calls.append(prepared)
            if len(calls) == 2:
                raise OSError('private failure')
            return original(surface, prepared)
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', write):
            result = candidate.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'partial')
        self.assertEqual([item.outcome for item in result.files], ['published', 'uncertain', 'not_attempted'])
        self.assertEqual((self.target / self.refs[0]).read_bytes(), tl(0))
        self.assertFalse((self.target / self.refs[2]).exists())
        self.assertNotIn('private failure', repr(result))
        self.assertTrue(all(prepared.closed for prepared in calls))

    def test_second_binding_failure_is_failed_without_erasing_first(self):
        self.parents()
        candidate = self.prepare()
        original = ExportDirectoryBatch.materialize_target
        calls = []
        def materialize(batch, plan, cancellation=None):
            calls.append(plan)
            if len(calls) == 2:
                raise OSError('bind failure')
            return original(batch, plan, cancellation)
        with mock.patch.object(ExportDirectoryBatch, 'materialize_target', materialize):
            result = candidate.publish(self.context, self.target)
        self.assertEqual([item.outcome for item in result.files], ['published', 'failed', 'not_attempted'])
        self.assertEqual(result.outcome, 'partial')

    def test_cancel_after_first_keeps_exact_publication_facts(self):
        self.parents()
        token = CancellationToken()
        candidate = self.prepare(cancellation=token)
        original = ParserApplicationSurface.write_prepared
        def write(surface, prepared):
            result = original(surface, prepared)
            token.cancel()
            return result
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', write):
            result = candidate.publish(self.context, self.target)
        self.assertEqual([item.outcome for item in result.files], ['published', 'not_attempted', 'not_attempted'])
        self.assertEqual(result.outcome, 'partial')
        self.assertTrue(candidate.closed)

    def test_edit_after_preview_blocks_before_first_file(self):
        self.parents()
        candidate = self.prepare()
        self.service.update_segment_edit(self.service.flat_segments[0].identity, target='稍后编辑',
            confirmed=False, session_id=self.service.session_id, base_revision=self.service.revision)
        result = candidate.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'stale')
        self.assertEqual([item.outcome for item in result.files], ['not_attempted'] * 3)
        self.assert_no_files()

    def test_external_absent_target_creation_is_not_adopted(self):
        self.parents()
        candidate = self.prepare()
        (self.target / self.refs[2]).write_bytes(b'external')
        result = candidate.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'stale')
        self.assertFalse((self.target / self.refs[0]).exists())
        self.assertEqual((self.target / self.refs[2]).read_bytes(), b'external')

    def test_directory_failure_reports_created_facts_no_tl(self):
        candidate = self.prepare()
        backend = type(candidate._directory_batch._backend)
        original = backend.open_export_directory
        def create(instance, parent, name, *, create=False):
            if create and name == 'extra':
                raise OSError('denied')
            return original(instance, parent, name, create=create)
        with mock.patch.object(backend, 'open_export_directory', create):
            result = candidate.prepare_directories(self.context, self.target)
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.created_directories, ('b',))
        self.assertEqual(result.directory_failures[0].relative_path, 'b/extra')
        self.assert_no_files()
        self.assertTrue(candidate.closed)

    def test_cleanup_interruption_keeps_dispatched_file_facts(self):
        self.parents()
        candidate = self.prepare()
        original = export._TargetObservation.close
        armed = [True]
        class Interrupted(BaseException):
            pass
        def close(observation):
            original(observation)
            if armed[0]:
                armed[0] = False
                raise Interrupted()
        with mock.patch.object(export._TargetObservation, 'close', close), self.assertRaises(Interrupted):
            candidate.publish(self.context, self.target)
        result = candidate.terminal_result
        self.assertIsNotNone(result)
        self.assertEqual(result.files[0].outcome, 'uncertain')
        self.assertEqual([item.outcome for item in result.files[1:]], ['not_attempted'] * 2)
        self.assertEqual((self.target / self.refs[0]).read_bytes(), tl(0))
        self.assertTrue(candidate.closed)

    def test_validation_interruption_closes_all_candidates(self):
        self.parents()
        candidate = self.prepare()
        prepared = tuple(c._prepared for c in candidate._candidates)
        class Interrupted(BaseException):
            pass
        with mock.patch.object(candidate, '_require_current', side_effect=Interrupted), self.assertRaises(Interrupted):
            candidate.publish(self.context, self.target)
        self.assertTrue(candidate.closed)
        self.assertTrue(all(item.closed for item in prepared))
        self.assertEqual([item.outcome for item in candidate.terminal_result.files], ['not_attempted'] * 3)
        self.assert_no_files()

    def test_directory_cleanup_interruption_retains_creation_facts(self):
        candidate = self.prepare()
        original = candidate._candidates[0].close
        class Interrupted(BaseException):
            pass
        def close():
            original()
            raise Interrupted()
        with mock.patch.object(candidate._candidates[0], 'close', close), self.assertRaises(Interrupted):
            candidate.prepare_directories(self.context, self.target)
        result = candidate.terminal_result
        self.assertIsNotNone(result)
        self.assertEqual(set(result.created_directories), {'a', 'b', 'b/extra'})
        self.assertEqual(result.outcome, 'uncertain')
        self.assertTrue(candidate.closed)
        self.assert_no_files()

    def test_prepare_failure_cleanup_interruption_closes_earlier_candidates(self):
        self.service.update_segment_edit(self.service.flat_segments[2].identity, target='[new_call()]',
            confirmed=False, session_id=self.service.session_id, base_revision=self.service.revision)
        original_prepare = ParserApplicationSurface.prepare_round_trip
        original_close = export.PreparedRpyProjectExport.close
        retained = []
        class Interrupted(BaseException):
            pass
        def prepare(surface, *args, **kwargs):
            result = original_prepare(surface, *args, **kwargs)
            retained.append(result)
            return result
        def close(candidate):
            interrupt = candidate._document.document_id == self.ids[2] and not candidate.closed
            original_close(candidate)
            if interrupt:
                raise Interrupted()
        with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', prepare), \
                mock.patch.object(export.PreparedRpyProjectExport, 'close', close), self.assertRaises(Interrupted):
            self.prepare()
        self.assertEqual(len(retained), 2)
        self.assertTrue(all(item.closed for item in retained))
        self.assertEqual(list(self.config.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
