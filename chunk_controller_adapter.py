"""Application composition for Workspace sessions and collaborative Chunks.

This module is the only business boundary allowed to hold both owners.  It
does not decode ProjectPackage or Chunk metadata; their existing services do.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Callable

from chunk_controller_contracts import (
    ChunkApplicationAccessView,
    ChunkApplicationChunkView,
    ChunkApplicationMode,
    ChunkApplicationMutationPreview,
    ChunkApplicationMutationReceipt,
    ChunkApplicationProgressView,
    ChunkApplicationProjectView,
    ChunkApplicationRebaseInspection,
    ChunkApplicationSegmentChoice,
    ChunkApplicationSplitChild,
    CollaborativeSearchScopeV2,
    CollaborativeWorkspaceSearchHitV2,
    CollaborativeWorkspaceSearchReportV2,
    CollaborativeWorkspaceSearchRequestV2,
)
from collaborative_chunk_contracts import (
    AssigneeRef,
    ChunkError,
    ChunkPlanBinding,
    ChunkMutationPreview,
    ChunkOperationReceipt,
    ChunkRebasePreview,
    ChunkScopeProjection,
    ChunkSegmentRef,
    ChunkSplitChild,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_plan_binding,
)
import collaborative_chunk_conflict as chunk_conflict
from collaborative_chunk_store import CollaborativeChunkStore
from collaborative_chunk_workspace_adapter import (
    capture_live_workspace_progress,
    capture_live_workspace_transition,
    capture_live_workspace_universe,
)
from collaborative_chunks import (
    AuthenticatedActorHandle,
    AuthenticatedActorPort,
    ChunkActorCapability,
    ChunkManagerCapability,
    ChunkScopeProjectionService,
    ChunkProgressService,
    ChunkTopologyPublicationAuthority,
    CollaborativeChunkAssignmentService,
    CollaborativeChunkPermissionService,
    CollaborativeChunkTopologyService,
)
from editor_contracts import SearchScope, WorkspaceSearchHit, WorkspaceSearchRequest
from editor_controller import EditorController, EditorControllerError
from project_workspace import (
    ProjectWorkspaceService,
    PublishedWorkspaceTransitionProjection,
    WorkspaceEditReceipt,
)
from project_workspace_contracts import SegmentIdentity
from project_workspace_identity import ProjectWorkspaceError, validate_project_id


def create_chunk_metadata_binding_resolver(
    metadata_root: Path,
) -> Callable[[str], tuple[Path, str]]:
    """Own the safe per-project sidecar path outside the Qt composition root."""

    if not isinstance(metadata_root, Path):
        raise TypeError("chunk metadata root must be a Path")
    root = metadata_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    def resolve(project_id: str) -> tuple[Path, str]:
        validated_project_id = validate_project_id(project_id)
        project_root = (root / validated_project_id).resolve()
        if not project_root.is_relative_to(root):
            raise ValueError("chunk metadata project root escaped app data")
        project_root.mkdir(parents=True, exist_ok=True)
        return project_root, "chunks.json"

    return resolve


class ChunkControllerSessionMode(str, Enum):
    NO_PLAN = "no_plan"
    ACTIVE = "active"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ChunkControllerSessionView:
    mode: ChunkControllerSessionMode
    project_id: str
    chunk_plan_id: str | None
    plan_revision: int | None
    current_chunk_id: str | None
    selection_generation: int
    safe_code: str | None


@dataclass(slots=True)
class _PreparedWorkspaceEdit:
    service: ProjectWorkspaceService
    identity: SegmentIdentity
    target: str
    confirmed: bool
    session_id: str
    base_revision: int
    workspace_binding: ChunkWorkspaceBinding | None
    plan_binding: ChunkPlanBinding | None
    segment: ChunkSegmentRef | None
    capability: ChunkActorCapability | None
    consumed: bool = False


@dataclass(slots=True)
class _PreparedApplicationMutation:
    public: ChunkApplicationMutationPreview
    private_preview: object
    capability: ChunkManagerCapability | None
    workspace_binding: ChunkWorkspaceBinding | None
    plan_binding: ChunkPlanBinding | None
    kind: str
    consumed: bool = False


class _WorkspaceEditMutationPort:
    __slots__ = (
        "_service",
        "_identity",
        "_target",
        "_confirmed",
        "_session_id",
        "_base_revision",
        "_publish_resources",
        "calls",
    )

    def __init__(
        self,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
        session_id: str,
        base_revision: int,
        publish_resources: Callable[[], object] | None = None,
    ) -> None:
        self._service = service
        self._identity = identity
        self._target = target
        self._confirmed = confirmed
        self._session_id = session_id
        self._base_revision = base_revision
        self._publish_resources = publish_resources
        self.calls = 0

    def apply_segment_edit(
        self,
        segment: ChunkSegmentRef,
        expected_workspace_binding: ChunkWorkspaceBinding,
    ) -> object:
        live = capture_live_workspace_universe(self._service)
        if (
            segment.identity != self._identity
            or live.binding != expected_workspace_binding
            or self._service.session_id != self._session_id
            or self._service.revision != self._base_revision
        ):
            raise ChunkError("CHUNK.PERMISSION_STALE")
        self.calls += 1
        report = None
        if self._publish_resources is not None:
            report = self._publish_resources()
            if not bool(getattr(report, "succeeded", False)):
                return (report, None)
        try:
            receipt = self._service.update_segment_edit(
                self._identity,
                target=self._target,
                confirmed=self._confirmed,
                session_id=self._session_id,
                base_revision=self._base_revision,
            )
        except ProjectWorkspaceError:
            raise ChunkError("CHUNK.PERMISSION_STALE") from None
        return receipt if report is None else (report, receipt)


class ChunkControllerAdapter:
    """One session-scoped façade for actor, scope, search and edit gates."""

    def __init__(
        self,
        controller: EditorController,
        actor_port: AuthenticatedActorPort,
        actor_handle: AuthenticatedActorHandle,
        *,
        metadata_binding_resolver: Callable[[str], tuple[Path, str]],
    ) -> None:
        if type(controller) is not EditorController:
            raise TypeError("chunk controller requires exact EditorController")
        if type(actor_handle) is not AuthenticatedActorHandle:
            raise TypeError("chunk controller requires authenticated actor handle")
        if not callable(getattr(actor_port, "revalidate_actor", None)):
            raise TypeError("chunk controller requires authenticated actor port")
        if not callable(metadata_binding_resolver):
            raise TypeError("chunk controller requires metadata binding resolver")
        actor_ref = actor_port.revalidate_actor(actor_handle)
        if type(actor_ref) is not AssigneeRef:
            raise TypeError("chunk controller requires exact actor reference")
        actor_ref.__post_init__()
        self._controller = controller
        self._actor_port = actor_port
        self._actor_handle = actor_handle
        self._actor_ref = actor_ref
        self._metadata_binding_resolver = metadata_binding_resolver
        self._metadata_bindings: dict[str, tuple[Path, str]] = {}
        self._mode = ChunkControllerSessionMode.BLOCKED
        self._safe_code = "CHUNK.WORKSPACE_UNBOUND"
        self._project_id = ""
        self._authority: ChunkTopologyPublicationAuthority | None = None
        self._topology: CollaborativeChunkTopologyService | None = None
        self._assignment: CollaborativeChunkAssignmentService | None = None
        self._permission: CollaborativeChunkPermissionService | None = None
        self._scope: ChunkScopeProjectionService | None = None
        self._progress: ChunkProgressService | None = None
        self._conflict: chunk_conflict.ChunkMetadataConflictService | None = None
        self._manager = LocalReferenceManagerHandle(
            actor_ref.authority_id,
            actor_ref.subject_id,
        )
        self._accepted_workspace_transition = None
        self._workspace_transition_capture_failure: str | None = None
        self._current_chunk_id: str | None = None
        self._selected_plan_binding: ChunkPlanBinding | None = None
        self._selected_workspace_session_id: str | None = None
        self._selection_generation = 0
        self._pending: dict[int, _PreparedWorkspaceEdit] = {}
        self._pending_application: dict[int, _PreparedApplicationMutation] = {}
        self._bound_owner: ProjectWorkspaceService | None = None
        self._bound_workspace_session_id: str | None = None
        self._cached_universe_owner: ProjectWorkspaceService | None = None
        self._cached_universe_state: tuple[str, int, int] | None = None
        self._cached_universe: ChunkWorkspaceUniverseProjection | None = None
        self._cached_plan_binding: ChunkPlanBinding | None = None
        self._cached_plan_snapshot: object | None = None
        self._issued_search: tuple[
            CollaborativeWorkspaceSearchReportV2,
            tuple[WorkspaceSearchHit, ...],
            ChunkWorkspaceBinding,
            ChunkPlanBinding,
            int,
            ChunkScopeProjection | None,
        ] | None = None
        controller._install_workspace_chunk_edit_gate(self)

    @property
    def session_view(self) -> ChunkControllerSessionView:
        snapshot = self._current_plan_snapshot()
        self._revoke_current_selection_if_stale(snapshot)
        return ChunkControllerSessionView(
            mode=self._mode,
            project_id=self._project_id,
            chunk_plan_id=None if snapshot is None else snapshot.chunk_plan_id,
            plan_revision=None if snapshot is None else snapshot.revision,
            current_chunk_id=self._current_chunk_id,
            selection_generation=self._selection_generation,
            safe_code=self._safe_code,
        )

    def _revoke_current_selection_if_stale(
        self,
        snapshot: object,
        *,
        live_binding: ChunkWorkspaceBinding | None = None,
    ) -> None:
        if self._current_chunk_id is None:
            return
        try:
            if snapshot is None:
                stale = True
            else:
                current_plan = chunk_plan_binding(snapshot)
                live = (
                    self._live_universe().binding
                    if live_binding is None
                    else live_binding
                )
                stale = (
                    self._selected_plan_binding != current_plan
                    or self._selected_workspace_session_id
                    != live.workspace_session_id
                )
        except Exception:
            stale = True
        if stale:
            permission = self._permission
            if permission is not None:
                permission._revoke_actor_session_for_controller(self._actor_ref)
            for prepared in self._pending.values():
                prepared.consumed = True
            self._pending.clear()
            self._current_chunk_id = None
            self._selected_plan_binding = None
            self._selected_workspace_session_id = None
            self._selection_generation += 1
            self._issued_search = None

    def open_project_package(
        self,
        path: Path,
    ) -> object:
        """Open Workspace first, then bind optional exact-project metadata."""

        workspace = self._controller.open_project_package(path)
        owner = self._controller._workspace_owner_for_chunk_controller()
        if (
            self._bound_owner is not owner
            or self._bound_workspace_session_id != owner.session_id
        ):
            self.workspace_opened()
        return workspace

    def workspace_opened(self) -> ChunkControllerSessionView:
        """Bind the live Workspace without eagerly building the Qt product view."""

        owner = self._controller._workspace_owner_for_chunk_controller()
        if (
            self._bound_owner is not owner
            or self._bound_workspace_session_id != owner.session_id
            or self._project_id != owner.workspace.project_id
        ):
            return self.bind_current_workspace_metadata()
        return self.session_view

    def workspace_closed(self) -> None:
        """Revoke all issued session state without touching a closed owner."""

        if self._permission is not None:
            try:
                self._permission._revoke_actor_session_for_controller(
                    self._actor_ref
                )
            except Exception:
                pass
        for prepared in self._pending.values():
            prepared.consumed = True
        for prepared in self._pending_application.values():
            prepared.consumed = True
        self._pending.clear()
        self._pending_application.clear()
        self._issued_search = None
        self._accepted_workspace_transition = None
        self._workspace_transition_capture_failure = None
        self._current_chunk_id = None
        self._selected_plan_binding = None
        self._selected_workspace_session_id = None
        self._authority = None
        self._topology = None
        self._assignment = None
        self._permission = None
        self._scope = None
        self._progress = None
        self._conflict = None
        self._bound_owner = None
        self._bound_workspace_session_id = None
        self._clear_live_universe_cache()
        self._selection_generation += 1
        self._mode = ChunkControllerSessionMode.BLOCKED
        self._safe_code = "CHUNK.WORKSPACE_UNBOUND"
        self._project_id = ""

    def capture_workspace_transition(
        self,
        workspace_owner: ProjectWorkspaceService,
        owner_issued_projection: PublishedWorkspaceTransitionProjection,
    ) -> object:
        """Accept only a transition revalidated by the exact live owner."""

        if (
            workspace_owner is not self._bound_owner
            or workspace_owner.session_id != self._bound_workspace_session_id
        ):
            raise ChunkError("CHUNK.IDENTITY_FOREIGN")
        captured = capture_live_workspace_transition(
            workspace_owner,
            owner_issued_projection,
        )
        live = capture_live_workspace_universe(workspace_owner).binding
        if (
            captured.current.binding.project_id != live.project_id
            or captured.current.binding.workspace_session_id
            != live.workspace_session_id
            or captured.current.binding.segment_universe_digest
            != live.segment_universe_digest
        ):
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        self._accepted_workspace_transition = captured
        self._workspace_transition_capture_failure = None
        return captured

    def workspace_transition_capture_failed(
        self,
        workspace_owner: ProjectWorkspaceService,
    ) -> None:
        """Fail closed after Workspace publication outlives transition capture."""

        if (
            workspace_owner is not self._bound_owner
            or workspace_owner.session_id != self._bound_workspace_session_id
        ):
            return
        self._accepted_workspace_transition = None
        self._workspace_transition_capture_failure = (
            "CHUNK.RECOVERY_REQUIRED"
        )

    def workspace_open_failed(self) -> None:
        """Keep the optional Chunk layer closed when its open callback faults."""

        try:
            owner = self._controller._workspace_owner_for_chunk_controller()
        except Exception:
            self.workspace_closed()
            return
        for prepared in self._pending.values():
            prepared.consumed = True
        for prepared in self._pending_application.values():
            prepared.consumed = True
        self._pending.clear()
        self._pending_application.clear()
        self._issued_search = None
        self._accepted_workspace_transition = None
        self._workspace_transition_capture_failure = None
        self._current_chunk_id = None
        self._selected_plan_binding = None
        self._selected_workspace_session_id = None
        self._authority = None
        self._topology = None
        self._assignment = None
        self._permission = None
        self._scope = None
        self._progress = None
        self._conflict = None
        self._bound_owner = owner
        self._bound_workspace_session_id = owner.session_id
        self._clear_live_universe_cache()
        self._selection_generation += 1
        self._mode = ChunkControllerSessionMode.BLOCKED
        self._safe_code = "CHUNK.RECOVERY_REQUIRED"
        self._project_id = owner.workspace.project_id

    def validate_workspace_replacement(
        self,
        candidate_owner: ProjectWorkspaceService,
    ) -> None:
        """Reject an untrusted same-project universe swap before publication."""

        if type(candidate_owner) is not ProjectWorkspaceService:
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        if candidate_owner.workspace.project_id != self._project_id:
            return
        if self._mode is ChunkControllerSessionMode.BLOCKED:
            raise ChunkError(self._safe_code or "CHUNK.RECOVERY_REQUIRED")
        authority = self._authority
        snapshot = None if authority is None else authority.current_snapshot()
        if snapshot is None:
            return
        candidate = capture_live_workspace_universe(candidate_owner)
        if snapshot.segment_universe_digest != candidate.binding.segment_universe_digest:
            raise ChunkError("CHUNK.REBASE_REQUIRED")

    def bind_current_workspace_metadata(
        self,
    ) -> ChunkControllerSessionView:
        owner = self._controller._workspace_owner_for_chunk_controller()
        project_id = owner.workspace.project_id
        carried_transition = self._accepted_workspace_transition
        carried_capture_failure = self._workspace_transition_capture_failure
        self._selection_generation += 1
        self._project_id = project_id
        self._authority = None
        self._topology = None
        self._assignment = None
        self._permission = None
        self._scope = None
        self._progress = None
        self._conflict = None
        self._current_chunk_id = None
        self._selected_plan_binding = None
        self._selected_workspace_session_id = None
        self._issued_search = None
        self._pending.clear()
        self._pending_application.clear()
        self._clear_live_universe_cache()
        self._accepted_workspace_transition = None
        self._workspace_transition_capture_failure = None
        self._mode = ChunkControllerSessionMode.BLOCKED
        self._safe_code = "CHUNK.RECOVERY_REQUIRED"
        try:
            if carried_capture_failure is not None:
                raise ChunkError(carried_capture_failure, retryable=True)
            try:
                resolved = self._metadata_binding_resolver(project_id)
            except ChunkError:
                raise
            except Exception:
                raise ChunkError(
                    "CHUNK.RECOVERY_REQUIRED",
                    retryable=True,
                ) from None
            if (
                type(resolved) is not tuple
                or len(resolved) != 2
                or not isinstance(resolved[0], Path)
                or type(resolved[1]) is not str
            ):
                raise ChunkError("CHUNK.CONTRACT_INVALID")
            metadata_root, metadata_filename = resolved
            binding = (metadata_root, metadata_filename)
            established = self._metadata_bindings.get(project_id)
            if established is None:
                self._metadata_bindings[project_id] = binding
            elif established != binding:
                raise ChunkError("CHUNK.METADATA_BINDING_STALE")
            store = CollaborativeChunkStore(
                metadata_root,
                metadata_filename,
                project_id=project_id,
            )
            authority = ChunkTopologyPublicationAuthority(
                project_id=project_id,
                workspace_binding_provider=lambda: self._live_universe().binding,
                metadata_store=store,
            )
            plan_binding = authority.current_plan_binding()
            live = self._live_universe()
            if carried_transition is not None:
                current = carried_transition.current.binding
                if (
                    current.project_id == live.binding.project_id
                    and current.workspace_session_id
                    == live.binding.workspace_session_id
                    and current.workspace_revision
                    == live.binding.workspace_revision
                    and current.workspace_composition_revision
                    == live.binding.workspace_composition_revision
                    and current.segment_universe_digest
                    == live.binding.segment_universe_digest
                ):
                    self._accepted_workspace_transition = carried_transition
            universe_mismatch = plan_binding is not None and (
                plan_binding.project_id != project_id
                or plan_binding.segment_universe_digest
                != live.binding.segment_universe_digest
            )
            requires_rebase = bool(universe_mismatch)
            if not requires_rebase:
                self._accepted_workspace_transition = None
            self._authority = authority
            self._permission = authority.create_permission_service(
                workspace_universe_provider=self._live_universe,
            )
            self._scope = ChunkScopeProjectionService(
                authority.current_snapshot,
                retired_chunk_ids_provider=authority.retired_chunk_ids,
            )
            self._progress = ChunkProgressService(
                authority.current_snapshot,
                lambda: capture_live_workspace_progress(self._owner()),
                snapshot_binding_provider=authority.current_plan_binding,
            )
            self._conflict = chunk_conflict.ChunkMetadataConflictService(
                authority,
                workspace_binding_provider=lambda: self._live_universe().binding,
            )
            if requires_rebase:
                assert plan_binding is not None
                plan = plan_binding
                capability = authority.issue_manager_capability(
                    self._manager,
                    workspace_binding=live.binding,
                    expected_plan_binding=plan,
                    action=TopologyAction.REBASE,
                )
                try:
                    self._topology_service().inspect_rebase(
                        capability,
                        self._manager,
                        workspace_binding=live.binding,
                        expected_plan_binding=plan,
                    )
                except ChunkError as error:
                    if self._accepted_workspace_transition is None:
                        raise ChunkError("CHUNK.UNIVERSE_MISMATCH") from error
                    raise
            self._mode = (
                ChunkControllerSessionMode.NO_PLAN
                if plan_binding is None
                else ChunkControllerSessionMode.ACTIVE
            )
            self._safe_code = (
                "CHUNK.REBASE_REQUIRED" if requires_rebase else None
            )
        except ChunkError as error:
            self._mode = ChunkControllerSessionMode.BLOCKED
            self._safe_code = error.code
            self._authority = None
            self._topology = None
            self._assignment = None
            self._permission = None
            self._scope = None
            self._progress = None
            self._conflict = None
        self._bound_owner = owner
        self._bound_workspace_session_id = owner.session_id
        return self.session_view

    def _published_workspace_transition_for_rebase(self):
        transition = self._accepted_workspace_transition
        if transition is None:
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        return transition

    def _topology_service(self) -> CollaborativeChunkTopologyService:
        if self._topology is None:
            authority = self._authority
            if authority is None:
                raise ChunkError("CHUNK.PERMISSION_STALE")
            self._topology = authority.create_topology_service(
                workspace_universe_provider=self._live_universe,
                workspace_transition_provider=(
                    lambda: self._published_workspace_transition_for_rebase()
                ),
            )
        return self._topology

    def _assignment_service(self) -> CollaborativeChunkAssignmentService:
        if self._assignment is None:
            authority = self._authority
            if authority is None:
                raise ChunkError("CHUNK.PERMISSION_STALE")
            self._assignment = authority.create_assignment_service()
        return self._assignment

    def _owner(self) -> ProjectWorkspaceService:
        owner = self._controller._workspace_owner_for_chunk_controller()
        if self._project_id and owner.workspace.project_id != self._project_id:
            raise ChunkError("CHUNK.IDENTITY_FOREIGN")
        return owner

    def _clear_live_universe_cache(self) -> None:
        self._cached_universe_owner = None
        self._cached_universe_state = None
        self._cached_universe = None
        self._cached_plan_binding = None
        self._cached_plan_snapshot = None

    def _live_universe(self):
        owner = self._owner()
        state = (
            owner.session_id,
            owner.revision,
            owner.composition_revision,
        )
        if (
            self._cached_universe_owner is owner
            and self._cached_universe_state == state
            and self._cached_universe is not None
        ):
            return self._cached_universe
        projection = capture_live_workspace_universe(owner)
        self._cached_universe_owner = owner
        self._cached_universe_state = state
        self._cached_universe = projection
        return projection

    def _current_plan_snapshot(self):
        authority = self._authority
        if authority is None:
            self._cached_plan_binding = None
            self._cached_plan_snapshot = None
            return None
        binding = authority.current_plan_binding()
        if binding is None:
            self._cached_plan_binding = None
            self._cached_plan_snapshot = None
            return None
        if (
            self._cached_plan_binding == binding
            and self._cached_plan_snapshot is not None
        ):
            return self._cached_plan_snapshot
        snapshot = authority.current_snapshot()
        if snapshot is None or chunk_plan_binding(snapshot) != binding:
            raise ChunkError("CHUNK.REVISION_STALE")
        self._cached_plan_binding = binding
        self._cached_plan_snapshot = snapshot
        return snapshot

    def _active_services(
        self,
    ) -> tuple[
        ChunkTopologyPublicationAuthority,
        CollaborativeChunkPermissionService,
        ChunkScopeProjectionService,
        ChunkWorkspaceBinding,
        ChunkPlanBinding,
    ]:
        if self._mode is not ChunkControllerSessionMode.ACTIVE:
            code = self._safe_code or "CHUNK.PERMISSION_STALE"
            raise ChunkError(code)
        if self._safe_code == "CHUNK.REBASE_REQUIRED":
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        authority = self._authority
        permission = self._permission
        scope = self._scope
        if authority is None or permission is None or scope is None:
            raise ChunkError("CHUNK.PERMISSION_STALE")
        snapshot = authority.current_snapshot()
        if snapshot is None:
            raise ChunkError("CHUNK.PERMISSION_STALE")
        live = self._live_universe().binding
        binding = chunk_plan_binding(snapshot)
        if binding.segment_universe_digest != live.segment_universe_digest:
            raise ChunkError("CHUNK.UNIVERSE_MISMATCH")
        self._revoke_current_selection_if_stale(
            snapshot,
            live_binding=live,
        )
        return authority, permission, scope, live, binding

    def select_current_chunk(self, chunk_id: str) -> ChunkControllerSessionView:
        _authority, permission, _scope, workspace, plan = self._active_services()
        permission.select_current_chunk(
            self._actor_port,
            self._actor_handle,
            workspace_binding=workspace,
            expected_plan_binding=plan,
            chunk_id=chunk_id,
        )
        self._current_chunk_id = chunk_id
        self._selected_plan_binding = plan
        self._selected_workspace_session_id = workspace.workspace_session_id
        self._selection_generation += 1
        self._issued_search = None
        return self.session_view

    def clear_current_chunk(self) -> ChunkControllerSessionView:
        if self._permission is not None:
            self._permission.clear_current_chunk(
                self._actor_port,
                self._actor_handle,
            )
        self._current_chunk_id = None
        self._selected_plan_binding = None
        self._selected_workspace_session_id = None
        self._selection_generation += 1
        self._issued_search = None
        return self.session_view

    def issue_current_scope_projection(self) -> ChunkScopeProjection:
        _authority, _permission, scope, _workspace, plan = self._active_services()
        if self._current_chunk_id is None:
            raise ChunkError("CHUNK.OUTSIDE_CURRENT")
        return scope.issue_scope_projection(self._current_chunk_id, plan)

    def issue_scope_projection(self, explicit_chunk_id: str) -> ChunkScopeProjection:
        """Issue one explicitly selected active chunk for downstream export."""

        _authority, _permission, scope, _workspace, plan = self._active_services()
        return scope.issue_scope_projection(explicit_chunk_id, plan)

    def revalidate_scope_projection(
        self,
        projection: ChunkScopeProjection,
    ) -> ChunkScopeProjection:
        """Reprove a TMX/downstream scope without exposing Chunk internals."""

        _authority, _permission, scope, _workspace, _plan = self._active_services()
        return scope.revalidate_scope_projection(projection)

    @staticmethod
    def _segment_ref(project_id: str, identity: SegmentIdentity) -> ChunkSegmentRef:
        return ChunkSegmentRef(project_id=project_id, identity=identity)

    def prepare_segment_edit(
        self,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
        session_id: str,
        base_revision: int,
    ) -> object:
        if service is not self._owner():
            raise EditorControllerError("CHUNK.PERMISSION_STALE")
        if self._mode is ChunkControllerSessionMode.BLOCKED:
            raise EditorControllerError(self._safe_code or "CHUNK.PERMISSION_STALE")
        if self._mode is ChunkControllerSessionMode.NO_PLAN:
            prepared = _PreparedWorkspaceEdit(
                service,
                identity,
                target,
                confirmed,
                session_id,
                base_revision,
                None,
                None,
                None,
                None,
            )
            self._pending[id(prepared)] = prepared
            return prepared
        try:
            _authority, permission, _scope, workspace, plan = (
                self._active_services()
            )
            if (
                self._current_chunk_id is None
                or self._selected_plan_binding != plan
                or self._selected_workspace_session_id
                != workspace.workspace_session_id
            ):
                raise ChunkError("CHUNK.OUTSIDE_CURRENT")
            segment = self._segment_ref(self._project_id, identity)
            capability = permission.issue_edit_capability(
                self._actor_port,
                self._actor_handle,
                workspace_binding=workspace,
                expected_plan_binding=plan,
                segment=segment,
            )
        except ChunkError as error:
            raise EditorControllerError(error.code) from error
        prepared = _PreparedWorkspaceEdit(
            service,
            identity,
            target,
            confirmed,
            session_id,
            base_revision,
            workspace,
            plan,
            segment,
            capability,
        )
        self._pending[id(prepared)] = prepared
        return prepared

    def commit_segment_edit(
        self,
        preparation: object,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
        session_id: str,
        base_revision: int,
    ) -> WorkspaceEditReceipt:
        prepared = self._take_prepared(
            preparation,
            service,
            identity,
            target=target,
            confirmed=confirmed,
            session_id=session_id,
            base_revision=base_revision,
        )
        if prepared.capability is None:
            if self._mode is not ChunkControllerSessionMode.NO_PLAN:
                raise EditorControllerError("CHUNK.PERMISSION_STALE")
            try:
                return service.update_segment_edit(
                    identity,
                    target=target,
                    confirmed=confirmed,
                    session_id=session_id,
                    base_revision=base_revision,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
        if (
            prepared.workspace_binding is None
            or prepared.plan_binding is None
            or prepared.segment is None
            or self._permission is None
        ):
            raise EditorControllerError("CHUNK.PERMISSION_STALE")
        port = _WorkspaceEditMutationPort(
            service,
            identity,
            target=target,
            confirmed=confirmed,
            session_id=session_id,
            base_revision=base_revision,
        )
        try:
            result = self._permission.execute_segment_edit(
                prepared.capability,
                self._actor_port,
                self._actor_handle,
                port,
                workspace_binding=prepared.workspace_binding,
                expected_plan_binding=prepared.plan_binding,
                segment=prepared.segment,
            )
        except ChunkError as error:
            raise EditorControllerError(error.code) from error
        if type(result) is not WorkspaceEditReceipt or port.calls != 1:
            raise EditorControllerError("CHUNK.COMMIT_FAILED")
        return result

    def _take_prepared(
        self,
        preparation: object,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
        session_id: str,
        base_revision: int,
    ) -> _PreparedWorkspaceEdit:
        prepared = self._pending.pop(id(preparation), None)
        if (
            type(prepared) is not _PreparedWorkspaceEdit
            or prepared is not preparation
            or prepared.consumed
            or prepared.service is not service
            or prepared.identity != identity
            or prepared.target != target
            or prepared.confirmed is not confirmed
            or prepared.session_id != session_id
            or prepared.base_revision != base_revision
        ):
            raise EditorControllerError("CHUNK.PERMISSION_STALE")
        prepared.consumed = True
        return prepared

    def commit_confirmed_edit(
        self,
        preparation: object,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        session_id: str,
        base_revision: int,
        publish_resources: Callable[[], object],
    ) -> tuple[object, object | None]:
        if not callable(publish_resources):
            raise TypeError("confirmed resource publication must be callable")
        prepared = self._take_prepared(
            preparation,
            service,
            identity,
            target=target,
            confirmed=True,
            session_id=session_id,
            base_revision=base_revision,
        )
        if prepared.capability is None:
            if self._mode is not ChunkControllerSessionMode.NO_PLAN:
                raise EditorControllerError("CHUNK.PERMISSION_STALE")
            report = publish_resources()
            if not bool(getattr(report, "succeeded", False)):
                return report, None
            try:
                receipt = service.update_segment_edit(
                    identity,
                    target=target,
                    confirmed=True,
                    session_id=session_id,
                    base_revision=base_revision,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            return report, receipt
        if (
            prepared.workspace_binding is None
            or prepared.plan_binding is None
            or prepared.segment is None
            or self._permission is None
        ):
            raise EditorControllerError("CHUNK.PERMISSION_STALE")
        port = _WorkspaceEditMutationPort(
            service,
            identity,
            target=target,
            confirmed=True,
            session_id=session_id,
            base_revision=base_revision,
            publish_resources=publish_resources,
        )
        try:
            result = self._permission.execute_segment_edit(
                prepared.capability,
                self._actor_port,
                self._actor_handle,
                port,
                workspace_binding=prepared.workspace_binding,
                expected_plan_binding=prepared.plan_binding,
                segment=prepared.segment,
            )
        except ChunkError as error:
            raise EditorControllerError(error.code) from error
        if (
            type(result) is not tuple
            or len(result) != 2
            or port.calls != 1
        ):
            raise EditorControllerError("CHUNK.COMMIT_FAILED")
        return result

    def cancel_segment_edit(self, preparation: object) -> None:
        prepared = self._pending.pop(id(preparation), None)
        if prepared is not None:
            prepared.consumed = True

    def search_workspace(
        self,
        request: CollaborativeWorkspaceSearchRequestV2,
    ) -> CollaborativeWorkspaceSearchReportV2:
        if type(request) is not CollaborativeWorkspaceSearchRequestV2:
            raise TypeError("collaborative search request must be v2")
        request.__post_init__()
        _authority, permission, scope_service, workspace, plan = (
            self._active_services()
        )
        v1_scope = (
            SearchScope.CURRENT_DOCUMENT
            if request.scope is CollaborativeSearchScopeV2.CURRENT_DOCUMENT
            else SearchScope.ENTIRE_PROJECT
        )
        v1_request = WorkspaceSearchRequest(
            query=request.query,
            fields=request.fields,
            options=request.options,
            status=request.status,
            scope=v1_scope,
        )
        projection: ChunkScopeProjection | None = None
        if request.scope is CollaborativeSearchScopeV2.CURRENT_CHUNK:
            projection = self.issue_current_scope_projection()
            projection = scope_service.revalidate_scope_projection(projection)
            members = tuple(member.identity for member in projection.members)
            base_report = (
                self._controller._search_workspace_selection_for_chunk_controller(
                    v1_request,
                    members,
                )
            )
        else:
            base_report = self._controller.search_workspace(v1_request)
        controller_hits = tuple(base_report.hits)
        hits = tuple(
            CollaborativeWorkspaceSearchHitV2(
                workspace_hit=replace(hit),
                access=permission.decide_access(
                    self._actor_port,
                    self._actor_handle,
                    workspace_binding=workspace,
                    expected_plan_binding=plan,
                    segment=self._segment_ref(
                        self._project_id,
                        SegmentIdentity(hit.document_id, hit.local_segment_id),
                    ),
                ),
            )
            for hit in base_report.hits
        )
        report = CollaborativeWorkspaceSearchReportV2(
            hits=hits,
            capability=base_report.capability,
        )
        self._issued_search = (
            report,
            controller_hits,
            workspace,
            plan,
            self._selection_generation,
            projection,
        )
        return report

    def go_to_search_hit(
        self,
        hit: CollaborativeWorkspaceSearchHitV2,
    ) -> object:
        issued = self._issued_search
        if issued is None or not any(hit is item for item in issued[0].hits):
            raise EditorControllerError("PROJECT_SEARCH.HIT_NOT_ISSUED")
        report, controller_hits, workspace, plan, generation, projection = issued
        issued_index = next(
            (index for index, item in enumerate(report.hits) if hit is item),
            None,
        )
        if issued_index is None:
            raise EditorControllerError("PROJECT_SEARCH.HIT_NOT_ISSUED")
        try:
            _authority, permission, scope, live_workspace, live_plan = (
                self._active_services()
            )
            if (
                workspace != live_workspace
                or plan != live_plan
                or generation != self._selection_generation
            ):
                raise ChunkError("CHUNK.PERMISSION_STALE")
            if projection is not None:
                scope.revalidate_scope_projection(projection)
            decision = permission.decide_access(
                self._actor_port,
                self._actor_handle,
                workspace_binding=live_workspace,
                expected_plan_binding=live_plan,
                segment=hit.access.segment,
            )
            if decision != hit.access:
                raise ChunkError("CHUNK.PERMISSION_STALE")
        except ChunkError as error:
            raise EditorControllerError(error.code) from error
        return self._controller.go_to_workspace_search_hit(
            controller_hits[issued_index]
        )

    # ---- C4 product projection -------------------------------------------------

    def _project_ordered_live_entries(self):
        """Overlay canonical universe facts onto Workspace-owned visible order."""

        owner = self._owner()
        before = self._live_universe()
        ordered_identities = tuple(
            SegmentIdentity(
                item.identity.document_id,
                item.identity.local_segment_id,
            )
            for item in owner.flat_segments
        )
        after = self._live_universe()
        if before != after:
            raise ChunkError("CHUNK.PERMISSION_STALE")
        by_identity = {entry.segment.identity: entry for entry in before.entries}
        if (
            len(by_identity) != len(before.entries)
            or len(ordered_identities) != len(before.entries)
            or set(ordered_identities) != set(by_identity)
        ):
            raise ChunkError("CHUNK.PERMISSION_STALE")
        return before, tuple(by_identity[identity] for identity in ordered_identities)

    def segment_choices(self) -> tuple[ChunkApplicationSegmentChoice, ...]:
        """Return manager-owned body-free range rows in live project order."""

        owner = self._owner()
        _live, ordered_entries = self._project_ordered_live_entries()
        documents = {
            document.document_id: document.display_name
            for document in owner.workspace.documents
        }
        authority = self._authority
        snapshot = self._current_plan_snapshot()
        membership = {}
        if snapshot is not None:
            for chunk in snapshot.chunks:
                for member in chunk.members:
                    membership[member.identity] = (chunk.chunk_id, chunk.name)
        choices = []
        for entry in ordered_entries:
            identity = entry.segment.identity
            chunk = membership.get(identity)
            choice = ChunkApplicationSegmentChoice(
                identity=SegmentIdentity(
                    identity.document_id,
                    identity.local_segment_id,
                ),
                document_label=documents.get(identity.document_id, "未知文档"),
                segment_label=identity.local_segment_id,
                chunk_id=None if chunk is None else chunk[0],
                chunk_label=None if chunk is None else chunk[1],
                attached=entry.source_presence.value == "attached",
            )
            choice.__post_init__()
            choices.append(choice)
        return tuple(choices)

    @staticmethod
    def _plain_identity(value: object) -> SegmentIdentity:
        try:
            nested = getattr(value, "segment_identity", None)
            if type(nested) is SegmentIdentity:
                nested.__post_init__()
                return SegmentIdentity(
                    nested.document_id,
                    nested.local_segment_id,
                )
            identity = SegmentIdentity(
                getattr(value, "document_id"),
                getattr(value, "local_segment_id"),
            )
        except Exception:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from None
        identity.__post_init__()
        return identity

    def _current_segment_identity(self) -> SegmentIdentity:
        view = self._controller.workspace_view
        return self._plain_identity(view.current_segment)

    def project_view(self) -> ChunkApplicationProjectView:
        """Return one frozen product projection without private authority."""

        owner = self._owner()
        identity = self._current_segment_identity()
        if self._mode is ChunkControllerSessionMode.BLOCKED:
            return ChunkApplicationProjectView(
                mode=ChunkApplicationMode.BLOCKED,
                project_id=self._project_id,
                chunk_plan_id=None,
                plan_revision=None,
                current_chunk_id=None,
                reference_label="本机工作流身份（非账号）",
                chunks=(),
                unallocated_count=0,
                current_segment_access=ChunkApplicationAccessView(
                    identity,
                    "read_only_stale",
                    False,
                    False,
                    (self._safe_code or "CHUNK.PERMISSION_STALE",),
                ),
                safe_code=self._safe_code or "CHUNK.PERMISSION_STALE",
            )
        live = self._live_universe()
        authority = self._authority
        snapshot = self._current_plan_snapshot()
        if snapshot is None:
            return ChunkApplicationProjectView(
                mode=ChunkApplicationMode.NO_PLAN,
                project_id=self._project_id,
                chunk_plan_id=None,
                plan_revision=None,
                current_chunk_id=None,
                reference_label="本机工作流身份（非账号）",
                chunks=(),
                unallocated_count=sum(
                    entry.source_presence.value == "attached"
                    for entry in live.entries
                ),
                current_segment_access=ChunkApplicationAccessView(
                    identity,
                    "legacy_editable_no_plan",
                    True,
                    True,
                    (),
                ),
                safe_code=None,
            )
        if snapshot is not None and self._safe_code == "CHUNK.REBASE_REQUIRED":
            return ChunkApplicationProjectView(
                mode=ChunkApplicationMode.BLOCKED,
                project_id=self._project_id,
                chunk_plan_id=snapshot.chunk_plan_id,
                plan_revision=snapshot.revision,
                current_chunk_id=None,
                reference_label="本机工作流身份（非账号）",
                chunks=(),
                unallocated_count=0,
                current_segment_access=ChunkApplicationAccessView(
                    identity,
                    "read_only_stale",
                    False,
                    False,
                    ("CHUNK.REBASE_REQUIRED",),
                ),
                safe_code="CHUNK.REBASE_REQUIRED",
            )
        self._revoke_current_selection_if_stale(snapshot, live_binding=live.binding)
        binding = chunk_plan_binding(snapshot)
        if binding.segment_universe_digest != live.binding.segment_universe_digest:
            return ChunkApplicationProjectView(
                mode=ChunkApplicationMode.BLOCKED,
                project_id=self._project_id,
                chunk_plan_id=snapshot.chunk_plan_id,
                plan_revision=snapshot.revision,
                current_chunk_id=None,
                reference_label="本机工作流身份（非账号）",
                chunks=(),
                unallocated_count=0,
                current_segment_access=ChunkApplicationAccessView(
                    identity,
                    "read_only_stale",
                    False,
                    False,
                    ("CHUNK.REBASE_REQUIRED",),
                ),
                safe_code="CHUNK.REBASE_REQUIRED",
            )
        progress_service = self._progress
        permission = self._permission
        if progress_service is None or permission is None:
            raise ChunkError("CHUNK.PERMISSION_STALE")
        chunks = []
        allocated = set()
        for chunk in snapshot.chunks:
            allocated.update(member.identity for member in chunk.members)
            progress = progress_service.progress(
                workspace_binding=live.binding,
                expected_plan_binding=binding,
                chunk_id=chunk.chunk_id,
            )
            assigned_here = chunk.assignee == self._actor_ref
            chunks.append(
                ChunkApplicationChunkView(
                    chunk_id=chunk.chunk_id,
                    name=chunk.name,
                    order=chunk.order,
                    assignee_label=(
                        "未分配"
                        if chunk.assignee is None
                        else (
                            "本机当前工作流身份（非账号）"
                            if assigned_here
                            else "其他工作流身份（非账号）"
                        )
                    ),
                    assigned_to_current_reference=assigned_here,
                    member_count=len(chunk.members),
                    progress=ChunkApplicationProgressView(
                        attached_total=progress.attached_total,
                        unfilled=progress.unfilled,
                        draft=progress.draft,
                        confirmed=progress.confirmed,
                        detached=progress.detached,
                    ),
                    is_current=chunk.chunk_id == self._current_chunk_id,
                )
            )
        decision = permission.decide_access(
            self._actor_port,
            self._actor_handle,
            workspace_binding=live.binding,
            expected_plan_binding=binding,
            segment=self._segment_ref(self._project_id, identity),
        )
        return ChunkApplicationProjectView(
            mode=ChunkApplicationMode.ACTIVE,
            project_id=self._project_id,
            chunk_plan_id=snapshot.chunk_plan_id,
            plan_revision=snapshot.revision,
            current_chunk_id=self._current_chunk_id,
            reference_label="本机工作流身份（非账号）",
            chunks=tuple(chunks),
            unallocated_count=sum(
                entry.source_presence.value == "attached"
                and entry.segment.identity not in allocated
                for entry in live.entries
            ),
            current_segment_access=ChunkApplicationAccessView(
                identity=identity,
                access=decision.access.value,
                may_edit_target=decision.may_edit_target,
                may_change_confirmed=decision.may_change_confirmed,
                safe_codes=decision.safe_codes,
            ),
            safe_code=None,
        )

    # ---- C4 body-safe preview/apply façade ------------------------------------

    def _management_context(self, action: TopologyAction):
        if self._mode is ChunkControllerSessionMode.BLOCKED:
            raise ChunkError(self._safe_code or "CHUNK.PERMISSION_STALE")
        if (
            self._safe_code == "CHUNK.REBASE_REQUIRED"
            and action not in {TopologyAction.REBASE, TopologyAction.DISSOLVE_PLAN}
        ):
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        authority = self._authority
        if authority is None:
            raise ChunkError("CHUNK.PERMISSION_STALE")
        snapshot = authority.current_snapshot()
        if snapshot is None and action is not TopologyAction.CREATE:
            raise ChunkError("CHUNK.REVISION_STALE")
        if snapshot is not None and action is TopologyAction.CREATE:
            expected = chunk_plan_binding(snapshot)
        else:
            expected = None if snapshot is None else chunk_plan_binding(snapshot)
        workspace = self._live_universe().binding
        capability = authority.issue_manager_capability(
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=expected,
            action=action,
        )
        return authority, capability, workspace, expected

    def _refs(
        self,
        identities: tuple[SegmentIdentity, ...],
    ) -> tuple[ChunkSegmentRef, ...]:
        if type(identities) is not tuple or any(
            type(identity) is not SegmentIdentity for identity in identities
        ):
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        return tuple(
            self._segment_ref(self._project_id, identity)
            for identity in identities
        )

    def _register_application_preview(
        self,
        private_preview: object,
        capability: ChunkManagerCapability | None,
        workspace: ChunkWorkspaceBinding | None,
        plan: ChunkPlanBinding | None,
        kind: str,
    ) -> ChunkApplicationMutationPreview:
        if type(private_preview) is ChunkRebasePreview:
            mutation = private_preview.mutation
            missing = len(private_preview.inspection.missing_members)
            unallocated = len(private_preview.inspection.new_unallocated_members)
            classification = "workspace_rebase"
        elif type(private_preview) is chunk_conflict.ChunkUndoPreview:
            mutation = private_preview.mutation
            missing = unallocated = 0
            classification = "current_head"
        elif type(private_preview) is chunk_conflict.ChunkConflictPreview:
            mutation = private_preview.replacement
            public = ChunkApplicationMutationPreview(
                operation_id=private_preview.preview_id,
                action="metadata_conflict",
                project_id=private_preview.project_id,
                chunk_plan_id=(
                    None
                    if private_preview.current_plan_binding is None
                    else private_preview.current_plan_binding.chunk_plan_id
                ),
                base_revision=(
                    None
                    if private_preview.current_plan_binding is None
                    else private_preview.current_plan_binding.plan_revision
                ),
                published_revision=(
                    None if mutation is None else mutation.published_revision
                ),
                affected_chunk_ids=(
                    () if mutation is None else mutation.affected_chunk_ids
                ),
                created_chunk_ids=(
                    () if mutation is None else mutation.created_chunk_ids
                ),
                retired_chunk_ids=(
                    () if mutation is None else mutation.retired_chunk_ids
                ),
                affected_chunk_count=(
                    0 if mutation is None else mutation.affected_chunk_count
                ),
                created_chunk_count=(
                    0 if mutation is None else mutation.created_chunk_count
                ),
                retired_chunk_count=(
                    0 if mutation is None else mutation.retired_chunk_count
                ),
                affected_member_count=(
                    0 if mutation is None else mutation.affected_member_count
                ),
                assignment_count=(
                    0 if mutation is None else mutation.assignment_count
                ),
                missing_member_count=0,
                new_unallocated_count=0,
                warnings=(),
                blockers=private_preview.blockers,
                truncated=False if mutation is None else mutation.truncated,
                classification=private_preview.classification.value,
            )
            self._pending_application[id(public)] = _PreparedApplicationMutation(
                public, private_preview, capability, workspace, plan, kind
            )
            return public
        elif type(private_preview) is ChunkMutationPreview:
            mutation = private_preview
            missing = unallocated = 0
            classification = None
        else:
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        public = ChunkApplicationMutationPreview(
            operation_id=mutation.operation_id,
            action=mutation.action.value,
            project_id=mutation.project_id,
            chunk_plan_id=mutation.chunk_plan_id,
            base_revision=mutation.base_revision,
            published_revision=mutation.published_revision,
            affected_chunk_ids=mutation.affected_chunk_ids,
            created_chunk_ids=mutation.created_chunk_ids,
            retired_chunk_ids=mutation.retired_chunk_ids,
            affected_chunk_count=mutation.affected_chunk_count,
            created_chunk_count=mutation.created_chunk_count,
            retired_chunk_count=mutation.retired_chunk_count,
            affected_member_count=mutation.affected_member_count,
            assignment_count=mutation.assignment_count,
            missing_member_count=missing,
            new_unallocated_count=unallocated,
            warnings=mutation.warnings,
            blockers=mutation.blockers,
            truncated=mutation.truncated,
            classification=classification,
        )
        self._pending_application[id(public)] = _PreparedApplicationMutation(
            public, private_preview, capability, workspace, plan, kind
        )
        return public

    def _preview_topology(self, action: TopologyAction, method: str, **kwargs):
        _authority, capability, workspace, plan = self._management_context(action)
        topology = self._topology_service()
        preview = getattr(topology, method)(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
            **kwargs,
        )
        return self._register_application_preview(
            preview, capability, workspace, plan, "topology"
        )

    def preview_create_chunk(
        self, name: str, members: tuple[SegmentIdentity, ...]
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.CREATE,
            "preview_create",
            name=name,
            members=self._refs(members),
        )

    @staticmethod
    def _balanced_groups(values: tuple[object, ...], count: int) -> tuple[tuple[object, ...], ...]:
        if type(count) is not int or count < 2 or count > len(values):
            raise ChunkError("CHUNK.SPLIT_INVALID")
        width, remainder = divmod(len(values), count)
        groups = []
        offset = 0
        for index in range(count):
            size = width + int(index < remainder)
            groups.append(values[offset : offset + size])
            offset += size
        return tuple(groups)

    def preview_partition_project(
        self,
        names: tuple[str, ...],
    ) -> ChunkApplicationMutationPreview:
        """Create one multi-chunk plan from the live project in one publication."""

        if type(names) is not tuple or any(
            type(name) is not str or not name.strip() for name in names
        ):
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        _authority, capability, workspace, plan = self._management_context(
            TopologyAction.CREATE
        )
        if plan is not None:
            raise ChunkError("CHUNK.REVISION_STALE")
        topology = self._topology_service()
        _live, ordered_entries = self._project_ordered_live_entries()
        ordered = tuple(
            entry.segment
            for entry in ordered_entries
            if entry.source_presence.value == "attached"
        )
        groups = self._balanced_groups(ordered, len(names))
        preview = topology.preview_create_plan(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=None,
            children=tuple(
                ChunkSplitChild(
                    name=name.strip(),
                    # The groups are cut from live project order.  Canonical
                    # storage order is applied only inside each already-cut
                    # contiguous group, never before partitioning.
                    members=canonicalize_chunk_members(group),
                    assignee=None,
                    assignment_decided=True,
                )
                for name, group in zip(names, groups, strict=True)
            ),
        )
        return self._register_application_preview(
            preview, capability, workspace, plan, "topology"
        )

    def preview_rename_chunk(
        self, chunk_id: str, name: str
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.RENAME, "preview_rename", chunk_id=chunk_id, name=name
        )

    def preview_reorder_chunks(
        self, ordered_chunk_ids: tuple[str, ...]
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.REORDER,
            "preview_reorder",
            ordered_chunk_ids=ordered_chunk_ids,
        )

    def preview_split_chunk(
        self,
        source_chunk_id: str,
        children: tuple[ChunkApplicationSplitChild, ...],
    ) -> ChunkApplicationMutationPreview:
        if type(children) is not tuple or any(
            type(child) is not ChunkApplicationSplitChild for child in children
        ):
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        private_children = tuple(
            ChunkSplitChild(
                name=child.name,
                members=canonicalize_chunk_members(self._refs(child.members)),
                assignee=(
                    self._actor_ref
                    if child.assign_to_current_reference is True
                    else None
                ),
                assignment_decided=child.assign_to_current_reference is not None,
            )
            for child in children
        )
        return self._preview_topology(
            TopologyAction.SPLIT,
            "preview_split",
            source_chunk_id=source_chunk_id,
            children=private_children,
        )

    def preview_split_chunk_evenly(
        self,
        source_chunk_id: str,
        names: tuple[str, ...],
        assignment_decision: str,
    ) -> ChunkApplicationMutationPreview:
        """Split all live members of one chunk without editor-row selection."""

        if type(names) is not tuple or any(
            type(name) is not str or not name.strip() for name in names
        ):
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        if assignment_decision not in {"inherit", "unassign"}:
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        _authority, capability, workspace, plan = self._management_context(
            TopologyAction.SPLIT
        )
        if plan is None or self._authority is None:
            raise ChunkError("CHUNK.REVISION_STALE")
        snapshot = self._authority.current_snapshot()
        if snapshot is None:
            raise ChunkError("CHUNK.REVISION_STALE")
        source = next(
            (chunk for chunk in snapshot.chunks if chunk.chunk_id == source_chunk_id),
            None,
        )
        if source is None:
            raise ChunkError("CHUNK.IDENTITY_FOREIGN")
        member_set = set(source.members)
        _live, ordered_entries = self._project_ordered_live_entries()
        ordered = tuple(
            entry.segment
            for entry in ordered_entries
            if entry.segment in member_set
        )
        if len(ordered) != len(member_set):
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        groups = self._balanced_groups(ordered, len(names))
        inherited = source.assignee if assignment_decision == "inherit" else None
        preview = self._topology_service().preview_split(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
            source_chunk_id=source_chunk_id,
            children=tuple(
                ChunkSplitChild(
                    name=name.strip(),
                    # Keep the same contiguous live-order membership while
                    # satisfying the domain's canonical storage contract.
                    members=canonicalize_chunk_members(group),
                    assignee=inherited,
                    assignment_decided=True,
                )
                for name, group in zip(names, groups, strict=True)
            ),
        )
        return self._register_application_preview(
            preview, capability, workspace, plan, "topology"
        )

    def preview_merge_chunks(
        self,
        source_chunk_ids: tuple[str, ...],
        result_name: str | None,
        *,
        assign_to_current_reference: bool | None = None,
    ) -> ChunkApplicationMutationPreview:
        if type(source_chunk_ids) is not tuple or any(
            type(chunk_id) is not str for chunk_id in source_chunk_ids
        ):
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        if result_name is None or (type(result_name) is str and not result_name.strip()):
            authority = self._authority
            snapshot = None if authority is None else authority.current_snapshot()
            if snapshot is None:
                raise ChunkError("CHUNK.REVISION_STALE")
            source_set = set(source_chunk_ids)
            surviving_names = {
                chunk.name
                for chunk in snapshot.chunks
                if chunk.chunk_id not in source_set
            }
            base = "合并分工"
            result_name = base
            suffix = 2
            while result_name in surviving_names:
                result_name = f"{base} {suffix}"
                suffix += 1
        elif type(result_name) is not str:
            raise ChunkError("CHUNK.CONTRACT_INVALID")
        return self._preview_topology(
            TopologyAction.MERGE,
            "preview_merge",
            source_chunk_ids=source_chunk_ids,
            result_name=result_name.strip(),
            result_assignee=(
                self._actor_ref if assign_to_current_reference is True else None
            ),
            result_assignment_decided=assign_to_current_reference is not None,
        )

    def preview_move_members(
        self,
        source_chunk_id: str,
        destination_chunk_id: str,
        members: tuple[SegmentIdentity, ...],
        *,
        retire_source_if_empty: bool,
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.MOVE,
            "preview_move",
            source_chunk_id=source_chunk_id,
            destination_chunk_id=destination_chunk_id,
            members=self._refs(members),
            retire_source_if_empty=retire_source_if_empty,
        )

    def preview_release_members(
        self,
        source_chunk_id: str,
        members: tuple[SegmentIdentity, ...],
        *,
        retire_source_if_empty: bool,
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.RELEASE,
            "preview_release",
            source_chunk_id=source_chunk_id,
            members=self._refs(members),
            retire_source_if_empty=retire_source_if_empty,
        )

    def preview_dissolve_chunk(
        self, chunk_id: str
    ) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.DISSOLVE_CHUNK,
            "preview_dissolve_chunk",
            chunk_id=chunk_id,
        )

    def preview_dissolve_plan(self) -> ChunkApplicationMutationPreview:
        return self._preview_topology(
            TopologyAction.DISSOLVE_PLAN, "preview_dissolve_plan"
        )

    def _preview_assignment(
        self, action: TopologyAction, chunk_id: str
    ) -> ChunkApplicationMutationPreview:
        _authority, capability, workspace, plan = self._management_context(action)
        if plan is None:
            raise ChunkError("CHUNK.REVISION_STALE")
        assignment = self._assignment_service()
        kwargs = {}
        if action is not TopologyAction.UNASSIGN:
            kwargs = {
                "target_actor_port": self._actor_port,
                "target_actor_handle": self._actor_handle,
            }
        method = {
            TopologyAction.ASSIGN: assignment.preview_assign,
            TopologyAction.REASSIGN: assignment.preview_reassign,
            TopologyAction.UNASSIGN: assignment.preview_unassign,
        }[action]
        preview = method(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
            chunk_id=chunk_id,
            **kwargs,
        )
        return self._register_application_preview(
            preview, capability, workspace, plan, "assignment"
        )

    def preview_assign_to_current_reference(self, chunk_id: str):
        return self._preview_assignment(TopologyAction.ASSIGN, chunk_id)

    def preview_reassign_to_current_reference(self, chunk_id: str):
        return self._preview_assignment(TopologyAction.REASSIGN, chunk_id)

    def preview_unassign_chunk(self, chunk_id: str):
        return self._preview_assignment(TopologyAction.UNASSIGN, chunk_id)

    def inspect_workspace_rebase(self) -> ChunkApplicationRebaseInspection:
        _authority, capability, workspace, plan = self._management_context(
            TopologyAction.REBASE
        )
        if plan is None:
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        topology = self._topology_service()
        inspection = topology.inspect_rebase(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
        )
        snapshot = self._authority.current_snapshot() if self._authority else None
        if snapshot is None:
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        missing = set(inspection.missing_members)
        empty_ids = tuple(
            chunk.chunk_id
            for chunk in snapshot.chunks
            if set(chunk.members).issubset(missing)
        )
        public = ChunkApplicationRebaseInspection(
            missing_members=tuple(
                SegmentIdentity(
                    member.identity.document_id,
                    member.identity.local_segment_id,
                )
                for member in inspection.missing_members
            ),
            new_unallocated_count=len(inspection.new_unallocated_members),
            empty_chunk_ids=empty_ids,
            all_chunks_empty=bool(
                snapshot.chunks and len(empty_ids) == len(snapshot.chunks)
            ),
        )
        public.__post_init__()
        return public

    def preview_workspace_rebase(
        self,
        released_missing_members: tuple[SegmentIdentity, ...] = (),
        retire_empty_chunk_ids: tuple[str, ...] = (),
    ) -> ChunkApplicationMutationPreview:
        _authority, capability, workspace, plan = self._management_context(
            TopologyAction.REBASE
        )
        if plan is None:
            raise ChunkError("CHUNK.REBASE_REQUIRED")
        topology = self._topology_service()
        inspection = topology.inspect_rebase(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
        )
        if type(released_missing_members) is not tuple or type(
            retire_empty_chunk_ids
        ) is not tuple:
            raise ChunkError("CHUNK.REBASE_DECISION_REQUIRED")
        preview = topology.preview_rebase(
            capability,
            self._manager,
            workspace_binding=workspace,
            expected_plan_binding=plan,
            released_missing_members=self._refs(released_missing_members),
            retire_empty_chunk_ids=retire_empty_chunk_ids,
        )
        return self._register_application_preview(
            preview, capability, workspace, plan, "rebase"
        )

    def preview_undo_current_head(self) -> ChunkApplicationMutationPreview:
        authority = self._authority
        conflict = self._conflict
        if authority is None or conflict is None:
            raise ChunkError("CHUNK.UNDO_UNAVAILABLE")
        receipts = authority.operation_receipts()
        if not receipts:
            raise ChunkError("CHUNK.UNDO_UNAVAILABLE")
        preview = conflict.preview_undo(
            receipts[-1].operation_id,
            manager=self._manager,
        )
        capability = authority.issue_manager_capability(
            self._manager,
            workspace_binding=preview.workspace_binding,
            expected_plan_binding=chunk_plan_binding(authority.current_snapshot()),
            action=TopologyAction.UNDO,
        )
        return self._register_application_preview(
            preview,
            capability,
            preview.workspace_binding,
            chunk_plan_binding(authority.current_snapshot()),
            "undo",
        )

    def preview_metadata_conflict(
        self, incoming_payload: bytes
    ) -> ChunkApplicationMutationPreview:
        if self._conflict is None:
            raise ChunkError("CHUNK.METADATA_UNAVAILABLE")
        preview = self._conflict.preview(incoming_payload)
        return self._register_application_preview(
            preview, None, preview.workspace_binding, preview.current_plan_binding, "conflict"
        )

    def _take_application_preview(
        self, preview: ChunkApplicationMutationPreview
    ) -> _PreparedApplicationMutation:
        record = self._pending_application.pop(id(preview), None)
        if (
            type(preview) is not ChunkApplicationMutationPreview
            or record is None
            or record.public is not preview
            or record.consumed
        ):
            raise ChunkError("CHUNK.PREVIEW_STALE")
        record.consumed = True
        return record

    def apply_mutation(
        self,
        preview: ChunkApplicationMutationPreview,
        *,
        conflict_resolution: str | None = None,
    ) -> ChunkApplicationMutationReceipt:
        record = self._take_application_preview(preview)
        if record.kind == "topology":
            if self._topology is None or record.capability is None:
                raise ChunkError("CHUNK.PREVIEW_STALE")
            receipt = self._topology.apply_topology(
                record.private_preview,
                record.capability,
                self._manager,
                workspace_binding=record.workspace_binding,
                expected_plan_binding=record.plan_binding,
            )
        elif record.kind == "assignment":
            if self._assignment is None or record.capability is None:
                raise ChunkError("CHUNK.PREVIEW_STALE")
            receipt = self._assignment.apply_assignment(
                record.private_preview,
                record.capability,
                self._manager,
                workspace_binding=record.workspace_binding,
                expected_plan_binding=record.plan_binding,
            )
        elif record.kind == "rebase":
            if self._topology is None or record.capability is None:
                raise ChunkError("CHUNK.PREVIEW_STALE")
            receipt = self._topology.apply_rebase(
                record.private_preview,
                record.capability,
                self._manager,
                workspace_binding=record.workspace_binding,
                expected_plan_binding=record.plan_binding,
            )
            self._accepted_workspace_transition = None
        elif record.kind == "undo":
            if self._conflict is None or record.capability is None:
                raise ChunkError("CHUNK.PREVIEW_STALE")
            receipt = self._conflict.apply_undo(
                record.private_preview,
                record.capability,
                self._manager,
            )
        elif record.kind == "conflict":
            if self._conflict is None:
                raise ChunkError("CHUNK.PREVIEW_STALE")
            try:
                resolution = chunk_conflict.ChunkConflictResolution(
                    conflict_resolution or "auto"
                )
            except ValueError:
                raise ChunkError("CHUNK.CONFLICT_RESOLUTION_INVALID") from None
            private = record.private_preview
            capability = None
            if private.required_action is not None:
                authority = self._authority
                if authority is None:
                    raise ChunkError("CHUNK.PREVIEW_STALE")
                capability = authority.issue_manager_capability(
                    self._manager,
                    workspace_binding=private.workspace_binding,
                    expected_plan_binding=private.current_plan_binding,
                    action=private.required_action,
                )
            receipt = self._conflict.apply(
                private,
                resolution,
                capability=capability,
                manager=self._manager if capability is not None else None,
            )
            if receipt is None:
                self._refresh_after_application_mutation()
                return ChunkApplicationMutationReceipt(
                    operation_id=preview.operation_id,
                    action="metadata_conflict_" + resolution.value,
                    project_id=preview.project_id,
                    chunk_plan_id=preview.chunk_plan_id,
                    published_revision=preview.base_revision,
                    affected_chunk_count=0,
                    affected_member_count=0,
                    assignment_count=0,
                    safe_issues=(),
                )
        else:
            raise ChunkError("CHUNK.PREVIEW_STALE")
        if type(receipt) is not ChunkOperationReceipt:
            raise ChunkError("CHUNK.COMMIT_FAILED")
        self._refresh_after_application_mutation()
        return ChunkApplicationMutationReceipt(
            operation_id=receipt.operation_id,
            action=receipt.action.value,
            project_id=receipt.project_id,
            chunk_plan_id=receipt.chunk_plan_id,
            published_revision=receipt.published_revision,
            affected_chunk_count=receipt.affected_chunk_count,
            affected_member_count=receipt.affected_member_count,
            assignment_count=receipt.assignment_count,
            safe_issues=receipt.safe_issues,
        )

    def _refresh_after_application_mutation(self) -> None:
        authority = self._authority
        snapshot = None if authority is None else authority.current_snapshot()
        self._mode = (
            ChunkControllerSessionMode.NO_PLAN
            if snapshot is None
            else ChunkControllerSessionMode.ACTIVE
        )
        self._safe_code = None
        if self._permission is not None:
            self._permission._revoke_actor_session_for_controller(self._actor_ref)
        self._current_chunk_id = None
        self._selected_plan_binding = None
        self._selected_workspace_session_id = None
        self._selection_generation += 1
        self._issued_search = None
        for pending in self._pending.values():
            pending.consumed = True
        self._pending.clear()
        for pending in self._pending_application.values():
            pending.consumed = True
        self._pending_application.clear()


__all__ = [
    "ChunkControllerAdapter",
    "ChunkControllerSessionMode",
    "ChunkControllerSessionView",
    "create_chunk_metadata_binding_resolver",
]
