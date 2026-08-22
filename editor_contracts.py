"""Immutable cross-layer contracts for the LocalCAT desktop editor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from tm_contracts import (
    SearchOptions as SearchOptions,
    TMMatchType,
    TextMatcherState as TextMatcherState,
    TextMatchProfile,
)
from project_workspace_identity import (
    validate_document_id,
    validate_local_segment_id,
    validate_project_id,
)


DEFAULT_EDITOR_FONT_SIZE = 15
MIN_EDITOR_FONT_SIZE = 10
MAX_EDITOR_FONT_SIZE = 28
EDITOR_FONT_SIZE_STEP = 1
EDITOR_TM_CONTRACT_CODEC_VERSION = 1

_SAFE_DIAGNOSTIC_CODE = re.compile(
    r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z"
)
_LOWER_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_RECORD_ID = re.compile(r"canonical:[1-9][0-9]*\Z")
_LEGACY_RECORD_ID = re.compile(r"legacy:[0-9a-f]{64}\Z")
_OPAQUE_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_TM_ACTIVATION_PHASES = frozenset(("ACTIVATING", "COMPLETED"))


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


class SearchField(str, Enum):
    """Searchable raw fields in one JSON project segment."""

    SOURCE = "source"
    TARGET = "target"
    SPEAKER = "speaker"


class SearchScope(str, Enum):
    """Closed multi-document search scope; chunk is intentionally absent."""

    CURRENT_DOCUMENT = "current_document"
    ENTIRE_PROJECT = "entire_project"


class SegmentTranslationStatus(str, Enum):
    """Translation state derived from one JSON project segment."""

    UNFILLED = "unfilled"
    DRAFT = "draft"
    TRANSLATED = "translated"


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


class TermbaseImportHeaderMode(str, Enum):
    """How a confirmed physical column selection treats the first row."""

    FIRST_ROW = "first_row"
    NO_HEADER = "no_header"


class TMResourceDisplayMode(str, Enum):
    """Non-authoritative UI projection of one TM resource lifecycle."""

    LEGACY_EXACT_ONLY = "LEGACY_EXACT_ONLY"
    ACTIVATING = "ACTIVATING"
    CANONICAL_ACTIVE = "CANONICAL_ACTIVE"
    SOURCE_DIVERGED = "SOURCE_DIVERGED"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


def _validate_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_exact_non_empty_string(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_exact_raw_text(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if value == "":
        raise ValueError(f"{field_name} must not be empty")


def _validate_lower_sha256_digest(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if _LOWER_SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _validate_exact_nonnegative_int(value: object, field_name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")


def _validate_exact_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be an exact boolean")


def _validate_safe_code(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if len(value) > 96 or _SAFE_DIAGNOSTIC_CODE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe diagnostic code")


def _validate_safe_codes(value: object, field_name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    for code in value:
        _validate_safe_code(code, f"{field_name} item")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must not contain duplicates")


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


@dataclass(frozen=True, slots=True)
class RecentWorkspaceProject:
    """Device-local composite position for one ProjectPackage session."""

    path: Path
    project_id: str
    document_id: str
    local_segment_id: str
    index: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("recent workspace path must be absolute")
        validate_project_id(self.project_id)
        validate_document_id(self.document_id)
        validate_local_segment_id(self.local_segment_id)
        if type(self.index) is not int or self.index < 0:
            raise ValueError("recent workspace index must be nonnegative")


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
class TMPreferences:
    """Device-local TM query preferences shared across projects."""

    minimum_similarity: float = 0.60
    result_limit: int = 10

    def __post_init__(self) -> None:
        if type(self.minimum_similarity) is not float:
            raise TypeError("TM minimum similarity must be an exact float")
        if not math.isfinite(self.minimum_similarity) or not (
            0.60 <= self.minimum_similarity <= 1.00
        ):
            raise ValueError(
                "TM minimum similarity must be finite and between 0.60 and 1.00"
            )
        if type(self.result_limit) is not int:
            raise TypeError("TM result limit must be an exact integer")
        if self.result_limit != 10:
            raise ValueError("TM result limit must be fixed at 10")


@dataclass(frozen=True, slots=True)
class TMActivationPreflightView:
    """Body-free counts shown before one explicit TM activation."""

    resource_id: str
    resource_name: str
    valid_count: int
    invalid_count: int
    variant_count: int

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.resource_id,
            "TM activation resource id",
        )
        _validate_exact_non_empty_string(
            self.resource_name,
            "TM activation resource name",
        )
        _validate_exact_nonnegative_int(
            self.valid_count,
            "TM activation valid count",
        )
        _validate_exact_nonnegative_int(
            self.invalid_count,
            "TM activation invalid count",
        )
        _validate_exact_nonnegative_int(
            self.variant_count,
            "TM activation variant count",
        )
        if self.valid_count + self.invalid_count == 0:
            raise ValueError("TM activation preflight must describe source rows")
        if self.variant_count > self.valid_count:
            raise ValueError(
                "TM activation variants cannot exceed valid rows"
            )


@dataclass(frozen=True, slots=True)
class TMActivationOperationView:
    """Opaque, body-free lifecycle projection for one activation worker."""

    operation_id: str
    resource_id: str
    phase: str
    completed: bool
    succeeded: bool
    safe_code: str | None
    retryable: bool

    def __post_init__(self) -> None:
        if (
            type(self.operation_id) is not str
            or _OPAQUE_OPERATION_ID.fullmatch(self.operation_id) is None
        ):
            raise ValueError(
                "TM activation operation id must be opaque lowercase hex"
            )
        _validate_exact_non_empty_string(
            self.resource_id,
            "TM activation operation resource id",
        )
        if type(self.phase) is not str or self.phase not in _TM_ACTIVATION_PHASES:
            raise ValueError("TM activation operation phase is unsupported")
        _validate_exact_bool(
            self.completed,
            "TM activation operation completed state",
        )
        _validate_exact_bool(
            self.succeeded,
            "TM activation operation succeeded state",
        )
        _validate_exact_bool(
            self.retryable,
            "TM activation operation retryable state",
        )
        if not self.completed:
            if (
                self.phase != "ACTIVATING"
                or self.succeeded
                or self.safe_code is not None
                or self.retryable
            ):
                raise ValueError(
                    "running TM activation state is contradictory"
                )
            return
        if self.phase != "COMPLETED":
            raise ValueError("completed TM activation must use COMPLETED phase")
        if self.succeeded:
            if self.safe_code is not None or self.retryable:
                raise ValueError(
                    "successful TM activation cannot retain failure facts"
                )
            return
        _validate_safe_code(
            self.safe_code,
            "TM activation operation safe code",
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
class SpeakerInventoryItem:
    """One non-empty raw speaker in first-occurrence order."""

    raw_speaker: str
    count: int
    first_segment_id: str
    first_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.raw_speaker, str):
            raise TypeError("inventory raw speaker must be a string")
        if not self.raw_speaker:
            raise ValueError("inventory raw speaker must not be empty")
        if not isinstance(self.count, int) or isinstance(self.count, bool):
            raise TypeError("inventory count must be an integer")
        if self.count <= 0:
            raise ValueError("inventory count must be positive")
        if not isinstance(self.first_segment_id, str):
            raise TypeError("inventory first segment id must be a string")
        if not self.first_segment_id.strip():
            raise ValueError("inventory first segment id must not be empty")
        if not isinstance(self.first_index, int) or isinstance(
            self.first_index,
            bool,
        ):
            raise TypeError("inventory first index must be an integer")
        if self.first_index < 0:
            raise ValueError("inventory first index must not be negative")


@dataclass(frozen=True)
class SpeakerInventory:
    """Deterministic, read-only projection of project raw speakers."""

    items: tuple[SpeakerInventoryItem, ...]
    empty_count: int
    segment_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise TypeError("inventory items must be a tuple")
        if not all(isinstance(item, SpeakerInventoryItem) for item in self.items):
            raise TypeError("inventory items must contain SpeakerInventoryItem values")
        for name, value in (
            ("empty count", self.empty_count),
            ("segment count", self.segment_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"inventory {name} must be an integer")
            if value < 0:
                raise ValueError(f"inventory {name} must not be negative")
        raw_speakers = tuple(item.raw_speaker for item in self.items)
        if len(raw_speakers) != len(set(raw_speakers)):
            raise ValueError("inventory raw speakers must be unique")
        first_indices = tuple(item.first_index for item in self.items)
        if first_indices != tuple(sorted(first_indices)):
            raise ValueError("inventory items must follow first-occurrence order")
        if len(first_indices) != len(set(first_indices)):
            raise ValueError("inventory first indices must be unique")
        if any(index >= self.segment_count for index in first_indices):
            raise ValueError("inventory first index must reference a project segment")
        if self.empty_count + sum(item.count for item in self.items) != self.segment_count:
            raise ValueError("inventory counts must cover every project segment")


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
class PreprocessPreferences:
    """Device-local saved literal rules and segment-status selection."""

    rules: tuple[LiteralReplaceRule, ...] = ()
    include_draft: bool = True
    include_confirmed: bool = True

    def __post_init__(self) -> None:
        if type(self.rules) is not tuple:
            raise TypeError("preprocess preference rules must be a tuple")
        if not all(type(rule) is LiteralReplaceRule for rule in self.rules):
            raise TypeError(
                "preprocess preference rules must contain exact LiteralReplaceRule values"
            )
        if type(self.include_draft) is not bool:
            raise TypeError("preprocess include_draft must be an exact boolean")
        if type(self.include_confirmed) is not bool:
            raise TypeError("preprocess include_confirmed must be an exact boolean")
        if not self.include_draft and not self.include_confirmed:
            raise ValueError("at least one preprocess segment status must be selected")


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


@dataclass(frozen=True, slots=True)
class SuggestionQueryIdentity:
    """Current-segment identity captured when a TM query is issued.

    ``query_epoch`` is the Controller-owned aggregate epoch.  A resource,
    capability, threshold, project, segment, or source change advances that
    one value; the underlying runtime generations remain application-private.
    """

    project_session_id: str
    segment_id: str
    source_digest: str
    query_epoch: int

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.project_session_id,
            "suggestion project session id",
        )
        _validate_exact_non_empty_string(
            self.segment_id,
            "suggestion segment id",
        )
        _validate_lower_sha256_digest(
            self.source_digest,
            "suggestion source digest",
        )
        _validate_exact_nonnegative_int(
            self.query_epoch,
            "suggestion query epoch",
        )


@dataclass(frozen=True, slots=True)
class TMSuggestionProvenance:
    """Body-free resource display facts attached to one TM suggestion."""

    resource_name: str
    resource_mode: TMResourceDisplayMode

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.resource_name,
            "TM suggestion resource name",
        )
        if type(self.resource_mode) is not TMResourceDisplayMode:
            raise TypeError(
                "TM suggestion resource mode must be TMResourceDisplayMode"
            )


@dataclass(frozen=True, slots=True)
class TMSuggestion:
    """Safe UI projection of one Core or legacy TM result.

    Match category and score remain owned by Feature 5 Core.  This projection
    deliberately omits evidence, folds, candidate proof, intermediate scorer
    values, paths, and mutable collections.
    """

    resource_id: str
    record_id: str
    query_source: str
    matched_source: str
    target: str
    match_type: TMMatchType
    final_similarity: float
    provenance: TMSuggestionProvenance
    query_identity: SuggestionQueryIdentity

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.resource_id,
            "TM suggestion resource id",
        )
        if type(self.record_id) is not str:
            raise TypeError("TM suggestion record id must be an exact string")
        canonical_record = _CANONICAL_RECORD_ID.fullmatch(self.record_id) is not None
        legacy_record = _LEGACY_RECORD_ID.fullmatch(self.record_id) is not None
        if not canonical_record and not legacy_record:
            raise ValueError("TM suggestion record id has an unsupported shape")
        for field_name, value in (
            ("query source", self.query_source),
            ("matched source", self.matched_source),
            ("target", self.target),
        ):
            _validate_exact_raw_text(
                value,
                f"TM suggestion {field_name}",
            )
        if type(self.match_type) is not TMMatchType:
            raise TypeError("TM suggestion match type must be TMMatchType")
        if type(self.final_similarity) is not float:
            raise TypeError("TM suggestion final similarity must be an exact float")
        if not math.isfinite(self.final_similarity) or not (
            0.0 <= self.final_similarity <= 1.0
        ):
            raise ValueError(
                "TM suggestion final similarity must be finite and between 0 and 1"
            )
        if type(self.provenance) is not TMSuggestionProvenance:
            raise TypeError(
                "TM suggestion provenance must be TMSuggestionProvenance"
            )
        if type(self.query_identity) is not SuggestionQueryIdentity:
            raise TypeError(
                "TM suggestion query identity must be SuggestionQueryIdentity"
            )

        if self.match_type is TMMatchType.FUZZY:
            if self.query_source == self.matched_source:
                raise ValueError(
                    "fuzzy TM suggestion must expose a distinct matched source"
                )
            if not canonical_record:
                raise ValueError("fuzzy TM suggestion must use a canonical record id")
            if self.provenance.resource_mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
                raise ValueError("legacy exact-only resources cannot produce fuzzy")
        else:
            if self.final_similarity != 1.0:
                raise ValueError(
                    f"{self.match_type.value} TM suggestion similarity must be 1.0"
                )
            if self.query_source != self.matched_source:
                raise ValueError(
                    f"{self.match_type.value} TM suggestion sources must be identical"
                )
        if self.provenance.resource_mode is TMResourceDisplayMode.UNAVAILABLE:
            raise ValueError("unavailable resources cannot produce suggestions")
        if legacy_record:
            if self.match_type is not TMMatchType.EXACT:
                raise ValueError("legacy TM records can only produce exact suggestions")
            if self.provenance.resource_mode not in (
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
                TMResourceDisplayMode.ACTIVATING,
            ):
                raise ValueError(
                    "legacy TM record id requires legacy or activating provenance"
                )
        if self.provenance.resource_mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
            if not legacy_record or self.match_type is not TMMatchType.EXACT:
                raise ValueError(
                    "legacy exact-only provenance requires a legacy exact record"
                )


@dataclass(frozen=True, slots=True)
class TMResourceStatus:
    """Safe display projection for one configured TM resource."""

    resource_id: str
    resource_name: str
    mode: TMResourceDisplayMode
    exact_available: bool
    context_available: bool
    fuzzy_available: bool
    safe_codes: tuple[str, ...]
    retryable: bool

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.resource_id,
            "TM resource status id",
        )
        _validate_exact_non_empty_string(
            self.resource_name,
            "TM resource status name",
        )
        if type(self.mode) is not TMResourceDisplayMode:
            raise TypeError("TM resource status mode must be TMResourceDisplayMode")
        for field_name, value in (
            ("exact available", self.exact_available),
            ("context available", self.context_available),
            ("fuzzy available", self.fuzzy_available),
            ("retryable", self.retryable),
        ):
            _validate_exact_bool(value, f"TM resource status {field_name}")
        _validate_safe_codes(self.safe_codes, "TM resource status safe codes")
        if self.retryable and not self.safe_codes:
            raise ValueError("retryable TM resource status requires a safe code")
        if (self.context_available or self.fuzzy_available) and not self.exact_available:
            raise ValueError(
                "advanced TM availability requires canonical exact availability"
            )
        if self.mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
            if not self.exact_available or self.context_available or self.fuzzy_available:
                raise ValueError(
                    "legacy exact-only status must expose exact and close advanced matches"
                )
        if self.mode is TMResourceDisplayMode.UNAVAILABLE:
            if self.exact_available or self.context_available or self.fuzzy_available:
                raise ValueError("unavailable TM resource cannot expose match capability")
            if not self.safe_codes:
                raise ValueError("unavailable TM resource requires a safe code")
        if self.mode in (
            TMResourceDisplayMode.DEGRADED,
            TMResourceDisplayMode.SOURCE_DIVERGED,
        ) and not self.safe_codes:
            raise ValueError("degraded or diverged TM resource requires a safe code")
        if self.mode in (
            TMResourceDisplayMode.CANONICAL_ACTIVE,
            TMResourceDisplayMode.SOURCE_DIVERGED,
        ) and not self.exact_available:
            raise ValueError("canonical TM resource must retain exact availability")


@dataclass(frozen=True, slots=True)
class RetrievalDisplayState:
    """Non-authoritative safe projection of Core retrieval availability."""

    context_available: bool
    fuzzy_available: bool
    safe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_exact_bool(
            self.context_available,
            "retrieval context available",
        )
        _validate_exact_bool(
            self.fuzzy_available,
            "retrieval fuzzy available",
        )
        _validate_safe_codes(self.safe_codes, "retrieval safe codes")
        if (
            not self.context_available or not self.fuzzy_available
        ) and not self.safe_codes:
            raise ValueError(
                "closed retrieval availability requires at least one safe code"
            )


class FuzzyValidationState(str, Enum):
    """Process-local Gate D lifecycle without retrieval authority."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class FuzzyValidationDisplay:
    """Body-safe UI projection that never authorizes fuzzy retrieval."""

    state: FuzzyValidationState
    safe_code: str | None

    def __post_init__(self) -> None:
        if type(self.state) is not FuzzyValidationState:
            raise TypeError(
                "fuzzy validation state must be FuzzyValidationState"
            )
        if self.state is FuzzyValidationState.FAILED:
            _validate_safe_code(
                self.safe_code,
                "fuzzy validation failure code",
            )
        elif self.safe_code is not None:
            raise ValueError(
                "non-failed fuzzy validation cannot carry a safe code"
            )


