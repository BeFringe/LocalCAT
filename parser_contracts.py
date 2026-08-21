"""Standard-library-only immutable contracts for the Parser Foundation.

This module intentionally contains no codec, registry, source, application, Engine,
Store, Controller, or UI behavior.  It is the neutral vocabulary shared by those
boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Callable, Final, Iterator, Protocol, runtime_checkable


MAX_SNIFF_PREFIX_BYTES: Final = 4 * 1024
MAX_HINT_VALUES_PER_KIND: Final = 16
MAX_EXTENSION_HINT_CHARS: Final = 32
MAX_MIME_HINT_CHARS: Final = 127
MAX_FORMAT_ID_CHARS: Final = 128
MAX_RETAINED_SUPPORTED_COMBINATIONS: Final = 64
MAX_SAFE_SUMMARY_CHARS: Final = 512


_ISSUE_CODE_PATTERN = re.compile(r"^PARSER(?:\.[A-Z][A-Z0-9_]*){2,}$")
_EXTENSION_HINT_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._+-]*$")
_MIME_HINT_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+$"
)
_FORMAT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _require_issue_code(value: object, *, field_name: str) -> str:
    _require_nonempty_text(value, field_name=field_name)
    if not _ISSUE_CODE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is not a stable PARSER issue code")
    return value


def _require_nonempty_text(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _require_safe_summary(value: object) -> str:
    summary = _require_nonempty_text(value, field_name="safe_summary")
    if len(summary) > MAX_SAFE_SUMMARY_CHARS:
        raise ValueError("safe_summary is too long")
    if any(not character.isprintable() for character in summary):
        raise ValueError("safe_summary must be a single printable line")
    return summary


def _require_tuple(value: object, *, field_name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    return value


def _require_exact_instance(value: object, expected: type, field_name: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{field_name} must be exact {expected.__name__}")


class ContractViolation(ValueError):
    """A deterministic contract-construction failure with a stable safe code."""

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = _require_issue_code(code, field_name="code")
        self.safe_summary = _require_safe_summary(safe_summary)
        super().__init__(f"{self.code}: {self.safe_summary}")


class EffectivePurpose(Enum):
    PROJECT_DOCUMENT = "project_document"
    TRANSLATION_MEMORY = "language_resource.translation_memory"
    TERMBASE = "language_resource.termbase"


@dataclass(frozen=True, slots=True)
class FormatId:
    value: str

    def __post_init__(self) -> None:
        _require_nonempty_text(self.value, field_name="FormatId.value")
        if len(self.value) > MAX_FORMAT_ID_CHARS:
            raise ValueError("FormatId.value is too long")
        if not _FORMAT_ID_PATTERN.fullmatch(self.value):
            raise ValueError("FormatId.value must be a stable lowercase identifier")


LOCALCAT_JSON_V1: Final = FormatId("localcat-json-v1")
LINE_TEXT_V1: Final = FormatId("line-text-v1")
GETTEXT_PO_V1: Final = FormatId("gettext-po-v1")
GETTEXT_POT_V1: Final = FormatId("gettext-pot-v1")
TMX_LEVEL1_V1: Final = FormatId("tmx-level1-v1")
NORMALIZED_TM_JSON_V1: Final = FormatId("normalized-tm-json-v1")
TERMBASE_CSV_V1: Final = FormatId("termbase-csv-v1")
TERMBASE_XLSX_V1: Final = FormatId("termbase-xlsx-v1")

BUILTIN_FORMAT_IDS: Final = (
    LOCALCAT_JSON_V1,
    LINE_TEXT_V1,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    TMX_LEVEL1_V1,
    NORMALIZED_TM_JSON_V1,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
)

_BUILTIN_PURPOSE_BY_FORMAT: Final = MappingProxyType({
    LOCALCAT_JSON_V1: EffectivePurpose.PROJECT_DOCUMENT,
    LINE_TEXT_V1: EffectivePurpose.PROJECT_DOCUMENT,
    GETTEXT_PO_V1: EffectivePurpose.PROJECT_DOCUMENT,
    GETTEXT_POT_V1: EffectivePurpose.PROJECT_DOCUMENT,
    TMX_LEVEL1_V1: EffectivePurpose.TRANSLATION_MEMORY,
    NORMALIZED_TM_JSON_V1: EffectivePurpose.TRANSLATION_MEMORY,
    TERMBASE_CSV_V1: EffectivePurpose.TERMBASE,
    TERMBASE_XLSX_V1: EffectivePurpose.TERMBASE,
})


def builtin_purpose_for_format(format_id: FormatId) -> EffectivePurpose | None:
    """Return the closed built-in purpose mapping without exposing mutable state."""

    _require_exact_instance(format_id, FormatId, "format_id")
    return _BUILTIN_PURPOSE_BY_FORMAT.get(format_id)


@dataclass(frozen=True, slots=True)
class CodecIdentity:
    provider_id: str
    codec_id: str
    codec_version: str

    def __post_init__(self) -> None:
        for field_name in ("provider_id", "codec_id", "codec_version"):
            value = getattr(self, field_name)
            _require_nonempty_text(value, field_name=f"CodecIdentity.{field_name}")
            if value != value.strip():
                raise ValueError(
                    f"CodecIdentity.{field_name} must not have surrounding whitespace"
                )


@dataclass(frozen=True, slots=True)
class SelectionHints:
    extensions: tuple[str, ...] = ()
    mime_types: tuple[str, ...] = ()
    prefix: bytes = b""

    def __post_init__(self) -> None:
        _require_tuple(self.extensions, field_name="SelectionHints.extensions")
        _require_tuple(self.mime_types, field_name="SelectionHints.mime_types")
        if type(self.prefix) is not bytes:
            raise TypeError("SelectionHints.prefix must be exact bytes")
        if not self.extensions and not self.mime_types and not self.prefix:
            raise ValueError("SelectionHints must contain at least one hint")
        if len(self.extensions) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("too many extension hints")
        if len(self.mime_types) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("too many MIME hints")
        if len(self.prefix) > MAX_SNIFF_PREFIX_BYTES:
            raise ValueError("sniff prefix exceeds 4 KiB")
        normalized_extensions = tuple(
            _normalize_extension_hint(extension) for extension in self.extensions
        )
        normalized_mime_types = tuple(
            _normalize_mime_hint(mime_type) for mime_type in self.mime_types
        )
        object.__setattr__(self, "extensions", normalized_extensions)
        object.__setattr__(self, "mime_types", normalized_mime_types)
        if len(set(normalized_extensions)) != len(normalized_extensions):
            raise ValueError("extension hints must be unique")
        if len(set(normalized_mime_types)) != len(normalized_mime_types):
            raise ValueError("MIME hints must be unique")


@dataclass(frozen=True, slots=True)
class SelectionHintSummary:
    """Body-safe observations for failures; sniff bytes never enter this DTO."""

    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    sniff_prefix_present: bool
    sniff_prefix_byte_count: int

    def __post_init__(self) -> None:
        _require_tuple(self.extensions, field_name="SelectionHintSummary.extensions")
        _require_tuple(self.mime_types, field_name="SelectionHintSummary.mime_types")
        if len(self.extensions) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("too many summarized extension hints")
        if len(self.mime_types) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("too many summarized MIME hints")
        normalized_extensions = tuple(
            _normalize_extension_hint(extension) for extension in self.extensions
        )
        normalized_mime_types = tuple(
            _normalize_mime_hint(mime_type) for mime_type in self.mime_types
        )
        if len(set(normalized_extensions)) != len(normalized_extensions):
            raise ValueError("summarized extension hints must be unique")
        if len(set(normalized_mime_types)) != len(normalized_mime_types):
            raise ValueError("summarized MIME hints must be unique")
        object.__setattr__(self, "extensions", normalized_extensions)
        object.__setattr__(self, "mime_types", normalized_mime_types)
        if type(self.sniff_prefix_present) is not bool:
            raise TypeError("sniff_prefix_present must be exact bool")
        _require_nonnegative_int(
            self.sniff_prefix_byte_count,
            field_name="sniff_prefix_byte_count",
        )
        if self.sniff_prefix_byte_count > MAX_SNIFF_PREFIX_BYTES:
            raise ValueError("summarized sniff prefix exceeds 4 KiB")
        if self.sniff_prefix_present != (self.sniff_prefix_byte_count > 0):
            raise ValueError("sniff prefix presence and byte count are inconsistent")

    @classmethod
    def from_hints(cls, hints: SelectionHints) -> SelectionHintSummary:
        _require_exact_instance(hints, SelectionHints, "hints")
        return cls(
            extensions=hints.extensions,
            mime_types=hints.mime_types,
            sniff_prefix_present=bool(hints.prefix),
            sniff_prefix_byte_count=len(hints.prefix),
        )


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    purpose: EffectivePurpose
    format_id: FormatId | None = None
    hints: SelectionHints | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self.purpose, EffectivePurpose, "SelectionRequest.purpose")
        if (self.format_id is None) == (self.hints is None):
            raise ValueError("SelectionRequest requires exactly one of format_id or hints")
        if self.format_id is not None:
            _require_exact_instance(self.format_id, FormatId, "SelectionRequest.format_id")
        if self.hints is not None:
            _require_exact_instance(self.hints, SelectionHints, "SelectionRequest.hints")


@dataclass(frozen=True, slots=True)
class SupportedCombination:
    purpose: EffectivePurpose
    format_id: FormatId

    def __post_init__(self) -> None:
        _require_exact_instance(self.purpose, EffectivePurpose, "SupportedCombination.purpose")
        _require_exact_instance(self.format_id, FormatId, "SupportedCombination.format_id")


@dataclass(frozen=True, slots=True)
class SelectionFailure:
    code: str
    requested_purpose: EffectivePurpose
    requested_format_id: FormatId | None
    observed_hints: SelectionHintSummary | None
    supported_combinations: tuple[SupportedCombination, ...]
    supported_combination_count: int
    supported_combinations_truncated: bool

    def __post_init__(self) -> None:
        _require_issue_code(self.code, field_name="SelectionFailure.code")
        if not self.code.startswith("PARSER.SELECTION."):
            raise ValueError("selection failure code must use PARSER.SELECTION namespace")
        _require_exact_instance(
            self.requested_purpose,
            EffectivePurpose,
            "SelectionFailure.requested_purpose",
        )
        if self.requested_format_id is not None:
            _require_exact_instance(
                self.requested_format_id,
                FormatId,
                "SelectionFailure.requested_format_id",
            )
        if self.observed_hints is not None:
            _require_exact_instance(
                self.observed_hints,
                SelectionHintSummary,
                "SelectionFailure.observed_hints",
            )
        _require_tuple(
            self.supported_combinations,
            field_name="SelectionFailure.supported_combinations",
        )
        if len(self.supported_combinations) > MAX_RETAINED_SUPPORTED_COMBINATIONS:
            raise ValueError("selection failure retains too many supported combinations")
        for combination in self.supported_combinations:
            _require_exact_instance(
                combination,
                SupportedCombination,
                "SelectionFailure.supported_combinations item",
            )
        if len(set(self.supported_combinations)) != len(self.supported_combinations):
            raise ValueError("supported combinations must be unique")
        combination_keys = tuple(
            (combination.purpose.value, combination.format_id.value)
            for combination in self.supported_combinations
        )
        if tuple(sorted(combination_keys)) != combination_keys:
            raise ValueError("supported combinations must use deterministic sorted order")
        _require_nonnegative_int(
            self.supported_combination_count,
            field_name="SelectionFailure.supported_combination_count",
        )
        if self.supported_combination_count < len(self.supported_combinations):
            raise ValueError("supported combination count is smaller than retained entries")
        if type(self.supported_combinations_truncated) is not bool:
            raise TypeError("supported_combinations_truncated must be exact bool")
        expected_truncation = self.supported_combination_count > len(
            self.supported_combinations
        )
        if self.supported_combinations_truncated != expected_truncation:
            raise ValueError("supported combination truncation state is inconsistent")


class ColumnSelectorKind(Enum):
    HEADER_NAME = "header_name"
    ZERO_BASED_INDEX = "zero_based_index"


class TermbaseHeaderPolicy(Enum):
    FIRST_ROW = "first_row"
    NO_HEADER = "no_header"
    LEGACY_ALLOWLIST = "legacy_allowlist"


@dataclass(frozen=True, slots=True)
class TermbaseColumnSelector:
    kind: ColumnSelectorKind
    header_name: str | None = None
    zero_based_index: int | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self.kind, ColumnSelectorKind, "TermbaseColumnSelector.kind")
        if self.kind is ColumnSelectorKind.HEADER_NAME:
            if self.zero_based_index is not None:
                raise ValueError("header-name selector cannot contain an index")
            _require_nonempty_text(self.header_name, field_name="header_name")
            normalized = self.header_name.strip()
            if not normalized:
                raise ValueError("header_name must remain non-empty after trimming")
            object.__setattr__(self, "header_name", normalized)
            return
        if self.header_name is not None:
            raise ValueError("index selector cannot contain a header name")
        if type(self.zero_based_index) is not int:
            raise TypeError("zero_based_index must be an exact integer")
        if self.zero_based_index < 0:
            raise ValueError("zero_based_index must be non-negative")


@dataclass(frozen=True, slots=True)
class TermbaseColumnSelection:
    source: TermbaseColumnSelector
    target: TermbaseColumnSelector
    header_policy: TermbaseHeaderPolicy

    def __post_init__(self) -> None:
        _require_exact_instance(self.source, TermbaseColumnSelector, "column source")
        _require_exact_instance(self.target, TermbaseColumnSelector, "column target")
        _require_exact_instance(self.header_policy, TermbaseHeaderPolicy, "header_policy")
        has_header_selector = (
            self.source.kind is ColumnSelectorKind.HEADER_NAME
            or self.target.kind is ColumnSelectorKind.HEADER_NAME
        )
        if has_header_selector and self.header_policy is not TermbaseHeaderPolicy.FIRST_ROW:
            raise ValueError("header-name selectors require FIRST_ROW policy")
        if (
            self.source.kind is ColumnSelectorKind.HEADER_NAME
            and self.target.kind is ColumnSelectorKind.HEADER_NAME
        ):
            if self.source.header_name == self.target.header_name:
                raise ValueError("source and target header names must differ")
            return
        if (
            self.source.kind is ColumnSelectorKind.ZERO_BASED_INDEX
            and self.target.kind is ColumnSelectorKind.ZERO_BASED_INDEX
            and self.source.zero_based_index == self.target.zero_based_index
        ):
            raise ValueError("source and target physical columns must differ")
        if self.header_policy is TermbaseHeaderPolicy.LEGACY_ALLOWLIST and (
            self.source.kind is not ColumnSelectorKind.ZERO_BASED_INDEX
            or self.target.kind is not ColumnSelectorKind.ZERO_BASED_INDEX
            or self.source.zero_based_index != 0
            or self.target.zero_based_index != 1
        ):
            raise ValueError("LEGACY_ALLOWLIST is restricted to source 0 and target 1")

    @classmethod
    def legacy_first_two_columns(cls) -> TermbaseColumnSelection:
        return cls(
            source=TermbaseColumnSelector(
                kind=ColumnSelectorKind.ZERO_BASED_INDEX,
                zero_based_index=0,
            ),
            target=TermbaseColumnSelector(
                kind=ColumnSelectorKind.ZERO_BASED_INDEX,
                zero_based_index=1,
            ),
            header_policy=TermbaseHeaderPolicy.LEGACY_ALLOWLIST,
        )


@dataclass(frozen=True, slots=True)
class TermbaseReadOptions:
    columns: TermbaseColumnSelection

    def __post_init__(self) -> None:
        _require_exact_instance(self.columns, TermbaseColumnSelection, "TermbaseReadOptions.columns")


@dataclass(frozen=True, slots=True)
class ReadRequest:
    purpose: EffectivePurpose
    format_id: FormatId
    termbase_options: TermbaseReadOptions | None = None

    def __post_init__(self) -> None:
        _require_exact_instance(self.purpose, EffectivePurpose, "ReadRequest.purpose")
        _require_exact_instance(self.format_id, FormatId, "ReadRequest.format_id")
        expected_purpose = _BUILTIN_PURPOSE_BY_FORMAT.get(self.format_id)
        if expected_purpose is not None and expected_purpose is not self.purpose:
            raise ContractViolation(
                "PARSER.SELECTION.UNSUPPORTED",
                "requested purpose and built-in format are not a supported combination",
            )
        if self.purpose is EffectivePurpose.TERMBASE:
            if self.termbase_options is None:
                raise ContractViolation(
                    "PARSER.TERMBASE.COLUMN_SELECTION_REQUIRED",
                    "termbase reads require an explicit column selection",
                )
            _require_exact_instance(
                self.termbase_options,
                TermbaseReadOptions,
                "ReadRequest.termbase_options",
            )
        elif self.termbase_options is not None:
            raise ContractViolation(
                "PARSER.TERMBASE.COLUMN_SELECTION_NOT_APPLICABLE",
                "termbase column selection is not valid for the requested purpose",
            )


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Caller-selected source and the safe root that must contain it."""

    safe_root: str
    selected_path: str
    display_hint: str

    def __post_init__(self) -> None:
        for field_name in ("safe_root", "selected_path", "display_hint"):
            value = getattr(self, field_name)
            _require_nonempty_text(value, field_name=f"SourceReference.{field_name}")
            if "\x00" in value:
                raise ValueError(f"SourceReference.{field_name} cannot contain NUL")
        _require_safe_summary(self.display_hint)


