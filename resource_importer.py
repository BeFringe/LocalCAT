"""Safe, atomic importers for LocalCAT translation resources."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import cast
from uuid import uuid4

from editor_contracts import (
    ImportReport,
    LegacyTermRow,
    TermbaseImportHeaderMode,
    TermbaseImportPreview,
    TermbaseImportPreviewColumn,
    TermbaseImportSelection,
    TermbaseImportSourceIdentity,
)
from parser_composition import create_parser_application_surface
from parser_contracts import (
    ContractViolation,
    EffectivePurpose,
    FormatId,
    IssueSeverity,
    ParseIssue,
    ReadRequest,
    ResourceRecord,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    SourceSnapshotIdentity,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
    TermbaseColumnPreview,
    TermbaseColumnPreviewRequest,
    TMX_LEVEL1_V1,
    TermbaseColumnSelection,
    TermbaseColumnSelector,
    TermbaseHeaderPolicy,
    TermbaseReadOptions,
    TmxReadOptions,
    ColumnSelectorKind,
)
from tm_contracts import TMRecordDraft
from tm_engine import open_canonical_tm_store
from tm_sqlite_store import (
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
)


LOGGER = logging.getLogger(__name__)

_TERMBASE_FORMAT_BY_SUFFIX = {
    ".csv": TERMBASE_CSV_V1,
    ".xlsx": TERMBASE_XLSX_V1,
}
_TERMBASE_EXISTING_ALLOWED_WARNING_CODES = frozenset(
    {
        "PARSER.TERMBASE.HEADER_SKIPPED",
        "PARSER.TERMBASE.ROW_EMPTY",
    }
)


class ImportFailure(RuntimeError):
    """Internal all-or-nothing import failure."""


@dataclass(frozen=True, slots=True)
class _StagedResource:
    records: tuple[ResourceRecord, ...]
    warnings: tuple[ParseIssue, ...]
    source_digest: str


def _source_reference(path: Path) -> SourceReference:
    expanded = path.expanduser()
    absolute = expanded if expanded.is_absolute() else expanded.absolute()
    parent = absolute.parent
    return SourceReference(
        safe_root=str(parent),
        selected_path=str(absolute),
        display_hint="selected resource",
    )


def _format_parser_failure(error: ContractViolation) -> str:
    return f"{error.code}: {error.safe_summary}"


def _format_parser_issue(issue: ParseIssue) -> str:
    return f"{issue.code}: {issue.safe_summary}"


def _stage_parser_resource(
    path: Path,
    *,
    purpose: EffectivePurpose,
    format_id: FormatId,
    request: ReadRequest,
    expected_source_identity: SourceSnapshotIdentity | None = None,
    expected_preview_column_count: int | None = None,
) -> _StagedResource:
    """Consume one guarded stream and expose records only after its terminal."""

    surface = create_parser_application_surface()
    try:
        opened = surface.open_input(
            _source_reference(path),
            SelectionRequest(purpose=purpose, format_id=format_id),
            request,
        )
    except ContractViolation as exc:
        raise ImportFailure(_format_parser_failure(exc)) from None
    if type(opened) is SelectionFailure:
        raise ImportFailure(
            f"{opened.code}: no compatible Parser codec is registered for the resource"
        )

    try:
        if (
            expected_source_identity is not None
            and opened.source_identity != expected_source_identity
        ):
            raise ImportFailure(
                "PARSER.SOURCE.STALE: current termbase input differs from the previewed source"
            )
        if expected_preview_column_count is not None:
            try:
                current_preview = opened.preview_termbase_columns(
                    TermbaseColumnPreviewRequest(
                        purpose=purpose,
                        format_id=format_id,
                    )
                )
            except ContractViolation as exc:
                raise ImportFailure(_format_parser_failure(exc)) from None
            if len(current_preview.columns) != expected_preview_column_count:
                raise ImportFailure(
                    "PARSER.TERMBASE.COLUMN_SELECTION_INVALID: "
                    "the selected columns do not match the current visible preview"
                )
        records: list[ResourceRecord] = []
        warnings: list[ParseIssue] = []
        source_digest = opened.source_identity.content_sha256
        try:
            session = opened.stream()
        except ContractViolation as exc:
            raise ImportFailure(_format_parser_failure(exc)) from None
        try:
            try:
                for event in session:
                    if type(event) is ResourceRecord:
                        records.append(event)
                    elif (
                        type(event) is ParseIssue
                        and event.severity is IssueSeverity.WARNING
                    ):
                        warnings.append(event)
                terminal = session.verified_terminal()
            except ContractViolation as exc:
                raise ImportFailure(_format_parser_failure(exc)) from None
            if terminal.record_count != len(records):
                raise ImportFailure(
                    "PARSER.SESSION.UNVERIFIED: staged record count does not match the verified terminal"
                )
            if terminal.source != opened.source_identity:
                raise ImportFailure(
                    "PARSER.SOURCE.STALE: verified terminal does not bind the opened snapshot"
                )
        finally:
            session.close()
    finally:
        opened.close()
    return _StagedResource(
        records=tuple(records),
        warnings=tuple(warnings),
        source_digest=source_digest,
    )


def _termbase_request(
    format_id: FormatId,
    columns: TermbaseColumnSelection,
) -> ReadRequest:
    return ReadRequest(
        purpose=EffectivePurpose.TERMBASE,
        format_id=format_id,
        termbase_options=TermbaseReadOptions(columns=columns),
    )


def _stage_termbase(
    path: Path,
    *,
    format_id: FormatId | None = None,
    columns: TermbaseColumnSelection | None = None,
    expected_source_identity: SourceSnapshotIdentity | None = None,
    expected_preview_column_count: int | None = None,
) -> _StagedResource:
    selected_format = format_id
    if selected_format is None:
        selected_format = _TERMBASE_FORMAT_BY_SUFFIX.get(path.suffix.lower())
        if selected_format is None:
            supported = ", ".join(sorted(_TERMBASE_FORMAT_BY_SUFFIX))
            raise ImportFailure(
                f"unsupported import format; expected one of: {supported}"
            )
    return _stage_parser_resource(
        path,
        purpose=EffectivePurpose.TERMBASE,
        format_id=selected_format,
        request=_termbase_request(
            selected_format,
            columns or TermbaseColumnSelection.legacy_first_two_columns(),
        ),
        expected_source_identity=expected_source_identity,
        expected_preview_column_count=expected_preview_column_count,
    )


def _parser_source_identity(
    identity: TermbaseImportSourceIdentity,
) -> SourceSnapshotIdentity:
    return SourceSnapshotIdentity(
        relative_reference_sha256=identity.relative_reference_sha256,
        regular_file_identity=identity.regular_file_identity,
        original_size=identity.original_size,
        original_mtime_ns=identity.original_mtime_ns,
        content_sha256=identity.content_sha256,
        byte_count=identity.byte_count,
        schema_version=identity.schema_version,
    )


def _editor_source_identity(
    identity: SourceSnapshotIdentity,
) -> TermbaseImportSourceIdentity:
    return TermbaseImportSourceIdentity(
        relative_reference_sha256=identity.relative_reference_sha256,
        regular_file_identity=identity.regular_file_identity,
        original_size=identity.original_size,
        original_mtime_ns=identity.original_mtime_ns,
        content_sha256=identity.content_sha256,
        byte_count=identity.byte_count,
        schema_version=identity.schema_version,
    )


def _selected_termbase_columns(
    selection: TermbaseImportSelection,
) -> TermbaseColumnSelection:
    if type(selection) is not TermbaseImportSelection:
        raise TypeError("termbase selection must use the exact import contract")
    selection.__post_init__()
    header_policy = (
        TermbaseHeaderPolicy.FIRST_ROW
        if selection.header_mode is TermbaseImportHeaderMode.FIRST_ROW
        else TermbaseHeaderPolicy.NO_HEADER
    )
    return TermbaseColumnSelection(
        source=TermbaseColumnSelector(
            kind=ColumnSelectorKind.ZERO_BASED_INDEX,
            zero_based_index=selection.source_zero_based_index,
        ),
        target=TermbaseColumnSelector(
            kind=ColumnSelectorKind.ZERO_BASED_INDEX,
            zero_based_index=selection.target_zero_based_index,
        ),
        header_policy=header_policy,
    )


def preview_termbase_import(input_path: Path) -> TermbaseImportPreview:
    """Return one bounded Qt-safe column preview without touching any store."""

    try:
        source = _validate_input(input_path, {".csv", ".xlsx"})
        format_id = _TERMBASE_FORMAT_BY_SUFFIX[source.suffix.lower()]
        surface = create_parser_application_surface()
        report = surface.preview_termbase_columns(
            _source_reference(source),
            SelectionRequest(
                purpose=EffectivePurpose.TERMBASE,
                format_id=format_id,
            ),
            TermbaseColumnPreviewRequest(
                purpose=EffectivePurpose.TERMBASE,
                format_id=format_id,
            ),
        )
    except ContractViolation as exc:
        raise ImportFailure(_format_parser_failure(exc)) from None
    except OSError as exc:
        raise ImportFailure(
            f"unable to preview termbase input: {type(exc).__name__}"
        ) from exc
    if type(report) is SelectionFailure:
        raise ImportFailure(
            f"{report.code}: no compatible Parser codec is registered for the resource"
        )
    if type(report) is not TermbaseColumnPreview:
        raise AssertionError("Parser termbase preview returned an invalid contract")
    if report.format_id != format_id:
        raise AssertionError("Parser termbase preview changed the selected format")
    return TermbaseImportPreview(
        format_name=source.suffix.lower().removeprefix("."),
        columns=tuple(
            TermbaseImportPreviewColumn(
                zero_based_index=column.zero_based_index,
                header_candidate=column.header_candidate,
                header_original_char_count=column.header_original_char_count,
                header_truncated=column.header_truncated,
            )
            for column in report.columns
        ),
        total_column_count=report.total_column_count,
        columns_truncated=report.columns_truncated,
        legacy_header_detected=report.legacy_header_detected,
        active_sheet_name=report.active_sheet_name,
        source_identity=_editor_source_identity(report.source),
    )


def _tmx_request(source_locale: str, target_locale: str) -> ReadRequest:
    try:
        options = TmxReadOptions(
            source_locale=source_locale,
            target_locale=target_locale,
        )
    except (TypeError, ValueError):
        raise ImportFailure(
            "PARSER.TMX.LOCALE_SELECTION_INVALID: TMX source and target locale selection is invalid"
        ) from None
    return ReadRequest(
        purpose=EffectivePurpose.TRANSLATION_MEMORY,
        format_id=TMX_LEVEL1_V1,
        tmx_options=options,
    )


def _physical_row_ordinal(record: ResourceRecord) -> int:
    prefix, separator, raw_ordinal = record.local_id.partition("-")
    if prefix != "row" or separator != "-" or not raw_ordinal.isdecimal():
        raise ImportFailure(
            "PARSER.SYNTAX.INVALID_EVENT: termbase record has an invalid physical row identity"
        )
    ordinal = int(raw_ordinal)
    if ordinal <= 0:
        raise ImportFailure(
            "PARSER.SYNTAX.INVALID_EVENT: termbase record has an invalid physical row identity"
        )
    return ordinal


def read_legacy_termbase_import(
    input_path: Path,
    selection: TermbaseImportSelection | None = None,
) -> tuple[tuple[LegacyTermRow, ...], int]:
    """Read one CSV/XLSX import without touching the managed resource.

    The Controller passes the validated rows to ``TermbaseStore`` so the
    mixed legacy/v1 target remains under its single transaction boundary.
    Duplicate rows deliberately remain present: the Store owns source-LWW
    ordering and overwrite counts.
    """

    source = _validate_input(input_path, {".csv", ".xlsx"})
    columns = None
    expected_source_identity = None
    expected_preview_column_count = None
    if selection is not None:
        columns = _selected_termbase_columns(selection)
        expected_source_identity = _parser_source_identity(
            selection.preview_source_identity
        )
        expected_preview_column_count = selection.preview_column_count
    staged = _stage_termbase(
        source,
        columns=columns,
        expected_source_identity=expected_source_identity,
        expected_preview_column_count=expected_preview_column_count,
    )
    accepted = tuple(
        LegacyTermRow(
            source=record.source,
            target=record.target,
            input_ordinal=_physical_row_ordinal(record) - 1,
        )
        for record in staged.records
    )
    if not accepted:
        raise ImportFailure("termbase contains no valid source/target rows")
    return accepted, len(staged.warnings)


def import_tmx(
    input_path: Path,
    target_path: Path,
    source_locale: str,
    target_locale: str,
) -> ImportReport:
    """Merge one safe TMX Level 1 file into a translation memory.

    Task 6.1: an activated resource receives its validated ordered units
    directly in canonical storage (same-source variants retained, no
    folding); a not-yet-activated resource keeps the existing atomic
    JSONL last-write-wins merge unchanged.
    """

    try:
        source = _validate_input(input_path, {".tmx"})
        # Freeze compatibility receipt provenance before the Parser opens its
        # sealed snapshot.  The Parser still receives the unresolved lexical
        # selection so SourceReference preserves the user's selected path;
        # no path lookup is repeated after streaming begins.
        receipt_source = source.resolve()
        target = target_path.expanduser().resolve()
        staged = _stage_parser_resource(
            source,
            purpose=EffectivePurpose.TRANSLATION_MEMORY,
            format_id=TMX_LEVEL1_V1,
            request=_tmx_request(source_locale, target_locale),
        )
        if not staged.records:
            raise ImportFailure(
                "TMX contains no valid units for "
                f"{source_locale.strip()} → {target_locale.strip()}"
            )
        ordered_units = tuple(
            (record.source, record.target) for record in staged.records
        )
        incoming: dict[str, dict[str, object]] = {}
        duplicate_count = 0
        for source_text, target_text in ordered_units:
            if source_text in incoming:
                duplicate_count += 1
            incoming[source_text] = {
                "source": source_text,
                "target": target_text,
            }
        skipped = len(staged.warnings)
        warnings = tuple(_format_parser_issue(issue) for issue in staged.warnings)
        canonical = open_canonical_tm_store(target)
        if canonical is not None:
            drafts = tuple(
                _tmx_import_draft(source_text, target_text, receipt_source.name)
                for source_text, target_text in ordered_units
            )
            try:
                canonical.append_batch(
                    batch_id=f"import.{uuid4().hex}",
                    kind="import",
                    drafts=drafts,
                    source_digest=staged.source_digest,
                    source_path=receipt_source,
                    invalid_count=skipped,
                    duplicate_source_count=duplicate_count,
                )
            except sqlite3.IntegrityError as exc:
                if _is_identical_import_constraint(exc):
                    raise ImportFailure(
                        "import already applied: identical source digest"
                    ) from exc
                raise ImportFailure(
                    "canonical import transaction constraint failed"
                ) from exc
            except sqlite3.Error as exc:
                raise ImportFailure(
                    "canonical import transaction failed"
                ) from exc
            LOGGER.info(
                "Imported %d canonical TM entries from %s",
                len(drafts),
                receipt_source,
            )
            return ImportReport(
                imported=len(drafts),
                skipped=skipped,
                overwritten=0,
                errors=warnings,
            )
        existing = _read_existing_tm(target)
        overwritten = duplicate_count + sum(key in existing for key in incoming)
        merged = dict(existing)
        merged.update(incoming)
        rendered = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in merged.values()
        )
        _atomic_write_text(target, rendered, "utf-8")
        LOGGER.info("Imported %d TM entries from %s", len(incoming), source)
        return ImportReport(
            imported=len(incoming),
            skipped=skipped,
            overwritten=overwritten,
            errors=warnings,
        )
    except (
        ImportFailure,
        OSError,
        UnicodeError,
        ValueError,
        SQLiteStoreSchemaError,
        SQLiteStoreLifecycleError,
    ) as exc:
        return ImportReport(errors=(str(exc),))


def import_termbase(input_path: Path, target_path: Path) -> ImportReport:
    """Merge the first two columns of a CSV/XLSX file into a UTF-8-SIG CSV."""

    try:
        source = _validate_input(input_path, {".csv", ".xlsx"})
        target = target_path.expanduser().resolve()
        staged = _stage_termbase(source)
        incoming: dict[str, str] = {}
        duplicate_count = 0
        for record in staged.records:
            if record.source in incoming:
                duplicate_count += 1
            incoming[record.source] = record.target
        skipped = len(staged.warnings)
        if not incoming:
            raise ImportFailure("termbase contains no valid source/target rows")
        existing = _read_existing_terms(target)
        overwritten = duplicate_count + sum(key in existing for key in incoming)
        merged = dict(existing)
        merged.update(incoming)

        _atomic_write_text(target, _render_terms(merged), "utf-8-sig")
        LOGGER.info("Imported %d terms from %s", len(incoming), source)
        return ImportReport(
            imported=len(incoming),
            skipped=skipped,
            overwritten=overwritten,
        )
    except (ImportFailure, OSError, UnicodeError, csv.Error, ValueError) as exc:
        return ImportReport(errors=(str(exc),))


def upsert_term(target_path: Path, source_term: str, target_term: str) -> ImportReport:
    """Atomically add or replace one term in an existing managed termbase."""

    source = source_term.strip()
    target_text = target_term.strip()
    if not source or not target_text:
        return ImportReport(errors=("source and target terms must not be empty",))
    target = target_path.expanduser().resolve()
    if target.suffix.lower() != ".csv":
        return ImportReport(errors=("managed termbase must use the .csv format",))
    try:
        existing = _read_existing_terms(target)
        overwritten = int(source in existing)
        updated = dict(existing)
        updated[source] = target_text
        _atomic_write_text(target, _render_terms(updated), "utf-8-sig")
        return ImportReport(imported=1, overwritten=overwritten)
    except (ImportFailure, OSError, UnicodeError, csv.Error, ValueError) as exc:
        return ImportReport(errors=(str(exc),))


def _validate_input(input_path: Path, suffixes: set[str]) -> Path:
    expanded = input_path.expanduser()
    path = expanded if expanded.is_absolute() else expanded.absolute()
    if not path.exists() or not path.is_file():
        raise ImportFailure(f"input file does not exist: {path}")
    if path.suffix.lower() not in suffixes:
        supported = ", ".join(sorted(suffixes))
        raise ImportFailure(f"unsupported import format; expected one of: {supported}")
    return path


def _tmx_import_draft(
    source_text: str,
    target_text: str,
    file_name: str,
) -> TMRecordDraft:
    """One private exact import draft in validated input order."""

    return TMRecordDraft(
        source_raw=source_text,
        target_raw=target_text,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=file_name,
        provenance=(("source", "tmx-import"), ("file", file_name)),
    )


def _is_identical_import_constraint(error: sqlite3.IntegrityError) -> bool:
    """Recognize only the origin digest uniqueness contract."""

    return (
        getattr(error, "sqlite_errorcode", None)
        == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and "tm_origin_batch.kind, tm_origin_batch.source_digest"
        in str(error)
    )


def _read_existing_tm(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, object]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ImportFailure(f"unable to read target translation memory: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw_record = cast(object, json.loads(line))
        except json.JSONDecodeError as exc:
            raise ImportFailure(f"target TM has invalid JSON on line {line_number}") from exc
        if not isinstance(raw_record, dict):
            raise ImportFailure(f"target TM line {line_number} must be an object")
        record = cast(dict[str, object], raw_record)
        source = record.get("source")
        target = record.get("target")
        if not isinstance(source, str) or not source.strip():
            raise ImportFailure(f"target TM line {line_number} has no source text")
        if not isinstance(target, str) or not target.strip():
            raise ImportFailure(f"target TM line {line_number} has no target text")
        records[source] = record
    return records


def _read_existing_terms(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    staged = _stage_termbase(path, format_id=TERMBASE_CSV_V1)
    rejected = tuple(
        issue
        for issue in staged.warnings
        if issue.code not in _TERMBASE_EXISTING_ALLOWED_WARNING_CODES
    )
    if rejected:
        raise ImportFailure(
            "managed termbase contains an invalid source/target row"
        )
    terms: dict[str, str] = {}
    for record in staged.records:
        terms[record.source] = record.target
    return terms


def _render_terms(terms: dict[str, str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerows(terms.items())
    return buffer.getvalue()


def _atomic_write_text(path: Path, content: str, encoding: str) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            _ = handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise ImportFailure(f"unable to replace target resource '{path}': {exc}") from exc


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "terms.csv"
        target = root / "terms-target.csv"
        source.write_text("Source,Target\nEngine,引擎\n", encoding="utf-8-sig")
        assert import_termbase(source, target).imported == 1
    print("Resource importer self-test passed.")
