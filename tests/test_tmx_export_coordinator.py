from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from collaborative_chunk_contracts import (
    ChunkScopeProjection,
    ChunkSegmentRef,
    canonicalize_chunk_members,
    issue_chunk_id,
    issue_chunk_plan_id,
)
from project_workspace import (
    DocumentProgress,
    IssuedDocumentIdentity,
    IssuedProjectIdentity,
    IssuedSegmentIdentity,
    ProjectProgress,
    WorkspaceDocumentView,
    WorkspaceSaveState,
    WorkspaceSegmentUniverseEntry,
    WorkspaceSegmentView,
    WorkspaceSessionView,
    WorkspaceUniverseBinding,
    WorkspaceUniverseProjection,
    workspace_segment_universe_digest_v1,
)
from project_workspace_contracts import SegmentIdentity, SourcePresence
from tmx_context_contracts import TmxEffectiveLocales, TmxPropScope, TmxScopeKind
from tmx_context_interchange import prepare_tmx_payload
from tmx_export_coordinator import TmxExportCoordinator
from tmx_export_scope_contracts import TmxScopeCoordinatorError
from tm_contracts import TMRecord
from tm_sqlite_store import (
    CanonicalExportRecord,
    CanonicalExportSnapshot,
    CanonicalRevisionSnapshot,
)


_PROJECT = "prj-" + "1" * 64
_DOC_A = "doc-" + "a" * 64
_DOC_B = "doc-" + "b" * 64
_SESSION = "session-tmx-scope"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(
    record_id: int,
    source: str,
    target: str,
    *,
    provenance: tuple[tuple[str, str], ...] = (),
) -> CanonicalExportRecord:
    return CanonicalExportRecord(
        record=TMRecord(
            record_id=record_id,
            source_raw=source,
            target_raw=target,
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            file_source=None,
            provenance=provenance,
            legacy_line_no=None,
            origin_batch_id="batch.scope-test",
            origin_ordinal=record_id - 1,
        ),
        usage_count=record_id,
        last_used=None,
    )


def _snapshot(
    records: tuple[CanonicalExportRecord, ...],
    *,
    revision: int = 1,
) -> CanonicalExportSnapshot:
    return CanonicalExportSnapshot(
        revision=CanonicalRevisionSnapshot(
            resource_id="tm.scope-test",
            canonical_store_id="store.scope-test",
            generation=2,
            head_revision=revision,
            record_count=len(records),
        ),
        records=records,
    )


class _ResourceOwner:
    def __init__(self, snapshot: CanonicalExportSnapshot) -> None:
        self.snapshot = snapshot

    def capture_export_snapshot(self) -> CanonicalExportSnapshot:
        return self.snapshot


def _workspace(
    *,
    target_suffix: str = "",
    missing_universe_identity: SegmentIdentity | None = None,
) -> tuple[WorkspaceSessionView, WorkspaceUniverseProjection]:
    project = IssuedProjectIdentity(
        project_id=_PROJECT,
        session_id=_SESSION,
        generation=3,
        workspace_revision=7,
    )
    document_b = IssuedDocumentIdentity(project=project, document_id=_DOC_B)
    document_a = IssuedDocumentIdentity(project=project, document_id=_DOC_A)
    doc_views = (
        WorkspaceDocumentView(
            identity=document_b,
            display_name="Second physical id, first navigation document",
            source_ref="chapters/first.txt",
            order=0,
            progress=DocumentProgress(_DOC_B, 2, 2, 1),
        ),
        WorkspaceDocumentView(
            identity=document_a,
            display_name="First physical id, second navigation document",
            source_ref="chapters/second.txt",
            order=1,
            progress=DocumentProgress(_DOC_A, 1, 0, 0),
        ),
    )
    identities = (
        IssuedSegmentIdentity(document_b, "segment-z"),
        IssuedSegmentIdentity(document_b, "segment-a"),
        IssuedSegmentIdentity(document_a, "segment-m"),
    )
    segments = (
        WorkspaceSegmentView(
            identity=identities[0],
            document_local_index=0,
            project_global_index=0,
            source="B first",
            target="乙一" + target_suffix,
            raw_speaker="speaker-b",
            confirmed=True,
        ),
        WorkspaceSegmentView(
            identity=identities[1],
            document_local_index=1,
            project_global_index=1,
            source="B second",
            target="乙二" + target_suffix,
            raw_speaker="",
            confirmed=False,
        ),
        WorkspaceSegmentView(
            identity=identities[2],
            document_local_index=0,
            project_global_index=2,
            source="A only",
            target="",
            raw_speaker="",
            confirmed=False,
        ),
    )
    session = WorkspaceSessionView(
        project=project,
        name="scope-project",
        source_locale="en",
        target_locale="zh-CN",
        documents=doc_views,
        segments=segments,
        current_segment=segments[1].identity,
        project_progress=ProjectProgress(2, 3, 2, 1),
        save_state=WorkspaceSaveState((), False, False),
    )
    universe_entries = tuple(
        sorted(
            (
                WorkspaceSegmentUniverseEntry(
                    identities[0].segment_identity,
                    SourcePresence.ATTACHED,
                ),
                WorkspaceSegmentUniverseEntry(
                    identities[1].segment_identity,
                    SourcePresence.DETACHED,
                ),
                WorkspaceSegmentUniverseEntry(
                    identities[2].segment_identity,
                    SourcePresence.ATTACHED,
                ),
            ),
            key=lambda item: (
                item.identity.document_id.encode("ascii"),
                len(item.identity.local_segment_id.encode("utf-8")).to_bytes(8, "big")
                + item.identity.local_segment_id.encode("utf-8"),
            ),
        )
    )
    if missing_universe_identity is not None:
        universe_entries = tuple(
            item for item in universe_entries if item.identity != missing_universe_identity
        )
    universe = WorkspaceUniverseProjection(
        binding=WorkspaceUniverseBinding(
            project_id=_PROJECT,
            workspace_session_id=_SESSION,
            workspace_revision=7,
            workspace_composition_revision=4,
            workspace_digest=_sha("workspace" + target_suffix),
            segment_universe_digest=workspace_segment_universe_digest_v1(
                _PROJECT,
                universe_entries,
            ),
        ),
        entries=universe_entries,
    )
    return session, universe


