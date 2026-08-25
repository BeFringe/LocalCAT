"""Exact owner-scope adapters for TMX export.

This module joins owner-issued Resource, Workspace, and Chunk facts.  It does
not decide TMX inclusion/loss, parse or write XML, bind destinations, or own a
carrier transaction.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Protocol

from collaborative_chunk_contracts import ChunkError, ChunkScopeProjection
from project_workspace import WorkspaceSessionView, WorkspaceUniverseProjection
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import ProjectWorkspaceError
from tmx_context_contracts import (
    TmxExportUnit,
    TmxOrderedProp,
    TmxPropScope,
    TmxProvenanceEntry,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_export_scope_contracts import (
    EntireProjectScopeMaterialization,
    ManagedResourceScopeBinding,
    ManagedResourceScopeMaterialization,
    SelectedChunkScopeBinding,
    SelectedChunkScopeMaterialization,
    TmxScopeCoordinatorError,
    WorkspaceScopeBinding,
    WorkspaceScopeSegment,
)
from tm_sqlite_store import CanonicalExportRecord, CanonicalExportSnapshot


class ManagedTmScopeOwner(Protocol):
    """Canonical resource owner seam used by the coordinator."""

    def capture_export_snapshot(self) -> CanonicalExportSnapshot: ...


class WorkspaceTmxScopeOwner(Protocol):
    """Workspace body/order and universe owner seam."""

    def capture_session_view(self) -> WorkspaceSessionView: ...

    def capture_workspace_universe(self) -> WorkspaceUniverseProjection: ...

    def revalidate_workspace_universe(
        self,
        projection: WorkspaceUniverseProjection,
    ) -> WorkspaceUniverseProjection: ...


class ChunkTmxScopeOwner(Protocol):
    """One explicitly selected active chunk membership seam."""

    def capture_scope_projection(self, chunk_id: str) -> ChunkScopeProjection: ...

    def revalidate_scope_projection(
        self,
        projection: ChunkScopeProjection,
    ) -> ChunkScopeProjection: ...


class WorkspaceUniverseOwner(Protocol):
    """Existing ProjectWorkspaceService universe seam."""

    def capture_workspace_universe(self) -> WorkspaceUniverseProjection: ...

    def validate_workspace_universe(
        self,
        projection: WorkspaceUniverseProjection,
    ) -> WorkspaceUniverseProjection: ...


class WorkspaceTmxScopeAdapter:
    """Adapt a frozen session-view provider plus the Workspace owner seam."""

    __slots__ = ("_session", "_universe")

    def __init__(
        self,
        session_view_provider: Callable[[], WorkspaceSessionView],
        universe_owner: WorkspaceUniverseOwner,
    ) -> None:
        if not callable(session_view_provider):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self._session = session_view_provider
        self._universe = universe_owner

    def capture_session_view(self) -> WorkspaceSessionView:
        return self._session()

    def capture_workspace_universe(self) -> WorkspaceUniverseProjection:
        try:
            return self._universe.capture_workspace_universe()
        except ProjectWorkspaceError as exc:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE") from exc

    def revalidate_workspace_universe(
        self,
        projection: WorkspaceUniverseProjection,
    ) -> WorkspaceUniverseProjection:
        try:
            return self._universe.validate_workspace_universe(projection)
        except ProjectWorkspaceError as exc:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE") from exc


class ChunkTmxScopeAdapter:
    """Adapt explicit chunk capture/revalidation callables without UI state."""

    __slots__ = ("_capture", "_revalidate")

    def __init__(
        self,
        capture: Callable[[str], ChunkScopeProjection],
        revalidate: Callable[[ChunkScopeProjection], ChunkScopeProjection],
    ) -> None:
        if not callable(capture) or not callable(revalidate):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self._capture = capture
        self._revalidate = revalidate

    def capture_scope_projection(self, chunk_id: str) -> ChunkScopeProjection:
        try:
            return self._capture(chunk_id)
        except ChunkError as exc:
            raise _mapped_chunk_error(exc) from exc

    def revalidate_scope_projection(
        self,
        projection: ChunkScopeProjection,
    ) -> ChunkScopeProjection:
        try:
            return self._revalidate(projection)
        except ChunkError as exc:
            raise _mapped_chunk_error(exc) from exc


def _mapped_chunk_error(error: ChunkError) -> TmxScopeCoordinatorError:
    if error.code == "CHUNK.IDENTITY_FOREIGN":
        return TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
    if error.code == "CHUNK.MEMBER_UNKNOWN":
        return TmxScopeCoordinatorError("TMX.SCOPE.MISSING")
    return TmxScopeCoordinatorError("TMX.SCOPE.STALE")


def _length_prefixed(digest: "hashlib._Hash", value: object) -> None:
    if type(value) is bool:
        encoded = b"b1" if value else b"b0"
    elif value is None:
        encoded = b"n"
    elif type(value) is int:
        encoded = b"i" + str(value).encode("ascii", errors="strict")
    elif type(value) is str:
        encoded = b"s" + value.encode("utf-8", errors="strict")
    else:
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    digest.update(len(encoded).to_bytes(8, "big", signed=False))
    digest.update(encoded)


def _scope_digest(domain: bytes, values: tuple[object, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    try:
        for value in values:
            _length_prefixed(digest, value)
    except UnicodeError:
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID") from None
    return digest.hexdigest()


def _resource_snapshot_digest(snapshot: CanonicalExportSnapshot) -> str:
    if type(snapshot) is not CanonicalExportSnapshot:
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    try:
        snapshot.__post_init__()
    except (TypeError, ValueError):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID") from None
    digest = hashlib.sha256()
    digest.update(b"localcat.tmx.managed-resource-scope.v1\0")
    revision = snapshot.revision
    for value in (
        revision.resource_id,
        revision.canonical_store_id,
        revision.generation,
        revision.head_revision,
        revision.record_count,
    ):
        _length_prefixed(digest, value)
    for exported in snapshot.records:
        record = exported.record
        for value in (
            record.record_id,
            record.source_raw,
            record.target_raw,
            record.speaker_raw,
            record.context_prev_raw,
            record.context_next_raw,
            record.file_source,
            record.legacy_line_no,
            record.origin_batch_id,
            record.origin_ordinal,
            exported.usage_count,
            exported.last_used,
        ):
            _length_prefixed(digest, value)
        _length_prefixed(digest, len(record.provenance))
        for key, value in record.provenance:
            _length_prefixed(digest, key)
            _length_prefixed(digest, value)
    return digest.hexdigest()


_SEMANTIC_PROP_TYPES = frozenset(
    {
        "x-localcat-context-prev",
        "x-localcat-context-next",
        "x-localcat-speaker",
        "x-localcat-file-source",
        "x-localcat-confirmed",
        "x-localcat-status",
        "x-localcat-provenance",
        "x-matecat-status",
    }
)

_PROP_SCOPE = {
    "tu": TmxPropScope.TU,
    "source_tuv": TmxPropScope.SOURCE_TUV,
    "target_tuv": TmxPropScope.TARGET_TUV,
}


def _resource_metadata(
    exported: CanonicalExportRecord,
) -> tuple[
    tuple[TmxProvenanceEntry, ...],
    tuple[TmxOrderedProp, ...],
    str | None,
    bool,
]:
    provenance: list[TmxProvenanceEntry] = []
    imported_props: list[TmxOrderedProp] = []
    status: str | None = None
    confirmed = True
    for key, value in exported.record.provenance:
        if key == "tmx.status":
            status = value
            continue
        if key != "tmx.prop":
            provenance.append(TmxProvenanceEntry(key=key, value=value))
            continue
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID") from None
        if (
            type(decoded) is not list
            or len(decoded) != 4
            or any(type(item) is not str for item in decoded)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        scope, prop_type, xml_lang, prop_value = decoded
        prop_scope = _PROP_SCOPE.get(scope)
        if prop_scope is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        normalized_type = prop_type.casefold()
        if normalized_type == "x-localcat-confirmed":
            normalized_value = prop_value.casefold()
            if normalized_value in {"true", "false"}:
                confirmed = normalized_value == "true"
                continue
        elif normalized_type == "x-localcat-provenance":
            try:
                semantic_value = json.loads(prop_value)
            except (TypeError, ValueError, json.JSONDecodeError):
                semantic_value = None
            if (
                type(semantic_value) is list
                and len(semantic_value) == 2
                and all(type(item) is str and item for item in semantic_value)
            ):
                continue
        elif normalized_type in _SEMANTIC_PROP_TYPES:
            continue
        imported_props.append(
            TmxOrderedProp(
                type=prop_type,
                value=prop_value,
                xml_lang=xml_lang or None,
                scope=prop_scope,
            )
        )
    return tuple(provenance), tuple(imported_props), status, confirmed


def _resource_unit(
    binding: ManagedResourceScopeBinding,
    exported: CanonicalExportRecord,
) -> TmxExportUnit:
    record = exported.record
    provenance, imported_props, status, confirmed = _resource_metadata(exported)
    return TmxExportUnit(
        unit_identity=f"resource:{binding.resource_id}:{record.record_id}",
        source=record.source_raw,
        target=record.target_raw,
        confirmed=confirmed,
        attached=True,
        speaker=record.speaker_raw,
        context_prev=record.context_prev_raw,
        context_next=record.context_next_raw,
        file_source=record.file_source,
        status=status,
        provenance=provenance,
        imported_props=imported_props,
    )


def _workspace_tmx_unit(
    project_id: str,
    segment: WorkspaceScopeSegment,
) -> TmxExportUnit:
    return TmxExportUnit(
        unit_identity=(
            f"project:{project_id}:"
            f"{segment.identity.document_id}:{segment.identity.local_segment_id}"
        ),
        source=segment.source_raw,
        target=segment.target_raw,
        confirmed=segment.confirmed,
        attached=segment.source_presence is SourcePresence.ATTACHED,
        speaker=segment.speaker_raw or None,
        context_prev=segment.context_prev_raw,
        context_next=segment.context_next_raw,
        file_source=segment.file_source or None,
        status="confirmed" if segment.confirmed else "draft",
    )


class TmxExportCoordinator:
    """Capture and revalidate exact export scopes from their domain owners."""

    __slots__ = ("_resource", "_workspace", "_chunk")

    def __init__(
        self,
        *,
        resource_owner: ManagedTmScopeOwner | None = None,
        workspace_owner: WorkspaceTmxScopeOwner | None = None,
        chunk_owner: ChunkTmxScopeOwner | None = None,
    ) -> None:
        self._resource = resource_owner
        self._workspace = workspace_owner
        self._chunk = chunk_owner

    def capture_managed_resource(self) -> ManagedResourceScopeMaterialization:
        if self._resource is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.UNAVAILABLE")
        snapshot = self._resource.capture_export_snapshot()
        if type(snapshot) is not CanonicalExportSnapshot:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        snapshot_digest = _resource_snapshot_digest(snapshot)
        revision = snapshot.revision
        binding = ManagedResourceScopeBinding(
            resource_id=revision.resource_id,
            canonical_store_id=revision.canonical_store_id,
            generation=revision.generation,
            head_revision=revision.head_revision,
            record_count=revision.record_count,
            snapshot_digest=snapshot_digest,
        )
        units = tuple(_resource_unit(binding, record) for record in snapshot.records)
        tmx_binding = TmxScopeBinding(
            scope_kind=TmxScopeKind.MANAGED_RESOURCE,
            scope_id=binding.resource_id,
            binding_digest=binding.snapshot_digest,
            unit_count=len(units),
            attached_count=len(units),
        )
        return ManagedResourceScopeMaterialization(
            binding=binding,
            tmx_binding=tmx_binding,
            units=units,
            records=snapshot.records,
            owner_snapshot=snapshot,
        )

    def revalidate_managed_resource(
        self,
        captured: ManagedResourceScopeMaterialization,
    ) -> ManagedResourceScopeMaterialization:
        if type(captured) is not ManagedResourceScopeMaterialization:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        fresh = self.capture_managed_resource()
        if (
            fresh.binding != captured.binding
            or fresh.tmx_binding != captured.tmx_binding
            or fresh.records != captured.records
            or fresh.units != captured.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return fresh

    def _capture_project(self) -> EntireProjectScopeMaterialization:
        owner = self._workspace
        if owner is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.UNAVAILABLE")
        session = owner.capture_session_view()
        universe = owner.capture_workspace_universe()
        if type(session) is not WorkspaceSessionView or type(
            universe
        ) is not WorkspaceUniverseProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        revalidated = owner.revalidate_workspace_universe(universe)
        if type(revalidated) is not WorkspaceUniverseProjection or revalidated != universe:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return _materialize_project(session, universe)

    def capture_entire_project(self) -> EntireProjectScopeMaterialization:
        return self._capture_project()

    def revalidate_entire_project(
        self,
        captured: EntireProjectScopeMaterialization,
    ) -> EntireProjectScopeMaterialization:
        if type(captured) is not EntireProjectScopeMaterialization:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        owner = self._workspace
        if owner is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.UNAVAILABLE")
        revalidated = owner.revalidate_workspace_universe(captured.owner_universe)
        if type(revalidated) is not WorkspaceUniverseProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        fresh = self._capture_project()
        if (
            fresh.binding != captured.binding
            or fresh.tmx_binding != captured.tmx_binding
            or fresh.segments != captured.segments
            or fresh.units != captured.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return fresh

    def capture_selected_chunk(
        self,
        explicit_chunk_id: str,
    ) -> SelectedChunkScopeMaterialization:
        if type(explicit_chunk_id) is not str or not explicit_chunk_id:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        chunk_owner = self._chunk
        if chunk_owner is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.UNAVAILABLE")
        projection = chunk_owner.capture_scope_projection(explicit_chunk_id)
        if type(projection) is not ChunkScopeProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if projection.chunk_id != explicit_chunk_id:
            raise TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
        project = self._capture_project()
        revalidated = chunk_owner.revalidate_scope_projection(projection)
        if type(revalidated) is not ChunkScopeProjection or revalidated != projection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return _materialize_chunk(project, projection)

    def revalidate_selected_chunk(
        self,
        captured: SelectedChunkScopeMaterialization,
    ) -> SelectedChunkScopeMaterialization:
        if type(captured) is not SelectedChunkScopeMaterialization:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        chunk_owner = self._chunk
        workspace_owner = self._workspace
        if chunk_owner is None or workspace_owner is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.UNAVAILABLE")
        universe = workspace_owner.revalidate_workspace_universe(
            captured.owner_project.owner_universe
        )
        projection = chunk_owner.revalidate_scope_projection(captured.owner_chunk)
        if type(universe) is not WorkspaceUniverseProjection or type(
            projection
        ) is not ChunkScopeProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        fresh = self.capture_selected_chunk(captured.binding.chunk_id)
        if (
            fresh.binding != captured.binding
            or fresh.tmx_binding != captured.tmx_binding
            or fresh.segments != captured.segments
            or fresh.units != captured.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
        return fresh


def _materialize_project(
    session: WorkspaceSessionView,
    universe: WorkspaceUniverseProjection,
) -> EntireProjectScopeMaterialization:
    project = session.project
    owner_binding = universe.binding
    if (
        project.project_id != owner_binding.project_id
        or any(document.identity.project != project for document in session.documents)
        or any(
            segment.identity.document.project != project for segment in session.segments
        )
    ):
        raise TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
    if (
        project.session_id != owner_binding.workspace_session_id
        or project.workspace_revision != owner_binding.workspace_revision
    ):
        raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")

    documents = {item.identity.document_id: item for item in session.documents}
    if len(documents) != len(session.documents):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    universe_by_identity = {item.identity: item for item in universe.entries}
    if len(universe_by_identity) != len(universe.entries):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    segment_by_identity = {
        item.identity.segment_identity: item for item in session.segments
    }
    if len(segment_by_identity) != len(session.segments):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    if set(segment_by_identity) != set(universe_by_identity):
        raise TmxScopeCoordinatorError("TMX.SCOPE.MISSING")
    if tuple(item.project_global_index for item in session.segments) != tuple(
        range(len(session.segments))
    ):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")

    local_groups: dict[str, list[object]] = {}
    for segment in session.segments:
        local_groups.setdefault(segment.identity.document.document_id, []).append(segment)
    for document_id, group in local_groups.items():
        if document_id not in documents or tuple(
            item.document_local_index for item in group
        ) != tuple(range(len(group))):
            raise TmxScopeCoordinatorError("TMX.SCOPE.MISSING")

    rows: list[WorkspaceScopeSegment] = []
    for index, segment in enumerate(session.segments):
        identity = segment.identity.segment_identity
        document = documents.get(identity.document_id)
        presence = universe_by_identity[identity].source_presence
        if document is None:
            raise TmxScopeCoordinatorError("TMX.SCOPE.MISSING")
        previous = (
            session.segments[index - 1]
            if index > 0
            and session.segments[index - 1].identity.document.document_id
            == identity.document_id
            else None
        )
        following = (
            session.segments[index + 1]
            if index + 1 < len(session.segments)
            and session.segments[index + 1].identity.document.document_id
            == identity.document_id
            else None
        )
        rows.append(
            WorkspaceScopeSegment(
                identity=identity,
                document_order=document.order,
                document_local_index=segment.document_local_index,
                project_global_index=segment.project_global_index,
                source_raw=segment.source,
                target_raw=segment.target,
                speaker_raw=segment.raw_speaker,
                confirmed=segment.confirmed,
                file_source=document.source_ref,
                source_presence=presence,
                context_prev_raw=previous.source if previous is not None else None,
                context_next_raw=following.source if following is not None else None,
            )
        )

    binding = WorkspaceScopeBinding(
        project_id=owner_binding.project_id,
        workspace_session_id=owner_binding.workspace_session_id,
        generation=project.generation,
        workspace_revision=owner_binding.workspace_revision,
        workspace_composition_revision=(
            owner_binding.workspace_composition_revision
        ),
        workspace_digest=owner_binding.workspace_digest,
        segment_universe_digest=owner_binding.segment_universe_digest,
        document_count=len(session.documents),
        segment_count=len(rows),
        source_locale=session.source_locale,
        target_locale=session.target_locale,
    )
    binding_digest = _scope_digest(
        b"localcat.tmx.workspace-scope.v1\0",
        (
            binding.project_id,
            binding.workspace_session_id,
            binding.generation,
            binding.workspace_revision,
            binding.workspace_composition_revision,
            binding.workspace_digest,
            binding.segment_universe_digest,
            binding.document_count,
            binding.segment_count,
        ),
    )
    units = tuple(_workspace_tmx_unit(binding.project_id, row) for row in rows)
    tmx_binding = TmxScopeBinding(
        scope_kind=TmxScopeKind.ENTIRE_PROJECT,
        scope_id=binding.project_id,
        binding_digest=binding_digest,
        unit_count=len(units),
        project_id=binding.project_id,
        document_count=binding.document_count,
        attached_count=sum(unit.attached for unit in units),
    )
    return EntireProjectScopeMaterialization(
        binding=binding,
        tmx_binding=tmx_binding,
        units=units,
        segments=tuple(rows),
        owner_session=session,
        owner_universe=universe,
    )


def _materialize_chunk(
    project: EntireProjectScopeMaterialization,
    projection: ChunkScopeProjection,
) -> SelectedChunkScopeMaterialization:
    workspace = project.binding
    if projection.project_id != workspace.project_id:
        raise TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
    if projection.segment_universe_digest != workspace.segment_universe_digest:
        raise TmxScopeCoordinatorError("TMX.SCOPE.STALE")
    members: set[SegmentIdentity] = set()
    for member in projection.members:
        if member.project_id != workspace.project_id:
            raise TmxScopeCoordinatorError("TMX.SCOPE.FOREIGN")
        members.add(member.identity)
    if len(members) != len(projection.members):
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    available = {segment.identity for segment in project.segments}
    if not members.issubset(available):
        raise TmxScopeCoordinatorError("TMX.SCOPE.MISSING")
    selected = tuple(
        segment for segment in project.segments if segment.identity in members
    )
    if len(selected) != len(members):
        raise TmxScopeCoordinatorError("TMX.SCOPE.MISSING")
    binding = SelectedChunkScopeBinding(
        workspace=workspace,
        chunk_plan_id=projection.chunk_plan_id,
        plan_revision=projection.plan_revision,
        plan_digest=projection.plan_digest,
        chunk_id=projection.chunk_id,
        member_count=len(selected),
    )
    binding_digest = _scope_digest(
        b"localcat.tmx.selected-chunk-scope.v1\0",
        (
            project.tmx_binding.binding_digest,
            projection.chunk_plan_id,
            projection.plan_revision,
            projection.plan_digest,
            projection.segment_universe_digest,
            projection.chunk_id,
            len(selected),
        ),
    )
    units_by_identity = {
        segment.identity: unit
        for segment, unit in zip(project.segments, project.units, strict=True)
    }
    units = tuple(units_by_identity[segment.identity] for segment in selected)
    tmx_binding = TmxScopeBinding(
        scope_kind=TmxScopeKind.SELECTED_CHUNK,
        scope_id=projection.chunk_id,
        binding_digest=binding_digest,
        unit_count=len(units),
        project_id=workspace.project_id,
        chunk_plan_id=projection.chunk_plan_id,
        chunk_plan_revision=projection.plan_revision,
        chunk_id=projection.chunk_id,
        document_count=len({segment.identity.document_id for segment in selected}),
        attached_count=sum(unit.attached for unit in units),
    )
    return SelectedChunkScopeMaterialization(
        binding=binding,
        tmx_binding=tmx_binding,
        units=units,
        segments=selected,
        owner_project=project,
        owner_chunk=projection,
    )


__all__ = [
    "ChunkTmxScopeAdapter",
    "ChunkTmxScopeOwner",
    "ManagedTmScopeOwner",
    "TmxExportCoordinator",
    "WorkspaceTmxScopeAdapter",
    "WorkspaceTmxScopeOwner",
    "WorkspaceUniverseOwner",
]
