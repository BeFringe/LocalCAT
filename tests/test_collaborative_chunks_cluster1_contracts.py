from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import copy
import pickle
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from collaborative_chunk_contracts import (
    CHUNK_METADATA_NAMESPACE,
    CHUNK_ERROR_CODES,
    CHUNK_LIMIT_PROFILE_V1,
    EMPTY_CHUNK_AUDIT_DIGEST,
    MAX_ACTIVE_CHUNKS,
    MAX_ACTIVE_MEMBERS,
    MAX_ACTOR_REF_BYTES,
    MAX_AUDIT_RECORDS,
    MAX_CHUNK_NAME_BYTES,
    MAX_CHUNK_NAME_SCALARS,
    MAX_JSON_NESTING_DEPTH,
    MAX_MEMBERS_PER_CHUNK,
    MAX_METADATA_BYTES,
    MAX_PUBLIC_AFFECTED_IDS,
    MAX_RETAINED_SAFE_ISSUES,
    AssigneeRef,
    ChunkError,
    ChunkMutationPreview,
    ChunkOperationReceipt,
    ChunkPlanBinding,
    ChunkPlanSnapshot,
    ChunkScopeProjection,
    ChunkSegmentRef,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    CollaborativeChunk,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_plan_digest_v1,
    chunk_plan_binding,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
    validate_c1_mutation_preview,
    validate_c1_operation_receipt,
    validate_c1_snapshot,
    validate_chunk_plan_snapshot,
)
from collaborative_chunks import (
    ChunkManagerCapability,
    ChunkManagerCapabilityService,
    ChunkScopeProjectionService,
    LocalReferenceManagerHandle,
)
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import issue_project_id


def _project(seed: int = 1) -> str:
    return issue_project_id(bytes([seed]) * 32)


def _document(seed: int) -> str:
    return "doc-" + bytes([seed]).hex() * 32


def _member(project_id: str, document_seed: int, local_id: str) -> ChunkSegmentRef:
    return ChunkSegmentRef(
        project_id=project_id,
        identity=SegmentIdentity(_document(document_seed), local_id),
    )


def _snapshot(
    project_id: str,
    members: tuple[ChunkSegmentRef, ...],
    *,
    revision: int = 1,
    assignee: AssigneeRef | None = None,
    chunk_seed: bytes = b"1" * 32,
) -> ChunkPlanSnapshot:
    universe = tuple(
        ChunkUniverseEntry(
            member,
            SourcePresence.DETACHED if index == len(members) - 1 else SourcePresence.ATTACHED,
        )
        for index, member in enumerate(members)
    )
    return ChunkPlanSnapshot(
        schema_version=1,
        namespace=CHUNK_METADATA_NAMESPACE,
        chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
        project_id=project_id,
        revision=revision,
        segment_universe_digest=segment_universe_digest_v1(project_id, universe),
        chunks=(
            CollaborativeChunk(
                issue_chunk_id(chunk_seed),
                "一",
                0,
                canonicalize_chunk_members(members),
                assignee,
            ),
        ),
        audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
    )


def _operation_common(
    project_id: str,
    *,
    assignment_count: int,
) -> dict[str, object]:
    chunk_id = issue_chunk_id(b"1" * 32)
    return {
        "operation_id": issue_chunk_operation_id(b"o" * 32),
        "action": TopologyAction.CREATE,
        "project_id": project_id,
        "chunk_plan_id": issue_chunk_plan_id(b"p" * 32),
        "base_revision": 0,
        "published_revision": 1,
        "before_plan_digest": EMPTY_CHUNK_AUDIT_DIGEST,
        "after_plan_digest": "2" * 64,
        "affected_chunk_ids": (chunk_id,),
        "created_chunk_ids": (chunk_id,),
        "retired_chunk_ids": (),
        "affected_chunk_count": 1,
        "created_chunk_count": 1,
        "retired_chunk_count": 0,
        "affected_member_count": 1,
        "assignment_count": assignment_count,
    }