class _WorkspaceOwner:
    def __init__(
        self,
        session: WorkspaceSessionView,
        universe: WorkspaceUniverseProjection,
    ) -> None:
        self.session = session
        self.universe = universe

    def capture_session_view(self) -> WorkspaceSessionView:
        return self.session

    def capture_workspace_universe(self) -> WorkspaceUniverseProjection:
        return self.universe

    def revalidate_workspace_universe(
        self,
        projection: WorkspaceUniverseProjection,
    ) -> WorkspaceUniverseProjection:
        if projection != self.universe:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return self.universe


class _ChunkOwner:
    def __init__(self, projection: ChunkScopeProjection) -> None:
        self.projection = projection

    def capture_scope_projection(self, chunk_id: str) -> ChunkScopeProjection:
        if chunk_id != self.projection.chunk_id:
            raise TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
        return self.projection

    def revalidate_scope_projection(
        self,
        projection: ChunkScopeProjection,
    ) -> ChunkScopeProjection:
        if projection != self.projection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return self.projection


def _chunk_projection(
    universe: WorkspaceUniverseProjection,
    identities: tuple[SegmentIdentity, ...],
) -> ChunkScopeProjection:
    members = canonicalize_chunk_members(
        tuple(ChunkSegmentRef(_PROJECT, identity) for identity in identities)
    )
    return ChunkScopeProjection(
        project_id=_PROJECT,
        chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
        plan_revision=5,
        plan_digest=_sha("plan"),
        segment_universe_digest=universe.binding.segment_universe_digest,
        chunk_id=issue_chunk_id(b"c" * 32),
        members=members,
    )