@dataclass(frozen=True, slots=True)
class TMSuggestionReport:
    """One immutable, body-safe current-segment TM query projection."""

    suggestions: tuple[TMSuggestion, ...]
    resource_statuses: tuple[TMResourceStatus, ...]
    retrieval_status: RetrievalDisplayState
    query_identity: SuggestionQueryIdentity

    def __post_init__(self) -> None:
        if type(self.suggestions) is not tuple or any(
            type(suggestion) is not TMSuggestion
            for suggestion in self.suggestions
        ):
            raise TypeError(
                "TM suggestion report suggestions must be an exact tuple"
            )
        if len(self.suggestions) > 10:
            raise ValueError("TM suggestion report may contain at most ten items")
        if type(self.resource_statuses) is not tuple or any(
            type(status) is not TMResourceStatus
            for status in self.resource_statuses
        ):
            raise TypeError(
                "TM suggestion report statuses must be an exact tuple"
            )
        if type(self.retrieval_status) is not RetrievalDisplayState:
            raise TypeError(
                "TM suggestion report retrieval status must be RetrievalDisplayState"
            )
        if type(self.query_identity) is not SuggestionQueryIdentity:
            raise TypeError(
                "TM suggestion report query identity must be SuggestionQueryIdentity"
            )

        self.query_identity.__post_init__()
        self.retrieval_status.__post_init__()
        status_by_resource_id: dict[str, TMResourceStatus] = {}
        for status in self.resource_statuses:
            status.__post_init__()
            if status.resource_id in status_by_resource_id:
                raise ValueError(
                    "TM suggestion report resource statuses must be unique"
                )
            status_by_resource_id[status.resource_id] = status

        result_identities: set[tuple[str, str]] = set()
        query_source: str | None = None
        for suggestion in self.suggestions:
            suggestion.__post_init__()
            if suggestion.query_identity is not self.query_identity:
                raise ValueError(
                    "TM suggestions must share the report query identity"
                )
            result_identity = (suggestion.resource_id, suggestion.record_id)
            if result_identity in result_identities:
                raise ValueError(
                    "TM suggestion report must not contain duplicate records"
                )
            result_identities.add(result_identity)
            if query_source is None:
                query_source = suggestion.query_source
            elif suggestion.query_source != query_source:
                raise ValueError(
                    "TM suggestions must share one raw query source"
                )
            status = status_by_resource_id.get(suggestion.resource_id)
            if status is None:
                raise ValueError(
                    "TM suggestion resource must have a projected status"
                )
            if (
                suggestion.provenance.resource_name != status.resource_name
                or suggestion.provenance.resource_mode is not status.mode
            ):
                raise ValueError(
                    "TM suggestion provenance must match its resource status"
                )
        if (
            query_source is not None
            and hashlib.sha256(query_source.encode("utf-8")).hexdigest()
            != self.query_identity.source_digest
        ):
            raise ValueError(
                "TM suggestion query source must match the identity digest"
            )


