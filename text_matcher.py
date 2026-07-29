"""Pinned Unicode primitives used by LocalCAT's internal text-v1 matcher.

This module deliberately stops below substring search and capability gating.
It owns fold-v1 projection, Unicode 16.0.0 default word boundaries, and the
strict pure-CJK classifier consumed by later Feature 5 tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from unicode_word_break_data import (
    UNICODE_VERSION as DATA_UNICODE_VERSION,
    cjk_script,
    is_extended_pictographic,
    is_variation_selector,
    word_break_property,
)


UNICODE_VERSION = "16.0.0"
TEXT_MATCHER_SEMANTICS_VERSION = "text-v1-unicode-16.0.0"

_IGNORED_WORD_BREAK_PROPERTIES = frozenset({"Extend", "Format", "ZWJ"})
_NEWLINE_WORD_BREAK_PROPERTIES = frozenset({"CR", "LF", "Newline"})
_AH_LETTER_PROPERTIES = frozenset({"ALetter", "Hebrew_Letter"})
_MID_NUM_LET_Q_PROPERTIES = frozenset({"MidNumLet", "Single_Quote"})
_LETTER_BRIDGE_PROPERTIES = frozenset(
    {"MidLetter", "MidNumLet", "Single_Quote"}
)
_NUMERIC_BRIDGE_PROPERTIES = frozenset(
    {"MidNum", "MidNumLet", "Single_Quote"}
)
_EXTEND_NUM_LET_LEFT_PROPERTIES = frozenset(
    {"ALetter", "Hebrew_Letter", "Numeric", "Katakana", "ExtendNumLet"}
)
_EXTEND_NUM_LET_RIGHT_PROPERTIES = frozenset(
    {"ALetter", "Hebrew_Letter", "Numeric", "Katakana"}
)


@dataclass(frozen=True)
class FoldProjection:
    """fold-v1 text plus one original half-open span per folded code point."""

    folded_text: str
    source_spans: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.folded_text, str):
            raise TypeError("folded text must be a string")
        if not isinstance(self.source_spans, tuple):
            raise TypeError("source spans must be a tuple")
        if len(self.folded_text) != len(self.source_spans):
            raise ValueError(
                "source spans must align one-to-one with folded text"
            )
        for span in self.source_spans:
            if (
                not isinstance(span, tuple)
                or len(span) != 2
                or not all(
                    isinstance(index, int) and not isinstance(index, bool)
                    for index in span
                )
            ):
                raise TypeError(
                    "source spans must contain integer index pairs"
                )
            if span[0] < 0 or span[1] <= span[0]:
                raise ValueError(
                    "source spans must be non-empty half-open ranges"
                )


@dataclass(frozen=True)
class _NormalizedUnit:
    code_point: str
    source_start: int
    source_end: int


def _require_pinned_runtime() -> None:
    if DATA_UNICODE_VERSION != UNICODE_VERSION:
        raise RuntimeError(
            "generated Unicode data mismatch: "
            f"expected {UNICODE_VERSION}, got {DATA_UNICODE_VERSION}"
        )
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise RuntimeError(
            "Unicode runtime mismatch: "
            f"expected {UNICODE_VERSION}, got {unicodedata.unidata_version}"
        )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _canonical_decomposition_units(text: str) -> tuple[_NormalizedUnit, ...]:
    segments: list[list[_NormalizedUnit]] = []
    current_segment: list[_NormalizedUnit] = []
    for source_index, code_point in enumerate(text):
        for decomposed in unicodedata.normalize("NFD", code_point):
            unit = _NormalizedUnit(
                code_point=decomposed,
                source_start=source_index,
                source_end=source_index + 1,
            )
            if unicodedata.combining(decomposed) == 0:
                if current_segment:
                    segments.append(current_segment)
                current_segment = [unit]
            else:
                current_segment.append(unit)
    if current_segment:
        segments.append(current_segment)

    ordered: list[_NormalizedUnit] = []
    for segment in segments:
        starter_count = (
            1 if unicodedata.combining(segment[0].code_point) == 0 else 0
        )
        ordered.extend(segment[:starter_count])
        ordered.extend(
            sorted(
                segment[starter_count:],
                key=lambda item: unicodedata.combining(item.code_point),
            )
        )
    expected_nfd = unicodedata.normalize("NFD", text)
    actual_nfd = "".join(unit.code_point for unit in ordered)
    if actual_nfd != expected_nfd:
        raise RuntimeError("fold-v1 NFD projection invariant failed")
    return tuple(ordered)


def _canonical_composition_units(
    decomposed_units: tuple[_NormalizedUnit, ...],
) -> tuple[_NormalizedUnit, ...]:
    """Canonically compose ordered NFD units without losing provenance."""

    composed: list[_NormalizedUnit] = []
    starter_index: int | None = None
    last_combining_class = 0
    for unit in decomposed_units:
        combining_class = unicodedata.combining(unit.code_point)
        composite: str | None = None
        if starter_index is not None and (
            last_combining_class < combining_class
            or last_combining_class == 0
        ):
            starter = composed[starter_index]
            candidate = unicodedata.normalize(
                "NFC",
                starter.code_point + unit.code_point,
            )
            if len(candidate) == 1:
                composite = candidate
                composed[starter_index] = _NormalizedUnit(
                    code_point=composite,
                    source_start=min(
                        starter.source_start,
                        unit.source_start,
                    ),
                    source_end=max(
                        starter.source_end,
                        unit.source_end,
                    ),
                )
        if composite is not None:
            # A consumed combining character never blocks a later composition.
            continue

        composed.append(unit)
        if combining_class == 0:
            starter_index = len(composed) - 1
            last_combining_class = 0
        else:
            last_combining_class = combining_class
    return tuple(composed)


def fold_text_v1(text: str) -> FoldProjection:
    """Apply whole-string NFC then default casefold with original projection."""

    _require_pinned_runtime()
    raw = _require_text(text, "text")
    decomposed_units = _canonical_decomposition_units(raw)
    composed_units = _canonical_composition_units(decomposed_units)
    nfc_text = "".join(unit.code_point for unit in composed_units)
    authoritative_nfc = unicodedata.normalize("NFC", raw)
    if nfc_text != authoritative_nfc:
        raise RuntimeError("fold-v1 NFC projection invariant failed")

    folded_parts: list[str] = []
    folded_spans: list[tuple[int, int]] = []
    for unit in composed_units:
        folded_piece = unit.code_point.casefold()
        folded_parts.append(folded_piece)
        folded_spans.extend(
            ((unit.source_start, unit.source_end),) * len(folded_piece)
        )
    folded_text = "".join(folded_parts)
    authoritative_fold = authoritative_nfc.casefold()
    if folded_text != authoritative_fold:
        raise RuntimeError("fold-v1 casefold projection invariant failed")
    return FoldProjection(
        folded_text=folded_text,
        source_spans=tuple(folded_spans),
    )


def project_folded_span_v1(
    projection: FoldProjection,
    folded_start: int,
    folded_end: int,
) -> tuple[int, int] | None:
    """Project a folded half-open range to its minimal original cover."""

    _require_pinned_runtime()
    if not isinstance(projection, FoldProjection):
        raise TypeError("projection must be a FoldProjection")
    if (
        not isinstance(folded_start, int)
        or isinstance(folded_start, bool)
        or not isinstance(folded_end, int)
        or isinstance(folded_end, bool)
    ):
        raise TypeError("folded range indices must be integers")
    if (
        folded_start < 0
        or folded_end < folded_start
        or folded_end > len(projection.folded_text)
    ):
        raise ValueError("folded range is outside the projection")
    if folded_start == folded_end:
        return None
    covered = projection.source_spans[folded_start:folded_end]
    return (
        min(span[0] for span in covered),
        max(span[1] for span in covered),
    )


def _previous_significant(
    properties: tuple[str, ...],
    start_index: int,
) -> int | None:
    for index in range(start_index, -1, -1):
        if properties[index] not in _IGNORED_WORD_BREAK_PROPERTIES:
            return index
    return None


def _next_significant(
    properties: tuple[str, ...],
    start_index: int,
) -> int | None:
    for index in range(start_index, len(properties)):
        if properties[index] not in _IGNORED_WORD_BREAK_PROPERTIES:
            return index
    return None


def _is_boundary_at(
    text: str,
    properties: tuple[str, ...],
    boundary: int,
) -> bool:
    if boundary == 0 or boundary == len(text):
        return True

    direct_left = properties[boundary - 1]
    direct_right = properties[boundary]
    if direct_left == "CR" and direct_right == "LF":
        return False
    if direct_left in _NEWLINE_WORD_BREAK_PROPERTIES:
        return True
    if direct_right in _NEWLINE_WORD_BREAK_PROPERTIES:
        return True
    if (
        direct_left == "ZWJ"
        and is_extended_pictographic(ord(text[boundary]))
    ):
        return False
    if direct_left == direct_right == "WSegSpace":
        return False
    if (
        direct_right in _IGNORED_WORD_BREAK_PROPERTIES
        and direct_left not in _NEWLINE_WORD_BREAK_PROPERTIES
    ):
        return False

    left_index = _previous_significant(properties, boundary - 1)
    right_index = _next_significant(properties, boundary)
    if left_index is None or right_index is None:
        return True
    left = properties[left_index]
    right = properties[right_index]

    previous_index = _previous_significant(properties, left_index - 1)
    next_index = _next_significant(properties, right_index + 1)
    previous = (
        properties[previous_index] if previous_index is not None else None
    )
    following = properties[next_index] if next_index is not None else None

    if left in _AH_LETTER_PROPERTIES and right in _AH_LETTER_PROPERTIES:
        return False
    if (
        left in _AH_LETTER_PROPERTIES
        and right in _LETTER_BRIDGE_PROPERTIES
        and following in _AH_LETTER_PROPERTIES
    ):
        return False
    if (
        previous in _AH_LETTER_PROPERTIES
        and left in _LETTER_BRIDGE_PROPERTIES
        and right in _AH_LETTER_PROPERTIES
    ):
        return False
    if left == "Hebrew_Letter" and right == "Single_Quote":
        return False
    if (
        left == "Hebrew_Letter"
        and right == "Double_Quote"
        and following == "Hebrew_Letter"
    ):
        return False
    if (
        previous == "Hebrew_Letter"
        and left == "Double_Quote"
        and right == "Hebrew_Letter"
    ):
        return False
    if left == right == "Numeric":
        return False
    if left in _AH_LETTER_PROPERTIES and right == "Numeric":
        return False
    if left == "Numeric" and right in _AH_LETTER_PROPERTIES:
        return False
    if (
        previous == "Numeric"
        and left in _NUMERIC_BRIDGE_PROPERTIES
        and right == "Numeric"
    ):
        return False
    if (
        left == "Numeric"
        and right in _NUMERIC_BRIDGE_PROPERTIES
        and following == "Numeric"
    ):
        return False
    if left == right == "Katakana":
        return False
    if (
        left in _EXTEND_NUM_LET_LEFT_PROPERTIES
        and right == "ExtendNumLet"
    ):
        return False
    if (
        left == "ExtendNumLet"
        and right in _EXTEND_NUM_LET_RIGHT_PROPERTIES
    ):
        return False
    if left == right == "Regional_Indicator":
        regional_indicator_count = 0
        scan_index: int | None = left_index
        while (
            scan_index is not None
            and properties[scan_index] == "Regional_Indicator"
        ):
            regional_indicator_count += 1
            scan_index = _previous_significant(
                properties,
                scan_index - 1,
            )
        if regional_indicator_count % 2 == 1:
            return False
    return True


def word_boundaries_v1(text: str) -> tuple[int, ...]:
    """Return pinned UAX #29 rev.45 default word boundaries."""

    _require_pinned_runtime()
    raw = _require_text(text, "text")
    if not raw:
        return ()
    properties = tuple(
        word_break_property(ord(code_point)) for code_point in raw
    )
    return tuple(
        boundary
        for boundary in range(len(raw) + 1)
        if _is_boundary_at(raw, properties, boundary)
    )


