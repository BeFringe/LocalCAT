from __future__ import annotations

import copy
from dataclasses import fields, replace
from pathlib import Path
import pickle
import tempfile
import unittest

from collaborative_chunk_contracts import (
    AssigneeRef,
    ChunkAccessKind,
    ChunkEditOperation,
    ChunkError,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    ChunkSplitChild,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_plan_binding,
    chunk_segment_ref_from_ids,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
    validate_c1_operation_receipt,
    validate_c1_snapshot,
    validate_chunk_operation_receipt,
    validate_chunk_plan_snapshot,
)
from collaborative_chunk_store import CollaborativeChunkStore
from collaborative_chunks import (
    AuthenticatedActorHandle,
    ChunkActorCapability,
    ChunkTopologyPublicationAuthority,
    LocalReferenceActorPort,
)
from project_workspace_contracts import SourcePresence
from project_workspace_identity import issue_project_id
from tests.test_collaborative_chunks_cluster1_acceptance import _RealPackageHarness


class _Issuer:
    def __init__(self, kind: str, start: int) -> None:
        self.kind = kind
        self.value = start

    def __call__(self) -> str:
        seed = self.value.to_bytes(32, "big", signed=False)
        self.value += 1
        if self.kind == "chunk":
            return issue_chunk_id(seed)
        if self.kind == "plan":
            return issue_chunk_plan_id(seed)
        return issue_chunk_operation_id(seed)


class _RecordingMutationPort:
    def __init__(self, result: object = None) -> None:
        self.calls = 0
        self.result = result

    def apply_segment_edit(self, segment, expected_workspace_binding):
        self.calls += 1
        self.last_segment = segment
        self.last_binding = expected_workspace_binding
        return self.result


class _WorkspaceEditPort:
    def __init__(self, service, *, target: str, confirmed: bool) -> None:
        self.service = service
        self.target = target
        self.confirmed = confirmed
        self.calls = 0

    def apply_segment_edit(self, segment, expected_workspace_binding):
        self.calls += 1
        return self.service.update_segment_edit(
            segment.identity,
            target=self.target,
            confirmed=self.confirmed,
            session_id=expected_workspace_binding.workspace_session_id,
            base_revision=expected_workspace_binding.workspace_revision,
        )


class _ScriptedActorPort:
    def __init__(self, actors: tuple[AssigneeRef, ...]) -> None:
        self._actors = list(actors)

    def revalidate_actor(self, handle):
        if type(handle) is not AuthenticatedActorHandle or not self._actors:
            raise ChunkError("CHUNK.ACTOR_UNVERIFIED")
        return self._actors.pop(0)


class _LateRaisingMutationPort:
    def __init__(self) -> None:
        self.calls = 0

    def apply_segment_edit(self, segment, expected_workspace_binding):
        self.calls += 1
        raise ChunkError("CHUNK.NOT_ASSIGNEE")


