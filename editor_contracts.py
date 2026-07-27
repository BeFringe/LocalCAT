"""Immutable cross-layer contracts for the LocalCAT desktop editor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ResourceKind(str, Enum):
    """Supported local language-resource categories."""

    TRANSLATION_MEMORY = "translation_memory"
    TERMBASE = "termbase"


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
