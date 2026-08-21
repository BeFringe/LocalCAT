"""Single-input normalized TM JSON grammar for neutral Parser records.

The codec preserves physical array order and duplicate sources.  It neither
applies cross-file last-write-wins policy nor writes JSONL or commits resources;
those remain Application/facade responsibilities.
"""

from __future__ import annotations

from typing import Iterator

from parser_contracts import (
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    ContractViolation,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    InputConsumptionPolicy,
    IssueSeverity,
    LimitProfile,
    NORMALIZED_TM_JSON_V1,
    ParseIssue,
    RawParseEvent,
    RawSpeaker,
    ReadRequest,
    ResourceRecord,
    SnapshotCursorLease,
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
_RECORDS = 100_000
_RETAINED_ISSUES = 256
_METADATA_ENTRIES = 256
_METADATA_CONTAINER_CHARS = 1 * _MIB
_METADATA_TOTAL_CHARS = 16 * _MIB

_INVALID_FIELD = "PARSER.SYNTAX.INVALID_FIELD"
_EMPTY_INPUT = "PARSER.SYNTAX.EMPTY_INPUT"
_ENCODING_FAILED = "PARSER.SOURCE.ENCODING_FAILED"
_DEPTH_LIMIT = "PARSER.LIMIT.DEPTH"


def _issue_codes(*additional: str) -> tuple[str, ...]:
    return tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES + additional)))


NORMALIZED_TM_JSON_LIMIT_PROFILE = LimitProfile(
    profile_id="normalized-tm-json-v1",
    profile_version=1,
    max_input_bytes=_INPUT_BYTES,
    max_decoded_field_chars=_FIELD_CHARS,
    max_records=_RECORDS,
    max_materialized_records=_RECORDS,
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


def _issue(
    severity: IssueSeverity,
    code: str,
    safe_summary: str,
    *,
    byte_offset: int | None = None,
    record_number: int | None = None,
) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity=severity,
        safe_summary=safe_summary,
        byte_offset=byte_offset,
        record_number=record_number,
    )


def _fatal(
    code: str,
    safe_summary: str,
    *,
    byte_offset: int | None = None,
    record_number: int | None = None,
) -> ParseIssue:
    return _issue(
        IssueSeverity.FATAL,
        code,
        safe_summary,
        byte_offset=byte_offset,
        record_number=record_number,
    )


def _warning(safe_summary: str, *, record_number: int) -> ParseIssue:
    return _issue(
        IssueSeverity.WARNING,
        _INVALID_FIELD,
        safe_summary,
        record_number=record_number,
    )


def _validate_request(request: ReadRequest) -> None:
    if type(request) is not ReadRequest:
        raise TypeError("request must be an exact ReadRequest")
    if (
        request.purpose is not EffectivePurpose.TRANSLATION_MEMORY
        or request.format_id != NORMALIZED_TM_JSON_V1
    ):
        raise ContractViolation(
            "PARSER.SELECTION.UNSUPPORTED",
            "read request does not match the normalized TM JSON codec",
        )


def _resource_record(
    entry: object,
    ordinal: int,
) -> tuple[ResourceRecord | None, ParseIssue | None]:
    if type(entry) is not dict:
        return None, _warning(
            f"record {ordinal} must be a JSON object",
            record_number=ordinal,
        )
    mapping: dict[str, object] = entry

    source = mapping.get("source")
    if type(source) is not str or not source.strip():
        return None, _warning(
            f"record {ordinal} requires a non-empty source string",
            record_number=ordinal,
        )
    target = mapping.get("target")
    if type(target) is not str or not target.strip():
        return None, _warning(
            f"record {ordinal} requires a non-empty target string",
            record_number=ordinal,
        )

    speaker = mapping.get("speaker")
    if speaker is None:
        raw_speaker = ""
    elif type(speaker) is str:
        raw_speaker = speaker.strip()
    else:
        return None, _warning(
            f"record {ordinal} has a non-string speaker field",
            record_number=ordinal,
        )

    return (
        ResourceRecord(
            local_id=f"record-{ordinal}",
            source=source.strip(),
            target=target.strip(),
            speaker=RawSpeaker(raw_speaker),
            format_metadata=(),
        ),
        None,
    )


class NormalizedTmJsonReader:
    """Reader-only, non-streaming normalized TM JSON codec."""

    __slots__ = ("descriptor",)

    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        selected = (
            descriptor
            if descriptor is not None
            else NORMALIZED_TM_JSON_DESCRIPTOR
        )
        if (
            type(selected) is not CodecDescriptor
            or selected.format_id != NORMALIZED_TM_JSON_V1
        ):
            raise TypeError("descriptor must be a normalized TM JSON CodecDescriptor")
        self.descriptor = selected

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        _validate_request(request)
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
                bom_policy=JsonBomPolicy.REJECT,
            )
        except JsonPreflightError as exc:
            yield _fatal(
                exc.code,
                exc.safe_summary,
                byte_offset=exc.byte_offset,
            )
            return

        payload = preflight.value
        if type(payload) is not list:
            yield _fatal(
                _INVALID_FIELD,
                "normalized TM JSON root must be an array",
            )
            return
        if not payload:
            yield _fatal(
                _EMPTY_INPUT,
                "normalized TM JSON contains no records",
            )
            return
        if len(payload) > profile.max_records:
            yield _fatal(
                "PARSER.LIMIT.RECORD",
                "normalized TM JSON record count exceeds the active limit profile",
            )
            return

        accepted = 0
        for ordinal, entry in enumerate(payload, start=1):
            record, issue = _resource_record(entry, ordinal)
            if issue is not None:
                yield issue
                continue
            assert record is not None
            accepted += 1
            yield record

        if accepted == 0:
            yield _fatal(
                _EMPTY_INPUT,
                "normalized TM JSON contains no valid records",
            )


NORMALIZED_TM_JSON_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "normalized-tm-json", "1"),
    purpose=EffectivePurpose.TRANSLATION_MEMORY,
    format_id=NORMALIZED_TM_JSON_V1,
    extensions=(".json",),
    mime_types=("application/json",),
    sniff_prefixes=(b"[",),
    capabilities=CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=False,
        source_round_trip_write=False,
        streaming_input=False,
        iterator_view=True,
        materialized_view=True,
        format_profile=NORMALIZED_TM_JSON_LIMIT_PROFILE.profile_id,
    ),
    limit_profile=NORMALIZED_TM_JSON_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=NormalizedTmJsonReader,
    canonical_serializer_factory=None,
)


def normalized_tm_json_descriptors() -> tuple[CodecDescriptor]:
    """Return the immutable descriptor for explicit built-in composition."""

    return (NORMALIZED_TM_JSON_DESCRIPTOR,)


__all__ = (
    "NORMALIZED_TM_JSON_DESCRIPTOR",
    "NORMALIZED_TM_JSON_LIMIT_PROFILE",
    "NormalizedTmJsonReader",
    "normalized_tm_json_descriptors",
)
