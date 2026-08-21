"""Reader-only CSV/XLSX termbase codecs with one explicit row-selection grammar.

The codecs expose neutral ``ResourceRecord`` events only.  They neither choose
columns implicitly nor perform deduplication, persistence, transaction, or
project aggregation.  XLSX container/XML safety remains owned by
``parser_xlsx_support`` and is invoked through the Foundation seekable-input
preflight port before openpyxl sees the workbook.
"""

from __future__ import annotations

import codecs
import csv
import importlib
import threading
from typing import Iterable, Iterator, Sequence

from parser_contracts import (
    MAX_TERMBASE_PREVIEW_COLUMNS,
    MAX_TERMBASE_PREVIEW_LABEL_CHARS,
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    ColumnSelectorKind,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    InputConsumptionPolicy,
    IssueSeverity,
    LimitProfile,
    ParseIssue,
    RawSpeaker,
    ReadRequest,
    ResourceRecord,
    SnapshotCursorLease,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
    TermbaseColumnSelection,
    TermbaseColumnPreview,
    TermbaseColumnPreviewRequest,
    TermbaseHeaderPolicy,
    TermbasePreviewColumn,
)
from parser_source import ParserSourceError
from parser_xlsx_support import (
    ARCHIVE_INVALID,
    COMPRESSION_RATIO_LIMIT,
    DATA_DESCRIPTOR_UNSUPPORTED,
    ENCODING_FAILED,
    EXPANSION_LIMIT,
    MALFORMED_XML,
    MEMBER_DUPLICATE,
    MEMBER_LIMIT,
    MEMBER_NAME_UNSAFE,
    SOURCE_NOT_SEEKABLE,
    SOURCE_RESTORE_FAILED,
    STRUCTURE_DEPTH_LIMIT,
    XML_DECLARATION_FORBIDDEN,
    XlsxPreflightError,
    XlsxPreflightLimits,
    preflight_xlsx,
)


_MIB = 1024 * 1024
_INPUT_BYTES = 100 * _MIB
_MATERIALIZED_RECORDS = 100_000
_METADATA_ENTRIES = 256
_METADATA_CONTAINER_CHARS = 1 * _MIB
_METADATA_TOTAL_CHARS = 16 * _MIB
_RETAINED_ISSUES = 256
_CSV_READ_CHUNK_BYTES = 64 * 1024
_CSV_FIELD_SIZE_LOCK = threading.RLock()

_LEGACY_SOURCE_HEADERS = frozenset(
    {"source", "source term", "source text", "原文", "源术语"}
)
_LEGACY_TARGET_HEADERS = frozenset(
    {"target", "target term", "target text", "translation", "译文", "目标术语"}
)

_HEADER_MISSING = "PARSER.TERMBASE.HEADER_MISSING"
_HEADER_DUPLICATE = "PARSER.TERMBASE.HEADER_DUPLICATE"
_HEADER_SKIPPED = "PARSER.TERMBASE.HEADER_SKIPPED"
_COLUMN_SAME = "PARSER.TERMBASE.COLUMN_SELECTION_SAME"
_ROW_EMPTY = "PARSER.TERMBASE.ROW_EMPTY"
_ROW_MISSING_COLUMN = "PARSER.TERMBASE.ROW_MISSING_COLUMN"
_SOURCE_EMPTY = "PARSER.TERMBASE.SOURCE_EMPTY"
_TARGET_EMPTY = "PARSER.TERMBASE.TARGET_EMPTY"
_ACTIVE_SHEET_MISSING = "PARSER.TERMBASE.ACTIVE_SHEET_MISSING"
_DEPENDENCY_MISSING = "PARSER.CAPABILITY.CONDITIONAL_DEPENDENCY_MISSING"
_DEPENDENCY_INCOMPATIBLE = "PARSER.CAPABILITY.CONDITIONAL_DEPENDENCY_INCOMPATIBLE"
_PREVIEW_EMPTY = "PARSER.TERMBASE.PREVIEW_EMPTY"

