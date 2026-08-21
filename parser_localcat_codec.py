"""Sole LocalCAT JSON/TXT project grammar and canonical JSON v1 serializer.

This format boundary emits only neutral Parser contracts.  It does not map an
editor session, infer project identity, or write a target itself.  Raw readers
consume their supplied sealed lease completely on successful EOF; only the
Foundation guarded session may issue ``TerminalSuccess``.
"""

from __future__ import annotations

import codecs
import json
from pathlib import PurePath
from typing import Iterator

from parser_contracts import (
    CanonicalBytes,
    CanonicalSerializeRequest,
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    InputConsumptionPolicy,
    IssueSeverity,
    LimitProfile,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    ParseIssue,
    ParsedSegment,
    RawParseEvent,
    RawSpeaker,
    ReadRequest,
    SnapshotCursorLease,
    TargetPresence,
    TranslationState,
)
from parser_json_support import (
    JsonBomPolicy,
    JsonPreflightError,
    JsonPreflightLimits,
    load_bounded_json,
)


_MIB = 1024 * 1024
_INPUT_BYTES = 100 * _MIB
_FIELD_CHARS = 100 * _MIB
_MATERIALIZED_RECORDS = 100_000
_RETAINED_ISSUES = 256
_METADATA_ENTRIES = 256
_METADATA_CONTAINER_CHARS = 1 * _MIB
_METADATA_TOTAL_CHARS = 16 * _MIB
_TEXT_READ_CHUNK_BYTES = 64 * 1024
_SCHEMA_VERSION = 1
_SINGLE_CHAR_LINE_ENDINGS = frozenset(
    ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
)

_INVALID_FIELD = "PARSER.SYNTAX.INVALID_FIELD"
_EMPTY_INPUT = "PARSER.SYNTAX.EMPTY_INPUT"
_ENCODING_FAILED = "PARSER.SOURCE.ENCODING_FAILED"
_DEPTH_LIMIT = "PARSER.LIMIT.DEPTH"
_WRITE_UNSUPPORTED = "PARSER.CAPABILITY.WRITE_UNSUPPORTED"


def _issue_codes(*additional: str) -> tuple[str, ...]:
    return tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES + additional)))


LOCALCAT_JSON_LIMIT_PROFILE = LimitProfile(
    profile_id="localcat-json-v1",
    profile_version=1,
    max_input_bytes=_INPUT_BYTES,
    max_decoded_field_chars=_FIELD_CHARS,
    max_records=100_000,
    max_materialized_records=_MATERIALIZED_RECORDS,
    max_retained_issues=_RETAINED_ISSUES,
    declared_issue_codes=_issue_codes(
        _DEPTH_LIMIT,
        _EMPTY_INPUT,
        _ENCODING_FAILED,
        _INVALID_FIELD,
    ),
    max_metadata_entries_per_container=_METADATA_ENTRIES,
    max_metadata_decoded_chars_per_container=_METADATA_CONTAINER_CHARS,
    max_metadata_decoded_chars_total=_METADATA_TOTAL_CHARS,
    max_structure_depth=64,
)

LINE_TEXT_LIMIT_PROFILE = LimitProfile(
    profile_id="line-text-v1",
    profile_version=1,
    max_input_bytes=_INPUT_BYTES,
    max_decoded_field_chars=_FIELD_CHARS,
    max_records=1_000_000,
    max_materialized_records=_MATERIALIZED_RECORDS,
    max_retained_issues=_RETAINED_ISSUES,
    declared_issue_codes=_issue_codes(
        _EMPTY_INPUT,
        _ENCODING_FAILED,
    ),
    max_metadata_entries_per_container=_METADATA_ENTRIES,
    max_metadata_decoded_chars_per_container=_METADATA_CONTAINER_CHARS,
    max_metadata_decoded_chars_total=_METADATA_TOTAL_CHARS,
    max_structure_depth=8,
)


def _fatal(
    code: str,
    safe_summary: str,
    *,
    byte_offset: int | None = None,
    line_number: int | None = None,
    record_number: int | None = None,
) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity=IssueSeverity.FATAL,
        safe_summary=safe_summary,
        byte_offset=byte_offset,
        line_number=line_number,
        record_number=record_number,
    )


def _document_stem(source: SnapshotCursorLease) -> str:
    hint = source.source_name_hint
    if type(hint) is not str or not hint:
        raise ContractViolation(
            "PARSER.SOURCE.READ_FAILED",
            "sealed source does not publish a usable final-component name",
        )
    # Foundation derives this final component from rooted authority.  Replacing
    # a backslash is only a portable basename guard; it does not reopen a path.
    stem = PurePath(hint.replace("\\", "/")).stem
    if not stem.strip():
        raise ContractViolation(
            "PARSER.SOURCE.READ_FAILED",
            "sealed source final-component name has no usable stem",
        )
    return stem