@dataclass(frozen=True, slots=True)
class TMThresholdUpdateOutcome:
    """Body-free result of one Controller-owned threshold update."""

    succeeded: bool
    preferences: TMPreferences
    safe_code: str | None

    def __post_init__(self) -> None:
        _validate_exact_bool(self.succeeded, "TM threshold update succeeded")
        if type(self.preferences) is not TMPreferences:
            raise TypeError(
                "TM threshold update preferences must be exact TMPreferences"
            )
        self.preferences.__post_init__()
        if self.succeeded:
            if self.safe_code is not None:
                raise ValueError(
                    "successful TM threshold update cannot contain a safe code"
                )
            return
        _validate_safe_code(
            self.safe_code,
            "failed TM threshold update safe code",
        )


_BASIC_DISPLAY_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
)
_TEXT_V1_DISPLAY_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
    TextMatchProfile.CONFIGURABLE_TEXT_V1,
)


@dataclass(frozen=True, slots=True)
class TextMatcherDisplayState:
    """One-way display projection of the Core matcher authority."""

    state: TextMatcherState
    supported_profiles: tuple[TextMatchProfile, ...]
    safe_reason: str | None

    def __post_init__(self) -> None:
        if type(self.state) is not TextMatcherState:
            raise TypeError("matcher display state must be TextMatcherState")
        if type(self.supported_profiles) is not tuple:
            raise TypeError("matcher display supported profiles must be an exact tuple")
        if any(type(profile) is not TextMatchProfile for profile in self.supported_profiles):
            raise TypeError(
                "matcher display profiles must contain TextMatchProfile values"
            )
        if self.state is TextMatcherState.UNAVAILABLE:
            expected_profiles: tuple[TextMatchProfile, ...] = ()
            if self.safe_reason is None:
                raise ValueError("unavailable matcher display requires a safe reason")
            _validate_safe_code(self.safe_reason, "matcher display safe reason")
        elif self.state is TextMatcherState.BASIC_VALIDATED:
            expected_profiles = _BASIC_DISPLAY_PROFILES
            if self.safe_reason is not None:
                raise ValueError("available matcher display must omit safe reason")
        else:
            expected_profiles = _TEXT_V1_DISPLAY_PROFILES
            if self.safe_reason is not None:
                raise ValueError("available matcher display must omit safe reason")
        if self.supported_profiles != expected_profiles:
            raise ValueError(
                "matcher display profiles must exactly match the Core state"
            )


