"""Original directory selection, verified intake, and first package publication."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

import project_workspace_intake as intake
from parser_composition import ParserApplicationSurface, create_parser_application_surface
from parser_source import CancellationToken
from platform_fs import compose_platform_file_backend
from project_directory_contracts import DirectorySelectionRequest
from project_package import ProjectPackageService
from project_save import SaveJournalState
from project_workspace_contracts import ProjectOriginKind, ProjectWorkspaceError
from project_workspace_discovery import DirectoryDiscoveryError, ProjectDirectoryDiscoveryService
from tests.test_multi_document_cluster2c_project_package import _write_project
from tests.test_project_single_file_profile import _session, _PACKAGE_PORT


class ProjectDirectoryIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'sources'
        self.root.mkdir()
        self.backend = compose_platform_file_backend(self.root)
        self.surface = create_parser_application_surface()
        self.discovery = ProjectDirectoryDiscoveryService(self.backend, self.surface)
        self.addCleanup(self.discovery.close)
        self.request = intake.SelectedProjectDocumentsRequest('Nested project', 'en', 'zh-CN')
        self.package = ProjectPackageService()

    def text(self, relative, payload=b'Hello\n'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def select(self, *refs):
        preview = self.discovery.preview(self.root)
        self.assertTrue(preview.complete)
        by_ref = {entry.source_ref: entry.entry_id for entry in preview.entries if entry.selectable}
        selected = self.discovery.select(DirectorySelectionRequest(
            preview.preview_id, preview.generation, tuple(by_ref[ref] for ref in refs)))
        return self.discovery.revalidate(selected)

    def prepare(self, selection, **kwargs):
        result = intake.prepare_directory_project_documents(
            selection, self.request, parser_surface=self.surface, file_system=self.backend, **kwargs)
        self.addCleanup(result.close)
        return result

    def test_nested_single_and_ordered_multiple_publish_inside_selected_root_and_cold_open(self):
        text = self.text('b/deep/intro.txt')
        json = _write_project(self.root, 'a/intro.json', source='JSON source', target='旧译')
        (self.root / 'unselected.json').write_bytes(b'invalid unselected JSON')
        for refs in (('b/deep/intro.txt',), ('a/intro.json',), ('b/deep/intro.txt', 'a/intro.json')):
            with self.subTest(refs=refs):
                selected = self.select(*refs)
                prepared = self.prepare(selected)
                staged = prepared.staged
                workspace = staged.workspace
                self.assertEqual(staged.origin_binding.absolute_root, str(self.root))
                self.assertEqual(tuple(d.source_ref for d in workspace.documents), refs)
                self.assertEqual(tuple(d.order for d in workspace.documents), tuple(range(len(refs))))
                self.assertIs(workspace.origin.kind, ProjectOriginKind.SINGLE_FILE if len(refs) == 1 else ProjectOriginKind.DIRECTORY)
                self.assertFalse(staged.source_write_back_authorized)
                save = _session(staged)
                target = self.root / ('single-' + str(len(refs)) + '.localcat-project')
                # Each independent intake uses a new destination; old output is not selected.
                target = target.with_name(refs[0].split('/')[0] + target.name)
                result = prepared.save_workspace(self.package, save, target)
                self.assertIs(result.save_report.journal_state, SaveJournalState.COMMITTED)
                self.assertTrue(result.receipt.durable)
                self.assertTrue(prepared.closed)
                self.assertFalse(save.project_dirty)
                selected.check_current()  # intake never closes the caller's discovery lifecycle
                self.discovery.cancel()
                offline = self.base / target.name
                target.rename(offline)
                moved = self.base / 'offline-source'
                self.root.rename(moved)
                opened = self.package.open(offline)
                self.assertEqual(opened.workspace, workspace)
                self.assertIsNone(opened.create_save_service(session_id='cold', revision=0).workspace_service.origin_binding)
                moved.rename(self.root)
        self.assertEqual(text.read_bytes(), b'Hello\n')
        self.assertIn('旧译', json.read_text())

    def test_only_selected_bodies_are_read_and_all_inputs_must_verify(self):
        selected_path = self.text('selected/deep/chapter.txt')
        self.text('unselected.json', b'invalid JSON')
        selection = self.select('selected/deep/chapter.txt')
        seen = []
        original_open = ParserApplicationSurface.open_input

        def record(surface, reference, *args, **kwargs):
            seen.append(reference.selected_path)
            self.assertEqual(reference.selected_path, str(selected_path))
            return original_open(surface, reference, *args, **kwargs)

        with mock.patch.object(ParserApplicationSurface, 'open_input', record), \
             mock.patch.object(self.backend, 'observe_children', side_effect=AssertionError('intake cannot rescan')):
            prepared = self.prepare(selection)
        self.assertEqual(seen, [str(selected_path)])
        prepared.close()
        both = self.select('selected/deep/chapter.txt', 'unselected.json')
        with self.assertRaises(intake.SelectedProjectDocumentsError) as rejected:
            self.prepare(both)
        self.assertEqual(rejected.exception.document_order, 1)
        self.assertFalse(tuple(self.root.glob('*.localcat-project')))

    def test_empty_foreign_stale_and_revoked_choices_never_open_a_body(self):
        self.text('nested/file.txt')
        preview = self.discovery.preview(self.root)
        entry = next(entry for entry in preview.entries if entry.selectable)
        with mock.patch.object(ParserApplicationSurface, 'open_input') as opened:
            for ids in ((), ('foreign',)):
                with self.subTest(ids=ids), self.assertRaises(DirectoryDiscoveryError):
                    self.discovery.select(DirectorySelectionRequest(preview.preview_id, preview.generation, ids))
            issued = self.discovery.select(DirectorySelectionRequest(preview.preview_id, preview.generation, (entry.entry_id,)))
            with self.assertRaises(DirectoryDiscoveryError):
                self.discovery.revalidate(replace(issued))
            retained = self.discovery.revalidate(issued)
            self.discovery.preview(self.root)
            with self.assertRaises((DirectoryDiscoveryError, ProjectWorkspaceError)):
                self.prepare(retained)
            retained = self.select('nested/file.txt')
            self.discovery.cancel()
            with self.assertRaises((DirectoryDiscoveryError, ProjectWorkspaceError)):
                self.prepare(retained)
            opened.assert_not_called()

    def test_preview_snapshot_drift_and_new_hardlink_alias_are_rejected_before_parse(self):
        path = self.text('deep/file.txt')
        for fault in ('content', 'alias'):
            with self.subTest(fault=fault):
                selection = self.select('deep/file.txt')
                if fault == 'content':
                    path.write_bytes(b'Changed after preview\n')
                else:
                    os.link(path, path.parent / 'alias.txt')
                with mock.patch.object(ParserApplicationSurface, 'open_input') as opened:
                    with self.assertRaises((ProjectWorkspaceError, DirectoryDiscoveryError)):
                        self.prepare(selection)
                    opened.assert_not_called()
                if fault == 'alias':
                    (path.parent / 'alias.txt').unlink()

    def test_original_root_or_selected_file_replacement_cannot_cross_intake(self):
        for fault in ('root', 'file'):
            with self.subTest(fault=fault):
                source = self.text('deep/file.txt')
                retained = self.select('deep/file.txt')
                if fault == 'root':
                    moved = self.base / 'moved-original'
                    if os.name == 'nt':
                        # Native observation handles pin the original directory name.
                        with self.assertRaises(PermissionError):
                            self.root.rename(moved)
                        self.prepare(retained).close()
                        continue
                    self.root.rename(moved)
                    self.root.mkdir()
                    (moved / 'deep').rename(self.root / 'deep')
                else:
                    replacement = self.root / 'replacement.txt'
                    replacement.write_bytes(source.read_bytes())
                    os.replace(replacement, source)
                with mock.patch.object(ParserApplicationSurface, 'open_input') as opened:
                    with self.assertRaises((DirectoryDiscoveryError, ProjectWorkspaceError)):
                        self.prepare(retained)
                    opened.assert_not_called()

    def test_cancel_before_or_during_parse_releases_inputs_without_candidate(self):
        self.text('deep/file.txt')
        for phase in ('before', 'during'):
            with self.subTest(phase=phase):
                selected = self.select('deep/file.txt')
                cancellation = CancellationToken()
                if phase == 'before':
                    cancellation.cancel()
                materialize = ParserApplicationSurface.materialize_handoff

                def revoke(surface, opened):
                    result = materialize(surface, opened)
                    self.discovery.cancel()
                    return result

                original = revoke if phase == 'during' else materialize
                retained_inputs = []

                def track(method):
                    def call(*args):
                        result = method(*args)
                        retained_inputs.append(result)
                        return result
                    return call

                with mock.patch.object(ParserApplicationSurface, 'materialize_handoff', original), \
                     mock.patch.object(self.backend, 'bind_root', track(self.backend.bind_root)), \
                     mock.patch.object(self.backend, 'open_regular', track(self.backend.open_regular)):
                    with self.assertRaises((ProjectWorkspaceError, DirectoryDiscoveryError, ValueError)):
                        self.prepare(selected, cancellation=cancellation)
                self.assertTrue(all(item.closed for item in retained_inputs))
                if phase == 'during':
                    self.assertTrue(retained_inputs)
                self.assertFalse(tuple(self.root.glob('*.localcat-project')))

    def test_existing_reproof_only_source_lease_remains_compatible(self):
        self.text('deep/file.txt')
        prepared = self.prepare(self.select('deep/file.txt'))
        save = _session(prepared.staged)

        checks = []

        class ReproofOnly:
            def reprove(self):
                checks.append(True)

        result = self.package.save_workspace(save, self.base / 'legacy-lease.localcat-project',
                                             source_reproof=ReproofOnly())
        self.assertTrue(result.receipt.durable)
        self.assertFalse(save.project_dirty)
        self.assertTrue(checks)

    def test_revocation_before_publication_prevents_save_and_clears_candidate(self):
        self.text('deep/file.txt')
        prepared = self.prepare(self.select('deep/file.txt'))
        save = _session(prepared.staged)
        target = self.base / 'not-published.localcat-project'
        import project_package
        port_type = getattr(project_package, _PACKAGE_PORT.rsplit('.', 1)[1])
        validate = port_type.validate_candidate

        def revoke(port, handle):
            validate(port, handle)
            self.discovery.cancel()

        with mock.patch.object(port_type, 'validate_candidate', revoke):
            result = prepared.save_workspace(self.package, save, target)
        self.assertIsNone(result.receipt)
        self.assertIsNone(save.saved_workspace_snapshot)
        self.assertTrue(save.project_dirty)
        self.assertTrue(prepared.closed)
        self.assertFalse(target.exists())

    def test_raw_unicode_spelling_is_used_for_intake_and_initial_package(self):
        self.text('cafe\u0301/deep/file.txt')
        selection = self.select('caf\u00e9/deep/file.txt')
        raw = selection.files[0].raw_components
        seen = []
        original_open = self.backend.open_regular

        def record(root, relative):
            seen.append(relative.parts)
            self.assertEqual(relative.parts, raw)
            return original_open(root, relative)

        native_open = os.open

        def spelling_sensitive_open(name, *args, **kwargs):
            # Exercise a spelling-sensitive filesystem even on normalizing APFS.
            if name == 'caf\u00e9':
                raise FileNotFoundError('only the original decomposed name exists')
            return native_open(name, *args, **kwargs)

        with mock.patch.object(self.backend, 'open_regular', record):
            prepared = self.prepare(selection)
            with mock.patch('os.open', spelling_sensitive_open):
                result = prepared.save_workspace(self.package, _session(prepared.staged), self.base / 'unicode.localcat-project')
        self.assertTrue(result.receipt.durable)
        self.assertTrue(seen)
        self.assertEqual(prepared.staged.workspace.documents[0].source_ref, 'caf\u00e9/deep/file.txt')
        self.discovery.close()
        self.root.rename(self.base / 'gone')
        opened = self.package.open(self.base / 'unicode.localcat-project')
        with opened.open_member(opened.manifest.documents[0].source_member.path) as member:
            self.assertEqual(member.read(), b'Hello\n')


if __name__ == '__main__':
    unittest.main()