def _validate_request(request: ReadRequest, expected_format) -> None:
    if type(request) is not ReadRequest:
        raise TypeError("request must be an exact ReadRequest")
    if (
        request.purpose is not EffectivePurpose.PROJECT_DOCUMENT
        or request.format_id != expected_format
    ):
        raise ContractViolation(
            "PARSER.SELECTION.UNSUPPORTED",
            "read request does not match the selected LocalCAT project codec",
        )


def _clean_optional_string(
    mapping: dict[str, object],
    field_name: str,
    *,
    record_number: int | None = None,
) -> tuple[str | None, ParseIssue | None]:
    if field_name not in mapping or mapping[field_name] is None:
        return None, None
    value = mapping[field_name]
    if type(value) is not str:
        return None, _fatal(
            _INVALID_FIELD,
            f"record {record_number} has an invalid {field_name} field type"
            if record_number is not None
            else f"document has an invalid {field_name} field type",
            record_number=record_number,
        )
    return value.strip(), None


def _json_segment(
    entry: object,
    ordinal: int,
) -> tuple[ParsedSegment | None, ParseIssue | None]:
    if type(entry) is not dict:
        return None, _fatal(
            _INVALID_FIELD,
            f"record {ordinal} must be a JSON object",
            record_number=ordinal,
        )
    mapping: dict[str, object] = entry

    local_id, issue = _clean_optional_string(
        mapping,
        "id",
        record_number=ordinal,
    )
    if issue is not None:
        return None, issue
    local_id = local_id or f"segment-{ordinal}"

    source, issue = _clean_optional_string(
        mapping,
        "source",
        record_number=ordinal,
    )
    if issue is not None:
        return None, issue
    if not source:
        return None, _fatal(
            _INVALID_FIELD,
            f"record {ordinal} requires a non-empty source string",
            record_number=ordinal,
        )

    target, issue = _clean_optional_string(
        mapping,
        "target",
        record_number=ordinal,
    )
    if issue is not None:
        return None, issue
    if target is None:
        target_presence = TargetPresence.MISSING
    elif target == "":
        target_presence = TargetPresence.EXPLICIT_EMPTY
    else:
        target_presence = TargetPresence.PRESENT

    speaker, issue = _clean_optional_string(
        mapping,
        "speaker",
        record_number=ordinal,
    )
    if issue is not None:
        return None, issue

    confirmed = mapping.get("confirmed", False)
    if type(confirmed) is not bool:
        return None, _fatal(
            _INVALID_FIELD,
            f"record {ordinal} has an invalid confirmed field type",
            record_number=ordinal,
        )

    return (
        ParsedSegment(
            local_id=local_id,
            source=source,
            target=target,
            target_presence=target_presence,
            translation_state=(
                TranslationState.CONFIRMED
                if confirmed
                else TranslationState.UNCONFIRMED
            ),
            speaker=RawSpeaker(speaker or ""),
            format_metadata=(),
        ),
        None,
    )


class LocalCatJsonReader:
    """Non-streaming LocalCAT JSON reader over one sealed byte snapshot."""

    __slots__ = ("descriptor",)

    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        selected = descriptor if descriptor is not None else LOCALCAT_JSON_DESCRIPTOR
        if type(selected) is not CodecDescriptor or selected.format_id != LOCALCAT_JSON_V1:
            raise TypeError("descriptor must be a LocalCAT JSON CodecDescriptor")
        self.descriptor = selected

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        _validate_request(request, LOCALCAT_JSON_V1)
        profile = self.descriptor.limit_profile
        data = source.read()
        try:
            preflight = load_bounded_json(
                data,
                JsonPreflightLimits(
                    max_input_bytes=profile.max_input_bytes,
                    max_string_chars=profile.max_decoded_field_chars,
                    max_structure_depth=profile.max_structure_depth,
                ),
                bom_policy=JsonBomPolicy.ALLOW,
            )
        except JsonPreflightError as exc:
            yield _fatal(
                exc.code,
                exc.safe_summary,
                byte_offset=exc.byte_offset,
            )
            return

        payload = preflight.value
        try:
            fallback_name = _document_stem(source)
        except ContractViolation as exc:
            yield _fatal(exc.code, exc.safe_summary)
            return

        if type(payload) is list:
            raw_segments = payload
            name = fallback_name
            source_locale = "en-US"
            target_locale = "zh-CN"
        elif type(payload) is dict:
            raw_segments = payload.get("segments")
            if type(raw_segments) is not list:
                yield _fatal(
                    _INVALID_FIELD,
                    "LocalCAT JSON object root requires a segments array",
                )
                return
            name, issue = _clean_optional_string(payload, "name")
            if issue is not None:
                yield issue
                return
            source_locale, issue = _clean_optional_string(payload, "source_locale")
            if issue is not None:
                yield issue
                return
            target_locale, issue = _clean_optional_string(payload, "target_locale")
            if issue is not None:
                yield issue
                return
            name = name or fallback_name
            source_locale = source_locale or "en-US"
            target_locale = target_locale or "zh-CN"
        else:
            yield _fatal(
                _INVALID_FIELD,
                "LocalCAT JSON root must be an array or object",
            )
            return

        if not raw_segments:
            yield _fatal(
                _EMPTY_INPUT,
                "LocalCAT JSON contains no translatable segments",
            )
            return
        if len(raw_segments) > profile.max_records:
            yield _fatal(
                "PARSER.LIMIT.RECORD",
                "LocalCAT JSON record count exceeds the active limit profile",
            )
            return

        segments: list[ParsedSegment] = []
        local_ids: set[str] = set()
        for ordinal, entry in enumerate(raw_segments, start=1):
            segment, issue = _json_segment(entry, ordinal)
            if issue is not None:
                yield issue
                return
            assert segment is not None
            if segment.local_id in local_ids:
                yield _fatal(
                    "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
                    f"record {ordinal} duplicates a local segment identity",
                    record_number=ordinal,
                )
                return
            local_ids.add(segment.local_id)
            segments.append(segment)

        yield DocumentHeader(
            name=name,
            source_locale=source_locale,
            target_locale=target_locale,
            metadata=(),
        )
        yield from segments


