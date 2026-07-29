"""Strict local storage boundary for mixed legacy/v1 termbase CSV files."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import uuid
from pathlib import Path
from typing import TextIO

from editor_contracts import (
    LegacyTermRow,
    PreparedTermMutation,
    TermDraft,
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
    """Read and prepare atomic mutations for one mixed CSV termbase."""

    def prepare_create(
        self,
        path: Path,
        draft: TermDraft,
    ) -> PreparedTermMutation:
        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        path = _absolute_resource_path(path)
        original, base_digest, records = self._read_snapshot(path)
        conflicting_ordinal = _source_ordinal(records, draft.source)
        if conflicting_ordinal is not None:
            raise TermbaseValidationError(
                "DUPLICATE_SOURCE",
                len(records),
                conflicting_ordinal,
            )

        existing_ids = {
            record.record_id for record in records if record.record_id is not None
        }
        record_id = str(uuid.uuid4())
        while record_id in existing_ids:
            record_id = str(uuid.uuid4())

        candidate_rows = _rows_from_records(records)
        candidate_rows.append(
            [
                _V1_MARKER,
                record_id,
                draft.source,
                draft.target,
                "false",
                "true",
            ]
        )
        return self._prepare_artifacts(
            action="create",
            path=path,
            original=original,
            base_digest=base_digest,
            candidate_rows=candidate_rows,
        )

    def prepare_update(
        self,
        path: Path,
        locator: TermRecordLocator,
        draft: TermDraft,
    ) -> PreparedTermMutation:
        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        path = _absolute_resource_path(path)
        original, base_digest, records = self._read_snapshot(path)
        row_ordinal, current = _locate_current_record(
            records,
            base_digest,
            locator,
        )
        conflicting_ordinal = _source_ordinal(records, draft.source)
        if (
            conflicting_ordinal is not None
            and conflicting_ordinal != row_ordinal
        ):
            raise TermbaseValidationError(
                "CONFLICTING_SOURCE",
                row_ordinal,
                conflicting_ordinal,
            )

        candidate_rows = _rows_from_records(records)
        if current.locator.row_kind is TermRowKind.LEGACY:
            candidate_rows[row_ordinal] = [draft.source, draft.target]
        else:
            record_id = current.record_id
            if record_id is None:
                raise AssertionError("validated v1 record must have an id")
            candidate_rows[row_ordinal] = [
                _V1_MARKER,
                record_id,
                draft.source,
                draft.target,
                _format_bool(draft.match_case),
                _format_bool(draft.whole_word),
            ]
        return self._prepare_artifacts(
            action="update",
            path=path,
            original=original,
            base_digest=base_digest,
            candidate_rows=candidate_rows,
        )

    def prepare_delete(
        self,
        path: Path,
        locator: TermRecordLocator,
    ) -> PreparedTermMutation:
        path = _absolute_resource_path(path)
        original, base_digest, records = self._read_snapshot(path)
        row_ordinal, _ = _locate_current_record(
            records,
            base_digest,
            locator,
        )
        candidate_rows = _rows_from_records(records)
        del candidate_rows[row_ordinal]
        return self._prepare_artifacts(
            action="delete",
            path=path,
            original=original,
            base_digest=base_digest,
            candidate_rows=candidate_rows,
        )

    def prepare_merge_legacy(
        self,
        path: Path,
        rows: tuple[LegacyTermRow, ...],
    ) -> PreparedTermMutation:
        if not isinstance(rows, tuple):
            raise TypeError("legacy merge rows must be a tuple")
        if not all(isinstance(row, LegacyTermRow) for row in rows):
            raise TypeError("legacy merge rows must contain LegacyTermRow values")
        if not rows:
            raise TermbaseValidationError("EMPTY_IMPORT")

        path = _absolute_resource_path(path)
        original, base_digest, records = self._read_snapshot(path)
        incoming_targets: dict[str, str] = {}
        first_input_order: list[str] = []
        for row in rows:
            if row.source not in incoming_targets:
                first_input_order.append(row.source)
            incoming_targets[row.source] = row.target

        candidate_rows = _rows_from_records(records)
        existing_sources = {record.source for record in records}
        for row_ordinal, record in enumerate(records):
            target = incoming_targets.get(record.source)
            if target is not None:
                candidate_rows[row_ordinal] = _row_with_target(record, target)

        for source in first_input_order:
            if source not in existing_sources:
                candidate_rows.append([source, incoming_targets[source]])

        return self._prepare_artifacts(
            action="merge_legacy",
            path=path,
            original=original,
            base_digest=base_digest,
            candidate_rows=candidate_rows,
        )

    def list_records(self, path: Path) -> tuple[TermRecord, ...]:
        """Return a fully validated immutable snapshot in file order."""

        return self._records_from_bytes(path.read_bytes())

    def _read_snapshot(
        self,
        path: Path,
    ) -> tuple[bytes, str, tuple[TermRecord, ...]]:
        original = path.read_bytes()
        file_digest = hashlib.sha256(original).hexdigest()
        return original, file_digest, self._records_from_bytes(original)

    def _records_from_bytes(self, original: bytes) -> tuple[TermRecord, ...]:
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

    def _prepare_artifacts(
        self,
        *,
        action: str,
        path: Path,
        original: bytes,
        base_digest: str,
        candidate_rows: list[list[str]],
    ) -> PreparedTermMutation:
        candidate_bytes = _serialize_rows(candidate_rows)
        candidate_records = self._records_from_bytes(candidate_bytes)
        recovery_path: Path | None = None
        staged_path: Path | None = None
        try:
            recovery_path = _write_durable_temp(
                path.parent,
                f".{path.name}.recovery-",
                original,
            )
            staged_path = _write_durable_temp(
                path.parent,
                f".{path.name}.staged-",
                candidate_bytes,
            )
            _fsync_directory(path.parent)
            return PreparedTermMutation(
                action=action,
                resource_path=path,
                base_digest=base_digest,
                staged_path=staged_path,
                recovery_path=recovery_path,
                candidate_records=candidate_records,
            )
        except BaseException:
            _cleanup_prepare_artifacts(staged_path, recovery_path)
            _best_effort_fsync_directory(path.parent)
            raise

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


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _absolute_resource_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("term resource path must be a Path")
    return Path(os.path.abspath(path))


def _source_ordinal(
    records: tuple[TermRecord, ...],
    source: str,
) -> int | None:
    for row_ordinal, record in enumerate(records):
        if record.source == source:
            return row_ordinal
    return None


def _locate_current_record(
    records: tuple[TermRecord, ...],
    base_digest: str,
    locator: TermRecordLocator,
) -> tuple[int, TermRecord]:
    if not isinstance(locator, TermRecordLocator):
        raise TypeError("term locator must be a TermRecordLocator")
    if locator.file_digest != base_digest:
        raise TermbaseValidationError("STALE_LOCATOR")
    if locator.row_ordinal >= len(records):
        raise TermbaseValidationError("STALE_LOCATOR")
    current = records[locator.row_ordinal]
    if current.locator != locator:
        raise TermbaseValidationError("STALE_LOCATOR")
    return locator.row_ordinal, current


def _row_from_record(record: TermRecord) -> list[str]:
    if record.locator.row_kind is TermRowKind.LEGACY:
        return [record.source, record.target]
    record_id = record.record_id
    match_case = record.match_case
    whole_word = record.whole_word
    if (
        record_id is None
        or match_case is None
        or whole_word is None
    ):
        raise AssertionError("validated v1 record must have identity and flags")
    return [
        _V1_MARKER,
        record_id,
        record.source,
        record.target,
        _format_bool(match_case),
        _format_bool(whole_word),
    ]


def _row_with_target(record: TermRecord, target: str) -> list[str]:
    row = _row_from_record(record)
    if record.locator.row_kind is TermRowKind.LEGACY:
        row[1] = target
    else:
        row[3] = target
    return row


def _rows_from_records(records: tuple[TermRecord, ...]) -> list[list[str]]:
    return [_row_from_record(record) for record in records]


def _serialize_rows(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _write_durable_temp(directory: Path, prefix: str, data: bytes) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    artifact_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            _ = stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        artifact_path.unlink(missing_ok=True)
        raise
    return artifact_path


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_prepare_artifacts(*paths: Path | None) -> None:
    for path in paths:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _best_effort_fsync_directory(directory: Path) -> None:
    try:
        _fsync_directory(directory)
    except OSError:
        pass


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
