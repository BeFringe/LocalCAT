"""Integrity tests for the 86-criterion Feature 5 release decision."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

import tools.validate_tm_release_criteria as validator

from tests.acceptance_matrix_registry import ACCEPTANCE_MATRIX_ROWS
from tests.fault_matrix_registry import FAULT_MATRIX_ROWS
from tests.release_criteria_registry import (
    BENCHMARK_CLAIMS,
    RELEASE_CRITERIA_BINDINGS,
    RELEASE_CRITERIA_SCHEMA_VERSION,
    parse_requirement_criteria,
    release_criteria_registry_digest,
    release_criteria_source_fingerprint,
    release_criteria_source_paths,
)
from tm_benchmark_gate import benchmark_evidence_bundle_from_json


_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS = (
    _ROOT / ".kiro/specs/tm-storage-retrieval-index/requirements.md"
)
_EVIDENCE = _ROOT / "release_criteria_evidence.json"
_BENCHMARK = _ROOT / "benchmark_tm_evidence.json"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_evidence() -> dict[str, object]:
    value = json.loads(
        _EVIDENCE.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token: {token}")
        ),
    )
    if type(value) is not dict:
        raise TypeError("release evidence must be an object")
    return cast(dict[str, object], value)


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


class ReleaseCriteriaRegistryTests(unittest.TestCase):
    def test_release_owner_source_inventory_is_closed(self) -> None:
        paths = release_criteria_source_paths()
        self.assertEqual(paths, tuple(sorted(set(paths))))
        self.assertIn("tools/validate_tm_release_criteria.py", paths)
        self.assertIn("tests/release_criteria_registry.py", paths)
        self.assertIn("tests/test_editor_controller_writes.py", paths)
        source_files = tuple(
            (path, hashlib.sha256((_ROOT / path).read_bytes()).hexdigest())
            for path in paths
        )
        fingerprint = release_criteria_source_fingerprint(
            release_criteria_registry_digest(),
            source_files,
        )
        self.assertIsNotNone(_SHA256.fullmatch(fingerprint))

    def test_requirements_parser_and_registry_are_exactly_86(self) -> None:
        criteria = parse_requirement_criteria(
            _REQUIREMENTS.read_text(encoding="utf-8")
        )
        criterion_ids = tuple(item.criterion_id for item in criteria)
        binding_ids = tuple(
            item.criterion_id for item in RELEASE_CRITERIA_BINDINGS
        )
        self.assertEqual(len(criteria), 86)
        self.assertEqual(binding_ids, criterion_ids)
        self.assertEqual(len(binding_ids), len(set(binding_ids)))
        self.assertEqual(
            Counter(item.split(".")[0] for item in binding_ids),
            Counter(
                {
                    "1": 9,
                    "2": 13,
                    "3": 7,
                    "4": 7,
                    "5": 7,
                    "6": 10,
                    "7": 14,
                    "8": 7,
                    "9": 12,
                }
            ),
        )

    def test_every_reference_resolves_to_closed_evidence(self) -> None:
        acceptance_rows = {row.row_id for row in ACCEPTANCE_MATRIX_ROWS}
        fault_rows = {row.row_id for row in FAULT_MATRIX_ROWS}
        loader = unittest.TestLoader()
        direct_tests: set[str] = set()
        for binding in RELEASE_CRITERIA_BINDINGS:
            for evidence_ref in binding.evidence_refs:
                kind, _separator, value = evidence_ref.partition(":")
                if kind == "acceptance":
                    self.assertIn(value, acceptance_rows)
                elif kind == "fault":
                    self.assertIn(value, fault_rows)
                elif kind == "test":
                    resolved = _flatten(loader.loadTestsFromName(value))
                    self.assertEqual(len(resolved), 1, value)
                    self.assertEqual(resolved[0].id(), value)
                    direct_tests.add(value)
                elif kind == "benchmark":
                    self.assertIn(value, BENCHMARK_CLAIMS)
                else:
                    self.fail(f"unknown evidence kind: {kind}")
        self.assertEqual(len(direct_tests), 12)

    def test_release_execution_replays_every_matrix_test(self) -> None:
        matrix_ids, direct_ids, executed_ids = (
            validator._release_execution_test_ids()
        )
        expected_matrix = tuple(
            dict.fromkeys(
                test_id
                for row in (*ACCEPTANCE_MATRIX_ROWS, *FAULT_MATRIX_ROWS)
                for test_id in row.test_ids
            )
        )
        self.assertEqual(matrix_ids, expected_matrix)
        self.assertEqual(len(direct_ids), 12)
        self.assertEqual(
            executed_ids,
            tuple(dict.fromkeys((*matrix_ids, *direct_ids))),
        )

    def test_parser_rejects_unowned_or_duplicate_criteria(self) -> None:
        with self.assertRaisesRegex(ValueError, "no requirement"):
            parse_requirement_criteria("#### Acceptance Criteria\n1. orphan")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_requirement_criteria(
                "### Requirement 1: Duplicate\n"
                "#### Acceptance Criteria\n"
                "1. first\n"
                "1. second\n"
            )


class ReleaseCriteriaEvidenceTests(unittest.TestCase):
    def test_evidence_is_fresh_complete_and_truthfully_go(self) -> None:
        evidence = _load_evidence()
        self.assertEqual(
            set(evidence),
            {
                "benchmark_blockers",
                "blocked_criteria",
                "generated_at_utc",
                "input_evidence",
                "registry_digest",
                "release_decision",
                "rows",
                "schema_version",
                "source_files",
                "source_fingerprint",
                "summary",
            },
        )
        self.assertEqual(
            evidence["schema_version"],
            RELEASE_CRITERIA_SCHEMA_VERSION,
        )
        generated_at = evidence["generated_at_utc"]
        if type(generated_at) is not str:
            raise AssertionError("generated timestamp must be a string")
        self.assertIsNotNone(_UTC.fullmatch(generated_at))
        registry_digest = release_criteria_registry_digest()
        self.assertEqual(evidence["registry_digest"], registry_digest)
        self.assertIsNotNone(_SHA256.fullmatch(registry_digest))

        raw_inputs = evidence["input_evidence"]
        if type(raw_inputs) is not dict:
            raise AssertionError("input evidence must be an object")
        inputs = cast(dict[str, object], raw_inputs)
        self.assertEqual(
            set(inputs),
            {
                "acceptance_evidence_sha256",
                "acceptance_source_fingerprint",
                "benchmark_bundle_digest",
                "benchmark_evidence_sha256",
                "fault_evidence_sha256",
                "fault_source_fingerprint",
                "release_owner_source_fingerprint",
                "requirements_sha256",
            },
        )
        expected_file_digests = {
            "acceptance_evidence_sha256": hashlib.sha256(
                (_ROOT / "acceptance_matrix_evidence.json").read_bytes()
            ).hexdigest(),
            "benchmark_evidence_sha256": hashlib.sha256(
                _BENCHMARK.read_bytes()
            ).hexdigest(),
            "fault_evidence_sha256": hashlib.sha256(
                (_ROOT / "fault_matrix_evidence.json").read_bytes()
            ).hexdigest(),
            "requirements_sha256": hashlib.sha256(
                _REQUIREMENTS.read_bytes()
            ).hexdigest(),
        }
        for field_name, expected in expected_file_digests.items():
            self.assertEqual(inputs[field_name], expected)
        for value in inputs.values():
            if type(value) is not str:
                raise AssertionError("input digest must be a string")
            self.assertIsNotNone(_SHA256.fullmatch(value))

        raw_source_files = evidence["source_files"]
        if type(raw_source_files) is not list:
            raise AssertionError("release source files must be a list")
        source_files: list[tuple[str, str]] = []
        for raw_source in raw_source_files:
            if type(raw_source) is not dict:
                raise AssertionError("release source fact must be an object")
            source = cast(dict[str, object], raw_source)
            self.assertEqual(set(source), {"path", "sha256"})
            path = source["path"]
            digest = source["sha256"]
            if type(path) is not str or type(digest) is not str:
                raise AssertionError("release source fact must use strings")
            self.assertEqual(
                digest,
                hashlib.sha256((_ROOT / path).read_bytes()).hexdigest(),
            )
            source_files.append((path, digest))
        self.assertEqual(
            tuple(path for path, _digest_value in source_files),
            release_criteria_source_paths(),
        )
        self.assertEqual(
            inputs["release_owner_source_fingerprint"],
            release_criteria_source_fingerprint(
                registry_digest,
                tuple(source_files),
            ),
        )

        benchmark_bundle = benchmark_evidence_bundle_from_json(
            _BENCHMARK.read_text(encoding="utf-8")
        )
        self.assertEqual(
            inputs["benchmark_bundle_digest"],
            benchmark_bundle.bundle_digest,
        )
        fingerprint_payload = dict(inputs)
        fingerprint_payload["registry_digest"] = registry_digest
        self.assertEqual(
            evidence["source_fingerprint"],
            hashlib.sha256(
                validator._canonical_json(fingerprint_payload).encode("utf-8")
            ).hexdigest(),
        )

        criteria = parse_requirement_criteria(
            _REQUIREMENTS.read_text(encoding="utf-8")
        )
        raw_rows = evidence["rows"]
        if type(raw_rows) is not list:
            raise AssertionError("release rows must be a list")
        self.assertEqual(len(raw_rows), 86)
        statuses: dict[str, str] = {}
        for criterion, binding, raw_row in zip(
            criteria,
            RELEASE_CRITERIA_BINDINGS,
            raw_rows,
        ):
            if type(raw_row) is not dict:
                raise AssertionError("release row must be an object")
            row = cast(dict[str, object], raw_row)
            self.assertEqual(
                set(row),
                {
                    "criterion_id",
                    "criterion_text_digest",
                    "evidence_refs",
                    "status",
                },
            )
            self.assertEqual(row["criterion_id"], criterion.criterion_id)
            self.assertEqual(
                row["criterion_text_digest"],
                hashlib.sha256(criterion.text.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(row["evidence_refs"], list(binding.evidence_refs))
            status = row["status"]
            if type(status) is not str:
                raise AssertionError("release status must be a string")
            statuses[criterion.criterion_id] = status
        self.assertEqual(
            Counter(statuses.values()),
            Counter({"PASS": 86}),
        )
        self.assertEqual(statuses["8.2"], "PASS")
        self.assertEqual(statuses["8.3"], "PASS")
        self.assertEqual(evidence["blocked_criteria"], [])
        self.assertEqual(evidence["release_decision"], "GO")
        self.assertEqual(
            evidence["summary"],
            {
                "blocked_criteria": 0,
                "direct_tests": 12,
                "executed_tests": len(
                    validator._release_execution_test_ids()[2]
                ),
                "matrix_tests": len(
                    validator._release_execution_test_ids()[0]
                ),
                "mapped_criteria": 86,
                "passed_criteria": 86,
                "total_criteria": 86,
            },
        )

    def test_benchmark_blockers_are_derived_from_strict_bundle(self) -> None:
        evidence = _load_evidence()
        bundle = benchmark_evidence_bundle_from_json(
            _BENCHMARK.read_text(encoding="utf-8")
        )
        self.assertEqual(
            validator._benchmark_claim_statuses(bundle),
            {
                "CANDIDATE_RECALL": "PASS",
                "ENVIRONMENT": "PASS",
                "EXACT_P95": "PASS",
                "FAILURE_REPORT": "PASS",
                "FUZZY_P95": "PASS",
                "METRICS": "PASS",
                "MIGRATION": "PASS",
                "PEAK_RSS": "PASS",
            },
        )
        self.assertEqual(
            evidence["benchmark_blockers"],
            list(validator._benchmark_blockers(bundle)),
        )
        self.assertEqual(len(cast(list[object], evidence["benchmark_blockers"])), 0)

    def test_evidence_parser_rejects_duplicates_and_nonfinite(self) -> None:
        with self.assertRaises(ValueError):
            json.loads(
                '{"schema":"a","schema":"b"}',
                object_pairs_hook=_reject_duplicate_keys,
            )
        with self.assertRaises(ValueError):
            validator._parse_strict_json('{"value":NaN}')


class ReleaseCriteriaValidatorTests(unittest.TestCase):
    def test_matrix_metadata_is_recomputed_not_self_reported(self) -> None:
        rows = ACCEPTANCE_MATRIX_ROWS
        valid: dict[str, object] = {
            "generated_at_utc": "2026-08-15T00:00:00Z",
            "tasks": sorted({row.task for row in rows}),
            "summary": {
                "passed_rows": len(rows),
                "referenced_tests": sum(len(row.test_ids) for row in rows),
                "total_rows": len(rows),
            },
        }
        validator._validate_matrix_metadata(valid, rows)
        for field, forged in (
            ("generated_at_utc", "not-utc"),
            ("tasks", []),
            ("summary", {"passed_rows": len(rows)}),
        ):
            altered = dict(valid)
            altered[field] = forged
            with self.assertRaises((TypeError, ValueError)):
                validator._validate_matrix_metadata(altered, rows)

    def test_full_pass_satisfies_conditional_failure_report_claim(self) -> None:
        contract = SimpleNamespace(
            candidate_recall_gate=1.0,
            exact_p95_gate_ms=50.0,
            fuzzy_p95_gate_ms=500.0,
            migration_gate_seconds=120.0,
            peak_rss_gate_mib=512.0,
            exact_cohort_count=1200,
            fuzzy_cohort_count=240,
            oracle_query_count=200,
        )
        report = SimpleNamespace(
            candidate_recall=1.0,
            environment=(
                ("cpu", "test"),
                ("os", "test"),
                ("python_version", "test"),
                ("ram_mib", "1024"),
                ("sqlite_version", "test"),
                ("unicode_version", "test"),
            ),
            exact_p95_ms=49.0,
            fuzzy_top10_p95_ms=499.0,
            migration_seconds=119.0,
            peak_rss_mib=511.0,
            exact_sample_count=1200,
            fuzzy_sample_count=240,
            oracle_query_count=200,
            failed_gates=(),
            passed=True,
        )
        bundle = SimpleNamespace(
            contract=contract,
            suite_report=SimpleNamespace(
                path_reports=(report, report),
                failed_paths=(),
                passed=True,
            ),
        )
        self.assertEqual(
            validator._benchmark_claim_statuses(cast(Any, bundle))[
                "FAILURE_REPORT"
            ],
            "PASS",
        )

    def test_validator_rejects_alternate_root_and_emit_before_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            alternate = Path(temporary).resolve()
            with patch.object(
                validator,
                "_run_direct_tests",
                side_effect=AssertionError("tests must not execute"),
            ):
                with self.assertRaisesRegex(ValueError, "repository root"):
                    validator.main(["--repository-root", str(alternate)])
                with self.assertRaisesRegex(ValueError, "canonical output"):
                    validator.main(["--emit", "AGENTS.md"])

    def test_require_go_succeeds_after_truthful_go_adjudication(self) -> None:
        with (
            patch.object(validator, "_run_direct_tests", return_value=True),
            patch.object(validator, "_atomic_write"),
        ):
            self.assertEqual(validator.main(["--require-go"]), 0)

    def test_source_walk_rejects_symlink_and_dotdot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real"
            real.mkdir()
            source = real / "source.json"
            source.write_text("{}\n", encoding="utf-8")
            final_alias = root / "alias.json"
            final_alias.symlink_to(source)
            parent_alias = root / "alias-parent"
            parent_alias.symlink_to(real, target_is_directory=True)

            observed, digest = validator._read_strict_regular(
                root,
                "real/source.json",
            )
            self.assertEqual(observed, b"{}\n")
            self.assertEqual(digest, hashlib.sha256(observed).hexdigest())
            self.assertTrue(stat.S_ISREG(os.lstat(source).st_mode))
            with self.assertRaisesRegex(ValueError, "source"):
                validator._read_strict_regular(root, "alias.json")
            with self.assertRaisesRegex(ValueError, "source"):
                validator._read_strict_regular(
                    root,
                    "alias-parent/source.json",
                )
            with self.assertRaisesRegex(ValueError, "canonical"):
                validator._read_strict_regular(
                    root,
                    "real/../real/source.json",
                )

    def test_validator_rejects_nonregular_evidence_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ordinary = root / "ordinary.json"
            ordinary.write_text("{}\n", encoding="utf-8")
            alias = root / "evidence.json"
            alias.symlink_to(ordinary)
            with self.assertRaisesRegex(ValueError, "regular"):
                validator._validate_evidence_target(alias)
            with self.assertRaisesRegex(ValueError, "regular"):
                validator._validate_evidence_target(root)
            hardlink = root / "hardlink.json"
            os.link(ordinary, hardlink)
            with self.assertRaisesRegex(ValueError, "regular"):
                validator._validate_evidence_target(ordinary)

    def test_atomic_write_validates_before_and_after_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "evidence.json"
            calls = 0

            def validate() -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError("input drift after replace")

            with self.assertRaisesRegex(ValueError, "after replace"):
                validator._atomic_write(target, b"{}\n", validate)
            self.assertEqual(calls, 2)
            self.assertEqual(target.read_bytes(), b"{}\n")


if __name__ == "__main__":
    unittest.main()
