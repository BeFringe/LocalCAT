"""Integrity tests for the executable Feature 5 fault matrix."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

import tools.validate_tm_fault_matrix as validator

from tests.fault_matrix_registry import (
    FAULT_MATRIX_ROWS,
    FAULT_MATRIX_SCHEMA_VERSION,
    SNAPSHOT_PROCESS_DEATH_BOUNDARIES,
    TASK_9_1_ROWS,
    TASK_9_2_ROWS,
    fault_matrix_registry_digest,
    fault_matrix_source_fingerprint,
    fault_matrix_source_paths,
)


_ROOT = Path(__file__).resolve().parent.parent
_EVIDENCE_PATH = _ROOT / "fault_matrix_evidence.json"
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_evidence() -> dict[str, object]:
    raw = _EVIDENCE_PATH.read_text(encoding="utf-8")
    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token: {token}")
        ),
    )
    if type(value) is not dict:
        raise TypeError("fault matrix evidence must be an object")
    return value


def _flatten(suite: unittest.TestSuite) -> tuple[unittest.TestCase, ...]:
    tests: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten(item))
        elif isinstance(item, unittest.TestCase):
            tests.append(item)
        else:
            raise TypeError("unittest loader returned an invalid item")
    return tuple(tests)


class FaultMatrixRegistryTests(unittest.TestCase):
    def test_task_9_1_registry_is_closed_and_unique(self) -> None:
        self.assertEqual(len(TASK_9_1_ROWS), 29)
        self.assertTrue(all(row.task == "9.1" for row in TASK_9_1_ROWS))

    def test_task_9_2_registry_is_closed_and_unique(self) -> None:
        self.assertEqual(len(TASK_9_2_ROWS), 30)
        self.assertTrue(all(row.task == "9.2" for row in TASK_9_2_ROWS))
        self.assertEqual(
            FAULT_MATRIX_ROWS,
            TASK_9_1_ROWS + TASK_9_2_ROWS,
        )
        row_ids = tuple(row.row_id for row in FAULT_MATRIX_ROWS)
        self.assertEqual(len(row_ids), len(set(row_ids)))
        for task_rows in (TASK_9_1_ROWS, TASK_9_2_ROWS):
            test_sets = tuple(row.test_ids for row in task_rows)
            self.assertEqual(len(test_sets), len(set(test_sets)))

    def test_process_death_boundary_catalog_is_closed_and_unique(self) -> None:
        self.assertEqual(len(SNAPSHOT_PROCESS_DEATH_BOUNDARIES), 15)
        boundary_ids = tuple(
            item.boundary_id for item in SNAPSHOT_PROCESS_DEATH_BOUNDARIES
        )
        self.assertEqual(len(boundary_ids), len(set(boundary_ids)))
        self.assertEqual(
            {
                (item.seam, item.ordinal)
                for item in SNAPSHOT_PROCESS_DEATH_BOUNDARIES
            },
            {
                ("file_fsync", 1),
                ("file_fsync", 2),
                ("file_fsync", 3),
                ("file_fsync", 4),
                ("register", 1),
                ("handoff", 1),
                ("replace", 1),
                ("replace", 2),
                ("directory_fsync", 1),
                ("directory_fsync", 2),
                ("directory_fsync", 3),
                ("complete", 1),
                ("cleanup_unlink", 1),
                ("cleanup_unlink", 2),
                ("clear", 1),
            },
        )

    def test_every_reference_resolves_to_one_exact_test(self) -> None:
        loader = unittest.TestLoader()
        for row in FAULT_MATRIX_ROWS:
            for test_id in row.test_ids:
                tests = _flatten(loader.loadTestsFromName(test_id))
                self.assertEqual(len(tests), 1, test_id)
                self.assertEqual(tests[0].id(), test_id)

    def test_every_bound_source_file_exists_inside_repository(self) -> None:
        root = _ROOT.resolve(strict=True)
        for relative in fault_matrix_source_paths():
            path = (root / relative).resolve(strict=True)
            self.assertIn(root, path.parents)
            self.assertTrue(path.is_file(), relative)


class FaultMatrixEvidenceTests(unittest.TestCase):
    def test_evidence_is_closed_fresh_and_complete(self) -> None:
        evidence = _load_evidence()
        self.assertEqual(
            set(evidence),
            {
                "generated_at_utc",
                "registry_digest",
                "rows",
                "schema_version",
                "source_files",
                "source_fingerprint",
                "summary",
                "tasks",
            },
        )
        self.assertEqual(
            evidence["schema_version"],
            FAULT_MATRIX_SCHEMA_VERSION,
        )
        generated = evidence["generated_at_utc"]
        if type(generated) is not str:
            raise AssertionError("generated_at_utc must be a string")
        self.assertIsNotNone(_UTC.fullmatch(generated))
        registry_digest = evidence["registry_digest"]
        self.assertEqual(registry_digest, fault_matrix_registry_digest())
        if type(registry_digest) is not str:
            raise AssertionError("registry_digest must be a string")
        self.assertIsNotNone(_SHA256.fullmatch(registry_digest))
        self.assertEqual(evidence["tasks"], ["9.1", "9.2"])

        raw_sources = evidence["source_files"]
        if type(raw_sources) is not list:
            raise AssertionError("source_files must be a list")
        source_files: list[tuple[str, str]] = []
        for raw_item in raw_sources:
            if type(raw_item) is not dict:
                raise AssertionError("source file facts must be objects")
            item = cast(dict[str, object], raw_item)
            self.assertEqual(set(item), {"path", "sha256"})
            relative = item["path"]
            digest = item["sha256"]
            if type(relative) is not str or type(digest) is not str:
                raise AssertionError("source file path/digest must be strings")
            self.assertIsNotNone(_SHA256.fullmatch(digest))
            path = (_ROOT / relative).resolve(strict=True)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                digest,
            )
            source_files.append((relative, digest))
        self.assertEqual(
            tuple(relative for relative, _digest in source_files),
            fault_matrix_source_paths(),
        )
        self.assertEqual(
            evidence["source_fingerprint"],
            fault_matrix_source_fingerprint(
                registry_digest,
                tuple(source_files),
            ),
        )

        raw_rows = evidence["rows"]
        if type(raw_rows) is not list:
            raise AssertionError("rows must be a list")
        self.assertEqual(len(raw_rows), len(FAULT_MATRIX_ROWS))
        for expected, raw_observed in zip(FAULT_MATRIX_ROWS, raw_rows):
            if type(raw_observed) is not dict:
                raise AssertionError("row evidence must be an object")
            observed = cast(dict[str, object], raw_observed)
            self.assertEqual(
                set(observed),
                {"row_id", "status", "test_ids"},
            )
            self.assertEqual(observed["row_id"], expected.row_id)
            self.assertEqual(observed["status"], "PASS")
            self.assertEqual(observed["test_ids"], list(expected.test_ids))

        summary = evidence["summary"]
        if type(summary) is not dict:
            raise AssertionError("summary must be an object")
        self.assertEqual(
            summary,
            {
                "passed_rows": len(FAULT_MATRIX_ROWS),
                "referenced_tests": sum(
                    len(row.test_ids) for row in FAULT_MATRIX_ROWS
                ),
                "total_rows": len(FAULT_MATRIX_ROWS),
            },
        )

    def test_evidence_parser_rejects_duplicates_and_nonfinite(self) -> None:
        with self.assertRaises(ValueError):
            json.loads(
                '{"schema":"a","schema":"b"}',
                object_pairs_hook=_reject_duplicate_keys,
            )
        with self.assertRaises(ValueError):
            json.loads(
                '{"value":NaN}',
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(token)
                ),
            )

    def test_validator_rejects_alternate_root_and_arbitrary_emit_before_tests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            alternate = Path(temporary).resolve()
            with patch.object(
                validator,
                "_run_row",
                side_effect=AssertionError("tests must not execute"),
            ):
                with self.assertRaisesRegex(ValueError, "repository root"):
                    validator.main(["--repository-root", str(alternate)])
                with self.assertRaisesRegex(ValueError, "canonical output"):
                    validator.main(["--emit", "AGENTS.md"])

    def test_validator_source_walk_rejects_final_and_intermediate_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real"
            real.mkdir()
            source = real / "source.py"
            source.write_text("pass\n", encoding="utf-8")
            final_alias = root / "alias.py"
            final_alias.symlink_to(source)
            parent_alias = root / "alias-parent"
            parent_alias.symlink_to(real, target_is_directory=True)

            self.assertEqual(
                validator._strict_source_file(root, "real/source.py"),
                source,
            )
            self.assertTrue(stat.S_ISREG(os.lstat(source).st_mode))
            with self.assertRaisesRegex(ValueError, "source"):
                validator._strict_source_file(root, "alias.py")
            with self.assertRaisesRegex(ValueError, "source"):
                validator._strict_source_file(
                    root,
                    "alias-parent/source.py",
                )
            with self.assertRaisesRegex(ValueError, "canonical"):
                validator._strict_source_file(root, "real/../real/source.py")

    def test_validator_rejects_symlink_or_nonregular_evidence_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ordinary = root / "ordinary.json"
            ordinary.write_text("{}\n", encoding="utf-8")
            alias = root / "evidence.json"
            alias.symlink_to(ordinary)
            with self.assertRaisesRegex(ValueError, "regular file"):
                validator._validate_evidence_target(alias)
            with self.assertRaisesRegex(ValueError, "regular file"):
                validator._validate_evidence_target(root)


if __name__ == "__main__":
    unittest.main()