_XLSX_PREFLIGHT_CODES = (
    ARCHIVE_INVALID,
    COMPRESSION_RATIO_LIMIT,
    DATA_DESCRIPTOR_UNSUPPORTED,
    ENCODING_FAILED,
    EXPANSION_LIMIT,
    MALFORMED_XML,
    MEMBER_DUPLICATE,
    MEMBER_LIMIT,
    MEMBER_NAME_UNSAFE,
    SOURCE_NOT_SEEKABLE,
    SOURCE_RESTORE_FAILED,
    STRUCTURE_DEPTH_LIMIT,
    XML_DECLARATION_FORBIDDEN,
)

_TERMBASE_CODES = (
    _ACTIVE_SHEET_MISSING,
    _COLUMN_SAME,
    _DEPENDENCY_INCOMPATIBLE,
    _DEPENDENCY_MISSING,
    _HEADER_DUPLICATE,
    _HEADER_MISSING,
    _HEADER_SKIPPED,
    _ROW_EMPTY,
    _ROW_MISSING_COLUMN,
    _SOURCE_EMPTY,
    _TARGET_EMPTY,
    "PARSER.SOURCE.ENCODING_FAILED",
    "PARSER.SYNTAX.MALFORMED",
)


def _issue_codes(*additional: str) -> tuple[str, ...]:
    return tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES + _TERMBASE_CODES + additional)))


CSV_LIMIT_PROFILE = LimitProfile(
    profile_id="termbase-csv-v1",
    profile_version=1,
    max_input_bytes=_INPUT_BYTES,
    max_decoded_field_chars=100 * _MIB,
    max_records=1_000_000,
    max_materialized_records=_MATERIALIZED_RECORDS,
    max_retained_issues=_RETAINED_ISSUES,
    declared_issue_codes=_issue_codes(),
    max_metadata_entries_per_container=_METADATA_ENTRIES,
    max_metadata_decoded_chars_per_container=_METADATA_CONTAINER_CHARS,
    max_metadata_decoded_chars_total=_METADATA_TOTAL_CHARS,
    max_structure_depth=8,
)

XLSX_LIMIT_PROFILE = LimitProfile(
    profile_id="termbase-xlsx-v1",
    profile_version=1,
    max_input_bytes=_INPUT_BYTES,
    max_decoded_field_chars=1_000_000,
    max_records=1_048_576,
    max_materialized_records=_MATERIALIZED_RECORDS,
    max_retained_issues=_RETAINED_ISSUES,
    declared_issue_codes=_issue_codes(*_XLSX_PREFLIGHT_CODES),
    max_metadata_entries_per_container=_METADATA_ENTRIES,
    max_metadata_decoded_chars_per_container=_METADATA_CONTAINER_CHARS,
    max_metadata_decoded_chars_total=_METADATA_TOTAL_CHARS,
    max_structure_depth=64,
    max_expanded_bytes=256 * _MIB,
    max_archive_members=4_096,
    max_compression_ratio=100.0,
)


class _SelectionFailure(ValueError):
    __slots__ = ("code", "safe_summary")

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = code
        self.safe_summary = safe_summary
        super().__init__(f"{code}: {safe_summary}")


