"""Singular-profile PO/POT readers for the neutral Parser Foundation.

The codec owns gettext tokenization and quoted-string decoding.  It deliberately
does not map ``msgctxt`` to speaker or TM context and exposes no write surface.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from pathlib import PurePath
import re
from typing import Iterator

from parser_contracts import (
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    InputConsumptionPolicy,
    IssueSeverity,
    LimitProfile,
    MetadataEntry,
    ParseIssue,
    ParsedSegment,
    RawParseEvent,
    RawSpeaker,
    ReadRequest,
    SnapshotCursorLease,
    TargetPresence,
    TranslationState,
)


_MIB = 1024 * 1024
_INPUT_BYTES = 100 * _MIB
_FIELD_CHARS = 100 * _MIB
_RECORDS = 1_000_000
_MATERIALIZED_RECORDS = 100_000
_RETAINED_ISSUES = 256
_METADATA_ENTRIES = 256
_METADATA_CONTAINER_CHARS = 1 * _MIB
_METADATA_TOTAL_CHARS = 16 * _MIB
_TEXT_READ_CHUNK_BYTES = 64 * 1024

_SYNTAX = "PARSER.GETTEXT.SYNTAX"
_PLURAL_UNSUPPORTED = "PARSER.GETTEXT.PLURAL_UNSUPPORTED"
_CHARSET_UNSUPPORTED = "PARSER.GETTEXT.CHARSET_UNSUPPORTED"
_EMPTY_INPUT = "PARSER.GETTEXT.EMPTY_INPUT"
_ENCODING_FAILED = "PARSER.SOURCE.ENCODING_FAILED"

_SIMPLE_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
    "'": b"'",
    "?": b"?",
}
_OCTAL = frozenset("01234567")
_HEX = frozenset("0123456789abcdefABCDEF")
_DIRECTIVE = re.compile(r"^(msgctxt|msgid|msgstr)[ \t]+(.*)$")
_PLURAL_DIRECTIVE = re.compile(r"^(?:msgid_plural\b|msgstr[ \t]*\[)")
_CHARSET = re.compile(r"(?im)^content-type:[^\n]*?\bcharset[ \t]*=[ \t]*([^;\s]+)")
_CHARSET_MARKER = re.compile(r"(?i)\bcharset[ \t]*=")
_UTF8_BOM = b"\xef\xbb\xbf"


def _issue_codes(*additional: str) -> tuple[str, ...]:
    return tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES + additional)))


def _limit_profile(profile_id: str) -> LimitProfile:
    return LimitProfile(
        profile_id=profile_id,
        profile_version=1,
        max_input_bytes=_INPUT_BYTES,
        max_decoded_field_chars=_FIELD_CHARS,
        max_records=_RECORDS,
        max_materialized_records=_MATERIALIZED_RECORDS,
        max_retained_issues=_RETAINED_ISSUES,
        declared_issue_codes=_issue_codes(
            _CHARSET_UNSUPPORTED,
            _EMPTY_INPUT,
            _ENCODING_FAILED,
            _PLURAL_UNSUPPORTED,
            _SYNTAX,
        ),
        max_metadata_entries_per_container=_METADATA_ENTRIES,
        max_metadata_decoded_chars_per_container=_METADATA_CONTAINER_CHARS,
        max_metadata_decoded_chars_total=_METADATA_TOTAL_CHARS,
        max_structure_depth=16,
    )


GETTEXT_PO_LIMIT_PROFILE = _limit_profile("gettext-po-v1")
GETTEXT_POT_LIMIT_PROFILE = _limit_profile("gettext-pot-v1")


@dataclass(slots=True)
class _Entry:
    start_line: int
    comments: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    previous_values: list[str] = field(default_factory=list)
    msgctxt: bytes | None = None
    msgid: bytes | None = None
    msgstr: bytes | None = None
    active_field: str | None = None
    saw_directive: bool = False

    def append_comment(self, line: str) -> None:
        if line.startswith("#:"):
            self.references.append(line)
        elif line.startswith("#,"):
            self.flags.append(line)
        elif line.startswith("#|"):
            self.previous_values.append(line)
        else:
            self.comments.append(line)

    @property
    def fuzzy(self) -> bool:
        for raw in self.flags:
            payload = raw[2:]
            if any(item.strip() == "fuzzy" for item in payload.split(",")):
                return True
        return False


@dataclass(frozen=True, slots=True)
class _CompletedEntry:
    start_line: int
    comments: tuple[str, ...]
    references: tuple[str, ...]
    flags: tuple[str, ...]
    previous_values: tuple[str, ...]
    msgctxt: str | None
    msgid: str
    msgstr: str

    @property
    def fuzzy(self) -> bool:
        for raw in self.flags:
            if any(item.strip() == "fuzzy" for item in raw[2:].split(",")):
                return True
        return False


class _GettextInputError(Exception):
    __slots__ = ("code", "line_number", "safe_summary")

    def __init__(self, code: str, line_number: int, safe_summary: str) -> None:
        self.code = code
        self.line_number = line_number
        self.safe_summary = safe_summary
        super().__init__(safe_summary)


class _Utf8SigLines(Iterator[tuple[int, str]]):
    """Strict UTF-8[-BOM] reader whose physical lines are LF/CR/CRLF only."""

    __slots__ = ("_source", "_buffer", "_eof", "_line_number", "_first_line")

    def __init__(self, source: SnapshotCursorLease) -> None:
        self._source = source
        self._buffer = b""
        self._eof = False
        self._line_number = 0
        self._first_line = True

    def __iter__(self) -> _Utf8SigLines:
        return self

    def __next__(self) -> tuple[int, str]:
        while True:
            line_end = self._line_end()
            if line_end is not None:
                line = self._buffer[:line_end]
                self._buffer = self._buffer[line_end:]
                self._line_number += 1
                return self._line_number, self._decode_line(_remove_line_ending(line))
            if self._eof:
                if not self._buffer:
                    raise StopIteration
                line = self._buffer
                self._buffer = b""
                self._line_number += 1
                return self._line_number, self._decode_line(line)
            payload = self._source.read(_TEXT_READ_CHUNK_BYTES)
            if payload:
                self._buffer += payload
            else:
                self._eof = True

    def _line_end(self) -> int | None:
        for index, value in enumerate(self._buffer):
            if value not in {0x0A, 0x0D}:
                continue
            if value == 0x0A:
                return index + 1
            if index + 1 < len(self._buffer):
                return index + 2 if self._buffer[index + 1] == 0x0A else index + 1
            if self._eof:
                return index + 1
            return None
        return None

    def _decode_line(self, value: bytes) -> str:
        if self._first_line:
            self._first_line = False
            if value.startswith(_UTF8_BOM):
                value = value[len(_UTF8_BOM) :]
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _GettextInputError(
                _ENCODING_FAILED,
                self._line_number,
                f"gettext physical line {self._line_number} is not valid UTF-8",
            ) from None


def _remove_line_ending(value: bytes) -> bytes:
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith((b"\r", b"\n")):
        return value[:-1]
    return value


def _fatal(code: str, summary: str, *, line_number: int | None = None) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity=IssueSeverity.FATAL,
        safe_summary=summary,
        line_number=line_number,
    )


def _decode_quoted(raw: str, line_number: int) -> bytes:
    """Decode one gettext/C string to bytes; UTF-8 is verified after concatenation."""

    if not raw.startswith('"'):
        raise _GettextInputError(
            _SYNTAX,
            line_number,
            f"gettext line {line_number} requires a quoted string",
        )
    output = bytearray()
    index = 1
    while index < len(raw):
        character = raw[index]
        if character == '"':
            if raw[index + 1 :].strip(" \t"):
                raise _GettextInputError(
                    _SYNTAX,
                    line_number,
                    f"gettext line {line_number} has trailing quoted-string syntax",
                )
            return bytes(output)
        if character != "\\":
            if ord(character) < 0x20 or ord(character) == 0x7F:
                raise _GettextInputError(
                    _SYNTAX,
                    line_number,
                    f"gettext line {line_number} contains an unescaped control character",
                )
            output.extend(character.encode("utf-8", errors="strict"))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            raise _GettextInputError(
                _SYNTAX,
                line_number,
                f"gettext line {line_number} ends in an incomplete escape",
            )
        escaped = raw[index]
        simple = _SIMPLE_ESCAPES.get(escaped)
        if simple is not None:
            output.extend(simple)
            index += 1
            continue
        if escaped in _OCTAL:
            end = index + 1
            while end < len(raw) and end - index < 3 and raw[end] in _OCTAL:
                end += 1
            value = int(raw[index:end], 8)
            if value > 0xFF:
                raise _GettextInputError(
                    _SYNTAX,
                    line_number,
                    f"gettext line {line_number} has an out-of-range octal escape",
                )
            output.append(value)
            index = end
            continue
        if escaped == "x":
            start = index + 1
            end = start
            while end < len(raw) and raw[end] in _HEX:
                end += 1
            if end == start:
                raise _GettextInputError(
                    _SYNTAX,
                    line_number,
                    f"gettext line {line_number} has an incomplete hexadecimal escape",
                )
            value = int(raw[start:end], 16)
            if value > 0xFF:
                raise _GettextInputError(
                    _SYNTAX,
                    line_number,
                    f"gettext line {line_number} has an out-of-range byte escape",
                )
            output.append(value)
            index = end
            continue
        raise _GettextInputError(
            _SYNTAX,
            line_number,
            f"gettext line {line_number} contains an unsupported escape",
        )
    raise _GettextInputError(
        _SYNTAX,
        line_number,
        f"gettext line {line_number} has an unterminated quoted string",
    )


def _append_field(
    entry: _Entry,
    field_name: str,
    value: bytes,
    *,
    line_number: int,
    maximum: int,
) -> None:
    current = getattr(entry, field_name)
    combined = value if current is None else current + value
    try:
        decoded_so_far = codecs.getincrementaldecoder("utf-8")("strict").decode(
            combined,
            final=False,
        )
    except UnicodeDecodeError:
        raise _GettextInputError(
            _ENCODING_FAILED,
            line_number,
            f"gettext field ending at line {line_number} is not valid UTF-8",
        ) from None
    if len(decoded_so_far) > maximum:
        raise _GettextInputError(
            "PARSER.LIMIT.FIELD",
            line_number,
            f"gettext field ending at line {line_number} exceeds the active limit profile",
        )
    setattr(entry, field_name, combined)


def _accept_line(
    entry: _Entry | None,
    raw_line: str,
    line_number: int,
    *,
    maximum: int,
) -> _Entry:
    logical = raw_line.lstrip(" \t")
    if entry is None:
        entry = _Entry(start_line=line_number)
    if logical.startswith("#"):
        if entry.saw_directive:
            raise _GettextInputError(
                _SYNTAX,
                line_number,
                f"gettext line {line_number} places a comment inside an entry",
            )
        entry.append_comment(logical)
        return entry
    if _PLURAL_DIRECTIVE.match(logical):
        raise _GettextInputError(
            _PLURAL_UNSUPPORTED,
            line_number,
            f"gettext plural syntax at line {line_number} is outside the singular profile",
        )
    if logical.startswith('"'):
        if entry.active_field is None:
            raise _GettextInputError(
                _SYNTAX,
                line_number,
                f"gettext line {line_number} has a continuation without a field",
            )
        _append_field(
            entry,
            entry.active_field,
            _decode_quoted(logical, line_number),
            line_number=line_number,
            maximum=maximum,
        )
        return entry
    match = _DIRECTIVE.match(logical)
    if match is None:
        raise _GettextInputError(
            _SYNTAX,
            line_number,
            f"gettext line {line_number} contains unsupported syntax",
        )
    directive, quoted = match.groups()
    value = _decode_quoted(quoted, line_number)
    if directive == "msgctxt":
        if entry.saw_directive or entry.msgctxt is not None:
            raise _GettextInputError(
                _SYNTAX,
                line_number,
                f"gettext line {line_number} places msgctxt outside its singular position",
            )
    elif directive == "msgid":
        if entry.msgid is not None or entry.msgstr is not None:
            raise _GettextInputError(
                _SYNTAX,
                line_number,
                f"gettext line {line_number} duplicates or misorders msgid",
            )
    elif entry.msgid is None or entry.msgstr is not None:
        raise _GettextInputError(
            _SYNTAX,
            line_number,
            f"gettext line {line_number} duplicates or misorders msgstr",
        )
    entry.saw_directive = True
    entry.active_field = directive
    _append_field(
        entry,
        directive,
        value,
        line_number=line_number,
        maximum=maximum,
    )
    return entry


def _finish_entry(entry: _Entry, line_number: int) -> _CompletedEntry:
    if not entry.saw_directive or entry.msgid is None or entry.msgstr is None:
        raise _GettextInputError(
            _SYNTAX,
            line_number,
            f"gettext entry ending at line {line_number} is incomplete",
        )
    decoded: dict[str, str | None] = {}
    for field_name in ("msgctxt", "msgid", "msgstr"):
        value = getattr(entry, field_name)
        if value is None:
            decoded[field_name] = None
            continue
        try:
            decoded[field_name] = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _GettextInputError(
                _ENCODING_FAILED,
                line_number,
                f"gettext entry ending at line {line_number} has invalid escaped UTF-8",
            ) from None
    assert decoded["msgid"] is not None and decoded["msgstr"] is not None
    return _CompletedEntry(
        start_line=entry.start_line,
        comments=tuple(entry.comments),
        references=tuple(entry.references),
        flags=tuple(entry.flags),
        previous_values=tuple(entry.previous_values),
        msgctxt=decoded["msgctxt"],
        msgid=decoded["msgid"],
        msgstr=decoded["msgstr"],
    )


def _metadata(
    entry: _CompletedEntry,
    *,
    include_context: bool,
) -> tuple[MetadataEntry, ...]:
    result: list[MetadataEntry] = []
    for key, values in (
        ("gettext.comments", entry.comments),
        ("gettext.references", entry.references),
        ("gettext.flags", entry.flags),
        ("gettext.previous_values", entry.previous_values),
    ):
        if values:
            result.append(MetadataEntry(key, values))
    if include_context and entry.msgctxt is not None:
        result.append(MetadataEntry("gettext.msgctxt", entry.msgctxt))
    return tuple(result)


def _charset_is_utf8(header: str) -> bool:
    declarations = _CHARSET.findall(header)
    if _CHARSET_MARKER.search(header) and not declarations:
        return False
    return all(value.strip('"\'').lower() == "utf-8" for value in declarations)


def _document_name(source: SnapshotCursorLease) -> str:
    hint = source.source_name_hint
    if type(hint) is not str or not hint:
        raise ContractViolation(
            "PARSER.SOURCE.READ_FAILED",
            "sealed source does not publish a usable final-component name",
        )
    stem = PurePath(hint.replace("\\", "/")).stem
    if not stem:
        raise ContractViolation(
            "PARSER.SOURCE.READ_FAILED",
            "sealed gettext source has no usable file stem",
        )
    return stem


class _GettextCodec:
    __slots__ = ("descriptor", "_pot")

    def __init__(self, descriptor: CodecDescriptor, *, pot: bool) -> None:
        expected = GETTEXT_POT_V1 if pot else GETTEXT_PO_V1
        if type(descriptor) is not CodecDescriptor or descriptor.format_id != expected:
            raise TypeError("descriptor must match the selected gettext format")
        self.descriptor = descriptor
        self._pot = pot

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        if type(request) is not ReadRequest:
            raise TypeError("request must be an exact ReadRequest")
        if (
            request.purpose is not EffectivePurpose.PROJECT_DOCUMENT
            or request.format_id != self.descriptor.format_id
        ):
            raise ContractViolation(
                "PARSER.SELECTION.UNSUPPORTED",
                "read request does not match the selected gettext project codec",
            )
        try:
            name = _document_name(source)
        except ContractViolation as exc:
            yield _fatal(exc.code, exc.safe_summary)
            return

        profile = self.descriptor.limit_profile
        entry: _Entry | None = None
        header_metadata: tuple[MetadataEntry, ...] = ()
        header_emitted = False
        header_seen = False
        record_count = 0
        last_line = 1

        def events_for(completed: _CompletedEntry) -> Iterator[RawParseEvent]:
            nonlocal header_metadata, header_emitted, header_seen, record_count
            assert completed.msgid is not None and completed.msgstr is not None
            if completed.msgid == "":
                if header_seen or header_emitted or record_count or completed.msgctxt is not None:
                    raise _GettextInputError(
                        _SYNTAX,
                        completed.start_line,
                        f"gettext line {completed.start_line} has a misplaced header entry",
                    )
                if not _charset_is_utf8(completed.msgstr):
                    raise _GettextInputError(
                        _CHARSET_UNSUPPORTED,
                        completed.start_line,
                        f"gettext header at line {completed.start_line} declares a non-UTF-8 charset",
                    )
                header_seen = True
                header_metadata = _metadata(completed, include_context=False) + (
                    MetadataEntry("gettext.header", completed.msgstr),
                )
                return
            if not header_emitted:
                header_emitted = True
                yield DocumentHeader(name, None, None, header_metadata)
            if record_count >= profile.max_records:
                raise _GettextInputError(
                    "PARSER.LIMIT.RECORD",
                    completed.start_line,
                    f"gettext record at line {completed.start_line} exceeds the active limit profile",
                )
            if self._pot and completed.msgstr:
                raise _GettextInputError(
                    _SYNTAX,
                    completed.start_line,
                    f"gettext POT entry at line {completed.start_line} contains a translated target",
                )
            record_count += 1
            target = completed.msgstr
            yield ParsedSegment(
                local_id=f"entry-{completed.start_line}-1",
                source=completed.msgid,
                target=target,
                target_presence=(
                    TargetPresence.EXPLICIT_EMPTY
                    if target == ""
                    else TargetPresence.PRESENT
                ),
                translation_state=(
                    TranslationState.FORMAT_DERIVED_UNCONFIRMED
                    if completed.fuzzy
                    else None
                ),
                speaker=RawSpeaker(""),
                format_metadata=_metadata(completed, include_context=True),
            )

        try:
            for line_number, raw_line in _Utf8SigLines(source):
                last_line = line_number
                if not raw_line.strip(" \t"):
                    if entry is None:
                        continue
                    completed = _finish_entry(entry, line_number)
                    entry = None
                    yield from events_for(completed)
                    continue
                entry = _accept_line(
                    entry,
                    raw_line,
                    line_number,
                    maximum=profile.max_decoded_field_chars,
                )
            if entry is not None:
                completed = _finish_entry(entry, last_line)
                yield from events_for(completed)
        except UnicodeDecodeError:
            yield _fatal(
                _ENCODING_FAILED,
                "gettext input is not valid UTF-8",
                line_number=last_line,
            )
            return
        except _GettextInputError as exc:
            yield _fatal(exc.code, exc.safe_summary, line_number=exc.line_number)
            return

        if not header_emitted:
            yield DocumentHeader(name, None, None, header_metadata)
        if record_count == 0:
            yield _fatal(
                _EMPTY_INPUT,
                "gettext input contains no singular translatable entries",
                line_number=last_line,
            )


class GettextPoCodec(_GettextCodec):
    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        super().__init__(descriptor or GETTEXT_PO_DESCRIPTOR, pot=False)


class GettextPotCodec(_GettextCodec):
    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        super().__init__(descriptor or GETTEXT_POT_DESCRIPTOR, pot=True)


def _po_reader_factory() -> GettextPoCodec:
    return GettextPoCodec(GETTEXT_PO_DESCRIPTOR)


def _pot_reader_factory() -> GettextPotCodec:
    return GettextPotCodec(GETTEXT_POT_DESCRIPTOR)


def _capabilities(profile_id: str) -> CodecCapabilities:
    return CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=False,
        source_round_trip_write=False,
        streaming_input=True,
        iterator_view=True,
        materialized_view=True,
        format_profile=profile_id,
    )


GETTEXT_PO_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "gettext-po", "1"),
    purpose=EffectivePurpose.PROJECT_DOCUMENT,
    format_id=GETTEXT_PO_V1,
    extensions=(".po",),
    mime_types=("text/x-gettext-translation",),
    sniff_prefixes=(b"msgid", b"#"),
    capabilities=_capabilities(GETTEXT_PO_LIMIT_PROFILE.profile_id),
    limit_profile=GETTEXT_PO_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=_po_reader_factory,
    canonical_serializer_factory=None,
)

GETTEXT_POT_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "gettext-pot", "1"),
    purpose=EffectivePurpose.PROJECT_DOCUMENT,
    format_id=GETTEXT_POT_V1,
    extensions=(".pot",),
    mime_types=("text/x-gettext-template",),
    sniff_prefixes=(b"msgid", b"#"),
    capabilities=_capabilities(GETTEXT_POT_LIMIT_PROFILE.profile_id),
    limit_profile=GETTEXT_POT_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=_pot_reader_factory,
    canonical_serializer_factory=None,
)


def gettext_descriptors() -> tuple[CodecDescriptor, CodecDescriptor]:
    """Return immutable PO/POT descriptors for explicit built-in composition."""

    return GETTEXT_PO_DESCRIPTOR, GETTEXT_POT_DESCRIPTOR


__all__ = (
    "GETTEXT_PO_DESCRIPTOR",
    "GETTEXT_PO_LIMIT_PROFILE",
    "GETTEXT_POT_DESCRIPTOR",
    "GETTEXT_POT_LIMIT_PROFILE",
    "GettextPoCodec",
    "GettextPotCodec",
    "gettext_descriptors",
)
