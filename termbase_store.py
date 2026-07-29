"""Strict local storage boundary for mixed legacy/v1 termbase CSV files."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import TextIO

from editor_contracts import (
    TermMatchPolicy,
    TermRecord,
    TermRecordLocator,
    TermRowKind,
)


_V1_MARKER = TermRowKind.V1.value
_BOOLEAN_VALUES = {"false": False, "true": True}


class TermbaseValidationError(ValueError):
    """Structured, content-safe validation failure for a termbase snapshot."""

    def __init__(
        self,
        code: str,
        row_ordinal: int | None = None,
        conflicting_row_ordinal: int | None = None,
    ) -> None:
        self.code = code
        self.row_ordinal = row_ordinal
        self.conflicting_row_ordinal = conflicting_row_ordinal
        location = "" if row_ordinal is None else f" at row {row_ordinal}"
        if conflicting_row_ordinal is not None:
            location += f" (conflicts with row {conflicting_row_ordinal})"
        super().__init__(f"{code}{location}")


class _CapturingLineIterator:
    """Feed ``csv.reader`` while retaining the physical text of each record."""

    def __init__(self, stream: TextIO) -> None:
        self._lines = iter(stream)
        self._captured: list[str] = []

    def __iter__(self) -> _CapturingLineIterator:
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        self._captured.append(line)
        return line

    def start_record(self) -> None:
        self._captured.clear()

    def captured_record(self) -> str:
        return "".join(self._captured)


class TermbaseStore:
    """Read and eventually mutate one atomic mixed CSV termbase."""

    def list_records(self, path: Path) -> tuple[TermRecord, ...]:
        """Return a fully validated immutable snapshot in file order."""

        original = path.read_bytes()
        file_digest = hashlib.sha256(original).hexdigest()
        try:
            text = original.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TermbaseValidationError("INVALID_UTF8") from exc

        lines = _CapturingLineIterator(io.StringIO(text, newline=""))
        reader = csv.reader(lines, strict=True)
        records: list[TermRecord] = []
        source_ordinals: dict[str, int] = {}
        id_ordinals: dict[str, int] = {}

        while True:
            row_ordinal = len(records)
            lines.start_record()
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                raise TermbaseValidationError(
                    "MALFORMED_CSV",
                    row_ordinal,
                ) from exc

            raw_record = lines.captured_record()
            _validate_strict_csv_record(raw_record, row_ordinal)
            record = self._record_from_row(
                row=row,
                raw_record=raw_record,
                file_digest=file_digest,
                row_ordinal=row_ordinal,
            )

            conflicting_source = source_ordinals.get(record.source)
            if conflicting_source is not None:
                raise TermbaseValidationError(
                    "DUPLICATE_SOURCE",
                    row_ordinal,
                    conflicting_source,
                )
            source_ordinals[record.source] = row_ordinal

            if record.record_id is not None:
                conflicting_id = id_ordinals.get(record.record_id)
                if conflicting_id is not None:
                    raise TermbaseValidationError(
                        "DUPLICATE_ID",
                        row_ordinal,
                        conflicting_id,
                    )
                id_ordinals[record.record_id] = row_ordinal

            records.append(record)

        return tuple(records)

    @staticmethod
    def _record_from_row(
        *,
        row: list[str],
        raw_record: str,
        file_digest: str,
        row_ordinal: int,
    ) -> TermRecord:
        if not row:
            raise TermbaseValidationError("EMPTY_ROW", row_ordinal)

        if len(row) == 2:
            record_id = None
            source, target = row
            row_kind = TermRowKind.LEGACY
            policy = TermMatchPolicy.LEGACY
            match_case = None
            whole_word = None
        elif len(row) == 6:
            marker, record_id, source, target, match_case_raw, whole_word_raw = row
            if marker != _V1_MARKER:
                raise TermbaseValidationError("UNKNOWN_MARKER", row_ordinal)
            if not record_id.strip():
                raise TermbaseValidationError("EMPTY_RECORD_ID", row_ordinal)
            match_case = _parse_bool(match_case_raw, row_ordinal)
            whole_word = _parse_bool(whole_word_raw, row_ordinal)
            row_kind = TermRowKind.V1
            policy = TermMatchPolicy.CONFIGURED
        else:
            raise TermbaseValidationError("INVALID_COLUMN_COUNT", row_ordinal)

        if not source.strip():
            raise TermbaseValidationError("EMPTY_SOURCE", row_ordinal)
        if not target.strip():
            raise TermbaseValidationError("EMPTY_TARGET", row_ordinal)

        locator = TermRecordLocator(
            row_kind=row_kind,
            file_digest=file_digest,
            row_ordinal=row_ordinal,
            row_digest=hashlib.sha256(raw_record.encode("utf-8")).hexdigest(),
            record_id=record_id,
        )
        return TermRecord(
            locator=locator,
            record_id=record_id,
            source=source,
            target=target,
            policy=policy,
            match_case=match_case,
            whole_word=whole_word,
        )


def _parse_bool(value: str, row_ordinal: int) -> bool:
    try:
        return _BOOLEAN_VALUES[value]
    except KeyError as exc:
        raise TermbaseValidationError("INVALID_BOOLEAN", row_ordinal) from exc


def _validate_strict_csv_record(raw_record: str, row_ordinal: int) -> None:
    """Reject quote placement that ``csv.reader(strict=True)`` still tolerates."""

    if raw_record.endswith("\r\n"):
        content = raw_record[:-2]
    elif raw_record.endswith(("\r", "\n")):
        content = raw_record[:-1]
    else:
        content = raw_record

    field_start = True
    in_quoted_field = False
    after_quoted_field = False
    index = 0

    while index < len(content):
        character = content[index]
        if in_quoted_field:
            if character != '"':
                index += 1
                continue
            if index + 1 < len(content) and content[index + 1] == '"':
                index += 2
                continue
            in_quoted_field = False
            after_quoted_field = True
            index += 1
            continue

        if after_quoted_field:
            if character != ",":
                raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
            after_quoted_field = False
            field_start = True
            index += 1
            continue

        if field_start:
            if character == '"':
                field_start = False
                in_quoted_field = True
            elif character == ",":
                field_start = True
            elif character in "\r\n":
                raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
            else:
                field_start = False
            index += 1
            continue

        if character == '"' or character in "\r\n":
            raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
        if character == ",":
            field_start = True
        index += 1

    if in_quoted_field:
        raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