class _Utf8CsvLines(Iterator[str]):
    """Incrementally decode one sequential snapshot as strict UTF-8[-BOM] lines."""

    __slots__ = ("_source", "_decoder", "_buffer", "_eof")

    def __init__(self, source: SnapshotCursorLease) -> None:
        self._source = source
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._buffer = ""
        self._eof = False

    def __iter__(self) -> _Utf8CsvLines:
        return self

    def __next__(self) -> str:
        while True:
            end = self._line_end()
            if end is not None:
                line = self._buffer[:end]
                self._buffer = self._buffer[end:]
                return line
            if self._eof:
                if not self._buffer:
                    raise StopIteration
                line = self._buffer
                self._buffer = ""
                return line
            payload = self._source.read(_CSV_READ_CHUNK_BYTES)
            if payload:
                self._buffer += self._decoder.decode(payload, final=False)
                continue
            self._buffer += self._decoder.decode(b"", final=True)
            self._eof = True

    def _line_end(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character == "\n":
                return index + 1
            if character != "\r":
                continue
            if index + 1 < len(self._buffer):
                return index + 2 if self._buffer[index + 1] == "\n" else index + 1
            if self._eof:
                return index + 1
            return None
        return None


class _DescriptorBoundCsvRows(Iterator[tuple[str, ...]]):
    """Run each stdlib CSV grammar step under one restored global field limit.

    ``csv.field_size_limit`` is process-global.  The lock prevents LocalCAT CSV
    readers with different descriptor profiles from observing each other's
    temporary value.  Limiting the scope to one ``reader.__next__`` also means
    the global is restored before a raw event is yielded to a possibly paused
    consumer.  This intentionally serializes CSV record parsing, not downstream
    event consumption.
    """

    __slots__ = ("_reader", "_field_size_limit")

    def __init__(
        self,
        lines: Iterable[str],
        *,
        field_size_limit: int,
    ) -> None:
        if type(field_size_limit) is not int or field_size_limit <= 0:
            raise ValueError("CSV field size limit must be a positive exact integer")
        self._reader = csv.reader(lines, strict=True)
        self._field_size_limit = field_size_limit

    def __iter__(self) -> _DescriptorBoundCsvRows:
        return self

    def __next__(self) -> tuple[str, ...]:
        with _CSV_FIELD_SIZE_LOCK:
            previous = csv.field_size_limit()
            try:
                csv.field_size_limit(self._field_size_limit)
                try:
                    return tuple(next(self._reader))
                except csv.Error as exc:
                    if str(exc).startswith("field larger than field limit"):
                        raise _CsvFieldLimitExceeded from None
                    raise
            finally:
                csv.field_size_limit(previous)


class _CsvFieldLimitExceeded(csv.Error):
    """Body-free marker for stdlib's descriptor-bound field-size rejection."""


def _validate_request(request: ReadRequest, expected_format) -> TermbaseColumnSelection:
    if type(request) is not ReadRequest:
        raise TypeError("request must be an exact ReadRequest")
    if request.purpose is not EffectivePurpose.TERMBASE or request.format_id != expected_format:
        raise _SelectionFailure(
            "PARSER.SELECTION.UNSUPPORTED",
            "read request does not match the selected termbase codec",
        )
    options = request.termbase_options
    if options is None:
        raise _SelectionFailure(
            "PARSER.TERMBASE.COLUMN_SELECTION_REQUIRED",
            "termbase reads require an explicit column selection",
        )
    return options.columns


def _validate_preview_request(
    request: TermbaseColumnPreviewRequest,
    expected_format,
) -> None:
    if type(request) is not TermbaseColumnPreviewRequest:
        raise TypeError("request must be an exact TermbaseColumnPreviewRequest")
    if request.purpose is not EffectivePurpose.TERMBASE or request.format_id != expected_format:
        raise ParserSourceError(
            "PARSER.SELECTION.UNSUPPORTED",
            "preview request does not match the selected termbase codec",
        )


def _header_text(value: object) -> str | None:
    if type(value) is not str:
        return None
    trimmed = value.strip()
    return trimmed or None


def _preview_column(index: int, value: object) -> TermbasePreviewColumn:
    header = _header_text(value)
    if header is None:
        return TermbasePreviewColumn(index, None)
    retained = header[:MAX_TERMBASE_PREVIEW_LABEL_CHARS]
    return TermbasePreviewColumn(
        zero_based_index=index,
        header_candidate=retained,
        header_original_char_count=len(header),
        header_truncated=len(retained) != len(header),
    )


def _preview_from_row(
    row: Sequence[object],
    *,
    source: SnapshotCursorLease,
    descriptor: CodecDescriptor,
    active_sheet_name: str | None,
) -> TermbaseColumnPreview:
    if not row:
        raise ParserSourceError(
            _PREVIEW_EMPTY,
            "the selected termbase has no previewable first record",
        )
    retained_count = min(len(row), MAX_TERMBASE_PREVIEW_COLUMNS)
    return TermbaseColumnPreview(
        source=source.source_identity,
        codec_identity=descriptor.identity,
        format_id=descriptor.format_id,
        columns=tuple(
            _preview_column(index, row[index])
            for index in range(retained_count)
        ),
        total_column_count=len(row),
        columns_truncated=len(row) > retained_count,
        legacy_header_detected=_legacy_header(row),
        active_sheet_name=active_sheet_name,
    )


def _resolve_columns(
    selection: TermbaseColumnSelection,
    header: Sequence[object],
) -> tuple[int, int]:
    resolved: list[int] = []
    for selector in (selection.source, selection.target):
        if selector.kind is ColumnSelectorKind.ZERO_BASED_INDEX:
            assert selector.zero_based_index is not None
            resolved.append(selector.zero_based_index)
            continue
        expected = selector.header_name
        assert expected is not None
        matches = tuple(
            index
            for index, value in enumerate(header)
            if _header_text(value) == expected
        )
        if not matches:
            raise _SelectionFailure(
                _HEADER_MISSING,
                "the selected header name is missing from the first physical row",
            )
        if len(matches) != 1:
            raise _SelectionFailure(
                _HEADER_DUPLICATE,
                "the selected header name occurs more than once in the first physical row",
            )
        resolved.append(matches[0])
    source_index, target_index = resolved
    if source_index == target_index:
        raise _SelectionFailure(
            _COLUMN_SAME,
            "source and target selections resolve to the same physical column",
        )
    return source_index, target_index


def _legacy_header(row: Sequence[object]) -> bool:
    if len(row) < 2:
        return False
    source = _header_text(row[0])
    target = _header_text(row[1])
    return bool(
        source is not None
        and target is not None
        and source.casefold() in _LEGACY_SOURCE_HEADERS
        and target.casefold() in _LEGACY_TARGET_HEADERS
    )


def _fatal(failure: _SelectionFailure) -> ParseIssue:
    return ParseIssue(
        code=failure.code,
        severity=IssueSeverity.FATAL,
        safe_summary=failure.safe_summary,
    )


def _warning(code: str, summary: str, ordinal: int) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity=IssueSeverity.WARNING,
        safe_summary=summary,
        record_number=ordinal,
    )


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _row_events(
    row: Sequence[object],
    *,
    ordinal: int,
    source_index: int,
    target_index: int,
    present_columns: frozenset[int] | None = None,
) -> Iterator[ResourceRecord | ParseIssue]:
    if not row or all(not _cell_text(value) for value in row):
        yield _warning(_ROW_EMPTY, "an empty physical row was skipped", ordinal)
        return
    if present_columns is None:
        missing = source_index >= len(row) or target_index >= len(row)
    else:
        missing = source_index not in present_columns or target_index not in present_columns
    if missing:
        yield _warning(
            _ROW_MISSING_COLUMN,
            "a physical row does not contain both selected columns",
            ordinal,
        )
        return
    source = _cell_text(row[source_index])
    target = _cell_text(row[target_index])
    if not source:
        yield _warning(_SOURCE_EMPTY, "a physical row has an empty selected source", ordinal)
        return
    if not target:
        yield _warning(_TARGET_EMPTY, "a physical row has an empty selected target", ordinal)
        return
    yield ResourceRecord(
        local_id=f"row-{ordinal}",
        source=source,
        target=target,
        speaker=RawSpeaker(""),
        format_metadata=(),
    )


