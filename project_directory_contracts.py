"""Immutable, path-authority-free projections for explicit directory selection."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DirectoryEntryKind(str, Enum):
    REGULAR = "regular"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    REPARSE = "reparse"
    OTHER = "other"


class DirectoryEntryUnavailableReason(str, Enum):
    DIRECTORY = "directory"
    LINK = "link"
    REPARSE = "reparse"
    NOT_REGULAR = "not_regular"
    INVALID_REFERENCE = "invalid_reference"
    UNSUPPORTED_FORMAT = "unsupported_format"


class DirectoryRejectionCode(str, Enum):
    STALE = "PROJECT.DIRECTORY.STALE"
    INCOMPLETE = "PROJECT.DIRECTORY.INCOMPLETE"
    EMPTY_SELECTION = "PROJECT.DIRECTORY.EMPTY_SELECTION"
    INVALID_SELECTION = "PROJECT.DIRECTORY.INVALID_SELECTION"
    ALIAS = "PROJECT.DIRECTORY.ALIAS"
    CANCELLED = "PROJECT.DIRECTORY.CANCELLED"
    CLOSED = "PROJECT.DIRECTORY.CLOSED"
    LIMIT_EXCEEDED = "PROJECT.DIRECTORY.LIMIT_EXCEEDED"
    OBSERVATION_FAILED = "PROJECT.DIRECTORY.OBSERVATION_FAILED"


def _identifier(value: str) -> None:
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("issued identifiers must be nonempty bounded strings")


def _generation(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("generation must be a positive exact integer")


def _entry_ids(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or len(values) > 10000:
        raise ValueError("entry IDs must be a bounded exact tuple")
    for value in values:
        _identifier(value)


@dataclass(frozen=True, slots=True)
class DirectoryDiscoveryRejection:
    code: DirectoryRejectionCode
    retryable: bool = True

    def __post_init__(self) -> None:
        if type(self.code) is not DirectoryRejectionCode or type(self.retryable) is not bool:
            raise TypeError("rejection requires an exact code and boolean")


@dataclass(frozen=True, slots=True)
class DirectorySelectionEntry:
    entry_id: str
    parent_entry_id: str | None
    display_name: str
    source_ref: str | None
    kind: DirectoryEntryKind
    byte_count: int | None
    selectable: bool
    unavailable_reason: DirectoryEntryUnavailableReason | None

    def __post_init__(self) -> None:
        _identifier(self.entry_id)
        if self.parent_entry_id is not None:
            _identifier(self.parent_entry_id)
        if type(self.display_name) is not str or not self.display_name:
            raise ValueError("display name must be nonempty text")
        if self.source_ref is not None and type(self.source_ref) is not str:
            raise TypeError("source ref must be text or None")
        if type(self.kind) is not DirectoryEntryKind or type(self.selectable) is not bool:
            raise TypeError("entry kind and selectable must have exact types")
        if self.byte_count is not None and (type(self.byte_count) is not int or self.byte_count < 0):
            raise ValueError("byte count must be a nonnegative integer or None")
        if self.unavailable_reason is not None and type(self.unavailable_reason) is not DirectoryEntryUnavailableReason:
            raise TypeError("unavailable reason must have its exact type")
        if self.selectable != (self.unavailable_reason is None):
            raise ValueError("entry availability must match its reason")
        if self.selectable and (self.kind is not DirectoryEntryKind.REGULAR or self.source_ref is None):
            raise ValueError("only regular files with a portable ref are selectable")


@dataclass(frozen=True, slots=True)
class DirectorySelectionPreview:
    preview_id: str
    generation: int
    root_binding_ref: str
    entries: tuple[DirectorySelectionEntry, ...]
    complete: bool
    rejection: DirectoryDiscoveryRejection | None = None
    selected_entry_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.preview_id)
        _generation(self.generation)
        _identifier(self.root_binding_ref)
        if (type(self.entries) is not tuple or len(self.entries) > 10000
                or any(type(entry) is not DirectorySelectionEntry for entry in self.entries)):
            raise TypeError("preview entries must be a bounded immutable tuple")
        if type(self.complete) is not bool or self.complete != (self.rejection is None):
            raise ValueError("an incomplete preview must carry a rejection")
        if self.rejection is not None and type(self.rejection) is not DirectoryDiscoveryRejection:
            raise TypeError("preview rejection must have its exact type")
        if self.selected_entry_ids != () or type(self.selected_entry_ids) is not tuple:
            raise ValueError("a fresh preview never selects files implicitly")


@dataclass(frozen=True, slots=True)
class DirectorySelectionRequest:
    preview_id: str
    generation: int
    entry_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.preview_id)
        _generation(self.generation)
        _entry_ids(self.entry_ids)


@dataclass(frozen=True, slots=True)
class IssuedDirectorySelection:
    """An ordered metadata choice, never a verified source or writer capability."""

    selection_id: str
    preview_id: str
    generation: int
    root_binding_ref: str
    entry_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (self.selection_id, self.preview_id, self.root_binding_ref):
            _identifier(value)
        _generation(self.generation)
        _entry_ids(self.entry_ids)
