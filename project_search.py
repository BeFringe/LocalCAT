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
    SegmentTranslationStatus,
    TextMatcherDisplayState,
)
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

        profile = (
            TextMatchProfile.BASIC_CONTIGUOUS
            if request.options == _BASIC_OPTIONS
            else TextMatchProfile.CONFIGURABLE_TEXT_V1
        )
        selected_fields = request.fields
        hits: list[ProjectSearchHit] = []
        used_capability: TextMatcherCapability | None = None

        for segment_index, segment in enumerate(project.segments):
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
                    hits.append(
                        ProjectSearchHit(
                            segment_id=segment.id,
                            segment_index=segment_index,
                            field=field,
                            start_index=core_hit.start_index,
                            end_index=core_hit.end_index,
                            preview=text,
                        )
                    )

        if used_capability is None:
            used_capability = self._matcher.capability()
        return ProjectSearchReport(
            hits=tuple(hits),
            capability=_display_from_core(used_capability),
        )


def _field_text(segment: EditorSegment, field: SearchField) -> str:
    if field is SearchField.SOURCE:
        return segment.source
    if field is SearchField.TARGET:
        return segment.target
    return segment.speaker


def segment_translation_status(
    segment: EditorSegment,
) -> SegmentTranslationStatus:
    """Derive the sole frozen translation state before matching fields."""

    if type(segment) is not EditorSegment:
        raise TypeError("segment translation status requires EditorSegment")
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
