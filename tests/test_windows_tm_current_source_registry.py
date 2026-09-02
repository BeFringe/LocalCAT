from __future__ import annotations

import unittest

from tests.windows_tm_current_source_registry import (
    WINDOWS_TM_CURRENT_SOURCE_ROWS,
    require_strict_success,
    resolve_row_tests,
    sorted_test_ids_sha256,
)


class WindowsTMCurrentSourceRegistryTests(unittest.TestCase):
    def test_registry_resolves_the_closed_220_test_inventory(self) -> None:
        self.assertEqual(
            tuple(row.row_id for row in WINDOWS_TM_CURRENT_SOURCE_ROWS),
            (
                "activation_recovery",
                "private_attack",
                "replacement_schema",
                "snapshot",
                "retrieval_benchmark",
            ),
        )
        observed_total = 0
        all_test_ids: list[str] = []
        for row in WINDOWS_TM_CURRENT_SOURCE_ROWS:
            with self.subTest(row=row.row_id):
                tests = resolve_row_tests(row)
                self.assertEqual(len(tests), row.expected_count)
                self.assertEqual(
                    sorted_test_ids_sha256(tests),
                    row.sorted_test_ids_sha256,
                )
                observed_total += len(tests)
                all_test_ids.extend(test.id() for test in tests)
        self.assertEqual(observed_total, 220)
        self.assertEqual(len(all_test_ids), len(set(all_test_ids)))

    def test_strict_result_validation_never_counts_skip_as_pass(self) -> None:
        class PlaceholderTest(unittest.TestCase):
            def runTest(self) -> None:
                return None

        row = WINDOWS_TM_CURRENT_SOURCE_ROWS[-1]
        passing = unittest.TestResult()
        passing.testsRun = row.expected_count
        require_strict_success(row, passing)

        skipped = unittest.TestResult()
        skipped.testsRun = row.expected_count
        skipped.addSkip(PlaceholderTest(), "environment missing")
        with self.assertRaisesRegex(AssertionError, "contains skipped tests"):
            require_strict_success(row, skipped)


if __name__ == "__main__":
    unittest.main()