class CollaborativeChunkContractTests(unittest.TestCase):
    def test_limit_profile_and_error_vocabulary_match_approved_design(self) -> None:
        self.assertEqual(
            (
                MAX_ACTIVE_CHUNKS,
                MAX_ACTIVE_MEMBERS,
                MAX_MEMBERS_PER_CHUNK,
                MAX_CHUNK_NAME_SCALARS,
                MAX_CHUNK_NAME_BYTES,
                MAX_ACTOR_REF_BYTES,
                MAX_METADATA_BYTES,
                MAX_JSON_NESTING_DEPTH,
                MAX_RETAINED_SAFE_ISSUES,
                MAX_AUDIT_RECORDS,
                MAX_PUBLIC_AFFECTED_IDS,
            ),
            (4_096, 100_000, 100_000, 256, 1_024, 256, 32 * 1024 * 1024, 32, 256, 100_000, 100_000),
        )
        self.assertEqual(CHUNK_LIMIT_PROFILE_V1.max_active_chunks, 4_096)
        self.assertEqual(CHUNK_LIMIT_PROFILE_V1.max_active_members, 100_000)
        self.assertEqual(
            CHUNK_ERROR_CODES,
            frozenset(
                {
                    "CHUNK.CONTRACT_INVALID",
                    "CHUNK.IDENTITY_DUPLICATE",
                    "CHUNK.IDENTITY_FOREIGN",
                    "CHUNK.LIMIT_EXCEEDED",
                    "CHUNK.MEMBER_UNKNOWN",
                    "CHUNK.MEMBER_DUPLICATE",
                    "CHUNK.MEMBER_OVERLAP",
                    "CHUNK.MEMBER_UNALLOCATED_REQUIRED",
                    "CHUNK.SPLIT_INVALID",
                    "CHUNK.MERGE_DECISION_REQUIRED",
                    "CHUNK.REBASE_REQUIRED",
                    "CHUNK.REBASE_DECISION_REQUIRED",
                    "CHUNK.ASSIGNMENT_UNAVAILABLE",
                    "CHUNK.ACTOR_UNAVAILABLE",
                    "CHUNK.ACTOR_UNVERIFIED",
                    "CHUNK.MANAGER_REQUIRED",
                    "CHUNK.NOT_ASSIGNEE",
                    "CHUNK.OUTSIDE_CURRENT",
                    "CHUNK.UNALLOCATED_READ_ONLY",
                    "CHUNK.DETACHED_READ_ONLY",
                    "CHUNK.PERMISSION_STALE",
                    "CHUNK.PREVIEW_STALE",
                    "CHUNK.REVISION_STALE",
                    "CHUNK.DIVERGED",
                    "CHUNK.UNIVERSE_MISMATCH",
                    "CHUNK.UNDO_NOT_HEAD",
                    "CHUNK.UNDO_UNAVAILABLE",
                    "CHUNK.METADATA_UNSUPPORTED",
                    "CHUNK.METADATA_INVALID",
                    "CHUNK.METADATA_UNAVAILABLE",
                    "CHUNK.METADATA_BINDING_STALE",
                    "CHUNK.CONFLICT_STALE",
                    "CHUNK.CONFLICT_RESOLUTION_INVALID",
                    "CHUNK.CONFLICT_RESOLUTION_REQUIRED",
                    "CHUNK.CONFLICT_REPLACE_UNAVAILABLE",
                    "CHUNK.DIGEST_MISMATCH",
                    "CHUNK.STAGE_FAILED",
                    "CHUNK.DESTINATION_STALE",
                    "CHUNK.COMMIT_FAILED",
                    "CHUNK.RECOVERY_REQUIRED",
                }
            ),
        )

    def test_domain_separated_ids_are_stable_ascii_tokens(self) -> None:
        seed = b"a" * 32
        self.assertEqual(issue_chunk_plan_id(seed), issue_chunk_plan_id(seed))
        self.assertEqual(issue_chunk_id(seed), issue_chunk_id(seed))
        self.assertRegex(issue_chunk_plan_id(seed), r"^cpl-[0-9a-f]{64}$")
        self.assertRegex(issue_chunk_id(seed), r"^chk-[0-9a-f]{64}$")
        self.assertRegex(issue_chunk_operation_id(seed), r"^cop-[0-9a-f]{64}$")
        self.assertNotEqual(issue_chunk_plan_id(seed)[4:], issue_chunk_id(seed)[4:])
        self.assertNotEqual(issue_chunk_id(seed)[4:], issue_chunk_operation_id(seed)[4:])

    def test_universe_digest_uses_composite_identity_and_presence_not_input_order(self) -> None:
        project_id = _project()
        first = _member(project_id, 1, "same")
        second = _member(project_id, 2, "same")
        attached = ChunkUniverseEntry(first, SourcePresence.ATTACHED)
        detached = ChunkUniverseEntry(second, SourcePresence.DETACHED)

        digest = segment_universe_digest_v1(project_id, (attached, detached))
        self.assertEqual(
            digest,
            segment_universe_digest_v1(project_id, (detached, attached)),
        )
        self.assertNotEqual(
            digest,
            segment_universe_digest_v1(
                project_id,
                (
                    attached,
                    ChunkUniverseEntry(second, SourcePresence.ATTACHED),
                ),
            ),
        )

    def test_member_canonical_order_is_document_then_length_prefixed_local_id(self) -> None:
        project_id = _project()
        members = (
            _member(project_id, 2, "z"),
            _member(project_id, 1, "aa"),
            _member(project_id, 1, "b"),
        )
        ordered = canonicalize_chunk_members(members)
        self.assertEqual(
            tuple((item.identity.document_id, item.identity.local_segment_id) for item in ordered),
            (
                (_document(1), "b"),
                (_document(1), "aa"),
                (_document(2), "z"),
            ),
        )

    def test_c1_snapshot_rejects_overlap_and_non_null_assignee(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        universe_digest = segment_universe_digest_v1(
            project_id,
            (ChunkUniverseEntry(member, SourcePresence.ATTACHED),),
        )
        overlap = ChunkPlanSnapshot(
            schema_version=1,
            namespace=CHUNK_METADATA_NAMESPACE,
            chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
            project_id=project_id,
            revision=1,
            segment_universe_digest=universe_digest,
            chunks=(
                CollaborativeChunk(issue_chunk_id(b"1" * 32), "一", 0, (member,), None),
                CollaborativeChunk(issue_chunk_id(b"2" * 32), "二", 1, (member,), None),
            ),
            audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.MEMBER_OVERLAP"):
            validate_c1_snapshot(overlap)

        assigned = ChunkPlanSnapshot(
            schema_version=1,
            namespace=CHUNK_METADATA_NAMESPACE,
            chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
            project_id=project_id,
            revision=1,
            segment_universe_digest=universe_digest,
            chunks=(
                CollaborativeChunk(
                    issue_chunk_id(b"1" * 32),
                    "一",
                    0,
                    (member,),
                    AssigneeRef("local", "person"),
                ),
            ),
            audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            validate_c1_snapshot(assigned)

        self.assertIs(validate_chunk_plan_snapshot(assigned), assigned)
        self.assertEqual(chunk_plan_binding(assigned).plan_revision, 1)
        self.assertEqual(len(chunk_plan_digest_v1(assigned)), 64)

    def test_generic_snapshot_and_digest_reject_invalid_semantics(self) -> None:
        project_id = _project()
        first = _member(project_id, 1, "s1")
        second = _member(project_id, 1, "s2")
        valid = _snapshot(project_id, (first, second))

        noncanonical = replace(
            valid,
            chunks=(replace(valid.chunks[0], members=(second, first)),),
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            chunk_plan_digest_v1(noncanonical)

        overlap = replace(
            valid,
            chunks=(
                valid.chunks[0],
                CollaborativeChunk(
                    issue_chunk_id(b"2" * 32),
                    "二",
                    1,
                    (first,),
                    None,
                ),
            ),
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.MEMBER_OVERLAP"):
            chunk_plan_digest_v1(overlap)

        foreign = _member(_project(2), 1, "foreign")
        foreign_snapshot = replace(
            valid,
            chunks=(replace(valid.chunks[0], members=(foreign,)),),
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.IDENTITY_FOREIGN"):
            chunk_plan_digest_v1(foreign_snapshot)

    def test_nested_tamper_cannot_receive_plan_digest_or_binding(self) -> None:
        project_id = _project()
        snapshot = _snapshot(project_id, (_member(project_id, 1, "s1"),))
        object.__setattr__(snapshot.chunks[0], "chunk_id", "forged")
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            chunk_plan_digest_v1(snapshot)
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            chunk_plan_binding(snapshot)

        snapshot = _snapshot(project_id, (_member(project_id, 1, "s1"),))
        object.__setattr__(
            snapshot.chunks[0].members[0].identity,
            "local_segment_id",
            "bad\0id",
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID") as caught:
            chunk_plan_digest_v1(snapshot)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("PROJECT.", str(caught.exception))

        for document_id, local_id in (("文档", "s1"), (_document(1), "\ud800")):
            hostile = _member(project_id, 1, "s1")
            object.__setattr__(hostile.identity, "document_id", document_id)
            object.__setattr__(hostile.identity, "local_segment_id", local_id)
            with self.subTest(document_id=document_id, local_id=local_id):
                with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
                    canonicalize_chunk_members((hostile,))
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            canonicalize_chunk_members((), allow_empty=1)  # type: ignore[arg-type]

    def test_name_actor_and_collection_limits_fail_before_publication(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        chunk_id = issue_chunk_id(b"1" * 32)
        CollaborativeChunk(chunk_id, "a" * 256, 0, (member,), None)
        CollaborativeChunk(chunk_id, "四" * 256, 0, (member,), None)
        AssigneeRef("a" * 256, "b" * 256)

        for invalid_name in ("a" * 257, "四" * 257, "\0", "\ud800", "   "):
            with self.assertRaises(ChunkError):
                CollaborativeChunk(chunk_id, invalid_name, 0, (member,), None)
        with self.assertRaisesRegex(ChunkError, "CHUNK.LIMIT_EXCEEDED"):
            AssigneeRef("a" * 257, "subject")

        with patch("collaborative_chunk_contracts.MAX_MEMBERS_PER_CHUNK", 1):
            with self.assertRaisesRegex(ChunkError, "CHUNK.LIMIT_EXCEEDED"):
                canonicalize_chunk_members((member, _member(project_id, 2, "s2")))
        one = _snapshot(project_id, (member,))
        with patch("collaborative_chunk_contracts.MAX_ACTIVE_CHUNKS", 0):
            with self.assertRaisesRegex(ChunkError, "CHUNK.LIMIT_EXCEEDED"):
                validate_chunk_plan_snapshot(one)

    def test_digest_golden_and_audit_head_is_not_semantic(self) -> None:
        project_id = _project()
        snapshot = _snapshot(
            project_id,
            (_member(project_id, 1, "a"), _member(project_id, 2, "bb")),
        )
        self.assertEqual(
            snapshot.segment_universe_digest,
            "9cb851050b2d0001c084310c8cc2816ae497237512864231516032674cccc93d",
        )
        self.assertEqual(
            chunk_plan_digest_v1(snapshot),
            "2ff84ced353be502840b871afbca32b03c9db822dfa44fe93ad2c83ccbfdf264",
        )
        self.assertEqual(
            chunk_plan_digest_v1(replace(snapshot, audit_head_digest="a" * 64)),
            chunk_plan_digest_v1(snapshot),
        )

    def test_operation_projection_rejects_ambiguity_and_validates_truncation(self) -> None:
        project_id = _project()
        common = _operation_common(project_id, assignment_count=0)
        chunk_id = common["affected_chunk_ids"][0]  # type: ignore[index]
        for changes in (
            {"before_plan_digest": "2" * 64},
            {"created_chunk_ids": (chunk_id,), "retired_chunk_ids": (chunk_id,)},
            {"base_revision": True},
            {"affected_member_count": False},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ChunkError):
                    ChunkMutationPreview(
                        **(common | changes),
                        warnings=(),
                        blockers=(),
                        truncated=False,
                    )
        truncated = common | {
            "affected_chunk_count": MAX_PUBLIC_AFFECTED_IDS + 1,
            "created_chunk_count": MAX_PUBLIC_AFFECTED_IDS + 1,
        }
        ChunkMutationPreview(
            **truncated,
            warnings=("CHUNK.LIMIT_EXCEEDED",),
            blockers=(),
            truncated=True,
        )
        ChunkOperationReceipt(
            **truncated,
            actor_ref=AssigneeRef("local", "manager"),
            safe_issues=("CHUNK.LIMIT_EXCEEDED",),
            truncated=True,
            audit_record_digest="3" * 64,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            ChunkMutationPreview(
                **truncated,
                warnings=(),
                blockers=(),
                truncated=True,
            )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            ChunkMutationPreview(
                **truncated,
                warnings=("CHUNK.LIMIT_EXCEEDED",),
                blockers=(),
                truncated=False,
            )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            ChunkMutationPreview(
                **(truncated | {"retired_chunk_count": MAX_PUBLIC_AFFECTED_IDS + 1}),
                warnings=("CHUNK.LIMIT_EXCEEDED",),
                blockers=(),
                truncated=True,
            )

    def test_plan_digest_binding_and_scope_are_body_free_frozen_contracts(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        universe_digest = segment_universe_digest_v1(
            project_id,
            (ChunkUniverseEntry(member, SourcePresence.ATTACHED),),
        )
        snapshot = ChunkPlanSnapshot(
            schema_version=1,
            namespace=CHUNK_METADATA_NAMESPACE,
            chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
            project_id=project_id,
            revision=1,
            segment_universe_digest=universe_digest,
            chunks=(
                CollaborativeChunk(issue_chunk_id(b"1" * 32), "一", 0, (member,), None),
            ),
            audit_head_digest=EMPTY_CHUNK_AUDIT_DIGEST,
        )
        validate_c1_snapshot(snapshot)
        digest = chunk_plan_digest_v1(snapshot)
        binding = ChunkPlanBinding(
            project_id=project_id,
            chunk_plan_id=snapshot.chunk_plan_id,
            plan_revision=1,
            plan_digest=digest,
            segment_universe_digest=universe_digest,
        )
        projection = ChunkScopeProjection(
            **{field.name: getattr(binding, field.name) for field in fields(binding)},
            chunk_id=snapshot.chunks[0].chunk_id,
            members=(member,),
        )
        self.assertEqual(len(digest), 64)
        self.assertEqual(
            {field.name for field in fields(projection)},
            {
                "project_id",
                "chunk_plan_id",
                "plan_revision",
                "plan_digest",
                "segment_universe_digest",
                "chunk_id",
                "members",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            projection.chunk_id = issue_chunk_id(b"2" * 32)  # type: ignore[misc]

    def test_generic_preview_receipt_allow_future_assignment_but_c1_gate_rejects(self) -> None:
        project_id = _project()
        common = _operation_common(project_id, assignment_count=1)
        actor_ref = AssigneeRef("local", "manager")
        preview = ChunkMutationPreview(
            **common,
            warnings=(),
            blockers=(),
            truncated=False,
        )
        receipt = ChunkOperationReceipt(
            **common,
            actor_ref=actor_ref,
            safe_issues=(),
            truncated=False,
            audit_record_digest="3" * 64,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            validate_c1_mutation_preview(preview)
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            validate_c1_operation_receipt(receipt)

        zero_common = _operation_common(project_id, assignment_count=0)
        validate_c1_mutation_preview(
            ChunkMutationPreview(
                **zero_common,
                warnings=(),
                blockers=(),
                truncated=False,
            )
        )
        validate_c1_operation_receipt(
            ChunkOperationReceipt(
                **zero_common,
                actor_ref=actor_ref,
                safe_issues=(),
                truncated=False,
                audit_record_digest="3" * 64,
            )
        )

    def test_scope_service_issues_full_exact_membership_and_revalidates_drift(self) -> None:
        project_id = _project()
        attached = _member(project_id, 1, "attached")
        detached = _member(project_id, 2, "detached")
        current = [_snapshot(project_id, (attached, detached))]
        service = ChunkScopeProjectionService(
            lambda: current[0],
            retired_chunk_ids_provider=lambda: (),
        )
        binding = chunk_plan_binding(current[0])

        projection = service.issue_scope_projection(
            current[0].chunks[0].chunk_id,
            binding,
        )
        self.assertEqual(projection.members, current[0].chunks[0].members)
        self.assertIsNot(projection.members, current[0].chunks[0].members)
        self.assertIsNot(projection.members[0], current[0].chunks[0].members[0])
        self.assertIsNot(
            projection.members[0].identity,
            current[0].chunks[0].members[0].identity,
        )
        self.assertEqual(
            {field.name for field in fields(projection)},
            {
                "project_id",
                "chunk_plan_id",
                "plan_revision",
                "plan_digest",
                "segment_universe_digest",
                "chunk_id",
                "members",
            },
        )
        self.assertEqual(service.revalidate_scope_projection(projection), projection)

        owner_digest = chunk_plan_digest_v1(current[0])
        object.__setattr__(
            projection.members[0].identity,
            "local_segment_id",
            "projection-only-tamper",
        )
        self.assertEqual(chunk_plan_digest_v1(current[0]), owner_digest)
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.revalidate_scope_projection(projection)
        projection = service.issue_scope_projection(
            current[0].chunks[0].chunk_id,
            binding,
        )

        forged = replace(projection, members=(attached,))
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.revalidate_scope_projection(forged)

        current[0] = _snapshot(
            project_id,
            (attached,),
            revision=2,
            chunk_seed=b"2" * 32,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.revalidate_scope_projection(projection)
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.issue_scope_projection(projection.chunk_id, binding)

    def test_scope_service_rejects_retired_reactivation_and_provider_failure(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        retired: list[tuple[str, ...]] = [()]
        service = ChunkScopeProjectionService(
            lambda: snapshot,
            retired_chunk_ids_provider=lambda: retired[0],
        )
        binding = chunk_plan_binding(snapshot)
        projection = service.issue_scope_projection(snapshot.chunks[0].chunk_id, binding)
        retired[0] = (snapshot.chunks[0].chunk_id,)
        with self.assertRaisesRegex(ChunkError, "CHUNK.METADATA_INVALID"):
            service.issue_scope_projection(snapshot.chunks[0].chunk_id, binding)
        with self.assertRaisesRegex(ChunkError, "CHUNK.METADATA_INVALID"):
            service.revalidate_scope_projection(projection)

        def broken() -> ChunkPlanSnapshot:
            raise OSError("/secret/path and source body")

        broken_service = ChunkScopeProjectionService(
            broken,
            retired_chunk_ids_provider=lambda: (),
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.RECOVERY_REQUIRED") as caught:
            broken_service.issue_scope_projection(snapshot.chunks[0].chunk_id, binding)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_scope_service_rejects_unknown_foreign_and_non_owner_projection(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        service = ChunkScopeProjectionService(
            lambda: snapshot,
            retired_chunk_ids_provider=lambda: (),
        )
        binding = chunk_plan_binding(snapshot)

        with self.assertRaisesRegex(ChunkError, "CHUNK.IDENTITY_FOREIGN"):
            service.issue_scope_projection(issue_chunk_id(b"x" * 32), binding)

        foreign_binding = replace(binding, project_id=_project(2))
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.issue_scope_projection(snapshot.chunks[0].chunk_id, foreign_binding)

        arbitrary = ChunkScopeProjection(
            project_id=binding.project_id,
            chunk_plan_id=binding.chunk_plan_id,
            plan_revision=binding.plan_revision,
            plan_digest=binding.plan_digest,
            segment_universe_digest=binding.segment_universe_digest,
            chunk_id=snapshot.chunks[0].chunk_id,
            members=(member,),
        )
        other_service = ChunkScopeProjectionService(
            lambda: snapshot,
            retired_chunk_ids_provider=lambda: (),
        )
        self.assertEqual(
            other_service.revalidate_scope_projection(arbitrary),
            arbitrary,
        )

    def test_manager_capability_is_opaque_exact_single_use_and_topology_only(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        current = [_snapshot(project_id, (member,))]
        service = ChunkManagerCapabilityService(
            lambda: current[0],
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        binding = chunk_plan_binding(current[0])
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=binding.segment_universe_digest,
        )
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=binding,
            action=TopologyAction.SPLIT,
        )

        self.assertIs(type(capability), ChunkManagerCapability)
        self.assertFalse(manager.is_account_authenticated)
        self.assertFalse(hasattr(capability, "mutate_target"))
        self.assertFalse(hasattr(capability, "mutate_confirmed"))
        with self.assertRaises(TypeError):
            pickle.dumps(capability)
        with self.assertRaises(TypeError):
            copy.copy(capability)
        with self.assertRaises(TypeError):
            copy.deepcopy(capability)

        self.assertEqual(
            service.revalidate_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.SPLIT,
            ),
            manager.actor_ref,
        )

        self.assertEqual(
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.SPLIT,
            ),
            manager.actor_ref,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.SPLIT,
            )

    def test_manager_capability_consumption_is_atomic_and_actor_bound(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        binding = chunk_plan_binding(snapshot)
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=binding.segment_universe_digest,
        )
        service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=binding,
            action=TopologyAction.RENAME,
        )
        clone = LocalReferenceManagerHandle("local", "manager")
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                clone,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.RENAME,
            )

        def consume() -> str:
            try:
                service.consume_manager_capability(
                    capability,
                    manager,
                    workspace_binding=workspace_binding,
                    expected_plan_binding=binding,
                    action=TopologyAction.RENAME,
                )
            except ChunkError as error:
                return error.code
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: consume(), range(2)))
        self.assertCountEqual(results, ("ok", "CHUNK.PREVIEW_STALE"))

        with self.assertRaisesRegex(ChunkError, "CHUNK.ACTOR_UNVERIFIED"):
            LocalReferenceManagerHandle(
                "local",
                "manager",
                is_account_authenticated=True,
            )

    def test_manager_capability_revalidates_authoritative_workspace_and_failures(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        binding = chunk_plan_binding(snapshot)
        current_workspace = [
            ChunkWorkspaceBinding(
                project_id=project_id,
                workspace_session_id="session-1",
                workspace_revision=7,
                segment_universe_digest=binding.segment_universe_digest,
            )
        ]
        service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=lambda: current_workspace[0],
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=current_workspace[0],
            expected_plan_binding=binding,
            action=TopologyAction.REORDER,
        )
        old_workspace = current_workspace[0]
        current_workspace[0] = replace(old_workspace, workspace_revision=8)
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=old_workspace,
                expected_plan_binding=binding,
                action=TopologyAction.REORDER,
            )

        def broken_workspace() -> ChunkWorkspaceBinding:
            raise RuntimeError("target body at /secret/path")

        broken_service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=broken_workspace,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.RECOVERY_REQUIRED") as caught:
            broken_service.issue_manager_capability(
                manager,
                workspace_binding=old_workspace,
                expected_plan_binding=binding,
                action=TopologyAction.REORDER,
            )
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_manager_capability_privately_copies_caller_bindings(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        plan_binding = chunk_plan_binding(snapshot)
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=plan_binding.segment_universe_digest,
        )
        service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=plan_binding,
            action=TopologyAction.MOVE,
        )
        object.__setattr__(workspace_binding, "workspace_revision", 8)
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=plan_binding,
                action=TopologyAction.MOVE,
            )

        workspace_binding = replace(workspace_binding, workspace_revision=7)
        service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        plan_binding = chunk_plan_binding(snapshot)
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=plan_binding,
            action=TopologyAction.MOVE,
        )
        object.__setattr__(plan_binding, "plan_revision", 2)
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=plan_binding,
                action=TopologyAction.MOVE,
            )

    def test_manager_capability_does_not_expose_private_actor_binding(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        snapshot = _snapshot(project_id, (member,))
        plan_binding = chunk_plan_binding(snapshot)
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=plan_binding.segment_universe_digest,
        )
        service = ChunkManagerCapabilityService(
            lambda: snapshot,
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "original")
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=plan_binding,
            action=TopologyAction.RENAME,
        )
        returned = service.revalidate_manager_capability(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=plan_binding,
            action=TopologyAction.RENAME,
        )
        second = service.revalidate_manager_capability(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=plan_binding,
            action=TopologyAction.RENAME,
        )
        self.assertIsNot(returned, second)
        object.__setattr__(manager, "subject_id", "replacement")
        object.__setattr__(returned, "subject_id", "replacement")
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=plan_binding,
                action=TopologyAction.RENAME,
            )

    def test_manager_capability_models_initial_plan_creation_explicitly(self) -> None:
        project_id = _project()
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=0,
            segment_universe_digest="1" * 64,
        )
        service = ChunkManagerCapabilityService(
            lambda: None,
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        self.assertEqual(
            service.revalidate_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=None,
                action=TopologyAction.CREATE,
            ),
            manager.actor_ref,
        )
        with self.assertRaisesRegex(ChunkError, "CHUNK.REVISION_STALE"):
            service.issue_manager_capability(
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=None,
                action=TopologyAction.MERGE,
            )

        with self.assertRaisesRegex(ChunkError, "CHUNK.IDENTITY_FOREIGN"):
            service.issue_manager_capability(
                manager,
                workspace_binding=replace(workspace_binding, project_id=_project(2)),
                expected_plan_binding=None,
                action=TopologyAction.CREATE,
            )

    def test_manager_capability_rejects_forged_foreign_action_and_plan_drift(self) -> None:
        project_id = _project()
        member = _member(project_id, 1, "s1")
        current = [_snapshot(project_id, (member,))]
        service = ChunkManagerCapabilityService(
            lambda: current[0],
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        other_service = ChunkManagerCapabilityService(
            lambda: current[0],
            project_id=project_id,
            workspace_binding_provider=lambda: workspace_binding,
        )
        manager = LocalReferenceManagerHandle("local", "manager")
        binding = chunk_plan_binding(current[0])
        workspace_binding = ChunkWorkspaceBinding(
            project_id=project_id,
            workspace_session_id="session-1",
            workspace_revision=7,
            segment_universe_digest=binding.segment_universe_digest,
        )
        capability = service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=binding,
            action=TopologyAction.MERGE,
        )

        forged = object.__new__(ChunkManagerCapability)
        with self.assertRaisesRegex(ChunkError, "CHUNK.MANAGER_REQUIRED"):
            service.consume_manager_capability(
                forged,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.MERGE,
            )
        with self.assertRaisesRegex(ChunkError, "CHUNK.MANAGER_REQUIRED"):
            other_service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.MERGE,
            )
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.SPLIT,
            )

        current[0] = replace(current[0], revision=2)
        with self.assertRaisesRegex(ChunkError, "CHUNK.PREVIEW_STALE"):
            service.consume_manager_capability(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=binding,
                action=TopologyAction.MERGE,
            )

    def test_upstream_validation_errors_are_translated_to_chunk_codes(self) -> None:
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            ChunkPlanBinding(
                project_id="not-a-project-id",
                chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
                plan_revision=1,
                plan_digest="1" * 64,
                segment_universe_digest="2" * 64,
            )
        try:
            ChunkPlanBinding(
                project_id="not-a-project-id",
                chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
                plan_revision=1,
                plan_digest="1" * 64,
                segment_universe_digest="2" * 64,
            )
        except ChunkError as error:
            self.assertTrue(error.__suppress_context__)
            self.assertNotIn("PROJECT.", str(error))
        with self.assertRaisesRegex(ChunkError, "CHUNK.CONTRACT_INVALID"):
            ChunkPlanBinding(
                project_id=_project(),
                chunk_plan_id=issue_chunk_plan_id(b"p" * 32),
                plan_revision=1,
                plan_digest="not-a-digest",
                segment_universe_digest="2" * 64,
            )

    def test_body_safe_error_is_immutable(self) -> None:
        error = ChunkError("CHUNK.REVISION_STALE", retryable=True)
        self.assertEqual(str(error), "CHUNK.REVISION_STALE")
        self.assertTrue(error.retryable)
        with self.assertRaises(AttributeError):
            error.code = "CHUNK.CONTRACT_INVALID"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
