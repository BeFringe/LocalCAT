from dataclasses import replace
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import project_workspace_intake as intake
from project_package import ProjectPackageBlobSource, ProjectPackageService
from project_save import SaveJournalState
from project_workspace_contracts import ProjectWorkspaceError, MAX_CODEC_PRIVATE_MEMBER_BYTES
from parser_source import CancellationToken
import parser_contracts as contracts
from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from tests.test_parser_source_handoff import NeutralFixture
from tests.test_project_single_file_profile import _session, _PACKAGE_PORT


class ProjectPrivateHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'sources'
        self.path = self.root / 'nested' / 'chapter.neutral'
        self.path.parent.mkdir(parents=True)
        self.original = b'original opaque source\r\n'
        self.path.write_bytes(self.original)
        self.fixture = NeutralFixture()
        self.package = ProjectPackageService()
        self.target = self.base / 'saved.localcat-project'

    def prepare(self, cancellation=None):
        prepared = intake.prepare_selected_project_documents(
            self.root, (self.path,), intake.SelectedProjectDocumentsRequest('Neutral project', 'en', 'zh'),
            parser_surface=self.fixture.surface(), cancellation=cancellation,
        )
        self.addCleanup(prepared.close)
        return prepared

    def edited(self, prepared):
        save = _session(prepared.staged)
        identity = save.workspace_service.workspace.documents[0].segment_identities[0]
        save.workspace_service.update_segment_edit(
            identity, target='current edited target', confirmed=True, session_id='single', base_revision=0,
        )
        return save

    def test_first_package_cold_reopen_preserves_exact_bytes_identity_and_edits_without_codec(self):
        prepared = self.prepare()
        self.assertFalse(prepared.closed)
        self.assertFalse(prepared.staged.durable)
        self.assertEqual(prepared.staged.workspace.documents[0].source_ref, 'nested/chapter.neutral')
        self.assertEqual(prepared.staged.workspace.origin.profile_version, 'explicit-single-file-v1')
        save = self.edited(prepared)
        result = prepared.save_workspace(self.package, save, self.target)
        self.assertIs(result.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertTrue(result.receipt.durable)
        self.assertTrue(prepared.closed)
        self.assertFalse(save.project_dirty)
        self.root.rename(self.base / 'removed-origin')
        opened = self.package.open(self.target)
        document = opened.workspace.documents[0]
        self.assertEqual(document.codec_identity, self.fixture.descriptor.identity)
        self.assertEqual(tuple(s.local_segment_id for s in document.source_segments), ('slot-b', 'slot-a'))
        self.assertEqual((document.editing_overlay[0].target, document.editing_overlay[0].confirmed),
                         ('current edited target', True))
        entry = opened.manifest.documents[0]
        with opened.open_member(entry.source_member.path) as source:
            self.assertEqual(source.read(), self.original)
        with opened.open_member(entry.codec_private_member.path, codec_identity=self.fixture.descriptor.identity) as private:
            self.assertEqual(private.read(), self.fixture.payload)
        cold = opened.create_save_service(session_id='cold', revision=0)
        resave = self.package.save_workspace(cold, self.target, persistence_binding=opened.persistence_binding)
        self.assertTrue(resave.receipt.durable)
        self.assertEqual(self.package.open(self.target).workspace, opened.workspace)

    def test_first_destination_failure_preserves_holder_for_retry_and_edited_overlay(self):
        prepared = self.prepare()
        save = self.edited(prepared)
        with mock.patch(_PACKAGE_PORT + '.validate_candidate', side_effect=OSError('destination failure')):
            failed = prepared.save_workspace(self.package, save, self.target)
        self.assertIsNone(failed.receipt)
        self.assertIsNone(save.saved_workspace_snapshot)
        self.assertTrue(save.project_dirty)
        self.assertFalse(prepared.closed)
        self.assertFalse(self.target.exists())
        retry = prepared.save_workspace(self.package, save, self.base / 'retry.localcat-project')
        self.assertTrue(retry.receipt.durable)
        self.assertTrue(prepared.closed)
        self.assertEqual(save.saved_workspace_snapshot.documents[0].editing_overlay[0].target, 'current edited target')

    def test_cancel_or_source_drift_rejects_and_releases_without_false_baseline(self):
        for fault in ('cancel', 'content'):
            if os.name == 'nt' and fault == 'content':
                # Native retained handles deny this external mutation.
                continue
            with self.subTest(fault=fault):
                self.path.write_bytes(self.original)
                cancellation = CancellationToken()
                prepared = self.prepare(cancellation)
                save = self.edited(prepared)
                if fault == 'cancel': cancellation.cancel()
                else: self.path.write_bytes(b'changed')
                result = prepared.save_workspace(self.package, save, self.target)
                self.assertIsNone(result.receipt)
                self.assertIsNone(save.saved_workspace_snapshot)
                self.assertTrue(save.project_dirty)
                self.assertTrue(prepared.closed)
                self.assertFalse(self.target.exists())

    @unittest.skipIf(os.name == 'nt', 'native retained Windows handles prevent source replacement')
    def test_source_swap_before_publication_is_rejected_by_retained_reproof(self):
        prepared = self.prepare()
        save = self.edited(prepared)
        import project_package
        port_type = getattr(project_package, _PACKAGE_PORT.rsplit('.', 1)[1])
        validate = port_type.validate_candidate
        def swap_after_candidate(port, handle):
            validate(port, handle)
            replacement = self.root / 'replacement'
            replacement.write_bytes(self.original)
            os.replace(replacement, self.path)
        with mock.patch.object(port_type, 'validate_candidate', swap_after_candidate):
            result = prepared.save_workspace(self.package, save, self.target)
        self.assertIsNone(result.receipt)
        self.assertTrue(prepared.closed)
        self.assertIsNone(save.saved_workspace_snapshot)
        self.assertFalse(self.target.exists())

    def test_close_is_idempotent_and_cannot_save_released_candidate(self):
        prepared = self.prepare()
        save = self.edited(prepared)
        prepared.close()
        prepared.close()
        self.assertTrue(prepared.closed)
        with self.assertRaises(ProjectWorkspaceError):
            prepared.save_workspace(self.package, save, self.target)
        self.assertFalse(self.target.exists())

    def test_unconfigured_codec_and_partial_private_never_publish_candidate(self):
        with self.assertRaises(ProjectWorkspaceError):
            intake.prepare_selected_project_documents(
                self.root, (self.path,), intake.SelectedProjectDocumentsRequest('Neutral', 'en', 'zh'),
            )
        self.fixture.consume = False
        with self.assertRaises(ProjectWorkspaceError):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_bytes_blob_is_data_only_bounded_and_digest_checked(self):
        prepared = self.prepare()
        document = prepared.staged.workspace.documents[0]
        private = document.codec_private_member
        args = dict(document_id=document.document_id, member_path=private.member_path,
                    payload=self.fixture.payload, expected_sha256=private.sha256,
                    expected_byte_count=private.byte_count)
        blob = ProjectPackageBlobSource.from_bytes(**args)
        with blob._blob().open() as stream:
            self.assertEqual(stream.read(), self.fixture.payload)
        for changes in ({'expected_sha256': '0' * 64}, {'expected_byte_count': 0},
                        {'payload': bytearray(self.fixture.payload)},
                        {'expected_byte_count': MAX_CODEC_PRIVATE_MEMBER_BYTES + 1}):
            with self.subTest(changes=changes), self.assertRaises((ProjectWorkspaceError, TypeError)):
                ProjectPackageBlobSource.from_bytes(**(args | changes))

    def test_handles_retained_until_publish_then_all_closed(self):
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
            prepared = intake.prepare_selected_project_documents(
                self.root, (self.path,), intake.SelectedProjectDocumentsRequest('Neutral', 'en', 'zh'),
                parser_surface=self.fixture.surface(), file_system=backend,
            )
            self.addCleanup(prepared.close)
            self.assertEqual(sum(not item.closed for item in authorities), 2)
            saved = prepared.save_workspace(self.package, self.edited(prepared), self.target)
        self.assertTrue(saved.receipt.durable)
        self.assertTrue(all(item.closed for item in authorities))

    def test_parse_failure_and_snapshot_failure_release_every_bound_authority(self):
        for fault in ('private', 'snapshot'):
            with self.subTest(fault=fault):
                backend = compose_platform_file_backend(self.root)
                roots, files = [], []
                bind, open_regular = backend.bind_root, backend.open_regular
                def root(*args):
                    result = bind(*args)
                    roots.append(result)
                    return result
                stack = ExitStack()
                self.addCleanup(stack.close)
                def regular(*args):
                    result = open_regular(*args)
                    files.append(result)
                    if fault == 'snapshot':
                        # Fail the exact first file after its handle is acquired.
                        stack.enter_context(mock.patch.object(
                            type(result), 'snapshot', side_effect=PlatformFileError(
                                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
                            ),
                        ))
                    return result
                self.fixture.consume = False
                with mock.patch.object(backend, 'bind_root', root), \
                     mock.patch.object(backend, 'open_regular', regular):
                    with self.assertRaises(ProjectWorkspaceError):
                        intake.prepare_selected_project_documents(
                            self.root, (self.path,), intake.SelectedProjectDocumentsRequest('Neutral', 'en', 'zh'),
                            parser_surface=self.fixture.surface(), file_system=backend,
                        )
                self.assertTrue(roots and files)
                self.assertTrue(all(item.closed for item in (*roots, *files)))
                stack.close()

    def test_intake_failure_preserves_safe_parser_locations(self):
        self.fixture.reader_issues = (contracts.ParseIssue(
            'PARSER.SYNTAX.MALFORMED', contracts.IssueSeverity.FATAL, 'secret source text',
            line_number=3, byte_offset=8,
        ),)
        with self.assertRaises(intake.SelectedProjectDocumentsError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.parser_code, 'PARSER.SYNTAX.MALFORMED')
        self.assertEqual(caught.exception.document_order, 0)
        self.assertEqual(caught.exception.diagnostics[0].line_number, 3)
        self.assertNotIn('secret', repr(caught.exception.diagnostics))

    def test_cancel_after_publication_rolls_back_without_false_success(self):
        cancellation = CancellationToken()
        prepared = self.prepare(cancellation)
        save = self.edited(prepared)
        import project_package
        port_type = getattr(project_package, _PACKAGE_PORT.rsplit('.', 1)[1])
        publish = port_type.publish_candidate
        def cancel_after_publish(port, handle):
            publish(port, handle)
            cancellation.cancel()
        with mock.patch.object(port_type, 'publish_candidate', cancel_after_publish):
            result = prepared.save_workspace(self.package, save, self.target)
        self.assertIsNone(result.receipt)
        self.assertTrue(prepared.closed)
        self.assertIsNone(save.saved_workspace_snapshot)
        self.assertTrue(save.project_dirty)
        self.assertFalse(self.target.exists())

    def test_foreign_workspace_cannot_consume_retained_source(self):
        prepared = self.prepare()
        other = self.prepare()
        with self.assertRaises(ProjectWorkspaceError):
            prepared.save_workspace(self.package, self.edited(other), self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(prepared.closed)

    @unittest.skipIf(os.name == 'nt', 'native retained Windows handles deny external source mutation')
    def test_source_drift_during_handoff_discards_candidate_and_retained_handles(self):
        self.fixture.during = lambda request: self.path.write_bytes(b'changed during private preparation')
        with self.assertRaises(ProjectWorkspaceError) as rejected:
            self.prepare()
        self.assertEqual(rejected.exception.code, 'PROJECT.INTAKE.SOURCE_STALE')
        self.assertTrue(self.fixture.last_request.source.closed)
        self.assertFalse(self.target.exists())

    def test_unavailable_root_backend_is_mapped_to_project_error(self):
        with mock.patch('platform_fs.compose_platform_file_backend', side_effect=PlatformFileError(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
        )):
            with self.assertRaises(ProjectWorkspaceError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'PROJECT.INTAKE.SOURCE_UNSAFE')
        self.assertEqual(str(caught.exception), 'PROJECT.INTAKE.SOURCE_UNSAFE')


if __name__ == '__main__':
    unittest.main()