class _Utf8SigLines(Iterator[tuple[int, str]]):
    """Incremental strict UTF-8[-BOM] physical-line iterator."""

    __slots__ = ("_source", "_decoder", "_buffer", "_eof", "_line_number")

    def __init__(self, source: SnapshotCursorLease) -> None:
        self._source = source
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._buffer = ""
        self._eof = False
        self._line_number = 0

    def __iter__(self) -> _Utf8SigLines:
        return self

    def __next__(self) -> tuple[int, str]:
        while True:
            end = self._line_end()
            if end is not None:
                raw_line = self._buffer[:end]
                self._buffer = self._buffer[end:]
                self._line_number += 1
                return self._line_number, _remove_line_ending(raw_line)
            if self._eof:
                if not self._buffer:
                    raise StopIteration
                raw_line = self._buffer
                self._buffer = ""
                self._line_number += 1
                return self._line_number, raw_line
            payload = self._source.read(_TEXT_READ_CHUNK_BYTES)
            if payload:
                self._buffer += self._decoder.decode(payload, final=False)
                continue
            self._buffer += self._decoder.decode(b"", final=True)
            self._eof = True

    def _line_end(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character not in _SINGLE_CHAR_LINE_ENDINGS:
                continue
            if character != "\r":
                return index + 1
            if index + 1 < len(self._buffer):
                return index + 2 if self._buffer[index + 1] == "\n" else index + 1
            if self._eof:
                return index + 1
            return None
        return None


def _remove_line_ending(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value and value[-1] in _SINGLE_CHAR_LINE_ENDINGS:
        return value[:-1]
    return value


class LineTextReader:
    """Streaming source-only line-text reader with no writer capability."""

    __slots__ = ("descriptor",)

    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        selected = descriptor if descriptor is not None else LINE_TEXT_DESCRIPTOR
        if type(selected) is not CodecDescriptor or selected.format_id != LINE_TEXT_V1:
            raise TypeError("descriptor must be a line-text CodecDescriptor")
        self.descriptor = selected

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        _validate_request(request, LINE_TEXT_V1)
        try:
            name = _document_stem(source)
        except ContractViolation as exc:
            yield _fatal(exc.code, exc.safe_summary)
            return
        yield DocumentHeader(
            name=name,
            source_locale=None,
            target_locale=None,
            metadata=(),
        )

        accepted = 0
        try:
            for line_number, line in _Utf8SigLines(source):
                source_text = line.strip()
                if not source_text:
                    continue
                if len(source_text) > self.descriptor.limit_profile.max_decoded_field_chars:
                    yield _fatal(
                        "PARSER.LIMIT.FIELD",
                        f"line {line_number} exceeds the active decoded-field limit",
                        line_number=line_number,
                    )
                    return
                if accepted >= self.descriptor.limit_profile.max_records:
                    yield _fatal(
                        "PARSER.LIMIT.RECORD",
                        "line-text record count exceeds the active limit profile",
                        line_number=line_number,
                    )
                    return
                accepted += 1
                yield ParsedSegment(
                    local_id=f"segment-{accepted}",
                    source=source_text,
                    target=None,
                    target_presence=TargetPresence.MISSING,
                    translation_state=None,
                    speaker=RawSpeaker(""),
                    format_metadata=(),
                )
        except UnicodeDecodeError:
            yield _fatal(
                _ENCODING_FAILED,
                "line-text input is not valid UTF-8",
            )
            return

        if accepted == 0:
            yield _fatal(
                _EMPTY_INPUT,
                "line-text input contains no non-empty source lines",
            )


class LocalCatJsonCanonicalSerializer:
    """Deterministic schema-v1 canonical transformation, not source round-trip."""

    __slots__ = ("descriptor",)

    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        selected = descriptor if descriptor is not None else LOCALCAT_JSON_DESCRIPTOR
        if type(selected) is not CodecDescriptor or selected.format_id != LOCALCAT_JSON_V1:
            raise TypeError("descriptor must be a LocalCAT JSON CodecDescriptor")
        self.descriptor = selected

    def serialize_canonical(self, request: CanonicalSerializeRequest) -> CanonicalBytes:
        if type(request) is not CanonicalSerializeRequest:
            raise TypeError("request must be an exact CanonicalSerializeRequest")
        if request.format_id != LOCALCAT_JSON_V1:
            raise ContractViolation(
                _WRITE_UNSUPPORTED,
                "LocalCAT JSON serializer cannot write the requested format",
            )
        document = request.document
        profile = self.descriptor.limit_profile
        if len(document.segments) > profile.max_records:
            raise ContractViolation(
                "PARSER.LIMIT.RECORD",
                "canonical document record count exceeds the active limit profile",
            )
        fields = (document.name, document.source_locale, document.target_locale)
        segment_fields = tuple(
            value
            for segment in document.segments
            for value in (
                segment.local_id,
                segment.source,
                segment.target,
                segment.speaker.value,
            )
        )
        if any(len(value) > profile.max_decoded_field_chars for value in fields + segment_fields):
            raise ContractViolation(
                "PARSER.LIMIT.FIELD",
                "canonical document field exceeds the active limit profile",
            )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "name": document.name,
            "source_locale": document.source_locale,
            "target_locale": document.target_locale,
            "segments": [
                {
                    "id": segment.local_id,
                    "source": segment.source,
                    "target": segment.target,
                    "speaker": segment.speaker.value,
                    "confirmed": segment.confirmed,
                }
                for segment in document.segments
            ],
        }
        try:
            rendered = (
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ContractViolation(
                _INVALID_FIELD,
                "canonical document contains text that cannot be encoded as UTF-8",
            ) from None
        if len(rendered) > profile.max_input_bytes:
            raise ContractViolation(
                "PARSER.LIMIT.INPUT",
                "canonical JSON bytes exceed the active input-size profile",
            )
        return CanonicalBytes(
            codec_identity=self.descriptor.identity,
            format_id=LOCALCAT_JSON_V1,
            schema_version=_SCHEMA_VERSION,
            payload=rendered,
        )


LOCALCAT_JSON_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "localcat-json", "1"),
    purpose=EffectivePurpose.PROJECT_DOCUMENT,
    format_id=LOCALCAT_JSON_V1,
    extensions=(".json",),
    mime_types=("application/json",),
    sniff_prefixes=(b"[", b"{"),
    capabilities=CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=True,
        source_round_trip_write=False,
        streaming_input=False,
        iterator_view=True,
        materialized_view=True,
        format_profile=LOCALCAT_JSON_LIMIT_PROFILE.profile_id,
    ),
    limit_profile=LOCALCAT_JSON_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=LocalCatJsonReader,
    canonical_serializer_factory=LocalCatJsonCanonicalSerializer,
)

LINE_TEXT_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "line-text", "1"),
    purpose=EffectivePurpose.PROJECT_DOCUMENT,
    format_id=LINE_TEXT_V1,
    extensions=(".txt",),
    mime_types=("text/plain",),
    sniff_prefixes=(),
    capabilities=CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=False,
        source_round_trip_write=False,
        streaming_input=True,
        iterator_view=True,
        materialized_view=True,
        format_profile=LINE_TEXT_LIMIT_PROFILE.profile_id,
    ),
    limit_profile=LINE_TEXT_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=LineTextReader,
    canonical_serializer_factory=None,
)


def localcat_descriptors() -> tuple[CodecDescriptor, CodecDescriptor]:
    """Return immutable descriptors for explicit built-in composition."""

    return LOCALCAT_JSON_DESCRIPTOR, LINE_TEXT_DESCRIPTOR


__all__ = (
    "LINE_TEXT_DESCRIPTOR",
    "LINE_TEXT_LIMIT_PROFILE",
    "LOCALCAT_JSON_DESCRIPTOR",
    "LOCALCAT_JSON_LIMIT_PROFILE",
    "LineTextReader",
    "LocalCatJsonCanonicalSerializer",
    "LocalCatJsonReader",
    "localcat_descriptors",
)
