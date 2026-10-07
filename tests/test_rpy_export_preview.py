"""Real Parser and ProjectPackage export previews, without publication."""

from dataclasses import fields, is_dataclass, replace
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_composition import ParserApplicationSurface
from parser_source import CancellationToken
from platform_fs_contracts import BoundDirectoryAuthority
from project_codec_settings import (
    CodecProviderSetting, CodecSettings, CodecSettingsRepository,
    compose_project_codec_runtime,
)
from project_package import OpenedProjectPackage, ProjectPackageBlobSource, ProjectPackageService
from project_save import ProjectSaveService, WorkspaceSaveBaseline
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import ProjectWorkspaceError
from rpy_project_adapter import RpyProjectAdapter
from rpy_project_export import RpyExportContext, prepare_rpy_export
from project_workspace_intake import SelectedProjectDocumentsRequest
from tests.test_rpy_provider import FIXTURES


class RpyExportPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.config = self.base / 'config'
        self.config.mkdir()
        self.raw = (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes()
        self.source = self.base / 'chapter.rpy'
        self.source.write_bytes(self.raw)
        self.runtime = compose_project_codec_runtime(self.config)
        self.package = ProjectPackageService()
        self.adapter = RpyProjectAdapter(self.runtime, package_service=self.package)
        session = self.adapter.prepare_single(
            self.base, self.source, SelectedProjectDocumentsRequest('TL', 'en', 'zh'),
            session_id='intake',
        )
        self.addCleanup(session.close)
        self.package_path = self.base / 'project.localcat-project'
        self.assertTrue(session.save(self.package_path).receipt.durable)
        session.close()
        self.opened = self.package.open(self.package_path)
        # This is the ordinary package-open path used by Controller.
        self.save = self.opened.create_save_service(session_id='editor', revision=0)
        self.service = self.save.workspace_service
        self.context = RpyExportContext(self.service, self.opened.persistence_binding, self.runtime, 7)
        self.target = self.base / 'output.rpy'
        self.baseline = self.save.saved_workspace_snapshot
        self.package_bytes = self.package_path.read_bytes()
        self.source.unlink()

    def prepare(self, context=None, **kwargs):
        preview = prepare_rpy_export(
            context or self.context, self.target, config_dir=self.config,
            package_service=self.package, **kwargs,
        )
        self.addCleanup(preview.close)
        return preview

    def edit(self, index, target, confirmed=False):
        self.service.update_segment_edit(
            self.service.flat_segments[index].identity, target=target, confirmed=confirmed,
            session_id=self.service.session_id, base_revision=self.service.revision,
        )

    def assert_unchanged(self):
        self.assertEqual(self.package_path.read_bytes(), self.package_bytes)
        self.assertEqual(self.save.saved_workspace_snapshot, self.baseline)

    def test_noop_counts_original_bytes_and_neutral_view_without_authority(self):
        with mock.patch.object(ParserApplicationSurface, 'write_prepared', side_effect=AssertionError('published')):
            preview = self.prepare()
        view = preview.view
        self.assertEqual(view.status, 'ready')
        self.assertEqual((view.segment_count, view.modified_count, view.empty_count, view.unconfirmed_count), (6, 0, 2, 6))
        self.assertEqual(view.target_path, str(self.target))
        self.assertEqual(view.output_sha256, hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(view.output_byte_count, len(self.raw))
        self.assertEqual((view.session_id, view.workspace_revision, view.request_generation), ('editor', 0, 7))
        self.assertFalse(self.target.exists())
        self.assert_unchanged()
        self.assertTrue(is_dataclass(view))
        self.assertFalse(any(word in field.name for field in fields(view) for word in ('token', 'private', 'prepared', 'writer', 'payload')))
        preview.close()
        self.assertTrue(preview.closed)
        self.assertEqual(list(self.config.iterdir()), [])

    def test_unsaved_edits_export_every_target_and_compare_original_not_saved_overlay(self):
        self.edit(0, '新的译文', True)
        self.edit(2, '', True)
        result = self.package.save_workspace(self.save, self.package_path, persistence_binding=self.context.persistence_binding)
        self.context = replace(self.context, persistence_binding=result.persistence_binding)
        self.edit(3, '未保存的修改')
        self.baseline = self.save.saved_workspace_snapshot
        self.package_bytes = self.package_path.read_bytes()
        observed = []
        original = ParserApplicationSurface.prepare_round_trip
        def capture(surface, opened, token, edits):
            observed.extend(edits)
            return original(surface, opened, token, edits)
        with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', capture):
            preview = self.prepare()
        self.assertEqual(preview.view.status, 'ready')
        self.assertEqual((preview.view.modified_count, preview.view.empty_count, preview.view.unconfirmed_count), (3, 3, 4))
        self.assertEqual(tuple(edit.target for edit in observed), tuple(item.segment.target for item in self.service.flat_segments))
        self.assertTrue(self.save.project_dirty)
        self.assert_unchanged()

    def test_invalid_placeholder_is_blocked_with_safe_slot_and_line(self):
        self.edit(0, '敏感正文 [new_expression()]')
        preview = self.prepare()
        self.assertEqual(preview.view.status, 'blocked')
        diagnostic = preview.view.diagnostics[0]
        self.assertEqual(diagnostic.local_segment_id, self.service.flat_segments[0].identity.local_segment_id)
        self.assertEqual(diagnostic.source_ref, 'chapter.rpy')
        self.assertGreater(diagnostic.line_number, 0)
        self.assertNotIn('敏感正文', repr(preview.view))
        self.assertNotIn('new_expression', repr(preview.view))
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.config.iterdir()), [])

    def test_edits_confirm_and_project_generation_changes_stale_and_cleanup(self):
        for change in ('target', 'confirmed', 'generation', 'service'):
            with self.subTest(change=change):
                preview = self.prepare()
                context = self.context
                if change == 'target':
                    self.edit(0, 'Changed')
                elif change == 'confirmed':
                    self.edit(0, self.service.flat_segments[0].segment.target, True)
                elif change == 'generation':
                    context = replace(context, request_generation=8)
                else:
                    context = replace(context, workspace_service=self.opened.create_workspace_service(session_id='other', revision=0))
                self.assertEqual(preview.revalidate(context, self.target).status, 'stale')
                self.assertTrue(preview.closed)
                self.assertEqual(list(self.config.iterdir()), [])
                self.assertFalse(self.target.exists())

    def test_disabled_codec_blocks_and_changed_runtime_invalidates(self):
        preview = self.prepare()
        CodecSettingsRepository(self.config).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        runtime = compose_project_codec_runtime(self.config)
        disabled = replace(self.context, runtime=runtime)
        self.assertEqual(preview.revalidate(disabled, self.target).status, 'stale')
        blocked = self.prepare(disabled)
        self.assertEqual(blocked.view.status, 'blocked')
        self.assertEqual(blocked.view.diagnostics[0].code, 'PARSER.SELECTION.PROVIDER_DISABLED')
        self.assertFalse(self.target.exists())

    def test_existing_absent_and_reselected_target_conditions_stale(self):
        for condition in ('existing', 'absent', 'reselected'):
            with self.subTest(condition=condition):
                self.target.unlink(missing_ok=True)
                if condition == 'existing':
                    self.target.write_bytes(b'old target')
                preview = self.prepare()
                target = self.target
                if condition == 'existing':
                    try:
                        self.target.unlink()
                    except OSError as error:
                        if os.name != 'nt' or error.winerror != 32:
                            raise
                        # A retained Windows SOURCE rejects replacement. A
                        # refused mutation must not be treated as a stale file.
                        self.assertEqual(self.target.read_bytes(), b'old target')
                        self.assertEqual(preview.revalidate(self.context, target).status, 'ready')
                        self.assert_unchanged()
                        preview.close()
                        self.assertTrue(preview.closed)
                        self.target.unlink()
                        self.target.write_bytes(b'replaced target')
                        fresh = self.prepare()
                        self.assertEqual(fresh.view.status, 'ready')
                        self.assertNotEqual(fresh.view.preview_id, preview.view.preview_id)
                        fresh.close()
                        self.assertEqual(list(self.config.iterdir()), [])
                        continue
                    self.target.write_bytes(b'replaced target')
                elif condition == 'absent':
                    self.target.write_bytes(b'competitor')
                else:
                    target = self.base / 'other.rpy'
                self.assertEqual(preview.revalidate(self.context, target).status, 'stale')
                self.assertTrue(preview.closed)
                self.assertEqual(list(self.config.iterdir()), [])
                self.assert_unchanged()

    def test_cancel_before_and_after_preparation_closes_without_publication(self):
        for before in (True, False):
            with self.subTest(before=before):
                token = CancellationToken()
                if before:
                    token.cancel()
                preview = self.prepare(cancellation=token)
                if not before:
                    token.cancel()
                    preview.revalidate(self.context, self.target)
                self.assertEqual(preview.view.status, 'cancelled')
                preview.close()
                self.assertEqual(list(self.config.iterdir()), [])
                self.assertFalse(self.target.exists())
                self.assert_unchanged()

    def test_adapter_consumes_normal_package_open_and_recomposed_runtime(self):
        preview = self.adapter.prepare_export(
            self.service, self.context.persistence_binding, self.target,
            config_dir=self.config, request_generation=7,
        )
        self.addCleanup(preview.close)
        current = replace(self.context, runtime=compose_project_codec_runtime(self.config))
        self.assertIsNot(current.runtime.surface, self.context.runtime.surface)
        self.assertEqual(preview.revalidate(current, self.target).status, 'ready')

    def test_settings_changed_without_refreshing_runtime_stales(self):
        preview = self.prepare()
        self.assertEqual(preview.view.status, 'ready')
        self.assertEqual(list(self.config.iterdir()), [])
        CodecSettingsRepository(self.config).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
        self.assertEqual(preview.revalidate(self.context, self.target).status, 'stale')
        self.assertTrue(preview.closed)
        self.assertFalse(self.target.exists())
        self.assert_unchanged()

    def test_source_bridge_cleanup_failure_blocks_before_preview_ready(self):
        from rpy_project_export import _SourceBridge
        original = _SourceBridge.close
        failed = False
        def close_then_fail_once(bridge):
            nonlocal failed
            original(bridge)
            if not failed:
                failed = True
                raise OSError('private cleanup detail')
        with mock.patch.object(_SourceBridge, 'close', close_then_fail_once):
            preview = self.prepare()
        self.assertTrue(failed)
        self.assertEqual(preview.view.status, 'blocked')
        self.assertTrue(preview.closed)
        self.assertNotIn('private cleanup detail', repr(preview.view))
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.config.iterdir()), [])
        self.assert_unchanged()

    def test_confirm_only_does_not_change_output(self):
        self.edit(0, self.service.flat_segments[0].segment.target, True)
        preview = self.prepare()
        self.assertEqual((preview.view.modified_count, preview.view.unconfirmed_count), (0, 5))
        self.assertEqual(preview.view.output_sha256, hashlib.sha256(self.raw).hexdigest())
        self.assertTrue(self.save.project_dirty)

    def test_changed_package_after_preview_stales(self):
        preview = self.prepare()
        replacement = self.base / 'copy.localcat-project'
        replacement.write_bytes(self.package_bytes)
        try:
            replacement.replace(self.package_path)
        except OSError as error:
            if os.name != 'nt' or error.winerror != 32:
                raise
            self.assert_unchanged()
            self.assertEqual(replacement.read_bytes(), self.package_bytes)
            self.assertEqual(preview.revalidate(self.context, self.target).status, 'ready')
            preview.close()
            replacement.replace(self.package_path)
            # The old saved binding cannot authorize the replaced package,
            # even after the Windows guard has been released.
            stale = self.prepare()
            self.assertEqual(stale.view.status, 'blocked')
            self.assertTrue(stale.closed)
            self.assertEqual(stale.view.diagnostics[0].code, 'PROJECT.PACKAGE.SOURCE_STALE')
            self.assertEqual(list(self.config.iterdir()), [])
            return
        self.assertEqual(preview.revalidate(self.context, self.target).status, 'stale')
        self.assertTrue(preview.closed)
        self.assertEqual(list(self.config.iterdir()), [])

    def test_persistent_bridge_cleanup_failure_is_blocked_with_residual_report(self):
        for operation in ('unlink', 'rmdir'):
            with self.subTest(operation=operation):
                if operation == 'unlink':
                    original = BoundDirectoryAuthority.unlink_owned
                    def deny_source(parent, name, identity):
                        if name == 'source.rpy':
                            raise PermissionError('private persistent removal failure')
                        return original(parent, name, identity)
                    fault = mock.patch.object(BoundDirectoryAuthority, 'unlink_owned', deny_source)
                else:
                    original = Path.rmdir
                    def deny_directory(path):
                        if path.name.startswith('rpy-export-'):
                            raise PermissionError('private persistent removal failure')
                        return original(path)
                    fault = mock.patch.object(Path, 'rmdir', deny_directory)
                with fault:
                    preview = self.prepare()
                self.assertEqual(preview.view.status, 'blocked')
                self.assertTrue(preview.closed)
                self.assertIn('RPY.EXPORT.CLEANUP_FAILED', [item.code for item in preview.view.diagnostics])
                self.assertNotIn('private persistent removal failure', repr(preview.view))
                self.assertEqual(preview.publish(self.context, self.target).outcome, 'blocked')
                self.assertFalse(self.target.exists())
                self.assert_unchanged()
                residuals = list(self.config.glob('rpy-export-*'))
                self.assertEqual(len(residuals), 1)
                residual = residuals[0]
                if operation == 'unlink':
                    self.assertEqual((residual / 'source.rpy').read_bytes(), self.raw)
                    (residual / 'source.rpy').unlink()
                # Failure is visible; no product retry or recursive cleanup
                # is implied. These real removals also prove handles released.
                residual.rmdir()
                self.assertEqual(list(self.config.iterdir()), [])

    def test_project_package_internal_storage_and_hardlink_targets_are_rejected(self):
        link = self.base / 'looks-like-tl.rpy'
        cases = (self.package_path, self.config / 'output.rpy', link)
        for target in cases:
            with self.subTest(target=target):
                if target == link:
                    target.hardlink_to(self.package_path)
                old_target = self.target
                self.target = target
                try:
                    preview = self.prepare()
                    self.assertEqual(preview.view.status, 'blocked')
                    self.assertTrue(preview.closed)
                    self.assertEqual(self.package_path.read_bytes(), self.package_bytes)
                    self.assertEqual(list(self.config.iterdir()), [])
                finally:
                    self.target = old_target
                    if target == link:
                        target.unlink()

    def test_missing_parent_is_not_created(self):
        self.target = self.base / 'missing' / 'nested' / 'target.rpy'
        preview = self.prepare()
        self.assertEqual(preview.view.status, 'blocked')
        self.assertFalse((self.base / 'missing').exists())
        self.assertEqual(list(self.config.iterdir()), [])
        self.assert_unchanged()

    def test_symlink_target_is_rejected(self):
        self.target = self.base / 'symlink.rpy'
        try:
            self.target.symlink_to(self.package_path)
        except OSError as error:
            if os.name == 'nt' and error.winerror == 1314:
                self.skipTest('Windows symlink privilege unavailable (WinError 1314)')
            raise
        preview = self.prepare()
        self.assertEqual(preview.view.status, 'blocked')
        self.assertEqual(list(self.config.iterdir()), [])
        self.assert_unchanged()

    def test_member_context_exit_failure_is_not_consumed_by_parser(self):
        original = OpenedProjectPackage.open_member
        for member_kind in ('sources/', 'codec-private/'):
            with self.subTest(member_kind=member_kind):
                @contextmanager
                def failure(package, path, **kwargs):
                    with original(package, path, **kwargs) as stream:
                        yield stream
                    if path.startswith(member_kind):
                        raise ProjectWorkspaceError('PROJECT.PACKAGE.SOURCE_STALE')
                with mock.patch.object(OpenedProjectPackage, 'open_member', failure), \
                     mock.patch.object(ParserApplicationSurface, 'open_input', side_effect=AssertionError('premature parser input')):
                    preview = self.prepare()
                self.assertEqual(preview.view.status, 'blocked')
                self.assertEqual(preview.view.diagnostics[0].code, 'PROJECT.PACKAGE.SOURCE_STALE')
                self.assertEqual(list(self.config.iterdir()), [])
                self.assertFalse(self.target.exists())

    def test_wrong_member_content_is_detected_before_parser(self):
        original = OpenedProjectPackage.open_member
        for member_kind in ('sources/', 'codec-private/'):
            with self.subTest(member_kind=member_kind):
                @contextmanager
                def corrupt(package, path, **kwargs):
                    with original(package, path, **kwargs) as stream:
                        data = stream.read()
                    yield io.BytesIO(data[:-1] + b'!' if path.startswith(member_kind) else data)
                with mock.patch.object(OpenedProjectPackage, 'open_member', corrupt), \
                     mock.patch.object(ParserApplicationSurface, 'open_input', side_effect=AssertionError('premature parser input')):
                    preview = self.prepare()
                self.assertEqual(preview.view.status, 'blocked')
                self.assertEqual(list(self.config.iterdir()), [])

    def save_altered_document(self, document, private_sources=()):
        workspace = replace(self.service.workspace, documents=(document,))
        service = ProjectWorkspaceService(workspace, None, session_id='altered', revision=0)
        previous = self.package.open(self.package_path)
        save = ProjectSaveService(service, baseline=WorkspaceSaveBaseline.from_workspace(
            previous.workspace, workspace_revision=0,
            saved_package_digest=previous.validation.workspace_content_digest))
        result = self.package.save_workspace(
            save, self.package_path, persistence_binding=self.context.persistence_binding,
            codec_private_sources=private_sources,
        )
        self.assertIsNotNone(result.receipt, result.save_report)
        opened = self.package.open(self.package_path)
        return RpyExportContext(opened.create_workspace_service(session_id='altered', revision=0),
                                opened.persistence_binding, self.runtime, 7)

    def test_real_package_with_valid_digest_but_wrong_private_is_rejected_by_codec(self):
        document = self.service.workspace.documents[0]
        payload = b'{}'
        digest = hashlib.sha256(payload).hexdigest()
        private = replace(document.codec_private_member,
                          member_path=f'codec-private/{document.document_id}/{digest}.bin',
                          sha256=digest, byte_count=len(payload))
        blob = ProjectPackageBlobSource.from_bytes(
            document_id=document.document_id, member_path=private.member_path,
            payload=payload, expected_sha256=digest, expected_byte_count=len(payload),
        )
        context = self.save_altered_document(replace(document, codec_private_member=private), (blob,))
        with mock.patch.object(ParserApplicationSurface, 'bind_round_trip_target', side_effect=AssertionError('target touched')):
            preview = self.prepare(context)
        self.assertEqual(preview.view.status, 'blocked')
        self.assertTrue(preview.view.diagnostics[0].code.startswith('PARSER.RPY.'))
        self.assertEqual(list(self.config.iterdir()), [])

    def test_real_package_neutral_source_facts_must_match_parser_slot_set(self):
        document = self.service.workspace.documents[0]
        for field, value in (('source', 'not the original source'), ('raw_speaker', 'other_speaker')):
            with self.subTest(field=field):
                changed = replace(document, source_segments=(
                    replace(document.source_segments[0], **{field: value}), *document.source_segments[1:]))
                context = self.save_altered_document(changed)
                with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', side_effect=AssertionError('invalid source prepared')):
                    preview = self.prepare(context)
                self.assertEqual(preview.view.status, 'blocked')
                self.assertEqual(preview.view.diagnostics[0].code, 'RPY.EXPORT.SOURCE_MISMATCH')
                self.context = replace(self.context, persistence_binding=context.persistence_binding)
                self.assertEqual(list(self.config.iterdir()), [])

    def test_current_source_private_version_or_slot_change_cannot_reuse_saved_data(self):
        document = self.service.workspace.documents[0]
        cases = (
            replace(document, source_snapshot_digest='0' * 64),
            replace(document, codec_identity=replace(document.codec_identity, codec_version='2'),
                    codec_private_member=replace(document.codec_private_member,
                                                 codec_identity=replace(document.codec_identity, codec_version='2'))),
            replace(document, codec_private_member=replace(document.codec_private_member, profile_version='wrong-v1')),
            replace(document, source_segments=tuple(reversed(document.source_segments)),
                    editing_overlay=tuple(reversed(document.editing_overlay))),
        )
        for changed in cases:
            with self.subTest(document=changed.document_id):
                service = ProjectWorkspaceService(replace(self.service.workspace, documents=(changed,)),
                                                  None, session_id='changed', revision=0)
                context = replace(self.context, workspace_service=service)
                preview = self.prepare(context)
                self.assertEqual(preview.view.status, 'blocked')
                self.assertEqual(list(self.config.iterdir()), [])

    def test_cancel_during_preparation_closes_actual_prepared_and_input(self):
        token = CancellationToken()
        original = ParserApplicationSurface.prepare_round_trip
        held = []
        def cancel(surface, opened, opaque, edits):
            prepared = original(surface, opened, opaque, edits)
            held.append(prepared)
            token.cancel()
            return prepared
        with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', cancel):
            preview = self.prepare(cancellation=token)
        self.assertEqual(preview.view.status, 'cancelled')
        self.assertTrue(all(prepared.closed for prepared in held))
        self.assertEqual(list(self.config.iterdir()), [])
        self.assertFalse(self.target.exists())

    def test_edit_while_preparing_discards_late_result(self):
        original = ParserApplicationSurface.prepare_round_trip
        def edit(surface, opened, opaque, edits):
            prepared = original(surface, opened, opaque, edits)
            self.edit(0, 'later edit')
            return prepared
        with mock.patch.object(ParserApplicationSurface, 'prepare_round_trip', edit):
            preview = self.prepare()
        self.assertEqual(preview.view.status, 'stale')
        self.assertTrue(preview.closed)
        self.assertEqual(list(self.config.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
