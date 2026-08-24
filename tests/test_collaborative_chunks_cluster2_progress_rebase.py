from __future__ import annotations

import ast
from dataclasses import fields, replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import collaborative_chunk_store as chunk_store_module
from collaborative_chunk_contracts import (
    CHUNK_METADATA_NAMESPACE,
    DERIVED_MAX_REBASE_INTENT_BYTES_V1,
    MAX_REBASE_INTENT_BYTES,
    AssigneeRef,
    EMPTY_CHUNK_AUDIT_DIGEST,
    ChunkPublishedUniverseBinding,
    ChunkPublishedUniverseProjection,
    ChunkPublishedWorkspaceTransition,
    ChunkRebaseInspection,
    ChunkRebaseIntent,
    ChunkRebasePreview,
    ChunkError,
    ChunkPlanSnapshot,
    ChunkProgressSegmentFact,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceProgressProjection,
    ChunkWorkspaceUniverseProjection,
    CollaborativeChunk,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_published_workspace_transition_digest_v1,
    chunk_plan_binding,
    chunk_segment_ref_from_ids,
    issue_chunk_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
    validate_chunk_plan_snapshot,
)
from collaborative_chunk_store import (
    CollaborativeChunkStore,
    decode_chunk_rebase_intent,
    encode_chunk_rebase_intent,
)
from collaborative_chunks import (
    ChunkProgressService,
    ChunkTopologyPublicationAuthority,
    LocalReferenceActorPort,
)
from project_workspace_contracts import SourcePresence
from project_workspace_identity import issue_project_id


