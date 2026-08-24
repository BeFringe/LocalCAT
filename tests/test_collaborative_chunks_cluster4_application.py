from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chunk_controller_adapter import ChunkControllerAdapter
from chunk_controller_contracts import ChunkApplicationMode
from collaborative_chunk_contracts import ChunkError
from collaborative_chunks import LocalReferenceActorPort
from project_package import ProjectPackageService
from project_save import ProjectSaveService
from project_workspace import (
    ProjectWorkspaceService,
    ReconciliationDecision,
    ReconciliationDisposition,
)
from project_workspace_contracts import SegmentIdentity
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    revalidate_staged_selected_documents,
    stage_selected_project_documents,
)
from editor_controller import EditorControllerError
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository


def _source(path: Path, name: str, rows: tuple[tuple[str, str], ...]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": identity,
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                    for identity, source in rows
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


class CollaborativeChunkCluster4ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chunk-c4-app-")
        self.root = Path(self.temporary.name).resolve()
        sources = self.root / "sources"
        sources.mkdir()
        first = sources / "a.json"
        second = sources / "b.json"
        self.sources = sources
        self.first = first
        self.second = second
        _source(first, "A", (("001", "one"), ("002", "two")))
        _source(second, "B", (("001", "three"), ("002", "four")))
        staged = stage_selected_project_documents(
            sources,
            (first, second),
            SelectedProjectDocumentsRequest(
                name="C4", source_locale="en", target_locale="zh-CN"
            ),
        )
        export = ProjectWorkspaceService(
            staged.workspace, staged.origin_binding, session_id="c4-export", revision=0
        )
        self.package = self.root / "c4.localcat-project"
        ProjectPackageService().export_workspace(
            ProjectSaveService(export, baseline=None), self.package
        )
        self.package_bytes = self.package.read_bytes()
        self.metadata = self.root / "metadata"
        self.metadata.mkdir()
        controller, composition = _compose_editor_controller(
            ResourceRepository(self.root / "app-data")
        )
        composition.matcher_validation_owner.validate_basic(
            generated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            valid_until_utc=datetime(2030, 1, 2, tzinfo=timezone.utc),
            evaluated_at_utc=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.actor = LocalReferenceActorPort("local", "c4-user")
        self.adapter = ChunkControllerAdapter(
            controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=lambda _project: (self.metadata, "chunks.json"),
        )
        self.adapter.open_project_package(self.package)
        self.controller = controller
        self.owner = controller._workspace_owner_for_chunk_controller()
        self.documents = self.owner.workspace.documents

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _identity(self, document: int, local: str) -> SegmentIdentity:
        return SegmentIdentity(self.documents[document].document_id, local)

    def test_projection_preview_apply_replay_progress_access_and_cold_reopen(self) -> None:
        initial = self.adapter.project_view()
        self.assertIs(initial.mode, ChunkApplicationMode.NO_PLAN)
        self.assertTrue(initial.current_segment_access.may_edit_target)
        self.assertEqual(initial.unallocated_count, 4)

        members = (self._identity(0, "001"), self._identity(1, "001"))
        preview = self.adapter.preview_create_chunk("Cross document", members)
        self.assertEqual(preview.affected_member_count, 2)
        with self.assertRaises(ChunkError) as clone_error:
            self.adapter.apply_mutation(replace(preview))
        self.assertEqual(clone_error.exception.code, "CHUNK.PREVIEW_STALE")
        receipt = self.adapter.apply_mutation(preview)
        self.assertEqual(receipt.action, "create")
        with self.assertRaises(ChunkError) as replay_error:
            self.adapter.apply_mutation(preview)
        self.assertEqual(replay_error.exception.code, "CHUNK.PREVIEW_STALE")

        view = self.adapter.project_view()
        self.assertIs(view.mode, ChunkApplicationMode.ACTIVE)
        self.assertEqual(view.chunks[0].member_count, 2)
        self.assertEqual(view.chunks[0].progress.unfilled, 2)
        self.assertEqual(view.unallocated_count, 2)
        chunk_id = view.chunks[0].chunk_id
        self.adapter.apply_mutation(
            self.adapter.preview_assign_to_current_reference(chunk_id)
        )
        self.adapter.select_current_chunk(chunk_id)
        access = self.adapter.project_view().current_segment_access
        self.assertTrue(access.may_edit_target)
        self.assertEqual(access.access, "editable_assigned_current")
        self.controller.update_workspace_target("translated")
        progress = self.adapter.project_view().chunks[0].progress
        self.assertEqual(progress.draft, 1)

        self.assertEqual(self.package.read_bytes(), self.package_bytes)
        cold_controller, _ = _compose_editor_controller(
            ResourceRepository(self.root / "cold-app-data")
        )
        cold = ChunkControllerAdapter(
            cold_controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=lambda _project: (self.metadata, "chunks.json"),
        )
        cold.open_project_package(self.package)
        cold_view = cold.project_view()
        self.assertEqual(len(cold_view.chunks), 1)
        self.assertEqual(cold_view.chunks[0].name, "Cross document")

    def test_dynamic_project_partition_and_even_split_do_not_depend_on_editor_selection(self) -> None:
        self.assertEqual(
            self.adapter._balanced_groups(tuple(range(27)), 4),
            (
                tuple(range(0, 7)),
                tuple(range(7, 14)),
                tuple(range(14, 21)),
                tuple(range(21, 27)),
            ),
        )
        choices = self.adapter.segment_choices()
        self.assertEqual(len(choices), 4)
        self.assertTrue(all(choice.attached for choice in choices))
        preview = self.adapter.preview_partition_project(("前半", "后半"))
        self.assertEqual(preview.created_chunk_count, 2)
        self.assertEqual(preview.affected_member_count, 4)
        self.adapter.apply_mutation(preview)
        view = self.adapter.project_view()
        self.assertEqual(
            tuple((chunk.name, chunk.member_count) for chunk in view.chunks),
            (("前半", 2), ("后半", 2)),
        )
        self.assertEqual(
            tuple(choice.chunk_label for choice in self.adapter.segment_choices()),
            ("前半", "前半", "后半", "后半"),
        )

        first_id = view.chunks[0].chunk_id
        self.adapter.apply_mutation(
            self.adapter.preview_assign_to_current_reference(first_id)
        )
        split = self.adapter.preview_split_chunk_evenly(
            first_id,
            ("前半 A", "前半 B"),
            "inherit",
        )
        self.assertEqual(split.created_chunk_count, 2)
        self.assertEqual(split.assignment_count, 2)
        self.adapter.apply_mutation(split)
        after = self.adapter.project_view()
        self.assertEqual(len(after.chunks), 3)
        self.assertEqual(
            tuple(chunk.member_count for chunk in after.chunks[:2]),
            (1, 1),
        )
        self.assertTrue(
            all(
                chunk.assigned_to_current_reference
                for chunk in after.chunks[:2]
            )
        )
        self.assertEqual(
            tuple(choice.chunk_label for choice in self.adapter.segment_choices()),
            ("前半 A", "前半 B", "后半", "后半"),
        )

    def test_merge_blank_name_is_defaulted_by_the_application(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_partition_project(("前半", "后半"))
        )
        chunks = self.adapter.project_view().chunks
        preview = self.adapter.preview_merge_chunks(
            tuple(chunk.chunk_id for chunk in chunks),
            None,
        )
        self.adapter.apply_mutation(preview)
        merged = self.adapter.project_view().chunks
        self.assertEqual(
            tuple((chunk.name, chunk.member_count) for chunk in merged),
            (("合并分工", 4),),
        )

    def test_segment_choices_and_partition_follow_workspace_document_order(self) -> None:
        first, second = self.owner.workspace.documents
        self.owner._workspace = replace(
            self.owner.workspace,
            documents=(
                replace(second, order=0),
                replace(first, order=1),
            ),
        )
        choices = self.adapter.segment_choices()
        self.assertEqual(
            tuple(choice.document_label for choice in choices),
            ("B", "B", "A", "A"),
        )
        self.adapter.apply_mutation(
            self.adapter.preview_partition_project(("项目前半", "项目后半"))
        )
        self.assertEqual(
            tuple(choice.chunk_label for choice in self.adapter.segment_choices()),
            ("项目前半", "项目前半", "项目后半", "项目后半"),
        )

    def test_rename_then_current_head_undo_and_transition_seam_fail_closed(self) -> None:
        create = self.adapter.preview_create_chunk(
            "Original", (self._identity(0, "001"), self._identity(0, "002"))
        )
        self.adapter.apply_mutation(create)
        chunk_id = self.adapter.project_view().chunks[0].chunk_id
        self.adapter.apply_mutation(
            self.adapter.preview_rename_chunk(chunk_id, "Renamed")
        )
        undo = self.adapter.preview_undo_current_head()
        self.assertEqual(undo.classification, "current_head")
        self.adapter.apply_mutation(undo)
        self.assertEqual(self.adapter.project_view().chunks[0].name, "Original")

        with self.assertRaises(ChunkError) as foreign:
            self.adapter.capture_workspace_transition(object(), object())  # type: ignore[arg-type]
        self.assertEqual(foreign.exception.code, "CHUNK.IDENTITY_FOREIGN")
        self.adapter.workspace_closed()
        self.assertEqual(self.adapter.session_view.safe_code, "CHUNK.WORKSPACE_UNBOUND")

    def test_controller_reconciliation_captures_transition_and_requires_rebase(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Source-sensitive",
                (self._identity(0, "001"), self._identity(1, "001")),
            )
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first, self.second),
        )
        bind_preview = self.controller.preview_workspace_reconciliation(staged)
        self.controller.apply_workspace_reconciliation(bind_preview, staged)
        compatible = self.adapter.project_view()
        self.assertIs(compatible.mode, ChunkApplicationMode.ACTIVE)

        _source(
            self.second,
            "B",
            (("001", "three"), ("002", "four"), ("003", "new")),
        )
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        self.controller.apply_workspace_reconciliation(preview, incoming)
        blocked = self.adapter.project_view()
        self.assertIs(blocked.mode, ChunkApplicationMode.BLOCKED)
        self.assertEqual(blocked.safe_code, "CHUNK.REBASE_REQUIRED")
        self.controller.save_workspace_package()
        cold_controller, _ = _compose_editor_controller(
            ResourceRepository(self.root / "rebase-cold-app-data")
        )
        cold = ChunkControllerAdapter(
            cold_controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=lambda _project: (
                self.metadata,
                "chunks.json",
            ),
        )
        cold.open_project_package(self.package)
        cold_blocked = cold.project_view()
        self.assertIs(cold_blocked.mode, ChunkApplicationMode.BLOCKED)
        self.assertEqual(cold_blocked.safe_code, "CHUNK.REBASE_REQUIRED")
        with self.assertRaisesRegex(
            EditorControllerError,
            "^CHUNK\\.REBASE_REQUIRED$",
        ):
            cold_controller.update_workspace_target("must not publish")
        receipt = cold.apply_mutation(
            cold.preview_workspace_rebase()
        )
        self.assertEqual(receipt.action, "rebase")
        active = cold.project_view()
        self.assertIs(active.mode, ChunkApplicationMode.ACTIVE)
        self.assertEqual(active.chunks[0].member_count, 2)

    def test_post_commit_transition_capture_fault_installs_workspace_and_blocks_chunk(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Fault guarded",
                (self._identity(0, "001"), self._identity(1, "001")),
            )
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first, self.second),
        )
        _source(
            self.second,
            "B",
            (("001", "three"), ("002", "four"), ("003", "new")),
        )
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        with patch.object(
            self.adapter,
            "capture_workspace_transition",
            side_effect=ChunkError("CHUNK.PREVIEW_STALE"),
        ):
            receipt = self.controller.apply_workspace_reconciliation(
                preview,
                incoming,
            )

        self.assertEqual(receipt.published_revision, 1)
        self.assertEqual(self.controller.project_revision, 1)
        self.assertEqual(len(self.controller.workspace_view.segments), 5)
        blocked = self.adapter.project_view()
        self.assertIs(blocked.mode, ChunkApplicationMode.BLOCKED)
        self.assertEqual(blocked.safe_code, "CHUNK.RECOVERY_REQUIRED")
        with self.assertRaisesRegex(
            EditorControllerError,
            "^CHUNK\\.RECOVERY_REQUIRED$",
        ):
            self.controller.update_workspace_target("must remain blocked")

    def test_rebase_requires_explicit_missing_release_and_all_empty_can_dissolve(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Will disappear",
                (self._identity(1, "001"),),
            )
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first, self.second),
        )
        _source(self.second, "B", (("002", "four"),))
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        self.controller.apply_workspace_reconciliation(
            preview,
            incoming,
            decisions=(
                ReconciliationDecision(
                    identity=self._identity(1, "001"),
                    disposition=ReconciliationDisposition.REMOVE,
                ),
            ),
        )
        inspection = self.adapter.inspect_workspace_rebase()
        self.assertEqual(
            inspection.missing_members,
            (self._identity(1, "001"),),
        )
        self.assertTrue(inspection.all_chunks_empty)
        with self.assertRaisesRegex(
            ChunkError,
            "^CHUNK\\.REBASE_DECISION_REQUIRED$",
        ):
            self.adapter.preview_workspace_rebase()
        with self.assertRaisesRegex(
            ChunkError,
            "^CHUNK\\.REBASE_DECISION_REQUIRED$",
        ):
            self.adapter.preview_workspace_rebase(
                inspection.missing_members,
                inspection.empty_chunk_ids,
            )

        dissolve = self.adapter.preview_dissolve_plan()
        self.assertEqual(dissolve.action, "dissolve_plan")
        self.adapter.apply_mutation(dissolve)
        self.assertIs(
            self.adapter.project_view().mode,
            ChunkApplicationMode.NO_PLAN,
        )

    def test_partial_missing_rebase_publishes_only_explicit_exact_decisions(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Retained",
                (self._identity(1, "001"), self._identity(1, "002")),
            )
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first, self.second),
        )
        _source(self.second, "B", (("002", "four"), ("003", "new")))
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        self.controller.apply_workspace_reconciliation(
            preview,
            incoming,
            decisions=(
                ReconciliationDecision(
                    identity=self._identity(1, "001"),
                    disposition=ReconciliationDisposition.REMOVE,
                ),
            ),
        )
        inspection = self.adapter.inspect_workspace_rebase()
        self.assertFalse(inspection.all_chunks_empty)
        self.assertEqual(inspection.empty_chunk_ids, ())
        with self.assertRaisesRegex(
            ChunkError,
            "^CHUNK\\.REBASE_DECISION_REQUIRED$",
        ):
            self.adapter.preview_workspace_rebase((), ())
        rebase = self.adapter.preview_workspace_rebase(
            inspection.missing_members,
            inspection.empty_chunk_ids,
        )
        self.adapter.apply_mutation(rebase)
        active = self.adapter.project_view()
        self.assertIs(active.mode, ChunkApplicationMode.ACTIVE)
        self.assertEqual(active.chunks[0].member_count, 1)
        self.assertEqual(active.unallocated_count, 3)

    def test_same_project_package_universe_change_is_rejected_before_publish(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Protected",
                (self._identity(0, "001"), self._identity(1, "001")),
            )
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first, self.second),
        )
        _source(
            self.second,
            "B",
            (("001", "three"), ("002", "four"), ("003", "new")),
        )
        incoming = revalidate_staged_selected_documents(staged)
        candidate = ProjectWorkspaceService(
            incoming.workspace,
            incoming.origin_binding,
            session_id="same-project-candidate",
            revision=1,
        )
        incoming_package = self.root / "same-project.localcat-project"
        ProjectPackageService().export_workspace(
            ProjectSaveService(candidate, baseline=None),
            incoming_package,
        )
        before_package = self.package.read_bytes()
        preview = self.controller.preview_workspace_package_import(
            incoming_package
        )
        with self.assertRaisesRegex(
            EditorControllerError,
            "^CHUNK\\.REBASE_REQUIRED$",
        ):
            self.controller.apply_workspace_package_import(preview)
        self.assertEqual(self.package.read_bytes(), before_package)
        self.assertEqual(len(self.controller.workspace_view.segments), 4)
        self.assertIs(
            self.adapter.project_view().mode,
            ChunkApplicationMode.ACTIVE,
        )

    def test_metadata_conflict_preview_requires_explicit_replace(self) -> None:
        self.adapter.apply_mutation(
            self.adapter.preview_create_chunk(
                "Common",
                (self._identity(0, "001"), self._identity(1, "001")),
            )
        )
        common_payload = self.metadata.joinpath("chunks.json").read_bytes()
        chunk_id = self.adapter.project_view().chunks[0].chunk_id
        self.adapter.apply_mutation(
            self.adapter.preview_rename_chunk(chunk_id, "Current")
        )

        incoming_root = self.root / "incoming-metadata"
        incoming_root.mkdir()
        incoming_root.joinpath("chunks.json").write_bytes(common_payload)
        incoming_controller, _ = _compose_editor_controller(
            ResourceRepository(self.root / "incoming-app-data")
        )
        incoming = ChunkControllerAdapter(
            incoming_controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=lambda _project: (
                incoming_root,
                "chunks.json",
            ),
        )
        incoming.open_project_package(self.package)
        incoming_chunk = incoming.project_view().chunks[0].chunk_id
        incoming.apply_mutation(
            incoming.preview_rename_chunk(incoming_chunk, "Incoming")
        )
        payload = incoming_root.joinpath("chunks.json").read_bytes()

        conflict = self.adapter.preview_metadata_conflict(payload)
        self.assertEqual(conflict.classification, "diverged")
        self.assertEqual(conflict.affected_chunk_count, 1)
        receipt = self.adapter.apply_mutation(
            conflict,
            conflict_resolution="replace_incoming",
        )
        self.assertEqual(receipt.action, "conflict_replace")
        self.assertEqual(self.adapter.project_view().chunks[0].name, "Incoming")


if __name__ == "__main__":
    unittest.main()
