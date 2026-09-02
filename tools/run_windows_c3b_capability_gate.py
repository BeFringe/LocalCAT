"""Run and aggregate the Task 3.7 Windows C3B capability matrix."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.run_windows_documented_publish_evidence import CONTRACT_KEY
from tools.validate_windows_release import (
    EvidenceValidationError,
    validate_evidence_bundle,
)
from tools.windows_c3b_source_snapshot import source_snapshot_sha256

ELEVATED_TESTS = (
    "tests.test_platform_fs_windows_private.WindowsPrivateRuntimeTests."
    "test_w2_elevated_positive_control_is_not_silently_substituted",
    "tests.test_platform_fs_windows_private.WindowsPrivateRuntimeTests."
    "test_w2_owner_tamper_requires_elevated_profile",
)
ELEVATED_SCHEMA = "localcat.windows-c3b-elevated.v1"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GROUPS = (
    (
        "shared-contracts",
        (
            "tests.test_platform_fs_contracts",
            "tests.test_windows_c3b_source_snapshot",
        ),
        (
            "opaque authority",
            "live-only identity",
            "pending publication",
            "source snapshot",
        ),
    ),
    (
        "factory-mint",
        (
            "tests.test_platform_fs_composition.CompositionStaticBoundaryTests",
            "tests.test_platform_fs_composition.CompositionFactoryMatrixTests",
        ),
        ("factory mint", "fail-closed composition", "no POSIX import"),
    ),
    (
        "windows-ffi",
        ("tests.test_windows_file_api",),
        ("Win32 ABI", "native same-parent naming", "handle cleanup"),
    ),
    (
        "windows-host",
        ("tests.test_platform_fs_windows",),
        ("host gate", "local fixed NTFS", "factory backend"),
    ),
    (
        "windows-rooted-share-cleanup",
        ("tests.test_platform_fs_windows_rooted",),
        ("rooted", "share", "identity swap", "resource cleanup"),
    ),
    (
        "windows-lock-kill-identity",
        ("tests.test_platform_fs_windows_lock",),
        ("lock", "two creator", "kill release", "identity reuse"),
    ),
    (
        "windows-private-standard",
        ("tests.test_platform_fs_windows_private",),
        ("private", "standard token", "low/restricted", "restart proof"),
    ),
    (
        "windows-publish-target-open",
        ("tests.test_platform_fs_windows_publish",),
        ("publish", "target-open", "share-none", "non-CAS recovery"),
    ),
    (
        "documented-publish-recovery",
        ("tests.test_platform_fs_windows_documented_publish",),
        ("instruction fault", "process termination", "application restart"),
    ),
)


def current_environment() -> dict[str, str]:
    return {
        "architecture": platform.machine(),
        "os": platform.platform(),
        "python": platform.python_version(),
    }


def _require_current_windows_environment(environment: object) -> dict[str, str]:
    if type(environment) is not dict or set(environment) != {
        "architecture",
        "os",
        "python",
    }:
        raise ValueError("C3B environment fields are invalid")
    expected = current_environment()
    if (
        environment != expected
        or sys.platform != "win32"
        or platform.python_implementation() != "CPython"
        or sys.version_info[:2] != (3, 14)
        or platform.machine().upper() not in {"AMD64", "X86_64"}
    ):
        raise ValueError("C3B evidence is not from the current Windows CPython 3.14 x64 host")
    return expected


def _iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _load_group(names: tuple[str, ...]) -> unittest.TestSuite:
    loaded = unittest.defaultTestLoader.loadTestsFromNames(names)
    selected = unittest.TestSuite()
    for test in _iter_tests(loaded):
        if test.id() not in ELEVATED_TESTS:
            selected.addTest(test)
    return selected


def _run_group(
    output_root: Path,
    group_id: str,
    names: tuple[str, ...],
    coverage: tuple[str, ...],
) -> dict[str, object]:
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(_load_group(names))
    log_key = f"logs/{group_id}.log"
    log_path = output_root.joinpath(*log_key.split("/"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(stream.getvalue(), encoding="utf-8")
    log_sha256 = hashlib.sha256(log_path.read_bytes()).hexdigest()
    verdict = "PASS"
    if result.failures or result.errors or result.unexpectedSuccesses:
        verdict = "FAIL"
    elif result.skipped or result.expectedFailures:
        verdict = "NOT_RUN"
    return {
        "coverage": list(coverage),
        "errors": len(result.errors),
        "expected_failures": len(result.expectedFailures),
        "failures": len(result.failures),
        "id": group_id,
        "log": log_key,
        "log_sha256": log_sha256,
        "selected_tests": list(names),
        "skipped": len(result.skipped),
        "tests_run": result.testsRun,
        "unexpected_successes": len(result.unexpectedSuccesses),
        "verdict": verdict,
    }


def _pending_rows() -> list[dict[str, object]]:
    return [
        {
            "coverage": ["private", "elevated positive"],
            "id": "elevated-private-positive",
            "test": ELEVATED_TESTS[0],
            "verdict": "NOT_RUN",
        },
        {
            "coverage": ["private", "elevated owner tamper"],
            "id": "elevated-owner-tamper",
            "test": ELEVATED_TESTS[1],
            "verdict": "NOT_RUN",
        },
        {
            "coverage": ["documented publish", "normal OS reboot"],
            "id": "normal-os-reboot",
            "test": "tools.run_windows_documented_publish_evidence --resume-reboot",
            "verdict": "NOT_RUN",
        },
    ]


def _elevated_rows(output_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, test_name in enumerate(ELEVATED_TESTS):
        row_id = ("elevated-private-positive", "elevated-owner-tamper")[index]
        coverage = (
            ["private", "elevated positive"]
            if index == 0
            else ["private", "elevated owner tamper"]
        )
        stream = io.StringIO()
        suite = unittest.defaultTestLoader.loadTestsFromName(test_name)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        log_key = f"logs/{row_id}.log"
        log_path = output_root.joinpath(*log_key.split("/"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(stream.getvalue(), encoding="utf-8")
        verdict = "PASS"
        if result.failures or result.errors or result.unexpectedSuccesses:
            verdict = "FAIL"
        elif result.skipped or result.expectedFailures:
            verdict = "NOT_RUN"
        rows.append(
            {
                "coverage": coverage,
                "errors": len(result.errors),
                "expected_failures": len(result.expectedFailures),
                "failures": len(result.failures),
                "id": row_id,
                "log": log_key,
                "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                "skipped": len(result.skipped),
                "test": test_name,
                "tests_run": result.testsRun,
                "unexpected_successes": len(result.unexpectedSuccesses),
                "verdict": verdict,
            }
        )
    return rows


def _write_elevated_evidence(
    output_root: Path,
    repository_commit: str,
    source_snapshot: str,
) -> tuple[Path, str]:
    rows = _elevated_rows(output_root)
    failed = any(row["verdict"] == "FAIL" for row in rows)
    pending = any(row["verdict"] == "NOT_RUN" for row in rows)
    status = "FAIL" if failed else "NOT_RUN" if pending else "PASS"
    report = {
        "environment": current_environment(),
        "repository_commit": repository_commit,
        "rows": rows,
        "schema": ELEVATED_SCHEMA,
        "source_snapshot_sha256": source_snapshot,
        "status": status,
    }
    path = output_root / "c3b-elevated.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, status


def _load_elevated_evidence(
    path: Path,
    repository_commit: str,
    expected_sha256: str,
    expected_source_snapshot: str,
    expected_environment: dict[str, str],
    *,
    aggregate_root: Path | None = None,
) -> list[dict[str, object]]:
    try:
        report, raw = _strict_json_object(path, label="elevated evidence")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid elevated evidence: {error}") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("elevated evidence SHA-256 mismatch")
    if set(report) != {
        "environment",
        "repository_commit",
        "rows",
        "schema",
        "source_snapshot_sha256",
        "status",
    }:
        raise ValueError("elevated evidence must be an exact object")
    if report["schema"] != ELEVATED_SCHEMA:
        raise ValueError("elevated evidence schema mismatch")
    if report["repository_commit"] != repository_commit:
        raise ValueError("elevated evidence repository commit mismatch")
    if report["source_snapshot_sha256"] != expected_source_snapshot:
        raise ValueError("elevated evidence source snapshot mismatch")
    if report["environment"] != expected_environment:
        raise ValueError("elevated evidence environment mismatch")
    rows = report["rows"]
    if type(rows) is not list or len(rows) != len(ELEVATED_TESTS):
        raise ValueError("elevated evidence must contain both mandatory rows")
    expected_ids = ("elevated-private-positive", "elevated-owner-tamper")
    for row, expected_id, expected_test in zip(rows, expected_ids, ELEVATED_TESTS):
        if type(row) is not dict or set(row) != {
            "coverage",
            "errors",
            "expected_failures",
            "failures",
            "id",
            "log",
            "log_sha256",
            "skipped",
            "test",
            "tests_run",
            "unexpected_successes",
            "verdict",
        }:
            raise ValueError("elevated evidence row must be an object")
        expected_coverage = (
            ["private", "elevated positive"]
            if expected_id == expected_ids[0]
            else ["private", "elevated owner tamper"]
        )
        failures = row.get("failures")
        errors = row.get("errors")
        skipped = row.get("skipped")
        expected_failures = row.get("expected_failures")
        unexpected_successes = row.get("unexpected_successes")
        if (
            row.get("id") != expected_id
            or row.get("coverage") != expected_coverage
            or row.get("test") != expected_test
            or row.get("tests_run") != 1
            or type(failures) is not int
            or failures < 0
            or type(errors) is not int
            or errors < 0
            or type(skipped) is not int
            or skipped < 0
            or type(expected_failures) is not int
            or expected_failures < 0
            or type(unexpected_successes) is not int
            or unexpected_successes < 0
        ):
            raise ValueError(f"elevated evidence row {expected_id!r} is invalid")
        expected_verdict = (
            "FAIL"
            if failures or errors or unexpected_successes
            else "NOT_RUN"
            if skipped or expected_failures
            else "PASS"
        )
        if row.get("verdict") != expected_verdict:
            raise ValueError(
                f"elevated evidence row {expected_id!r} verdict is inconsistent"
            )
        expected_log = f"logs/{expected_id}.log"
        log_sha256 = row.get("log_sha256")
        if row.get("log") != expected_log or type(log_sha256) is not str:
            raise ValueError(f"elevated evidence row {expected_id!r} log is invalid")
        log_path = path.parent.joinpath(*expected_log.split("/"))
        log_bytes = log_path.read_bytes()
        if hashlib.sha256(log_bytes).hexdigest() != log_sha256:
            raise ValueError(f"elevated evidence row {expected_id!r} log SHA-256 mismatch")
        if aggregate_root is not None:
            aggregate_log_path = aggregate_root.joinpath(*expected_log.split("/"))
            aggregate_log_path.parent.mkdir(parents=True, exist_ok=True)
            aggregate_log_path.write_bytes(log_bytes)
    verdicts = [row["verdict"] for row in rows]
    expected_status = (
        "FAIL"
        if "FAIL" in verdicts
        else "NOT_RUN"
        if "NOT_RUN" in verdicts
        else "PASS"
    )
    if report["status"] != expected_status:
        raise ValueError("elevated evidence aggregate status is inconsistent")
    return rows


def _load_reboot_evidence(
    artifact_root: Path,
    repository_commit: str,
    expected_manifest_sha256: str,
    expected_source_snapshot: str,
) -> dict[str, object]:
    contract_path = ROOT.joinpath(*CONTRACT_KEY.split("/"))
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    manifest = validate_evidence_bundle(
        artifact_root,
        repository_root=ROOT,
        scenario_contract_key=CONTRACT_KEY,
        expected_repository_commit=repository_commit,
        expected_scenario_contract_sha256=contract_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    matrix_path = artifact_root.joinpath(
        *str(manifest["outputs"]["matrix"]["path"]).split("/")
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    scenarios = {
        row["id"]: row["verdict"]
        for row in matrix["scenarios"]
        if type(row) is dict and "id" in row and "verdict" in row
    }
    run_status = manifest["run"]["status"]
    scenario_status = scenarios.get("normal-os-reboot")
    if run_status not in {"PASS", "FAIL", "NOT_RUN"} or scenario_status not in {
        "PASS",
        "FAIL",
        "NOT_RUN",
    }:
        raise ValueError("normal OS reboot evidence verdict is invalid")
    resume_records = [
        record for record in manifest["commands"] if record["id"] == "reboot-resume"
    ]
    if len(resume_records) != 1 or resume_records[0]["status"] not in {
        "PASS",
        "FAIL",
        "NOT_RUN",
    }:
        raise ValueError("normal OS reboot resume command verdict is invalid")
    command_status = resume_records[0]["status"]
    verdicts = (run_status, scenario_status, command_status)
    verdict = (
        "FAIL"
        if "FAIL" in verdicts
        else "NOT_RUN"
        if "NOT_RUN" in verdicts
        else "PASS"
    )
    if command_status == "PASS":
        resume_log = artifact_root.joinpath(
            *str(resume_records[0]["stdout_log"]).split("/")
        )
        resume_output = json.loads(resume_log.read_text(encoding="utf-8"))
        if (
            type(resume_output) is not dict
            or resume_output.get("source_snapshot_sha256")
            != expected_source_snapshot
        ):
            raise ValueError("normal OS reboot log source snapshot mismatch")
    checklist_path = artifact_root.joinpath(
        *str(manifest["outputs"]["windows_fs_lock_checklist"]["path"]).split("/")
    )
    snapshot_line = f"- C3B source snapshot SHA-256: {expected_source_snapshot}"
    if snapshot_line not in checklist_path.read_text(encoding="utf-8").splitlines():
        raise ValueError("normal OS reboot manifest output source snapshot mismatch")
    return {
        "coverage": ["documented publish", "normal OS reboot"],
        "manifest_sha256": expected_manifest_sha256,
        "id": "normal-os-reboot",
        "scenario_contract_sha256": contract_sha256,
        "source_snapshot_sha256": expected_source_snapshot,
        "test": "tools.run_windows_documented_publish_evidence --resume-reboot",
        "verdict": verdict,
    }


def _strict_json_object(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}")
            result[key] = value
        return result

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token in {label}: {token}")
        ),
    )
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value, raw


def _validate_log(
    matrix_root: Path,
    *,
    row_id: str,
    log_key: object,
    log_sha256: object,
) -> None:
    expected_log = f"logs/{row_id}.log"
    if log_key != expected_log or type(log_sha256) is not str:
        raise ValueError(f"C3B row {row_id!r} log binding is invalid")
    if SHA256_RE.fullmatch(log_sha256) is None:
        raise ValueError(f"C3B row {row_id!r} log digest is invalid")
    log_path = matrix_root.joinpath(*expected_log.split("/"))
    if hashlib.sha256(log_path.read_bytes()).hexdigest() != log_sha256:
        raise ValueError(f"C3B row {row_id!r} log digest mismatch")


def validate_aggregate_matrix(
    path: Path,
    *,
    expected_sha256: str,
    repository_commit: str,
    expected_source_snapshot: str,
) -> dict[str, object]:
    """Strictly validate a complete C3B handoff and its local log closure."""

    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ValueError("C3B matrix SHA-256 is invalid")
    if COMMIT_RE.fullmatch(repository_commit) is None:
        raise ValueError("C3B repository commit is invalid")
    if SHA256_RE.fullmatch(expected_source_snapshot) is None:
        raise ValueError("C3B source snapshot SHA-256 is invalid")
    matrix, raw = _strict_json_object(path, label="C3B matrix")
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ValueError("C3B matrix SHA-256 mismatch")
    if set(matrix) != {
        "environment",
        "repository_commit",
        "rows",
        "source_snapshot_sha256",
        "status",
    }:
        raise ValueError("C3B matrix fields are not closed")
    _require_current_windows_environment(matrix["environment"])
    if matrix["repository_commit"] != repository_commit:
        raise ValueError("C3B matrix repository commit mismatch")
    if matrix["source_snapshot_sha256"] != expected_source_snapshot:
        raise ValueError("C3B matrix source snapshot mismatch")
    rows = matrix["rows"]
    expected_row_ids = tuple(group[0] for group in GROUPS) + (
        "elevated-private-positive",
        "elevated-owner-tamper",
        "normal-os-reboot",
    )
    if type(rows) is not list or len(rows) != len(expected_row_ids):
        raise ValueError("C3B row inventory is stale")
    row_ids = tuple(row.get("id") if type(row) is dict else None for row in rows)
    if row_ids != expected_row_ids:
        raise ValueError("C3B row order is stale")
    matrix_root = path.parent
    verdicts: list[str] = []
    for row, (row_id, selectors, coverage) in zip(rows[: len(GROUPS)], GROUPS):
        if type(row) is not dict or set(row) != {
            "coverage",
            "errors",
            "expected_failures",
            "failures",
            "id",
            "log",
            "log_sha256",
            "selected_tests",
            "skipped",
            "tests_run",
            "unexpected_successes",
            "verdict",
        }:
            raise ValueError(f"C3B row {row_id!r} fields are not closed")
        if (
            row["id"] != row_id
            or row["coverage"] != list(coverage)
            or row["selected_tests"] != list(selectors)
            or row["tests_run"] != _load_group(selectors).countTestCases()
            or type(row["failures"]) is not int
            or row["failures"] < 0
            or type(row["errors"]) is not int
            or row["errors"] < 0
            or type(row["skipped"]) is not int
            or row["skipped"] < 0
            or type(row["expected_failures"]) is not int
            or row["expected_failures"] < 0
            or type(row["unexpected_successes"]) is not int
            or row["unexpected_successes"] < 0
        ):
            raise ValueError(f"C3B row {row_id!r} facts are invalid")
        expected_verdict = (
            "FAIL"
            if row["failures"] or row["errors"] or row["unexpected_successes"]
            else "NOT_RUN"
            if row["skipped"] or row["expected_failures"]
            else "PASS"
        )
        if row["verdict"] != expected_verdict:
            raise ValueError(f"C3B row {row_id!r} verdict is inconsistent")
        verdicts.append(expected_verdict)
        _validate_log(
            matrix_root,
            row_id=row_id,
            log_key=row["log"],
            log_sha256=row["log_sha256"],
        )
    elevated_ids = ("elevated-private-positive", "elevated-owner-tamper")
    for row, row_id, test_name in zip(
        rows[len(GROUPS) : len(GROUPS) + 2],
        elevated_ids,
        ELEVATED_TESTS,
    ):
        if type(row) is not dict:
            raise ValueError(f"C3B row {row_id!r} is not an object")
        if set(row) == {"coverage", "id", "test", "verdict"}:
            expected_coverage = (
                ["private", "elevated positive"]
                if row_id == elevated_ids[0]
                else ["private", "elevated owner tamper"]
            )
            if (
                row["id"] != row_id
                or row["coverage"] != expected_coverage
                or row["test"] != test_name
                or row["verdict"] != "NOT_RUN"
            ):
                raise ValueError(f"C3B pending row {row_id!r} is invalid")
            verdicts.append("NOT_RUN")
            continue
        if set(row) != {
            "coverage",
            "errors",
            "expected_failures",
            "failures",
            "id",
            "log",
            "log_sha256",
            "skipped",
            "test",
            "tests_run",
            "unexpected_successes",
            "verdict",
        }:
            raise ValueError(f"C3B row {row_id!r} fields are not closed")
        expected_coverage = (
            ["private", "elevated positive"]
            if row_id == elevated_ids[0]
            else ["private", "elevated owner tamper"]
        )
        if (
            row["id"] != row_id
            or row["coverage"] != expected_coverage
            or row["test"] != test_name
            or row["tests_run"] != 1
            or type(row["failures"]) is not int
            or row["failures"] < 0
            or type(row["errors"]) is not int
            or row["errors"] < 0
            or type(row["skipped"]) is not int
            or row["skipped"] < 0
            or type(row["expected_failures"]) is not int
            or row["expected_failures"] < 0
            or type(row["unexpected_successes"]) is not int
            or row["unexpected_successes"] < 0
        ):
            raise ValueError(f"C3B row {row_id!r} facts are invalid")
        expected_verdict = (
            "FAIL"
            if row["failures"] or row["errors"] or row["unexpected_successes"]
            else "NOT_RUN"
            if row["skipped"] or row["expected_failures"]
            else "PASS"
        )
        if row["verdict"] != expected_verdict:
            raise ValueError(f"C3B row {row_id!r} verdict is inconsistent")
        verdicts.append(expected_verdict)
        _validate_log(
            matrix_root,
            row_id=row_id,
            log_key=row["log"],
            log_sha256=row["log_sha256"],
        )
    reboot = rows[-1]
    if type(reboot) is not dict:
        raise ValueError("C3B normal reboot row is not an object")
    if set(reboot) == {"coverage", "id", "test", "verdict"}:
        if (
            reboot["coverage"] != ["documented publish", "normal OS reboot"]
            or reboot["id"] != "normal-os-reboot"
            or reboot["test"]
            != "tools.run_windows_documented_publish_evidence --resume-reboot"
            or reboot["verdict"] != "NOT_RUN"
        ):
            raise ValueError("C3B pending normal reboot row is invalid")
        verdicts.append("NOT_RUN")
    elif set(reboot) != {
        "coverage",
        "id",
        "manifest_sha256",
        "scenario_contract_sha256",
        "source_snapshot_sha256",
        "test",
        "verdict",
    }:
        raise ValueError("C3B normal reboot fields are not closed")
    else:
        if (
            reboot["coverage"] != ["documented publish", "normal OS reboot"]
            or reboot["id"] != "normal-os-reboot"
            or reboot["test"]
            != "tools.run_windows_documented_publish_evidence --resume-reboot"
            or reboot["verdict"] not in {"PASS", "FAIL", "NOT_RUN"}
            or reboot["source_snapshot_sha256"] != expected_source_snapshot
            or type(reboot["manifest_sha256"]) is not str
            or SHA256_RE.fullmatch(reboot["manifest_sha256"]) is None
            or type(reboot["scenario_contract_sha256"]) is not str
            or SHA256_RE.fullmatch(reboot["scenario_contract_sha256"]) is None
        ):
            raise ValueError("C3B normal reboot proof is invalid")
        verdicts.append(reboot["verdict"])
    expected_status = (
        "FAIL"
        if "FAIL" in verdicts
        else "NOT_RUN"
        if "NOT_RUN" in verdicts
        else "PASS"
    )
    if matrix["status"] != expected_status:
        raise ValueError("C3B aggregate status is inconsistent")
    return {
        "matrix_sha256": observed_sha256,
        "repository_commit": repository_commit,
        "source_snapshot_sha256": expected_source_snapshot,
        "status": expected_status,
    }


def _write_markdown(path: Path, report: dict[str, object]) -> None:
    rows = report["rows"]
    assert isinstance(rows, list)
    lines = [
        "# Windows C3B capability gate",
        "",
        f"Overall: **{report['status']}**",
        "",
        f"Source snapshot SHA-256: `{report['source_snapshot_sha256']}`",
        "",
        "| Row | Coverage | Tests | Fail | Error | Skip | Verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        assert isinstance(row, dict)
        lines.append(
            "| {id} | {coverage} | {tests} | {failures} | {errors} | {skipped} | {verdict} |".format(
                id=row["id"],
                coverage=", ".join(row["coverage"]),
                tests=row.get("tests_run", "-"),
                failures=row.get("failures", "-"),
                errors=row.get("errors", "-"),
                skipped=row.get("skipped", "-"),
                verdict=row["verdict"],
            )
        )
    lines.extend(
        (
            "",
            "The two elevated rows and the normal-reboot row are mandatory. "
            "They remain NOT_RUN until fresh, commit-bound evidence and its "
            "external SHA-256 are supplied to this runner; "
            "no unittest skip is counted as PASS.",
            "",
        )
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--repository-commit")
    parser.add_argument("--elevated-only", action="store_true")
    parser.add_argument("--elevated-evidence", type=Path)
    parser.add_argument("--elevated-evidence-sha256")
    parser.add_argument("--reboot-evidence-root", type=Path)
    parser.add_argument("--reboot-manifest-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repository_commit is not None and COMMIT_RE.fullmatch(args.repository_commit) is None:
        raise SystemExit("--repository-commit must be lowercase 40-hex")
    if args.elevated_only and args.repository_commit is None:
        raise SystemExit("--elevated-only requires --repository-commit")
    if (args.elevated_evidence or args.reboot_evidence_root) and args.repository_commit is None:
        raise SystemExit("evidence aggregation requires --repository-commit")
    if (args.elevated_evidence is None) != (args.elevated_evidence_sha256 is None):
        raise SystemExit("--elevated-evidence and its SHA-256 must be provided together")
    if (args.reboot_evidence_root is None) != (args.reboot_manifest_sha256 is None):
        raise SystemExit("--reboot-evidence-root and manifest SHA-256 must be provided together")
    for digest in (args.elevated_evidence_sha256, args.reboot_manifest_sha256):
        if digest is not None and SHA256_RE.fullmatch(digest) is None:
            raise SystemExit("evidence SHA-256 must be lowercase 64-hex")
    source_snapshot = source_snapshot_sha256(ROOT)
    environment = _require_current_windows_environment(current_environment())
    args.output_root.mkdir(parents=True, exist_ok=True)
    if args.elevated_only:
        evidence_path, status = _write_elevated_evidence(
            args.output_root,
            args.repository_commit,
            source_snapshot,
        )
        print(
            json.dumps(
                {
                    "evidence": str(evidence_path),
                    "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                    "source_snapshot_sha256": source_snapshot,
                    "status": status,
                },
                sort_keys=True,
            )
        )
        return 0 if status == "PASS" else 1 if status == "FAIL" else 2
    rows: list[dict[str, object]] = []
    if sys.platform == "win32":
        for group_id, names, coverage in GROUPS:
            rows.append(_run_group(args.output_root, group_id, names, coverage))
    else:
        rows.append(
            {
                "coverage": ["Windows runtime"],
                "errors": 1,
                "failures": 0,
                "id": "windows-host",
                "skipped": 0,
                "tests_run": 0,
                "verdict": "FAIL",
            }
        )
    pending = {row["id"]: row for row in _pending_rows()}
    try:
        if args.elevated_evidence is not None:
            for row in _load_elevated_evidence(
                args.elevated_evidence,
                args.repository_commit,
                args.elevated_evidence_sha256,
                source_snapshot,
                environment,
                aggregate_root=args.output_root,
            ):
                pending[row["id"]] = row
        if args.reboot_evidence_root is not None:
            reboot_row = _load_reboot_evidence(
                args.reboot_evidence_root,
                args.repository_commit,
                args.reboot_manifest_sha256,
                source_snapshot,
            )
            pending[reboot_row["id"]] = reboot_row
    except (EvidenceValidationError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        rows.append(
            {
                "coverage": ["mandatory external evidence"],
                "errors": 1,
                "failures": 0,
                "id": "external-evidence-validation",
                "skipped": 0,
                "tests_run": 0,
                "verdict": "FAIL",
                "message": str(error),
            }
        )
    rows.extend(pending.values())
    failed = any(row["verdict"] == "FAIL" for row in rows)
    not_run = any(row["verdict"] == "NOT_RUN" for row in rows)
    status = "FAIL" if failed else "NOT_RUN" if not_run else "PASS"
    report = {
        "environment": environment,
        "repository_commit": args.repository_commit,
        "rows": rows,
        "source_snapshot_sha256": source_snapshot,
        "status": status,
    }
    json_path = args.output_root / "c3b-matrix.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_markdown(args.output_root / "c3b-matrix.md", report)
    print(
        json.dumps(
            {
                "matrix": str(json_path),
                "source_snapshot_sha256": source_snapshot,
                "status": status,
            },
            sort_keys=True,
        )
    )
    return 1 if failed else 2 if not_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
