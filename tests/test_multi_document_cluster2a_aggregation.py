"""Cluster 2A adversarial contracts for explicit intake and reconciliation.

The tests in this module intentionally exercise the Application-owned public
surface.  Fixture bytes are fed through the real Parser composition root; no
codec grammar is copied into the workspace tests.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import ModuleType
from unittest import mock
import unittest

from parser_composition import OpenedParserInput, ParserApplicationSurface
from parser_contracts import (
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
)
from project_workspace_contracts import (
    ProjectOriginKind,
    ProjectPersistenceKind,
    SegmentIdentity,
    SourcePresence,
)
from project_workspace_identity import (
    ProjectWorkspaceError,
    derive_explicit_selected_document_id,
)


_ROOT = Path(__file__).resolve().parents[1]
_PAYLOADS = _ROOT / "tests" / "fixtures" / "parser" / "project" / "payloads"


def _cluster2a_intake() -> ModuleType:
    try:
        module = importlib.import_module("project_workspace_intake")
    except ModuleNotFoundError:
        raise AssertionError(
            "Cluster 2A RED: public module project_workspace_intake is missing"
        ) from None
    required = (
        "OriginBinding",
        "OriginRenameMapping",
        "SelectedProjectDocumentsRequest",
        "StagedSelectedProjectDocuments",
        "revalidate_staged_selected_documents",
        "stage_selected_project_documents",
    )
    missing = tuple(name for name in required if not hasattr(module, name))
    if missing:
        raise AssertionError(
            f"Cluster 2A RED: intake public contract is missing {missing!r}"
        )
    return module


def _cluster2a_workspace() -> ModuleType:
    try:
        module = importlib.import_module("project_workspace")
    except ModuleNotFoundError:
        raise AssertionError(
            "Cluster 2A RED: public module project_workspace is missing"
        ) from None
    required = (
        "ProjectWorkspaceService",
        "ReconciliationAssociation",
        "ReconciliationCategory",
        "ReconciliationDecision",
        "ReconciliationDisposition",
        "ReconciliationPreview",
        "ReconciliationReceipt",
    )
    missing = tuple(name for name in required if not hasattr(module, name))
    if missing:
        raise AssertionError(
            f"Cluster 2A RED: reconciliation public contract is missing {missing!r}"
        )
    return module


def _fixture_bytes(name: str) -> bytes:
    path = _PAYLOADS / name
    if path.suffix == ".hex":
        return bytes.fromhex(path.read_text(encoding="ascii"))
    return path.read_bytes()


def _write_fixture(root: Path, relative: str, fixture: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_fixture_bytes(fixture))
    return path


def _write_localcat_json(root: Path, relative: str, local_id: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        '  "schema_version": 1,\n'
        '  "name": "Shared local id",\n'
        '  "source_locale": "en",\n'
        '  "target_locale": "zh-CN",\n'
        '  "segments": [\n'
        "    {"
        f'"id": "{local_id}", "source": "{source}", '
        '"target": "", "speaker": "", "confirmed": false}'
        "\n  ]\n"
        "}\n",
        encoding="utf-8",
    )
    return path


def _write_localcat_project(
    root: Path,
    relative: str,
    segments: tuple[tuple[str, str, str, bool], ...],
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": relative,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": local_id,
                        "source": source,
                        "target": target,
                        "speaker": "",
                        "confirmed": confirmed,
                    }
                    for local_id, source, target, confirmed in segments
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _request(module: ModuleType, **overrides: object) -> object:
    values: dict[str, object] = {
        "name": "Selected sources",
        "source_locale": "en",
        "target_locale": "zh-CN",
    }
    values.update(overrides)
    return module.SelectedProjectDocumentsRequest(**values)


def _stage(
    module: ModuleType,
    root: Path,
    selected: tuple[Path, ...],
    *,
    request: object | None = None,
) -> object:
    return module.stage_selected_project_documents(
        root,
        selected,
        _request(module) if request is None else request,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Cluster2AExplicitSelectedFilesIntakeTests(unittest.TestCase):
    def test_real_json_txt_po_pot_preserve_explicit_order_and_composite_ids(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "portable-root"
            root.mkdir()
            json_a = _write_localcat_json(
                root,
                "chapters/a.json",
                "same-local-id",
                "JSON source A",
            )
            text = _write_fixture(
                root,
                "notes/readme.txt",
                "line-text-valid.hex",
            )
            po = _write_fixture(root, "locale/zh.po", "gettext-po-valid.po")
            pot = _write_fixture(root, "locale/base.pot", "gettext-pot-valid.pot")
            json_b = _write_localcat_json(
                root,
                "chapters/b.json",
                "same-local-id",
                "JSON source B",
            )
            adjacent_fatal = _write_fixture(
                root,
                "chapters/not-selected.json",
                "localcat-fatal-tail.json",
            )
            adjacent_digest = _sha256(adjacent_fatal)
            selected = (pot, json_b, text, json_a, po)

            staged = _stage(module, root, selected)
            workspace = staged.workspace

            self.assertIs(type(staged), module.StagedSelectedProjectDocuments)
            self.assertFalse(staged.durable)
            self.assertEqual(workspace.origin.kind, ProjectOriginKind.DIRECTORY)
            self.assertEqual(
                workspace.origin.profile_version,
                "explicit-selected-files-v1",
            )
            self.assertEqual(
                workspace.persistence_kind,
                ProjectPersistenceKind.PROJECT_PACKAGE,
            )
            self.assertEqual(
                tuple(document.source_ref for document in workspace.documents),
                (
                    "locale/base.pot",
                    "chapters/b.json",
                    "notes/readme.txt",
                    "chapters/a.json",
                    "locale/zh.po",
                ),
            )
            self.assertEqual(
                tuple(document.order for document in workspace.documents),
                tuple(range(5)),
            )
            self.assertEqual(
                tuple(document.format_id for document in workspace.documents),
                tuple(
                    format_id.value
                    for format_id in (
                        GETTEXT_POT_V1,
                        LOCALCAT_JSON_V1,
                        LINE_TEXT_V1,
                        LOCALCAT_JSON_V1,
                        GETTEXT_PO_V1,
                    )
                ),
            )

            shared = tuple(
                identity
                for document in workspace.documents
                for identity in document.segment_identities
                if identity.local_segment_id == "same-local-id"
            )
            self.assertEqual(len(shared), 2)
            self.assertNotEqual(shared[0].document_id, shared[1].document_id)
            self.assertEqual(len(set(shared)), 2)
            self.assertNotIn(
                "chapters/not-selected.json",
                tuple(document.source_ref for document in workspace.documents),
            )
            self.assertEqual(_sha256(adjacent_fatal), adjacent_digest)

            by_format = {document.format_id: document for document in workspace.documents}
            for reader_only in (LINE_TEXT_V1, GETTEXT_PO_V1, GETTEXT_POT_V1):
                capability = by_format[reader_only.value].writer_capability_snapshot
                self.assertFalse(capability.canonical_write)
                self.assertFalse(capability.source_round_trip_write)
            self.assertFalse(staged.source_write_back_authorized)

    def test_only_selected_paths_reach_the_real_parser_surface_in_exact_order(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            _write_fixture(root, "adjacent.pot", "gettext-pot-fatal-tail.pot")
            opened_paths: list[str] = []
            original = ParserApplicationSurface.open_input

            def recording_open(surface, reference, selection, request, **kwargs):
                opened_paths.append(reference.selected_path)
                return original(surface, reference, selection, request, **kwargs)

            with mock.patch.object(
                ParserApplicationSurface,
                "open_input",
                recording_open,
            ):
                staged = _stage(module, root, (second, first))

            self.assertEqual(
                tuple(Path(path).name for path in opened_paths),
                ("second.po", "first.txt"),
            )
            self.assertEqual(
                tuple(document.source_ref for document in staged.workspace.documents),
                ("second.po", "first.txt"),
            )

    def test_binding_pins_root_device_inode_and_verified_source_identities(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            root_stat = os.stat(root, follow_symlinks=False)

            staged = _stage(module, root, (first, second))
            binding = staged.origin_binding

            self.assertIs(type(binding), module.OriginBinding)
            self.assertEqual(binding.root_device, root_stat.st_dev)
            self.assertEqual(binding.root_inode, root_stat.st_ino)
            self.assertEqual(binding.project_id, staged.workspace.project_id)
            self.assertEqual(binding.profile_version, "explicit-selected-files-v1")
            self.assertGreaterEqual(binding.revision, 1)
            self.assertEqual(
                tuple(item.source_ref for item in binding.documents),
                ("first.txt", "second.po"),
            )
            self.assertEqual(
                tuple(item.document_id for item in binding.documents),
                tuple(document.document_id for document in staged.workspace.documents),
            )
            self.assertEqual(
                tuple(item.source_identity for item in binding.documents),
                staged.source_identities,
            )

    def test_flat_projection_and_progress_follow_document_then_segment_order(self) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_project(
                root,
                "first.json",
                (
                    ("same", "First A", "translated", True),
                    ("second", "First B", "", False),
                ),
            )
            second = _write_localcat_project(
                root,
                "second.json",
                (("same", "Second A", "draft", False),),
            )
            staged = _stage(intake, root, (second, first))
            service = workspace_module.ProjectWorkspaceService(
                staged.workspace,
                staged.origin_binding,
                session_id="aggregate-session",
                revision=0,
            )

            flattened = service.flat_segments
            self.assertEqual(
                tuple(item.identity for item in flattened),
                tuple(
                    identity
                    for document in staged.workspace.documents
                    for identity in document.segment_identities
                ),
            )
            self.assertEqual(
                tuple(item.document_local_index for item in flattened),
                (0, 0, 1),
            )
            self.assertEqual(
                tuple(item.project_global_index for item in flattened),
                (0, 1, 2),
            )
            self.assertEqual(
                (
                    service.project_progress.total_documents,
                    service.project_progress.total_segments,
                    service.project_progress.translated_segments,
                    service.project_progress.confirmed_segments,
                ),
                (2, 3, 2, 1),
            )
            self.assertEqual(
                tuple(
                    (
                        item.total_segments,
                        item.translated_segments,
                        item.confirmed_segments,
                    )
                    for item in service.document_progress
                ),
                ((1, 1, 0), (2, 1, 1)),
            )

    def test_container_minimum_symlink_escape_nonregular_and_hardlink_fail_closed(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.txt", "line-text-valid.hex")
            outside = _write_fixture(base, "outside.txt", "line-text-valid.hex")
            symlink = root / "link.txt"
            symlink.symlink_to(first)
            folder = root / "folder.txt"
            folder.mkdir()
            hardlink = root / "same-inode.txt"
            os.link(first, hardlink)
            original_digests = {
                path: _sha256(path) for path in (first, second, outside, hardlink)
            }

            invalid_calls = (
                ([first, second],),
                ((first,),),
                ((first, first),),
                ((first, symlink),),
                ((first, outside),),
                ((first, folder),),
                ((first, hardlink),),
            )
            for (selected,) in invalid_calls:
                with self.subTest(selected=selected):
                    with self.assertRaises((ProjectWorkspaceError, TypeError, ValueError)):
                        _stage(module, root, selected)  # type: ignore[arg-type]

            self.assertEqual(
                {path: _sha256(path) for path in original_digests},
                original_digests,
            )


class Cluster2AOriginBindingRenameTests(unittest.TestCase):
    def test_explicit_same_root_rename_reuses_id_and_mints_new_binding_revision(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            old = _write_localcat_json(root, "old.json", "one", "Old source")
            anchor = _write_localcat_json(
                root,
                "anchor.json",
                "anchor",
                "Anchor source",
            )
            current = _stage(intake, root, (old, anchor))
            old_document = current.workspace.documents[0]
            old_binding = current.origin_binding
            renamed = root / "renamed.json"
            old.rename(renamed)
            mapping = intake.OriginRenameMapping(
                old_source_ref="old.json",
                new_source_ref="renamed.json",
                document_id=old_document.document_id,
            )
            request = _request(
                intake,
                origin_binding=old_binding,
                expected_binding_revision=old_binding.revision,
                rename_mappings=(mapping,),
            )

            incoming = _stage(intake, root, (renamed, anchor), request=request)

            self.assertEqual(
                incoming.workspace.documents[0].document_id,
                old_document.document_id,
            )
            self.assertEqual(
                incoming.workspace.documents[0].source_ref,
                "renamed.json",
            )
            self.assertEqual(
                incoming.origin_binding.revision,
                old_binding.revision + 1,
            )
            self.assertEqual(
                incoming.origin_binding.root_device,
                old_binding.root_device,
            )
            self.assertEqual(
                incoming.origin_binding.root_inode,
                old_binding.root_inode,
            )

    def test_forged_cross_root_and_stale_rename_reject_before_parser_or_mutation(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            first = _write_localcat_json(root, "one.json", "one", "One")
            second = _write_localcat_json(root, "two.json", "two", "Two")
            current = _stage(intake, root, (first, second))
            before_workspace = current.workspace
            before_binding = current.origin_binding
            renamed = root / "renamed.json"
            first.rename(renamed)
            opened_paths: list[str] = []
            original_open = ParserApplicationSurface.open_input

            def recording_open(surface, reference, selection, request, **kwargs):
                opened_paths.append(reference.selected_path)
                return original_open(surface, reference, selection, request, **kwargs)

            wrong_document_id = derive_explicit_selected_document_id("forged.json")
            forged = intake.OriginRenameMapping(
                old_source_ref="one.json",
                new_source_ref="renamed.json",
                document_id=wrong_document_id,
            )
            stale = intake.OriginRenameMapping(
                old_source_ref="one.json",
                new_source_ref="renamed.json",
                document_id=before_workspace.documents[0].document_id,
            )
            requests = (
                _request(
                    intake,
                    origin_binding=before_binding,
                    expected_binding_revision=before_binding.revision,
                    rename_mappings=(forged,),
                ),
                _request(
                    intake,
                    origin_binding=before_binding,
                    expected_binding_revision=before_binding.revision + 1,
                    rename_mappings=(stale,),
                ),
            )
            with mock.patch.object(
                ParserApplicationSurface,
                "open_input",
                recording_open,
            ):
                for request in requests:
                    with self.subTest(request=request):
                        with self.assertRaises(ProjectWorkspaceError):
                            _stage(
                                intake,
                                root,
                                (renamed, second),
                                request=request,
                            )

                other_root = base / "other-root"
                other_root.mkdir()
                other_one = _write_localcat_json(
                    other_root,
                    "renamed.json",
                    "one",
                    "One",
                )
                other_two = _write_localcat_json(
                    other_root,
                    "two.json",
                    "two",
                    "Two",
                )
                cross_root_request = _request(
                    intake,
                    origin_binding=before_binding,
                    expected_binding_revision=before_binding.revision,
                    rename_mappings=(stale,),
                )
                with self.assertRaises(ProjectWorkspaceError):
                    _stage(
                        intake,
                        other_root,
                        (other_one, other_two),
                        request=cross_root_request,
                    )

            self.assertEqual(opened_paths, [])
            self.assertEqual(current.workspace, before_workspace)
            self.assertEqual(current.origin_binding, before_binding)

    def test_rename_mapping_cannot_attach_an_unrelated_replacement_file(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            old = _write_localcat_json(root, "old.json", "one", "Original")
            anchor = _write_localcat_json(root, "anchor.json", "anchor", "Anchor")
            current = _stage(intake, root, (old, anchor))
            before = current.workspace
            old.unlink()
            replacement = _write_localcat_json(
                root,
                "renamed.json",
                "one",
                "Unrelated replacement",
            )
            mapping = intake.OriginRenameMapping(
                old_source_ref="old.json",
                new_source_ref="renamed.json",
                document_id=before.documents[0].document_id,
            )
            request = _request(
                intake,
                origin_binding=current.origin_binding,
                expected_binding_revision=current.origin_binding.revision,
                rename_mappings=(mapping,),
            )
            parser_opens: list[str] = []
            original_open = ParserApplicationSurface.open_input

            def recording_open(surface, reference, selection, read_request, **kwargs):
                parser_opens.append(reference.selected_path)
                return original_open(
                    surface,
                    reference,
                    selection,
                    read_request,
                    **kwargs,
                )

            with mock.patch.object(
                ParserApplicationSurface,
                "open_input",
                recording_open,
            ):
                with self.assertRaises(ProjectWorkspaceError):
                    _stage(
                        intake,
                        root,
                        (replacement, anchor),
                        request=request,
                    )

            self.assertEqual(parser_opens, [])
            self.assertEqual(current.workspace, before)

    def test_forged_previous_binding_cannot_swap_authority_through_double_rename(
        self,
    ) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "a.json", "a", "A")
            second = _write_localcat_json(root, "b.json", "b", "B")
            current = _stage(intake, root, (first, second))
            first_document, second_document = current.workspace.documents
            first_bound, second_bound = current.origin_binding.documents
            forged_previous = replace(
                current.origin_binding,
                documents=(
                    replace(first_bound, document_id=second_document.document_id),
                    replace(second_bound, document_id=first_document.document_id),
                ),
            )
            renamed_first = root / "c.json"
            renamed_second = root / "d.json"
            first.rename(renamed_first)
            second.rename(renamed_second)
            forged_mappings = (
                intake.OriginRenameMapping(
                    old_source_ref="a.json",
                    new_source_ref="c.json",
                    document_id=second_document.document_id,
                ),
                intake.OriginRenameMapping(
                    old_source_ref="b.json",
                    new_source_ref="d.json",
                    document_id=first_document.document_id,
                ),
            )
            forged_candidate = _stage(
                intake,
                root,
                (renamed_first, renamed_second),
                request=_request(
                    intake,
                    origin_binding=forged_previous,
                    expected_binding_revision=forged_previous.revision,
                    rename_mappings=forged_mappings,
                ),
            )
            self.assertEqual(
                tuple(
                    (document.source_ref, document.document_id)
                    for document in forged_candidate.workspace.documents
                ),
                (
                    ("c.json", second_document.document_id),
                    ("d.json", first_document.document_id),
                ),
            )
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="double-rename-session",
                revision=31,
            )
            before_workspace = service.workspace
            before_binding = service.origin_binding
            before_digest = service.workspace_digest

            with mock.patch(
                "project_workspace.secrets.token_hex",
                side_effect=AssertionError("forged rename minted a plan"),
            ):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    service.stage_reconciliation(
                        forged_candidate,
                        associations=(),
                        session_id="double-rename-session",
                        base_revision=31,
                    )

            self.assertEqual(
                caught.exception.code,
                "PROJECT.RECONCILE.INPUT_INVALID",
            )
            self.assertEqual(service.workspace, before_workspace)
            self.assertEqual(service.origin_binding, before_binding)
            self.assertEqual(service.workspace_digest, before_digest)
            self.assertEqual(service.revision, 31)

            legitimate_mappings = (
                intake.OriginRenameMapping(
                    old_source_ref="a.json",
                    new_source_ref="c.json",
                    document_id=first_document.document_id,
                ),
                intake.OriginRenameMapping(
                    old_source_ref="b.json",
                    new_source_ref="d.json",
                    document_id=second_document.document_id,
                ),
            )
            legitimate_candidate = _stage(
                intake,
                root,
                (renamed_first, renamed_second),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=current.origin_binding.revision,
                    rename_mappings=legitimate_mappings,
                ),
            )
            self.assertEqual(
                tuple(
                    (document.source_ref, document.document_id)
                    for document in legitimate_candidate.workspace.documents
                ),
                (
                    ("c.json", first_document.document_id),
                    ("d.json", second_document.document_id),
                ),
            )
            self.assertEqual(
                legitimate_candidate.origin_binding.revision,
                current.origin_binding.revision + 1,
            )
            preview = service.stage_reconciliation(
                legitimate_candidate,
                associations=(),
                session_id="double-rename-session",
                base_revision=31,
            )
            receipt = service.apply_reconciliation(
                preview.operation_id,
                decisions=(),
                session_id="double-rename-session",
                base_revision=31,
                incoming_source_identities=legitimate_candidate.source_identities,
            )
            self.assertIs(type(receipt), workspace_module.ReconciliationReceipt)
            self.assertEqual(
                tuple(document.source_ref for document in service.workspace.documents),
                ("c.json", "d.json"),
            )
            self.assertEqual(service.revision, 32)


class Cluster2AOriginBindingRevisionTests(unittest.TestCase):
    @staticmethod
    def _with_binding(intake: ModuleType, staged: object, binding: object) -> object:
        return intake.StagedSelectedProjectDocuments(
            workspace=staged.workspace,
            origin_binding=binding,
            source_identities=staged.source_identities,
            source_write_back_authorized=False,
            durable=False,
        )

    def test_swapped_ref_to_document_mapping_is_rejected_before_plan_or_mutation(
        self,
    ) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "a.json", "a", "A")
            second = _write_localcat_json(root, "b.json", "b", "B")
            current = _stage(intake, root, (first, second))
            first_bound, second_bound = current.origin_binding.documents
            swapped_binding = replace(
                current.origin_binding,
                documents=(
                    replace(first_bound, document_id=second_bound.document_id),
                    replace(second_bound, document_id=first_bound.document_id),
                ),
            )
            forged = self._with_binding(intake, current, swapped_binding)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="swapped-mapping-session",
                revision=23,
            )
            before_workspace = service.workspace
            before_binding = service.origin_binding
            before_digest = service.workspace_digest

            with mock.patch(
                "project_workspace.secrets.token_hex",
                side_effect=AssertionError("invalid mapping minted a plan"),
            ):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    service.stage_reconciliation(
                        forged,
                        associations=(),
                        session_id="swapped-mapping-session",
                        base_revision=23,
                    )

            self.assertEqual(
                caught.exception.code,
                "PROJECT.RECONCILE.INPUT_INVALID",
            )
            self.assertEqual(service.workspace, before_workspace)
            self.assertEqual(service.origin_binding, before_binding)
            self.assertEqual(service.workspace_digest, before_digest)
            self.assertEqual(service.revision, 23)

            valid = intake.revalidate_staged_selected_documents(current)
            preview = service.stage_reconciliation(
                valid,
                associations=(),
                session_id="swapped-mapping-session",
                base_revision=23,
            )
            self.assertIs(type(preview), workspace_module.ReconciliationPreview)

    def test_revision_changes_only_when_ref_to_document_mapping_set_changes(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "a.json", "a", "A-v1")
            second = _write_localcat_json(root, "b.json", "b", "B")
            current = _stage(intake, root, (first, second))
            base_revision = current.origin_binding.revision

            reordered = _stage(
                intake,
                root,
                (second, first),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=base_revision,
                ),
            )
            self.assertEqual(reordered.origin_binding.revision, base_revision)

            _write_localcat_json(root, "a.json", "a", "A-v2")
            content_changed = _stage(
                intake,
                root,
                (first, second),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=base_revision,
                ),
            )
            self.assertNotEqual(
                content_changed.source_identities,
                current.source_identities,
            )
            self.assertEqual(
                content_changed.origin_binding.revision,
                base_revision,
            )

            third = _write_localcat_json(root, "c.json", "c", "C")
            added = _stage(
                intake,
                root,
                (first, second, third),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=base_revision,
                ),
            )
            self.assertEqual(
                added.origin_binding.revision,
                base_revision + 1,
            )

            removed = _stage(
                intake,
                root,
                (first, second),
                request=_request(
                    intake,
                    origin_binding=added.origin_binding,
                    expected_binding_revision=added.origin_binding.revision,
                ),
            )
            self.assertEqual(
                removed.origin_binding.revision,
                added.origin_binding.revision + 1,
            )

    def test_service_rejects_mapping_revision_mismatch_with_zero_mutation(self) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "a.json", "a", "A")
            second = _write_localcat_json(root, "b.json", "b", "B")
            current = _stage(intake, root, (first, second))
            third = _write_localcat_json(root, "c.json", "c", "C")
            added = _stage(
                intake,
                root,
                (first, second, third),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=current.origin_binding.revision,
                ),
            )
            unchanged = intake.revalidate_staged_selected_documents(current)
            hostile = (
                (
                    "mapping-changed-without-revision",
                    self._with_binding(
                        intake,
                        added,
                        replace(
                            added.origin_binding,
                            revision=current.origin_binding.revision,
                        ),
                    ),
                ),
                (
                    "mapping-same-with-extra-revision",
                    self._with_binding(
                        intake,
                        unchanged,
                        replace(
                            unchanged.origin_binding,
                            revision=current.origin_binding.revision + 1,
                        ),
                    ),
                ),
            )

            for label, forged in hostile:
                with self.subTest(label=label):
                    service = workspace_module.ProjectWorkspaceService(
                        current.workspace,
                        current.origin_binding,
                        session_id="revision-mismatch-session",
                        revision=29,
                    )
                    before_workspace = service.workspace
                    before_binding = service.origin_binding
                    before_digest = service.workspace_digest
                    with self.assertRaises(ProjectWorkspaceError) as caught:
                        service.stage_reconciliation(
                            forged,
                            associations=(),
                            session_id="revision-mismatch-session",
                            base_revision=29,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "PROJECT.RECONCILE.INPUT_INVALID",
                    )
                    self.assertEqual(service.workspace, before_workspace)
                    self.assertEqual(service.origin_binding, before_binding)
                    self.assertEqual(service.workspace_digest, before_digest)
                    self.assertEqual(service.revision, 29)


class Cluster2AIntakeHostileBoundaryTests(unittest.TestCase):
    def test_directory_enumeration_apis_are_never_touched(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            unselected = _write_fixture(
                root,
                "malicious-adjacent.pot",
                "gettext-pot-fatal-tail.pot",
            )
            unselected_digest = _sha256(unselected)
            forbidden = AssertionError("directory enumeration is forbidden")

            with ExitStack() as stack:
                for owner, name in (
                    (Path, "iterdir"),
                    (Path, "glob"),
                    (Path, "rglob"),
                    (os, "listdir"),
                    (os, "scandir"),
                    (os, "walk"),
                ):
                    stack.enter_context(
                        mock.patch.object(owner, name, side_effect=forbidden)
                    )
                staged = _stage(intake, root, (second, first))

            self.assertEqual(
                tuple(document.source_ref for document in staged.workspace.documents),
                ("second.po", "first.txt"),
            )
            self.assertEqual(_sha256(unselected), unselected_digest)

    def test_non_project_formats_fail_before_parser_open(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            valid = _write_fixture(root, "valid.txt", "line-text-valid.hex")
            unsupported = tuple(
                _write_localcat_json(root, f"not-project{suffix}", "one", "One")
                for suffix in (".tmx", ".csv", ".xlsx")
            )
            with mock.patch.object(
                ParserApplicationSurface,
                "open_input",
                side_effect=AssertionError("unsupported input reached Parser"),
            ) as opened:
                for path in unsupported:
                    with self.subTest(suffix=path.suffix):
                        with self.assertRaises(ProjectWorkspaceError) as caught:
                            _stage(intake, root, (valid, path))
                        self.assertEqual(
                            caught.exception.code,
                            "PROJECT.INTAKE.INPUT_INVALID",
                        )
            opened.assert_not_called()

    def test_selected_file_modify_or_replace_after_parse_is_source_stale(self) -> None:
        intake = _cluster2a_intake()
        for fault in ("modify", "replace"):
            with self.subTest(fault=fault):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "root"
                    root.mkdir()
                    first = _write_fixture(root, "first.txt", "line-text-valid.hex")
                    second = _write_fixture(root, "second.po", "gettext-po-valid.po")
                    replacement = _write_fixture(
                        root,
                        "replacement.txt",
                        "line-text-valid.hex",
                    )
                    original_materialize = OpenedParserInput.materialize
                    parsed = 0

                    def fault_after_first(opened):
                        nonlocal parsed
                        result = original_materialize(opened)
                        parsed += 1
                        if parsed == 1:
                            if fault == "modify":
                                first.write_bytes(first.read_bytes() + b"changed")
                            else:
                                os.replace(replacement, first)
                        return result

                    with mock.patch.object(
                        OpenedParserInput,
                        "materialize",
                        fault_after_first,
                    ):
                        with self.assertRaises(ProjectWorkspaceError) as caught:
                            _stage(intake, root, (first, second))
                    self.assertEqual(
                        caught.exception.code,
                        "PROJECT.INTAKE.SOURCE_STALE",
                    )

    def test_po_format_metadata_changes_source_fingerprint_for_same_source(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            original = _fixture_bytes("gettext-po-valid.po")
            changed = original.replace(
                b"#: chapter.rpy:10",
                b"#: renamed-chapter.rpy:99",
            )
            self.assertNotEqual(changed, original)
            first = root / "first.po"
            second = root / "second.po"
            first.write_bytes(original)
            second.write_bytes(changed)

            staged = _stage(intake, root, (first, second))
            first_segment = staged.workspace.documents[0].source_segments[0]
            second_segment = staged.workspace.documents[1].source_segments[0]

            self.assertEqual(first_segment.source, second_segment.source)
            self.assertEqual(first_segment.raw_speaker, second_segment.raw_speaker)
            self.assertNotEqual(
                first_segment.source_fingerprint,
                second_segment.source_fingerprint,
            )

    def test_success_is_read_only_for_root_and_every_selected_file(self) -> None:
        intake = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            root_before = os.stat(root, follow_symlinks=False)
            files_before = {
                path: (
                    _sha256(path),
                    os.stat(path, follow_symlinks=False).st_mtime_ns,
                )
                for path in (first, second)
            }

            _stage(intake, root, (first, second))

            root_after = os.stat(root, follow_symlinks=False)
            self.assertEqual(
                (
                    root_after.st_dev,
                    root_after.st_ino,
                    root_after.st_mtime_ns,
                ),
                (
                    root_before.st_dev,
                    root_before.st_ino,
                    root_before.st_mtime_ns,
                ),
            )
            self.assertEqual(
                {
                    path: (
                        _sha256(path),
                        os.stat(path, follow_symlinks=False).st_mtime_ns,
                    )
                    for path in files_before
                },
                files_before,
            )


class Cluster2AReconciliationTests(unittest.TestCase):
    def _current_and_incoming(self, root: Path) -> tuple[object, object, object, object]:
        intake = _cluster2a_intake()
        primary = _write_localcat_project(
            root,
            "primary.json",
            (
                ("unchanged", "Same source", "keep-confirmed", True),
                ("changed", "Old source", "keep-draft", True),
                ("removed", "Removed source", "recover-removed", True),
                ("ambiguous-old", "Old ambiguous", "recover-ambiguous", True),
                ("unresolved-old", "Old unresolved", "recover-unresolved", True),
            ),
        )
        anchor = _write_localcat_project(
            root,
            "anchor.json",
            (("anchor", "Anchor source", "anchor-target", True),),
        )
        current = _stage(intake, root, (primary, anchor))
        _write_localcat_project(
            root,
            "primary.json",
            (
                ("unchanged", "Same source", "incoming-must-not-win", False),
                ("changed", "New source", "incoming-must-not-win", True),
                ("new", "Brand new source", "incoming-new-target", True),
                ("ambiguous-new-1", "Ambiguous one", "candidate-one", False),
                ("ambiguous-new-2", "Ambiguous two", "candidate-two", False),
            ),
        )
        request = _request(
            intake,
            origin_binding=current.origin_binding,
            expected_binding_revision=current.origin_binding.revision,
        )
        incoming = _stage(intake, root, (primary, anchor), request=request)
        return intake, current, incoming, primary

    def test_six_states_are_identity_based_body_safe_and_apply_exact_rules(self) -> None:
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _intake, current, incoming, _primary = self._current_and_incoming(root)
            primary_document_id = current.workspace.documents[0].document_id
            identity = SegmentIdentity
            ambiguous_old = identity(primary_document_id, "ambiguous-old")
            unresolved_old = identity(primary_document_id, "unresolved-old")
            ambiguous_new_1 = identity(primary_document_id, "ambiguous-new-1")
            ambiguous_new_2 = identity(primary_document_id, "ambiguous-new-2")
            associations = (
                workspace_module.ReconciliationAssociation(
                    current_identity=ambiguous_old,
                    incoming_identities=(ambiguous_new_1, ambiguous_new_2),
                ),
                workspace_module.ReconciliationAssociation(
                    current_identity=unresolved_old,
                    incoming_identities=(),
                ),
            )
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="session-a",
                revision=7,
            )

            preview = service.stage_reconciliation(
                incoming,
                associations=associations,
                session_id="session-a",
                base_revision=7,
            )

            self.assertIs(type(preview), workspace_module.ReconciliationPreview)
            self.assertEqual(
                tuple(category.value for category in workspace_module.ReconciliationCategory),
                (
                    "unchanged",
                    "source_changed",
                    "new",
                    "removed",
                    "ambiguous",
                    "unresolved",
                ),
            )
            self.assertEqual(preview.unchanged_count, 2)
            self.assertEqual(preview.source_changed_count, 1)
            self.assertEqual(preview.new_count, 1)
            self.assertEqual(preview.removed_count, 1)
            self.assertEqual(preview.ambiguous_count, 1)
            self.assertEqual(preview.unresolved_count, 1)
            preview_text = repr(preview)
            for body in (
                "Same source",
                "Old source",
                "New source",
                "keep-confirmed",
                "recover-removed",
                "incoming-new-target",
            ):
                self.assertNotIn(body, preview_text)

            decision = workspace_module.ReconciliationDecision
            disposition = workspace_module.ReconciliationDisposition
            decisions = (
                decision(
                    identity=identity(primary_document_id, "removed"),
                    disposition=disposition.REMOVE,
                ),
                decision(
                    identity=ambiguous_old,
                    disposition=disposition.ACCEPT_ASSOCIATION,
                    accepted_incoming_identity=ambiguous_new_1,
                ),
                decision(
                    identity=unresolved_old,
                    disposition=disposition.KEEP_DETACHED,
                ),
            )
            receipt = service.apply_reconciliation(
                preview.operation_id,
                decisions=decisions,
                session_id="session-a",
                base_revision=7,
                incoming_source_identities=incoming.source_identities,
            )
            updated = service.workspace
            updated_primary = updated.documents[0]
            by_local_id = {
                segment.identity.local_segment_id: segment
                for segment in updated_primary.segments
            }

            self.assertEqual(by_local_id["unchanged"].target, "keep-confirmed")
            self.assertTrue(by_local_id["unchanged"].confirmed)
            self.assertEqual(by_local_id["changed"].source, "New source")
            self.assertEqual(by_local_id["changed"].target, "keep-draft")
            self.assertFalse(by_local_id["changed"].confirmed)
            self.assertEqual(by_local_id["new"].target, "incoming-new-target")
            self.assertFalse(by_local_id["new"].confirmed)
            self.assertNotIn("removed", by_local_id)
            self.assertEqual(
                by_local_id["ambiguous-old"].target,
                "recover-ambiguous",
            )
            self.assertFalse(by_local_id["ambiguous-old"].confirmed)
            self.assertIn("unresolved-old", by_local_id)
            self.assertIn(unresolved_old, receipt.detached_identities)
            self.assertEqual(service.revision, 8)

    def test_missing_extra_forged_and_cross_service_decisions_never_mutate(self) -> None:
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _intake, current, incoming, _primary = self._current_and_incoming(root)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="session-a",
                revision=11,
            )
            preview = service.stage_reconciliation(
                incoming,
                associations=(),
                session_id="session-a",
                base_revision=11,
            )
            before = service.workspace
            before_revision = service.revision
            forged_identity = SegmentIdentity(
                current.workspace.documents[0].document_id,
                "forged",
            )
            forged_decision = workspace_module.ReconciliationDecision(
                identity=forged_identity,
                disposition=workspace_module.ReconciliationDisposition.REMOVE,
            )
            with self.assertRaises(ProjectWorkspaceError):
                service.apply_reconciliation(
                    preview.operation_id,
                    decisions=(forged_decision,),
                    session_id="session-a",
                    base_revision=11,
                    incoming_source_identities=incoming.source_identities,
                )
            self.assertEqual(service.workspace, before)
            self.assertEqual(service.revision, before_revision)

            other_service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="session-a",
                revision=11,
            )
            with self.assertRaises(ProjectWorkspaceError):
                other_service.apply_reconciliation(
                    preview.operation_id,
                    decisions=(),
                    session_id="session-a",
                    base_revision=11,
                    incoming_source_identities=incoming.source_identities,
                )
            self.assertEqual(other_service.workspace, before)
            self.assertEqual(other_service.revision, before_revision)

    def test_session_revision_source_stale_and_reused_capability_are_zero_mutation(self) -> None:
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _intake, current, incoming, primary = self._current_and_incoming(root)
            scenarios = {
                "session": "PROJECT.RECONCILE.PREVIEW_STALE",
                "revision": "PROJECT.RECONCILE.PREVIEW_STALE",
                "source": "PROJECT.RECONCILE.SOURCE_STALE",
            }
            for scenario, expected_code in scenarios.items():
                with self.subTest(scenario=scenario):
                    service = workspace_module.ProjectWorkspaceService(
                        current.workspace,
                        current.origin_binding,
                        session_id="session-a",
                        revision=19,
                    )
                    preview = service.stage_reconciliation(
                        incoming,
                        associations=(),
                        session_id="session-a",
                        base_revision=19,
                    )
                    before = service.workspace
                    identities = incoming.source_identities
                    session_id = "session-a"
                    revision = 19
                    if scenario == "session":
                        session_id = "session-b"
                    elif scenario == "revision":
                        revision = 20
                    else:
                        primary.write_bytes(primary.read_bytes() + b"\n")
                        identities = tuple(
                            replace(
                                item,
                                content_sha256="f" * 64,
                            )
                            if index == 0
                            else item
                            for index, item in enumerate(identities)
                        )
                    with self.assertRaises(ProjectWorkspaceError) as caught:
                        service.apply_reconciliation(
                            preview.operation_id,
                            decisions=(),
                            session_id=session_id,
                            base_revision=revision,
                            incoming_source_identities=identities,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        expected_code,
                    )
                    self.assertEqual(service.workspace, before)
                    self.assertEqual(service.revision, 19)
                    with self.assertRaises(ProjectWorkspaceError):
                        service.apply_reconciliation(
                            preview.operation_id,
                            decisions=(),
                            session_id="session-a",
                            base_revision=19,
                            incoming_source_identities=incoming.source_identities,
                        )
                    self.assertEqual(service.workspace, before)


class Cluster2AReconciliationOrderingAndStaleTests(unittest.TestCase):
    def test_reversed_incoming_keeps_existing_order_and_appends_only_new_documents(
        self,
    ) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "first.json", "first", "First")
            second = _write_localcat_json(root, "second.json", "second", "Second")
            current = _stage(intake, root, (first, second))
            new = _write_localcat_json(root, "new.json", "new", "New")
            incoming = _stage(
                intake,
                root,
                (second, first, new),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=current.origin_binding.revision,
                ),
            )
            current_ids = tuple(
                document.document_id for document in current.workspace.documents
            )
            new_id = next(
                document.document_id
                for document in incoming.workspace.documents
                if document.source_ref == "new.json"
            )
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="order-session",
                revision=3,
            )
            preview = service.stage_reconciliation(
                incoming,
                associations=(),
                session_id="order-session",
                base_revision=3,
            )

            service.apply_reconciliation(
                preview.operation_id,
                decisions=(),
                session_id="order-session",
                base_revision=3,
                incoming_source_identities=incoming.source_identities,
            )

            self.assertEqual(
                tuple(document.document_id for document in service.workspace.documents),
                (*current_ids, new_id),
            )
            self.assertEqual(
                tuple(document.source_ref for document in service.workspace.documents),
                ("first.json", "second.json", "new.json"),
            )
            self.assertEqual(
                tuple(item.project_global_index for item in service.flat_segments),
                (0, 1, 2),
            )
            self.assertEqual(
                tuple(item.identity.document_id for item in service.flat_segments),
                (*current_ids, new_id),
            )

    def test_keep_detached_allows_binding_to_omit_document_and_next_preview(self) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            detached_path = _write_localcat_json(
                root,
                "detached.json",
                "detached",
                "Detached source",
            )
            second = _write_localcat_json(root, "second.json", "second", "Second")
            third = _write_localcat_json(root, "third.json", "third", "Third")
            current = _stage(intake, root, (detached_path, second, third))
            detached_document_id = current.workspace.documents[0].document_id
            detached_identity = current.workspace.documents[0].segment_identities[0]
            incoming = _stage(
                intake,
                root,
                (second, third),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=current.origin_binding.revision,
                ),
            )
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="detach-session",
                revision=5,
            )
            preview = service.stage_reconciliation(
                incoming,
                associations=(),
                session_id="detach-session",
                base_revision=5,
            )
            self.assertEqual(preview.removed_identities, (detached_identity,))
            keep = workspace_module.ReconciliationDecision(
                identity=detached_identity,
                disposition=workspace_module.ReconciliationDisposition.KEEP_DETACHED,
            )

            service.apply_reconciliation(
                preview.operation_id,
                decisions=(keep,),
                session_id="detach-session",
                base_revision=5,
                incoming_source_identities=incoming.source_identities,
            )

            detached_document = next(
                document
                for document in service.workspace.documents
                if document.document_id == detached_document_id
            )
            self.assertEqual(
                tuple(
                    segment.source_presence
                    for segment in detached_document.source_segments
                ),
                (SourcePresence.DETACHED,),
            )
            self.assertNotIn(
                detached_document_id,
                tuple(
                    document.document_id
                    for document in service.origin_binding.documents
                ),
            )

            fresh = intake.revalidate_staged_selected_documents(incoming)
            next_preview = service.stage_reconciliation(
                fresh,
                associations=(),
                session_id="detach-session",
                base_revision=6,
            )
            self.assertIs(type(next_preview), workspace_module.ReconciliationPreview)
            self.assertIn(detached_identity, next_preview.removed_identities)

    def test_revalidated_source_change_after_preview_is_source_stale_zero_mutation(
        self,
    ) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "first.json", "first", "Version one")
            second = _write_localcat_json(root, "second.json", "second", "Second")
            current = _stage(intake, root, (first, second))
            _write_localcat_json(root, "first.json", "first", "Version two")
            incoming = _stage(
                intake,
                root,
                (first, second),
                request=_request(
                    intake,
                    origin_binding=current.origin_binding,
                    expected_binding_revision=current.origin_binding.revision,
                ),
            )
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="source-stale-session",
                revision=9,
            )
            preview = service.stage_reconciliation(
                incoming,
                associations=(),
                session_id="source-stale-session",
                base_revision=9,
            )
            before_workspace = service.workspace
            before_binding = service.origin_binding
            _write_localcat_json(root, "first.json", "first", "Version three")

            revalidated = intake.revalidate_staged_selected_documents(incoming)

            self.assertNotEqual(
                revalidated.source_identities,
                incoming.source_identities,
            )
            with self.assertRaises(ProjectWorkspaceError) as caught:
                service.apply_reconciliation(
                    preview.operation_id,
                    decisions=(),
                    session_id="source-stale-session",
                    base_revision=9,
                    incoming_source_identities=revalidated.source_identities,
                )
            self.assertEqual(
                caught.exception.code,
                "PROJECT.RECONCILE.SOURCE_STALE",
            )
            self.assertEqual(service.workspace, before_workspace)
            self.assertEqual(service.origin_binding, before_binding)
            self.assertEqual(service.revision, 9)

    def test_preview_and_receipt_reject_forged_project_id(self) -> None:
        intake = _cluster2a_intake()
        workspace_module = _cluster2a_workspace()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_localcat_json(root, "first.json", "first", "First")
            second = _write_localcat_json(root, "second.json", "second", "Second")
            current = _stage(intake, root, (first, second))
            incoming = intake.revalidate_staged_selected_documents(current)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="dto-session",
                revision=17,
            )
            preview = service.stage_reconciliation(
                incoming,
                associations=(),
                session_id="dto-session",
                base_revision=17,
            )
            with self.assertRaises(ProjectWorkspaceError):
                replace(preview, project_id="forged-project-id")
            receipt = service.apply_reconciliation(
                preview.operation_id,
                decisions=(),
                session_id="dto-session",
                base_revision=17,
                incoming_source_identities=incoming.source_identities,
            )
            self.assertIs(type(receipt), workspace_module.ReconciliationReceipt)
            with self.assertRaises(ProjectWorkspaceError):
                replace(receipt, project_id="forged-project-id")


class Cluster2AExplicitSelectedFilesFaultTests(unittest.TestCase):
    def test_root_symlink_and_mid_intake_root_swap_never_publish(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            root_link = base / "root-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises((ProjectWorkspaceError, TypeError, ValueError)):
                _stage(
                    module,
                    root_link,
                    (root_link / "first.txt", root_link / "second.po"),
                )

            original = ParserApplicationSurface.open_input
            moved_root = base / "moved-root"
            swapped = False

            def swap_then_open(surface, reference, selection, request, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    root.rename(moved_root)
                    root.mkdir()
                    shutil.copyfile(moved_root / "first.txt", root / "first.txt")
                    shutil.copyfile(moved_root / "second.po", root / "second.po")
                return original(surface, reference, selection, request, **kwargs)

            with mock.patch.object(
                ParserApplicationSurface,
                "open_input",
                swap_then_open,
            ):
                with self.assertRaises((ProjectWorkspaceError, TypeError, ValueError)):
                    _stage(module, root, (first, second))

    def test_missing_verified_terminal_and_late_fatal_leave_no_staged_candidate(self) -> None:
        module = _cluster2a_intake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_fixture(root, "first.txt", "line-text-valid.hex")
            second = _write_fixture(root, "second.po", "gettext-po-valid.po")
            original_materialize = OpenedParserInput.materialize

            def without_terminal(opened):
                result = original_materialize(opened)
                return replace(result, terminal=None)

            with mock.patch.object(
                OpenedParserInput,
                "materialize",
                without_terminal,
            ):
                with self.assertRaises((ProjectWorkspaceError, TypeError, ValueError)):
                    _stage(module, root, (first, second))

            fatal = _write_fixture(
                root,
                "fatal.po",
                "gettext-po-fatal-tail.po",
            )
            original_digests = {path: _sha256(path) for path in (first, fatal)}
            with self.assertRaises((ProjectWorkspaceError, TypeError, ValueError)) as caught:
                _stage(module, root, (first, fatal))
            self.assertNotIn(str(root), str(caught.exception))
            self.assertNotIn("First line", str(caught.exception))
            self.assertEqual(
                {path: _sha256(path) for path in original_digests},
                original_digests,
            )


if __name__ == "__main__":
    unittest.main()
