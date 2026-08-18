"""Qt-free editing session and language-resource coordination."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, RLock, Thread
from uuid import uuid4

from editor_contracts import (
    ConfirmResult,
    DisplayPreferences,
    EditorProject,
    EditorSegment,
    ImportReport,
    ImportRequest,
    ResourceConfig,
    ResourceKind,
    RecentProject,
    LegacyExactTMSuggestion,
    SuggestionBundle,
    TMPreferences,
    TMActivationOperationView,
    TMActivationPreflightView,
    TMResourceDisplayMode,
    TMSuggestion,
    TMSuggestionReport,
    TMThresholdUpdateOutcome,
    TermSuggestion,
    WriteReport,
)
from editor_tm_adapter import EditorTMAdapter, _TMQueryGenerationChanged
from editor_project import load_project, sample_project, save_project as save_project_file
from glossary_engine import GlossaryEngine
from resource_importer import import_termbase, import_tmx, upsert_term
from resource_repository import ResourceError, ResourceRepository
from renpy_tm_compat import build_dialogue_alias, unwrap_dialogue_target
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationPreflight,
    MigrationReport,
    StoreHealth,
)
from tm_engine import SourceUnit, TMEngine
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator
from workspace_state import WorkspaceStateError, WorkspaceStateRepository


LOGGER = logging.getLogger(__name__)


class EditorControllerError(RuntimeError):
    """Raised when an editor operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class _PreparedTMActivation:
    """Controller-private Core authority retained after read-only preflight."""

    config: ResourceConfig
    service: TMMigrationService
    preflight: MigrationPreflight
    view: TMActivationPreflightView

    def __post_init__(self) -> None:
        if type(self.config) is not ResourceConfig:
            raise TypeError("prepared activation config must be ResourceConfig")
        self.config.__post_init__()
        if type(self.service) is not TMMigrationService:
            raise TypeError("prepared activation service must be TMMigrationService")
        if type(self.preflight) is not MigrationPreflight:
            raise TypeError("prepared activation preflight must be MigrationPreflight")
        self.preflight.__post_init__()
        if type(self.view) is not TMActivationPreflightView:
            raise TypeError("prepared activation view must be exact contract")
        self.view.__post_init__()


def _initial_tm_activation_service(config: ResourceConfig) -> TMMigrationService:
    """Construct the sole Core public first-activation owner for a resource."""

    if type(config) is not ResourceConfig:
        raise TypeError("TM activation config must be ResourceConfig")
    config.__post_init__()
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        config.id,
        config.path,
    )
    canonical_store_id = f"store.{config.id}"
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
        coordinator=coordinator,
    )


def _tm_rebuild_service(config: ResourceConfig) -> TMMigrationService:
    """Reopen the resource's proven LKG coordinator for explicit rebuild."""

    if type(config) is not ResourceConfig:
        raise TypeError("TM rebuild config must be ResourceConfig")
    config.__post_init__()
    engine = TMEngine(str(config.path))
    store = engine.canonical_store
    if store is None:
        raise EditorControllerError(
            "TM rebuild requires an active canonical resource"
        )
    coordinator = store.coordinator
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        config.id,
        config.path,
    )
    if coordinator.resource_id != config.id:
        raise EditorControllerError("TM rebuild canonical identity changed")
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=coordinator.canonical_store_id,
        coordinator=coordinator,
    )


def _activation_preflight_equal(
    left: MigrationPreflight,
    right: MigrationPreflight,
) -> bool:
    """Compare every source/count fact needed to bind user confirmation."""

    left.__post_init__()
    right.__post_init__()
    return (
        left.source_digest == right.source_digest
        and left.valid_count == right.valid_count
        and left.invalid_count == right.invalid_count
        and left.duplicate_source_count == right.duplicate_source_count
        and left.variant_count == right.variant_count
        and left.diagnostics == right.diagnostics
    )


def _activation_view_equal(
    left: TMActivationPreflightView,
    right: TMActivationPreflightView,
) -> bool:
    """Compare the complete body-free preflight membership projection."""

    left.__post_init__()
    right.__post_init__()
    return (
        left.resource_id == right.resource_id
        and left.resource_name == right.resource_name
        and left.valid_count == right.valid_count
        and left.invalid_count == right.invalid_count
        and left.variant_count == right.variant_count
    )


def _validate_activation_runtime_candidate(
    snapshot: object,
    *,
    resource_id: str,
    outcome: MigrationReport | MigrationFailure,
    service_canonical_store_id: str,
) -> None:
    """Bind a complete runtime candidate to one exact Core outcome."""

    from tm_application_composition import TMRuntimeSnapshot

    if type(snapshot) is not TMRuntimeSnapshot:
        raise TypeError("activation runtime candidate must be TMRuntimeSnapshot")
    if (
        type(service_canonical_store_id) is not str
        or not service_canonical_store_id.strip()
    ):
        raise TypeError("activation service canonical store id is required")
    snapshot.__post_init__()
    statuses = tuple(
        status for status in snapshot.statuses
        if status.resource_id == resource_id
    )
    if len(statuses) != 1:
        raise ValueError("activation runtime candidate status is incomplete")
    status = statuses[0]
    legacy_ids = tuple(port.resource_id for port in snapshot.legacy_ports)
    canonical_ids = tuple(
        port.handle.resource_id for port in snapshot.canonical_ports
    )
    canonical_port = next(
        (
            port
            for port in snapshot.canonical_ports
            if port.handle.resource_id == resource_id
        ),
        None,
    )

    def canonical_health() -> StoreHealth:
        if canonical_port is None:
            raise ValueError("activation runtime canonical port is missing")
        health = canonical_port.handle.store.health()
        if type(health) is not StoreHealth:
            raise TypeError("activation runtime health contract is invalid")
        health.__post_init__()
        return health

    def require_service_canonical_store() -> None:
        if canonical_port is None:
            raise ValueError("activation runtime canonical port is missing")
        coordinator = getattr(canonical_port.handle.store, "coordinator", None)
        if (
            coordinator is None
            or getattr(coordinator, "canonical_store_id", None)
            != service_canonical_store_id
        ):
            raise ValueError("activation service canonical store changed")

    if isinstance(outcome, MigrationReport):
        outcome.__post_init__()
        if outcome.resource_id != resource_id:
            raise ValueError("activation report resource identity changed")
        if outcome.canonical_store_id != service_canonical_store_id:
            raise ValueError("activation report service authority changed")
        if (
            status.mode is not TMResourceDisplayMode.CANONICAL_ACTIVE
            or not status.exact_available
            or status.context_available
            or status.fuzzy_available
            or resource_id in legacy_ids
            or canonical_ids.count(resource_id) != 1
            or outcome.context_available
            or outcome.fuzzy_available
        ):
            raise ValueError("activation success runtime is not canonical")
        health = canonical_health()
        if health.generation != outcome.activated_generation:
            raise ValueError("activation success runtime generation changed")
        require_service_canonical_store()
        return

    outcome.__post_init__()
    if outcome.canonical_authority_ambiguous:
        if (
            status.mode is not TMResourceDisplayMode.UNAVAILABLE
            or status.exact_available
            or resource_id in legacy_ids
            or resource_id in canonical_ids
        ):
            raise ValueError("ambiguous activation retained query authority")
        return
    if outcome.canonical_authority_published:
        if status.mode is TMResourceDisplayMode.UNAVAILABLE:
            if (
                status.exact_available
                or resource_id in legacy_ids
                or resource_id in canonical_ids
            ):
                raise ValueError("published-unavailable runtime is contradictory")
            return
        if (
            status.mode is TMResourceDisplayMode.CANONICAL_ACTIVE
            and status.exact_available
            and resource_id not in legacy_ids
            and canonical_ids.count(resource_id) == 1
        ):
            if canonical_health().generation != outcome.active_generation:
                raise ValueError("published activation generation changed")
            require_service_canonical_store()
            return
        raise ValueError("published activation runtime is not fail-closed")
    if outcome.active_generation is not None:
        if (
            status.mode
            not in (
                TMResourceDisplayMode.CANONICAL_ACTIVE,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            )
            or not status.exact_available
            or status.context_available
            or status.fuzzy_available
            or resource_id in legacy_ids
            or canonical_ids.count(resource_id) != 1
        ):
            raise ValueError("canonical update failure did not preserve LKG")
        if canonical_health().generation != outcome.active_generation:
            raise ValueError("canonical LKG generation changed")
        require_service_canonical_store()
        return
    if (
        status.mode is not TMResourceDisplayMode.LEGACY_EXACT_ONLY
        or not status.exact_available
        or status.context_available
        or status.fuzzy_available
        or legacy_ids.count(resource_id) != 1
        or resource_id in canonical_ids
    ):
        raise ValueError("proven activation failure did not preserve legacy")


