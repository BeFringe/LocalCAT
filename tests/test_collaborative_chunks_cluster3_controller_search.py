from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chunk_controller_adapter import (
    ChunkControllerAdapter,
    ChunkControllerSessionMode,
)
from chunk_controller_contracts import (
    CollaborativeSearchScopeV2,
    CollaborativeWorkspaceSearchRequestV2,
)
from collaborative_chunk_contracts import (
    ChunkAccessKind,
    ChunkError,
    LocalReferenceManagerHandle,
    TopologyAction,
    chunk_plan_binding,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
)
from collaborative_chunk_store import CollaborativeChunkStore
from collaborative_chunk_workspace_adapter import capture_live_workspace_universe
from collaborative_chunks import (
    ChunkTopologyPublicationAuthority,
    LocalReferenceActorPort,
)
from editor_contracts import SearchField
from editor_controller import EditorControllerError
from project_package import ProjectPackageService
from project_save import ProjectSaveService
from project_workspace import ProjectWorkspaceService
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from tm_contracts import SearchOptions


class _Issuer:
    def __init__(self, kind: str, start: int) -> None:
        self.kind = kind
        self.value = start

    def __call__(self) -> str:
        seed = self.value.to_bytes(32, "big")
        self.value += 1
        if self.kind == "chunk":
            return issue_chunk_id(seed)
        if self.kind == "plan":
            return issue_chunk_plan_id(seed)
        return issue_chunk_operation_id(seed)


