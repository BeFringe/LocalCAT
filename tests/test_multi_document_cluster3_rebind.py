"""Cluster 3 acceptance for explicit cold-package source rebind intake."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    OriginRenameMapping,
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
    stage_workspace_rebind,
)


def _write_document(path: Path, *, source: str, target: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": path.stem,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "segment-1",
                        "source": source,
                        "target": target,
                        "speaker": "",
                        "confirmed": bool(target),
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _initial_workspace(root: Path):
    first = _write_document(root / "a.json", source="old A", target="saved A")
    second = _write_document(root / "b.json", source="old B", target="saved B")
    return stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest(
            name="Cold project",
            source_locale="en",
            target_locale="zh-CN",
        ),
    ).workspace


class Cluster3WorkspaceRebindTests(unittest.TestCase):
    def test_exact_refs_preserve_manifest_ids_while_source_evidence_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c3-rebind-") as directory:
            base = Path(directory)
            workspace = _initial_workspace(base / "old-root")
            new_root = base / "new-root"
            new_root.mkdir()
            first = _write_document(new_root / "a.json", source="new A")
            second = _write_document(new_root / "b.json", source="new B")
            before = (first.read_bytes(), second.read_bytes())

            with mock.patch(
                "project_workspace_intake.stage_selected_project_documents",
                wraps=stage_selected_project_documents,
            ) as delegated:
                staged = stage_workspace_rebind(
                    new_root,
                    (second, first),
                    workspace,
                )

            delegated.assert_called_once()
            self.assertEqual(staged.workspace.project_id, workspace.project_id)
            self.assertEqual(
                tuple(document.document_id for document in staged.workspace.documents),
                (
                    workspace.documents[1].document_id,
                    workspace.documents[0].document_id,
                ),
            )
            self.assertEqual(
                tuple(document.document_id for document in staged.origin_binding.documents),
                (
                    workspace.documents[1].document_id,
                    workspace.documents[0].document_id,
                ),
            )
            self.assertEqual(
                tuple(document.source_segments[0].source for document in staged.workspace.documents),
                ("new B", "new A"),
            )
            self.assertNotEqual(
                tuple(document.source_snapshot_digest for document in staged.workspace.documents),
                tuple(document.source_snapshot_digest for document in workspace.documents),
            )
            self.assertEqual((first.read_bytes(), second.read_bytes()), before)

    def test_explicit_rename_preserves_only_the_authorized_document_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c3-rebind-") as directory:
            base = Path(directory)
            workspace = _initial_workspace(base / "old-root")
            first_document, second_document = workspace.documents
            new_root = base / "new-root"
            new_root.mkdir()
            renamed = _write_document(
                new_root / "renamed-a.json",
                source="replacement A",
            )
            second = _write_document(new_root / "b.json", source="replacement B")
            before = (renamed.read_bytes(), second.read_bytes())

            staged = stage_workspace_rebind(
                new_root,
                (renamed, second),
                workspace,
                rename_mappings=(
                    OriginRenameMapping(
                        old_source_ref="a.json",
                        new_source_ref="renamed-a.json",
                        document_id=first_document.document_id,
                    ),
                ),
            )

            self.assertEqual(
                tuple(
                    (document.source_ref, document.document_id)
                    for document in staged.workspace.documents
                ),
                (
                    ("renamed-a.json", first_document.document_id),
                    ("b.json", second_document.document_id),
                ),
            )
            self.assertEqual(
                staged.origin_binding.absolute_root,
                str(new_root),
            )
            self.assertEqual((renamed.read_bytes(), second.read_bytes()), before)

    def test_missing_extra_duplicate_and_forged_authority_reject_before_parse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c3-rebind-") as directory:
            base = Path(directory)
            workspace = _initial_workspace(base / "old-root")
            first_document, second_document = workspace.documents
            new_root = base / "new-root"
            new_root.mkdir()
            first = _write_document(new_root / "a.json", source="new A")
            second = _write_document(new_root / "b.json", source="new B")
            extra = _write_document(new_root / "c.json", source="extra")
            renamed = _write_document(new_root / "renamed-a.json", source="renamed")
            before = {
                path: path.read_bytes()
                for path in (first, second, extra, renamed)
            }
            wrong_document_id = second_document.document_id
            forged = OriginRenameMapping(
                old_source_ref="a.json",
                new_source_ref="renamed-a.json",
                document_id=wrong_document_id,
            )
            unused = OriginRenameMapping(
                old_source_ref="a.json",
                new_source_ref="renamed-a.json",
                document_id=first_document.document_id,
            )
            legitimate = OriginRenameMapping(
                old_source_ref="a.json",
                new_source_ref="renamed-a.json",
                document_id=first_document.document_id,
            )
            invalid_calls = (
                ((first,), (), "PROJECT.RECONCILE.INPUT_INVALID"),
                ((first, second, extra), (), "PROJECT.RECONCILE.INPUT_INVALID"),
                ((first, first), (), "PROJECT.WORKSPACE.IDENTITY_DUPLICATE"),
                ((renamed, second), (), "PROJECT.RECONCILE.INPUT_INVALID"),
                ((renamed, second), (forged,), "PROJECT.RECONCILE.INPUT_INVALID"),
                (
                    (renamed, second),
                    (legitimate, legitimate),
                    "PROJECT.RECONCILE.INPUT_INVALID",
                ),
                ((first, second), (unused,), "PROJECT.RECONCILE.INPUT_INVALID"),
            )

            with mock.patch(
                "project_workspace_intake.stage_selected_project_documents",
                side_effect=AssertionError("invalid authority reached Parser staging"),
            ) as delegated:
                for selected, mappings, expected_code in invalid_calls:
                    with self.subTest(selected=selected, mappings=mappings):
                        with self.assertRaises(ProjectWorkspaceError) as caught:
                            stage_workspace_rebind(
                                new_root,
                                selected,
                                workspace,
                                rename_mappings=mappings,
                            )
                        self.assertEqual(
                            caught.exception.code,
                            expected_code,
                        )

            delegated.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )


if __name__ == "__main__":
    unittest.main()