class TmxExportCoordinatorTests(unittest.TestCase):
    def test_managed_resource_is_complete_ordered_snapshot_with_props(self) -> None:
        raw_unknown_a = json.dumps(
            ("source_tuv", "x-vendor-duplicate", "en", "first"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw_unknown_b = json.dumps(
            ("target_tuv", "x-vendor-duplicate", "zh-CN", "second"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        owner = _ResourceOwner(
            _snapshot(
                (
                    _record(
                        1,
                        "source one",
                        "target one",
                        provenance=(
                            ("source", "tmx-import"),
                            ("tmx.prop", raw_unknown_a),
                            ("tmx.prop", raw_unknown_b),
                            ("tmx.status", "translated"),
                        ),
                    ),
                    _record(2, "source one", "target one"),
                )
            )
        )

        captured = TmxExportCoordinator(resource_owner=owner).capture_managed_resource()

        self.assertEqual(captured.tmx_binding.scope_kind, TmxScopeKind.MANAGED_RESOURCE)
        self.assertEqual(
            tuple(unit.target for unit in captured.units),
            ("target one", "target one"),
        )
        self.assertEqual(tuple(unit.unit_identity.rsplit(":", 1)[1] for unit in captured.units), ("1", "2"))
        self.assertEqual(captured.units[0].status, "translated")
        self.assertEqual(
            tuple(prop.scope for prop in captured.units[0].imported_props),
            (TmxPropScope.SOURCE_TUV, TmxPropScope.TARGET_TUV),
        )
        self.assertEqual(
            tuple(prop.type for prop in captured.units[0].imported_props),
            ("x-vendor-duplicate", "x-vendor-duplicate"),
        )

    def test_managed_resource_revalidation_rejects_revision_or_body_drift(self) -> None:
        owner = _ResourceOwner(_snapshot((_record(1, "source", "target"),)))
        coordinator = TmxExportCoordinator(resource_owner=owner)
        captured = coordinator.capture_managed_resource()
        owner.snapshot = _snapshot((_record(1, "source", "changed"),), revision=2)

        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.STALE"):
            coordinator.revalidate_managed_resource(captured)

    def test_project_joins_by_identity_and_keeps_workspace_navigation_order(self) -> None:
        session, universe = _workspace()
        coordinator = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe)
        )

        captured = coordinator.capture_entire_project()

        self.assertEqual(captured.tmx_binding.scope_kind, TmxScopeKind.ENTIRE_PROJECT)
        self.assertEqual(
            tuple(unit.source for unit in captured.units),
            ("B first", "B second", "A only"),
        )
        self.assertEqual(captured.owner_session.current_segment, session.segments[1].identity)
        self.assertEqual(captured.tmx_binding.attached_count, 2)
        self.assertFalse(captured.units[1].attached)
        self.assertEqual(captured.units[0].context_next, "B second")
        self.assertEqual(captured.units[1].context_prev, "B first")
        self.assertIsNone(captured.units[2].context_prev)
        self.assertEqual(captured.units[2].target, "")

    def test_project_revalidation_rejects_content_drift_even_when_universe_is_same(self) -> None:
        session, universe = _workspace()
        owner = _WorkspaceOwner(session, universe)
        coordinator = TmxExportCoordinator(workspace_owner=owner)
        captured = coordinator.capture_entire_project()
        changed_session, _ = _workspace(target_suffix=" changed")
        owner.session = changed_session

        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.STALE"):
            coordinator.revalidate_entire_project(captured)

    def test_project_adapter_leaves_detached_and_empty_target_to_core_loss_policy(self) -> None:
        session, universe = _workspace()
        captured = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe)
        ).capture_entire_project()

        payload = prepare_tmx_payload(
            captured.tmx_binding,
            TmxEffectiveLocales("en", "zh-CN"),
            captured.units,
        )

        self.assertEqual(payload.proof.included_count, 1)
        self.assertEqual(payload.proof.loss_report.excluded_count, 2)
        self.assertEqual(
            {count.code: count.count for count in payload.proof.loss_report.counts},
            {"detached_member": 1, "empty_target": 1},
        )

    def test_project_missing_identity_is_blocking_not_silent(self) -> None:
        complete_session, complete_universe = _workspace()
        missing = complete_session.segments[1].identity.segment_identity
        session, universe = _workspace(missing_universe_identity=missing)
        coordinator = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe)
        )

        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.MISSING"):
            coordinator.capture_entire_project()
        self.assertNotEqual(
            complete_universe.binding.segment_universe_digest,
            universe.binding.segment_universe_digest,
        )

    def test_selected_chunk_is_one_explicit_projection_in_project_order(self) -> None:
        session, universe = _workspace()
        chosen = (
            session.segments[0].identity.segment_identity,
            session.segments[1].identity.segment_identity,
        )
        projection = _chunk_projection(universe, tuple(reversed(chosen)))
        coordinator = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe),
            chunk_owner=_ChunkOwner(projection),
        )

        captured = coordinator.capture_selected_chunk(projection.chunk_id)

        self.assertEqual(captured.tmx_binding.scope_kind, TmxScopeKind.SELECTED_CHUNK)
        self.assertEqual(captured.tmx_binding.chunk_id, projection.chunk_id)
        self.assertEqual(
            tuple(unit.source for unit in captured.units),
            ("B first", "B second"),
        )
        self.assertEqual(captured.tmx_binding.attached_count, 1)
        self.assertFalse(captured.units[1].attached)
        self.assertEqual(captured.tmx_binding.document_count, 1)

    def test_selected_chunk_rejects_missing_member_and_foreign_explicit_id(self) -> None:
        session, universe = _workspace()
        missing = SegmentIdentity(_DOC_B, "segment-missing")
        projection = _chunk_projection(universe, (missing,))
        coordinator = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe),
            chunk_owner=_ChunkOwner(projection),
        )

        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.MISSING"):
            coordinator.capture_selected_chunk(projection.chunk_id)
        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.FOREIGN"):
            coordinator.capture_selected_chunk(issue_chunk_id(b"x" * 32))

    def test_selected_chunk_revalidation_rejects_plan_drift(self) -> None:
        session, universe = _workspace()
        projection = _chunk_projection(
            universe,
            (session.segments[0].identity.segment_identity,),
        )
        chunk_owner = _ChunkOwner(projection)
        coordinator = TmxExportCoordinator(
            workspace_owner=_WorkspaceOwner(session, universe),
            chunk_owner=chunk_owner,
        )
        captured = coordinator.capture_selected_chunk(projection.chunk_id)
        chunk_owner.projection = replace(
            projection,
            plan_revision=projection.plan_revision + 1,
            plan_digest=_sha("new-plan"),
        )

        with self.assertRaisesRegex(TmxScopeCoordinatorError, "TMX.SCOPE.STALE"):
            coordinator.revalidate_selected_chunk(captured)


if __name__ == "__main__":
    unittest.main()
