from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from tools import validate_windows_tm_current_source_release as validator


class WindowsTMCurrentSourceValidatorTests(unittest.TestCase):
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
            facts, observed = validator._validate_benchmark_input(validator.ROOT)
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
            validator._validate_benchmark_input(validator.ROOT)


if __name__ == "__main__":
    unittest.main()