def _activation_outcome_requires_query_block(
    outcome: MigrationReport | MigrationFailure,
) -> bool:
    """Return whether stale legacy use is forbidden after refresh failure."""

    if isinstance(outcome, MigrationReport):
        return True
    return (
        outcome.canonical_authority_published
        or outcome.canonical_authority_ambiguous
    )


def _validate_activation_compatibility_engine(
    engine: TMEngine,
    outcome: MigrationReport | MigrationFailure,
    *,
    service_canonical_store_id: str,
) -> None:
    """Re-prove one no-adapter Controller engine against the Core outcome."""

    if type(engine) is not TMEngine:
        raise TypeError("activation compatibility engine must be TMEngine")
    if (
        type(service_canonical_store_id) is not str
        or not service_canonical_store_id.strip()
    ):
        raise TypeError("activation service canonical store id is required")
    store = engine.canonical_store
    if isinstance(outcome, MigrationReport):
        if store is None:
            raise ValueError("activation success compatibility engine is legacy")
        health = store.health()
        health.__post_init__()
        if (
            health.generation != outcome.activated_generation
            or store.coordinator.canonical_store_id
            != service_canonical_store_id
            or outcome.canonical_store_id != service_canonical_store_id
        ):
            raise ValueError("activation success compatibility authority changed")
        return
    if outcome.canonical_authority_ambiguous:
        raise ValueError("ambiguous activation has no safe compatibility engine")
    if outcome.canonical_authority_published or outcome.active_generation is not None:
        if store is None:
            raise ValueError("canonical failure compatibility engine is legacy")
        health = store.health()
        health.__post_init__()
        if (
            health.generation != outcome.active_generation
            or store.coordinator.canonical_store_id
            != service_canonical_store_id
        ):
            raise ValueError("canonical failure compatibility generation changed")
        return
    if store is not None:
        raise ValueError("proven first failure compatibility engine is canonical")


def _clone_tm_suggestion_report(
    report: TMSuggestionReport,
) -> TMSuggestionReport:
    """Create a defensive UI-contract graph with one private query identity."""

    if type(report) is not TMSuggestionReport:
        raise TypeError("TM suggestion report must be TMSuggestionReport")
    report.__post_init__()
    identity = replace(report.query_identity)
    cloned = TMSuggestionReport(
        suggestions=tuple(
            replace(
                suggestion,
                provenance=replace(suggestion.provenance),
                query_identity=identity,
            )
            for suggestion in report.suggestions
        ),
        resource_statuses=tuple(
            replace(status) for status in report.resource_statuses
        ),
        retrieval_status=replace(report.retrieval_status),
        query_identity=identity,
    )
    cloned.__post_init__()
    return cloned


def _clone_tm_suggestion(suggestion: TMSuggestion) -> TMSuggestion:
    """Capture one exact suggestion graph before membership comparison."""

    if type(suggestion) is not TMSuggestion:
        raise TypeError("TM suggestion must be TMSuggestion")
    suggestion.__post_init__()
    identity = replace(suggestion.query_identity)
    cloned = replace(
        suggestion,
        provenance=replace(suggestion.provenance),
        query_identity=identity,
    )
    cloned.__post_init__()
    return cloned


def _tm_suggestion_fields_equal(
    candidate: TMSuggestion,
    issued: TMSuggestion,
) -> bool:
    """Compare every frozen membership field without trusting dataclass eq."""

    return (
        candidate.resource_id == issued.resource_id
        and candidate.record_id == issued.record_id
        and candidate.query_source == issued.query_source
        and candidate.matched_source == issued.matched_source
        and candidate.target == issued.target
        and candidate.match_type is issued.match_type
        and candidate.final_similarity == issued.final_similarity
        and candidate.provenance.resource_name
        == issued.provenance.resource_name
        and candidate.provenance.resource_mode
        is issued.provenance.resource_mode
        and candidate.query_identity.project_session_id
        == issued.query_identity.project_session_id
        and candidate.query_identity.segment_id
        == issued.query_identity.segment_id
        and candidate.query_identity.source_digest
        == issued.query_identity.source_digest
        and candidate.query_identity.query_epoch
        == issued.query_identity.query_epoch
    )


