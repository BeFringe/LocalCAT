"""Cluster 3 application acceptance over a real Cluster 2 ProjectPackage."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import cast
from unittest import mock
import unittest

from editor_contracts import (
    ProjectSearchHit,
    ProjectSearchRequest,
    RecentProject,
    RecentWorkspaceProject,
    SearchField,
    SearchScope,
    WorkspaceSearchHit,
    WorkspaceSearchRequest,
)
from editor_controller import (
    ControllerWorkspaceConfirmResult,
    ControllerWorkspaceSaveResult,
    EditorController,
    EditorControllerError,
)
from project_package import (
    PreparedProjectPackageImport,
    ProjectPackageImportMode,
    ProjectPackageService,
)
from project_save import (
    DocumentSaveStatus,
    ProjectSaveService,
    SaveJournalState,
    SaveScope,
)
from project_workspace import PreparedReconciliationToken, ProjectWorkspaceService
from project_workspace_contracts import SegmentIdentity
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    StagedSelectedProjectDocuments,
    revalidate_staged_selected_documents,
    stage_selected_project_documents,
)
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from tm_contracts import SearchOptions


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_BASIC_OPTIONS = SearchOptions(match_case=False, whole_word=False)


def _write_source(
    path: Path,
    *,
    name: str,
    segments: tuple[tuple[str, str, str, bool], ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


class Cluster3ApplicationTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    root: Path = cast(Path, cast(object, None))
    package_path: Path = cast(Path, cast(object, None))
    first_source: Path = cast(Path, cast(object, None))
    second_source: Path = cast(Path, cast(object, None))
    source_bytes: tuple[bytes, bytes] = ()
    package_service: ProjectPackageService = cast(
        ProjectPackageService,
        cast(object, None),
    )
    initial_staged: StagedSelectedProjectDocuments = cast(
        StagedSelectedProjectDocuments,
        cast(object, None),
    )
    controller: EditorController = cast(EditorController, cast(object, None))
    composition: object = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-multidoc-c3-",
        )
        self.root = Path(self.temporary.name).resolve()
        source_root = self.root / "sources"
        source_root.mkdir()
        self.first_source = source_root / "chapters" / "a.json"
        self.second_source = source_root / "chapters" / "b.json"
        _write_source(
            self.first_source,
            name="Chapter A",
            segments=(
                ("shared", "needle in A", "A saved", True),
                ("a-tail", "A tail", "", False),
            ),
        )
        _write_source(
            self.second_source,
            name="Chapter B",
            segments=(
                ("shared", "needle in B", "B saved", True),
                ("b-tail", "needle closing", "", False),
            ),
        )
        self.source_bytes = (
            self.first_source.read_bytes(),
            self.second_source.read_bytes(),
        )
        staged = stage_selected_project_documents(
            source_root,
            (self.first_source, self.second_source),
            SelectedProjectDocumentsRequest(
                name="Cluster 3 project",
                source_locale="en",
                target_locale="zh-CN",
            ),
        )
        self.initial_staged = staged
        workspace_service = ProjectWorkspaceService(
            staged.workspace,
            staged.origin_binding,
            session_id="cluster3-export-session",
            revision=0,
        )
        save_service = ProjectSaveService(workspace_service, baseline=None)
        self.package_service = ProjectPackageService()
        self.package_path = self.root / "project.localcat-project"
        exported = self.package_service.export_workspace(
            save_service,
            self.package_path,
        )
        self.assertIsNotNone(exported.receipt)
        self.assertIsNotNone(exported.persistence_binding)
        repository = ResourceRepository(self.root / "app-data")
        self.controller, self.composition = _compose_editor_controller(repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _validate_basic_matcher(self) -> None:
        owner = self.composition.matcher_validation_owner  # type: ignore[union-attr]
        owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def _open(self):
        opened_view = self.controller.open_project_package(self.package_path)
        self.assertEqual(
            opened_view.project.project_id,
            self.package_service.open(self.package_path).workspace.project_id,
        )
        return self.package_service.open(self.package_path).workspace

    def _document_ids(self) -> tuple[str, str]:
        documents = self.controller.workspace_view.documents
        self.assertEqual(len(documents), 2)
        return (
            documents[0].identity.document_id,
            documents[1].identity.document_id,
        )

    def _export_foreign_package(self) -> Path:
        source_root = self.root / "foreign-sources"
        source_root.mkdir()
        first = source_root / "foreign-a.json"
        second = source_root / "foreign-b.json"
        _write_source(
            first,
            name="Foreign A",
            segments=(("shared", "foreign source A", "foreign target A", True),),
        )
        _write_source(
            second,
            name="Foreign B",
            segments=(("shared", "foreign source B", "foreign target B", True),),
        )
        staged = stage_selected_project_documents(
            source_root,
            (first, second),
            SelectedProjectDocumentsRequest(
                name="Foreign project",
                source_locale="en",
                target_locale="zh-CN",
            ),
        )
        service = ProjectWorkspaceService(
            staged.workspace,
            staged.origin_binding,
            session_id="foreign-export-session",
            revision=0,
        )
        destination = self.root / "foreign.localcat-project"
        ProjectPackageService().export_workspace(
            ProjectSaveService(service, baseline=None),
            destination,
        )
        return destination

    def _export_same_project_package(self) -> Path:
        destination = self.root / "same-project-incoming.localcat-project"
        destination.write_bytes(self.package_path.read_bytes())
        package_service = ProjectPackageService()
        opened = package_service.open(destination)
        save_service = opened.create_save_service(
            session_id="same-project-export-session",
            revision=0,
        )
        service = save_service.workspace_service
        service.update_segment_edit(
            service.flat_segments[0].identity,
            target="incoming same-project target",
            confirmed=False,
            session_id=service.session_id,
            base_revision=service.revision,
        )
        result = package_service.save_workspace(
            save_service,
            destination,
            persistence_binding=opened.persistence_binding,
        )
        self.assertIsNotNone(result.receipt)
        return destination

    def test_search_scope_contract_is_workspace_only_and_legacy_shape_is_exact(
        self,
    ) -> None:
        self.assertEqual(
            tuple(item.value for item in SearchScope),
            ("current_document", "entire_project"),
        )
        self.assertFalse(hasattr(SearchScope, "CURRENT_CHUNK"))
        self.assertEqual(
            tuple(item.name for item in fields(ProjectSearchRequest)),
            ("query", "fields", "options", "status"),
        )
        self.assertEqual(
            tuple(item.name for item in fields(ProjectSearchHit)),
            (
                "segment_id",
                "segment_index",
                "field",
                "start_index",
                "end_index",
                "preview",
            ),
        )
        self.assertEqual(
            tuple(item.name for item in fields(RecentProject)),
            ("path", "segment_id", "index"),
        )
        self.assertEqual(
            tuple(item.name for item in fields(RecentWorkspaceProject)),
            (
                "path",
                "project_id",
                "document_id",
                "local_segment_id",
                "index",
            ),
        )
        legacy = ProjectSearchRequest(
            query="needle",
            fields=(SearchField.SOURCE,),
            options=_BASIC_OPTIONS,
        )
        self.assertFalse(hasattr(legacy, "scope"))
        request = WorkspaceSearchRequest(
            query="needle",
            fields=(SearchField.SOURCE,),
            options=_BASIC_OPTIONS,
        )
        self.assertIs(request.scope, SearchScope.ENTIRE_PROJECT)
        with self.assertRaises(TypeError):
            replace(request, scope="current_document")

    def test_real_package_cold_open_preserves_composite_identity_navigation_and_progress(
        self,
    ) -> None:
        workspace = self._open()
        first_id, second_id = self._document_ids()
        identities = self.controller.workspace_segment_identities
        self.assertEqual(
            tuple(identity.segment_identity for identity in identities),
            tuple(
                segment.identity
                for document in workspace.documents
                for segment in document.segments
            ),
        )
        self.assertEqual(
            tuple(identity.local_segment_id for identity in identities),
            ("shared", "a-tail", "shared", "b-tail"),
        )
        self.assertEqual(len(set(identities)), 4)
        self.assertEqual(self.controller.project_session_id != "", True)
        self.assertEqual(self.controller.project_revision, 0)
        self.assertEqual(self.controller.current_workspace_identity, identities[0])
        issued_first = self.controller.current_workspace_identity
        self.assertEqual(self.controller.current_workspace_document_id, first_id)
        self.assertEqual(self.controller.workspace_global_index, 0)

        self.controller.go_to_workspace_index(
            1,
            project=self.controller.workspace_view.project,
        )
        self.assertEqual(self.controller.current_workspace_identity, identities[1])
        self.controller.move_workspace(1)
        self.assertEqual(self.controller.current_workspace_identity, identities[2])
        self.assertEqual(self.controller.current_workspace_document_id, second_id)
        self.controller.move_workspace(-1)
        self.assertEqual(self.controller.current_workspace_identity, identities[1])
        self.controller.go_to_workspace_segment(issued_first)
        self.controller.update_workspace_target("A unsaved")
        self.assertFalse(self.controller.current_workspace_segment.confirmed)

        documents = self.controller.workspace_view.documents
        second_document = next(
            item.identity for item in documents if item.identity.document_id == second_id
        )
        first_document = next(
            item.identity for item in documents if item.identity.document_id == first_id
        )
        self.controller.select_workspace_document(second_document)
        self.assertEqual(
            self.controller.current_workspace_identity.segment_identity,
            identities[2].segment_identity,
        )
        first_document = next(
            item.identity
            for item in self.controller.workspace_view.documents
            if item.identity.document_id == first_id
        )
        self.controller.select_workspace_document(first_document)
        self.assertEqual(
            self.controller.current_workspace_identity.segment_identity,
            identities[0].segment_identity,
        )
        self.assertEqual(self.controller.current_workspace_segment.target, "A unsaved")

        progress = self.controller.workspace_document_progress
        self.assertEqual(
            tuple(
                (
                    item.document_id,
                    item.total_segments,
                    item.translated_segments,
                    item.confirmed_segments,
                )
                for item in progress
            ),
            ((first_id, 2, 1, 0), (second_id, 2, 1, 1)),
        )
        project_progress = self.controller.workspace_project_progress
        self.assertEqual(project_progress.total_documents, 2)
        self.assertEqual(project_progress.total_segments, 4)
        self.assertEqual(project_progress.translated_segments, 2)
        self.assertEqual(project_progress.confirmed_segments, 1)
        self.assertFalse(hasattr(self.controller, "current_chunk"))

    def test_workspace_edits_reject_stale_or_forged_authority_before_mutation(
        self,
    ) -> None:
        opened = self.package_service.open(self.package_path)
        service = opened.create_workspace_service(
            session_id="issued-session",
            revision=7,
        )
        first = service.flat_segments[0].identity
        second_same_local = service.flat_segments[2].identity
        before = service.workspace
        before_digest = service.workspace_content_digest

        with self.assertRaises(ProjectWorkspaceError) as stale_session:
            service.update_segment_edit(
                first,
                target="must not apply",
                confirmed=False,
                session_id="foreign-session",
                base_revision=7,
            )
        self.assertEqual(stale_session.exception.code, "PROJECT.WORKSPACE.SESSION_STALE")
        self.assertIs(service.workspace, before)
        self.assertEqual(service.workspace_content_digest, before_digest)
        self.assertEqual(service.revision, 7)

        with self.assertRaises(ProjectWorkspaceError) as stale_revision:
            service.update_segment_edit(
                second_same_local,
                target="must not apply either",
                confirmed=False,
                session_id="issued-session",
                base_revision=6,
            )
        self.assertEqual(stale_revision.exception.code, "PROJECT.WORKSPACE.SESSION_STALE")
        self.assertIs(service.workspace, before)
        self.assertEqual(service.revision, 7)

        forged = SegmentIdentity(first.document_id, "forged-local-id")
        with self.assertRaises(ProjectWorkspaceError):
            service.update_segment_edit(
                forged,
                target="must not apply",
                confirmed=False,
                session_id="issued-session",
                base_revision=7,
            )
        self.assertIs(service.workspace, before)
        self.assertEqual(service.revision, 7)

        receipt = service.update_segment_edit(
            second_same_local,
            target="B changed only",
            confirmed=False,
            session_id="issued-session",
            base_revision=7,
        )
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.identity, second_same_local)
        self.assertEqual(receipt.previous_revision, 7)
        self.assertEqual(receipt.resulting_revision, 8)
        self.assertEqual(service.flat_segments[0].segment.target, "A saved")
        self.assertEqual(service.flat_segments[2].segment.target, "B changed only")

        self._open()
        issued = self.controller.current_workspace_identity
        self.controller.open_project_package(self.package_path)
        before_position = self.controller.workspace_global_index
        before_state = self.controller.workspace_save_state
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.WORKSPACE\\.IDENTITY_NOT_ISSUED$",
        ):
            self.controller.go_to_workspace_segment(issued)
        self.assertEqual(self.controller.workspace_global_index, before_position)
        self.assertEqual(self.controller.workspace_save_state, before_state)

    def test_recent_position_prefers_composite_identity_and_reads_legacy_index(
        self,
    ) -> None:
        self._open()
        self.controller.go_to_workspace_index(
            3,
            project=self.controller.workspace_view.project,
        )
        expected = self.controller.current_workspace_identity
        remembered = self.controller.workspace_state.find_workspace_project(
            self.package_path
        )
        self.assertIsNotNone(remembered)
        assert remembered is not None
        self.assertEqual(
            remembered.project_id,
            self.controller.workspace_view.project.project_id,
        )
        self.assertEqual(remembered.document_id, expected.document.document_id)
        self.assertEqual(remembered.local_segment_id, expected.local_segment_id)

        repository = ResourceRepository(self.root / "app-data")
        reopened, _composition = _compose_editor_controller(repository)
        reopened.open_project_package(self.package_path)
        self.assertEqual(
            reopened.current_workspace_identity.segment_identity,
            expected.segment_identity,
        )
        self.assertEqual(reopened.workspace_global_index, 3)

        # A schema-v1 entry without composite fields remains readable.  Its
        # validated index is only a compatibility fallback, never a new ID.
        reopened.workspace_state.remember_project(
            self.package_path,
            "shared",
            2,
        )
        legacy_reopened, _composition = _compose_editor_controller(repository)
        legacy_reopened.open_project_package(self.package_path)
        self.assertEqual(legacy_reopened.workspace_global_index, 2)
        self.assertEqual(
            legacy_reopened.current_workspace_identity.local_segment_id,
            "shared",
        )

    def test_search_scopes_share_matcher_and_stale_hits_cannot_navigate(
        self,
    ) -> None:
        self._validate_basic_matcher()
        self._open()
        first_id, second_id = self._document_ids()
        current_request = WorkspaceSearchRequest(
            query="needle",
            fields=(SearchField.SOURCE,),
            options=_BASIC_OPTIONS,
            scope=SearchScope.CURRENT_DOCUMENT,
        )
        entire_request = replace(
            current_request,
            scope=SearchScope.ENTIRE_PROJECT,
        )

        current = self.controller.search_workspace(current_request)
        self.assertEqual(
            tuple((hit.document_id, hit.local_segment_id) for hit in current.hits),
            ((first_id, "shared"),),
        )
        entire = self.controller.search_workspace(entire_request)
        self.assertEqual(
            tuple((hit.document_id, hit.local_segment_id) for hit in entire.hits),
            (
                (first_id, "shared"),
                (second_id, "shared"),
                (second_id, "b-tail"),
            ),
        )
        self.assertEqual(
            tuple(hit.project_global_index for hit in entire.hits),
            (0, 2, 3),
        )
        self.assertTrue(all(type(hit) is WorkspaceSearchHit for hit in entire.hits))

        self.controller.go_to_workspace_search_hit(entire.hits[1])
        self.assertEqual(self.controller.current_workspace_document_id, second_id)
        self.assertEqual(self.controller.current_workspace_identity.local_segment_id, "shared")
        stale_hit = entire.hits[0]
        self.controller.update_workspace_target("revision changed")
        before_identity = self.controller.current_workspace_identity
        before_index = self.controller.workspace_global_index
        before_state = self.controller.workspace_save_state
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.STALE_WORKSPACE$",
        ):
            self.controller.go_to_workspace_search_hit(stale_hit)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_global_index, before_index)
        self.assertEqual(self.controller.workspace_save_state, before_state)

        fresh = self.controller.search_workspace(entire_request)
        tampered_global_index = replace(fresh.hits[1], project_global_index=0)
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT_SEARCH\\.HIT_NOT_ISSUED$",
        ):
            self.controller.go_to_workspace_search_hit(tampered_global_index)
        self.assertEqual(self.controller.workspace_global_index, before_index)

    def test_document_and_full_save_clear_only_proven_dirty_and_adopt_receipt_binding(
        self,
    ) -> None:
        self._open()
        first_id, second_id = self._document_ids()
        first_identity = self.controller.current_workspace_identity.segment_identity
        self.controller.update_workspace_target("A package edit")
        second_document = next(
            item.identity
            for item in self.controller.workspace_view.documents
            if item.identity.document_id == second_id
        )
        self.controller.select_workspace_document(second_document)
        self.controller.update_workspace_target("B package edit")
        state = self.controller.workspace_save_state
        self.assertEqual(state.dirty_document_ids, (first_id, second_id))
        self.assertTrue(state.project_dirty)
        first_artifact_digest = state.artifact_digest

        first_document = next(
            item.identity
            for item in self.controller.workspace_view.documents
            if item.identity.document_id == first_id
        )
        document_result = self.controller.save_workspace_document(first_document)
        self.assertIs(type(document_result), ControllerWorkspaceSaveResult)
        self.assertIs(document_result.save_report.scope, SaveScope.DOCUMENT)
        self.assertIs(document_result.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertEqual(
            tuple(
                item.document_id
                for item in document_result.save_report.document_results
                if item.status is DocumentSaveStatus.SAVED
            ),
            (first_id,),
        )
        self.assertIsNotNone(document_result.receipt)
        assert document_result.receipt is not None
        self.assertEqual(
            document_result.package_artifact_digest,
            document_result.receipt.artifact_digest,
        )
        self.assertNotEqual(document_result.package_artifact_digest, first_artifact_digest)
        after_document = self.controller.workspace_save_state
        self.assertEqual(after_document.dirty_document_ids, (second_id,))
        self.assertTrue(after_document.project_dirty)
        self.assertEqual(
            after_document.artifact_digest,
            document_result.package_artifact_digest,
        )

        cold_after_document = self.package_service.open(self.package_path).workspace
        cold_first, cold_second = cold_after_document.documents
        self.assertEqual(cold_first.segments[0].identity, first_identity)
        self.assertEqual(cold_first.segments[0].target, "A package edit")
        self.assertEqual(cold_second.segments[0].target, "B saved")

        workspace_result = self.controller.save_workspace_package()
        self.assertIs(workspace_result.save_report.scope, SaveScope.WORKSPACE)
        self.assertIs(workspace_result.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertEqual(self.controller.workspace_save_state.dirty_document_ids, ())
        self.assertFalse(self.controller.workspace_save_state.project_dirty)
        self.assertEqual(
            self.controller.workspace_save_state.artifact_digest,
            workspace_result.package_artifact_digest,
        )
        cold_after_workspace = self.package_service.open(self.package_path).workspace
        self.assertEqual(cold_after_workspace.documents[0].segments[0].target, "A package edit")
        self.assertEqual(cold_after_workspace.documents[1].segments[0].target, "B package edit")
        self.assertEqual(
            (self.first_source.read_bytes(), self.second_source.read_bytes()),
            self.source_bytes,
        )

    def test_open_and_save_failures_preserve_existing_session_position_and_dirty(
        self,
    ) -> None:
        self._open()
        _first_id, second_id = self._document_ids()
        second_document = next(
            item.identity
            for item in self.controller.workspace_view.documents
            if item.identity.document_id == second_id
        )
        self.controller.select_workspace_document(second_document)
        self.controller.update_workspace_target("keep this dirty edit")
        before_session = self.controller.project_session_id
        before_revision = self.controller.project_revision
        before_tm_epoch = self.controller.query_epoch
        before_workspace = self.controller.workspace_view
        before_identity = self.controller.current_workspace_identity
        before_index = self.controller.workspace_global_index
        before_state = self.controller.workspace_save_state

        corrupt = self.root / "corrupt.localcat-project"
        corrupt.write_bytes(b"not a package")
        with self.assertRaises(EditorControllerError):
            self.controller.open_project_package(corrupt)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.query_epoch, before_tm_epoch)
        self.assertIs(self.controller.workspace_view, before_workspace)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_global_index, before_index)
        self.assertEqual(self.controller.workspace_save_state, before_state)

        with mock.patch(
            "editor_controller.ProjectPackageService.save_workspace",
            side_effect=ProjectWorkspaceError("PROJECT.PACKAGE.APPLY_FAILED"),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.PACKAGE\\.APPLY_FAILED$",
        ):
            self.controller.save_workspace_package()
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.query_epoch, before_tm_epoch)
        self.assertIs(self.controller.workspace_view, before_workspace)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_global_index, before_index)
        self.assertEqual(self.controller.workspace_save_state, before_state)

    def test_edit_arriving_after_durable_save_remains_dirty_on_new_binding(
        self,
    ) -> None:
        self._open()
        first_id, _second_id = self._document_ids()
        self.controller.update_workspace_target("durable before race")
        package_service = self.controller._workspace_package_service
        real_save = package_service.save_workspace

        def save_then_edit(*args: object, **kwargs: object):
            result = real_save(*args, **kwargs)  # type: ignore[arg-type]
            self.controller.update_workspace_target("new edit after durable save")
            return result

        with mock.patch.object(
            package_service,
            "save_workspace",
            side_effect=save_then_edit,
        ):
            result = self.controller.save_workspace_package()

        self.assertIs(result.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertEqual(
            self.controller.current_workspace_segment.target,
            "new edit after durable save",
        )
        self.assertEqual(self.controller.workspace_save_state.dirty_document_ids, (first_id,))
        self.assertEqual(
            self.controller.workspace_save_state.artifact_digest,
            result.package_artifact_digest,
        )
        durable = self.package_service.open(self.package_path).workspace
        self.assertEqual(durable.documents[0].segments[0].target, "durable before race")

    def test_source_reconciliation_reuses_c2_preview_and_preserves_local_overlay(
        self,
    ) -> None:
        self._open()
        session = self.controller.project_session_id

        # A cold package has no portable origin authority.  Re-selecting the
        # exact original sources establishes the device-local binding through
        # the same C2 preview/apply transaction.
        cold_staged = self.controller.stage_workspace_source_rebind(
            self.first_source.parent.parent,
            (self.first_source, self.second_source),
        )
        bind_preview = self.controller.preview_workspace_reconciliation(
            cold_staged,
        )
        bind_receipt = self.controller.apply_workspace_reconciliation(
            bind_preview,
            cold_staged,
        )
        self.assertEqual(bind_receipt.session_id, session)
        self.assertEqual(
            bind_receipt.published_revision,
            self.controller.project_revision,
        )

        self.controller.update_workspace_target("local overlay survives source change")
        _write_source(
            self.first_source,
            name="Chapter A",
            segments=(
                ("shared", "needle changed in A", "origin must not win", True),
                ("a-tail", "A tail", "", False),
            ),
        )
        incoming = revalidate_staged_selected_documents(cold_staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        self.assertEqual(
            tuple(
                (item.document_id, item.local_segment_id)
                for item in preview.source_changed_identities
            ),
            ((self.controller.current_workspace_document_id, "shared"),),
        )
        receipt = self.controller.apply_workspace_reconciliation(preview, incoming)
        self.assertEqual(self.controller.project_session_id, session)
        self.assertEqual(receipt.published_revision, self.controller.project_revision)
        self.assertEqual(self.controller.current_workspace_segment.source, "needle changed in A")
        self.assertEqual(
            self.controller.current_workspace_segment.target,
            "local overlay survives source change",
        )
        self.assertFalse(self.controller.current_workspace_segment.confirmed)
        self.assertEqual(
            self.controller.workspace_save_state.dirty_document_ids,
            (self.controller.current_workspace_document_id,),
        )

        stale_preview = self.controller.preview_workspace_reconciliation(incoming)
        self.controller.update_workspace_target("newer local edit")
        before_workspace = self.controller.workspace_view
        before_revision = self.controller.project_revision
        before_identity = self.controller.current_workspace_identity
        before_state = self.controller.workspace_save_state
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.RECONCILE\\.PREVIEW_STALE$",
        ):
            self.controller.apply_workspace_reconciliation(stale_preview, incoming)
        self.assertIs(self.controller.workspace_view, before_workspace)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_save_state, before_state)

    def test_source_reconciliation_revalidates_sealed_sources_at_apply(self) -> None:
        self._open()
        staged = self.controller.stage_workspace_source_rebind(
            self.first_source.parent.parent,
            (self.first_source, self.second_source),
        )
        binding_preview = self.controller.preview_workspace_reconciliation(staged)
        self.controller.apply_workspace_reconciliation(binding_preview, staged)
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        before_view = self.controller.workspace_view
        before_revision = self.controller.project_revision
        before_state = self.controller.workspace_save_state
        _write_source(
            self.first_source,
            name="Chapter A",
            segments=(("shared", "changed after preview", "", False),),
        )

        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.RECONCILE\\.SOURCE_STALE$",
        ):
            self.controller.apply_workspace_reconciliation(preview, incoming)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.workspace_save_state, before_state)

    def test_reconciliation_candidate_projection_fault_precedes_session_publish(
        self,
    ) -> None:
        self._open()
        staged = self.controller.stage_workspace_source_rebind(
            self.first_source.parent.parent,
            (self.first_source, self.second_source),
        )
        preview = self.controller.preview_workspace_reconciliation(staged)
        before_service = self.controller._workspace_service
        before_view = self.controller.workspace_view
        before_revision = self.controller.project_revision
        before_state = self.controller.workspace_save_state
        before_package = self.package_path.read_bytes()
        captured: list[PreparedReconciliationToken] = []
        real_prepare = ProjectWorkspaceService.prepare_reconciliation

        def capture_prepare(
            owner: ProjectWorkspaceService,
            *args: object,
            **kwargs: object,
        ) -> PreparedReconciliationToken:
            token = real_prepare(owner, *args, **kwargs)  # type: ignore[arg-type]
            captured.append(token)
            return token

        with mock.patch.object(
            ProjectWorkspaceService,
            "prepare_reconciliation",
            autospec=True,
            side_effect=capture_prepare,
        ), mock.patch.object(
            self.controller,
            "_prepare_workspace_install",
            side_effect=RuntimeError("candidate-projection-fault"),
        ), self.assertRaisesRegex(RuntimeError, "^candidate-projection-fault$"):
            self.controller.apply_workspace_reconciliation(preview, staged)

        self.assertIs(self.controller._workspace_service, before_service)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.package_path.read_bytes(), before_package)
        self.assertEqual(len(captured), 1)
        assert before_service is not None
        with self.assertRaises(ProjectWorkspaceError):
            before_service.commit_reconciliation(captured[0])
        before_service.discard_prepared_reconciliation(captured[0])
        before_service.discard_prepared_reconciliation(captured[0])

        retry = self.controller.preview_workspace_reconciliation(staged)
        self.controller.apply_workspace_reconciliation(retry, staged)

    def test_package_candidate_projection_fault_precedes_durable_commit(self) -> None:
        self._open()
        incoming = self._export_foreign_package()
        preview = self.controller.preview_workspace_package_import(incoming)
        before_view = self.controller.workspace_view
        before_revision = self.controller.project_revision
        before_state = self.controller.workspace_save_state
        before_package = self.package_path.read_bytes()
        package_service = self.controller._workspace_package_service
        captured: list[PreparedProjectPackageImport] = []
        real_prepare = package_service.prepare_import

        def capture_prepare(*args: object, **kwargs: object) -> PreparedProjectPackageImport:
            token = real_prepare(*args, **kwargs)  # type: ignore[arg-type]
            captured.append(token)
            return token

        with mock.patch.object(
            package_service,
            "prepare_import",
            side_effect=capture_prepare,
        ), mock.patch.object(
            self.controller,
            "_prepare_workspace_install",
            side_effect=RuntimeError("candidate-projection-fault"),
        ), mock.patch.object(
            package_service,
            "commit_prepared_import",
            wraps=package_service.commit_prepared_import,
        ) as commit, self.assertRaisesRegex(
            RuntimeError,
            "^candidate-projection-fault$",
        ):
            self.controller.apply_workspace_package_import(preview)

        commit.assert_not_called()
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.package_path.read_bytes(), before_package)
        self.assertEqual(len(captured), 1)
        with self.assertRaises(ProjectWorkspaceError):
            package_service.commit_prepared_import(captured[0])
        package_service.discard_prepared_import(captured[0])
        package_service.discard_prepared_import(captured[0])

        retry = self.controller.preview_workspace_package_import(incoming)
        result = self.controller.apply_workspace_package_import(retry)
        self.assertTrue(result.active_session_changed)

    def test_package_prepared_import_token_is_exact_single_use_authority(self) -> None:
        self._open()
        incoming = self._export_foreign_package()
        preview = self.controller.preview_workspace_package_import(incoming)
        package_service = self.controller._workspace_package_service
        prepared = package_service.prepare_import(preview.operation_id)
        self.assertIs(type(prepared), PreparedProjectPackageImport)
        candidate_save = package_service.create_prepared_import_save_service(
            prepared,
            session_id="prepared-controller-session",
        )
        self.assertEqual(
            candidate_save.workspace_service.workspace.project_id,
            preview.project_id,
        )

        with self.assertRaises(ProjectWorkspaceError):
            package_service.create_prepared_import_save_service(
                replace(prepared),
                session_id="forged-prepared-session",
            )

        result = package_service.commit_prepared_import(prepared)
        self.assertEqual(result.receipt.project_id, preview.project_id)
        with self.assertRaises(ProjectWorkspaceError):
            package_service.commit_prepared_import(prepared)

    def test_workspace_confirmation_uses_overlay_authority_and_cross_document_order(
        self,
    ) -> None:
        self._open()
        first_document_id, _second_document_id = self._document_ids()
        self.controller.update_workspace_target("confirmed workspace target")
        before_revision = self.controller.project_revision

        result = self.controller.confirm_current()

        self.assertIs(type(result), ControllerWorkspaceConfirmResult)
        assert type(result) is ControllerWorkspaceConfirmResult
        self.assertTrue(result.write_report.succeeded)
        self.assertEqual(self.controller.project_revision, before_revision + 1)
        self.assertTrue(
            self.controller._workspace_service.workspace.documents[0].segments[0].confirmed
        )
        self.assertEqual(
            self.controller.workspace_save_state.dirty_document_ids,
            (first_document_id,),
        )
        self.assertEqual(result.current_identity, self.controller.current_workspace_identity)
        self.assertEqual(result.current_global_index, 1)

    def test_raw_document_and_index_values_cannot_outlive_issued_revision(self) -> None:
        self._open()
        issued_project = self.controller.workspace_view.project
        issued_document = self.controller.workspace_view.documents[1].identity
        self.controller.go_to_workspace_index(1, project=issued_project)
        self.controller.update_workspace_target("revision-bound token change")
        before_view = self.controller.workspace_view
        before_index = self.controller.workspace_global_index
        before_state = self.controller.workspace_save_state

        for operation in (
            lambda: self.controller.go_to_workspace_index(
                0,
                project=issued_project,
            ),
            lambda: self.controller.select_workspace_document(issued_document),
            lambda: self.controller.save_workspace_document(issued_document),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "^PROJECT\\.WORKSPACE\\.IDENTITY_NOT_ISSUED$",
                ):
                    operation()
                self.assertIs(self.controller.workspace_view, before_view)
                self.assertEqual(self.controller.workspace_global_index, before_index)
                self.assertEqual(self.controller.workspace_save_state, before_state)
        with self.assertRaises(TypeError):
            self.controller.select_workspace_document(
                self.controller.current_workspace_document_id  # type: ignore[arg-type]
            )

    def test_workspace_session_swap_fault_restores_every_controller_projection(self) -> None:
        self._open()
        foreign = self._export_foreign_package()
        self.controller.update_workspace_target("retain dirty session")
        before_view = self.controller.workspace_view
        before_session = self.controller.project_session_id
        before_revision = self.controller.project_revision
        before_state = self.controller.workspace_save_state
        before_index = self.controller.workspace_global_index
        before_epoch = self.controller.query_epoch

        with mock.patch.object(
            self.controller,
            "_record_current_tm_baseline",
            side_effect=RuntimeError("injected session projection fault"),
        ), self.assertRaisesRegex(RuntimeError, "injected session projection fault"):
            self.controller.open_project_package(foreign)

        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.controller.workspace_global_index, before_index)
        self.assertEqual(self.controller.query_epoch, before_epoch)

    def test_package_import_preview_is_session_bound_and_success_swaps_once(
        self,
    ) -> None:
        self._open()
        foreign = self._export_foreign_package()
        foreign_workspace = self.package_service.open(foreign).workspace
        self.controller.update_workspace_target("dirty current session")
        before_dirty = self.controller.workspace_view
        before_dirty_state = self.controller.workspace_save_state
        before_bytes = self.package_path.read_bytes()
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.PACKAGE\\.ACTIVE_WORKSPACE_DIRTY$",
        ):
            self.controller.preview_workspace_package_import(foreign)
        self.assertIs(self.controller.workspace_view, before_dirty)
        self.assertEqual(self.controller.workspace_save_state, before_dirty_state)
        self.assertEqual(self.package_path.read_bytes(), before_bytes)

        self.controller.save_workspace_package()
        preview = self.controller.preview_workspace_package_import(foreign)
        self.assertNotEqual(
            preview.project_id,
            self.controller.workspace_view.project.project_id,
        )
        self.assertEqual(preview.destination_before_digest, self.package_path_digest())
        before_session = self.controller.project_session_id
        before_workspace = self.controller.workspace_view
        before_bytes = self.package_path.read_bytes()

        # Even a still-valid C2 carrier plan cannot be applied after its
        # Controller session/revision authority becomes stale.
        self.controller.update_workspace_target("newer dirty current session")
        stale_workspace = self.controller.workspace_view
        stale_revision = self.controller.project_revision
        stale_state = self.controller.workspace_save_state
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.PACKAGE\\.PREVIEW_STALE$",
        ):
            self.controller.apply_workspace_package_import(preview)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, stale_workspace)
        self.assertEqual(self.controller.project_revision, stale_revision)
        self.assertEqual(self.controller.workspace_save_state, stale_state)
        self.assertEqual(self.package_path.read_bytes(), before_bytes)
        self.assertIsNot(self.controller.workspace_view, before_workspace)

        self.controller.save_workspace_package()
        fresh = self.controller.preview_workspace_package_import(foreign)
        result = self.controller.apply_workspace_package_import(fresh)
        self.assertTrue(result.receipt.durable)
        self.assertNotEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.project_revision, 0)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            foreign_workspace.project_id,
        )
        self.assertEqual(self.controller.workspace_save_state.dirty_document_ids, ())
        self.assertEqual(
            self.controller.workspace_save_state.artifact_digest,
            result.receipt.destination_after_digest,
        )
        self.assertEqual(
            self.package_service.open(self.package_path).workspace,
            foreign_workspace,
        )

    def test_independent_package_import_preserves_dirty_active_session(self) -> None:
        self._open()
        self.controller.update_workspace_target("unsaved active work")
        incoming = self._export_foreign_package()
        incoming_project_id = self.package_service.open(incoming).workspace.project_id
        destination = self.root / "independent-import.localcat-project"
        before_view = self.controller.workspace_view
        before_session = self.controller.project_session_id
        before_revision = self.controller.project_revision
        before_state = self.controller.workspace_save_state
        before_index = self.controller.workspace_global_index
        before_epoch = self.controller.query_epoch

        preview = self.controller.preview_workspace_package_import(
            incoming,
            destination=destination,
        )
        result = self.controller.apply_workspace_package_import(preview)

        self.assertFalse(result.active_session_changed)
        self.assertIs(result.session, before_view)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.project_revision, before_revision)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.controller.workspace_global_index, before_index)
        self.assertEqual(self.controller.query_epoch, before_epoch)
        self.assertEqual(
            self.package_service.open(destination).workspace.project_id,
            incoming_project_id,
        )

    def test_same_project_import_reconciles_and_io_failure_preserves_session(
        self,
    ) -> None:
        self._open()
        incoming = self._export_same_project_package()
        before_project_id = self.controller.workspace_view.project.project_id
        before_session = self.controller.project_session_id

        stale_preview = self.controller.preview_workspace_package_import(incoming)
        self.assertTrue(stale_preview.same_project)
        self.assertEqual(stale_preview.project_id, before_project_id)
        self.controller.update_workspace_target("newer active same-project target")
        before_target = self.controller.current_workspace_segment.target
        stale_workspace = self.controller.workspace_view
        stale_revision = self.controller.project_revision
        stale_state = self.controller.workspace_save_state
        with self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.PACKAGE\\.PREVIEW_STALE$",
        ):
            self.controller.apply_workspace_package_import(stale_preview)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, stale_workspace)
        self.assertEqual(self.controller.project_revision, stale_revision)
        self.assertEqual(self.controller.workspace_save_state, stale_state)

        preview = self.controller.preview_workspace_package_import(incoming)
        with mock.patch.object(
            self.controller._workspace_package_service,
            "commit_prepared_import",
            side_effect=ProjectWorkspaceError("PROJECT.PACKAGE.APPLY_FAILED"),
        ), self.assertRaisesRegex(
            EditorControllerError,
            "^PROJECT\\.PACKAGE\\.APPLY_FAILED$",
        ):
            self.controller.apply_workspace_package_import(preview)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            before_project_id,
        )
        self.assertEqual(self.controller.current_workspace_segment.target, before_target)
        self.assertTrue(self.controller.workspace_save_state.project_dirty)

        fresh = self.controller.preview_workspace_package_import(incoming)
        result = self.controller.apply_workspace_package_import(fresh)
        self.assertIs(result.receipt.mode, ProjectPackageImportMode.UPDATE_SAME_PROJECT)
        self.assertEqual(result.receipt.project_id, before_project_id)
        self.assertNotEqual(self.controller.project_session_id, before_session)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            before_project_id,
        )
        # Unchanged source identity preserves the active package overlay; an
        # incoming package target is not a second merge authority.
        self.assertEqual(self.controller.current_workspace_segment.target, before_target)
        self.assertFalse(self.controller.workspace_save_state.project_dirty)
        self.assertEqual(
            self.controller.workspace_save_state.artifact_digest,
            result.receipt.destination_after_digest,
        )

    def package_path_digest(self) -> str:
        return self.package_service.open(self.package_path).validation.artifact_digest

    def test_legacy_single_json_controller_journey_is_unchanged(self) -> None:
        self._validate_basic_matcher()
        legacy = self.root / "legacy.json"
        _write_source(
            legacy,
            name="Legacy",
            segments=(("legacy-segment", "legacy needle", "", False),),
        )
        project = self.controller.open_project(legacy)
        self.assertEqual(project.segments[0].id, "legacy-segment")
        self.controller.update_target("legacy target")
        self.assertTrue(self.controller.dirty)
        report = self.controller.search_project(
            ProjectSearchRequest(
                query="needle",
                fields=(SearchField.SOURCE,),
                options=_BASIC_OPTIONS,
            )
        )
        self.assertEqual(len(report.hits), 1)
        self.assertFalse(hasattr(report.hits[0], "document_id"))
        self.controller.go_to_search_hit(report.hits[0])
        saved = self.controller.save_project(legacy)
        self.assertEqual(saved.segments[0].target, "legacy target")
        self.assertFalse(self.controller.dirty)
        reopened = self.controller.open_project(legacy)
        self.assertEqual(reopened.segments[0].target, "legacy target")


if __name__ == "__main__":
    unittest.main()
