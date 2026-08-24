"""Qt-free project-field orchestration over the Core text matcher port."""

from __future__ import annotations

from typing import final

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ProjectSearchHit,
    ProjectSearchReport,
    ProjectSearchRequest,
    SearchField,
    SearchScope,
    SegmentTranslationStatus,
    TextMatcherDisplayState,
    WorkspaceSearchHit,
    WorkspaceSearchReport,
    WorkspaceSearchRequest,
)
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import ProjectSegment
from tm_contracts import (
    CapabilityGatedTextMatcher,
    SearchOptions,
    TextMatchProfile,
    TextMatchRejected,
    TextMatchRequest,
    TextMatcherCapability,
    TextMatchSuccess,
)


_BASIC_OPTIONS = SearchOptions(match_case=False, whole_word=False)
_FIELD_ORDER = (
    SearchField.SOURCE,
    SearchField.TARGET,
    SearchField.SPEAKER,
)


@final
class ProjectSearchError(RuntimeError):
    """Safe fail-closed error raised when Core cannot execute a search."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@final
class ProjectSearchService:
    """Traverse one immutable project while delegating all matching to Core."""

    _matcher: CapabilityGatedTextMatcher

    def __init__(self, matcher: CapabilityGatedTextMatcher | None) -> None:
        if matcher is None:
            raise ProjectSearchError("MATCHER.PORT_UNAVAILABLE")
        self._matcher = matcher

    def search(
        self,
        project: EditorProject,
        request: ProjectSearchRequest,
    ) -> ProjectSearchReport:
        """Return stable project hits without navigating or mutating the project."""

        if type(project) is not EditorProject:
            raise TypeError("project must be exact EditorProject")
        if type(request) is not ProjectSearchRequest:
            raise TypeError("request must be exact ProjectSearchRequest")
        project.__post_init__()
        request.__post_init__()

        entries = tuple(
            (None, segment.id, index, segment)
            for index, segment in enumerate(project.segments)
        )
        return self._search_entries(entries, request)

    def search_workspace(
        self,
        workspace_service: ProjectWorkspaceService,
        request: WorkspaceSearchRequest,
        *,
        current_document_id: str,
    ) -> WorkspaceSearchReport:
        """Search one exact workspace projection using the same matcher pipeline."""

        if type(workspace_service) is not ProjectWorkspaceService:
            raise TypeError("workspace_service must be exact ProjectWorkspaceService")
        if type(request) is not WorkspaceSearchRequest:
            raise TypeError("request must be exact WorkspaceSearchRequest")
        request.__post_init__()
        documents = workspace_service.workspace.documents
        if not any(item.document_id == current_document_id for item in documents):
            raise ProjectSearchError("PROJECT_SEARCH.CURRENT_DOCUMENT_STALE")
        entries = tuple(
            (
                flat.document_id,
                flat.identity.local_segment_id,
                flat.project_global_index,
                flat.segment,
            )
            for flat in workspace_service.flat_segments
            if request.scope is SearchScope.ENTIRE_PROJECT
            or flat.document_id == current_document_id
        )
        return self._search_entries(entries, request)

    def search_workspace_selection(
        self,
        workspace_service: ProjectWorkspaceService,
        request: WorkspaceSearchRequest,
        *,
        members: tuple[object, ...],
    ) -> WorkspaceSearchReport:
        """Search only an exact composite-identity selection in Workspace order.

        The selection is intentionally chunk-neutral.  Downstream reference
        domains own issuance; this service only verifies membership against the
        live Workspace graph before entering the existing matcher pipeline.
        """

        if type(workspace_service) is not ProjectWorkspaceService:
            raise TypeError("workspace_service must be exact ProjectWorkspaceService")
        if type(request) is not WorkspaceSearchRequest:
            raise TypeError("request must be exact WorkspaceSearchRequest")
        request.__post_init__()
        flat_segments = workspace_service.flat_segments
        if not flat_segments:
            raise ProjectSearchError("PROJECT_SEARCH.SELECTION_STALE")
        identity_type = type(flat_segments[0].identity)
        if type(members) is not tuple or any(
            type(member) is not identity_type for member in members
        ):
            raise TypeError("workspace selection members must be exact identities")
        for member in members:
            member.__post_init__()
        if len(members) != len(set(members)):
            raise ProjectSearchError("PROJECT_SEARCH.SELECTION_INVALID")
        selected = set(members)
        live = {
            flat.identity
            for flat in flat_segments
        }
        if not selected.issubset(live):
            raise ProjectSearchError("PROJECT_SEARCH.SELECTION_STALE")
        entries = tuple(
            (
                flat.document_id,
                flat.identity.local_segment_id,
                flat.project_global_index,
                flat.segment,
            )
            for flat in flat_segments
            if flat.identity in selected
        )
        report = self._search_entries(entries, request)
        if type(report) is not WorkspaceSearchReport:
            raise TypeError("workspace selection search returned wrong report")
        return report

    def _search_entries(
        self,
        entries: tuple[
            tuple[str | None, str, int, EditorSegment | ProjectSegment], ...
        ],
        request: ProjectSearchRequest | WorkspaceSearchRequest,
    ) -> ProjectSearchReport | WorkspaceSearchReport:
        profile = (
            TextMatchProfile.BASIC_CONTIGUOUS
            if request.options == _BASIC_OPTIONS
            else TextMatchProfile.CONFIGURABLE_TEXT_V1
        )
        selected_fields = request.fields
        project_hits: list[ProjectSearchHit] = []
        workspace_hits: list[WorkspaceSearchHit] = []
        used_capability: TextMatcherCapability | None = None

        for document_id, segment_id, segment_index, segment in entries:
            if (
                request.status is not None
                and segment_translation_status(segment) is not request.status
            ):
                continue
            for field in _FIELD_ORDER:
                if field not in selected_fields:
                    continue
                text = _field_text(segment, field)
                match_request = TextMatchRequest(
                    text=text,
                    query=request.query,
                    profile=profile,
                    options=request.options,
                )
                outcome = self._matcher.match(match_request)
                if type(outcome) is TextMatchRejected:
                    raise ProjectSearchError(outcome.safe_reason)
                if type(outcome) is not TextMatchSuccess:
                    raise TypeError("Core matcher returned an unsupported outcome")
                if (
                    outcome.request_digest != match_request.request_digest
                    or outcome.request_profile is not profile
                    or outcome.request_options != request.options
                ):
                    raise ProjectSearchError("MATCHER.OUTCOME_MISMATCH")
                if used_capability is None:
                    used_capability = outcome.capability
                elif outcome.capability != used_capability:
                    raise ProjectSearchError("MATCHER.CAPABILITY_CHANGED")

                for core_hit in outcome.hits:
                    if core_hit.end_index > len(text):
                        raise ProjectSearchError("MATCHER.OFFSET_OUT_OF_RANGE")
                    if document_id is None:
                        project_hits.append(
                            ProjectSearchHit(
                                segment_id=segment_id,
                                segment_index=segment_index,
                                field=field,
                                start_index=core_hit.start_index,
                                end_index=core_hit.end_index,
                                preview=text,
                            )
                        )
                    else:
                        workspace_hits.append(
                            WorkspaceSearchHit(
                                document_id=document_id,
                                local_segment_id=segment_id,
                                project_global_index=segment_index,
                                field=field,
                                start_index=core_hit.start_index,
                                end_index=core_hit.end_index,
                                preview=text,
                            )
                        )

        if used_capability is None:
            used_capability = self._matcher.capability()
        display = _display_from_core(used_capability)
        if type(request) is WorkspaceSearchRequest:
            return WorkspaceSearchReport(
                hits=tuple(workspace_hits),
                capability=display,
            )
        return ProjectSearchReport(hits=tuple(project_hits), capability=display)


def _field_text(
    segment: EditorSegment | ProjectSegment,
    field: SearchField,
) -> str:
    if field is SearchField.SOURCE:
        return segment.source
    if field is SearchField.TARGET:
        return segment.target
    return segment.speaker if type(segment) is EditorSegment else segment.raw_speaker


def segment_translation_status(
    segment: EditorSegment | ProjectSegment,
) -> SegmentTranslationStatus:
    """Derive the sole frozen translation state before matching fields."""

    if type(segment) not in (EditorSegment, ProjectSegment):
        raise TypeError("segment translation status requires a project segment")
    segment.__post_init__()
    if segment.confirmed:
        return SegmentTranslationStatus.TRANSLATED
    if segment.target.strip() == "":
        return SegmentTranslationStatus.UNFILLED
    return SegmentTranslationStatus.DRAFT


def _display_from_core(
    capability: TextMatcherCapability,
) -> TextMatcherDisplayState:
    return TextMatcherDisplayState(
        state=capability.state,
        supported_profiles=capability.supported_profiles,
        safe_reason=capability.unavailable_reason,
    )


__all__ = [
    "ProjectSearchError",
    "ProjectSearchService",
    "segment_translation_status",
]
