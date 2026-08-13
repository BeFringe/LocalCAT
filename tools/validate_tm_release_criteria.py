#!/usr/bin/env python3
"""Build the closed 86-criterion Feature 5 release decision."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Protocol, cast
import unittest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tests.acceptance_matrix_registry import (  # noqa: E402
    ACCEPTANCE_MATRIX_ROWS,
    ACCEPTANCE_MATRIX_SCHEMA_VERSION,
    acceptance_matrix_registry_digest,
    acceptance_matrix_source_fingerprint,
    acceptance_matrix_source_paths,
)
from tests.fault_matrix_registry import (  # noqa: E402
    FAULT_MATRIX_ROWS,
    FAULT_MATRIX_SCHEMA_VERSION,
    fault_matrix_registry_digest,
    fault_matrix_source_fingerprint,
    fault_matrix_source_paths,
)
from tests.release_criteria_registry import (  # noqa: E402
    RELEASE_CRITERIA_BINDINGS,
    RELEASE_CRITERIA_SCHEMA_VERSION,
    parse_requirement_criteria,
    release_criteria_registry_digest,
)
from tm_benchmark_gate import (  # noqa: E402
    BenchmarkEvidenceBundle,
    benchmark_evidence_bundle_from_json,
)


_REQUIREMENTS_PATH = ".kiro/specs/tm-storage-retrieval-index/requirements.md"
_ACCEPTANCE_EVIDENCE_PATH = "acceptance_matrix_evidence.json"
_FAULT_EVIDENCE_PATH = "fault_matrix_evidence.json"
_BENCHMARK_EVIDENCE_PATH = "benchmark_tm_evidence.json"
_RELEASE_EVIDENCE_PATH = "release_criteria_evidence.json"


class _EvidenceRow(Protocol):
    @property
    def row_id(self) -> str: ...

    @property
    def test_ids(self) -> tuple[str, ...]: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate all 86 Feature 5 acceptance criteria.",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=_REPOSITORY_ROOT / _RELEASE_EVIDENCE_PATH,
    )
    parser.add_argument(
        "--require-go",
        action="store_true",
        help="return non-zero when the truthful release decision is NO_GO",
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


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_strict_json(raw: str) -> dict[str, object]:
    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token: {token}")
        ),
    )
    if type(value) is not dict:
        raise TypeError("evidence root must be an object")
    return cast(dict[str, object], value)


def _canonical_relative(relative: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("release evidence path is not canonical")
    return path


def _read_strict_regular(root: Path, relative: str) -> tuple[bytes, str]:
    """Read one file through a root-to-file no-follow descriptor walk."""

    relative_path = _canonical_relative(relative)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    directory_descriptor = os.open(root, directory_flags)
    file_descriptor = -1
    try:
        for component in relative_path.parts[:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            observed = os.fstat(next_descriptor)
            if not stat.S_ISDIR(observed.st_mode):
                os.close(next_descriptor)
                raise ValueError("release evidence parent is not a directory")
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        filename = relative_path.parts[-1]
        file_descriptor = os.open(
            filename,
            file_flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("release evidence source is not regular")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        terminal = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(terminal.st_mode)
            or (terminal.st_dev, terminal.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("release evidence source identity changed")
        return b"".join(chunks), digest.hexdigest()
    except OSError as error:
        raise ValueError(
            "release evidence source is not no-follow regular"
        ) from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(directory_descriptor)


def _validate_evidence_target(path: Path) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(observed.st_mode):
        raise ValueError("release evidence target is not regular")


def _matrix_row_map(
    rows: Sequence[_EvidenceRow],
) -> dict[str, _EvidenceRow]:
    result = {row.row_id: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("matrix registry row ids are not unique")
    return result


def _validate_matrix_evidence(
    *,
    root: Path,
    relative: str,
    schema_version: str,
    rows: Sequence[_EvidenceRow],
    registry_digest: str,
    source_paths: tuple[str, ...],
    source_fingerprint: Callable[
        [str, tuple[tuple[str, str], ...]], str
    ],
) -> tuple[dict[str, str], str, str]:
    raw, evidence_digest = _read_strict_regular(root, relative)
    evidence = _parse_strict_json(raw.decode("utf-8"))
    if set(evidence) != {
        "generated_at_utc",
        "registry_digest",
        "rows",
        "schema_version",
        "source_files",
        "source_fingerprint",
        "summary",
        "tasks",
    }:
        raise ValueError("matrix evidence fields are not closed")
    if evidence["schema_version"] != schema_version:
        raise ValueError("matrix evidence schema is stale")
    if evidence["registry_digest"] != registry_digest:
        raise ValueError("matrix registry digest is stale")

    raw_sources = evidence["source_files"]
    if type(raw_sources) is not list:
        raise TypeError("matrix source files must be a list")
    observed_sources: list[tuple[str, str]] = []
    for raw_source in raw_sources:
        if type(raw_source) is not dict or set(raw_source) != {"path", "sha256"}:
            raise ValueError("matrix source fact is invalid")
        source = cast(dict[str, object], raw_source)
        path = source["path"]
        digest = source["sha256"]
        if type(path) is not str or type(digest) is not str:
            raise TypeError("matrix source fact must contain strings")
        _bytes, actual_digest = _read_strict_regular(root, path)
        if digest != actual_digest:
            raise ValueError("matrix source digest is stale")
        observed_sources.append((path, digest))
    if tuple(path for path, _digest in observed_sources) != source_paths:
        raise ValueError("matrix source inventory is stale")
    if evidence["source_fingerprint"] != source_fingerprint(
        registry_digest,
        tuple(observed_sources),
    ):
        raise ValueError("matrix source fingerprint is stale")

    raw_rows = evidence["rows"]
    if type(raw_rows) is not list or len(raw_rows) != len(rows):
        raise ValueError("matrix evidence row inventory is stale")
    observed_status: dict[str, str] = {}
    for expected, raw_row in zip(rows, raw_rows):
        if type(raw_row) is not dict or set(raw_row) != {
            "row_id",
            "status",
            "test_ids",
        }:
            raise ValueError("matrix row evidence is invalid")
        row = cast(dict[str, object], raw_row)
        if row["row_id"] != expected.row_id:
            raise ValueError("matrix row identity is stale")
        if row["test_ids"] != list(expected.test_ids):
            raise ValueError("matrix row tests are stale")
        if row["status"] != "PASS":
            raise ValueError("matrix evidence contains a failing row")
        observed_status[expected.row_id] = "PASS"
    source_digest = evidence["source_fingerprint"]
    if type(source_digest) is not str:
        raise TypeError("matrix source fingerprint must be a string")
    return observed_status, evidence_digest, source_digest


def _flatten(suite: unittest.TestSuite) -> tuple[unittest.TestCase, ...]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten(item))
        elif isinstance(item, unittest.TestCase):
            tests.append(item)
        else:
            raise TypeError("unittest loader returned an invalid item")
    return tuple(tests)


def _run_direct_tests(test_ids: tuple[str, ...]) -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_id in test_ids:
        loaded = loader.loadTestsFromName(test_id)
        resolved = _flatten(loaded)
        if len(resolved) != 1 or resolved[0].id() != test_id:
            raise ValueError(f"direct evidence test does not resolve: {test_id}")
        suite.addTest(loaded)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    if not result.wasSuccessful():
        sys.stderr.write(output.getvalue())
        return False
    return True


def _benchmark_claim_statuses(
    bundle: BenchmarkEvidenceBundle,
) -> dict[str, str]:
    reports = bundle.suite_report.path_reports
    contract = bundle.contract
    environments = tuple(dict(report.environment) for report in reports)
    required_environment = {
        "cpu",
        "os",
        "python_version",
        "ram_mib",
        "sqlite_version",
        "unicode_version",
    }
    statuses = {
        "CANDIDATE_RECALL": (
            "PASS"
            if all(
                report.candidate_recall >= contract.candidate_recall_gate
                for report in reports
            )
            else "BLOCKED"
        ),
        "ENVIRONMENT": (
            "PASS"
            if all(required_environment.issubset(environment) for environment in environments)
            else "BLOCKED"
        ),
        "EXACT_P95": (
            "PASS"
            if all(report.exact_p95_ms <= contract.exact_p95_gate_ms for report in reports)
            else "BLOCKED"
        ),
        "FAILURE_REPORT": (
            "PASS"
            if (
                not bundle.suite_report.passed
                and bool(bundle.suite_report.failed_paths)
                and all(report.failed_gates for report in reports if not report.passed)
            )
            else "BLOCKED"
        ),
        "FUZZY_P95": (
            "PASS"
            if all(
                report.fuzzy_top10_p95_ms <= contract.fuzzy_p95_gate_ms
                for report in reports
            )
            else "BLOCKED"
        ),
        "METRICS": (
            "PASS"
            if all(
                report.exact_sample_count == contract.exact_cohort_count
                and report.fuzzy_sample_count == contract.fuzzy_cohort_count
                and report.oracle_query_count == contract.oracle_query_count
                for report in reports
            )
            else "BLOCKED"
        ),
        "MIGRATION": (
            "PASS"
            if all(
                report.migration_seconds <= contract.migration_gate_seconds
                for report in reports
            )
            else "BLOCKED"
        ),
        "PEAK_RSS": (
            "PASS"
            if all(report.peak_rss_mib <= contract.peak_rss_gate_mib for report in reports)
            else "BLOCKED"
        ),
    }
    return statuses


def _benchmark_blockers(bundle: BenchmarkEvidenceBundle) -> tuple[str, ...]:
    return tuple(
        f"{report.execution_path.value}.{gate}"
        for report in bundle.suite_report.path_reports
        for gate in report.failed_gates
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tm-release-criteria-",
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
    repository_root = arguments.repository_root.absolute()
    if repository_root != _REPOSITORY_ROOT:
        raise ValueError("release repository root must match validator checkout")
    if repository_root.resolve(strict=True) != _REPOSITORY_ROOT:
        raise ValueError("release repository root is not canonical")
    evidence_path = arguments.emit
    if not evidence_path.is_absolute():
        evidence_path = repository_root / evidence_path
    evidence_path = evidence_path.absolute()
    canonical_evidence_path = repository_root / _RELEASE_EVIDENCE_PATH
    if evidence_path != canonical_evidence_path:
        raise ValueError("release evidence must use the canonical output path")
    _validate_evidence_target(evidence_path)

    requirements_bytes, requirements_digest = _read_strict_regular(
        repository_root,
        _REQUIREMENTS_PATH,
    )
    criteria = parse_requirement_criteria(requirements_bytes.decode("utf-8"))
    criterion_ids = tuple(item.criterion_id for item in criteria)
    binding_ids = tuple(item.criterion_id for item in RELEASE_CRITERIA_BINDINGS)
    if len(criteria) != 86 or binding_ids != criterion_ids:
        raise ValueError("release registry must exactly bind all 86 criteria")

    acceptance_status, acceptance_digest, acceptance_fingerprint = (
        _validate_matrix_evidence(
            root=repository_root,
            relative=_ACCEPTANCE_EVIDENCE_PATH,
            schema_version=ACCEPTANCE_MATRIX_SCHEMA_VERSION,
            rows=ACCEPTANCE_MATRIX_ROWS,
            registry_digest=acceptance_matrix_registry_digest(),
            source_paths=acceptance_matrix_source_paths(),
            source_fingerprint=acceptance_matrix_source_fingerprint,
        )
    )
    fault_status, fault_digest, fault_fingerprint = _validate_matrix_evidence(
        root=repository_root,
        relative=_FAULT_EVIDENCE_PATH,
        schema_version=FAULT_MATRIX_SCHEMA_VERSION,
        rows=FAULT_MATRIX_ROWS,
        registry_digest=fault_matrix_registry_digest(),
        source_paths=fault_matrix_source_paths(),
        source_fingerprint=fault_matrix_source_fingerprint,
    )
    benchmark_bytes, benchmark_digest = _read_strict_regular(
        repository_root,
        _BENCHMARK_EVIDENCE_PATH,
    )
    benchmark_bundle = benchmark_evidence_bundle_from_json(
        benchmark_bytes.decode("utf-8")
    )
    benchmark_status = _benchmark_claim_statuses(benchmark_bundle)

    acceptance_rows = _matrix_row_map(ACCEPTANCE_MATRIX_ROWS)
    fault_rows = _matrix_row_map(FAULT_MATRIX_ROWS)
    direct_test_ids = tuple(
        dict.fromkeys(
            evidence_ref.partition(":")[2]
            for binding in RELEASE_CRITERIA_BINDINGS
            for evidence_ref in binding.evidence_refs
            if evidence_ref.startswith("test:")
        )
    )
    if not _run_direct_tests(direct_test_ids):
        return 1

    row_results: list[dict[str, object]] = []
    blocked_criteria: list[str] = []
    for criterion, binding in zip(criteria, RELEASE_CRITERIA_BINDINGS):
        reference_statuses: list[str] = []
        for evidence_ref in binding.evidence_refs:
            kind, _separator, value = evidence_ref.partition(":")
            if kind == "acceptance":
                if value not in acceptance_rows:
                    raise ValueError("release references unknown acceptance row")
                reference_statuses.append(acceptance_status[value])
            elif kind == "fault":
                if value not in fault_rows:
                    raise ValueError("release references unknown fault row")
                reference_statuses.append(fault_status[value])
            elif kind == "test":
                if value not in direct_test_ids:
                    raise ValueError("release references an unexecuted test")
                reference_statuses.append("PASS")
            elif kind == "benchmark":
                reference_statuses.append(benchmark_status[value])
            else:
                raise ValueError("release evidence reference kind is invalid")
        status = (
            "PASS"
            if all(item == "PASS" for item in reference_statuses)
            else "BLOCKED"
        )
        if status == "BLOCKED":
            blocked_criteria.append(criterion.criterion_id)
        row_results.append(
            {
                "criterion_id": criterion.criterion_id,
                "criterion_text_digest": hashlib.sha256(
                    criterion.text.encode("utf-8")
                ).hexdigest(),
                "evidence_refs": list(binding.evidence_refs),
                "status": status,
            }
        )

    benchmark_blockers = _benchmark_blockers(benchmark_bundle)
    release_decision = (
        "GO" if not blocked_criteria and not benchmark_blockers else "NO_GO"
    )
    registry_digest = release_criteria_registry_digest()
    source_fingerprint = hashlib.sha256(
        _canonical_json(
            {
                "acceptance_evidence_sha256": acceptance_digest,
                "acceptance_source_fingerprint": acceptance_fingerprint,
                "benchmark_bundle_digest": benchmark_bundle.bundle_digest,
                "benchmark_evidence_sha256": benchmark_digest,
                "fault_evidence_sha256": fault_digest,
                "fault_source_fingerprint": fault_fingerprint,
                "registry_digest": registry_digest,
                "requirements_sha256": requirements_digest,
            }
        ).encode("utf-8")
    ).hexdigest()
    evidence = {
        "benchmark_blockers": list(benchmark_blockers),
        "blocked_criteria": blocked_criteria,
        "generated_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "input_evidence": {
            "acceptance_evidence_sha256": acceptance_digest,
            "acceptance_source_fingerprint": acceptance_fingerprint,
            "benchmark_bundle_digest": benchmark_bundle.bundle_digest,
            "benchmark_evidence_sha256": benchmark_digest,
            "fault_evidence_sha256": fault_digest,
            "fault_source_fingerprint": fault_fingerprint,
            "requirements_sha256": requirements_digest,
        },
        "registry_digest": registry_digest,
        "release_decision": release_decision,
        "rows": row_results,
        "schema_version": RELEASE_CRITERIA_SCHEMA_VERSION,
        "source_fingerprint": source_fingerprint,
        "summary": {
            "blocked_criteria": len(blocked_criteria),
            "direct_tests": len(direct_test_ids),
            "mapped_criteria": len(row_results),
            "passed_criteria": len(row_results) - len(blocked_criteria),
            "total_criteria": len(criteria),
        },
    }
    _atomic_write(
        evidence_path,
        (_canonical_json(evidence) + "\n").encode("utf-8"),
    )
    print(
        _canonical_json(
            {
                "blocked_criteria": blocked_criteria,
                "evidence": evidence_path.name,
                "mapped_criteria": len(row_results),
                "release_decision": release_decision,
                "source_fingerprint": source_fingerprint,
            }
        )
    )
    if arguments.require_go and release_decision != "GO":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
