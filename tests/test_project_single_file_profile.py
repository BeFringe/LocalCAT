"""Direct single-file intake and real ProjectPackage persistence contracts."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from editor_project_workspace_adapter import load_legacy_single_json_workspace
from parser_composition import OpenedParserInput, ParserApplicationSurface
from platform_fs import compose_platform_file_backend
from project_package import ProjectPackageService
from project_save import (
    DocumentSourceWriteStatus,
    OriginWriteState,
    ProjectSaveService,
    SaveJournalState,
)
from project_workspace import ProjectWorkspaceService, workspace_content_digest_v1
from project_workspace_contracts import ProjectOriginKind, ProjectPersistenceKind
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)
from tests.test_multi_document_cluster2c_project_package import _write_project


_PACKAGE_PORT = (
    "project_package._WindowsProjectPackagePersistencePort"
    if os.name == "nt" else "project_package._PosixProjectPackagePersistencePort"
)


def _stage(root: Path, *selected: Path):
    return stage_selected_project_documents(
        root, selected, SelectedProjectDocumentsRequest("Single project", "en", "zh-CN")
    )


def _session(staged):
    workspace = ProjectWorkspaceService(
        staged.workspace, staged.origin_binding, session_id="single", revision=0,
    )
    return ProjectSaveService(workspace, baseline=None)


def _source(root: Path, suffix: str) -> Path:
    if suffix == "json":
        return _write_project(root, "script/Chapters/intro.json", source="Hello", target="旧译")
    path = root / "script/Chapters/intro.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Hello\n")
    return path


def _rewrite_manifest(source: Path, destination: Path, **changes) -> None:
    with zipfile.ZipFile(source) as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for info, payload in members:
            if info.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest.update(changes)
                payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            archive.writestr(info, payload)


class SingleFileProfileTests(unittest.TestCase):
    def test_zero_single_and_multiple_keep_separate_cardinality_and_original_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = _source(root, "json")
            second = _source(root, "txt")
            with mock.patch.object(ParserApplicationSurface, "open_input") as opened:
                with self.assertRaises(ProjectWorkspaceError):
                    _stage(root)
                opened.assert_not_called()
            one = _stage(root, first)
            self.assertEqual(one.workspace.origin.kind, ProjectOriginKind.SINGLE_FILE)
            self.assertEqual(one.workspace.origin.profile_version, "explicit-single-file-v1")
            self.assertEqual(one.origin_binding.profile_version, "explicit-single-file-v1")
            self.assertEqual(one.origin_binding.absolute_root, str(root))
            self.assertEqual(one.workspace.documents[0].source_ref, "script/Chapters/intro.json")
            self.assertIs(one.workspace.persistence_kind, ProjectPersistenceKind.PROJECT_PACKAGE)
            self.assertFalse(one.durable)
            self.assertFalse(one.source_write_back_authorized)
            multiple = _stage(root, second, first)
            self.assertEqual(multiple.workspace.origin.kind, ProjectOriginKind.DIRECTORY)
            self.assertEqual(multiple.workspace.origin.profile_version, "explicit-selected-files-v1")
            self.assertEqual(tuple(d.source_ref for d in multiple.workspace.documents),
                             ("script/Chapters/intro.txt", "script/Chapters/intro.json"))
            for invalid in (
                {"profile_version": "explicit-selected-files-v1"},
                {"profile_version": "unknown-v1"},
                {"documents": ()},
                {"documents": multiple.origin_binding.documents},
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ProjectWorkspaceError):
                    replace(one.origin_binding, **invalid)
            with self.assertRaises(ProjectWorkspaceError):
                replace(multiple.origin_binding, documents=(multiple.origin_binding.documents[0],))

    def test_single_selection_neither_enumerates_nor_reads_unselected_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selected = _source(root, "txt")
            (root / "ignored.json").write_bytes(b"not JSON")
            references = []
            original_open = ParserApplicationSurface.open_input

            def recording_open(surface, reference, *args, **kwargs):
                references.append(reference.selected_path)
                self.assertEqual(reference.selected_path, str(selected))
                return original_open(surface, reference, *args, **kwargs)

            with (
                mock.patch.object(ParserApplicationSurface, "open_input", recording_open),
                mock.patch("os.scandir", side_effect=AssertionError("no enumeration")),
                mock.patch.object(Path, "iterdir", side_effect=AssertionError("no enumeration")),
                mock.patch.object(Path, "rglob", side_effect=AssertionError("no enumeration")),
            ):
                staged = _stage(root, selected)
            self.assertEqual(references, [str(selected)])
            self.assertEqual(len(staged.workspace.documents), 1)
            self.assertEqual((root / "ignored.json").read_bytes(), b"not JSON")

    def test_real_single_json_and_txt_save_cold_reopen_edit_and_resave_without_source_root(self) -> None:
        for suffix in ("json", "txt"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "source"
                root.mkdir()
                selected = _source(root, suffix)
                source_bytes = selected.read_bytes()
                staged = _stage(root, selected)
                save = _session(staged)
                target = base / "saved.localcat-project"
                result = ProjectPackageService().save_workspace(save, target)
                self.assertIs(result.save_report.journal_state, SaveJournalState.COMMITTED)
                self.assertTrue(result.receipt.durable)
                self.assertFalse(save.project_dirty)
                moved = base / "moved-source"
                root.rename(moved)

                opened = ProjectPackageService().open(target)
                cold = opened.create_save_service(session_id="cold-single", revision=0)
                self.assertIsNone(cold.workspace_service.origin_binding)
                self.assertEqual(opened.workspace.project_id, staged.workspace.project_id)
                self.assertEqual(opened.workspace.documents[0].document_id,
                                 staged.workspace.documents[0].document_id)
                self.assertEqual(opened.workspace.documents[0].segment_identities,
                                 staged.workspace.documents[0].segment_identities)
                self.assertEqual(opened.workspace.documents[0].source_ref, f"script/Chapters/intro.{suffix}")
                self.assertEqual(opened.workspace.documents[0].editing_overlay,
                                 save.saved_workspace_snapshot.documents[0].editing_overlay)
                cold.workspace_service.update_segment_edit(
                    opened.workspace.documents[0].segment_identities[0],
                    target="新译文", confirmed=True, session_id="cold-single", base_revision=0,
                )
                self.assertTrue(cold.project_dirty)
                saved = ProjectPackageService().save_workspace(
                    cold, target, persistence_binding=opened.persistence_binding,
                )
                self.assertIs(saved.save_report.journal_state, SaveJournalState.COMMITTED)
                self.assertTrue(saved.receipt.durable)
                self.assertFalse(cold.project_dirty)
                reopened = ProjectPackageService().open(target)
                overlay = reopened.workspace.documents[0].editing_overlay[0]
                self.assertEqual((overlay.target, overlay.confirmed), ("新译文", True))
                self.assertEqual(reopened.workspace.project_id, staged.workspace.project_id)
                self.assertEqual(reopened.workspace.documents[0].segment_identities,
                                 staged.workspace.documents[0].segment_identities)
                with reopened.open_member(reopened.manifest.documents[0].source_member.path) as member:
                    self.assertEqual(member.read(), source_bytes)
                self.assertEqual((moved / f"script/Chapters/intro.{suffix}").read_bytes(), source_bytes)

    def test_single_package_does_not_acquire_legacy_writer_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selected = _source(root, "json")
            original = selected.read_bytes()
            save = _session(_stage(root, selected))
            document = save.workspace_service.workspace.documents[0]
            self.assertTrue(document.writer_capability_snapshot.canonical_write)
            self.assertIs(save.origin_write_state[0].state, OriginWriteState.UNSUPPORTED)
            writer = mock.Mock()
            self.assertIs(save.source_write_back_status(document.document_id, writer).status,
                          DocumentSourceWriteStatus.UNSUPPORTED)
            writer.prepare.assert_not_called()
            legacy = load_legacy_single_json_workspace(selected)
            self.assertIs(legacy.persistence_kind, ProjectPersistenceKind.LEGACY_SINGLE_JSON)
            self.assertEqual(legacy.origin.profile_version, "localcat-json-v1")
            self.assertEqual(selected.read_bytes(), original)

    def test_binding_cannot_be_attached_to_an_incompatible_origin_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            staged = _stage(root, _source(root, "txt"))
            for origin in (
                replace(staged.workspace.origin, profile_version="localcat-json-v1"),
                replace(staged.workspace.origin, kind=ProjectOriginKind.DIRECTORY),
            ):
                with self.subTest(origin=origin), self.assertRaises(ProjectWorkspaceError):
                    ProjectWorkspaceService(replace(staged.workspace, origin=origin),
                                            staged.origin_binding, session_id="forged", revision=0)

    def test_real_decoder_rejects_unknown_workbook_mismatched_and_wrong_cardinality_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            one = _source(root, "json")
            two = _source(root, "txt")
            package = ProjectPackageService()
            single = root / "single.localcat-project"
            multi = root / "multi.localcat-project"
            package.save_workspace(_session(_stage(root, one)), single)
            package.save_workspace(_session(_stage(root, one, two)), multi)
            invalid = (
                (single, "single_file", "unknown-v1", "PROJECT.PACKAGE.FORMAT_UNSUPPORTED"),
                (single, "workbook", "workbook-v1", "PROJECT.PACKAGE.FORMAT_UNSUPPORTED"),
                (single, "directory", "explicit-single-file-v1", "PROJECT.PACKAGE.FORMAT_UNSUPPORTED"),
                (multi, "single_file", "explicit-selected-files-v1", "PROJECT.PACKAGE.FORMAT_UNSUPPORTED"),
                (single, "directory", "explicit-selected-files-v1", "PROJECT.PACKAGE.MANIFEST_INVALID"),
                (multi, "single_file", "explicit-single-file-v1", "PROJECT.PACKAGE.MANIFEST_INVALID"),
            )
            for index, (source, kind, profile, code) in enumerate(invalid):
                with self.subTest(kind=kind, profile=profile, source=source.name):
                    hostile = root / f"hostile-{index}.localcat-project"
                    _rewrite_manifest(source, hostile, origin_kind=kind, origin_profile=profile)
                    with self.assertRaises(ProjectWorkspaceError) as rejected:
                        package.open(hostile)
                    self.assertEqual(rejected.exception.code, code)
                    self.assertEqual(str(rejected.exception), code)

    def test_existing_single_json_package_profile_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "new.localcat-project"
            package = ProjectPackageService()
            package.save_workspace(_session(_stage(root, _source(root, "json"))), source)
            opened = package.open(source)
            compatible = replace(opened.workspace, origin=replace(
                opened.workspace.origin, profile_version="localcat-json-v1",
            ))
            target = root / "existing.localcat-project"
            _rewrite_manifest(source, target, origin_profile="localcat-json-v1",
                              workspace_content_digest=workspace_content_digest_v1(compatible))
            self.assertEqual(package.open(target).workspace, compatible)

    def test_first_save_fault_does_not_issue_receipt_or_install_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selected = _source(root, "txt")
            original = selected.read_bytes()
            save = _session(_stage(root, selected))
            target = root / "new.localcat-project"
            with mock.patch(_PACKAGE_PORT + ".validate_candidate", side_effect=OSError("fault")) as failed:
                result = ProjectPackageService().save_workspace(save, target)
            failed.assert_called_once()
            self.assertIsNone(result.receipt)
            self.assertIsNone(result.persistence_binding)
            self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertFalse(target.exists())
            self.assertIsNone(save.saved_workspace_snapshot)
            self.assertTrue(save.project_dirty)
            self.assertEqual(selected.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "Windows reopens fresh rooted bytes without historical FileId authority")
    def test_source_or_root_replacement_before_first_save_does_not_publish(self) -> None:
        for changed in ("source", "root"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "source"
                root.mkdir()
                selected = _source(root, "txt")
                original = selected.read_bytes()
                save = _session(_stage(root, selected))
                if changed == "root":
                    root.rename(base / "previous-source")
                    root.mkdir()
                    _source(root, "txt")
                else:
                    replacement = root / "replacement.txt"
                    replacement.write_bytes(original)
                    os.replace(replacement, selected)
                target = base / "not-published.localcat-project"
                result = ProjectPackageService().save_workspace(save, target)
                self.assertIsNone(result.receipt)
                self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
                self.assertFalse(target.exists())
                self.assertIsNone(save.saved_workspace_snapshot)
                self.assertTrue(save.project_dirty)
                self.assertEqual(selected.read_bytes(), original)

    def test_source_content_drift_before_first_save_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selected = _source(root, "txt")
            save = _session(_stage(root, selected))
            changed_source = b"Other\n"
            selected.write_bytes(changed_source)
            target = root / "not-published.localcat-project"
            result = ProjectPackageService().save_workspace(save, target)
            self.assertIsNone(result.receipt)
            self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertFalse(target.exists())
            self.assertIsNone(save.saved_workspace_snapshot)
            self.assertTrue(save.project_dirty)
            self.assertEqual(selected.read_bytes(), changed_source)

    def test_resave_fault_preserves_real_last_known_good_package_and_dirty_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            save = _session(_stage(root, _source(root, "txt")))
            target = root / "existing.localcat-project"
            package = ProjectPackageService()
            first = package.save_workspace(save, target)
            old_bytes, old_baseline = target.read_bytes(), save.saved_workspace_snapshot
            save.workspace_service.update_segment_edit(
                save.workspace_service.workspace.documents[0].segment_identities[0],
                target="Unpublished", confirmed=False, session_id="single", base_revision=0,
            )
            with mock.patch(_PACKAGE_PORT + ".validate_candidate", side_effect=OSError("fault")) as failed:
                result = package.save_workspace(save, target, persistence_binding=first.persistence_binding)
            failed.assert_called_once()
            self.assertIsNone(result.receipt)
            self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertEqual(target.read_bytes(), old_bytes)
            self.assertEqual(save.saved_workspace_snapshot, old_baseline)
            self.assertTrue(save.project_dirty)
            self.assertEqual(package.open(target).workspace, old_baseline)

    @unittest.skipIf(os.name == "nt", "Windows retained-handle mutation denial is covered natively")
    def test_source_or_root_swap_during_single_intake_is_rejected(self) -> None:
        for fault in ("source", "root"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root = base / "source"
                root.mkdir()
                selected = _source(root, "txt")
                materialize = OpenedParserInput.materialize

                def swap_after_parse(opened):
                    result = materialize(opened)
                    if fault == "source":
                        replacement = root / "replacement.txt"
                        replacement.write_bytes(selected.read_bytes())
                        os.replace(replacement, selected)
                    else:
                        root.rename(base / "moved")
                        root.mkdir()
                        _source(root, "txt")
                    return result

                with mock.patch.object(OpenedParserInput, "materialize", swap_after_parse):
                    with self.assertRaises(ProjectWorkspaceError) as rejected:
                        _stage(root, selected)
                self.assertIn(rejected.exception.code,
                              {"PROJECT.INTAKE.SOURCE_STALE", "PROJECT.INTAKE.SOURCE_UNSAFE"})

    @unittest.skipIf(os.name == "nt", "Windows root replacement is prevented by retained handles")
    def test_final_root_swap_with_same_selected_file_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "source"
            root.mkdir()
            selected = _source(root, "txt")
            source_identity = (selected.stat().st_dev, selected.stat().st_ino)
            initial_root_identity = (root.stat().st_dev, root.stat().st_ino)
            backend = compose_platform_file_backend(root)
            bind_root = backend._bind_root
            calls = 0

            def replace_root_before_final_bind(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    moved = base / "previous-source"
                    root.rename(moved)
                    root.mkdir()
                    (moved / "script").rename(root / "script")
                    self.assertEqual((selected.stat().st_dev, selected.stat().st_ino), source_identity)
                    self.assertNotEqual((root.stat().st_dev, root.stat().st_ino), initial_root_identity)
                return bind_root(path)

            with mock.patch.object(backend, "_bind_root", replace_root_before_final_bind):
                with self.assertRaises(ProjectWorkspaceError) as rejected:
                    stage_selected_project_documents(
                        root, (selected,), SelectedProjectDocumentsRequest("single", "en", "zh-CN"),
                        file_system=backend,
                    )
            self.assertEqual(calls, 2)
            self.assertEqual(rejected.exception.code, "PROJECT.INTAKE.SOURCE_STALE")
            self.assertEqual(selected.read_bytes(), b"Hello\n")


if __name__ == "__main__":
    unittest.main()