class EditorController:
    """Own one immutable editor session while engines remain UI-state free."""

    def __init__(
        self,
        repository: ResourceRepository,
        workspace_state: WorkspaceStateRepository | None = None,
        tm_adapter: EditorTMAdapter | None = None,
    ) -> None:
        if tm_adapter is not None and type(tm_adapter) is not EditorTMAdapter:
            raise TypeError("TM adapter must be EditorTMAdapter")
        self.repository = repository
        self.workspace_state = workspace_state or WorkspaceStateRepository(
            repository.config_dir
        )
        self._tm_adapter = tm_adapter
        self._tm_query_lock = RLock()
        self._project_session_id = uuid4().hex
        self._tm_query_epoch = 0
        self._issued_tm_suggestions: tuple[TMSuggestion, ...] = ()
        self._issued_legacy_tm_suggestions: tuple[
            LegacyExactTMSuggestion,
            ...,
        ] = ()
        self._legacy_issued_context: tuple[
            str,
            str,
            str,
            str,
            int,
        ] | None = None
        self._current_tm_report: TMSuggestionReport | None = None
        self._observed_tm_signature: tuple[
            str,
            str,
            str,
            int,
            int,
            float,
            int,
            str,
        ] | None = None
        self._tm_activation_condition = Condition(RLock())
        self._prepared_tm_activation: _PreparedTMActivation | None = None
        self._tm_activation_operation: TMActivationOperationView | None = None
        self._tm_activation_outcome: MigrationReport | MigrationFailure | None = (
            None
        )
        self._tm_activation_worker_error: BaseException | None = None
        self._tm_runtime_blocked_safe_code: str | None = None
        self._project: EditorProject | None = None
        self._current_index = 0
        self._dirty = False
        self._tm_engines: dict[str, TMEngine] = {}
        self._glossary_engines: dict[str, GlossaryEngine] = {}
        self.reload_resources(_refresh_runtime=False)

    @property
    def project(self) -> EditorProject:
        if self._project is None:
            raise EditorControllerError("no project is open")
        return self._project

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def current_segment(self) -> EditorSegment:
        return self.project.segments[self._current_index]

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def project_session_id(self) -> str:
        """Return the opaque identity of the current project session."""

        return self._project_session_id

    @property
    def query_epoch(self) -> int:
        """Return the aggregate query epoch after observing external drift."""

        with self._tm_query_lock:
            self._synchronize_tm_query_state()
            return self._tm_query_epoch

    @property
    def issued_tm_suggestions(self) -> tuple[TMSuggestion, ...]:
        """Return only the complete tuple issued for the current epoch."""

        with self._tm_query_lock:
            self._synchronize_tm_query_state()
            report = self._current_tm_report
            if report is None:
                return ()
            return _clone_tm_suggestion_report(report).suggestions

    @property
    def has_project(self) -> bool:
        return self._project is not None

    @property
    def confirmed_count(self) -> int:
        return sum(segment.confirmed for segment in self.project.segments)

    @property
    def completion_ratio(self) -> float:
        segments = self.project.segments
        return self.confirmed_count / len(segments) if segments else 0.0

    def open_project(self, path: Path) -> EditorProject:
        """Load a local JSON/TXT project and reset navigation only after success."""

        project = load_project(path)
        with self._tm_query_lock:
            self.set_project(project)
            remembered = self.workspace_state.find_project(project.path or path)
            if remembered is not None:
                restored_index = next(
                    (
                        index
                        for index, segment in enumerate(project.segments)
                        if segment.id == remembered.segment_id
                    ),
                    remembered.index
                    if remembered.index < len(project.segments)
                    else 0,
                )
                self._current_index = restored_index
                self._record_current_tm_baseline()
            self._remember_current_position()
            return self.project

    def load_sample(self) -> EditorProject:
        """Open the bundled original sample project."""

        return self.set_project(sample_project())

    def set_project(self, project: EditorProject) -> EditorProject:
        """Install an already validated project contract as the current session."""

        if not project.segments:
            raise EditorControllerError("project contains no segments")
        with self._tm_query_lock:
            self._project = project
            self._current_index = 0
            self._dirty = False
            self._project_session_id = uuid4().hex
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
            return project

    def update_target(self, target: str) -> EditorProject:
        """Persist the current edit in the immutable session and reopen confirmation."""

        if not isinstance(target, str):
            raise EditorControllerError("target text must be a string")
        with self._tm_query_lock:
            current = self.current_segment
            if target == current.target:
                return self.project
            segments = list(self.project.segments)
            segments[self._current_index] = replace(
                current,
                target=target,
                confirmed=False,
            )
            self._project = replace(self.project, segments=tuple(segments))
            self._dirty = True
            return self.project

    def move(self, direction: int, unconfirmed_only: bool = False) -> EditorProject:
        """Move one segment or find the next unconfirmed segment without losing edits."""

        if direction == 0:
            raise EditorControllerError("navigation direction must not be zero")
        with self._tm_query_lock:
            segments = self.project.segments
            step = 1 if direction > 0 else -1
            if unconfirmed_only:
                candidates = range(
                    self._current_index + step,
                    len(segments) if step > 0 else -1,
                    step,
                )
                destination = next(
                    (index for index in candidates if not segments[index].confirmed),
                    self._current_index,
                )
            else:
                destination = min(
                    max(self._current_index + step, 0),
                    len(segments) - 1,
                )
            if destination != self._current_index:
                self._current_index = destination
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
            self._remember_current_position()
            return self.project

    def go_to(self, index: int) -> EditorProject:
        """Select one segment by index while preserving all current edits."""

        if index < 0 or index >= len(self.project.segments):
            raise EditorControllerError(f"segment index is out of range: {index}")
        with self._tm_query_lock:
            if index != self._current_index:
                self._current_index = index
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
            self._remember_current_position()
            return self.project

    def save_project(self, path: Path) -> EditorProject:
        """Atomically save the current project and clear the session dirty flag."""

        saved_path = save_project_file(self.project, path)
        self._project = replace(self.project, path=saved_path)
        self._dirty = False
        self._remember_current_position()
        return self.project

    def close_project(self) -> None:
        """Leave the current project after the frontend has handled unsaved changes."""

        with self._tm_query_lock:
            if self._project is not None:
                self._remember_current_position()
            self._project = None
            self._current_index = 0
            self._dirty = False
            self._project_session_id = uuid4().hex
            self._advance_tm_query_epoch()
            self._observed_tm_signature = None

    def recent_projects(self) -> tuple[RecentProject, ...]:
        """Return locally remembered projects in most-recent-first order."""

        return self.workspace_state.recent_projects()

    def remove_recent_project(self, path: Path) -> None:
        """Forget a stale recent-project entry without touching its file."""

        try:
            self.workspace_state.remove_recent(path)
        except WorkspaceStateError as exc:
            raise EditorControllerError(str(exc)) from exc

    def display_preferences(self) -> DisplayPreferences:
        """Return local editor-only display preferences."""

        return self.workspace_state.display_preferences()

    def update_display_preferences(
        self,
        preferences: DisplayPreferences,
    ) -> DisplayPreferences:
        """Persist editor-only display preferences."""

        try:
            return self.workspace_state.update_display_preferences(preferences)
        except WorkspaceStateError as exc:
            raise EditorControllerError(str(exc)) from exc

    def tm_preferences(self) -> TMPreferences:
        """Return one defensive copy of the shared device-local TM preference."""

        with self._tm_query_lock:
            preferences = self.workspace_state.tm_preferences()
            if type(preferences) is not TMPreferences:
                raise TypeError("workspace TM preferences contract is invalid")
            preferences.__post_init__()
            return TMPreferences(
                minimum_similarity=preferences.minimum_similarity,
                result_limit=preferences.result_limit,
            )

    def update_tm_minimum_similarity(
        self,
        minimum_similarity: float,
    ) -> TMThresholdUpdateOutcome:
        """Persist one threshold and refresh the current query as one UI action."""

        with self._tm_query_lock:
            previous = self.tm_preferences()
            try:
                requested = TMPreferences(
                    minimum_similarity=minimum_similarity,
                    result_limit=previous.result_limit,
                )
            except (TypeError, ValueError):
                return TMThresholdUpdateOutcome(
                    succeeded=False,
                    preferences=previous,
                    safe_code="TM.THRESHOLD.INVALID",
                )

            if requested == previous:
                return TMThresholdUpdateOutcome(
                    succeeded=True,
                    preferences=previous,
                    safe_code=None,
                )

            try:
                persisted = self.workspace_state.update_tm_preferences(requested)
            except WorkspaceStateError:
                return TMThresholdUpdateOutcome(
                    succeeded=False,
                    preferences=previous,
                    safe_code="TM.THRESHOLD.PERSISTENCE_FAILED",
                )
            if type(persisted) is not TMPreferences:
                raise TypeError("workspace TM preference update returned invalid type")
            persisted.__post_init__()
            if persisted != requested:
                raise ValueError("workspace TM preference update changed the request")

            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
            if self._tm_adapter is not None and self._project is not None:
                _ = self.tm_suggestion_report()
            return TMThresholdUpdateOutcome(
                succeeded=True,
                preferences=TMPreferences(
                    minimum_similarity=persisted.minimum_similarity,
                    result_limit=persisted.result_limit,
                ),
                safe_code=None,
            )

    def tm_suggestion_report(self) -> TMSuggestionReport:
        """Query and atomically issue the current frozen TM suggestion tuple."""

        with self._tm_query_lock:
            self._require_tm_runtime_available()
            self._synchronize_tm_query_state(refresh_current=False)
            return self._query_and_issue_current_tm_report()

    def _query_and_issue_current_tm_report(self) -> TMSuggestionReport:
        """Run one production query without recursively synchronizing state."""

        adapter = self._tm_adapter
        if adapter is None:
            raise EditorControllerError("TM query adapter is not configured")
        for _attempt in range(4):
            segment = self.current_segment
            preferences = self.workspace_state.tm_preferences()
            operation = adapter._query_current_operation(
                segment=segment,
                project_session_id=self._project_session_id,
                query_epoch=self._tm_query_epoch,
                preferences=preferences,
            )
            operation.__post_init__()
            operation_signature = self._tm_signature(
                segment=segment,
                preferences=preferences,
                runtime_generation=operation.runtime_generation,
                retrieval_generation=operation.retrieval_generation,
            )
            try:
                current_signature = self._capture_current_tm_signature()
            except ValueError:
                self._advance_tm_query_epoch()
                self._observed_tm_signature = None
                continue
            if current_signature != operation_signature:
                self._advance_tm_query_epoch()
                self._observed_tm_signature = current_signature
                continue
            if (
                self._observed_tm_signature is not None
                and operation_signature != self._observed_tm_signature
            ):
                self._advance_tm_query_epoch()
                self._observed_tm_signature = operation_signature
                continue

            report = operation.report
            report.__post_init__()
            identity = report.query_identity
            expected_digest = hashlib.sha256(
                segment.source.encode("utf-8")
            ).hexdigest()
            if (
                identity.project_session_id != self._project_session_id
                or identity.segment_id != segment.id
                or identity.source_digest != expected_digest
                or identity.query_epoch != self._tm_query_epoch
            ):
                raise EditorControllerError(
                    "TM query report identity does not match the current session"
                )
            private_report = _clone_tm_suggestion_report(report)

            def commit_current_report() -> TMSuggestionReport:
                self._observed_tm_signature = operation_signature
                self._current_tm_report = private_report
                self._issued_tm_suggestions = private_report.suggestions
                return report

            try:
                return adapter._run_if_query_generations_current(
                    runtime_generation=operation.runtime_generation,
                    retrieval_generation=operation.retrieval_generation,
                    operation=commit_current_report,
                )
            except _TMQueryGenerationChanged:
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
                continue
        raise EditorControllerError("unable to capture a stable TM query snapshot")

    def suggestions(self) -> SuggestionBundle:
        """Issue the temporary legacy bundle for the pre-integration Qt UI."""

        with self._tm_query_lock:
            self._require_tm_runtime_available()
            self._synchronize_tm_query_state()
            bundle = self._legacy_suggestions()
            issued = tuple(replace(item) for item in bundle.tm_matches)
            for item in issued:
                item.__post_init__()
            segment = self.current_segment
            self._issued_legacy_tm_suggestions = issued
            self._legacy_issued_context = (
                self._project_session_id,
                segment.id,
                hashlib.sha256(segment.source.encode("utf-8")).hexdigest(),
                hashlib.sha256(
                    (segment.speaker or "").encode("utf-8")
                ).hexdigest(),
                self._tm_query_epoch,
            )
            return bundle

    def _legacy_suggestions(self) -> SuggestionBundle:
        """Query every currently active Lookup resource for the current source."""

        segment = self.current_segment
        source = segment.source
        tm_matches: list[LegacyExactTMSuggestion] = []
        terms: list[TermSuggestion] = []
        for resource in self.repository.list_resources():
            if not resource.active or not resource.lookup:
                continue
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                engine = self._tm_engines.get(resource.id)
                match = engine.query_exact(source) if engine is not None else None
                if match is not None:
                    tm_matches.append(
                        LegacyExactTMSuggestion(
                            source=match.source,
                            target=match.target,
                            resource_id=resource.id,
                            resource_name=resource.name,
                            similarity=match.similarity,
                            match_type=match.match_type,
                        )
                    )
                    continue
                alias = build_dialogue_alias(segment.speaker, source)
                wrapped = (
                    engine.query_exact(alias)
                    if engine is not None and alias is not None
                    else None
                )
                target = (
                    unwrap_dialogue_target(wrapped.target, segment.speaker)
                    if wrapped is not None
                    else None
                )
                if wrapped is not None and target is not None:
                    tm_matches.append(
                        LegacyExactTMSuggestion(
                            source=source,
                            target=target,
                            resource_id=resource.id,
                            resource_name=resource.name,
                            similarity=wrapped.similarity,
                            match_type=wrapped.match_type,
                        )
                    )
            else:
                engine = self._glossary_engines.get(resource.id)
                if engine is None:
                    continue
                for hit in engine.extract_terms(source):
                    terms.append(
                        TermSuggestion(
                            source_term=hit.source_term,
                            target_term=hit.target_term,
                            start_index=hit.start_index,
                            end_index=hit.end_index,
                            resource_id=resource.id,
                            resource_name=resource.name,
                            definition=hit.definition,
                        )
                    )
        return SuggestionBundle(tm_matches=tuple(tm_matches), terms=tuple(terms))

    def confirm_current(self) -> ConfirmResult:
        """Write the current translation to every writable TM before confirmation."""

        with self._tm_query_lock:
            self._require_tm_runtime_available()
            current = self.current_segment
            if not current.target.strip():
                raise EditorControllerError(
                    "target text must not be empty before confirmation"
                )
            adapter = self._tm_adapter
            if adapter is not None:
                report = adapter.append_confirmed(
                    segment=current,
                    target=current.target,
                    file_source=self.project.name,
                )
                if type(report) is not WriteReport:
                    raise TypeError("TM adapter must return WriteReport")
                report.__post_init__()
                if not report.outcomes and (
                    report.written_resource_ids or report.errors
                ):
                    raise ValueError(
                        "TM adapter write report must retain structured outcomes"
                    )
            else:
                unit = SourceUnit(
                    id=current.id,
                    text=current.source,
                    speaker=current.speaker or None,
                    file_source=self.project.name,
                )
                written: list[str] = []
                errors: list[str] = []
                for resource in self.repository.list_resources():
                    if (
                        resource.kind is not ResourceKind.TRANSLATION_MEMORY
                        or not resource.active
                        or not resource.update
                    ):
                        continue
                    engine = self._tm_engines.get(resource.id)
                    if engine is None:
                        errors.append(
                            f"{resource.name}: translation memory is not loaded"
                        )
                    elif engine.save_record(unit, current.target):
                        written.append(resource.id)
                    else:
                        errors.append(
                            f"{resource.name}: unable to write translation memory"
                        )
                report = WriteReport(
                    written_resource_ids=tuple(written),
                    errors=tuple(errors),
                )

            if not report.succeeded:
                return ConfirmResult(
                    project=self.project,
                    current_index=self._current_index,
                    write_report=report,
                )

            segments = list(self.project.segments)
            segments[self._current_index] = replace(current, confirmed=True)
            self._project = replace(self.project, segments=tuple(segments))
            self._dirty = True
            current_index = self._current_index
            next_index = next(
                (
                    index
                    for index in range(current_index + 1, len(segments))
                    if not segments[index].confirmed
                ),
                current_index,
            )
            self._current_index = next_index
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
            self._remember_current_position()
            return ConfirmResult(
                project=self.project,
                current_index=next_index,
                write_report=report,
            )

    def prepare_tm_activation(
        self,
        resource_id: str,
    ) -> TMActivationPreflightView:
        """Return read-only activation counts and retain Core authority privately."""

        return self._prepare_initial_tm_activation(resource_id)

    def _prepare_initial_tm_activation(
        self,
        resource_id: str,
    ) -> TMActivationPreflightView:
        """Issue one initial-activation preflight without exposing Core authority."""

        if type(resource_id) is not str or not resource_id.strip():
            raise EditorControllerError("TM activation resource id is required")
        with self._tm_activation_condition:
            if self._tm_activation_operation is not None:
                if not self._tm_activation_operation.completed:
                    raise EditorControllerError(
                        "a TM activation is already in progress"
                    )
                self._tm_activation_operation = None
                self._tm_activation_outcome = None
                self._tm_activation_worker_error = None
            try:
                config = self.repository.get(resource_id)
            except ResourceError as error:
                raise EditorControllerError(
                    "TM activation resource is unavailable"
                ) from error
            if config.kind is not ResourceKind.TRANSLATION_MEMORY:
                raise EditorControllerError(
                    "TM activation requires a translation memory resource"
                )
            service = _initial_tm_activation_service(config)
            try:
                preflight = service.preflight(config.path)
            except MigrationPreflightError as error:
                raise EditorControllerError(error.error_code) from error
            preflight.__post_init__()
            view = TMActivationPreflightView(
                resource_id=config.id,
                resource_name=config.name,
                valid_count=preflight.valid_count,
                invalid_count=preflight.invalid_count,
                variant_count=preflight.variant_count,
            )
            private_view = replace(view)
            prepared = _PreparedTMActivation(
                config=replace(config),
                service=service,
                preflight=preflight,
                view=private_view,
            )
            prepared.__post_init__()
            self._prepared_tm_activation = prepared
            return replace(view)

    def cancel_tm_activation(
        self,
        preflight: TMActivationPreflightView,
    ) -> None:
        """Revoke one issued preflight before the Core transaction starts."""

        if type(preflight) is not TMActivationPreflightView:
            raise EditorControllerError("TM activation preflight is required")
        candidate = replace(preflight)
        candidate.__post_init__()
        with self._tm_activation_condition:
            if self._tm_activation_operation is not None:
                raise EditorControllerError(
                    "TM activation cannot be cancelled after it starts"
                )
            prepared = self._prepared_tm_activation
            if prepared is None or not _activation_view_equal(
                candidate,
                prepared.view,
            ):
                raise EditorControllerError(
                    "TM activation preflight is stale or was not issued"
                )
            self._prepared_tm_activation = None

    def activate_tm_resource(
        self,
        preflight: TMActivationPreflightView,
    ) -> TMActivationOperationView:
        """Start the Core-owned first activation in one background worker."""

        return self._start_initial_tm_activation(preflight)

    def rebuild_tm_resource(
        self,
        resource_id: str,
    ) -> TMActivationOperationView:
        """Start one explicitly confirmed Core-owned canonical rebuild."""

        if type(resource_id) is not str or not resource_id.strip():
            raise EditorControllerError("TM rebuild resource id is required")
        with self._tm_activation_condition:
            current_operation = self._tm_activation_operation
            if current_operation is not None:
                if not current_operation.completed:
                    raise EditorControllerError(
                        "a TM activation is already in progress"
                    )
                self._tm_activation_operation = None
                self._tm_activation_outcome = None
                self._tm_activation_worker_error = None
            try:
                config = self.repository.get(resource_id)
            except ResourceError as error:
                raise EditorControllerError(
                    "TM rebuild resource is unavailable"
                ) from error
            if config.kind is not ResourceKind.TRANSLATION_MEMORY:
                raise EditorControllerError(
                    "TM rebuild requires a translation memory resource"
                )
            try:
                service = _tm_rebuild_service(config)
            except (OSError, UnicodeError, ValueError) as error:
                raise EditorControllerError(
                    "TM rebuild canonical resource is unavailable"
                ) from error
            operation = TMActivationOperationView(
                operation_id=uuid4().hex,
                resource_id=config.id,
                phase="ACTIVATING",
                completed=False,
                succeeded=False,
                safe_code=None,
                retryable=False,
            )
            private_operation = replace(operation)
            self._tm_activation_operation = private_operation
            self._tm_activation_outcome = None
            self._tm_activation_worker_error = None
            self._prepared_tm_activation = None
            worker = Thread(
                target=self._run_tm_activation,
                kwargs={
                    "operation_id": private_operation.operation_id,
                    "service": service,
                    "source": config.path,
                    "resource_id": config.id,
                    "action": "REBUILD",
                },
                name=f"localcat-tm-rebuild-{operation.operation_id[:8]}",
                daemon=True,
            )
            try:
                worker.start()
            except BaseException:
                self._tm_activation_operation = None
                raise
            return replace(operation)

    def _start_initial_tm_activation(
        self,
        preflight: TMActivationPreflightView,
    ) -> TMActivationOperationView:
        """Start one initial activation bound by Controller preflight."""

        if type(preflight) is not TMActivationPreflightView:
            raise EditorControllerError("TM activation preflight is required")
        candidate = replace(preflight)
        candidate.__post_init__()
        with self._tm_activation_condition:
            if self._tm_activation_operation is not None:
                raise EditorControllerError("a TM activation is already in progress")
            prepared = self._prepared_tm_activation
            if prepared is None or not _activation_view_equal(
                candidate,
                prepared.view,
            ):
                raise EditorControllerError(
                    "TM activation preflight is stale or was not issued"
                )
            try:
                current_config = self.repository.get(candidate.resource_id)
            except ResourceError as error:
                self._prepared_tm_activation = None
                raise EditorControllerError(
                    "TM activation resource is unavailable"
                ) from error
            current_config.__post_init__()
            expected_config = prepared.config
            if not (
                current_config.id == expected_config.id
                and current_config.name == expected_config.name
                and current_config.kind is expected_config.kind
                and current_config.path == expected_config.path
                and current_config.active is expected_config.active
                and current_config.lookup is expected_config.lookup
                and current_config.update is expected_config.update
            ):
                self._prepared_tm_activation = None
                raise EditorControllerError(
                    "TM activation resource changed after preflight"
                )
            try:
                current_preflight = prepared.service.preflight(
                    current_config.path
                )
            except MigrationPreflightError as error:
                self._prepared_tm_activation = None
                raise EditorControllerError(error.error_code) from error
            if not _activation_preflight_equal(
                current_preflight,
                prepared.preflight,
            ):
                self._prepared_tm_activation = None
                raise EditorControllerError(
                    "TM activation source changed after preflight"
                )

            operation = TMActivationOperationView(
                operation_id=uuid4().hex,
                resource_id=current_config.id,
                phase="ACTIVATING",
                completed=False,
                succeeded=False,
                safe_code=None,
                retryable=False,
            )
            private_operation = replace(operation)
            self._tm_activation_operation = private_operation
            self._tm_activation_outcome = None
            self._tm_activation_worker_error = None
            self._prepared_tm_activation = None
            worker = Thread(
                target=self._run_tm_activation,
                kwargs={
                    "operation_id": private_operation.operation_id,
                    "service": prepared.service,
                    "source": current_config.path,
                    "resource_id": current_config.id,
                    "action": "INITIAL",
                },
                name=f"localcat-tm-activation-{operation.operation_id[:8]}",
                daemon=True,
            )
            try:
                worker.start()
            except BaseException:
                self._tm_activation_operation = None
                self._prepared_tm_activation = prepared
                raise
            return replace(operation)

    def _run_tm_activation(
        self,
        *,
        operation_id: str,
        service: TMMigrationService,
        source: Path,
        resource_id: str,
        action: str,
    ) -> None:
        """Execute Core activation and publish only a body-free completion."""

        outcome: MigrationReport | MigrationFailure | None = None
        worker_error: BaseException | None = None
        succeeded = False
        safe_code: str | None = None
        retryable = False
        service_canonical_store_id: str | None = None
        try:
            if action == "INITIAL":
                candidate = service.activate_initial(source, resource_id)
            elif action == "REBUILD":
                candidate = service.rebuild_from_snapshot(source, resource_id)
            else:
                raise ValueError("TM operation action is unsupported")
            if type(candidate) is MigrationReport:
                candidate.__post_init__()
                outcome = candidate
                succeeded = True
            elif type(candidate) is MigrationFailure:
                candidate.__post_init__()
                outcome = candidate
                safe_code = candidate.error_code
                retryable = candidate.retryable
            else:
                raise TypeError(
                    "TM activation service returned an unsupported outcome"
                )
            service_canonical_store_id = service.canonical_store_id
            if (
                type(service_canonical_store_id) is not str
                or not service_canonical_store_id.strip()
            ):
                raise TypeError(
                    "TM activation service canonical store id is invalid"
                )
        except MigrationPreflightError as error:
            safe_code = error.error_code
        except (OSError, UnicodeError):
            safe_code = "TM.ACTIVATION.IO_FAILED"
            retryable = True
        except BaseException as error:
            outcome = None
            succeeded = False
            worker_error = error
            safe_code = "TM.ACTIVATION.PROGRAMMER_ERROR"

        if outcome is not None and service_canonical_store_id is not None:
            try:
                self._refresh_runtime_for_activation_outcome(
                    resource_id=resource_id,
                    outcome=outcome,
                    service_canonical_store_id=service_canonical_store_id,
                )
            except (ValueError, OSError, UnicodeError):
                succeeded = False
                safe_code = "TM.ACTIVATION.RUNTIME_REFRESH_FAILED"
                retryable = True
            except BaseException as error:
                succeeded = False
                safe_code = "TM.ACTIVATION.PROGRAMMER_ERROR"
                retryable = False
                worker_error = error

        completed = TMActivationOperationView(
            operation_id=operation_id,
            resource_id=resource_id,
            phase="COMPLETED",
            completed=True,
            succeeded=succeeded,
            safe_code=(None if succeeded else safe_code),
            retryable=(False if succeeded else retryable),
        )
        with self._tm_activation_condition:
            current = self._tm_activation_operation
            if current is None or current.operation_id != operation_id:
                return
            self._tm_activation_outcome = outcome
            self._tm_activation_worker_error = worker_error
            self._tm_activation_operation = completed
            self._tm_activation_condition.notify_all()

    def _refresh_runtime_for_activation_outcome(
        self,
        *,
        resource_id: str,
        outcome: MigrationReport | MigrationFailure,
        service_canonical_store_id: str,
    ) -> None:
        """Prevalidate and atomically publish one Core-outcome runtime graph."""

        if type(resource_id) is not str or not resource_id.strip():
            raise TypeError("activation completion resource id is required")
        if type(outcome) not in (MigrationReport, MigrationFailure):
            raise TypeError("activation completion outcome is unsupported")
        if (
            type(service_canonical_store_id) is not str
            or not service_canonical_store_id.strip()
        ):
            raise TypeError("activation service canonical store id is required")
        outcome.__post_init__()
        adapter = self._tm_adapter
        with self._tm_query_lock:
            try:
                configs = self.repository.list_resources()
                tm_engines: dict[str, TMEngine] | None = None

                def validate_candidate(snapshot: object) -> None:
                    from tm_application_composition import TMRuntimeSnapshot

                    nonlocal tm_engines
                    if type(snapshot) is not TMRuntimeSnapshot:
                        raise TypeError(
                            "activation runtime candidate must be TMRuntimeSnapshot"
                        )
                    _validate_activation_runtime_candidate(
                        snapshot,
                        resource_id=resource_id,
                        outcome=outcome,
                        service_canonical_store_id=service_canonical_store_id,
                    )
                    tm_engines = self._build_tm_engine_set_for_runtime_snapshot(
                        configs,
                        snapshot,
                    )
                    target_status = next(
                        status
                        for status in snapshot.statuses
                        if status.resource_id == resource_id
                    )
                    target_engine = tm_engines.get(resource_id)
                    if target_status.mode is TMResourceDisplayMode.UNAVAILABLE:
                        if target_engine is not None:
                            raise ValueError(
                                "unavailable activation retained compatibility authority"
                            )
                    else:
                        if target_engine is None:
                            raise ValueError(
                                "activation compatibility engine is missing"
                            )
                        _validate_activation_compatibility_engine(
                            target_engine,
                            outcome,
                            service_canonical_store_id=(
                                service_canonical_store_id
                            ),
                        )

                if adapter is None:
                    target_config = next(
                        (
                            config
                            for config in configs
                            if config.id == resource_id
                            and config.kind
                            is ResourceKind.TRANSLATION_MEMORY
                            and config.active
                        ),
                        None,
                    )
                    if target_config is None:
                        raise ValueError(
                            "activation completion resource is unavailable"
                        )
                    target_engine = self._load_tm_engine(target_config.path)
                    _validate_activation_compatibility_engine(
                        target_engine,
                        outcome,
                        service_canonical_store_id=(
                            service_canonical_store_id
                        ),
                    )
                    tm_engines = {
                        config.id: (
                            target_engine
                            if config.id == resource_id
                            else self._load_tm_engine(config.path)
                        )
                        for config in configs
                        if config.active
                        and config.kind
                        is ResourceKind.TRANSLATION_MEMORY
                    }
                else:
                    _ = adapter._refresh_runtime_after_activation(
                        configs,
                        validate_candidate,
                    )
                if tm_engines is None:
                    raise ValueError("TM runtime compatibility candidate is missing")
                self._tm_engines = tm_engines
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
                if adapter is not None and self._project is not None:
                    _ = self._query_and_issue_current_tm_report()
                self._tm_runtime_blocked_safe_code = None
            except BaseException:
                if _activation_outcome_requires_query_block(outcome):
                    if self._tm_runtime_blocked_safe_code is None:
                        self._advance_tm_query_epoch()
                    self._tm_runtime_blocked_safe_code = (
                        "TM.ACTIVATION.RUNTIME_REFRESH_FAILED"
                    )
                    self._observed_tm_signature = None
                raise

    def tm_activation_operation(self) -> TMActivationOperationView | None:
        """Return the current body-free activation lifecycle snapshot."""

        with self._tm_activation_condition:
            operation = self._tm_activation_operation
            if operation is None:
                return None
            operation.__post_init__()
            return replace(operation)

    def wait_tm_activation(
        self,
        operation_id: str,
        *,
        timeout: float | None = None,
    ) -> TMActivationOperationView:
        """Wait for one known activation and surface programmer failures."""

        if type(operation_id) is not str or not operation_id:
            raise EditorControllerError("TM activation operation id is required")
        if timeout is not None and (
            type(timeout) is not float
            or not math.isfinite(timeout)
            or timeout < 0.0
        ):
            raise TypeError("TM activation timeout must be finite non-negative float")
        with self._tm_activation_condition:
            current = self._tm_activation_operation
            if current is None or current.operation_id != operation_id:
                raise EditorControllerError("TM activation operation is unknown")
            _ = self._tm_activation_condition.wait_for(
                lambda: (
                    self._tm_activation_operation is not None
                    and self._tm_activation_operation.operation_id == operation_id
                    and self._tm_activation_operation.completed
                ),
                timeout=timeout,
            )
            current = self._tm_activation_operation
            if current is None or current.operation_id != operation_id:
                raise EditorControllerError("TM activation operation is unknown")
            result = replace(current)
            worker_error = (
                self._tm_activation_worker_error if result.completed else None
            )
        if worker_error is not None:
            raise worker_error
        return result

    def _advance_tm_query_epoch(self) -> None:
        """Invalidate every issued suggestion before advancing one epoch."""

        self._issued_tm_suggestions = ()
        self._issued_legacy_tm_suggestions = ()
        self._legacy_issued_context = None
        self._current_tm_report = None
        self._tm_query_epoch += 1

    def _tm_signature(
        self,
        *,
        segment: EditorSegment,
        preferences: TMPreferences,
        runtime_generation: int,
        retrieval_generation: int,
    ) -> tuple[str, str, str, int, int, float, int, str]:
        return (
            self._project_session_id,
            segment.id,
            hashlib.sha256(segment.source.encode("utf-8")).hexdigest(),
            runtime_generation,
            retrieval_generation,
            preferences.minimum_similarity,
            preferences.result_limit,
            hashlib.sha256(
                (segment.speaker or "").encode("utf-8")
            ).hexdigest(),
        )

    def _capture_current_tm_signature(
        self,
    ) -> tuple[str, str, str, int, int, float, int, str]:
        adapter = self._tm_adapter
        if adapter is None:
            raise ValueError("TM query adapter is not configured")
        segment = self.current_segment
        preferences = self.workspace_state.tm_preferences()
        runtime_generation, retrieval_generation = (
            adapter._current_query_generations()
        )
        return self._tm_signature(
            segment=segment,
            preferences=preferences,
            runtime_generation=runtime_generation,
            retrieval_generation=retrieval_generation,
        )

    def _record_current_tm_baseline(self) -> None:
        if self._tm_adapter is None or self._project is None:
            self._observed_tm_signature = None
            return
        try:
            self._observed_tm_signature = self._capture_current_tm_signature()
        except ValueError:
            self._observed_tm_signature = None

    def _synchronize_tm_query_state(
        self,
        *,
        refresh_current: bool = True,
    ) -> None:
        """Observe resource, capability, source, and preference changes."""

        if self._tm_adapter is None or self._project is None:
            return
        if self._tm_runtime_blocked_safe_code is not None:
            return
        try:
            current = self._capture_current_tm_signature()
        except ValueError:
            if self._observed_tm_signature is not None:
                self._advance_tm_query_epoch()
                self._observed_tm_signature = None
            return
        refresh_required = self._current_tm_report is None
        if self._observed_tm_signature is None:
            self._observed_tm_signature = current
        elif current != self._observed_tm_signature:
            self._advance_tm_query_epoch()
            self._observed_tm_signature = current
            refresh_required = True
        if refresh_current and refresh_required:
            _ = self._query_and_issue_current_tm_report()

    def _require_tm_runtime_available(self) -> None:
        """Block stale TM use after a published/ambiguous refresh failure."""

        safe_code = self._tm_runtime_blocked_safe_code
        if safe_code is not None:
            raise EditorControllerError(safe_code)

    def _latch_persisted_runtime_refresh_failure(self) -> None:
        """Invalidate stale runtime authority after repository facts changed."""

        with self._tm_query_lock:
            if self._tm_adapter is None:
                return
            if self._tm_runtime_blocked_safe_code is None:
                self._advance_tm_query_epoch()
            self._tm_runtime_blocked_safe_code = "TM.RUNTIME.REFRESH_FAILED"
            self._observed_tm_signature = None

    def _reload_resources_after_persisted_mutation(self) -> None:
        """Refresh a persisted mutation or fail closed without laundering errors."""

        try:
            self.reload_resources()
        except EditorControllerError as error:
            self._latch_persisted_runtime_refresh_failure()
            raise EditorControllerError("TM.RUNTIME.REFRESH_FAILED") from error
        except BaseException:
            self._latch_persisted_runtime_refresh_failure()
            raise

    def _apply_tm_target_if_generations_current(
        self,
        target: str,
    ) -> EditorProject:
        """Commit a target while the issued runtime/retrieval pair is current."""

        adapter = self._tm_adapter
        if adapter is None:
            return self.update_target(target)
        signature = self._observed_tm_signature
        if signature is None:
            raise EditorControllerError(
                "TM suggestion is stale for the current runtime"
            )
        runtime_generation = signature[3]
        retrieval_generation = signature[4]
        try:
            return adapter._run_if_query_generations_current(
                runtime_generation=runtime_generation,
                retrieval_generation=retrieval_generation,
                operation=lambda: self.update_target(target),
            )
        except _TMQueryGenerationChanged as error:
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
            raise EditorControllerError(
                "TM suggestion is stale for the current runtime"
            ) from error

    def _remember_current_position(self) -> None:
        if self._project is None or self._project.path is None:
            return
        segment = self._project.segments[self._current_index]
        try:
            self.workspace_state.remember_project(
                self._project.path,
                segment.id,
                self._current_index,
            )
        except WorkspaceStateError as exc:
            LOGGER.warning("Unable to remember project position: %s", exc)

    def apply_tm_suggestion(
        self,
        suggestion: TMSuggestion | LegacyExactTMSuggestion,
    ) -> EditorProject:
        """Apply one typed TM suggestion without confirming the segment."""

        if type(suggestion) is LegacyExactTMSuggestion:
            with self._tm_query_lock:
                self._require_tm_runtime_available()
                self._synchronize_tm_query_state()
                try:
                    candidate = replace(suggestion)
                    candidate.__post_init__()
                    issued = tuple(
                        replace(item)
                        for item in self._issued_legacy_tm_suggestions
                    )
                    for item in issued:
                        item.__post_init__()
                except ValueError as error:
                    raise EditorControllerError(
                        "legacy TM suggestion contract is invalid or tampered"
                    ) from error
                segment = self.current_segment
                current_context = (
                    self._project_session_id,
                    segment.id,
                    hashlib.sha256(segment.source.encode("utf-8")).hexdigest(),
                    hashlib.sha256(
                        (segment.speaker or "").encode("utf-8")
                    ).hexdigest(),
                    self._tm_query_epoch,
                )
                if self._legacy_issued_context != current_context:
                    raise EditorControllerError(
                        "legacy TM suggestion is stale for the current segment"
                    )
                if not any(
                    candidate.source == member.source
                    and candidate.target == member.target
                    and candidate.resource_id == member.resource_id
                    and candidate.resource_name == member.resource_name
                    and candidate.similarity == member.similarity
                    and candidate.match_type == member.match_type
                    for member in issued
                ):
                    raise EditorControllerError(
                        "legacy TM suggestion was not issued for the current query"
                    )
                if candidate.source != segment.source:
                    raise EditorControllerError(
                        "TM suggestion does not belong to the current segment"
                    )
                return self._apply_tm_target_if_generations_current(
                    candidate.target
                )
        if type(suggestion) is not TMSuggestion:
            raise EditorControllerError("a TM suggestion contract is required")

        with self._tm_query_lock:
            self._require_tm_runtime_available()
            self._synchronize_tm_query_state()
            try:
                candidate = _clone_tm_suggestion(suggestion)
                issued = tuple(
                    _clone_tm_suggestion(item)
                    for item in self._issued_tm_suggestions
                )
            except ValueError as error:
                raise EditorControllerError(
                    "TM suggestion contract is invalid or was tampered with"
                ) from error

            segment = self.current_segment
            identity = candidate.query_identity
            source_digest = hashlib.sha256(
                segment.source.encode("utf-8")
            ).hexdigest()
            if (
                identity.project_session_id != self._project_session_id
                or identity.segment_id != segment.id
                or identity.source_digest != source_digest
                or identity.query_epoch != self._tm_query_epoch
            ):
                raise EditorControllerError(
                    "TM suggestion is stale for the current segment"
                )
            if not any(
                _tm_suggestion_fields_equal(candidate, member)
                for member in issued
            ):
                raise EditorControllerError(
                    "TM suggestion was not issued for the current query"
                )
            return self._apply_tm_target_if_generations_current(
                candidate.target
            )

    def insert_term_suggestion(
        self,
        suggestion: TermSuggestion,
        position: int | None = None,
    ) -> EditorProject:
        """Insert one term target at an editor cursor position without confirmation."""

        if not isinstance(suggestion, TermSuggestion):
            raise EditorControllerError("a term suggestion contract is required")
        target = self.current_segment.target
        insertion_point = len(target) if position is None else position
        if insertion_point < 0 or insertion_point > len(target):
            raise EditorControllerError("term insertion position is outside the target text")
        updated = target[:insertion_point] + suggestion.target_term + target[insertion_point:]
        return self.update_target(updated)

    def add_term(self, source: str, target: str) -> ResourceConfig:
        """Persist a term in the first active Update termbase and reload it."""

        if not source.strip() or not target.strip():
            raise EditorControllerError("源术语和目标术语均不能为空。")
        resource = next(
            (
                configured
                for configured in self.repository.list_resources()
                if configured.kind is ResourceKind.TERMBASE
                and configured.active
                and configured.update
            ),
            None,
        )
        if resource is None:
            raise EditorControllerError(
                "没有可写术语表。请打开“语言资源设置”，"
                "将至少一个术语表设为 Active，并开启 Update。"
            )
        report = upsert_term(resource.path, source, target)
        if not report.succeeded:
            raise EditorControllerError("; ".join(report.errors))
        self._glossary_engines[resource.id] = self._load_glossary_engine(resource.path)
        return resource

    def list_resources(self) -> tuple[ResourceConfig, ...]:
        """Return the current persistent resource configuration."""

        return self.repository.list_resources()

    def create_resource(self, name: str, kind: ResourceKind | str) -> ResourceConfig:
        """Create a managed resource and make it available immediately."""

        with self._tm_query_lock:
            try:
                resource = self.repository.create_resource(name, kind)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            self._reload_resources_after_persisted_mutation()
            return resource

    def update_resource(self, resource: ResourceConfig) -> ResourceConfig:
        """Persist resource state through the repository and rebuild engine sets."""

        with self._tm_query_lock:
            try:
                updated = self.repository.update_resource(resource)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            self._reload_resources_after_persisted_mutation()
            return updated

    def delete_resource(self, resource_id: str) -> ResourceConfig:
        """Delete one configured resource and remove it from live engine sets."""

        with self._tm_query_lock:
            try:
                deleted = self.repository.delete_resource(resource_id)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            self._tm_engines.pop(resource_id, None)
            self._glossary_engines.pop(resource_id, None)
            try:
                self._reload_resources_after_persisted_mutation()
            except EditorControllerError as exc:
                LOGGER.warning(
                    "Resource %s was deleted; remaining resources kept their last "
                    "known-good engines because reload failed: %s",
                    resource_id,
                    exc,
                )
            return deleted

    def import_resource(self, request: ImportRequest) -> ImportReport:
        """Import into one configured resource and hot reload on any written result."""

        with self._tm_query_lock:
            try:
                resource = self.repository.get(request.resource_id)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                report = import_tmx(
                    request.input_path,
                    resource.path,
                    request.source_locale,
                    request.target_locale,
                )
            else:
                report = import_termbase(request.input_path, resource.path)
            if report.imported:
                try:
                    self._reload_resources_after_persisted_mutation()
                except EditorControllerError as exc:
                    return ImportReport(
                        imported=report.imported,
                        skipped=report.skipped,
                        overwritten=report.overwritten,
                        errors=(
                            *report.errors,
                            f"resource reload failed: {exc}",
                        ),
                    )
            return report

    def reload_resources(self, *, _refresh_runtime: bool = True) -> None:
        """Build a complete active engine set before replacing the last known-good set."""

        if type(_refresh_runtime) is not bool:
            raise TypeError("resource runtime refresh flag must be exact bool")
        configs = self.repository.list_resources()
        try:
            with self._tm_query_lock:
                adapter = self._tm_adapter
                if adapter is None:
                    tm_engines, glossary_engines = self._build_resource_engine_sets(
                        configs
                    )
                elif not _refresh_runtime:
                    if configs:
                        initial_runtime = adapter._capture_runtime_for_controller(
                            configs
                        )
                        tm_engines, glossary_engines = (
                            self._build_resource_engine_sets(
                                configs,
                                runtime_snapshot=initial_runtime,
                            )
                        )
                    else:
                        tm_engines, glossary_engines = (
                            self._build_resource_engine_sets(configs)
                        )
                else:
                    engine_candidate: tuple[
                        dict[str, TMEngine],
                        dict[str, GlossaryEngine],
                    ] | None = None

                    def validate_candidate(snapshot: object) -> None:
                        nonlocal engine_candidate
                        engine_candidate = self._build_resource_engine_sets(
                            configs,
                            runtime_snapshot=snapshot,
                        )

                    _ = adapter._refresh_runtime(configs, validate_candidate)
                    if engine_candidate is None:
                        raise ValueError(
                            "resource engine refresh candidate is missing"
                        )
                    tm_engines, glossary_engines = engine_candidate

                self._tm_engines = tm_engines
                self._glossary_engines = glossary_engines
                if self._project is not None:
                    self._advance_tm_query_epoch()
                    self._record_current_tm_baseline()
                    if adapter is not None:
                        _ = self._query_and_issue_current_tm_report()
                self._tm_runtime_blocked_safe_code = None
        except (OSError, UnicodeError, ValueError, csv.Error, json.JSONDecodeError) as exc:
            raise EditorControllerError(f"unable to reload language resources: {exc}") from exc

    def _build_resource_engine_sets(
        self,
        configs: tuple[ResourceConfig, ...],
        *,
        runtime_snapshot: object | None = None,
    ) -> tuple[dict[str, TMEngine], dict[str, GlossaryEngine]]:
        """Build compatibility engines against one validated runtime cohort."""

        tm_engines = (
            {}
            if runtime_snapshot is None
            else self._build_tm_engine_set_for_runtime_snapshot(
                configs,
                runtime_snapshot,
            )
        )
        glossary_engines: dict[str, GlossaryEngine] = {}
        for resource in configs:
            if not resource.active:
                continue
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                if runtime_snapshot is None:
                    tm_engines[resource.id] = self._load_tm_engine(
                        resource.path
                    )
            else:
                glossary_engines[resource.id] = self._load_glossary_engine(
                    resource.path
                )
        return tm_engines, glossary_engines

    def _build_tm_engine_set_for_runtime_snapshot(
        self,
        configs: tuple[ResourceConfig, ...],
        runtime_snapshot: object,
    ) -> dict[str, TMEngine]:
        """Build only TM compatibility engines before activation publication."""

        from tm_application_composition import TMRuntimeSnapshot

        if type(runtime_snapshot) is not TMRuntimeSnapshot:
            raise TypeError("resource runtime candidate must be TMRuntimeSnapshot")
        runtime_snapshot.__post_init__()
        legacy_ids = {
            port.resource_id for port in runtime_snapshot.legacy_ports
        }
        canonical_ids = {
            port.resource_id for port in runtime_snapshot.canonical_ports
        }
        tm_engines: dict[str, TMEngine] = {}
        for resource in configs:
            if (
                resource.kind is not ResourceKind.TRANSLATION_MEMORY
                or not resource.active
                or resource.id not in legacy_ids | canonical_ids
            ):
                continue
            engine = self._load_tm_engine(resource.path)
            if resource.id in legacy_ids:
                if engine.canonical_store is not None:
                    raise ValueError(
                        "legacy compatibility engine became canonical"
                    )
            elif engine.canonical_store is None:
                raise ValueError(
                    "canonical compatibility engine lost canonical authority"
                )
            tm_engines[resource.id] = engine
        return tm_engines

    @staticmethod
    def _load_tm_engine(path: Path) -> TMEngine:
        if not path.exists() or not path.is_file():
            raise ValueError(f"translation memory does not exist: {path}")
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"translation memory line {line_number} must be an object")
            source = record.get("source")
            target = record.get("target")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"translation memory line {line_number} has no source")
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"translation memory line {line_number} has no target")
        return TMEngine(str(path))

    @staticmethod
    def _load_glossary_engine(path: Path) -> GlossaryEngine:
        if not path.exists() or not path.is_file():
            raise ValueError(f"termbase does not exist: {path}")
        terms: dict[str, str] = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for line_number, row in enumerate(csv.reader(handle), start=1):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) < 2:
                    raise ValueError(f"termbase row {line_number} has fewer than two columns")
                source = row[0].strip()
                target = row[1].strip()
                if source.casefold() == "source" and target.casefold() == "target":
                    continue
                if not source or not target:
                    raise ValueError(f"termbase row {line_number} has an empty term")
                terms[source] = target
        engine = GlossaryEngine()
        for source, target in terms.items():
            engine.add_term(source, target, path.name)
        return engine


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        controller = EditorController(ResourceRepository(Path(temp_dir)))
        controller.load_sample()
        assert controller.current_segment.source
    print("Editor controller self-test passed.")
