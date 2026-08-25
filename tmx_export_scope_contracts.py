"""Internal contracts for TMX export source-scope materialization.

The contracts in this module deliberately contain owner-issued snapshots and
body-bearing internal rows.  They are coordinator-to-profile handoff values,
not public previews or receipts.  This adapter layer may name exact upstream
owner DTOs, but it does not define TMX grammar, carrier semantics, or
inclusion/loss policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from collaborative_chunk_contracts import ChunkScopeProjection
from project_workspace import (
    WorkspaceSessionView,
    WorkspaceUniverseProjection,
)
from project_workspace_contracts import SegmentIdentity, SourcePresence
from tmx_context_contracts import TmxExportUnit, TmxScopeBinding, TmxScopeKind
from tm_sqlite_store import CanonicalExportRecord, CanonicalExportSnapshot


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"TMX\.SCOPE\.(?:CONTRACT_INVALID|UNAVAILABLE|STALE|FOREIGN|MISSING)\Z")


class TmxScopeCoordinatorError(RuntimeError):
    """Body-safe source-scope failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid TMX scope error code")
        self.code = code
        super().__init__(code)


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    return value


def _nonnegative(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class ManagedResourceScopeBinding:
    resource_id: str
    canonical_store_id: str
    generation: int
    head_revision: int
    record_count: int
    snapshot_digest: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.resource_id, self.canonical_store_id)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        for value in (self.generation, self.head_revision, self.record_count):
            _nonnegative(value)
        _digest(self.snapshot_digest, "snapshot_digest")