@dataclass(frozen=True, slots=True)
class ProjectToolCapability:
    """Single-JSON project-tool availability for one editor session."""

    project_session_id: str | None
    single_json_tools_available: bool
    project_kind: str
    unavailable_reason: str | None

    def __post_init__(self) -> None:
        if self.project_session_id is not None:
            _validate_exact_non_empty_string(
                self.project_session_id,
                "project tool session id",
            )
        _validate_exact_bool(
            self.single_json_tools_available,
            "single JSON tools available state",
        )
        _validate_exact_non_empty_string(
            self.project_kind,
            "project tool project kind",
        )

        if self.single_json_tools_available:
            if self.project_session_id is None:
                raise ValueError(
                    "available project tools require a project session"
                )
            if self.project_kind != "json":
                raise ValueError(
                    "single JSON tools require the json project kind"
                )
            if self.unavailable_reason is not None:
                raise ValueError(
                    "available project tools must omit unavailable reason"
                )
            return

        if self.unavailable_reason is None:
            raise ValueError(
                "unavailable project tools require an unavailable reason"
            )
        _validate_safe_code(
            self.unavailable_reason,
            "project tools unavailable reason",
        )


_SEARCH_FIELD_ORDER = {
    SearchField.SOURCE: 0,
    SearchField.TARGET: 1,
    SearchField.SPEAKER: 2,
}


@dataclass(frozen=True, slots=True)
class ProjectSearchRequest:
    """One field selection and Core-owned option set for project search."""

    query: str
    fields: tuple[SearchField, ...]
    options: SearchOptions
    status: SegmentTranslationStatus | None = None

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(self.query, "project search query")
        if type(self.fields) is not tuple:
            raise TypeError("project search fields must be an exact tuple")
        if not self.fields:
            raise ValueError("project search fields must not be empty")
        if any(type(field) is not SearchField for field in self.fields):
            raise TypeError(
                "project search fields must contain SearchField values"
            )
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("project search fields must not contain duplicates")
        field_order = tuple(_SEARCH_FIELD_ORDER[field] for field in self.fields)
        if field_order != tuple(sorted(field_order)):
            raise ValueError(
                "project search fields must use source-target-speaker order"
            )
        if type(self.options) is not SearchOptions:
            raise TypeError(
                "project search options must be exact Core SearchOptions"
            )
        self.options.__post_init__()
        if (
            self.status is not None
            and type(self.status) is not SegmentTranslationStatus
        ):
            raise TypeError(
                "project search status must be SegmentTranslationStatus or None"
            )


@dataclass(frozen=True, slots=True)
class WorkspaceSearchRequest:
    """Workspace-only field selection plus a closed document scope."""

    query: str
    fields: tuple[SearchField, ...]
    options: SearchOptions
    status: SegmentTranslationStatus | None = None
    scope: SearchScope = SearchScope.ENTIRE_PROJECT

    def __post_init__(self) -> None:
        ProjectSearchRequest(
            query=self.query,
            fields=self.fields,
            options=self.options,
            status=self.status,
        )
        if type(self.scope) is not SearchScope:
            raise TypeError("workspace search scope must be SearchScope")


