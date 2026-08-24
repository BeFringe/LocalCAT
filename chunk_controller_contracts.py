"""Versioned leaf contracts for the Chunk/Workspace application boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from collaborative_chunk_contracts import ChunkAccessDecision, SegmentIdentity
from editor_contracts import (
    SearchField,
    SegmentTranslationStatus,
    TextMatcherDisplayState,
    WorkspaceSearchHit,
)
from tm_contracts import SearchOptions


class CollaborativeSearchScopeV2(str, Enum):
    """Additive collaborative scope; the frozen Workspace v1 enum is unchanged."""

    CURRENT_DOCUMENT = "current_document"
    ENTIRE_PROJECT = "entire_project"
    CURRENT_CHUNK = "current_chunk"


@dataclass(frozen=True, slots=True)
class CollaborativeWorkspaceSearchRequestV2:
    query: str
    fields: tuple[SearchField, ...]
    options: SearchOptions
    status: SegmentTranslationStatus | None = None
    scope: CollaborativeSearchScopeV2 = CollaborativeSearchScopeV2.ENTIRE_PROJECT

    def __post_init__(self) -> None:
        from editor_contracts import ProjectSearchRequest

        ProjectSearchRequest(
            query=self.query,
            fields=self.fields,
            options=self.options,
            status=self.status,
        )
        if type(self.scope) is not CollaborativeSearchScopeV2:
            raise TypeError("collaborative search scope must be v2")


@dataclass(frozen=True, slots=True)
class CollaborativeWorkspaceSearchHitV2:
    """One normal Workspace hit plus the current permission projection."""

    workspace_hit: WorkspaceSearchHit
    access: ChunkAccessDecision

    def __post_init__(self) -> None:
        if type(self.workspace_hit) is not WorkspaceSearchHit:
            raise TypeError("collaborative hit requires WorkspaceSearchHit")
        if type(self.access) is not ChunkAccessDecision:
            raise TypeError("collaborative hit requires ChunkAccessDecision")
        self.workspace_hit.__post_init__()
        self.access.__post_init__()
        identity = self.access.segment.identity
        if (
            identity.document_id != self.workspace_hit.document_id
            or identity.local_segment_id != self.workspace_hit.local_segment_id
        ):
            raise ValueError("collaborative search access identity mismatch")


@dataclass(frozen=True, slots=True)
class CollaborativeWorkspaceSearchReportV2:
    hits: tuple[CollaborativeWorkspaceSearchHitV2, ...]
    capability: TextMatcherDisplayState

    def __post_init__(self) -> None:
        if type(self.hits) is not tuple or any(
            type(hit) is not CollaborativeWorkspaceSearchHitV2
            for hit in self.hits
        ):
            raise TypeError("collaborative search hits must be exact v2 hits")
        for hit in self.hits:
            hit.__post_init__()
        if type(self.capability) is not TextMatcherDisplayState:
            raise TypeError("collaborative search capability must be exact")
        self.capability.__post_init__()

    @property
    def total(self) -> int:
        return len(self.hits)


class ChunkApplicationMode(str, Enum):
    NO_PLAN = "no_plan"
    ACTIVE = "active"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ChunkApplicationAccessView:
    """Body-safe access projection for the current Workspace segment."""

    identity: SegmentIdentity
    access: str
    may_edit_target: bool
    may_change_confirmed: bool
    safe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            raise TypeError("chunk access requires exact SegmentIdentity")
        self.identity.__post_init__()
        if type(self.access) is not str or not self.access:
            raise TypeError("chunk access requires a safe access label")
        if type(self.may_edit_target) is not bool or type(
            self.may_change_confirmed
        ) is not bool:
            raise TypeError("chunk access flags must be exact bools")
        if type(self.safe_codes) is not tuple or any(
            type(code) is not str or not code for code in self.safe_codes
        ):
            raise TypeError("chunk access codes must be exact strings")


@dataclass(frozen=True, slots=True)
class ChunkApplicationProgressView:
    attached_total: int
    unfilled: int
    draft: int
    confirmed: int
    detached: int

    def __post_init__(self) -> None:
        values = (
            self.attached_total,
            self.unfilled,
            self.draft,
            self.confirmed,
            self.detached,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise TypeError("chunk progress values must be nonnegative ints")
        if self.attached_total != self.unfilled + self.draft + self.confirmed:
            raise ValueError("chunk progress total mismatch")


@dataclass(frozen=True, slots=True)
class ChunkApplicationChunkView:
    chunk_id: str
    name: str
    order: int
    assignee_label: str
    assigned_to_current_reference: bool
    member_count: int
    progress: ChunkApplicationProgressView
    is_current: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.chunk_id, self.name, self.assignee_label)
        ):
            raise TypeError("chunk projection labels must be nonempty strings")
        if type(self.order) is not int or self.order < 0:
            raise TypeError("chunk order must be a nonnegative int")
        if type(self.member_count) is not int or self.member_count < 1:
            raise TypeError("chunk member count must be positive")
        if type(self.assigned_to_current_reference) is not bool or type(
            self.is_current
        ) is not bool:
            raise TypeError("chunk projection flags must be exact bools")
        if type(self.progress) is not ChunkApplicationProgressView:
            raise TypeError("chunk projection requires exact progress")
        self.progress.__post_init__()


@dataclass(frozen=True, slots=True)
class ChunkApplicationSegmentChoice:
    """One body-free segment row in the application-issued project order."""

    identity: SegmentIdentity
    document_label: str
    segment_label: str
    chunk_id: str | None
    chunk_label: str | None
    attached: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            raise TypeError("segment choice requires exact identity")
        self.identity.__post_init__()
        if any(
            type(value) is not str or not value
            for value in (self.document_label, self.segment_label)
        ):
            raise TypeError("segment choice labels must be nonempty strings")
        if (self.chunk_id is None) != (self.chunk_label is None):
            raise ValueError("segment choice membership must be paired")
        if self.chunk_id is not None and (
            type(self.chunk_id) is not str
            or not self.chunk_id
            or type(self.chunk_label) is not str
            or not self.chunk_label
        ):
            raise TypeError("segment choice chunk labels must be strings")
        if type(self.attached) is not bool:
            raise TypeError("segment choice attached flag must be exact bool")


@dataclass(frozen=True, slots=True)
class ChunkApplicationSegmentSelectionRequest:
    """One-use, body-safe request for selecting exact members in Browse/Review."""

    action: str
    action_label: str
    allowed_identities: tuple[SegmentIdentity, ...]
    selected_identities: tuple[SegmentIdentity, ...]
    bulk_select_label: str | None
    bulk_select_identities: tuple[SegmentIdentity, ...]
    minimum_selection: int

    def __post_init__(self) -> None:
        if type(self.action) is not str or not self.action:
            raise TypeError("segment selection action must be nonempty")
        if type(self.action_label) is not str or not self.action_label:
            raise TypeError("segment selection label must be nonempty")
        for values, label in (
            (self.allowed_identities, "allowed"),
            (self.selected_identities, "selected"),
            (self.bulk_select_identities, "bulk"),
        ):
            if type(values) is not tuple or any(
                type(identity) is not SegmentIdentity for identity in values
            ):
                raise TypeError(
                    f"segment selection {label} identities must be exact"
                )
            for identity in values:
                identity.__post_init__()
            if len(values) != len(set(values)):
                raise ValueError(
                    f"segment selection {label} identities must be unique"
                )
        allowed = set(self.allowed_identities)
        if not self.allowed_identities:
            raise ValueError("segment selection must allow at least one identity")
        if any(
            identity not in allowed for identity in self.selected_identities
        ):
            raise ValueError("segment selection contains a disallowed selection")
        if any(
            identity not in allowed for identity in self.bulk_select_identities
        ):
            raise ValueError(
                "segment selection bulk scope exceeds allowed identities"
            )
        order = {
            identity: index
            for index, identity in enumerate(self.allowed_identities)
        }
        if (
            tuple(sorted(self.selected_identities, key=order.__getitem__))
            != self.selected_identities
        ):
            raise ValueError("segment selection is not in issued project order")
        if (
            tuple(sorted(self.bulk_select_identities, key=order.__getitem__))
            != self.bulk_select_identities
        ):
            raise ValueError(
                "segment selection bulk scope is not in issued project order"
            )
        if (self.bulk_select_label is None) != (not self.bulk_select_identities):
            raise ValueError("segment selection bulk label and scope must be paired")
        if self.bulk_select_label is not None and (
            type(self.bulk_select_label) is not str or not self.bulk_select_label
        ):
            raise TypeError("segment selection bulk label must be nonempty")
        if (
            type(self.minimum_selection) is not int
            or self.minimum_selection < 1
            or self.minimum_selection > len(self.allowed_identities)
        ):
            raise ValueError("segment selection minimum is outside its allowed scope")


@dataclass(frozen=True, slots=True)
class ChunkApplicationProjectView:
    mode: ChunkApplicationMode
    project_id: str
    chunk_plan_id: str | None
    plan_revision: int | None
    current_chunk_id: str | None
    reference_label: str
    chunks: tuple[ChunkApplicationChunkView, ...]
    unallocated_count: int
    current_segment_access: ChunkApplicationAccessView
    safe_code: str | None

    def __post_init__(self) -> None:
        if type(self.mode) is not ChunkApplicationMode:
            raise TypeError("chunk application mode must be exact")
        if type(self.project_id) is not str or not self.project_id:
            raise TypeError("chunk application project must be nonempty")
        if type(self.reference_label) is not str or not self.reference_label:
            raise TypeError("chunk reference label must be nonempty")
        if type(self.chunks) is not tuple or any(
            type(chunk) is not ChunkApplicationChunkView for chunk in self.chunks
        ):
            raise TypeError("chunk application chunks must be exact")
        if tuple(chunk.order for chunk in self.chunks) != tuple(
            range(len(self.chunks))
        ):
            raise ValueError("chunk application order mismatch")
        if type(self.unallocated_count) is not int or self.unallocated_count < 0:
            raise TypeError("unallocated count must be a nonnegative int")
        if type(self.current_segment_access) is not ChunkApplicationAccessView:
            raise TypeError("chunk application access must be exact")
        self.current_segment_access.__post_init__()


@dataclass(frozen=True, slots=True)
class ChunkApplicationSplitChild:
    name: str
    members: tuple[SegmentIdentity, ...]
    assign_to_current_reference: bool | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise TypeError("split child name must be nonempty")
        if type(self.members) is not tuple or not self.members or any(
            type(identity) is not SegmentIdentity for identity in self.members
        ):
            raise TypeError("split child members must be exact identities")
        for identity in self.members:
            identity.__post_init__()
        if self.assign_to_current_reference not in {None, True, False}:
            raise TypeError("split assignment decision must be bool or None")


@dataclass(frozen=True, slots=True)
class ChunkApplicationRebaseInspection:
    """Body-safe decisions required before a Workspace rebase preview."""

    missing_members: tuple[SegmentIdentity, ...]
    new_unallocated_count: int
    empty_chunk_ids: tuple[str, ...]
    all_chunks_empty: bool

    def __post_init__(self) -> None:
        if type(self.missing_members) is not tuple or any(
            type(identity) is not SegmentIdentity
            for identity in self.missing_members
        ):
            raise TypeError("rebase members must be exact identities")
        for identity in self.missing_members:
            identity.__post_init__()
        if (
            type(self.new_unallocated_count) is not int
            or self.new_unallocated_count < 0
        ):
            raise TypeError("rebase unallocated count must be nonnegative")
        if type(self.empty_chunk_ids) is not tuple or any(
            type(chunk_id) is not str or not chunk_id
            for chunk_id in self.empty_chunk_ids
        ):
            raise TypeError("rebase empty chunk ids must be exact strings")
        if type(self.all_chunks_empty) is not bool:
            raise TypeError("rebase all-empty flag must be exact bool")


@dataclass(frozen=True, slots=True)
class ChunkApplicationMutationPreview:
    """Issued, body-safe confirmation surface; authority stays in adapter."""

    operation_id: str
    action: str
    project_id: str
    chunk_plan_id: str | None
    base_revision: int | None
    published_revision: int | None
    affected_chunk_ids: tuple[str, ...]
    created_chunk_ids: tuple[str, ...]
    retired_chunk_ids: tuple[str, ...]
    affected_chunk_count: int
    created_chunk_count: int
    retired_chunk_count: int
    affected_member_count: int
    assignment_count: int
    missing_member_count: int
    new_unallocated_count: int
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    truncated: bool
    classification: str | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.operation_id, self.action, self.project_id)
        ):
            raise TypeError("chunk mutation identity must be nonempty")
        for value in (
            self.affected_chunk_count,
            self.created_chunk_count,
            self.retired_chunk_count,
            self.affected_member_count,
            self.assignment_count,
            self.missing_member_count,
            self.new_unallocated_count,
        ):
            if type(value) is not int or value < 0:
                raise TypeError("chunk mutation counts must be nonnegative")
        for values in (
            self.affected_chunk_ids,
            self.created_chunk_ids,
            self.retired_chunk_ids,
            self.warnings,
            self.blockers,
        ):
            if type(values) is not tuple or any(
                type(value) is not str or not value for value in values
            ):
                raise TypeError("chunk mutation tuples must contain strings")
        if type(self.truncated) is not bool:
            raise TypeError("chunk mutation truncation flag must be exact")


@dataclass(frozen=True, slots=True)
class ChunkApplicationMutationReceipt:
    operation_id: str
    action: str
    project_id: str
    chunk_plan_id: str | None
    published_revision: int | None
    affected_chunk_count: int
    affected_member_count: int
    assignment_count: int
    safe_issues: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.operation_id, self.action, self.project_id)
        ):
            raise TypeError("chunk receipt identity must be nonempty")
        if any(
            type(value) is not int or value < 0
            for value in (
                self.affected_chunk_count,
                self.affected_member_count,
                self.assignment_count,
            )
        ):
            raise TypeError("chunk receipt counts must be nonnegative")
        if type(self.safe_issues) is not tuple or any(
            type(value) is not str or not value for value in self.safe_issues
        ):
            raise TypeError("chunk receipt issues must be exact strings")


__all__ = [
    "ChunkApplicationAccessView",
    "ChunkApplicationChunkView",
    "ChunkApplicationMode",
    "ChunkApplicationMutationPreview",
    "ChunkApplicationMutationReceipt",
    "ChunkApplicationProgressView",
    "ChunkApplicationProjectView",
    "ChunkApplicationRebaseInspection",
    "ChunkApplicationSegmentChoice",
    "ChunkApplicationSplitChild",
    "CollaborativeSearchScopeV2",
    "CollaborativeWorkspaceSearchHitV2",
    "CollaborativeWorkspaceSearchReportV2",
    "CollaborativeWorkspaceSearchRequestV2",
]