@dataclass(frozen=True, slots=True)
class ManagedResourceScopeMaterialization:
    binding: ManagedResourceScopeBinding
    tmx_binding: TmxScopeBinding
    units: tuple[TmxExportUnit, ...]
    records: tuple[CanonicalExportRecord, ...]
    owner_snapshot: CanonicalExportSnapshot

    def __post_init__(self) -> None:
        if type(self.binding) is not ManagedResourceScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self.binding.__post_init__()
        if type(self.records) is not tuple or any(
            type(item) is not CanonicalExportRecord for item in self.records
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.tmx_binding) is not TmxScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.units) is not tuple or any(
            type(item) is not TmxExportUnit for item in self.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.owner_snapshot) is not CanonicalExportSnapshot:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if self.records != self.owner_snapshot.records:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if self.binding.record_count != len(self.records):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if (
            self.tmx_binding.scope_kind is not TmxScopeKind.MANAGED_RESOURCE
            or self.tmx_binding.scope_id != self.binding.resource_id
            or self.tmx_binding.binding_digest != self.binding.snapshot_digest
            or self.tmx_binding.unit_count != len(self.units)
            or len(self.units) != len(self.records)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceScopeBinding:
    project_id: str
    workspace_session_id: str
    generation: int
    workspace_revision: int
    workspace_composition_revision: int
    workspace_digest: str
    segment_universe_digest: str
    document_count: int
    segment_count: int
    source_locale: str
    target_locale: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (
                self.project_id,
                self.workspace_session_id,
                self.source_locale,
                self.target_locale,
            )
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        for value in (
            self.generation,
            self.workspace_revision,
            self.workspace_composition_revision,
            self.document_count,
            self.segment_count,
        ):
            _nonnegative(value)
        _digest(self.workspace_digest, "workspace_digest")
        _digest(self.segment_universe_digest, "segment_universe_digest")


@dataclass(frozen=True, slots=True)
class WorkspaceScopeSegment:
    identity: SegmentIdentity
    document_order: int
    document_local_index: int
    project_global_index: int
    source_raw: str
    target_raw: str
    speaker_raw: str
    confirmed: bool
    file_source: str
    source_presence: SourcePresence
    context_prev_raw: str | None
    context_next_raw: str | None

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self.identity.__post_init__()
        for value in (
            self.document_order,
            self.document_local_index,
            self.project_global_index,
        ):
            _nonnegative(value)
        if any(
            type(value) is not str
            for value in (
                self.source_raw,
                self.target_raw,
                self.speaker_raw,
                self.file_source,
            )
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.confirmed) is not bool:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.source_presence) is not SourcePresence:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if any(
            value is not None and type(value) is not str
            for value in (self.context_prev_raw, self.context_next_raw)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class EntireProjectScopeMaterialization:
    binding: WorkspaceScopeBinding
    tmx_binding: TmxScopeBinding
    units: tuple[TmxExportUnit, ...]
    segments: tuple[WorkspaceScopeSegment, ...]
    owner_session: WorkspaceSessionView
    owner_universe: WorkspaceUniverseProjection

    def __post_init__(self) -> None:
        if type(self.binding) is not WorkspaceScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self.binding.__post_init__()
        if type(self.segments) is not tuple or any(
            type(item) is not WorkspaceScopeSegment for item in self.segments
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.tmx_binding) is not TmxScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.units) is not tuple or any(
            type(item) is not TmxExportUnit for item in self.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.owner_session) is not WorkspaceSessionView or type(
            self.owner_universe
        ) is not WorkspaceUniverseProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if self.binding.segment_count != len(self.segments):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if (
            self.tmx_binding.scope_kind is not TmxScopeKind.ENTIRE_PROJECT
            or self.tmx_binding.scope_id != self.binding.project_id
            or self.tmx_binding.project_id != self.binding.project_id
            or self.tmx_binding.document_count != self.binding.document_count
            or self.tmx_binding.unit_count != len(self.units)
            or len(self.units) != len(self.segments)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class SelectedChunkScopeBinding:
    workspace: WorkspaceScopeBinding
    chunk_plan_id: str
    plan_revision: int
    plan_digest: str
    chunk_id: str
    member_count: int

    def __post_init__(self) -> None:
        if type(self.workspace) is not WorkspaceScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self.workspace.__post_init__()
        if any(
            type(value) is not str or not value
            for value in (self.chunk_plan_id, self.chunk_id)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        _nonnegative(self.plan_revision)
        _nonnegative(self.member_count)
        _digest(self.plan_digest, "plan_digest")


@dataclass(frozen=True, slots=True)
class SelectedChunkScopeMaterialization:
    binding: SelectedChunkScopeBinding
    tmx_binding: TmxScopeBinding
    units: tuple[TmxExportUnit, ...]
    segments: tuple[WorkspaceScopeSegment, ...]
    owner_project: EntireProjectScopeMaterialization
    owner_chunk: ChunkScopeProjection

    def __post_init__(self) -> None:
        if type(self.binding) is not SelectedChunkScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        self.binding.__post_init__()
        if type(self.segments) is not tuple or any(
            type(item) is not WorkspaceScopeSegment for item in self.segments
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.tmx_binding) is not TmxScopeBinding:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.units) is not tuple or any(
            type(item) is not TmxExportUnit for item in self.units
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.owner_project) is not EntireProjectScopeMaterialization:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if type(self.owner_chunk) is not ChunkScopeProjection:
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if self.binding.member_count != len(self.segments):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")
        if (
            self.tmx_binding.scope_kind is not TmxScopeKind.SELECTED_CHUNK
            or self.tmx_binding.scope_id != self.binding.chunk_id
            or self.tmx_binding.project_id != self.binding.workspace.project_id
            or self.tmx_binding.chunk_plan_id != self.binding.chunk_plan_id
            or self.tmx_binding.chunk_plan_revision != self.binding.plan_revision
            or self.tmx_binding.chunk_id != self.binding.chunk_id
            or self.tmx_binding.unit_count != len(self.units)
            or len(self.units) != len(self.segments)
        ):
            raise TmxScopeCoordinatorError("TMX.SCOPE.CONTRACT_INVALID")


__all__ = [
    "EntireProjectScopeMaterialization",
    "ManagedResourceScopeBinding",
    "ManagedResourceScopeMaterialization",
    "SelectedChunkScopeBinding",
    "SelectedChunkScopeMaterialization",
    "TmxScopeCoordinatorError",
    "WorkspaceScopeBinding",
    "WorkspaceScopeSegment",
]