def _iter_selected_rows(
    rows: Iterable[tuple[Sequence[object], frozenset[int] | None]],
    selection: TermbaseColumnSelection,
) -> Iterator[ResourceRecord | ParseIssue]:
    iterator = iter(rows)
    try:
        first_row, first_present = next(iterator)
    except StopIteration:
        if selection.header_policy is TermbaseHeaderPolicy.FIRST_ROW:
            yield _fatal(
                _SelectionFailure(
                    _HEADER_MISSING,
                    "the selected header row is missing from the empty input",
                )
            )
        return

    if selection.header_policy is TermbaseHeaderPolicy.FIRST_ROW:
        try:
            source_index, target_index = _resolve_columns(selection, first_row)
        except _SelectionFailure as failure:
            yield _fatal(failure)
            return
        yield _warning(
            _HEADER_SKIPPED,
            "the first physical row was consumed as the selected header",
            1,
        )
        first_data = None
    else:
        source_index, target_index = _resolve_columns(selection, ())
        first_data = (first_row, first_present)
        if (
            selection.header_policy is TermbaseHeaderPolicy.LEGACY_ALLOWLIST
            and _legacy_header(first_row)
        ):
            yield _warning(
                _HEADER_SKIPPED,
                "the first physical row matched the legacy header allowlist and was skipped",
                1,
            )
            first_data = None

    if first_data is not None:
        yield from _row_events(
            first_data[0],
            ordinal=1,
            source_index=source_index,
            target_index=target_index,
            present_columns=first_data[1],
        )
    for ordinal, (row, present_columns) in enumerate(iterator, start=2):
        yield from _row_events(
            row,
            ordinal=ordinal,
            source_index=source_index,
            target_index=target_index,
            present_columns=present_columns,
        )


