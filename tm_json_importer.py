"""Normalized TM JSON CLI facade.

Single-input grammar belongs to the Parser codec. This module owns only input
discovery, per-file failure policy, cross-file source LWW, and JSONL output.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import cast

from parser_composition import create_parser_application_surface
from parser_contracts import (
    ContractViolation,
    EffectivePurpose,
    NORMALIZED_TM_JSON_V1,
    ParseIssue,
    ReadRequest,
    ResourceRecord,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
)


BASE_DIR = Path(__file__).absolute().parent


class TMJSONImportError(Exception):
    """Stable, body-safe CLI facade failure."""

    def __init__(self, code: str, safe_summary: str) -> None:
        self.code = code
        self.safe_summary = safe_summary
        super().__init__(f"{code}: {safe_summary}")


class TMJSONInputPathError(ValueError):
    """Expected CLI path-selection failure."""


class _TMJSONBatchPolicy(Enum):
    CONTINUE = "continue"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class _TMJSONFileOutcome:
    input_file: Path
    records: tuple[ResourceRecord, ...]
    issues: tuple[ParseIssue, ...]
    failure_code: str | None
    failure_summary: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.input_file, Path):
            raise TypeError("input_file must be a Path")
        if type(self.records) is not tuple or type(self.issues) is not tuple:
            raise TypeError("per-file records and issues must be immutable tuples")
        if (self.failure_code is None) != (self.failure_summary is None):
            raise ValueError("failure code and summary must be present together")
        if self.failure_code is not None and self.records:
            raise ValueError("a failed file outcome cannot carry accepted records")

    @property
    def succeeded(self) -> bool:
        return self.failure_code is None


@dataclass(frozen=True, slots=True)
class _TMJSONBatchRecord:
    source: str
    target: str
    speaker: str
    file_source: str

    def as_json_record(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "speaker": self.speaker,
            "file_source": self.file_source,
        }


@dataclass(frozen=True, slots=True)
class _TMJSONBatchResult:
    file_outcomes: tuple[_TMJSONFileOutcome, ...]
    records: tuple[_TMJSONBatchRecord, ...]

    def __post_init__(self) -> None:
        if type(self.file_outcomes) is not tuple or type(self.records) is not tuple:
            raise TypeError("batch outcomes and records must be immutable tuples")

    @property
    def successful_file_count(self) -> int:
        return sum(outcome.succeeded for outcome in self.file_outcomes)

    def as_ordered_dict(self) -> OrderedDict[str, dict[str, str]]:
        return OrderedDict(
            (record.source, record.as_json_record()) for record in self.records
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import normalized TM JSON into the JSONL format used by LocalCAT Phase-3."
    )
    _ = parser.add_argument(
        "--input",
        nargs="+",
        required=True,
        help="One or more normalized TM JSON files or directories containing JSON files.",
    )
    _ = parser.add_argument(
        "--output",
        default=str(BASE_DIR / "tm.jsonl"),
        help="Output JSONL path. Defaults to CAT/tm.jsonl.",
    )
    return parser.parse_args()


def _absolute_without_resolving(path: Path) -> Path:
    """Make a lexical absolute path without following a source symlink."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _entry_mode(path: Path) -> int:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        raise FileNotFoundError(f"Input path not found: {path}") from None