@dataclass(frozen=True, slots=True)
class TargetReference:
    """Caller-selected byte target and its retained safe-root authority."""

    safe_root: str
    selected_path: str
    display_hint: str

    def __post_init__(self) -> None:
        for field_name in ("safe_root", "selected_path", "display_hint"):
            value = getattr(self, field_name)
            _require_nonempty_text(value, field_name=f"TargetReference.{field_name}")
            if "\x00" in value:
                raise ValueError(f"TargetReference.{field_name} cannot contain NUL")
        _require_safe_summary(self.display_hint)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourceSnapshotIdentity:
    relative_reference_sha256: str
    regular_file_identity: str
    original_size: int
    original_mtime_ns: int
    content_sha256: str
    byte_count: int
    schema_version: int

    def __post_init__(self) -> None:
        for field_name in ("relative_reference_sha256", "content_sha256"):
            value = getattr(self, field_name)
            if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"SourceSnapshotIdentity.{field_name} must be lowercase SHA-256")
        _require_nonempty_text(
            self.regular_file_identity,
            field_name="SourceSnapshotIdentity.regular_file_identity",
        )
        _require_nonnegative_int(self.original_size, field_name="original_size")
        _require_nonnegative_int(self.original_mtime_ns, field_name="original_mtime_ns")
        _require_nonnegative_int(self.byte_count, field_name="byte_count")
        _require_positive_int(self.schema_version, field_name="schema_version")
        if self.original_size != self.byte_count:
            raise ValueError("sealed snapshot byte count must equal the stable original size")


