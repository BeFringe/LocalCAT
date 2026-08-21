"""Shared bounded JSON lexical preflight for Parser codecs.

This module deliberately knows only UTF-8 and JSON lexical structure.  It does
not interpret LocalCAT project fields, normalized TM records, or Parser DTOs.
The preflight performs the bounded checks that must happen before the one
stdlib ``json.loads`` materialization used by non-streaming JSON codecs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Final


_UTF8_BOM: Final = b"\xef\xbb\xbf"
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES: Final = frozenset('"\\/bfnrt')


class JsonBomPolicy(str, Enum):
    """Format-owned UTF-8 BOM policy supplied to the shared preflight."""

    REJECT = "reject"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class JsonPreflightLimits:
    """The JSON-specific limits projected from a codec's limit profile."""

    max_input_bytes: int
    max_string_chars: int
    max_structure_depth: int

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_bytes", self.max_input_bytes),
            ("max_string_chars", self.max_string_chars),
            ("max_structure_depth", self.max_structure_depth),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class JsonPreflightResult:
    """A bounded stdlib materialization plus content-free scan observations."""

    value: object
    input_bytes: int
    decoded_chars: int
    max_string_chars_seen: int
    max_structure_depth_seen: int
    had_utf8_bom: bool


class JsonPreflightError(ValueError):
    """Stable, body-safe failure returned by the neutral JSON support layer."""

    def __init__(
        self,
        code: str,
        safe_summary: str,
        *,
        byte_offset: int | None = None,
        char_offset: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {safe_summary}")
        self.code = code
        self.safe_summary = safe_summary
        self.byte_offset = byte_offset
        self.char_offset = char_offset


@dataclass(frozen=True, slots=True)
class _ScanResult:
    max_string_chars_seen: int
    max_structure_depth_seen: int


def _syntax_failure(summary: str, offset: int) -> JsonPreflightError:
    return JsonPreflightError(
        "PARSER.SYNTAX.MALFORMED",
        summary,
        char_offset=offset,
    )


def _count_string(text: str, opening_quote: int, maximum: int) -> tuple[int, int]:
    """Return ``(next_offset, decoded_char_count)`` for one JSON string."""

    offset = opening_quote + 1
    decoded_chars = 0
    length = len(text)

    while offset < length:
        character = text[offset]
        if character == '"':
            return offset + 1, decoded_chars
        if ord(character) < 0x20:
            raise _syntax_failure(
                "JSON string contains an unescaped control character",
                offset,
            )

        if character != "\\":
            decoded_chars += 1
            offset += 1
        else:
            escape_offset = offset
            offset += 1
            if offset >= length:
                raise _syntax_failure("JSON string escape is truncated", escape_offset)
            escape = text[offset]
            if escape in _SIMPLE_ESCAPES:
                decoded_chars += 1
                offset += 1
            elif escape == "u":
                digits_start = offset + 1
                digits_end = digits_start + 4
                if digits_end > length or any(
                    digit not in _HEX_DIGITS for digit in text[digits_start:digits_end]
                ):
                    raise _syntax_failure(
                        "JSON unicode escape is truncated or invalid",
                        escape_offset,
                    )

                code_unit = int(text[digits_start:digits_end], 16)
                offset = digits_end
                if (
                    0xD800 <= code_unit <= 0xDBFF
                    and text.startswith("\\u", offset)
                    and offset + 6 <= length
                    and all(
                        digit in _HEX_DIGITS
                        for digit in text[offset + 2 : offset + 6]
                    )
                ):
                    following = int(text[offset + 2 : offset + 6], 16)
                    if 0xDC00 <= following <= 0xDFFF:
                        offset += 6
                decoded_chars += 1
            else:
                raise _syntax_failure("JSON string escape is invalid", escape_offset)

        if decoded_chars > maximum:
            raise JsonPreflightError(
                "PARSER.LIMIT.FIELD",
                "JSON string exceeds the configured decoded-character limit",
                char_offset=opening_quote,
            )

    raise _syntax_failure("JSON string is truncated", opening_quote)


def _scan_json(text: str, limits: JsonPreflightLimits) -> _ScanResult:
    """Check JSON framing, strings, and nesting without duplicating its grammar."""

    stack: list[str] = []
    root_kind: str | None = None
    root_complete = False
    max_depth_seen = 0
    max_string_seen = 0
    offset = 0
    length = len(text)

    while offset < length:
        character = text[offset]

        if root_complete:
            if character.isspace():
                offset += 1
                continue
            raise _syntax_failure("JSON input has data after the complete root", offset)

        if character.isspace():
            if root_kind == "primitive" and not stack:
                root_complete = True
            offset += 1
            continue

        if root_kind == "primitive" and not stack:
            if character in '"{}[],:':
                raise _syntax_failure("JSON input has data after the root value", offset)
            offset += 1
            continue

        if character == '"':
            if root_kind is None:
                root_kind = "string"
            next_offset, decoded_chars = _count_string(
                text,
                offset,
                limits.max_string_chars,
            )
            max_string_seen = max(max_string_seen, decoded_chars)
            offset = next_offset
            if root_kind == "string" and not stack:
                root_complete = True
            continue

        if character in "{[":
            if root_kind is None:
                root_kind = "container"
            elif not stack:
                raise _syntax_failure("JSON input has data after the root value", offset)
            stack.append(character)
            depth = len(stack)
            if depth > limits.max_structure_depth:
                raise JsonPreflightError(
                    "PARSER.LIMIT.DEPTH",
                    "JSON structure exceeds the configured nesting-depth limit",
                    char_offset=offset,
                )
            max_depth_seen = max(max_depth_seen, depth)
            offset += 1
            continue

        if character in "}]":
            expected = "{" if character == "}" else "["
            if not stack or stack[-1] != expected:
                raise _syntax_failure("JSON container delimiter is mismatched", offset)
            stack.pop()
            offset += 1
            if not stack:
                root_complete = True
            continue

        if root_kind is None:
            root_kind = "primitive"
        offset += 1

    if root_kind is None:
        raise JsonPreflightError(
            "PARSER.SYNTAX.EMPTY_INPUT",
            "JSON input is empty",
        )
    if stack:
        raise _syntax_failure("JSON container is truncated", length)

    return _ScanResult(
        max_string_chars_seen=max_string_seen,
        max_structure_depth_seen=max_depth_seen,
    )


def load_bounded_json(
    data: bytes,
    limits: JsonPreflightLimits,
    *,
    bom_policy: JsonBomPolicy = JsonBomPolicy.REJECT,
) -> JsonPreflightResult:
    """Preflight UTF-8 JSON and materialize it exactly once with stdlib JSON.

    BOM acceptance belongs to the selected format profile: LocalCAT JSON passes
    ``ALLOW`` while normalized TM JSON keeps the default ``REJECT`` policy.
    """

    if type(data) is not bytes:
        raise TypeError("data must be exact bytes")
    if not isinstance(limits, JsonPreflightLimits):
        raise TypeError("limits must be JsonPreflightLimits")
    if not isinstance(bom_policy, JsonBomPolicy):
        raise TypeError("bom_policy must be JsonBomPolicy")

    input_bytes = len(data)
    if input_bytes > limits.max_input_bytes:
        raise JsonPreflightError(
            "PARSER.LIMIT.INPUT",
            "JSON input exceeds the configured byte limit",
        )

    had_utf8_bom = data.startswith(_UTF8_BOM)
    if had_utf8_bom and bom_policy is JsonBomPolicy.REJECT:
        raise JsonPreflightError(
            "PARSER.SYNTAX.MALFORMED",
            "UTF-8 BOM is not accepted by the selected JSON format profile",
            byte_offset=0,
        )
    encoded_body = data[len(_UTF8_BOM) :] if had_utf8_bom else data

    try:
        text = encoded_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise JsonPreflightError(
            "PARSER.SOURCE.ENCODING_FAILED",
            "JSON input is not valid UTF-8",
            byte_offset=error.start + (len(_UTF8_BOM) if had_utf8_bom else 0),
        ) from None

    scan = _scan_json(text, limits)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsonPreflightError(
            "PARSER.SYNTAX.MALFORMED",
            "JSON syntax is invalid",
            char_offset=error.pos,
        ) from None

    return JsonPreflightResult(
        value=value,
        input_bytes=input_bytes,
        decoded_chars=len(text),
        max_string_chars_seen=scan.max_string_chars_seen,
        max_structure_depth_seen=scan.max_structure_depth_seen,
        had_utf8_bom=had_utf8_bom,
    )


__all__ = (
    "JsonBomPolicy",
    "JsonPreflightError",
    "JsonPreflightLimits",
    "JsonPreflightResult",
    "load_bounded_json",
)