def is_word_boundary_v1(text: str, index: int) -> bool:
    """Test one original-text code-point offset under pinned UAX #29."""

    _require_pinned_runtime()
    raw = _require_text(text, "text")
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("boundary index must be an integer")
    if index < 0 or index > len(raw):
        raise ValueError("boundary index is outside the text")
    return index in word_boundaries_v1(raw)


def is_pure_cjk_v1(query: str) -> bool:
    """Return whether every base uses an accepted pinned CJK Script value."""

    _require_pinned_runtime()
    raw = _require_text(query, "query")
    saw_base = False
    may_attach = False
    for code_point in raw:
        numeric = ord(code_point)
        if cjk_script(numeric) is not None:
            saw_base = True
            may_attach = True
            continue
        property_name = word_break_property(numeric)
        if (
            property_name == "Extend"
            or property_name == "ZWJ"
            or is_variation_selector(numeric)
        ):
            if not may_attach:
                return False
            continue
        return False
    return saw_base


__all__ = [
    "TEXT_MATCHER_SEMANTICS_VERSION",
    "UNICODE_VERSION",
    "FoldProjection",
    "fold_text_v1",
    "is_pure_cjk_v1",
    "is_word_boundary_v1",
    "project_folded_span_v1",
    "word_boundaries_v1",
]
