"""Qt-free current-segment translation-memory adapter.

Tasks 4.1/4.2 own the canonical and legacy query lanes. Task 4.3 composes
those lanes into the frozen, body-safe public report. Task 4.4 dispatches
confirmed writes through one captured runtime cohort. EditorController Task
5.1 owns the aggregate epoch and issued-suggestion membership.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Callable, TypeVar

from capability_host import (
    CapabilityHost,
    MatcherHandoffSnapshot,
    RetrievalHandoffSnapshot,
    _MatcherGenerationChanged,
    _RetrievalGenerationChanged,
)
from editor_contracts import (
    EditorSegment,
    FuzzyValidationDisplay,
    FuzzyValidationState,
    ResourceConfig,
    RetrievalDisplayState,
    SuggestionQueryIdentity,
    TMPreferences,
    TMResourceWriteOutcome,
    TMResourceDisplayMode,
    TMResourceStatus,
    TMSuggestion,
    TMSuggestionProvenance,
    TMSuggestionReport,
    WriteReport,
)
from renpy_tm_compat import build_dialogue_alias, unwrap_dialogue_target
from tm_application_composition import (
    CanonicalResourcePort,
    LegacyAppendOperationError,
    LegacyExactPort,
    TMRuntimeHost,
    TMRuntimeSnapshot,
    _RuntimeGenerationChanged,
)
from tm_contracts import (
    CandidateRecallMetadata,
    QueryReport,
    ResourceQueryFailure,
    ResourceQueryMetadata,
    TMMatchType,
    TMQuery,
    TMRecordDraft,
    TMResourceHandle,
    TMResult,
)
from tm_engine import TMMatch
from tm_sqlite_store import SQLiteStoreSchemaError


_LEGACY_QUERY_FAILED_CODE = "TM.LEGACY.QUERY_FAILED"
_LEGACY_FAILURE_STAGES = frozenset(("DIRECT", "ALIAS"))
_LEGACY_OPERATION_ERRORS = (OSError, UnicodeError, json.JSONDecodeError)
_WRITE_OPERATION_ERRORS = (
    SQLiteStoreSchemaError,
    OSError,
    UnicodeError,
)
_LEGACY_APPEND_FAILED_CODE = "TM.WRITE.LEGACY_APPEND_FAILED"
_CANONICAL_APPEND_FAILED_CODE = "TM.WRITE.CANONICAL_APPEND_FAILED"


_OperationResultT = TypeVar("_OperationResultT")


class _TMQueryGenerationChanged(RuntimeError):
    """Controller-facing private signal for a stale TM query operation."""


class _TMMatcherGenerationChanged(RuntimeError):
    """Controller-facing signal for a stale matcher cohort operation."""


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


@dataclass(frozen=True, slots=True)
class _TMQueryOperation:
    """Controller-private report plus its exact application generations."""

    report: TMSuggestionReport
    runtime_generation: int
    retrieval_generation: int

    def __post_init__(self) -> None:
        if type(self.report) is not TMSuggestionReport:
            raise TypeError("TM query operation report must be TMSuggestionReport")
        self.report.__post_init__()
        for field_name, value in (
            ("runtime", self.runtime_generation),
            ("retrieval", self.retrieval_generation),
        ):
            if type(value) is not int or value < 0:
                raise TypeError(
                    f"TM query operation {field_name} generation must be "
                    "a non-negative exact int"
                )


class EditorTMAdapter:
    """Consume current runtime/capability snapshots without owning authority."""

    __slots__ = (
        "_capability_host",
        "_fuzzy_validation_start",
        "_fuzzy_validation_status",
        "_runtime_host",
    )

    def __init__(
        self,
        *,
        runtime_host: TMRuntimeHost,
        capability_host: CapabilityHost,
        fuzzy_validation_status: Callable[[], FuzzyValidationDisplay]
        | None = None,
        fuzzy_validation_start: Callable[[], FuzzyValidationDisplay]
        | None = None,
    ) -> None:
        if type(runtime_host) is not TMRuntimeHost:
            raise TypeError("editor TM adapter runtime host must be TMRuntimeHost")
        if type(capability_host) is not CapabilityHost:
            raise TypeError(
                "editor TM adapter capability host must be CapabilityHost"
            )
        if fuzzy_validation_status is not None and not callable(
            fuzzy_validation_status
        ):
            raise TypeError(
                "editor TM adapter fuzzy validation status must be callable"
            )
        if fuzzy_validation_start is not None and not callable(
            fuzzy_validation_start
        ):
            raise TypeError(
                "editor TM adapter fuzzy validation start must be callable"
            )
        self._runtime_host = runtime_host
        self._capability_host = capability_host
        self._fuzzy_validation_status = fuzzy_validation_status
        self._fuzzy_validation_start = fuzzy_validation_start

    def _text_matcher_handoff_for_controller(
        self,
    ) -> MatcherHandoffSnapshot:
        """Return the one host-owned matcher port and safe projection."""

        return self._capability_host.matcher_snapshot()

    def _is_current_text_matcher_handoff_for_controller(
        self,
        candidate: object,
    ) -> bool:
        """Prove candidate identity against this adapter's current Host issue."""

        return candidate is self._capability_host.matcher_snapshot()

    def _run_if_text_matcher_handoff_current_for_controller(
        self,
        candidate: MatcherHandoffSnapshot,
        operation: Callable[[], _OperationResultT],
    ) -> _OperationResultT:
        """Run one short term commit/publication under the Host reservation."""

        try:
            return self._capability_host._run_if_matcher_handoff_current(
                candidate,
                operation,
            )
        except _MatcherGenerationChanged as error:
            raise _TMMatcherGenerationChanged from error

    def query_current(
        self,
        *,
        segment: EditorSegment,
        project_session_id: str,
        query_epoch: int,
        preferences: TMPreferences,
    ) -> TMSuggestionReport:
        """Query one current segment and return one immutable mixed report."""

        return self._query_current_operation(
            segment=segment,
            project_session_id=project_session_id,
            query_epoch=query_epoch,
            preferences=preferences,
        ).report

    def _query_current_operation(
        self,
        *,
        segment: EditorSegment,
        project_session_id: str,
        query_epoch: int,
        preferences: TMPreferences,
    ) -> _TMQueryOperation:
        """Return one report with the generations captured by that query."""

        _validate_query_inputs(
            segment=segment,
            project_session_id=project_session_id,
            query_epoch=query_epoch,
            preferences=preferences,
        )
        canonical_batch = self._query_canonical(
            segment=segment,
            project_session_id=project_session_id,
            query_epoch=query_epoch,
            preferences=preferences,
        )
        _validate_canonical_batch_for_legacy(canonical_batch)
        _validate_current_query_binding(
            canonical_batch=canonical_batch,
            segment=segment,
            preferences=preferences,
        )
        legacy_batch = self._query_legacy_exact(
            canonical_batch=canonical_batch,
        )
        if legacy_batch.canonical_batch is not canonical_batch:
            raise ValueError("legacy query must retain the issued canonical batch")
        legacy_batch.__post_init__()
        _validate_canonical_batch_for_legacy(canonical_batch)

        query_identity = SuggestionQueryIdentity(
            project_session_id=project_session_id,
            segment_id=segment.id,
            source_digest=hashlib.sha256(
                segment.source.encode("utf-8")
            ).hexdigest(),
            query_epoch=query_epoch,
        )
        statuses = _project_resource_statuses(legacy_batch)
        status_by_resource_id = {
            status.resource_id: status for status in statuses
        }
        projected = _project_mixed_suggestions(
            legacy_batch=legacy_batch,
            status_by_resource_id=status_by_resource_id,
            query_identity=query_identity,
        )
        retrieval_status = _project_retrieval_display(canonical_batch.retrieval)
        report = TMSuggestionReport(
            suggestions=projected,
            resource_statuses=statuses,
            retrieval_status=retrieval_status,
            query_identity=query_identity,
        )
        return _TMQueryOperation(
            report=report,
            runtime_generation=canonical_batch.runtime.generation,
            retrieval_generation=canonical_batch.retrieval.generation,
        )

    def _current_query_generations(self) -> tuple[int, int]:
        """Read current host generations without capturing another operation."""

        runtime_generation = self._runtime_host._current_generation()
        retrieval_generation = (
            self._capability_host
            .retrieval_generation_notifications()
            .current()
        )
        if (
            type(runtime_generation) is not int
            or runtime_generation < 0
            or type(retrieval_generation) is not int
            or retrieval_generation < 0
        ):
            raise ValueError("TM query host generation drift")
        return runtime_generation, retrieval_generation

    def _run_if_query_generations_current(
        self,
        *,
        runtime_generation: int,
        retrieval_generation: int,
        operation: Callable[[], _OperationResultT],
    ) -> _OperationResultT:
        """Linearize one short editor commit against both host generations."""

        if type(runtime_generation) is not int or runtime_generation < 0:
            raise TypeError("runtime generation must be non-negative int")
        if type(retrieval_generation) is not int or retrieval_generation < 0:
            raise TypeError("retrieval generation must be non-negative int")
        if not callable(operation):
            raise TypeError("TM generation operation must be callable")

        def with_runtime_generation() -> _OperationResultT:
            return self._capability_host._run_if_retrieval_generation_current(
                retrieval_generation,
                operation,
            )

        try:
            return self._runtime_host._run_if_generation_current(
                runtime_generation,
                with_runtime_generation,
            )
        except (
            _RuntimeGenerationChanged,
            _RetrievalGenerationChanged,
        ) as error:
            raise _TMQueryGenerationChanged from error

    def _refresh_runtime_after_activation(
        self,
        configs: tuple[ResourceConfig, ...],
        validate_candidate: Callable[[TMRuntimeSnapshot], None],
    ) -> TMRuntimeSnapshot:
        """Atomically publish one activation-proven runtime candidate."""

        return self._refresh_runtime(
            configs,
            validate_candidate,
        )

    def _refresh_runtime(
        self,
        configs: tuple[ResourceConfig, ...],
        validate_candidate: Callable[[TMRuntimeSnapshot], None],
    ) -> TMRuntimeSnapshot:
        """Publish one fully validated resource-config runtime candidate."""

        return self._runtime_host._refresh_validated(
            configs,
            validate_candidate,
        )

    def _capture_runtime_for_controller(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot:
        """Return the defensive runtime cohort injected at Controller startup."""

        return self._runtime_host._capture_operation_snapshot_for_configs(
            configs
        )

    def _inspect_resource_statuses_for_controller(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> tuple[TMResourceStatus, ...]:
        """Return fresh read-only lifecycle facts from the injected resolver."""

        return self._runtime_host._inspect_resource_statuses(configs)

    def _inspect_retrieval_status_for_controller(self) -> RetrievalDisplayState:
        """Return the current frozen retrieval display without running a query."""

        _generation, display = (
            self._inspect_retrieval_projection_for_controller()
        )
        return display

    def _inspect_fuzzy_validation_for_controller(
        self,
    ) -> FuzzyValidationDisplay:
        """Return process-local validation lifecycle without capability."""

        reader = self._fuzzy_validation_status
        if reader is None:
            return FuzzyValidationDisplay(
                state=FuzzyValidationState.IDLE,
                safe_code=None,
            )
        status = reader()
        if type(status) is not FuzzyValidationDisplay:
            raise TypeError("fuzzy validation display contract is invalid")
        status.__post_init__()
        return FuzzyValidationDisplay(
            state=status.state,
            safe_code=status.safe_code,
        )

    def _start_fuzzy_validation_for_controller(
        self,
    ) -> FuzzyValidationDisplay:
        """Request the composition-owned real Gate D run."""

        starter = self._fuzzy_validation_start
        if starter is None:
            return FuzzyValidationDisplay(
                state=FuzzyValidationState.FAILED,
                safe_code="GATE_D.REVALIDATION_UNAVAILABLE",
            )
        status = starter()
        if type(status) is not FuzzyValidationDisplay:
            raise TypeError("fuzzy validation display contract is invalid")
        status.__post_init__()
        return FuzzyValidationDisplay(
            state=status.state,
            safe_code=status.safe_code,
        )

    def _inspect_retrieval_projection_for_controller(
        self,
    ) -> tuple[int, RetrievalDisplayState]:
        """Return one generation-bound display without running a query."""

        handoff = self._capability_host.retrieval_operation_snapshot()
        return handoff.generation, _project_retrieval_display(handoff)

    def append_confirmed(
        self,
        *,
        segment: EditorSegment,
        target: str,
        file_source: str,
    ) -> WriteReport:
        """Append one confirmed translation to the captured writable cohort."""

        draft = _confirmed_translation_draft(
            segment=segment,
            target=target,
            file_source=file_source,
        )
        runtime = self._runtime_host.capture_operation_snapshot()
        runtime.__post_init__()
        outcomes: list[TMResourceWriteOutcome] = []
        for port in _writable_ports(runtime):
            try:
                port.append(draft)
            except Exception as error:
                failure = _write_failure_outcome(port=port, error=error)
                if failure is None:
                    raise
                outcomes.append(failure)
                continue
            outcomes.append(
                TMResourceWriteOutcome(
                    resource_id=port.resource_id,
                    resource_name=port.resource_name,
                    global_order=port.global_order,
                    written=True,
                    error_code=None,
                    retryable=False,
                )
            )

        frozen_outcomes = tuple(outcomes)
        return WriteReport(
            written_resource_ids=tuple(
                outcome.resource_id
                for outcome in frozen_outcomes
                if outcome.written
            ),
            errors=tuple(
                outcome.error_code
                for outcome in frozen_outcomes
                if not outcome.written and outcome.error_code is not None
            ),
            outcomes=frozen_outcomes,
        )

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


def _validate_current_query_binding(
    *,
    canonical_batch: _CanonicalQueryBatch,
    segment: EditorSegment,
    preferences: TMPreferences,
) -> None:
    query = canonical_batch.query
    expected_resource_order = tuple(
        handle.resource_id
        for handle in canonical_batch.runtime.canonical_handles
        if handle.active and handle.lookup
    )
    if (
        query.query_source != segment.source
        or query.speaker_raw != (segment.speaker or None)
        or query.context_prev_raw is not None
        or query.context_next_raw is not None
        or query.minimum_similarity != preferences.minimum_similarity
        or query.limit != preferences.result_limit
        or query.resource_order != expected_resource_order
    ):
        raise ValueError("canonical batch drifted from the public query inputs")


def _project_mixed_suggestions(
    *,
    legacy_batch: _LegacyQueryBatch,
    status_by_resource_id: dict[str, TMResourceStatus],
    query_identity: SuggestionQueryIdentity,
) -> tuple[TMSuggestion, ...]:
    canonical_batch = legacy_batch.canonical_batch
    order_by_resource_id = dict(
        canonical_batch.runtime.global_order_by_resource_id
    )
    canonical_exact_by_resource_id: dict[str, list[TMResult]] = {}
    trailing_canonical: list[TMResult] = []
    for result in canonical_batch.report.results:
        if result.match_type is TMMatchType.EXACT:
            canonical_exact_by_resource_id.setdefault(
                result.resource_id,
                [],
            ).append(result)
        else:
            trailing_canonical.append(result)
    legacy_exact_by_resource_id = {
        result.resource_id: result for result in legacy_batch.results
    }

    projected: list[TMSuggestion] = []
    for resource_id, _global_order in canonical_batch.runtime.global_order_by_resource_id:
        legacy_result = legacy_exact_by_resource_id.get(resource_id)
        if legacy_result is not None:
            projected.append(
                _project_legacy_result(
                    legacy_result,
                    status=status_by_resource_id[resource_id],
                    query_identity=query_identity,
                )
            )
        for result in canonical_exact_by_resource_id.get(resource_id, ()):
            projected.append(
                _project_canonical_result(
                    result,
                    status=status_by_resource_id[resource_id],
                    query_identity=query_identity,
                )
            )
    for result in trailing_canonical:
        if result.resource_id not in order_by_resource_id:
            raise ValueError("canonical result is outside declarative order")
        projected.append(
            _project_canonical_result(
                result,
                status=status_by_resource_id[result.resource_id],
                query_identity=query_identity,
            )
        )

    unique: list[TMSuggestion] = []
    seen: set[tuple[str, str]] = set()
    for suggestion in projected:
        key = (suggestion.resource_id, suggestion.record_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(suggestion)
    return tuple(unique[:10])


def _project_canonical_result(
    result: TMResult,
    *,
    status: TMResourceStatus,
    query_identity: SuggestionQueryIdentity,
) -> TMSuggestion:
    return TMSuggestion(
        resource_id=result.resource_id,
        record_id=f"canonical:{result.record_id}",
        query_source=result.query_source,
        matched_source=result.matched_source,
        target=result.target,
        match_type=result.match_type,
        final_similarity=result.similarity,
        provenance=TMSuggestionProvenance(
            resource_name=status.resource_name,
            resource_mode=status.mode,
        ),
        query_identity=query_identity,
    )


def _project_legacy_result(
    result: _LegacyExactResult,
    *,
    status: TMResourceStatus,
    query_identity: SuggestionQueryIdentity,
) -> TMSuggestion:
    return TMSuggestion(
        resource_id=result.resource_id,
        record_id=_legacy_record_id(result),
        query_source=result.query_source,
        matched_source=result.matched_source,
        target=result.target,
        match_type=result.match_type,
        final_similarity=result.similarity,
        provenance=TMSuggestionProvenance(
            resource_name=status.resource_name,
            resource_mode=status.mode,
        ),
        query_identity=query_identity,
    )


def _legacy_record_id(result: _LegacyExactResult) -> str:
    source = result.record_source.encode("utf-8")
    target = result.record_target.encode("utf-8")
    digest = hashlib.sha256(
        b"localcat-legacy-record-v1\x00"
        + len(source).to_bytes(8, "big")
        + source
        + len(target).to_bytes(8, "big")
        + target
    ).hexdigest()
    return f"legacy:{digest}"


def _project_resource_statuses(
    legacy_batch: _LegacyQueryBatch,
) -> tuple[TMResourceStatus, ...]:
    canonical_batch = legacy_batch.canonical_batch
    runtime = canonical_batch.runtime
    metadata_by_resource_id = {
        metadata.resource_id: metadata
        for metadata in canonical_batch.report.resource_metadata
    }
    canonical_failure_by_resource_id = {
        failure.resource_id: failure
        for failure in canonical_batch.report.resource_failures
    }
    legacy_failure_by_resource_id = {
        failure.resource_id: failure for failure in legacy_batch.failures
    }
    retrieval_display = canonical_batch.retrieval.display
    for result in canonical_batch.report.results:
        metadata = metadata_by_resource_id[result.resource_id]
        if (
            result.match_type is TMMatchType.CONTEXT
            and not metadata.context_available
        ):
            raise ValueError(
                "context result exceeds captured resource authority"
            )
        if (
            result.match_type is TMMatchType.FUZZY
            and not metadata.recall.fuzzy_available
        ):
            raise ValueError(
                "fuzzy result exceeds captured resource authority"
            )
    statuses: list[TMResourceStatus] = []
    for base in runtime.statuses:
        metadata = metadata_by_resource_id.get(base.resource_id)
        canonical_failure = canonical_failure_by_resource_id.get(base.resource_id)
        legacy_failure = legacy_failure_by_resource_id.get(base.resource_id)
        if canonical_failure is not None and legacy_failure is not None:
            raise ValueError("one resource cannot fail in both runtime lanes")
        failure = canonical_failure or legacy_failure
        if metadata is not None:
            if (
                metadata.context_available
                and not retrieval_display.context_available
            ):
                raise ValueError(
                    "resource context availability exceeds captured Core authority"
                )
            if (
                metadata.recall.fuzzy_available
                and not retrieval_display.fuzzy_available
            ):
                raise ValueError(
                    "resource fuzzy availability exceeds captured Core authority"
                )

        safe_codes = list(base.safe_codes)
        if failure is not None:
            safe_codes.extend((failure.stage, failure.error_code))
        if metadata is not None:
            if metadata.context_unavailable_code is not None:
                safe_codes.append(metadata.context_unavailable_code)
            if metadata.recall.fuzzy_unavailable_code is not None:
                safe_codes.append(metadata.recall.fuzzy_unavailable_code)

        mode = base.mode
        exact_available = base.exact_available
        context_available = False
        fuzzy_available = False
        retryable = base.retryable
        if metadata is not None:
            context_available = metadata.context_available
            fuzzy_available = metadata.recall.fuzzy_available
        if failure is not None:
            mode = TMResourceDisplayMode.DEGRADED
            exact_available = metadata is not None and base.exact_available
            retryable = retryable or failure.retryable
        statuses.append(
            TMResourceStatus(
                resource_id=base.resource_id,
                resource_name=base.resource_name,
                mode=mode,
                exact_available=exact_available,
                context_available=context_available,
                fuzzy_available=fuzzy_available,
                safe_codes=_stable_unique_codes(safe_codes),
                retryable=retryable,
            )
        )
    return tuple(statuses)


def _stable_unique_codes(codes: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
    return tuple(result)


def _project_retrieval_display(
    retrieval: RetrievalHandoffSnapshot,
) -> RetrievalDisplayState:
    retrieval.__post_init__()
    display = retrieval.display
    display.__post_init__()
    return RetrievalDisplayState(
        context_available=display.context_available,
        fuzzy_available=display.fuzzy_available,
        safe_codes=tuple(display.safe_codes),
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


type _WritableTMPort = LegacyExactPort | CanonicalResourcePort


def _confirmed_translation_draft(
    *,
    segment: EditorSegment,
    target: str,
    file_source: str,
) -> TMRecordDraft:
    if type(segment) is not EditorSegment:
        raise TypeError("TM append segment must be EditorSegment")
    segment.__post_init__()
    if type(target) is not str:
        raise TypeError("TM append target must be exact str")
    if not target.strip():
        raise ValueError("TM append target must not be empty")
    if type(file_source) is not str:
        raise TypeError("TM append file source must be exact str")
    if not file_source.strip():
        raise ValueError("TM append file source must not be empty")
    return TMRecordDraft(
        source_raw=segment.source,
        target_raw=target,
        speaker_raw=segment.speaker or None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=file_source,
        provenance=(("source", "local-write"),),
    )


def _writable_ports(runtime: TMRuntimeSnapshot) -> tuple[_WritableTMPort, ...]:
    ports: dict[str, _WritableTMPort] = {}
    for port in (*runtime.legacy_ports, *runtime.canonical_ports):
        if port.resource_id in ports:
            raise ValueError("runtime resource cannot have two append ports")
        ports[port.resource_id] = port

    selected: list[_WritableTMPort] = []
    for resource_id, global_order in runtime.global_order_by_resource_id:
        port = ports.get(resource_id)
        if port is None:
            continue
        if port.global_order != global_order:
            raise ValueError("TM append port drifted from declarative order")
        if port.active and port.update:
            selected.append(port)
    return tuple(selected)


def _write_failure_outcome(
    *,
    port: _WritableTMPort,
    error: Exception,
) -> TMResourceWriteOutcome | None:
    known_operation_error = _is_write_operation_error(error)
    formal_legacy_failure = (
        error
        if (
            type(port) is LegacyExactPort
            and type(error) is LegacyAppendOperationError
        )
        else None
    )
    if formal_legacy_failure is not None and (
        formal_legacy_failure.error_code != _LEGACY_APPEND_FAILED_CODE
        or formal_legacy_failure.retryable is not True
    ):
        raise ValueError("formal legacy append failure contract drift")
    known_formal_legacy_failure = (
        type(port) is LegacyExactPort
        and formal_legacy_failure is not None
    )
    if not known_operation_error and not known_formal_legacy_failure:
        return None
    retryable_fact = getattr(error, "retryable", None)
    retryable = (
        retryable_fact
        if type(retryable_fact) is bool
        else isinstance(error, (OSError, sqlite3.OperationalError))
        or known_formal_legacy_failure
    )
    error_code = (
        _LEGACY_APPEND_FAILED_CODE
        if type(port) is LegacyExactPort
        else _CANONICAL_APPEND_FAILED_CODE
    )
    return TMResourceWriteOutcome(
        resource_id=port.resource_id,
        resource_name=port.resource_name,
        global_order=port.global_order,
        written=False,
        error_code=error_code,
        retryable=retryable,
    )


def _is_write_operation_error(error: Exception) -> bool:
    if isinstance(
        error,
        (sqlite3.ProgrammingError, sqlite3.NotSupportedError),
    ):
        return False
    return isinstance(error, _WRITE_OPERATION_ERRORS) or isinstance(
        error,
        sqlite3.DatabaseError,
    )


__all__ = ["EditorTMAdapter"]
