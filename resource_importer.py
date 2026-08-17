"""Safe, atomic importers for LocalCAT translation resources."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, cast
from uuid import uuid4

from editor_contracts import ImportReport
from tm_contracts import TMRecordDraft
from tm_engine import open_canonical_tm_store
from tm_sqlite_store import (
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
)


LOGGER = logging.getLogger(__name__)
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_SEGMENT_CHARS = 1_000_000
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
SOURCE_HEADERS = frozenset({"source", "source term", "source text", "原文", "源术语"})
TARGET_HEADERS = frozenset({"target", "target term", "target text", "translation", "译文", "目标术语"})


class ImportFailure(RuntimeError):
    """Internal all-or-nothing import failure."""


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
        source_language = _normalize_locale(source_locale)
        target_language = _normalize_locale(target_locale)
        if source_language == target_language:
            raise ImportFailure("source and target locales must be different")
        source = _validate_input(input_path, {".tmx"})
        target = target_path.expanduser().resolve()
        source_bytes = _read_tmx_snapshot(source)
        incoming, ordered_units, skipped, warnings, duplicate_count = _parse_tmx(
            io.BytesIO(source_bytes),
            source_language,
            target_language,
        )
        if not incoming:
            raise ImportFailure(
                "TMX contains no valid units for "
                f"{source_locale.strip()} → {target_locale.strip()}"
            )
        canonical = open_canonical_tm_store(target)
        if canonical is not None:
            source_digest = hashlib.sha256(source_bytes).hexdigest()
            drafts = tuple(
                _tmx_import_draft(source_text, target_text, source.name)
                for source_text, target_text in ordered_units
            )
            try:
                canonical.append_batch(
                    batch_id=f"import.{uuid4().hex}",
                    kind="import",
                    drafts=drafts,
                    source_digest=source_digest,
                    source_path=source,
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
            LOGGER.info(
                "Imported %d canonical TM entries from %s",
                len(drafts),
                source,
            )
            return ImportReport(
                imported=len(drafts),
                skipped=skipped,
                overwritten=0,
                errors=tuple(warnings),
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
            errors=tuple(warnings),
        )
    except (
        ImportFailure,
        OSError,
        UnicodeError,
        ET.ParseError,
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
        rows = _read_termbase_rows(source)
        incoming, skipped, duplicate_count = _collect_terms(rows)
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
    path = input_path.expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ImportFailure(f"input file does not exist: {path}")
    if path.suffix.lower() not in suffixes:
        supported = ", ".join(sorted(suffixes))
        raise ImportFailure(f"unsupported import format; expected one of: {supported}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImportFailure(f"unable to inspect input file '{path}': {exc}") from exc
    if size > MAX_INPUT_BYTES:
        raise ImportFailure("input exceeds the 100 MB safety limit")
    return path


def _read_tmx_snapshot(path: Path) -> bytes:
    """Read one bounded immutable TMX snapshot for validation and parsing."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImportFailure(f"unable to read TMX '{path}': {exc}") from exc
    if len(data) > MAX_INPUT_BYTES:
        raise ImportFailure("input exceeds the 100 MB safety limit")
    declarations = data.upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise ImportFailure("TMX containing DTD or ENTITY declarations is not supported")
    return data


def _normalize_locale(locale: str) -> str:
    normalized = locale.strip().replace("_", "-").lower()
    pieces = [piece for piece in normalized.split("-") if piece]
    if not pieces or any(not piece.isalnum() for piece in pieces):
        raise ImportFailure(f"invalid locale: {locale!r}")
    return "-".join(pieces)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    wanted = name.lower()
    return next((child for child in element if _local_name(child.tag) == wanted), None)


def _tuv_language(element: ET.Element) -> str | None:
    raw = element.attrib.get(XML_LANG) or element.attrib.get("lang")
    if raw is None or not raw.strip():
        return None
    try:
        return _normalize_locale(raw)
    except ImportFailure:
        return None


def _select_locale(
    entries: list[tuple[str, str]],
    requested: str,
) -> tuple[str | None, str | None]:
    exact = [text for locale, text in entries if locale == requested]
    if exact:
        return exact[-1], None
    base = requested.split("-", 1)[0]
    candidates = [(locale, text) for locale, text in entries if locale.split("-", 1)[0] == base]
    locales = {locale for locale, _ in candidates}
    if len(locales) == 1:
        return candidates[-1][1], None
    if len(locales) > 1:
        return None, f"ambiguous base-language fallback for '{requested}'"
    return None, f"language '{requested}' not found"