class CsvTermbaseCodec:
    __slots__ = ("descriptor",)

    def __init__(self, descriptor: CodecDescriptor | None = None) -> None:
        selected = descriptor if descriptor is not None else TERMBASE_CSV_DESCRIPTOR
        if type(selected) is not CodecDescriptor or selected.format_id != TERMBASE_CSV_V1:
            raise TypeError("descriptor must be a termbase CSV CodecDescriptor")
        self.descriptor = selected

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[ResourceRecord | ParseIssue]:
        try:
            selection = _validate_request(request, TERMBASE_CSV_V1)
        except _SelectionFailure as failure:
            yield _fatal(failure)
            return
        try:
            profile = self.descriptor.limit_profile
            reader = _DescriptorBoundCsvRows(
                _Utf8CsvLines(source),
                field_size_limit=profile.max_decoded_field_chars,
            )
            rows = ((row, None) for row in reader)
            yield from _iter_selected_rows(rows, selection)
        except UnicodeDecodeError:
            yield ParseIssue(
                code="PARSER.SOURCE.ENCODING_FAILED",
                severity=IssueSeverity.FATAL,
                safe_summary="CSV input is not strict UTF-8 or UTF-8-BOM",
            )
        except _CsvFieldLimitExceeded:
            yield ParseIssue(
                code="PARSER.LIMIT.FIELD",
                severity=IssueSeverity.FATAL,
                safe_summary="a CSV field exceeds the active decoded-field limit",
            )
        except csv.Error:
            yield ParseIssue(
                code="PARSER.SYNTAX.MALFORMED",
                severity=IssueSeverity.FATAL,
                safe_summary="CSV input does not satisfy the declared CSV grammar",
            )

    def preview_columns(
        self,
        source: SnapshotCursorLease,
        request: TermbaseColumnPreviewRequest,
    ) -> TermbaseColumnPreview:
        _validate_preview_request(request, TERMBASE_CSV_V1)
        try:
            reader = _DescriptorBoundCsvRows(
                _Utf8CsvLines(source),
                field_size_limit=self.descriptor.limit_profile.max_decoded_field_chars,
            )
            try:
                first_record = next(reader)
            except StopIteration:
                raise ParserSourceError(
                    _PREVIEW_EMPTY,
                    "the selected CSV has no previewable logical record",
                ) from None
            return _preview_from_row(
                first_record,
                source=source,
                descriptor=self.descriptor,
                active_sheet_name=None,
            )
        except ParserSourceError:
            raise
        except UnicodeDecodeError:
            raise ParserSourceError(
                "PARSER.SOURCE.ENCODING_FAILED",
                "CSV input is not strict UTF-8 or UTF-8-BOM",
            ) from None
        except _CsvFieldLimitExceeded:
            raise ParserSourceError(
                "PARSER.LIMIT.FIELD",
                "a CSV field exceeds the active decoded-field limit",
            ) from None
        except csv.Error:
            raise ParserSourceError(
                "PARSER.SYNTAX.MALFORMED",
                "CSV input does not satisfy the declared CSV grammar",
            ) from None