@dataclass(frozen=True, slots=True)
class RawSpeaker:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("RawSpeaker.value must be an exact string")


JsonMetadataScalar = str | int | float | bool | None
JsonMetadataValue = JsonMetadataScalar | tuple["JsonMetadataValue", ...]


@dataclass(frozen=True, slots=True)
class MetadataEntry:
    key: str
    value: JsonMetadataValue

    def __post_init__(self) -> None:
        _require_nonempty_text(self.key, field_name="MetadataEntry.key")
        if self.key != self.key.strip():
            raise ValueError("MetadataEntry.key must not have surrounding whitespace")
        _validate_metadata_value(self.value)


@dataclass(frozen=True, slots=True)
class DocumentHeader:
    """The single neutral header allowed for a project-document raw stream."""

    name: str
    source_locale: str | None
    target_locale: str | None
    metadata: tuple[MetadataEntry, ...]

    def __post_init__(self) -> None:
        _require_nonempty_text(self.name, field_name="DocumentHeader.name")
        _require_optional_text(
            self.source_locale,
            field_name="DocumentHeader.source_locale",
        )
        _require_optional_text(
            self.target_locale,
            field_name="DocumentHeader.target_locale",
        )
        _validate_metadata_entries(self.metadata, field_name="DocumentHeader.metadata")


class TargetPresence(Enum):
    MISSING = "missing"
    EXPLICIT_EMPTY = "explicit_empty"
    PRESENT = "present"


