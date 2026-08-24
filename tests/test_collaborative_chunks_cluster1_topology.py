from __future__ import annotations

from dataclasses import fields, replace
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from collaborative_chunk_contracts import (
    CHUNK_METADATA_NAMESPACE,
    DISSOLVED_CHUNK_PLAN_DIGEST,
    EMPTY_CHUNK_AUDIT_DIGEST,
    AssigneeRef,
    ChunkError,
    ChunkMutationPreview,
    ChunkOperationReceipt,
    ChunkPlanSnapshot,
    ChunkSegmentRef,
    ChunkSplitChild,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    CollaborativeChunk,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_operation_audit_digest_v1,
    chunk_plan_binding,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
    validate_c1_snapshot,
)
from collaborative_chunks import (
    ChunkTopologyPublicationAuthority,
    ChunkTopologyPublicationResult,
    CollaborativeChunkTopologyService,
)
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import issue_project_id


def _document(seed: int) -> str:
    return "doc-" + bytes([seed]).hex() * 32


def _member(project_id: str, document_seed: int, local_id: str) -> ChunkSegmentRef:
    return ChunkSegmentRef(
        project_id=project_id,
        identity=SegmentIdentity(_document(document_seed), local_id),
    )


class _DeterministicIssuer:
    def __init__(self, kind: str, start: int) -> None:
        self.kind = kind
        self.next_value = start
        self.calls = 0

    def __call__(self) -> str:
        value = self.next_value
        self.next_value += 1
        self.calls += 1
        seed = value.to_bytes(32, "big", signed=False)
        if self.kind == "chunk":
            return issue_chunk_id(seed)
        if self.kind == "plan":
            return issue_chunk_plan_id(seed)
        return issue_chunk_operation_id(seed)


class _TopologyOwner:
    def __init__(
        self,
        project_id: str,
        entries: tuple[ChunkUniverseEntry, ...],
    ) -> None:
        self.project_id = project_id
        self.entries = entries
        self.workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=segment_universe_digest_v1(
                project_id,
                entries,
            ),
        )
        self.fail_universe = False
        self.authority: ChunkTopologyPublicationAuthority | None = None

    def universe(self) -> ChunkWorkspaceUniverseProjection:
        if self.fail_universe:
            raise RuntimeError("/private/source.txt SECRET target body")
        return ChunkWorkspaceUniverseProjection(
            binding=self.workspace_binding,
            entries=self.entries,
        )

    @property
    def snapshot(self) -> ChunkPlanSnapshot | None:
        assert self.authority is not None
        return self.authority.current_snapshot()

    @property
    def retired(self) -> tuple[str, ...]:
        assert self.authority is not None
        return self.authority.retired_chunk_ids()

    @property
    def receipts(self) -> tuple[ChunkOperationReceipt, ...]:
        assert self.authority is not None
        return self.authority.operation_receipts()

    @property
    def publish_calls(self) -> int:
        return len(self.receipts)


class CollaborativeChunkTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_id = issue_project_id(b"P" * 32)
        self.same_doc_1 = _member(self.project_id, 1, "same")
        self.same_doc_2 = _member(self.project_id, 2, "same")
        self.a = _member(self.project_id, 1, "a")
        self.b = _member(self.project_id, 1, "b")
        self.c = _member(self.project_id, 2, "c")
        self.detached = _member(self.project_id, 2, "detached")
        self.entries = (
            ChunkUniverseEntry(self.same_doc_1, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.same_doc_2, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.a, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.b, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.c, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.detached, SourcePresence.DETACHED),
        )
        self.owner = _TopologyOwner(self.project_id, self.entries)
        self.manager = LocalReferenceManagerHandle("local", "manager")
        self.chunk_issuer = _DeterministicIssuer("chunk", 1)
        self.plan_issuer = _DeterministicIssuer("plan", 100)
        self.operation_issuer = _DeterministicIssuer("operation", 200)
        self.configure_runtime()

    def configure_runtime(
        self,
        *,
        initial_snapshot: ChunkPlanSnapshot | None = None,
        retired_chunk_ids: tuple[str, ...] = (),
        used_chunk_plan_ids: tuple[str, ...] = (),
    ) -> None:
        self.authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.owner.workspace_binding,
            initial_snapshot=initial_snapshot,
            retired_chunk_ids=retired_chunk_ids,
            used_chunk_plan_ids=used_chunk_plan_ids,
        )
        self.owner.authority = self.authority
        self.topology = self.authority.create_topology_service(
            workspace_universe_provider=self.owner.universe,
            chunk_id_issuer=self.chunk_issuer,
            plan_id_issuer=self.plan_issuer,
            operation_id_issuer=self.operation_issuer,
        )

    def binding(self):
        return None if self.owner.snapshot is None else chunk_plan_binding(
            self.owner.snapshot
        )

    def capability(self, action: TopologyAction):
        expected = self.binding()
        return self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            action=action,
        ), expected

    def apply(self, preview, capability, expected):
        return self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
        )

    def create(self, name: str, members: tuple[ChunkSegmentRef, ...]) -> str:
        capability, expected = self.capability(TopologyAction.CREATE)
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            name=name,
            members=canonicalize_chunk_members(members),
        )
        self.apply(preview, capability, expected)
        return preview.created_chunk_ids[0]

    def seed_snapshot(
        self,
        chunk_specs: tuple[tuple[str, tuple[ChunkSegmentRef, ...]], ...],
    ) -> None:
        chunks = tuple(
            CollaborativeChunk(
                chunk_id=issue_chunk_id((220 + order).to_bytes(32, "big")),
                name=name,
                order=order,
                members=canonicalize_chunk_members(members),
                assignee=None,
            )
            for order, (name, members) in enumerate(chunk_specs)
        )
        snapshot = validate_c1_snapshot(
            ChunkPlanSnapshot(
                schema_version=1,
                namespace=CHUNK_METADATA_NAMESPACE,
                chunk_plan_id=issue_chunk_plan_id(b"S" * 32),
                project_id=self.project_id,
                revision=1,
                segment_universe_digest=self.owner.workspace_binding.segment_universe_digest,
                chunks=chunks,
                audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
            )
        )
        self.configure_runtime(initial_snapshot=snapshot)

    def assert_error(self, code: str, callable_) -> None:
        with self.assertRaises(ChunkError) as captured:
            callable_()
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), code)

    def test_universe_projection_and_unallocated_use_attached_composite_identity(self) -> None:
        projection = self.owner.universe()
        self.assertEqual(projection.binding, self.owner.workspace_binding)
        self.assertEqual(
            set(self.topology.unallocated_members(
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=None,
            )),
            {
                self.same_doc_1,
                self.same_doc_2,
                self.a,
                self.b,
                self.c,
            },
        )
        self.assertNotIn(
            self.detached,
            self.topology.unallocated_members(
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=None,
            ),
        )
        bad_binding = replace(
            self.owner.workspace_binding,
            segment_universe_digest="f" * 64,
        )
        self.assert_error(
            "CHUNK.UNIVERSE_MISMATCH",
            lambda: ChunkWorkspaceUniverseProjection(bad_binding, self.entries),
        )

    def test_create_initial_and_existing_plan_are_exact_and_unassigned(self) -> None:
        first_id = self.create("跨文档", (self.same_doc_1, self.same_doc_2))
        self.assertEqual(self.owner.snapshot.revision, 1)
        self.assertEqual(self.owner.snapshot.chunks[0].chunk_id, first_id)
        self.assertIsNone(self.owner.snapshot.chunks[0].assignee)
        self.assertEqual(self.owner.receipts[-1].assignment_count, 0)
        second_id = self.create("第二组", (self.a,))
        self.assertEqual(self.owner.snapshot.revision, 2)
        self.assertEqual(
            tuple(chunk.chunk_id for chunk in self.owner.snapshot.chunks),
            (first_id, second_id),
        )
        self.assertEqual(
            self.topology.unallocated_members(
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=self.binding(),
            ),
            canonicalize_chunk_members((self.b, self.c), allow_empty=True),
        )

    def test_create_rejects_detached_unknown_allocated_and_assignment_before_id_issue(self) -> None:
        self.create("一", (self.a,))
        before = self.owner.snapshot
        before_calls = self.chunk_issuer.calls
        capability, expected = self.capability(TopologyAction.CREATE)

        def preview(members, assignee=None):
            return self.topology.preview_create(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                name="非法",
                members=canonicalize_chunk_members(members),
                assignee=assignee,
            )

        self.assert_error(
            "CHUNK.MEMBER_UNALLOCATED_REQUIRED",
            lambda: preview((self.detached,)),
        )
        self.assert_error(
            "CHUNK.MEMBER_UNALLOCATED_REQUIRED",
            lambda: preview((self.a,)),
        )
        unknown = _member(self.project_id, 1, "unknown")
        self.assert_error("CHUNK.MEMBER_UNKNOWN", lambda: preview((unknown,)))
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: preview((self.b,), AssigneeRef("a", "b")),
        )
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_create(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                name="",
                members=(self.b,),
            ),
        )
        self.assertEqual(self.owner.snapshot, before)
        self.assertEqual(self.chunk_issuer.calls, before_calls)
        self.assertEqual(self.owner.publish_calls, 1)

    def test_rename_and_full_permutation_reorder_preserve_identity_and_membership(self) -> None:
        first = self.create("一", (self.a,))
        second = self.create("二", (self.b,))
        before_members = {
            chunk.chunk_id: chunk.members for chunk in self.owner.snapshot.chunks
        }
        capability, expected = self.capability(TopologyAction.RENAME)
        preview = self.topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=first,
            name="改名",
        )
        self.apply(preview, capability, expected)
        capability, expected = self.capability(TopologyAction.REORDER)
        preview = self.topology.preview_reorder(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            ordered_chunk_ids=(second, first),
        )
        self.apply(preview, capability, expected)
        self.assertEqual(
            tuple(chunk.chunk_id for chunk in self.owner.snapshot.chunks),
            (second, first),
        )
        self.assertEqual(
            {chunk.chunk_id: chunk.members for chunk in self.owner.snapshot.chunks},
            before_members,
        )
        capability, expected = self.capability(TopologyAction.REORDER)
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_reorder(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                ordered_chunk_ids=(second, first),
            ),
        )

    def test_split_is_exact_retires_parent_and_preserves_detached(self) -> None:
        self.seed_snapshot((("原", (self.a, self.b, self.detached)),))
        parent_id = self.owner.snapshot.chunks[0].chunk_id
        capability, expected = self.capability(TopologyAction.SPLIT)
        children = (
            ChunkSplitChild("左", canonicalize_chunk_members((self.a, self.detached))),
            ChunkSplitChild("右", canonicalize_chunk_members((self.b,))),
        )
        preview = self.topology.preview_split(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_id=parent_id,
            children=children,
        )
        receipt = self.apply(preview, capability, expected)
        self.assertEqual(receipt.retired_chunk_ids, (parent_id,))
        self.assertIn(parent_id, self.owner.retired)
        self.assertEqual(len(self.owner.snapshot.chunks), 2)
        self.assertTrue(all(chunk.assignee is None for chunk in self.owner.snapshot.chunks))
        self.assertEqual(
            {_member_key for _member_key in (
                member for chunk in self.owner.snapshot.chunks for member in chunk.members
            )},
            {self.a, self.b, self.detached},
        )

    def test_invalid_split_fails_before_new_ids_or_private_publication(self) -> None:
        parent = self.create("原", (self.a, self.b))
        before = self.owner.snapshot
        before_ids = self.chunk_issuer.calls
        capability, expected = self.capability(TopologyAction.SPLIT)
        bad = (
            ChunkSplitChild("左", canonicalize_chunk_members((self.a,))),
            ChunkSplitChild("右", canonicalize_chunk_members((self.a,))),
        )
        self.assert_error(
            "CHUNK.SPLIT_INVALID",
            lambda: self.topology.preview_split(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_id=parent,
                children=bad,
            ),
        )
        self.assertEqual(self.owner.snapshot, before)
        self.assertEqual(self.chunk_issuer.calls, before_ids)
        assigned = (
            ChunkSplitChild(
                "左",
                canonicalize_chunk_members((self.a,)),
                AssigneeRef("future", "actor"),
            ),
            ChunkSplitChild("右", canonicalize_chunk_members((self.b,))),
        )
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_split(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_id=parent,
                children=assigned,
            ),
        )

    def test_foreign_project_same_document_local_key_cannot_move_release_or_split(self) -> None:
        source = self.create("源", (self.a, self.b))
        destination = self.create("目标", (self.c,))
        foreign_project = issue_project_id(b"F" * 32)
        foreign_a = ChunkSegmentRef(
            foreign_project,
            SegmentIdentity(
                self.a.identity.document_id,
                self.a.identity.local_segment_id,
            ),
        )
        before = self.owner.snapshot
        for action, invoke in (
            (
                TopologyAction.RELEASE,
                lambda capability, expected: self.topology.preview_release(
                    capability,
                    self.manager,
                    workspace_binding=self.owner.workspace_binding,
                    expected_plan_binding=expected,
                    source_chunk_id=source,
                    members=(foreign_a,),
                    retire_source_if_empty=False,
                ),
            ),
            (
                TopologyAction.MOVE,
                lambda capability, expected: self.topology.preview_move(
                    capability,
                    self.manager,
                    workspace_binding=self.owner.workspace_binding,
                    expected_plan_binding=expected,
                    source_chunk_id=source,
                    destination_chunk_id=destination,
                    members=(foreign_a,),
                    retire_source_if_empty=False,
                ),
            ),
            (
                TopologyAction.SPLIT,
                lambda capability, expected: self.topology.preview_split(
                    capability,
                    self.manager,
                    workspace_binding=self.owner.workspace_binding,
                    expected_plan_binding=expected,
                    source_chunk_id=source,
                    children=(
                        ChunkSplitChild("外", (foreign_a,)),
                        ChunkSplitChild("内", (self.b,)),
                    ),
                ),
            ),
        ):
            capability, expected = self.capability(action)
            self.assert_error(
                "CHUNK.IDENTITY_FOREIGN",
                lambda capability=capability, expected=expected, invoke=invoke: invoke(
                    capability,
                    expected,
                ),
            )
        self.assertEqual(self.owner.snapshot, before)
        self.assertEqual(self.owner.publish_calls, 2)

    def test_merge_nonadjacent_chunks_uses_exact_union_and_minimum_order(self) -> None:
        first = self.create("一", (self.a,))
        middle = self.create("二", (self.b,))
        third = self.create("三", (self.c,))
        capability, expected = self.capability(TopologyAction.MERGE)
        preview = self.topology.preview_merge(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_ids=(third, first),
            result_name="合并",
        )
        self.apply(preview, capability, expected)
        result = self.owner.snapshot.chunks[0]
        self.assertEqual(result.chunk_id, preview.created_chunk_ids[0])
        self.assertEqual(result.members, canonicalize_chunk_members((self.a, self.c)))
        self.assertEqual(self.owner.snapshot.chunks[1].chunk_id, middle)
        self.assertEqual(set(preview.retired_chunk_ids), {first, third})

    def test_move_release_and_dissolve_keep_union_disjoint_and_explicit_retirement(self) -> None:
        first = self.create("一", (self.a, self.b))
        second = self.create("二", (self.c,))
        capability, expected = self.capability(TopologyAction.MOVE)
        preview = self.topology.preview_move(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_id=first,
            destination_chunk_id=second,
            members=canonicalize_chunk_members((self.a,)),
            retire_source_if_empty=False,
        )
        self.apply(preview, capability, expected)
        by_id = {chunk.chunk_id: chunk for chunk in self.owner.snapshot.chunks}
        self.assertEqual(by_id[first].members, (self.b,))
        self.assertEqual(by_id[second].members, canonicalize_chunk_members((self.a, self.c)))
        capability, expected = self.capability(TopologyAction.RELEASE)
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_release(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_id=first,
                members=(self.b,),
                retire_source_if_empty=False,
            ),
        )
        preview = self.topology.preview_release(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_id=first,
            members=(self.b,),
            retire_source_if_empty=True,
        )
        self.apply(preview, capability, expected)
        self.assertIn(first, self.owner.retired)
        self.assertIn(
            self.b,
            self.topology.unallocated_members(
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=self.binding(),
            ),
        )
        capability, expected = self.capability(TopologyAction.DISSOLVE_CHUNK)
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_dissolve_chunk(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=second,
            ),
        )

    def test_release_detached_never_turns_it_into_unallocated(self) -> None:
        self.seed_snapshot(
            (
                ("含分离", (self.a, self.detached)),
                ("保留", (self.b,)),
            )
        )
        source = self.owner.snapshot.chunks[0].chunk_id
        capability, expected = self.capability(TopologyAction.RELEASE)
        preview = self.topology.preview_release(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_id=source,
            members=(self.detached,),
            retire_source_if_empty=False,
        )
        self.apply(preview, capability, expected)
        unallocated = self.topology.unallocated_members(
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=self.binding(),
        )
        self.assertNotIn(self.detached, unallocated)
        self.assertIn(self.same_doc_1, unallocated)

    def test_dissolve_plan_publishes_absent_snapshot_and_tombstone_digest(self) -> None:
        first = self.create("一", (self.a,))
        second = self.create("二", (self.b,))
        capability, expected = self.capability(TopologyAction.DISSOLVE_PLAN)
        preview = self.topology.preview_dissolve_plan(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
        )
        self.assertEqual(preview.after_plan_digest, DISSOLVED_CHUNK_PLAN_DIGEST)
        receipt = self.apply(preview, capability, expected)
        self.assertIsNone(self.owner.snapshot)
        self.assertEqual(set(self.owner.retired), {first, second})
        self.assertEqual(receipt.published_revision, expected.plan_revision + 1)

    def test_dissolved_plan_id_cannot_be_reissued_at_revision_one(self) -> None:
        self.create("一", (self.a,))
        old_plan_id = self.owner.snapshot.chunk_plan_id
        capability, expected = self.capability(TopologyAction.DISSOLVE_PLAN)
        preview = self.topology.preview_dissolve_plan(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
        )
        self.apply(preview, capability, expected)
        self.assertIn(old_plan_id, self.authority.used_chunk_plan_ids())
        self.plan_issuer.next_value = 100
        before_chunk_calls = self.chunk_issuer.calls
        before_operation_calls = self.operation_issuer.calls
        capability, expected = self.capability(TopologyAction.CREATE)
        self.assert_error(
            "CHUNK.IDENTITY_DUPLICATE",
            lambda: self.topology.preview_create(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                name="重建",
                members=(self.b,),
            ),
        )
        self.assertIsNone(self.owner.snapshot)
        self.assertEqual(self.owner.publish_calls, 2)
        self.assertEqual(self.chunk_issuer.calls, before_chunk_calls)
        self.assertEqual(self.operation_issuer.calls, before_operation_calls)

    def test_one_capability_has_one_bounded_pending_plan_and_old_preview_stales(self) -> None:
        capability, expected = self.capability(TopologyAction.CREATE)
        previews = []
        for index in range(20):
            previews.append(
                self.topology.preview_create(
                    capability,
                    self.manager,
                    workspace_binding=self.owner.workspace_binding,
                    expected_plan_binding=expected,
                    name=f"候选 {index}",
                    members=(self.a,),
                )
            )
        prepared = self.topology._CollaborativeChunkTopologyService__prepared
        pending = self.topology._CollaborativeChunkTopologyService__pending_by_capability
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(pending), 1)
        self.assert_error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.apply(previews[0], capability, expected),
        )
        self.apply(previews[-1], capability, expected)
        self.assertEqual(len(prepared), 0)
        self.assertEqual(len(pending), 0)

    def test_last_chunk_release_and_rename_noop_require_explicit_other_action(self) -> None:
        chunk_id = self.create("一", (self.a,))
        capability, expected = self.capability(TopologyAction.RELEASE)
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_release(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_id=chunk_id,
                members=(self.a,),
                retire_source_if_empty=True,
            ),
        )
        capability, expected = self.capability(TopologyAction.RENAME)
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.topology.preview_rename(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=chunk_id,
                name="一",
            ),
        )

    def test_public_preview_needs_registered_capability_and_is_single_use(self) -> None:
        capability, expected = self.capability(TopologyAction.CREATE)
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            name="一",
            members=(self.a,),
        )
        forged = replace(preview, affected_member_count=2)
        self.assert_error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.apply(forged, capability, expected),
        )
        receipt = self.apply(preview, capability, expected)
        self.assertEqual(receipt.operation_id, preview.operation_id)
        self.assert_error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.apply(preview, capability, expected),
        )
        self.assertEqual(self.owner.publish_calls, 1)

    def test_publication_authority_has_one_sealed_topology_owner(self) -> None:
        self.assert_error(
            "CHUNK.MANAGER_REQUIRED",
            lambda: self.authority.publish_atomic(
                object(),
                None,
                None,
                (),
                None,
            ),
        )
        self.assert_error(
            "CHUNK.MANAGER_REQUIRED",
            lambda: self.authority.create_topology_service(
                workspace_universe_provider=self.owner.universe,
            ),
        )
        fresh_authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.owner.workspace_binding,
        )
        self.assertFalse(hasattr(fresh_authority, "_bind_topology_owner"))
        self.assert_error(
            "CHUNK.MANAGER_REQUIRED",
            lambda: CollaborativeChunkTopologyService(
                fresh_authority,
                project_id=self.project_id,
                workspace_universe_provider=self.owner.universe,
            ),
        )
        self.assertIsNone(fresh_authority.current_snapshot())
        self.assertEqual(fresh_authority.operation_receipts(), ())
        self.assertIsNone(self.owner.snapshot)
        self.assertEqual(self.owner.receipts, ())

    def test_concurrent_apply_has_exactly_one_winner(self) -> None:
        capability, expected = self.capability(TopologyAction.CREATE)
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            name="并发",
            members=(self.a,),
        )

        def run():
            try:
                return self.apply(preview, capability, expected).operation_id
            except ChunkError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: run(), range(2)))
        self.assertEqual(results.count(preview.operation_id), 1)
        self.assertEqual(results.count("CHUNK.PREVIEW_STALE"), 1)
        self.assertEqual(self.owner.snapshot.revision, 1)
        self.assertEqual(self.owner.publish_calls, 1)

    def test_workspace_plan_and_universe_drift_fail_before_publication(self) -> None:
        self.create("一", (self.a,))
        capability, expected = self.capability(TopologyAction.RENAME)
        preview = self.topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.owner.snapshot.chunks[0].chunk_id,
            name="新",
        )
        before = self.owner.snapshot
        self.owner.workspace_binding = replace(
            self.owner.workspace_binding,
            workspace_revision=self.owner.workspace_binding.workspace_revision + 1,
        )
        self.assert_error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.apply(preview, capability, expected),
        )
        self.assertEqual(self.owner.snapshot, before)
        self.assertEqual(self.owner.publish_calls, 1)

    def test_universe_provider_failure_is_body_safe_and_zero_publish(self) -> None:
        self.owner.fail_universe = True
        capability, expected = self.capability(TopologyAction.CREATE)
        self.assert_error(
            "CHUNK.RECOVERY_REQUIRED",
            lambda: self.topology.preview_create(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                name="一",
                members=(self.a,),
            ),
        )
        self.owner.fail_universe = False
        self.assertIsNone(self.owner.snapshot)
        self.assertEqual(self.owner.retired, ())
        self.assertEqual(self.owner.receipts, ())
        self.assertEqual(
            self.authority.revalidate_manager_capability(
                capability,
                self.manager,
                workspace_binding=self.owner.workspace_binding,
                expected_plan_binding=expected,
                action=TopologyAction.CREATE,
            ),
            self.manager.actor_ref,
        )

    def test_two_distinct_previews_on_one_base_never_union(self) -> None:
        chunk_id = self.create("一", (self.a,))
        first_capability, expected = self.capability(TopologyAction.RENAME)
        second_capability, second_expected = self.capability(TopologyAction.RENAME)
        first = self.topology.preview_rename(
            first_capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=chunk_id,
            name="甲",
        )
        second = self.topology.preview_rename(
            second_capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=second_expected,
            chunk_id=chunk_id,
            name="乙",
        )
        self.apply(first, first_capability, expected)
        self.assert_error(
            "CHUNK.PREVIEW_STALE",
            lambda: self.apply(second, second_capability, second_expected),
        )
        self.assertEqual(self.owner.snapshot.revision, expected.plan_revision + 1)
        self.assertEqual(self.owner.snapshot.chunks[0].name, "甲")
        self.assertEqual(
            len(self.topology._CollaborativeChunkTopologyService__prepared),
            0,
        )

    def test_c2_assigned_baseline_is_accepted_and_rename_preserves_assignment(self) -> None:
        self.seed_snapshot((("一", (self.a,)),))
        assigned = replace(
            self.owner.snapshot.chunks[0],
            assignee=AssigneeRef("future", "actor"),
        )
        invalid = replace(self.owner.snapshot, chunks=(assigned,))
        self.configure_runtime(initial_snapshot=invalid)
        expected = self.binding()
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            action=TopologyAction.RENAME,
        )
        preview = self.topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.owner.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=assigned.chunk_id,
            name="已分配",
        )
        self.apply(preview, capability, expected)
        self.assertEqual(
            self.owner.snapshot.chunks[0].assignee,
            AssigneeRef("future", "actor"),
        )

    def test_assignment_commands_fail_closed(self) -> None:
        self.assertIsNone(self.owner.snapshot)
        self.assert_error(
            "CHUNK.ASSIGNMENT_UNAVAILABLE",
            lambda: self.topology.reject_assignment_command("assign"),
        )
        for command in (
            self.topology.preview_assign,
            self.topology.preview_reassign,
            self.topology.preview_unassign,
        ):
            self.assert_error("CHUNK.ASSIGNMENT_UNAVAILABLE", command)

    def test_truncated_public_preview_is_never_an_audit_authority(self) -> None:
        chunk_id = issue_chunk_id(b"A" * 32)
        preview = ChunkMutationPreview(
            operation_id=issue_chunk_operation_id(b"O" * 32),
            action=TopologyAction.CREATE,
            project_id=self.project_id,
            chunk_plan_id=issue_chunk_plan_id(b"Q" * 32),
            base_revision=0,
            published_revision=1,
            before_plan_digest="0" * 64,
            after_plan_digest="1" * 64,
            affected_chunk_ids=(chunk_id,),
            created_chunk_ids=(chunk_id,),
            retired_chunk_ids=(),
            affected_chunk_count=100_001,
            created_chunk_count=1,
            retired_chunk_count=0,
            affected_member_count=1,
            assignment_count=0,
            warnings=("CHUNK.LIMIT_EXCEEDED",),
            blockers=(),
            truncated=True,
        )
        self.assert_error(
            "CHUNK.CONTRACT_INVALID",
            lambda: chunk_operation_audit_digest_v1(
                preview,
                self.manager.actor_ref,
                EMPTY_CHUNK_AUDIT_DIGEST,
            ),
        )

    def test_topology_module_dependency_and_public_field_boundaries_are_closed(self) -> None:
        source = Path(__file__).parents[1].joinpath("collaborative_chunks.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
        self.assertTrue(
            imported.issubset(
                {
                    "__future__",
                    "dataclasses",
                    "secrets",
                    "threading",
                    "typing",
                    "collaborative_chunk_contracts",
                    "collaborative_chunk_store",
                    "project_workspace_contracts",
                    "project_workspace_identity",
                }
            )
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
            ChunkMutationPreview,
            ChunkOperationReceipt,
            ChunkWorkspaceUniverseProjection,
            ChunkTopologyPublicationResult,
            ChunkSplitChild,
        ):
            names = {field.name.casefold() for field in fields(contract)}
            self.assertFalse(names & forbidden, (contract.__name__, names & forbidden))


if __name__ == "__main__":
    unittest.main()
