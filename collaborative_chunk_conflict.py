"""C3 semantic conflict classification and current-head undo transactions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
from threading import RLock
from typing import Callable

from collaborative_chunk_contracts import (
    AssigneeRef,
    ChunkError,
    ChunkMutationPreview,
    ChunkOperationReceipt,
    ChunkPlanBinding,
    ChunkPlanSnapshot,
    ChunkWorkspaceBinding,
    CollaborativeChunk,
    LocalReferenceManagerHandle,
    TopologyAction,
    chunk_operation_audit_digest_v1,
    chunk_plan_binding,
    chunk_plan_digest_v1,
    issue_chunk_operation_id,
    validate_chunk_operation_id,
    validate_chunk_mutation_preview,
    validate_chunk_operation_receipt,
    validate_chunk_plan_snapshot,
)
from collaborative_chunk_store import (
    ChunkMetadataState,
    build_next_chunk_metadata_state,
    decode_chunk_metadata_state,
    validate_chunk_metadata_state,
    validate_chunk_metadata_successor,
)
from collaborative_chunks import (
    ChunkManagerCapability,
    ChunkTopologyPublicationAuthority,
)


def _fail(code: str) -> None:
    raise ChunkError(code)


class ChunkConflictClassification(str, Enum):
    IDENTICAL = "identical"
    FAST_FORWARD = "fast_forward"
    STALE = "stale"
    DIVERGED = "diverged"
    FOREIGN = "foreign"
    UNIVERSE_MISMATCH = "universe_mismatch"


class ChunkConflictResolution(str, Enum):
    AUTO = "auto"
    KEEP_CURRENT = "keep_current"
    REPLACE_INCOMING = "replace_incoming"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ChunkConflictPreview:
    preview_id: str
    classification: ChunkConflictClassification
    project_id: str
    current_metadata_digest: str | None
    incoming_payload_digest: str
    current_plan_binding: ChunkPlanBinding | None
    incoming_plan_binding: ChunkPlanBinding | None
    workspace_binding: ChunkWorkspaceBinding
    requires_explicit_resolution: bool
    blockers: tuple[str, ...]
    replacement: ChunkMutationPreview | None
    required_action: TopologyAction | None


@dataclass(frozen=True, slots=True)
class ChunkUndoPreview:
    requested_operation_id: str
    mutation: ChunkMutationPreview
    workspace_binding: ChunkWorkspaceBinding


@dataclass(slots=True)
class _ConflictRecord:
    preview: ChunkConflictPreview
    current: ChunkMetadataState | None
    incoming: ChunkMetadataState
    state: str = "pending"


@dataclass(slots=True)
class _UndoRecord:
    preview: ChunkUndoPreview
    current: ChunkMetadataState
    candidate: ChunkMetadataState
    receipt: ChunkOperationReceipt
    state: str = "pending"


class ChunkMetadataConflictService:
    """Compare strict envelopes; publish only proven or explicit successors."""

    def __init__(
        self,
        authority: ChunkTopologyPublicationAuthority,
        *,
        workspace_binding_provider: Callable[[], ChunkWorkspaceBinding],
        operation_id_issuer: Callable[[], str] = issue_chunk_operation_id,
    ) -> None:
        if type(authority) is not ChunkTopologyPublicationAuthority:
            _fail("CHUNK.CONTRACT_INVALID")
        if not callable(workspace_binding_provider) or not callable(
            operation_id_issuer
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        self._authority = authority
        self._workspace_binding_provider = workspace_binding_provider
        self._operation_id_issuer = operation_id_issuer
        self._conflicts: dict[str, _ConflictRecord] = {}
        self._undos: dict[str, _UndoRecord] = {}
        self._issued_operation_ids: set[str] = set()
        self._lock = RLock()

    def _workspace_binding(self) -> ChunkWorkspaceBinding:
        try:
            binding = self._workspace_binding_provider()
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if type(binding) is not ChunkWorkspaceBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        binding.__post_init__()
        if binding.project_id != self._authority.project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        return binding

    def _operation_id(self) -> str:
        try:
            value = self._operation_id_issuer()
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        value = validate_chunk_operation_id(value)
        with self._lock:
            if value in self._issued_operation_ids or any(
                receipt.operation_id == value
                for receipt in self._authority.operation_receipts()
            ):
                _fail("CHUNK.IDENTITY_DUPLICATE")
            self._issued_operation_ids.add(value)
        return value

    @staticmethod
    def _binding(state: ChunkMetadataState | None) -> ChunkPlanBinding | None:
        if state is None or state.active_snapshot is None:
            return None
        return chunk_plan_binding(state.active_snapshot)

    @staticmethod
    def _classify(
        current: ChunkMetadataState | None,
        incoming: ChunkMetadataState,
        workspace: ChunkWorkspaceBinding,
    ) -> ChunkConflictClassification:
        incoming = validate_chunk_metadata_state(incoming)
        if incoming.project_id != workspace.project_id:
            return ChunkConflictClassification.FOREIGN
        if current is not None:
            current = validate_chunk_metadata_state(current)
            if current.project_id != incoming.project_id:
                return ChunkConflictClassification.FOREIGN
        current_active = None if current is None else current.active_snapshot
        incoming_active = incoming.active_snapshot
        current_plan_id = (
            None
            if current is None
            else current.audit_records[-1].receipt.chunk_plan_id
        )
        incoming_plan_id = incoming.audit_records[-1].receipt.chunk_plan_id
        if any(
            active is not None
            and active.segment_universe_digest
            != workspace.segment_universe_digest
            for active in (current_active, incoming_active)
        ):
            return ChunkConflictClassification.UNIVERSE_MISMATCH
        if (
            current_active is not None
            and incoming_active is not None
            and chunk_plan_digest_v1(current_active)
            == chunk_plan_digest_v1(incoming_active)
        ) or (
            current_active is None
            and incoming_active is None
            and current == incoming
        ):
            return ChunkConflictClassification.IDENTICAL
        try:
            validate_chunk_metadata_successor(current, incoming)
        except ChunkError:
            pass
        else:
            return ChunkConflictClassification.FAST_FORWARD
        if current is not None and (
            len(incoming.audit_records) < len(current.audit_records)
            and current.audit_records[: len(incoming.audit_records)]
            == incoming.audit_records
        ):
            return ChunkConflictClassification.STALE
        if (
            current_plan_id is not None
            and current_plan_id != incoming_plan_id
        ):
            return ChunkConflictClassification.FOREIGN
        return ChunkConflictClassification.DIVERGED

    def preview(self, incoming_payload: bytes) -> ChunkConflictPreview:
        if type(incoming_payload) is not bytes:
            raise TypeError("incoming Chunk metadata must be exact bytes")
        incoming = decode_chunk_metadata_state(incoming_payload)
        current = self._authority._metadata_state_for_conflict_owner()
        workspace = self._workspace_binding()
        classification = self._classify(current, incoming, workspace)
        blockers = {
            ChunkConflictClassification.STALE: ("CHUNK.CONFLICT_STALE",),
            ChunkConflictClassification.FOREIGN: ("CHUNK.IDENTITY_FOREIGN",),
            ChunkConflictClassification.UNIVERSE_MISMATCH: (
                "CHUNK.UNIVERSE_MISMATCH",
            ),
        }.get(classification, ())
        preview_id = self._operation_id()
        replacement = None
        if classification is ChunkConflictClassification.DIVERGED:
            if current is None:
                _fail("CHUNK.CONFLICT_REPLACE_UNAVAILABLE")
            if incoming.active_snapshot is not None:
                _state, replacement_receipt = self._replacement_successor(
                    current,
                    incoming,
                    AssigneeRef("localcat-conflict-preview", "unbound"),
                    preview_id,
                )
                replacement = self._mutation_from_receipt(replacement_receipt)
            else:
                blockers = (*blockers, "CHUNK.CONFLICT_REPLACE_UNAVAILABLE")
        preview = ChunkConflictPreview(
            preview_id=preview_id,
            classification=classification,
            project_id=workspace.project_id,
            current_metadata_digest=self._authority.metadata_digest(),
            incoming_payload_digest=hashlib.sha256(incoming_payload).hexdigest(),
            current_plan_binding=self._binding(current),
            incoming_plan_binding=self._binding(incoming),
            workspace_binding=workspace,
            requires_explicit_resolution=(
                classification is ChunkConflictClassification.DIVERGED
            ),
            blockers=blockers,
            replacement=replacement,
            required_action=(
                TopologyAction.CREATE
                if classification is ChunkConflictClassification.FAST_FORWARD
                and (current is None or current.active_snapshot is None)
                else (
                    TopologyAction.CONFLICT_REPLACE
                    if classification
                    in {
                        ChunkConflictClassification.FAST_FORWARD,
                        ChunkConflictClassification.DIVERGED,
                    }
                    else None
                )
            ),
        )
        with self._lock:
            if preview_id in self._conflicts:
                _fail("CHUNK.IDENTITY_DUPLICATE")
            self._conflicts[preview_id] = _ConflictRecord(
                preview,
                current,
                incoming,
            )
        return preview

    @staticmethod
    def _semantic_chunks(snapshot: ChunkPlanSnapshot) -> tuple[CollaborativeChunk, ...]:
        return tuple(
            CollaborativeChunk(
                chunk_id=chunk.chunk_id,
                name=chunk.name,
                order=chunk.order,
                members=tuple(chunk.members),
                assignee=chunk.assignee,
            )
            for chunk in snapshot.chunks
        )

    @staticmethod
    def _mutation_from_receipt(
        receipt: ChunkOperationReceipt,
    ) -> ChunkMutationPreview:
        return ChunkMutationPreview(
            operation_id=receipt.operation_id,
            action=receipt.action,
            project_id=receipt.project_id,
            chunk_plan_id=receipt.chunk_plan_id,
            base_revision=receipt.base_revision,
            published_revision=receipt.published_revision,
            before_plan_digest=receipt.before_plan_digest,
            after_plan_digest=receipt.after_plan_digest,
            affected_chunk_ids=receipt.affected_chunk_ids,
            created_chunk_ids=receipt.created_chunk_ids,
            retired_chunk_ids=receipt.retired_chunk_ids,
            affected_chunk_count=receipt.affected_chunk_count,
            created_chunk_count=receipt.created_chunk_count,
            retired_chunk_count=receipt.retired_chunk_count,
            affected_member_count=receipt.affected_member_count,
            assignment_count=receipt.assignment_count,
            warnings=receipt.safe_issues,
            blockers=(),
            truncated=receipt.truncated,
        )

    def _replacement_successor(
        self,
        current: ChunkMetadataState,
        incoming: ChunkMetadataState,
        actor_ref: AssigneeRef,
        operation_id: str,
    ) -> tuple[ChunkMetadataState, ChunkOperationReceipt]:
        before = current.active_snapshot
        source = incoming.active_snapshot
        if before is None or source is None:
            _fail("CHUNK.CONFLICT_REPLACE_UNAVAILABLE")
        incoming_ids = {chunk.chunk_id for chunk in source.chunks}
        if incoming_ids & set(current.retired_chunk_ids):
            _fail("CHUNK.IDENTITY_DUPLICATE")
        initial = validate_chunk_plan_snapshot(
            ChunkPlanSnapshot(
                schema_version=before.schema_version,
                namespace=before.namespace,
                chunk_plan_id=before.chunk_plan_id,
                project_id=before.project_id,
                revision=before.revision + 1,
                segment_universe_digest=before.segment_universe_digest,
                chunks=self._semantic_chunks(source),
                audit_head_digest=before.audit_head_digest,
            )
        )
        before_by_id = {chunk.chunk_id: chunk for chunk in before.chunks}
        after_by_id = {chunk.chunk_id: chunk for chunk in initial.chunks}
        affected = tuple(
            sorted(
                chunk_id
                for chunk_id in set(before_by_id) | set(after_by_id)
                if before_by_id.get(chunk_id) != after_by_id.get(chunk_id)
            )
        )
        created = tuple(sorted(set(after_by_id) - set(before_by_id)))
        retired = tuple(sorted(set(before_by_id) - set(after_by_id)))
        assignment_count = sum(
            (
                None
                if before_by_id.get(chunk_id) is None
                else before_by_id[chunk_id].assignee
            )
            != (
                None
                if after_by_id.get(chunk_id) is None
                else after_by_id[chunk_id].assignee
            )
            for chunk_id in affected
        )
        preview = validate_chunk_mutation_preview(
            ChunkMutationPreview(
                operation_id=operation_id,
                action=TopologyAction.CONFLICT_REPLACE,
                project_id=before.project_id,
                chunk_plan_id=before.chunk_plan_id,
                base_revision=before.revision,
                published_revision=initial.revision,
                before_plan_digest=chunk_plan_digest_v1(before),
                after_plan_digest=chunk_plan_digest_v1(initial),
                affected_chunk_ids=affected,
                created_chunk_ids=created,
                retired_chunk_ids=retired,
                affected_chunk_count=len(affected),
                created_chunk_count=len(created),
                retired_chunk_count=len(retired),
                affected_member_count=sum(
                    len(chunk.members) for chunk in initial.chunks
                ),
                assignment_count=assignment_count,
                warnings=(),
                blockers=(),
                truncated=False,
            )
        )
        audit = chunk_operation_audit_digest_v1(
            preview,
            actor_ref,
            before.audit_head_digest,
        )
        candidate = replace(initial, audit_head_digest=audit)
        receipt = validate_chunk_operation_receipt(
            ChunkOperationReceipt(
                operation_id=preview.operation_id,
                action=preview.action,
                project_id=preview.project_id,
                chunk_plan_id=preview.chunk_plan_id,
                base_revision=preview.base_revision,
                published_revision=preview.published_revision,
                before_plan_digest=preview.before_plan_digest,
                after_plan_digest=preview.after_plan_digest,
                affected_chunk_ids=preview.affected_chunk_ids,
                created_chunk_ids=preview.created_chunk_ids,
                retired_chunk_ids=preview.retired_chunk_ids,
                affected_chunk_count=preview.affected_chunk_count,
                created_chunk_count=preview.created_chunk_count,
                retired_chunk_count=preview.retired_chunk_count,
                affected_member_count=preview.affected_member_count,
                assignment_count=preview.assignment_count,
                actor_ref=actor_ref,
                safe_issues=(),
                truncated=False,
                audit_record_digest=audit,
            )
        )
        state = build_next_chunk_metadata_state(
            current,
            project_id=current.project_id,
            active_snapshot=candidate,
            retired_chunk_ids=tuple(
                sorted(set(current.retired_chunk_ids) | set(retired))
            ),
            used_chunk_plan_ids=current.used_chunk_plan_ids,
            receipt=receipt,
        )
        return state, receipt

    def apply(
        self,
        preview: ChunkConflictPreview,
        resolution: ChunkConflictResolution,
        *,
        capability: ChunkManagerCapability | None = None,
        manager: LocalReferenceManagerHandle | None = None,
    ) -> ChunkOperationReceipt | None:
        if type(preview) is not ChunkConflictPreview:
            raise TypeError("conflict preview must be exact")
        if type(resolution) is not ChunkConflictResolution:
            raise TypeError("conflict resolution must be exact")
        with self._lock:
            record = self._conflicts.pop(preview.preview_id, None)
        if record is None or record.preview is not preview or record.state != "pending":
            _fail("CHUNK.PREVIEW_STALE")
        record.state = "terminal"
        workspace = self._workspace_binding()
        current = self._authority._metadata_state_for_conflict_owner()
        classification = self._classify(current, record.incoming, workspace)
        if (
            current != record.current
            or workspace != preview.workspace_binding
            or self._authority.metadata_digest()
            != preview.current_metadata_digest
            or classification is not preview.classification
        ):
            _fail("CHUNK.PREVIEW_STALE")
        if resolution in {
            ChunkConflictResolution.KEEP_CURRENT,
            ChunkConflictResolution.CANCEL,
        }:
            return None
        if classification is ChunkConflictClassification.IDENTICAL:
            if resolution is not ChunkConflictResolution.AUTO:
                _fail("CHUNK.CONFLICT_RESOLUTION_INVALID")
            return None
        if classification in {
            ChunkConflictClassification.STALE,
            ChunkConflictClassification.FOREIGN,
            ChunkConflictClassification.UNIVERSE_MISMATCH,
        }:
            _fail(preview.blockers[0])
        if classification is ChunkConflictClassification.FAST_FORWARD:
            if resolution is not ChunkConflictResolution.AUTO:
                _fail("CHUNK.CONFLICT_RESOLUTION_INVALID")
        elif classification is ChunkConflictClassification.DIVERGED:
            if resolution is not ChunkConflictResolution.REPLACE_INCOMING:
                _fail("CHUNK.CONFLICT_RESOLUTION_REQUIRED")
            if preview.replacement is None:
                _fail("CHUNK.CONFLICT_REPLACE_UNAVAILABLE")
        if capability is None or type(manager) is not LocalReferenceManagerHandle:
            _fail("CHUNK.MANAGER_REQUIRED")
        expected = self._binding(current)
        published: list[ChunkOperationReceipt] = []

        def publish(actor_ref: AssigneeRef) -> None:
            if classification is ChunkConflictClassification.FAST_FORWARD:
                candidate = record.incoming
                receipt = candidate.audit_records[-1].receipt
            else:
                if current is None:
                    _fail("CHUNK.CONFLICT_REPLACE_UNAVAILABLE")
                candidate, receipt = self._replacement_successor(
                    current,
                    record.incoming,
                    actor_ref,
                    preview.preview_id,
                )
                if (
                    preview.replacement is None
                    or self._mutation_from_receipt(receipt)
                    != preview.replacement
                ):
                    _fail("CHUNK.PREVIEW_STALE")
            self._authority._publish_metadata_successor_for_conflict_owner(
                current,
                candidate,
                expected_metadata_digest=preview.current_metadata_digest,
                workspace_binding=workspace,
            )
            published.append(receipt)

        capability_action = preview.required_action
        if capability_action is None:
            _fail("CHUNK.MANAGER_REQUIRED")
        self._authority.consume_manager_capability_with_publication(
            capability,
            manager,
            publish,
            workspace_binding=workspace,
            expected_plan_binding=expected,
            action=capability_action,
        )
        if len(published) != 1:
            _fail("CHUNK.RECOVERY_REQUIRED")
        return published[0]

    def preview_undo(
        self,
        requested_operation_id: str,
        *,
        manager: LocalReferenceManagerHandle,
    ) -> ChunkUndoPreview:
        if type(manager) is not LocalReferenceManagerHandle:
            _fail("CHUNK.ACTOR_UNAVAILABLE")
        manager.__post_init__()
        current = self._authority._metadata_state_for_conflict_owner()
        workspace = self._workspace_binding()
        if current is None or current.active_snapshot is None:
            _fail("CHUNK.UNDO_UNAVAILABLE")
        head = current.audit_records[-1].receipt
        previous = current.head_previous_active_snapshot
        if previous is None:
            _fail("CHUNK.UNDO_UNAVAILABLE")
        previous_ids = {chunk.chunk_id for chunk in previous.chunks}
        current_ids = {
            chunk.chunk_id for chunk in current.active_snapshot.chunks
        }
        if (
            requested_operation_id != head.operation_id
            or previous.segment_universe_digest
            != current.active_snapshot.segment_universe_digest
            or previous.chunk_plan_id != current.active_snapshot.chunk_plan_id
            or previous_ids & set(current.retired_chunk_ids)
            or previous_ids != current_ids
        ):
            _fail("CHUNK.UNDO_UNAVAILABLE")
        if current.active_snapshot.segment_universe_digest != workspace.segment_universe_digest:
            _fail("CHUNK.UNIVERSE_MISMATCH")
        operation_id = self._operation_id()
        initial = replace(
            previous,
            revision=current.active_snapshot.revision + 1,
            audit_head_digest=current.active_snapshot.audit_head_digest,
        )
        before_by_id = {
            chunk.chunk_id: chunk for chunk in current.active_snapshot.chunks
        }
        after_by_id = {chunk.chunk_id: chunk for chunk in initial.chunks}
        affected = tuple(
            sorted(
                chunk_id
                for chunk_id in set(before_by_id) | set(after_by_id)
                if before_by_id.get(chunk_id) != after_by_id.get(chunk_id)
            )
        )
        assignment_count = sum(
            before_by_id[chunk_id].assignee != after_by_id[chunk_id].assignee
            for chunk_id in affected
        )
        mutation = validate_chunk_mutation_preview(
            ChunkMutationPreview(
                operation_id=operation_id,
                action=TopologyAction.UNDO,
                project_id=current.project_id,
                chunk_plan_id=current.active_snapshot.chunk_plan_id,
                base_revision=current.active_snapshot.revision,
                published_revision=initial.revision,
                before_plan_digest=chunk_plan_digest_v1(current.active_snapshot),
                after_plan_digest=chunk_plan_digest_v1(initial),
                affected_chunk_ids=affected,
                created_chunk_ids=(),
                retired_chunk_ids=(),
                affected_chunk_count=len(affected),
                created_chunk_count=0,
                retired_chunk_count=0,
                affected_member_count=sum(
                    len(chunk.members) for chunk in initial.chunks
                ),
                assignment_count=assignment_count,
                warnings=(),
                blockers=(),
                truncated=False,
            )
        )
        audit = chunk_operation_audit_digest_v1(
            mutation,
            manager.actor_ref,
            current.active_snapshot.audit_head_digest,
        )
        candidate_snapshot = replace(initial, audit_head_digest=audit)
        receipt = validate_chunk_operation_receipt(
            ChunkOperationReceipt(
                operation_id=mutation.operation_id,
                action=mutation.action,
                project_id=mutation.project_id,
                chunk_plan_id=mutation.chunk_plan_id,
                base_revision=mutation.base_revision,
                published_revision=mutation.published_revision,
                before_plan_digest=mutation.before_plan_digest,
                after_plan_digest=mutation.after_plan_digest,
                affected_chunk_ids=mutation.affected_chunk_ids,
                created_chunk_ids=(),
                retired_chunk_ids=(),
                affected_chunk_count=mutation.affected_chunk_count,
                created_chunk_count=0,
                retired_chunk_count=0,
                affected_member_count=mutation.affected_member_count,
                assignment_count=mutation.assignment_count,
                actor_ref=manager.actor_ref,
                safe_issues=(),
                truncated=False,
                audit_record_digest=audit,
            )
        )
        candidate = build_next_chunk_metadata_state(
            current,
            project_id=current.project_id,
            active_snapshot=candidate_snapshot,
            retired_chunk_ids=current.retired_chunk_ids,
            used_chunk_plan_ids=current.used_chunk_plan_ids,
            receipt=receipt,
        )
        preview = ChunkUndoPreview(
            requested_operation_id=requested_operation_id,
            mutation=mutation,
            workspace_binding=workspace,
        )
        with self._lock:
            self._undos[operation_id] = _UndoRecord(
                preview,
                current,
                candidate,
                receipt,
            )
        return preview

    def apply_undo(
        self,
        preview: ChunkUndoPreview,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
    ) -> ChunkOperationReceipt:
        if type(preview) is not ChunkUndoPreview:
            raise TypeError("undo preview must be exact")
        with self._lock:
            record = self._undos.pop(preview.mutation.operation_id, None)
        if record is None or record.preview is not preview or record.state != "pending":
            _fail("CHUNK.PREVIEW_STALE")
        record.state = "terminal"
        workspace = self._workspace_binding()
        current = self._authority._metadata_state_for_conflict_owner()
        if current != record.current or workspace != preview.workspace_binding:
            _fail("CHUNK.PREVIEW_STALE")
        expected = self._binding(current)
        if expected is None:
            _fail("CHUNK.UNDO_UNAVAILABLE")
        published = False

        def publish(actor_ref: AssigneeRef) -> None:
            nonlocal published
            if actor_ref != record.receipt.actor_ref:
                _fail("CHUNK.PREVIEW_STALE")
            self._authority._publish_metadata_successor_for_conflict_owner(
                current,
                record.candidate,
                expected_metadata_digest=self._authority.metadata_digest(),
                workspace_binding=workspace,
            )
            published = True

        self._authority.consume_manager_capability_with_publication(
            capability,
            manager,
            publish,
            workspace_binding=workspace,
            expected_plan_binding=expected,
            action=TopologyAction.UNDO,
        )
        if not published:
            _fail("CHUNK.RECOVERY_REQUIRED")
        return record.receipt


__all__ = [
    "ChunkConflictClassification",
    "ChunkConflictPreview",
    "ChunkConflictResolution",
    "ChunkMetadataConflictService",
    "ChunkUndoPreview",
]