class TranslationState(Enum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    FORMAT_DERIVED_UNCONFIRMED = "format_derived_unconfirmed"


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    local_id: str
    source: str
    target: str | None
    target_presence: TargetPresence
    translation_state: TranslationState | None
    speaker: RawSpeaker
    format_metadata: tuple[MetadataEntry, ...]

    def __post_init__(self) -> None:
        _require_nonempty_text(self.local_id, field_name="ParsedSegment.local_id")
        _require_nonempty_text(self.source, field_name="ParsedSegment.source")
        _require_exact_instance(
            self.target_presence,
            TargetPresence,
            "ParsedSegment.target_presence",
        )
        if self.translation_state is not None:
            _require_exact_instance(
                self.translation_state,
                TranslationState,
                "ParsedSegment.translation_state",
            )
        _require_exact_instance(self.speaker, RawSpeaker, "ParsedSegment.speaker")
        _validate_metadata_entries(self.format_metadata, field_name="ParsedSegment.format_metadata")
        if self.target_presence is TargetPresence.MISSING:
            if self.target is not None:
                raise ValueError("missing target presence requires target=None")
        elif self.target_presence is TargetPresence.EXPLICIT_EMPTY:
            if type(self.target) is not str or self.target != "":
                raise ValueError("explicit-empty target presence requires target='' ")
        elif type(self.target) is not str or not self.target:
            raise ValueError("present target presence requires a non-empty string target")


@dataclass(frozen=True, slots=True)
class CodecCapabilities:
    readable: bool
    validatable: bool
    canonical_write: bool
    source_round_trip_write: bool
    streaming_input: bool
    iterator_view: bool
    materialized_view: bool
    format_profile: str
    active_sheet_only: bool = False
    opaque_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "readable",
            "validatable",
            "canonical_write",
            "source_round_trip_write",
            "streaming_input",
            "iterator_view",
            "materialized_view",
            "active_sheet_only",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"CodecCapabilities.{field_name} must be exact bool")
        _require_nonempty_text(self.format_profile, field_name="CodecCapabilities.format_profile")
        if self.format_profile != self.format_profile.strip():
            raise ValueError("format_profile must not have surrounding whitespace")
        _require_tuple(self.opaque_features, field_name="CodecCapabilities.opaque_features")
        for feature in self.opaque_features:
            _require_nonempty_text(feature, field_name="opaque feature")
        if len(set(self.opaque_features)) != len(self.opaque_features):
            raise ValueError("opaque features must be unique")


@dataclass(frozen=True, slots=True)
class RoundTripTokenEnvelope:
    codec_identity: CodecIdentity
    source_fingerprint: str
    format_state_fingerprint: str
    opaque_payload: bytes

    def __post_init__(self) -> None:
        _require_exact_instance(
            self.codec_identity,
            CodecIdentity,
            "RoundTripTokenEnvelope.codec_identity",
        )
        _require_sha256(self.source_fingerprint, field_name="source_fingerprint")
        _require_sha256(
            self.format_state_fingerprint,
            field_name="format_state_fingerprint",
        )
        if type(self.opaque_payload) is not bytes:
            raise TypeError("RoundTripTokenEnvelope.opaque_payload must be exact bytes")


class RoundTripTokenFailureReason(Enum):
    MISSING = "missing"
    FOREIGN_CODEC = "foreign_codec"
    VERSION_INCOMPATIBLE = "version_incompatible"
    STALE_SOURCE = "stale_source"
    FORMAT_STATE_MISMATCH = "format_state_mismatch"


class RoundTripTokenValidationError(ContractViolation):
    def __init__(self, reason: RoundTripTokenFailureReason, safe_summary: str) -> None:
        _require_exact_instance(
            reason,
            RoundTripTokenFailureReason,
            "RoundTripTokenValidationError.reason",
        )
        self.reason = reason
        super().__init__("PARSER.CAPABILITY.INVALID_TOKEN", safe_summary)


def validate_round_trip_token(
    token: RoundTripTokenEnvelope | None,
    *,
    expected_codec_identity: CodecIdentity,
    expected_source_fingerprint: str,
    expected_format_state_fingerprint: str,
) -> RoundTripTokenEnvelope:
    """Validate an opaque token before any writer is allowed to open a target."""

    _require_exact_instance(
        expected_codec_identity,
        CodecIdentity,
        "expected_codec_identity",
    )
    _require_sha256(expected_source_fingerprint, field_name="expected_source_fingerprint")
    _require_sha256(
        expected_format_state_fingerprint,
        field_name="expected_format_state_fingerprint",
    )
    if token is None:
        raise RoundTripTokenValidationError(
            RoundTripTokenFailureReason.MISSING,
            "source round-trip write requires an opaque token",
        )
    _require_exact_instance(token, RoundTripTokenEnvelope, "token")
    actual = token.codec_identity
    if (
        actual.provider_id != expected_codec_identity.provider_id
        or actual.codec_id != expected_codec_identity.codec_id
    ):
        raise RoundTripTokenValidationError(
            RoundTripTokenFailureReason.FOREIGN_CODEC,
            "round-trip token belongs to another provider or codec",
        )
    if actual.codec_version != expected_codec_identity.codec_version:
        raise RoundTripTokenValidationError(
            RoundTripTokenFailureReason.VERSION_INCOMPATIBLE,
            "round-trip token codec version is incompatible",
        )
    if token.source_fingerprint != expected_source_fingerprint:
        raise RoundTripTokenValidationError(
            RoundTripTokenFailureReason.STALE_SOURCE,
            "round-trip token does not bind the current source snapshot",
        )
    if token.format_state_fingerprint != expected_format_state_fingerprint:
        raise RoundTripTokenValidationError(
            RoundTripTokenFailureReason.FORMAT_STATE_MISMATCH,
            "round-trip token does not bind the current format state",
        )
    return token


MAX_DECLARED_ISSUE_CODES: Final = 64
MAX_RETAINED_ISSUES: Final = 256

# Every descriptor consumed by GuardedParseSession must reserve these stable
# Foundation-generated codes.  Codec-specific warnings/fatals remain additive.
FOUNDATION_GUARDED_ISSUE_CODES: Final = tuple(
    sorted(
        (
            "PARSER.LIMIT.FIELD",
            "PARSER.LIMIT.INPUT",
            "PARSER.LIMIT.MATERIALIZATION",
            "PARSER.LIMIT.METADATA",
            "PARSER.LIMIT.RECORD",
            "PARSER.PLUGIN.ISSUE_UNDECLARED",
            "PARSER.SOURCE.CANCELLED",
            "PARSER.SOURCE.READ_FAILED",
            "PARSER.SOURCE.STALE",
            "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
            "PARSER.SYNTAX.INVALID_EVENT",
            "PARSER.SYNTAX.INVALID_HEADER",
            "PARSER.SYNTAX.MALFORMED",
        )
    )
)


