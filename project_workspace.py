"""Carrier-neutral aggregation and reconciliation for project workspaces.

The service in this module consumes immutable, already verified workspace
facts.  It deliberately has no Parser, codec, UI, TM, package-carrier, or
provider dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import re
import secrets

from project_workspace_contracts import (
    EditingOverlayEntry,
    OriginBinding,
    ProjectDocument,
    ProjectSegment,
    ProjectSourceSegment,
    ProjectWorkspace,
    SegmentIdentity,
    SourcePresence,
    StagedSelectedProjectDocuments,
)
from project_workspace_identity import (
    ProjectWorkspaceError,
    editing_state_digest_v1,
    validate_document_id,
    validate_project_id,
    validate_sha256,
)


_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OPERATION_ID = re.compile(r"reconcile-[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"PROJECT(?:\.[A-Z0-9_]+){2,7}\Z")


def _fail(code: str) -> None:
    raise ProjectWorkspaceError(code)


def _exact_tuple(value: object, *, code: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        _fail(code)
    return value


def _validate_session_id(value: object) -> str:
    if type(value) is not str or _SESSION_ID.fullmatch(value) is None:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    return value


def _validate_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    return value


class ReconciliationCategory(Enum):
    UNCHANGED = "unchanged"
    SOURCE_CHANGED = "source_changed"
    NEW = "new"
    REMOVED = "removed"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class ReconciliationDisposition(Enum):
    KEEP_DETACHED = "keep_detached"
    REMOVE = "remove"
    ACCEPT_ASSOCIATION = "accept_association"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class FlatProjectSegment:
    identity: SegmentIdentity
    document_id: str
    document_local_index: int
    project_global_index: int
    segment: ProjectSegment

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        validate_document_id(self.document_id)
        if self.identity.document_id != self.document_id:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.document_local_index) is not int or self.document_local_index < 0:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.project_global_index) is not int or self.project_global_index < 0:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.segment) is not ProjectSegment or self.segment.identity != self.identity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")

    @property
    def local_index(self) -> int:
        return self.document_local_index

    @property
    def global_index(self) -> int:
        return self.project_global_index


@dataclass(frozen=True, slots=True)
class DocumentProgress:
    document_id: str
    total_segments: int
    translated_segments: int
    confirmed_segments: int

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        values = (
            self.total_segments,
            self.translated_segments,
            self.confirmed_segments,
        )
        if any(type(value) is not int or value < 0 for value in values):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.translated_segments > self.total_segments:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.confirmed_segments > self.total_segments:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectProgress:
    total_documents: int
    total_segments: int
    translated_segments: int
    confirmed_segments: int

    def __post_init__(self) -> None:
        values = (
            self.total_documents,
            self.total_segments,
            self.translated_segments,
            self.confirmed_segments,
        )
        if any(type(value) is not int or value < 0 for value in values):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.translated_segments > self.total_segments:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.confirmed_segments > self.total_segments:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceEditReceipt:
    """One immutable overlay edit bound to an exact workspace revision."""

    project_id: str
    session_id: str
    identity: SegmentIdentity
    previous_revision: int
    resulting_revision: int
    previous_workspace_digest: str
    resulting_workspace_digest: str
    changed: bool

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        _validate_session_id(self.session_id)
        if type(self.identity) is not SegmentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        previous = _validate_revision(self.previous_revision)
        resulting = _validate_revision(self.resulting_revision)
        validate_sha256(self.previous_workspace_digest)
        validate_sha256(self.resulting_workspace_digest)
        if type(self.changed) is not bool:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if resulting != previous + int(self.changed):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if (self.previous_workspace_digest != self.resulting_workspace_digest) != self.changed:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class IssuedProjectIdentity:
    project_id: str
    session_id: str
    generation: int
    workspace_revision: int

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        _validate_session_id(self.session_id)
        if type(self.generation) is not int or self.generation < 0:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _validate_revision(self.workspace_revision)


@dataclass(frozen=True, slots=True)
class IssuedDocumentIdentity:
    project: IssuedProjectIdentity
    document_id: str

    def __post_init__(self) -> None:
        if type(self.project) is not IssuedProjectIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        validate_document_id(self.document_id)


@dataclass(frozen=True, slots=True)
class IssuedSegmentIdentity:
    document: IssuedDocumentIdentity
    local_segment_id: str

    def __post_init__(self) -> None:
        if type(self.document) is not IssuedDocumentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        SegmentIdentity(self.document.document_id, self.local_segment_id)

    @property
    def segment_identity(self) -> SegmentIdentity:
        return SegmentIdentity(self.document.document_id, self.local_segment_id)


@dataclass(frozen=True, slots=True)
class WorkspaceDocumentView:
    identity: IssuedDocumentIdentity
    display_name: str
    source_ref: str
    order: int
    progress: DocumentProgress

    def __post_init__(self) -> None:
        if type(self.identity) is not IssuedDocumentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.display_name) is not str or type(self.source_ref) is not str:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.order) is not int or self.order < 0:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.progress) is not DocumentProgress:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.progress.document_id != self.identity.document_id:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceSegmentView:
    identity: IssuedSegmentIdentity
    document_local_index: int
    project_global_index: int
    source: str
    target: str
    raw_speaker: str
    confirmed: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not IssuedSegmentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if (
            type(self.document_local_index) is not int
            or self.document_local_index < 0
            or type(self.project_global_index) is not int
            or self.project_global_index < 0
            or type(self.source) is not str
            or type(self.target) is not str
            or type(self.raw_speaker) is not str
            or type(self.confirmed) is not bool
        ):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceSaveState:
    dirty_document_ids: tuple[str, ...]
    manifest_dirty: bool
    project_dirty: bool

    def __post_init__(self) -> None:
        _exact_tuple(
            self.dirty_document_ids,
            code="PROJECT.WORKSPACE.CONTRACT_INVALID",
        )
        if any(type(item) is not str for item in self.dirty_document_ids):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if len(self.dirty_document_ids) != len(set(self.dirty_document_ids)):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        for document_id in self.dirty_document_ids:
            validate_document_id(document_id)
        if type(self.manifest_dirty) is not bool or type(self.project_dirty) is not bool:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if self.project_dirty != bool(self.dirty_document_ids or self.manifest_dirty):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class WorkspaceSessionView:
    project: IssuedProjectIdentity
    documents: tuple[WorkspaceDocumentView, ...]
    segments: tuple[WorkspaceSegmentView, ...]
    current_segment: IssuedSegmentIdentity
    project_progress: ProjectProgress
    save_state: WorkspaceSaveState

    def __post_init__(self) -> None:
        if type(self.project) is not IssuedProjectIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        _exact_tuple(self.documents, code="PROJECT.WORKSPACE.CONTRACT_INVALID")
        _exact_tuple(self.segments, code="PROJECT.WORKSPACE.CONTRACT_INVALID")
        if not self.documents or not self.segments:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if any(type(item) is not WorkspaceDocumentView for item in self.documents):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if any(type(item) is not WorkspaceSegmentView for item in self.segments):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.current_segment) is not IssuedSegmentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if not any(item.identity is self.current_segment for item in self.segments):
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.project_progress) is not ProjectProgress:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(self.save_state) is not WorkspaceSaveState:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")


@dataclass(frozen=True, slots=True)
class ReconciliationAssociation:
    """One authenticated old identity and its admissible incoming candidates."""

    current_identity: SegmentIdentity
    incoming_identities: tuple[SegmentIdentity, ...]

    def __post_init__(self) -> None:
        if type(self.current_identity) is not SegmentIdentity:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        _exact_tuple(
            self.incoming_identities,
            code="PROJECT.RECONCILE.INPUT_INVALID",
        )
        if any(
            type(identity) is not SegmentIdentity
            for identity in self.incoming_identities
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if len(self.incoming_identities) != len(set(self.incoming_identities)):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    identity: SegmentIdentity
    disposition: ReconciliationDisposition
    accepted_incoming_identity: SegmentIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if type(self.disposition) is not ReconciliationDisposition:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if self.disposition is ReconciliationDisposition.ACCEPT_ASSOCIATION:
            if type(self.accepted_incoming_identity) is not SegmentIdentity:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
        elif self.accepted_incoming_identity is not None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class ReconciliationPreview:
    """Body-safe public projection of a privately retained candidate plan."""

    operation_id: str
    project_id: str
    session_id: str
    base_revision: int
    current_workspace_digest: str
    incoming_workspace_digest: str
    proposed_workspace_digest: str
    unchanged_identities: tuple[SegmentIdentity, ...]
    source_changed_identities: tuple[SegmentIdentity, ...]
    new_identities: tuple[SegmentIdentity, ...]
    removed_identities: tuple[SegmentIdentity, ...]
    ambiguous_identities: tuple[SegmentIdentity, ...]
    unresolved_identities: tuple[SegmentIdentity, ...]
    required_decision_identities: tuple[SegmentIdentity, ...]
    association_options: tuple[ReconciliationAssociation, ...] = ()
    safe_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or _OPERATION_ID.fullmatch(
            self.operation_id
        ) is None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        validate_project_id(self.project_id)
        _validate_session_id(self.session_id)
        _validate_revision(self.base_revision)
        for digest in (
            self.current_workspace_digest,
            self.incoming_workspace_digest,
            self.proposed_workspace_digest,
        ):
            validate_sha256(digest)
        identity_groups = (
            self.unchanged_identities,
            self.source_changed_identities,
            self.new_identities,
            self.removed_identities,
            self.ambiguous_identities,
            self.unresolved_identities,
            self.required_decision_identities,
        )
        for group in identity_groups:
            _exact_tuple(group, code="PROJECT.RECONCILE.INPUT_INVALID")
            if any(type(identity) is not SegmentIdentity for identity in group):
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            if len(group) != len(set(group)):
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
        _exact_tuple(
            self.association_options,
            code="PROJECT.RECONCILE.INPUT_INVALID",
        )
        if any(
            type(association) is not ReconciliationAssociation
            for association in self.association_options
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        _exact_tuple(self.safe_codes, code="PROJECT.RECONCILE.INPUT_INVALID")
        if any(
            type(code) is not str or _SAFE_CODE.fullmatch(code) is None
            for code in self.safe_codes
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")

    @property
    def current_digest(self) -> str:
        return self.current_workspace_digest

    @property
    def incoming_digest(self) -> str:
        return self.incoming_workspace_digest

    @property
    def proposed_digest(self) -> str:
        return self.proposed_workspace_digest

    @property
    def unchanged_count(self) -> int:
        return len(self.unchanged_identities)

    @property
    def source_changed_count(self) -> int:
        return len(self.source_changed_identities)

    @property
    def new_count(self) -> int:
        return len(self.new_identities)

    @property
    def removed_count(self) -> int:
        return len(self.removed_identities)

    @property
    def ambiguous_count(self) -> int:
        return len(self.ambiguous_identities)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved_identities)


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    operation_id: str
    project_id: str
    session_id: str
    base_revision: int
    published_revision: int
    previous_workspace_digest: str
    published_workspace_digest: str
    detached_identities: tuple[SegmentIdentity, ...]
    removed_identities: tuple[SegmentIdentity, ...]
    accepted_association_identities: tuple[SegmentIdentity, ...]

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or _OPERATION_ID.fullmatch(
            self.operation_id
        ) is None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        validate_project_id(self.project_id)
        _validate_session_id(self.session_id)
        base = _validate_revision(self.base_revision)
        published = _validate_revision(self.published_revision)
        if published != base + 1:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        validate_sha256(self.previous_workspace_digest)
        validate_sha256(self.published_workspace_digest)
        for group in (
            self.detached_identities,
            self.removed_identities,
            self.accepted_association_identities,
        ):
            _exact_tuple(group, code="PROJECT.RECONCILE.INPUT_INVALID")
            if any(type(identity) is not SegmentIdentity for identity in group):
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            if len(group) != len(set(group)):
                _fail("PROJECT.RECONCILE.INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class PreparedReconciliationToken:
    """Opaque, service-issued capability for one prepared reconciliation."""

    operation_id: str

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or _OPERATION_ID.fullmatch(
            self.operation_id
        ) is None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class _SegmentFacts:
    document: ProjectDocument
    source: ProjectSourceSegment
    overlay: EditingOverlayEntry


class _ReconciliationInputKind(Enum):
    SELECTED_SOURCE = "selected_source"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class _ReconciliationClassification:
    unchanged_identities: tuple[SegmentIdentity, ...]
    source_changed_identities: tuple[SegmentIdentity, ...]
    new_identities: tuple[SegmentIdentity, ...]
    removed_identities: tuple[SegmentIdentity, ...]
    ambiguous_identities: tuple[SegmentIdentity, ...]
    unresolved_identities: tuple[SegmentIdentity, ...]
    associations: tuple[ReconciliationAssociation, ...]


@dataclass(frozen=True, slots=True)
class _ReconciliationPlan:
    operation_id: str
    session_id: str
    base_revision: int
    current_workspace_digest: str
    incoming_workspace_digest: str
    proposed_workspace_digest: str
    input_kind: _ReconciliationInputKind
    current_binding: OriginBinding | None
    incoming_workspace: ProjectWorkspace
    incoming_binding: OriginBinding | None
    incoming_source_identities: tuple[object, ...]
    unchanged_identities: tuple[SegmentIdentity, ...]
    source_changed_identities: tuple[SegmentIdentity, ...]
    new_identities: tuple[SegmentIdentity, ...]
    removed_identities: tuple[SegmentIdentity, ...]
    ambiguous_identities: tuple[SegmentIdentity, ...]
    unresolved_identities: tuple[SegmentIdentity, ...]
    associations: tuple[ReconciliationAssociation, ...]

    @property
    def required_identities(self) -> tuple[SegmentIdentity, ...]:
        return (
            self.removed_identities
            + self.ambiguous_identities
            + self.unresolved_identities
        )


@dataclass(frozen=True, slots=True)
class _PreparedReconciliation:
    token: PreparedReconciliationToken
    plan: _ReconciliationPlan
    candidate_service: ProjectWorkspaceService
    receipt: ReconciliationReceipt


def _hash_value(hasher: object, value: object) -> None:
    """Feed an exact, type-tagged representation into a SHA-256 object."""

    if value is None:
        hasher.update(b"n")
        return
    if type(value) is bool:
        hasher.update(b"b1" if value else b"b0")
        return
    if type(value) is int:
        encoded = str(value).encode("ascii")
        hasher.update(b"i" + len(encoded).to_bytes(8, "big") + encoded)
        return
    if type(value) is str:
        encoded = value.encode("utf-8", errors="strict")
        hasher.update(b"s" + len(encoded).to_bytes(8, "big") + encoded)
        return
    if isinstance(value, Enum):
        _hash_value(hasher, value.value)
        return
    if type(value) is tuple:
        hasher.update(b"t" + len(value).to_bytes(8, "big"))
        for item in value:
            _hash_value(hasher, item)
        return
    if is_dataclass(value) and not isinstance(value, type):
        type_name = type(value).__qualname__.encode("utf-8")
        hasher.update(b"d" + len(type_name).to_bytes(8, "big") + type_name)
        for field in fields(value):
            _hash_value(hasher, getattr(value, field.name))
        return
    raise TypeError("unsupported canonical workspace value")


def _digest_value(domain: bytes, value: object) -> str:
    hasher = hashlib.sha256(domain)
    _hash_value(hasher, value)
    return hasher.hexdigest()


def project_document_content_digest_v1(document: ProjectDocument) -> str:
    """Return the canonical content digest used for per-document dirty state.

    Display order and overlay ``saved_state_digest`` are deliberately excluded:
    order is manifest-owned, while the saved-state digest is a baseline receipt
    rather than editable document content.
    """

    if type(document) is not ProjectDocument:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    content = (
        document.document_id,
        document.source_ref,
        document.display_name,
        document.format_id,
        document.codec_identity,
        document.writer_capability_snapshot,
        document.source_snapshot_digest,
        document.source_segments,
        tuple(
            (
                overlay.document_id,
                overlay.local_segment_id,
                overlay.source_fingerprint,
                overlay.target,
                overlay.confirmed,
            )
            for overlay in document.editing_overlay
        ),
        document.codec_private_member,
    )
    return _digest_value(b"localcat.project-document-content.v1\0", content)


def workspace_manifest_digest_v1(workspace: ProjectWorkspace) -> str:
    """Return the canonical project-level manifest/ordering digest."""

    if type(workspace) is not ProjectWorkspace:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    content = (
        workspace.schema_version,
        workspace.project_id,
        workspace.name,
        workspace.source_locale,
        workspace.target_locale,
        workspace.origin,
        workspace.persistence_kind,
        tuple(document.document_id for document in workspace.documents),
    )
    return _digest_value(b"localcat.project-workspace-manifest.v1\0", content)


def workspace_content_digest_v1(workspace: ProjectWorkspace) -> str:
    """Return the canonical workspace content digest.

    This is the single digest authority consumed by reconciliation, save and
    the later ProjectPackage layer.
    """

    if type(workspace) is not ProjectWorkspace:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    documents = tuple(
        (
            document.document_id,
            document.source_ref,
            document.display_name,
            document.order,
            document.format_id,
            document.codec_identity,
            document.writer_capability_snapshot,
            document.source_snapshot_digest,
            document.source_segments,
            tuple(
                (
                    overlay.document_id,
                    overlay.local_segment_id,
                    overlay.source_fingerprint,
                    overlay.target,
                    overlay.confirmed,
                )
                for overlay in document.editing_overlay
            ),
            document.codec_private_member,
        )
        for document in workspace.documents
    )
    content = (
        workspace.schema_version,
        workspace.project_id,
        workspace.name,
        workspace.source_locale,
        workspace.target_locale,
        workspace.origin,
        workspace.persistence_kind,
        documents,
    )
    return _digest_value(b"localcat.project-workspace-content.v1\0", content)


def _workspace_digest(workspace: ProjectWorkspace) -> str:
    """Compatibility alias for private reconciliation call sites."""

    return workspace_content_digest_v1(workspace)


def _segment_facts(workspace: ProjectWorkspace) -> dict[SegmentIdentity, _SegmentFacts]:
    result: dict[SegmentIdentity, _SegmentFacts] = {}
    for document in workspace.documents:
        for source, overlay in zip(
            document.source_segments,
            document.editing_overlay,
            strict=True,
        ):
            identity = SegmentIdentity(document.document_id, source.local_segment_id)
            result[identity] = _SegmentFacts(document, source, overlay)
    return result


def _workspace_identity_order(workspace: ProjectWorkspace) -> tuple[SegmentIdentity, ...]:
    return tuple(
        SegmentIdentity(document.document_id, source.local_segment_id)
        for document in workspace.documents
        for source in document.source_segments
    )


def _classify_reconciliation(
    current_workspace: ProjectWorkspace,
    incoming_workspace: ProjectWorkspace,
    associations: tuple[ReconciliationAssociation, ...],
) -> _ReconciliationClassification:
    """Classify one exact workspace transition by stable composite identity.

    Both selected-source reconciliation and ProjectPackage reconciliation call
    this function.  Carrier and device-local origin binding validation happen
    at their respective boundaries and cannot fork the six-category grammar.
    """

    if (
        type(current_workspace) is not ProjectWorkspace
        or type(incoming_workspace) is not ProjectWorkspace
        or current_workspace.project_id != incoming_workspace.project_id
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    association_values = _exact_tuple(
        associations,
        code="PROJECT.RECONCILE.INPUT_INVALID",
    )
    if any(
        type(item) is not ReconciliationAssociation
        for item in association_values
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")

    current_facts = _segment_facts(current_workspace)
    incoming_facts = _segment_facts(incoming_workspace)
    current_order = _workspace_identity_order(current_workspace)
    incoming_order = _workspace_identity_order(incoming_workspace)
    current_ids = set(current_facts)
    incoming_ids = set(incoming_facts)
    current_only = current_ids - incoming_ids
    incoming_only = incoming_ids - current_ids

    by_current: dict[SegmentIdentity, ReconciliationAssociation] = {}
    associated_incoming: set[SegmentIdentity] = set()
    for association in association_values:
        if (
            association.current_identity not in current_only
            or association.current_identity in by_current
            or any(
                identity not in incoming_only
                or identity.document_id
                != association.current_identity.document_id
                for identity in association.incoming_identities
            )
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        by_current[association.current_identity] = association
        associated_incoming.update(association.incoming_identities)

    unchanged = tuple(
        identity
        for identity in incoming_order
        if identity in current_ids
        and current_facts[identity].source.source_fingerprint
        == incoming_facts[identity].source.source_fingerprint
    )
    source_changed = tuple(
        identity
        for identity in incoming_order
        if identity in current_ids
        and current_facts[identity].source.source_fingerprint
        != incoming_facts[identity].source.source_fingerprint
    )
    new = tuple(
        identity
        for identity in incoming_order
        if identity in incoming_only and identity not in associated_incoming
    )
    removed = tuple(
        identity
        for identity in current_order
        if identity in current_only and identity not in by_current
    )
    ambiguous = tuple(
        identity
        for identity in current_order
        if identity in by_current and by_current[identity].incoming_identities
    )
    unresolved = tuple(
        identity
        for identity in current_order
        if identity in by_current and not by_current[identity].incoming_identities
    )
    return _ReconciliationClassification(
        unchanged_identities=unchanged,
        source_changed_identities=source_changed,
        new_identities=new,
        removed_identities=removed,
        ambiguous_identities=ambiguous,
        unresolved_identities=unresolved,
        associations=association_values,
    )


def _require_binding_matches_workspace(
    workspace: ProjectWorkspace,
    binding: OriginBinding,
) -> None:
    if type(workspace) is not ProjectWorkspace or type(binding) is not OriginBinding:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    if binding.project_id != workspace.project_id:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    attached_documents = tuple(
        document
        for document in workspace.documents
        if any(
            source.source_presence is SourcePresence.ATTACHED
            for source in document.source_segments
        )
    )
    workspace_by_id = {
        document.document_id: document for document in attached_documents
    }
    binding_by_id = {
        document.document_id: document for document in binding.documents
    }
    if set(binding_by_id) != set(workspace_by_id):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    for document_id, document in workspace_by_id.items():
        bound = binding_by_id[document_id]
        if (
            document.document_id != bound.document_id
            or document.source_ref != bound.source_ref
            or document.format_id != bound.format_id
            or document.codec_identity != bound.codec_identity
            or document.source_snapshot_digest
            != bound.source_identity.content_sha256
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")


def _staged_facts(value: object) -> tuple[
    ProjectWorkspace,
    OriginBinding,
    tuple[object, ...],
]:
    if type(value) is not StagedSelectedProjectDocuments:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    workspace = value.workspace
    binding = value.origin_binding
    source_identities = value.source_identities
    if type(workspace) is not ProjectWorkspace or type(binding) is not OriginBinding:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    _exact_tuple(source_identities, code="PROJECT.RECONCILE.INPUT_INVALID")
    if tuple(document.source_identity for document in binding.documents) != source_identities:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    _require_binding_matches_workspace(workspace, binding)
    if any(
        source.source_presence is not SourcePresence.ATTACHED
        for document in workspace.documents
        for source in document.source_segments
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    return workspace, binding, source_identities


def _require_binding_transition(
    current: OriginBinding,
    incoming: OriginBinding,
) -> None:
    if (
        incoming.project_id != current.project_id
        or incoming.absolute_root != current.absolute_root
        or incoming.root_device != current.root_device
        or incoming.root_inode != current.root_inode
        or incoming.profile_version != current.profile_version
    ):
        _fail("PROJECT.RECONCILE.SOURCE_STALE")
    current_by_ref = {
        item.source_ref: item.document_id for item in current.documents
    }
    incoming_by_ref = {
        item.source_ref: item.document_id for item in incoming.documents
    }
    for source_ref in current_by_ref.keys() & incoming_by_ref.keys():
        if current_by_ref[source_ref] != incoming_by_ref[source_ref]:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
    current_by_id = {
        item.document_id: item for item in current.documents
    }
    incoming_by_id = {
        item.document_id: item for item in incoming.documents
    }
    for document_id in current_by_id.keys() & incoming_by_id.keys():
        previous = current_by_id[document_id]
        next_value = incoming_by_id[document_id]
        if (
            previous.source_ref != next_value.source_ref
            and previous.source_identity.regular_file_identity
            != next_value.source_identity.regular_file_identity
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
    mapping_changed = frozenset(current_by_ref.items()) != frozenset(
        incoming_by_ref.items()
    )
    expected_revision = current.revision + int(mapping_changed)
    if incoming.revision != expected_revision:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")


def _overlay_for(
    *,
    document_id: str,
    local_segment_id: str,
    source_fingerprint: str,
    target: str,
    confirmed: bool,
    saved_state_digest: str | None = None,
) -> EditingOverlayEntry:
    return EditingOverlayEntry(
        document_id=document_id,
        local_segment_id=local_segment_id,
        source_fingerprint=source_fingerprint,
        target=target,
        confirmed=confirmed,
        saved_state_digest=(
            editing_state_digest_v1(
                document_id,
                local_segment_id,
                source_fingerprint,
                target,
                confirmed,
            )
            if saved_state_digest is None
            else saved_state_digest
        ),
    )


class ProjectWorkspaceService:
    """Own one workspace revision and private single-use reconciliation plans."""

    __slots__ = (
        "_workspace",
        "_binding",
        "_session_id",
        "_revision",
        "_plans",
        "_prepared_reconciliations",
    )

    def __init__(
        self,
        current: ProjectWorkspace,
        binding: OriginBinding | None,
        *,
        session_id: str,
        revision: int,
    ) -> None:
        if type(current) is not ProjectWorkspace:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if binding is not None:
            _require_binding_matches_workspace(current, binding)
        self._workspace = current
        self._binding = binding
        self._session_id = _validate_session_id(session_id)
        self._revision = _validate_revision(revision)
        self._plans: dict[str, _ReconciliationPlan] = {}
        self._prepared_reconciliations: dict[
            str,
            _PreparedReconciliation,
        ] = {}

    @property
    def workspace(self) -> ProjectWorkspace:
        return self._workspace

    @property
    def origin_binding(self) -> OriginBinding | None:
        return self._binding

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def workspace_digest(self) -> str:
        return _workspace_digest(self._workspace)

    @property
    def workspace_content_digest(self) -> str:
        return workspace_content_digest_v1(self._workspace)

    @property
    def flat_segments(self) -> tuple[FlatProjectSegment, ...]:
        flattened: list[FlatProjectSegment] = []
        for document in self._workspace.documents:
            for local_index, segment in enumerate(document.segments):
                flattened.append(
                    FlatProjectSegment(
                        identity=segment.identity,
                        document_id=document.document_id,
                        document_local_index=local_index,
                        project_global_index=len(flattened),
                        segment=segment,
                    )
                )
        return tuple(flattened)

    @property
    def document_progress(self) -> tuple[DocumentProgress, ...]:
        return tuple(
            DocumentProgress(
                document_id=document.document_id,
                total_segments=len(document.segments),
                translated_segments=sum(
                    bool(segment.target) for segment in document.segments
                ),
                confirmed_segments=sum(
                    segment.confirmed for segment in document.segments
                ),
            )
            for document in self._workspace.documents
        )

    @property
    def project_progress(self) -> ProjectProgress:
        per_document = self.document_progress
        return ProjectProgress(
            total_documents=len(per_document),
            total_segments=sum(item.total_segments for item in per_document),
            translated_segments=sum(
                item.translated_segments for item in per_document
            ),
            confirmed_segments=sum(item.confirmed_segments for item in per_document),
        )

    def progress_for_document(self, document_id: str) -> DocumentProgress:
        validated = validate_document_id(document_id)
        for item in self.document_progress:
            if item.document_id == validated:
                return item
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")

    def update_segment_edit(
        self,
        identity: SegmentIdentity,
        *,
        target: str,
        confirmed: bool,
        session_id: str,
        base_revision: int,
    ) -> WorkspaceEditReceipt:
        """Replace exactly one editing overlay after all stale gates pass."""

        requested_session = _validate_session_id(session_id)
        requested_revision = _validate_revision(base_revision)
        if (
            requested_session != self._session_id
            or requested_revision != self._revision
        ):
            _fail("PROJECT.WORKSPACE.SESSION_STALE")
        if type(identity) is not SegmentIdentity:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
        if type(target) is not str or type(confirmed) is not bool:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")

        previous_digest = workspace_content_digest_v1(self._workspace)
        replacement_documents: list[ProjectDocument] = []
        matched = False
        changed = False
        for document in self._workspace.documents:
            if document.document_id != identity.document_id:
                replacement_documents.append(document)
                continue
            replacement_overlay: list[EditingOverlayEntry] = []
            for overlay in document.editing_overlay:
                if overlay.local_segment_id != identity.local_segment_id:
                    replacement_overlay.append(overlay)
                    continue
                matched = True
                next_overlay = EditingOverlayEntry(
                    document_id=document.document_id,
                    local_segment_id=overlay.local_segment_id,
                    source_fingerprint=overlay.source_fingerprint,
                    target=target,
                    confirmed=confirmed,
                    saved_state_digest=overlay.saved_state_digest,
                )
                changed = next_overlay != overlay
                replacement_overlay.append(next_overlay)
            replacement_documents.append(
                document
                if not matched or not changed
                else replace(document, editing_overlay=tuple(replacement_overlay))
            )
        if not matched:
            _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")

        candidate = (
            self._workspace
            if not changed
            else replace(self._workspace, documents=tuple(replacement_documents))
        )
        resulting_digest = workspace_content_digest_v1(candidate)
        resulting_revision = self._revision + int(changed)
        receipt = WorkspaceEditReceipt(
            project_id=self._workspace.project_id,
            session_id=self._session_id,
            identity=identity,
            previous_revision=self._revision,
            resulting_revision=resulting_revision,
            previous_workspace_digest=previous_digest,
            resulting_workspace_digest=resulting_digest,
            changed=changed,
        )
        self._workspace = candidate
        self._revision = resulting_revision
        if changed:
            self._plans.clear()
            self._prepared_reconciliations.clear()
        return receipt

    def stage_reconciliation(
        self,
        incoming: object,
        *,
        associations: tuple[ReconciliationAssociation, ...],
        session_id: str,
        base_revision: int,
    ) -> ReconciliationPreview:
        requested_session = _validate_session_id(session_id)
        requested_revision = _validate_revision(base_revision)
        if (
            requested_session != self._session_id
            or requested_revision != self._revision
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")

        incoming_workspace, incoming_binding, source_identities = _staged_facts(
            incoming
        )
        if incoming_workspace.project_id != self._workspace.project_id:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if self._binding is None:
            _fail("PROJECT.RECONCILE.SOURCE_STALE")
        _require_binding_transition(self._binding, incoming_binding)
        return self._stage_exact_workspace_reconciliation(
            incoming_workspace,
            associations=associations,
            input_kind=_ReconciliationInputKind.SELECTED_SOURCE,
            current_binding=self._binding,
            incoming_binding=incoming_binding,
            incoming_source_identities=source_identities,
        )

    def stage_source_rebind(
        self,
        incoming: object,
        *,
        associations: tuple[ReconciliationAssociation, ...],
        session_id: str,
        base_revision: int,
    ) -> ReconciliationPreview:
        """Stage one explicit device-local binding for an unbound package session."""

        requested_session = _validate_session_id(session_id)
        requested_revision = _validate_revision(base_revision)
        if (
            requested_session != self._session_id
            or requested_revision != self._revision
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        if self._binding is not None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        incoming_workspace, incoming_binding, source_identities = _staged_facts(
            incoming
        )
        if incoming_workspace.project_id != self._workspace.project_id:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        return self._stage_exact_workspace_reconciliation(
            incoming_workspace,
            associations=associations,
            input_kind=_ReconciliationInputKind.SELECTED_SOURCE,
            current_binding=None,
            incoming_binding=incoming_binding,
            incoming_source_identities=source_identities,
        )

    def stage_workspace_reconciliation(
        self,
        incoming: ProjectWorkspace,
        *,
        associations: tuple[ReconciliationAssociation, ...],
        session_id: str,
        base_revision: int,
    ) -> ReconciliationPreview:
        """Stage a carrier- and binding-neutral exact workspace transition."""

        requested_session = _validate_session_id(session_id)
        requested_revision = _validate_revision(base_revision)
        if (
            requested_session != self._session_id
            or requested_revision != self._revision
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        if type(incoming) is not ProjectWorkspace:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        return self._stage_exact_workspace_reconciliation(
            incoming,
            associations=associations,
            input_kind=_ReconciliationInputKind.WORKSPACE,
            current_binding=None,
            incoming_binding=None,
            incoming_source_identities=(),
        )

    def _stage_exact_workspace_reconciliation(
        self,
        incoming_workspace: ProjectWorkspace,
        *,
        associations: tuple[ReconciliationAssociation, ...],
        input_kind: _ReconciliationInputKind,
        current_binding: OriginBinding | None,
        incoming_binding: OriginBinding | None,
        incoming_source_identities: tuple[object, ...],
    ) -> ReconciliationPreview:
        classification = _classify_reconciliation(
            self._workspace,
            incoming_workspace,
            associations,
        )
        current_digest = _workspace_digest(self._workspace)
        incoming_digest = _workspace_digest(incoming_workspace)
        plan_projection = (
            current_digest,
            incoming_digest,
            classification.unchanged_identities,
            classification.source_changed_identities,
            classification.new_identities,
            classification.removed_identities,
            classification.ambiguous_identities,
            classification.unresolved_identities,
            classification.associations,
        )
        proposed_digest = _digest_value(
            b"localcat.reconciliation-plan.v1\0",
            plan_projection,
        )
        operation_id = "reconcile-" + secrets.token_hex(32)
        plan = _ReconciliationPlan(
            operation_id=operation_id,
            session_id=self._session_id,
            base_revision=self._revision,
            current_workspace_digest=current_digest,
            incoming_workspace_digest=incoming_digest,
            proposed_workspace_digest=proposed_digest,
            input_kind=input_kind,
            current_binding=current_binding,
            incoming_workspace=incoming_workspace,
            incoming_binding=incoming_binding,
            incoming_source_identities=incoming_source_identities,
            unchanged_identities=classification.unchanged_identities,
            source_changed_identities=classification.source_changed_identities,
            new_identities=classification.new_identities,
            removed_identities=classification.removed_identities,
            ambiguous_identities=classification.ambiguous_identities,
            unresolved_identities=classification.unresolved_identities,
            associations=classification.associations,
        )
        self._plans[operation_id] = plan
        return ReconciliationPreview(
            operation_id=operation_id,
            project_id=self._workspace.project_id,
            session_id=self._session_id,
            base_revision=self._revision,
            current_workspace_digest=current_digest,
            incoming_workspace_digest=incoming_digest,
            proposed_workspace_digest=proposed_digest,
            unchanged_identities=classification.unchanged_identities,
            source_changed_identities=classification.source_changed_identities,
            new_identities=classification.new_identities,
            removed_identities=classification.removed_identities,
            ambiguous_identities=classification.ambiguous_identities,
            unresolved_identities=classification.unresolved_identities,
            required_decision_identities=plan.required_identities,
            association_options=classification.associations,
        )

    def apply_reconciliation(
        self,
        operation_id: str,
        *,
        decisions: tuple[ReconciliationDecision, ...],
        session_id: str,
        base_revision: int,
        incoming_source_identities: tuple[object, ...],
    ) -> ReconciliationReceipt:
        """Compatibility path: prepare fully, then publish the issued token."""

        token = self.prepare_reconciliation(
            operation_id,
            decisions=decisions,
            session_id=session_id,
            base_revision=base_revision,
            incoming_source_identities=incoming_source_identities,
        )
        return self.commit_reconciliation(token)

    def prepare_reconciliation(
        self,
        operation_id: str,
        *,
        decisions: tuple[ReconciliationDecision, ...],
        session_id: str,
        base_revision: int,
        incoming_source_identities: tuple[object, ...],
    ) -> PreparedReconciliationToken:
        """Build and validate a source candidate without publishing authority.

        The original preview plan is consumed only after every candidate and
        receipt projection succeeds.  The returned token is accepted solely
        by this service instance and solely by object identity.
        """

        plan = self._peek_reconciliation_plan(
            operation_id,
            input_kind=_ReconciliationInputKind.SELECTED_SOURCE,
        )
        source_identities = _exact_tuple(
            incoming_source_identities,
            code="PROJECT.RECONCILE.INPUT_INVALID",
        )
        if len(source_identities) != len(plan.incoming_source_identities) or any(
            type(actual) is not type(expected) or actual != expected
            for actual, expected in zip(
                source_identities,
                plan.incoming_source_identities,
                strict=True,
            )
        ):
            _fail("PROJECT.RECONCILE.SOURCE_STALE")
        self._require_current_plan(
            plan,
            session_id=session_id,
            base_revision=base_revision,
            require_binding=True,
        )
        if type(plan.incoming_binding) is not OriginBinding:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        candidate, receipt = self._prepare_staged_plan(
            plan,
            decisions=decisions,
            published_binding=plan.incoming_binding,
        )
        candidate_service = ProjectWorkspaceService(
            candidate,
            plan.incoming_binding,
            session_id=self._session_id,
            revision=receipt.published_revision,
        )
        token = PreparedReconciliationToken(plan.operation_id)
        prepared = _PreparedReconciliation(
            token=token,
            plan=plan,
            candidate_service=candidate_service,
            receipt=receipt,
        )
        if self._plans.get(operation_id) is not plan:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        self._plans.pop(operation_id)
        self._prepared_reconciliations[operation_id] = prepared
        return token

    def prepared_workspace_service(
        self,
        token: PreparedReconciliationToken,
    ) -> ProjectWorkspaceService:
        """Project the exact candidate authority without publishing it."""

        prepared = self._require_prepared_reconciliation(token)
        self._require_current_plan(
            prepared.plan,
            session_id=prepared.plan.session_id,
            base_revision=prepared.plan.base_revision,
            require_binding=True,
        )
        self._require_candidate_service_current(prepared)
        return prepared.candidate_service

    def commit_reconciliation(
        self,
        token: PreparedReconciliationToken,
    ) -> ReconciliationReceipt:
        """Consume one prepared capability and publish its facts exactly once."""

        prepared = self._take_prepared_reconciliation(token)
        self._require_current_plan(
            prepared.plan,
            session_id=prepared.plan.session_id,
            base_revision=prepared.plan.base_revision,
            require_binding=True,
        )
        self._require_candidate_service_current(prepared)

        candidate_service = prepared.candidate_service
        receipt = prepared.receipt
        self._workspace = candidate_service.workspace
        self._binding = candidate_service.origin_binding
        self._revision = candidate_service.revision
        return receipt

    def discard_prepared_reconciliation(
        self,
        token: PreparedReconciliationToken,
    ) -> None:
        """Revoke one uncommitted prepared candidate without publishing it."""

        if type(token) is not PreparedReconciliationToken:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        prepared = self._prepared_reconciliations.get(token.operation_id)
        if prepared is None:
            return
        if prepared.token is not token:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        self._prepared_reconciliations.pop(token.operation_id)

    def apply_workspace_reconciliation(
        self,
        operation_id: str,
        *,
        incoming: ProjectWorkspace,
        decisions: tuple[ReconciliationDecision, ...],
        session_id: str,
        base_revision: int,
    ) -> ReconciliationReceipt:
        """Consume one exact-workspace preview and atomically publish its result."""

        plan = self._take_reconciliation_plan(
            operation_id,
            input_kind=_ReconciliationInputKind.WORKSPACE,
        )
        if type(incoming) is not ProjectWorkspace:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if _workspace_digest(incoming) != plan.incoming_workspace_digest:
            _fail("PROJECT.RECONCILE.SOURCE_STALE")
        self._require_current_plan(
            plan,
            session_id=session_id,
            base_revision=base_revision,
            require_binding=False,
        )
        return self._apply_staged_plan(
            plan,
            decisions=decisions,
            published_binding=self._binding,
        )

    def _take_reconciliation_plan(
        self,
        operation_id: str,
        *,
        input_kind: _ReconciliationInputKind,
    ) -> _ReconciliationPlan:
        if type(operation_id) is not str or _OPERATION_ID.fullmatch(operation_id) is None:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        plan = self._plans.pop(operation_id, None)
        if plan is None or plan.input_kind is not input_kind:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        return plan

    def _peek_reconciliation_plan(
        self,
        operation_id: str,
        *,
        input_kind: _ReconciliationInputKind,
    ) -> _ReconciliationPlan:
        if (
            type(operation_id) is not str
            or _OPERATION_ID.fullmatch(operation_id) is None
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        plan = self._plans.get(operation_id)
        if plan is None or plan.input_kind is not input_kind:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        return plan

    def _require_prepared_reconciliation(
        self,
        token: PreparedReconciliationToken,
    ) -> _PreparedReconciliation:
        if type(token) is not PreparedReconciliationToken:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        prepared = self._prepared_reconciliations.get(token.operation_id)
        if prepared is None or prepared.token is not token:
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")
        return prepared

    def _take_prepared_reconciliation(
        self,
        token: PreparedReconciliationToken,
    ) -> _PreparedReconciliation:
        prepared = self._require_prepared_reconciliation(token)
        self._prepared_reconciliations.pop(token.operation_id)
        return prepared

    def _require_candidate_service_current(
        self,
        prepared: _PreparedReconciliation,
    ) -> None:
        candidate_service = prepared.candidate_service
        receipt = prepared.receipt
        if (
            type(candidate_service) is not ProjectWorkspaceService
            or candidate_service.session_id != self._session_id
            or candidate_service.revision != receipt.published_revision
            or candidate_service.origin_binding != prepared.plan.incoming_binding
            or candidate_service.workspace_content_digest
            != receipt.published_workspace_digest
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")

    def _require_current_plan(
        self,
        plan: _ReconciliationPlan,
        *,
        session_id: str,
        base_revision: int,
        require_binding: bool,
    ) -> None:
        requested_session = _validate_session_id(session_id)
        requested_revision = _validate_revision(base_revision)
        if (
            requested_session != plan.session_id
            or requested_session != self._session_id
            or requested_revision != plan.base_revision
            or requested_revision != self._revision
            or _workspace_digest(self._workspace) != plan.current_workspace_digest
            or (require_binding and self._binding != plan.current_binding)
        ):
            _fail("PROJECT.RECONCILE.PREVIEW_STALE")

    def _apply_staged_plan(
        self,
        plan: _ReconciliationPlan,
        *,
        decisions: tuple[ReconciliationDecision, ...],
        published_binding: OriginBinding | None,
    ) -> ReconciliationReceipt:
        candidate, receipt = self._prepare_staged_plan(
            plan,
            decisions=decisions,
            published_binding=published_binding,
        )
        self._workspace = candidate
        self._binding = published_binding
        self._revision = receipt.published_revision
        return receipt

    def _prepare_staged_plan(
        self,
        plan: _ReconciliationPlan,
        *,
        decisions: tuple[ReconciliationDecision, ...],
        published_binding: OriginBinding | None,
    ) -> tuple[ProjectWorkspace, ReconciliationReceipt]:
        decision_values = _exact_tuple(
            decisions,
            code="PROJECT.RECONCILE.INPUT_INVALID",
        )
        if any(type(item) is not ReconciliationDecision for item in decision_values):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        by_identity: dict[SegmentIdentity, ReconciliationDecision] = {}
        for decision in decision_values:
            if decision.identity in by_identity:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            by_identity[decision.identity] = decision
        required = set(plan.required_identities)
        provided = set(by_identity)
        if provided - required:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if required - provided:
            _fail("PROJECT.RECONCILE.DECISION_REQUIRED")

        association_by_current = {
            association.current_identity: association
            for association in plan.associations
        }
        accepted_incoming: dict[SegmentIdentity, SegmentIdentity] = {}
        detached: list[SegmentIdentity] = []
        removed: list[SegmentIdentity] = []
        accepted_current: list[SegmentIdentity] = []
        for identity in plan.required_identities:
            decision = by_identity[identity]
            if identity in plan.removed_identities:
                allowed = {
                    ReconciliationDisposition.KEEP_DETACHED,
                    ReconciliationDisposition.REMOVE,
                }
            elif identity in plan.ambiguous_identities:
                allowed = {
                    ReconciliationDisposition.KEEP_DETACHED,
                    ReconciliationDisposition.ACCEPT_ASSOCIATION,
                    ReconciliationDisposition.CANCEL,
                }
            else:
                allowed = {
                    ReconciliationDisposition.KEEP_DETACHED,
                    ReconciliationDisposition.CANCEL,
                }
            if decision.disposition not in allowed:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            if decision.disposition is ReconciliationDisposition.CANCEL:
                _fail("PROJECT.RECONCILE.APPLY_FAILED")
            if decision.disposition is ReconciliationDisposition.KEEP_DETACHED:
                detached.append(identity)
                continue
            if decision.disposition is ReconciliationDisposition.REMOVE:
                removed.append(identity)
                continue
            candidate = decision.accepted_incoming_identity
            if candidate is None:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            association = association_by_current[identity]
            if candidate not in association.incoming_identities:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            if candidate in accepted_incoming:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            accepted_incoming[candidate] = identity
            accepted_current.append(identity)

        try:
            candidate = self._build_candidate(
                plan,
                detached_identities=frozenset(detached),
                removed_identities=frozenset(removed),
                accepted_incoming=accepted_incoming,
            )
        except ProjectWorkspaceError as error:
            raise ProjectWorkspaceError(
                "PROJECT.RECONCILE.APPLY_FAILED"
            ) from error
        candidate_digest = _workspace_digest(candidate)
        receipt = ReconciliationReceipt(
            operation_id=plan.operation_id,
            project_id=candidate.project_id,
            session_id=self._session_id,
            base_revision=self._revision,
            published_revision=self._revision + 1,
            previous_workspace_digest=plan.current_workspace_digest,
            published_workspace_digest=candidate_digest,
            detached_identities=tuple(detached),
            removed_identities=tuple(removed),
            accepted_association_identities=tuple(accepted_current),
        )
        if (
            plan.input_kind is _ReconciliationInputKind.SELECTED_SOURCE
            and published_binding is not None
        ):
            _require_binding_matches_workspace(candidate, published_binding)
        return candidate, receipt

    def _build_candidate(
        self,
        plan: _ReconciliationPlan,
        *,
        detached_identities: frozenset[SegmentIdentity],
        removed_identities: frozenset[SegmentIdentity],
        accepted_incoming: dict[SegmentIdentity, SegmentIdentity],
    ) -> ProjectWorkspace:
        current_facts = _segment_facts(self._workspace)
        incoming_facts = _segment_facts(plan.incoming_workspace)
        unchanged = set(plan.unchanged_identities)
        source_changed = set(plan.source_changed_identities)
        new = set(plan.new_identities)
        associated_candidates = {
            identity
            for association in plan.associations
            for identity in association.incoming_identities
        }

        output_by_document: dict[
            str,
            list[tuple[ProjectSourceSegment, EditingOverlayEntry]],
        ] = {}
        document_templates: dict[str, ProjectDocument] = {
            document.document_id: document
            for document in plan.incoming_workspace.documents
        }

        for incoming_document in plan.incoming_workspace.documents:
            output = output_by_document.setdefault(incoming_document.document_id, [])
            for incoming_source, incoming_overlay in zip(
                incoming_document.source_segments,
                incoming_document.editing_overlay,
                strict=True,
            ):
                incoming_identity = SegmentIdentity(
                    incoming_document.document_id,
                    incoming_source.local_segment_id,
                )
                if incoming_identity in unchanged:
                    current = current_facts[incoming_identity]
                    output.append((incoming_source, current.overlay))
                    continue
                if incoming_identity in source_changed:
                    current = current_facts[incoming_identity]
                    output.append(
                        (
                            incoming_source,
                            _overlay_for(
                                document_id=incoming_document.document_id,
                                local_segment_id=incoming_source.local_segment_id,
                                source_fingerprint=incoming_source.source_fingerprint,
                                target=current.overlay.target,
                                confirmed=False,
                                saved_state_digest=current.overlay.saved_state_digest,
                            ),
                        )
                    )
                    continue
                if incoming_identity in new:
                    output.append(
                        (
                            incoming_source,
                            _overlay_for(
                                document_id=incoming_document.document_id,
                                local_segment_id=incoming_source.local_segment_id,
                                source_fingerprint=incoming_source.source_fingerprint,
                                target=incoming_overlay.target,
                                confirmed=False,
                            ),
                        )
                    )
                    continue
                accepted_old_identity = accepted_incoming.get(incoming_identity)
                if accepted_old_identity is not None:
                    current = current_facts[accepted_old_identity]
                    reassociated_source = replace(
                        incoming_source,
                        local_segment_id=accepted_old_identity.local_segment_id,
                        source_presence=SourcePresence.ATTACHED,
                    )
                    output.append(
                        (
                            reassociated_source,
                            _overlay_for(
                                document_id=accepted_old_identity.document_id,
                                local_segment_id=accepted_old_identity.local_segment_id,
                                source_fingerprint=reassociated_source.source_fingerprint,
                                target=current.overlay.target,
                                confirmed=False,
                                saved_state_digest=current.overlay.saved_state_digest,
                            ),
                        )
                    )
                    continue
                if incoming_identity in associated_candidates:
                    output.append(
                        (
                            incoming_source,
                            _overlay_for(
                                document_id=incoming_document.document_id,
                                local_segment_id=incoming_source.local_segment_id,
                                source_fingerprint=incoming_source.source_fingerprint,
                                target=incoming_overlay.target,
                                confirmed=False,
                            ),
                        )
                    )
                    continue
                if incoming_identity not in associated_candidates:
                    _fail("PROJECT.RECONCILE.APPLY_FAILED")

        for identity in _workspace_identity_order(self._workspace):
            if identity in removed_identities or identity not in detached_identities:
                continue
            current = current_facts[identity]
            detached_source = replace(
                current.source,
                source_presence=SourcePresence.DETACHED,
            )
            output_by_document.setdefault(identity.document_id, []).append(
                (detached_source, current.overlay)
            )
            document_templates.setdefault(identity.document_id, current.document)

        documents: list[ProjectDocument] = []
        emitted_document_ids: set[str] = set()
        for template in (
            *self._workspace.documents,
            *plan.incoming_workspace.documents,
        ):
            if template.document_id in emitted_document_ids:
                continue
            pairs = output_by_document.get(template.document_id, [])
            if not pairs:
                continue
            emitted_document_ids.add(template.document_id)
            chosen = document_templates[template.document_id]
            documents.append(
                replace(
                    chosen,
                    order=len(documents),
                    source_segments=tuple(source for source, _overlay in pairs),
                    editing_overlay=tuple(overlay for _source, overlay in pairs),
                )
            )

        return replace(
            plan.incoming_workspace,
            documents=tuple(documents),
        )


__all__ = (
    "DocumentProgress",
    "FlatProjectSegment",
    "IssuedDocumentIdentity",
    "IssuedProjectIdentity",
    "IssuedSegmentIdentity",
    "PreparedReconciliationToken",
    "ProjectProgress",
    "ProjectWorkspaceService",
    "ReconciliationAssociation",
    "ReconciliationCategory",
    "ReconciliationDecision",
    "ReconciliationDisposition",
    "ReconciliationPreview",
    "ReconciliationReceipt",
    "WorkspaceEditReceipt",
    "WorkspaceDocumentView",
    "WorkspaceSaveState",
    "WorkspaceSegmentView",
    "WorkspaceSessionView",
    "project_document_content_digest_v1",
    "workspace_content_digest_v1",
    "workspace_manifest_digest_v1",
)