def _parse_tmx(
    source: io.BytesIO,
    source_locale: str,
    target_locale: str,
) -> tuple[
    dict[str, dict[str, object]],
    tuple[tuple[str, str], ...],
    int,
    list[str],
    int,
]:
    incoming: dict[str, dict[str, object]] = {}
    ordered_units: list[tuple[str, str]] = []
    skipped = 0
    duplicate_count = 0
    warnings: list[str] = []
    total_units = 0
    root: ET.Element | None = None

    for event, element in ET.iterparse(source, events=("start", "end")):
        if root is None and event == "start":
            root = element
        if event != "end" or _local_name(element.tag) != "tu":
            continue
        total_units += 1
        entries: list[tuple[str, str]] = []
        inline_tag = False
        for tuv in (child for child in element if _local_name(child.tag) == "tuv"):
            locale = _tuv_language(tuv)
            segment = _direct_child(tuv, "seg")
            if locale is None or segment is None:
                continue
            if len(segment):
                inline_tag = True
                break
            text = (segment.text or "").strip()
            if len(text) > MAX_SEGMENT_CHARS:
                inline_tag = True
                warnings.append(
                    f"TMX unit {total_units} skipped: segment exceeds the safety length limit"
                )
                break
            if text:
                entries.append((locale, text))
        if inline_tag:
            skipped += 1
            if not warnings or not warnings[-1].startswith(f"TMX unit {total_units} skipped:"):
                warnings.append(f"TMX unit {total_units} skipped: inline XML tags are not supported")
        else:
            source_text, source_error = _select_locale(entries, source_locale)
            target_text, target_error = _select_locale(entries, target_locale)
            if source_text is None or target_text is None:
                skipped += 1
                if source_error and "ambiguous" in source_error:
                    warnings.append(f"TMX unit {total_units} skipped: {source_error}")
                elif target_error and "ambiguous" in target_error:
                    warnings.append(f"TMX unit {total_units} skipped: {target_error}")
            else:
                ordered_units.append((source_text, target_text))
                if source_text in incoming:
                    duplicate_count += 1
                incoming[source_text] = {"source": source_text, "target": target_text}
        element.clear()
        if root is not None and element is not root:
            root.clear()

    if total_units == 0:
        raise ImportFailure("TMX contains no translation units")
    return incoming, tuple(ordered_units), skipped, warnings, duplicate_count


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


def _read_termbase_rows(path: Path) -> Iterable[tuple[object, ...]]:
    if path.suffix.lower() == ".csv":
        return _read_csv_rows(path)
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportFailure("XLSX import requires openpyxl 3.1 or newer") from exc
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet is None:
                raise ImportFailure("XLSX termbase has no active worksheet")
            return list(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
    except Exception as exc:
        raise ImportFailure(f"unable to read XLSX termbase: {exc}") from exc


def _read_csv_rows(path: Path) -> list[tuple[str, ...]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [tuple(row) for row in csv.reader(handle)]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ImportFailure(f"unable to read CSV termbase: {exc}") from exc


def _read_existing_terms(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    terms: dict[str, str] = {}
    for line_number, row in enumerate(_read_csv_rows(path), start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) < 2:
            raise ImportFailure(f"target termbase row {line_number} has fewer than two columns")
        source = row[0].strip()
        target = row[1].strip()
        if _is_header(source, target):
            continue
        if not source or not target:
            raise ImportFailure(f"target termbase row {line_number} has an empty term")
        terms[source] = target
    return terms


def _collect_terms(
    rows: Iterable[tuple[object, ...] | list[object]],
) -> tuple[dict[str, str], int, int]:
    terms: dict[str, str] = {}
    skipped = 0
    duplicate_count = 0
    for row in rows:
        if len(row) < 2:
            skipped += 1
            continue
        source = "" if row[0] is None else str(row[0]).strip()
        target = "" if row[1] is None else str(row[1]).strip()
        if _is_header(source, target):
            skipped += 1
            continue
        if not source or not target:
            skipped += 1
            continue
        if source in terms:
            duplicate_count += 1
        terms[source] = target
    return terms, skipped, duplicate_count


def _is_header(source: str, target: str) -> bool:
    return source.casefold() in SOURCE_HEADERS and target.casefold() in TARGET_HEADERS


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