@dataclass(frozen=True, slots=True)
class LimitProfile:
    profile_id: str
    profile_version: int
    max_input_bytes: int
    max_decoded_field_chars: int
    max_records: int
    max_materialized_records: int
    max_retained_issues: int
    declared_issue_codes: tuple[str, ...]
    max_metadata_entries_per_container: int
    max_metadata_decoded_chars_per_container: int
    max_metadata_decoded_chars_total: int
    max_structure_depth: int
    max_expanded_bytes: int | None = None
    max_archive_members: int | None = None
    max_compression_ratio: float | None = None

    def __post_init__(self) -> None:
        _require_nonempty_text(self.profile_id, field_name="LimitProfile.profile_id")
        if self.profile_id != self.profile_id.strip():
            raise ValueError("LimitProfile.profile_id must not have surrounding whitespace")
        _require_positive_int(self.profile_version, field_name="profile_version")
        for field_name in (
            "max_input_bytes",
            "max_decoded_field_chars",
            "max_records",
            "max_materialized_records",
            "max_retained_issues",
            "max_metadata_entries_per_container",
            "max_metadata_decoded_chars_per_container",
            "max_metadata_decoded_chars_total",
            "max_structure_depth",
        ):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        if self.max_materialized_records > self.max_records:
            raise ValueError("materialized record limit cannot exceed record limit")
        if self.max_retained_issues > MAX_RETAINED_ISSUES:
            raise ValueError("retained issue limit exceeds the Foundation maximum")
        if (
            self.max_metadata_decoded_chars_total
            < self.max_metadata_decoded_chars_per_container
        ):
            raise ValueError("total metadata character limit cannot be smaller than one container")
        _require_tuple(
            self.declared_issue_codes,
            field_name="LimitProfile.declared_issue_codes",
        )
        if not self.declared_issue_codes:
            raise ValueError("LimitProfile must declare a finite non-empty issue allowlist")
        if len(self.declared_issue_codes) > MAX_DECLARED_ISSUE_CODES:
            raise ValueError("declared issue-code allowlist exceeds 64 codes")
        for code in self.declared_issue_codes:
            _require_issue_code(code, field_name="declared issue code")
        if tuple(sorted(self.declared_issue_codes)) != self.declared_issue_codes:
            raise ValueError("declared issue codes must use deterministic sorted order")
        if len(set(self.declared_issue_codes)) != len(self.declared_issue_codes):
            raise ValueError("declared issue codes must be unique")
        for field_name in ("max_expanded_bytes", "max_archive_members"):
            value = getattr(self, field_name)
            if value is not None:
                _require_positive_int(value, field_name=field_name)
        if self.max_compression_ratio is not None:
            if type(self.max_compression_ratio) not in {int, float}:
                raise TypeError("max_compression_ratio must be a finite number")
            ratio = float(self.max_compression_ratio)
            if not math.isfinite(ratio) or ratio < 1.0:
                raise ValueError("max_compression_ratio must be finite and at least 1")
            object.__setattr__(self, "max_compression_ratio", ratio)


