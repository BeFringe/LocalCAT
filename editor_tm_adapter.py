"""Qt-free current-segment translation-memory adapter.

Task 4.1 owns only the canonical query lane.  Legacy compatibility, mixed
aggregation, safe UI projection, issued-suggestion membership, and append
remain in later tasks.  The private batch below therefore never crosses the
Controller/Qt boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from capability_host import CapabilityHost, RetrievalHandoffSnapshot
from editor_contracts import EditorSegment, TMPreferences
from tm_application_composition import TMRuntimeHost, TMRuntimeSnapshot
from tm_contracts import (
    CandidateRecallMetadata,
    QueryReport,
    ResourceQueryFailure,
    ResourceQueryMetadata,
    TMMatchType,
    TMQuery,
    TMResourceHandle,
    TMResult,
)


@dataclass(frozen=True, slots=True)
class _CanonicalQueryBatch:
    """Application-private Core query/result pair for later mixed assembly."""

    query: TMQuery
    report: QueryReport
    runtime: TMRuntimeSnapshot
    retrieval: RetrievalHandoffSnapshot

    def __post_init__(self) -> None:
        if type(self.query) is not TMQuery:
            raise TypeError("canonical batch query must be TMQuery")
        if type(self.report) is not QueryReport:
            raise TypeError("canonical batch report must be QueryReport")
        if type(self.runtime) is not TMRuntimeSnapshot:
            raise TypeError("canonical batch runtime must be TMRuntimeSnapshot")
        if type(self.retrieval) is not RetrievalHandoffSnapshot:
            raise TypeError(
                "canonical batch retrieval must be RetrievalHandoffSnapshot"
            )


class EditorTMAdapter:
    """Consume current runtime/capability snapshots without owning authority."""

    __slots__ = ("_capability_host", "_runtime_host")

    def __init__(
        self,
        *,
        runtime_host: TMRuntimeHost,
        capability_host: CapabilityHost,
    ) -> None:
        if type(runtime_host) is not TMRuntimeHost:
            raise TypeError("editor TM adapter runtime host must be TMRuntimeHost")
        if type(capability_host) is not CapabilityHost:
            raise TypeError(
                "editor TM adapter capability host must be CapabilityHost"
            )
        self._runtime_host = runtime_host
        self._capability_host = capability_host

    def _query_canonical(
        self,
        *,
        segment: EditorSegment,
        project_session_id: str,
        query_epoch: int,
        preferences: TMPreferences,
    ) -> _CanonicalQueryBatch:
        """Run one current-segment query over the canonical lookup cohort.

        This is deliberately private until Tasks 4.2/4.3 close the frozen
        public ``query_current() -> TMSuggestionReport`` contract.  It returns
        Core values unchanged and never inspects scorer/proof evidence.
        """

        _validate_query_inputs(
            segment=segment,
            project_session_id=project_session_id,
            query_epoch=query_epoch,
            preferences=preferences,
        )
        runtime = self._runtime_host.capture_operation_snapshot()
        canonical_handles = _canonical_lookup_handles(runtime)
        query = TMQuery(
            query_source=segment.source,
            speaker_raw=segment.speaker or None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=preferences.minimum_similarity,
            limit=10,
            resource_order=tuple(
                handle.resource_id for handle in canonical_handles
            ),
        )
        operation = self._capability_host.query_retrieval_operation(
            canonical_handles,
            query,
        )
        retrieval = operation.handoff
        report = operation.report
        _validate_captured_snapshots(runtime=runtime, retrieval=retrieval)
        _validate_canonical_report(query=query, report=report)
        return _CanonicalQueryBatch(
            query=query,
            report=report,
            runtime=runtime,
            retrieval=retrieval,
        )


def _canonical_lookup_handles(
    runtime: TMRuntimeSnapshot,
) -> tuple[TMResourceHandle, ...]:
    selected = tuple(
        handle
        for handle in runtime.canonical_handles
        if handle.active and handle.lookup
    )
    remapped = tuple(
        TMResourceHandle(
            resource_id=handle.resource_id,
            store=handle.store,
            active=True,
            lookup=True,
            update=handle.update,
            order=order,
        )
        for order, handle in enumerate(selected)
    )
    for order, (original, current) in enumerate(
        zip(selected, remapped, strict=True)
    ):
        if (
            current.resource_id != original.resource_id
            or current.store is not original.store
            or current.active is not True
            or current.lookup is not True
            or current.update is not original.update
            or current.order != order
        ):
            raise ValueError("canonical cohort remap drift")
    return remapped


def _validate_canonical_report(
    *,
    query: TMQuery,
    report: QueryReport,
) -> None:
    """Validate frozen Core contracts and mapping closure without reordering."""

    query.__post_init__()
    if type(report) is not QueryReport:
        raise TypeError("canonical query port must return QueryReport")
    if (
        type(report.results) is not tuple
        or any(type(result) is not TMResult for result in report.results)
        or type(report.resource_failures) is not tuple
        or any(
            type(failure) is not ResourceQueryFailure
            for failure in report.resource_failures
        )
        or type(report.resource_metadata) is not tuple
        or any(
            type(metadata) is not ResourceQueryMetadata
            for metadata in report.resource_metadata
        )
    ):
        raise TypeError("canonical report nested contracts must be exact")
    for result in report.results:
        result.__post_init__()
    for failure in report.resource_failures:
        failure.__post_init__()
    for metadata in report.resource_metadata:
        if type(metadata.recall) is not CandidateRecallMetadata:
            raise TypeError("canonical report recall contract must be exact")
        metadata.recall.__post_init__()
        metadata.__post_init__()
    report.__post_init__()
    if len(report.results) > query.limit:
        raise ValueError("canonical report exceeds query limit")
    position_by_resource_id = {
        resource_id: position
        for position, resource_id in enumerate(query.resource_order)
    }
    cohort = set(query.resource_order)
    for result in report.results:
        if result.resource_id not in cohort:
            raise ValueError("canonical report result is outside canonical cohort")
        if result.query_source != query.query_source:
            raise ValueError("canonical report query source drift")
        if (
            type(result.match_type) is not TMMatchType
            or type(result.stable_tie_key) is not tuple
            or len(result.stable_tie_key) != 2
            or any(type(item) is not int for item in result.stable_tie_key)
        ):
            raise TypeError("canonical report result contract drift")
        if result.stable_tie_key[0] != position_by_resource_id[result.resource_id]:
            raise ValueError("canonical report stable tie resource order drift")
        if (
            result.match_type is TMMatchType.FUZZY
            and not result.similarity >= query.minimum_similarity
        ):
            raise ValueError("canonical report fuzzy result is below threshold")
    validation_keys = tuple(
        _core_order_validation_key(result)
        for result in report.results
    )
    if validation_keys != tuple(sorted(validation_keys)):
        raise ValueError("canonical report violates Core stable order")
    for failure in report.resource_failures:
        if failure.resource_id not in cohort:
            raise ValueError("canonical report failure is outside canonical cohort")
    for metadata in report.resource_metadata:
        if metadata.resource_id not in cohort:
            raise ValueError("canonical report metadata is outside canonical cohort")
        if (
            type(metadata.recall) is not CandidateRecallMetadata
            or type(metadata.recall.result_limit) is not int
            or metadata.recall.result_limit != 10
        ):
            raise ValueError("canonical report metadata limit drift")
    accounted_resources = {
        metadata.resource_id for metadata in report.resource_metadata
    }.union(
        failure.resource_id for failure in report.resource_failures
    )
    if accounted_resources != cohort:
        raise ValueError(
            "canonical metadata and failures must cover canonical cohort"
        )
    if not cohort and (
        report.results
        or report.resource_failures
        or report.resource_metadata
    ):
        raise ValueError("empty canonical cohort requires an empty report")


_MATCH_LANE = {
    TMMatchType.EXACT: 0,
    TMMatchType.CONTEXT: 1,
    TMMatchType.FUZZY: 2,
}


def _core_order_validation_key(
    result: TMResult,
) -> tuple[int, float, tuple[int, ...], int, int]:
    """Project the frozen Feature 5 order contract for validation only."""

    return (
        _MATCH_LANE[result.match_type],
        -result.similarity,
        tuple(-value for value in result.context_evidence.strength_v1),
        result.stable_tie_key[0],
        -result.record_id,
    )


def _validate_query_inputs(
    *,
    segment: EditorSegment,
    project_session_id: str,
    query_epoch: int,
    preferences: TMPreferences,
) -> None:
    if type(segment) is not EditorSegment:
        raise TypeError("canonical query segment must be EditorSegment")
    segment.__post_init__()
    if type(project_session_id) is not str or not project_session_id.strip():
        raise TypeError("canonical query project session id must be non-empty str")
    if type(query_epoch) is not int or query_epoch < 0:
        raise TypeError("canonical query epoch must be non-negative int")
    if type(preferences) is not TMPreferences:
        raise TypeError("canonical query preferences must be TMPreferences")
    preferences.__post_init__()


def _validate_captured_snapshots(
    *,
    runtime: TMRuntimeSnapshot,
    retrieval: RetrievalHandoffSnapshot,
) -> None:
    if type(runtime) is not TMRuntimeSnapshot:
        raise TypeError("runtime host must return TMRuntimeSnapshot")
    if type(retrieval) is not RetrievalHandoffSnapshot:
        raise TypeError("capability host must return RetrievalHandoffSnapshot")


__all__ = ["EditorTMAdapter"]
