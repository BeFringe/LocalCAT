"""Owner services for collaborative chunk scope and manager authority.

The services expose no workspace body, metadata bytes, search state, or export
policy.  They only revalidate exact current chunk-plan facts supplied by their
owner and issue narrow, non-serializable authority objects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import secrets
from threading import Lock, RLock
from typing import Callable, Protocol

from collaborative_chunk_contracts import (
    AssigneeRef,
    CHUNK_METADATA_NAMESPACE,
    CHUNK_REBASE_INTENT_SCHEMA,
    DISSOLVED_CHUNK_PLAN_DIGEST,
    EMPTY_CHUNK_AUDIT_DIGEST,
    MAX_ACTIVE_CHUNKS,
    MAX_AUDIT_RECORDS,
    ChunkError,
    ChunkAccessDecision,
    ChunkAccessKind,
    ChunkEditOperation,
    ChunkMutationPreview,
    ChunkOperationReceipt,
    ChunkPlanBinding,
    ChunkPlanSnapshot,
    ChunkPublishedWorkspaceTransition,
    ChunkProgress,
    ChunkProgressSegmentFact,
    ChunkScopeProjection,
    ChunkRebaseInspection,
    ChunkRebaseIntent,
    ChunkRebasePreview,
    ChunkSegmentRef,
    ChunkSplitChild,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceProgressProjection,
    ChunkWorkspaceUniverseProjection,
    CollaborativeChunk,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_operation_audit_digest_v1,
    chunk_rebase_intent_digest_v1,
    chunk_plan_digest_v1,
    chunk_plan_binding,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    validate_c1_mutation_preview,
    validate_c1_operation_receipt,
    validate_chunk_mutation_preview,
    validate_chunk_operation_receipt,
    validate_chunk_published_workspace_transition,
    validate_chunk_rebase_inspection,
    validate_chunk_rebase_intent,
    validate_chunk_rebase_preview,
    validate_chunk_id,
    validate_chunk_name,
    validate_chunk_operation_id,
    validate_chunk_plan_id,
    validate_chunk_plan_snapshot,
    validate_assignment_plan_successor,
    validate_rebase_plan_successor,
    validate_topology_assignment_successor,
)
from collaborative_chunk_store import (
    ChunkMetadataState,
    CollaborativeChunkStore,
    build_next_chunk_metadata_state,
    decode_chunk_metadata_state,
    encode_chunk_metadata_state,
    validate_chunk_metadata_successor,
)
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import ProjectWorkspaceError, validate_project_id


def _fail(code: str) -> None:
    raise ChunkError(code)


def _current_snapshot(
    provider: Callable[[], ChunkPlanSnapshot | None],
) -> ChunkPlanSnapshot | None:
    try:
        snapshot = provider()
    except ChunkError:
        raise
    except Exception:
        raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
    if snapshot is None:
        return None
    return validate_chunk_plan_snapshot(snapshot)


def _current_workspace_binding(
    provider: Callable[[], ChunkWorkspaceBinding],
) -> ChunkWorkspaceBinding:
    try:
        binding = provider()
    except ChunkError:
        raise
    except Exception:
        raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
    if type(binding) is not ChunkWorkspaceBinding:
        _fail("CHUNK.CONTRACT_INVALID")
    binding.__post_init__()
    return binding


def _current_retired_chunk_ids(
    provider: Callable[[], tuple[str, ...]],
) -> frozenset[str]:
    try:
        values = provider()
    except ChunkError:
        raise
    except Exception:
        raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
    if type(values) is not tuple:
        _fail("CHUNK.METADATA_INVALID")
    validated = tuple(validate_chunk_id(value) for value in values)
    if len(validated) != len(set(validated)):
        _fail("CHUNK.METADATA_INVALID")
    return frozenset(validated)


def _copy_members(
    members: tuple[ChunkSegmentRef, ...],
) -> tuple[ChunkSegmentRef, ...]:
    return tuple(
        ChunkSegmentRef(
            project_id=member.project_id,
            identity=SegmentIdentity(
                document_id=member.identity.document_id,
                local_segment_id=member.identity.local_segment_id,
            ),
        )
        for member in members
    )


def _copy_workspace_binding(
    binding: ChunkWorkspaceBinding,
) -> ChunkWorkspaceBinding:
    return ChunkWorkspaceBinding(
        project_id=binding.project_id,
        workspace_session_id=binding.workspace_session_id,
        workspace_revision=binding.workspace_revision,
        segment_universe_digest=binding.segment_universe_digest,
        workspace_composition_revision=binding.workspace_composition_revision,
    )


def _copy_plan_binding(
    binding: ChunkPlanBinding | None,
) -> ChunkPlanBinding | None:
    if binding is None:
        return None
    return ChunkPlanBinding(
        project_id=binding.project_id,
        chunk_plan_id=binding.chunk_plan_id,
        plan_revision=binding.plan_revision,
        plan_digest=binding.plan_digest,
        segment_universe_digest=binding.segment_universe_digest,
    )


def _copy_rebase_inspection(
    inspection: ChunkRebaseInspection,
) -> ChunkRebaseInspection:
    validated = validate_chunk_rebase_inspection(inspection)
    previous = validated.previous_workspace_binding
    current = validated.current_workspace_binding
    return ChunkRebaseInspection(
        intent_digest=validated.intent_digest,
        transition_digest=validated.transition_digest,
        plan_binding=_copy_plan_binding(validated.plan_binding),
        previous_workspace_binding=type(previous)(
            project_id=previous.project_id,
            workspace_session_id=previous.workspace_session_id,
            workspace_revision=previous.workspace_revision,
            workspace_composition_revision=(
                previous.workspace_composition_revision
            ),
            workspace_digest=previous.workspace_digest,
            segment_universe_digest=previous.segment_universe_digest,
        ),
        current_workspace_binding=type(current)(
            project_id=current.project_id,
            workspace_session_id=current.workspace_session_id,
            workspace_revision=current.workspace_revision,
            workspace_composition_revision=(
                current.workspace_composition_revision
            ),
            workspace_digest=current.workspace_digest,
            segment_universe_digest=current.segment_universe_digest,
        ),
        retained_attached_members=_copy_members(
            validated.retained_attached_members
        ),
        retained_detached_members=_copy_members(
            validated.retained_detached_members
        ),
        source_changed_members=_copy_members(validated.source_changed_members),
        missing_members=_copy_members(validated.missing_members),
        new_unallocated_members=_copy_members(
            validated.new_unallocated_members
        ),
    )


def _copy_actor_ref(actor_ref: AssigneeRef) -> AssigneeRef:
    return AssigneeRef(
        authority_id=actor_ref.authority_id,
        subject_id=actor_ref.subject_id,
    )


class ChunkScopeProjectionService:
    """Read-only owner seam for explicit active-chunk membership scopes."""

    __slots__ = ("__snapshot_provider", "__retired_chunk_ids_provider")

    def __init__(
        self,
        snapshot_provider: Callable[[], ChunkPlanSnapshot | None],
        *,
        retired_chunk_ids_provider: Callable[[], tuple[str, ...]],
    ) -> None:
        if not callable(snapshot_provider) or not callable(retired_chunk_ids_provider):
            _fail("CHUNK.CONTRACT_INVALID")
        self.__snapshot_provider = snapshot_provider
        self.__retired_chunk_ids_provider = retired_chunk_ids_provider

    def _current(
        self,
    ) -> tuple[ChunkPlanSnapshot, ChunkPlanBinding, frozenset[str]]:
        snapshot = _current_snapshot(self.__snapshot_provider)
        if snapshot is None:
            _fail("CHUNK.REVISION_STALE")
        retired = _current_retired_chunk_ids(self.__retired_chunk_ids_provider)
        if any(chunk.chunk_id in retired for chunk in snapshot.chunks):
            _fail("CHUNK.METADATA_INVALID")
        return snapshot, chunk_plan_binding(snapshot), retired

    def issue_scope_projection(
        self,
        explicit_chunk_id: str,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkScopeProjection:
        chunk_id = validate_chunk_id(explicit_chunk_id)
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        expected_plan_binding.__post_init__()
        snapshot, current_binding, retired = self._current()
        if expected_plan_binding != current_binding:
            _fail("CHUNK.REVISION_STALE")
        if chunk_id in retired:
            _fail("CHUNK.REVISION_STALE")
        chunk = next(
            (candidate for candidate in snapshot.chunks if candidate.chunk_id == chunk_id),
            None,
        )
        if chunk is None:
            _fail("CHUNK.IDENTITY_FOREIGN")
        return ChunkScopeProjection(
            project_id=current_binding.project_id,
            chunk_plan_id=current_binding.chunk_plan_id,
            plan_revision=current_binding.plan_revision,
            plan_digest=current_binding.plan_digest,
            segment_universe_digest=current_binding.segment_universe_digest,
            chunk_id=chunk.chunk_id,
            members=_copy_members(chunk.members),
        )

    def revalidate_scope_projection(
        self,
        projection: ChunkScopeProjection,
    ) -> ChunkScopeProjection:
        if type(projection) is not ChunkScopeProjection:
            _fail("CHUNK.CONTRACT_INVALID")
        projection.__post_init__()
        snapshot, current_binding, retired = self._current()
        projection_binding = ChunkPlanBinding(
            project_id=projection.project_id,
            chunk_plan_id=projection.chunk_plan_id,
            plan_revision=projection.plan_revision,
            plan_digest=projection.plan_digest,
            segment_universe_digest=projection.segment_universe_digest,
        )
        if projection_binding != current_binding:
            _fail("CHUNK.REVISION_STALE")
        if projection.chunk_id in retired:
            _fail("CHUNK.REVISION_STALE")
        chunk = next(
            (
                candidate
                for candidate in snapshot.chunks
                if candidate.chunk_id == projection.chunk_id
            ),
            None,
        )
        if chunk is None or chunk.members != projection.members:
            _fail("CHUNK.REVISION_STALE")
        return ChunkScopeProjection(
            project_id=current_binding.project_id,
            chunk_plan_id=current_binding.chunk_plan_id,
            plan_revision=current_binding.plan_revision,
            plan_digest=current_binding.plan_digest,
            segment_universe_digest=current_binding.segment_universe_digest,
            chunk_id=chunk.chunk_id,
            members=_copy_members(chunk.members),
        )


class ChunkProgressService:
    """Derive current per-chunk progress without reading or storing text."""

    __slots__ = (
        "__snapshot_provider",
        "__snapshot_binding_provider",
        "__workspace_progress_provider",
        "__cached_workspace_binding",
        "__cached_progress_projection",
        "__cached_progress_entries",
        "__cached_plan_binding",
        "__cached_plan_snapshot",
    )

    def __init__(
        self,
        snapshot_provider: Callable[[], ChunkPlanSnapshot | None],
        workspace_progress_provider: Callable[
            [], ChunkWorkspaceProgressProjection
        ],
        *,
        snapshot_binding_provider: Callable[
            [], ChunkPlanBinding | None
        ] | None = None,
    ) -> None:
        if not callable(snapshot_provider) or not callable(
            workspace_progress_provider
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        if snapshot_binding_provider is not None and not callable(
            snapshot_binding_provider
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        self.__snapshot_provider = snapshot_provider
        self.__snapshot_binding_provider = snapshot_binding_provider
        self.__workspace_progress_provider = workspace_progress_provider
        self.__cached_workspace_binding: ChunkWorkspaceBinding | None = None
        self.__cached_progress_projection: (
            ChunkWorkspaceProgressProjection | None
        ) = None
        self.__cached_progress_entries: dict[
            tuple[str, str, str], ChunkProgressSegmentFact
        ] | None = None
        self.__cached_plan_binding: ChunkPlanBinding | None = None
        self.__cached_plan_snapshot: ChunkPlanSnapshot | None = None

    def _current_plan_snapshot(
        self,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkPlanSnapshot:
        binding_provider = self.__snapshot_binding_provider
        if binding_provider is not None:
            try:
                current_binding = binding_provider()
            except ChunkError:
                raise
            except Exception:
                raise ChunkError(
                    "CHUNK.RECOVERY_REQUIRED",
                    retryable=True,
                ) from None
            if current_binding != expected_plan_binding:
                _fail("CHUNK.REVISION_STALE")
            if (
                self.__cached_plan_binding == current_binding
                and self.__cached_plan_snapshot is not None
            ):
                return self.__cached_plan_snapshot
        snapshot = _current_snapshot(self.__snapshot_provider)
        if snapshot is None:
            _fail("CHUNK.REVISION_STALE")
        current_binding = chunk_plan_binding(snapshot)
        if current_binding != expected_plan_binding:
            _fail("CHUNK.REVISION_STALE")
        if binding_provider is not None:
            self.__cached_plan_binding = current_binding
            self.__cached_plan_snapshot = snapshot
        return snapshot

    def _current_progress_projection(
        self,
        workspace_binding: ChunkWorkspaceBinding,
    ) -> tuple[
        ChunkWorkspaceProgressProjection,
        dict[tuple[str, str, str], ChunkProgressSegmentFact],
    ]:
        if (
            self.__cached_workspace_binding == workspace_binding
            and self.__cached_progress_projection is not None
            and self.__cached_progress_entries is not None
        ):
            return (
                self.__cached_progress_projection,
                self.__cached_progress_entries,
            )
        try:
            projection = self.__workspace_progress_provider()
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if type(projection) is not ChunkWorkspaceProgressProjection:
            _fail("CHUNK.CONTRACT_INVALID")
        projection.__post_init__()
        if projection.binding != workspace_binding:
            _fail("CHUNK.REVISION_STALE")
        entries: dict[tuple[str, str, str], ChunkProgressSegmentFact] = {
            _member_key(entry.segment): entry for entry in projection.entries
        }
        if len(entries) != len(projection.entries):
            _fail("CHUNK.MEMBER_DUPLICATE")
        self.__cached_workspace_binding = workspace_binding
        self.__cached_progress_projection = projection
        self.__cached_progress_entries = entries
        return projection, entries

    def progress(
        self,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
    ) -> ChunkProgress:
        if type(workspace_binding) is not ChunkWorkspaceBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        workspace_binding.__post_init__()
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        expected_plan_binding.__post_init__()
        validated_chunk_id = validate_chunk_id(chunk_id)
        projection, entries = self._current_progress_projection(
            workspace_binding
        )
        snapshot = self._current_plan_snapshot(expected_plan_binding)
        if workspace_binding.project_id != snapshot.project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        if (
            workspace_binding.segment_universe_digest
            != snapshot.segment_universe_digest
        ):
            _fail("CHUNK.REBASE_REQUIRED")
        chunk = next(
            (
                value
                for value in snapshot.chunks
                if value.chunk_id == validated_chunk_id
            ),
            None,
        )
        if chunk is None:
            _fail("CHUNK.IDENTITY_FOREIGN")
        unfilled = 0
        draft = 0
        confirmed = 0
        detached = 0
        for member in chunk.members:
            entry = entries.get(_member_key(member))
            if entry is None:
                _fail("CHUNK.REBASE_REQUIRED")
            if entry.source_presence is SourcePresence.DETACHED:
                detached += 1
            elif entry.confirmed:
                confirmed += 1
            elif entry.target_is_blank:
                unfilled += 1
            else:
                draft += 1
        attached_total = unfilled + draft + confirmed
        return ChunkProgress(
            chunk_id=chunk.chunk_id,
            attached_total=attached_total,
            unfilled=unfilled,
            draft=draft,
            confirmed=confirmed,
            detached=detached,
            completion_numerator=confirmed,
            completion_denominator=attached_total,
        )

_ACTOR_HANDLE_CONSTRUCTOR_SEAL = object()


class AuthenticatedActorHandle:
    """Opaque identity-owner handle; never a persisted assignee reference."""

    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> AuthenticatedActorHandle:
        if kwargs.get("_seal") is not _ACTOR_HANDLE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.ACTOR_UNVERIFIED")
        return super().__new__(cls)

    def __init__(self, *, _seal: object, owner_token: object) -> None:
        if _seal is not _ACTOR_HANDLE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.ACTOR_UNVERIFIED")
        object.__setattr__(self, "_AuthenticatedActorHandle__owner_token", owner_token)
        object.__setattr__(
            self,
            "_AuthenticatedActorHandle__nonce",
            secrets.token_bytes(32),
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("AuthenticatedActorHandle cannot be subclassed")

    def __repr__(self) -> str:
        return "<AuthenticatedActorHandle opaque>"

    def __reduce__(self) -> object:
        raise TypeError("AuthenticatedActorHandle is not serializable")

    def __copy__(self) -> object:
        raise TypeError("AuthenticatedActorHandle is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("AuthenticatedActorHandle is not copyable")


class AuthenticatedActorPort(Protocol):
    """Trusted identity-owner seam used only to resolve an opaque handle."""

    def current_actor(self) -> AuthenticatedActorHandle: ...

    def revalidate_actor(self, handle: AuthenticatedActorHandle) -> AssigneeRef: ...


class LocalReferenceActorPort:
    """Honest local-workflow identity composition, not account authentication."""

    __slots__ = ("__actor_ref", "__handle", "__owner_token", "__available", "__lock")

    identity_kind = "local_reference"
    is_account_authenticated = False

    def __init__(self, authority_id: str, subject_id: str) -> None:
        self.__actor_ref = AssigneeRef(authority_id, subject_id)
        self.__owner_token = object()
        self.__handle = AuthenticatedActorHandle(
            _seal=_ACTOR_HANDLE_CONSTRUCTOR_SEAL,
            owner_token=self.__owner_token,
        )
        self.__available = True
        self.__lock = Lock()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("LocalReferenceActorPort cannot be subclassed")

    def current_actor(self) -> AuthenticatedActorHandle:
        with self.__lock:
            if not self.__available:
                _fail("CHUNK.ACTOR_UNAVAILABLE")
            return self.__handle

    def revalidate_actor(self, handle: AuthenticatedActorHandle) -> AssigneeRef:
        with self.__lock:
            if not self.__available:
                _fail("CHUNK.ACTOR_UNAVAILABLE")
            if type(handle) is not AuthenticatedActorHandle or handle is not self.__handle:
                _fail("CHUNK.ACTOR_UNVERIFIED")
            return _copy_actor_ref(self.__actor_ref)

    def set_available(self, available: bool) -> None:
        """Reference-harness lifecycle control; it does not model account auth."""

        if type(available) is not bool:
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            self.__available = available


def _resolve_authenticated_actor(
    port: AuthenticatedActorPort,
    handle: AuthenticatedActorHandle,
) -> AssigneeRef:
    resolver = getattr(port, "revalidate_actor", None)
    if not callable(resolver):
        _fail("CHUNK.ACTOR_UNAVAILABLE")
    if type(handle) is not AuthenticatedActorHandle:
        _fail("CHUNK.ACTOR_UNVERIFIED")
    try:
        actor_ref = resolver(handle)
    except ChunkError as error:
        if error.code in {"CHUNK.ACTOR_UNAVAILABLE", "CHUNK.ACTOR_UNVERIFIED"}:
            raise
        raise ChunkError("CHUNK.ACTOR_UNVERIFIED") from None
    except Exception:
        raise ChunkError("CHUNK.ACTOR_UNAVAILABLE", retryable=True) from None
    if type(actor_ref) is not AssigneeRef:
        _fail("CHUNK.ACTOR_UNVERIFIED")
    actor_ref.__post_init__()
    return _copy_actor_ref(actor_ref)


_CAPABILITY_CONSTRUCTOR_SEAL = object()


class ChunkManagerCapability:
    """Opaque, owner-registered, non-serializable single-use authority."""

    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> ChunkManagerCapability:
        if kwargs.get("_seal") is not _CAPABILITY_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        return super().__new__(cls)

    def __init__(
        self,
        *,
        _seal: object,
        owner_token: object,
        nonce: object,
    ) -> None:
        if _seal is not _CAPABILITY_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        object.__setattr__(self, "_ChunkManagerCapability__owner_token", owner_token)
        object.__setattr__(self, "_ChunkManagerCapability__nonce", nonce)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ChunkManagerCapability cannot be subclassed")

    def __repr__(self) -> str:
        return "<ChunkManagerCapability opaque>"

    def __reduce__(self) -> object:
        raise TypeError("ChunkManagerCapability is not serializable")

    def __copy__(self) -> object:
        raise TypeError("ChunkManagerCapability is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("ChunkManagerCapability is not copyable")


@dataclass(slots=True)
class _CapabilityRecord:
    capability: ChunkManagerCapability
    manager_handle: LocalReferenceManagerHandle
    actor_ref: AssigneeRef
    workspace_binding: ChunkWorkspaceBinding
    plan_binding: ChunkPlanBinding | None
    action: TopologyAction
    consumed: bool = False


class ChunkManagerCapabilityService:
    """Issues and atomically consumes topology-only manager capabilities."""

    __slots__ = (
        "__snapshot_provider",
        "__workspace_binding_provider",
        "__owner_token",
        "__records",
        "__lock",
        "__project_id",
    )

    def __init__(
        self,
        snapshot_provider: Callable[[], ChunkPlanSnapshot | None],
        *,
        project_id: str,
        workspace_binding_provider: Callable[[], ChunkWorkspaceBinding],
    ) -> None:
        if not callable(snapshot_provider) or not callable(workspace_binding_provider):
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            validated_project_id = validate_project_id(project_id)
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        self.__snapshot_provider = snapshot_provider
        self.__workspace_binding_provider = workspace_binding_provider
        self.__owner_token = object()
        self.__records: dict[int, _CapabilityRecord] = {}
        self.__lock = Lock()
        self.__project_id = validated_project_id

    def _validate_current(
        self,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
        *,
        stale_code: str,
    ) -> None:
        if type(workspace_binding) is not ChunkWorkspaceBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        workspace_binding.__post_init__()
        if workspace_binding.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        if workspace_binding != _current_workspace_binding(
            self.__workspace_binding_provider
        ):
            _fail(stale_code)
        if type(action) is not TopologyAction:
            _fail("CHUNK.CONTRACT_INVALID")
        snapshot = _current_snapshot(self.__snapshot_provider)
        if snapshot is None:
            if action is not TopologyAction.CREATE or expected_plan_binding is not None:
                _fail(stale_code)
            return
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail(stale_code)
        expected_plan_binding.__post_init__()
        current_binding = chunk_plan_binding(snapshot)
        binding_mismatch = (
            current_binding.project_id != self.__project_id
            or expected_plan_binding != current_binding
            or workspace_binding.project_id != current_binding.project_id
        )
        universe_mismatch = (
            workspace_binding.segment_universe_digest
            != current_binding.segment_universe_digest
        )
        if binding_mismatch or (
            universe_mismatch
            and action not in {TopologyAction.REBASE, TopologyAction.DISSOLVE_PLAN}
        ):
            _fail(stale_code)

    def issue_manager_capability(
        self,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> ChunkManagerCapability:
        if type(manager) is not LocalReferenceManagerHandle:
            _fail("CHUNK.ACTOR_UNAVAILABLE")
        manager.__post_init__()
        self._validate_current(
            workspace_binding,
            expected_plan_binding,
            action,
            stale_code="CHUNK.REVISION_STALE",
        )
        with self.__lock:
            nonce = secrets.token_bytes(32)
            capability = ChunkManagerCapability(
                _seal=_CAPABILITY_CONSTRUCTOR_SEAL,
                owner_token=self.__owner_token,
                nonce=nonce,
            )
            self.__records[id(capability)] = _CapabilityRecord(
                capability=capability,
                manager_handle=manager,
                actor_ref=manager.actor_ref,
                workspace_binding=_copy_workspace_binding(workspace_binding),
                plan_binding=_copy_plan_binding(expected_plan_binding),
                action=action,
            )
            return capability

    def _revalidate_registered(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> _CapabilityRecord:
        if type(capability) is not ChunkManagerCapability:
            _fail("CHUNK.MANAGER_REQUIRED")
        if type(manager) is not LocalReferenceManagerHandle:
            _fail("CHUNK.ACTOR_UNAVAILABLE")
        manager.__post_init__()
        record = self.__records.get(id(capability))
        if record is None or record.capability is not capability:
            _fail("CHUNK.MANAGER_REQUIRED")
        if record.consumed:
            _fail("CHUNK.PREVIEW_STALE")
        if (
            record.manager_handle is not manager
            or record.actor_ref != manager.actor_ref
            or record.workspace_binding != workspace_binding
            or record.plan_binding != expected_plan_binding
            or record.action is not action
        ):
            _fail("CHUNK.PREVIEW_STALE")
        self._validate_current(
            workspace_binding,
            expected_plan_binding,
            action,
            stale_code="CHUNK.PREVIEW_STALE",
        )
        return record

    def revalidate_manager_capability(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> AssigneeRef:
        with self.__lock:
            record = self._revalidate_registered(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected_plan_binding,
                action=action,
            )
            return _copy_actor_ref(record.actor_ref)

    def consume_manager_capability(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> AssigneeRef:
        with self.__lock:
            record = self._revalidate_registered(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected_plan_binding,
                action=action,
            )
            record.consumed = True
            return _copy_actor_ref(record.actor_ref)

    def consume_manager_capability_with_publication(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        publication: Callable[[AssigneeRef], None],
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> AssigneeRef:
        """Consume only after one owner-provided atomic publication returns."""

        if not callable(publication):
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            record = self._revalidate_registered(
                capability,
                manager,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected_plan_binding,
                action=action,
            )
            actor_ref = _copy_actor_ref(record.actor_ref)
            publication(_copy_actor_ref(actor_ref))
            record.consumed = True
            return actor_ref


def _copy_snapshot(snapshot: ChunkPlanSnapshot) -> ChunkPlanSnapshot:
    return ChunkPlanSnapshot(
        schema_version=snapshot.schema_version,
        namespace=snapshot.namespace,
        chunk_plan_id=snapshot.chunk_plan_id,
        project_id=snapshot.project_id,
        revision=snapshot.revision,
        segment_universe_digest=snapshot.segment_universe_digest,
        chunks=tuple(
            CollaborativeChunk(
                chunk_id=chunk.chunk_id,
                name=chunk.name,
                order=chunk.order,
                members=_copy_members(chunk.members),
                assignee=(
                    None
                    if chunk.assignee is None
                    else _copy_actor_ref(chunk.assignee)
                ),
            )
            for chunk in snapshot.chunks
        ),
        audit_head_digest=snapshot.audit_head_digest,
    )


def _copy_preview(preview: ChunkMutationPreview) -> ChunkMutationPreview:
    return ChunkMutationPreview(
        operation_id=preview.operation_id,
        action=preview.action,
        project_id=preview.project_id,
        chunk_plan_id=preview.chunk_plan_id,
        base_revision=preview.base_revision,
        published_revision=preview.published_revision,
        before_plan_digest=preview.before_plan_digest,
        after_plan_digest=preview.after_plan_digest,
        affected_chunk_ids=tuple(preview.affected_chunk_ids),
        created_chunk_ids=tuple(preview.created_chunk_ids),
        retired_chunk_ids=tuple(preview.retired_chunk_ids),
        affected_chunk_count=preview.affected_chunk_count,
        created_chunk_count=preview.created_chunk_count,
        retired_chunk_count=preview.retired_chunk_count,
        affected_member_count=preview.affected_member_count,
        assignment_count=preview.assignment_count,
        warnings=tuple(preview.warnings),
        blockers=tuple(preview.blockers),
        truncated=preview.truncated,
    )


def _copy_receipt(receipt: ChunkOperationReceipt) -> ChunkOperationReceipt:
    return ChunkOperationReceipt(
        operation_id=receipt.operation_id,
        action=receipt.action,
        project_id=receipt.project_id,
        chunk_plan_id=receipt.chunk_plan_id,
        base_revision=receipt.base_revision,
        published_revision=receipt.published_revision,
        before_plan_digest=receipt.before_plan_digest,
        after_plan_digest=receipt.after_plan_digest,
        affected_chunk_ids=tuple(receipt.affected_chunk_ids),
        created_chunk_ids=tuple(receipt.created_chunk_ids),
        retired_chunk_ids=tuple(receipt.retired_chunk_ids),
        affected_chunk_count=receipt.affected_chunk_count,
        created_chunk_count=receipt.created_chunk_count,
        retired_chunk_count=receipt.retired_chunk_count,
        affected_member_count=receipt.affected_member_count,
        assignment_count=receipt.assignment_count,
        actor_ref=_copy_actor_ref(receipt.actor_ref),
        safe_issues=tuple(receipt.safe_issues),
        truncated=receipt.truncated,
        audit_record_digest=receipt.audit_record_digest,
    )


def _member_key(member: ChunkSegmentRef) -> tuple[str, str, str]:
    return (
        member.project_id,
        member.identity.document_id,
        member.identity.local_segment_id,
    )


def _dense_chunks(
    chunks: tuple[CollaborativeChunk, ...],
) -> tuple[CollaborativeChunk, ...]:
    return tuple(
        CollaborativeChunk(
            chunk_id=chunk.chunk_id,
            name=chunk.name,
            order=order,
            members=_copy_members(chunk.members),
            assignee=(
                None
                if chunk.assignee is None
                else _copy_actor_ref(chunk.assignee)
            ),
        )
        for order, chunk in enumerate(chunks)
    )


@dataclass(slots=True)
class _PreparedTopologyPlan:
    preview: ChunkMutationPreview
    receipt: ChunkOperationReceipt
    candidate: ChunkPlanSnapshot | None
    expected_plan_binding: ChunkPlanBinding | None
    workspace_binding: ChunkWorkspaceBinding
    retired_baseline: frozenset[str]
    retired_additions: tuple[str, ...]
    capability: ChunkManagerCapability
    manager: LocalReferenceManagerHandle
    rebase_intent_digest: str | None = None
    state: str = "pending"


@dataclass(frozen=True, slots=True)
class ChunkTopologyPublicationResult:
    """Cold-readback facts returned by the topology publication owner."""

    snapshot: ChunkPlanSnapshot | None
    retired_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.snapshot is not None:
            validate_chunk_plan_snapshot(self.snapshot)
        if type(self.retired_chunk_ids) is not tuple:
            _fail("CHUNK.CONTRACT_INVALID")
        validated = tuple(
            validate_chunk_id(value) for value in self.retired_chunk_ids
        )
        if len(validated) != len(set(validated)):
            _fail("CHUNK.METADATA_INVALID")
        retired = set(validated)
        if self.snapshot is not None and any(
            chunk.chunk_id in retired for chunk in self.snapshot.chunks
        ):
            _fail("CHUNK.METADATA_INVALID")


_PUBLICATION_PERMIT_SEAL = object()
_TOPOLOGY_SERVICE_CONSTRUCTOR_SEAL = object()
_ASSIGNMENT_PUBLICATION_PERMIT_SEAL = object()
_ASSIGNMENT_SERVICE_CONSTRUCTOR_SEAL = object()
_PERMISSION_MUTATION_PERMIT_SEAL = object()
_PERMISSION_SERVICE_CONSTRUCTOR_SEAL = object()


class _TopologyPublicationPermit:
    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> _TopologyPublicationPermit:
        if kwargs.get("_seal") is not _PUBLICATION_PERMIT_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        return super().__new__(cls)

    def __init__(self, *, _seal: object, owner_token: object) -> None:
        if _seal is not _PUBLICATION_PERMIT_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        object.__setattr__(self, "_TopologyPublicationPermit__owner_token", owner_token)
        object.__setattr__(
            self,
            "_TopologyPublicationPermit__nonce",
            secrets.token_bytes(32),
        )

    def __reduce__(self) -> object:
        raise TypeError("topology publication permit is not serializable")

    def __copy__(self) -> object:
        raise TypeError("topology publication permit is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("topology publication permit is not copyable")


class _AssignmentPublicationPermit:
    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> _AssignmentPublicationPermit:
        if kwargs.get("_seal") is not _ASSIGNMENT_PUBLICATION_PERMIT_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        return super().__new__(cls)

    def __init__(self, *, _seal: object, owner_token: object) -> None:
        if _seal is not _ASSIGNMENT_PUBLICATION_PERMIT_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        object.__setattr__(
            self,
            "_AssignmentPublicationPermit__owner_token",
            owner_token,
        )
        object.__setattr__(
            self,
            "_AssignmentPublicationPermit__nonce",
            secrets.token_bytes(32),
        )

    def __reduce__(self) -> object:
        raise TypeError("assignment publication permit is not serializable")

    def __copy__(self) -> object:
        raise TypeError("assignment publication permit is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("assignment publication permit is not copyable")


class _PermissionMutationPermit:
    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> _PermissionMutationPermit:
        if kwargs.get("_seal") is not _PERMISSION_MUTATION_PERMIT_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        return super().__new__(cls)

    def __init__(self, *, _seal: object, owner_token: object) -> None:
        if _seal is not _PERMISSION_MUTATION_PERMIT_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        object.__setattr__(
            self,
            "_PermissionMutationPermit__owner_token",
            owner_token,
        )
        object.__setattr__(
            self,
            "_PermissionMutationPermit__nonce",
            secrets.token_bytes(32),
        )

    def __reduce__(self) -> object:
        raise TypeError("permission mutation permit is not serializable")

    def __copy__(self) -> object:
        raise TypeError("permission mutation permit is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("permission mutation permit is not copyable")


class ChunkSegmentEditMutationPort(Protocol):
    """Trusted Workspace owner seam; edit bodies stay behind this port.

    Implementations must either raise before changing Workspace state or return
    only after the exact edit is complete.  A late raise is an owner contract
    violation; the caller still consumes the edit capability and never replays
    it automatically.
    """

    def apply_segment_edit(
        self,
        segment: ChunkSegmentRef,
        expected_workspace_binding: ChunkWorkspaceBinding,
    ) -> object: ...


class ChunkTopologyPublicationAuthority:
    """Chunk owner for atomic plan mutation and optional durable publication."""

    __slots__ = (
        "__project_id",
        "__snapshot",
        "__snapshot_binding",
        "__retired",
        "__receipts",
        "__used_plan_ids",
        "__lock",
        "__capability_service",
        "__permit_owner_token",
        "__topology_permit",
        "__assignment_permit",
        "__permission_permit",
        "__workspace_binding_provider",
        "__metadata_store",
        "__metadata_state",
        "__metadata_digest",
        "__recovery_required",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ChunkTopologyPublicationAuthority cannot be subclassed")

    def __init__(
        self,
        *,
        project_id: str,
        workspace_binding_provider: Callable[[], ChunkWorkspaceBinding],
        initial_snapshot: ChunkPlanSnapshot | None = None,
        retired_chunk_ids: tuple[str, ...] = (),
        used_chunk_plan_ids: tuple[str, ...] = (),
        metadata_store: CollaborativeChunkStore | None = None,
    ) -> None:
        try:
            validated_project_id = validate_project_id(project_id)
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        if not callable(workspace_binding_provider):
            _fail("CHUNK.CONTRACT_INVALID")
        if metadata_store is not None and type(metadata_store) is not CollaborativeChunkStore:
            _fail("CHUNK.CONTRACT_INVALID")
        durable_state = None
        durable_digest = None
        if metadata_store is not None:
            durable_state, durable_digest = metadata_store.load_with_digest()
            durable_intent = metadata_store.load_rebase_intent()
            if durable_intent is not None and durable_state is not None:
                final = durable_state.audit_records[-1].receipt
                consumed = (
                    durable_state.active_snapshot is not None
                    and final.action is TopologyAction.REBASE
                    and final.chunk_plan_id
                    == durable_intent.plan_binding.chunk_plan_id
                    and final.base_revision
                    == durable_intent.plan_binding.plan_revision
                    and final.before_plan_digest
                    == durable_intent.plan_binding.plan_digest
                    and durable_state.active_snapshot.segment_universe_digest
                    == durable_intent.transition.current.binding.segment_universe_digest
                ) or (
                    durable_state.active_snapshot is None
                    and final.action is TopologyAction.DISSOLVE_PLAN
                    and final.chunk_plan_id
                    == durable_intent.plan_binding.chunk_plan_id
                    and final.before_plan_digest
                    == durable_intent.plan_binding.plan_digest
                )
                if consumed:
                    metadata_store.clear_consumed_rebase_intent()
            if durable_state is not None:
                if (
                    durable_state.project_id != validated_project_id
                    or initial_snapshot is not None
                    or retired_chunk_ids
                    or used_chunk_plan_ids
                ):
                    _fail("CHUNK.METADATA_INVALID")
                initial_snapshot = durable_state.active_snapshot
                retired_chunk_ids = durable_state.retired_chunk_ids
                used_chunk_plan_ids = durable_state.used_chunk_plan_ids
            elif initial_snapshot is not None or retired_chunk_ids or used_chunk_plan_ids:
                _fail("CHUNK.METADATA_INVALID")
        snapshot = None
        if initial_snapshot is not None:
            snapshot = _copy_snapshot(validate_chunk_plan_snapshot(initial_snapshot))
            if snapshot.project_id != validated_project_id or not snapshot.chunks:
                _fail("CHUNK.METADATA_INVALID")
        if type(retired_chunk_ids) is not tuple or type(used_chunk_plan_ids) is not tuple:
            _fail("CHUNK.CONTRACT_INVALID")
        retired = frozenset(validate_chunk_id(value) for value in retired_chunk_ids)
        if len(retired) != len(retired_chunk_ids):
            _fail("CHUNK.METADATA_INVALID")
        used = frozenset(
            validate_chunk_plan_id(value) for value in used_chunk_plan_ids
        )
        if len(used) != len(used_chunk_plan_ids):
            _fail("CHUNK.METADATA_INVALID")
        if snapshot is not None:
            if any(chunk.chunk_id in retired for chunk in snapshot.chunks):
                _fail("CHUNK.METADATA_INVALID")
            used = used | {snapshot.chunk_plan_id}
        self.__project_id = validated_project_id
        self.__workspace_binding_provider = workspace_binding_provider
        self.__snapshot = snapshot
        self.__snapshot_binding = (
            None if snapshot is None else chunk_plan_binding(snapshot)
        )
        self.__retired = retired
        self.__receipts = (
            ()
            if durable_state is None
            else tuple(record.receipt for record in durable_state.audit_records)
        )
        self.__used_plan_ids = used
        self.__metadata_store = metadata_store
        self.__metadata_state = durable_state
        self.__metadata_digest = durable_digest
        self.__recovery_required = False
        self.__lock = Lock()
        self.__permit_owner_token = object()
        self.__topology_permit: _TopologyPublicationPermit | None = None
        self.__assignment_permit: _AssignmentPublicationPermit | None = None
        self.__permission_permit: _PermissionMutationPermit | None = None
        self.__capability_service = ChunkManagerCapabilityService(
            self.current_snapshot,
            project_id=validated_project_id,
            workspace_binding_provider=workspace_binding_provider,
        )

    @property
    def project_id(self) -> str:
        return self.__project_id

    def current_snapshot(self) -> ChunkPlanSnapshot | None:
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return (
                None
                if self.__snapshot is None
                else _copy_snapshot(self.__snapshot)
            )

    def current_plan_binding(self) -> ChunkPlanBinding | None:
        """Return the current immutable plan version without copying members."""

        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return _copy_plan_binding(self.__snapshot_binding)

    def retired_chunk_ids(self) -> tuple[str, ...]:
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return tuple(sorted(self.__retired))

    def used_chunk_plan_ids(self) -> tuple[str, ...]:
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return tuple(sorted(self.__used_plan_ids))

    def operation_receipts(self) -> tuple[ChunkOperationReceipt, ...]:
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return tuple(_copy_receipt(receipt) for receipt in self.__receipts)

    def metadata_digest(self) -> str | None:
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            return self.__metadata_digest

    def _metadata_state_for_conflict_owner(self) -> ChunkMetadataState | None:
        """Return one strict detached graph to the C3 metadata owner."""

        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            if self.__metadata_store is None:
                _fail("CHUNK.METADATA_UNAVAILABLE")
            if self.__metadata_state is None:
                return None
            return decode_chunk_metadata_state(
                encode_chunk_metadata_state(self.__metadata_state)
            )

    def _publish_metadata_successor_for_conflict_owner(
        self,
        current: ChunkMetadataState | None,
        candidate: ChunkMetadataState,
        *,
        expected_metadata_digest: str | None,
        workspace_binding: ChunkWorkspaceBinding,
    ) -> ChunkMetadataState:
        """Publish one already-classified exact successor with store CAS."""

        validated = validate_chunk_metadata_successor(current, candidate)
        workspace_binding.__post_init__()
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            if self.__metadata_store is None:
                _fail("CHUNK.METADATA_UNAVAILABLE")
            if (
                self.__metadata_state != current
                or self.__metadata_digest != expected_metadata_digest
                or workspace_binding
                != _current_workspace_binding(self.__workspace_binding_provider)
                or workspace_binding.project_id != self.__project_id
                or validated.project_id != self.__project_id
                or (
                    validated.active_snapshot is not None
                    and validated.active_snapshot.segment_universe_digest
                    != workspace_binding.segment_universe_digest
                )
            ):
                _fail("CHUNK.DESTINATION_STALE")
            try:
                publication = self.__metadata_store.publish(
                    validated,
                    expected_metadata_digest=expected_metadata_digest,
                )
            except ChunkError as error:
                if error.code in {
                    "CHUNK.RECOVERY_REQUIRED",
                    "CHUNK.DESTINATION_STALE",
                }:
                    self.__recovery_required = True
                raise
            self.__snapshot = (
                None
                if validated.active_snapshot is None
                else _copy_snapshot(validated.active_snapshot)
            )
            self.__snapshot_binding = (
                None
                if self.__snapshot is None
                else chunk_plan_binding(self.__snapshot)
            )
            self.__retired = frozenset(validated.retired_chunk_ids)
            self.__used_plan_ids = frozenset(validated.used_chunk_plan_ids)
            self.__receipts = tuple(
                _copy_receipt(record.receipt)
                for record in validated.audit_records
            )
            self.__metadata_state = decode_chunk_metadata_state(
                encode_chunk_metadata_state(validated)
            )
            self.__metadata_digest = publication.metadata_digest
            return decode_chunk_metadata_state(
                encode_chunk_metadata_state(validated)
            )

    def current_rebase_intent(
        self,
        permit: _TopologyPublicationPermit,
    ) -> ChunkRebaseIntent | None:
        self._validate_topology_permit(permit)
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            if self.__metadata_store is None:
                return None
            return self.__metadata_store.load_rebase_intent()

    def capture_rebase_intent(
        self,
        permit: _TopologyPublicationPermit,
        intent: ChunkRebaseIntent,
    ) -> ChunkRebaseIntent:
        self._validate_topology_permit(permit)
        validated = validate_chunk_rebase_intent(intent)
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            if self.__metadata_store is None:
                _fail("CHUNK.RECOVERY_REQUIRED")
            if (
                self.__snapshot is None
                or chunk_plan_binding(self.__snapshot) != validated.plan_binding
            ):
                _fail("CHUNK.REBASE_REQUIRED")
            try:
                return self.__metadata_store.capture_rebase_intent(validated)
            except ChunkError as error:
                if error.code in {
                    "CHUNK.RECOVERY_REQUIRED",
                    "CHUNK.DESTINATION_STALE",
                }:
                    self.__recovery_required = True
                raise

    def create_topology_service(
        self,
        *,
        workspace_universe_provider: Callable[
            [], ChunkWorkspaceUniverseProjection
        ],
        workspace_transition_provider: Callable[
            [], ChunkPublishedWorkspaceTransition
        ] | None = None,
        chunk_id_issuer: Callable[[], str] = issue_chunk_id,
        plan_id_issuer: Callable[[], str] = issue_chunk_plan_id,
        operation_id_issuer: Callable[[], str] = issue_chunk_operation_id,
    ) -> CollaborativeChunkTopologyService:
        if workspace_transition_provider is not None and not callable(
            workspace_transition_provider
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        if any(
            not callable(value)
            for value in (
                workspace_universe_provider,
                chunk_id_issuer,
                plan_id_issuer,
                operation_id_issuer,
            )
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            if self.__topology_permit is not None:
                _fail("CHUNK.MANAGER_REQUIRED")
            permit = _TopologyPublicationPermit(
                _seal=_PUBLICATION_PERMIT_SEAL,
                owner_token=self.__permit_owner_token,
            )
            self.__topology_permit = permit
        try:
            return CollaborativeChunkTopologyService(
                self,
                _seal=_TOPOLOGY_SERVICE_CONSTRUCTOR_SEAL,
                publication_permit=permit,
                project_id=self.__project_id,
                workspace_universe_provider=workspace_universe_provider,
                workspace_transition_provider=workspace_transition_provider,
                chunk_id_issuer=chunk_id_issuer,
                plan_id_issuer=plan_id_issuer,
                operation_id_issuer=operation_id_issuer,
            )
        except Exception:
            with self.__lock:
                if self.__topology_permit is permit:
                    self.__topology_permit = None
            raise

    def create_assignment_service(
        self,
        *,
        operation_id_issuer: Callable[[], str] = issue_chunk_operation_id,
    ) -> CollaborativeChunkAssignmentService:
        if not callable(operation_id_issuer):
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            if self.__assignment_permit is not None:
                _fail("CHUNK.MANAGER_REQUIRED")
            permit = _AssignmentPublicationPermit(
                _seal=_ASSIGNMENT_PUBLICATION_PERMIT_SEAL,
                owner_token=self.__permit_owner_token,
            )
            self.__assignment_permit = permit
        try:
            return CollaborativeChunkAssignmentService(
                self,
                _seal=_ASSIGNMENT_SERVICE_CONSTRUCTOR_SEAL,
                publication_permit=permit,
                project_id=self.__project_id,
                operation_id_issuer=operation_id_issuer,
            )
        except Exception:
            with self.__lock:
                if self.__assignment_permit is permit:
                    self.__assignment_permit = None
            raise

    def create_permission_service(
        self,
        *,
        workspace_universe_provider: Callable[
            [], ChunkWorkspaceUniverseProjection
        ],
    ) -> CollaborativeChunkPermissionService:
        if not callable(workspace_universe_provider):
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            if self.__permission_permit is not None:
                _fail("CHUNK.MANAGER_REQUIRED")
            permit = _PermissionMutationPermit(
                _seal=_PERMISSION_MUTATION_PERMIT_SEAL,
                owner_token=self.__permit_owner_token,
            )
            self.__permission_permit = permit
        try:
            return CollaborativeChunkPermissionService(
                self,
                _seal=_PERMISSION_SERVICE_CONSTRUCTOR_SEAL,
                mutation_permit=permit,
                project_id=self.__project_id,
                workspace_universe_provider=workspace_universe_provider,
            )
        except Exception:
            with self.__lock:
                if self.__permission_permit is permit:
                    self.__permission_permit = None
            raise

    def _validate_topology_permit(
        self,
        permit: _TopologyPublicationPermit,
    ) -> None:
        if (
            type(permit) is not _TopologyPublicationPermit
            or permit is not self.__topology_permit
        ):
            _fail("CHUNK.MANAGER_REQUIRED")

    def _validate_assignment_permit(
        self,
        permit: _AssignmentPublicationPermit,
    ) -> None:
        if (
            type(permit) is not _AssignmentPublicationPermit
            or permit is not self.__assignment_permit
        ):
            _fail("CHUNK.MANAGER_REQUIRED")

    def _validate_permission_permit(
        self,
        permit: _PermissionMutationPermit,
    ) -> None:
        if (
            type(permit) is not _PermissionMutationPermit
            or permit is not self.__permission_permit
        ):
            _fail("CHUNK.PERMISSION_STALE")

    def perform_permission_edit(
        self,
        permit: _PermissionMutationPermit,
        mutation_port: ChunkSegmentEditMutationPort,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        segment: ChunkSegmentRef,
    ) -> object:
        self._validate_permission_permit(permit)
        method = getattr(mutation_port, "apply_segment_edit", None)
        if not callable(method):
            _fail("CHUNK.PERMISSION_STALE")
        if type(workspace_binding) is not ChunkWorkspaceBinding:
            _fail("CHUNK.PERMISSION_STALE")
        workspace_binding.__post_init__()
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail("CHUNK.PERMISSION_STALE")
        expected_plan_binding.__post_init__()
        if type(segment) is not ChunkSegmentRef:
            _fail("CHUNK.PERMISSION_STALE")
        segment.__post_init__()
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            snapshot = self.__snapshot
            if (
                snapshot is None
                or chunk_plan_binding(snapshot) != expected_plan_binding
                or workspace_binding
                != _current_workspace_binding(self.__workspace_binding_provider)
                or workspace_binding.project_id != self.__project_id
                or segment.project_id != self.__project_id
            ):
                _fail("CHUNK.PERMISSION_STALE")
            try:
                return method(
                    _copy_members((segment,))[0],
                    _copy_workspace_binding(workspace_binding),
                )
            except Exception:
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None

    def issue_manager_capability(
        self,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> ChunkManagerCapability:
        return self.__capability_service.issue_manager_capability(
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )

    def revalidate_manager_capability(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> AssigneeRef:
        return self.__capability_service.revalidate_manager_capability(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )

    def consume_manager_capability_with_publication(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        publication: Callable[[AssigneeRef], None],
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> AssigneeRef:
        return self.__capability_service.consume_manager_capability_with_publication(
            capability,
            manager,
            publication,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )

    def publish_atomic(
        self,
        permit: _TopologyPublicationPermit | _AssignmentPublicationPermit,
        expected_binding: ChunkPlanBinding | None,
        candidate: ChunkPlanSnapshot | None,
        retired_additions: tuple[str, ...],
        receipt: ChunkOperationReceipt,
        rebase_intent_digest: str | None = None,
        rebase_workspace_binding: ChunkWorkspaceBinding | None = None,
    ) -> ChunkTopologyPublicationResult:
        topology_permit = (
            type(permit) is _TopologyPublicationPermit
            and permit is self.__topology_permit
        )
        assignment_permit = (
            type(permit) is _AssignmentPublicationPermit
            and permit is self.__assignment_permit
        )
        if not topology_permit and not assignment_permit:
            _fail("CHUNK.MANAGER_REQUIRED")
        if candidate is not None:
            candidate = _copy_snapshot(validate_chunk_plan_snapshot(candidate))
            if not candidate.chunks:
                _fail("CHUNK.METADATA_INVALID")
        if type(retired_additions) is not tuple:
            _fail("CHUNK.CONTRACT_INVALID")
        additions = tuple(validate_chunk_id(value) for value in retired_additions)
        if len(additions) != len(set(additions)):
            _fail("CHUNK.METADATA_INVALID")
        receipt = _copy_receipt(validate_chunk_operation_receipt(receipt))
        assignment_actions = {
            TopologyAction.ASSIGN,
            TopologyAction.REASSIGN,
            TopologyAction.UNASSIGN,
        }
        if (
            (topology_permit and receipt.action in assignment_actions)
            or (assignment_permit and receipt.action not in assignment_actions)
        ):
            _fail("CHUNK.MANAGER_REQUIRED")
        with self.__lock:
            if self.__recovery_required:
                _fail("CHUNK.RECOVERY_REQUIRED")
            current = self.__snapshot
            current_binding = None if current is None else chunk_plan_binding(current)
            if current_binding != expected_binding:
                _fail("CHUNK.PREVIEW_STALE")
            if current is None:
                if candidate is None or candidate.project_id != self.__project_id:
                    _fail("CHUNK.CONTRACT_INVALID")
                if candidate.chunk_plan_id in self.__used_plan_ids:
                    _fail("CHUNK.IDENTITY_DUPLICATE")
                base_revision = 0
                before_digest = EMPTY_CHUNK_AUDIT_DIGEST
                current_chunk_ids: set[str] = set()
            else:
                if candidate is not None and (
                    candidate.project_id != self.__project_id
                    or candidate.chunk_plan_id != current.chunk_plan_id
                ):
                    _fail("CHUNK.IDENTITY_FOREIGN")
                base_revision = current.revision
                before_digest = chunk_plan_digest_v1(current)
                current_chunk_ids = {chunk.chunk_id for chunk in current.chunks}
            if not set(additions).issubset(current_chunk_ids):
                _fail("CHUNK.METADATA_INVALID")
            if set(additions) & self.__retired:
                _fail("CHUNK.METADATA_INVALID")
            final_retired = self.__retired | set(additions)
            if candidate is not None and any(
                chunk.chunk_id in final_retired for chunk in candidate.chunks
            ):
                _fail("CHUNK.METADATA_INVALID")
            after_digest = (
                DISSOLVED_CHUNK_PLAN_DIGEST
                if candidate is None
                else chunk_plan_digest_v1(candidate)
            )
            published_revision = (
                base_revision + 1 if candidate is None else candidate.revision
            )
            plan_id = (
                current.chunk_plan_id if candidate is None else candidate.chunk_plan_id
            )
            if (
                receipt.project_id != self.__project_id
                or receipt.chunk_plan_id != plan_id
                or receipt.base_revision != base_revision
                or receipt.published_revision != published_revision
                or receipt.before_plan_digest != before_digest
                or receipt.after_plan_digest != after_digest
                or set(receipt.retired_chunk_ids) != set(additions)
                or receipt.retired_chunk_count != len(additions)
            ):
                _fail("CHUNK.METADATA_INVALID")
            if candidate is not None and (
                candidate.revision != base_revision + 1
                or candidate.audit_head_digest != receipt.audit_record_digest
            ):
                _fail("CHUNK.METADATA_INVALID")
            if assignment_permit:
                if current is None or candidate is None or additions:
                    _fail("CHUNK.METADATA_INVALID")
                validate_assignment_plan_successor(current, candidate, receipt)
            elif receipt.action is TopologyAction.REBASE:
                if (
                    current is None
                    or candidate is None
                    or self.__metadata_store is None
                    or type(rebase_intent_digest) is not str
                    or type(rebase_workspace_binding) is not ChunkWorkspaceBinding
                ):
                    _fail("CHUNK.REBASE_REQUIRED")
                intent = self.__metadata_store.load_rebase_intent()
                if (
                    intent is None
                    or intent.intent_digest != rebase_intent_digest
                    or intent.plan_binding != current_binding
                    or candidate.segment_universe_digest
                    != intent.transition.current.binding.segment_universe_digest
                ):
                    _fail("CHUNK.REBASE_REQUIRED")
                live_workspace = _current_workspace_binding(
                    self.__workspace_binding_provider
                )
                if (
                    live_workspace != rebase_workspace_binding
                    or live_workspace.project_id != self.__project_id
                    or live_workspace.segment_universe_digest
                    != candidate.segment_universe_digest
                    or (
                        live_workspace.workspace_session_id
                        == intent.transition.current.binding.workspace_session_id
                        and live_workspace.workspace_composition_revision
                        != intent.transition.current.binding.workspace_composition_revision
                    )
                    or (
                        live_workspace.workspace_session_id
                        != intent.transition.current.binding.workspace_session_id
                        and live_workspace.workspace_composition_revision != 0
                    )
                ):
                    _fail("CHUNK.PREVIEW_STALE")
                validate_rebase_plan_successor(current, candidate, receipt)
            else:
                if (
                    rebase_intent_digest is not None
                    or rebase_workspace_binding is not None
                ):
                    _fail("CHUNK.METADATA_INVALID")
                validate_topology_assignment_successor(current, candidate, receipt)
            if len(self.__receipts) >= MAX_AUDIT_RECORDS:
                _fail("CHUNK.LIMIT_EXCEEDED")
            next_snapshot = (
                None if candidate is None else _copy_snapshot(candidate)
            )
            next_retired = frozenset(final_retired)
            next_used = self.__used_plan_ids | {plan_id}
            next_receipts = self.__receipts + (_copy_receipt(receipt),)
            next_metadata_state: ChunkMetadataState | None = None
            if self.__metadata_store is not None:
                next_metadata_state = build_next_chunk_metadata_state(
                    self.__metadata_state,
                    project_id=self.__project_id,
                    active_snapshot=next_snapshot,
                    retired_chunk_ids=tuple(sorted(next_retired)),
                    used_chunk_plan_ids=tuple(sorted(next_used)),
                    receipt=receipt,
                )
            result = ChunkTopologyPublicationResult(
                snapshot=(
                    None
                    if next_snapshot is None
                    else _copy_snapshot(next_snapshot)
                ),
                retired_chunk_ids=tuple(sorted(next_retired)),
            )
            next_metadata_digest = self.__metadata_digest
            if self.__metadata_store is not None:
                assert next_metadata_state is not None
                try:
                    publication = self.__metadata_store.publish(
                        next_metadata_state,
                        expected_metadata_digest=self.__metadata_digest,
                        expected_rebase_intent_digest=rebase_intent_digest,
                    )
                except ChunkError as error:
                    if error.code in {
                        "CHUNK.RECOVERY_REQUIRED",
                        "CHUNK.DESTINATION_STALE",
                    }:
                        self.__recovery_required = True
                    raise
                next_metadata_digest = publication.metadata_digest
            next_snapshot_binding = (
                None
                if next_snapshot is None
                else chunk_plan_binding(next_snapshot)
            )
            # Single no-fail state swap.  No validation/callback/allocation may
            # occur between the first assignment and return.
            self.__snapshot = next_snapshot
            self.__snapshot_binding = next_snapshot_binding
            self.__retired = next_retired
            self.__used_plan_ids = next_used
            self.__receipts = next_receipts
            self.__metadata_state = next_metadata_state
            self.__metadata_digest = next_metadata_digest
            return result

    def publish_assignment_atomic(
        self,
        permit: _AssignmentPublicationPermit,
        expected_binding: ChunkPlanBinding,
        candidate: ChunkPlanSnapshot,
        receipt: ChunkOperationReceipt,
    ) -> ChunkTopologyPublicationResult:
        self._validate_assignment_permit(permit)
        return self.publish_atomic(
            permit,
            expected_binding,
            candidate,
            (),
            receipt,
        )


class CollaborativeChunkTopologyService:
    """Owner service for C1 two-phase, topology-only plan mutations.

    Publication is delegated only to the sealed Chunk owner authority.  The
    service never opens a ProjectPackage or metadata path and never mutates
    workspace content.
    """

    __slots__ = (
        "__publication_authority",
        "__publication_permit",
        "__workspace_universe_provider",
        "__workspace_transition_provider",
        "__project_id",
        "__chunk_id_issuer",
        "__plan_id_issuer",
        "__operation_id_issuer",
        "__prepared",
        "__pending_by_capability",
        "__lock",
    )

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> CollaborativeChunkTopologyService:
        if kwargs.get("_seal") is not _TOPOLOGY_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        return super().__new__(cls)

    def __init__(
        self,
        publication_authority: ChunkTopologyPublicationAuthority,
        *,
        _seal: object,
        publication_permit: _TopologyPublicationPermit,
        project_id: str,
        workspace_universe_provider: Callable[
            [], ChunkWorkspaceUniverseProjection
        ],
        workspace_transition_provider: Callable[
            [], ChunkPublishedWorkspaceTransition
        ] | None = None,
        chunk_id_issuer: Callable[[], str] = issue_chunk_id,
        plan_id_issuer: Callable[[], str] = issue_chunk_plan_id,
        operation_id_issuer: Callable[[], str] = issue_chunk_operation_id,
    ) -> None:
        if _seal is not _TOPOLOGY_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        callables = (
            workspace_universe_provider,
            chunk_id_issuer,
            plan_id_issuer,
            operation_id_issuer,
        )
        if any(not callable(value) for value in callables):
            _fail("CHUNK.CONTRACT_INVALID")
        if workspace_transition_provider is not None and not callable(
            workspace_transition_provider
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        if type(publication_authority) is not ChunkTopologyPublicationAuthority:
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            validated_project_id = validate_project_id(project_id)
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        if publication_authority.project_id != validated_project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        publication_authority._validate_topology_permit(publication_permit)
        self.__publication_authority = publication_authority
        self.__publication_permit = publication_permit
        self.__workspace_universe_provider = workspace_universe_provider
        self.__workspace_transition_provider = workspace_transition_provider
        self.__project_id = validated_project_id
        self.__chunk_id_issuer = chunk_id_issuer
        self.__plan_id_issuer = plan_id_issuer
        self.__operation_id_issuer = operation_id_issuer
        self.__prepared: dict[str, _PreparedTopologyPlan] = {}
        self.__pending_by_capability: dict[int, str] = {}
        self.__lock = Lock()

    def _current_universe(
        self,
    ) -> tuple[
        ChunkWorkspaceBinding,
        tuple[ChunkUniverseEntry, ...],
        dict[tuple[str, str, str], ChunkUniverseEntry],
    ]:
        try:
            projection = self.__workspace_universe_provider()
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if type(projection) is not ChunkWorkspaceUniverseProjection:
            _fail("CHUNK.CONTRACT_INVALID")
        projection.__post_init__()
        values = projection.entries
        copied: list[ChunkUniverseEntry] = []
        for value in values:
            if type(value) is not ChunkUniverseEntry:
                _fail("CHUNK.CONTRACT_INVALID")
            value.__post_init__()
            copied.append(
                ChunkUniverseEntry(
                    segment=_copy_members((value.segment,))[0],
                    source_presence=value.source_presence,
                )
            )
        entries = tuple(copied)
        by_key = {_member_key(entry.segment): entry for entry in entries}
        if len(by_key) != len(entries):
            _fail("CHUNK.MEMBER_DUPLICATE")
        return _copy_workspace_binding(projection.binding), entries, by_key

    def _load_context(
        self,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
        stale_code: str,
    ) -> tuple[
        ChunkPlanSnapshot | None,
        dict[tuple[str, str, str], ChunkUniverseEntry],
        frozenset[str],
    ]:
        if type(workspace_binding) is not ChunkWorkspaceBinding:
            _fail("CHUNK.CONTRACT_INVALID")
        workspace_binding.__post_init__()
        if workspace_binding.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        if type(action) is not TopologyAction:
            _fail("CHUNK.CONTRACT_INVALID")
        current_workspace_binding, _, universe = self._current_universe()
        if workspace_binding != current_workspace_binding:
            _fail(stale_code)
        universe_digest = current_workspace_binding.segment_universe_digest
        snapshot = self.__publication_authority.current_snapshot()
        retired = frozenset(self.__publication_authority.retired_chunk_ids())
        if snapshot is None:
            if action is not TopologyAction.CREATE or expected_plan_binding is not None:
                _fail(stale_code)
            return None, universe, retired
        snapshot = validate_chunk_plan_snapshot(snapshot)
        if not snapshot.chunks:
            _fail("CHUNK.METADATA_INVALID")
        current_binding = chunk_plan_binding(snapshot)
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail(stale_code)
        expected_plan_binding.__post_init__()
        binding_mismatch = (
            expected_plan_binding != current_binding
            or current_binding.project_id != self.__project_id
            or workspace_binding.segment_universe_digest != universe_digest
        )
        universe_mismatch = (
            current_binding.segment_universe_digest != universe_digest
        )
        if binding_mismatch or (
            universe_mismatch
            and action not in {TopologyAction.REBASE, TopologyAction.DISSOLVE_PLAN}
        ):
            _fail(stale_code)
        if any(chunk.chunk_id in retired for chunk in snapshot.chunks):
            _fail("CHUNK.METADATA_INVALID")
        if action not in {TopologyAction.REBASE, TopologyAction.DISSOLVE_PLAN}:
            for chunk in snapshot.chunks:
                if any(_member_key(member) not in universe for member in chunk.members):
                    _fail("CHUNK.REBASE_REQUIRED")
        return _copy_snapshot(snapshot), universe, retired

    def _authorize_and_load(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        action: TopologyAction,
    ) -> tuple[
        ChunkPlanSnapshot | None,
        dict[tuple[str, str, str], ChunkUniverseEntry],
        frozenset[str],
        AssigneeRef,
    ]:
        snapshot, universe, retired = self._load_context(
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
            stale_code="CHUNK.REVISION_STALE",
        )
        actor_ref = self.__publication_authority.revalidate_manager_capability(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )
        return snapshot, universe, retired, actor_ref

    @staticmethod
    def _find_chunk(
        snapshot: ChunkPlanSnapshot,
        chunk_id: object,
        retired: frozenset[str],
    ) -> CollaborativeChunk:
        validated_id = validate_chunk_id(chunk_id)
        if validated_id in retired:
            _fail("CHUNK.REVISION_STALE")
        for chunk in snapshot.chunks:
            if chunk.chunk_id == validated_id:
                return chunk
        _fail("CHUNK.IDENTITY_FOREIGN")

    def _issue_chunk_id(
        self,
        active_ids: set[str],
        retired: frozenset[str],
    ) -> str:
        try:
            issued = validate_chunk_id(self.__chunk_id_issuer())
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if issued in active_ids or issued in retired:
            _fail("CHUNK.IDENTITY_DUPLICATE")
        active_ids.add(issued)
        return issued

    def _issue_plan_id(self) -> str:
        try:
            plan_id = validate_chunk_plan_id(self.__plan_id_issuer())
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if plan_id in self.__publication_authority.used_chunk_plan_ids():
            _fail("CHUNK.IDENTITY_DUPLICATE")
        return plan_id

    def _issue_operation_id(self) -> str:
        try:
            operation_id = validate_chunk_operation_id(
                self.__operation_id_issuer()
            )
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if operation_id in self.__prepared:
            _fail("CHUNK.IDENTITY_DUPLICATE")
        return operation_id

    @staticmethod
    def _candidate_snapshot(
        current: ChunkPlanSnapshot | None,
        *,
        chunk_plan_id: str,
        project_id: str,
        segment_universe_digest: str,
        chunks: tuple[CollaborativeChunk, ...],
    ) -> ChunkPlanSnapshot:
        candidate = ChunkPlanSnapshot(
            schema_version=1,
            namespace=CHUNK_METADATA_NAMESPACE,
            chunk_plan_id=chunk_plan_id,
            project_id=project_id,
            revision=1 if current is None else current.revision + 1,
            segment_universe_digest=segment_universe_digest,
            chunks=_dense_chunks(chunks),
            audit_head_digest=(
                EMPTY_CHUNK_AUDIT_DIGEST
                if current is None
                else current.audit_head_digest
            ),
        )
        return validate_chunk_plan_snapshot(candidate)

    def _register(
        self,
        *,
        action: TopologyAction,
        current: ChunkPlanSnapshot | None,
        candidate: ChunkPlanSnapshot | None,
        actor_ref: AssigneeRef,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        affected_chunk_ids: tuple[str, ...],
        created_chunk_ids: tuple[str, ...],
        retired_chunk_ids: tuple[str, ...],
        retired_baseline: frozenset[str],
        affected_member_count: int,
        assignment_count: int = 0,
        rebase_intent_digest: str | None = None,
    ) -> ChunkMutationPreview:
        if current is None and candidate is None:
            _fail("CHUNK.CONTRACT_INVALID")
        if (action is TopologyAction.REBASE) != (
            type(rebase_intent_digest) is str
        ):
            _fail("CHUNK.REBASE_REQUIRED")
        operation_id = self._issue_operation_id()
        plan_id = (
            candidate.chunk_plan_id if candidate is not None else current.chunk_plan_id
        )
        base_revision = 0 if current is None else current.revision
        published_revision = (
            candidate.revision if candidate is not None else base_revision + 1
        )
        before_digest = (
            EMPTY_CHUNK_AUDIT_DIGEST
            if current is None
            else chunk_plan_digest_v1(current)
        )
        after_digest = (
            DISSOLVED_CHUNK_PLAN_DIGEST
            if candidate is None
            else chunk_plan_digest_v1(candidate)
        )
        preview = validate_chunk_mutation_preview(
            ChunkMutationPreview(
                operation_id=operation_id,
                action=action,
                project_id=self.__project_id,
                chunk_plan_id=plan_id,
                base_revision=base_revision,
                published_revision=published_revision,
                before_plan_digest=before_digest,
                after_plan_digest=after_digest,
                affected_chunk_ids=affected_chunk_ids,
                created_chunk_ids=created_chunk_ids,
                retired_chunk_ids=retired_chunk_ids,
                affected_chunk_count=len(affected_chunk_ids),
                created_chunk_count=len(created_chunk_ids),
                retired_chunk_count=len(retired_chunk_ids),
                affected_member_count=affected_member_count,
                assignment_count=assignment_count,
                warnings=(),
                blockers=(),
                truncated=False,
            )
        )
        previous_audit_head = (
            EMPTY_CHUNK_AUDIT_DIGEST
            if current is None
            else current.audit_head_digest
        )
        audit_digest = chunk_operation_audit_digest_v1(
            preview,
            actor_ref,
            previous_audit_head,
        )
        if candidate is not None:
            candidate = validate_chunk_plan_snapshot(
                replace(candidate, audit_head_digest=audit_digest)
            )
            if chunk_plan_digest_v1(candidate) != preview.after_plan_digest:
                _fail("CHUNK.CONTRACT_INVALID")
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
                assignment_count=assignment_count,
                actor_ref=_copy_actor_ref(actor_ref),
                safe_issues=(),
                truncated=False,
                audit_record_digest=audit_digest,
            )
        )
        if action is TopologyAction.REBASE:
            if current is None or candidate is None:
                _fail("CHUNK.REBASE_DECISION_REQUIRED")
            validate_rebase_plan_successor(current, candidate, receipt)
        record = _PreparedTopologyPlan(
            preview=_copy_preview(preview),
            receipt=_copy_receipt(receipt),
            candidate=(None if candidate is None else _copy_snapshot(candidate)),
            expected_plan_binding=_copy_plan_binding(expected_plan_binding),
            workspace_binding=_copy_workspace_binding(workspace_binding),
            retired_baseline=frozenset(retired_baseline),
            retired_additions=tuple(retired_chunk_ids),
            capability=capability,
            manager=manager,
            rebase_intent_digest=rebase_intent_digest,
        )
        with self.__lock:
            if operation_id in self.__prepared:
                _fail("CHUNK.IDENTITY_DUPLICATE")
            previous_operation_id = self.__pending_by_capability.get(id(capability))
            if previous_operation_id is not None:
                previous = self.__prepared.pop(previous_operation_id, None)
                if previous is not None:
                    previous.state = "terminal"
            if len(self.__prepared) >= MAX_ACTIVE_CHUNKS:
                _fail("CHUNK.LIMIT_EXCEEDED")
            self.__prepared[operation_id] = record
            self.__pending_by_capability[id(capability)] = operation_id
        return _copy_preview(preview)

    def unallocated_members(
        self,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
    ) -> tuple[ChunkSegmentRef, ...]:
        action = TopologyAction.CREATE
        snapshot, universe, _ = self._load_context(
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
            stale_code="CHUNK.REVISION_STALE",
        )
        allocated = {
            _member_key(member)
            for chunk in (() if snapshot is None else snapshot.chunks)
            for member in chunk.members
        }
        available = tuple(
            entry.segment
            for key, entry in universe.items()
            if entry.source_presence is SourcePresence.ATTACHED
            and key not in allocated
        )
        return _copy_members(canonicalize_chunk_members(available, allow_empty=True))

    def preview_create(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        name: str,
        members: tuple[ChunkSegmentRef, ...],
        assignee: AssigneeRef | None = None,
    ) -> ChunkMutationPreview:
        if assignee is not None:
            _fail("CHUNK.CONTRACT_INVALID")
        validate_chunk_name(name)
        requested = canonicalize_chunk_members(members)
        current, universe, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.CREATE,
        )
        allocated = {
            _member_key(member)
            for chunk in (() if current is None else current.chunks)
            for member in chunk.members
        }
        for member in requested:
            if member.project_id != self.__project_id:
                _fail("CHUNK.IDENTITY_FOREIGN")
            entry = universe.get(_member_key(member))
            if entry is None:
                _fail("CHUNK.MEMBER_UNKNOWN")
            if (
                entry.source_presence is not SourcePresence.ATTACHED
                or _member_key(member) in allocated
            ):
                _fail("CHUNK.MEMBER_UNALLOCATED_REQUIRED")
        active_ids = set() if current is None else {
            chunk.chunk_id for chunk in current.chunks
        }
        if current is not None and len(current.chunks) >= MAX_ACTIVE_CHUNKS:
            _fail("CHUNK.LIMIT_EXCEEDED")
        plan_id = current.chunk_plan_id if current is not None else self._issue_plan_id()
        chunk_id = self._issue_chunk_id(active_ids, retired)
        chunks = (() if current is None else current.chunks) + (
            CollaborativeChunk(
                chunk_id=chunk_id,
                name=name,
                order=0 if current is None else len(current.chunks),
                members=_copy_members(requested),
                assignee=None,
            ),
        )
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=plan_id,
            project_id=self.__project_id,
            segment_universe_digest=workspace_binding.segment_universe_digest,
            chunks=chunks,
        )
        return self._register(
            action=TopologyAction.CREATE,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=(chunk_id,),
            created_chunk_ids=(chunk_id,),
            retired_chunk_ids=(),
            retired_baseline=retired,
            affected_member_count=len(requested),
        )

    def preview_create_plan(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        children: tuple[ChunkSplitChild, ...],
    ) -> ChunkMutationPreview:
        """Atomically partition one unplanned project into dynamic chunks."""

        if (
            type(children) is not tuple
            or len(children) < 2
            or len(children) > MAX_ACTIVE_CHUNKS
        ):
            _fail("CHUNK.SPLIT_INVALID")
        for child in children:
            if type(child) is not ChunkSplitChild:
                _fail("CHUNK.SPLIT_INVALID")
            child.__post_init__()
            if child.assignee is not None:
                _fail("CHUNK.CONTRACT_INVALID")
            if any(
                member.project_id != self.__project_id
                for member in child.members
            ):
                _fail("CHUNK.IDENTITY_FOREIGN")
        current, universe, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.CREATE,
        )
        if current is not None:
            _fail("CHUNK.REVISION_STALE")
        child_keys = [
            {_member_key(member) for member in child.members}
            for child in children
        ]
        flattened = [key for keys in child_keys for key in keys]
        attached = {
            key
            for key, entry in universe.items()
            if entry.source_presence is SourcePresence.ATTACHED
        }
        if (
            len(flattened) != len(set(flattened))
            or set(flattened) != attached
        ):
            _fail("CHUNK.SPLIT_INVALID")
        active_ids: set[str] = set()
        created_chunks: list[CollaborativeChunk] = []
        for order, child in enumerate(children):
            created_chunks.append(
                CollaborativeChunk(
                    chunk_id=self._issue_chunk_id(active_ids, retired),
                    name=child.name,
                    order=order,
                    members=_copy_members(child.members),
                    assignee=None,
                )
            )
        plan_id = self._issue_plan_id()
        candidate = self._candidate_snapshot(
            None,
            chunk_plan_id=plan_id,
            project_id=self.__project_id,
            segment_universe_digest=workspace_binding.segment_universe_digest,
            chunks=tuple(created_chunks),
        )
        created_ids = tuple(chunk.chunk_id for chunk in created_chunks)
        return self._register(
            action=TopologyAction.CREATE,
            current=None,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=None,
            affected_chunk_ids=created_ids,
            created_chunk_ids=created_ids,
            retired_chunk_ids=(),
            retired_baseline=retired,
            affected_member_count=len(flattened),
        )

    def preview_rename(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
        name: str,
    ) -> ChunkMutationPreview:
        validate_chunk_name(name)
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.RENAME,
        )
        assert current is not None
        source = self._find_chunk(current, chunk_id, retired)
        if name == source.name:
            _fail("CHUNK.CONTRACT_INVALID")
        chunks = tuple(
            CollaborativeChunk(
                chunk_id=chunk.chunk_id,
                name=name if chunk.chunk_id == source.chunk_id else chunk.name,
                order=chunk.order,
                members=_copy_members(chunk.members),
                assignee=chunk.assignee,
            )
            for chunk in current.chunks
        )
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=chunks,
        )
        return self._register(
            action=TopologyAction.RENAME,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=(source.chunk_id,),
            created_chunk_ids=(),
            retired_chunk_ids=(),
            retired_baseline=retired,
            affected_member_count=len(source.members),
        )

    def preview_reorder(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        ordered_chunk_ids: tuple[str, ...],
    ) -> ChunkMutationPreview:
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.REORDER,
        )
        assert current is not None
        if type(ordered_chunk_ids) is not tuple:
            _fail("CHUNK.CONTRACT_INVALID")
        validated = tuple(validate_chunk_id(value) for value in ordered_chunk_ids)
        if len(validated) != len(set(validated)):
            _fail("CHUNK.IDENTITY_DUPLICATE")
        current_ids = tuple(chunk.chunk_id for chunk in current.chunks)
        if set(validated) != set(current_ids) or len(validated) != len(current_ids):
            if any(value in retired for value in validated):
                _fail("CHUNK.REVISION_STALE")
            _fail("CHUNK.IDENTITY_FOREIGN")
        if validated == current_ids:
            _fail("CHUNK.CONTRACT_INVALID")
        by_id = {chunk.chunk_id: chunk for chunk in current.chunks}
        chunks = tuple(by_id[chunk_id] for chunk_id in validated)
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=chunks,
        )
        return self._register(
            action=TopologyAction.REORDER,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=validated,
            created_chunk_ids=(),
            retired_chunk_ids=(),
            retired_baseline=retired,
            affected_member_count=sum(len(chunk.members) for chunk in current.chunks),
        )

    def preview_split(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        source_chunk_id: str,
        children: tuple[ChunkSplitChild, ...],
    ) -> ChunkMutationPreview:
        if type(children) is not tuple or len(children) < 2:
            _fail("CHUNK.SPLIT_INVALID")
        for child in children:
            if type(child) is not ChunkSplitChild:
                _fail("CHUNK.SPLIT_INVALID")
            child.__post_init__()
            if any(
                member.project_id != self.__project_id for member in child.members
            ):
                _fail("CHUNK.IDENTITY_FOREIGN")
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.SPLIT,
        )
        assert current is not None
        source = self._find_chunk(current, source_chunk_id, retired)
        if source.assignee is not None:
            for child in children:
                if not child.assignment_decided:
                    _fail("CHUNK.SPLIT_INVALID")
                if child.assignee not in {None, source.assignee}:
                    _fail("CHUNK.ACTOR_UNVERIFIED")
        elif any(child.assignee is not None for child in children):
            _fail("CHUNK.CONTRACT_INVALID")
        child_keys = [
            {_member_key(member) for member in child.members} for child in children
        ]
        flattened = [key for keys in child_keys for key in keys]
        if (
            len(flattened) != len(set(flattened))
            or set(flattened) != {_member_key(member) for member in source.members}
        ):
            _fail("CHUNK.SPLIT_INVALID")
        if len(current.chunks) - 1 + len(children) > MAX_ACTIVE_CHUNKS:
            _fail("CHUNK.LIMIT_EXCEEDED")
        active_ids = {chunk.chunk_id for chunk in current.chunks}
        created_chunks: list[CollaborativeChunk] = []
        for child in children:
            new_id = self._issue_chunk_id(active_ids, retired)
            created_chunks.append(
                CollaborativeChunk(
                    chunk_id=new_id,
                    name=child.name,
                    order=0,
                    members=_copy_members(child.members),
                    assignee=(
                        None
                        if child.assignee is None
                        else _copy_actor_ref(child.assignee)
                    ),
                )
            )
        chunks = list(current.chunks)
        chunks[source.order : source.order + 1] = created_chunks
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=tuple(chunks),
        )
        created_ids = tuple(chunk.chunk_id for chunk in created_chunks)
        return self._register(
            action=TopologyAction.SPLIT,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=(source.chunk_id,) + created_ids,
            created_chunk_ids=created_ids,
            retired_chunk_ids=(source.chunk_id,),
            retired_baseline=retired,
            affected_member_count=len(source.members),
            assignment_count=sum(
                child.assignee is not None for child in children
            ),
        )

    def preview_merge(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        source_chunk_ids: tuple[str, ...],
        result_name: str,
        result_assignee: AssigneeRef | None = None,
        result_assignment_decided: bool = False,
    ) -> ChunkMutationPreview:
        if type(result_assignment_decided) is not bool:
            _fail("CHUNK.CONTRACT_INVALID")
        if result_assignee is not None:
            if type(result_assignee) is not AssigneeRef:
                _fail("CHUNK.ACTOR_UNVERIFIED")
            result_assignee.__post_init__()
        validate_chunk_name(result_name)
        if type(source_chunk_ids) is not tuple or len(source_chunk_ids) < 2:
            _fail("CHUNK.CONTRACT_INVALID")
        validated_ids = tuple(validate_chunk_id(value) for value in source_chunk_ids)
        if len(validated_ids) != len(set(validated_ids)):
            _fail("CHUNK.IDENTITY_DUPLICATE")
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.MERGE,
        )
        assert current is not None
        sources = tuple(
            sorted(
                (
                    self._find_chunk(current, chunk_id, retired)
                    for chunk_id in validated_ids
                ),
                key=lambda chunk: chunk.order,
            )
        )
        source_assignees = {chunk.assignee for chunk in sources}
        if len(source_assignees) > 1:
            if not result_assignment_decided:
                _fail("CHUNK.MERGE_DECISION_REQUIRED")
            if result_assignee is not None and result_assignee not in source_assignees:
                _fail("CHUNK.ACTOR_UNVERIFIED")
            final_assignee = result_assignee
        else:
            existing_assignee = next(iter(source_assignees))
            if result_assignment_decided:
                if (
                    result_assignee is not None
                    and result_assignee != existing_assignee
                ):
                    _fail("CHUNK.ACTOR_UNVERIFIED")
                final_assignee = result_assignee
            else:
                if result_assignee is not None:
                    _fail("CHUNK.CONTRACT_INVALID")
                final_assignee = existing_assignee
        members = canonicalize_chunk_members(
            tuple(member for chunk in sources for member in chunk.members)
        )
        active_ids = {chunk.chunk_id for chunk in current.chunks}
        result_id = self._issue_chunk_id(active_ids, retired)
        result = CollaborativeChunk(
            chunk_id=result_id,
            name=result_name,
            order=min(chunk.order for chunk in sources),
            members=_copy_members(members),
            assignee=(
                None
                if final_assignee is None
                else _copy_actor_ref(final_assignee)
            ),
        )
        source_set = {chunk.chunk_id for chunk in sources}
        chunks = [
            chunk for chunk in current.chunks if chunk.chunk_id not in source_set
        ]
        chunks.insert(result.order, result)
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=tuple(chunks),
        )
        retired_ids = tuple(chunk.chunk_id for chunk in sources)
        return self._register(
            action=TopologyAction.MERGE,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=retired_ids + (result_id,),
            created_chunk_ids=(result_id,),
            retired_chunk_ids=retired_ids,
            retired_baseline=retired,
            affected_member_count=len(members),
            assignment_count=1 if final_assignee is not None else 0,
        )

    def _preview_move_or_release(
        self,
        action: TopologyAction,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        source_chunk_id: str,
        destination_chunk_id: str | None,
        members: tuple[ChunkSegmentRef, ...],
        retire_source_if_empty: bool,
    ) -> ChunkMutationPreview:
        if type(retire_source_if_empty) is not bool:
            _fail("CHUNK.CONTRACT_INVALID")
        moved = canonicalize_chunk_members(members)
        if any(member.project_id != self.__project_id for member in moved):
            _fail("CHUNK.IDENTITY_FOREIGN")
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )
        assert current is not None
        source = self._find_chunk(current, source_chunk_id, retired)
        destination = None
        if action is TopologyAction.MOVE:
            if destination_chunk_id is None:
                _fail("CHUNK.CONTRACT_INVALID")
            destination = self._find_chunk(current, destination_chunk_id, retired)
            if destination.chunk_id == source.chunk_id:
                _fail("CHUNK.CONTRACT_INVALID")
        elif action is TopologyAction.RELEASE:
            if destination_chunk_id is not None:
                _fail("CHUNK.CONTRACT_INVALID")
        else:
            _fail("CHUNK.CONTRACT_INVALID")
        source_keys = {_member_key(member) for member in source.members}
        moved_keys = {_member_key(member) for member in moved}
        if not moved_keys.issubset(source_keys):
            _fail("CHUNK.MEMBER_UNKNOWN")
        remaining = canonicalize_chunk_members(
            tuple(
                member
                for member in source.members
                if _member_key(member) not in moved_keys
            ),
            allow_empty=True,
        )
        source_is_empty = not remaining
        if source_is_empty != retire_source_if_empty:
            _fail("CHUNK.CONTRACT_INVALID")
        if source_is_empty and destination is None and len(current.chunks) == 1:
            _fail("CHUNK.CONTRACT_INVALID")
        replacement: dict[str, CollaborativeChunk] = {}
        if not source_is_empty:
            replacement[source.chunk_id] = CollaborativeChunk(
                source.chunk_id,
                source.name,
                source.order,
                _copy_members(remaining),
                source.assignee,
            )
        if destination is not None:
            destination_members = canonicalize_chunk_members(
                destination.members + moved
            )
            replacement[destination.chunk_id] = CollaborativeChunk(
                destination.chunk_id,
                destination.name,
                destination.order,
                _copy_members(destination_members),
                destination.assignee,
            )
        chunks = tuple(
            replacement.get(chunk.chunk_id, chunk)
            for chunk in current.chunks
            if not (source_is_empty and chunk.chunk_id == source.chunk_id)
        )
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=chunks,
        )
        retired_ids = (source.chunk_id,) if source_is_empty else ()
        affected = (
            (source.chunk_id,)
            if destination is None
            else (source.chunk_id, destination.chunk_id)
        )
        return self._register(
            action=action,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=affected,
            created_chunk_ids=(),
            retired_chunk_ids=retired_ids,
            retired_baseline=retired,
            affected_member_count=len(moved),
        )

    def preview_move(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        source_chunk_id: str,
        destination_chunk_id: str,
        members: tuple[ChunkSegmentRef, ...],
        retire_source_if_empty: bool,
    ) -> ChunkMutationPreview:
        return self._preview_move_or_release(
            TopologyAction.MOVE,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            source_chunk_id=source_chunk_id,
            destination_chunk_id=destination_chunk_id,
            members=members,
            retire_source_if_empty=retire_source_if_empty,
        )

    def preview_release(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        source_chunk_id: str,
        members: tuple[ChunkSegmentRef, ...],
        retire_source_if_empty: bool,
    ) -> ChunkMutationPreview:
        return self._preview_move_or_release(
            TopologyAction.RELEASE,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            source_chunk_id=source_chunk_id,
            destination_chunk_id=None,
            members=members,
            retire_source_if_empty=retire_source_if_empty,
        )

    def _inspection_from_intent(
        self,
        snapshot: ChunkPlanSnapshot,
        workspace_binding: ChunkWorkspaceBinding,
        universe: dict[tuple[str, str, str], ChunkUniverseEntry],
        intent: ChunkRebaseIntent,
    ) -> ChunkRebaseInspection:
        validated = validate_chunk_rebase_intent(intent)
        if chunk_plan_binding(snapshot) != validated.plan_binding:
            _fail("CHUNK.REBASE_REQUIRED")
        previous = {
            _member_key(entry.segment): entry
            for entry in validated.transition.previous.entries
        }
        current = {
            _member_key(entry.segment): entry
            for entry in validated.transition.current.entries
        }
        captured = validated.transition.current.binding
        if (
            current != universe
            or workspace_binding.project_id != captured.project_id
            or workspace_binding.segment_universe_digest
            != captured.segment_universe_digest
            or (
                workspace_binding.workspace_session_id
                == captured.workspace_session_id
                and workspace_binding.workspace_composition_revision
                != captured.workspace_composition_revision
            )
            or (
                workspace_binding.workspace_session_id
                != captured.workspace_session_id
                and workspace_binding.workspace_composition_revision != 0
            )
        ):
            _fail("CHUNK.REBASE_REQUIRED")
        plan_members = canonicalize_chunk_members(
            tuple(member for chunk in snapshot.chunks for member in chunk.members),
            allow_empty=True,
        )
        if any(_member_key(member) not in previous for member in plan_members):
            _fail("CHUNK.UNIVERSE_MISMATCH")
        retained_attached = []
        retained_detached = []
        missing = []
        for member in plan_members:
            current_entry = current.get(_member_key(member))
            if current_entry is None:
                missing.append(member)
            elif current_entry.source_presence is SourcePresence.ATTACHED:
                retained_attached.append(member)
            else:
                retained_detached.append(member)
        plan_keys = {_member_key(member) for member in plan_members}
        source_changed = tuple(
            member
            for member in validated.transition.source_changed_members
            if _member_key(member) in plan_keys
        )
        new_unallocated = tuple(
            entry.segment
            for key, entry in current.items()
            if key not in previous
        )
        return validate_chunk_rebase_inspection(
            ChunkRebaseInspection(
                intent_digest=validated.intent_digest,
                transition_digest=validated.transition.transition_digest,
                plan_binding=_copy_plan_binding(validated.plan_binding),
                previous_workspace_binding=validated.transition.previous.binding,
                current_workspace_binding=validated.transition.current.binding,
                retained_attached_members=_copy_members(
                    canonicalize_chunk_members(
                        tuple(retained_attached),
                        allow_empty=True,
                    )
                ),
                retained_detached_members=_copy_members(
                    canonicalize_chunk_members(
                        tuple(retained_detached),
                        allow_empty=True,
                    )
                ),
                source_changed_members=_copy_members(
                    canonicalize_chunk_members(
                        source_changed,
                        allow_empty=True,
                    )
                ),
                missing_members=_copy_members(
                    canonicalize_chunk_members(tuple(missing), allow_empty=True)
                ),
                new_unallocated_members=_copy_members(
                    canonicalize_chunk_members(
                        new_unallocated,
                        allow_empty=True,
                    )
                ),
            )
        )

    def inspect_rebase(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkRebaseInspection:
        current, universe, _, _ = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.REBASE,
        )
        assert current is not None
        intent = self.__publication_authority.current_rebase_intent(
            self.__publication_permit
        )
        if intent is None:
            provider = self.__workspace_transition_provider
            if provider is None:
                _fail("CHUNK.REBASE_REQUIRED")
            try:
                transition = provider()
            except ChunkError:
                raise
            except Exception:
                raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
            transition = validate_chunk_published_workspace_transition(transition)
            transition_current = {
                _member_key(entry.segment): entry
                for entry in transition.current.entries
            }
            if (
                transition.current.binding.project_id != self.__project_id
                or transition.current.binding.workspace_session_id
                != workspace_binding.workspace_session_id
                or transition.current.binding.workspace_composition_revision
                != workspace_binding.workspace_composition_revision
                or transition.current.binding.workspace_revision
                > workspace_binding.workspace_revision
                or transition.current.binding.segment_universe_digest
                != workspace_binding.segment_universe_digest
                or transition_current != universe
            ):
                _fail("CHUNK.REBASE_REQUIRED")
            plan_binding = chunk_plan_binding(current)
            digest = chunk_rebase_intent_digest_v1(
                self.__project_id,
                plan_binding,
                transition,
            )
            intent = self.__publication_authority.capture_rebase_intent(
                self.__publication_permit,
                ChunkRebaseIntent(
                    schema=CHUNK_REBASE_INTENT_SCHEMA,
                    project_id=self.__project_id,
                    plan_binding=plan_binding,
                    transition=transition,
                    intent_digest=digest,
                ),
            )
        inspection = self._inspection_from_intent(
            current,
            workspace_binding,
            universe,
            intent,
        )
        return _copy_rebase_inspection(inspection)

    def preview_rebase(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        released_missing_members: tuple[ChunkSegmentRef, ...],
        retire_empty_chunk_ids: tuple[str, ...],
    ) -> ChunkRebasePreview:
        inspection = self.inspect_rebase(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
        )
        released = canonicalize_chunk_members(
            released_missing_members,
            allow_empty=True,
        )
        if released != released_missing_members or released != inspection.missing_members:
            _fail("CHUNK.REBASE_DECISION_REQUIRED")
        current, universe, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.REBASE,
        )
        assert current is not None
        if inspection.plan_binding != chunk_plan_binding(current):
            _fail("CHUNK.PREVIEW_STALE")
        empty_ids = tuple(
            chunk.chunk_id
            for chunk in current.chunks
            if not any(_member_key(member) in universe for member in chunk.members)
        )
        if type(retire_empty_chunk_ids) is not tuple:
            _fail("CHUNK.REBASE_DECISION_REQUIRED")
        retire_decision = tuple(
            validate_chunk_id(value) for value in retire_empty_chunk_ids
        )
        if (
            len(retire_decision) != len(set(retire_decision))
            or retire_decision != empty_ids
            or len(empty_ids) == len(current.chunks)
        ):
            _fail("CHUNK.REBASE_DECISION_REQUIRED")
        chunks = []
        for chunk in current.chunks:
            retained_members = tuple(
                member
                for member in chunk.members
                if _member_key(member) in universe
            )
            if not retained_members:
                continue
            chunks.append(
                CollaborativeChunk(
                    chunk_id=chunk.chunk_id,
                    name=chunk.name,
                    order=chunk.order,
                    members=_copy_members(retained_members),
                    assignee=(
                        None
                        if chunk.assignee is None
                        else _copy_actor_ref(chunk.assignee)
                    ),
                )
            )
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=workspace_binding.segment_universe_digest,
            chunks=tuple(chunks),
        )
        mutation = self._register(
            action=TopologyAction.REBASE,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=tuple(chunk.chunk_id for chunk in current.chunks),
            created_chunk_ids=(),
            retired_chunk_ids=empty_ids,
            retired_baseline=retired,
            affected_member_count=len(released),
            assignment_count=0,
            rebase_intent_digest=inspection.intent_digest,
        )
        return validate_chunk_rebase_preview(
            ChunkRebasePreview(
                inspection=_copy_rebase_inspection(inspection),
                mutation=mutation,
                released_missing_members=_copy_members(released),
                retired_empty_chunk_ids=empty_ids,
            )
        )

    def preview_dissolve_chunk(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
    ) -> ChunkMutationPreview:
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.DISSOLVE_CHUNK,
        )
        assert current is not None
        source = self._find_chunk(current, chunk_id, retired)
        if len(current.chunks) == 1:
            _fail("CHUNK.CONTRACT_INVALID")
        chunks = tuple(
            chunk for chunk in current.chunks if chunk.chunk_id != source.chunk_id
        )
        candidate = self._candidate_snapshot(
            current,
            chunk_plan_id=current.chunk_plan_id,
            project_id=current.project_id,
            segment_universe_digest=current.segment_universe_digest,
            chunks=chunks,
        )
        return self._register(
            action=TopologyAction.DISSOLVE_CHUNK,
            current=current,
            candidate=candidate,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=(source.chunk_id,),
            created_chunk_ids=(),
            retired_chunk_ids=(source.chunk_id,),
            retired_baseline=retired,
            affected_member_count=len(source.members),
        )

    def preview_dissolve_plan(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkMutationPreview:
        current, _, retired, actor = self._authorize_and_load(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=TopologyAction.DISSOLVE_PLAN,
        )
        assert current is not None
        retired_ids = tuple(chunk.chunk_id for chunk in current.chunks)
        return self._register(
            action=TopologyAction.DISSOLVE_PLAN,
            current=current,
            candidate=None,
            actor_ref=actor,
            capability=capability,
            manager=manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            affected_chunk_ids=retired_ids,
            created_chunk_ids=(),
            retired_chunk_ids=retired_ids,
            retired_baseline=retired,
            affected_member_count=sum(len(chunk.members) for chunk in current.chunks),
        )

    @staticmethod
    def reject_assignment_command(*args: object, **kwargs: object) -> None:
        del args, kwargs
        _fail("CHUNK.ASSIGNMENT_UNAVAILABLE")

    preview_assign = reject_assignment_command
    preview_reassign = reject_assignment_command
    preview_unassign = reject_assignment_command

    def _discard_prepared(
        self,
        operation_id: str,
        record: _PreparedTopologyPlan,
    ) -> None:
        if self.__prepared.get(operation_id) is record:
            self.__prepared.pop(operation_id, None)
        capability_key = id(record.capability)
        if self.__pending_by_capability.get(capability_key) == operation_id:
            self.__pending_by_capability.pop(capability_key, None)

    def apply_topology(
        self,
        preview: ChunkMutationPreview,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
    ) -> ChunkOperationReceipt:
        validated_preview = validate_chunk_mutation_preview(preview)
        if validated_preview.action is TopologyAction.REBASE:
            _fail("CHUNK.PREVIEW_STALE")
        with self.__lock:
            record = self.__prepared.get(validated_preview.operation_id)
            if record is None or record.state != "pending":
                _fail("CHUNK.PREVIEW_STALE")
            if (
                record.preview != validated_preview
                or record.capability is not capability
                or record.manager is not manager
                or record.workspace_binding != workspace_binding
                or record.expected_plan_binding != expected_plan_binding
                or record.preview.action is not validated_preview.action
            ):
                _fail("CHUNK.PREVIEW_STALE")
            try:
                _, _, current_retired = self._load_context(
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    action=validated_preview.action,
                    stale_code="CHUNK.PREVIEW_STALE",
                )
                if current_retired != record.retired_baseline:
                    _fail("CHUNK.PREVIEW_STALE")
                if record.candidate is not None and any(
                    chunk.chunk_id in current_retired
                    for chunk in record.candidate.chunks
                ):
                    _fail("CHUNK.PREVIEW_STALE")

                def publish(actor_ref: AssigneeRef) -> None:
                    if actor_ref != record.receipt.actor_ref:
                        _fail("CHUNK.PREVIEW_STALE")
                    record.state = "publishing"
                    result = self.__publication_authority.publish_atomic(
                        self.__publication_permit,
                        _copy_plan_binding(record.expected_plan_binding),
                        (
                            None
                            if record.candidate is None
                            else _copy_snapshot(record.candidate)
                        ),
                        tuple(record.retired_additions),
                        _copy_receipt(record.receipt),
                    )
                    result.__post_init__()
                    expected_retired = record.retired_baseline | set(
                        record.retired_additions
                    )
                    if (
                        result.snapshot != record.candidate
                        or set(result.retired_chunk_ids) != expected_retired
                    ):
                        _fail("CHUNK.RECOVERY_REQUIRED")

                actor_ref = self.__publication_authority.consume_manager_capability_with_publication(
                        capability,
                        manager,
                        publish,
                        workspace_binding=workspace_binding,
                        expected_plan_binding=expected_plan_binding,
                        action=validated_preview.action,
                )
            except ChunkError:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise
            except Exception:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None
            if actor_ref != record.receipt.actor_ref:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                _fail("CHUNK.RECOVERY_REQUIRED")
            record.state = "applied"
            self._discard_prepared(validated_preview.operation_id, record)
            return _copy_receipt(record.receipt)

    def apply_rebase(
        self,
        preview: ChunkRebasePreview,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkOperationReceipt:
        validated = validate_chunk_rebase_preview(preview)
        validated_preview = validated.mutation
        with self.__lock:
            record = self.__prepared.get(validated_preview.operation_id)
            if record is None or record.state != "pending":
                _fail("CHUNK.PREVIEW_STALE")
            if (
                record.preview != validated_preview
                or record.capability is not capability
                or record.manager is not manager
                or record.workspace_binding != workspace_binding
                or record.expected_plan_binding != expected_plan_binding
                or record.preview.action is not TopologyAction.REBASE
                or record.rebase_intent_digest != validated.inspection.intent_digest
            ):
                _fail("CHUNK.PREVIEW_STALE")
            try:
                current, universe, current_retired = self._load_context(
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    action=TopologyAction.REBASE,
                    stale_code="CHUNK.PREVIEW_STALE",
                )
                if current is None or current_retired != record.retired_baseline:
                    _fail("CHUNK.PREVIEW_STALE")
                intent = self.__publication_authority.current_rebase_intent(
                    self.__publication_permit
                )
                if (
                    intent is None
                    or intent.intent_digest != record.rebase_intent_digest
                    or self._inspection_from_intent(
                        current,
                        workspace_binding,
                        universe,
                        intent,
                    )
                    != validated.inspection
                ):
                    _fail("CHUNK.PREVIEW_STALE")
                if record.candidate is None or any(
                    chunk.chunk_id in current_retired
                    for chunk in record.candidate.chunks
                ):
                    _fail("CHUNK.PREVIEW_STALE")

                def publish(actor_ref: AssigneeRef) -> None:
                    if actor_ref != record.receipt.actor_ref:
                        _fail("CHUNK.PREVIEW_STALE")
                    record.state = "publishing"
                    result = self.__publication_authority.publish_atomic(
                        self.__publication_permit,
                        _copy_plan_binding(record.expected_plan_binding),
                        _copy_snapshot(record.candidate),
                        tuple(record.retired_additions),
                        _copy_receipt(record.receipt),
                        rebase_intent_digest=record.rebase_intent_digest,
                        rebase_workspace_binding=_copy_workspace_binding(
                            record.workspace_binding
                        ),
                    )
                    result.__post_init__()
                    expected_retired = record.retired_baseline | set(
                        record.retired_additions
                    )
                    if (
                        result.snapshot != record.candidate
                        or set(result.retired_chunk_ids) != expected_retired
                    ):
                        _fail("CHUNK.RECOVERY_REQUIRED")

                actor_ref = self.__publication_authority.consume_manager_capability_with_publication(
                    capability,
                    manager,
                    publish,
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    action=TopologyAction.REBASE,
                )
            except ChunkError:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise
            except Exception:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None
            if actor_ref != record.receipt.actor_ref:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                _fail("CHUNK.RECOVERY_REQUIRED")
            record.state = "applied"
            self._discard_prepared(validated_preview.operation_id, record)
            return _copy_receipt(record.receipt)


@dataclass(slots=True)
class _PreparedAssignmentPlan:
    preview: ChunkMutationPreview
    receipt: ChunkOperationReceipt
    candidate: ChunkPlanSnapshot
    expected_plan_binding: ChunkPlanBinding
    workspace_binding: ChunkWorkspaceBinding
    retired_baseline: frozenset[str]
    capability: ChunkManagerCapability
    manager: LocalReferenceManagerHandle
    target_actor_port: AuthenticatedActorPort | None
    target_actor_handle: AuthenticatedActorHandle | None
    target_actor_ref: AssigneeRef | None
    state: str = "pending"


class CollaborativeChunkAssignmentService:
    """Two-phase assignment owner; it never grants target-edit authority."""

    __slots__ = (
        "__publication_authority",
        "__publication_permit",
        "__project_id",
        "__operation_id_issuer",
        "__prepared",
        "__pending_by_capability",
        "__lock",
    )

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> CollaborativeChunkAssignmentService:
        if kwargs.get("_seal") is not _ASSIGNMENT_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        return super().__new__(cls)

    def __init__(
        self,
        publication_authority: ChunkTopologyPublicationAuthority,
        *,
        _seal: object,
        publication_permit: _AssignmentPublicationPermit,
        project_id: str,
        operation_id_issuer: Callable[[], str] = issue_chunk_operation_id,
    ) -> None:
        if _seal is not _ASSIGNMENT_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.MANAGER_REQUIRED")
        if type(publication_authority) is not ChunkTopologyPublicationAuthority:
            _fail("CHUNK.CONTRACT_INVALID")
        if not callable(operation_id_issuer):
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            validated_project_id = validate_project_id(project_id)
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        if publication_authority.project_id != validated_project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        publication_authority._validate_assignment_permit(publication_permit)
        self.__publication_authority = publication_authority
        self.__publication_permit = publication_permit
        self.__project_id = validated_project_id
        self.__operation_id_issuer = operation_id_issuer
        self.__prepared: dict[str, _PreparedAssignmentPlan] = {}
        self.__pending_by_capability: dict[int, str] = {}
        self.__lock = Lock()

    @staticmethod
    def _resolve_actor(
        port: AuthenticatedActorPort,
        handle: AuthenticatedActorHandle,
    ) -> AssigneeRef:
        return _resolve_authenticated_actor(port, handle)

    def _issue_operation_id(self) -> str:
        try:
            operation_id = validate_chunk_operation_id(self.__operation_id_issuer())
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if operation_id in self.__prepared:
            _fail("CHUNK.IDENTITY_DUPLICATE")
        return operation_id

    def _authorize_current(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        action: TopologyAction,
        stale_code: str,
    ) -> tuple[ChunkPlanSnapshot, frozenset[str], AssigneeRef]:
        if type(expected_plan_binding) is not ChunkPlanBinding:
            _fail(stale_code)
        expected_plan_binding.__post_init__()
        actor_ref = self.__publication_authority.revalidate_manager_capability(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
        )
        snapshot = self.__publication_authority.current_snapshot()
        if snapshot is None:
            _fail(stale_code)
        snapshot = validate_chunk_plan_snapshot(snapshot)
        if chunk_plan_binding(snapshot) != expected_plan_binding:
            _fail(stale_code)
        retired = frozenset(self.__publication_authority.retired_chunk_ids())
        if any(chunk.chunk_id in retired for chunk in snapshot.chunks):
            _fail("CHUNK.METADATA_INVALID")
        return _copy_snapshot(snapshot), retired, actor_ref

    @staticmethod
    def _find_chunk(
        snapshot: ChunkPlanSnapshot,
        chunk_id: str,
        retired: frozenset[str],
    ) -> CollaborativeChunk:
        validated_id = validate_chunk_id(chunk_id)
        if validated_id in retired:
            _fail("CHUNK.REVISION_STALE")
        for chunk in snapshot.chunks:
            if chunk.chunk_id == validated_id:
                return chunk
        _fail("CHUNK.IDENTITY_FOREIGN")

    def _preview_assignment(
        self,
        action: TopologyAction,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
        target_actor_port: AuthenticatedActorPort | None,
        target_actor_handle: AuthenticatedActorHandle | None,
    ) -> ChunkMutationPreview:
        if action not in {
            TopologyAction.ASSIGN,
            TopologyAction.REASSIGN,
            TopologyAction.UNASSIGN,
        }:
            _fail("CHUNK.CONTRACT_INVALID")
        current, retired, manager_ref = self._authorize_current(
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            action=action,
            stale_code="CHUNK.REVISION_STALE",
        )
        chunk = self._find_chunk(current, chunk_id, retired)
        if action is TopologyAction.UNASSIGN:
            if target_actor_port is not None or target_actor_handle is not None:
                _fail("CHUNK.CONTRACT_INVALID")
            if chunk.assignee is None:
                _fail("CHUNK.CONTRACT_INVALID")
            next_assignee = None
        else:
            if target_actor_port is None or target_actor_handle is None:
                _fail("CHUNK.ACTOR_UNAVAILABLE")
            next_assignee = self._resolve_actor(
                target_actor_port,
                target_actor_handle,
            )
            if action is TopologyAction.ASSIGN:
                if chunk.assignee is not None:
                    _fail("CHUNK.CONTRACT_INVALID")
            elif chunk.assignee is None or chunk.assignee == next_assignee:
                _fail("CHUNK.CONTRACT_INVALID")

        candidate = validate_chunk_plan_snapshot(
            ChunkPlanSnapshot(
                schema_version=current.schema_version,
                namespace=current.namespace,
                chunk_plan_id=current.chunk_plan_id,
                project_id=current.project_id,
                revision=current.revision + 1,
                segment_universe_digest=current.segment_universe_digest,
                chunks=tuple(
                    CollaborativeChunk(
                        chunk_id=value.chunk_id,
                        name=value.name,
                        order=value.order,
                        members=_copy_members(value.members),
                        assignee=(
                            next_assignee
                            if value.chunk_id == chunk.chunk_id
                            else (
                                None
                                if value.assignee is None
                                else _copy_actor_ref(value.assignee)
                            )
                        ),
                    )
                    for value in current.chunks
                ),
                audit_head_digest=current.audit_head_digest,
            )
        )
        operation_id = self._issue_operation_id()
        preview = validate_chunk_mutation_preview(
            ChunkMutationPreview(
                operation_id=operation_id,
                action=action,
                project_id=current.project_id,
                chunk_plan_id=current.chunk_plan_id,
                base_revision=current.revision,
                published_revision=current.revision + 1,
                before_plan_digest=chunk_plan_digest_v1(current),
                after_plan_digest=chunk_plan_digest_v1(candidate),
                affected_chunk_ids=(chunk.chunk_id,),
                created_chunk_ids=(),
                retired_chunk_ids=(),
                affected_chunk_count=1,
                created_chunk_count=0,
                retired_chunk_count=0,
                affected_member_count=len(chunk.members),
                assignment_count=1,
                warnings=(),
                blockers=(),
                truncated=False,
            )
        )
        audit_digest = chunk_operation_audit_digest_v1(
            preview,
            manager_ref,
            current.audit_head_digest,
        )
        candidate = validate_chunk_plan_snapshot(
            replace(candidate, audit_head_digest=audit_digest)
        )
        if chunk_plan_digest_v1(candidate) != preview.after_plan_digest:
            _fail("CHUNK.CONTRACT_INVALID")
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
                created_chunk_ids=(),
                retired_chunk_ids=(),
                affected_chunk_count=1,
                created_chunk_count=0,
                retired_chunk_count=0,
                affected_member_count=preview.affected_member_count,
                assignment_count=1,
                actor_ref=_copy_actor_ref(manager_ref),
                safe_issues=(),
                truncated=False,
                audit_record_digest=audit_digest,
            )
        )
        record = _PreparedAssignmentPlan(
            preview=_copy_preview(preview),
            receipt=_copy_receipt(receipt),
            candidate=_copy_snapshot(candidate),
            expected_plan_binding=_copy_plan_binding(expected_plan_binding),
            workspace_binding=_copy_workspace_binding(workspace_binding),
            retired_baseline=retired,
            capability=capability,
            manager=manager,
            target_actor_port=target_actor_port,
            target_actor_handle=target_actor_handle,
            target_actor_ref=(
                None if next_assignee is None else _copy_actor_ref(next_assignee)
            ),
        )
        assert type(record.expected_plan_binding) is ChunkPlanBinding
        with self.__lock:
            previous_id = self.__pending_by_capability.get(id(capability))
            if previous_id is not None:
                previous = self.__prepared.pop(previous_id, None)
                if previous is not None:
                    previous.state = "terminal"
            if len(self.__prepared) >= MAX_ACTIVE_CHUNKS:
                _fail("CHUNK.LIMIT_EXCEEDED")
            self.__prepared[operation_id] = record
            self.__pending_by_capability[id(capability)] = operation_id
        return _copy_preview(preview)

    def preview_assign(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
        target_actor_port: AuthenticatedActorPort,
        target_actor_handle: AuthenticatedActorHandle,
    ) -> ChunkMutationPreview:
        return self._preview_assignment(
            TopologyAction.ASSIGN,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            chunk_id=chunk_id,
            target_actor_port=target_actor_port,
            target_actor_handle=target_actor_handle,
        )

    def preview_reassign(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
        target_actor_port: AuthenticatedActorPort,
        target_actor_handle: AuthenticatedActorHandle,
    ) -> ChunkMutationPreview:
        return self._preview_assignment(
            TopologyAction.REASSIGN,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            chunk_id=chunk_id,
            target_actor_port=target_actor_port,
            target_actor_handle=target_actor_handle,
        )

    def preview_unassign(
        self,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
    ) -> ChunkMutationPreview:
        return self._preview_assignment(
            TopologyAction.UNASSIGN,
            capability,
            manager,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            chunk_id=chunk_id,
            target_actor_port=None,
            target_actor_handle=None,
        )

    def _discard_prepared(
        self,
        operation_id: str,
        record: _PreparedAssignmentPlan,
    ) -> None:
        if self.__prepared.get(operation_id) is record:
            self.__prepared.pop(operation_id, None)
        key = id(record.capability)
        if self.__pending_by_capability.get(key) == operation_id:
            self.__pending_by_capability.pop(key, None)

    def apply_assignment(
        self,
        preview: ChunkMutationPreview,
        capability: ChunkManagerCapability,
        manager: LocalReferenceManagerHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
    ) -> ChunkOperationReceipt:
        validated_preview = validate_chunk_mutation_preview(preview)
        if validated_preview.action not in {
            TopologyAction.ASSIGN,
            TopologyAction.REASSIGN,
            TopologyAction.UNASSIGN,
        }:
            _fail("CHUNK.CONTRACT_INVALID")
        with self.__lock:
            record = self.__prepared.get(validated_preview.operation_id)
            if record is None or record.state != "pending":
                _fail("CHUNK.PREVIEW_STALE")
            if (
                record.preview != validated_preview
                or record.capability is not capability
                or record.manager is not manager
                or record.workspace_binding != workspace_binding
                or record.expected_plan_binding != expected_plan_binding
            ):
                _fail("CHUNK.PREVIEW_STALE")
            try:
                _, retired, _ = self._authorize_current(
                    capability,
                    manager,
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    action=validated_preview.action,
                    stale_code="CHUNK.PREVIEW_STALE",
                )
                if retired != record.retired_baseline:
                    _fail("CHUNK.PREVIEW_STALE")
                if record.target_actor_ref is not None:
                    assert record.target_actor_port is not None
                    assert record.target_actor_handle is not None
                    if self._resolve_actor(
                        record.target_actor_port,
                        record.target_actor_handle,
                    ) != record.target_actor_ref:
                        _fail("CHUNK.ACTOR_UNVERIFIED")

                def publish(manager_ref: AssigneeRef) -> None:
                    if manager_ref != record.receipt.actor_ref:
                        _fail("CHUNK.PREVIEW_STALE")
                    record.state = "publishing"
                    result = self.__publication_authority.publish_assignment_atomic(
                        self.__publication_permit,
                        record.expected_plan_binding,
                        _copy_snapshot(record.candidate),
                        _copy_receipt(record.receipt),
                    )
                    result.__post_init__()
                    if (
                        result.snapshot != record.candidate
                        or set(result.retired_chunk_ids) != record.retired_baseline
                    ):
                        _fail("CHUNK.RECOVERY_REQUIRED")

                manager_ref = self.__publication_authority.consume_manager_capability_with_publication(
                    capability,
                    manager,
                    publish,
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    action=validated_preview.action,
                )
            except ChunkError:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise
            except Exception:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None
            if manager_ref != record.receipt.actor_ref:
                record.state = "terminal"
                self._discard_prepared(validated_preview.operation_id, record)
                _fail("CHUNK.RECOVERY_REQUIRED")
            record.state = "applied"
            self._discard_prepared(validated_preview.operation_id, record)
            return _copy_receipt(record.receipt)


_ACTOR_CAPABILITY_CONSTRUCTOR_SEAL = object()


class ChunkActorCapability:
    """Opaque, single-use authority for one exact segment edit attempt."""

    __slots__ = ("__owner_token", "__nonce")

    def __new__(cls, *args: object, **kwargs: object) -> ChunkActorCapability:
        if kwargs.get("_seal") is not _ACTOR_CAPABILITY_CONSTRUCTOR_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        return super().__new__(cls)

    def __init__(self, *, _seal: object, owner_token: object) -> None:
        if _seal is not _ACTOR_CAPABILITY_CONSTRUCTOR_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        object.__setattr__(self, "_ChunkActorCapability__owner_token", owner_token)
        object.__setattr__(
            self,
            "_ChunkActorCapability__nonce",
            secrets.token_bytes(32),
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ChunkActorCapability cannot be subclassed")

    def __repr__(self) -> str:
        return "<ChunkActorCapability opaque>"

    def __reduce__(self) -> object:
        raise TypeError("ChunkActorCapability is not serializable")

    def __copy__(self) -> object:
        raise TypeError("ChunkActorCapability is not copyable")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("ChunkActorCapability is not copyable")


@dataclass(frozen=True, slots=True)
class _CurrentChunkSelection:
    plan_binding: ChunkPlanBinding
    workspace_session_id: str
    chunk_id: str | None
    epoch: int


@dataclass(slots=True)
class _ActorCapabilityRecord:
    capability: ChunkActorCapability
    actor_port: AuthenticatedActorPort
    actor_handle: AuthenticatedActorHandle
    actor_ref: AssigneeRef
    workspace_binding: ChunkWorkspaceBinding
    plan_binding: ChunkPlanBinding
    chunk_id: str
    segment: ChunkSegmentRef
    selection_epoch: int
    operation: ChunkEditOperation
    consumed: bool = False


class CollaborativeChunkPermissionService:
    """Body-safe current-chunk decisions and atomic edit-capability consumption."""

    __slots__ = (
        "__publication_authority",
        "__mutation_permit",
        "__project_id",
        "__workspace_universe_provider",
        "__selections",
        "__capabilities",
        "__observed_workspace_session_id",
        "__cached_universe_projection",
        "__cached_universe_binding",
        "__cached_universe_entries",
        "__cached_plan_binding",
        "__cached_plan_snapshot",
        "__owner_token",
        "__lock",
    )

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> CollaborativeChunkPermissionService:
        if kwargs.get("_seal") is not _PERMISSION_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        return super().__new__(cls)

    def __init__(
        self,
        publication_authority: ChunkTopologyPublicationAuthority,
        *,
        _seal: object,
        mutation_permit: _PermissionMutationPermit,
        project_id: str,
        workspace_universe_provider: Callable[
            [], ChunkWorkspaceUniverseProjection
        ],
    ) -> None:
        if _seal is not _PERMISSION_SERVICE_CONSTRUCTOR_SEAL:
            _fail("CHUNK.PERMISSION_STALE")
        if type(publication_authority) is not ChunkTopologyPublicationAuthority:
            _fail("CHUNK.CONTRACT_INVALID")
        if not callable(workspace_universe_provider):
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            validated_project_id = validate_project_id(project_id)
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        if publication_authority.project_id != validated_project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        publication_authority._validate_permission_permit(mutation_permit)
        self.__publication_authority = publication_authority
        self.__mutation_permit = mutation_permit
        self.__project_id = validated_project_id
        self.__workspace_universe_provider = workspace_universe_provider
        self.__selections: dict[AssigneeRef, _CurrentChunkSelection] = {}
        self.__capabilities: dict[int, _ActorCapabilityRecord] = {}
        self.__observed_workspace_session_id: str | None = None
        self.__cached_universe_projection: (
            ChunkWorkspaceUniverseProjection | None
        ) = None
        self.__cached_universe_binding: ChunkWorkspaceBinding | None = None
        self.__cached_universe_entries: dict[
            tuple[str, str, str], ChunkUniverseEntry
        ] | None = None
        self.__cached_plan_binding: ChunkPlanBinding | None = None
        self.__cached_plan_snapshot: ChunkPlanSnapshot | None = None
        self.__owner_token = object()
        self.__lock = RLock()

    def _current_universe(
        self,
    ) -> tuple[
        ChunkWorkspaceBinding,
        dict[tuple[str, str, str], ChunkUniverseEntry],
    ]:
        try:
            projection = self.__workspace_universe_provider()
        except ChunkError:
            raise
        except Exception:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if (
            projection is self.__cached_universe_projection
            and self.__cached_universe_binding is not None
            and self.__cached_universe_entries is not None
        ):
            return (
                _copy_workspace_binding(self.__cached_universe_binding),
                self.__cached_universe_entries,
            )
        if type(projection) is not ChunkWorkspaceUniverseProjection:
            _fail("CHUNK.CONTRACT_INVALID")
        projection.__post_init__()
        if projection.binding.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        entries = {
            _member_key(entry.segment): entry for entry in projection.entries
        }
        if len(entries) != len(projection.entries):
            _fail("CHUNK.MEMBER_DUPLICATE")
        binding = _copy_workspace_binding(projection.binding)
        self.__cached_universe_projection = projection
        self.__cached_universe_binding = binding
        self.__cached_universe_entries = entries
        return _copy_workspace_binding(binding), entries

    def _live_plan(
        self,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
    ) -> tuple[
        ChunkPlanSnapshot | None,
        ChunkPlanBinding | None,
        dict[tuple[str, str, str], ChunkUniverseEntry],
        bool,
    ]:
        current_workspace, universe = self._current_universe()
        self._observe_workspace_session(current_workspace.workspace_session_id)
        authority_binding = self.__publication_authority.current_plan_binding()
        if authority_binding is None:
            return None, None, universe, expected_plan_binding is None
        if (
            self.__cached_plan_binding == authority_binding
            and self.__cached_plan_snapshot is not None
        ):
            snapshot = self.__cached_plan_snapshot
            binding = authority_binding
        else:
            snapshot = self.__publication_authority.current_snapshot()
            if snapshot is None:
                _fail("CHUNK.REVISION_STALE")
            snapshot = validate_chunk_plan_snapshot(snapshot)
            binding = chunk_plan_binding(snapshot)
            if binding != authority_binding:
                _fail("CHUNK.REVISION_STALE")
            self.__cached_plan_binding = binding
            self.__cached_plan_snapshot = snapshot
        current = (
            type(expected_plan_binding) is ChunkPlanBinding
            and expected_plan_binding == binding
            and workspace_binding == current_workspace
            and workspace_binding.segment_universe_digest
            == binding.segment_universe_digest
        )
        return snapshot, binding, universe, current

    def _observe_workspace_session(self, workspace_session_id: str) -> None:
        with self.__lock:
            previous = self.__observed_workspace_session_id
            if previous is None:
                self.__observed_workspace_session_id = workspace_session_id
                return
            if previous == workspace_session_id:
                return
            self.__observed_workspace_session_id = workspace_session_id
            self.__selections.clear()
            for record in self.__capabilities.values():
                record.consumed = True
            self.__capabilities.clear()

    @staticmethod
    def _decision(
        *,
        project_id: str,
        binding: ChunkPlanBinding | None,
        actor: AssigneeRef,
        current_chunk_id: str | None,
        segment: ChunkSegmentRef,
        access: ChunkAccessKind,
    ) -> ChunkAccessDecision:
        code = {
            ChunkAccessKind.READ_ONLY_NO_PLAN: "CHUNK.PERMISSION_STALE",
            ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK: "CHUNK.OUTSIDE_CURRENT",
            ChunkAccessKind.READ_ONLY_UNALLOCATED: "CHUNK.UNALLOCATED_READ_ONLY",
            ChunkAccessKind.READ_ONLY_OUTSIDE_CURRENT: "CHUNK.OUTSIDE_CURRENT",
            ChunkAccessKind.READ_ONLY_NOT_ASSIGNEE: "CHUNK.NOT_ASSIGNEE",
            ChunkAccessKind.READ_ONLY_DETACHED: "CHUNK.DETACHED_READ_ONLY",
            ChunkAccessKind.READ_ONLY_STALE: "CHUNK.PERMISSION_STALE",
        }.get(access)
        editable = access is ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT
        return ChunkAccessDecision(
            project_id=project_id,
            chunk_plan_id=None if binding is None else binding.chunk_plan_id,
            plan_revision=None if binding is None else binding.plan_revision,
            plan_digest=None if binding is None else binding.plan_digest,
            actor=_copy_actor_ref(actor),
            current_chunk_id=current_chunk_id,
            segment=_copy_members((segment,))[0],
            access=access,
            may_edit_target=editable,
            may_change_confirmed=editable,
            safe_codes=() if editable else (code,),
        )

    def select_current_chunk(
        self,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        chunk_id: str,
    ) -> str:
        actor = _resolve_authenticated_actor(actor_port, actor_handle)
        validated_chunk_id = validate_chunk_id(chunk_id)
        snapshot, binding, _universe, current = self._live_plan(
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
        )
        if snapshot is None or binding is None or not current:
            _fail("CHUNK.PERMISSION_STALE")
        if not any(chunk.chunk_id == validated_chunk_id for chunk in snapshot.chunks):
            _fail("CHUNK.IDENTITY_FOREIGN")
        with self.__lock:
            previous = self.__selections.get(actor)
            epoch = 1 if previous is None else previous.epoch + 1
            self.__selections[actor] = _CurrentChunkSelection(
                plan_binding=_copy_plan_binding(binding),
                workspace_session_id=workspace_binding.workspace_session_id,
                chunk_id=validated_chunk_id,
                epoch=epoch,
            )
            self._discard_actor_capabilities(actor)
        return validated_chunk_id

    def clear_current_chunk(
        self,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
    ) -> None:
        actor = _resolve_authenticated_actor(actor_port, actor_handle)
        with self.__lock:
            previous = self.__selections.get(actor)
            if previous is None:
                return
            self.__selections[actor] = _CurrentChunkSelection(
                plan_binding=previous.plan_binding,
                workspace_session_id=previous.workspace_session_id,
                chunk_id=None,
                epoch=previous.epoch + 1,
            )
            self._discard_actor_capabilities(actor)

    def _revoke_actor_session_for_controller(self, actor: AssigneeRef) -> None:
        """Deny-only revocation for the session composition boundary.

        This private seam deliberately does not re-resolve identity: it cannot
        grant or select anything, and remains available when actor resolution
        itself is the reason the application must fail closed.
        """

        if type(actor) is not AssigneeRef:
            _fail("CHUNK.ACTOR_UNVERIFIED")
        actor.__post_init__()
        with self.__lock:
            previous = self.__selections.get(actor)
            if previous is not None:
                self.__selections[actor] = _CurrentChunkSelection(
                    plan_binding=previous.plan_binding,
                    workspace_session_id=previous.workspace_session_id,
                    chunk_id=None,
                    epoch=previous.epoch + 1,
                )
            self._discard_actor_capabilities(actor)

    def _discard_actor_capabilities(self, actor: AssigneeRef) -> None:
        for capability_key, record in tuple(self.__capabilities.items()):
            if record.actor_ref == actor:
                record.consumed = True
                self.__capabilities.pop(capability_key, None)

    def _decide_access_for_actor(
        self,
        actor: AssigneeRef,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        segment: ChunkSegmentRef,
    ) -> ChunkAccessDecision:
        if type(actor) is not AssigneeRef:
            _fail("CHUNK.ACTOR_UNVERIFIED")
        actor.__post_init__()
        if type(segment) is not ChunkSegmentRef:
            _fail("CHUNK.CONTRACT_INVALID")
        segment.__post_init__()
        if segment.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        snapshot, binding, universe, current = self._live_plan(
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
        )
        with self.__lock:
            selection = self.__selections.get(actor)
            selected_id = None if selection is None else selection.chunk_id
            if snapshot is None:
                return self._decision(
                    project_id=self.__project_id,
                    binding=None,
                    actor=actor,
                    current_chunk_id=None,
                    segment=segment,
                    access=ChunkAccessKind.READ_ONLY_NO_PLAN,
                )
            assert binding is not None
            if not current:
                return self._decision(
                    project_id=self.__project_id,
                    binding=binding,
                    actor=actor,
                    current_chunk_id=selected_id,
                    segment=segment,
                    access=ChunkAccessKind.READ_ONLY_STALE,
                )
            if (
                selection is None
                or selection.chunk_id is None
                or selection.plan_binding != binding
                or selection.workspace_session_id
                != workspace_binding.workspace_session_id
            ):
                return self._decision(
                    project_id=self.__project_id,
                    binding=binding,
                    actor=actor,
                    current_chunk_id=None,
                    segment=segment,
                    access=ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK,
                )
            entry = universe.get(_member_key(segment))
            if entry is None:
                return self._decision(
                    project_id=self.__project_id,
                    binding=binding,
                    actor=actor,
                    current_chunk_id=selection.chunk_id,
                    segment=segment,
                    access=ChunkAccessKind.READ_ONLY_STALE,
                )
            allocated = next(
                (
                    chunk
                    for chunk in snapshot.chunks
                    if any(
                        _member_key(member) == _member_key(segment)
                        for member in chunk.members
                    )
                ),
                None,
            )
            if allocated is None:
                access = ChunkAccessKind.READ_ONLY_UNALLOCATED
            elif allocated.chunk_id != selection.chunk_id:
                access = ChunkAccessKind.READ_ONLY_OUTSIDE_CURRENT
            elif allocated.assignee != actor:
                access = ChunkAccessKind.READ_ONLY_NOT_ASSIGNEE
            elif entry.source_presence is SourcePresence.DETACHED:
                access = ChunkAccessKind.READ_ONLY_DETACHED
            else:
                access = ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT
            return self._decision(
                project_id=self.__project_id,
                binding=binding,
                actor=actor,
                current_chunk_id=selection.chunk_id,
                segment=segment,
                access=access,
            )

    def decide_access(
        self,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding | None,
        segment: ChunkSegmentRef,
    ) -> ChunkAccessDecision:
        actor = _resolve_authenticated_actor(actor_port, actor_handle)
        return self._decide_access_for_actor(
            actor,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            segment=segment,
        )

    def issue_edit_capability(
        self,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        segment: ChunkSegmentRef,
        operation: ChunkEditOperation = ChunkEditOperation.SEGMENT_EDIT,
    ) -> ChunkActorCapability:
        if type(operation) is not ChunkEditOperation:
            _fail("CHUNK.CONTRACT_INVALID")
        actor = _resolve_authenticated_actor(actor_port, actor_handle)
        decision = self._decide_access_for_actor(
            actor,
            workspace_binding=workspace_binding,
            expected_plan_binding=expected_plan_binding,
            segment=segment,
        )
        if decision.access is not ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT:
            _fail(decision.safe_codes[0])
        with self.__lock:
            selection = self.__selections.get(actor)
            if selection is None or selection.chunk_id is None:
                _fail("CHUNK.PERMISSION_STALE")
            self._discard_actor_capabilities(actor)
            for capability_key, record in tuple(self.__capabilities.items()):
                if record.consumed or record.plan_binding != expected_plan_binding:
                    record.consumed = True
                    self.__capabilities.pop(capability_key, None)
            capability = ChunkActorCapability(
                _seal=_ACTOR_CAPABILITY_CONSTRUCTOR_SEAL,
                owner_token=self.__owner_token,
            )
            self.__capabilities[id(capability)] = _ActorCapabilityRecord(
                capability=capability,
                actor_port=actor_port,
                actor_handle=actor_handle,
                actor_ref=_copy_actor_ref(actor),
                workspace_binding=_copy_workspace_binding(workspace_binding),
                plan_binding=_copy_plan_binding(expected_plan_binding),
                chunk_id=selection.chunk_id,
                segment=_copy_members((segment,))[0],
                selection_epoch=selection.epoch,
                operation=operation,
            )
            return capability

    def execute_segment_edit(
        self,
        capability: ChunkActorCapability,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
        mutation_port: ChunkSegmentEditMutationPort,
        *,
        workspace_binding: ChunkWorkspaceBinding,
        expected_plan_binding: ChunkPlanBinding,
        segment: ChunkSegmentRef,
        operation: ChunkEditOperation = ChunkEditOperation.SEGMENT_EDIT,
    ) -> object:
        if type(capability) is not ChunkActorCapability:
            _fail("CHUNK.PERMISSION_STALE")
        if type(operation) is not ChunkEditOperation:
            _fail("CHUNK.PERMISSION_STALE")
        with self.__lock:
            record = self.__capabilities.get(id(capability))
            if record is None or record.capability is not capability or record.consumed:
                _fail("CHUNK.PERMISSION_STALE")
            if (
                record.actor_port is not actor_port
                or record.actor_handle is not actor_handle
                or record.workspace_binding != workspace_binding
                or record.plan_binding != expected_plan_binding
                or record.segment != segment
                or record.operation is not operation
            ):
                _fail("CHUNK.PERMISSION_STALE")
            record.consumed = True
            self.__capabilities.pop(id(capability), None)
            actor = _resolve_authenticated_actor(actor_port, actor_handle)
            selection = self.__selections.get(actor)
            if (
                record.actor_ref != actor
                or selection is None
                or selection.chunk_id != record.chunk_id
                or selection.epoch != record.selection_epoch
                or selection.plan_binding != record.plan_binding
                or selection.workspace_session_id
                != record.workspace_binding.workspace_session_id
            ):
                _fail("CHUNK.PERMISSION_STALE")
            decision = self._decide_access_for_actor(
                actor,
                workspace_binding=workspace_binding,
                expected_plan_binding=expected_plan_binding,
                segment=segment,
            )
            if decision.access is not ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT:
                _fail(decision.safe_codes[0])
            try:
                result = self.__publication_authority.perform_permission_edit(
                    self.__mutation_permit,
                    mutation_port,
                    workspace_binding=workspace_binding,
                    expected_plan_binding=expected_plan_binding,
                    segment=segment,
                )
            except ChunkError:
                raise
            except Exception:
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None
            return result