@dataclass(frozen=True, slots=True)
class ProjectSearchHit:
    """One half-open match range bound to stable project navigation identity."""

    segment_id: str
    segment_index: int
    field: SearchField
    start_index: int
    end_index: int
    preview: str

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.segment_id,
            "project search hit segment id",
        )
        _validate_exact_nonnegative_int(
            self.segment_index,
            "project search hit segment index",
        )
        if type(self.field) is not SearchField:
            raise TypeError("project search hit field must be SearchField")
        _validate_exact_nonnegative_int(
            self.start_index,
            "project search hit start index",
        )
        _validate_exact_nonnegative_int(
            self.end_index,
            "project search hit end index",
        )
        if self.end_index <= self.start_index:
            raise ValueError(
                "project search hit end index must be greater than start index"
            )
        _validate_exact_raw_text(self.preview, "project search hit preview")
        if self.end_index > len(self.preview):
            raise ValueError(
                "project search hit end index must not exceed preview length"
            )


@dataclass(frozen=True, slots=True)
class WorkspaceSearchHit:
    """One workspace match bound to document and local segment identity."""

    document_id: str
    local_segment_id: str
    project_global_index: int
    field: SearchField
    start_index: int
    end_index: int
    preview: str

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        validate_local_segment_id(self.local_segment_id)
        _validate_exact_nonnegative_int(
            self.project_global_index,
            "workspace search hit global index",
        )
        if type(self.field) is not SearchField:
            raise TypeError("workspace search hit field must be SearchField")
        _validate_exact_nonnegative_int(
            self.start_index,
            "workspace search hit start index",
        )
        _validate_exact_nonnegative_int(
            self.end_index,
            "workspace search hit end index",
        )
        if self.end_index <= self.start_index:
            raise ValueError("workspace search hit range must be nonempty")
        _validate_exact_raw_text(self.preview, "workspace search hit preview")
        if self.end_index > len(self.preview):
            raise ValueError("workspace search hit range exceeds preview")


@dataclass(frozen=True, slots=True)
class ProjectSearchReport:
    """Stable ordered hits plus the exact Integration matcher projection."""

    hits: tuple[ProjectSearchHit, ...]
    capability: TextMatcherDisplayState

    def __post_init__(self) -> None:
        if type(self.hits) is not tuple:
            raise TypeError("project search report hits must be an exact tuple")
        if any(type(hit) is not ProjectSearchHit for hit in self.hits):
            raise TypeError(
                "project search report hits must contain ProjectSearchHit values"
            )
        for hit in self.hits:
            hit.__post_init__()
        if type(self.capability) is not TextMatcherDisplayState:
            raise TypeError(
                "project search report capability must be TextMatcherDisplayState"
            )
        self.capability.__post_init__()
        if (
            self.capability.state is TextMatcherState.UNAVAILABLE
            and self.hits
        ):
            raise ValueError(
                "unavailable project search report cannot contain hits"
            )

        identities_by_index: dict[int, str] = {}
        indexes_by_identity: dict[str, int] = {}
        hit_identities: list[tuple[str, SearchField, int, int]] = []
        sort_keys: list[tuple[int, int, int, int]] = []
        for hit in self.hits:
            known_identity = identities_by_index.setdefault(
                hit.segment_index,
                hit.segment_id,
            )
            if known_identity != hit.segment_id:
                raise ValueError(
                    "project search segment index must have one stable identity"
                )
            known_index = indexes_by_identity.setdefault(
                hit.segment_id,
                hit.segment_index,
            )
            if known_index != hit.segment_index:
                raise ValueError(
                    "project search segment identity must have one stable index"
                )
            hit_identities.append(
                (
                    hit.segment_id,
                    hit.field,
                    hit.start_index,
                    hit.end_index,
                )
            )
            sort_keys.append(
                (
                    hit.segment_index,
                    _SEARCH_FIELD_ORDER[hit.field],
                    hit.start_index,
                    hit.end_index,
                )
            )
        if len(hit_identities) != len(set(hit_identities)):
            raise ValueError(
                "project search report must not contain duplicate hits"
            )
        if sort_keys != sorted(sort_keys):
            raise ValueError(
                "project search report hits must use stable project order"
            )

    @property
    def total(self) -> int:
        """Return the derived result count without duplicating report state."""

        return len(self.hits)


@dataclass(frozen=True, slots=True)
class WorkspaceSearchReport:
    """Stable composite workspace hits plus the shared matcher projection."""

    hits: tuple[WorkspaceSearchHit, ...]
    capability: TextMatcherDisplayState

    def __post_init__(self) -> None:
        if type(self.hits) is not tuple or any(
            type(hit) is not WorkspaceSearchHit for hit in self.hits
        ):
            raise TypeError(
                "workspace search hits must be exact WorkspaceSearchHit values"
            )
        for hit in self.hits:
            hit.__post_init__()
        if type(self.capability) is not TextMatcherDisplayState:
            raise TypeError("workspace search capability must be exact")
        self.capability.__post_init__()
        if self.capability.state is TextMatcherState.UNAVAILABLE and self.hits:
            raise ValueError("unavailable workspace search cannot contain hits")
        identity_by_index: dict[int, tuple[str, str]] = {}
        index_by_identity: dict[tuple[str, str], int] = {}
        identities: list[tuple[str, str, SearchField, int, int]] = []
        sort_keys: list[tuple[int, int, int, int]] = []
        for hit in self.hits:
            identity = (hit.document_id, hit.local_segment_id)
            if identity_by_index.setdefault(
                hit.project_global_index,
                identity,
            ) != identity:
                raise ValueError("workspace global index changed identity")
            if index_by_identity.setdefault(
                identity,
                hit.project_global_index,
            ) != hit.project_global_index:
                raise ValueError("workspace identity changed global index")
            identities.append(
                (
                    hit.document_id,
                    hit.local_segment_id,
                    hit.field,
                    hit.start_index,
                    hit.end_index,
                )
            )
            sort_keys.append(
                (
                    hit.project_global_index,
                    _SEARCH_FIELD_ORDER[hit.field],
                    hit.start_index,
                    hit.end_index,
                )
            )
        if len(identities) != len(set(identities)):
            raise ValueError("workspace search report contains duplicate hits")
        if sort_keys != sorted(sort_keys):
            raise ValueError("workspace search hits must use stable project order")

    @property
    def total(self) -> int:
        return len(self.hits)


