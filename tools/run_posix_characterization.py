from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import unittest


ROOT = Path(__file__).parents[1]
DEFAULT_MATRIX = (
    ROOT
    / ".kiro"
    / "specs"
    / "windows-platform-enablement"
    / "posix-characterization-matrix.json"
)


def _git_head() -> str | None:
    try:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _environment() -> dict[str, object]:
    return {
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform": platform.platform(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_executable": str(Path(sys.executable).resolve()),
        "git_head": _git_head(),
    }


def _oracle_ids(matrix: dict[str, object]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for owner in matrix["owners"]:  # type: ignore[index]
        for invariant in owner["business_invariants"]:  # type: ignore[index]
            test_id = invariant["oracle_test"]
            if test_id not in seen:
                ordered.append(test_id)
                seen.add(test_id)
    return tuple(ordered)


def _emit(report: dict[str, object]) -> None:
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the migration-pinned POSIX filesystem characterization oracle."
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    arguments = parser.parse_args()
    matrix_path = arguments.matrix.resolve()
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    baseline = matrix["baseline"]
    environment = _environment()

    if os.name != "posix" or sys.platform not in {"darwin", "linux"}:
        _emit(
            {
                "schema_version": 1,
                "status": "STATIC_ONLY",
                "reason": "POSIX_RUNTIME_REQUIRED",
                "baseline_commit": baseline["commit"],
                "baseline_tree": baseline["tree"],
                "environment": environment,
                "tests_run": 0,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "expected_failures": 0,
                "unexpected_successes": 0,
            }
        )
        return 2

    test_ids = _oracle_ids(matrix)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(test_ids)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    skipped = len(result.skipped)
    expected_failures = len(result.expectedFailures)
    unexpected_successes = len(result.unexpectedSuccesses)
    passed = (
        result.wasSuccessful()
        and result.testsRun == len(test_ids)
        and skipped == 0
        and expected_failures == 0
        and unexpected_successes == 0
    )
    _emit(
        {
            "schema_version": 1,
            "status": "PASS" if passed else "FAIL",
            "reason": None if passed else "ORACLE_NOT_CLEAN",
            "baseline_commit": baseline["commit"],
            "baseline_tree": baseline["tree"],
            "environment": environment,
            "oracle_tests": list(test_ids),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": skipped,
            "expected_failures": expected_failures,
            "unexpected_successes": unexpected_successes,
        }
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
