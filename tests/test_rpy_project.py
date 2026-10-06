"""Real RPY provider and ProjectPackage single-document journeys."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_source import CancellationToken
from platform_fs import compose_platform_file_backend
from project_codec_settings import (
    CodecProviderSetting, CodecSettings, CodecSettingsRepository,
    compose_project_codec_runtime,
)
from project_package import ProjectPackageService
from project_save import SaveJournalState
from project_workspace_contracts import ProjectOriginKind, ProjectWorkspaceError
from project_workspace_intake import SelectedProjectDocumentsRequest, SelectedProjectDocumentsError
from rpy_project_adapter import RpyProjectAdapter, RpyProjectUnavailableError
from tests.test_project_single_file_profile import _PACKAGE_PORT
from tests.test_rpy_provider import FIXTURES
from parser_rpy_codec import build_private_payload, RPY_CODEC_IDENTITY


class RpyProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'tl'
        self.source = self.root / 'nested' / 'chapter.rpy'
        self.source.parent.mkdir(parents=True)
        self.raw = (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes()
        self.source.write_bytes(self.raw)
        self.config = self.base / 'config'
        self.runtime = compose_project_codec_runtime(self.config)
        self.package = ProjectPackageService()
        self.adapter = RpyProjectAdapter(self.runtime, package_service=self.package)
        self.destination = self.base / 'single.localcat-project'
        self.request = SelectedProjectDocumentsRequest('Single TL', 'en', 'zh_Hans')

    def prepare(self, **kwargs):
        session = self.adapter.prepare_single(
            self.root, self.source, self.request, session_id='single', **kwargs,
        )
        self.addCleanup(session.close)
        return session

    def open(self, adapter=None):
        session = (adapter or self.adapter).open_package(
            self.destination, session_id='cold', revision=0,
        )
        self.addCleanup(session.close)
        return session

    def edit(self, session, index, target, confirmed):
        service = session.workspace_service
        identity = service.flat_segments[index].identity
        service.update_segment_edit(
            identity, target=target, confirmed=confirmed,
            session_id=service.session_id, base_revision=service.revision,
        )

    def members(self, opened):
        entry = opened.manifest.documents[0]
        with opened.open_member(entry.source_member.path) as stream:
            source = stream.read()
        with opened.open_member(entry.codec_private_member.path,
                                codec_identity=RPY_CODEC_IDENTITY) as stream:
            private = stream.read()
        return source, private

    def test_prepare_is_editable_non_durable_exact_profile_and_no_sidecar(self):
        session = self.prepare()
        workspace = session.workspace_service.workspace
        self.assertIs(workspace.origin.kind, ProjectOriginKind.SINGLE_FILE)
        self.assertEqual(workspace.origin.profile_version, 'explicit-single-file-v1')
        document = workspace.documents[0]
        self.assertEqual(document.source_ref, 'nested/chapter.rpy')
        self.assertEqual(document.codec_identity, RPY_CODEC_IDENTITY)
        self.assertEqual(document.format_id, 'renpy-tl-v1')
        self.assertEqual(len(document.segments), 6)
        self.assertEqual([s.raw_speaker for s in document.segments], ['guide', '', '', '', '', 'guide'])
        self.assertEqual([s.target for s in document.segments], ['灯笼亮着。', '', '开启{#verb}', '桥上很安静。', '关闭', ''])
        self.assertTrue(all(not s.confirmed for s in document.segments))
        self.assertIs(session.save_service.workspace_service, session.workspace_service)
        self.assertIsNone(session.persistence_binding)
        self.assertIsNone(session.save_service.saved_workspace_snapshot)
        self.assertTrue(session.save_service.project_dirty)
        self.assertTrue(session.source_retained)
        self.assertTrue(session.export_availability(self.runtime).available)
        self.assertEqual(list(self.root.rglob('*.*')), [self.source])
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_real_package_source_private_edits_and_source_absent_cold_reopen(self):
        session = self.prepare()
        service = session.workspace_service
        ids = tuple(s.identity for s in service.flat_segments)
        self.edit(session, 0, '新的译文', True)
        self.edit(session, 2, '', True)
        result = session.save(self.destination)
        self.assertIs(result.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertTrue(result.receipt.durable)
        self.assertIs(session.workspace_service, service)
        self.assertEqual(session.persistence_binding, result.persistence_binding)
        self.assertFalse(session.source_retained)
        self.assertFalse(session.save_service.project_dirty)
        opened = session.open_saved_package()
        self.assertEqual(self.members(opened), (self.raw, build_private_payload(self.raw)))
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.root.rename(self.base / 'removed-origin')
        session.close()
        cold = self.open()
        self.assertIsNone(cold.workspace_service.origin_binding)
        self.assertEqual(tuple(s.identity for s in cold.workspace_service.flat_segments), ids)
        segments = cold.workspace_service.workspace.documents[0].segments
        self.assertEqual((segments[0].target, segments[0].confirmed), ('新的译文', True))
        self.assertEqual((segments[2].target, segments[2].confirmed), ('', True))
        self.assertFalse(cold.save_service.project_dirty)
        self.assertTrue(cold.export_availability(self.runtime).available)
        self.edit(cold, 5, '又一个译文', False)
        resaved = cold.save()
        self.assertTrue(resaved.receipt.durable)
        self.assertFalse(cold.save_service.project_dirty)
        self.assertEqual(self.members(cold.open_saved_package()), (self.raw, build_private_payload(self.raw)))

    def test_disabled_and_missing_codec_keep_package_editable_and_private_exact(self):
        session = self.prepare()
        self.assertTrue(session.save(self.destination).receipt.durable)
        session.close()
        self.root.rename(self.base / 'removed-origin')
        for missing in (False, True):
            with self.subTest(missing=missing), ExitStack() as stack:
                if missing:
                    CodecSettingsRepository(self.config).save(CodecSettings())
                    stack.enter_context(mock.patch('project_codec_settings._bundled_rpy_provider', return_value=None))
                else:
                    CodecSettingsRepository(self.config).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
                runtime = compose_project_codec_runtime(self.config)
                adapter = RpyProjectAdapter(runtime, package_service=self.package)
                cold = self.open(adapter)
                availability = cold.export_availability(runtime)
                self.assertFalse(availability.available)
                self.assertEqual(availability.code, 'PARSER.SELECTION.PROVIDER_MISSING' if missing else 'PARSER.SELECTION.PROVIDER_DISABLED')
                self.edit(cold, 1, '缺少 codec 也可编辑' if missing else '禁用时也可编辑', True)
                result = cold.save()
                self.assertTrue(result.receipt.durable)
                self.assertFalse(cold.save_service.project_dirty)
                self.assertEqual(self.members(cold.open_saved_package()), (self.raw, build_private_payload(self.raw)))
                cold.close()
        enabled = compose_project_codec_runtime(self.config)
        self.assertTrue(self.open().export_availability(enabled).available)

    def test_unavailable_provider_rejects_new_source_without_intake(self):
        for missing in (False, True):
            with self.subTest(missing=missing), ExitStack() as stack:
                if missing:
                    CodecSettingsRepository(self.config).save(CodecSettings())
                    stack.enter_context(mock.patch('project_codec_settings._bundled_rpy_provider', return_value=None))
                else:
                    CodecSettingsRepository(self.config).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
                adapter = RpyProjectAdapter(compose_project_codec_runtime(self.config))
                with mock.patch('rpy_project_adapter.prepare_selected_project_documents') as intake:
                    with self.assertRaises(RpyProjectUnavailableError) as caught:
                        adapter.prepare_single(self.root, self.source, self.request, session_id='unavailable')
                    intake.assert_not_called()
                self.assertIn('PROVIDER_', caught.exception.availability.code)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_failed_first_save_retries_but_never_invents_saved_binding(self):
        session = self.prepare()
        self.edit(session, 0, '保留未保存编辑', True)
        with mock.patch(_PACKAGE_PORT + '.stage_candidate', side_effect=OSError('fault')):
            failed = session.save(self.destination)
        self.assertIsNone(failed.receipt)
        self.assertIsNone(session.persistence_binding)
        self.assertIsNone(session.save_service.saved_workspace_snapshot)
        self.assertTrue(session.save_service.project_dirty)
        self.assertTrue(session.source_retained)
        self.assertFalse(self.destination.exists())
        result = session.save(self.base / 'retry.localcat-project')
        self.assertTrue(result.receipt.durable)
        self.assertFalse(session.source_retained)
        self.assertEqual(session.open_saved_package().workspace.documents[0].segments[0].target, '保留未保存编辑')

    def test_failed_resave_preserves_lkg_binding_and_dirty_current_edits(self):
        session = self.prepare()
        session.save(self.destination)
        saved = self.destination.read_bytes()
        baseline = session.save_service.saved_workspace_snapshot
        binding = session.persistence_binding
        self.edit(session, 0, '必须留在工作区', True)
        with mock.patch(_PACKAGE_PORT + '.publish_candidate', side_effect=OSError('fault')):
            result = session.save()
        self.assertIsNone(result.receipt)
        self.assertEqual(self.destination.read_bytes(), saved)
        self.assertEqual(session.persistence_binding, binding)
        self.assertEqual(session.save_service.saved_workspace_snapshot, baseline)
        self.assertTrue(session.save_service.project_dirty)
        self.assertEqual(session.workspace_service.flat_segments[0].segment.target, '必须留在工作区')
        self.assertTrue(session.save().receipt.durable)

    def test_recovery_required_result_is_returned_without_receipt_or_blind_retry(self):
        session = self.prepare()
        with mock.patch(_PACKAGE_PORT + '.commit_candidate', side_effect=OSError('cleanup fault')):
            result = session.save(self.destination)
        self.assertIsNone(result.receipt)
        self.assertIs(result.save_report.journal_state, SaveJournalState.RECOVERY_REQUIRED)
        self.assertTrue(session.recovery_required)
        self.assertFalse(session.source_retained)
        with self.assertRaises(ProjectWorkspaceError) as caught:
            session.save(self.base / 'must-not-retry.localcat-project')
        self.assertEqual(caught.exception.code, 'PROJECT.SAVE.RECOVERY_REQUIRED')
        self.assertFalse((self.base / 'must-not-retry.localcat-project').exists())

    def test_cancel_and_close_release_retained_source_and_do_not_publish(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                token = CancellationToken()
                backend = compose_platform_file_backend(self.root)
                authorities = []
                def record(method):
                    def call(*args, **kwargs):
                        authority = method(*args, **kwargs)
                        authorities.append(authority)
                        return authority
                    return call
                with mock.patch.object(backend, 'bind_root', record(backend.bind_root)), \
                     mock.patch.object(backend, 'open_regular', record(backend.open_regular)):
                    session = self.prepare(cancellation=token, file_system=backend)
                self.assertTrue(any(not item.closed for item in authorities))
                if cancel:
                    token.cancel()
                    result = session.save(self.destination)
                    self.assertIsNone(result.receipt)
                    self.assertIsNone(session.persistence_binding)
                    self.assertTrue(session.save_service.project_dirty)
                else:
                    session.close()
                    session.close()
                    self.assertTrue(session.closed)
                    with self.assertRaises(ProjectWorkspaceError):
                        session.save(self.destination)
                self.assertFalse(session.source_retained)
                self.assertTrue(all(item.closed for item in authorities))
                self.assertFalse(self.destination.exists())

    def test_invalid_syntax_retains_parser_positions_and_closes_source(self):
        self.source.write_bytes((FIXTURES / 'mixed_languages.rpy').read_bytes())
        with self.assertRaises(SelectedProjectDocumentsError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.parser_code, 'PARSER.RPY.MIXED_LANGUAGE')
        self.assertGreater(caught.exception.diagnostics[0].line_number, 1)
        self.assertFalse(self.destination.exists())

    def test_reject_non_rpy_single_input_before_read_and_require_first_save_target(self):
        other = self.root / 'other.txt'
        other.write_text('not a TL', encoding='utf-8')
        with mock.patch('rpy_project_adapter.prepare_selected_project_documents') as intake:
            with self.assertRaises(ProjectWorkspaceError):
                self.adapter.prepare_single(self.root, other, self.request, session_id='wrong-format')
            intake.assert_not_called()
        session = self.prepare()
        with self.assertRaises(ProjectWorkspaceError):
            session.save()
        with self.assertRaises(ProjectWorkspaceError):
            session.open_saved_package()
        self.assertTrue(session.source_retained)


if __name__ == '__main__':
    unittest.main()
