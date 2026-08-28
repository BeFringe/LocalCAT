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
    verdict = "PASS"
    if result.failures or result.errors:
        verdict = "FAIL"
    elif result.skipped:
        verdict = "NOT_RUN"
    return {
        "coverage": list(coverage),
        "errors": len(result.errors),
        "failures": len(result.failures),
        "id": group_id,
        "log": log_key,
        "selected_tests": list(names),
        "skipped": len(result.skipped),
        "tests_run": result.testsRun,
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
        if result.failures or result.errors:
            verdict = "FAIL"
        elif result.skipped:
            verdict = "NOT_RUN"
        rows.append(
            {
                "coverage": coverage,
                "errors": len(result.errors),
                "failures": len(result.failures),
                "id": row_id,
                "log": log_key,
                "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                "skipped": len(result.skipped),
                "test": test_name,
                "tests_run": result.testsRun,
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
        "environment": {
            "architecture": platform.machine(),
            "os": platform.platform(),
            "python": platform.python_version(),
        },
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
) -> list[dict[str, object]]:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("elevated evidence SHA-256 mismatch")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid elevated evidence: {error}") from error
    if type(report) is not dict or set(report) != {
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
    rows = report["rows"]
    if type(rows) is not list or len(rows) != len(ELEVATED_TESTS):
        raise ValueError("elevated evidence must contain both mandatory rows")
    expected_ids = ("elevated-private-positive", "elevated-owner-tamper")
    for row, expected_id, expected_test in zip(rows, expected_ids, ELEVATED_TESTS):
        if type(row) is not dict:
            raise ValueError("elevated evidence row must be an object")
        if (
            row.get("id") != expected_id
            or row.get("test") != expected_test
            or row.get("tests_run") != 1
            or row.get("failures") != 0
            or row.get("errors") != 0
            or row.get("skipped") != 0
            or row.get("verdict") != "PASS"
        ):
            raise ValueError(f"elevated evidence row {expected_id!r} is not PASS")
        expected_log = f"logs/{expected_id}.log"
        log_sha256 = row.get("log_sha256")
        if row.get("log") != expected_log or type(log_sha256) is not str:
            raise ValueError(f"elevated evidence row {expected_id!r} log is invalid")
        log_path = path.parent.joinpath(*expected_log.split("/"))
        if hashlib.sha256(log_path.read_bytes()).hexdigest() != log_sha256:
            raise ValueError(f"elevated evidence row {expected_id!r} log SHA-256 mismatch")
    if report["status"] != "PASS":
        raise ValueError("elevated evidence aggregate is not PASS")
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
    if manifest["run"]["status"] != "PASS" or scenarios.get("normal-os-reboot") != "PASS":
        raise ValueError("normal OS reboot evidence is not PASS")
    resume_records = [
        record for record in manifest["commands"] if record["id"] == "reboot-resume"
    ]
    if len(resume_records) != 1 or resume_records[0]["status"] != "PASS":
        raise ValueError("normal OS reboot resume command is not PASS")
    resume_log = artifact_root.joinpath(
        *str(resume_records[0]["stdout_log"]).split("/")
    )
    resume_output = json.loads(resume_log.read_text(encoding="utf-8"))
    if (
        type(resume_output) is not dict
        or resume_output.get("source_snapshot_sha256") != expected_source_snapshot
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
        "evidence": str(artifact_root),
        "id": "normal-os-reboot",
        "test": "tools.run_windows_documented_publish_evidence --resume-reboot",
        "verdict": "PASS",
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
        "environment": {
            "architecture": platform.machine(),
            "os": platform.platform(),
            "python": platform.python_version(),
        },
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