class IssueSeverity(Enum):
    WARNING = "warning"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: str
    severity: IssueSeverity
    safe_summary: str
    byte_offset: int | None = None
    line_number: int | None = None
    record_number: int | None = None

    def __post_init__(self) -> None:
        _require_issue_code(self.code, field_name="ParseIssue.code")
        _require_exact_instance(self.severity, IssueSeverity, "ParseIssue.severity")
        _require_safe_summary(self.safe_summary)
        if self.byte_offset is not None:
            _require_nonnegative_int(self.byte_offset, field_name="byte_offset")
        for field_name in ("line_number", "record_number"):
            value = getattr(self, field_name)
            if value is not None:
                _require_positive_int(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class IssueCount:
    code: str
    severity: IssueSeverity
    count: int

    def __post_init__(self) -> None:
        _require_issue_code(self.code, field_name="IssueCount.code")
        _require_exact_instance(self.severity, IssueSeverity, "IssueCount.severity")
        _require_positive_int(self.count, field_name="IssueCount.count")


@dataclass(frozen=True, slots=True, init=False)
class TerminalSuccess:
    source: SourceSnapshotIdentity
    codec_identity: CodecIdentity
    limit_profile: LimitProfile
    record_count: int
    warning_counts: tuple[IssueCount, ...]
    issues_truncated: bool
    fatal_count: int

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("TerminalSuccess can only be issued by the Parser Foundation")


def _issue_terminal_success(
    *,
    source: SourceSnapshotIdentity,
    codec_identity: CodecIdentity,
    limit_profile: LimitProfile,
    record_count: int,
    warning_counts: tuple[IssueCount, ...],
    issues_truncated: bool,
) -> TerminalSuccess:
    """Foundation-private terminal factory used by the guarded session."""

    _require_exact_instance(source, SourceSnapshotIdentity, "TerminalSuccess.source")
    _require_exact_instance(
        codec_identity,
        CodecIdentity,
        "TerminalSuccess.codec_identity",
    )
    _require_exact_instance(limit_profile, LimitProfile, "TerminalSuccess.limit_profile")
    _require_nonnegative_int(record_count, field_name="TerminalSuccess.record_count")
    if record_count > limit_profile.max_records:
        raise ValueError("terminal record count exceeds the bound limit profile")
    _validate_issue_counts(
        warning_counts,
        limit_profile=limit_profile,
        warning_only=True,
        field_name="TerminalSuccess.warning_counts",
    )
    if type(issues_truncated) is not bool:
        raise TypeError("TerminalSuccess.issues_truncated must be exact bool")
    terminal = object.__new__(TerminalSuccess)
    object.__setattr__(terminal, "source", source)
    object.__setattr__(terminal, "codec_identity", codec_identity)
    object.__setattr__(terminal, "limit_profile", limit_profile)
    object.__setattr__(terminal, "record_count", record_count)
    object.__setattr__(terminal, "warning_counts", warning_counts)
    object.__setattr__(terminal, "issues_truncated", issues_truncated)
    object.__setattr__(terminal, "fatal_count", 0)
    return terminal


class ValidationOutcome(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    outcome: ValidationOutcome
    source: SourceSnapshotIdentity | None
    format_id: FormatId
    codec_identity: CodecIdentity
    observed_capabilities: CodecCapabilities
    limit_profile: LimitProfile
    provisional_record_count: int
    issue_counts: tuple[IssueCount, ...]
    issues: tuple[ParseIssue, ...]
    issues_truncated: bool
    terminal: TerminalSuccess | None

    def __post_init__(self) -> None:
        _require_exact_instance(self.outcome, ValidationOutcome, "ValidationReport.outcome")
        if self.source is not None:
            _require_exact_instance(
                self.source,
                SourceSnapshotIdentity,
                "ValidationReport.source",
            )
        _require_exact_instance(self.format_id, FormatId, "ValidationReport.format_id")
        _require_exact_instance(
            self.codec_identity,
            CodecIdentity,
            "ValidationReport.codec_identity",
        )
        _require_exact_instance(
            self.observed_capabilities,
            CodecCapabilities,
            "ValidationReport.observed_capabilities",
        )
        _require_exact_instance(
            self.limit_profile,
            LimitProfile,
            "ValidationReport.limit_profile",
        )
        if self.observed_capabilities.format_profile != self.limit_profile.profile_id:
            raise ValueError("capability and limit profile identities must match")
        if not self.observed_capabilities.validatable:
            raise ValueError("validation reports require a validatable codec capability")
        _require_nonnegative_int(
            self.provisional_record_count,
            field_name="ValidationReport.provisional_record_count",
        )
        if self.provisional_record_count > self.limit_profile.max_records:
            raise ValueError("provisional record count exceeds the bound limit profile")
        _validate_issue_counts(
            self.issue_counts,
            limit_profile=self.limit_profile,
            warning_only=False,
            field_name="ValidationReport.issue_counts",
        )
        _require_tuple(self.issues, field_name="ValidationReport.issues")
        if len(self.issues) > self.limit_profile.max_retained_issues:
            raise ValueError("retained issues exceed the active limit profile")
        retained_counts: dict[tuple[str, IssueSeverity], int] = {}
        for issue in self.issues:
            _require_exact_instance(issue, ParseIssue, "ValidationReport issue")
            if issue.code not in self.limit_profile.declared_issue_codes:
                raise ValueError("retained issue code is outside the profile allowlist")
            key = (issue.code, issue.severity)
            retained_counts[key] = retained_counts.get(key, 0) + 1
        reported_counts = {
            (item.code, item.severity): item.count for item in self.issue_counts
        }
        for key, retained_count in retained_counts.items():
            if reported_counts.get(key, 0) < retained_count:
                raise ValueError("issue counts cannot be smaller than retained issues")
        total_reported = sum(item.count for item in self.issue_counts)
        if type(self.issues_truncated) is not bool:
            raise TypeError("ValidationReport.issues_truncated must be exact bool")
        if self.issues_truncated:
            if total_reported <= len(self.issues):
                raise ValueError("truncated reports must count omitted issues")
        elif total_reported != len(self.issues):
            raise ValueError("untruncated issue counts must exactly match retained issues")
        fatal_count = sum(
            item.count
            for item in self.issue_counts
            if item.severity is IssueSeverity.FATAL
        )
        if self.outcome is ValidationOutcome.SUCCESS:
            if self.source is None or self.terminal is None:
                raise ValueError("successful validation requires source and verified terminal")
            _require_exact_instance(
                self.terminal,
                TerminalSuccess,
                "ValidationReport.terminal",
            )
            if fatal_count != 0 or self.terminal.fatal_count != 0:
                raise ValueError("successful validation cannot contain fatal issues")
            if self.terminal.source != self.source:
                raise ValueError("terminal source snapshot does not match validation")
            if self.terminal.codec_identity != self.codec_identity:
                raise ValueError("terminal codec identity does not match validation")
            if self.terminal.limit_profile != self.limit_profile:
                raise ValueError("terminal limit profile does not match validation")
            if self.terminal.record_count != self.provisional_record_count:
                raise ValueError("terminal record count does not match validation")
            warning_counts = tuple(
                item
                for item in self.issue_counts
                if item.severity is IssueSeverity.WARNING
            )
            if self.terminal.warning_counts != warning_counts:
                raise ValueError("terminal warning counts do not match validation")
            if self.terminal.issues_truncated != self.issues_truncated:
                raise ValueError("terminal issue truncation does not match validation")
        else:
            if self.terminal is not None:
                raise ValueError("failed or cancelled validation cannot carry a terminal")
            if self.outcome is ValidationOutcome.FAILED and fatal_count == 0:
                raise ValueError("failed validation requires at least one fatal issue")


def validate_metadata_containers(
    containers: tuple[tuple[MetadataEntry, ...], ...],
    *,
    limit_profile: LimitProfile,
) -> None:
    """Apply one profile's container, total-character, and depth metadata bounds."""

    _require_exact_instance(limit_profile, LimitProfile, "limit_profile")
    _require_tuple(containers, field_name="metadata containers")
    total_chars = 0
    for container in containers:
        _validate_metadata_entries(container, field_name="metadata container")
        if len(container) > limit_profile.max_metadata_entries_per_container:
            _raise_metadata_limit()
        container_chars = 0
        for entry in container:
            remaining = (
                limit_profile.max_metadata_decoded_chars_per_container
                - container_chars
                - len(entry.key)
            )
            if remaining < 0:
                _raise_metadata_limit()
            container_chars += len(entry.key) + _metadata_decoded_chars(
                entry.value,
                cutoff=remaining,
            )
            if _metadata_depth(entry.value) > limit_profile.max_structure_depth:
                _raise_metadata_limit()
        if container_chars > limit_profile.max_metadata_decoded_chars_per_container:
            _raise_metadata_limit()
        total_chars += container_chars
        if total_chars > limit_profile.max_metadata_decoded_chars_total:
            _raise_metadata_limit()


def validate_metadata_container_increment(
    container: tuple[MetadataEntry, ...],
    *,
    limit_profile: LimitProfile,
    prior_total_decoded_chars: int,
) -> int:
    """Validate one streamed metadata container and return its new total budget.

    Guarded sessions use this incrementally so a million-record streaming profile
    never needs to retain a million metadata tuples merely to enforce the same
    contract used by materialized records.
    """

    _require_exact_instance(limit_profile, LimitProfile, "limit_profile")
    prior = _require_nonnegative_int(
        prior_total_decoded_chars,
        field_name="prior_total_decoded_chars",
    )
    _validate_metadata_entries(container, field_name="metadata container")
    if len(container) > limit_profile.max_metadata_entries_per_container:
        _raise_metadata_limit()
    container_chars = 0
    for entry in container:
        remaining = (
            limit_profile.max_metadata_decoded_chars_per_container
            - container_chars
            - len(entry.key)
        )
        if remaining < 0:
            _raise_metadata_limit()
        container_chars += len(entry.key) + _metadata_decoded_chars(
            entry.value,
            cutoff=remaining,
        )
        if _metadata_depth(entry.value) > limit_profile.max_structure_depth:
            _raise_metadata_limit()
    if container_chars > limit_profile.max_metadata_decoded_chars_per_container:
        _raise_metadata_limit()
    total = prior + container_chars
    if total > limit_profile.max_metadata_decoded_chars_total:
        _raise_metadata_limit()
    return total


def _raise_metadata_limit() -> None:
    raise ContractViolation(
        "PARSER.LIMIT.METADATA",
        "metadata exceeds the active limit profile",
    )


def _metadata_decoded_chars(value: JsonMetadataValue, *, cutoff: int) -> int:
    """Count a deterministic decoded representation, stopping beyond ``cutoff``.

    Strings count their decoded characters; JSON scalar tokens count their canonical
    textual form; tuples count separators so large all-empty structures remain bounded.
    """

    total = 0
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            total += len(item)
        elif item is None:
            total += 4  # null
        elif type(item) is bool:
            total += 4 if item else 5  # true / false
        elif type(item) is int:
            total += _integer_decoded_chars(item, cutoff=max(0, cutoff - total))
        elif type(item) is float:
            total += len(repr(item))
        elif type(item) is tuple:
            total += 2 + max(0, len(item) - 1)  # tuple brackets and separators
            pending.extend(item)
        if total > cutoff:
            return cutoff + 1
    return total


def _integer_decoded_chars(value: int, *, cutoff: int) -> int:
    magnitude = abs(value)
    sign_chars = 1 if value < 0 else 0
    if magnitude == 0:
        return sign_chars + 1
    bit_length = magnitude.bit_length()
    lower_digits = ((bit_length - 1) * 30_102) // 100_000 + 1
    if sign_chars + lower_digits > cutoff:
        return cutoff + 1
    estimate = ((bit_length - 1) * 3_010_299_956_639_812) // 10**16 + 1
    threshold = 10**estimate
    if magnitude >= threshold:
        estimate += 1
    elif estimate > 1 and magnitude < threshold // 10:
        estimate -= 1
    return sign_chars + estimate


def _metadata_depth(value: JsonMetadataValue) -> int:
    if type(value) is not tuple:
        return 0
    maximum = 1
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        maximum = max(maximum, depth)
        for child in item:
            if type(child) is tuple:
                pending.append((child, depth + 1))
    return maximum


def _validate_issue_counts(
    counts: object,
    *,
    limit_profile: LimitProfile,
    warning_only: bool,
    field_name: str,
) -> None:
    values = _require_tuple(counts, field_name=field_name)
    for item in values:
        _require_exact_instance(item, IssueCount, f"{field_name} item")
        if item.code not in limit_profile.declared_issue_codes:
            raise ValueError(f"{field_name} contains an undeclared issue code")
        if warning_only and item.severity is not IssueSeverity.WARNING:
            raise ValueError(f"{field_name} may contain warning counts only")
    keys = tuple((item.code, item.severity.value) for item in values)
    if tuple(sorted(keys)) != keys:
        raise ValueError(f"{field_name} must use deterministic sorted order")
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field_name} must not contain duplicate code/severity pairs")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source: SourceSnapshotIdentity
    format_id: FormatId
    name: str
    source_locale: str | None
    target_locale: str | None
    segments: tuple[ParsedSegment, ...]
    document_metadata: tuple[MetadataEntry, ...]
    issues: tuple[ParseIssue, ...]
    capabilities: CodecCapabilities

    def __post_init__(self) -> None:
        _require_exact_instance(self.source, SourceSnapshotIdentity, "ParsedDocument.source")
        _require_exact_instance(self.format_id, FormatId, "ParsedDocument.format_id")
        _require_nonempty_text(self.name, field_name="ParsedDocument.name")
        _require_optional_text(self.source_locale, field_name="ParsedDocument.source_locale")
        _require_optional_text(self.target_locale, field_name="ParsedDocument.target_locale")
        _require_tuple(self.segments, field_name="ParsedDocument.segments")
        if not self.segments:
            raise ValueError("ParsedDocument.segments must be non-empty")
        for segment in self.segments:
            _require_exact_instance(segment, ParsedSegment, "ParsedDocument segment")
        _require_unique_local_ids(self.segments, owner="ParsedDocument")
        _validate_metadata_entries(
            self.document_metadata,
            field_name="ParsedDocument.document_metadata",
        )
        _require_tuple(self.issues, field_name="ParsedDocument.issues")
        for issue in self.issues:
            _require_exact_instance(issue, ParseIssue, "ParsedDocument issue")
        _require_exact_instance(
            self.capabilities,
            CodecCapabilities,
            "ParsedDocument.capabilities",
        )


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    local_id: str
    source: str
    target: str
    speaker: RawSpeaker
    format_metadata: tuple[MetadataEntry, ...]

    def __post_init__(self) -> None:
        _require_nonempty_text(self.local_id, field_name="ResourceRecord.local_id")
        _require_nonempty_text(self.source, field_name="ResourceRecord.source")
        _require_nonempty_text(self.target, field_name="ResourceRecord.target")
        _require_exact_instance(self.speaker, RawSpeaker, "ResourceRecord.speaker")
        _validate_metadata_entries(self.format_metadata, field_name="ResourceRecord.format_metadata")


@dataclass(frozen=True, slots=True)
class CanonicalSegmentWrite:
    local_id: str
    source: str
    target: str
    speaker: RawSpeaker
    confirmed: bool

    def __post_init__(self) -> None:
        _require_nonempty_text(self.local_id, field_name="CanonicalSegmentWrite.local_id")
        _require_nonempty_text(self.source, field_name="CanonicalSegmentWrite.source")
        if type(self.target) is not str:
            raise TypeError("CanonicalSegmentWrite.target must be an exact string")
        _require_exact_instance(self.speaker, RawSpeaker, "CanonicalSegmentWrite.speaker")
        if type(self.confirmed) is not bool:
            raise TypeError("CanonicalSegmentWrite.confirmed must be exact bool")


@dataclass(frozen=True, slots=True)
class CanonicalDocumentWrite:
    name: str
    source_locale: str
    target_locale: str
    segments: tuple[CanonicalSegmentWrite, ...]

    def __post_init__(self) -> None:
        _require_nonempty_text(self.name, field_name="CanonicalDocumentWrite.name")
        _require_nonempty_text(
            self.source_locale,
            field_name="CanonicalDocumentWrite.source_locale",
        )
        _require_nonempty_text(
            self.target_locale,
            field_name="CanonicalDocumentWrite.target_locale",
        )
        _require_tuple(self.segments, field_name="CanonicalDocumentWrite.segments")
        if not self.segments:
            raise ValueError("CanonicalDocumentWrite.segments must be non-empty")
        for segment in self.segments:
            _require_exact_instance(
                segment,
                CanonicalSegmentWrite,
                "CanonicalDocumentWrite segment",
            )
        _require_unique_local_ids(self.segments, owner="CanonicalDocumentWrite")


@dataclass(frozen=True, slots=True)
class CanonicalSerializeRequest:
    format_id: FormatId
    document: CanonicalDocumentWrite

    def __post_init__(self) -> None:
        _require_exact_instance(self.format_id, FormatId, "CanonicalSerializeRequest.format_id")
        _require_exact_instance(
            self.document,
            CanonicalDocumentWrite,
            "CanonicalSerializeRequest.document",
        )


@dataclass(frozen=True, slots=True)
class CanonicalBytes:
    codec_identity: CodecIdentity
    format_id: FormatId
    schema_version: int
    payload: bytes

    def __post_init__(self) -> None:
        _require_exact_instance(
            self.codec_identity,
            CodecIdentity,
            "CanonicalBytes.codec_identity",
        )
        _require_exact_instance(self.format_id, FormatId, "CanonicalBytes.format_id")
        _require_positive_int(self.schema_version, field_name="CanonicalBytes.schema_version")
        if type(self.payload) is not bytes:
            raise TypeError("CanonicalBytes.payload must be exact bytes")


class InputConsumptionPolicy(Enum):
    """Foundation-verifiable proof required before a raw EOF can authorize commit."""

    SEALED_BYTES_EOF = "sealed_bytes_eof"
    XLSX_PREFLIGHT_ACTIVE_SHEET = "xlsx_preflight_active_sheet"


@runtime_checkable
class SnapshotCursorLease(Protocol):
    """Read-only offset-zero cursor capability passed to a raw codec."""

    @property
    def source_identity(self) -> SourceSnapshotIdentity: ...

    @property
    def byte_count(self) -> int: ...

    @property
    def consumption_proved(self) -> bool: ...

    @property
    def closed(self) -> bool: ...

    def read(self, size: int = -1) -> bytes: ...

    def tell(self) -> int: ...

    def seekable(self) -> bool: ...

    def close(self) -> None: ...


RawParseEvent = DocumentHeader | ParsedSegment | ResourceRecord | ParseIssue


@runtime_checkable
class RawReaderCodec(Protocol):
    descriptor: "CodecDescriptor"

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]: ...


