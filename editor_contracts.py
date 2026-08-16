"""Immutable cross-layer contracts for the LocalCAT desktop editor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


DEFAULT_EDITOR_FONT_SIZE = 15
MIN_EDITOR_FONT_SIZE = 10
MAX_EDITOR_FONT_SIZE = 28
EDITOR_FONT_SIZE_STEP = 1


class ResourceKind(str, Enum):
    """Supported local language-resource categories."""

    TRANSLATION_MEMORY = "translation_memory"
    TERMBASE = "termbase"


class SegmentDensity(str, Enum):
    """Supported segment-navigation density modes."""

    COMPACT = "compact"
    WRAPPED = "wrapped"


class WorkspaceMode(str, Enum):
    """Supported desktop workspace pages."""

    EDIT = "edit"
    BROWSE = "browse"


class TermMatchPolicy(str, Enum):
    """Matching semantics attached to one persisted term row."""

    LEGACY = "legacy_case_sensitive_substring"
    CONFIGURED = "configured"


class TermRowKind(str, Enum):
    """Physical row formats supported by the mixed termbase."""

    LEGACY = "legacy"
    V1 = "localcat-term-v1"


class TermCommitState(str, Enum):
    """Durability state returned by a termbase commit attempt."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    ROLLED_BACK = "rolled_back"
    INDETERMINATE = "indeterminate"


def _validate_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_sha256_digest(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    is_hex_digest = len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )
    if not is_hex_digest:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")


def _validate_nonnegative_int(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")


def _validate_path(value: object, field_name: str) -> None:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")


@dataclass(frozen=True)
class RecentProject:
    """One locally remembered project and its last visited segment."""

    path: Path
    segment_id: str
    index: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("recent project path must be absolute")
        if not self.segment_id.strip():
            raise ValueError("recent project segment id must not be empty")
        if self.index < 0:
            raise ValueError("recent project index must not be negative")


@dataclass(frozen=True)
class DisplayPreferences:
    """Persistent local display preferences for the editor workspace."""

    segment_density: SegmentDensity = SegmentDensity.COMPACT
    workspace_mode: WorkspaceMode = WorkspaceMode.EDIT
    editor_font_size: int = DEFAULT_EDITOR_FONT_SIZE

    def __post_init__(self) -> None:
        if not isinstance(self.segment_density, SegmentDensity):
            raise TypeError("segment density must be a SegmentDensity")
        if not isinstance(self.workspace_mode, WorkspaceMode):
            raise TypeError("workspace mode must be a WorkspaceMode")
        if not isinstance(self.editor_font_size, int) or isinstance(
            self.editor_font_size,
            bool,
        ):
            raise TypeError("editor font size must be an integer")
        if not MIN_EDITOR_FONT_SIZE <= self.editor_font_size <= MAX_EDITOR_FONT_SIZE:
            raise ValueError(
                "editor font size must be between "
                f"{MIN_EDITOR_FONT_SIZE} and {MAX_EDITOR_FONT_SIZE}"
            )


@dataclass(frozen=True)
class EditorSegment:
    """One editable bilingual segment."""

    id: str
    source: str
    target: str = ""
    speaker: str = ""
    confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("segment id must not be empty")
        if not self.source.strip():
            raise ValueError("segment source must not be empty")


@dataclass(frozen=True)
class EditorProject:
    """A local translation project rendered by the editor."""

    name: str
    segments: tuple[EditorSegment, ...]
    source_locale: str = "en-US"
    target_locale: str = "zh-CN"
    path: Path | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("project name must not be empty")
        if not isinstance(self.segments, tuple):
            raise TypeError("project segments must be a tuple")
        segment_ids = tuple(segment.id for segment in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment ids must be unique within a project")
        if self.path is not None and not self.path.is_absolute():
            raise ValueError("project path must be absolute when provided")


@dataclass(frozen=True)
class LiteralReplaceRule:
    """One ordered, case-sensitive literal target replacement."""

    find: str
    replacement: str
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.find, str) or not isinstance(self.replacement, str):
            raise TypeError("literal rule values must be strings")
        if not self.find:
            raise ValueError("literal rule find value must not be empty")
        if not isinstance(self.enabled, bool):
            raise TypeError("literal rule enabled state must be a boolean")


@dataclass(frozen=True)
class PreprocessChange:
    """Complete before/after state for one changed target segment."""

    segment_id: str
    segment_index: int
    before_target: str
    after_target: str
    before_confirmed: bool
    after_confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str):
            raise TypeError("preprocess change segment id must be a string")
        if not self.segment_id.strip():
            raise ValueError("preprocess change segment id must not be empty")
        if (
            not isinstance(self.segment_index, int)
            or isinstance(self.segment_index, bool)
        ):
            raise TypeError("preprocess change segment index must be an integer")
        if self.segment_index < 0:
            raise ValueError("preprocess change segment index must not be negative")
        if not isinstance(self.before_target, str) or not isinstance(
            self.after_target,
            str,
        ):
            raise TypeError("preprocess change targets must be strings")
        if self.before_target == self.after_target:
            raise ValueError("preprocess change must contain an actual target change")
        if not isinstance(self.before_confirmed, bool) or not isinstance(
            self.after_confirmed,
            bool,
        ):
            raise TypeError("preprocess change confirmed states must be booleans")
        if self.after_confirmed:
            raise ValueError("a changed target must become unconfirmed")


