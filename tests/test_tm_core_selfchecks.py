"""Executable compatibility coverage for the retained Core self-checks."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_ROOT = Path(__file__).resolve().parent.parent


def _run_script(script_name: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(_ROOT)
    with tempfile.TemporaryDirectory() as temporary:
        working_directory = Path(temporary)
        result = subprocess.run(
            [sys.executable, "-B", str(_ROOT / script_name)],
            cwd=working_directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if tuple(working_directory.iterdir()):
            raise AssertionError(
                f"{script_name} left files in its disposable cwd"
            )
        return result


class TMCoreSelfCheckTests(unittest.TestCase):
    def test_tm_engine_script_selfcheck_uses_disposable_cwd(self) -> None:
        result = _run_script("tm_engine.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All tests passed successfully.", result.stdout)

    def test_stress_runner_main_selfcheck(self) -> None:
        result = _run_script("stress_runner.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Structural Integrity Test Complete.", result.stdout)

    def test_translation_runner_main_selfcheck(self) -> None:
        result = _run_script("translation_runner.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Integration Test Complete.", result.stdout)


if __name__ == "__main__":
    unittest.main()
