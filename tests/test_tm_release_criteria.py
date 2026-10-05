"""Integrity tests for current release validation and historical reports."""

from __future__ import annotations

from collections import Counter
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
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


def _arguments(*extra: str) -> list[str]:
    (_ROOT / "artifacts" / "windows").mkdir(parents=True, exist_ok=True)
    return [
        "--emit", "artifacts/windows/release-test.json",
        "--acceptance-evidence", "acceptance_matrix_evidence.json",
        "--fault-evidence", "fault_matrix_evidence.json",
        "--benchmark-evidence", "benchmark_tm_evidence.json", *extra,
    ]


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
        self.assertIn("tools/tm_release_evidence_io.py", paths)
        self.assertIn("tools/validate_tm_release_criteria.py", paths)
        self.assertIn("tests/release_criteria_registry.py", paths)
        self.assertIn("tests/test_editor_controller_writes.py", paths)
        source_files = tuple(
            (path, hashlib.sha256((_ROOT / path).read_bytes()).hexdigest())
            for path in paths
        )
        self.assertEqual(
            validator._release_source_file_digests(_ROOT),
            source_files,
        )
        fingerprint = release_criteria_source_fingerprint(
            release_criteria_registry_digest(),
            source_files,
        )
        self.assertIsNotNone(_SHA256.fullmatch(fingerprint))

    def test_requirements_parser_and_registry_cover_current_criteria(self) -> None:
        criteria = parse_requirement_criteria(
            _REQUIREMENTS.read_text(encoding="utf-8")
        )
        criterion_ids = tuple(item.criterion_id for item in criteria)
        binding_ids = tuple(
            item.criterion_id for item in RELEASE_CRITERIA_BINDINGS
        )
        self.assertTrue(criteria)
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
                    "8": 10,
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
        self.assertEqual(len(direct_tests), 19)

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
        self.assertEqual(len(direct_ids), 19)
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
    def test_historical_report_preserves_its_own_inputs_and_decision(self) -> None:
        evidence = _load_evidence()
        self.assertEqual(set(evidence), {
            "benchmark_blockers", "blocked_criteria", "generated_at_utc",
            "input_evidence", "registry_digest", "release_decision", "rows",
            "schema_version", "source_files", "source_fingerprint", "summary",
        })
        self.assertEqual(evidence["schema_version"], RELEASE_CRITERIA_SCHEMA_VERSION)
        self.assertRegex(evidence["generated_at_utc"], _UTC)
        inputs = evidence["input_evidence"]
        self.assertEqual(set(inputs), {
            "acceptance_evidence_sha256", "acceptance_source_fingerprint",
            "benchmark_bundle_digest", "benchmark_evidence_sha256",
            "fault_evidence_sha256", "fault_source_fingerprint",
            "release_owner_source_fingerprint", "requirements_sha256",
        })
        for digest in (*inputs.values(), evidence["registry_digest"]):
            self.assertRegex(digest, _SHA256)
        payload = dict(inputs, registry_digest=evidence["registry_digest"])
        self.assertEqual(evidence["source_fingerprint"], hashlib.sha256(
            validator._canonical_json(payload).encode("utf-8")
        ).hexdigest())
        sources = evidence["source_files"]
        self.assertEqual(len(sources), len({item["path"] for item in sources}))
        for item in sources:
            self.assertEqual(set(item), {"path", "sha256"})
            self.assertFalse(Path(item["path"]).is_absolute())
            self.assertRegex(item["sha256"], _SHA256)
        owner_payload = {
            "registry_digest": evidence["registry_digest"],
            "source_files": [[item["path"], item["sha256"]] for item in sources],
            "version": "tm-release-criteria-source-v1",
        }
        self.assertEqual(inputs["release_owner_source_fingerprint"], hashlib.sha256(
            validator._canonical_json(owner_payload).encode("utf-8")
        ).hexdigest())
        rows = evidence["rows"]
        self.assertEqual(len(rows), len({row["criterion_id"] for row in rows}))
        for row in rows:
            self.assertEqual(set(row), {"criterion_id", "criterion_text_digest", "evidence_refs", "status"})
            self.assertRegex(row["criterion_text_digest"], _SHA256)
            self.assertTrue(row["evidence_refs"])
            self.assertIn(row["status"], ("PASS", "BLOCKED"))
        blocked = [row["criterion_id"] for row in rows if row["status"] == "BLOCKED"]
        self.assertEqual(evidence["blocked_criteria"], blocked)
        self.assertEqual(evidence["release_decision"],
                         "NO_GO" if blocked or evidence["benchmark_blockers"] else "GO")
        summary = evidence["summary"]
        self.assertEqual(summary["total_criteria"], len(rows))
        self.assertEqual(summary["mapped_criteria"], len(rows))
        self.assertEqual(summary["passed_criteria"], len(rows) - len(blocked))
        self.assertEqual(summary["blocked_criteria"], len(blocked))

    def test_evidence_parser_rejects_duplicates_and_nonfinite(self) -> None:
        with self.assertRaises(ValueError):
            json.loads(
                '{"schema":"a","schema":"b"}',
                object_pairs_hook=_reject_duplicate_keys,
            )
        with self.assertRaises(ValueError):
            validator._parse_strict_json('{"value":NaN}')


