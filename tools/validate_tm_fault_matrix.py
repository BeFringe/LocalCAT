#!/usr/bin/env python3
"""Execute the closed Feature 5 fault matrix and emit fresh evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
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


def _source_file_digests(root: Path) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = []
    resolved_root = root.resolve(strict=True)
    for relative in fault_matrix_source_paths():
        path = (resolved_root / relative).resolve(strict=True)
        if resolved_root not in path.parents or not path.is_file():
            raise ValueError("fault-matrix source path escaped the repository")
        facts.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(facts)


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


def _atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tm-fault-matrix-",
        suffix=".tmp",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        parent_descriptor = os.open(parent, flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository_root = arguments.repository_root.resolve(strict=True)
    evidence_path = arguments.emit
    if not evidence_path.is_absolute():
        evidence_path = repository_root / evidence_path
    evidence_path = evidence_path.absolute()
    if evidence_path.parent.resolve(strict=True) != repository_root:
        raise ValueError("fault matrix evidence must be emitted at repository root")

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

    registry_digest = fault_matrix_registry_digest()
    source_files = _source_file_digests(repository_root)
    source_fingerprint = fault_matrix_source_fingerprint(
        registry_digest,
        source_files,
    )
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
    _atomic_write(
        evidence_path,
        (_canonical_json(evidence) + "\n").encode("utf-8"),
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
