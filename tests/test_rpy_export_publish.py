"""Task 3.4: real single-file publication through Parser and ProjectPackage."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_composition import ParserApplicationSurface
from parser_source import CancellationToken
from platform_fs_contracts import CandidateFile
from project_codec_settings import compose_project_codec_runtime
from project_package import ProjectPackageService
from project_workspace_intake import SelectedProjectDocumentsRequest
from rpy_project_adapter import RpyProjectAdapter
from rpy_project_export import RpyExportContext
from tests.test_parser_roundtrip import _CandidateFile, _PlatformAdapter, _POST_NAMING_FAULTS
from tests.test_rpy_provider import FIXTURES


class RpyExportPublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.config = self.base / 'config'
        self.config.mkdir()
        self.raw = b'\xef\xbb\xbf' + (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes().replace(b'\n', b'\r\n')
        source = self.base / 'chapter.rpy'
        source.write_bytes(self.raw)
        self.runtime = compose_project_codec_runtime(self.config)
        self.package = ProjectPackageService()
        self.adapter = RpyProjectAdapter(self.runtime, package_service=self.package)
        self.package_path = self.base / 'project.localcat-project'
        with self.adapter.prepare_single(
            self.base, source, SelectedProjectDocumentsRequest('TL', 'en', 'zh'),
            session_id='intake',
        ) as session:
            self.assertTrue(session.save(self.package_path).receipt.durable)
        source.unlink()
        opened = self.package.open(self.package_path)
        self.save = opened.create_save_service(session_id='editor', revision=0)
        self.service = self.save.workspace_service
        self.context = RpyExportContext(self.service, opened.persistence_binding, self.runtime, 7)
        self.target = self.base / 'output.rpy'
        self.baseline = self.save.saved_workspace_snapshot
        self.package_bytes = self.package_path.read_bytes()

    def prepare(self, cancellation=None):
        preview = self.adapter.prepare_export(
            self.service, self.context.persistence_binding, self.target,
            config_dir=self.config, request_generation=7, cancellation=cancellation,
        )
        self.addCleanup(preview.close)
        return preview

    def edit(self, index, target, confirmed=False):
        self.service.update_segment_edit(
            self.service.flat_segments[index].identity, target=target, confirmed=confirmed,
            session_id=self.service.session_id, base_revision=self.service.revision,
        )

    def assert_unchanged(self, dirty):
        self.assertEqual(self.package_path.read_bytes(), self.package_bytes)
        self.assertEqual(self.save.saved_workspace_snapshot, self.baseline)
        self.assertEqual(self.save.project_dirty, dirty)

    def assert_released(self, preview):
        self.assertTrue(preview.closed)
        self.assertEqual(list(self.config.iterdir()), [])
        self.assertEqual(list(self.base.glob('.parser-*.tmp')), [])

    def test_cold_package_noop_publishes_exact_bom_crlf_comments_once(self):
        preview = self.prepare()
        original = ParserApplicationSurface.write_prepared
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', autospec=True, side_effect=original) as writer:
            result = preview.publish(self.context, self.target)
            self.assertEqual(result.outcome, 'published')
            self.assertIs(preview.publish(self.context, self.target), result)
        writer.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.raw)
        self.assertEqual(result.output_sha256, hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result.output_byte_count, len(self.raw))
        self.assertEqual((result.preview_id, result.session_id, result.workspace_revision,
                          result.request_generation, result.target_path),
                         (preview.view.preview_id, 'editor', 0, 7, str(self.target)))
        self.assertEqual(preview.view.status, 'published')
        self.assertTrue(is_dataclass(result))
        self.assertFalse(any(word in field.name for field in fields(result)
                             for word in ('receipt', 'private', 'prepared', 'writer', 'payload', 'token')))
        with self.assertRaises(FrozenInstanceError):
            result.outcome = 'failed'
        self.assert_released(preview)
        self.assert_unchanged(False)

    def test_single_target_only_and_empty_target_does_not_fallback(self):
        for translated in ('引号 " 与 \\ 和\n换行', ''):
            with self.subTest(translated=translated):
                self.edit(0, translated, True)
                preview = self.prepare()
                result = preview.publish(self.context, self.target)
                self.assertEqual(result.outcome, 'published')
                encoded = translated.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').encode('utf-8')
                self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode('utf-8'), encoded))
                self.assert_released(preview)
                self.assert_unchanged(True)

    def test_confirmed_only_is_not_serialized_and_does_not_clear_dirty(self):
        self.edit(0, self.service.flat_segments[0].segment.target, True)
        preview = self.prepare()
        self.assertEqual(preview.publish(self.context, self.target).outcome, 'published')
        self.assertEqual(self.target.read_bytes(), self.raw)
        self.assert_released(preview)
        self.assert_unchanged(True)

    def test_recomposed_runtime_uses_original_issuing_surface(self):
        preview = self.prepare()
        current = replace(self.context, runtime=compose_project_codec_runtime(self.config))
        original = ParserApplicationSurface.write_prepared
        def require_issuer(surface, prepared):
            self.assertIs(surface, self.runtime.surface)
            return original(surface, prepared)
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', autospec=True, side_effect=require_issuer) as writer:
            self.assertEqual(preview.publish(current, self.target).outcome, 'published')
        writer.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.raw)
        self.assert_released(preview)

    def test_stale_edit_rejected_before_parser_and_requires_new_preview(self):
        self.target.write_bytes(b'previous target')
        preview = self.prepare()
        self.edit(0, 'changed')
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', side_effect=AssertionError('stale writer')):
            result = preview.publish(self.context, self.target)
            self.assertEqual(result.outcome, 'stale')
            self.assertIs(preview.publish(self.context, self.target), result)
        self.assertEqual(result.diagnostics[0].code, 'RPY.EXPORT.PREVIEW_STALE')
        self.assertEqual(self.target.read_bytes(), b'previous target')
        self.assert_released(preview)
        self.assert_unchanged(True)

    def test_target_replaced_after_preview_is_preserved(self):
        preview = self.prepare()
        self.target.write_bytes(b'competitor')
        self.assertEqual(preview.publish(self.context, self.target).outcome, 'stale')
        self.assertEqual(self.target.read_bytes(), b'competitor')
        self.assert_released(preview)
        self.assert_unchanged(False)

    def test_prepublication_fault_retains_old_target_and_cannot_retry(self):
        self.target.write_bytes(b'previous target')
        self.edit(0, 'changed')
        preview = self.prepare()
        with mock.patch.object(_CandidateFile, '_flush_content', side_effect=OSError('secret fault')) as fault:
            result = preview.publish(self.context, self.target)
        fault.assert_called_once()
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.diagnostics[0].code, 'PARSER.SOURCE.WRITE_FAILED')
        self.assertIsNone(result.output_sha256)
        self.assertNotIn('secret', repr(result))
        self.assertIs(preview.publish(self.context, self.target), result)
        self.assertEqual(self.target.read_bytes(), b'previous target')
        self.assert_released(preview)
        self.assert_unchanged(True)
        retry = self.prepare()
        self.assertEqual(retry.publish(self.context, self.target).outcome, 'published')
        self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode(), b'changed'))
        self.assert_unchanged(True)

    def test_native_post_naming_fault_reports_uncertain_with_actual_bytes(self):
        observed = []
        def fault(point):
            if point == _POST_NAMING_FAULTS[0]:
                observed.append(point)
                raise RuntimeError('secret fault after naming')
        self.runtime.surface._platform_backend_factory = lambda root: _PlatformAdapter(_fault_injector=fault)
        self.target.write_bytes(b'previous target')
        self.edit(0, 'changed')
        preview = self.prepare()
        result = preview.publish(self.context, self.target)
        self.assertEqual(observed, [_POST_NAMING_FAULTS[0]])
        self.assertEqual(result.outcome, 'uncertain')
        self.assertEqual(result.diagnostics[0].code, 'PARSER.SOURCE.WRITE_RECOVERY_REQUIRED')
        self.assertIsNone(result.output_sha256)
        self.assertIsNone(result.output_byte_count)
        self.assertNotIn('secret', repr(result))
        self.assertEqual(self.target.read_bytes(), self.raw.replace('灯笼亮着。'.encode(), b'changed'))
        self.assertIs(preview.publish(self.context, self.target), result)
        self.assert_released(preview)
        self.assert_unchanged(True)

    def test_cancel_before_publish_and_close_never_start_writer(self):
        for action in ('cancel', 'close'):
            with self.subTest(action=action):
                token = CancellationToken()
                preview = self.prepare(token)
                token.cancel() if action == 'cancel' else preview.close()
                with mock.patch.object(ParserApplicationSurface, 'write_prepared', side_effect=AssertionError('cancelled writer')):
                    result = preview.publish(self.context, self.target)
                self.assertEqual(result.outcome, 'cancelled')
                self.assertFalse(self.target.exists())
                self.assert_released(preview)
                self.assert_unchanged(False)

    def test_cancel_inside_writer_keeps_parser_failed_outcome(self):
        token = CancellationToken()
        self.target.write_bytes(b'previous target')
        preview = self.prepare(token)
        original = CandidateFile.write_all
        def cancel_after_write(*args, **kwargs):
            result = original(*args, **kwargs)
            token.cancel()
            return result
        with mock.patch.object(CandidateFile, 'write_all', autospec=True, side_effect=cancel_after_write):
            result = preview.publish(self.context, self.target)
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.diagnostics[0].code, 'PARSER.SOURCE.CANCELLED')
        self.assertEqual(self.target.read_bytes(), b'previous target')
        self.assert_released(preview)
        self.assert_unchanged(False)

    def test_cancel_after_naming_preserves_published_or_uncertain(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                token = CancellationToken()
                def cancel_after_naming(point):
                    if point == _POST_NAMING_FAULTS[0]:
                        token.cancel()
                        if raises:
                            raise RuntimeError('secret after naming')
                self.runtime.surface._platform_backend_factory = lambda root: _PlatformAdapter(_fault_injector=cancel_after_naming)
                preview = self.prepare(token)
                result = preview.publish(self.context, self.target)
                self.assertTrue(token.cancelled)
                self.assertEqual(result.outcome, 'uncertain' if raises else 'published')
                self.assertEqual(self.target.read_bytes(), self.raw)
                self.assert_released(preview)
                self.assert_unchanged(False)

    def test_application_cleanup_error_does_not_report_prepublication_failure(self):
        for authority_name in ('_opened', '_bridge'):
            with self.subTest(authority=authority_name):
                preview = self.prepare()
                authority = getattr(preview, authority_name)
                owner = type(authority)
                original = owner.close
                def close_then_raise(instance):
                    original(instance)
                    if instance is authority:
                        raise OSError('secret cleanup error')
                with mock.patch.object(owner, 'close', autospec=True, side_effect=close_then_raise):
                    result = preview.publish(self.context, self.target)
                self.assertEqual(result.outcome, 'uncertain')
                self.assertIsNone(result.output_sha256)
                self.assertNotIn('secret', repr(result))
                self.assertEqual(self.target.read_bytes(), self.raw)
                self.assert_released(preview)
                self.assert_unchanged(False)


if __name__ == '__main__':
    unittest.main()