class ReleaseCriteriaValidatorTests(unittest.TestCase):
    def test_selected_matrix_input_rejects_stale_or_incomplete_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "source.py").write_bytes(b"source\n")
            row = SimpleNamespace(row_id="9.3.TEST.01", test_ids=("one.test",), task="9.3")
            sources = (("source.py", hashlib.sha256(b"source\n").hexdigest()),)
            from tests.acceptance_matrix_registry import acceptance_matrix_source_fingerprint
            evidence = {
                "generated_at_utc": "2026-10-05T00:00:00Z",
                "schema_version": "matrix-test", "registry_digest": "a" * 64,
                "source_files": [{"path": path, "sha256": digest} for path, digest in sources],
                "source_fingerprint": acceptance_matrix_source_fingerprint("a" * 64, sources),
                "rows": [{"row_id": row.row_id, "status": "PASS", "test_ids": list(row.test_ids)}],
                "tasks": ["9.3"],
                "summary": {"passed_rows": 1, "referenced_tests": 1, "total_rows": 1},
            }
            path = root / "selected.json"
            def validate(payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                return validator._validate_matrix_evidence(
                    root=root, relative=path.name, schema_version="matrix-test",
                    rows=(row,), registry_digest="a" * 64, source_paths=("source.py",),
                    source_fingerprint=acceptance_matrix_source_fingerprint,
                )
            self.assertEqual(validate(evidence)[0], {row.row_id: "PASS"})
            for field, value in (
                ("registry_digest", "b" * 64), ("source_fingerprint", "b" * 64),
                ("source_files", []), ("rows", []),
                ("rows", [{"row_id": row.row_id, "status": "FAIL", "test_ids": list(row.test_ids)}]),
            ):
                with self.subTest(field=field):
                    altered = copy.deepcopy(evidence)
                    altered[field] = value
                    with self.assertRaises(ValueError):
                        validate(altered)
            (root / "source.py").write_bytes(b"changed\n")
            with self.assertRaisesRegex(ValueError, "source digest is stale"):
                validate(evidence)

    def test_selected_inputs_keep_time_failure_no_go_and_recheck_before_publish(self) -> None:
        from tm_benchmark_gate import benchmark_evidence_bundle_to_json
        from tests.test_tm_benchmark_gate import _time_failure_bundle
        bundle = _time_failure_bundle()
        (_ROOT / "artifacts" / "windows").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=_ROOT / "artifacts" / "windows") as temporary:
            run = Path(temporary)
            benchmark = run / "benchmark.json"
            benchmark.write_text(benchmark_evidence_bundle_to_json(bundle), encoding="utf-8")
            selected = {"acceptance": run / "acceptance.json", "fault": run / "fault.json"}
            rows = {"acceptance": ACCEPTANCE_MATRIX_ROWS, "fault": FAULT_MATRIX_ROWS}
            calls = []
            def matrix(**kwargs):
                calls.append(kwargs["relative"])
                owner = "acceptance" if kwargs["relative"] == selected["acceptance"].relative_to(_ROOT).as_posix() else "fault"
                self.assertEqual(kwargs["relative"], selected[owner].relative_to(_ROOT).as_posix())
                return {row.row_id: "PASS" for row in rows[owner]}, "a" * 64, "b" * 64
            argv = _arguments(
                "--emit", str(run / "release.json"),
                "--acceptance-evidence", str(selected["acceptance"]),
                "--fault-evidence", str(selected["fault"]),
                "--benchmark-evidence", str(benchmark), "--require-go",
            )
            emitted = []
            def capture(_path, payload, validate):
                validate()
                emitted.append(json.loads(payload))
            with (
                patch.object(validator, "_validate_matrix_evidence", side_effect=matrix),
                patch.object(validator, "benchmark_implementation_fingerprint", return_value=bundle.implementation_fingerprint),
                patch.object(validator, "_run_direct_tests", return_value=True) as execute,
                patch.object(validator, "_atomic_write", side_effect=capture),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(validator.main(argv), 1)
                self.assertEqual(emitted[0]["release_decision"], "NO_GO")
                self.assertTrue(emitted[0]["benchmark_blockers"])
                self.assertEqual(len(emitted[0]["rows"]), len(RELEASE_CRITERIA_BINDINGS))
                execute.assert_called_once_with(validator._release_execution_test_ids()[2])
                self.assertGreaterEqual(len(calls), 6)
            self.assertFalse((run / "release.json").exists())  # Unit fixture, never release evidence.
            with (
                patch.object(validator, "_validate_matrix_evidence", side_effect=matrix),
                patch.object(validator, "benchmark_implementation_fingerprint", return_value="0" * 64),
                patch.object(validator, "_run_direct_tests") as execute,
            ):
                with self.assertRaisesRegex(ValueError, "fingerprint is stale"):
                    validator.main(argv)
                execute.assert_not_called()
            def drift(_tests):
                benchmark.write_text("{}", encoding="utf-8")
                return True
            with (
                patch.object(validator, "_validate_matrix_evidence", side_effect=matrix),
                patch.object(validator, "benchmark_implementation_fingerprint", return_value=bundle.implementation_fingerprint),
                patch.object(validator, "_run_direct_tests", side_effect=drift),
                patch.object(validator, "_atomic_write") as publish,
            ):
                with self.assertRaisesRegex(ValueError, "benchmark evidence changed"):
                    validator.main(argv)
                publish.assert_not_called()

    def test_explicit_artifact_paths_preserve_checkout_sources(self) -> None:
        from tools.tm_release_evidence_io import artifact_output_path, evidence_input_path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run = root / "artifacts" / "run"
            run.mkdir(parents=True)
            source = root / "release_criteria_evidence.json"
            source.write_bytes(b"historical\n")
            observed = artifact_output_path(root, run / "release.json")
            self.assertEqual(observed, run / "release.json")
            self.assertEqual(
                evidence_input_path(root, run / "acceptance.json"),
                "artifacts/run/acceptance.json",
            )
            for target in (source, root / "AGENTS.md", root / "tests" / "x.json"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    artifact_output_path(root, target)
            with self.assertRaises(ValueError):
                artifact_output_path(root, run / "release.json", inputs=("artifacts/run/release.json",))
            with self.assertRaises(ValueError):
                evidence_input_path(root, "artifacts/run/../input.json")
            self.assertEqual(source.read_bytes(), b"historical\n")

    def test_current_registry_includes_approved_time_admission_criteria(self) -> None:
        criteria = parse_requirement_criteria(_REQUIREMENTS.read_text(encoding="utf-8"))
        self.assertEqual(
            tuple(binding.criterion_id for binding in RELEASE_CRITERIA_BINDINGS),
            tuple(criterion.criterion_id for criterion in criteria),
        )

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
                    validator.main(_arguments("--repository-root", str(alternate)))
                with self.assertRaisesRegex(ValueError, "artifact run directory"):
                    validator.main(_arguments("--emit", "AGENTS.md"))

    def test_source_walk_rejects_symlink_and_dotdot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real"
            real.mkdir()
            source = real / "source.json"
            source.write_bytes(b"{}\n")
            observed, digest = validator._read_strict_regular(
                root,
                "real/source.json",
            )
            self.assertEqual(observed, b"{}\n")
            self.assertEqual(digest, hashlib.sha256(observed).hexdigest())
            self.assertTrue(stat.S_ISREG(os.lstat(source).st_mode))
            final_alias = root / "alias.json"
            parent_alias = root / "alias-parent"
            if sys.platform == "win32":
                os.link(source, final_alias)
                completed = subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(parent_alias),
                        str(real),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            else:
                final_alias.symlink_to(source)
                parent_alias.symlink_to(real, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, "source"):
                    validator._read_strict_regular(root, "alias.json")
                with self.assertRaisesRegex(ValueError, "source"):
                    validator._read_strict_regular(
                        root,
                        "alias-parent/source.json",
                    )
            finally:
                if sys.platform == "win32":
                    parent_alias.rmdir()
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
            if sys.platform == "win32":
                os.link(ordinary, alias)
            else:
                alias.symlink_to(ordinary)
            with self.assertRaisesRegex(ValueError, "regular"):
                validator._validate_evidence_target(alias)
            with self.assertRaisesRegex(ValueError, "regular"):
                validator._validate_evidence_target(root)
            if sys.platform != "win32":
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