@dataclass(frozen=True)
class PreprocessPreview:
    """Immutable preprocessing preview bound to one project revision."""

    project_session_id: str
    base_revision: int
    changes: tuple[PreprocessChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_session_id, str):
            raise TypeError("preview project session id must be a string")
        if not self.project_session_id.strip():
            raise ValueError("preview project session id must not be empty")
        if not isinstance(self.base_revision, int) or isinstance(
            self.base_revision,
            bool,
        ):
            raise TypeError("preview base revision must be an integer")
        if self.base_revision < 0:
            raise ValueError("preview base revision must not be negative")
        if not isinstance(self.changes, tuple):
            raise TypeError("preview changes must be a tuple")
        if not all(isinstance(change, PreprocessChange) for change in self.changes):
            raise TypeError("preview changes must contain PreprocessChange values")
        segment_ids = tuple(change.segment_id for change in self.changes)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("preview changed segment ids must be unique")


@dataclass(frozen=True)
class BatchOperationReport:
    """Observable result of one preprocessing apply or undo request."""

    operation: str
    project_session_id: str
    resulting_revision: int
    changed_segment_ids: tuple[str, ...]
    dirty: bool

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str):
            raise TypeError("batch operation must be a string")
        if not self.operation.strip():
            raise ValueError("batch operation must not be empty")
        if not isinstance(self.project_session_id, str):
            raise TypeError("batch report project session id must be a string")
        if not self.project_session_id.strip():
            raise ValueError("batch report project session id must not be empty")
        if not isinstance(self.resulting_revision, int) or isinstance(
            self.resulting_revision,
            bool,
        ):
            raise TypeError("batch report revision must be an integer")
        if self.resulting_revision < 0:
            raise ValueError("batch report revision must not be negative")
        if not isinstance(self.changed_segment_ids, tuple):
            raise TypeError("batch report changed segment ids must be a tuple")
        if not all(
            isinstance(segment_id, str) and segment_id.strip()
            for segment_id in self.changed_segment_ids
        ):
            raise ValueError("batch report segment ids must be non-empty strings")
        if len(self.changed_segment_ids) != len(set(self.changed_segment_ids)):
            raise ValueError("batch report changed segment ids must be unique")
        if not isinstance(self.dirty, bool):
            raise TypeError("batch report dirty state must be a boolean")


