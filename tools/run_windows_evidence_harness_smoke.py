"""Materialize the three tracked Task 1.2 evidence-harness smoke lanes."""

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
import tempfile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.validate_windows_release import (
    EvidenceHarness,
    EvidenceValidationError,
    validate_evidence_bundle,
)


LANES = {
    "success": {
        "contract": "packaging/windows/evidence-scenarios/evidence-harness-success.json",
        "command_id": "evidence-success",
        "command": "Write-Output 'LOCALCAT_EVIDENCE_SUCCESS'",
        "timeout": 10.0,
        "windowed": False,
    },
    "expected-failure": {
        "contract": "packaging/windows/evidence-scenarios/evidence-harness-expected-failure.json",
        "command_id": "evidence-expected-failure",
        "command": "Write-Output 'LOCALCAT_EVIDENCE_EXPECTED_FAILURE'; exit 3",
        "timeout": 10.0,
        "windowed": False,
    },
    "interrupted": {
        "contract": "packaging/windows/evidence-scenarios/evidence-harness-interrupted.json",
        "command_id": "evidence-interrupted",
        "command": "Write-Output 'LOCALCAT_EVIDENCE_INTERRUPTED'; Start-Sleep -Seconds 5",
        "timeout": 0.1,
        "windowed": True,
    },
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


def _write_summaries(root: Path, lane: str) -> None:
    (root / "windows-fs-lock-adaptation-checklist.md").write_text(
        "# Task 1.2 evidence harness smoke\n\n"
        f"Lane `{lane}` validates portable process/log facts only; it does not "
        "claim a Windows filesystem or lock capability.\n",
        encoding="utf-8",
    )
    (root / "frozen-source-packaging-checklist.md").write_text(
        "# Task 1.2 packaging evidence smoke\n\n"
        f"Lane `{lane}` validates the evidence container only; it is not a "
        "PyInstaller, frozen-source, W3, or release result.\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one tracked LocalCAT Windows evidence-harness smoke lane."
    )
    parser.add_argument("lane", choices=sorted(LANES))
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--repository-branch", required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("WINDOWS_EVIDENCE_SMOKE_UNAVAILABLE: Windows is required", file=sys.stderr)
        return 2
    if args.artifact_root.exists():
        print("WINDOWS_EVIDENCE_SMOKE_INPUT_ERROR: artifact root already exists", file=sys.stderr)
        return 2

    lane = LANES[args.lane]
    try:
        harness = EvidenceHarness(
            args.artifact_root,
            repository_commit=args.repository_commit,
            repository_branch=args.repository_branch,
            run_id=args.run_id,
            environment=_environment(),
            repository_root=args.repository_root,
            scenario_contract_key=str(lane["contract"]),
        )
        with tempfile.TemporaryDirectory(prefix="localcat-evidence-nonrepo-") as directory:
            harness.run_command(
                str(lane["command_id"]),
                str(lane["command"]),
                cwd=Path(directory),
                timeout_seconds=float(lane["timeout"]),
                windowed=bool(lane["windowed"]),
            )
        matrix = harness.write_release_matrix()
        _write_summaries(args.artifact_root, args.lane)
        harness.register_release_outputs(
            matrix=matrix,
            windows_fs_lock_checklist="windows-fs-lock-adaptation-checklist.md",
            frozen_source_packaging_checklist="frozen-source-packaging-checklist.md",
        )
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
    except (EvidenceValidationError, OSError, ValueError) as error:
        print(f"WINDOWS_EVIDENCE_SMOKE_FAILED: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "artifact_key": manifest["run"]["artifact_key"],
                "contract_sha256": harness.scenario_contract_sha256,
                "lane": args.lane,
                "manifest_sha256": manifest_sha256,
                "run_status": manifest["run"]["status"],
                "validation_scope": "INTERNAL_CONSISTENCY_ONLY",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
