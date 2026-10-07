"""Verified TL source reconciliation, private replacement and cold rebind."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from parser_rpy_codec import build_private_payload, RPY_CODEC_IDENTITY
from parser_source import CancellationToken
from project_codec_settings import compose_project_codec_runtime
from project_package import ProjectPackageService
from project_workspace import ReconciliationDecision, ReconciliationDisposition
from project_workspace_contracts import ProjectWorkspaceError, SourcePresence
from project_workspace_intake import SelectedProjectDocumentsRequest, OriginRenameMapping
from rpy_project_adapter import RpyProjectAdapter, RpyProjectSession, RpyProjectSelectionError
from tests.test_rpy_codec import dialogue


class RpySourceUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'tl'
        self.root.mkdir()
        self.path = self.root / 'chapter.rpy'
        self.raw = dialogue()
        self.path.write_bytes(self.raw)
        self.package = ProjectPackageService()
        self.adapter = RpyProjectAdapter(compose_project_codec_runtime(self.base / 'config'), package_service=self.package)
        self.session = self.adapter.prepare_single(self.root, self.path, SelectedProjectDocumentsRequest('TL', 'en', 'zh_Hans'), session_id='live')
        self.addCleanup(self.session.close)
        self.destination = self.base / 'work.localcat-project'
        self.session.save(self.destination)
        self.service = self.session.workspace_service
        self.service.update_segment_edit(self.service.flat_segments[0].identity, target='译文', confirmed=True, session_id='live', base_revision=0)

    def prepare(self, **kwargs):
        prepared = self.adapter.prepare_source_update(self.service, self.root, (self.path,), **kwargs)
        self.addCleanup(prepared.close)
        return prepared

    def stage(self, prepared):
        method = self.service.stage_source_rebind if self.service.origin_binding is None else self.service.stage_reconciliation
        return method(prepared.staged, associations=(), session_id=self.service.session_id, base_revision=self.service.revision)

    def apply(self, prepared, preview, decisions=()):
        staged = prepared.revalidate()
        return self.service.apply_reconciliation(preview.operation_id, decisions=decisions, session_id=self.service.session_id, base_revision=self.service.revision, incoming_source_identities=staged.source_identities)

    def mutate_source_or_verify_native_pin(self, prepared, mutation, expected_bytes):
        """Exercise drift or prove Windows blocks it until the lease is closed."""
        before_bytes = self.path.read_bytes()
        before_workspace = self.service.workspace
        try:
            mutation()
        except PermissionError:
            if os.name != 'nt':
                raise
            self.assertEqual(self.path.read_bytes(), before_bytes)
            self.assertEqual(self.service.workspace, before_workspace)
            self.assertIs(prepared.revalidate(), prepared.staged)
            authority_type = type(prepared._source._files[0].authority)
            with mock.patch.object(authority_type, 'read_at', side_effect=AssertionError('body read')):
                self.assertIs(prepared.revalidate_identity(), prepared.staged)
            prepared.close()
            mutation()
            self.assertEqual(self.path.read_bytes(), expected_bytes)
            self.assertEqual(self.service.workspace, before_workspace)
            with self.assertRaises(ProjectWorkspaceError) as caught:
                prepared.revalidate_identity()
            self.assertEqual(caught.exception.code, 'PROJECT.INTAKE.SOURCE_STALE')
            return False
        return True

    def test_changed_source_keeps_target_unconfirms_and_replaces_complete_private(self):
        raw = dialogue('"Changed source"', '"upstream target"')
        self.path.write_bytes(raw)
        prepared = self.prepare()
        preview = self.stage(prepared)
        self.assertEqual(len(preview.source_changed_identities), 1)
        self.apply(prepared, preview)
        segment = self.service.flat_segments[0].segment
        self.assertEqual((segment.source, segment.target, segment.confirmed), ('Changed source', '译文', False))
        result = prepared.save_reconciled_workspace(self.package, self.session.save_service, self.destination, persistence_binding=self.session.persistence_binding)
        self.assertTrue(result.receipt.durable)
        opened = self.package.open(self.destination)
        entry = opened.manifest.documents[0]
        with opened.open_member(entry.source_member.path) as stream:
            self.assertEqual(stream.read(), raw)
        with opened.open_member(entry.codec_private_member.path, codec_identity=RPY_CODEC_IDENTITY) as stream:
            self.assertEqual(stream.read(), build_private_payload(raw))
        self.assertTrue(prepared.closed)

    def test_label_change_is_new_removed_and_detached_is_absent_from_private(self):
        raw = self.raw.replace(b' unit:', b' replacement:')
        self.path.write_bytes(raw)
        prepared = self.prepare()
        preview = self.stage(prepared)
        self.assertEqual((len(preview.new_identities), len(preview.removed_identities)), (1, 1))
        with self.assertRaises(ProjectWorkspaceError):
            self.apply(prepared, preview)
        decision = ReconciliationDecision(preview.removed_identities[0], ReconciliationDisposition.KEEP_DETACHED)
        self.apply(prepared, preview, (decision,))
        document = self.service.workspace.documents[0]
        self.assertEqual([s.source_presence for s in document.source_segments], [SourcePresence.ATTACHED, SourcePresence.DETACHED])
        self.assertEqual(document.segments[1].target, '译文')
        result = prepared.save_reconciled_workspace(self.package, self.session.save_service, self.destination, persistence_binding=self.session.persistence_binding)
        self.assertTrue(result.receipt.durable)
        self.assertEqual(prepared.staged.workspace.documents[0].codec_private_member, document.codec_private_member)
        preview = self.adapter.prepare_export(self.service, result.persistence_binding,
            self.base / 'output.rpy', config_dir=self.base / 'config')
        self.addCleanup(preview.close)
        self.assertEqual(preview.view.status, 'ready')
        self.assertEqual(preview.view.segment_count, 1)
        self.assertEqual(preview.view.modified_count, 0)

    def test_strings_old_change_does_not_match_equal_translation_or_position(self):
        # Import the original strings as a distinct stable source before update.
        self.path.write_bytes(b'translate zh_Hans strings:\n    old "Old"\n    new "Target"\n')
        prepared = self.prepare()
        preview = self.stage(prepared)
        self.apply(prepared, preview, tuple(ReconciliationDecision(i, ReconciliationDisposition.REMOVE) for i in preview.removed_identities))
        # The next explicit update starts after releasing the previous source lease.
        prepared.close()
        self.path.write_bytes(b'translate zh_Hans strings:\n    old "New"\n    new "Target"\n')
        updated = self.prepare()
        preview = self.stage(updated)
        self.assertEqual((len(preview.new_identities), len(preview.removed_identities)), (1, 1))
        self.assertFalse(preview.unchanged_identities or preview.source_changed_identities)

    def test_cold_rebind_explicit_rename_preserves_all_document_references(self):
        cold = self.adapter.open_package(self.destination, session_id='cold')
        self.addCleanup(cold.close)
        self.service = cold.workspace_service
        old = self.service.workspace.documents[0]
        self.path = self.path.rename(self.root / 'renamed.rpy')
        prepared = self.prepare(rename_mappings=(OriginRenameMapping(old.source_ref, 'renamed.rpy', old.document_id),))
        document = prepared.staged.workspace.documents[0]
        self.assertEqual(document.document_id, old.document_id)
        self.assertIn('/' + old.document_id + '/', document.codec_private_member.member_path)
        self.assertEqual(prepared.codec_private_sources[0].reference, document.codec_private_member)
        self.assertEqual(prepared.codec_private_sources[0].document_id, old.document_id)
        preview = self.stage(prepared)
        self.assertEqual(len(preview.unchanged_identities), 1)
        self.apply(prepared, preview)

    def test_bound_rename_requires_mapping_and_live_identity(self):
        old = self.service.workspace.documents[0]
        self.path = self.path.rename(self.root / 'renamed.rpy')
        prepared = self.prepare(rename_mappings=(OriginRenameMapping(old.source_ref, 'renamed.rpy', old.document_id),))
        self.assertEqual(prepared.staged.origin_binding.revision, self.service.origin_binding.revision + 1)
        self.assertEqual(len(self.stage(prepared).unchanged_identities), 1)

    def test_drift_cancel_and_close_never_change_workspace(self):
        for mode in ('drift', 'cancel', 'close'):
            with self.subTest(mode=mode):
                token = CancellationToken()
                prepared = self.prepare(cancellation=token)
                before = self.service.workspace
                if mode == 'drift':
                    changed = dialogue('"Drift"')
                    if not self.mutate_source_or_verify_native_pin(
                            prepared, lambda: self.path.write_bytes(changed), changed):
                        continue
                elif mode == 'cancel':
                    token.cancel()
                else:
                    prepared.close()
                with self.assertRaises(ProjectWorkspaceError):
                    prepared.revalidate()
                self.assertEqual(self.service.workspace, before)
                self.assertTrue(prepared.closed)

    def test_reconciled_session_save_failure_retains_lease_and_retries(self):
        from tests.test_project_single_file_profile import _PACKAGE_PORT
        self.path.write_bytes(dialogue('"Changed source"'))
        prepared = self.prepare()
        self.apply(prepared, self.stage(prepared))
        session = RpyProjectSession(self.package, self.session.save_service,
            prepared=prepared, persistence_binding=self.session.persistence_binding)
        self.addCleanup(session.close)
        original = self.destination.read_bytes()
        with mock.patch(_PACKAGE_PORT + '.stage_candidate', side_effect=OSError('fault')):
            self.assertIsNone(session.save().receipt)
        self.assertFalse(prepared.closed)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertTrue(session.save().receipt.durable)
        self.assertTrue(prepared.closed)
        self.assertEqual(session.open_saved_package().workspace.documents[0].segments[0].source, 'Changed source')

    def test_source_drift_at_save_preserves_package_and_dirty_workspace(self):
        self.path.write_bytes(dialogue('"Changed source"'))
        prepared = self.prepare()
        self.apply(prepared, self.stage(prepared))
        original = self.destination.read_bytes()
        changed = dialogue('"New drift"')
        if not self.mutate_source_or_verify_native_pin(
                prepared, lambda: self.path.write_bytes(changed), changed):
            with self.assertRaises(ProjectWorkspaceError):
                prepared.save_reconciled_workspace(self.package, self.session.save_service,
                    self.destination, persistence_binding=self.session.persistence_binding)
            self.assertEqual(self.destination.read_bytes(), original)
            self.assertTrue(self.session.save_service.project_dirty)
            return
        result = prepared.save_reconciled_workspace(self.package, self.session.save_service,
            self.destination, persistence_binding=self.session.persistence_binding)
        self.assertIsNone(result.receipt)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertTrue(self.session.save_service.project_dirty)
        self.assertTrue(prepared.closed)

    def test_cancelled_reconciliation_releases_plan_and_preserves_workspace(self):
        prepared = self.prepare()
        preview = self.stage(prepared)
        before = self.service.workspace
        self.service.discard_reconciliation(preview.operation_id)
        self.service.discard_reconciliation(preview.operation_id)
        with self.assertRaises(ProjectWorkspaceError):
            self.apply(prepared, preview)
        self.assertEqual(self.service.workspace, before)

    def test_update_preserves_custom_display_name_and_rename_updates_default(self):
        from dataclasses import replace
        from project_workspace import ProjectWorkspaceService
        old = self.service.workspace.documents[0]
        self.service = ProjectWorkspaceService(replace(self.service.workspace,
            documents=(replace(old, display_name='Custom title'),)),
            self.service.origin_binding, session_id='named', revision=0)
        self.path = self.path.rename(self.root / 'renamed.rpy')
        mapping = (OriginRenameMapping(old.source_ref, 'renamed.rpy', old.document_id),)
        prepared = self.prepare(rename_mappings=mapping)
        self.assertEqual(prepared.staged.workspace.documents[0].display_name, 'Custom title')
        cold = self.adapter.open_package(self.destination, session_id='cold')
        self.addCleanup(cold.close)
        self.service = cold.workspace_service
        prepared = self.prepare(rename_mappings=mapping)
        self.assertEqual(prepared.staged.workspace.documents[0].display_name, 'renamed.rpy')

    def test_explicit_rename_cannot_replace_rpy_codec_with_plain_text(self):
        cold = self.adapter.open_package(self.destination, session_id='cold')
        self.addCleanup(cold.close)
        self.service = cold.workspace_service
        old = self.service.workspace.documents[0]
        self.path = self.path.rename(self.root / 'renamed.txt')
        with self.assertRaises(ProjectWorkspaceError):
            self.prepare(rename_mappings=(OriginRenameMapping(old.source_ref, 'renamed.txt', old.document_id),))
        self.assertEqual(self.service.workspace.documents[0], old)

    def test_neutral_rebind_preserves_manifest_names_before_adapter_projection(self):
        from project_workspace_intake import prepare_workspace_rebind
        prepared = prepare_workspace_rebind(self.root, (self.path,), self.service.workspace,
            parser_surface=self.adapter.runtime.surface)
        self.addCleanup(prepared.close)
        self.assertEqual(prepared.staged.workspace.documents[0].display_name,
                         self.service.workspace.documents[0].display_name)
        with mock.patch.object(type(self.adapter.runtime.surface), 'open_input', side_effect=AssertionError('reparse')):
            self.assertIs(prepared.revalidate(), prepared.staged)

    def test_final_identity_check_avoids_body_reads_but_full_reproof_stays_strict(self):
        prepared = self.prepare()
        authority_type = type(prepared._source._files[0].authority)
        with mock.patch.object(authority_type, 'read_at', side_effect=AssertionError('body read')):
            self.assertIs(prepared.revalidate_identity(), prepared.staged)
        import project_workspace_intake as intake
        with mock.patch.object(intake, '_snapshot_digest', wraps=intake._snapshot_digest) as digest:
            prepared.revalidate()
            self.assertGreater(digest.call_count, 0)

    def test_final_identity_check_rejects_same_size_edit_without_reading_body(self):
        prepared = self.prepare()
        prepared.revalidate()
        changed = self.raw.replace(b'Source', b'Change')
        if not self.mutate_source_or_verify_native_pin(
                prepared, lambda: self.path.write_bytes(changed), changed):
            return
        authority_type = type(prepared._source._files[0].authority)
        with mock.patch.object(authority_type, 'read_at', side_effect=AssertionError('body read')):
            with self.assertRaises(ProjectWorkspaceError) as caught:
                prepared.revalidate_identity()
        self.assertEqual(caught.exception.code, 'PROJECT.INTAKE.SOURCE_STALE')
        self.assertTrue(prepared.closed)

    def test_final_identity_check_rejects_file_replacement_without_reading_body(self):
        prepared = self.prepare()
        prepared.revalidate()
        replacement = self.root / 'replacement.rpy'
        replacement.write_bytes(self.raw)
        changed = self.mutate_source_or_verify_native_pin(
            prepared, lambda: replacement.replace(self.path), self.raw)
        self.assertFalse(replacement.exists())
        if not changed:
            return
        authority_type = type(prepared._source._files[0].authority)
        with mock.patch.object(authority_type, 'read_at', side_effect=AssertionError('body read')):
            with self.assertRaises(ProjectWorkspaceError) as caught:
                prepared.revalidate_identity()
        self.assertEqual(caught.exception.code, 'PROJECT.INTAKE.SOURCE_STALE')
        self.assertTrue(prepared.closed)

    def test_final_identity_check_rejects_root_replacement_or_native_pin(self):
        prepared = self.prepare()
        prepared.revalidate()
        moved = self.base / 'moved-root'
        if os.name == 'nt':
            # Native retained directory handles prevent replacement of this root.
            with self.assertRaises(PermissionError):
                self.root.rename(moved)
            self.assertIs(prepared.revalidate_identity(), prepared.staged)
            return
        self.root.rename(moved)
        self.root.mkdir()
        (moved / self.path.name).rename(self.path)
        authority_type = type(prepared._source._files[0].authority)
        with mock.patch.object(authority_type, 'read_at', side_effect=AssertionError('body read')):
            with self.assertRaises(ProjectWorkspaceError) as caught:
                prepared.revalidate_identity()
        self.assertEqual(caught.exception.code, 'PROJECT.INTAKE.SOURCE_STALE')
        self.assertTrue(prepared.closed)

    def test_invalid_second_document_reports_ref_and_line_without_body_or_mutation(self):
        second = self.root / 'nested' / 'second.rpy'
        second.parent.mkdir()
        second.write_bytes(self.raw)
        sources = (self.path, second)
        session = self.adapter.prepare_selected(self.root, sources,
            SelectedProjectDocumentsRequest('Multi', 'en', 'zh_Hans'), session_id='multi')
        self.addCleanup(session.close)
        destination = self.base / 'multi.localcat-project'
        self.assertTrue(session.save(destination).receipt.durable)
        cold = self.package.open(destination).create_save_service(session_id='cold', revision=0).workspace_service
        second.write_bytes(b'translate zh_Hans python:\n    secret_payload()\n')
        for service in (session.workspace_service, cold):
            with self.subTest(bound=service.origin_binding is not None):
                before = service.workspace
                with self.assertRaises(RpyProjectSelectionError) as caught:
                    self.adapter.prepare_source_update(service, self.root, sources)
                self.assertEqual(caught.exception.source_ref, 'nested/second.rpy')
                self.assertGreater(caught.exception.diagnostics[0].line_number, 0)
                self.assertNotIn('secret_payload', str(caught.exception))
                self.assertNotIn('secret_payload', repr(caught.exception.diagnostics))
                self.assertEqual(service.workspace, before)

    def test_unmapped_cold_rename_fail_without_mutation(self):
        cold = self.adapter.open_package(self.destination, session_id='cold')
        self.addCleanup(cold.close)
        self.service = cold.workspace_service
        old = self.service.workspace
        self.path = self.path.rename(self.root / 'unknown.rpy')
        with self.assertRaises(ProjectWorkspaceError):
            self.prepare()
        self.assertEqual(self.service.workspace, old)
