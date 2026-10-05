from __future__ import annotations

from pathlib import Path
import io
from contextlib import chdir, redirect_stdout
import tempfile
import unittest
from unittest import mock

from tools import validate_windows_tm_current_source_release as validator


class WindowsTMCurrentSourceValidatorTests(unittest.TestCase):
    def test_relative_c3b_input_uses_invocation_directory_for_output_protection(self) -> None:
        (validator.ROOT / "artifacts" / "windows").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validator.ROOT / "artifacts" / "windows") as temporary:
            run = Path(temporary)
            matrix = run / "matrix.json"
            matrix.write_bytes(b"input\n")
            argv = [
                "--repository-commit", "a" * 40,
                "--benchmark-evidence", "benchmark_tm_evidence.json",
                "--c3b-matrix-sha256", "b" * 64,
                "--temp-root", str(run), "--emit", str(matrix),
            ]
            with (
                chdir(run),
                mock.patch.object(validator, "_validate_runtime", return_value={}),
                mock.patch.object(validator, "_validate_temp_root", return_value=run),
                mock.patch.object(validator, "_validate_benchmark_input", side_effect=AssertionError("input checks must precede execution")) as benchmark,
                mock.patch.object(validator, "_run_row_process") as worker,
                mock.patch.object(validator, "_atomic_write") as publish,
            ):
                for spelling in (matrix.name, str(Path("..") / run.name / matrix.name)):
                    with self.subTest(spelling=spelling), self.assertRaisesRegex(ValueError, "replace an input"):
                        validator.main([*argv, "--c3b-matrix", spelling])
                benchmark.assert_not_called()
                worker.assert_not_called()
                publish.assert_not_called()
            self.assertEqual(matrix.read_bytes(), b"input\n")

    def test_worker_mode_does_not_require_parent_artifact_arguments(self) -> None:
        row = next(iter(validator._ROW_BY_ID))
        with (
            mock.patch.object(validator, "_worker_result", return_value=({"row": row}, 1)) as worker,
            mock.patch.object(validator, "_validate_runtime") as parent,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(validator.main(["--worker-row", row]), 1)
            worker.assert_called_once_with(validator._ROW_BY_ID[row])
            parent.assert_not_called()

    def test_parent_output_cannot_replace_either_input_before_workers(self) -> None:
        (validator.ROOT / "artifacts" / "windows").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validator.ROOT / "artifacts" / "windows") as temporary:
            run = Path(temporary)
            benchmark = run / "benchmark.json"
            matrix = run / "matrix.json"
            for path in (benchmark, matrix):
                path.write_bytes(b"input\n")
            argv = [
                "--repository-commit", "a" * 40,
                "--benchmark-evidence", str(benchmark),
                "--c3b-matrix", str(matrix), "--c3b-matrix-sha256", "b" * 64,
                "--temp-root", str(run),
            ]
            with (
                mock.patch.object(validator, "_validate_runtime", return_value={}),
                mock.patch.object(validator, "_validate_temp_root", return_value=run),
                mock.patch.object(validator, "_run_row_process") as worker,
            ):
                for path in (benchmark, matrix):
                    with self.subTest(path=path), self.assertRaisesRegex(ValueError, "replace an input"):
                        validator.main([*argv, "--emit", str(path)])
                    self.assertEqual(path.read_bytes(), b"input\n")
                worker.assert_not_called()

    def test_registry_and_source_inventory_are_bounded(self) -> None:
        self.assertRegex(validator.registry_digest(), r"^[0-9a-f]{64}$")
        paths = validator.source_paths()
        self.assertEqual(paths, tuple(sorted(set(paths))))
        self.assertIn("tm_engine.py", paths)
        self.assertIn("tests/test_tm_schema_upgrade_windows.py", paths)
        self.assertIn("tests/windows_tm_current_source_retrieval_worker.py", paths)
        self.assertIn("tools/run_windows_c3b_capability_gate.py", paths)
        self.assertIn("tools/validate_windows_tm_current_source_release.py", paths)
        self.assertNotIn(validator.EVIDENCE_NAME, paths)

    def test_c3b_handoff_delegates_to_owner_strict_validator(self) -> None:
        expected = {
            "matrix_sha256": "d" * 64,
            "repository_commit": "a" * 40,
            "source_snapshot_sha256": "b" * 64,
            "status": "PASS",
        }
        matrix = Path("C:/evidence/c3b-matrix.json")
        with (
            mock.patch.object(
                validator,
                "source_snapshot_sha256",
                return_value="b" * 64,
            ),
            mock.patch.object(
                validator,
                "validate_aggregate_matrix",
                return_value=expected,
            ) as strict,
        ):
            result = validator._validate_c3b_matrix(
                matrix,
                expected_sha256="d" * 64,
                repository_commit="a" * 40,
                root=validator.ROOT,
            )
        self.assertEqual(result, expected)
        strict.assert_called_once_with(
            matrix,
            expected_sha256="d" * 64,
            repository_commit="a" * 40,
            expected_source_snapshot="b" * 64,
        )

    def test_benchmark_input_accepts_valid_windows_no_go(self) -> None:
        report_fts5 = mock.Mock(
            environment=(
                ("fts5_enabled", "true"),
                ("os", "Windows"),
                ("python_version", "3.14.7"),
                ("rss_platform", "windows"),
                ("rss_raw_unit", "bytes"),
            )
        )
        report_fallback = mock.Mock(
            environment=(
                ("fts5_enabled", "false"),
                ("os", "Windows"),
                ("python_version", "3.14.7"),
                ("rss_platform", "windows"),
                ("rss_raw_unit", "bytes"),
            )
        )
        bundle = mock.Mock(
            bundle_digest="a" * 64,
            implementation_fingerprint="b" * 64,
            fts5=mock.Mock(report=report_fts5),
            fallback=mock.Mock(report=report_fallback),
            suite_report=mock.Mock(passed=False),
        )
        with (
            mock.patch.object(
                validator,
                "_strict_read",
                return_value=(b"{}", "c" * 64),
            ),
            mock.patch.object(
                validator,
                "benchmark_evidence_bundle_from_json",
                return_value=bundle,
            ),
            mock.patch.object(
                validator,
                "benchmark_implementation_fingerprint",
                return_value="b" * 64,
            ),
        ):
            facts, observed = validator._validate_benchmark_input(validator.ROOT, "benchmark_tm_evidence.json")
        self.assertIs(observed, bundle)
        self.assertEqual(facts["benchmark_suite_decision"], "NO_GO")

    def test_benchmark_input_rejects_non_windows_environment(self) -> None:
        report = mock.Mock(
            environment=(
                ("fts5_enabled", "true"),
                ("os", "Linux"),
                ("python_version", "3.14.7"),
                ("rss_platform", "linux"),
                ("rss_raw_unit", "kib"),
            )
        )
        bundle = mock.Mock(
            implementation_fingerprint="b" * 64,
            fts5=mock.Mock(report=report),
            fallback=mock.Mock(report=report),
        )
        with (
            mock.patch.object(
                validator,
                "_strict_read",
                return_value=(b"{}", "c" * 64),
            ),
            mock.patch.object(
                validator,
                "benchmark_evidence_bundle_from_json",
                return_value=bundle,
            ),
            mock.patch.object(
                validator,
                "benchmark_implementation_fingerprint",
                return_value="b" * 64,
            ),
            self.assertRaisesRegex(ValueError, "not Windows CPython 3.14"),
        ):
            validator._validate_benchmark_input(validator.ROOT, "benchmark_tm_evidence.json")


if __name__ == "__main__":
    unittest.main()