@runtime_checkable
class CanonicalSerializerCodec(Protocol):
    descriptor: "CodecDescriptor"

    def serialize_canonical(
        self,
        request: CanonicalSerializeRequest,
    ) -> CanonicalBytes: ...


ReaderFactory = Callable[[], RawReaderCodec]
CanonicalSerializerFactory = Callable[[], CanonicalSerializerCodec]


@dataclass(frozen=True, slots=True)
class CodecDescriptor:
    """Immutable purpose/format authority and its behavior factories."""

    identity: CodecIdentity
    purpose: EffectivePurpose
    format_id: FormatId
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    sniff_prefixes: tuple[bytes, ...]
    capabilities: CodecCapabilities
    limit_profile: LimitProfile
    input_consumption_policy: InputConsumptionPolicy
    reader_factory: ReaderFactory | None
    canonical_serializer_factory: CanonicalSerializerFactory | None

    def __post_init__(self) -> None:
        _require_exact_instance(self.identity, CodecIdentity, "CodecDescriptor.identity")
        _require_exact_instance(self.purpose, EffectivePurpose, "CodecDescriptor.purpose")
        _require_exact_instance(self.format_id, FormatId, "CodecDescriptor.format_id")
        _require_tuple(self.extensions, field_name="CodecDescriptor.extensions")
        _require_tuple(self.mime_types, field_name="CodecDescriptor.mime_types")
        _require_tuple(self.sniff_prefixes, field_name="CodecDescriptor.sniff_prefixes")
        if len(self.extensions) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("descriptor declares too many extensions")
        if len(self.mime_types) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("descriptor declares too many MIME types")
        if len(self.sniff_prefixes) > MAX_HINT_VALUES_PER_KIND:
            raise ValueError("descriptor declares too many sniff prefixes")
        extensions = tuple(_normalize_extension_hint(item) for item in self.extensions)
        mime_types = tuple(_normalize_mime_hint(item) for item in self.mime_types)
        if len(set(extensions)) != len(extensions):
            raise ValueError("descriptor extensions must be unique")
        if len(set(mime_types)) != len(mime_types):
            raise ValueError("descriptor MIME types must be unique")
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "mime_types", mime_types)
        for prefix in self.sniff_prefixes:
            if type(prefix) is not bytes:
                raise TypeError("descriptor sniff prefixes must be exact bytes")
            if not prefix:
                raise ValueError("descriptor sniff prefixes must be non-empty")
            if len(prefix) > MAX_SNIFF_PREFIX_BYTES:
                raise ValueError("descriptor sniff prefix exceeds 4 KiB")
        if len(set(self.sniff_prefixes)) != len(self.sniff_prefixes):
            raise ValueError("descriptor sniff prefixes must be unique")
        _require_exact_instance(
            self.capabilities,
            CodecCapabilities,
            "CodecDescriptor.capabilities",
        )
        _require_exact_instance(
            self.limit_profile,
            LimitProfile,
            "CodecDescriptor.limit_profile",
        )
        _require_exact_instance(
            self.input_consumption_policy,
            InputConsumptionPolicy,
            "CodecDescriptor.input_consumption_policy",
        )
        if self.capabilities.format_profile != self.limit_profile.profile_id:
            raise ValueError("descriptor capability and limit profile identities differ")
        expected_purpose = builtin_purpose_for_format(self.format_id)
        if expected_purpose is not None and expected_purpose is not self.purpose:
            raise ContractViolation(
                "PARSER.SELECTION.UNSUPPORTED",
                "built-in format is registered for an incompatible purpose",
            )
        if self.capabilities.readable:
            if not callable(self.reader_factory):
                raise ValueError("readable descriptor requires a reader factory")
        elif self.reader_factory is not None:
            raise ValueError("non-readable descriptor cannot carry a reader factory")
        if self.capabilities.canonical_write:
            if not callable(self.canonical_serializer_factory):
                raise ValueError(
                    "canonical-write descriptor requires a serializer factory"
                )
        elif self.canonical_serializer_factory is not None:
            raise ValueError(
                "descriptor without canonical-write capability cannot carry a serializer"
            )
        if self.capabilities.validatable and not self.capabilities.readable:
            raise ValueError("validatable descriptor must also be readable")

    @property
    def declared_issue_codes(self) -> tuple[str, ...]:
        """One authority: issue codes are projected from the bound limit profile."""

        return self.limit_profile.declared_issue_codes