def resolve_input_files(raw_inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_input in raw_inputs:
        path = _absolute_without_resolving(Path(raw_input))
        mode = _entry_mode(path)
        if stat.S_ISLNK(mode):
            raise TMJSONInputPathError(f"Unsupported input path: {path}")
        if stat.S_ISDIR(mode):
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if child.suffix.lower() != ".json":
                    continue
                child_mode = _entry_mode(child)
                if stat.S_ISLNK(child_mode):
                    raise TMJSONInputPathError(f"Unsupported input path: {child}")
                if stat.S_ISREG(child_mode):
                    files.append(child)
        elif stat.S_ISREG(mode) and path.suffix.lower() == ".json":
            files.append(path)
        else:
            raise TMJSONInputPathError(f"Unsupported input path: {path}")
    if not files:
        raise TMJSONInputPathError("No JSON files found in the provided input paths.")
    return files


def _read_one_file(input_file: Path) -> _TMJSONFileOutcome:
    source = _absolute_without_resolving(input_file)
    surface = create_parser_application_surface()
    selection = SelectionRequest(
        purpose=EffectivePurpose.TRANSLATION_MEMORY,
        format_id=NORMALIZED_TM_JSON_V1,
    )
    request = ReadRequest(
        purpose=EffectivePurpose.TRANSLATION_MEMORY,
        format_id=NORMALIZED_TM_JSON_V1,
    )
    try:
        opened = surface.open_input(
            SourceReference(
                safe_root=str(source.parent),
                selected_path=str(source),
                display_hint=source.name,
            ),
            selection,
            request,
        )
        if type(opened) is SelectionFailure:
            return _TMJSONFileOutcome(
                source,
                (),
                (),
                opened.code,
                opened.safe_summary,
            )
        with opened:
            materialized = opened.materialize()
    except ContractViolation as exc:
        return _TMJSONFileOutcome(source, (), (), exc.code, exc.safe_summary)
    except OSError:
        return _TMJSONFileOutcome(
            source,
            (),
            (),
            "PARSER.SOURCE.READ_FAILED",
            "normalized TM JSON input could not be read through the Parser surface",
        )

    records: list[ResourceRecord] = []
    for record in materialized.records:
        if type(record) is not ResourceRecord:
            raise TypeError("normalized TM JSON codec returned an incompatible record type")
        records.append(record)
    return _TMJSONFileOutcome(
        source,
        tuple(records),
        materialized.issues,
        None,
        None,
    )


def load_batch(
    input_files: list[Path],
    failure_policy: _TMJSONBatchPolicy,
) -> _TMJSONBatchResult:
    if type(failure_policy) is not _TMJSONBatchPolicy:
        raise TypeError("failure_policy must be an exact _TMJSONBatchPolicy")
    records_by_source: OrderedDict[str, _TMJSONBatchRecord] = OrderedDict()
    outcomes: list[_TMJSONFileOutcome] = []
    for input_file in input_files:
        outcome = _read_one_file(input_file)
        outcomes.append(outcome)
        if not outcome.succeeded:
            if failure_policy is _TMJSONBatchPolicy.STOP:
                break
            continue
        for parsed in outcome.records:
            record = _TMJSONBatchRecord(
                parsed.source,
                parsed.target,
                parsed.speaker.value,
                outcome.input_file.name,
            )
            if parsed.source in records_by_source:
                del records_by_source[parsed.source]
            records_by_source[parsed.source] = record
    return _TMJSONBatchResult(tuple(outcomes), tuple(records_by_source.values()))


def _require_batch_success(batch: _TMJSONBatchResult) -> None:
    if batch.successful_file_count != 0:
        return
    if batch.file_outcomes:
        failure = batch.file_outcomes[0]
        if failure.failure_code is None or failure.failure_summary is None:
            raise RuntimeError("batch success accounting is internally inconsistent")
        raise TMJSONImportError(failure.failure_code, failure.failure_summary)
    raise TMJSONImportError(
        "PARSER.SYNTAX.EMPTY_INPUT",
        "no normalized TM JSON input reached a verified terminal",
    )


def load_records(input_files: list[Path]) -> OrderedDict[str, dict[str, str]]:
    batch = load_batch(input_files, _TMJSONBatchPolicy.CONTINUE)
    _require_batch_success(batch)
    return batch.as_ordered_dict()


def _jsonl_bytes(records_by_source: OrderedDict[str, dict[str, str]]) -> bytes:
    return b"".join(
        (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        for record in records_by_source.values()
    )


def write_jsonl(
    output_path: Path,
    records_by_source: OrderedDict[str, dict[str, str]],
) -> None:
    payload = _jsonl_bytes(records_by_source)
    output_path = _absolute_without_resolving(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=".tm-json-import-",
        suffix=".tmp",
        dir=output_path.parent,
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    args = parse_args()
    try:
        input_paths = cast(list[str], args.input)
        output_raw = cast(str, args.output)
        input_files = resolve_input_files(input_paths)
        batch = load_batch(input_files, _TMJSONBatchPolicy.CONTINUE)
        _require_batch_success(batch)
        records_by_source = batch.as_ordered_dict()
        output_path = _absolute_without_resolving(Path(output_raw))
        write_jsonl(output_path, records_by_source)
    except (FileNotFoundError, TMJSONInputPathError, TMJSONImportError, OSError) as exc:
        code = getattr(exc, "code", "TM_JSON_IMPORT_FAILED")
        print(f"{code}: import failed", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Imported {batch.successful_file_count} JSON file(s).")
    print(f"Wrote {len(records_by_source)} TM records to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
