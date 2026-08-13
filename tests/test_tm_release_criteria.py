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
from typing import cast
import unittest
from unittest.mock import patch

import tools.validate_tm_release_criteria as validator

from tests.acceptance_matrix_registry import ACCEPTANCE_MATRIX_ROWS
from tests.fault_matrix_registry import FAULT_MATRIX_ROWS
from tests.release_criteria_registry import (
    RELEASE_CRITERIA_BINDINGS,
    RELEASE_CRITERIA_SCHEMA_VERSION,
    parse_requirement_criteria,
    release_criteria_registry_digest,
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
                    self.assertIn(value, validator._benchmark_claim_statuses(
                        benchmark_evidence_bundle_from_json(
                            _BENCHMARK.read_text(encoding="utf-8")
                        )
                    ))
                else:
                    self.fail(f"unknown evidence kind: {kind}")
        self.assertEqual(len(direct_tests), 12)

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
    def test_evidence_is_fresh_complete_and_truthfully_no_go(self) -> None:
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
            Counter({"PASS": 84, "BLOCKED": 2}),
        )
        self.assertEqual(statuses["8.2"], "BLOCKED")
        self.assertEqual(statuses["8.3"], "BLOCKED")
        self.assertEqual(evidence["blocked_criteria"], ["8.2", "8.3"])
        self.assertEqual(evidence["release_decision"], "NO_GO")
        self.assertEqual(
            evidence["summary"],
            {
                "blocked_criteria": 2,
                "direct_tests": 12,
                "mapped_criteria": 86,
                "passed_criteria": 84,
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
                "CANDIDATE_RECALL": "BLOCKED",
                "ENVIRONMENT": "PASS",
                "EXACT_P95": "PASS",
                "FAILURE_REPORT": "PASS",
                "FUZZY_P95": "BLOCKED",
                "METRICS": "PASS",
                "MIGRATION": "BLOCKED",
                "PEAK_RSS": "PASS",
            },
        )
        self.assertEqual(
            evidence["benchmark_blockers"],
            list(validator._benchmark_blockers(bundle)),
        )
        self.assertEqual(len(cast(list[object], evidence["benchmark_blockers"])), 6)

    def test_evidence_parser_rejects_duplicates_and_nonfinite(self) -> None:
        with self.assertRaises(ValueError):
            json.loads(
                '{"schema":"a","schema":"b"}',
                object_pairs_hook=_reject_duplicate_keys,
            )
        with self.assertRaises(ValueError):
            validator._parse_strict_json('{"value":NaN}')


class ReleaseCriteriaValidatorTests(unittest.TestCase):
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

    def test_require_go_fails_after_truthful_no_go_adjudication(self) -> None:
        with (
            patch.object(validator, "_run_direct_tests", return_value=True),
            patch.object(validator, "_atomic_write"),
        ):
            self.assertEqual(validator.main(["--require-go"]), 1)

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


if __name__ == "__main__":
    unittest.main()