@runtime_checkable
class SeekableInputPreflightCodec(Protocol):
    """Policy-specific structural port for seekable container preflight.

    Foundation invokes this behavior before the codec's sole raw grammar.  The
    implementation must inspect the supplied lease itself; returning a token or
    boolean is deliberately not an alternative to Foundation-observed reads.
    """

    descriptor: CodecDescriptor

    def preflight_input(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> None: ...


@runtime_checkable
class CodecProvider(Protocol):
    provider_id: str
    provider_version: str

    def descriptors(self) -> tuple[CodecDescriptor, ...]: ...


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """Foundation-issued proof for one completed atomic byte replacement."""

    target_relative_reference_sha256: str
    regular_file_identity: str
    content_sha256: str
    byte_count: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_sha256(
            self.target_relative_reference_sha256,
            field_name="WriteReceipt.target_relative_reference_sha256",
        )
        _require_nonempty_text(
            self.regular_file_identity,
            field_name="WriteReceipt.regular_file_identity",
        )
        _require_sha256(
            self.content_sha256,
            field_name="WriteReceipt.content_sha256",
        )
        _require_nonnegative_int(self.byte_count, field_name="WriteReceipt.byte_count")
        _require_positive_int(self.schema_version, field_name="WriteReceipt.schema_version")


def _validate_metadata_value(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or type(item) in {str, int, bool}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("metadata floats must be finite")
            continue
        if type(item) is tuple:
            pending.extend(item)
            continue
        raise TypeError("metadata values must be JSON-compatible scalars or tuples")


def _validate_metadata_entries(value: object, *, field_name: str) -> None:
    entries = _require_tuple(value, field_name=field_name)
    for entry in entries:
        _require_exact_instance(entry, MetadataEntry, f"{field_name} item")
    keys = tuple(entry.key for entry in entries)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field_name} keys must be unique")


def _require_unique_local_ids(records: tuple[object, ...], *, owner: str) -> None:
    local_ids = tuple(getattr(record, "local_id") for record in records)
    if len(set(local_ids)) != len(local_ids):
        raise ValueError(f"{owner} local IDs must be unique within one input")


def _require_optional_text(value: object, *, field_name: str) -> None:
    if value is None:
        return
    _require_nonempty_text(value, field_name=field_name)


def _require_nonnegative_int(value: object, *, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    number = _require_nonnegative_int(value, field_name=field_name)
    if number == 0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _require_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be lowercase SHA-256")
    return value


def _normalize_extension_hint(value: object) -> str:
    extension = _require_nonempty_text(value, field_name="extension hint")
    if len(extension) > MAX_EXTENSION_HINT_CHARS:
        raise ValueError("extension hint is too long")
    if not _EXTENSION_HINT_PATTERN.fullmatch(extension):
        raise ValueError("extension hint has an invalid structure")
    return extension.lower()


def _normalize_mime_hint(value: object) -> str:
    mime_type = _require_nonempty_text(value, field_name="MIME hint")
    if len(mime_type) > MAX_MIME_HINT_CHARS:
        raise ValueError("MIME hint is too long")
    if not _MIME_HINT_PATTERN.fullmatch(mime_type):
        raise ValueError("MIME hint must be an unparameterized type/subtype")
    return mime_type.lower()