class CollaborativeChunkCluster2ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_id = issue_project_id(b"P" * 32)
        self.document_id = "doc-" + b"D".hex() * 32
        self.other_document_id = "doc-" + b"E".hex() * 32
        self.unfilled = self._member(self.document_id, "001")
        self.draft = self._member(self.document_id, "002")
        self.confirmed = self._member(self.document_id, "003")
        self.detached = self._member(self.document_id, "004")
        self.unallocated_same_local = self._member(self.other_document_id, "001")
        self.facts = (
            ChunkProgressSegmentFact(
                self.unfilled,
                SourcePresence.ATTACHED,
                True,
                False,
            ),
            ChunkProgressSegmentFact(
                self.draft,
                SourcePresence.ATTACHED,
                False,
                False,
            ),
            ChunkProgressSegmentFact(
                self.confirmed,
                SourcePresence.ATTACHED,
                True,
                True,
            ),
            ChunkProgressSegmentFact(
                self.detached,
                SourcePresence.DETACHED,
                False,
                True,
            ),
            ChunkProgressSegmentFact(
                self.unallocated_same_local,
                SourcePresence.ATTACHED,
                False,
                False,
            ),
        )
        self.binding = self._binding(self.facts, revision=8)
        self.projection = ChunkWorkspaceProgressProjection(
            self.binding,
            self.facts,
        )
        self.chunk_id = issue_chunk_id(b"C" * 32)
        self.snapshot = validate_chunk_plan_snapshot(
            ChunkPlanSnapshot(
                schema_version=1,
                namespace=CHUNK_METADATA_NAMESPACE,
                chunk_plan_id=issue_chunk_plan_id(b"L" * 32),
                project_id=self.project_id,
                revision=3,
                segment_universe_digest=self.binding.segment_universe_digest,
                chunks=(
                    CollaborativeChunk(
                        chunk_id=self.chunk_id,
                        name="进度分工",
                        order=0,
                        members=(
                            self.unfilled,
                            self.draft,
                            self.confirmed,
                            self.detached,
                        ),
                        assignee=None,
                    ),
                ),
                audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
            )
        )
        self.service = ChunkProgressService(
            lambda: self.snapshot,
            lambda: self.projection,
        )

    def _member(self, document_id: str, local_segment_id: str):
        return chunk_segment_ref_from_ids(
            self.project_id,
            document_id,
            local_segment_id,
        )

    def _binding(
        self,
        facts: tuple[ChunkProgressSegmentFact, ...],
        *,
        revision: int,
        session_id: str = "progress-session",
    ) -> ChunkWorkspaceBinding:
        universe = tuple(
            ChunkUniverseEntry(fact.segment, fact.source_presence)
            for fact in facts
        )
        return ChunkWorkspaceBinding(
            project_id=self.project_id,
            workspace_session_id=session_id,
            workspace_revision=revision,
            segment_universe_digest=segment_universe_digest_v1(
                self.project_id,
                universe,
            ),
        )

    def _error(self, code: str, callback) -> None:
        with self.assertRaises(ChunkError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), code)

    def _progress(self):
        return self.service.progress(
            workspace_binding=self.binding,
            expected_plan_binding=chunk_plan_binding(self.snapshot),
            chunk_id=self.chunk_id,
        )

    def test_progress_is_exact_body_free_and_ignores_unallocated_same_local_id(self) -> None:
        progress = self._progress()
        self.assertEqual(progress.attached_total, 3)
        self.assertEqual(progress.unfilled, 1)
        self.assertEqual(progress.draft, 1)
        self.assertEqual(progress.confirmed, 1)
        self.assertEqual(progress.detached, 1)
        self.assertEqual(progress.completion_numerator, 1)
        self.assertEqual(progress.completion_denominator, 3)
        self.assertTrue(
            {
                "source",
                "target",
                "speaker",
                "path",
                "credential",
            }.isdisjoint(field.name for field in fields(progress))
        )
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: replace(progress, completion_denominator=4),
        )

    def test_all_detached_has_explicit_zero_over_zero(self) -> None:
        detached_chunk = replace(
            self.snapshot.chunks[0],
            members=(self.detached,),
        )
        self.snapshot = validate_chunk_plan_snapshot(
            replace(self.snapshot, chunks=(detached_chunk,))
        )
        progress = self._progress()
        self.assertEqual(progress.attached_total, 0)
        self.assertEqual(progress.detached, 1)
        self.assertEqual(progress.completion_numerator, 0)
        self.assertEqual(progress.completion_denominator, 0)

    def test_target_and_confirmed_changes_recompute_without_persisted_counters(self) -> None:
        before = self._progress()
        changed_facts = tuple(
            replace(fact, target_is_blank=False, confirmed=True)
            if fact.segment == self.unfilled
            else fact
            for fact in self.facts
        )
        self.binding = self._binding(changed_facts, revision=9)
        self.projection = ChunkWorkspaceProgressProjection(
            self.binding,
            changed_facts,
        )
        after = self._progress()
        self.assertEqual(before.unfilled, 1)
        self.assertEqual(after.unfilled, 0)
        self.assertEqual(after.confirmed, 2)
        cold = ChunkProgressService(
            lambda: self.snapshot,
            lambda: self.projection,
        ).progress(
            workspace_binding=self.binding,
            expected_plan_binding=chunk_plan_binding(self.snapshot),
            chunk_id=self.chunk_id,
        )
        self.assertEqual(cold, after)

    def test_same_workspace_revision_captures_progress_projection_once(self) -> None:
        calls = 0

        def provide_progress():
            nonlocal calls
            calls += 1
            return self.projection

        expected_plan = chunk_plan_binding(self.snapshot)
        service = ChunkProgressService(
            lambda: self.snapshot,
            provide_progress,
            snapshot_binding_provider=lambda: expected_plan,
        )
        for _ in range(3):
            service.progress(
                workspace_binding=self.binding,
                expected_plan_binding=expected_plan,
                chunk_id=self.chunk_id,
            )
        self.assertEqual(calls, 1)

    def test_stale_bindings_universe_change_and_provider_failure_fail_closed(self) -> None:
        stale = replace(
            self.binding,
            workspace_revision=self.binding.workspace_revision + 1,
        )
        self._error(
            "CHUNK.REVISION_STALE",
            lambda: self.service.progress(
                workspace_binding=stale,
                expected_plan_binding=chunk_plan_binding(self.snapshot),
                chunk_id=self.chunk_id,
            ),
        )
        added = self._member(self.other_document_id, "new")
        changed_facts = self.facts + (
            ChunkProgressSegmentFact(
                added,
                SourcePresence.ATTACHED,
                True,
                False,
            ),
        )
        self.binding = self._binding(changed_facts, revision=9)
        self.projection = ChunkWorkspaceProgressProjection(
            self.binding,
            changed_facts,
        )
        self._error("CHUNK.REBASE_REQUIRED", self._progress)

        def unavailable():
            raise OSError("sensitive body")

        failed = ChunkProgressService(lambda: self.snapshot, unavailable)
        self._error(
            "CHUNK.RECOVERY_REQUIRED",
            lambda: failed.progress(
                workspace_binding=self.binding,
                expected_plan_binding=chunk_plan_binding(self.snapshot),
                chunk_id=self.chunk_id,
            ),
        )

    def test_projection_requires_complete_exact_universe(self) -> None:
        self._error(
            "CHUNK.UNIVERSE_MISMATCH",
            lambda: ChunkWorkspaceProgressProjection(
                self.binding,
                self.facts[:-1],
            ),
        )


