"""Qt-free editing session and language-resource coordination."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from time import monotonic
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, RLock, Thread
from uuid import uuid4

from capability_host import MatcherHandoffSnapshot
from configured_term_adapter import ConfiguredTermAdapter
from project_codec_settings import ProjectCodecRuntime, compose_project_codec_runtime
from editor_file_jobs import (ControllerFileJob, ControllerFileOutcome, FileOpenCandidate,
                              FileImportPublication, SingleTLImportReview,
                              FileExportPreparation, FileExportPublication)
from rpy_project_adapter import RpyProjectAdapter, RpyProjectSession
from editor_directory_open import DirectoryOpenCandidate
from editor_source_update import SourceUpdateCandidate, source_update_review

from project_export_contracts import ProjectExportDiagnostic, ProjectExportResult
from rpy_project_export import RpyExportContext
from editor_contracts import (
    BatchOperationReport,
    BatchUndoState,
    ConfirmResult,
    DisplayPreferences,
    EditorProject,
    EditorSegment,
    FuzzyValidationDisplay,
    TMThresholdDisplay,
    FuzzyValidationState,
    ImportReport,
    ImportRequest,
    LiteralReplaceRule,
    PreprocessChange,
    PreprocessPreferences,
    PreprocessPreview,
    ProjectSearchHit,
    ProjectSearchReport,
    ProjectSearchRequest,
    ProjectToolCapability,
    SearchField,
    SearchScope,
    SegmentTranslationStatus,
    SpeakerInventory,
    ResourceConfig,
    ResourceKind,
    RecentProject,
    RecentWorkspaceProject,
    RetrievalDisplayState,
    LegacyExactTMSuggestion,
    SuggestionBundle,
    TMPreferences,
    TMActivationOperationView,
    TMActivationPreflightView,
    TMResourceDisplayMode,
    TMResourceStatus,
    TMSuggestion,
    TMSuggestionReport,
    TMThresholdUpdateOutcome,
    PreparedTermMutation,
    TermCommitOutcome,
    TermCommitState,
    TermDraft,
    TermbaseImportPreview,
    TermRecord,
    TermRecordLocator,
    TermSuggestion,
    TextMatcherDisplayState,
    WriteReport,
    WorkspaceSearchHit,
    WorkspaceSearchReport,
    WorkspaceSearchRequest,
)
from editor_tm_adapter import (
    EditorTMAdapter,
    _TMMatcherGenerationChanged,
    _TMQueryGenerationChanged,
)
from editor_project import (
    ProjectError,
    load_project,
    sample_project,
    save_project as save_project_file,
)
from glossary_engine import GlossaryEngine
from resource_importer import (
    ImportFailure,
    import_tmx,
    preview_termbase_import as preview_termbase_import_file,
    read_legacy_termbase_import,
)
from resource_repository import ResourceError, ResourceRepository
from resource_package_contracts import (
    PortableResourceKind,
    ResourceExportOutcome,
    ResourceImportMode,
    ResourcePackageImportPreview,
    ResourcePackageImportResult,
    ResourcePackageValidationReport,
    ResourcePortabilityError,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
    ResourceRecoveryOutcome,
    ResourceRecoveryPreview,
)
from resource_portability import ResourcePortabilityService
from renpy_tm_compat import build_dialogue_alias, unwrap_dialogue_target
from project_search import (
    ProjectSearchError,
    ProjectSearchService,
    segment_translation_status,
)
from speaker_inventory import build_speaker_inventory
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationPreflight,
    MigrationReport,
    SearchOptions,
    StoreHealth,
    TextMatcherState,
)
from tm_engine import SourceUnit, TMEngine, canonical_authority_facts
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator
from termbase_store import TermbaseStore, TermbaseValidationError
from target_preprocessor import PreprocessValidationError, preview_preprocessing
from workspace_state import WorkspaceStateError, WorkspaceStateRepository
from project_package import (
    OpenedProjectPackage,
    ProjectPackageExportReceipt,
    ProjectPackagePersistenceBinding,
    ProjectPackageImportPreview,
    ProjectPackageImportReceipt,
    ProjectPackageImportMode,
    ProjectPackageService,
)
from project_save import ProjectSaveReport, ProjectSaveService
from project_workspace import (
    DocumentProgress,
    FlatProjectSegment,
    IssuedDocumentIdentity,
    IssuedProjectIdentity,
    IssuedSegmentIdentity,
    ProjectProgress,
    ProjectWorkspaceService,
    ReconciliationAssociation,
    ReconciliationDecision,
    ReconciliationDisposition,
    ReconciliationPreview,
    ReconciliationReceipt,
    WorkspaceDocumentView,
    WorkspaceSaveState,
    WorkspaceSegmentView,
    WorkspaceSessionView,
)
from project_workspace_contracts import (
    ProjectSegment,
    ProjectWorkspace,
    SegmentIdentity,
    StagedSelectedProjectDocuments,
)
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    OriginRenameMapping,
    SelectedProjectDocumentsRequest,
    revalidate_staged_selected_documents,
    stage_selected_project_documents,
    stage_workspace_rebind,
)


LOGGER = logging.getLogger(__name__)


_BASIC_PROJECT_SEARCH_OPTIONS = SearchOptions(
    match_case=False,
    whole_word=False,
)

_TermEngine = GlossaryEngine | ConfiguredTermAdapter
_PreparedTermOperation = Callable[[Path], PreparedTermMutation]


class EditorControllerError(RuntimeError):
    """Raised when an editor operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceSaveState:
    """Current package baseline projection without carrier internals."""

    dirty_document_ids: tuple[str, ...]
    manifest_dirty: bool
    project_dirty: bool
    package_path: Path | None
    artifact_digest: str | None

    def __post_init__(self) -> None:
        if type(self.dirty_document_ids) is not tuple:
            raise TypeError("workspace dirty ids must be an exact tuple")
        if len(self.dirty_document_ids) != len(set(self.dirty_document_ids)):
            raise ValueError("workspace dirty ids must be unique")
        if type(self.manifest_dirty) is not bool or type(self.project_dirty) is not bool:
            raise TypeError("workspace dirty flags must be exact bool")
        if self.project_dirty != bool(self.dirty_document_ids or self.manifest_dirty):
            raise ValueError("workspace project dirty must close over document state")
        if self.package_path is None and self.artifact_digest is None:
            return
        if not isinstance(self.package_path, Path) or not self.package_path.is_absolute():
            raise TypeError("workspace save state requires absolute package path")
        if (
            type(self.artifact_digest) is not str
            or len(self.artifact_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.artifact_digest)
        ):
            raise ValueError("workspace save state requires artifact digest")


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceSaveResult:
    save_report: ProjectSaveReport
    receipt: ProjectPackageExportReceipt | None
    session: WorkspaceSessionView
    package_artifact_digest: str

    def __post_init__(self) -> None:
        if type(self.save_report) is not ProjectSaveReport:
            raise TypeError("workspace save result requires exact report")
        if self.receipt is not None and type(self.receipt) is not ProjectPackageExportReceipt:
            raise TypeError("workspace save receipt must be exact or None")
        if type(self.session) is not WorkspaceSessionView:
            raise TypeError("workspace save result requires issued session")
        if (
            type(self.package_artifact_digest) is not str
            or len(self.package_artifact_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.package_artifact_digest)
        ):
            raise ValueError("workspace save result requires artifact digest")
        if self.receipt is not None and self.receipt.artifact_digest != self.package_artifact_digest:
            raise ValueError("workspace save receipt artifact changed")


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceCreationResult:
    """One newly exported package and the issued session opened from it."""

    receipt: ProjectPackageExportReceipt
    session: WorkspaceSessionView

    def __post_init__(self) -> None:
        if type(self.receipt) is not ProjectPackageExportReceipt:
            raise TypeError("workspace creation requires exact package receipt")
        if type(self.session) is not WorkspaceSessionView:
            raise TypeError("workspace creation requires exact issued session")
        if self.receipt.project_id != self.session.project.project_id:
            raise ValueError("workspace creation project identity changed")


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceImportResult:
    receipt: ProjectPackageImportReceipt
    session: WorkspaceSessionView
    active_session_changed: bool

    def __post_init__(self) -> None:
        if type(self.receipt) is not ProjectPackageImportReceipt:
            raise TypeError("workspace import result requires exact receipt")
        if type(self.session) is not WorkspaceSessionView:
            raise TypeError("workspace import result requires issued session")
        if type(self.active_session_changed) is not bool:
            raise TypeError("workspace import swap flag must be exact bool")
        if self.active_session_changed:
            if self.session.project.project_id != self.receipt.project_id:
                raise ValueError("workspace import session project changed")


@dataclass(frozen=True, slots=True)
class ControllerWorkspaceConfirmResult:
    """Workspace confirmation outcome without flattening it into EditorProject."""

    session: WorkspaceSessionView
    current_identity: IssuedSegmentIdentity
    current_global_index: int
    write_report: WriteReport

    def __post_init__(self) -> None:
        if type(self.session) is not WorkspaceSessionView:
            raise TypeError("workspace confirmation requires issued session")
        if type(self.current_identity) is not IssuedSegmentIdentity:
            raise TypeError("workspace confirmation requires issued identity")
        if type(self.current_global_index) is not int or self.current_global_index < 0:
            raise TypeError("workspace confirmation index must be nonnegative")
        if type(self.write_report) is not WriteReport:
            raise TypeError("workspace confirmation requires exact write report")


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryView:
    """Body-free navigation group; its leaves retain the issued document view."""

    name: str
    children: tuple[WorkspaceDirectoryView | WorkspaceDocumentView, ...]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise TypeError("directory navigation requires a nonempty name")
        if type(self.children) is not tuple or not self.children or any(
            type(child) not in (WorkspaceDirectoryView, WorkspaceDocumentView)
            for child in self.children
        ):
            raise TypeError("directory navigation requires frozen children")


@dataclass(frozen=True, slots=True)
class _PreparedWorkspaceInstall:
    """Fully built Controller state awaiting one non-failing field swap."""

    service: ProjectWorkspaceService
    save_service: ProjectSaveService
    projection: tuple[FlatProjectSegment, ...]
    current_index: int
    generation: int
    session_id: str
    view: WorkspaceSessionView
    observed_tm_signature: tuple[str, str, str, int, int, float, int, str] | None


@dataclass(frozen=True, slots=True)
class _PreparedTMActivation:
    """Controller-private Core authority retained after read-only preflight."""

    config: ResourceConfig
    service: TMMigrationService
    preflight: MigrationPreflight
    view: TMActivationPreflightView
    action: str = "INITIAL"

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
        if self.action not in {"INITIAL", "RECOVERY"}:
            raise TypeError("prepared activation action is unsupported")


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
    engine = TMEngine(
        str(config.path),
        expected_resource_id=config.id,
    )
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


def _tm_reattestation_service(config: ResourceConfig) -> TMMigrationService:
    """Construct an explicit owner for one refused canonical authority.

    This path consumes only durable identity facts.  It does not open the
    canonical store or grant query authority before the Core maintenance
    transition repeats its device-only proof under the persistent lock.
    """

    if type(config) is not ResourceConfig:
        raise TypeError("TM re-attestation config must be ResourceConfig")
    config.__post_init__()
    facts = canonical_authority_facts(config.path)
    if facts is None:
        raise EditorControllerError(
            "TM.RUNTIME.CANONICAL_REATTESTATION_NOT_APPLICABLE"
        )
    resource_id, canonical_store_id = facts
    if resource_id != config.id:
        raise EditorControllerError(
            "TM.RUNTIME.CANONICAL_REATTESTATION_IDENTITY_INVALID"
        )
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        config.id,
        config.path,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
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


def _clone_project_search_report(
    report: ProjectSearchReport,
) -> ProjectSearchReport:
    """Return a detached search graph without sharing issued hit objects."""

    if type(report) is not ProjectSearchReport:
        raise TypeError("project search report must be ProjectSearchReport")
    report.__post_init__()
    cloned = ProjectSearchReport(
        hits=tuple(replace(hit) for hit in report.hits),
        capability=replace(
            report.capability,
            supported_profiles=tuple(report.capability.supported_profiles),
        ),
    )
    cloned.__post_init__()
    return cloned


def _clone_project_search_request(
    request: ProjectSearchRequest,
) -> ProjectSearchRequest:
    """Validate and detach caller-owned search selection state."""

    if type(request) is not ProjectSearchRequest:
        raise TypeError("project search request must be ProjectSearchRequest")
    request.__post_init__()
    cloned = ProjectSearchRequest(
        query=request.query,
        fields=tuple(request.fields),
        options=SearchOptions(
            match_case=request.options.match_case,
            whole_word=request.options.whole_word,
        ),
        status=request.status,
    )
    cloned.__post_init__()
    return cloned


def _clone_workspace_search_request(
    request: WorkspaceSearchRequest,
) -> WorkspaceSearchRequest:
    if type(request) is not WorkspaceSearchRequest:
        raise TypeError("workspace search request must be exact")
    request.__post_init__()
    cloned = WorkspaceSearchRequest(
        query=request.query,
        fields=tuple(request.fields),
        options=SearchOptions(
            match_case=request.options.match_case,
            whole_word=request.options.whole_word,
        ),
        status=request.status,
        scope=request.scope,
    )
    cloned.__post_init__()
    return cloned


def _clone_workspace_search_report(
    report: WorkspaceSearchReport,
) -> WorkspaceSearchReport:
    if type(report) is not WorkspaceSearchReport:
        raise TypeError("workspace search report must be exact")
    report.__post_init__()
    cloned = WorkspaceSearchReport(
        hits=tuple(replace(hit) for hit in report.hits),
        capability=replace(
            report.capability,
            supported_profiles=tuple(report.capability.supported_profiles),
        ),
    )
    cloned.__post_init__()
    return cloned


def _clone_term_records(
    records: tuple[TermRecord, ...],
) -> tuple[TermRecord, ...]:
    """Validate and reconstruct a detached mixed-term snapshot graph."""

    if type(records) is not tuple:
        raise TypeError("private term records must be an exact tuple")
    cloned: list[TermRecord] = []
    for record in records:
        if type(record) is not TermRecord:
            raise TypeError("private term records must contain exact values")
        locator = record.locator
        if type(locator) is not TermRecordLocator:
            raise TypeError("private term locator must be an exact value")
        locator.__post_init__()
        record.__post_init__()
        cloned_locator = TermRecordLocator(
            row_kind=locator.row_kind,
            file_digest=locator.file_digest,
            row_ordinal=locator.row_ordinal,
            row_digest=locator.row_digest,
            record_id=locator.record_id,
        )
        cloned_record = TermRecord(
            locator=cloned_locator,
            record_id=record.record_id,
            source=record.source,
            target=record.target,
            policy=record.policy,
            match_case=record.match_case,
            whole_word=record.whole_word,
        )
        cloned_locator.__post_init__()
        cloned_record.__post_init__()
        cloned.append(cloned_record)
    return tuple(cloned)


_TERM_QUARANTINE_ERROR_CODES = frozenset(
    {"ROLLBACK_FAILED", "ROLLBACK_VERIFICATION_FAILED"}
)
_TERM_QUARANTINE_SAFE_DETAIL = (
    "Quarantine the resource and restore it from the recovery file "
    "before retrying."
)


def _clone_indeterminate_term_outcome(
    outcome: TermCommitOutcome,
) -> TermCommitOutcome:
    """Validate and detach one private quarantine recovery envelope."""

    if type(outcome) is not TermCommitOutcome:
        raise TypeError("private term quarantine must be TermCommitOutcome")
    outcome.__post_init__()
    if outcome.state is not TermCommitState.INDETERMINATE:
        raise ValueError("private term quarantine state drifted")
    if outcome.error_code not in _TERM_QUARANTINE_ERROR_CODES:
        raise ValueError("private term quarantine error code drifted")
    if outcome.safe_detail != _TERM_QUARANTINE_SAFE_DETAIL:
        raise ValueError("private term quarantine guidance drifted")
    cloned = TermCommitOutcome(
        state=outcome.state,
        report=None,
        error_code=outcome.error_code,
        retryable=outcome.retryable,
        recovery_path=outcome.recovery_path,
        quarantined=outcome.quarantined,
        safe_detail=outcome.safe_detail,
    )
    cloned.__post_init__()
    return cloned


def _project_search_hit_fields_equal(
    candidate: ProjectSearchHit,
    issued: ProjectSearchHit,
) -> bool:
    """Compare the complete frozen hit membership without dataclass equality."""

    return (
        candidate.segment_id == issued.segment_id
        and candidate.segment_index == issued.segment_index
        and candidate.field is issued.field
        and candidate.start_index == issued.start_index
        and candidate.end_index == issued.end_index
        and candidate.preview == issued.preview
    )


def _workspace_search_hit_fields_equal(
    candidate: WorkspaceSearchHit,
    issued: WorkspaceSearchHit,
) -> bool:
    return (
        candidate.document_id == issued.document_id
        and candidate.local_segment_id == issued.local_segment_id
        and candidate.project_global_index == issued.project_global_index
        and candidate.field is issued.field
        and candidate.start_index == issued.start_index
        and candidate.end_index == issued.end_index
        and candidate.preview == issued.preview
    )


