"""Closed Windows current-source TM release test inventory."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import unittest


@dataclass(frozen=True, slots=True)
class WindowsTMCurrentSourceRow:
    row_id: str
    selectors: tuple[str, ...]
    expected_count: int
    sorted_test_ids_sha256: str


WINDOWS_TM_CURRENT_SOURCE_ROWS = (
    WindowsTMCurrentSourceRow(
        row_id="activation_recovery",
        selectors=(
            "tests.test_tm_initial_activation_windows",
            "tests.test_tm_portable_publication_windows",
            "tests.test_tm_portable_recovery_windows",
            "tests.test_tm_portable_fresh_recovery_journal_windows",
            "tests.test_tm_stage_candidate_copy_windows",
        ),
        expected_count=93,
        sorted_test_ids_sha256=(
            "b85c6bd139e3182f63cac9eed5074be2ff592d5daa75d706d38efd55d90d4215"
        ),
    ),
    WindowsTMCurrentSourceRow(
        row_id="private_attack",
        selectors=(
            "tests.test_tm_portable_recovery_attack_windows",
        ),
        expected_count=15,
        sorted_test_ids_sha256=(
            "0d0ebbbefc3ee5b2b7f235625240ab21c534e146c338a27543a5455b1a1a9be6"
        ),
    ),
    WindowsTMCurrentSourceRow(
        row_id="replacement_schema",
        selectors=(
            "tests.test_tm_explicit_import_rebuild_windows",
            "tests.test_tm_portable_replacement_owner_windows",
            "tests.test_tm_schema_upgrade_windows",
        ),
        expected_count=26,
        sorted_test_ids_sha256=(
            "71e452f05ac31aa7a8538913f6a69bd9f2ab218d7f5ac77cb6f414e40bc28bd4"
        ),
    ),
    WindowsTMCurrentSourceRow(
        row_id="snapshot",
        selectors=(
            "tests.test_tm_export_windows",
            "tests.test_tm_snapshot_recovery_windows",
            "tests.test_tm_snapshot_refresh_windows",
        ),
        expected_count=76,
        sorted_test_ids_sha256=(
            "3d21427fdf692cd66ba4a2887eb43046cd5c04f39e10f02358e0ac517f13d18e"
        ),
    ),
    WindowsTMCurrentSourceRow(
        row_id="retrieval_benchmark",
        selectors=(
            "tests.test_tm_benchmark_platform_windows",
            "tests.test_tm_current_source_retrieval_windows",
        ),
        expected_count=10,
        sorted_test_ids_sha256=(
            "8db2d3c0762b1507e92234ea44e9ed30d21a94e9bd38749b1871e9bcccf5f16e"
        ),
    ),
)


def _flatten(suite: unittest.TestSuite) -> tuple[unittest.TestCase, ...]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten(item))
        elif isinstance(item, unittest.TestCase):
            tests.append(item)
        else:
            raise TypeError("current-source selector returned a foreign test")
    return tuple(tests)


def resolve_row_tests(
    row: WindowsTMCurrentSourceRow,
) -> tuple[unittest.TestCase, ...]:
    if type(row) is not WindowsTMCurrentSourceRow:
        raise TypeError("current-source row must be exact registry type")
    loader = unittest.defaultTestLoader
    tests = tuple(
        test
        for selector in row.selectors
        for test in _flatten(loader.loadTestsFromName(selector))
    )
    if any(type(test).__module__ == "unittest.loader" for test in tests):
        raise AssertionError(f"{row.row_id} contains an unresolved selector")
    test_ids = tuple(test.id() for test in tests)
    if len(test_ids) != len(set(test_ids)):
        raise AssertionError(f"{row.row_id} contains duplicate test ids")
    return tests


def sorted_test_ids_sha256(tests: tuple[unittest.TestCase, ...]) -> str:
    payload = (
        "\n".join(sorted(test.id() for test in tests)) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_strict_success(
    row: WindowsTMCurrentSourceRow,
    result: unittest.TestResult,
) -> None:
    if type(row) is not WindowsTMCurrentSourceRow:
        raise TypeError("current-source row must be exact registry type")
    if not isinstance(result, unittest.TestResult):
        raise TypeError("current-source result must be unittest.TestResult")
    if result.testsRun != row.expected_count:
        raise AssertionError(f"{row.row_id} executed an incomplete inventory")
    if result.skipped:
        raise AssertionError(f"{row.row_id} contains skipped tests")
    if result.expectedFailures or result.unexpectedSuccesses:
        raise AssertionError(f"{row.row_id} contains non-PASS outcomes")
    if result.errors or result.failures:
        raise AssertionError(f"{row.row_id} did not pass")


__all__ = [
    "WINDOWS_TM_CURRENT_SOURCE_ROWS",
    "WindowsTMCurrentSourceRow",
    "require_strict_success",
    "resolve_row_tests",
    "sorted_test_ids_sha256",
]
