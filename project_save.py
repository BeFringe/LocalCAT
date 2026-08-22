"""Carrier-neutral save, baseline, and cold-recovery coordination.

This module owns no ProjectPackage grammar or physical carrier.  A later C2C
adapter supplies the phase port; this coordinator only validates immutable
workspace facts, orders the phases, and publishes body-safe reports.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
import secrets
from typing import Literal, Protocol

from project_workspace import (
    ProjectWorkspaceService,
    project_document_content_digest_v1,
    workspace_content_digest_v1,
    workspace_manifest_digest_v1,
)
from project_workspace_contracts import (
    CodecIdentity,
    EditingOverlayEntry,
    OriginBinding,
    ProjectDocument,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectWorkspace,
    SourcePresence,
)
from project_workspace_identity import (
    ProjectWorkspaceError,
    editing_state_digest_v1,
    validate_document_id,
    validate_project_id,
    validate_sha256,
)


_OPERATION_ID = re.compile(r"save-[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"PROJECT\.SAVE\.[A-Z0-9_]+\Z")


def _fail(code: str) -> None:
    raise ProjectWorkspaceError(code)


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def _exact_tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def _operation_id(value: object) -> str:
    if type(value) is not str or _OPERATION_ID.fullmatch(value) is None:
        _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
    return value


def _safe_code(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


class SaveScope(Enum):
    WORKSPACE = "workspace"
    DOCUMENT = "document"


class DocumentSaveStatus(Enum):
    SAVED = "saved"
    ROLLED_BACK = "rolled_back"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class SaveJournalState(Enum):
    CLEAN = "clean"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class RecoveryAction(Enum):
    COMPLETE_COMMIT = "complete_commit"
    ROLLBACK = "rollback"
    ABANDON_STAGED_COPY = "abandon_staged_copy"


class RecoveryPhase(Enum):
    """Frozen carrier-neutral mutation windows.

    ``STAGING`` records durable intent before a complete candidate is
    guaranteed to exist, so recovery must not read that candidate and may
    only abandon it. ``PUBLISHING`` starts at the first target mutation; its
    carrier adapter must resolve an exact candidate from target/candidate
    facts before the coordinator offers complete or rollback.
    """

    STAGING = "staging"
    STAGED = "staged"
    ARMED = "armed"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    COMMIT_UNCERTAIN = "commit-uncertain"


class OriginWriteState(Enum):
    UNBOUND = "unbound"
    UNSUPPORTED = "unsupported"
    IN_SYNC = "in_sync"
    WORKSPACE_AHEAD = "workspace_ahead"
    SOURCE_DIVERGED = "source_diverged"


class DocumentSourceWriteStatus(Enum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class DocumentOriginWriteState:
    document_id: str
    state: OriginWriteState

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        if type(self.state) is not OriginWriteState:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class DocumentSourceWriteResult:
    document_id: str
    status: DocumentSourceWriteStatus
    safe_code: str | None = None

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        if type(self.status) is not DocumentSourceWriteStatus:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _safe_code(self.safe_code, allow_none=True)
        if self.status is DocumentSourceWriteStatus.UNSUPPORTED:
            if self.safe_code != "PROJECT.SAVE.WRITER_UNAVAILABLE":
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        elif self.safe_code is not None:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceSaveBaseline:
    saved_workspace_snapshot: ProjectWorkspace
    workspace_content_digest: str
    workspace_revision: int
    saved_package_digest: str

    def __post_init__(self) -> None:
        if type(self.saved_workspace_snapshot) is not ProjectWorkspace:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        validate_sha256(self.workspace_content_digest)
        validate_sha256(self.saved_package_digest)
        _exact_nonnegative_int(self.workspace_revision)
        if (
            workspace_content_digest_v1(self.saved_workspace_snapshot)
            != self.workspace_content_digest
        ):
            _fail("PROJECT.SAVE.VALIDATION_FAILED")
        if self.saved_package_digest != self.workspace_content_digest:
            _fail("PROJECT.SAVE.VALIDATION_FAILED")

    @classmethod
    def from_workspace(
        cls,
        saved_workspace_snapshot: ProjectWorkspace,
        *,
        workspace_revision: int,
        saved_package_digest: str | None = None,
    ) -> WorkspaceSaveBaseline:
        if type(saved_workspace_snapshot) is not ProjectWorkspace:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        digest = workspace_content_digest_v1(saved_workspace_snapshot)
        package_digest = digest if saved_package_digest is None else saved_package_digest
        return cls(
            saved_workspace_snapshot=saved_workspace_snapshot,
            workspace_content_digest=digest,
            workspace_revision=workspace_revision,
            saved_package_digest=package_digest,
        )


@dataclass(frozen=True, slots=True)
class DocumentSaveResult:
    document_id: str
    status: DocumentSaveStatus
    before_digest: str | None
    after_digest: str
    safe_code: str | None = None

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        if type(self.status) is not DocumentSaveStatus:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.before_digest is not None:
            validate_sha256(self.before_digest)
        validate_sha256(self.after_digest)
        _safe_code(self.safe_code, allow_none=True)
        if self.status is DocumentSaveStatus.UNCHANGED and self.safe_code is not None:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.status is DocumentSaveStatus.SAVED and self.safe_code not in {
            None,
            "PROJECT.SAVE.RECOVERY_REQUIRED",
        }:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.status in {
            DocumentSaveStatus.ROLLED_BACK,
            DocumentSaveStatus.FAILED,
        } and self.safe_code is None:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectSaveReport:
    operation_id: str
    scope: SaveScope
    origin_kind: ProjectOriginKind
    workspace_revision: int
    workspace_content_digest: str
    requested_count: int
    saved_count: int
    rolled_back_count: int
    unchanged_count: int
    failed_count: int
    document_results: tuple[DocumentSaveResult, ...]
    journal_state: SaveJournalState
    recovery_required: bool
    retryable: bool
    safe_code: str | None = None

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        if type(self.scope) is not SaveScope:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.origin_kind) is not ProjectOriginKind:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _exact_nonnegative_int(self.workspace_revision)
        validate_sha256(self.workspace_content_digest)
        counts = (
            self.requested_count,
            self.saved_count,
            self.rolled_back_count,
            self.unchanged_count,
            self.failed_count,
        )
        for count in counts:
            _exact_nonnegative_int(count)
        _exact_tuple(self.document_results)
        if any(type(item) is not DocumentSaveResult for item in self.document_results):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        document_ids = tuple(item.document_id for item in self.document_results)
        if len(document_ids) != len(set(document_ids)):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if not 0 < self.requested_count <= len(self.document_results):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.scope is SaveScope.WORKSPACE:
            if self.requested_count != len(self.document_results):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        elif self.requested_count != 1:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.saved_count != sum(
            item.status is DocumentSaveStatus.SAVED for item in self.document_results
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.rolled_back_count != sum(
            item.status is DocumentSaveStatus.ROLLED_BACK
            for item in self.document_results
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.unchanged_count != sum(
            item.status is DocumentSaveStatus.UNCHANGED
            for item in self.document_results
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.failed_count != sum(
            item.status is DocumentSaveStatus.FAILED for item in self.document_results
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.journal_state) is not SaveJournalState:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.recovery_required) is not bool or type(self.retryable) is not bool:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _safe_code(self.safe_code, allow_none=True)
        if self.journal_state is SaveJournalState.RECOVERY_REQUIRED:
            if (
                not self.recovery_required
                or not self.retryable
                or self.safe_code != "PROJECT.SAVE.RECOVERY_REQUIRED"
            ):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        elif self.recovery_required:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.journal_state is SaveJournalState.COMMITTED:
            if (
                self.failed_count
                or self.rolled_back_count
                or self.retryable
                or self.safe_code is not None
            ):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.journal_state is SaveJournalState.ROLLED_BACK:
            if (
                not self.rolled_back_count
                or self.saved_count
                or not self.retryable
                or self.safe_code != "PROJECT.SAVE.COMMIT_FAILED"
            ):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.journal_state is SaveJournalState.CLEAN:
            if (
                not self.failed_count
                or self.saved_count
                or self.rolled_back_count
                or not self.retryable
                or self.safe_code is None
            ):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class RecoveryPreview:
    operation_id: str
    project_id: str
    last_known_good_digest: str | None
    candidate_digest: str
    available_actions: tuple[RecoveryAction, ...]
    safe_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        validate_project_id(self.project_id)
        if self.last_known_good_digest is not None:
            validate_sha256(self.last_known_good_digest)
        validate_sha256(self.candidate_digest)
        _exact_tuple(self.available_actions)
        if any(type(action) is not RecoveryAction for action in self.available_actions):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.available_actions not in {
            (),
            (RecoveryAction.ABANDON_STAGED_COPY,),
            (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK),
        }:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _exact_tuple(self.safe_codes)
        for code in self.safe_codes:
            _safe_code(code)
        if self.safe_codes not in {(), ("PROJECT.SAVE.RECOVERY_REQUIRED",)}:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if bool(self.available_actions) == bool(self.safe_codes):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class PendingRecoveryFacts:
    operation_id: str
    project_id: str
    phase: RecoveryPhase
    candidate_digest: str
    last_known_good_digest: str | None

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        validate_project_id(self.project_id)
        if type(self.phase) is not RecoveryPhase:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        validate_sha256(self.candidate_digest)
        if self.last_known_good_digest is not None:
            validate_sha256(self.last_known_good_digest)


@dataclass(frozen=True, slots=True)
class ProjectRecoveryReport:
    operation_id: str
    action: RecoveryAction
    journal_state: SaveJournalState
    workspace_content_digest: str | None
    recovery_required: bool
    retryable: bool
    safe_code: str | None = None

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        if type(self.action) is not RecoveryAction:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.journal_state) is not SaveJournalState:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.workspace_content_digest is not None:
            validate_sha256(self.workspace_content_digest)
        if type(self.recovery_required) is not bool or type(self.retryable) is not bool:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _safe_code(self.safe_code, allow_none=True)
        expected_journal = {
            RecoveryAction.COMPLETE_COMMIT: SaveJournalState.COMMITTED,
            RecoveryAction.ROLLBACK: SaveJournalState.ROLLED_BACK,
            RecoveryAction.ABANDON_STAGED_COPY: SaveJournalState.CLEAN,
        }[self.action]
        if self.journal_state is SaveJournalState.RECOVERY_REQUIRED:
            if (
                not self.recovery_required
                or not self.retryable
                or self.safe_code != "PROJECT.SAVE.RECOVERY_REQUIRED"
            ):
                _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        elif (
            self.journal_state is not expected_journal
            or self.recovery_required
            or self.retryable
            or self.safe_code is not None
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


class ProjectDocumentWriterPort(Protocol):
    """Future live writer seam; persisted capability snapshots cannot satisfy it."""

    codec_identity: CodecIdentity
    format_id: str
    write_mode: Literal["canonical", "source_round_trip"]

    def prepare(
        self,
        document: ProjectDocument,
        origin_binding: OriginBinding,
        codec_private_member: object | None,
    ) -> object: ...


class ProjectWorkspacePersistencePort(Protocol):
    """Carrier-neutral phase port implemented by the later C2C carrier."""

    def stage_candidate(
        self,
        *,
        operation_id: str,
        candidate_workspace: ProjectWorkspace,
        last_known_good_workspace: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
    ) -> object: ...

    def validate_candidate(self, candidate_handle: object) -> None: ...

    def arm_publication(self, candidate_handle: object) -> None: ...

    def publish_candidate(self, candidate_handle: object) -> None: ...

    def readback_candidate(self, candidate_handle: object) -> ProjectWorkspace: ...

    def commit_candidate(self, candidate_handle: object) -> None: ...

    def rollback_candidate(
        self, candidate_handle: object
    ) -> ProjectWorkspace | None: ...

    def inspect_pending_recovery(self) -> object | None: ...

    def describe_pending_recovery(
        self, recovery_handle: object
    ) -> PendingRecoveryFacts: ...

    def read_recovery_last_known_good(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None: ...

    def read_recovery_candidate(self, recovery_handle: object) -> ProjectWorkspace: ...

    def complete_pending_commit(self, recovery_handle: object) -> ProjectWorkspace: ...

    def rollback_pending(self, recovery_handle: object) -> ProjectWorkspace | None: ...

    def abandon_staged_copy(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None: ...


def _resign_document(document: ProjectDocument) -> ProjectDocument:
    overlays = tuple(
        replace(
            overlay,
            saved_state_digest=editing_state_digest_v1(
                overlay.document_id,
                overlay.local_segment_id,
                overlay.source_fingerprint,
                overlay.target,
                overlay.confirmed,
            ),
        )
        for overlay in document.editing_overlay
    )
    return replace(document, editing_overlay=overlays)


def _documents_by_id(workspace: ProjectWorkspace) -> dict[str, ProjectDocument]:
    return {document.document_id: document for document in workspace.documents}


def _workspace_digest_or_none(workspace: ProjectWorkspace | None) -> str | None:
    if workspace is None:
        return None
    if type(workspace) is not ProjectWorkspace:
        _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
    return workspace_content_digest_v1(workspace)


def _workspace_matches(
    observed: ProjectWorkspace | None,
    expected: ProjectWorkspace | None,
) -> bool:
    if observed is None or expected is None:
        return observed is None and expected is None
    return (
        type(observed) is ProjectWorkspace
        and type(expected) is ProjectWorkspace
        and workspace_content_digest_v1(observed)
        == workspace_content_digest_v1(expected)
    )


def _recovery_handle_facts(
    port: ProjectWorkspacePersistencePort,
    handle: object,
) -> PendingRecoveryFacts:
    """Obtain frozen body-safe facts without interpreting an opaque handle."""
    try:
        facts = port.describe_pending_recovery(handle)
    except OSError:
        _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
    if type(facts) is not PendingRecoveryFacts:
        _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
    return facts


def _available_recovery_actions(phase: RecoveryPhase) -> tuple[RecoveryAction, ...]:
    if phase in {
        RecoveryPhase.STAGING,
        RecoveryPhase.STAGED,
        RecoveryPhase.ARMED,
    }:
        return (RecoveryAction.ABANDON_STAGED_COPY,)
    return (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK)


@dataclass(frozen=True, slots=True)
class _ColdRecoveryPlan:
    port: object
    operation_id: str
    project_id: str
    candidate_digest: str
    last_known_good_digest: str | None
    phase: RecoveryPhase
    available_actions: tuple[RecoveryAction, ...]
    proved: bool


class ProjectSaveService:
    """Coordinate one C2A workspace authority with a durable baseline."""

    __slots__ = ("_workspace_service", "_baseline")
    _cold_recovery_plans: dict[int, _ColdRecoveryPlan] = {}

    def __init__(
        self,
        workspace_service: ProjectWorkspaceService,
        *,
        baseline: WorkspaceSaveBaseline | None,
    ) -> None:
        if type(workspace_service) is not ProjectWorkspaceService:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if baseline is not None and type(baseline) is not WorkspaceSaveBaseline:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if baseline is not None:
            if baseline.saved_workspace_snapshot.project_id != workspace_service.workspace.project_id:
                _fail("PROJECT.SAVE.VALIDATION_FAILED")
            if baseline.workspace_revision > workspace_service.revision:
                _fail("PROJECT.SAVE.VALIDATION_FAILED")
        self._workspace_service = workspace_service
        self._baseline = baseline

    @property
    def workspace_service(self) -> ProjectWorkspaceService:
        return self._workspace_service

    @property
    def saved_workspace_snapshot(self) -> ProjectWorkspace | None:
        return None if self._baseline is None else self._baseline.saved_workspace_snapshot

    @property
    def saved_package_digest(self) -> str | None:
        return None if self._baseline is None else self._baseline.saved_package_digest

    @property
    def dirty_document_ids(self) -> tuple[str, ...]:
        current = self._workspace_service.workspace
        if self._baseline is None:
            return tuple(document.document_id for document in current.documents)
        saved_by_id = _documents_by_id(self._baseline.saved_workspace_snapshot)
        return tuple(
            document.document_id
            for document in current.documents
            if document.document_id not in saved_by_id
            or project_document_content_digest_v1(document)
            != project_document_content_digest_v1(saved_by_id[document.document_id])
        )

    @property
    def manifest_dirty(self) -> bool:
        if self._baseline is None:
            return True
        return workspace_manifest_digest_v1(self._workspace_service.workspace) != (
            workspace_manifest_digest_v1(self._baseline.saved_workspace_snapshot)
        )

    @property
    def project_dirty(self) -> bool:
        return self.manifest_dirty or bool(self.dirty_document_ids)

    @property
    def origin_write_state(self) -> tuple[DocumentOriginWriteState, ...]:
        result: list[DocumentOriginWriteState] = []
        dirty = set(self.dirty_document_ids)
        for document in self._workspace_service.workspace.documents:
            if any(
                source.source_presence is SourcePresence.DETACHED
                for source in document.source_segments
            ):
                state = OriginWriteState.UNBOUND
            elif (
                self._workspace_service.workspace.origin.kind
                is ProjectOriginKind.DIRECTORY
                and self._workspace_service.workspace.origin.profile_version
                == "explicit-selected-files-v1"
            ):
                state = OriginWriteState.UNSUPPORTED
            elif document.document_id in dirty:
                state = OriginWriteState.WORKSPACE_AHEAD
            else:
                state = OriginWriteState.UNBOUND
            result.append(DocumentOriginWriteState(document.document_id, state))
        return tuple(result)

    def source_write_back_status(
        self,
        document_id: str,
        writer_port: ProjectDocumentWriterPort | None = None,
    ) -> DocumentSourceWriteResult:
        requested = validate_document_id(document_id)
        document = next(
            (
                item
                for item in self._workspace_service.workspace.documents
                if item.document_id == requested
            ),
            None,
        )
        if document is None:
            _fail("PROJECT.SAVE.WRITER_UNAVAILABLE")
        origin_state = next(
            item.state
            for item in self.origin_write_state
            if item.document_id == requested
        )
        if writer_port is None or origin_state in {
            OriginWriteState.UNBOUND,
            OriginWriteState.UNSUPPORTED,
        } or (
            self._workspace_service.workspace.origin.kind
            is not ProjectOriginKind.SINGLE_FILE
            or self._workspace_service.workspace.persistence_kind
            is not ProjectPersistenceKind.LEGACY_SINGLE_JSON
        ):
            return DocumentSourceWriteResult(
                requested,
                DocumentSourceWriteStatus.UNSUPPORTED,
                "PROJECT.SAVE.WRITER_UNAVAILABLE",
            )
        try:
            codec_identity = writer_port.codec_identity
            format_id = writer_port.format_id
            write_mode = writer_port.write_mode
            prepare = writer_port.prepare
        except AttributeError:
            raise TypeError("writer port does not implement the frozen contract") from None
        if type(codec_identity) is not CodecIdentity or not callable(prepare):
            raise TypeError("writer port does not implement the frozen contract")
        capability = document.writer_capability_snapshot
        available = (
            codec_identity == document.codec_identity
            and type(format_id) is str
            and format_id == document.format_id
            and type(write_mode) is str
            and (
                (write_mode == "canonical" and capability.canonical_write)
                or (
                    write_mode == "source_round_trip"
                    and capability.source_round_trip_write
                )
            )
        )
        if not available:
            return DocumentSourceWriteResult(
                requested,
                DocumentSourceWriteStatus.UNSUPPORTED,
                "PROJECT.SAVE.WRITER_UNAVAILABLE",
            )
        return DocumentSourceWriteResult(
            requested,
            DocumentSourceWriteStatus.AVAILABLE,
            None,
        )

    def write_back_to_source(
        self,
        document_id: str,
        writer_port: ProjectDocumentWriterPort | None = None,
    ) -> DocumentOriginWriteState:
        """ProjectPackage save never implies source-origin write authority.

        C2B exposes the structured reader-only outcome needed by callers while
        leaving actual prepared source publication to a separately approved
        origin adapter.  Merely supplying a live writer port does not widen the
        current explicit-selected-files product boundary.
        """

        requested = validate_document_id(document_id)
        states = {
            item.document_id: item for item in self.origin_write_state
        }
        state = states.get(requested)
        if state is None:
            _fail("PROJECT.SAVE.WRITER_UNAVAILABLE")
        if state.state is OriginWriteState.UNBOUND:
            return state
        return DocumentOriginWriteState(requested, OriginWriteState.UNSUPPORTED)

    def save_workspace(
        self,
        port: ProjectWorkspacePersistencePort,
    ) -> ProjectSaveReport:
        source_session = self._workspace_service.session_id
        source_revision = self._workspace_service.revision
        current = self._workspace_service.workspace
        source_digest = workspace_content_digest_v1(current)
        candidate = replace(
            current,
            documents=tuple(_resign_document(document) for document in current.documents),
        )
        requested = tuple(document.document_id for document in current.documents)
        initially_dirty = set(
            requested
            if self._baseline is None or self.manifest_dirty
            else self.dirty_document_ids
        )
        lkg = None if self._baseline is None else self._baseline.saved_workspace_snapshot
        return self._run_save(
            port,
            scope=SaveScope.WORKSPACE,
            candidate=candidate,
            lkg=lkg,
            requested_document_ids=requested,
            changed_document_ids=initially_dirty,
            source_session=source_session,
            source_revision=source_revision,
            source_workspace=current,
            source_digest=source_digest,
        )

    def save_document(
        self,
        document_id: str,
        port: ProjectWorkspacePersistencePort,
    ) -> ProjectSaveReport:
        selected_id = validate_document_id(document_id)
        if self._baseline is None:
            _fail("PROJECT.SAVE.VALIDATION_FAILED")
        source_session = self._workspace_service.session_id
        source_revision = self._workspace_service.revision
        current = self._workspace_service.workspace
        source_digest = workspace_content_digest_v1(current)
        saved = self._baseline.saved_workspace_snapshot
        current_by_id = _documents_by_id(current)
        saved_by_id = _documents_by_id(saved)
        if set(current_by_id) != set(saved_by_id) or selected_id not in current_by_id:
            _fail("PROJECT.SAVE.VALIDATION_FAILED")
        selected = replace(
            _resign_document(current_by_id[selected_id]),
            order=saved_by_id[selected_id].order,
        )
        candidate = replace(
            saved,
            documents=tuple(
                selected if document.document_id == selected_id else document
                for document in saved.documents
            ),
        )
        changed = {selected_id} if selected_id in set(self.dirty_document_ids) else set()
        return self._run_save(
            port,
            scope=SaveScope.DOCUMENT,
            candidate=candidate,
            lkg=saved,
            requested_document_ids=(selected_id,),
            changed_document_ids=changed,
            source_session=source_session,
            source_revision=source_revision,
            source_workspace=current,
            source_digest=source_digest,
        )

    def _run_save(
        self,
        port: ProjectWorkspacePersistencePort,
        *,
        scope: SaveScope,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
        changed_document_ids: set[str],
        source_session: str,
        source_revision: int,
        source_workspace: ProjectWorkspace,
        source_digest: str,
    ) -> ProjectSaveReport:
        operation_id = "save-" + secrets.token_hex(32)
        initial_session = self._workspace_service.session_id
        initial_revision = self._workspace_service.revision
        initial_digest = self._workspace_service.workspace_content_digest
        candidate_digest = workspace_content_digest_v1(candidate)

        if (
            initial_session != source_session
            or initial_revision != source_revision
            or self._workspace_service.workspace is not source_workspace
            or initial_digest != source_digest
        ):
            return self._failure_report(
                operation_id,
                scope,
                source_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.SOURCE_STALE",
                SaveJournalState.CLEAN,
                recovery_required=False,
            )

        try:
            existing_recovery = port.inspect_pending_recovery()
        except OSError:
            existing_recovery = object()
        if existing_recovery is not None:
            return self._failure_report(
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.RECOVERY_REQUIRED",
                SaveJournalState.RECOVERY_REQUIRED,
                recovery_required=True,
            )

        try:
            handle = port.stage_candidate(
                operation_id=operation_id,
                candidate_workspace=candidate,
                last_known_good_workspace=lkg,
                requested_document_ids=requested_document_ids,
            )
        except OSError:
            return self._prepublication_failure_report(
                port,
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.STAGE_FAILED",
                SaveJournalState.CLEAN,
                recovery_required=False,
            )
        try:
            validation_result = port.validate_candidate(handle)
            if validation_result is not None:
                raise TypeError("persistence validation must return None")
        except OSError:
            return self._prepublication_failure_report(
                port,
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.VALIDATION_FAILED",
                SaveJournalState.CLEAN,
                recovery_required=False,
            )

        if (
            self._workspace_service.session_id != initial_session
            or self._workspace_service.revision != initial_revision
            or self._workspace_service.workspace_content_digest != initial_digest
        ):
            return self._prepublication_failure_report(
                port,
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.SOURCE_STALE",
                SaveJournalState.CLEAN,
                recovery_required=False,
            )

        try:
            arm_result = port.arm_publication(handle)
            if arm_result is not None:
                raise TypeError("persistence publication arm must return None")
        except OSError:
            return self._prepublication_failure_report(
                port,
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                "PROJECT.SAVE.VALIDATION_FAILED",
                SaveJournalState.CLEAN,
                recovery_required=False,
            )

        try:
            publication_result = port.publish_candidate(handle)
            if publication_result is not None:
                raise TypeError("persistence publication must return None")
            readback = port.readback_candidate(handle)
            if (
                type(readback) is not ProjectWorkspace
                or workspace_content_digest_v1(readback) != candidate_digest
            ):
                raise OSError("candidate readback mismatch")
        except OSError:
            return self._rollback_or_recovery_report(
                port,
                handle,
                operation_id=operation_id,
                scope=scope,
                operation_revision=initial_revision,
                candidate=candidate,
                lkg=lkg,
                requested_document_ids=requested_document_ids,
                changed_document_ids=changed_document_ids,
            )

        try:
            commit_result = port.commit_candidate(handle)
            if commit_result is not None:
                raise TypeError("persistence commit must return None")
        except OSError:
            try:
                final_readback = port.readback_candidate(handle)
            except OSError:
                final_readback = object()
            if _workspace_matches(final_readback, candidate):
                self._adopt_baseline(candidate, initial_revision)
                return self._success_report(
                    operation_id,
                    scope,
                    initial_revision,
                    candidate,
                    lkg,
                    requested_document_ids,
                    changed_document_ids,
                    journal_state=SaveJournalState.RECOVERY_REQUIRED,
                    recovery_required=True,
                    retryable=True,
                    safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
                )
            return self._rollback_or_recovery_report(
                port,
                handle,
                operation_id=operation_id,
                scope=scope,
                operation_revision=initial_revision,
                candidate=candidate,
                lkg=lkg,
                requested_document_ids=requested_document_ids,
                changed_document_ids=changed_document_ids,
            )

        try:
            final_readback = port.readback_candidate(handle)
        except OSError:
            final_readback = object()
        if not _workspace_matches(final_readback, candidate):
            return self._rollback_or_recovery_report(
                port,
                handle,
                operation_id=operation_id,
                scope=scope,
                operation_revision=initial_revision,
                candidate=candidate,
                lkg=lkg,
                requested_document_ids=requested_document_ids,
                changed_document_ids=changed_document_ids,
            )

        self._adopt_baseline(candidate, initial_revision)
        try:
            residue = port.inspect_pending_recovery()
        except OSError:
            residue = object()
        if residue is not None:
            return self._success_report(
                operation_id,
                scope,
                initial_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                journal_state=SaveJournalState.RECOVERY_REQUIRED,
                recovery_required=True,
                retryable=True,
                safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
            )
        return self._success_report(
            operation_id,
            scope,
            initial_revision,
            candidate,
            lkg,
            requested_document_ids,
            changed_document_ids,
        )

    def _adopt_baseline(
        self,
        candidate: ProjectWorkspace,
        operation_revision: int,
    ) -> None:
        candidate_digest = workspace_content_digest_v1(candidate)
        self._baseline = WorkspaceSaveBaseline.from_workspace(
            candidate,
            workspace_revision=operation_revision,
            saved_package_digest=candidate_digest,
        )

    def _rollback_or_recovery_report(
        self,
        port: ProjectWorkspacePersistencePort,
        handle: object,
        *,
        operation_id: str,
        scope: SaveScope,
        operation_revision: int,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
        changed_document_ids: set[str],
    ) -> ProjectSaveReport:
        try:
            rolled_back = port.rollback_candidate(handle)
            rollback_proved = _workspace_matches(rolled_back, lkg)
        except OSError:
            rollback_proved = False
        if rollback_proved:
            try:
                residue = port.inspect_pending_recovery()
            except OSError:
                residue = object()
            return self._failure_report(
                operation_id,
                scope,
                operation_revision,
                candidate,
                lkg,
                requested_document_ids,
                changed_document_ids,
                (
                    "PROJECT.SAVE.COMMIT_FAILED"
                    if residue is None
                    else "PROJECT.SAVE.RECOVERY_REQUIRED"
                ),
                (
                    SaveJournalState.ROLLED_BACK
                    if residue is None
                    else SaveJournalState.RECOVERY_REQUIRED
                ),
                recovery_required=residue is not None,
                rolled_back=True,
            )
        return self._failure_report(
            operation_id,
            scope,
            operation_revision,
            candidate,
            lkg,
            requested_document_ids,
            changed_document_ids,
            "PROJECT.SAVE.RECOVERY_REQUIRED",
            SaveJournalState.RECOVERY_REQUIRED,
            recovery_required=True,
        )

    def _prepublication_failure_report(
        self,
        port: ProjectWorkspacePersistencePort,
        operation_id: str,
        scope: SaveScope,
        operation_revision: int,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
        changed_document_ids: set[str],
        phase_code: str,
        _nominal_journal_state: SaveJournalState,
        *,
        recovery_required: bool,
    ) -> ProjectSaveReport:
        del recovery_required
        try:
            residue = port.inspect_pending_recovery()
            uncertain = residue is not None
        except OSError:
            uncertain = True
        return self._failure_report(
            operation_id,
            scope,
            operation_revision,
            candidate,
            lkg,
            requested_document_ids,
            changed_document_ids,
            "PROJECT.SAVE.RECOVERY_REQUIRED" if uncertain else phase_code,
            (
                SaveJournalState.RECOVERY_REQUIRED
                if uncertain
                else SaveJournalState.CLEAN
            ),
            recovery_required=uncertain,
        )

    def _document_results(
        self,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        changed_document_ids: set[str],
        *,
        changed_status: DocumentSaveStatus,
        safe_code: str | None,
    ) -> tuple[DocumentSaveResult, ...]:
        lkg_by_id = {} if lkg is None else _documents_by_id(lkg)
        results: list[DocumentSaveResult] = []
        for document in candidate.documents:
            before = lkg_by_id.get(document.document_id)
            status = (
                changed_status
                if document.document_id in changed_document_ids
                else DocumentSaveStatus.UNCHANGED
            )
            results.append(
                DocumentSaveResult(
                    document_id=document.document_id,
                    status=status,
                    before_digest=(
                        None
                        if before is None
                        else project_document_content_digest_v1(before)
                    ),
                    after_digest=project_document_content_digest_v1(document),
                    safe_code=safe_code if status is not DocumentSaveStatus.UNCHANGED else None,
                )
            )
        return tuple(results)

    def _success_report(
        self,
        operation_id: str,
        scope: SaveScope,
        operation_revision: int,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
        changed_document_ids: set[str],
        *,
        journal_state: SaveJournalState = SaveJournalState.COMMITTED,
        recovery_required: bool = False,
        retryable: bool = False,
        safe_code: str | None = None,
    ) -> ProjectSaveReport:
        results = self._document_results(
            candidate,
            lkg,
            changed_document_ids,
            changed_status=DocumentSaveStatus.SAVED,
            safe_code=safe_code,
        )
        return ProjectSaveReport(
            operation_id=operation_id,
            scope=scope,
            origin_kind=candidate.origin.kind,
            workspace_revision=operation_revision,
            workspace_content_digest=workspace_content_digest_v1(candidate),
            requested_count=len(requested_document_ids),
            saved_count=sum(item.status is DocumentSaveStatus.SAVED for item in results),
            rolled_back_count=0,
            unchanged_count=sum(
                item.status is DocumentSaveStatus.UNCHANGED for item in results
            ),
            failed_count=0,
            document_results=results,
            journal_state=journal_state,
            recovery_required=recovery_required,
            retryable=retryable,
            safe_code=safe_code,
        )

    def _failure_report(
        self,
        operation_id: str,
        scope: SaveScope,
        operation_revision: int,
        candidate: ProjectWorkspace,
        lkg: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
        changed_document_ids: set[str],
        safe_code: str,
        journal_state: SaveJournalState,
        *,
        recovery_required: bool,
        rolled_back: bool = False,
    ) -> ProjectSaveReport:
        status = (
            DocumentSaveStatus.ROLLED_BACK
            if rolled_back
            else DocumentSaveStatus.FAILED
        )
        results = self._document_results(
            candidate,
            lkg,
            changed_document_ids,
            changed_status=status,
            safe_code=safe_code,
        )
        return ProjectSaveReport(
            operation_id=operation_id,
            scope=scope,
            origin_kind=candidate.origin.kind,
            workspace_revision=operation_revision,
            workspace_content_digest=workspace_content_digest_v1(candidate),
            requested_count=len(requested_document_ids),
            saved_count=0,
            rolled_back_count=sum(
                item.status is DocumentSaveStatus.ROLLED_BACK for item in results
            ),
            unchanged_count=sum(
                item.status is DocumentSaveStatus.UNCHANGED for item in results
            ),
            failed_count=sum(item.status is DocumentSaveStatus.FAILED for item in results),
            document_results=results,
            journal_state=journal_state,
            recovery_required=recovery_required,
            retryable=True,
            safe_code=safe_code,
        )

    @classmethod
    def inspect_cold_recovery(
        cls,
        port: ProjectWorkspacePersistencePort,
    ) -> RecoveryPreview | None:
        plan_key = id(port)
        try:
            handle = port.inspect_pending_recovery()
        except OSError:
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        if handle is None:
            cls._cold_recovery_plans.pop(plan_key, None)
            return None
        facts = _recovery_handle_facts(port, handle)
        try:
            lkg = port.read_recovery_last_known_good(handle)
            candidate = (
                None
                if facts.phase is RecoveryPhase.STAGING
                else port.read_recovery_candidate(handle)
            )
        except OSError:
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        if (
            facts.phase is not RecoveryPhase.STAGING
            and type(candidate) is not ProjectWorkspace
        ):
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        observed_candidate_digest = (
            facts.candidate_digest
            if candidate is None
            else workspace_content_digest_v1(candidate)
        )
        observed_lkg_digest = _workspace_digest_or_none(lkg)
        if (candidate is not None and candidate.project_id != facts.project_id) or (
            lkg is not None and lkg.project_id != facts.project_id
        ):
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        proved = (
            (
                facts.phase is RecoveryPhase.STAGING
                or observed_candidate_digest == facts.candidate_digest
            )
            and observed_lkg_digest == facts.last_known_good_digest
        )
        available_actions = _available_recovery_actions(facts.phase) if proved else ()
        cls._cold_recovery_plans[plan_key] = _ColdRecoveryPlan(
            port=port,
            operation_id=facts.operation_id,
            project_id=facts.project_id,
            candidate_digest=facts.candidate_digest,
            last_known_good_digest=facts.last_known_good_digest,
            phase=facts.phase,
            available_actions=available_actions,
            proved=proved,
        )
        return RecoveryPreview(
            operation_id=facts.operation_id,
            project_id=facts.project_id,
            last_known_good_digest=observed_lkg_digest,
            candidate_digest=observed_candidate_digest,
            available_actions=available_actions,
            safe_codes=() if proved else ("PROJECT.SAVE.RECOVERY_REQUIRED",),
        )

    @classmethod
    def cold_recover(
        cls,
        port: ProjectWorkspacePersistencePort,
        *,
        operation_id: str,
        choice: RecoveryAction,
    ) -> ProjectRecoveryReport:
        requested_operation = _operation_id(operation_id)
        if type(choice) is not RecoveryAction:
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        plan = cls._cold_recovery_plans.pop(id(port), None)
        if (
            plan is None
            or plan.port is not port
            or plan.operation_id != requested_operation
        ):
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        if not plan.proved:
            return ProjectRecoveryReport(
                operation_id=requested_operation,
                action=choice,
                journal_state=SaveJournalState.RECOVERY_REQUIRED,
                workspace_content_digest=plan.last_known_good_digest,
                recovery_required=True,
                retryable=True,
                safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
            )
        if choice not in plan.available_actions:
            _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
        observed_digest: str | None = plan.last_known_good_digest
        try:
            handle = port.inspect_pending_recovery()
            if handle is None:
                _fail("PROJECT.SAVE.RECOVERY_REQUIRED")
            facts = _recovery_handle_facts(port, handle)
            if (
                facts.operation_id != requested_operation
                or facts.project_id != plan.project_id
                or facts.phase is not plan.phase
                or facts.candidate_digest != plan.candidate_digest
                or facts.last_known_good_digest != plan.last_known_good_digest
            ):
                raise OSError("recovery preview is stale")
            lkg = port.read_recovery_last_known_good(handle)
            lkg_digest = _workspace_digest_or_none(lkg)
            if facts.phase is RecoveryPhase.STAGING:
                candidate = None
                candidate_digest = facts.candidate_digest
                if (
                    lkg_digest != plan.last_known_good_digest
                    or (lkg is not None and lkg.project_id != plan.project_id)
                ):
                    raise OSError("recovery identity is not independently proved")
            else:
                candidate = port.read_recovery_candidate(handle)
                if type(candidate) is not ProjectWorkspace:
                    raise OSError("recovery candidate is invalid")
                candidate_digest = workspace_content_digest_v1(candidate)
                if (
                    lkg_digest != plan.last_known_good_digest
                    or candidate_digest != plan.candidate_digest
                    or candidate.project_id != plan.project_id
                    or (lkg is not None and lkg.project_id != plan.project_id)
                ):
                    raise OSError("recovery identity is not independently proved")
            if choice is RecoveryAction.COMPLETE_COMMIT:
                assert candidate is not None
                recovered = port.complete_pending_commit(handle)
                expected = candidate_digest
                expected_workspace = candidate
                journal_state = SaveJournalState.COMMITTED
            elif choice is RecoveryAction.ROLLBACK:
                recovered = port.rollback_pending(handle)
                expected = lkg_digest
                expected_workspace = lkg
                journal_state = SaveJournalState.ROLLED_BACK
            else:
                recovered = port.abandon_staged_copy(handle)
                expected = lkg_digest
                expected_workspace = lkg
                journal_state = SaveJournalState.CLEAN
            if not _workspace_matches(recovered, expected_workspace):
                raise OSError("recovery readback mismatch")
            observed_digest = expected
            residue = port.inspect_pending_recovery()
            if residue is not None:
                raise OSError("recovery cleanup is incomplete")
        except OSError:
            return ProjectRecoveryReport(
                operation_id=requested_operation,
                action=choice,
                journal_state=SaveJournalState.RECOVERY_REQUIRED,
                workspace_content_digest=observed_digest,
                recovery_required=True,
                retryable=True,
                safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
            )
        return ProjectRecoveryReport(
            operation_id=requested_operation,
            action=choice,
            journal_state=journal_state,
            workspace_content_digest=observed_digest,
            recovery_required=False,
            retryable=False,
            safe_code=None,
        )


__all__ = (
    "DocumentOriginWriteState",
    "DocumentSaveResult",
    "DocumentSaveStatus",
    "DocumentSourceWriteResult",
    "DocumentSourceWriteStatus",
    "OriginWriteState",
    "PendingRecoveryFacts",
    "ProjectDocumentWriterPort",
    "ProjectRecoveryReport",
    "ProjectSaveReport",
    "ProjectSaveService",
    "ProjectWorkspacePersistencePort",
    "RecoveryAction",
    "RecoveryPhase",
    "RecoveryPreview",
    "SaveJournalState",
    "SaveScope",
    "WorkspaceSaveBaseline",
)
