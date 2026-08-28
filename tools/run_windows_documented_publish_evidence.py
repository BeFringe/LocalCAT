"""Run the Task 3.6 WindowsDocumentedPublishV1 evidence lane."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.validate_windows_release import (
    EvidenceHarness,
    EvidenceValidationError,
    redact_text,
    validate_evidence_bundle,
)
from tests.windows_publish_recovery_worker import EXPECTED_RECOVERY, SCHEMA
from tools.windows_c3b_source_snapshot import source_snapshot_sha256


CONTRACT_KEY = "packaging/windows/evidence-scenarios/windows-documented-publish-v1.json"
WORKER = (
    "(Join-Path (Join-Path $env:LOCALCAT_REPOSITORY 'tests') "
    "'windows_publish_recovery_worker.py')"
)
COMMANDS = {
    "publish-success": (
        f"& $env:LOCALCAT_PYTHON {WORKER} success "
        "(Join-Path $env:LOCALCAT_RUN_ROOT 'success')"
    ),
    "instruction-faults": (
        f"& $env:LOCALCAT_PYTHON {WORKER} instruction-matrix "
        "(Join-Path $env:LOCALCAT_RUN_ROOT 'instruction')"
    ),
    "process-termination": (
        f"& $env:LOCALCAT_PYTHON {WORKER} termination-matrix "
        "(Join-Path $env:LOCALCAT_RUN_ROOT 'termination')"
    ),
    "application-restart": (
        f"& $env:LOCALCAT_PYTHON {WORKER} success "
        "(Join-Path $env:LOCALCAT_RUN_ROOT 'application-restart')"
    ),
    "reboot-prepare": (
        "$boot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime."
        "ToUniversalTime().Ticks.ToString(); "
        f"& $env:LOCALCAT_PYTHON {WORKER} reboot-prepare "
        "$env:LOCALCAT_REBOOT_ROOT --boot-session $boot"
    ),
    "reboot-resume": (
        "$boot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime."
        "ToUniversalTime().Ticks.ToString(); "
        f"& $env:LOCALCAT_PYTHON {WORKER} reboot-resume "
        "$env:LOCALCAT_REBOOT_ROOT --boot-session $boot"
    ),
}


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _environment() -> dict[str, str]:
    return {
        "os_name": "Windows",
        "os_version": platform.release(),
        "os_build": platform.version(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "pyside6": _distribution_version("PySide6"),
        "qt": _distribution_version("PySide6_Essentials"),
        "pyinstaller": _distribution_version("PyInstaller"),
        "sqlite": sqlite3.sqlite_version,
        "filesystem": "NTFS",
        "volume_class": "fixed-local",
    }


def _command_output(harness: EvidenceHarness, record: dict[str, object]) -> object:
    path = harness.root.joinpath(*str(record["stdout_log"]).split("/"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("documented publish worker output is invalid") from error


def _require_success(value: object) -> None:
    if type(value) is not dict or value != {"recovery": "NEW", "schema": SCHEMA}:
        raise EvidenceValidationError("documented publish success did not recover NEW")


def _require_matrix(value: object, kind: str) -> None:
    if type(value) is not dict or value != {
        "fault_kind": kind,
        "results": EXPECTED_RECOVERY,
        "schema": SCHEMA,
    }:
        raise EvidenceValidationError(f"{kind} matrix left the old/new/recovery oracle")


def _record(
    harness: EvidenceHarness,
    *,
    operation_id: str,
    command_id: str,
    operation: str,
    phase: str,
    result: str,
    recovery: str,
    stable_code: str | None = None,
) -> None:
    harness.record_event(
        operation_id=operation_id,
        command_id=command_id,
        operation=operation,
        profile="WindowsDocumentedPublishV1",
        phase=phase,
        result=result,
        stable_code=stable_code,
        filesystem="NTFS",
        volume_class="fixed-local",
        identity_comparison="MATCH" if result == "PASS" else "NOT_OBSERVED",
        final_path_verdict="MATCH" if result == "PASS" else "NOT_OBSERVED",
        reparse_verdict="CLEAR" if result == "PASS" else "NOT_OBSERVED",
        share_flags=("READ",),
        lock_range=(0, 1),
        recovery_result=recovery,
    )


def _write_checklists(
    root: Path,
    reboot_verdict: str,
    source_snapshot: str,
) -> None:
    (root / "windows-fs-lock-adaptation-checklist.md").write_text(
        "# WindowsDocumentedPublishV1 Task 3.6\n\n"
        "- local fixed NTFS rooted authority: exercised\n"
        "- WRITE_THROUGH candidate and FlushFileBuffers: exercised\n"
        "- handle-bound naming, candidate close, retained readback: exercised\n"
        "- test-owner durable state commit and terminal reproof: exercised\n"
        "- instruction fault/process termination/application restart: exercised\n"
        f"- normal OS reboot post-reproof: {reboot_verdict}\n\n"
        f"- C3B source snapshot SHA-256: {source_snapshot}\n\n"
        "This lane does not claim forced-power-loss or hardware cache qualification.\n",
        encoding="utf-8",
    )
    (root / "frozen-source-packaging-checklist.md").write_text(
        "# Task 3.6 delivery boundary\n\n"
        "This source-platform evidence does not claim frozen packaging or release GO.\n",
        encoding="utf-8",
    )


def _preflight_portable_metadata(harness: EvidenceHarness) -> None:
    values = {
        "repository": {
            "branch": harness.repository_branch,
            "commit": harness.repository_commit,
        },
        "run": {"artifact_key": harness.artifact_key, "id": harness.run_id},
        "environment": harness.environment,
    }
    for label, value in values.items():
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if redact_text(text, harness.private_values) != text:
            raise EvidenceValidationError(f"{label} metadata is not portable")
    for record in harness.command_records:
        for key, value in record.items():
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if redact_text(text, harness.private_values) != text:
                raise EvidenceValidationError(
                    f"command {record['id']!r} field {key!r} is not portable"
                )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--repository-branch", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume-reboot", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("WINDOWS_DOCUMENTED_PUBLISH_EVIDENCE_UNAVAILABLE", file=sys.stderr)
        return 2
    source_snapshot = source_snapshot_sha256(args.repository_root)
    args.state_root.mkdir(parents=True, exist_ok=True)
    original_cwd = Path.cwd()
    os.chdir(args.state_root)
    run_root = args.state_root / args.run_id
    reboot_root = args.state_root / "reboot"
    additions = {
        "LOCALCAT_PYTHON": str(Path(sys.executable).resolve()),
        "LOCALCAT_REPOSITORY": str(args.repository_root.resolve()),
        "LOCALCAT_REBOOT_ROOT": str(reboot_root.resolve()),
        "LOCALCAT_RUN_ROOT": str(run_root.resolve()),
    }
    try:
        harness = EvidenceHarness(
            args.artifact_root,
            repository_commit=args.repository_commit,
            repository_branch=args.repository_branch,
            run_id=args.run_id,
            environment=_environment(),
            repository_root=args.repository_root,
            scenario_contract_key=CONTRACT_KEY,
            private_values={
                "reboot-state-root": str(reboot_root.resolve()),
                "run-state-root": str(run_root.resolve()),
            },
        )
        for command_id in (
            "publish-success",
            "instruction-faults",
            "process-termination",
            "application-restart",
        ):
            record = harness.run_command(
                command_id,
                COMMANDS[command_id],
                cwd=args.state_root,
                environment_additions=additions,
            )
            if record["status"] != "PASS":
                raise EvidenceValidationError(f"{command_id} command failed")
            output = _command_output(harness, record)
            if command_id in {"publish-success", "application-restart"}:
                _require_success(output)
                _record(
                    harness,
                    operation_id=f"{command_id}-event",
                    command_id=command_id,
                    operation=command_id,
                    phase="terminal-reproof",
                    result="PASS",
                    recovery="NEW",
                )
            else:
                kind = "instruction" if command_id == "instruction-faults" else "terminate"
                _require_matrix(output, kind)
                _record(
                    harness,
                    operation_id=f"{command_id}-event",
                    command_id=command_id,
                    operation=command_id,
                    phase="create-through-terminal",
                    result="OBSERVED",
                    recovery="RECOVERY_REQUIRED",
                )

        reboot_verdict = "NOT_RUN"
        reboot_command = "reboot-resume" if args.resume_reboot else "reboot-prepare"
        record = harness.run_command(
            reboot_command,
            COMMANDS[reboot_command],
            cwd=args.state_root,
            environment_additions=additions,
        )
        if record["status"] != "PASS":
            raise EvidenceValidationError(f"{reboot_command} command failed")
        reboot_output = _command_output(harness, record)
        if (
            type(reboot_output) is not dict
            or reboot_output.get("source_snapshot_sha256") != source_snapshot
        ):
            raise EvidenceValidationError("reboot source snapshot mismatch")
        if args.resume_reboot:
            if reboot_output.get("reboot") != "PASS":
                # Omitting the expected event makes the mandatory scenario NOT_RUN.
                reboot_verdict = "NOT_RUN"
            else:
                reboot_verdict = "PASS"
                _record(
                    harness,
                    operation_id="normal-reboot-event",
                    command_id="reboot-resume",
                    operation="normal-os-reboot",
                    phase="post-reboot-reproof",
                    result="PASS",
                    recovery="NEW",
                )
        else:
            if reboot_output.get("reboot") != "PREPARED":
                raise EvidenceValidationError("normal reboot preparation failed")
            _record(
                harness,
                operation_id="reboot-prepare-event",
                command_id="reboot-prepare",
                operation="normal-os-reboot",
                phase="pre-reboot-prepared",
                result="PASS",
                recovery="NEW",
            )

        matrix = harness.write_release_matrix()
        _write_checklists(args.artifact_root, reboot_verdict, source_snapshot)
        harness.register_release_outputs(
            matrix=matrix,
            windows_fs_lock_checklist="windows-fs-lock-adaptation-checklist.md",
            frozen_source_packaging_checklist="frozen-source-packaging-checklist.md",
        )
        _preflight_portable_metadata(harness)
        manifest_path = harness.finalize()
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest = validate_evidence_bundle(
            args.artifact_root,
            repository_root=args.repository_root,
            scenario_contract_key=harness.scenario_contract_key,
            expected_repository_commit=args.repository_commit,
            expected_scenario_contract_sha256=harness.scenario_contract_sha256,
            expected_manifest_sha256=manifest_sha256,
        )
    except (EvidenceValidationError, OSError, ValueError, RuntimeError) as error:
        os.chdir(original_cwd)
        print(f"WINDOWS_DOCUMENTED_PUBLISH_EVIDENCE_FAILED: {error}", file=sys.stderr)
        return 1

    os.chdir(original_cwd)
    matrix_value = json.loads((args.artifact_root / matrix).read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "manifest_sha256": manifest_sha256,
                "reboot": reboot_verdict,
                "run_status": manifest["run"]["status"],
                "source_snapshot_sha256": source_snapshot,
                "scenarios": {
                    row["id"]: row["verdict"] for row in matrix_value["scenarios"]
                },
                "validation_scope": "INTERNAL_CONSISTENCY_ONLY",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