def _project_search_fields_digest(project: EditorProject) -> str:
    """Bind one issued report to stable identities and searchable field values."""

    if type(project) is not EditorProject:
        raise TypeError("project search identity requires EditorProject")
    project.__post_init__()
    payload = tuple(
        (
            segment.id,
            segment.source,
            segment.target,
            segment.speaker,
            segment.confirmed,
        )
        for segment in project.segments
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_content_digest(project: EditorProject) -> str:
    """Digest canonical editable project content without logging raw text."""

    if type(project) is not EditorProject:
        raise TypeError("project content digest requires EditorProject")
    project.__post_init__()
    payload = (
        project.name,
        project.source_locale,
        project.target_locale,
        tuple(
            (
                segment.id,
                segment.source,
                segment.target,
                segment.speaker,
                segment.confirmed,
            )
            for segment in project.segments
        ),
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clone_preprocess_preview(preview: PreprocessPreview) -> PreprocessPreview:
    """Validate and detach one caller-owned preprocessing preview graph."""

    if type(preview) is not PreprocessPreview:
        raise TypeError("preprocess preview must be PreprocessPreview")
    preview.__post_init__()
    cloned = PreprocessPreview(
        project_session_id=preview.project_session_id,
        base_revision=preview.base_revision,
        changes=tuple(replace(change) for change in preview.changes),
    )
    cloned.__post_init__()
    return cloned


def _clone_preprocess_preferences(
    preferences: PreprocessPreferences,
) -> PreprocessPreferences:
    """Validate and detach one device-local preprocessing preference graph."""

    if type(preferences) is not PreprocessPreferences:
        raise TypeError("preprocess preferences must be PreprocessPreferences")
    preferences.__post_init__()
    cloned = PreprocessPreferences(
        rules=tuple(
            LiteralReplaceRule(
                find=rule.find,
                replacement=rule.replacement,
                enabled=rule.enabled,
            )
            for rule in preferences.rules
        ),
        include_draft=preferences.include_draft,
        include_confirmed=preferences.include_confirmed,
    )
    cloned.__post_init__()
    return cloned


def _validate_exact_project_search_handoff(
    candidate: object,
) -> MatcherHandoffSnapshot:
    """Validate one raw handoff without converting its availability state."""

    if type(candidate) is not MatcherHandoffSnapshot:
        raise EditorControllerError("PROJECT_SEARCH.HANDOFF_INVALID")
    handoff = candidate
    handoff.display.__post_init__()
    handoff.__post_init__()
    return handoff


def _fresh_project_search_unavailable_display(
    safe_reason: str,
) -> TextMatcherDisplayState:
    """Mint a fresh closed projection even when a validator is failing."""

    if safe_reason not in (
        "MATCHER.HANDOFF_UNAVAILABLE",
        "PROJECT_SEARCH.HANDOFF_INVALID",
    ):
        raise AssertionError("project-search display reason must be closed")
    return TextMatcherDisplayState(
        state=TextMatcherState.UNAVAILABLE,
        supported_profiles=(),
        safe_reason=safe_reason,
    )


def _validate_project_search_report_against_project(
    report: ProjectSearchReport,
    project: EditorProject,
    request: ProjectSearchRequest,
) -> None:
    """Bind issued hit identities and previews to the current project graph."""

    if type(report) is not ProjectSearchReport:
        raise TypeError("project search binding requires ProjectSearchReport")
    if type(project) is not EditorProject:
        raise TypeError("project search binding requires EditorProject")
    if type(request) is not ProjectSearchRequest:
        raise TypeError("project search binding requires ProjectSearchRequest")
    report.__post_init__()
    project.__post_init__()
    request.__post_init__()
    for hit in report.hits:
        if hit.segment_index >= len(project.segments):
            raise ValueError("project search hit index is outside the project")
        segment = project.segments[hit.segment_index]
        if segment.id != hit.segment_id:
            raise ValueError("project search hit identity does not match the project")
        if hit.field not in request.fields:
            raise ValueError("project search hit field was not requested")
        if (
            request.status is not None
            and segment_translation_status(segment) is not request.status
        ):
            raise ValueError("project search hit status was not requested")
        if hit.field is SearchField.SOURCE:
            field_text = segment.source
        elif hit.field is SearchField.TARGET:
            field_text = segment.target
        else:
            field_text = segment.speaker
        if hit.preview != field_text:
            raise ValueError("project search hit preview does not match the field")


def _workspace_search_field_text(
    segment: ProjectSegment,
    field: SearchField,
) -> str:
    if field is SearchField.SOURCE:
        return segment.source
    if field is SearchField.TARGET:
        return segment.target
    return segment.raw_speaker


def _validate_workspace_search_report(
    report: WorkspaceSearchReport,
    service: ProjectWorkspaceService,
    request: WorkspaceSearchRequest,
    *,
    current_document_id: str,
) -> None:
    """Bind one report to current composite identities and exact field text."""

    if type(report) is not WorkspaceSearchReport:
        raise TypeError("workspace search report must be WorkspaceSearchReport")
    if type(service) is not ProjectWorkspaceService:
        raise TypeError("workspace search service must be ProjectWorkspaceService")
    if type(request) is not WorkspaceSearchRequest:
        raise TypeError("workspace search request must be WorkspaceSearchRequest")
    report.__post_init__()
    request.__post_init__()
    projection = service.flat_segments
    for hit in report.hits:
        if hit.project_global_index >= len(projection):
            raise ValueError("workspace search hit has no current composite identity")
        flat = projection[hit.project_global_index]
        if (
            flat.document_id != hit.document_id
            or flat.identity.local_segment_id != hit.local_segment_id
            or hit.field not in request.fields
            or (
                request.scope is SearchScope.CURRENT_DOCUMENT
                and flat.document_id != current_document_id
            )
            or (
                request.status is not None
                and segment_translation_status(flat.segment) is not request.status
            )
            or _workspace_search_field_text(flat.segment, hit.field) != hit.preview
        ):
            raise ValueError("workspace search hit does not match the workspace")


def _stable_tm_safe_codes(
    *groups: tuple[str, ...],
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for code in group:
            if code in seen:
                continue
            seen.add(code)
            result.append(code)
    return tuple(result)


def _merge_tm_lifecycle_and_query_status(
    *,
    lifecycle: TMResourceStatus,
    retrieval: RetrievalDisplayState,
    query: TMResourceStatus | None,
) -> TMResourceStatus:
    """Project fresh capability; let a same-generation report only narrow it."""

    if (
        type(lifecycle) is not TMResourceStatus
        or type(retrieval) is not RetrievalDisplayState
        or (query is not None and type(query) is not TMResourceStatus)
    ):
        raise TypeError("TM status merge requires exact display contracts")
    lifecycle.__post_init__()
    retrieval.__post_init__()
    if query is not None:
        query.__post_init__()
        if (
            lifecycle.resource_id != query.resource_id
            or lifecycle.resource_name != query.resource_name
        ):
            raise ValueError("TM status merge resource identity mismatch")

    if lifecycle.mode in (
        TMResourceDisplayMode.UNAVAILABLE,
        TMResourceDisplayMode.ACTIVATING,
    ):
        return replace(lifecycle)

    if lifecycle.mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
        if query is None or query.mode is not TMResourceDisplayMode.DEGRADED:
            return replace(lifecycle)
        return TMResourceStatus(
            resource_id=lifecycle.resource_id,
            resource_name=lifecycle.resource_name,
            mode=TMResourceDisplayMode.DEGRADED,
            exact_available=query.exact_available,
            context_available=False,
            fuzzy_available=False,
            safe_codes=_stable_tm_safe_codes(
                lifecycle.safe_codes,
                query.safe_codes,
            ),
            retryable=lifecycle.retryable or query.retryable,
        )

    base_exact = lifecycle.exact_available
    base_context = base_exact and retrieval.context_available
    base_fuzzy = base_exact and retrieval.fuzzy_available
    if query is None:
        return TMResourceStatus(
            resource_id=lifecycle.resource_id,
            resource_name=lifecycle.resource_name,
            mode=lifecycle.mode,
            exact_available=base_exact,
            context_available=base_context,
            fuzzy_available=base_fuzzy,
            safe_codes=_stable_tm_safe_codes(
                lifecycle.safe_codes,
                retrieval.safe_codes,
            ),
            retryable=lifecycle.retryable,
        )

    if query.mode is TMResourceDisplayMode.UNAVAILABLE:
        mode = TMResourceDisplayMode.UNAVAILABLE
    elif (
        query.mode is TMResourceDisplayMode.DEGRADED
        and lifecycle.mode is not TMResourceDisplayMode.SOURCE_DIVERGED
    ):
        mode = TMResourceDisplayMode.DEGRADED
    else:
        mode = lifecycle.mode

    usable = mode is not TMResourceDisplayMode.UNAVAILABLE
    return TMResourceStatus(
        resource_id=lifecycle.resource_id,
        resource_name=lifecycle.resource_name,
        mode=mode,
        exact_available=(
            usable
            and base_exact
            and query.exact_available
        ),
        context_available=(
            usable and base_context and query.context_available
        ),
        fuzzy_available=(usable and base_fuzzy and query.fuzzy_available),
        safe_codes=_stable_tm_safe_codes(
            lifecycle.safe_codes,
            retrieval.safe_codes,
            query.safe_codes,
        ),
        retryable=lifecycle.retryable or query.retryable,
    )


class EditorController:
    """Own one immutable editor session while engines remain UI-state free."""

    def __init__(
        self,
        repository: ResourceRepository,
        workspace_state: WorkspaceStateRepository | None = None,
        tm_adapter: EditorTMAdapter | None = None,
        workspace_package_service: ProjectPackageService | None = None,
        workspace_file_system: object | None = None,
        project_codec_runtime: ProjectCodecRuntime | None = None,
    ) -> None:
        if tm_adapter is not None and type(tm_adapter) is not EditorTMAdapter:
            raise TypeError("TM adapter must be EditorTMAdapter")
        if project_codec_runtime is not None and type(project_codec_runtime) is not ProjectCodecRuntime:
            raise TypeError("project codec runtime must be ProjectCodecRuntime")
        if (
            workspace_package_service is not None
            and type(workspace_package_service) is not ProjectPackageService
        ):
            raise TypeError("workspace package service must be ProjectPackageService")
        self.repository = repository
        self.project_codec_runtime = project_codec_runtime
        self.workspace_state = workspace_state or WorkspaceStateRepository(
            repository.config_dir
        )
        self._tm_adapter = tm_adapter
        self._tm_query_lock = RLock()
        self._rpy_project_session = None
        self._issued_file_jobs: dict[int, ControllerFileJob] = {}
        self._pending_tl_import = None
        self._pending_directory_open = None
        self._pending_source_update = None
        self._source_update_plan = None
        self._source_update_generation = 0
        self._issued_source_update_jobs = {}
        self._tl_export_generation = 0
        self._pending_tl_export = None
        self._issued_tl_export_jobs = {}
        self._tl_export_publish_job = None
        self._tl_export_display = None
        self._file_save_job: ControllerFileJob | None = None
        self._file_save_state: ControllerWorkspaceSaveState | None = None
        self._file_open_generation = 0
        self._workspace_recovery_target: Path | None = None
        self._project_session_id = uuid4().hex
        self._tm_query_epoch = 0
        self._current_project_search_report: ProjectSearchReport | None = None
        self._issued_project_search_hits: tuple[ProjectSearchHit, ...] = ()
        self._issued_project_search_public_hits: tuple[
            ProjectSearchHit,
            ...,
        ] = ()
        self._issued_project_search_context: tuple[
            str,
            int,
            str,
            tuple[SearchField, ...],
            SegmentTranslationStatus | None,
        ] | None = None
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
        self._tm_activation_workers: dict[str, Thread] = {}
        self._tm_runtime_blocked_safe_code: str | None = None
        self._project: EditorProject | None = None
        self._workspace_service: ProjectWorkspaceService | None = None
        self._workspace_save_service: ProjectSaveService | None = None
        self._workspace_package_service = (
            workspace_package_service or ProjectPackageService()
        )
        self._workspace_file_system = workspace_file_system
        self._workspace_persistence_binding: ProjectPackagePersistenceBinding | None = None
        self._workspace_flat_segments: tuple[FlatProjectSegment, ...] = ()
        self._workspace_global_index = 0
        self._workspace_generation = 0
        self._workspace_session_view: WorkspaceSessionView | None = None
        self._workspace_chunk_edit_gate: object | None = None
        self._current_workspace_search_report: WorkspaceSearchReport | None = None
        self._issued_workspace_search_hits: tuple[WorkspaceSearchHit, ...] = ()
        self._issued_workspace_search_public_hits: tuple[WorkspaceSearchHit, ...] = ()
        self._issued_workspace_search_context: tuple[
            str,
            int,
            int,
            str,
            int,
            str,
            tuple[SearchField, ...],
            SegmentTranslationStatus | None,
            SearchScope,
        ] | None = None
        self._issued_workspace_reconciliation: tuple[
            ReconciliationPreview,
            StagedSelectedProjectDocuments,
            ProjectWorkspaceService,
            str,
            int,
            int,
            str,
        ] | None = None
        self._issued_workspace_package_import: tuple[
            ProjectPackageImportPreview,
            str,
            int,
            int,
            str,
            bool,
            bool,
        ] | None = None
        self._current_index = 0
        self._dirty = False
        self._project_revision = 0
        self._saved_project_digest: str | None = None
        self._batch_undo_state: BatchUndoState | None = None
        self._tm_engines: dict[str, TMEngine] = {}
        self._glossary_engines: dict[str, _TermEngine] = {}
        self._term_record_snapshots: dict[str, tuple[TermRecord, ...]] = {}
        self._term_quarantines: dict[str, TermCommitOutcome] = {}
        self._term_matcher_handoff: MatcherHandoffSnapshot | None = None
        self._term_store = TermbaseStore()
        from tmx_resource_package_handler import TmxResourcePackagePayloadHandler

        self._resource_portability = ResourcePortabilityService(
            repository,
            termbase_store=self._term_store,
            tmx_payload_handler=TmxResourcePackagePayloadHandler(),
        )
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
        if self._workspace_service is not None:
            segment = self.current_workspace_segment
            return EditorSegment(
                id=segment.identity.local_segment_id,
                source=segment.source,
                target=segment.target,
                speaker=segment.raw_speaker,
                confirmed=segment.confirmed,
            )
        return self.project.segments[self._current_index]

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def project_revision(self) -> int:
        """Return the monotonic content revision for the current session."""

        return self._project_revision

    @property
    def _workspace_contract(self) -> ProjectWorkspace:
        service = self._workspace_service
        if service is None:
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SESSION")
        return service.workspace

    @property
    def has_workspace(self) -> bool:
        return self._workspace_service is not None

    @property
    def has_active_project(self) -> bool:
        """Return whether either the legacy or workspace session is active."""

        return self._project is not None or self._workspace_service is not None

    @property
    def active_project_dirty(self) -> bool:
        if self._workspace_service is not None:
            return self.workspace_save_state.project_dirty
        return self._dirty

    @property
    def workspace_global_index(self) -> int:
        _ = self._require_workspace_session()
        return self._workspace_global_index

    @property
    def workspace_view(self) -> WorkspaceSessionView:
        _ = self._require_workspace_session()
        view = self._workspace_session_view
        if view is None:
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SESSION")
        return view

    @property
    def workspace_document_tree(
        self,
    ) -> tuple[WorkspaceDirectoryView | WorkspaceDocumentView, ...]:
        """Group saved refs in first-occurrence order without reordering manifest.

        This projection is independent of the source filesystem. Directory names
        confer no identity; document leaves are the exact current-session views.
        """

        # Integer references are private builder positions, never document IDs.
        # Children are created after their parents, so reverse order freezes the
        # tree without consuming Python stack depth for legal long package refs.
        names = [""]
        children: list[list[int | WorkspaceDocumentView]] = [[]]
        directories: dict[tuple[int, str], int] = {}
        for document in self.workspace_view.documents:
            parent = 0
            for name in document.source_ref.split('/')[:-1]:
                key = (parent, name)
                child = directories.get(key)
                if child is None:
                    child = len(children)
                    directories[key] = child
                    names.append(name)
                    children.append([])
                    children[parent].append(child)
                parent = child
            children[parent].append(document)
        frozen: dict[int, WorkspaceDirectoryView] = {}
        for index in range(len(children) - 1, 0, -1):
            frozen[index] = WorkspaceDirectoryView(
                names[index],
                tuple(frozen[item] if isinstance(item, int) else item
                      for item in children[index]),
            )
        return tuple(frozen[item] if isinstance(item, int) else item
                     for item in children[0])

    @property
    def current_workspace_identity(self) -> IssuedSegmentIdentity:
        return self.workspace_view.current_segment

    @property
    def workspace_segment_identities(self) -> tuple[IssuedSegmentIdentity, ...]:
        return tuple(item.identity for item in self.workspace_view.segments)

    def issue_tmx_workspace_scope(
        self,
    ) -> tuple[WorkspaceSessionView, object]:
        """Issue the exact Workspace facts consumed by TMX export.

        The content/order view and body-free presence projection remain
        Workspace-owned.  The TMX coordinator may join them by stable segment
        identity but cannot derive either fact itself.
        """

        service = self._require_workspace_session()
        return self.workspace_view, service.capture_workspace_universe()

    def revalidate_tmx_workspace_scope(
        self,
        session: WorkspaceSessionView,
        universe: object,
    ) -> tuple[WorkspaceSessionView, object]:
        """Reprove one previously issued TMX Workspace scope before publish."""

        if type(session) is not WorkspaceSessionView:
            raise EditorControllerError("TMX.SCOPE.CONTRACT_INVALID")
        service = self._require_workspace_session()
        try:
            validated_universe = service.validate_workspace_universe(universe)
        except ProjectWorkspaceError as error:
            raise EditorControllerError("TMX.SCOPE.STALE") from error
        current = self.workspace_view
        if current != session:
            raise EditorControllerError("TMX.SCOPE.STALE")
        return current, validated_universe

    @property
    def current_workspace_document_id(self) -> str:
        return self.current_workspace_identity.document.document_id

    @property
    def current_workspace_segment(self) -> ProjectSegment:
        _ = self._require_workspace_session()
        return self._workspace_flat_segments[self._workspace_global_index].segment

    @property
    def workspace_document_progress(self) -> tuple[DocumentProgress, ...]:
        return self._require_workspace_session().document_progress

    @property
    def workspace_project_progress(self) -> ProjectProgress:
        return self._require_workspace_session().project_progress

    @property
    def workspace_save_state(self) -> ControllerWorkspaceSaveState:
        if self.workspace_save_running and self._file_save_state is not None:
            return self._file_save_state
        self._require_workspace_session()
        save_service = self._workspace_save_service
        binding = self._workspace_persistence_binding
        if save_service is None:
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
        return ControllerWorkspaceSaveState(
            dirty_document_ids=save_service.dirty_document_ids,
            manifest_dirty=save_service.manifest_dirty,
            project_dirty=save_service.project_dirty,
            package_path=None if binding is None else binding.path,
            artifact_digest=None if binding is None else binding.artifact_digest,
        )

    @property
    def has_preprocessing_undo(self) -> bool:
        """Return whether this session retains one applicable batch undo point."""

        with self._tm_query_lock:
            state = self._batch_undo_state
            return (
                self._project is not None
                and state is not None
                and state.project_session_id == self._project_session_id
            )

    @property
    def project_session_id(self) -> str:
        """Return the opaque identity of the current project session."""

        return self._project_session_id

    @property
    def tm_suggestion_reports_enabled(self) -> bool:
        """Return whether the application TM report seam is configured.

        This is a composition fact for presentation routing, not a retrieval
        capability signal.  Match authorization remains in the frozen report.
        """

        return self._tm_adapter is not None

    def text_matcher_handoff(self) -> MatcherHandoffSnapshot:
        """Expose the sole Core matcher handoff assembled for this app run."""

        adapter = self._tm_adapter
        if adapter is None:
            raise EditorControllerError("MATCHER.HANDOFF_UNAVAILABLE")
        return adapter._text_matcher_handoff_for_controller()

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

    @property
    def current_project_search_report(self) -> ProjectSearchReport | None:
        """Return a defensive view of the last successfully issued search."""

        with self._tm_query_lock:
            report = self._current_project_search_report
            if report is None:
                return None
            public_report = _clone_project_search_report(report)
            self._issued_project_search_public_hits += public_report.hits
            return public_report

    def clear_project_search(self) -> None:
        """Clear only current project-search report and issued membership."""

        with self._tm_query_lock:
            self._clear_project_search_state()

    def project_tool_capability(self) -> ProjectToolCapability:
        """Project the single-JSON tool gate from the sole project session."""

        with self._tm_query_lock:
            project = self._project
            if project is None:
                if self._workspace_service is not None:
                    return ProjectToolCapability(
                        project_session_id=self._project_session_id,
                        single_json_tools_available=False,
                        project_kind="project_package",
                        unavailable_reason="PROJECT_TOOLS.JSON_REQUIRED",
                    )
                return ProjectToolCapability(
                    project_session_id=None,
                    single_json_tools_available=False,
                    project_kind="none",
                    unavailable_reason="PROJECT_TOOLS.NO_PROJECT",
                )

            path = project.path
            project_kind = (
                "sample"
                if path is None
                else path.suffix.removeprefix(".").lower() or "unknown"
            )
            available = path is not None and path.suffix.lower() == ".json"
            return ProjectToolCapability(
                project_session_id=self._project_session_id,
                single_json_tools_available=available,
                project_kind="json" if available else project_kind,
                unavailable_reason=(
                    None if available else "PROJECT_TOOLS.JSON_REQUIRED"
                ),
            )

    def project_search_matcher_display(self) -> TextMatcherDisplayState:
        """Return a safe read-only matcher projection for project-search UI."""

        with self._tm_query_lock:
            try:
                candidate = self.text_matcher_handoff()
            except EditorControllerError as error:
                if (
                    type(error) is EditorControllerError
                    and error.args == ("MATCHER.HANDOFF_UNAVAILABLE",)
                ):
                    return _fresh_project_search_unavailable_display(
                        "MATCHER.HANDOFF_UNAVAILABLE"
                    )
                return _fresh_project_search_unavailable_display(
                    "PROJECT_SEARCH.HANDOFF_INVALID"
                )
            adapter = self._tm_adapter
            if adapter is None or not (
                adapter._is_current_text_matcher_handoff_for_controller(
                    candidate
                )
            ):
                return _fresh_project_search_unavailable_display(
                    "PROJECT_SEARCH.HANDOFF_INVALID"
                )
            try:
                handoff = _validate_exact_project_search_handoff(candidate)
            except EditorControllerError:
                return _fresh_project_search_unavailable_display(
                    "PROJECT_SEARCH.HANDOFF_INVALID"
                )
            return replace(
                handoff.display,
                supported_profiles=tuple(handoff.display.supported_profiles),
            )

    def term_matcher_display(self) -> TextMatcherDisplayState:
        """Return the same sole current-host matcher projection for term UI."""

        return self.project_search_matcher_display()

    def search_project(
        self,
        request: ProjectSearchRequest,
    ) -> ProjectSearchReport:
        """Issue one generation-bound search over the current JSON project."""

        with self._tm_query_lock:
            _ = self._require_project_search_json_gate()
            private_request = _clone_project_search_request(request)
            handoff = self._capture_project_search_handoff()
            self._authorize_project_search_request(handoff, private_request)
            matcher = handoff.matcher
            if matcher is None:
                raise EditorControllerError(
                    "PROJECT_SEARCH.HANDOFF_INVALID"
                )

            try:
                report = ProjectSearchService(matcher).search(
                    self.project,
                    private_request,
                )
            except ProjectSearchError as error:
                raise EditorControllerError(error.code) from error
            try:
                private_report = _clone_project_search_report(report)
                _validate_project_search_report_against_project(
                    private_report,
                    self.project,
                    private_request,
                )
            except ValueError as error:
                raise EditorControllerError(
                    "PROJECT_SEARCH.REPORT_INVALID"
                ) from error
            if private_report.capability != handoff.display:
                raise EditorControllerError(
                    "PROJECT_SEARCH.REPORT_MISMATCH"
                )

            try:
                issued_hits = tuple(
                    replace(hit) for hit in private_report.hits
                )
                issued_context = (
                    self._project_session_id,
                    handoff.generation,
                    _project_search_fields_digest(self.project),
                    tuple(private_request.fields),
                    private_request.status,
                )
                public_report = _clone_project_search_report(
                    private_report
                )
            except ValueError as error:
                raise EditorControllerError(
                    "PROJECT_SEARCH.REPORT_INVALID"
                ) from error
            self._current_project_search_report = private_report
            self._issued_project_search_hits = issued_hits
            self._issued_project_search_public_hits = public_report.hits
            self._issued_project_search_context = issued_context
            return public_report

    def go_to_search_hit(self, hit: ProjectSearchHit) -> EditorProject:
        """Navigate only one exact hit issued for the current search graph."""

        if type(hit) is not ProjectSearchHit:
            raise TypeError("project search hit must be ProjectSearchHit")
        with self._tm_query_lock:
            capability = self._require_project_search_json_gate()
            context = self._issued_project_search_context
            report = self._current_project_search_report
            if context is None or report is None:
                raise EditorControllerError(
                    "PROJECT_SEARCH.NO_ISSUED_REPORT"
                )
            if not any(
                hit is public_hit
                for public_hit in self._issued_project_search_public_hits
            ):
                raise EditorControllerError(
                    "PROJECT_SEARCH.HIT_NOT_ISSUED"
                )
            try:
                candidate = replace(hit)
                candidate.__post_init__()
            except ValueError as error:
                raise EditorControllerError(
                    "PROJECT_SEARCH.HIT_NOT_ISSUED"
                ) from error

            handoff = self._capture_project_search_handoff()
            (
                issued_session_id,
                issued_generation,
                issued_project_digest,
                issued_fields,
                issued_status,
            ) = context
            if (
                issued_session_id != self._project_session_id
                or issued_session_id != capability.project_session_id
            ):
                raise EditorControllerError(
                    "PROJECT_SEARCH.STALE_PROJECT_SESSION"
                )
            if (
                handoff.generation != issued_generation
                or handoff.display != report.capability
            ):
                raise EditorControllerError(
                    "PROJECT_SEARCH.STALE_MATCHER_GENERATION"
                )
            if (
                _project_search_fields_digest(self.project)
                != issued_project_digest
            ):
                raise EditorControllerError("PROJECT_SEARCH.STALE_PROJECT")

            issued = next(
                (
                    member
                    for member in self._issued_project_search_hits
                    if _project_search_hit_fields_equal(candidate, member)
                ),
                None,
            )
            if issued is None:
                raise EditorControllerError(
                    "PROJECT_SEARCH.HIT_NOT_ISSUED"
                )
            if (
                issued.segment_index >= len(self.project.segments)
                or self.project.segments[issued.segment_index].id
                != issued.segment_id
            ):
                raise EditorControllerError("PROJECT_SEARCH.STALE_PROJECT")
            segment = self.project.segments[issued.segment_index]
            if issued.field not in issued_fields:
                raise EditorControllerError("PROJECT_SEARCH.STALE_REQUEST")
            if (
                issued_status is not None
                and segment_translation_status(segment) is not issued_status
            ):
                raise EditorControllerError("PROJECT_SEARCH.STALE_REQUEST")
            return self.go_to(issued.segment_index)

    def _require_project_search_json_gate(self) -> ProjectToolCapability:
        """Validate the public single-JSON gate before matcher access."""

        try:
            capability = self.project_tool_capability()
        except ValueError as error:
            raise EditorControllerError(
                "PROJECT_SEARCH.PROJECT_GATE_INVALID"
            ) from error
        if type(capability) is not ProjectToolCapability:
            raise EditorControllerError(
                "PROJECT_SEARCH.PROJECT_GATE_INVALID"
            )
        try:
            capability.__post_init__()
        except ValueError as error:
            raise EditorControllerError(
                "PROJECT_SEARCH.PROJECT_GATE_INVALID"
            ) from error
        if not capability.single_json_tools_available:
            reason = capability.unavailable_reason
            if reason is None:
                raise EditorControllerError(
                    "PROJECT_SEARCH.PROJECT_GATE_INVALID"
                )
            raise EditorControllerError(reason)
        if capability.project_session_id != self._project_session_id:
            raise EditorControllerError(
                "PROJECT_SEARCH.PROJECT_GATE_INVALID"
            )
        return capability

    def _require_preprocessing_json_gate(self) -> ProjectToolCapability:
        """Validate the same single-JSON capability for preprocessing tools."""

        try:
            capability = self.project_tool_capability()
        except (TypeError, ValueError) as error:
            raise EditorControllerError(
                "PREPROCESS.PROJECT_GATE_INVALID"
            ) from error
        if type(capability) is not ProjectToolCapability:
            raise EditorControllerError(
                "PREPROCESS.PROJECT_GATE_INVALID"
            )
        try:
            capability.__post_init__()
        except (TypeError, ValueError) as error:
            raise EditorControllerError(
                "PREPROCESS.PROJECT_GATE_INVALID"
            ) from error
        if not capability.single_json_tools_available:
            reason = capability.unavailable_reason
            if reason is None:
                raise EditorControllerError(
                    "PREPROCESS.PROJECT_GATE_INVALID"
                )
            raise EditorControllerError(reason)
        if capability.project_session_id != self._project_session_id:
            raise EditorControllerError(
                "PREPROCESS.PROJECT_GATE_INVALID"
            )
        return capability

    def _require_speaker_inventory_json_gate(self) -> ProjectToolCapability:
        """Validate the single-JSON capability without introducing a new authority."""

        try:
            capability = self.project_tool_capability()
        except (TypeError, ValueError) as error:
            raise EditorControllerError(
                "SPEAKER_INVENTORY.PROJECT_GATE_INVALID"
            ) from error
        if type(capability) is not ProjectToolCapability:
            raise EditorControllerError(
                "SPEAKER_INVENTORY.PROJECT_GATE_INVALID"
            )
        try:
            capability.__post_init__()
        except (TypeError, ValueError) as error:
            raise EditorControllerError(
                "SPEAKER_INVENTORY.PROJECT_GATE_INVALID"
            ) from error
        if not capability.single_json_tools_available:
            reason = capability.unavailable_reason
            if reason is None:
                raise EditorControllerError(
                    "SPEAKER_INVENTORY.PROJECT_GATE_INVALID"
                )
            raise EditorControllerError(reason)
        if capability.project_session_id != self._project_session_id:
            raise EditorControllerError(
                "SPEAKER_INVENTORY.PROJECT_GATE_INVALID"
            )
        return capability

    def _capture_project_search_handoff(self) -> MatcherHandoffSnapshot:
        """Capture and validate exactly one Core matcher handoff."""

        try:
            candidate = self.text_matcher_handoff()
        except EditorControllerError as error:
            if (
                type(error) is EditorControllerError
                and error.args == ("MATCHER.HANDOFF_UNAVAILABLE",)
            ):
                raise
            raise EditorControllerError(
                "PROJECT_SEARCH.HANDOFF_INVALID"
            ) from error
        adapter = self._tm_adapter
        if adapter is None or not (
            adapter._is_current_text_matcher_handoff_for_controller(candidate)
        ):
            raise EditorControllerError("PROJECT_SEARCH.HANDOFF_INVALID")
        handoff = _validate_exact_project_search_handoff(candidate)
        if handoff.display.state is TextMatcherState.UNAVAILABLE:
            reason = handoff.display.safe_reason
            if reason is None:
                raise EditorControllerError(
                    "PROJECT_SEARCH.HANDOFF_INVALID"
                )
            raise EditorControllerError(reason)
        if handoff.matcher is None:
            raise EditorControllerError("PROJECT_SEARCH.HANDOFF_INVALID")
        return handoff

    def _authorize_project_search_request(
        self,
        handoff: MatcherHandoffSnapshot,
        request: ProjectSearchRequest | WorkspaceSearchRequest,
    ) -> None:
        """Apply the BASIC/TEXT_V1 gate without reproducing match semantics."""

        state = handoff.display.state
        if request.options == _BASIC_PROJECT_SEARCH_OPTIONS:
            if state not in (
                TextMatcherState.BASIC_VALIDATED,
                TextMatcherState.TEXT_V1_VALIDATED,
            ):
                raise EditorControllerError(
                    "PROJECT_SEARCH.BASIC_UNAVAILABLE"
                )
            return
        if state is not TextMatcherState.TEXT_V1_VALIDATED:
            raise EditorControllerError(
                "PROJECT_SEARCH.ADVANCED_OPTIONS_UNAVAILABLE"
            )

    def _file_context(self) -> tuple:
        return (self._project_session_id, self._workspace_generation,
                self._project_revision)

    @property
    def source_update_documents(self):
        return self.workspace_view.documents if self.is_tl_workspace else ()

    @property
    def source_update_root(self):
        binding = None if self._workspace_service is None else self._workspace_service.origin_binding
        return None if binding is None else Path(binding.absolute_root)

    def _discard_source_update_candidate(self, candidate):
        issued = self._issued_workspace_reconciliation
        if issued is not None and issued[1] is candidate.prepared.staged:
            issued[2].discard_reconciliation(issued[0].operation_id)
            self._issued_workspace_reconciliation = None
            self._source_update_plan = None
        candidate.close()

    def source_update_preview_current(self, review) -> bool:
        pending = self._pending_source_update
        return (pending is not None and pending[0] is review
                and pending[2] == self._file_context() + (self._source_update_generation,))

    def cancel_source_update_preview(self, review=None) -> None:
        pending = self._pending_source_update
        if pending is not None and (review is None or pending[0] is review):
            self._pending_source_update = None
            self._discard_source_update_candidate(pending[1])
        plan = self._source_update_plan
        if plan is not None and (review is None or plan[0] is review):
            self._source_update_plan = None
            plan[1].discard_reconciliation(plan[0].preview.operation_id)
            issued = self._issued_workspace_reconciliation
            if issued is not None and issued[0] is plan[0].preview:
                self._issued_workspace_reconciliation = None
        # A closed Qt runner will never deliver disposed jobs. Their worker
        # retains cleanup ownership until it finishes, independently of this map.
        self._issued_source_update_jobs = {
            key: job for key, job in self._issued_source_update_jobs.items() if not job.disposed}

    def begin_source_update_preview(self, root: Path, source_refs: tuple[str, ...]) -> ControllerFileJob:
        self._ensure_workspace_writable()
        service = self._require_workspace_session()
        documents = service.workspace.documents
        root = Path(root).expanduser().absolute()
        if self.source_update_root is not None and root != self.source_update_root:
            raise EditorControllerError('PROJECT.RECONCILE.ROOT_REBIND_REQUIRED')
        if (not self.is_tl_workspace or type(source_refs) is not tuple
                or len(source_refs) != len(documents)):
            raise EditorControllerError('PROJECT.RECONCILE.INPUT_INVALID')
        from project_workspace_identity import validate_portable_ref_collection
        try:
            validate_portable_ref_collection(source_refs, allow_exact_duplicates=False)
            renames = tuple(OriginRenameMapping(doc.source_ref, ref, doc.document_id)
                            for doc, ref in zip(documents, source_refs, strict=True)
                            if doc.source_ref != ref)
        except (ValueError, TypeError) as error:
            raise EditorControllerError('PROJECT.RECONCILE.INPUT_INVALID') from error
        self.cancel_source_update_preview()
        for old in self._issued_source_update_jobs.values():
            old.dispose()
        self._source_update_generation += 1
        context = self._file_context() + (self._source_update_generation,)
        sources = tuple(root.joinpath(*ref.split('/')) for ref in source_refs)
        # Freeze the input before handing it to a worker; edits remain on the GUI owner.
        snapshot = ProjectWorkspaceService(service.workspace, service.origin_binding,
                    session_id=service.session_id, revision=service.revision)
        config_dir = self.repository.config_dir
        file_system = self._workspace_file_system

        def prepare(cancellation):
            runtime = compose_project_codec_runtime(config_dir)
            prepared = RpyProjectAdapter(runtime).prepare_source_update(
                snapshot, root, sources, rename_mappings=renames,
                file_system=file_system, cancellation=cancellation)
            return SourceUpdateCandidate(prepared, runtime)

        job = ControllerFileJob('preview_source_update', root, context, prepare,
            service=service, cleanup=lambda value: value.close() if value is not None else None)
        self._issued_source_update_jobs[id(job)] = job
        return job

    def begin_source_update_apply(self, review, dispositions: tuple[str, ...]) -> ControllerFileJob:
        self._ensure_workspace_writable()
        if not self.source_update_preview_current(review):
            raise EditorControllerError('PROJECT.RECONCILE.PREVIEW_STALE')
        if type(dispositions) is not tuple or len(dispositions) != len(review.required_items):
            raise EditorControllerError('PROJECT.RECONCILE.DECISION_REQUIRED')
        preview = review.preview
        required = []
        for category in ('unchanged', 'source_changed', 'new', 'removed', 'ambiguous', 'unresolved'):
            required.extend((category, identity) for identity in getattr(preview, category + '_identities')
                            if identity in preview.required_decision_identities)
        decisions = []
        for (category, identity), disposition in zip(required, dispositions, strict=True):
            allowed = ('keep_detached', 'remove') if category == 'removed' else ('keep_detached',)
            if disposition not in allowed:
                raise EditorControllerError('PROJECT.RECONCILE.DECISION_REQUIRED')
            decisions.append(ReconciliationDecision(identity, ReconciliationDisposition(disposition)))
        _, candidate, context = self._pending_source_update
        self._pending_source_update = None
        config_dir = self.repository.config_dir

        def validate(cancellation):
            cancellation.raise_if_cancelled()
            runtime = compose_project_codec_runtime(config_dir)
            if (runtime.settings != candidate.runtime.settings
                    or runtime.availability != candidate.runtime.availability):
                raise ProjectWorkspaceError('PROJECT.RECONCILE.PREVIEW_STALE')
            staged = candidate.prepared.revalidate()
            cancellation.raise_if_cancelled()
            return (candidate, staged, tuple(decisions), review)

        job = ControllerFileJob('validate_source_update', Path('.'), context, validate,
            service=self._workspace_service, session=candidate,
            cleanup=lambda value: candidate.close())
        self._issued_source_update_jobs[id(job)] = job
        return job

    def finish_source_update_job(self, job: ControllerFileJob) -> ControllerFileOutcome:
        if self._issued_source_update_jobs.get(id(job)) is not job:
            raise EditorControllerError('PROJECT.FILE.NOT_ISSUED')
        del self._issued_source_update_jobs[id(job)]
        if job.disposed:
            return ControllerFileOutcome(job.kind, job.path, False, cancelled=True)
        value, error = job.take_result()
        current = (job.service is self._workspace_service
                   and job.context == self._file_context() + (self._source_update_generation,))
        accepted = current and not job.cancellation.cancelled and error is None and value is not None
        if not accepted:
            candidate = value if job.kind == 'preview_source_update' else job.session
            if candidate is not None:
                self._discard_source_update_candidate(candidate)
            if error is not None and current and not job.cancellation.cancelled:
                raise EditorControllerError(self._file_error_text(error)) from error
            return ControllerFileOutcome(job.kind, job.path, False, cancelled=job.cancellation.cancelled)
        if job.kind == 'preview_source_update':
            try:
                preview = self.preview_workspace_reconciliation(value.prepared.staged)
                review = source_update_review(preview, self._workspace_service.workspace,
                                              value.prepared.staged.workspace)
                self._pending_source_update = (review, value, job.context)
                self._source_update_plan = (review, self._workspace_service)
                return ControllerFileOutcome(job.kind, job.path, True, review)
            except BaseException:
                value.close()
                raise
        candidate, staged, decisions, review = value
        try:
            runtime = compose_project_codec_runtime(self.repository.config_dir)
            if (runtime.settings != candidate.runtime.settings
                    or runtime.availability != candidate.runtime.availability):
                raise ProjectWorkspaceError('PROJECT.RECONCILE.PREVIEW_STALE')
            candidate.prepared.revalidate_identity()
            receipt = self._apply_workspace_reconciliation(
                review.preview, candidate.prepared.staged, decisions=decisions, revalidated=staged)
            self._rpy_project_session = RpyProjectSession(
                self._workspace_package_service, self._workspace_save_service,
                prepared=candidate.prepared, persistence_binding=self._workspace_persistence_binding)
            self.project_codec_runtime = candidate.runtime
            return ControllerFileOutcome(job.kind, job.path, True, receipt)
        except ProjectWorkspaceError as error:
            self._discard_source_update_candidate(candidate)
            raise EditorControllerError(self._file_error_text(error)) from error
        except BaseException:
            self._discard_source_update_candidate(candidate)
            raise

    @property
    def workspace_save_running(self) -> bool:
        job = self._file_save_job
        return job is not None and job.service is self._workspace_service

    @property
    def workspace_recovery_target(self) -> Path | None:
        return self._workspace_recovery_target

    @property
    def is_tl_workspace(self) -> bool:
        return self.has_workspace and any(
            d.codec_identity.provider_id == "localcat.rpy"
            for d in self._workspace_contract.documents)

    @property
    def tl_export_default_path(self) -> Path | None:
        if not self.is_tl_workspace or self._workspace_persistence_binding is None:
            return None
        return (self._workspace_persistence_binding.path.parent
                / Path(self._workspace_contract.documents[0].source_ref).name)

    @property
    def tl_export_publish_running(self) -> bool:
        job = self._tl_export_publish_job
        return job is not None and job.service is self._workspace_service

    @property
    def tl_export_unavailable_reason(self) -> str | None:
        if not self.is_tl_workspace:
            return None
        if len(self._workspace_contract.documents) != 1:
            return "TL 导出目前仅支持单文档项目。"
        if self._workspace_persistence_binding is None:
            return "请先保存项目包，再导出 TL。"
        if self._workspace_recovery_target is not None:
            return "项目包需要恢复，暂不能导出 TL。"
        runtime = self.project_codec_runtime
        if runtime is None:
            return "TL 格式支持不可用。"
        document = self._workspace_contract.documents[0]
        available = next((item for item in runtime.availability
                          if item.codec_identity == document.codec_identity
                          and item.format_id.value == document.format_id), None)
        if available is None:
            return "TL 导出不可用：此项目需要其他版本的格式支持。"
        if not available.available:
            reasons = {
                "PARSER.SELECTION.PROVIDER_DISABLED": "格式支持已禁用",
                "PARSER.SELECTION.PROVIDER_MISSING": "格式支持缺失",
                "PARSER.SELECTION.PROVIDER_INCOMPATIBLE": "格式版本不兼容",
                "PARSER.SELECTION.CONFIGURATION_INVALID": "格式配置无效",
            }
            return "TL 导出不可用：" + reasons.get(available.code, "格式支持不可用") + "。项目包仍可编辑保存。"
        return None

    def begin_tl_export_preview(self, target: Path) -> ControllerFileJob:
        """Issue a worker preparation from the sole current saved workspace."""
        self._ensure_workspace_writable()
        if not self.is_tl_workspace or self.tl_export_unavailable_reason is not None:
            raise EditorControllerError('RPY.EXPORT.UNAVAILABLE')
        service = self._require_workspace_session()
        binding = self._workspace_persistence_binding
        target = Path(target).expanduser().absolute()
        self.cancel_tl_export_preview()
        generation = self._tl_export_generation
        context = self._file_context() + (generation, binding)
        config_dir = self.repository.config_dir
        package_service = self._workspace_package_service

        def prepare(cancellation):
            runtime = compose_project_codec_runtime(config_dir)
            prepared = RpyProjectAdapter(runtime, package_service=package_service).prepare_export(
                service, binding, target, config_dir=config_dir,
                request_generation=generation, cancellation=cancellation)
            return FileExportPreparation(prepared, runtime)

        job = ControllerFileJob('prepare_export', target, context, prepare, service=service,
                                cleanup=lambda value: value.close() if value is not None else None)
        self._issued_tl_export_jobs[id(job)] = (job, None)
        return job

    def tl_export_preview_current(self, view, target: Path) -> bool:
        """GUI-safe identity/context check: no package, settings or target I/O."""
        pending = self._pending_tl_export
        if pending is None or view is not pending[0]:
            return False
        _, candidate, context, _ = pending
        runtime = self.project_codec_runtime
        return (view.status == 'ready'
                and context == self._file_context() + (self._tl_export_generation,
                                                     self._workspace_persistence_binding)
                and str(Path(target).expanduser().absolute()) == view.target_path
                and runtime is not None
                and runtime.settings == candidate.runtime.settings
                and runtime.availability == candidate.runtime.availability)

    def cancel_tl_export_preview(self, view=None) -> None:
        pending = self._pending_tl_export
        if view is not None and (pending is None or view is not pending[0]):
            return
        self._tl_export_generation += 1
        self._pending_tl_export = None
        self._tl_export_display = None
        if pending is not None:
            pending[1].close()
        for job, _ in self._issued_tl_export_jobs.values():
            if job.kind == 'prepare_export':
                job.cancel()

    def begin_tl_export_publish(self, view, target: Path) -> ControllerFileJob:
        """Consume the exact issued preview; freeze edits only until worker ends."""
        self._ensure_workspace_writable()
        if not self.tl_export_preview_current(view, target):
            raise EditorControllerError('RPY.EXPORT.PREVIEW_STALE')
        _, candidate, context, cancellation = self._pending_tl_export
        self._pending_tl_export = None
        service = self._require_workspace_session()
        binding = self._workspace_persistence_binding
        config_dir = self.repository.config_dir
        generation = self._tl_export_generation
        target = Path(target).expanduser().absolute()

        def publish(_cancellation):
            try:
                runtime = compose_project_codec_runtime(config_dir)
                result = candidate.prepared.publish(
                    RpyExportContext(service, binding, runtime, generation), target)
                return FileExportPublication(result, runtime)
            finally:
                candidate.close()

        job = ControllerFileJob('publish_export', target, context, publish, service=service,
                                cancellation=cancellation, run_when_cancelled=True,
                                cleanup=lambda _value: candidate.close())
        self._issued_tl_export_jobs[id(job)] = (job, view)
        self._tl_export_publish_job = job
        return job

    def finish_tl_export_job(self, job: ControllerFileJob) -> ControllerFileOutcome:
        """Accept once; publication facts survive cancellation or session changes."""
        issued = self._issued_tl_export_jobs.get(id(job))
        if issued is None or issued[0] is not job:
            raise EditorControllerError('RPY.EXPORT.NOT_ISSUED')
        value, error = job.take_result()
        del self._issued_tl_export_jobs[id(job)]
        current = (job.service is self._workspace_service
                   and job.context == self._file_context() + (
                       self._tl_export_generation, self._workspace_persistence_binding))
        if self._tl_export_publish_job is job:
            self._tl_export_publish_job = None
        if current and value is not None:
            self.project_codec_runtime = value.runtime
        if job.kind == 'prepare_export':
            accepted = current and not job.cancellation.cancelled and value is not None and error is None
            view = None if value is None else value.prepared.view
            if accepted:
                self._pending_tl_export = (view, value, job.context, job.cancellation)
                self._tl_export_display = (view, job.context)
            elif value is not None:
                value.close()
            if error is not None and current and not job.cancellation.cancelled:
                raise EditorControllerError(self._file_error_text(error)) from error
            return ControllerFileOutcome(job.kind, job.path, accepted, view,
                                         job.cancellation.cancelled)
        result = None if value is None else value.result
        if result is None:
            view = issued[1]
            outcome = 'uncertain' if job.operation_invoked else 'failed'
            diagnostic = ProjectExportDiagnostic(
                'RPY.EXPORT.OPERATION_FAILED', 'fatal',
                '导出未取得发布结果，请检查目标文件。' if job.operation_invoked else '无法启动导出，目标未发布。',
                view.document_id, view.source_ref)
            result = ProjectExportResult(
                view.preview_id, view.session_id, view.workspace_revision, view.request_generation,
                view.document_id, view.source_ref, view.target_path,
                outcome, (diagnostic,))
        if current:
            self._tl_export_display = (result, job.context)
        return ControllerFileOutcome(job.kind, job.path, current, result,
                                     job.cancellation.cancelled)

    def tl_export_diagnostic_segment_number(self, projection, diagnostic) -> int | None:
        """Project a readable row only for an exact issued diagnostic and session."""
        issued = self._tl_export_display
        if (issued is None or projection is not issued[0]
                or issued[1] != self._file_context() + (self._tl_export_generation,
                                                       self._workspace_persistence_binding)
                or not any(diagnostic is item for item in projection.diagnostics)):
            return None
        for item in self.workspace_view.segments:
            identity = item.identity.segment_identity
            if (identity.document_id == diagnostic.document_id
                    and identity.local_segment_id == diagnostic.local_segment_id):
                return item.project_global_index + 1
        return None

    def locate_tl_export_diagnostic(self, projection, diagnostic) -> None:
        number = self.tl_export_diagnostic_segment_number(projection, diagnostic)
        if number is None:
            raise EditorControllerError('RPY.EXPORT.DIAGNOSTIC_STALE')
        self.go_to_workspace_segment(self.workspace_view.segments[number - 1].identity)

    def begin_directory_open(self, root: Path) -> ControllerFileJob:
        """Discover metadata in a worker; the current editing session stays live."""
        root = Path(root).expanduser().absolute()
        self._discard_pending_file_opens()
        self._file_open_generation += 1
        context = self._file_context() + (self._file_open_generation,)
        owner = DirectoryOpenCandidate(root, self.repository.config_dir,
            self._workspace_package_service, self._workspace_file_system)
        job = ControllerFileJob('preview_directory', root, context, owner.preview,
            service=owner, cleanup=lambda value: owner.close(), on_cancel=owner.cancel)
        self._issued_file_jobs[id(job)] = job
        return job

    def cancel_directory_open(self, review=None) -> None:
        pending = self._pending_directory_open
        if pending is not None and (review is None or review is pending[0]):
            self._pending_directory_open = None
            pending[1].close()

    def directory_review_current(self, review) -> bool:
        pending = self._pending_directory_open
        return (pending is not None and review is pending[0]
                and pending[2] == self._file_context() + (self._file_open_generation,))

    def begin_directory_publish(self, review, entry_ids: tuple[str, ...], destination: Path,
                                *, name: str, source_locale: str,
                                target_locale: str) -> ControllerFileJob:
        if not self.directory_review_current(review):
            raise EditorControllerError('PROJECT.DIRECTORY.STALE')
        available = {entry.entry_id for entry in review.preview.entries if entry.selectable}
        if (not review.preview.complete or type(entry_ids) is not tuple or not entry_ids
                or any(type(entry_id) is not str for entry_id in entry_ids)
                or len(set(entry_ids)) != len(entry_ids)
                or any(entry_id not in available for entry_id in entry_ids)):
            raise EditorControllerError('PROJECT.DIRECTORY.INVALID_SELECTION')
        request = SelectedProjectDocumentsRequest(name, source_locale, target_locale)
        destination = Path(destination).expanduser().absolute()
        _, owner, context = self._pending_directory_open
        self._pending_directory_open = None
        job = ControllerFileJob('import_directory', destination, context,
            lambda cancellation: owner.publish(entry_ids, request, destination, cancellation),
            service=owner, cleanup=lambda value: owner.close(), on_cancel=owner.cancel)
        self._issued_file_jobs[id(job)] = job
        return job

    def begin_file_open(self, path: Path, *, package: bool = False) -> ControllerFileJob:
        """Issue a background TL/package open without changing the session."""
        return self._begin_file_open(path, package=package)

    def begin_single_tl_import(self, path: Path) -> ControllerFileJob:
        """Validate a TL while retaining the current editable project."""
        if Path(path).suffix.lower() != '.rpy':
            raise EditorControllerError('PROJECT.FILE.FORMAT_UNSUPPORTED')
        return self._begin_file_open(path, prepare_tl=True)

    def _begin_file_open(self, path: Path, *, package=False, prepare_tl=False):
        path = Path(path).expanduser().absolute()
        if not package and path.suffix.lower() not in {".rpy", ".localcat-project", ".zip"}:
            raise EditorControllerError("PROJECT.FILE.FORMAT_UNSUPPORTED")
        self._discard_pending_file_opens()
        self._file_open_generation += 1
        generation = self._file_open_generation
        context = self._file_context() + (generation,)
        package_service = self._workspace_package_service
        file_system = self._workspace_file_system
        config_dir = self.repository.config_dir
        session_id = uuid4().hex

        def prepare(cancellation):
            runtime = compose_project_codec_runtime(config_dir)
            if not package and path.suffix.lower() == ".rpy":
                session = RpyProjectAdapter(runtime, package_service=package_service).prepare_single(
                    path.parent, path,
                    SelectedProjectDocumentsRequest(path.stem, "und", "und"),
                    session_id=session_id, file_system=file_system,
                    cancellation=cancellation,
                    creation_pending=prepare_tl,
                )
                return FileOpenCandidate(session.save_service, None, session, runtime)
            opened = package_service.open(path)
            return FileOpenCandidate(
                opened.create_save_service(session_id=session_id, revision=0),
                opened.persistence_binding, runtime=runtime,
            )

        job = ControllerFileJob("prepare_tl" if prepare_tl else "open", path, context, prepare)
        self._issued_file_jobs[id(job)] = job
        return job

    def cancel_single_tl_import(self, review: SingleTLImportReview | None = None) -> None:
        pending = self._pending_tl_import
        if pending is not None and (review is None or review is pending[0]):
            self._pending_tl_import = None
            pending[1].close()

    def begin_single_tl_publish(self, review: SingleTLImportReview, destination: Path,
                                *, name: str, source_locale: str,
                                target_locale: str) -> ControllerFileJob:
        pending = self._pending_tl_import
        if pending is None or review is not pending[0]:
            raise EditorControllerError('PROJECT.INTAKE.CANDIDATE_STALE')
        _, candidate, context = pending
        self._pending_tl_import = None
        if context != self._file_context() + (self._file_open_generation,):
            candidate.close()
            raise EditorControllerError('PROJECT.INTAKE.CANDIDATE_STALE')
        try:
            request = SelectedProjectDocumentsRequest(name, source_locale, target_locale)
            destination = Path(destination).expanduser().absolute()
        except BaseException:
            candidate.close()
            raise

        def publish(cancellation):
            candidate.session.configure_creation(request)
            candidate.save_service = candidate.session.save_service
            cancellation.raise_if_cancelled()
            result = candidate.session.save(destination)
            candidate.binding = candidate.session.persistence_binding
            return FileImportPublication(candidate, result)

        job = ControllerFileJob('import_tl', destination, context, publish,
                                session=candidate.session)
        self._issued_file_jobs[id(job)] = job
        return job

    def selected_files_use_tl(self, selected_paths: tuple[Path, ...]) -> bool:
        """Resolve configured formats without reading selected file bodies."""
        try:
            runtime = compose_project_codec_runtime(self.repository.config_dir)
            return bool(RpyProjectAdapter(runtime).validate_selected_refs(
                tuple(path.name for path in selected_paths)))
        except Exception as error:
            raise EditorControllerError(self._file_error_text(error)) from error

    def begin_selected_tl_publish(self, root: Path, selected_paths: tuple[Path, ...],
                                  destination: Path, *, name: str,
                                  source_locale: str, target_locale: str) -> ControllerFileJob:
        """Use the existing package job lifecycle for reviewed explicit TL files."""
        request = SelectedProjectDocumentsRequest(name, source_locale, target_locale)
        root = root.expanduser().absolute()
        selected_paths = tuple(path.expanduser().absolute() for path in selected_paths)
        destination = destination.expanduser().absolute()
        self._discard_pending_file_opens()
        self._file_open_generation += 1
        context = self._file_context() + (self._file_open_generation,)
        config_dir = self.repository.config_dir
        package_service, file_system = self._workspace_package_service, self._workspace_file_system

        def publish(cancellation):
            runtime = compose_project_codec_runtime(config_dir)
            adapter = RpyProjectAdapter(runtime, package_service=package_service)
            with adapter.prepare_selected(
                    root, selected_paths, request, session_id=uuid4().hex,
                    file_system=file_system, cancellation=cancellation) as session:
                cancellation.raise_if_cancelled()
                result = session.save(destination)
                candidate = FileOpenCandidate(session.save_service, result.persistence_binding,
                                              runtime=runtime)
                return FileImportPublication(candidate, result)

        job = ControllerFileJob('import_tl', destination, context, publish)
        self._issued_file_jobs[id(job)] = job
        return job

    def begin_file_save(self, destination: Path | None = None) -> ControllerFileJob:
        """Reserve the sole live owner, then let a worker do bounded package I/O."""
        with self._tm_query_lock:
            self._ensure_workspace_writable()
            if self._workspace_recovery_target is not None:
                raise EditorControllerError("PROJECT.SAVE.RECOVERY_REQUIRED")
            service = self._require_workspace_session()
            save_service = self._workspace_save_service
            binding = self._workspace_persistence_binding
            session = self._rpy_project_session
            if save_service is None or save_service.workspace_service is not service:
                raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
            if session is not None and (
                session.closed
                or session.save_service is not save_service
                or session.workspace_service is not service
            ):
                raise EditorControllerError("PROJECT.WORKSPACE.SESSION_STALE")
            if destination is None:
                if binding is None:
                    raise EditorControllerError("PROJECT.FILE.SAVE_AS_REQUIRED")
                destination = binding.path
            destination = Path(destination).expanduser().absolute()
            if session is None and binding is None:
                raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
            package_service = self._workspace_package_service

            def save(_cancellation):
                if session is not None:
                    return session.save(destination)
                return package_service.save_workspace(
                    save_service, destination, persistence_binding=binding)

            job = ControllerFileJob("save", destination, self._file_context(), save,
                                    session=session, service=service)
            self._file_save_state = self.workspace_save_state
            self._issued_file_jobs[id(job)] = job
            self._file_save_job = job
            return job

    def finish_file_job(self, job: ControllerFileJob) -> ControllerFileOutcome:
        """GUI-thread-only, once-only acceptance. A late save keeps its receipt."""
        if self._issued_file_jobs.get(id(job)) is not job:
            raise EditorControllerError("PROJECT.FILE.NOT_ISSUED")
        if job.kind in {"open", "prepare_tl", "preview_directory"} and job.disposed:
            del self._issued_file_jobs[id(job)]
            return ControllerFileOutcome(job.kind, job.path, False, cancelled=True)
        value, error = job.take_result()
        del self._issued_file_jobs[id(job)]
        context_matches = job.context[:3] == self._file_context()
        accepted = context_matches
        safe_code = None
        if job.kind in {"open", "prepare_tl", "import_tl", "preview_directory", "import_directory"}:
            accepted = (accepted and job.context[3] == self._file_open_generation
                        and not job.cancellation.cancelled and value is not None
                        and error is None)
            if job.kind == 'preview_directory':
                if accepted:
                    self._pending_directory_open = (value.review, value, job.context)
                    return ControllerFileOutcome(job.kind, job.path, True, value.review)
                job.service.close()
            elif job.kind == 'prepare_tl':
                if accepted:
                    review = SingleTLImportReview(job.path, job.path.stem,
                        len(value.save_service.workspace_service.flat_segments))
                    self._pending_tl_import = (review, value, job.context)
                    return ControllerFileOutcome(job.kind, job.path, True, review)
                if value is not None:
                    value.close()
            elif job.kind in {'import_tl', 'import_directory'}:
                publication = value
                result = None if publication is None else publication.result
                candidate = None if publication is None else publication.candidate
                accepted = (accepted and result.receipt is not None
                            and result.receipt.durable
                            and not result.save_report.recovery_required
                            and candidate.binding is not None)
                if accepted:
                    try:
                        self._validate_workspace_chunk_replacement(candidate.save_service.workspace_service)
                        self._install_workspace_services(
                            candidate.save_service, candidate.binding,
                            session_id=candidate.save_service.workspace_service.session_id)
                        self._rpy_project_session = candidate.session
                        self.project_codec_runtime = candidate.runtime
                    except Exception as install_error:
                        candidate.close()
                        accepted = False
                        safe_code = self._file_error_text(install_error)
                elif job.session is not None:
                    job.session.close()
                value = result
            elif accepted:
                try:
                    self._validate_workspace_chunk_replacement(value.save_service.workspace_service)
                    self._install_workspace_services(
                        value.save_service, value.binding,
                        session_id=value.save_service.workspace_service.session_id,
                    )
                    self._rpy_project_session = value.session
                    self.project_codec_runtime = value.runtime
                except BaseException:
                    value.close()
                    raise
            elif value is not None:
                value.close()
        else:
            accepted = context_matches and job.service is self._workspace_service
            if self._file_save_job is job:
                self._file_save_job = None
                self._file_save_state = None
            if accepted and value is not None:
                if value.save_report.recovery_required:
                    self._workspace_recovery_target = job.path
                if value.persistence_binding is not None:
                    self._workspace_persistence_binding = value.persistence_binding
                self._reissue_workspace_session_view()
                self._remember_current_workspace_position()
            elif not accepted and job.session is not None:
                job.session.close()
        if job.kind == "import_directory":
            job.service.close()
        if error is not None and not (job.kind in {"open", "prepare_tl", "import_tl", "preview_directory", "import_directory"} and
                                      (job.cancellation.cancelled or not context_matches)):
            raise EditorControllerError(self._file_error_text(error)) from error
        return ControllerFileOutcome(job.kind, job.path, accepted,
                                     value if job.kind in {"save", "import_tl", "import_directory"} else None,
                                     job.cancellation.cancelled, safe_code)

    @staticmethod
    def _file_error_text(error: Exception) -> str:
        availability = getattr(error, "availability", None)
        if availability is not None:
            reasons = {
                "PARSER.SELECTION.PROVIDER_DISABLED": "Ren’Py TL 格式支持已禁用。",
                "PARSER.SELECTION.PROVIDER_MISSING": "Ren’Py TL 格式支持缺失。",
                "PARSER.SELECTION.PROVIDER_INCOMPATIBLE": "Ren’Py TL 格式支持版本不兼容。",
                "PARSER.SELECTION.CONFIGURATION_INVALID": "本机格式配置无效。",
            }
            return availability.code + " · " + reasons.get(availability.code, availability.safe_summary)
        diagnostics = getattr(error, "diagnostics", ())
        if diagnostics:
            issue = diagnostics[0]
            location = f" · 行 {issue.line_number}" if issue.line_number is not None else ""
            source = getattr(error, "source_ref", None)
            return (source + " · " if source else "") + issue.code + location + " · " + issue.safe_summary
        code = getattr(error, "code", "PROJECT.FILE.OPERATION_FAILED")
        directory_reasons = {
            "RPY.IMPORT.MIXED_LANGUAGE": "所选 TL 的目标语言不一致，请选择同一语言的文件。",
            "RPY.IMPORT.DOCUMENT_LIMIT_EXCEEDED": "一次最多选择 256 个 TL 文件，请减少选择。",
            "PROJECT.DIRECTORY.STALE": "目录或选择已变化，请重新选择文件夹。",
            "PROJECT.INTAKE.SOURCE_STALE": "源文件已变化，请重新选择并验证。",
            "PROJECT.DIRECTORY.INVALID_SELECTION": "请选择可用文件后重试。",
            "PROJECT.DIRECTORY.ALIAS": "所选文件存在路径或文件身份重复，请重新选择。",
            "PROJECT.DIRECTORY.INCOMPLETE": "目录预览不完整，请选择较小的目录重试。",
            "PROJECT.DIRECTORY.LIMIT_EXCEEDED": "目录超过预览限制，请选择较小的目录。",
            "PROJECT.DIRECTORY.OBSERVATION_FAILED": "无法完整读取目录，请检查访问权限并重试。",
        }
        source = getattr(error, "source_ref", None)
        return ((source + " · " if source else "") + code
                + (" · " + directory_reasons[code] if code in directory_reasons else ""))

    def _ensure_workspace_writable(self) -> None:
        if self.tl_export_publish_running:
            raise EditorControllerError("PROJECT.FILE.EXPORT_IN_PROGRESS")
        if self.workspace_save_running:
            raise EditorControllerError("PROJECT.FILE.SAVE_IN_PROGRESS")

    def _discard_pending_file_opens(self) -> None:
        self.cancel_directory_open()
        self.cancel_single_tl_import()
        for job in self._issued_file_jobs.values():
            if job.kind in {"open", "prepare_tl", "preview_directory"}:
                job.dispose()
            elif job.kind in {'import_tl', 'import_directory'}:
                job.cancel()

    def _retire_rpy_session(self) -> None:
        self.cancel_source_update_preview()
        for job in self._issued_source_update_jobs.values():
            job.dispose()
        self.cancel_tl_export_preview()
        if self._tl_export_publish_job is not None:
            self._tl_export_publish_job.cancel()
        session = self._rpy_project_session
        self._rpy_project_session = None
        if session is not None:
            job = self._file_save_job
            if job is None or job.session is not session:
                session.close()

    def abandon_file_jobs(self) -> None:
        """Window shutdown revokes publication; workers finish their own cleanup."""
        self.cancel_directory_open()
        self.cancel_source_update_preview()
        for job in self._issued_source_update_jobs.values():
            job.dispose()
        self._issued_source_update_jobs.clear()
        self.cancel_single_tl_import()
        for job in self._issued_file_jobs.values():
            job.dispose()
        self._issued_file_jobs.clear()
        self.cancel_tl_export_preview()
        for job, _ in self._issued_tl_export_jobs.values():
            job.dispose()
        self._issued_tl_export_jobs.clear()
        self._tl_export_publish_job = None
        self._file_save_job = None
        self._rpy_project_session = None

    def open_project_package(self, path: Path) -> WorkspaceSessionView:
        """Cold-open one durable package and swap sessions only after success."""

        try:
            opened = self._workspace_package_service.open(path)
            session_id = uuid4().hex
            save_service = opened.create_save_service(
                session_id=session_id,
                revision=0,
            )
        except ProjectWorkspaceError as error:
            raise EditorControllerError(error.code) from error
        with self._tm_query_lock:
            self._install_opened_workspace(
                opened,
                save_service,
                session_id=session_id,
            )
            return self.workspace_view

    def create_workspace_package(
        self,
        root: Path,
        selected_paths: tuple[Path, ...],
        destination: Path,
        *,
        name: str,
        source_locale: str,
        target_locale: str,
    ) -> ControllerWorkspaceCreationResult:
        """Create one ProjectPackage from an explicit ordered file selection.

        Qt supplies only user selections and display metadata.  Root binding,
        Parser selection, format validation, identity issuance and package
        publication stay in the already-approved C2 owners.
        """

        if not isinstance(root, Path) or not isinstance(destination, Path):
            raise TypeError("workspace creation paths must be pathlib.Path")
        if type(selected_paths) is not tuple or any(
            not isinstance(path, Path) for path in selected_paths
        ):
            raise TypeError("workspace creation selection must be a Path tuple")
        with self._tm_query_lock:
            try:
                staged = stage_selected_project_documents(
                    root,
                    selected_paths,
                    SelectedProjectDocumentsRequest(
                        name=name,
                        source_locale=source_locale,
                        target_locale=target_locale,
                    ),
                    file_system=self._workspace_file_system,
                )
                staging_service = ProjectWorkspaceService(
                    staged.workspace,
                    staged.origin_binding,
                    session_id=uuid4().hex,
                    revision=0,
                )
                receipt = self._workspace_package_service.export_copy(
                    staging_service,
                    destination,
                )
                opened = self._workspace_package_service.open(destination)
                if (
                    opened.validation.artifact_digest != receipt.artifact_digest
                    or opened.validation.workspace_content_digest
                    != receipt.workspace_content_digest
                    or opened.workspace.project_id != receipt.project_id
                ):
                    raise ProjectWorkspaceError(
                        "PROJECT.PACKAGE.DESTINATION_STALE"
                    )
                session_id = uuid4().hex
                save_service = opened.create_save_service(
                    session_id=session_id,
                    revision=0,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            self._install_opened_workspace(
                opened,
                save_service,
                session_id=session_id,
            )
            return ControllerWorkspaceCreationResult(
                receipt=receipt,
                session=self.workspace_view,
            )

    def create_workspace_project_from_selected_files(
        self,
        root: Path,
        selected_paths: tuple[Path, ...],
        request: SelectedProjectDocumentsRequest,
        destination: Path,
    ) -> ControllerWorkspaceCreationResult:
        """Accept the frozen C2 intake request without exposing it to Qt imports."""

        if type(request) is not SelectedProjectDocumentsRequest:
            raise TypeError(
                "workspace creation requires SelectedProjectDocumentsRequest"
            )
        return self.create_workspace_package(
            root,
            selected_paths,
            destination,
            name=request.name,
            source_locale=request.source_locale,
            target_locale=request.target_locale,
        )

    def go_to_workspace_index(
        self,
        index: int,
        *,
        project: IssuedProjectIdentity,
    ) -> WorkspaceSessionView:
        """Navigate by one validated, current-session global projection index."""

        if type(index) is not int:
            raise TypeError("workspace global index must be exact int")
        if type(project) is not IssuedProjectIdentity:
            raise TypeError("workspace index navigation requires issued project")
        with self._tm_query_lock:
            _ = self._require_workspace_session()
            if (
                project is not self.workspace_view.project
                or index < 0
                or index >= len(self._workspace_flat_segments)
            ):
                raise EditorControllerError("PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED")
            self._set_workspace_global_index(index)
            return self.workspace_view

    def go_to_workspace_segment(
        self,
        identity: IssuedSegmentIdentity,
    ) -> WorkspaceSessionView:
        """Navigate only an exact composite identity issued by this session."""

        if type(identity) is not IssuedSegmentIdentity:
            raise TypeError("workspace navigation requires issued segment identity")
        with self._tm_query_lock:
            _ = self._require_workspace_session()
            index = next(
                (
                    item.project_global_index
                    for item in self.workspace_view.segments
                    if item.identity is identity
                ),
                None,
            )
            if index is None:
                raise EditorControllerError(
                    "PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED"
                )
            self._set_workspace_global_index(index)
            return self.workspace_view

    def select_workspace_document(
        self,
        document: IssuedDocumentIdentity,
    ) -> WorkspaceSessionView:
        """Select the first segment of one current-session document."""

        if type(document) is not IssuedDocumentIdentity:
            raise TypeError("workspace selection requires issued document identity")
        with self._tm_query_lock:
            _ = self._require_workspace_session()
            if not any(item.identity is document for item in self.workspace_view.documents):
                raise EditorControllerError(
                    "PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED"
                )
            index = next(
                (
                    item.project_global_index
                    for item in self._workspace_flat_segments
                    if item.document_id == document.document_id
                ),
                None,
            )
            if index is None:
                raise EditorControllerError(
                    "PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED"
                )
            self._set_workspace_global_index(index)
            return self.workspace_view

    def move_workspace(
        self,
        direction: int,
        *,
        unconfirmed_only: bool = False,
    ) -> WorkspaceSessionView:
        """Navigate continuously across document boundaries."""

        if type(direction) is not int or direction == 0:
            raise EditorControllerError("PROJECT.WORKSPACE.NAVIGATION_INVALID")
        if type(unconfirmed_only) is not bool:
            raise TypeError("workspace unconfirmed flag must be exact bool")
        with self._tm_query_lock:
            _ = self._require_workspace_session()
            step = 1 if direction > 0 else -1
            destination = min(
                max(self._workspace_global_index + step, 0),
                len(self._workspace_flat_segments) - 1,
            )
            if unconfirmed_only:
                destination = next(
                    (
                        index
                        for index in range(
                            self._workspace_global_index + step,
                            len(self._workspace_flat_segments) if step > 0 else -1,
                            step,
                        )
                        if not self._workspace_flat_segments[index].segment.confirmed
                    ),
                    self._workspace_global_index,
                )
            self._set_workspace_global_index(destination)
            return self.workspace_view

    def update_workspace_target(self, target: str) -> WorkspaceSessionView:
        """Edit only the current overlay through the workspace authority."""

        if type(target) is not str:
            raise TypeError("workspace target must be exact str")
        with self._tm_query_lock:
            service = self._require_workspace_session()
            current_identity = self.current_workspace_identity.segment_identity
            preparation = self._prepare_workspace_chunk_edit(
                service,
                current_identity,
                target=target,
                confirmed=False,
            )
            try:
                receipt = self._commit_workspace_chunk_edit(
                    preparation,
                    service,
                    current_identity,
                    target=target,
                    confirmed=False,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            if receipt.changed:
                self._project_revision = receipt.resulting_revision
                self._refresh_workspace_projection(current_identity)
                self._advance_tm_query_epoch()
                # Suggestion apply holds the runtime generation lock here.
                # Capture the new baseline on the next query, outside that lock.
                self._observed_tm_signature = None
            return self.workspace_view

    def search_workspace(
        self,
        request: WorkspaceSearchRequest,
    ) -> WorkspaceSearchReport:
        """Issue a scope-bound search over the current workspace graph."""

        with self._tm_query_lock:
            service = self._require_workspace_session()
            private_request = _clone_workspace_search_request(request)
            handoff = self._capture_project_search_handoff()
            self._authorize_project_search_request(handoff, private_request)
            matcher = handoff.matcher
            if matcher is None:
                raise EditorControllerError("PROJECT_SEARCH.HANDOFF_INVALID")
            try:
                report = ProjectSearchService(matcher).search_workspace(
                    service,
                    private_request,
                    current_document_id=self.current_workspace_document_id,
                )
                private_report = _clone_workspace_search_report(report)
                _validate_workspace_search_report(
                    private_report,
                    service,
                    private_request,
                    current_document_id=self.current_workspace_document_id,
                )
            except ProjectSearchError as error:
                raise EditorControllerError(error.code) from error
            except ValueError as error:
                raise EditorControllerError(
                    "PROJECT_SEARCH.REPORT_INVALID"
                ) from error
            if private_report.capability != handoff.display:
                raise EditorControllerError("PROJECT_SEARCH.REPORT_MISMATCH")
            issued_hits = tuple(replace(hit) for hit in private_report.hits)
            public_report = _clone_workspace_search_report(private_report)
            self._current_workspace_search_report = private_report
            self._issued_workspace_search_hits = issued_hits
            self._issued_workspace_search_public_hits = public_report.hits
            self._issued_workspace_search_context = (
                self._project_session_id,
                self._workspace_generation,
                service.revision,
                service.workspace_content_digest,
                handoff.generation,
                self.current_workspace_document_id,
                tuple(private_request.fields),
                private_request.status,
                private_request.scope,
            )
            return public_report

    def _search_workspace_selection_for_chunk_controller(
        self,
        request: WorkspaceSearchRequest,
        members: tuple[SegmentIdentity, ...],
    ) -> WorkspaceSearchReport:
        """Run the normal matcher over one exact, chunk-neutral selection."""

        with self._tm_query_lock:
            service = self._require_workspace_session()
            private_request = _clone_workspace_search_request(request)
            if private_request.scope is not SearchScope.ENTIRE_PROJECT:
                raise EditorControllerError("PROJECT_SEARCH.SELECTION_INVALID")
            handoff = self._capture_project_search_handoff()
            self._authorize_project_search_request(handoff, private_request)
            matcher = handoff.matcher
            if matcher is None:
                raise EditorControllerError("PROJECT_SEARCH.HANDOFF_INVALID")
            try:
                report = ProjectSearchService(
                    matcher
                ).search_workspace_selection(
                    service,
                    private_request,
                    members=members,
                )
                private_report = _clone_workspace_search_report(report)
                _validate_workspace_search_report(
                    private_report,
                    service,
                    private_request,
                    current_document_id=self.current_workspace_document_id,
                )
            except ProjectSearchError as error:
                raise EditorControllerError(error.code) from error
            except ValueError as error:
                raise EditorControllerError(
                    "PROJECT_SEARCH.REPORT_INVALID"
                ) from error
            if private_report.capability != handoff.display:
                raise EditorControllerError("PROJECT_SEARCH.REPORT_MISMATCH")
            issued_hits = tuple(replace(hit) for hit in private_report.hits)
            public_report = _clone_workspace_search_report(private_report)
            self._current_workspace_search_report = private_report
            self._issued_workspace_search_hits = issued_hits
            self._issued_workspace_search_public_hits = public_report.hits
            self._issued_workspace_search_context = (
                self._project_session_id,
                self._workspace_generation,
                service.revision,
                service.workspace_content_digest,
                handoff.generation,
                self.current_workspace_document_id,
                tuple(private_request.fields),
                private_request.status,
                private_request.scope,
            )
            return public_report

    def clear_workspace_search(self) -> None:
        """Revoke only workspace-search reports and issued hit membership."""

        with self._tm_query_lock:
            self._clear_workspace_search_state()

    def go_to_workspace_search_hit(
        self,
        hit: WorkspaceSearchHit,
    ) -> WorkspaceSessionView:
        """Navigate one exact issued hit after revalidating all live facts."""

        if type(hit) is not WorkspaceSearchHit:
            raise TypeError("workspace search hit must be WorkspaceSearchHit")
        with self._tm_query_lock:
            service = self._require_workspace_session()
            if not any(
                hit is public_hit
                for public_hit in self._issued_workspace_search_public_hits
            ):
                raise EditorControllerError("PROJECT_SEARCH.HIT_NOT_ISSUED")
            context = self._issued_workspace_search_context
            report = self._current_workspace_search_report
            if context is None or report is None:
                raise EditorControllerError("PROJECT_SEARCH.NO_ISSUED_REPORT")
            (
                issued_session,
                issued_generation,
                issued_revision,
                issued_digest,
                issued_matcher_generation,
                issued_current_document,
                issued_fields,
                issued_status,
                issued_scope,
            ) = context
            handoff = self._capture_project_search_handoff()
            if (
                issued_session != self._project_session_id
                or issued_generation != self._workspace_generation
                or issued_revision != service.revision
                or issued_digest != service.workspace_content_digest
            ):
                raise EditorControllerError("PROJECT_SEARCH.STALE_WORKSPACE")
            if (
                issued_matcher_generation != handoff.generation
                or report.capability != handoff.display
            ):
                raise EditorControllerError(
                    "PROJECT_SEARCH.STALE_MATCHER_GENERATION"
                )
            issued = next(
                (
                    item
                    for item in self._issued_workspace_search_hits
                    if _workspace_search_hit_fields_equal(hit, item)
                ),
                None,
            )
            if issued is None:
                raise EditorControllerError("PROJECT_SEARCH.HIT_NOT_ISSUED")
            if issued.project_global_index >= len(self._workspace_flat_segments):
                raise EditorControllerError("PROJECT_SEARCH.STALE_WORKSPACE")
            flat = self._workspace_flat_segments[issued.project_global_index]
            if (
                issued.document_id != flat.document_id
                or issued.local_segment_id != flat.identity.local_segment_id
                or issued.field not in issued_fields
                or (
                    issued_scope is SearchScope.CURRENT_DOCUMENT
                    and flat.document_id != issued_current_document
                )
                or (
                    issued_status is not None
                    and segment_translation_status(flat.segment) is not issued_status
                )
                or _workspace_search_field_text(flat.segment, issued.field)
                != issued.preview
            ):
                raise EditorControllerError("PROJECT_SEARCH.STALE_WORKSPACE")
            self._set_workspace_global_index(issued.project_global_index)
            return self.workspace_view

    def save_workspace_package(self) -> ControllerWorkspaceSaveResult:
        with self._tm_query_lock:
            self._ensure_workspace_writable()
            _service, save_service, binding = self._require_workspace_save_session()
            try:
                if self._rpy_project_session is not None:
                    result = self._rpy_project_session.save(binding.path)
                else:
                    result = self._workspace_package_service.save_workspace(
                        save_service,
                        binding.path,
                        persistence_binding=binding,
                    )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            if result.persistence_binding is not None:
                self._workspace_persistence_binding = result.persistence_binding
            self._reissue_workspace_session_view()
            binding = self._workspace_persistence_binding
            if binding is None:
                raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
            return ControllerWorkspaceSaveResult(
                save_report=result.save_report,
                receipt=result.receipt,
                session=self.workspace_view,
                package_artifact_digest=binding.artifact_digest,
            )

    def save_workspace_document(
        self,
        document: IssuedDocumentIdentity,
    ) -> ControllerWorkspaceSaveResult:
        if type(document) is not IssuedDocumentIdentity:
            raise TypeError("workspace save requires issued document identity")
        with self._tm_query_lock:
            self._ensure_workspace_writable()
            _service, save_service, binding = self._require_workspace_save_session()
            if not any(item.identity is document for item in self.workspace_view.documents):
                raise EditorControllerError(
                    "PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED"
                )
            try:
                result = self._workspace_package_service.save_document(
                    save_service,
                    document.document_id,
                    binding.path,
                    persistence_binding=binding,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            if result.persistence_binding is not None:
                self._workspace_persistence_binding = result.persistence_binding
            self._reissue_workspace_session_view()
            binding = self._workspace_persistence_binding
            if binding is None:
                raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
            return ControllerWorkspaceSaveResult(
                save_report=result.save_report,
                receipt=result.receipt,
                session=self.workspace_view,
                package_artifact_digest=binding.artifact_digest,
            )

    def preview_workspace_reconciliation(
        self,
        staged: StagedSelectedProjectDocuments,
        *,
        associations: tuple[ReconciliationAssociation, ...] = (),
    ) -> ReconciliationPreview:
        """Stage source reconciliation against the current issued session."""

        if type(staged) is not StagedSelectedProjectDocuments:
            raise TypeError("reconciliation input must be staged selected documents")
        with self._tm_query_lock:
            service = self._require_workspace_session()
            try:
                stage = (
                    service.stage_source_rebind
                    if service.origin_binding is None
                    else service.stage_reconciliation
                )
                preview = stage(
                    staged,
                    associations=associations,
                    session_id=self._project_session_id,
                    base_revision=self._project_revision,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            self._issued_workspace_reconciliation = (
                preview,
                staged,
                service,
                self._project_session_id,
                self._workspace_generation,
                self._project_revision,
                service.workspace_content_digest,
            )
            return preview

    def stage_workspace_source_rebind(
        self,
        root: Path,
        selected_paths: tuple[Path, ...],
        *,
        rename_mappings: tuple[OriginRenameMapping, ...] = (),
    ) -> StagedSelectedProjectDocuments:
        """Stage explicit device-local sources against manifest-issued IDs."""

        with self._tm_query_lock:
            service = self._require_workspace_session()
            try:
                return stage_workspace_rebind(
                    root,
                    selected_paths,
                    service.workspace,
                    rename_mappings=rename_mappings,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error

    def apply_workspace_reconciliation(
        self,
        preview: ReconciliationPreview,
        staged: StagedSelectedProjectDocuments,
        *,
        decisions: tuple[ReconciliationDecision, ...] = (),
    ) -> ReconciliationReceipt:
        """Consume one issued reconciliation and publish one in-memory swap."""
        return self._apply_workspace_reconciliation(preview, staged, decisions=decisions)

    def _apply_workspace_reconciliation(
        self, preview, staged, *, decisions=(), revalidated=None,
    ) -> ReconciliationReceipt:

        if type(preview) is not ReconciliationPreview:
            raise TypeError("reconciliation preview must be exact")
        if type(staged) is not StagedSelectedProjectDocuments:
            raise TypeError("reconciliation input must be exact")
        with self._tm_query_lock:
            self._ensure_workspace_writable()
            service = self._require_workspace_session()
            issued = self._issued_workspace_reconciliation
            if (
                issued is None
                or preview is not issued[0]
                or staged is not issued[1]
            ):
                raise EditorControllerError("PROJECT.RECONCILE.PREVIEW_STALE")
            if (
                issued[3] != self._project_session_id
                or issued[4] != self._workspace_generation
                or issued[5] != self._project_revision
                or issued[6] != service.workspace_content_digest
            ):
                raise EditorControllerError("PROJECT.RECONCILE.PREVIEW_STALE")
            reconciliation_service = issued[2]
            old_projection = self._workspace_flat_segments
            old_index = self._workspace_global_index
            binding = self._workspace_persistence_binding
            if binding is None:
                raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
            self._issued_workspace_reconciliation = None
            try:
                if revalidated is None:
                    revalidated = revalidate_staged_selected_documents(
                        staged,
                        file_system=self._workspace_file_system,
                    )
                token = reconciliation_service.prepare_reconciliation(
                    preview.operation_id,
                    decisions=decisions,
                    session_id=self._project_session_id,
                    base_revision=self._project_revision,
                    incoming_source_identities=revalidated.source_identities,
                )
                try:
                    candidate_service = (
                        reconciliation_service.prepared_workspace_service(token)
                    )
                    current_save_service = self._workspace_save_service
                    if current_save_service is None:
                        raise EditorControllerError(
                            "PROJECT.WORKSPACE.NO_SAVE_BINDING"
                        )
                    candidate_save_service = (
                        current_save_service.fork_for_workspace_service(
                            candidate_service
                        )
                    )
                    new_projection = candidate_service.flat_segments
                    old_identity = old_projection[old_index].identity
                    surviving = {item.identity for item in new_projection}
                    if old_identity in surviving:
                        chosen = old_identity
                    else:
                        ordered_old = tuple(item.identity for item in old_projection)
                        chosen = next(
                            (
                                identity
                                for identity in ordered_old[old_index + 1 :]
                                if identity in surviving
                            ),
                            next(
                                (
                                    identity
                                    for identity in reversed(
                                        ordered_old[:old_index]
                                    )
                                    if identity in surviving
                                ),
                                new_projection[0].identity,
                            ),
                        )
                    candidate_index = next(
                        item.project_global_index
                        for item in new_projection
                        if item.identity == chosen
                    )
                    candidate_install = self._prepare_workspace_install(
                        candidate_save_service,
                        session_id=self._project_session_id,
                        generation=self._workspace_generation,
                        current_index=candidate_index,
                    )
                except BaseException:
                    reconciliation_service.discard_prepared_reconciliation(
                        token
                    )
                    raise
                receipt = reconciliation_service.commit_reconciliation(token)
                try:
                    published_transition = (
                        reconciliation_service.published_workspace_transition(
                            receipt
                        )
                    )
                    self._capture_workspace_chunk_transition(
                        reconciliation_service,
                        published_transition,
                    )
                except BaseException:
                    # Reconciliation is already published and cannot be
                    # rolled back.  Preserve the mandatory Workspace swap and
                    # let the optional Chunk layer rebind in a blocked state.
                    self._mark_workspace_chunk_transition_capture_failed(
                        reconciliation_service
                    )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            self._retire_rpy_session()
            self._discard_pending_file_opens()
            self._project = None
            self._current_index = 0
            self._dirty = False
            self._saved_project_digest = None
            self._batch_undo_state = None
            self._workspace_service = candidate_install.service
            self._workspace_save_service = candidate_install.save_service
            self._workspace_persistence_binding = binding
            self._workspace_recovery_target = None
            self._workspace_flat_segments = candidate_install.projection
            self._workspace_global_index = candidate_install.current_index
            self._workspace_generation = candidate_install.generation
            self._workspace_session_view = candidate_install.view
            self._project_session_id = candidate_install.session_id
            self._project_revision = candidate_install.service.revision
            self._current_project_search_report = None
            self._issued_project_search_hits = ()
            self._issued_project_search_public_hits = ()
            self._issued_project_search_context = None
            self._current_workspace_search_report = None
            self._issued_workspace_search_hits = ()
            self._issued_workspace_search_public_hits = ()
            self._issued_workspace_search_context = None
            self._issued_workspace_reconciliation = None
            self._issued_workspace_package_import = None
            self._issued_tm_suggestions = ()
            self._issued_legacy_tm_suggestions = ()
            self._legacy_issued_context = None
            self._current_tm_report = None
            self._tm_query_epoch += 1
            self._observed_tm_signature = candidate_install.observed_tm_signature
            self._notify_workspace_chunk_opened()
            return receipt

    def preview_workspace_package_import(
        self,
        source: Path,
        *,
        associations: tuple[ReconciliationAssociation, ...] = (),
        destination: Path | None = None,
    ) -> ProjectPackageImportPreview:
        """Preview one package transaction against the active session.

        A workspace defaults to its current package binding.  A legacy project
        must supply a distinct package destination; importing there replaces
        the active session only after apply and never rewrites the legacy file.
        """

        with self._tm_query_lock:
            legacy_session = not self.has_workspace
            if legacy_session:
                if self._project is None:
                    raise EditorControllerError("PROJECT.WORKSPACE.NO_SESSION")
                if destination is None:
                    raise EditorControllerError(
                        "PROJECT.PACKAGE.DESTINATION_REQUIRED"
                    )
                service = None
                save_service = None
                binding_path = None
                target = destination
                content_digest = _project_content_digest(self._project)
            else:
                service, save_service, binding = (
                    self._require_workspace_save_session()
                )
                binding_path = binding.path
                target = binding.path if destination is None else destination
                content_digest = service.workspace_content_digest
            try:
                preview = self._workspace_package_service.preview_import(
                    source,
                    target,
                    workspace_service=service,
                    associations=associations,
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            swap_active = legacy_session or target == binding_path
            if (
                save_service is not None
                and swap_active
                and preview.mode is ProjectPackageImportMode.REPLACE
                and save_service.project_dirty
            ):
                raise EditorControllerError(
                    "PROJECT.PACKAGE.ACTIVE_WORKSPACE_DIRTY"
                )
            self._issued_workspace_package_import = (
                preview,
                self._project_session_id,
                self._workspace_generation,
                self._project_revision,
                content_digest,
                swap_active,
                legacy_session,
            )
            return preview

    def apply_workspace_package_import(
        self,
        preview: ProjectPackageImportPreview,
        *,
        decisions: tuple[ReconciliationDecision, ...] = (),
    ) -> ControllerWorkspaceImportResult:
        """Durably apply one issued package preview, then swap the session once."""

        if type(preview) is not ProjectPackageImportPreview:
            raise TypeError("package import preview must be exact")
        with self._tm_query_lock:
            self._ensure_workspace_writable()
            issued = self._issued_workspace_package_import
            if issued is None or preview is not issued[0]:
                raise EditorControllerError("PROJECT.PACKAGE.PREVIEW_STALE")
            legacy_session = issued[6]
            if legacy_session:
                current_content_digest = (
                    None
                    if self._project is None or self.has_workspace
                    else _project_content_digest(self._project)
                )
            else:
                service = self._require_workspace_session()
                current_content_digest = service.workspace_content_digest
            if (
                issued[1] != self._project_session_id
                or issued[2] != self._workspace_generation
                or issued[3] != self._project_revision
                or issued[4] != current_content_digest
            ):
                raise EditorControllerError("PROJECT.PACKAGE.PREVIEW_STALE")
            self._issued_workspace_package_import = None
            swap_active = issued[5]
            try:
                if not swap_active:
                    result = self._workspace_package_service.apply_import_result(
                        preview.operation_id,
                        decisions=decisions,
                        session_id=(
                            self._project_session_id if preview.same_project else None
                        ),
                        base_revision=(
                            self._project_revision if preview.same_project else None
                        ),
                    )
                    return ControllerWorkspaceImportResult(
                        receipt=result.receipt,
                        session=self.workspace_view,
                        active_session_changed=False,
                    )

                prepared_import = self._workspace_package_service.prepare_import(
                    preview.operation_id,
                    decisions=decisions,
                    session_id=(
                        self._project_session_id if preview.same_project else None
                    ),
                    base_revision=(
                        self._project_revision if preview.same_project else None
                    ),
                )
                try:
                    next_session = uuid4().hex
                    candidate_save_service = (
                        self._workspace_package_service.create_prepared_import_save_service(
                            prepared_import,
                            session_id=next_session,
                        )
                    )
                    candidate_install = self._prepare_workspace_install(
                        candidate_save_service,
                        session_id=next_session,
                        generation=self._workspace_generation + 1,
                        current_index=0,
                    )
                    self._validate_workspace_chunk_replacement(
                        candidate_install.service
                    )
                except BaseException:
                    self._workspace_package_service.discard_prepared_import(
                        prepared_import
                    )
                    raise
                result = self._workspace_package_service.commit_prepared_import(
                    prepared_import
                )
            except ProjectWorkspaceError as error:
                raise EditorControllerError(error.code) from error
            binding = result.installed.persistence_binding
            self._retire_rpy_session()
            self._discard_pending_file_opens()
            self._project = None
            self._current_index = 0
            self._dirty = False
            self._saved_project_digest = None
            self._batch_undo_state = None
            self._workspace_service = candidate_install.service
            self._workspace_save_service = candidate_install.save_service
            self._workspace_persistence_binding = binding
            self._workspace_recovery_target = None
            self._workspace_flat_segments = candidate_install.projection
            self._workspace_global_index = candidate_install.current_index
            self._workspace_generation = candidate_install.generation
            self._workspace_session_view = candidate_install.view
            self._project_session_id = candidate_install.session_id
            self._project_revision = candidate_install.service.revision
            self._current_project_search_report = None
            self._issued_project_search_hits = ()
            self._issued_project_search_public_hits = ()
            self._issued_project_search_context = None
            self._current_workspace_search_report = None
            self._issued_workspace_search_hits = ()
            self._issued_workspace_search_public_hits = ()
            self._issued_workspace_search_context = None
            self._issued_workspace_reconciliation = None
            self._issued_workspace_package_import = None
            self._issued_tm_suggestions = ()
            self._issued_legacy_tm_suggestions = ()
            self._legacy_issued_context = None
            self._current_tm_report = None
            self._tm_query_epoch += 1
            self._observed_tm_signature = candidate_install.observed_tm_signature
            self._notify_workspace_chunk_opened()
            return ControllerWorkspaceImportResult(
                receipt=result.receipt,
                session=self.workspace_view,
                active_session_changed=True,
            )

    def open_project(self, path: Path) -> EditorProject:
        """Load a local JSON/TXT project and reset navigation only after success."""

        try:
            project = load_project(path)
        except ProjectError as exc:
            raise EditorControllerError("PROJECT.LOAD_FAILED") from exc
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
            self._retire_rpy_session()
            self._discard_pending_file_opens()
            self._remember_current_workspace_position()
            if self._workspace_service is not None:
                self._notify_workspace_chunk_closed()
            self._workspace_service = None
            self._workspace_save_service = None
            self._workspace_persistence_binding = None
            self._workspace_recovery_target = None
            self._workspace_flat_segments = ()
            self._workspace_global_index = 0
            self._workspace_session_view = None
            self._clear_workspace_search_state()
            self._issued_workspace_reconciliation = None
            self._issued_workspace_package_import = None
            self._project = project
            self._current_index = 0
            self._dirty = False
            self._project_session_id = uuid4().hex
            self._project_revision = 0
            self._saved_project_digest = _project_content_digest(project)
            self._batch_undo_state = None
            self._clear_project_search_state()
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
            self._project_revision += 1
            self._recompute_project_dirty()
            return self.project

    def preview_preprocessing(
        self,
        rules: tuple[LiteralReplaceRule, ...],
        *,
        include_draft: bool = True,
        include_confirmed: bool = True,
    ) -> PreprocessPreview:
        """Preview ordered target-only literal rules without changing the project."""

        if type(rules) is not tuple or not all(
            type(rule) is LiteralReplaceRule for rule in rules
        ):
            raise EditorControllerError("PREPROCESS.INVALID_RULES")
        with self._tm_query_lock:
            self._require_preprocessing_json_gate()
            try:
                preview = preview_preprocessing(
                    self.project,
                    self._project_session_id,
                    self._project_revision,
                    tuple(replace(rule) for rule in rules),
                    include_draft=include_draft,
                    include_confirmed=include_confirmed,
                )
                return _clone_preprocess_preview(preview)
            except PreprocessValidationError as error:
                raise EditorControllerError(
                    f"PREPROCESS.{error.code}"
                ) from error
            except (TypeError, ValueError) as error:
                raise EditorControllerError("PREPROCESS.INVALID_RULES") from error

    def preprocess_preferences(self) -> PreprocessPreferences:
        """Return a detached view of saved device-local preprocessing settings."""

        with self._tm_query_lock:
            try:
                preferences = self.workspace_state.preprocess_preferences()
                return _clone_preprocess_preferences(preferences)
            except WorkspaceStateError as error:
                raise EditorControllerError(
                    "PREPROCESS.PREFERENCES_READ_FAILED"
                ) from error
            except (TypeError, ValueError) as error:
                raise EditorControllerError(
                    "PREPROCESS.PREFERENCES_INVALID"
                ) from error

    def update_preprocess_preferences(
        self,
        preferences: PreprocessPreferences,
    ) -> PreprocessPreferences:
        """Validate and atomically save device-local preprocessing settings."""

        try:
            private_preferences = _clone_preprocess_preferences(preferences)
        except (TypeError, ValueError) as error:
            raise EditorControllerError(
                "PREPROCESS.PREFERENCES_INVALID"
            ) from error
        with self._tm_query_lock:
            try:
                saved = self.workspace_state.update_preprocess_preferences(
                    private_preferences
                )
                return _clone_preprocess_preferences(saved)
            except WorkspaceStateError as error:
                raise EditorControllerError(
                    "PREPROCESS.PREFERENCES_SAVE_FAILED"
                ) from error
            except (TypeError, ValueError) as error:
                raise EditorControllerError(
                    "PREPROCESS.PREFERENCES_INVALID"
                ) from error

    def speaker_inventory(self) -> SpeakerInventory:
        """Return a fresh, deterministic inventory without changing the session."""

        with self._tm_query_lock:
            self._require_speaker_inventory_json_gate()
            try:
                inventory = build_speaker_inventory(self.project)
                result = SpeakerInventory(
                    items=tuple(replace(item) for item in inventory.items),
                    empty_count=inventory.empty_count,
                    segment_count=inventory.segment_count,
                )
                result.__post_init__()
                return result
            except (TypeError, ValueError) as error:
                raise EditorControllerError(
                    "SPEAKER_INVENTORY.INVALID_RESULT"
                ) from error

    def apply_preprocessing(
        self,
        preview: PreprocessPreview,
    ) -> BatchOperationReport:
        """Atomically apply one session/revision-bound preprocessing preview."""

        try:
            private_preview = _clone_preprocess_preview(preview)
        except (TypeError, ValueError) as error:
            raise EditorControllerError("PREPROCESS.PREVIEW_INVALID") from error
        with self._tm_query_lock:
            self._require_preprocessing_json_gate()
            if private_preview.project_session_id != self._project_session_id:
                raise EditorControllerError(
                    "PREPROCESS.STALE_PROJECT_SESSION"
                )
            if private_preview.base_revision != self._project_revision:
                raise EditorControllerError("PREPROCESS.STALE_REVISION")
            if not private_preview.changes:
                raise EditorControllerError("PREPROCESS.PREVIEW_INVALID")

            segments = list(self.project.segments)
            seen_indices: set[int] = set()
            for change in private_preview.changes:
                if type(change) is not PreprocessChange:
                    raise EditorControllerError(
                        "PREPROCESS.PREVIEW_INVALID"
                    )
                index = change.segment_index
                if index in seen_indices or index >= len(segments):
                    raise EditorControllerError(
                        "PREPROCESS.PREVIEW_INVALID"
                    )
                seen_indices.add(index)
                current = segments[index]
                if (
                    current.id != change.segment_id
                    or current.target != change.before_target
                    or current.confirmed != change.before_confirmed
                ):
                    raise EditorControllerError("PREPROCESS.STALE_SEGMENT")

            dirty_before = self._dirty
            baseline = self._saved_project_digest
            if baseline is None:
                raise EditorControllerError("PREPROCESS.BASELINE_UNAVAILABLE")
            for change in private_preview.changes:
                current = segments[change.segment_index]
                segments[change.segment_index] = replace(
                    current,
                    target=change.after_target,
                    confirmed=False,
                )

            self._project = replace(self.project, segments=tuple(segments))
            self._project_revision += 1
            self._recompute_project_dirty()
            self._batch_undo_state = BatchUndoState(
                project_session_id=self._project_session_id,
                applied_revision=self._project_revision,
                dirty_before=dirty_before,
                saved_baseline_digest_at_apply=baseline,
                changes=tuple(
                    replace(change) for change in private_preview.changes
                ),
            )
            report = BatchOperationReport(
                operation="apply",
                project_session_id=self._project_session_id,
                resulting_revision=self._project_revision,
                changed_segment_ids=tuple(
                    change.segment_id for change in private_preview.changes
                ),
                dirty=self._dirty,
            )
            report.__post_init__()
            return report

    def undo_latest_preprocessing(self) -> BatchOperationReport:
        """Undo the latest batch if every affected segment remains unchanged."""

        with self._tm_query_lock:
            self._require_preprocessing_json_gate()
            state = self._batch_undo_state
            if state is None:
                raise EditorControllerError("PREPROCESS.NO_UNDO")
            try:
                state.__post_init__()
            except (TypeError, ValueError) as error:
                raise EditorControllerError("PREPROCESS.UNDO_INVALID") from error
            if state.project_session_id != self._project_session_id:
                raise EditorControllerError(
                    "PREPROCESS.STALE_UNDO_SESSION"
                )

            segments = list(self.project.segments)
            seen_indices: set[int] = set()
            for change in state.changes:
                index = change.segment_index
                if index in seen_indices or index >= len(segments):
                    raise EditorControllerError("PREPROCESS.STALE_UNDO")
                seen_indices.add(index)
                current = segments[index]
                if (
                    current.id != change.segment_id
                    or current.target != change.after_target
                    or current.confirmed != change.after_confirmed
                ):
                    raise EditorControllerError("PREPROCESS.STALE_UNDO")

            for change in state.changes:
                current = segments[change.segment_index]
                segments[change.segment_index] = replace(
                    current,
                    target=change.before_target,
                    confirmed=change.before_confirmed,
                )
            self._project = replace(self.project, segments=tuple(segments))
            self._project_revision += 1
            self._batch_undo_state = None
            self._recompute_project_dirty()
            report = BatchOperationReport(
                operation="undo",
                project_session_id=self._project_session_id,
                resulting_revision=self._project_revision,
                changed_segment_ids=tuple(
                    change.segment_id for change in state.changes
                ),
                dirty=self._dirty,
            )
            report.__post_init__()
            return report

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

        with self._tm_query_lock:
            try:
                saved_path = save_project_file(self.project, path)
            except ProjectError as exc:
                raise EditorControllerError("PROJECT.SAVE_FAILED") from exc
            self._project = replace(self.project, path=saved_path)
            self._saved_project_digest = _project_content_digest(self.project)
            self._dirty = False
            self._remember_current_position()
            return self.project

    def close_project(self) -> None:
        """Leave the current project after the frontend has handled unsaved changes."""

        with self._tm_query_lock:
            self._retire_rpy_session()
            self._discard_pending_file_opens()
            if self._project is not None:
                self._remember_current_position()
            else:
                self._remember_current_workspace_position()
                if self._workspace_service is not None:
                    self._notify_workspace_chunk_closed()
            self._project = None
            self._workspace_service = None
            self._workspace_save_service = None
            self._workspace_persistence_binding = None
            self._workspace_recovery_target = None
            self._workspace_flat_segments = ()
            self._workspace_global_index = 0
            self._workspace_session_view = None
            self._current_index = 0
            self._dirty = False
            self._project_session_id = uuid4().hex
            self._project_revision = 0
            self._saved_project_digest = None
            self._batch_undo_state = None
            self._clear_project_search_state()
            self._clear_workspace_search_state()
            self._issued_workspace_reconciliation = None
            self._issued_workspace_package_import = None
            self._advance_tm_query_epoch()
            self._observed_tm_signature = None

    def recent_projects(self) -> tuple[RecentProject, ...]:
        """Return locally remembered projects in most-recent-first order."""

        return self.workspace_state.recent_projects()

    def recent_workspace_projects(self) -> tuple[RecentWorkspaceProject, ...]:
        """Return device-local composite ProjectPackage positions."""

        return self.workspace_state.recent_workspace_projects()

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

    def tm_retrieval_status(self) -> RetrievalDisplayState:
        """Return one fresh generation-bound projection without querying."""

        with self._tm_query_lock:
            self._synchronize_tm_query_state(refresh_current=False)
            blocked_code = self._tm_runtime_blocked_safe_code
            if blocked_code is not None:
                return RetrievalDisplayState(
                    context_available=False,
                    fuzzy_available=False,
                    safe_codes=(blocked_code,),
                )
            adapter = self._tm_adapter
            if adapter is None:
                return RetrievalDisplayState(
                    context_available=False,
                    fuzzy_available=False,
                    safe_codes=("TM.RETRIEVAL.UNAVAILABLE",),
                )
            status = self._inspect_tm_retrieval_status_no_query(adapter)
            if type(status) is not RetrievalDisplayState:
                raise TypeError("TM retrieval display contract is invalid")
            status.__post_init__()
            return replace(status)

    def poll_tm_threshold_display(self) -> TMThresholdDisplay | None:
        """Try one presentation read; a busy publication is retried by the UI."""

        if not self._tm_query_lock.acquire(False):
            return None
        try:
            adapter = self._tm_adapter
            if adapter is None:
                validation = FuzzyValidationDisplay(
                    FuzzyValidationState.IDLE, None,
                )
                display = RetrievalDisplayState(
                    False, False, ("TM.RETRIEVAL.UNAVAILABLE",),
                )
            else:
                pair = adapter._poll_fuzzy_display_for_controller()
                if pair is None:
                    return None
                validation, display = pair
            blocked = self._tm_runtime_blocked_safe_code
            if blocked is not None:
                display = RetrievalDisplayState(False, False, (blocked,))
            return TMThresholdDisplay(self.tm_preferences(), display, validation)
        finally:
            self._tm_query_lock.release()

    def tm_fuzzy_validation_status(self) -> FuzzyValidationDisplay:
        """Return process-local validation lifecycle without authorizing fuzzy."""

        with self._tm_query_lock:
            adapter = self._tm_adapter
            if adapter is None:
                return FuzzyValidationDisplay(
                    state=FuzzyValidationState.IDLE,
                    safe_code=None,
                )
            status = adapter._inspect_fuzzy_validation_for_controller()
            if type(status) is not FuzzyValidationDisplay:
                raise TypeError("fuzzy validation display contract is invalid")
            status.__post_init__()
            return replace(status)

    def revalidate_tm_fuzzy(self) -> FuzzyValidationDisplay:
        """Explicitly start the real device qualification without granting it."""

        with self._tm_query_lock:
            adapter = self._tm_adapter
            if adapter is None:
                raise EditorControllerError("TM.FUZZY.REVALIDATION_UNAVAILABLE")
            status = adapter._start_fuzzy_validation_for_controller()
            if type(status) is not FuzzyValidationDisplay:
                raise TypeError("fuzzy validation display contract is invalid")
            status.__post_init__()
            return replace(status)

    def _inspect_tm_retrieval_status_no_query(
        self,
        adapter: EditorTMAdapter,
    ) -> RetrievalDisplayState:
        """Read one host display and invalidate any differently-issued report."""

        generation, status = (
            adapter._inspect_retrieval_projection_for_controller()
        )
        if type(generation) is not int or generation < 0:
            raise ValueError("TM retrieval display generation is invalid")
        if type(status) is not RetrievalDisplayState:
            raise TypeError("TM retrieval display contract is invalid")
        status.__post_init__()
        observed = self._observed_tm_signature
        if observed is not None and observed[4] != generation:
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
        return replace(status)

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
                if self._tm_adapter is not None and (
                    self._project is not None or self._workspace_service is not None
                ):
                    try:
                        self._require_tm_runtime_available()
                        if self._current_tm_report is None:
                            _ = self._query_and_issue_current_tm_report()
                    except EditorControllerError:
                        return TMThresholdUpdateOutcome(
                            succeeded=False,
                            preferences=previous,
                            safe_code="TM.THRESHOLD.REFRESH_FAILED",
                        )
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
            if self._tm_adapter is not None and (
                self._project is not None or self._workspace_service is not None
            ):
                try:
                    self._require_tm_runtime_available()
                    _ = self._query_and_issue_current_tm_report()
                except EditorControllerError:
                    return TMThresholdUpdateOutcome(
                        succeeded=False,
                        preferences=TMPreferences(
                            minimum_similarity=(
                                persisted.minimum_similarity
                            ),
                            result_limit=persisted.result_limit,
                        ),
                        safe_code="TM.THRESHOLD.REFRESH_FAILED",
                    )
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

    def current_tm_suggestion_report(self) -> TMSuggestionReport | None:
        """Return the already-issued report without resolving or querying again.

        Resource mutations rebuild the runtime and issue the current report before
        returning.  Presentation code can use this defensive snapshot to repaint
        after that mutation without repeating the expensive Windows authority
        inspection on the GUI thread.
        """

        with self._tm_query_lock:
            report = self._current_tm_report
            return None if report is None else _clone_tm_suggestion_report(report)

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

    def term_suggestions(self) -> tuple[TermSuggestion, ...]:
        """Return current-segment term suggestions without issuing a TM query."""

        with self._tm_query_lock:
            source = self.current_segment.source
            return self._term_suggestions(source)

    def _legacy_suggestions(self) -> SuggestionBundle:
        """Query every currently active Lookup resource for the current source."""

        segment = self.current_segment
        source = segment.source
        tm_matches: list[LegacyExactTMSuggestion] = []
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
        return SuggestionBundle(
            tm_matches=tuple(tm_matches),
            terms=self._term_suggestions(source),
        )

    def _term_suggestions(self, source: str) -> tuple[TermSuggestion, ...]:
        adapter = self._tm_adapter
        if adapter is None:
            return self._extract_term_suggestions(source)

        for _attempt in range(2):
            self._synchronize_term_matcher_cohort()
            handoff = self._term_matcher_handoff
            if handoff is None:
                raise AssertionError(
                    "configured term cohort lost its matcher handoff"
                )
            try:
                return adapter._run_if_text_matcher_handoff_current_for_controller(
                    handoff,
                    lambda: self._extract_term_suggestions(source),
                )
            except _TMMatcherGenerationChanged:
                continue
        raise EditorControllerError("TERM.MATCHER_GENERATION_CHANGED")

    def _extract_term_suggestions(
        self,
        source: str,
    ) -> tuple[TermSuggestion, ...]:
        """Extract from one already-published immutable term cohort."""

        terms: list[TermSuggestion] = []
        for resource in self.repository.list_resources():
            if (
                not resource.active
                or not resource.lookup
                or resource.kind is not ResourceKind.TERMBASE
            ):
                continue
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
        return tuple(terms)

    def _synchronize_term_matcher_cohort(self) -> None:
        """Lazily replace the complete term graph after capability drift."""

        adapter = self._tm_adapter
        if adapter is None:
            return
        current = adapter._text_matcher_handoff_for_controller()
        if self._term_matcher_handoff is current:
            return
        configs = self.repository.list_resources()
        try:
            engines, records, handoff = self._build_term_engine_sets(configs)
        except (OSError, UnicodeError, ValueError) as error:
            raise EditorControllerError("TERM.COHORT_REBUILD_FAILED") from error
        if handoff is None:
            raise AssertionError("term candidate did not retain a host handoff")

        try:
            adapter._run_if_text_matcher_handoff_current_for_controller(
                handoff,
                lambda: self._publish_term_engine_graph(
                    engines,
                    records,
                    handoff,
                ),
            )
        except _TMMatcherGenerationChanged:
            return

    def _publish_term_engine_graph(
        self,
        engines: dict[str, _TermEngine],
        records: dict[str, tuple[TermRecord, ...]],
        handoff: MatcherHandoffSnapshot | None,
    ) -> None:
        """Swap one fully built engine/record graph without later allocation."""

        self._glossary_engines = engines
        self._term_record_snapshots = records
        self._term_matcher_handoff = handoff

    def confirm_current(
        self,
    ) -> ConfirmResult | ControllerWorkspaceConfirmResult:
        """Confirm the segment, writing nonblank translations to writable TMs."""

        with self._tm_query_lock:
            self._require_tm_runtime_available()
            current = self.current_segment
            workspace_mode = self._workspace_service is not None
            workspace_edit_preparation: object | None = None
            workspace_identity: SegmentIdentity | None = None
            workspace_service: ProjectWorkspaceService | None = None
            if workspace_mode:
                workspace_service = self._require_workspace_session()
                workspace_identity = (
                    self.current_workspace_identity.segment_identity
                )
                workspace_edit_preparation = self._prepare_workspace_chunk_edit(
                    workspace_service,
                    workspace_identity,
                    target=current.target,
                    confirmed=True,
                )
            file_source = (
                self._workspace_contract.name if workspace_mode else self.project.name
            )
            adapter = self._tm_adapter

            def publish_resources() -> WriteReport:
                if not current.target.strip():
                    return WriteReport()
                if adapter is not None:
                    published = adapter.append_confirmed(
                        segment=current,
                        target=current.target,
                        file_source=file_source,
                    )
                    if type(published) is not WriteReport:
                        raise TypeError("TM adapter must return WriteReport")
                    published.__post_init__()
                    if not published.outcomes and (
                        published.written_resource_ids or published.errors
                    ):
                        raise ValueError(
                            "TM adapter write report must retain structured outcomes"
                        )
                    return published
                unit = SourceUnit(
                    id=current.id,
                    text=current.source,
                    speaker=current.speaker or None,
                    file_source=file_source,
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
                return WriteReport(
                    written_resource_ids=tuple(written),
                    errors=tuple(errors),
                )

            workspace_receipt: object | None = None
            try:
                if workspace_mode:
                    assert workspace_service is not None
                    assert workspace_identity is not None
                    report, workspace_receipt = (
                        self._commit_workspace_chunk_confirmed_edit(
                            workspace_edit_preparation,
                            workspace_service,
                            workspace_identity,
                            target=current.target,
                            publish_resources=publish_resources,
                        )
                    )
                else:
                    report = publish_resources()
            except BaseException:
                self._cancel_workspace_chunk_edit(workspace_edit_preparation)
                raise

            if not report.succeeded:
                self._cancel_workspace_chunk_edit(workspace_edit_preparation)
                if workspace_mode:
                    return ControllerWorkspaceConfirmResult(
                        session=self.workspace_view,
                        current_identity=self.current_workspace_identity,
                        current_global_index=self._workspace_global_index,
                        write_report=report,
                    )
                return ConfirmResult(
                    project=self.project,
                    current_index=self._current_index,
                    write_report=report,
                )

            if workspace_mode:
                assert workspace_service is not None
                assert workspace_identity is not None
                receipt = workspace_receipt
                if receipt is None:
                    raise EditorControllerError("CHUNK.COMMIT_FAILED")
                if receipt.changed:
                    self._project_revision = receipt.resulting_revision
                    self._refresh_workspace_projection(workspace_identity)
                next_index = next(
                    (
                        index
                        for index in range(
                            self._workspace_global_index + 1,
                            len(self._workspace_flat_segments),
                        )
                        if not self._workspace_flat_segments[index].segment.confirmed
                    ),
                    self._workspace_global_index,
                )
                self._workspace_global_index = next_index
                self._reissue_workspace_session_view()
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
                self._remember_current_workspace_position()
                return ControllerWorkspaceConfirmResult(
                    session=self.workspace_view,
                    current_identity=self.current_workspace_identity,
                    current_global_index=next_index,
                    write_report=report,
                )

            segments = list(self.project.segments)
            segments[self._current_index] = replace(current, confirmed=True)
            self._project = replace(self.project, segments=tuple(segments))
            if not current.confirmed:
                self._project_revision += 1
            self._recompute_project_dirty()
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
            action = "INITIAL"
            try:
                preflight = service.preflight(config.path)
            except MigrationPreflightError as error:
                if error.error_code != "MIGRATION.SIDECAR_NOT_REUSABLE":
                    raise EditorControllerError(error.error_code) from error
                try:
                    preflight = service.preflight_pending_recovery(config.path)
                except MigrationPreflightError as recovery_error:
                    raise EditorControllerError(
                        recovery_error.error_code
                    ) from recovery_error
                action = "RECOVERY"
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
                action=action,
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
            self._tm_activation_workers[operation.operation_id] = worker
            try:
                worker.start()
            except BaseException:
                self._tm_activation_workers.pop(operation.operation_id, None)
                self._tm_activation_operation = None
                raise
            return replace(operation)

    def reattest_tm_resource(
        self,
        resource_id: str,
    ) -> TMActivationOperationView:
        """Start one explicit same-generation canonical re-attestation."""

        if type(resource_id) is not str or not resource_id.strip():
            raise EditorControllerError(
                "TM re-attestation resource id is required"
            )
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
                    "TM re-attestation resource is unavailable"
                ) from error
            if config.kind is not ResourceKind.TRANSLATION_MEMORY:
                raise EditorControllerError(
                    "TM re-attestation requires a translation memory resource"
                )
            try:
                service = _tm_reattestation_service(config)
            except (OSError, UnicodeError, ValueError) as error:
                raise EditorControllerError(
                    "TM.RUNTIME.CANONICAL_REATTESTATION_NOT_APPLICABLE"
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
                    "action": "REATTEST",
                },
                name=f"localcat-tm-reattest-{operation.operation_id[:8]}",
                daemon=True,
            )
            self._tm_activation_workers[operation.operation_id] = worker
            try:
                worker.start()
            except BaseException:
                self._tm_activation_workers.pop(operation.operation_id, None)
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
                if prepared.action == "RECOVERY":
                    current_preflight = (
                        prepared.service.preflight_pending_recovery(
                            current_config.path
                        )
                    )
                else:
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
                    "action": prepared.action,
                },
                name=f"localcat-tm-activation-{operation.operation_id[:8]}",
                daemon=True,
            )
            self._tm_activation_workers[operation.operation_id] = worker
            try:
                worker.start()
            except BaseException:
                self._tm_activation_workers.pop(operation.operation_id, None)
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
            if action in {"INITIAL", "RECOVERY"}:
                candidate = service.activate_initial(source, resource_id)
            elif action == "REBUILD":
                candidate = service.rebuild_from_snapshot(source, resource_id)
            elif action == "REATTEST":
                candidate = service.reattest_completed_authority(
                    source,
                    resource_id,
                )
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
                    target_engine = self._load_tm_engine(
                        target_config.path,
                        target_config.id,
                    )
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
                            else self._load_tm_engine(config.path, config.id)
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

    def tm_resource_statuses(self) -> tuple[TMResourceStatus, ...]:
        """Return freshly resolved lifecycle facts with query authority."""

        return self._tm_resource_statuses(refresh_lifecycle=True)

    def current_tm_resource_statuses(self) -> tuple[TMResourceStatus, ...]:
        """Return the published runtime projection without filesystem re-resolution."""

        return self._tm_resource_statuses(refresh_lifecycle=False)

    def _tm_resource_statuses(
        self,
        *,
        refresh_lifecycle: bool,
    ) -> tuple[TMResourceStatus, ...]:
        """Project lifecycle/query state, optionally re-resolving external facts."""

        operation = self.tm_activation_operation()
        activation_in_progress = (
            operation is not None and not operation.completed
        )
        with self._tm_query_lock:
            self._synchronize_tm_query_state(refresh_current=False)
            configs = self.repository.list_resources()
            adapter = self._tm_adapter
            if adapter is None:
                from tm_application_composition import TMResourceResolver

                runtime = TMResourceResolver().resolve(configs)
                runtime.__post_init__()
                statuses = tuple(replace(status) for status in runtime.statuses)
                retrieval_status = RetrievalDisplayState(
                    context_available=False,
                    fuzzy_available=False,
                    safe_codes=("TM.RETRIEVAL.UNAVAILABLE",),
                )
            else:
                if activation_in_progress or not refresh_lifecycle:
                    runtime = adapter._capture_runtime_for_controller(configs)
                    statuses = tuple(
                        replace(status) for status in runtime.statuses
                    )
                else:
                    statuses = adapter._inspect_resource_statuses_for_controller(
                        configs
                    )
                retrieval_status = self._inspect_tm_retrieval_status_no_query(
                    adapter
                )
            retrieval_status.__post_init__()
            for status in statuses:
                status.__post_init__()

            if not activation_in_progress and refresh_lifecycle:
                configs_by_id = {config.id: config for config in configs}
                classified: list[TMResourceStatus] = []
                for status in statuses:
                    if not (
                        status.mode is TMResourceDisplayMode.UNAVAILABLE
                        and status.safe_codes
                        == ("TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE",)
                    ):
                        classified.append(status)
                        continue
                    config = configs_by_id.get(status.resource_id)
                    if config is None:
                        classified.append(status)
                        continue
                    service = _initial_tm_activation_service(config)
                    try:
                        service.preflight_pending_recovery(config.path)
                    except MigrationPreflightError as error:
                        if error.error_code != "MIGRATION.RECOVERY_NOT_APPLICABLE":
                            raise
                        classified.append(status)
                    else:
                        classified.append(
                            TMResourceStatus(
                                resource_id=status.resource_id,
                                resource_name=status.resource_name,
                                mode=TMResourceDisplayMode.UNAVAILABLE,
                                exact_available=False,
                                context_available=False,
                                fuzzy_available=False,
                                safe_codes=(
                                    "TM.RUNTIME.CANONICAL_RECOVERY_REQUIRED",
                                ),
                                retryable=True,
                            )
                        )
                statuses = tuple(classified)

            current_report = self._current_tm_report
            if current_report is not None:
                current_report.__post_init__()
                projected = tuple(
                    replace(status)
                    for status in current_report.resource_statuses
                )
                base_identity = tuple(
                    (status.resource_id, status.resource_name)
                    for status in statuses
                )
                projected_identity = tuple(
                    (status.resource_id, status.resource_name)
                    for status in projected
                )
                if projected_identity != base_identity:
                    raise ValueError(
                        "current TM resource statuses drifted from runtime"
                    )
                projected_by_resource_id = {
                    status.resource_id: status for status in projected
                }
            else:
                projected_by_resource_id = {}

            statuses = tuple(
                _merge_tm_lifecycle_and_query_status(
                    lifecycle=status,
                    retrieval=retrieval_status,
                    query=projected_by_resource_id.get(status.resource_id),
                )
                for status in statuses
            )

            blocked_code = self._tm_runtime_blocked_safe_code
            if blocked_code is not None:
                statuses = tuple(
                    TMResourceStatus(
                        resource_id=status.resource_id,
                        resource_name=status.resource_name,
                        mode=TMResourceDisplayMode.UNAVAILABLE,
                        exact_available=False,
                        context_available=False,
                        fuzzy_available=False,
                        safe_codes=(blocked_code,),
                        retryable=True,
                    )
                    for status in statuses
                )

            for status in statuses:
                status.__post_init__()
            return tuple(replace(status) for status in statuses)

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
        deadline = None if timeout is None else monotonic() + timeout
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
            worker = self._tm_activation_workers.get(operation_id)
        if result.completed and worker is not None:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - monotonic())
            )
            worker.join(remaining)
            if worker.is_alive():
                return replace(
                    result,
                    phase="ACTIVATING",
                    completed=False,
                    succeeded=False,
                    safe_code=None,
                    retryable=False,
                )
        with self._tm_activation_condition:
            current = self._tm_activation_operation
            if current is None or current.operation_id != operation_id:
                raise EditorControllerError("TM activation operation is unknown")
            result = replace(current)
            if worker is not None and not worker.is_alive():
                retained = self._tm_activation_workers.get(operation_id)
                if retained is worker:
                    self._tm_activation_workers.pop(operation_id, None)
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

    def _recompute_project_dirty(self) -> None:
        """Compare current canonical content with the latest saved baseline."""

        baseline = self._saved_project_digest
        if baseline is None:
            raise EditorControllerError("PROJECT.BASELINE_UNAVAILABLE")
        self._dirty = _project_content_digest(self.project) != baseline

    def _require_workspace_session(self) -> ProjectWorkspaceService:
        service = self._workspace_service
        if (
            service is None
            or not self._workspace_flat_segments
            or service.session_id != self._project_session_id
            or service.revision != self._project_revision
        ):
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SESSION")
        return service

    def _workspace_owner_for_chunk_controller(self) -> ProjectWorkspaceService:
        """Return the exact live owner to the installed composition adapter."""

        self._ensure_workspace_writable()
        return self._require_workspace_session()

    def _install_workspace_chunk_edit_gate(self, gate: object) -> None:
        """Install one private prepare/commit gate without importing Chunk."""

        if not all(
            callable(getattr(gate, name, None))
            for name in (
                "prepare_segment_edit",
                "commit_segment_edit",
                "commit_confirmed_edit",
                "cancel_segment_edit",
            )
        ):
            raise TypeError("workspace chunk edit gate is incomplete")
        current = self._workspace_chunk_edit_gate
        if current is not None and current is not gate:
            raise EditorControllerError("CHUNK.CONTROLLER_GATE_ALREADY_INSTALLED")
        self._workspace_chunk_edit_gate = gate

    def _notify_workspace_chunk_opened(self) -> object | None:
        """Notify an optional application gate after a Workspace is installed."""

        gate = self._workspace_chunk_edit_gate
        callback = None if gate is None else getattr(gate, "workspace_opened", None)
        if callable(callback):
            try:
                return callback()
            except BaseException:
                recovery = getattr(gate, "workspace_open_failed", None)
                if callable(recovery):
                    try:
                        recovery()
                    except BaseException:
                        pass
        return None

    def _notify_workspace_chunk_closed(self) -> None:
        """Revoke optional Chunk application state before dropping its owner."""

        gate = self._workspace_chunk_edit_gate
        callback = None if gate is None else getattr(gate, "workspace_closed", None)
        if callable(callback):
            callback()

    def _capture_workspace_chunk_transition(
        self,
        workspace_owner: ProjectWorkspaceService,
        projection: object,
    ) -> object | None:
        """Pass one owner-issued transition through the optional Chunk seam."""

        gate = self._workspace_chunk_edit_gate
        callback = (
            None
            if gate is None
            else getattr(gate, "capture_workspace_transition", None)
        )
        if callable(callback):
            return callback(workspace_owner, projection)
        return None

    def _mark_workspace_chunk_transition_capture_failed(
        self,
        workspace_owner: ProjectWorkspaceService,
    ) -> None:
        """Tell the optional Chunk gate to fail closed after publication."""

        gate = self._workspace_chunk_edit_gate
        callback = (
            None
            if gate is None
            else getattr(gate, "workspace_transition_capture_failed", None)
        )
        if callable(callback):
            try:
                callback(workspace_owner)
            except BaseException:
                # Workspace publication is already durable and cannot be rolled
                # back.  Candidate installation below remains mandatory.
                pass

    def _validate_workspace_chunk_replacement(
        self,
        candidate_owner: ProjectWorkspaceService,
    ) -> None:
        """Let the optional Chunk gate reject a swap before package publish."""

        gate = self._workspace_chunk_edit_gate
        callback = (
            None
            if gate is None
            else getattr(gate, "validate_workspace_replacement", None)
        )
        if not callable(callback):
            return
        try:
            callback(candidate_owner)
        except EditorControllerError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            raise EditorControllerError(
                code if type(code) is str and code else "CHUNK.RECOVERY_REQUIRED"
            ) from error

    def _prepare_workspace_chunk_edit(
        self,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
    ) -> object | None:
        self._ensure_workspace_writable()
        gate = self._workspace_chunk_edit_gate
        if gate is None:
            return None
        return gate.prepare_segment_edit(
            service,
            identity,
            target=target,
            confirmed=confirmed,
            session_id=self._project_session_id,
            base_revision=self._project_revision,
        )

    def _commit_workspace_chunk_edit(
        self,
        preparation: object | None,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
    ) -> object:
        self._ensure_workspace_writable()
        gate = self._workspace_chunk_edit_gate
        if gate is None:
            return service.update_segment_edit(
                identity,
                target=target,
                confirmed=confirmed,
                session_id=self._project_session_id,
                base_revision=self._project_revision,
            )
        receipt = gate.commit_segment_edit(
            preparation,
            service,
            identity,
            target=target,
            confirmed=confirmed,
            session_id=self._project_session_id,
            base_revision=self._project_revision,
        )
        return receipt

    def _cancel_workspace_chunk_edit(self, preparation: object | None) -> None:
        gate = self._workspace_chunk_edit_gate
        if gate is not None:
            gate.cancel_segment_edit(preparation)

    def _commit_workspace_chunk_confirmed_edit(
        self,
        preparation: object | None,
        service: ProjectWorkspaceService,
        identity: SegmentIdentity,
        *,
        target: str,
        publish_resources: Callable[[], WriteReport],
    ) -> tuple[WriteReport, object | None]:
        """Run resource publication only after the final Chunk revalidation."""

        self._ensure_workspace_writable()
        gate = self._workspace_chunk_edit_gate
        if gate is None:
            report = publish_resources()
            if not report.succeeded:
                return report, None
            receipt = service.update_segment_edit(
                identity,
                target=target,
                confirmed=True,
                session_id=self._project_session_id,
                base_revision=self._project_revision,
            )
            return report, receipt
        result = gate.commit_confirmed_edit(
            preparation,
            service,
            identity,
            target=target,
            session_id=self._project_session_id,
            base_revision=self._project_revision,
            publish_resources=publish_resources,
        )
        if (
            type(result) is not tuple
            or len(result) != 2
            or type(result[0]) is not WriteReport
        ):
            raise EditorControllerError("CHUNK.COMMIT_FAILED")
        return result

    def _require_workspace_save_session(
        self,
    ) -> tuple[
        ProjectWorkspaceService,
        ProjectSaveService,
        ProjectPackagePersistenceBinding,
    ]:
        if self._workspace_recovery_target is not None:
            raise EditorControllerError("PROJECT.SAVE.RECOVERY_REQUIRED")
        service = self._require_workspace_session()
        save_service = self._workspace_save_service
        binding = self._workspace_persistence_binding
        if (
            save_service is None
            or save_service.workspace_service is not service
            or type(binding) is not ProjectPackagePersistenceBinding
            or binding.project_id != service.workspace.project_id
        ):
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
        return service, save_service, binding

    def _build_workspace_session_view(
        self,
        service: ProjectWorkspaceService,
        save_service: ProjectSaveService,
        projection: tuple[FlatProjectSegment, ...],
        *,
        current_index: int,
        generation: int,
    ) -> WorkspaceSessionView:
        if not projection or current_index < 0 or current_index >= len(projection):
            raise ValueError("workspace view requires one current segment")
        project_identity = IssuedProjectIdentity(
            project_id=service.workspace.project_id,
            session_id=service.session_id,
            generation=generation,
            workspace_revision=service.revision,
        )
        progress_by_id = {
            item.document_id: item for item in service.document_progress
        }
        document_identities = {
            document.document_id: IssuedDocumentIdentity(
                project=project_identity,
                document_id=document.document_id,
            )
            for document in service.workspace.documents
        }
        documents = tuple(
            WorkspaceDocumentView(
                identity=document_identities[document.document_id],
                display_name=document.display_name,
                source_ref=document.source_ref,
                order=document.order,
                progress=progress_by_id[document.document_id],
            )
            for document in service.workspace.documents
        )
        segments = tuple(
            WorkspaceSegmentView(
                identity=IssuedSegmentIdentity(
                    document=document_identities[item.document_id],
                    local_segment_id=item.identity.local_segment_id,
                ),
                document_local_index=item.document_local_index,
                project_global_index=item.project_global_index,
                source=item.segment.source,
                target=item.segment.target,
                raw_speaker=item.segment.raw_speaker,
                confirmed=item.segment.confirmed,
            )
            for item in projection
        )
        return WorkspaceSessionView(
            project=project_identity,
            name=service.workspace.name,
            source_locale=service.workspace.source_locale,
            target_locale=service.workspace.target_locale,
            documents=documents,
            segments=segments,
            current_segment=segments[current_index].identity,
            project_progress=service.project_progress,
            save_state=WorkspaceSaveState(
                dirty_document_ids=save_service.dirty_document_ids,
                manifest_dirty=save_service.manifest_dirty,
                project_dirty=save_service.project_dirty,
            ),
        )

    def _prepare_workspace_tm_signature(
        self,
        *,
        session_id: str,
        projection: tuple[FlatProjectSegment, ...],
        current_index: int,
    ) -> tuple[str, str, str, int, int, float, int, str] | None:
        adapter = self._tm_adapter
        if adapter is None:
            return None
        segment = projection[current_index].segment
        editor_segment = EditorSegment(
            id=segment.identity.local_segment_id,
            source=segment.source,
            target=segment.target,
            speaker=segment.raw_speaker,
            confirmed=segment.confirmed,
        )
        preferences = self.workspace_state.tm_preferences()
        try:
            runtime_generation, retrieval_generation = (
                adapter._current_query_generations()
            )
        except ValueError:
            return None
        return self._tm_signature(
            segment=editor_segment,
            preferences=preferences,
            runtime_generation=runtime_generation,
            retrieval_generation=retrieval_generation,
            session_id=session_id,
        )

    def _prepare_workspace_install(
        self,
        save_service: ProjectSaveService,
        *,
        session_id: str,
        generation: int,
        current_index: int,
    ) -> _PreparedWorkspaceInstall:
        if type(save_service) is not ProjectSaveService:
            raise TypeError("workspace save service must be exact")
        service = save_service.workspace_service
        if service.session_id != session_id:
            raise ValueError("workspace session id changed during preparation")
        projection = service.flat_segments
        if not projection or current_index < 0 or current_index >= len(projection):
            raise ValueError("workspace candidate has no selected segment")
        view = self._build_workspace_session_view(
            service,
            save_service,
            projection,
            current_index=current_index,
            generation=generation,
        )
        observed = self._prepare_workspace_tm_signature(
            session_id=session_id,
            projection=projection,
            current_index=current_index,
        )
        return _PreparedWorkspaceInstall(
            service=service,
            save_service=save_service,
            projection=projection,
            current_index=current_index,
            generation=generation,
            session_id=session_id,
            view=view,
            observed_tm_signature=observed,
        )

    def _reissue_workspace_session_view(self) -> None:
        service = self._require_workspace_session()
        save_service = self._workspace_save_service
        if save_service is None or save_service.workspace_service is not service:
            raise EditorControllerError("PROJECT.WORKSPACE.NO_SAVE_BINDING")
        self._workspace_session_view = self._build_workspace_session_view(
            service,
            save_service,
            self._workspace_flat_segments,
            current_index=self._workspace_global_index,
            generation=self._workspace_generation,
        )

    def _install_opened_workspace(
        self,
        opened: OpenedProjectPackage,
        save_service: ProjectSaveService,
        *,
        session_id: str,
    ) -> None:
        if type(opened) is not OpenedProjectPackage:
            raise TypeError("opened package must be exact OpenedProjectPackage")
        if type(save_service) is not ProjectSaveService:
            raise TypeError("workspace save service must be exact ProjectSaveService")
        service = save_service.workspace_service
        if (
            type(service) is not ProjectWorkspaceService
            or service.workspace != opened.workspace
            or service.session_id != session_id
        ):
            raise ValueError("opened workspace session binding is invalid")
        self._install_workspace_services(save_service, opened.persistence_binding,
                                         session_id=session_id)

    def _install_workspace_services(self, save_service, binding, *, session_id):
        service = save_service.workspace_service
        projection = service.flat_segments
        if not projection:
            raise ValueError("opened workspace contains no navigable segments")
        next_generation = self._workspace_generation + 1
        candidate_view = self._build_workspace_session_view(
            service,
            save_service,
            projection,
            current_index=0,
            generation=next_generation,
        )
        previous_state = self.__dict__.copy()
        try:
            self._remember_current_position()
            self._remember_current_workspace_position()
            self._project = None
            self._current_index = 0
            self._dirty = False
            self._saved_project_digest = None
            self._batch_undo_state = None
            self._workspace_service = service
            self._workspace_save_service = save_service
            self._workspace_persistence_binding = binding
            self._workspace_recovery_target = None
            self._workspace_flat_segments = projection
            self._workspace_global_index = 0
            self._workspace_generation = next_generation
            self._workspace_session_view = candidate_view
            self._project_session_id = session_id
            self._project_revision = service.revision
            self._clear_project_search_state()
            self._clear_workspace_search_state()
            self._issued_workspace_reconciliation = None
            self._issued_workspace_package_import = None
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
            if binding is not None:
                self._restore_workspace_position(binding.path)
            self._reissue_workspace_session_view()
            self._remember_current_workspace_position()
            self._notify_workspace_chunk_opened()
            self._retire_rpy_session()
            self._discard_pending_file_opens()
        except BaseException:
            self.__dict__.clear()
            self.__dict__.update(previous_state)
            raise

    def _refresh_workspace_projection(
        self,
        selected_identity: SegmentIdentity,
    ) -> None:
        service = self._require_workspace_session()
        projection = service.flat_segments
        index = next(
            (
                item.project_global_index
                for item in projection
                if item.identity == selected_identity
            ),
            None,
        )
        if index is None:
            raise EditorControllerError("PROJECT.WORKSPACE.IDENTITY_NOT_ISSUED")
        self._workspace_flat_segments = projection
        self._workspace_global_index = index
        self._reissue_workspace_session_view()

    def _set_workspace_global_index(self, index: int) -> None:
        if index != self._workspace_global_index:
            self._workspace_global_index = index
            view = self.workspace_view
            self._workspace_session_view = replace(
                view,
                current_segment=view.segments[index].identity,
            )
            self._advance_tm_query_epoch()
            self._record_current_tm_baseline()
        self._remember_current_workspace_position()

    def _clear_workspace_search_state(self) -> None:
        self._current_workspace_search_report = None
        self._issued_workspace_search_hits = ()
        self._issued_workspace_search_public_hits = ()
        self._issued_workspace_search_context = None

    def _clear_project_search_state(self) -> None:
        """Invalidate only project-search issuance, independent of TM epochs."""

        self._current_project_search_report = None
        self._issued_project_search_hits = ()
        self._issued_project_search_public_hits = ()
        self._issued_project_search_context = None

    def _tm_signature(
        self,
        *,
        segment: EditorSegment,
        preferences: TMPreferences,
        runtime_generation: int,
        retrieval_generation: int,
        session_id: str | None = None,
    ) -> tuple[str, str, str, int, int, float, int, str]:
        return (
            self._project_session_id if session_id is None else session_id,
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
        if self._tm_adapter is None or (
            self._project is None and self._workspace_service is None
        ):
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

        if self._tm_adapter is None or (
            self._project is None and self._workspace_service is None
        ):
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
            self._record_current_tm_baseline()
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
    ) -> EditorProject | WorkspaceSessionView:
        """Commit a target while the issued runtime/retrieval pair is current."""

        adapter = self._tm_adapter
        apply_target = (
            self.update_workspace_target
            if self._workspace_service is not None
            else self.update_target
        )
        if adapter is None:
            return apply_target(target)
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
                operation=lambda: apply_target(target),
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

    def _restore_workspace_position(self, path: Path) -> None:
        remembered = self.workspace_state.find_workspace_project(path)
        if remembered is None:
            legacy = self.workspace_state.find_project(path)
            if legacy is None:
                return
            if (
                legacy.index < len(self._workspace_flat_segments)
                and self._workspace_flat_segments[
                    legacy.index
                ].identity.local_segment_id == legacy.segment_id
            ):
                self._workspace_global_index = legacy.index
            return
        if (
            remembered.project_id != self._workspace_contract.project_id
        ):
            return
        index = next(
            (
                item.project_global_index
                for item in self._workspace_flat_segments
                if item.document_id == remembered.document_id
                and item.identity.local_segment_id == remembered.local_segment_id
            ),
            None,
        )
        if index is not None:
            self._workspace_global_index = index

    def _remember_current_workspace_position(self) -> None:
        binding = self._workspace_persistence_binding
        if (
            self._workspace_service is None
            or type(binding) is not ProjectPackagePersistenceBinding
            or not self._workspace_flat_segments
        ):
            return
        identity = self.current_workspace_identity
        try:
            self.workspace_state.remember_workspace_project(
                binding.path,
                project_id=self._workspace_contract.project_id,
                document_id=identity.document.document_id,
                local_segment_id=identity.local_segment_id,
                index=self._workspace_global_index,
            )
        except WorkspaceStateError as exc:
            LOGGER.warning("Unable to remember workspace position: %s", exc)

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
    ) -> EditorProject | WorkspaceSessionView:
        """Insert one term target at an editor cursor position without confirmation."""

        if not isinstance(suggestion, TermSuggestion):
            raise EditorControllerError("a term suggestion contract is required")
        target = self.current_segment.target
        insertion_point = len(target) if position is None else position
        if insertion_point < 0 or insertion_point > len(target):
            raise EditorControllerError("term insertion position is outside the target text")
        updated = target[:insertion_point] + suggestion.target_term + target[insertion_point:]
        if self._workspace_service is not None:
            return self.update_workspace_target(updated)
        return self.update_target(updated)

    def add_term(self, source: str, target: str) -> ResourceConfig:
        """Compatibility entry routed through the mixed term transaction."""

        if type(source) is not str or type(target) is not str:
            raise TypeError("term source and target must be exact strings")
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
        outcome = self.create_term(
            resource.id,
            TermDraft(source=source, target=target),
        )
        if outcome.state is not TermCommitState.COMMITTED:
            raise EditorControllerError(
                outcome.error_code or "TERM.COMMIT_FAILED"
            )
        return resource

    def list_terms(self, resource_id: str) -> tuple[TermRecord, ...]:
        """Return the immutable LKG snapshot for one writable termbase."""

        with self._tm_query_lock:
            resource = self._require_writable_termbase(resource_id)
            records = self._term_record_snapshots.get(resource.id)
            if records is None:
                raise EditorControllerError("TERM.RUNTIME_UNAVAILABLE")
            try:
                return _clone_term_records(records)
            except ValueError as error:
                raise EditorControllerError("TERM.RUNTIME_INVALID") from error

    def create_term(
        self,
        resource_id: str,
        draft: TermDraft,
    ) -> TermCommitOutcome:
        """Create one v1 row through prepare/build/commit/publication."""

        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        draft.__post_init__()
        return self._mutate_term_resource(
            resource_id,
            lambda path: self._term_store.prepare_create(path, draft),
        )

    def update_term(
        self,
        resource_id: str,
        locator: TermRecordLocator,
        draft: TermDraft,
    ) -> TermCommitOutcome:
        """Update one located row without changing its persisted row kind."""

        if not isinstance(locator, TermRecordLocator):
            raise TypeError("term locator must be a TermRecordLocator")
        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        locator.__post_init__()
        draft.__post_init__()
        return self._mutate_term_resource(
            resource_id,
            lambda path: self._term_store.prepare_update(
                path,
                locator,
                draft,
            ),
        )

    def delete_term(
        self,
        resource_id: str,
        locator: TermRecordLocator,
    ) -> TermCommitOutcome:
        """Delete one located row through the same atomic publication path."""

        if not isinstance(locator, TermRecordLocator):
            raise TypeError("term locator must be a TermRecordLocator")
        locator.__post_init__()
        return self._mutate_term_resource(
            resource_id,
            lambda path: self._term_store.prepare_delete(path, locator),
        )

    def _require_writable_termbase(self, resource_id: str) -> ResourceConfig:
        if type(resource_id) is not str:
            raise TypeError("term resource id must be an exact string")
        if not resource_id.strip():
            raise ValueError("term resource id must not be empty")
        try:
            resource = self.repository.get(resource_id)
        except ResourceError as error:
            raise EditorControllerError("TERM.RESOURCE_UNKNOWN") from error
        if resource.kind is not ResourceKind.TERMBASE:
            raise EditorControllerError("TERM.RESOURCE_KIND_INVALID")
        if not resource.active or not resource.update:
            raise EditorControllerError("TERM.RESOURCE_NOT_WRITABLE")
        return resource

    def _mutate_term_resource(
        self,
        resource_id: str,
        prepare: _PreparedTermOperation,
    ) -> TermCommitOutcome:
        """Publish one complete prebuilt term engine graph after commit only."""

        if not callable(prepare):
            raise TypeError("term prepare operation must be callable")
        with self._tm_query_lock:
            resource = self._require_writable_termbase(resource_id)
            quarantined = self._term_quarantines.get(resource.id)
            if quarantined is not None:
                try:
                    return _clone_indeterminate_term_outcome(quarantined)
                except ValueError as error:
                    raise EditorControllerError(
                        "TERM.QUARANTINE_INVALID"
                    ) from error

            try:
                prepared = prepare(resource.path)
            except TermbaseValidationError as error:
                raise EditorControllerError(error.code) from error
            except (OSError, UnicodeError) as error:
                raise EditorControllerError("TERM.PREPARE_FAILED") from error
            if type(prepared) is not PreparedTermMutation:
                raise TypeError(
                    "term prepare operation must return PreparedTermMutation"
                )
            prepared.__post_init__()

            try:
                candidate_engines, candidate_records, candidate_handoff = (
                    self._build_term_engine_sets(
                        self.repository.list_resources(),
                        candidate_records={
                            resource.id: prepared.candidate_records,
                        },
                    )
                )
            except BaseException as error:
                try:
                    self._term_store.discard(prepared)
                except (OSError, ValueError) as cleanup_error:
                    LOGGER.warning(
                        "Unable to discard failed term candidate for %s: %s",
                        resource.id,
                        type(cleanup_error).__name__,
                    )
                if isinstance(error, (OSError, UnicodeError, ValueError)):
                    raise EditorControllerError(
                        "TERM.CANDIDATE_BUILD_FAILED"
                    ) from error
                raise

            def commit_and_publish() -> TermCommitOutcome:
                outcome = self._term_store.commit(prepared)
                if type(outcome) is not TermCommitOutcome:
                    raise TypeError("term commit must return TermCommitOutcome")
                outcome.__post_init__()
                if outcome.state is TermCommitState.COMMITTED:
                    report = outcome.report
                    if report is None:
                        raise AssertionError(
                            "committed term mutation must contain its report"
                        )
                    if (
                        report.resource_path != resource.path
                        or report.records != prepared.candidate_records
                    ):
                        raise AssertionError(
                            "committed term report changed its prepared candidate"
                        )
                    self._publish_term_engine_graph(
                        candidate_engines,
                        candidate_records,
                        candidate_handoff,
                    )
                    _ = self._term_quarantines.pop(resource.id, None)
                elif outcome.state is TermCommitState.INDETERMINATE:
                    self._term_quarantines[resource.id] = (
                        _clone_indeterminate_term_outcome(outcome)
                    )
                return outcome

            adapter = self._tm_adapter
            try:
                if adapter is None:
                    outcome = commit_and_publish()
                elif candidate_handoff is None:
                    raise AssertionError(
                        "configured term mutation lost its matcher handoff"
                    )
                else:
                    outcome = adapter._run_if_text_matcher_handoff_current_for_controller(
                        candidate_handoff,
                        commit_and_publish,
                    )
            except _TMMatcherGenerationChanged as error:
                try:
                    self._term_store.discard(prepared)
                except (OSError, ValueError) as cleanup_error:
                    LOGGER.warning(
                        "Unable to discard stale term candidate for %s: %s",
                        resource.id,
                        type(cleanup_error).__name__,
                    )
                raise EditorControllerError(
                    "TERM.MATCHER_GENERATION_CHANGED"
                ) from error

            if outcome.state is TermCommitState.COMMITTED:
                cleanup = self._term_store.finalize(prepared, outcome)
                if not cleanup.cleaned:
                    LOGGER.warning(
                        "Committed termbase %s retained recovery artifact: %s",
                        resource.id,
                        cleanup.warning_code,
                    )
                return outcome

            if outcome.state is TermCommitState.INDETERMINATE:
                private_quarantine = self._term_quarantines.get(resource.id)
                if private_quarantine is None:
                    raise AssertionError(
                        "indeterminate term mutation lost quarantine state"
                    )
                try:
                    return _clone_indeterminate_term_outcome(
                        private_quarantine
                    )
                except ValueError as error:
                    raise EditorControllerError(
                        "TERM.QUARANTINE_INVALID"
                    ) from error

            try:
                self._term_store.discard(prepared)
            except (OSError, ValueError) as cleanup_error:
                LOGGER.warning(
                    "Unable to discard non-committed term mutation for %s: %s",
                    resource.id,
                    type(cleanup_error).__name__,
                )
            return outcome

    def list_resources(self) -> tuple[ResourceConfig, ...]:
        """Return the current persistent resource configuration."""

        return self.repository.list_resources()

    def export_resource_direct(
        self,
        resource_id: str,
        destination: Path,
    ) -> ResourceExportOutcome:
        """Export one owner snapshot as its direct JSONL/CSV profile."""

        with self._tm_query_lock:
            try:
                return self._resource_portability.export_direct(
                    resource_id,
                    destination,
                )
            except ResourcePortabilityError as error:
                raise EditorControllerError(error.code) from error

    def export_resource_package(
        self,
        resource_id: str,
        destination: Path,
    ) -> ResourceExportOutcome:
        """Export one configured resource as ResourcePackage."""

        with self._tm_query_lock:
            try:
                return self._resource_portability.export_package(
                    resource_id,
                    destination,
                )
            except ResourcePortabilityError as error:
                raise EditorControllerError(error.code) from error

    def export_tmx_resource_package(
        self,
        resource_id: str,
        destination: Path,
        source_locale: str,
        target_locale: str,
    ) -> ResourceExportOutcome:
        """Export one managed TM through the ResourcePackage TMX profile.

        The TMX handler only supplies deterministic payload and cold proof;
        ResourcePortabilityService remains the package/receipt/recovery owner.
        """

        from tmx_context_contracts import TmxEffectiveLocales
        from tmx_resource_package_handler import TmxResourcePackagePayloadHandler

        try:
            locales = TmxEffectiveLocales(source_locale, target_locale)
        except (TypeError, ValueError) as error:
            raise EditorControllerError("TMX.LOCALE.INVALID") from error
        handler = TmxResourcePackagePayloadHandler(locales)
        service = ResourcePortabilityService(
            self.repository,
            termbase_store=self._term_store,
            tmx_payload_handler=handler,
        )
        with self._tm_query_lock:
            try:
                return service.export_package(
                    resource_id,
                    destination,
                    payload_profile=handler.profile,
                )
            except ResourcePortabilityError as error:
                raise EditorControllerError(error.code) from error

    def validate_resource_package(
        self,
        source: Path,
    ) -> ResourcePackageValidationReport:
        try:
            return self._resource_portability.validate_resource_package(source)
        except ResourcePortabilityError as error:
            raise EditorControllerError(error.code) from error

    @staticmethod
    def resource_package_import_supported(
        report: ResourcePackageValidationReport,
    ) -> bool:
        """Project the package owner's import capability without leaking its enum."""

        if type(report) is not ResourcePackageValidationReport:
            raise TypeError("resource package validation report must be exact")
        return ResourcePortabilityService.import_supported(report)

    def preview_resource_package_import(
        self,
        source: Path,
        mode: ResourceImportMode,
        *,
        destination_resource_id: str | None = None,
        new_resource_name: str | None = None,
    ) -> ResourcePackageImportPreview:
        try:
            return self._resource_portability.preview_resource_package_import(
                source,
                mode,
                destination_resource_id=destination_resource_id,
                new_resource_name=new_resource_name,
            )
        except ResourcePortabilityError as error:
            raise EditorControllerError(error.code) from error

    def cancel_resource_package_import(
        self,
        preview: ResourcePackageImportPreview,
    ) -> None:
        try:
            self._resource_portability.cancel_resource_package_import(preview)
        except ResourcePortabilityError as error:
            raise EditorControllerError(error.code) from error

    def apply_resource_package_import(
        self,
        preview: ResourcePackageImportPreview,
    ) -> ResourcePackageImportResult:
        """Consume one preview and reload exactly the resulting resource graph."""

        with self._tm_query_lock:
            try:
                result = self._resource_portability.apply_resource_package_import(
                    preview
                )
            except ResourcePortabilityError as error:
                raise EditorControllerError(error.code) from error
            try:
                self._reload_resources_after_persisted_mutation()
            except EditorControllerError as error:
                try:
                    self._resource_portability.retain_runtime_recovery(result.receipt)
                except ResourcePortabilityError:
                    pass
                raise EditorControllerError(
                    "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                ) from error
            return result

    def inspect_resource_portability_recovery(
        self,
    ) -> tuple[ResourceRecoveryPreview, ...]:
        try:
            return self._resource_portability.inspect_resource_portability_recovery()
        except ResourcePortabilityError as error:
            raise EditorControllerError(error.code) from error

    def recover_resource_portability(
        self,
        preview: ResourceRecoveryPreview,
        action: ResourceRecoveryAction,
    ) -> ResourceRecoveryOutcome:
        with self._tm_query_lock:
            try:
                outcome = self._resource_portability.recover_resource_portability(
                    preview,
                    action,
                )
            except ResourcePortabilityError as error:
                raise EditorControllerError(error.code) from error
            if outcome.receipt is not None:
                try:
                    self._reload_resources_after_persisted_mutation()
                except EditorControllerError as error:
                    try:
                        self._resource_portability.retain_runtime_recovery(
                            outcome.receipt
                        )
                    except ResourcePortabilityError:
                        pass
                    raise EditorControllerError(
                        "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                    ) from error
            return outcome

    def preview_termbase_import(
        self,
        input_path: Path,
    ) -> TermbaseImportPreview:
        """Preview codec-owned columns without entering TM or Store authority."""

        if not isinstance(input_path, Path):
            raise TypeError("termbase preview input path must be a Path")
        try:
            return preview_termbase_import_file(input_path)
        except ImportFailure as error:
            raise EditorControllerError(str(error)) from error

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
        """Persist resource state and refresh only the graph that changed."""

        with self._tm_query_lock:
            try:
                previous = self.repository.get(resource.id)
                updated = self.repository.update_resource(resource)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            binding_unchanged = (
                previous.id,
                previous.name,
                previous.kind,
                previous.path,
            ) == (
                updated.id,
                updated.name,
                updated.kind,
                updated.path,
            )
            if self._tm_adapter is not None and binding_unchanged:
                self._reload_resource_flags_after_persisted_mutation()
            else:
                self._reload_resources_after_persisted_mutation()
            return updated

    def _reload_resource_flags_after_persisted_mutation(self) -> None:
        """Publish flag-only changes or fail closed after registry persistence."""

        try:
            self._reload_resource_flags()
        except EditorControllerError as error:
            self._latch_persisted_runtime_refresh_failure()
            raise EditorControllerError("TM.RUNTIME.REFRESH_FAILED") from error
        except BaseException:
            self._latch_persisted_runtime_refresh_failure()
            raise

    def _reload_resource_flags(self) -> None:
        """Reuse open TM authorities and cached term records for flag changes."""

        configs = self.repository.list_resources()
        adapter = self._tm_adapter
        if adapter is None:
            self.reload_resources()
            return
        term_candidate: tuple[
            dict[str, _TermEngine],
            dict[str, tuple[TermRecord, ...]],
            MatcherHandoffSnapshot | None,
        ] | None = None

        def validate_candidate(_snapshot: object) -> None:
            nonlocal term_candidate
            term_candidate = self._build_term_engine_sets_for_flag_update(configs)

        try:
            _ = adapter._refresh_runtime_flags(configs, validate_candidate)
            if term_candidate is None:
                raise ValueError("resource flag refresh candidate is missing")
            glossary_engines, term_records, term_handoff = term_candidate
            for attempt in range(2):
                if term_handoff is None:
                    raise AssertionError("configured resource graph lost its handoff")
                try:
                    adapter._run_if_text_matcher_handoff_current_for_controller(
                        term_handoff,
                        lambda: self._publish_term_engine_graph(
                            glossary_engines,
                            term_records,
                            term_handoff,
                        ),
                    )
                    break
                except _TMMatcherGenerationChanged:
                    if attempt == 1:
                        raise ValueError(
                            "matcher generation changed during resource flag refresh"
                        ) from None
                    glossary_engines, term_records, term_handoff = (
                        self._build_term_engine_sets_for_flag_update(configs)
                    )
            if self._project is not None:
                self._advance_tm_query_epoch()
                self._record_current_tm_baseline()
                _ = self._query_and_issue_current_tm_report()
            self._tm_runtime_blocked_safe_code = None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise EditorControllerError(
                f"unable to refresh language resource flags: {exc}"
            ) from exc

    def delete_resource(self, resource_id: str) -> ResourceConfig:
        """Delete one configured resource and remove it from live engine sets."""

        with self._tm_query_lock:
            if resource_id in self._term_quarantines:
                raise EditorControllerError("TERM.RESOURCE_QUARANTINED")
            try:
                deleted = self.repository.delete_resource(resource_id)
            except ResourceError as exc:
                raise EditorControllerError(str(exc)) from exc
            self._tm_engines.pop(resource_id, None)
            self._glossary_engines.pop(resource_id, None)
            self._term_record_snapshots.pop(resource_id, None)
            self._term_quarantines.pop(resource_id, None)
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

        if type(request) is not ImportRequest:
            raise TypeError("resource import request must be ImportRequest")
        request.__post_init__()
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
                    expected_resource_id=resource.id,
                )
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

            try:
                rows, skipped = read_legacy_termbase_import(
                    request.input_path,
                    request.termbase_selection,
                )
            except (
                ImportFailure,
                OSError,
                UnicodeError,
            ) as error:
                return ImportReport(errors=(str(error),))
            try:
                outcome = self._mutate_term_resource(
                    resource.id,
                    lambda path: self._term_store.prepare_merge_legacy(
                        path,
                        rows,
                    ),
                )
            except EditorControllerError as error:
                return ImportReport(
                    skipped=skipped,
                    errors=(str(error),),
                )
            if outcome.state is not TermCommitState.COMMITTED:
                if outcome.state is TermCommitState.INDETERMINATE:
                    recovery_path = outcome.recovery_path
                    safe_detail = outcome.safe_detail
                    if recovery_path is None or safe_detail is None:
                        raise AssertionError(
                            "indeterminate term import lost recovery guidance"
                        )
                    return ImportReport(
                        skipped=skipped,
                        errors=(
                            outcome.error_code or "TERM.COMMIT_INDETERMINATE",
                            f"Recovery file: {recovery_path}",
                            safe_detail,
                        ),
                    )
                return ImportReport(
                    skipped=skipped,
                    errors=(outcome.error_code or "TERM.COMMIT_FAILED",),
                )
            mutation = outcome.report
            if mutation is None:
                raise AssertionError(
                    "committed term import lost its mutation report"
                )
            return ImportReport(
                imported=mutation.imported,
                skipped=skipped,
                overwritten=mutation.overwritten,
            )

    def reload_resources(self, *, _refresh_runtime: bool = True) -> None:
        """Build a complete active engine set before replacing the last known-good set."""

        if type(_refresh_runtime) is not bool:
            raise TypeError("resource runtime refresh flag must be exact bool")
        configs = self.repository.list_resources()
        try:
            with self._tm_query_lock:
                adapter = self._tm_adapter
                if adapter is None:
                    (
                        tm_engines,
                        glossary_engines,
                        term_records,
                        term_handoff,
                    ) = self._build_resource_engine_sets(configs)
                elif not _refresh_runtime:
                    if configs:
                        initial_runtime = adapter._capture_runtime_for_controller(
                            configs
                        )
                        (
                            tm_engines,
                            glossary_engines,
                            term_records,
                            term_handoff,
                        ) = self._build_resource_engine_sets(
                            configs,
                            runtime_snapshot=initial_runtime,
                        )
                    else:
                        (
                            tm_engines,
                            glossary_engines,
                            term_records,
                            term_handoff,
                        ) = self._build_resource_engine_sets(configs)
                else:
                    engine_candidate: tuple[
                        dict[str, TMEngine],
                        dict[str, _TermEngine],
                        dict[str, tuple[TermRecord, ...]],
                        MatcherHandoffSnapshot | None,
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
                    (
                        tm_engines,
                        glossary_engines,
                        term_records,
                        term_handoff,
                    ) = engine_candidate

                def publish_resource_graph() -> None:
                    self._tm_engines = tm_engines
                    self._publish_term_engine_graph(
                        glossary_engines,
                        term_records,
                        term_handoff,
                    )

                if adapter is None:
                    publish_resource_graph()
                else:
                    for attempt in range(2):
                        if term_handoff is None:
                            raise AssertionError(
                                "configured resource graph lost its handoff"
                            )
                        try:
                            adapter._run_if_text_matcher_handoff_current_for_controller(
                                term_handoff,
                                publish_resource_graph,
                            )
                            break
                        except _TMMatcherGenerationChanged:
                            if attempt == 1:
                                raise ValueError(
                                    "matcher generation changed during resource reload"
                                ) from None
                            (
                                glossary_engines,
                                term_records,
                                term_handoff,
                            ) = self._build_term_engine_sets(configs)
                if self._project is not None:
                    self._advance_tm_query_epoch()
                    self._record_current_tm_baseline()
                    if adapter is not None:
                        _ = self._query_and_issue_current_tm_report()
                self._tm_runtime_blocked_safe_code = None
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise EditorControllerError(f"unable to reload language resources: {exc}") from exc

    def _build_resource_engine_sets(
        self,
        configs: tuple[ResourceConfig, ...],
        *,
        runtime_snapshot: object | None = None,
    ) -> tuple[
        dict[str, TMEngine],
        dict[str, _TermEngine],
        dict[str, tuple[TermRecord, ...]],
        MatcherHandoffSnapshot | None,
    ]:
        """Build compatibility engines against one validated runtime cohort."""

        tm_engines = (
            {}
            if runtime_snapshot is None
            else self._build_tm_engine_set_for_runtime_snapshot(
                configs,
                runtime_snapshot,
            )
        )
        (
            glossary_engines,
            term_records,
            term_handoff,
        ) = self._build_term_engine_sets(configs)
        for resource in configs:
            if not resource.active:
                continue
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                if runtime_snapshot is None:
                    tm_engines[resource.id] = self._load_tm_engine(
                        resource.path,
                        resource.id,
                    )
        return tm_engines, glossary_engines, term_records, term_handoff

    def _build_term_engine_sets(
        self,
        configs: tuple[ResourceConfig, ...],
        *,
        candidate_records: dict[str, tuple[TermRecord, ...]] | None = None,
    ) -> tuple[
        dict[str, _TermEngine],
        dict[str, tuple[TermRecord, ...]],
        MatcherHandoffSnapshot | None,
    ]:
        """Build one complete active term cohort from a single Host issue."""

        if type(configs) is not tuple or any(
            type(config) is not ResourceConfig for config in configs
        ):
            raise TypeError("term engine configs must be exact ResourceConfig values")
        overrides = {} if candidate_records is None else candidate_records
        if type(overrides) is not dict:
            raise TypeError("term candidate records must be a dictionary")

        engines: dict[str, _TermEngine] = {}
        snapshots: dict[str, tuple[TermRecord, ...]] = {}
        pending: list[tuple[ResourceConfig, tuple[TermRecord, ...]]] = []
        for resource in configs:
            if resource.kind is not ResourceKind.TERMBASE:
                continue
            if resource.id in self._term_quarantines and resource.id not in overrides:
                lkg_records = self._term_record_snapshots.get(resource.id)
                if lkg_records is None:
                    raise ValueError("quarantined termbase lost its in-memory LKG")
                if resource.active:
                    pending.append((resource, lkg_records))
                continue
            if not resource.active:
                continue

            records = overrides.get(resource.id)
            if records is None:
                records = self._load_glossary_engine(resource.path)
            if type(records) is not tuple or any(
                type(record) is not TermRecord for record in records
            ):
                raise TypeError("term engine records must be exact TermRecord values")
            for record in records:
                record.__post_init__()
            pending.append((resource, records))

        adapter = self._tm_adapter
        handoff: MatcherHandoffSnapshot | None = None
        if adapter is not None:
            handoff = adapter._text_matcher_handoff_for_controller()
            if not adapter._is_current_text_matcher_handoff_for_controller(
                handoff
            ):
                raise ValueError("term matcher handoff is not current-host issued")

        for resource, records in pending:
            snapshots[resource.id] = records
            if handoff is None:
                engines[resource.id] = self._build_legacy_term_engine(
                    records,
                    resource.name,
                )
            else:
                engines[resource.id] = ConfiguredTermAdapter(
                    records,
                    resource.name,
                    handoff,
                )

        if (
            adapter is not None
            and handoff is not None
            and not adapter._is_current_text_matcher_handoff_for_controller(
                handoff
            )
        ):
            raise ValueError("term matcher handoff changed during candidate build")
        return engines, snapshots, handoff

    def _build_term_engine_sets_for_flag_update(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> tuple[
        dict[str, _TermEngine],
        dict[str, tuple[TermRecord, ...]],
        MatcherHandoffSnapshot,
    ]:
        """Reuse the current term graph, loading only a newly active termbase."""

        adapter = self._tm_adapter
        if adapter is None:
            raise AssertionError("term flag projection requires the TM adapter")
        handoff = adapter._text_matcher_handoff_for_controller()
        if not adapter._is_current_text_matcher_handoff_for_controller(handoff):
            raise ValueError("term matcher handoff is not current-host issued")
        if self._term_matcher_handoff is not handoff:
            engines, records, rebuilt_handoff = self._build_term_engine_sets(
                configs,
                candidate_records=dict(self._term_record_snapshots),
            )
            if rebuilt_handoff is None:
                raise AssertionError("term matcher rebuild lost its handoff")
            return engines, records, rebuilt_handoff

        engines = dict(self._glossary_engines)
        snapshots = dict(self._term_record_snapshots)
        for resource in configs:
            if (
                resource.kind is not ResourceKind.TERMBASE
                or not resource.active
                or resource.id in engines
            ):
                continue
            records = snapshots.get(resource.id)
            if resource.id in self._term_quarantines:
                if records is None:
                    raise ValueError("quarantined termbase lost its in-memory LKG")
            elif records is None:
                records = self._load_glossary_engine(resource.path)
            if type(records) is not tuple or any(
                type(record) is not TermRecord for record in records
            ):
                raise TypeError("term engine records must be exact TermRecord values")
            for record in records:
                record.__post_init__()
            snapshots[resource.id] = records
            engines[resource.id] = ConfiguredTermAdapter(
                records,
                resource.name,
                handoff,
            )
        if not adapter._is_current_text_matcher_handoff_for_controller(handoff):
            raise ValueError("term matcher handoff changed during flag projection")
        return engines, snapshots, handoff

    @staticmethod
    def _build_legacy_term_engine(
        records: tuple[TermRecord, ...],
        glossary_source: str,
    ) -> GlossaryEngine:
        """Build the pre-integration legacy preset from validated mixed rows."""

        engine = GlossaryEngine()
        for record in records:
            engine.add_term(
                record.source,
                record.target,
                glossary_source,
            )
        return engine

    def _load_glossary_engine(
        self,
        path: Path,
    ) -> tuple[TermRecord, ...]:
        """Retain the legacy fault seam while strict Store owns parsing."""

        return self._term_store.list_records(path)

    def _build_tm_engine_set_for_runtime_snapshot(
        self,
        configs: tuple[ResourceConfig, ...],
        runtime_snapshot: object,
    ) -> dict[str, TMEngine]:
        """Build only TM compatibility engines before activation publication."""

        from tm_application_composition import TMRuntimeSnapshot, _TMEngineLegacyBackend

        if type(runtime_snapshot) is not TMRuntimeSnapshot:
            raise TypeError("resource runtime candidate must be TMRuntimeSnapshot")
        runtime_snapshot.__post_init__()
        legacy_ports = {port.resource_id: port for port in runtime_snapshot.legacy_ports}
        canonical_ports = {port.resource_id: port for port in runtime_snapshot.canonical_ports}
        tm_engines: dict[str, TMEngine] = {}
        for resource in configs:
            if (
                resource.kind is not ResourceKind.TRANSLATION_MEMORY
                or not resource.active
                or resource.id not in legacy_ports.keys() | canonical_ports.keys()
            ):
                continue
            port = legacy_ports.get(resource.id) or canonical_ports[resource.id]
            if port.path != resource.path:
                raise ValueError("compatibility resource path differs from runtime")
            if resource.id in legacy_ports:
                if type(port.backend) is _TMEngineLegacyBackend:
                    engine = port.backend._compatibility_engine()
                    self._validate_legacy_tm_path(resource.path)
                else:
                    # Custom resolver backends retain the existing compatibility seam.
                    engine = self._load_tm_engine(resource.path, resource.id)
                if engine.canonical_store is not None:
                    raise ValueError(
                        "legacy compatibility engine became canonical"
                    )
            else:
                engine = TMEngine.from_open_canonical_store(
                    port.handle.store,
                    configured_jsonl=resource.path,
                    expected_resource_id=resource.id,
                )
            tm_engines[resource.id] = engine
        return tm_engines

    @staticmethod
    def _load_tm_engine(path: Path, resource_id: str) -> TMEngine:
        if type(resource_id) is not str or not resource_id:
            raise TypeError("translation memory resource id must be a non-empty string")
        engine = TMEngine(str(path), expected_resource_id=resource_id)
        if engine.canonical_store is not None:
            return engine
        EditorController._validate_legacy_tm_path(path)
        return engine

    @staticmethod
    def _validate_legacy_tm_path(path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise ValueError("translation memory does not exist")
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
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


def compose_project_enabled_editor_controller(
    repository: ResourceRepository,
    *,
    tm_adapter: EditorTMAdapter | None = None,
) -> EditorController:
    """Compose Project filesystem ports and configured codecs at the Application edge."""

    from platform_fs import compose_platform_file_backend
    from project_codec_settings import compose_project_codec_runtime

    backend = compose_platform_file_backend(repository.config_dir)
    return EditorController(
        repository,
        tm_adapter=tm_adapter,
        workspace_package_service=ProjectPackageService(backend=backend),
        workspace_file_system=backend,
        project_codec_runtime=compose_project_codec_runtime(repository.config_dir),
    )

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        controller = EditorController(ResourceRepository(Path(temp_dir)))
        controller.load_sample()
        assert controller.current_segment.source
    print("Editor controller self-test passed.")