@dataclass(frozen=True, slots=True)
class LegacyExactTMSuggestion:
    """Temporary explicit bridge for the pre-integration exact-only UI.

    This type is not accepted by the new strict UI contract codec.  It keeps
    existing consumers honest until the Controller adapter starts issuing the
    double-source ``TMSuggestion`` contract.
    """

    source: str
    target: str
    resource_id: str
    resource_name: str
    similarity: float = 1.0
    match_type: str = "EXACT"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source", self.source),
            ("target", self.target),
            ("resource id", self.resource_id),
            ("resource name", self.resource_name),
        ):
            validator = (
                _validate_exact_raw_text
                if field_name in ("source", "target")
                else _validate_exact_non_empty_string
            )
            validator(
                value,
                f"legacy TM suggestion {field_name}",
            )
        if type(self.similarity) is not float:
            raise TypeError("legacy TM suggestion similarity must be an exact float")
        if not math.isfinite(self.similarity) or self.similarity != 1.0:
            raise ValueError("legacy TM suggestion similarity must be 1.0")
        if type(self.match_type) is not str:
            raise TypeError("legacy TM suggestion match type must be an exact string")
        if self.match_type != "EXACT":
            raise ValueError("legacy TM suggestion match type must be EXACT")


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

    tm_matches: tuple[LegacyExactTMSuggestion, ...] = ()
    terms: tuple[TermSuggestion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tm_matches, tuple) or not isinstance(self.terms, tuple):
            raise TypeError("suggestion collections must be tuples")
        if not all(
            type(suggestion) is LegacyExactTMSuggestion
            for suggestion in self.tm_matches
        ):
            raise TypeError(
                "legacy suggestion bundle must contain LegacyExactTMSuggestion values"
            )


type EditorTMContract = (
    SuggestionQueryIdentity
    | TMSuggestionProvenance
    | TMSuggestion
    | TMResourceStatus
    | RetrievalDisplayState
    | TextMatcherDisplayState
)


def _editor_tm_contract_payload(
    contract: EditorTMContract,
) -> tuple[str, dict[str, Any]]:
    if type(contract) is SuggestionQueryIdentity:
        contract.__post_init__()
        return "SuggestionQueryIdentity", {
            "project_session_id": contract.project_session_id,
            "query_epoch": contract.query_epoch,
            "segment_id": contract.segment_id,
            "source_digest": contract.source_digest,
        }
    if type(contract) is TMSuggestionProvenance:
        contract.__post_init__()
        return "TMSuggestionProvenance", {
            "resource_mode": contract.resource_mode.value,
            "resource_name": contract.resource_name,
        }
    if type(contract) is TMSuggestion:
        contract.__post_init__()
        _, identity_payload = _editor_tm_contract_payload(
            contract.query_identity
        )
        _, provenance_payload = _editor_tm_contract_payload(
            contract.provenance
        )
        return "TMSuggestion", {
            "final_similarity": contract.final_similarity,
            "match_type": contract.match_type.value,
            "matched_source": contract.matched_source,
            "provenance": provenance_payload,
            "query_identity": identity_payload,
            "query_source": contract.query_source,
            "record_id": contract.record_id,
            "resource_id": contract.resource_id,
            "target": contract.target,
        }
    if type(contract) is TMResourceStatus:
        contract.__post_init__()
        return "TMResourceStatus", {
            "context_available": contract.context_available,
            "exact_available": contract.exact_available,
            "fuzzy_available": contract.fuzzy_available,
            "mode": contract.mode.value,
            "resource_id": contract.resource_id,
            "resource_name": contract.resource_name,
            "retryable": contract.retryable,
            "safe_codes": list(contract.safe_codes),
        }
    if type(contract) is RetrievalDisplayState:
        contract.__post_init__()
        return "RetrievalDisplayState", {
            "context_available": contract.context_available,
            "fuzzy_available": contract.fuzzy_available,
            "safe_codes": list(contract.safe_codes),
        }
    if type(contract) is TextMatcherDisplayState:
        contract.__post_init__()
        return "TextMatcherDisplayState", {
            "safe_reason": contract.safe_reason,
            "state": contract.state.value,
            "supported_profiles": [
                profile.value for profile in contract.supported_profiles
            ],
        }
    raise TypeError(
        f"unsupported editor TM contract type: {type(contract).__name__}"
    )


