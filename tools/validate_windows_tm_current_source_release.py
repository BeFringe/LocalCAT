#!/usr/bin/env python3
"""Validate the Windows canonical-TM current-source release matrix."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
from typing import cast
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.windows_tm_current_source_registry import (  # noqa: E402
    WINDOWS_TM_CURRENT_SOURCE_ROWS,
    WindowsTMCurrentSourceRow,
    require_strict_success,
    resolve_row_tests,
    sorted_test_ids_sha256,
)
from tm_benchmark_gate import (  # noqa: E402
    BenchmarkEvidenceBundle,
    benchmark_evidence_bundle_from_json,
    benchmark_implementation_fingerprint,
)
from tools.run_windows_c3b_capability_gate import (  # noqa: E402
    validate_aggregate_matrix,
)
from tools.tm_release_evidence_io import (  # noqa: E402
    atomic_write as platform_atomic_write,
    strict_read_regular,
    validate_evidence_target,
)
from tools.windows_c3b_source_snapshot import source_snapshot_sha256  # noqa: E402


SCHEMA_VERSION = "localcat.windows-tm-current-source.v1"
EVIDENCE_NAME = "windows_tm_current_source_evidence.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_ROW_BY_ID = {row.row_id: row for row in WINDOWS_TM_CURRENT_SOURCE_ROWS}
_TM_SOURCE_PATHS = (
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
    "tm_application_composition.py",
    "tm_benchmark.py",
    "tm_benchmark_gate.py",
    "tm_benchmark_latency.py",
    "tm_benchmark_oracle.py",
    "tm_benchmark_platform_io.py",
    "tm_benchmark_process.py",
    "tm_benchmark_query_process.py",
    "tm_candidate_index.py",
    "tm_candidate_store_contracts.py",
    "tm_content_attestation.py",
    "tm_contracts.py",
    "tm_engine.py",
    "tm_gate_a.py",
    "tm_gate_b.py",
    "tm_json_importer.py",
    "tm_migration.py",
    "tm_resource_port.py",
    "tm_retrieval.py",
    "tm_retrieval_capability.py",
    "tm_retrieval_validation.py",
    "tm_schema_upgrade.py",
    "tm_similarity.py",
    "tm_snapshot_artifacts.py",
    "tm_snapshot_recovery.py",
    "tm_sqlite_candidate_projection.py",
    "tm_sqlite_store.py",
    "tm_stage_sealer.py",
)
_WINDOWS_TM_WORKER_PATHS = (
    "tests/windows_tm_current_source_registry.py",
    "tests/windows_tm_current_source_retrieval_worker.py",
    "tests/windows_tm_gate_d_attestation_worker.py",
    "tests/windows_tm_portable_publication_recovery_worker.py",
    "tests/windows_tm_portable_recovery_worker.py",
    "tests/windows_tm_schema_upgrade_worker.py",
    "tests/windows_tm_snapshot_recovery_worker.py",
)


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


def _parse_strict_json(raw: str, *, label: str) -> dict[str, object]:
    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token in {label}: {token}")
        ),
    )
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _canonical_relative(relative: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("current-source path is not canonical")
    return path


def _strict_read(root: Path, relative: str) -> tuple[bytes, str]:
    return strict_read_regular(
        root,
        _canonical_relative(relative),
        parent_error="current-source parent is not a directory",
        nonregular_error="current-source input is not regular",
        source_error="current-source input is not no-follow regular",
        identity_error="current-source input identity changed",
    )


def _registry_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rows": [
            {
                "expected_count": row.expected_count,
                "row_id": row.row_id,
                "selectors": list(row.selectors),
                "sorted_test_ids_sha256": row.sorted_test_ids_sha256,
            }
            for row in WINDOWS_TM_CURRENT_SOURCE_ROWS
        ],
    }


def registry_digest() -> str:
    return hashlib.sha256(
        _canonical_json(_registry_payload()).encode("utf-8")
    ).hexdigest()


def _selected_test_paths() -> tuple[str, ...]:
    paths = {
        ".".join(selector.split(".")[:2]).replace(".", "/") + ".py"
        for row in WINDOWS_TM_CURRENT_SOURCE_ROWS
        for selector in row.selectors
    }
    return tuple(sorted(paths))


def source_paths(root: Path = ROOT) -> tuple[str, ...]:
    """Return the bounded TM/platform/product evidence source inventory."""

    paths = {
        "benchmark_tm_contract.json",
        "capability_host.py",
        "platform_fs.py",
        "platform_fs_contracts.py",
        "platform_fs_windows.py",
        "tests/fixtures/retrieval_gate_c_roots_v1.json",
        "tests/test_validate_windows_tm_current_source_release.py",
        "tests/test_windows_tm_current_source_registry.py",
        "tests/windows_tm_current_source_registry.py",
        "tools/tm_release_evidence_io.py",
        "tools/run_windows_c3b_capability_gate.py",
        "tools/validate_windows_tm_current_source_release.py",
        "tools/windows_c3b_source_snapshot.py",
        "windows_file_api.py",
        *_TM_SOURCE_PATHS,
        *_WINDOWS_TM_WORKER_PATHS,
        *_selected_test_paths(),
    }
    return tuple(sorted(paths))


def _source_file_digests(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple((path, _strict_read(root, path)[1]) for path in source_paths(root))


def _source_fingerprint(
    current_registry_digest: str,
    source_files: tuple[tuple[str, str], ...],
    input_digests: dict[str, object],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "inputs": input_digests,
                "registry_digest": current_registry_digest,
                "source_files": [list(item) for item in source_files],
            }
        ).encode("utf-8")
    ).hexdigest()


def _validate_runtime() -> dict[str, str]:
    if sys.platform != "win32":
        raise ValueError("Windows current-source validation requires Windows")
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (
        3,
        14,
    ):
        raise ValueError("Windows current-source validation requires CPython 3.14")
    if struct.calcsize("P") != 8 or platform.machine().upper() not in {
        "AMD64",
        "X86_64",
    }:
        raise ValueError("Windows current-source validation requires x64")
    return {
        "architecture": platform.machine(),
        "os": platform.platform(),
        "python": platform.python_version(),
    }


def _validate_temp_root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("temporary root must be a directory")
    probe = resolved / f".localcat-current-source-probe-{os.getpid()}"
    probe.mkdir()
    probe.rmdir()
    return resolved


def _worker_result(row: WindowsTMCurrentSourceRow) -> tuple[dict[str, object], int]:
    tests = resolve_row_tests(row)
    if len(tests) != row.expected_count:
        raise ValueError(f"{row.row_id} test count is stale")
    observed_digest = sorted_test_ids_sha256(tests)
    if observed_digest != row.sorted_test_ids_sha256:
        raise ValueError(f"{row.row_id} test inventory digest is stale")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.TestSuite(tests)
    )
    verdict = "PASS"
    try:
        require_strict_success(row, result)
    except AssertionError:
        verdict = "FAIL"
    if verdict != "PASS":
        sys.stderr.write(stream.getvalue())
    payload = {
        "errors": len(result.errors),
        "expected_failures": len(result.expectedFailures),
        "failures": len(result.failures),
        "row_id": row.row_id,
        "selectors": list(row.selectors),
        "skipped": len(result.skipped),
        "sorted_test_ids_sha256": observed_digest,
        "test_ids": sorted(test.id() for test in tests),
        "tests_run": result.testsRun,
        "unexpected_successes": len(result.unexpectedSuccesses),
        "verdict": verdict,
    }
    return payload, 0 if verdict == "PASS" else 1


def _run_row_process(
    row: WindowsTMCurrentSourceRow,
    *,
    temp_root: Path,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "TMPDIR": str(temp_root),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--worker-row",
            row.row_id,
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=3600,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"{row.row_id} worker returned an invalid JSON stream")
    payload = _parse_strict_json(lines[0], label=f"{row.row_id} worker result")
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if set(payload) != {
        "errors",
        "expected_failures",
        "failures",
        "row_id",
        "selectors",
        "skipped",
        "sorted_test_ids_sha256",
        "test_ids",
        "tests_run",
        "unexpected_successes",
        "verdict",
    }:
        raise ValueError(f"{row.row_id} worker result fields are not closed")
    expected_exit = 0 if payload["verdict"] == "PASS" else 1
    if completed.returncode != expected_exit:
        raise ValueError(f"{row.row_id} worker exit/result mismatch")
    if (
        payload["row_id"] != row.row_id
        or payload["selectors"] != list(row.selectors)
        or payload["tests_run"] != row.expected_count
        or payload["sorted_test_ids_sha256"] != row.sorted_test_ids_sha256
        or not isinstance(payload["test_ids"], list)
        or len(cast(list[object], payload["test_ids"])) != row.expected_count
    ):
        raise ValueError(f"{row.row_id} worker inventory is stale")
    return payload


def _validate_benchmark_input(
    root: Path,
) -> tuple[dict[str, object], BenchmarkEvidenceBundle]:
    benchmark_bytes, benchmark_digest = _strict_read(root, "benchmark_tm_evidence.json")
    bundle = benchmark_evidence_bundle_from_json(benchmark_bytes.decode("utf-8"))
    if bundle.implementation_fingerprint != benchmark_implementation_fingerprint(root):
        raise ValueError("benchmark implementation fingerprint is stale")
    expected_paths = (
        (bundle.fts5.report, "true"),
        (bundle.fallback.report, "false"),
    )
    for report, expected_fts5 in expected_paths:
        environment = dict(report.environment)
        if (
            environment.get("os") != "Windows"
            or not environment.get("python_version", "").startswith("3.14.")
            or environment.get("rss_platform") != "windows"
            or environment.get("rss_raw_unit") != "bytes"
            or environment.get("fts5_enabled") != expected_fts5
        ):
            raise ValueError("benchmark environment is not Windows CPython 3.14")
    return (
        {
            "benchmark_bundle_digest": bundle.bundle_digest,
            "benchmark_evidence_sha256": benchmark_digest,
            "benchmark_implementation_fingerprint": bundle.implementation_fingerprint,
            "benchmark_suite_decision": (
                "GO" if bundle.suite_report.passed else "NO_GO"
            ),
        },
        bundle,
    )


def _validate_c3b_matrix(
    path: Path,
    *,
    expected_sha256: str,
    repository_commit: str,
    root: Path,
) -> dict[str, object]:
    current_snapshot = source_snapshot_sha256(root)
    return validate_aggregate_matrix(
        path,
        expected_sha256=expected_sha256,
        repository_commit=repository_commit,
        expected_source_snapshot=current_snapshot,
    )


def _atomic_write(path: Path, payload: bytes, validate_snapshot) -> None:
    platform_atomic_write(
        path,
        payload,
        validate_snapshot,
        candidate_prefix=".windows-tm-current-source-",
        identity_error="Windows TM evidence identity changed",
        readback_error="Windows TM evidence readback changed",
        platform_error="Windows TM evidence platform I/O failed",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--emit", type=Path, default=ROOT / EVIDENCE_NAME)
    parser.add_argument("--repository-commit")
    parser.add_argument("--c3b-matrix", type=Path)
    parser.add_argument("--c3b-matrix-sha256")
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--require-go", action="store_true")
    parser.add_argument("--worker-row", choices=tuple(_ROW_BY_ID))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.worker_row is not None:
        payload, exit_code = _worker_result(_ROW_BY_ID[arguments.worker_row])
        print(_canonical_json(payload))
        return exit_code

    environment = _validate_runtime()
    repository_root = arguments.repository_root.absolute()
    if repository_root != ROOT or repository_root.resolve(strict=True) != ROOT:
        raise ValueError("repository root must match the validator checkout")
    if (
        arguments.repository_commit is None
        or _COMMIT_RE.fullmatch(arguments.repository_commit) is None
    ):
        raise ValueError("repository commit must be lowercase 40-hex")
    if arguments.c3b_matrix is None or arguments.c3b_matrix_sha256 is None:
        raise ValueError("C3B matrix and SHA-256 are required")
    if _SHA256_RE.fullmatch(arguments.c3b_matrix_sha256) is None:
        raise ValueError("C3B matrix SHA-256 must be lowercase 64-hex")
    if arguments.temp_root is None:
        raise ValueError("a writable temporary root is required")
    temp_root = _validate_temp_root(arguments.temp_root)
    evidence_path = arguments.emit
    if not evidence_path.is_absolute():
        evidence_path = repository_root / evidence_path
    evidence_path = evidence_path.absolute()
    if evidence_path != repository_root / EVIDENCE_NAME:
        raise ValueError("Windows TM evidence must use the canonical output path")
    validate_evidence_target(
        evidence_path,
        target_error="Windows TM evidence target is not regular",
    )

    benchmark_input, benchmark_bundle = _validate_benchmark_input(repository_root)
    c3b = _validate_c3b_matrix(
        arguments.c3b_matrix,
        expected_sha256=arguments.c3b_matrix_sha256,
        repository_commit=arguments.repository_commit,
        root=repository_root,
    )
    current_registry_digest = registry_digest()
    source_files = _source_file_digests(repository_root)
    input_digests = {**benchmark_input, "c3b": c3b}
    source_fingerprint = _source_fingerprint(
        current_registry_digest,
        source_files,
        input_digests,
    )
    groups = [
        _run_row_process(row, temp_root=temp_root)
        for row in WINDOWS_TM_CURRENT_SOURCE_ROWS
    ]
    blockers = [
        f"WINDOWS_TM.{row['row_id']}.FAILED"
        for row in groups
        if row["verdict"] != "PASS"
    ]
    blockers.extend(
        f"BENCHMARK.{report.execution_path.value}.{gate}"
        for report in benchmark_bundle.suite_report.path_reports
        for gate in report.failed_gates
    )
    if not benchmark_bundle.suite_report.passed and not any(
        report.failed_gates for report in benchmark_bundle.suite_report.path_reports
    ):
        blockers.append("BENCHMARK.SUITE.NO_GO")
    if c3b["status"] != "PASS":
        blockers.append(f"WINDOWS_C3B.{c3b['status']}")
    decision = "GO" if not blockers else "NO_GO"

    def validate_snapshot() -> None:
        if _source_file_digests(repository_root) != source_files:
            raise ValueError("Windows TM sources changed during validation")
        benchmark_input_after, benchmark_after = _validate_benchmark_input(
            repository_root
        )
        if (
            benchmark_input_after != benchmark_input
            or benchmark_after != benchmark_bundle
        ):
            raise ValueError("benchmark input changed during validation")
        c3b_after = _validate_c3b_matrix(
            arguments.c3b_matrix,
            expected_sha256=arguments.c3b_matrix_sha256,
            repository_commit=arguments.repository_commit,
            root=repository_root,
        )
        if c3b_after != c3b:
            raise ValueError("C3B input changed during validation")

    validate_snapshot()
    evidence = {
        "blockers": blockers,
        "environment": environment,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": groups,
        "inputs": input_digests,
        "registry_digest": current_registry_digest,
        "release_decision": decision,
        "repository_commit": arguments.repository_commit,
        "schema_version": SCHEMA_VERSION,
        "source_files": [
            {"path": path, "sha256": digest} for path, digest in source_files
        ],
        "source_fingerprint": source_fingerprint,
        "summary": {
            "failed_groups": sum(row["verdict"] != "PASS" for row in groups),
            "passed_groups": sum(row["verdict"] == "PASS" for row in groups),
            "registered_tests": sum(
                row.expected_count for row in WINDOWS_TM_CURRENT_SOURCE_ROWS
            ),
            "total_groups": len(groups),
        },
        "temp_root_role": "CALLER_WRITABLE_TEMP_ROOT",
    }
    _atomic_write(
        evidence_path,
        (_canonical_json(evidence) + "\n").encode("utf-8"),
        validate_snapshot,
    )
    print(
        _canonical_json(
            {
                "evidence": evidence_path.name,
                "registered_tests": evidence["summary"]["registered_tests"],
                "release_decision": decision,
                "source_fingerprint": source_fingerprint,
            }
        )
    )
    return 1 if arguments.require_go and decision != "GO" else 0


if __name__ == "__main__":
    raise SystemExit(main())
