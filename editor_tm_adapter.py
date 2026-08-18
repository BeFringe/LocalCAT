"""Qt-free current-segment translation-memory adapter.

Tasks 4.1/4.2 own the canonical and legacy query lanes. Mixed aggregation,
safe UI projection, issued-suggestion membership, and append remain in later
tasks. The private batches below therefore never cross the Controller/Qt
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from capability_host import CapabilityHost, RetrievalHandoffSnapshot
from editor_contracts import EditorSegment, TMPreferences
from renpy_tm_compat import build_dialogue_alias, unwrap_dialogue_target
from tm_application_composition import (
    LegacyExactPort,
    TMRuntimeHost,
    TMRuntimeSnapshot,
)
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
from tm_engine import TMMatch


_LEGACY_QUERY_FAILED_CODE = "TM.LEGACY.QUERY_FAILED"
_LEGACY_FAILURE_STAGES = frozenset(("DIRECT", "ALIAS"))
_LEGACY_OPERATION_ERRORS = (OSError, UnicodeError, json.JSONDecodeError)


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


@dataclass(frozen=True, slots=True)
class _LegacyExactResult:
    """Application-private exact result retaining its legacy record body."""

    resource_id: str
    resource_name: str
    global_order: int
    query_source: str
    matched_source: str
    target: str
    record_source: str
    record_target: str
    match_type: TMMatchType
    similarity: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("resource id", self.resource_id),
            ("resource name", self.resource_name),
            ("query source", self.query_source),
            ("matched source", self.matched_source),
            ("target", self.target),
            ("record source", self.record_source),
            ("record target", self.record_target),
        ):
            if type(value) is not str:
                raise TypeError(f"legacy exact {field_name} must be exact str")
            if not value:
                raise ValueError(f"legacy exact {field_name} must not be empty")
        if type(self.global_order) is not int or self.global_order < 0:
            raise TypeError("legacy exact global order must be non-negative int")
        if self.query_source != self.matched_source:
            raise ValueError("legacy exact query and matched source must be equal")
        if self.match_type is not TMMatchType.EXACT:
            raise ValueError("legacy exact result must remain EXACT")
        if type(self.similarity) is not float or self.similarity != 1.0:
            raise ValueError("legacy exact similarity must remain 1.0")


@dataclass(frozen=True, slots=True)
class _LegacyQueryFailure:
    """Body-free resource-local failure for later safe status projection."""

    resource_id: str
    global_order: int
    stage: str
    error_code: str
    retryable: bool

    def __post_init__(self) -> None:
        if type(self.resource_id) is not str or not self.resource_id.strip():
            raise TypeError("legacy failure resource id must be non-empty str")
        if type(self.global_order) is not int or self.global_order < 0:
            raise TypeError("legacy failure global order must be non-negative int")
        if type(self.stage) is not str or self.stage not in _LEGACY_FAILURE_STAGES:
            raise ValueError("legacy failure stage must be DIRECT or ALIAS")
        if (
            type(self.error_code) is not str
            or self.error_code != _LEGACY_QUERY_FAILED_CODE
        ):
            raise ValueError("legacy failure code must be the closed safe code")
        if type(self.retryable) is not bool:
            raise TypeError("legacy failure retryable must be exact bool")


@dataclass(frozen=True, slots=True)
class _LegacyQueryBatch:
    """Legacy outcomes bound to one already-captured canonical operation."""

    canonical_batch: _CanonicalQueryBatch
    results: tuple[_LegacyExactResult, ...]
    failures: tuple[_LegacyQueryFailure, ...]

    def __post_init__(self) -> None:
        if type(self.canonical_batch) is not _CanonicalQueryBatch:
            raise TypeError("legacy batch must retain a canonical query batch")
        if type(self.results) is not tuple or any(
            type(result) is not _LegacyExactResult for result in self.results
        ):
            raise TypeError("legacy batch results must be exact private contracts")
        if type(self.failures) is not tuple or any(
            type(failure) is not _LegacyQueryFailure
            for failure in self.failures
        ):
            raise TypeError("legacy batch failures must be exact private contracts")
        for result in self.results:
            result.__post_init__()
        for failure in self.failures:
            failure.__post_init__()
        _validate_legacy_batch(self)


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

    def _query_legacy_exact(
        self,
        *,
        canonical_batch: _CanonicalQueryBatch,
    ) -> _LegacyQueryBatch:
        """Query legacy exact ports within one captured 4.1 operation.

        The method accepts the already-issued canonical batch so it cannot
        recapture a newer runtime or capability generation. It produces only
        application-private exact results and body-free local failures; mixed
        ordering, identity projection, and the global limit remain Task 4.3.
        """

        _validate_canonical_batch_for_legacy(canonical_batch)
        query_source = canonical_batch.query.query_source
        speaker_raw = canonical_batch.query.speaker_raw
        results: list[_LegacyExactResult] = []
        failures: list[_LegacyQueryFailure] = []
        for port in canonical_batch.runtime.legacy_ports:
            if not (port.active and port.lookup):
                continue
            try:
                direct = port.query_exact(query_source, speaker_raw)
            except _LEGACY_OPERATION_ERRORS as error:
                failures.append(
                    _legacy_query_failure(
                        port=port,
                        stage="DIRECT",
                        error=error,
                    )
                )
                continue
            if direct is not None:
                record_source, record_target = _snapshot_legacy_exact_match(
                    direct,
                    expected_source=query_source,
                )
                results.append(
                    _legacy_exact_result(
                        port=port,
                        query_source=query_source,
                        target=record_target,
                        record_source=record_source,
                        record_target=record_target,
                    )
                )
                continue

            if speaker_raw is None:
                continue
            alias = build_dialogue_alias(speaker_raw, query_source)
            if alias is None:
                continue
            try:
                wrapped = port.query_exact(alias, speaker_raw)
            except _LEGACY_OPERATION_ERRORS as error:
                failures.append(
                    _legacy_query_failure(
                        port=port,
                        stage="ALIAS",
                        error=error,
                    )
                )
                continue
            if wrapped is None:
                continue
            record_source, record_target = _snapshot_legacy_exact_match(
                wrapped,
                expected_source=alias,
            )
            target = unwrap_dialogue_target(record_target, speaker_raw)
            if target is None:
                continue
            results.append(
                _legacy_exact_result(
                    port=port,
                    query_source=query_source,
                    target=target,
                    record_source=record_source,
                    record_target=record_target,
                )
            )

        return _LegacyQueryBatch(
            canonical_batch=canonical_batch,
            results=tuple(results),
            failures=tuple(failures),
        )


def _validate_canonical_batch_for_legacy(
    canonical_batch: _CanonicalQueryBatch,
) -> None:
    if type(canonical_batch) is not _CanonicalQueryBatch:
        raise TypeError("legacy query requires an exact canonical query batch")
    canonical_batch.__post_init__()
    canonical_batch.runtime.__post_init__()
    _validate_captured_snapshots(
        runtime=canonical_batch.runtime,
        retrieval=canonical_batch.retrieval,
    )
    _validate_canonical_report(
        query=canonical_batch.query,
        report=canonical_batch.report,
    )


def _snapshot_legacy_exact_match(
    match: TMMatch,
    *,
    expected_source: str,
) -> tuple[str, str]:
    """Copy only the legacy record body after exact-contract validation."""

    if type(match) is not TMMatch:
        raise TypeError("legacy exact port must return TMMatch or None")
    if type(match.source) is not str or match.source != expected_source:
        raise ValueError("legacy exact source must equal the requested source")
    if type(match.target) is not str or not match.target:
        raise ValueError("legacy exact target must be a non-empty exact string")
    if type(match.match_type) is not str or match.match_type != "EXACT":
        raise ValueError("legacy exact port must not return another match type")
    if type(match.similarity) is not float or match.similarity != 1.0:
        raise ValueError("legacy exact port must return similarity 1.0")
    return str(match.source), str(match.target)


def _legacy_exact_result(
    *,
    port: LegacyExactPort,
    query_source: str,
    target: str,
    record_source: str,
    record_target: str,
) -> _LegacyExactResult:
    return _LegacyExactResult(
        resource_id=port.resource_id,
        resource_name=port.resource_name,
        global_order=port.global_order,
        query_source=query_source,
        matched_source=query_source,
        target=target,
        record_source=record_source,
        record_target=record_target,
        match_type=TMMatchType.EXACT,
        similarity=1.0,
    )


def _legacy_query_failure(
    *,
    port: LegacyExactPort,
    stage: str,
    error: Exception,
) -> _LegacyQueryFailure:
    """Discard exception text/path and retain only closed local facts."""

    if not isinstance(error, _LEGACY_OPERATION_ERRORS):
        raise TypeError("legacy local failure requires a read operation error")
    return _LegacyQueryFailure(
        resource_id=port.resource_id,
        global_order=port.global_order,
        stage=stage,
        error_code=_LEGACY_QUERY_FAILED_CODE,
        retryable=isinstance(error, OSError),
    )


def _validate_legacy_batch(batch: _LegacyQueryBatch) -> None:
    runtime = batch.canonical_batch.runtime
    query_source = batch.canonical_batch.query.query_source
    selected = {
        port.resource_id: port
        for port in runtime.legacy_ports
        if port.active and port.lookup
    }
    result_ids = tuple(result.resource_id for result in batch.results)
    failure_ids = tuple(failure.resource_id for failure in batch.failures)
    observed_ids = (*result_ids, *failure_ids)
    if len(observed_ids) != len(set(observed_ids)):
        raise ValueError("legacy resources may produce at most one outcome")
    if any(resource_id not in selected for resource_id in observed_ids):
        raise ValueError("legacy outcome is outside the Active+Lookup cohort")
    result_orders: list[int] = []
    for result in batch.results:
        port = selected[result.resource_id]
        if (
            result.resource_name != port.resource_name
            or result.global_order != port.global_order
            or result.query_source != query_source
        ):
            raise ValueError("legacy exact result drifted from runtime snapshot")
        result_orders.append(result.global_order)
    failure_orders: list[int] = []
    for failure in batch.failures:
        port = selected[failure.resource_id]
        if failure.global_order != port.global_order:
            raise ValueError("legacy failure drifted from runtime snapshot")
        failure_orders.append(failure.global_order)
    for orders in (result_orders, failure_orders):
        if any(left >= right for left, right in zip(orders, orders[1:])):
            raise ValueError("legacy outcomes must preserve global resource order")
    expected_result_order = tuple(
        port.resource_id
        for port in runtime.legacy_ports
        if port.active and port.lookup and port.resource_id in result_ids
    )
    expected_failure_order = tuple(
        port.resource_id
        for port in runtime.legacy_ports
        if port.active and port.lookup and port.resource_id in failure_ids
    )
    if result_ids != expected_result_order or failure_ids != expected_failure_order:
        raise ValueError("legacy outcome lanes must preserve global resource order")


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