def _openpyxl_module():
    try:
        module = importlib.import_module("openpyxl")
    except ImportError:
        raise ParserSourceError(
            _DEPENDENCY_MISSING,
            "XLSX termbase reading requires openpyxl 3.1 or newer and below 4",
        ) from None
    version = getattr(module, "__version__", "")
    try:
        major, minor, *_rest = (int(part) for part in version.split("."))
    except (TypeError, ValueError):
        raise ParserSourceError(
            _DEPENDENCY_INCOMPATIBLE,
            "the available openpyxl version cannot satisfy the XLSX codec contract",
        ) from None
    if not ((major == 3 and minor >= 1)):
        raise ParserSourceError(
            _DEPENDENCY_INCOMPATIBLE,
            "the available openpyxl version is outside the supported XLSX codec range",
        )
    return module


def _xlsx_preflight_limits() -> XlsxPreflightLimits:
    profile = TERMBASE_XLSX_DESCRIPTOR.limit_profile
    assert profile.max_archive_members is not None
    assert profile.max_expanded_bytes is not None
    assert profile.max_compression_ratio is not None
    return XlsxPreflightLimits(
        max_archive_members=profile.max_archive_members,
        max_expanded_bytes=profile.max_expanded_bytes,
        max_compression_ratio=profile.max_compression_ratio,
        max_xml_depth=profile.max_structure_depth,
    )


def _preflight_xlsx_source(source: SnapshotCursorLease) -> None:
    try:
        preflight_xlsx(source, _xlsx_preflight_limits())
    except XlsxPreflightError as failure:
        code = (
            "PARSER.SYNTAX.MALFORMED"
            if failure.code == ARCHIVE_INVALID
            else failure.code
        )
        raise ParserSourceError(
            code,
            "XLSX archive or OPC XML preflight rejected the sealed input",
        ) from None
    _openpyxl_module()