class CollaborativeChunkCluster2RebaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project_id = issue_project_id(b"R" * 32)
        self.document_id = "doc-" + b"A".hex() * 32
        self.a = self._member("001")
        self.b = self._member("002")
        self.c = self._member("003")
        self.old_unallocated = self._member("004")
        self.new = self._member("005")
        self.old_entries = self._entries(
            (self.a, SourcePresence.ATTACHED),
            (self.b, SourcePresence.ATTACHED),
            (self.c, SourcePresence.ATTACHED),
            (self.old_unallocated, SourcePresence.ATTACHED),
        )
        self.old_binding = self._binding(
            self.old_entries,
            session="published-transition",
            revision=7,
            composition_revision=0,
        )
        self.current_entries = self._entries(
            (self.a, SourcePresence.DETACHED),
            (self.c, SourcePresence.ATTACHED),
            (self.old_unallocated, SourcePresence.ATTACHED),
            (self.new, SourcePresence.ATTACHED),
        )
        self.current_binding = self._binding(
            self.current_entries,
            session="published-transition",
            revision=19,
            composition_revision=1,
        )
        self.live_binding = self.old_binding
        self.live_entries = self.old_entries
        self.transition = self._transition()
        self.manager = LocalReferenceManagerHandle("local-manager", "owner")
        self.store_path = Path(self.temporary.name).resolve()
        self.store_filename = "chunks.json"
        self.authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.live_binding,
            metadata_store=self._store(),
        )
        self.topology = self.authority.create_topology_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.live_binding,
                self.live_entries,
            ),
            workspace_transition_provider=lambda: self.transition,
        )
        self.chunk_a = self._create_chunk("第一分工", (self.a, self.b))
        self.chunk_b = self._create_chunk("第二分工", (self.c,))
        self.before_rebase_binding = chunk_plan_binding(
            self.authority.current_snapshot()
        )
        self.live_binding = self.current_binding
        self.live_entries = self.current_entries

    def _store(self) -> CollaborativeChunkStore:
        return CollaborativeChunkStore(
            self.store_path,
            self.store_filename,
            project_id=self.project_id,
        )

    def _member(self, local_segment_id: str):
        return chunk_segment_ref_from_ids(
            self.project_id,
            self.document_id,
            local_segment_id,
        )

    @staticmethod
    def _entries(*facts):
        return tuple(
            ChunkUniverseEntry(member, presence)
            for member, presence in facts
        )

    def _binding(
        self,
        entries,
        *,
        session: str,
        revision: int,
        composition_revision: int = 0,
    ):
        return ChunkWorkspaceBinding(
            project_id=self.project_id,
            workspace_session_id=session,
            workspace_revision=revision,
            workspace_composition_revision=composition_revision,
            segment_universe_digest=segment_universe_digest_v1(
                self.project_id,
                entries,
            ),
        )

    def _published_universe(
        self,
        entries,
        *,
        session: str,
        revision: int,
        composition_revision: int,
        workspace_digest_seed: bytes,
    ) -> ChunkPublishedUniverseProjection:
        return ChunkPublishedUniverseProjection(
            binding=ChunkPublishedUniverseBinding(
                project_id=self.project_id,
                workspace_session_id=session,
                workspace_revision=revision,
                workspace_composition_revision=composition_revision,
                workspace_digest=hashlib.sha256(workspace_digest_seed).hexdigest(),
                segment_universe_digest=segment_universe_digest_v1(
                    self.project_id,
                    entries,
                ),
            ),
            entries=entries,
        )

    def _transition(self, source_changed_members=None) -> ChunkPublishedWorkspaceTransition:
        previous = self._published_universe(
            self.old_entries,
            session="published-transition",
            revision=10,
            composition_revision=0,
            workspace_digest_seed=b"previous",
        )
        current = self._published_universe(
            self.current_entries,
            session="published-transition",
            revision=11,
            composition_revision=1,
            workspace_digest_seed=b"current",
        )
        source_changed = canonicalize_chunk_members(
            (self.c,) if source_changed_members is None else source_changed_members,
            allow_empty=True,
        )
        operation_id = "reconcile-" + hashlib.sha256(b"rebase").hexdigest()
        digest = chunk_published_workspace_transition_digest_v1(
            operation_id,
            previous,
            current,
            source_changed,
        )
        return ChunkPublishedWorkspaceTransition(
            operation_id=operation_id,
            previous=previous,
            current=current,
            source_changed_members=source_changed,
            transition_digest=digest,
        )

    def _create_chunk(self, name: str, members) -> str:
        expected = (
            None
            if self.authority.current_snapshot() is None
            else chunk_plan_binding(self.authority.current_snapshot())
        )
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=expected,
            action=TopologyAction.CREATE,
        )
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=expected,
            name=name,
            members=canonicalize_chunk_members(tuple(members)),
        )
        self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=expected,
        )
        return preview.created_chunk_ids[0]

    def _capability(self, action: TopologyAction):
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            action=action,
        )
        return capability

    def _error(self, code: str, callback) -> None:
        with self.assertRaises(ChunkError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), code)

    def test_exact_rebase_distinguishes_new_from_old_unallocated_and_cold_opens(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        inspection = self.topology.inspect_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertEqual(inspection.retained_detached_members, (self.a,))
        self.assertEqual(inspection.missing_members, (self.b,))
        self.assertEqual(inspection.source_changed_members, (self.c,))
        self.assertEqual(inspection.new_unallocated_members, (self.new,))
        self.assertNotIn(self.old_unallocated, inspection.new_unallocated_members)
        self.assertTrue(
            {"source", "target", "speaker", "path", "index"}.isdisjoint(
                field.name for field in fields(inspection)
            )
        )
        preview = self.topology.preview_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            released_missing_members=(self.b,),
            retire_empty_chunk_ids=(),
        )
        receipt = self.topology.apply_rebase(
            preview,
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertIs(receipt.action, TopologyAction.REBASE)
        after = self.authority.current_snapshot()
        self.assertEqual(after.segment_universe_digest, self.current_binding.segment_universe_digest)
        self.assertEqual(
            tuple(member for chunk in after.chunks for member in chunk.members),
            (self.a, self.c),
        )
        self.assertNotIn(self.new, tuple(member for chunk in after.chunks for member in chunk.members))
        self.assertIsNone(self._store().load_rebase_intent())
        cold = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.live_binding,
            metadata_store=self._store(),
        )
        self.assertEqual(cold.current_snapshot(), after)

    def test_partial_missing_release_and_second_transition_fail_closed(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        inspection = self.topology.inspect_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self._error(
            "CHUNK.REBASE_DECISION_REQUIRED",
            lambda: self.topology.preview_rebase(
                capability,
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=self.before_rebase_binding,
                released_missing_members=(),
                retire_empty_chunk_ids=(),
            ),
        )
        self.assertEqual(inspection.missing_members, (self.b,))

        all_missing_entries = self._entries(
            (self.old_unallocated, SourcePresence.ATTACHED),
            (self.new, SourcePresence.ATTACHED),
        )
        self.live_entries = all_missing_entries
        self.live_binding = self._binding(
            all_missing_entries,
            session="published-transition",
            revision=20,
            composition_revision=2,
        )
        second_capability = self._capability(TopologyAction.REBASE)
        self._error(
            "CHUNK.REBASE_REQUIRED",
            lambda: self.topology.inspect_rebase(
                second_capability,
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=self.before_rebase_binding,
            ),
        )

        # Returning to the same net universe must not erase evidence that a
        # second composition publication occurred in this live session.
        self.live_entries = self.current_entries
        self.live_binding = self._binding(
            self.current_entries,
            session="published-transition",
            revision=21,
            composition_revision=3,
        )
        returned_capability = self._capability(TopologyAction.REBASE)
        self._error(
            "CHUNK.REBASE_REQUIRED",
            lambda: self.topology.inspect_rebase(
                returned_capability,
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=self.before_rebase_binding,
            ),
        )

    def test_all_empty_requires_explicit_plan_dissolve(self) -> None:
        self.current_entries = self._entries(
            (self.old_unallocated, SourcePresence.ATTACHED),
            (self.new, SourcePresence.ATTACHED),
        )
        self.current_binding = self._binding(
            self.current_entries,
            session="published-transition",
            revision=20,
            composition_revision=1,
        )
        self.live_entries = self.current_entries
        self.live_binding = self.current_binding
        self.transition = self._transition(())
        capability = self._capability(TopologyAction.REBASE)
        inspection = self.topology.inspect_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertEqual(
            inspection.missing_members,
            canonicalize_chunk_members((self.a, self.b, self.c)),
        )
        self._error(
            "CHUNK.REBASE_DECISION_REQUIRED",
            lambda: self.topology.preview_rebase(
                capability,
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=self.before_rebase_binding,
                released_missing_members=inspection.missing_members,
                retire_empty_chunk_ids=(self.chunk_a, self.chunk_b),
            ),
        )
        dissolve_capability = self._capability(TopologyAction.DISSOLVE_PLAN)
        dissolve_preview = self.topology.preview_dissolve_plan(
            dissolve_capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.topology.apply_topology(
            dissolve_preview,
            dissolve_capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertIsNone(self.authority.current_snapshot())
        self.assertIsNone(self._store().load_rebase_intent())

    def test_single_empty_chunk_requires_exact_retirement_and_preserves_assignee_shape(self) -> None:
        self.live_binding = self.old_binding
        self.live_entries = self.old_entries
        assignment = self.authority.create_assignment_service()
        actor_port = LocalReferenceActorPort("local-actor", "alice")
        assignment_binding = self.before_rebase_binding
        assignment_capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.old_binding,
            expected_plan_binding=assignment_binding,
            action=TopologyAction.ASSIGN,
        )
        assignment_preview = assignment.preview_assign(
            assignment_capability,
            self.manager,
            workspace_binding=self.old_binding,
            expected_plan_binding=assignment_binding,
            chunk_id=self.chunk_a,
            target_actor_port=actor_port,
            target_actor_handle=actor_port.current_actor(),
        )
        assignment.apply_assignment(
            assignment_preview,
            assignment_capability,
            self.manager,
            workspace_binding=self.old_binding,
            expected_plan_binding=assignment_binding,
        )
        self.before_rebase_binding = chunk_plan_binding(
            self.authority.current_snapshot()
        )
        self.live_binding = self.current_binding
        self.live_entries = self.current_entries
        self.current_entries = self._entries(
            (self.a, SourcePresence.DETACHED),
            (self.old_unallocated, SourcePresence.ATTACHED),
            (self.new, SourcePresence.ATTACHED),
        )
        self.current_binding = self._binding(
            self.current_entries,
            session="published-transition",
            revision=20,
            composition_revision=1,
        )
        self.live_entries = self.current_entries
        self.live_binding = self.current_binding
        self.transition = self._transition(())
        capability = self._capability(TopologyAction.REBASE)
        inspection = self.topology.inspect_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertEqual(
            inspection.missing_members,
            canonicalize_chunk_members((self.b, self.c)),
        )
        self._error(
            "CHUNK.REBASE_DECISION_REQUIRED",
            lambda: self.topology.preview_rebase(
                capability,
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=self.before_rebase_binding,
                released_missing_members=inspection.missing_members,
                retire_empty_chunk_ids=(),
            ),
        )
        preview = self.topology.preview_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            released_missing_members=inspection.missing_members,
            retire_empty_chunk_ids=(self.chunk_b,),
        )
        self.topology.apply_rebase(
            preview,
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        after = self.authority.current_snapshot()
        self.assertEqual(tuple(chunk.chunk_id for chunk in after.chunks), (self.chunk_a,))
        self.assertEqual(after.chunks[0].members, (self.a,))
        self.assertEqual(
            after.chunks[0].assignee,
            AssigneeRef("local-actor", "alice"),
        )
        self.assertIn(self.chunk_b, self.authority.retired_chunk_ids())

    def test_intent_capture_fault_keeps_old_plan_and_no_partial_sidecar(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        before = self.authority.current_snapshot()
        real_replace = __import__("os").replace

        def fail_intent_replace(source, destination, *args, **kwargs):
            if str(destination).endswith("rebase-intent-v1"):
                raise OSError("private path")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch(
            "collaborative_chunk_store.os.replace",
            side_effect=fail_intent_replace,
        ):
            self._error(
                "CHUNK.COMMIT_FAILED",
                lambda: self.topology.inspect_rebase(
                    capability,
                    self.manager,
                    workspace_binding=self.live_binding,
                    expected_plan_binding=self.before_rebase_binding,
                ),
            )
        self.assertEqual(self.authority.current_snapshot(), before)
        self.assertIsNone(self._store().load_rebase_intent())

    def test_target_only_revision_keeps_plan_compatible_but_other_topology_cannot_rebase(self) -> None:
        self.live_entries = self.old_entries
        self.live_binding = replace(
            self.old_binding,
            workspace_revision=self.old_binding.workspace_revision + 1,
        )
        rename_capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            action=TopologyAction.RENAME,
        )
        rename_preview = self.topology.preview_rename(
            rename_capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            chunk_id=self.chunk_a,
            name="只改名称",
        )
        self.topology.apply_topology(
            rename_preview,
            rename_capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )

        changed_plan = chunk_plan_binding(self.authority.current_snapshot())
        self.live_entries = self.current_entries
        self.live_binding = self.current_binding
        self._error(
            "CHUNK.REVISION_STALE",
            lambda: self.authority.issue_manager_capability(
                self.manager,
                workspace_binding=self.live_binding,
                expected_plan_binding=changed_plan,
                action=TopologyAction.RENAME,
            ),
        )

    def test_rebase_publication_revalidates_exact_workspace_binding(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        preview = self.topology.preview_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            released_missing_members=(self.b,),
            retire_empty_chunk_ids=(),
        )
        expected = self.live_binding
        drifted = replace(expected, workspace_revision=expected.workspace_revision + 1)
        calls = 0

        def drift_after_capability_revalidation():
            nonlocal calls
            calls += 1
            return expected if calls == 1 else drifted

        capability_service = getattr(
            self.authority,
            "_ChunkTopologyPublicationAuthority__capability_service",
        )
        setattr(
            capability_service,
            "_ChunkManagerCapabilityService__workspace_binding_provider",
            drift_after_capability_revalidation,
        )
        setattr(
            self.authority,
            "_ChunkTopologyPublicationAuthority__workspace_binding_provider",
            drift_after_capability_revalidation,
        )
        self._error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.topology.apply_rebase(
                preview,
                capability,
                self.manager,
                workspace_binding=expected,
                expected_plan_binding=self.before_rebase_binding,
            ),
        )
        self.assertEqual(
            chunk_plan_binding(self.authority.current_snapshot()),
            self.before_rebase_binding,
        )

    def test_committed_intent_cleanup_fault_is_body_safe_and_cold_recoverable(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        preview = self.topology.preview_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
            released_missing_members=(self.b,),
            retire_empty_chunk_ids=(),
        )
        real_unlink = __import__("os").unlink

        def fail_intent_unlink(path, *args, **kwargs):
            if str(path).endswith("rebase-intent-v1"):
                raise PermissionError("private /secret/path")
            return real_unlink(path, *args, **kwargs)

        with mock.patch(
            "collaborative_chunk_store.os.unlink",
            side_effect=fail_intent_unlink,
        ):
            with self.assertRaises(ChunkError) as apply_failure:
                self.topology.apply_rebase(
                    preview,
                    capability,
                    self.manager,
                    workspace_binding=self.live_binding,
                    expected_plan_binding=self.before_rebase_binding,
                )
        self.assertEqual(apply_failure.exception.code, "CHUNK.RECOVERY_REQUIRED")
        self.assertTrue(apply_failure.exception.retryable)

        store = self._store()
        recovery = store.recover()
        self.assertEqual(recovery.outcome, "rolled_forward")
        self.assertIsNotNone(store.load_rebase_intent())
        committed = recovery.state.active_snapshot

        with mock.patch(
            "collaborative_chunk_store.os.unlink",
            side_effect=fail_intent_unlink,
        ):
            with self.assertRaises(ChunkError) as cold_failure:
                ChunkTopologyPublicationAuthority(
                    project_id=self.project_id,
                    workspace_binding_provider=lambda: self.live_binding,
                    metadata_store=store,
                )
        self.assertEqual(cold_failure.exception.code, "CHUNK.RECOVERY_REQUIRED")
        self.assertTrue(cold_failure.exception.retryable)
        self.assertNotIn("secret", str(cold_failure.exception))

        cold = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.live_binding,
            metadata_store=store,
        )
        self.assertEqual(cold.current_snapshot(), committed)
        self.assertIsNone(store.load_rebase_intent())

    def test_rebase_contracts_and_adapter_keep_authority_boundary_body_free(self) -> None:
        root = Path(__file__).parents[1]
        core_tree = ast.parse(
            root.joinpath("collaborative_chunks.py").read_text(encoding="utf-8")
        )
        core_imports = {
            node.module.split(".")[0]
            for node in ast.walk(core_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("project_workspace", core_imports)
        adapter_tree = ast.parse(
            root.joinpath("collaborative_chunk_workspace_adapter.py").read_text(
                encoding="utf-8"
            )
        )
        adapter_imports = {
            node.module.split(".")[0]
            for node in ast.walk(adapter_tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertTrue(
            adapter_imports.issubset(
                {
                    "__future__",
                    "collaborative_chunk_contracts",
                    "project_workspace",
                    "project_workspace_contracts",
                    "project_workspace_identity",
                }
            ),
            adapter_imports,
        )
        forbidden = {
            "source",
            "target",
            "speaker",
            "confirmed",
            "path",
            "payload",
            "carrier",
            "destination",
            "tmx",
        }
        for contract in (
            ChunkPublishedWorkspaceTransition,
            ChunkRebaseIntent,
            ChunkRebaseInspection,
            ChunkRebasePreview,
        ):
            names = {field.name.casefold() for field in fields(contract)}
            self.assertFalse(names & forbidden, (contract.__name__, names & forbidden))

    def test_cold_resume_uses_durable_intent_not_a_carried_transition(self) -> None:
        capability = self._capability(TopologyAction.REBASE)
        expected_inspection = self.topology.inspect_rebase(
            capability,
            self.manager,
            workspace_binding=self.live_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        cold_binding = replace(
            self.live_binding,
            workspace_session_id="cold-session",
            workspace_revision=0,
            workspace_composition_revision=0,
        )
        cycled_binding = replace(
            cold_binding,
            workspace_revision=2,
            workspace_composition_revision=2,
        )
        cycled_authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: cycled_binding,
            metadata_store=self._store(),
        )
        cycled_topology = cycled_authority.create_topology_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                cycled_binding,
                self.live_entries,
            ),
        )
        cycled_capability = cycled_authority.issue_manager_capability(
            self.manager,
            workspace_binding=cycled_binding,
            expected_plan_binding=self.before_rebase_binding,
            action=TopologyAction.REBASE,
        )
        self._error(
            "CHUNK.REBASE_REQUIRED",
            lambda: cycled_topology.inspect_rebase(
                cycled_capability,
                self.manager,
                workspace_binding=cycled_binding,
                expected_plan_binding=self.before_rebase_binding,
            ),
        )

        cold_authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: cold_binding,
            metadata_store=self._store(),
        )
        cold_topology = cold_authority.create_topology_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                cold_binding,
                self.live_entries,
            ),
        )
        cold_capability = cold_authority.issue_manager_capability(
            self.manager,
            workspace_binding=cold_binding,
            expected_plan_binding=self.before_rebase_binding,
            action=TopologyAction.REBASE,
        )
        self.assertEqual(
            cold_topology.inspect_rebase(
                cold_capability,
                self.manager,
                workspace_binding=cold_binding,
                expected_plan_binding=self.before_rebase_binding,
            ),
            expected_inspection,
        )
        intent = self._store().load_rebase_intent()
        encoded_intent = encode_chunk_rebase_intent(intent)
        self.assertEqual(decode_chunk_rebase_intent(encoded_intent), intent)
        self.assertIn(b'"source_changed_indices"', encoded_intent)
        self.assertNotIn(b'"source_changed_members"', encoded_intent)
        self.assertLessEqual(
            DERIVED_MAX_REBASE_INTENT_BYTES_V1,
            MAX_REBASE_INTENT_BYTES,
        )
        with mock.patch.object(
            chunk_store_module,
            "MAX_REBASE_INTENT_BYTES",
            8,
        ):
            self._error(
                "CHUNK.LIMIT_EXCEEDED",
                lambda: decode_chunk_rebase_intent(b"{" + b" " * 8),
            )
        tampered = encoded_intent.replace(
            intent.intent_digest.encode("ascii"),
            ("0" * 64).encode("ascii"),
            1,
        )
        self._error(
            "CHUNK.DIGEST_MISMATCH",
            lambda: decode_chunk_rebase_intent(tampered),
        )
        preview = cold_topology.preview_rebase(
            cold_capability,
            self.manager,
            workspace_binding=cold_binding,
            expected_plan_binding=self.before_rebase_binding,
            released_missing_members=(self.b,),
            retire_empty_chunk_ids=(),
        )
        cold_topology.apply_rebase(
            preview,
            cold_capability,
            self.manager,
            workspace_binding=cold_binding,
            expected_plan_binding=self.before_rebase_binding,
        )
        self.assertIsNone(self._store().load_rebase_intent())


if __name__ == "__main__":
    unittest.main()
