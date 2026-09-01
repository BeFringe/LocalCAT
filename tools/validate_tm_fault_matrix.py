#!/usr/bin/env python3
"""Execute the closed Feature 5 fault matrix and emit fresh evidence."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tests.fault_matrix_registry import (  # noqa: E402
    FAULT_MATRIX_ROWS,
    FAULT_MATRIX_SCHEMA_VERSION,
    fault_matrix_registry_digest,
    fault_matrix_source_fingerprint,
    fault_matrix_source_paths,
)
from tools.tm_release_evidence_io import (  # noqa: E402
    atomic_write as _platform_atomic_write,
    strict_read_regular as _platform_strict_read_regular,
    validate_evidence_target as _platform_validate_evidence_target,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute exact unittest ids in the Feature 5 fault matrix.",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=_REPOSITORY_ROOT / "fault_matrix_evidence.json",
    )
    return parser


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_source_relative(relative: str) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ValueError("fault-matrix source path is not canonical")
    return relative_path


def _strict_source_digest(root: Path, relative: str) -> str:
    relative_path = _canonical_source_relative(relative)
    return _platform_strict_read_regular(
        root,
        relative_path,
        parent_error="fault-matrix source parent is not a real directory",
        nonregular_error="fault-matrix source is not a regular file",
        source_error="fault-matrix source is not no-follow regular",
        identity_error="fault-matrix source identity changed",
    )[1]


def _strict_source_file(root: Path, relative: str) -> Path:
    """Validate one source and return its canonical lexical path for tests."""

    relative_path = _canonical_source_relative(relative)
    _strict_source_digest(root, relative)
    return root / relative_path


def _source_file_digests(root: Path) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = []
    for relative in fault_matrix_source_paths():
        facts.append((relative, _strict_source_digest(root, relative)))
    return tuple(facts)


def _validate_evidence_target(path: Path) -> None:
    """Reject aliases and non-regular existing canonical evidence targets."""

    _platform_validate_evidence_target(
        path,
        target_error="fault matrix evidence target is not a regular file",
    )


def _run_row(row_id: str, test_ids: tuple[str, ...]) -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromName(test_id) for test_id in test_ids
    )
    output = io.StringIO()
    result = unittest.TextTestRunner(
        stream=output,
        verbosity=0,
    ).run(suite)
    if not result.wasSuccessful():
        sys.stderr.write(f"{row_id}: referenced fault tests failed\n")
        sys.stderr.write(output.getvalue())
        return False
    return True


def _atomic_write(
    path: Path,
    payload: bytes,
    validate_snapshot: Callable[[], None],
) -> None:
    _platform_atomic_write(
        path,
        payload,
        validate_snapshot,
        candidate_prefix=".tm-fault-matrix-",
        identity_error="fault matrix evidence identity changed",
        readback_error="fault matrix evidence readback changed",
        platform_error="fault matrix evidence platform I/O failed",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository_root = arguments.repository_root.absolute()
    if repository_root != _REPOSITORY_ROOT:
        raise ValueError("fault matrix repository root must match the validator checkout")
    if repository_root.resolve(strict=True) != _REPOSITORY_ROOT:
        raise ValueError("fault matrix repository root is not canonical")
    evidence_path = arguments.emit
    if not evidence_path.is_absolute():
        evidence_path = repository_root / evidence_path
    evidence_path = evidence_path.absolute()
    canonical_evidence_path = repository_root / "fault_matrix_evidence.json"
    if evidence_path != canonical_evidence_path:
        raise ValueError("fault matrix evidence must use the canonical output path")
    _validate_evidence_target(evidence_path)

    registry_digest = fault_matrix_registry_digest()
    source_files = _source_file_digests(repository_root)
    source_fingerprint = fault_matrix_source_fingerprint(
        registry_digest,
        source_files,
    )

    row_results: list[dict[str, object]] = []
    for row in FAULT_MATRIX_ROWS:
        if not _run_row(row.row_id, row.test_ids):
            return 1
        row_results.append(
            {
                "row_id": row.row_id,
                "status": "PASS",
                "test_ids": list(row.test_ids),
            }
        )
    source_files_after = _source_file_digests(repository_root)
    if source_files_after != source_files:
        raise ValueError("fault-matrix sources changed during validation")

    evidence = {
        "generated_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "registry_digest": registry_digest,
        "rows": row_results,
        "schema_version": FAULT_MATRIX_SCHEMA_VERSION,
        "source_files": [
            {"path": path, "sha256": digest}
            for path, digest in source_files
        ],
        "source_fingerprint": source_fingerprint,
        "summary": {
            "passed_rows": len(row_results),
            "referenced_tests": sum(
                len(row.test_ids) for row in FAULT_MATRIX_ROWS
            ),
            "total_rows": len(FAULT_MATRIX_ROWS),
        },
        "tasks": sorted({row.task for row in FAULT_MATRIX_ROWS}),
    }
    def validate_snapshot() -> None:
        if _source_file_digests(repository_root) != source_files:
            raise ValueError("fault-matrix sources changed before emit")

    _atomic_write(
        evidence_path,
        (_canonical_json(evidence) + "\n").encode("utf-8"),
        validate_snapshot,
    )
    print(
        _canonical_json(
            {
                "evidence": evidence_path.name,
                "passed_rows": len(row_results),
                "source_fingerprint": source_fingerprint,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