class XlsxTermbaseCodec:
    @property
    def descriptor(self) -> CodecDescriptor:
        return TERMBASE_XLSX_DESCRIPTOR

    def preflight_input(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> None:
        try:
            _validate_request(request, TERMBASE_XLSX_V1)
        except _SelectionFailure as failure:
            raise ParserSourceError(failure.code, failure.safe_summary) from None
        _preflight_xlsx_source(source)

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[ResourceRecord | ParseIssue]:
        try:
            selection = _validate_request(request, TERMBASE_XLSX_V1)
        except _SelectionFailure as failure:
            yield _fatal(failure)
            return
        module = _openpyxl_module()
        workbook = None
        try:
            workbook = module.load_workbook(
                source,
                read_only=True,
                data_only=True,
                keep_links=False,
                keep_vba=False,
            )
            sheet = workbook.active
            if sheet is None:
                yield ParseIssue(
                    code=_ACTIVE_SHEET_MISSING,
                    severity=IssueSeverity.FATAL,
                    safe_summary="XLSX termbase has no active worksheet",
                )
                return

            def rows():
                for row in sheet.iter_rows(values_only=False):
                    values = tuple(getattr(cell, "value", None) for cell in row)
                    present = frozenset(
                        index
                        for index, cell in enumerate(row)
                        if cell.__class__.__name__ != "EmptyCell"
                    )
                    yield values, present

            yield from _iter_selected_rows(rows(), selection)
        except ParserSourceError:
            raise
        except Exception:
            yield ParseIssue(
                code="PARSER.SYNTAX.MALFORMED",
                severity=IssueSeverity.FATAL,
                safe_summary="XLSX workbook does not satisfy the declared active-sheet grammar",
            )
        finally:
            if workbook is not None:
                workbook.close()

    def preview_columns(
        self,
        source: SnapshotCursorLease,
        request: TermbaseColumnPreviewRequest,
    ) -> TermbaseColumnPreview:
        _validate_preview_request(request, TERMBASE_XLSX_V1)
        _preflight_xlsx_source(source)
        module = _openpyxl_module()
        invalid_file_error = module.utils.exceptions.InvalidFileException
        expected_input_errors = (
            invalid_file_error,
            EOFError,
            KeyError,
            ValueError,
        )
        workbook = None
        try:
            workbook = module.load_workbook(
                source,
                read_only=True,
                data_only=True,
                keep_links=False,
                keep_vba=False,
            )
            sheet = workbook.active
            if sheet is None:
                raise ParserSourceError(
                    _ACTIVE_SHEET_MISSING,
                    "XLSX termbase has no active worksheet",
                )
            try:
                first_row = next(sheet.iter_rows(values_only=False))
            except StopIteration:
                raise ParserSourceError(
                    _PREVIEW_EMPTY,
                    "the active worksheet has no previewable first row",
                ) from None
            present = tuple(
                index
                for index, cell in enumerate(first_row)
                if cell.__class__.__name__ != "EmptyCell"
            )
            if not present:
                raise ParserSourceError(
                    _PREVIEW_EMPTY,
                    "the active worksheet has no previewable first row",
                )
            width = max(present) + 1
            values = tuple(
                getattr(cell, "value", None)
                for cell in first_row[:width]
            )
            raw_title = getattr(sheet, "title", "")
            title = raw_title if type(raw_title) is str else ""
            return _preview_from_row(
                values,
                source=source,
                descriptor=self.descriptor,
                active_sheet_name=title[:MAX_TERMBASE_PREVIEW_LABEL_CHARS],
            )
        except ParserSourceError:
            raise
        except expected_input_errors:
            raise ParserSourceError(
                "PARSER.SYNTAX.MALFORMED",
                "XLSX workbook does not satisfy the declared active-sheet grammar",
            ) from None
        finally:
            if workbook is not None:
                workbook.close()


TERMBASE_CSV_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "termbase-csv", "1"),
    purpose=EffectivePurpose.TERMBASE,
    format_id=TERMBASE_CSV_V1,
    extensions=(".csv",),
    mime_types=("text/csv",),
    sniff_prefixes=(),
    capabilities=CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=False,
        source_round_trip_write=False,
        streaming_input=True,
        iterator_view=True,
        materialized_view=True,
        format_profile=CSV_LIMIT_PROFILE.profile_id,
        active_sheet_only=False,
        termbase_column_preview=True,
        opaque_features=("explicit-column-selection", "legacy-header-allowlist"),
    ),
    limit_profile=CSV_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
    reader_factory=CsvTermbaseCodec,
    canonical_serializer_factory=None,
)

TERMBASE_XLSX_DESCRIPTOR = CodecDescriptor(
    identity=CodecIdentity("localcat", "termbase-xlsx", "1"),
    purpose=EffectivePurpose.TERMBASE,
    format_id=TERMBASE_XLSX_V1,
    extensions=(".xlsx",),
    mime_types=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    sniff_prefixes=(b"PK\x03\x04",),
    capabilities=CodecCapabilities(
        readable=True,
        validatable=True,
        canonical_write=False,
        source_round_trip_write=False,
        streaming_input=True,
        iterator_view=True,
        materialized_view=True,
        format_profile=XLSX_LIMIT_PROFILE.profile_id,
        active_sheet_only=True,
        termbase_column_preview=True,
        opaque_features=(
            "conditional-dependency:openpyxl>=3.1,<4",
            "data-only-cells",
            "preflight-all-opc-xml",
        ),
    ),
    limit_profile=XLSX_LIMIT_PROFILE,
    input_consumption_policy=InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET,
    reader_factory=XlsxTermbaseCodec,
    canonical_serializer_factory=None,
)


def termbase_descriptors() -> tuple[CodecDescriptor, CodecDescriptor]:
    """Return the immutable built-in descriptors for explicit composition."""

    return TERMBASE_CSV_DESCRIPTOR, TERMBASE_XLSX_DESCRIPTOR