@dataclass(frozen=True)
class BatchUndoState:
    """The single undo snapshot retained for the latest applied batch."""

    project_session_id: str
    applied_revision: int
    dirty_before: bool
    saved_baseline_digest_at_apply: str
    changes: tuple[PreprocessChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_session_id, str):
            raise TypeError("batch undo project session id must be a string")
        if not self.project_session_id.strip():
            raise ValueError("batch undo project session id must not be empty")
        if not isinstance(self.applied_revision, int) or isinstance(
            self.applied_revision,
            bool,
        ):
            raise TypeError("batch undo applied revision must be an integer")
        if self.applied_revision < 0:
            raise ValueError("batch undo applied revision must not be negative")
        if not isinstance(self.dirty_before, bool):
            raise TypeError("batch undo dirty state must be a boolean")
        if not isinstance(self.saved_baseline_digest_at_apply, str):
            raise TypeError("batch undo saved baseline digest must be a string")
        if not self.saved_baseline_digest_at_apply.strip():
            raise ValueError("batch undo saved baseline digest must not be empty")
        if not isinstance(self.changes, tuple):
            raise TypeError("batch undo changes must be a tuple")
        if not self.changes:
            raise ValueError("batch undo state must contain at least one change")
        if not all(isinstance(change, PreprocessChange) for change in self.changes):
            raise TypeError("batch undo changes must contain PreprocessChange values")
        segment_ids = tuple(change.segment_id for change in self.changes)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("batch undo changed segment ids must be unique")


@dataclass(frozen=True)
class TermRecordLocator:
    """Stable locator for one row in a validated termbase snapshot."""

    row_kind: TermRowKind
    file_digest: str
    row_ordinal: int
    row_digest: str
    record_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.row_kind, TermRowKind):
            raise TypeError("term locator row kind must be a TermRowKind")
        _validate_sha256_digest(self.file_digest, "term locator file digest")
        _validate_nonnegative_int(self.row_ordinal, "term locator row ordinal")
        _validate_sha256_digest(self.row_digest, "term locator row digest")
        if self.row_kind is TermRowKind.LEGACY:
            if self.record_id is not None:
                raise ValueError("legacy term locator must not have a record id")
        else:
            _validate_non_empty_string(
                self.record_id,
                "v1 term locator record id",
            )


@dataclass(frozen=True)
class TermRecord:
    """One validated legacy or v1 termbase record."""

    locator: TermRecordLocator
    record_id: str | None
    source: str
    target: str
    policy: TermMatchPolicy
    match_case: bool | None
    whole_word: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.locator, TermRecordLocator):
            raise TypeError("term record locator must be a TermRecordLocator")
        _validate_non_empty_string(self.source, "term record source")
        _validate_non_empty_string(self.target, "term record target")
        if not isinstance(self.policy, TermMatchPolicy):
            raise TypeError("term record policy must be a TermMatchPolicy")

        if self.locator.row_kind is TermRowKind.LEGACY:
            if self.record_id is not None:
                raise ValueError("legacy term record must not have a record id")
            if self.policy is not TermMatchPolicy.LEGACY:
                raise ValueError("legacy term record must use the legacy policy")
            if self.match_case is not None or self.whole_word is not None:
                raise ValueError("legacy term record flags must both be empty")
            return

        _validate_non_empty_string(self.record_id, "v1 term record id")
        if self.record_id != self.locator.record_id:
            raise ValueError("v1 term record id must match its locator")
        if self.policy is not TermMatchPolicy.CONFIGURED:
            raise ValueError("v1 term record must use the configured policy")
        if not isinstance(self.match_case, bool) or not isinstance(
            self.whole_word,
            bool,
        ):
            raise TypeError("v1 term record flags must be booleans")


@dataclass(frozen=True)
class TermDraft:
    """User-editable values for creating or updating a configured term."""

    source: str
    target: str
    match_case: bool = False
    whole_word: bool = True

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.source, "term draft source")
        _validate_non_empty_string(self.target, "term draft target")
        if not isinstance(self.match_case, bool) or not isinstance(
            self.whole_word,
            bool,
        ):
            raise TypeError("term draft flags must be booleans")


@dataclass(frozen=True)
class LegacyTermRow:
    """One incoming two-column row used by the legacy merge path."""

    source: str
    target: str
    input_ordinal: int

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.source, "legacy term source")
        _validate_non_empty_string(self.target, "legacy term target")
        _validate_nonnegative_int(
            self.input_ordinal,
            "legacy term input ordinal",
        )


