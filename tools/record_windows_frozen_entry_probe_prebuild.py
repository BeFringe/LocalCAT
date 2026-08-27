"""Record the pristine build-copy inputs before the Windows stock probe runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from tools.audit_w3_stock_bootloader import (
    EXPECTED_SDIST_SHA256,
    EXPECTED_SOURCE_SCAN_COUNT,
    EXPECTED_SOURCE_SCAN_SHA256,
    AuditInputError,
    audit_sdist_sources,
    audit_sources,
    sha256_file,
)
from tools.prepare_windows_frozen_entry_inputs import (
    CandidateInputError,
    PROBE_PREBUILD_EVIDENCE_SCHEMA,
    _canonical_json,
    _directory_input_fact,
)


def build_prebuild_evidence(args: argparse.Namespace) -> dict[str, Any]:
    source_copy = args.source_copy.resolve()
    try:
        source_audit = audit_sources(source_copy)
        archive_audit = audit_sdist_sources(args.sdist.resolve())
    except AuditInputError as exc:
        raise CandidateInputError(str(exc)) from exc
    if (
        sha256_file(args.sdist.resolve()) != EXPECTED_SDIST_SHA256
        or not source_audit["pinned_hashes_match"]
        or source_audit["source_scan"]["count"] != EXPECTED_SOURCE_SCAN_COUNT
        or source_audit["source_scan"]["sha256"] != EXPECTED_SOURCE_SCAN_SHA256
        or archive_audit["count"] != EXPECTED_SOURCE_SCAN_COUNT
        or archive_audit["sha256"] != EXPECTED_SOURCE_SCAN_SHA256
    ):
        raise CandidateInputError("probe pre-build source is not the pinned sdist source")

    payload: dict[str, Any] = {
        "schema": PROBE_PREBUILD_EVIDENCE_SCHEMA,
        "source": {
            "sdist_sha256": EXPECTED_SDIST_SHA256,
            "actual_build_copy_source_scan": source_audit["source_scan"],
            "actual_build_copy_build_driver": _directory_input_fact(
                source_copy / "bootloader",
                "PyInstaller bootloader build driver",
            ),
            "archive_source_scan": archive_audit,
        },
    }
    payload["evidence_digest"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    payload["status"] = "RECORDED"
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-copy", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        record = build_prebuild_evidence(args)
    except (CandidateInputError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"INVALID_INPUT: {exc}")
        return 2
    output = args.output.resolve()
    output.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"RECORDED: {record['evidence_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