def _write_source(path: Path, name: str, rows: tuple[tuple[str, str], ...]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": local_id,
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                    for local_id, source in rows
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class CollaborativeChunkCluster3ControllerSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="localcat-chunk-c3-")
        self.root = Path(self.temporary.name).resolve()
        source_root = self.root / "source"
        source_root.mkdir()
        first = source_root / "a.json"
        second = source_root / "b.json"
        _write_source(
            first,
            "A",
            (("shared", "needle A"), ("a-tail", "outside A")),
        )
        _write_source(
            second,
            "B",
            (("shared", "needle B"), ("b-tail", "needle outside B")),
        )
        staged = stage_selected_project_documents(
            source_root,
            (first, second),
            SelectedProjectDocumentsRequest(
                name="C3",
                source_locale="en",
                target_locale="zh-CN",
            ),
        )
        export_owner = ProjectWorkspaceService(
            staged.workspace,
            staged.origin_binding,
            session_id="c3-export",
            revision=0,
        )
        self.package_path = self.root / "project.localcat-project"
        ProjectPackageService().export_workspace(
            ProjectSaveService(export_owner, baseline=None),
            self.package_path,
        )
        opened = ProjectPackageService().open(self.package_path)
        setup_owner = opened.create_workspace_service(
            session_id="c3-chunk-setup",
            revision=0,
        )
        self.metadata_root = self.root / "chunk-metadata"
        self.metadata_root.mkdir()
        store = CollaborativeChunkStore(
            self.metadata_root,
            "chunks.json",
            project_id=setup_owner.workspace.project_id,
        )
        authority = ChunkTopologyPublicationAuthority(
            project_id=setup_owner.workspace.project_id,
            workspace_binding_provider=(
                lambda: capture_live_workspace_universe(setup_owner).binding
            ),
            metadata_store=store,
        )
        topology = authority.create_topology_service(
            workspace_universe_provider=(
                lambda: capture_live_workspace_universe(setup_owner)
            ),
            chunk_id_issuer=_Issuer("chunk", 1),
            plan_id_issuer=_Issuer("plan", 100),
            operation_id_issuer=_Issuer("operation", 200),
        )
        assignment = authority.create_assignment_service(
            operation_id_issuer=_Issuer("operation", 300),
        )
        universe = capture_live_workspace_universe(setup_owner)
        by_identity = {
            (
                entry.segment.identity.document_id,
                entry.segment.identity.local_segment_id,
            ): entry.segment
            for entry in universe.entries
        }
        documents = setup_owner.workspace.documents
        member_a = by_identity[(documents[0].document_id, "shared")]
        member_b = by_identity[(documents[1].document_id, "shared")]
        member_other = by_identity[(documents[0].document_id, "a-tail")]
        manager = LocalReferenceManagerHandle("local", "manager")
        workspace_binding = universe.binding

        capability = authority.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        preview = topology.preview_create(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=None,
            name="Alpha",
            members=(member_a, member_b),
        )
        topology.apply_topology(
            preview,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=None,
        )
        self.chunk_a = preview.created_chunk_ids[0]

        expected = chunk_plan_binding(authority.current_snapshot())
        capability = authority.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected,
            action=TopologyAction.CREATE,
        )
        preview = topology.preview_create(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected,
            name="Beta",
            members=(member_other,),
        )
        topology.apply_topology(
            preview,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected,
        )
        self.chunk_b = preview.created_chunk_ids[0]

        self.actor = LocalReferenceActorPort("local", "alice")
        for chunk_id in (self.chunk_a, self.chunk_b):
            expected = chunk_plan_binding(authority.current_snapshot())
            capability = authority.issue_manager_capability(
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected,
                action=TopologyAction.ASSIGN,
            )
            assign = assignment.preview_assign(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected,
                chunk_id=chunk_id,
                target_actor_port=self.actor,
                target_actor_handle=self.actor.current_actor(),
            )
            assignment.apply_assignment(
                assign,
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected,
            )

        repository = ResourceRepository(self.root / "app-data")
        self.controller, composition = _compose_editor_controller(repository)
        owner = composition.matcher_validation_owner
        owner.validate_basic(
            generated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            valid_until_utc=datetime(2030, 1, 2, tzinfo=timezone.utc),
            evaluated_at_utc=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.adapter = ChunkControllerAdapter(
            self.controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=(
                lambda _project_id: (self.metadata_root, "chunks.json")
            ),
        )
        self.adapter.open_project_package(self.package_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _search(self, scope: CollaborativeSearchScopeV2):
        return self.adapter.search_workspace(
            CollaborativeWorkspaceSearchRequestV2(
                query="needle",
                fields=(SearchField.SOURCE,),
                options=SearchOptions(match_case=False, whole_word=False),
                scope=scope,
            )
        )

    def _fresh_adapter(self, metadata_root: Path, label: str):
        controller, _composition = _compose_editor_controller(
            ResourceRepository(self.root / f"app-data-{label}")
        )
        adapter = ChunkControllerAdapter(
            controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=(
                lambda _project_id: (metadata_root, "chunks.json")
            ),
        )
        adapter.open_project_package(self.package_path)
        return controller, adapter

    def _rename_plan(self, name: str, seed: int):
        authority = self.adapter._authority
        assert authority is not None
        topology = getattr(self, "_test_topology", None)
        if topology is None:
            topology = authority.create_topology_service(
                workspace_universe_provider=self.adapter._live_universe,
                chunk_id_issuer=_Issuer("chunk", seed),
                plan_id_issuer=_Issuer("plan", seed + 100),
                operation_id_issuer=_Issuer("operation", seed + 200),
            )
            self._test_topology = topology
        manager = LocalReferenceManagerHandle("local", f"manager-{seed}")
        workspace = self.adapter._live_universe().binding
        expected = chunk_plan_binding(authority.current_snapshot())
        capability = authority.issue_manager_capability(
            manager,
            workspace_binding=workspace,
            expected_plan_binding=expected,
            action=TopologyAction.RENAME,
        )
        preview = topology.preview_rename(
            capability,
            manager,
            workspace_binding=workspace,
            expected_plan_binding=expected,
            chunk_id=self.chunk_a,
            name=name,
        )
        topology.apply_topology(
            preview,
            capability,
            manager,
            workspace_binding=workspace,
            expected_plan_binding=expected,
        )
        return expected

    def test_assigned_current_target_and_confirm_use_permission_gate(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        before = self.controller.project_revision
        self.controller.update_workspace_target("译文")
        self.assertEqual(self.controller.current_segment.target, "译文")
        self.assertEqual(self.controller.project_revision, before + 1)
        result = self.controller.confirm_current()
        self.assertTrue(result.write_report.succeeded)
        self.assertTrue(
            self.controller.workspace_view.segments[0].confirmed
        )

    def test_outside_current_fails_before_workspace_mutation(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        outside = self.controller.workspace_view.segments[1].identity
        self.controller.go_to_workspace_segment(outside)
        before = (
            self.controller.project_revision,
            self.controller.current_segment.target,
            self.controller.workspace_global_index,
        )
        with self.assertRaises(EditorControllerError) as caught:
            self.controller.update_workspace_target("must not publish")
        self.assertEqual(caught.exception.args[0], "CHUNK.OUTSIDE_CURRENT")
        self.assertEqual(
            (
                self.controller.project_revision,
                self.controller.current_segment.target,
                self.controller.workspace_global_index,
            ),
            before,
        )

    def test_current_chunk_search_is_exact_cross_document_workspace_order(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        report = self._search(CollaborativeSearchScopeV2.CURRENT_CHUNK)
        self.assertEqual(len(report.hits), 2)
        self.assertEqual(
            [hit.workspace_hit.local_segment_id for hit in report.hits],
            ["shared", "shared"],
        )
        self.assertTrue(
            all(
                hit.access.access is ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT
                for hit in report.hits
            )
        )
        entire = self._search(CollaborativeSearchScopeV2.ENTIRE_PROJECT)
        self.assertEqual(len(entire.hits), 3)
        self.assertEqual(
            entire.hits[-1].access.access,
            ChunkAccessKind.READ_ONLY_UNALLOCATED,
        )

    def test_chunk_switch_a_b_a_revokes_old_search_hit(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        report = self._search(CollaborativeSearchScopeV2.CURRENT_CHUNK)
        old_hit = report.hits[0]
        self.adapter.select_current_chunk(self.chunk_b)
        self.adapter.select_current_chunk(self.chunk_a)
        with self.assertRaises(EditorControllerError) as caught:
            self.adapter.go_to_search_hit(old_hit)
        self.assertEqual(caught.exception.args[0], "PROJECT_SEARCH.HIT_NOT_ISSUED")
        with self.assertRaises(EditorControllerError) as raw:
            self.controller.go_to_workspace_search_hit(old_hit.workspace_hit)
        self.assertEqual(raw.exception.args[0], "PROJECT_SEARCH.HIT_NOT_ISSUED")

    def test_plan_revision_change_revokes_current_chunk_selection(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        before_generation = self.adapter.session_view.selection_generation
        expected = self._rename_plan("Alpha renamed", 600)
        refreshed = self.adapter.session_view
        self.assertIsNone(refreshed.current_chunk_id)
        self.assertEqual(refreshed.plan_revision, expected.plan_revision + 1)
        self.assertEqual(
            refreshed.selection_generation,
            before_generation + 1,
        )
        self._rename_plan("Alpha restored", 900)
        self.controller.update_workspace_target("whole project after revoke")
        self.assertEqual(
            self.controller.current_segment.target,
            "whole project after revoke",
        )
        with self.assertRaises(ChunkError) as caught:
            self._search(CollaborativeSearchScopeV2.CURRENT_CHUNK)
        self.assertEqual(caught.exception.code, "CHUNK.OUTSIDE_CURRENT")
        self.assertIsNone(self.adapter.session_view.current_chunk_id)

    def test_plan_drift_revokes_already_prepared_edit_after_plan_restores(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        service = self.controller._workspace_owner_for_chunk_controller()
        identity = self.controller.current_workspace_identity.segment_identity
        preparation = self.controller._prepare_workspace_chunk_edit(
            service,
            identity,
            target="prepared target",
            confirmed=False,
        )
        self._rename_plan("Temporary drift", 1000)
        self.assertIsNone(self.adapter.session_view.current_chunk_id)
        self._rename_plan("Alpha restored again", 1100)
        with self.assertRaises(EditorControllerError) as caught:
            self.controller._commit_workspace_chunk_edit(
                preparation,
                service,
                identity,
                target="prepared target",
                confirmed=False,
            )
        self.assertEqual(caught.exception.args[0], "CHUNK.PERMISSION_STALE")
        self.assertEqual(self.controller.current_segment.target, "")

    def test_confirm_revalidates_before_any_tm_publication(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        self.controller.update_workspace_target("ready")
        original_prepare = self.controller._prepare_workspace_chunk_edit

        def prepare_then_lose_actor(*args, **kwargs):
            prepared = original_prepare(*args, **kwargs)
            self.actor.set_available(False)
            return prepared

        tm_adapter = self.controller._tm_adapter
        assert tm_adapter is not None
        append_confirmed = type(tm_adapter).append_confirmed
        try:
            with mock.patch.object(
                self.controller,
                "_prepare_workspace_chunk_edit",
                side_effect=prepare_then_lose_actor,
            ), mock.patch.object(
                type(tm_adapter),
                "append_confirmed",
                autospec=True,
                side_effect=(
                    lambda instance, *args, **kwargs: append_confirmed(
                        instance,
                        *args,
                        **kwargs,
                    )
                ),
            ) as publish:
                with self.assertRaises(EditorControllerError) as caught:
                    self.controller.confirm_current()
                self.assertEqual(caught.exception.args[0], "CHUNK.ACTOR_UNAVAILABLE")
                publish.assert_not_called()
        finally:
            self.actor.set_available(True)
        self.assertFalse(self.controller.current_segment.confirmed)

    def test_workspace_package_save_does_not_publish_chunk_metadata(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        metadata_path = self.metadata_root / "chunks.json"
        metadata_before = metadata_path.read_bytes()
        self.controller.update_workspace_target("saved workspace edit")
        result = self.controller.save_workspace_package()
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertEqual(
            result.package_artifact_digest,
            result.receipt.artifact_digest,
        )

        controller, adapter = self._fresh_adapter(
            self.metadata_root,
            "package-cold-reopen",
        )
        self.assertEqual(adapter.session_view.mode, ChunkControllerSessionMode.ACTIVE)
        self.assertEqual(
            controller.current_segment.target,
            "saved workspace edit",
        )
        adapter.select_current_chunk(self.chunk_a)
        self.assertEqual(adapter.session_view.current_chunk_id, self.chunk_a)

    def test_missing_metadata_is_compatible_personal_mode(self) -> None:
        empty = self.root / "empty-metadata"
        empty.mkdir()
        controller, adapter = self._fresh_adapter(empty, "empty")
        view = adapter.session_view
        self.assertEqual(view.mode, ChunkControllerSessionMode.NO_PLAN)
        controller.update_workspace_target("personal")
        self.assertEqual(controller.current_segment.target, "personal")

    def test_invalid_metadata_blocks_writes_without_replacing_workspace(self) -> None:
        invalid = self.root / "invalid-metadata"
        invalid.mkdir()
        (invalid / "chunks.json").write_bytes(b"{}")
        controller, adapter = self._fresh_adapter(invalid, "invalid")
        project = controller.workspace_view.project
        revision = controller.project_revision
        view = adapter.session_view
        self.assertEqual(view.mode, ChunkControllerSessionMode.BLOCKED)
        self.assertIs(controller.workspace_view.project, project)
        with self.assertRaises(EditorControllerError):
            controller.update_workspace_target("blocked")
        self.assertEqual(controller.project_revision, revision)

    def test_canonical_metadata_binding_cannot_downgrade_to_empty_root(self) -> None:
        binding = [self.metadata_root]
        controller, _composition = _compose_editor_controller(
            ResourceRepository(self.root / "app-data-binding")
        )
        adapter = ChunkControllerAdapter(
            controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=lambda _project_id: (
                binding[0],
                "chunks.json",
            ),
        )
        adapter.open_project_package(self.package_path)
        adapter.select_current_chunk(self.chunk_a)
        empty = self.root / "alternate-empty"
        empty.mkdir()
        binding[0] = empty
        view = adapter.bind_current_workspace_metadata()
        self.assertEqual(view.mode, ChunkControllerSessionMode.BLOCKED)
        outside = controller.workspace_view.segments[1].identity
        controller.go_to_workspace_segment(outside)
        with self.assertRaises(EditorControllerError):
            controller.update_workspace_target("must stay blocked")

    def test_metadata_resolver_failure_is_body_safe_and_fail_closed(self) -> None:
        controller, _composition = _compose_editor_controller(
            ResourceRepository(self.root / "app-data-resolver-failure")
        )

        def fail(_project_id: str):
            raise OSError("/private/secret/chunks denied")

        adapter = ChunkControllerAdapter(
            controller,
            self.actor,
            self.actor.current_actor(),
            metadata_binding_resolver=fail,
        )
        adapter.open_project_package(self.package_path)
        self.assertEqual(adapter.session_view.mode, ChunkControllerSessionMode.BLOCKED)
        self.assertEqual(
            adapter.session_view.safe_code,
            "CHUNK.RECOVERY_REQUIRED",
        )
        with self.assertRaises(EditorControllerError) as caught:
            controller.update_workspace_target("blocked")
        self.assertNotIn("secret", str(caught.exception))

    def test_foreign_chunk_selection_preserves_navigation_and_selection(self) -> None:
        self.adapter.select_current_chunk(self.chunk_a)
        before = (
            self.adapter.session_view,
            self.controller.workspace_global_index,
            self.controller.current_segment.target,
        )
        foreign = issue_chunk_id(b"F" * 32)
        with self.assertRaises(ChunkError) as caught:
            self.adapter.select_current_chunk(foreign)
        self.assertEqual(caught.exception.code, "CHUNK.IDENTITY_FOREIGN")
        self.assertEqual(
            (
                self.adapter.session_view,
                self.controller.workspace_global_index,
                self.controller.current_segment.target,
            ),
            before,
        )

    def test_c3_composition_modules_keep_exact_local_import_lanes(self) -> None:
        expected = {
            "chunk_controller_contracts.py": {
                "collaborative_chunk_contracts",
                "editor_contracts",
                "tm_contracts",
            },
            "chunk_controller_adapter.py": {
                "chunk_controller_contracts",
                "collaborative_chunk_contracts",
                "collaborative_chunk_store",
                "collaborative_chunk_workspace_adapter",
                "collaborative_chunks",
                "editor_contracts",
                "editor_controller",
                "project_workspace",
                "project_workspace_contracts",
                "project_workspace_identity",
            },
            "collaborative_chunk_conflict.py": {
                "collaborative_chunk_contracts",
                "collaborative_chunk_store",
                "collaborative_chunks",
            },
        }
        root = Path(__file__).parents[1]
        for filename, allowed in expected.items():
            with self.subTest(filename=filename):
                tree = ast.parse(root.joinpath(filename).read_text(encoding="utf-8"))
                local = {
                    node.module.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module
                    and root.joinpath(
                        node.module.split(".")[0] + ".py"
                    ).is_file()
                }
                self.assertEqual(local, allowed)


if __name__ == "__main__":
    unittest.main()