def _validate_term_records(records: object, field_name: str) -> None:
    if not isinstance(records, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if not all(isinstance(record, TermRecord) for record in records):
        raise TypeError(f"{field_name} must contain TermRecord values")
    sources = tuple(record.source for record in records)
    if len(sources) != len(set(sources)):
        raise ValueError(f"{field_name} must not contain duplicate sources")
    record_ids = tuple(
        record.record_id for record in records if record.record_id is not None
    )
    if len(record_ids) != len(set(record_ids)):
        raise ValueError(f"{field_name} must not contain duplicate record ids")


@dataclass(frozen=True)
class PreparedTermMutation:
    """Validated candidate and recovery artifacts prepared before commit."""

    action: str
    resource_path: Path
    base_digest: str
    staged_path: Path
    recovery_path: Path | None
    candidate_records: tuple[TermRecord, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.action, "prepared term mutation action")
        _validate_path(self.resource_path, "term resource path")
        _validate_sha256_digest(
            self.base_digest,
            "prepared term mutation base digest",
        )
        _validate_path(self.staged_path, "term staged path")
        if self.staged_path == self.resource_path:
            raise ValueError("term staged path must differ from the resource path")
        if self.staged_path.parent != self.resource_path.parent:
            raise ValueError("term staged path must share the resource directory")

        if self.recovery_path is not None:
            _validate_path(self.recovery_path, "term recovery path")
            if self.recovery_path in (self.resource_path, self.staged_path):
                raise ValueError(
                    "term recovery path must differ from resource and staged paths"
                )
            if self.recovery_path.parent != self.resource_path.parent:
                raise ValueError(
                    "term recovery path must share the resource directory"
                )
        _validate_term_records(
            self.candidate_records,
            "prepared term candidate records",
        )


@dataclass(frozen=True)
class TermMutationReport:
    """Committed termbase mutation and its durable record snapshot."""

    action: str
    resource_path: Path
    committed_digest: str
    records: tuple[TermRecord, ...]
    created: int
    updated: int
    deleted: int
    imported: int
    overwritten: int

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.action, "term mutation report action")
        _validate_path(self.resource_path, "term mutation resource path")
        _validate_sha256_digest(
            self.committed_digest,
            "term mutation committed digest",
        )
        _validate_term_records(self.records, "term mutation report records")
        for field_name in (
            "created",
            "updated",
            "deleted",
            "imported",
            "overwritten",
        ):
            _validate_nonnegative_int(
                getattr(self, field_name),
                f"term mutation {field_name} count",
            )


@dataclass(frozen=True)
class TermCommitOutcome:
    """Typed commit result with explicit rollback and recovery semantics."""

    state: TermCommitState
    report: TermMutationReport | None
    error_code: str | None
    retryable: bool
    recovery_path: Path | None
    quarantined: bool
    safe_detail: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, TermCommitState):
            raise TypeError("term commit state must be a TermCommitState")
        if not isinstance(self.retryable, bool):
            raise TypeError("term commit retryable state must be a boolean")
        if not isinstance(self.quarantined, bool):
            raise TypeError("term commit quarantine state must be a boolean")
        if self.recovery_path is not None:
            _validate_path(self.recovery_path, "term commit recovery path")

        if self.state is TermCommitState.COMMITTED:
            if not isinstance(self.report, TermMutationReport):
                raise ValueError("committed term outcome must contain a report")
            if self.error_code is not None or self.safe_detail is not None:
                raise ValueError("committed term outcome must not contain an error")
            if self.retryable or self.quarantined:
                raise ValueError(
                    "committed term outcome cannot be retryable or quarantined"
                )
            return

        if self.report is not None:
            raise ValueError("non-committed term outcome must not contain a report")
        _validate_non_empty_string(
            self.error_code,
            "failed term outcome error code",
        )
        _validate_non_empty_string(
            self.safe_detail,
            "failed term outcome safe detail",
        )

        if self.state is TermCommitState.INDETERMINATE:
            if self.recovery_path is None:
                raise ValueError(
                    "indeterminate term outcome must provide a recovery path"
                )
            if not self.quarantined:
                raise ValueError(
                    "indeterminate term outcome must quarantine the resource"
                )
            if self.retryable:
                raise ValueError(
                    "indeterminate term outcome must not be directly retryable"
                )
        elif self.quarantined:
            raise ValueError(
                "only an indeterminate term outcome may quarantine the resource"
            )