def editor_tm_contract_to_json(contract: EditorTMContract) -> str:
    """Encode one safe UI contract into deterministic versioned JSON."""

    contract_type, payload = _editor_tm_contract_payload(contract)
    return json.dumps(
        {
            "contract_type": contract_type,
            "contract_version": EDITOR_TM_CONTRACT_CODEC_VERSION,
            "payload": payload,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _editor_tm_mapping(value: object) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ValueError("editor TM contract value must be a JSON object")
    if any(type(key) is not str for key in value):
        raise ValueError("editor TM contract object keys must be strings")
    return value


def _editor_tm_strict_fields(
    value: object,
    expected_fields: tuple[str, ...],
) -> Mapping[str, Any]:
    mapping = _editor_tm_mapping(value)
    if set(mapping) != set(expected_fields):
        raise ValueError("editor TM contract fields do not match the schema")
    return mapping


def _editor_tm_string_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("editor TM contract tuple must be a JSON array")
    if any(type(item) is not str for item in value):
        raise ValueError("editor TM contract tuple values must be strings")
    return tuple(value)


def _decode_suggestion_query_identity(value: object) -> SuggestionQueryIdentity:
    payload = _editor_tm_strict_fields(
        value,
        (
            "project_session_id",
            "query_epoch",
            "segment_id",
            "source_digest",
        ),
    )
    return SuggestionQueryIdentity(
        project_session_id=payload["project_session_id"],
        segment_id=payload["segment_id"],
        source_digest=payload["source_digest"],
        query_epoch=payload["query_epoch"],
    )


def _decode_tm_suggestion_provenance(value: object) -> TMSuggestionProvenance:
    payload = _editor_tm_strict_fields(
        value,
        ("resource_mode", "resource_name"),
    )
    return TMSuggestionProvenance(
        resource_name=payload["resource_name"],
        resource_mode=TMResourceDisplayMode(payload["resource_mode"]),
    )


def _decode_tm_suggestion(value: object) -> TMSuggestion:
    payload = _editor_tm_strict_fields(
        value,
        (
            "final_similarity",
            "match_type",
            "matched_source",
            "provenance",
            "query_identity",
            "query_source",
            "record_id",
            "resource_id",
            "target",
        ),
    )
    return TMSuggestion(
        resource_id=payload["resource_id"],
        record_id=payload["record_id"],
        query_source=payload["query_source"],
        matched_source=payload["matched_source"],
        target=payload["target"],
        match_type=TMMatchType(payload["match_type"]),
        final_similarity=payload["final_similarity"],
        provenance=_decode_tm_suggestion_provenance(payload["provenance"]),
        query_identity=_decode_suggestion_query_identity(
            payload["query_identity"]
        ),
    )


def _decode_tm_resource_status(value: object) -> TMResourceStatus:
    payload = _editor_tm_strict_fields(
        value,
        (
            "context_available",
            "exact_available",
            "fuzzy_available",
            "mode",
            "resource_id",
            "resource_name",
            "retryable",
            "safe_codes",
        ),
    )
    return TMResourceStatus(
        resource_id=payload["resource_id"],
        resource_name=payload["resource_name"],
        mode=TMResourceDisplayMode(payload["mode"]),
        exact_available=payload["exact_available"],
        context_available=payload["context_available"],
        fuzzy_available=payload["fuzzy_available"],
        safe_codes=_editor_tm_string_tuple(payload["safe_codes"]),
        retryable=payload["retryable"],
    )


def _decode_retrieval_display_state(value: object) -> RetrievalDisplayState:
    payload = _editor_tm_strict_fields(
        value,
        ("context_available", "fuzzy_available", "safe_codes"),
    )
    return RetrievalDisplayState(
        context_available=payload["context_available"],
        fuzzy_available=payload["fuzzy_available"],
        safe_codes=_editor_tm_string_tuple(payload["safe_codes"]),
    )


def _decode_text_matcher_display_state(value: object) -> TextMatcherDisplayState:
    payload = _editor_tm_strict_fields(
        value,
        ("safe_reason", "state", "supported_profiles"),
    )
    profiles = _editor_tm_string_tuple(payload["supported_profiles"])
    return TextMatcherDisplayState(
        state=TextMatcherState(payload["state"]),
        supported_profiles=tuple(TextMatchProfile(profile) for profile in profiles),
        safe_reason=payload["safe_reason"],
    )


def _decode_editor_tm_contract(
    contract_type: object,
    payload: object,
) -> EditorTMContract:
    if type(contract_type) is not str:
        raise ValueError("editor TM contract type must be a string")
    decoders = {
        "SuggestionQueryIdentity": _decode_suggestion_query_identity,
        "TMSuggestionProvenance": _decode_tm_suggestion_provenance,
        "TMSuggestion": _decode_tm_suggestion,
        "TMResourceStatus": _decode_tm_resource_status,
        "RetrievalDisplayState": _decode_retrieval_display_state,
        "TextMatcherDisplayState": _decode_text_matcher_display_state,
    }
    decoder = decoders.get(contract_type)
    if decoder is None:
        raise ValueError("unsupported editor TM contract type")
    return decoder(payload)


def editor_tm_contract_from_json(serialized: str) -> EditorTMContract:
    """Decode a strict UI contract without accepting body-bearing internals."""

    if type(serialized) is not str:
        raise TypeError("serialized editor TM contract must be an exact string")

    class _DuplicateEditorTMKey(ValueError):
        pass

    def reject_duplicate_object(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for key, item in pairs:
            if key in decoded:
                raise _DuplicateEditorTMKey
            decoded[key] = item
        return decoded

    def reject_non_finite(_value: str) -> None:
        raise ValueError("non-finite editor TM contract number is not allowed")

    try:
        value = json.loads(
            serialized,
            parse_constant=reject_non_finite,
            object_pairs_hook=reject_duplicate_object,
        )
        envelope = _editor_tm_strict_fields(
            value,
            ("contract_type", "contract_version", "payload"),
        )
        version = envelope["contract_version"]
        if type(version) is not int:
            raise ValueError("editor TM contract version must be an integer")
        if version != EDITOR_TM_CONTRACT_CODEC_VERSION:
            raise ValueError("unsupported editor TM contract version")
        return _decode_editor_tm_contract(
            envelope["contract_type"],
            envelope["payload"],
        )
    except (
        json.JSONDecodeError,
        _DuplicateEditorTMKey,
        TypeError,
        ValueError,
    ):
        raise ValueError("serialized editor TM contract is invalid") from None


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


MAX_TERMBASE_IMPORT_PREVIEW_COLUMNS = 256
MAX_TERMBASE_IMPORT_PREVIEW_LABEL_CHARS = 256


@dataclass(frozen=True)
class TermbaseImportSourceIdentity:
    """Qt-safe exact projection of one Parser sealed-source identity."""

    relative_reference_sha256: str
    regular_file_identity: str
    original_size: int
    original_mtime_ns: int
    content_sha256: str
    byte_count: int
    schema_version: int

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.relative_reference_sha256, "relative reference digest"),
            (self.content_sha256, "content digest"),
        ):
            if type(value) is not str or not _LOWER_SHA256_DIGEST.fullmatch(value):
                raise ValueError(f"termbase preview {field_name} must be lowercase SHA-256")
        _validate_exact_non_empty_string(
            self.regular_file_identity,
            "termbase preview regular file identity",
        )
        _validate_exact_nonnegative_int(
            self.original_size,
            "termbase preview original size",
        )
        _validate_exact_nonnegative_int(
            self.original_mtime_ns,
            "termbase preview original mtime",
        )
        _validate_exact_nonnegative_int(
            self.byte_count,
            "termbase preview byte count",
        )
        if self.original_size != self.byte_count:
            raise ValueError("termbase preview byte count must equal original size")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("termbase preview schema version must be a positive integer")


@dataclass(frozen=True)
class TermbaseImportPreviewColumn:
    zero_based_index: int
    header_candidate: str | None
    header_original_char_count: int = 0
    header_truncated: bool = False

    def __post_init__(self) -> None:
        _validate_exact_nonnegative_int(
            self.zero_based_index,
            "termbase preview column index",
        )
        if self.header_candidate is not None:
            if type(self.header_candidate) is not str:
                raise TypeError("termbase preview header candidate must be a string or None")
            if len(self.header_candidate) > MAX_TERMBASE_IMPORT_PREVIEW_LABEL_CHARS:
                raise ValueError("termbase preview header candidate is too long")
            _validate_exact_nonnegative_int(
                self.header_original_char_count,
                "termbase preview header original length",
            )
            _validate_exact_bool(
                self.header_truncated,
                "termbase preview header truncation",
            )
            if self.header_original_char_count < len(self.header_candidate):
                raise ValueError("termbase preview header length is inconsistent")
            if self.header_truncated != (
                self.header_original_char_count > len(self.header_candidate)
            ):
                raise ValueError("termbase preview header truncation is inconsistent")
        elif self.header_original_char_count != 0 or self.header_truncated:
            raise ValueError("missing termbase preview header cannot carry truncation facts")


@dataclass(frozen=True)
class TermbaseImportPreview:
    format_name: str
    columns: tuple[TermbaseImportPreviewColumn, ...]
    total_column_count: int
    columns_truncated: bool
    legacy_header_detected: bool
    active_sheet_name: str | None
    source_identity: TermbaseImportSourceIdentity

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(self.format_name, "termbase preview format")
        if len(self.format_name) > 32:
            raise ValueError("termbase preview format name is too long")
        if type(self.columns) is not tuple or any(
            type(column) is not TermbaseImportPreviewColumn
            for column in self.columns
        ):
            raise TypeError("termbase preview columns must be an exact immutable tuple")
        if not self.columns:
            raise ValueError("termbase preview must expose at least one column")
        if len(self.columns) > MAX_TERMBASE_IMPORT_PREVIEW_COLUMNS:
            raise ValueError("termbase preview retains too many columns")
        expected_indices = tuple(range(len(self.columns)))
        if tuple(column.zero_based_index for column in self.columns) != expected_indices:
            raise ValueError("termbase preview columns must preserve dense physical order")
        _validate_exact_nonnegative_int(
            self.total_column_count,
            "termbase preview total column count",
        )
        if self.total_column_count < len(self.columns):
            raise ValueError("termbase preview total column count is inconsistent")
        _validate_exact_bool(self.columns_truncated, "termbase preview truncation")
        if self.columns_truncated != (self.total_column_count > len(self.columns)):
            raise ValueError("termbase preview truncation state is inconsistent")
        _validate_exact_bool(
            self.legacy_header_detected,
            "termbase preview legacy header detection",
        )
        if self.active_sheet_name is not None:
            if type(self.active_sheet_name) is not str:
                raise TypeError("termbase preview active sheet name must be a string or None")
            if len(self.active_sheet_name) > MAX_TERMBASE_IMPORT_PREVIEW_LABEL_CHARS:
                raise ValueError("termbase preview active sheet name is too long")
        if type(self.source_identity) is not TermbaseImportSourceIdentity:
            raise TypeError("termbase preview source identity is invalid")


@dataclass(frozen=True)
class TermbaseImportSelection:
    source_zero_based_index: int
    target_zero_based_index: int
    header_mode: TermbaseImportHeaderMode
    preview_column_count: int
    preview_source_identity: TermbaseImportSourceIdentity

    def __post_init__(self) -> None:
        _validate_exact_nonnegative_int(
            self.source_zero_based_index,
            "termbase import source column index",
        )
        _validate_exact_nonnegative_int(
            self.target_zero_based_index,
            "termbase import target column index",
        )
        if self.source_zero_based_index == self.target_zero_based_index:
            raise ValueError("termbase source and target columns must differ")
        if type(self.preview_column_count) is not int or self.preview_column_count <= 0:
            raise ValueError("termbase preview column count must be a positive integer")
        if self.preview_column_count > MAX_TERMBASE_IMPORT_PREVIEW_COLUMNS:
            raise ValueError("termbase preview column count exceeds the visible preview limit")
        if max(self.source_zero_based_index, self.target_zero_based_index) >= self.preview_column_count:
            raise ValueError("termbase column selection exceeds the visible preview")
        if type(self.header_mode) is not TermbaseImportHeaderMode:
            raise TypeError("termbase import header mode is invalid")
        if type(self.preview_source_identity) is not TermbaseImportSourceIdentity:
            raise TypeError("termbase import preview source identity is invalid")


@dataclass(frozen=True)
class TMResourceWriteOutcome:
    """Body-free result of one confirmed-translation TM append attempt."""

    resource_id: str
    resource_name: str
    global_order: int
    written: bool
    error_code: str | None
    retryable: bool

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string(
            self.resource_id,
            "TM write outcome resource id",
        )
        _validate_exact_non_empty_string(
            self.resource_name,
            "TM write outcome resource name",
        )
        _validate_exact_nonnegative_int(
            self.global_order,
            "TM write outcome global order",
        )
        _validate_exact_bool(self.written, "TM write outcome written state")
        _validate_exact_bool(
            self.retryable,
            "TM write outcome retryable state",
        )
        if self.written:
            if self.error_code is not None or self.retryable:
                raise ValueError(
                    "successful TM write outcome cannot retain failure facts"
                )
            return
        _validate_safe_code(self.error_code, "TM write outcome error code")


@dataclass(frozen=True)
class WriteReport:
    """Structured result of writing a confirmed translation."""

    written_resource_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    outcomes: tuple[TMResourceWriteOutcome, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.written_resource_ids, tuple) or not isinstance(self.errors, tuple):
            raise TypeError("write report collections must be tuples")
        if type(self.outcomes) is not tuple or any(
            type(outcome) is not TMResourceWriteOutcome
            for outcome in self.outcomes
        ):
            raise TypeError(
                "write report outcomes must be exact TMResourceWriteOutcome values"
            )
        for outcome in self.outcomes:
            outcome.__post_init__()
        if not self.outcomes:
            return
        resource_ids = tuple(outcome.resource_id for outcome in self.outcomes)
        global_orders = tuple(outcome.global_order for outcome in self.outcomes)
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("write report outcome resources must be unique")
        if any(
            left >= right
            for left, right in zip(global_orders, global_orders[1:])
        ):
            raise ValueError(
                "write report outcomes must preserve declarative resource order"
            )
        expected_written = tuple(
            outcome.resource_id for outcome in self.outcomes if outcome.written
        )
        expected_errors = tuple(
            outcome.error_code
            for outcome in self.outcomes
            if not outcome.written
        )
        if self.written_resource_ids != expected_written:
            raise ValueError(
                "write report written resources must close over outcomes"
            )
        if self.errors != expected_errors:
            raise ValueError("write report errors must close over outcomes")

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
    termbase_selection: TermbaseImportSelection | None = None

    def __post_init__(self) -> None:
        if not self.resource_id.strip():
            raise ValueError("resource id must not be empty")
        if not self.input_path.is_absolute():
            raise ValueError("input path must be absolute")
        if self.termbase_selection is not None:
            if type(self.termbase_selection) is not TermbaseImportSelection:
                raise TypeError("termbase selection must use the typed import contract")
            if self.input_path.suffix.lower() not in {".csv", ".xlsx"}:
                raise ValueError("termbase column selection only applies to CSV/XLSX imports")