class CollaborativeChunkCluster2AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_id = issue_project_id(b"P" * 32)
        self.document_id = "doc-" + b"D".hex() * 32
        self.member = chunk_segment_ref_from_ids(
            self.project_id,
            self.document_id,
            "001",
        )
        self.member_2 = chunk_segment_ref_from_ids(
            self.project_id,
            self.document_id,
            "002",
        )
        self.member_3 = chunk_segment_ref_from_ids(
            self.project_id,
            self.document_id,
            "003",
        )
        self.detached = chunk_segment_ref_from_ids(
            self.project_id,
            self.document_id,
            "detached",
        )
        self.entries = (
            ChunkUniverseEntry(self.member, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.member_2, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.member_3, SourcePresence.ATTACHED),
            ChunkUniverseEntry(self.detached, SourcePresence.DETACHED),
        )
        self.workspace_binding = ChunkWorkspaceBinding(
            project_id=self.project_id,
            workspace_session_id="session-c2",
            workspace_revision=4,
            segment_universe_digest=segment_universe_digest_v1(
                self.project_id,
                self.entries,
            ),
        )
        self.manager = LocalReferenceManagerHandle("local-manager", "owner")
        self.authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.workspace_binding,
        )
        self.topology = self.authority.create_topology_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.workspace_binding,
                self.entries,
            ),
            chunk_id_issuer=_Issuer("chunk", 1),
            plan_id_issuer=_Issuer("plan", 2),
            operation_id_issuer=_Issuer("operation", 3),
        )
        self.assignment = self.authority.create_assignment_service(
            operation_id_issuer=_Issuer("operation", 100),
        )
        self.permission = self.authority.create_permission_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.workspace_binding,
                self.entries,
            )
        )
        self.chunk_id = self._create_chunk()

    def _error(self, code: str, callback) -> None:
        with self.assertRaises(ChunkError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(str(captured.exception), code)

    def _capability(self, action: TopologyAction):
        expected = chunk_plan_binding(self.authority.current_snapshot())
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            action=action,
        )
        return capability, expected

    def _create_chunk(self) -> str:
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=None,
            name="第一分工",
            members=canonicalize_chunk_members((self.member,)),
        )
        self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=None,
        )
        return preview.created_chunk_ids[0]

    def _create_additional_chunk(self, name: str, member) -> str:
        expected = chunk_plan_binding(self.authority.current_snapshot())
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            action=TopologyAction.CREATE,
        )
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            name=name,
            members=(member,),
        )
        self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )
        return preview.created_chunk_ids[0]

    def _apply_assignment(self, action: TopologyAction, port=None):
        capability, expected = self._capability(action)
        if action is TopologyAction.ASSIGN:
            preview = self.assignment.preview_assign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
                target_actor_port=port,
                target_actor_handle=port.current_actor(),
            )
        elif action is TopologyAction.REASSIGN:
            preview = self.assignment.preview_reassign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
                target_actor_port=port,
                target_actor_handle=port.current_actor(),
            )
        else:
            preview = self.assignment.preview_unassign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
            )
        receipt = self.assignment.apply_assignment(
            preview,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )
        return preview, receipt

    def test_local_reference_actor_handle_is_opaque_and_honestly_labeled(self) -> None:
        port = LocalReferenceActorPort("local-actor", "alice")
        handle = port.current_actor()
        self.assertEqual(port.identity_kind, "local_reference")
        self.assertFalse(port.is_account_authenticated)
        self.assertEqual(port.revalidate_actor(handle), AssigneeRef("local-actor", "alice"))
        self.assertEqual(repr(handle), "<AuthenticatedActorHandle opaque>")
        self._error("CHUNK.ACTOR_UNVERIFIED", lambda: AuthenticatedActorHandle())
        with self.assertRaises(TypeError):
            copy.copy(handle)
        with self.assertRaises(TypeError):
            copy.deepcopy(handle)
        with self.assertRaises(TypeError):
            pickle.dumps(handle)
        foreign = LocalReferenceActorPort("local-actor", "bob")
        self._error(
            "CHUNK.ACTOR_UNVERIFIED",
            lambda: port.revalidate_actor(foreign.current_actor()),
        )

    def test_assign_reassign_unassign_are_exact_revisioned_transactions(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        before = self.authority.current_snapshot()

        assign_preview, assign_receipt = self._apply_assignment(
            TopologyAction.ASSIGN,
            alice,
        )
        assigned = self.authority.current_snapshot()
        self.assertEqual(assigned.revision, before.revision + 1)
        self.assertEqual(assigned.chunks[0].assignee, AssigneeRef("local-actor", "alice"))
        self.assertEqual(assign_preview.assignment_count, 1)
        self.assertEqual(assign_receipt.assignment_count, 1)
        validate_chunk_operation_receipt(assign_receipt)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: validate_c1_operation_receipt(assign_receipt),
        )
        self.assertEqual(
            tuple(field.name for field in fields(before.chunks[0])),
            tuple(field.name for field in fields(assigned.chunks[0])),
        )
        self.assertEqual(before.chunks[0].chunk_id, assigned.chunks[0].chunk_id)
        self.assertEqual(before.chunks[0].name, assigned.chunks[0].name)
        self.assertEqual(before.chunks[0].members, assigned.chunks[0].members)

        self._apply_assignment(TopologyAction.REASSIGN, bob)
        reassigned = self.authority.current_snapshot()
        self.assertEqual(reassigned.revision, assigned.revision + 1)
        self.assertEqual(reassigned.chunks[0].assignee, AssigneeRef("local-actor", "bob"))

        self._apply_assignment(TopologyAction.UNASSIGN)
        unassigned = self.authority.current_snapshot()
        self.assertEqual(unassigned.revision, reassigned.revision + 1)
        self.assertIsNone(unassigned.chunks[0].assignee)
        validate_c1_snapshot(unassigned)

    def test_assignment_state_matrix_fails_before_publication(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        baseline = self.authority.current_snapshot()
        receipts = self.authority.operation_receipts()

        capability, expected = self._capability(TopologyAction.ASSIGN)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.assignment.preview_assign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
                target_actor_port=bob,
                target_actor_handle=bob.current_actor(),
            ),
        )
        capability, expected = self._capability(TopologyAction.REASSIGN)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: self.assignment.preview_reassign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
                target_actor_port=alice,
                target_actor_handle=alice.current_actor(),
            ),
        )
        self.assertEqual(self.authority.current_snapshot(), baseline)
        self.assertEqual(self.authority.operation_receipts(), receipts)

    def test_actor_loss_between_preview_and_apply_preserves_exact_state(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        capability, expected = self._capability(TopologyAction.ASSIGN)
        preview = self.assignment.preview_assign(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
            target_actor_port=alice,
            target_actor_handle=alice.current_actor(),
        )
        baseline = self.authority.current_snapshot()
        receipts = self.authority.operation_receipts()
        alice.set_available(False)
        self._error(
            "CHUNK.ACTOR_UNAVAILABLE",
            lambda: self.assignment.apply_assignment(
                preview,
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
            ),
        )
        self.assertEqual(self.authority.current_snapshot(), baseline)
        self.assertEqual(self.authority.operation_receipts(), receipts)

    def test_topology_rename_preserves_existing_assignment(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        capability, expected = self._capability(TopologyAction.RENAME)
        preview = self.topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
            name="已分配",
        )
        self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )
        self.assertEqual(
            self.authority.current_snapshot().chunks[0].assignee,
            AssigneeRef("local-actor", "alice"),
        )

    def test_assigned_merge_requires_mixed_decision_and_split_requires_each_child(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        other_id = self._create_additional_chunk("第二分工", self.member_2)

        capability, expected = self._capability(TopologyAction.ASSIGN)
        assign_other = self.assignment.preview_assign(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=other_id,
            target_actor_port=bob,
            target_actor_handle=bob.current_actor(),
        )
        self.assignment.apply_assignment(
            assign_other,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )

        capability, expected = self._capability(TopologyAction.MERGE)
        self._error(
            "CHUNK.MERGE_DECISION_REQUIRED",
            lambda: self.topology.preview_merge(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_ids=(self.chunk_id, other_id),
                result_name="合并",
            ),
        )
        merge = self.topology.preview_merge(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_ids=(self.chunk_id, other_id),
            result_name="合并",
            result_assignee=AssigneeRef("local-actor", "alice"),
            result_assignment_decided=True,
        )
        self.topology.apply_topology(
            merge,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )
        merged_id = merge.created_chunk_ids[0]
        merged = self.authority.current_snapshot().chunks[0]
        self.assertEqual(merged.assignee, AssigneeRef("local-actor", "alice"))
        self.assertEqual(merge.assignment_count, 1)

        capability, expected = self._capability(TopologyAction.SPLIT)
        self._error(
            "CHUNK.SPLIT_INVALID",
            lambda: self.topology.preview_split(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                source_chunk_id=merged_id,
                children=(
                    ChunkSplitChild("甲", (self.member,)),
                    ChunkSplitChild("乙", (self.member_2,)),
                ),
            ),
        )
        split = self.topology.preview_split(
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            source_chunk_id=merged_id,
            children=(
                ChunkSplitChild(
                    "甲",
                    (self.member,),
                    AssigneeRef("local-actor", "alice"),
                    True,
                ),
                ChunkSplitChild("乙", (self.member_2,), None, True),
            ),
        )
        self.topology.apply_topology(
            split,
            capability,
            self.manager,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
        )
        assignees = tuple(
            chunk.assignee for chunk in self.authority.current_snapshot().chunks
        )
        self.assertEqual(
            assignees,
            (AssigneeRef("local-actor", "alice"), None),
        )
        self.assertEqual(split.assignment_count, 1)

    def test_assignment_cold_reopens_with_canonical_opaque_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = CollaborativeChunkStore(
                root,
                "chunks.json",
                project_id=self.project_id,
            )
            authority = ChunkTopologyPublicationAuthority(
                project_id=self.project_id,
                workspace_binding_provider=lambda: self.workspace_binding,
                metadata_store=store,
            )
            topology = authority.create_topology_service(
                workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                    self.workspace_binding,
                    self.entries,
                ),
                chunk_id_issuer=_Issuer("chunk", 400),
                plan_id_issuer=_Issuer("plan", 401),
                operation_id_issuer=_Issuer("operation", 402),
            )
            capability = authority.issue_manager_capability(
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=None,
                action=TopologyAction.CREATE,
            )
            preview = topology.preview_create(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=None,
                name="持久分工",
                members=(self.member,),
            )
            topology.apply_topology(
                preview,
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=None,
            )
            assignment = authority.create_assignment_service(
                operation_id_issuer=_Issuer("operation", 500),
            )
            expected = chunk_plan_binding(authority.current_snapshot())
            capability = authority.issue_manager_capability(
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                action=TopologyAction.ASSIGN,
            )
            alice = LocalReferenceActorPort("local-actor", "alice")
            assign_preview = assignment.preview_assign(
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=preview.created_chunk_ids[0],
                target_actor_port=alice,
                target_actor_handle=alice.current_actor(),
            )
            assignment.apply_assignment(
                assign_preview,
                capability,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
            )

            payload = (root / "chunks.json").read_bytes()
            self.assertIn(b'"assignee":{"authority_id":"local-actor","subject_id":"alice"}', payload)
            for forbidden in (b"password", b"token", b"credential", b"display_label"):
                self.assertNotIn(forbidden, payload)
            reopened = ChunkTopologyPublicationAuthority(
                project_id=self.project_id,
                workspace_binding_provider=lambda: self.workspace_binding,
                metadata_store=CollaborativeChunkStore(
                    root,
                    "chunks.json",
                    project_id=self.project_id,
                ),
            )
            snapshot = validate_chunk_plan_snapshot(reopened.current_snapshot())
            self.assertEqual(snapshot.chunks[0].assignee, AssigneeRef("local-actor", "alice"))
            self.assertEqual(reopened.operation_receipts()[-1].action, TopologyAction.ASSIGN)

    def test_permission_reason_priority_and_exact_current_chunk(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        expected = chunk_plan_binding(self.authority.current_snapshot())

        self._error(
            "CHUNK.ACTOR_UNAVAILABLE",
            lambda: self.permission.decide_access(
                self.manager,
                self.manager,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )

        no_current = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.assertIs(no_current.access, ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: replace(no_current, current_chunk_id=self.chunk_id),
        )
        no_current_unallocated = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member_3,
        )
        self.assertIs(
            no_current_unallocated.access,
            ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK,
        )

        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        editable = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.assertIs(editable.access, ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT)
        self.assertTrue(editable.may_edit_target)
        self.assertTrue(editable.may_change_confirmed)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: replace(editable, may_edit_target=False),
        )

        unallocated = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member_3,
        )
        self.assertIs(unallocated.access, ChunkAccessKind.READ_ONLY_UNALLOCATED)

        self._create_additional_chunk("其他", self.member_2)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        outside = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member_2,
        )
        self.assertIs(outside.access, ChunkAccessKind.READ_ONLY_OUTSIDE_CURRENT)

        self.permission.select_current_chunk(
            bob,
            bob.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        not_assignee = self.permission.decide_access(
            bob,
            bob.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.assertIs(not_assignee.access, ChunkAccessKind.READ_ONLY_NOT_ASSIGNEE)

    def test_no_plan_and_detached_are_body_safe_read_only_decisions(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        empty_authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.workspace_binding,
        )
        empty_permission = empty_authority.create_permission_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.workspace_binding,
                self.entries,
            )
        )
        no_plan = empty_permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=None,
            segment=self.member,
        )
        self.assertIs(no_plan.access, ChunkAccessKind.READ_ONLY_NO_PLAN)
        self._error(
            "CHUNK.CONTRACT_INVALID",
            lambda: replace(no_plan, current_chunk_id=self.chunk_id),
        )

        self._apply_assignment(TopologyAction.ASSIGN, alice)
        assigned = self.authority.current_snapshot()
        widened_chunk = replace(
            assigned.chunks[0],
            members=canonicalize_chunk_members((self.member, self.detached)),
        )
        widened = validate_chunk_plan_snapshot(
            replace(assigned, chunks=(widened_chunk,))
        )
        detached_authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.workspace_binding,
            initial_snapshot=widened,
        )
        detached_permission = detached_authority.create_permission_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.workspace_binding,
                self.entries,
            )
        )
        expected = chunk_plan_binding(widened)
        detached_permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=widened_chunk.chunk_id,
        )
        decision = detached_permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.detached,
        )
        self.assertIs(decision.access, ChunkAccessKind.READ_ONLY_DETACHED)
        self.assertFalse(decision.may_edit_target)
        self.assertFalse(decision.may_change_confirmed)
        self.assertTrue(
            {
                "source",
                "target",
                "speaker",
                "path",
                "credential",
            }.isdisjoint(field.name for field in fields(decision))
        )

    def test_edit_capability_is_single_use_and_stales_on_selection_or_assignment(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        capability = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.assertEqual(repr(capability), "<ChunkActorCapability opaque>")
        self._error("CHUNK.PERMISSION_STALE", lambda: ChunkActorCapability())
        with self.assertRaises(TypeError):
            copy.copy(capability)
        with self.assertRaises(TypeError):
            pickle.dumps(capability)
        port = _RecordingMutationPort("edited")
        self.assertEqual(
            self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
                operation=ChunkEditOperation.SEGMENT_EDIT,
            ),
            "edited",
        )
        self.assertEqual(port.calls, 1)

        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 1)

        stale_by_selection = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.permission.clear_current_chunk(alice, alice.current_actor())
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                stale_by_selection,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 1)

        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        stale_by_assignment = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self._apply_assignment(TopologyAction.REASSIGN, bob)
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                stale_by_assignment,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 1)

    def test_actor_revalidation_cannot_splice_two_principals(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        bob = LocalReferenceActorPort("local-actor", "bob")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        for actor in (alice, bob):
            self.permission.select_current_chunk(
                actor,
                actor.current_actor(),
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                chunk_id=self.chunk_id,
            )
        scripted = _ScriptedActorPort(
            (
                AssigneeRef("local-actor", "alice"),
                AssigneeRef("local-actor", "bob"),
                AssigneeRef("local-actor", "alice"),
            )
        )
        handle = alice.current_actor()
        capability = self.permission.issue_edit_capability(
            scripted,
            handle,
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        port = _RecordingMutationPort()
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                scripted,
                handle,
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 0)
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                scripted,
                handle,
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 0)

    def test_late_owner_failure_consumes_capability_without_replay(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        capability = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        port = _LateRaisingMutationPort()
        self._error(
            "CHUNK.COMMIT_FAILED",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 1)
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 1)

    def test_workspace_drift_blocks_mutation_port_before_call(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        old_workspace = self.workspace_binding
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=old_workspace,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        capability = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=old_workspace,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.workspace_binding = replace(
            old_workspace,
            workspace_session_id="session-c2-reopened",
            workspace_revision=old_workspace.workspace_revision + 1,
        )
        port = _RecordingMutationPort()
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=old_workspace,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 0)
        decision = self.permission.decide_access(
            alice,
            alice.current_actor(),
            workspace_binding=self.workspace_binding,
            expected_plan_binding=expected,
            segment=self.member,
        )
        self.assertIs(
            decision.access,
            ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK,
        )
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=self.workspace_binding,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 0)

    def test_workspace_session_change_permanently_invalidates_selection(self) -> None:
        alice = LocalReferenceActorPort("local-actor", "alice")
        self._apply_assignment(TopologyAction.ASSIGN, alice)
        expected = chunk_plan_binding(self.authority.current_snapshot())
        session_one = self.workspace_binding
        self.permission.select_current_chunk(
            alice,
            alice.current_actor(),
            workspace_binding=session_one,
            expected_plan_binding=expected,
            chunk_id=self.chunk_id,
        )
        capability = self.permission.issue_edit_capability(
            alice,
            alice.current_actor(),
            workspace_binding=session_one,
            expected_plan_binding=expected,
            segment=self.member,
        )
        session_two = replace(
            session_one,
            workspace_session_id="session-c2-reopened",
            workspace_revision=1,
        )
        self.workspace_binding = session_two
        self.assertIs(
            self.permission.decide_access(
                alice,
                alice.current_actor(),
                workspace_binding=session_two,
                expected_plan_binding=expected,
                segment=self.member,
            ).access,
            ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK,
        )
        self.workspace_binding = session_one
        self.assertIs(
            self.permission.decide_access(
                alice,
                alice.current_actor(),
                workspace_binding=session_one,
                expected_plan_binding=expected,
                segment=self.member,
            ).access,
            ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK,
        )
        port = _RecordingMutationPort()
        self._error(
            "CHUNK.PERMISSION_STALE",
            lambda: self.permission.execute_segment_edit(
                capability,
                alice,
                alice.current_actor(),
                port,
                workspace_binding=session_one,
                expected_plan_binding=expected,
                segment=self.member,
            ),
        )
        self.assertEqual(port.calls, 0)

    def test_real_workspace_edit_occurs_only_after_positive_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = _RealPackageHarness(Path(directory))

            def binding() -> ChunkWorkspaceBinding:
                return ChunkWorkspaceBinding(
                    project_id=harness.workspace_service.workspace.project_id,
                    workspace_session_id=harness.workspace_service.session_id,
                    workspace_revision=harness.workspace_service.revision,
                    segment_universe_digest=segment_universe_digest_v1(
                        harness.workspace.project_id,
                        harness.entries,
                    ),
                )

            authority = ChunkTopologyPublicationAuthority(
                project_id=harness.workspace.project_id,
                workspace_binding_provider=binding,
            )
            topology = authority.create_topology_service(
                workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                    binding(),
                    harness.entries,
                ),
                chunk_id_issuer=_Issuer("chunk", 700),
                plan_id_issuer=_Issuer("plan", 701),
                operation_id_issuer=_Issuer("operation", 702),
            )
            assignment = authority.create_assignment_service(
                operation_id_issuer=_Issuer("operation", 800),
            )
            permission = authority.create_permission_service(
                workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                    binding(),
                    harness.entries,
                )
            )
            member = next(iter(harness.members.values()))
            create_capability = authority.issue_manager_capability(
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=None,
                action=TopologyAction.CREATE,
            )
            create_preview = topology.preview_create(
                create_capability,
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=None,
                name="真实编辑",
                members=(member,),
            )
            topology.apply_topology(
                create_preview,
                create_capability,
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=None,
            )
            alice = LocalReferenceActorPort("local-actor", "alice")
            expected = chunk_plan_binding(authority.current_snapshot())
            assign_capability = authority.issue_manager_capability(
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=expected,
                action=TopologyAction.ASSIGN,
            )
            assign_preview = assignment.preview_assign(
                assign_capability,
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=expected,
                chunk_id=create_preview.created_chunk_ids[0],
                target_actor_port=alice,
                target_actor_handle=alice.current_actor(),
            )
            assignment.apply_assignment(
                assign_preview,
                assign_capability,
                harness.manager,
                workspace_binding=binding(),
                expected_plan_binding=expected,
            )
            expected = chunk_plan_binding(authority.current_snapshot())
            permission.select_current_chunk(
                alice,
                alice.current_actor(),
                workspace_binding=binding(),
                expected_plan_binding=expected,
                chunk_id=create_preview.created_chunk_ids[0],
            )
            edit_capability = permission.issue_edit_capability(
                alice,
                alice.current_actor(),
                workspace_binding=binding(),
                expected_plan_binding=expected,
                segment=member,
            )
            package_before = harness.package_path.read_bytes()
            edit = _WorkspaceEditPort(
                harness.workspace_service,
                target="已授权真实编辑",
                confirmed=True,
            )
            receipt = permission.execute_segment_edit(
                edit_capability,
                alice,
                alice.current_actor(),
                edit,
                workspace_binding=binding(),
                expected_plan_binding=expected,
                segment=member,
            )
            self.assertTrue(receipt.changed)
            self.assertEqual(edit.calls, 1)
            edited = next(
                item
                for item in harness.workspace_service.flat_segments
                if item.identity == member.identity
            )
            self.assertEqual(edited.segment.target, "已授权真实编辑")
            self.assertTrue(edited.segment.confirmed)
            post_edit_binding = binding()
            self.assertIs(
                permission.decide_access(
                    alice,
                    alice.current_actor(),
                    workspace_binding=post_edit_binding,
                    expected_plan_binding=expected,
                    segment=member,
                ).access,
                ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT,
            )
            second_capability = permission.issue_edit_capability(
                alice,
                alice.current_actor(),
                workspace_binding=post_edit_binding,
                expected_plan_binding=expected,
                segment=member,
            )
            second_edit = _WorkspaceEditPort(
                harness.workspace_service,
                target="同一会话再编辑",
                confirmed=False,
            )
            permission.execute_segment_edit(
                second_capability,
                alice,
                alice.current_actor(),
                second_edit,
                workspace_binding=post_edit_binding,
                expected_plan_binding=expected,
                segment=member,
            )
            self.assertEqual(second_edit.calls, 1)
            self.assertEqual(harness.package_path.read_bytes(), package_before)


if __name__ == "__main__":
    unittest.main()