@dataclass(frozen=True)
class TermCleanupReport:
    """Result of removing a redundant recovery artifact after commit."""

    cleaned: bool
    recovery_path: Path | None
    warning_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.cleaned, bool):
            raise TypeError("term cleanup state must be a boolean")
        if self.recovery_path is not None:
            _validate_path(self.recovery_path, "term cleanup recovery path")

        if self.cleaned:
            if self.recovery_path is not None or self.warning_code is not None:
                raise ValueError(
                    "successful term cleanup must not retain recovery warnings"
                )
            return

        if self.recovery_path is None:
            raise ValueError("failed term cleanup must identify the recovery path")
        _validate_non_empty_string(
            self.warning_code,
            "failed term cleanup warning code",
        )


@dataclass(frozen=True)
class ResourceConfig:
    """Persistent configuration for one TM or termbase resource."""

    id: str
    name: str
    kind: ResourceKind
    path: Path
    active: bool = True
    lookup: bool = True
    update: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("resource id must not be empty")
        if not self.name.strip():
            raise ValueError("resource name must not be empty")
        if not self.path.is_absolute():
            raise ValueError("resource path must be absolute")


@dataclass(frozen=True)
class TMSuggestion:
    """An exact translation-memory suggestion with resource provenance."""

    source: str
    target: str
    resource_id: str
    resource_name: str
    similarity: float = 1.0
    match_type: str = "EXACT"

    def __post_init__(self) -> None:
        if not self.source or not self.target:
            raise ValueError("TM suggestion source and target must not be empty")
        if not self.resource_id or not self.resource_name:
            raise ValueError("TM suggestion resource provenance must not be empty")
        if not 0.0 <= self.similarity <= 1.0:
            raise ValueError("TM suggestion similarity must be between 0 and 1")


@dataclass(frozen=True)
class TermSuggestion:
    """A glossary hit adapted for the editor frontend."""

    source_term: str
    target_term: str
    start_index: int
    end_index: int
    resource_id: str
    resource_name: str
    definition: str | None = None

    def __post_init__(self) -> None:
        if not self.source_term or not self.target_term:
            raise ValueError("term suggestion source and target must not be empty")
        if self.start_index < 0 or self.end_index <= self.start_index:
            raise ValueError("term suggestion range is invalid")
        if not self.resource_id or not self.resource_name:
            raise ValueError("term suggestion resource provenance must not be empty")


@dataclass(frozen=True)
class SuggestionBundle:
    """TM and termbase results for the same active segment."""

    tm_matches: tuple[TMSuggestion, ...] = ()
    terms: tuple[TermSuggestion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tm_matches, tuple) or not isinstance(self.terms, tuple):
            raise TypeError("suggestion collections must be tuples")


@dataclass(frozen=True)
class ImportReport:
    """Structured result of importing one language-resource file."""

    imported: int = 0
    skipped: int = 0
    overwritten: int = 0
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.imported, self.skipped, self.overwritten) < 0:
            raise ValueError("import counters must not be negative")
        if not isinstance(self.errors, tuple):
            raise TypeError("import errors must be a tuple")

    @property
    def succeeded(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class WriteReport:
    """Structured result of writing a confirmed translation."""

    written_resource_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.written_resource_ids, tuple) or not isinstance(self.errors, tuple):
            raise TypeError("write report collections must be tuples")

    @property
    def succeeded(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ConfirmResult:
    """Updated project state and resource-write evidence after confirmation."""

    project: EditorProject
    current_index: int
    write_report: WriteReport


@dataclass(frozen=True)
class ImportRequest:
    """Typed request for importing into a configured resource."""

    resource_id: str
    input_path: Path
    source_locale: str = ""
    target_locale: str = ""

    def __post_init__(self) -> None:
        if not self.resource_id.strip():
            raise ValueError("resource id must not be empty")
        if not self.input_path.is_absolute():
            raise ValueError("input path must be absolute")
