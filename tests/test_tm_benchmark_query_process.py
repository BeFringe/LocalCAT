"""Focused tests for the Task 8.5A query-process execution bridge."""

from __future__ import annotations

from typing import Any, cast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_benchmark_query_process
from tm_benchmark import load_benchmark_contract
from tm_benchmark_latency import (
    LATENCY_EVIDENCE_SCHEMA_VERSION,
    LatencyEvidence,
    LatencySample,
    latency_evidence_to_json,
    latency_evidence_to_payload,
    recompute_cohort_statistics,
)
from tm_benchmark_process import (
    TMBenchmarkProcessEvidence,
    artifact_snapshot_digest,
    process_canonical_artifact_paths,
    process_evidence_to_json,
    process_evidence_to_payload,
    rss_peak_bytes_facts,
    run_process_migration_evidence,
)
from tm_benchmark_query_process import (
    QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
    QUERY_PROBE_SCHEMA_VERSION,
    QUERY_WORKER_PROTOCOL_VERSION,
    ArtifactFileIdentity,
    ArtifactSnapshot,
    QueryProcessError,
    QueryProcessEvidence,
    QueryProbeReport,
    artifact_snapshot_from_payload,
    artifact_snapshot_to_payload,
    query_process_evidence_from_json,
    query_process_evidence_from_payload,
    query_process_evidence_to_json,
    query_process_evidence_to_payload,
    query_probe_from_json,
    query_probe_from_payload,
    query_probe_to_json,
    query_probe_to_payload,
    run_query_process_evidence,
    run_query_process_probe,
    verify_canonical_artifact,
)
from tm_contracts import (
    BENCHMARK_PERCENTILE_METHOD,
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
)

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")

_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK
_SCOPE = _CONTRACT.rss_scope

_IMPORT_RE = re.compile(r"^(?:import|from)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)


def _digest(prefix: str) -> str:
    return prefix * 64


def _latency_environment(fts5_enabled: str) -> tuple[tuple[str, str], ...]:
    facts = {
        "cpu": "test-cpu",
        "fts5_enabled": fts5_enabled,
        "os": "test-os",
        "python_version": "test-python",
        "ram_mib": "1024",
        "sqlite_version": "test-sqlite",
        "unicode_version": "test-unicode",
        "timing_clock": "test-clock-v1",
        "percentile_method": BENCHMARK_PERCENTILE_METHOD,
        "warmup_queries_per_cohort": "100",
        "measured_repeats": "1",
    }
    return tuple(sorted(facts.items()))


def _query_environment(
    fts5_enabled: str,
    rss_scope: str = _SCOPE,
) -> tuple[tuple[str, str], ...]:
    facts = dict(_latency_environment(fts5_enabled))
    facts["rss_platform"] = "test-platform"
    facts["rss_raw_unit"] = "bytes"
    facts["rss_scope"] = rss_scope
    return tuple(sorted(facts.items()))


def _latency_evidence(
    *,
    path: BenchmarkExecutionPath = _FTS5,
    fts5_enabled: str = "true",
) -> LatencyEvidence:
    exact_samples = tuple(
        LatencySample(
            query_id=index,
            elapsed_ns=index * 37 + 3,
            cohort="exact",
            actual_path=path,
            succeeded=True,
            result_count=1,
        )
        for index in range(1, _CONTRACT.exact_cohort_count + 1)
    )
    fuzzy_samples = tuple(
        LatencySample(
            query_id=index,
            elapsed_ns=index * 41 + 7,
            cohort="fuzzy",
            actual_path=path,
            succeeded=True,
            result_count=1,
            minimum_similarity=_CONTRACT.minimum_similarity,
            top_k=_CONTRACT.top_k,
        )
        for index in range(1, _CONTRACT.fuzzy_cohort_count + 1)
    )
    exact_stats = recompute_cohort_statistics(
        tuple(sample.elapsed_ns for sample in exact_samples)
    )
    fuzzy_stats = recompute_cohort_statistics(
        tuple(sample.elapsed_ns for sample in fuzzy_samples)
    )
    environment = _latency_environment(fts5_enabled)
    return LatencyEvidence(
        schema_version=LATENCY_EVIDENCE_SCHEMA_VERSION,
        contract=_CONTRACT,
        contract_digest=benchmark_contract_digest(_CONTRACT),
        exact_cohort_digest=_CONTRACT.exact_cohort_digest,
        fuzzy_cohort_digest=_CONTRACT.fuzzy_cohort_digest,
        execution_path=path,
        path_config_digest=(
            _CONTRACT.fast_path_config_digest
            if path is _FTS5
            else _CONTRACT.fallback_path_config_digest
        ),
        warmup_queries_per_cohort=100,
        measured_repeats=1,
        percentile_method=BENCHMARK_PERCENTILE_METHOD,
        timing_clock="test-clock-v1",
        minimum_similarity=_CONTRACT.minimum_similarity,
        top_k=_CONTRACT.top_k,
        exact_samples=exact_samples,
        fuzzy_samples=fuzzy_samples,
        exact_sample_count=len(exact_samples),
        fuzzy_sample_count=len(fuzzy_samples),
        exact_p50_ns=exact_stats[0],
        exact_p95_ns=exact_stats[1],
        exact_max_ns=exact_stats[2],
        fuzzy_p50_ns=fuzzy_stats[0],
        fuzzy_p95_ns=fuzzy_stats[1],
        fuzzy_max_ns=fuzzy_stats[2],
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )


def _artifact_snapshot(
    *,
    sidecar_digest: str | None = None,
    manifest_digest: str | None = None,
    family_digest: str | None = None,
) -> ArtifactSnapshot:
    identity = ArtifactFileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    return ArtifactSnapshot(
        sidecar_digest=(
            sidecar_digest if sidecar_digest is not None else _digest("a")
        ),
        manifest_digest=(
            manifest_digest if manifest_digest is not None else _digest("b")
        ),
        family_digest=(
            family_digest if family_digest is not None else _digest("7")
        ),
        sidecar_identity=identity,
        manifest_identity=identity,
    )


def _query_evidence(**overrides: Any) -> QueryProcessEvidence:
    latency = _latency_evidence()
    snapshot = _artifact_snapshot()
    environment = _query_environment("true")
    kwargs: dict[str, Any] = dict(
        schema_version=QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
        artifact_key=_digest("c"),
        contract_digest=latency.contract_digest,
        corpus_digest=_CONTRACT.corpus_digest,
        corpus_record_count=100_000,
        fixture_digest=_digest("d"),
        fixture_record_count=100_000,
        resource_id="tm.benchmark",
        canonical_store_id="store.benchmark",
        execution_path=_FTS5,
        path_config_digest=_CONTRACT.fast_path_config_digest,
        actual_index_kind="FTS5_TRIGRAM",
        record_count=100_000,
        generation=0,
        process_evidence_digest=_digest("e"),
        artifact_baseline_digest=_digest("2"),
        process_test_mode=False,
        processes_distinct=True,
        process_pair_digest=_digest("f"),
        query_protocol_digest=_digest("1"),
        artifact_pre=snapshot,
        artifact_post=snapshot,
        latency_evidence=latency,
        latency_evidence_digest=latency.evidence_digest,
        query_peak_rss_bytes=123,
        query_rss_start_bytes=100,
        query_rss_terminal_bytes=123,
        query_rss_unit="bytes",
        query_rss_scope=_SCOPE,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )
    kwargs.update(overrides)
    return QueryProcessEvidence(**kwargs)


def _probe_report(**overrides: Any) -> QueryProbeReport:
    snapshot = _artifact_snapshot()
    environment = _query_environment("true")
    kwargs: dict[str, Any] = dict(
        schema_version=QUERY_PROBE_SCHEMA_VERSION,
        process_evidence_digest=_digest("e"),
        artifact_baseline_digest=_digest("2"),
        processes_distinct=True,
        process_pair_digest=_digest("f"),
        query_protocol_digest=_digest("1"),
        artifact_pre=snapshot,
        artifact_post=snapshot,
        reopen_phase="GENERATION_PUBLISHED",
        reopen_action="COMPLETED",
        reopen_health_healthy=True,
        reopen_health_index_kind="FTS5_TRIGRAM",
        reopen_health_record_count=100_000,
        generation=0,
        actual_index_kind="FTS5_TRIGRAM",
        record_count=100_000,
        exact_calls=1,
        exact_actual_path=_FTS5,
        exact_result_count=1,
        fuzzy_calls=3,
        fuzzy_actual_path=_FTS5,
        fuzzy_result_count=3,
        migration_rerun=False,
        query_peak_rss_bytes=123,
        query_rss_start_bytes=100,
        query_rss_terminal_bytes=123,
        query_rss_unit="bytes",
        query_rss_scope=_SCOPE,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )
    kwargs.update(overrides)
    return QueryProbeReport(**kwargs)


def _migrate(
    root: Path,
    path: BenchmarkExecutionPath,
    *,
    record_count: int = 40,
) -> TMBenchmarkProcessEvidence:
    return run_process_migration_evidence(
        contract_path=_ROOT / "benchmark_tm_contract.json",
        execution_path=path,
        run_root=root,
        test_mode=True,
        test_record_count=record_count,
        timeout_seconds=120.0,
    )


class QueryProcessEvidenceConstructorTests(unittest.TestCase):
    def test_final_evidence_derived_only_from_non_test_facts(self) -> None:
        evidence = _query_evidence()
        self.assertTrue(evidence.final_evidence)
        self.assertEqual(evidence.recompute_evidence_digest(), evidence.evidence_digest)
        test_mode = _query_evidence(
            process_test_mode=True,
            corpus_record_count=40,
            fixture_record_count=40,
            record_count=40,
            corpus_digest=_digest("9"),
        )
        self.assertFalse(test_mode.final_evidence)

    def test_final_evidence_refuses_small_corpus(self) -> None:
        with self.assertRaisesRegex(ValueError, "100000-record corpus"):
            _query_evidence(record_count=40, fixture_record_count=40, corpus_record_count=40)

    def test_rejects_processes_not_distinct(self) -> None:
        with self.assertRaisesRegex(ValueError, "distinct process"):
            _query_evidence(processes_distinct=False)

    def test_rejects_pre_post_artifact_drift(self) -> None:
        mutated = _artifact_snapshot(sidecar_digest=_digest("9"))
        with self.assertRaisesRegex(ValueError, "drifted"):
            _query_evidence(artifact_post=mutated)

    def test_rejects_latency_path_digest_and_contract_drift(self) -> None:
        fallback_latency = _latency_evidence(path=_FALLBACK, fts5_enabled="false")
        with self.assertRaisesRegex(ValueError, "path must match"):
            _query_evidence(latency_evidence=fallback_latency)
        latency = _latency_evidence()
        with self.assertRaisesRegex(ValueError, "digest must bind"):
            _query_evidence(latency_evidence_digest=_digest("0"))
        tampered = _latency_evidence()
        object.__setattr__(tampered, "contract_digest", _digest("0"))
        with self.assertRaisesRegex(ValueError, "contract digest"):
            _query_evidence(latency_evidence=tampered)

    def test_rejects_actual_index_kind_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "actual index kind"):
            _query_evidence(actual_index_kind="GRAM_FALLBACK")

    def test_rejects_rss_invariants(self) -> None:
        with self.assertRaisesRegex(ValueError, "terminal high-water"):
            _query_evidence(
                query_peak_rss_bytes=200,
                query_rss_terminal_bytes=123,
            )
        with self.assertRaisesRegex(ValueError, "below start"):
            _query_evidence(
                query_rss_start_bytes=200,
                query_rss_terminal_bytes=123,
                query_peak_rss_bytes=123,
            )
        with self.assertRaisesRegex(ValueError, "RSS unit"):
            _query_evidence(query_rss_unit="kib")

    def test_rejects_environment_path_and_digest_drift(self) -> None:
        environment = _query_environment("false")
        with self.assertRaisesRegex(ValueError, "fts5_enabled"):
            _query_evidence(environment=environment)
        environment = _query_environment("true")
        with self.assertRaisesRegex(ValueError, "environment digest"):
            _query_evidence(environment_digest=_digest("0"))

    def test_rejects_non_final_evidence_flag_input(self) -> None:
        # final_evidence is init=False and can never be supplied by callers.
        with self.assertRaises(TypeError):
            _query_evidence(final_evidence=True)  # type: ignore[call-arg]

    def test_rejects_invalid_artifact_baseline_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact baseline digest"):
            _query_evidence(artifact_baseline_digest="not-a-digest")
        with self.assertRaisesRegex(ValueError, "artifact baseline digest"):
            _probe_report(artifact_baseline_digest="not-a-digest")
        with self.assertRaisesRegex(ValueError, "artifact baseline digest"):
            _query_evidence(artifact_baseline_digest=_digest("a").upper())


class QueryProcessEvidenceCodecTests(unittest.TestCase):
    def test_payload_and_json_round_trip(self) -> None:
        evidence = _query_evidence()
        self.assertEqual(
            query_process_evidence_from_payload(
                query_process_evidence_to_payload(evidence)
            ),
            evidence,
        )
        self.assertEqual(
            query_process_evidence_from_json(query_process_evidence_to_json(evidence)),
            evidence,
        )

    def test_rejects_duplicate_json_keys(self) -> None:
        serialized = query_process_evidence_to_json(_query_evidence())
        payload = json.loads(serialized)
        duplicated = json.dumps(
            [("schema_version", payload["schema_version"])]
            + list(payload.items())
        )
        duplicated = (
            '{"schema_version":' + json.dumps(payload["schema_version"])
            + "," + serialized[1:]
        )
        with self.assertRaisesRegex(ValueError, "not strict"):
            query_process_evidence_from_json(duplicated)

    def test_rejects_non_finite_json_number(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["generation"] = float("inf")
        with self.assertRaises((TypeError, ValueError)):
            query_process_evidence_from_payload(payload)
        serialized = query_process_evidence_to_json(_query_evidence())
        with self.assertRaisesRegex(ValueError, "not strict"):
            query_process_evidence_from_json(
                serialized.replace('"generation":0', '"generation":NaN')
            )

    def test_rejects_bool_as_int(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["record_count"] = True
        with self.assertRaises((TypeError, ValueError)):
            query_process_evidence_from_payload(payload)

    def test_rejects_unknown_and_missing_fields(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["extra"] = 1
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            query_process_evidence_from_payload(payload)
        del payload["extra"]
        del payload["record_count"]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            query_process_evidence_from_payload(payload)

    def test_caller_evidence_digest_is_not_trusted(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["evidence_digest"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "does not match"):
            query_process_evidence_from_payload(payload)

    def test_caller_final_evidence_and_artifact_unchanged_cannot_authorize(
        self,
    ) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["final_evidence"] = False
        with self.assertRaisesRegex(ValueError, "final evidence fact"):
            query_process_evidence_from_payload(payload)
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["artifact_unchanged"] = False
        with self.assertRaisesRegex(ValueError, "artifact unchanged fact"):
            query_process_evidence_from_payload(payload)

    def test_nested_latency_digest_drift_rejected(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["latency_evidence_digest"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "latency evidence digest"):
            query_process_evidence_from_payload(payload)

    def test_generation_and_path_drift_rejected(self) -> None:
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["generation"] = 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            query_process_evidence_from_payload(payload)
        payload = query_process_evidence_to_payload(_query_evidence())
        payload["execution_path"] = "GRAM_FALLBACK"
        with self.assertRaises((TypeError, ValueError)):
            query_process_evidence_from_payload(payload)

    def test_artifact_snapshot_codec_round_trip(self) -> None:
        snapshot = _artifact_snapshot()
        self.assertEqual(
            artifact_snapshot_from_payload(artifact_snapshot_to_payload(snapshot)),
            snapshot,
        )

    def test_rejects_missing_artifact_baseline_digest(self) -> None:
        payload = query_probe_to_payload(_probe_report())
        del payload["artifact_baseline_digest"]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            query_probe_from_payload(payload)
        payload = query_process_evidence_to_payload(_query_evidence())
        del payload["artifact_baseline_digest"]
        with self.assertRaisesRegex(ValueError, "missing fields"):
            query_process_evidence_from_payload(payload)


class QueryProbeCodecTests(unittest.TestCase):
    def test_payload_and_json_round_trip(self) -> None:
        report = _probe_report()
        self.assertEqual(
            query_probe_from_payload(query_probe_to_payload(report)),
            report,
        )
        self.assertEqual(
            query_probe_from_json(query_probe_to_json(report)),
            report,
        )
        self.assertEqual(report.recompute_probe_digest(), report.probe_digest)

    def test_rejects_actual_path_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "index kind must equal"):
            _probe_report(actual_index_kind="GRAM_FALLBACK")
        with self.assertRaisesRegex(ValueError, "must agree"):
            _probe_report(
                fuzzy_actual_path=_FALLBACK,
                fuzzy_result_count=3,
            )

    def test_rejects_migration_rerun_and_process_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "never re-run migration"):
            _probe_report(migration_rerun=True)
        with self.assertRaisesRegex(ValueError, "distinct process"):
            _probe_report(processes_distinct=False)

    def test_rejects_rss_and_environment_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "terminal high-water"):
            _probe_report(query_peak_rss_bytes=200, query_rss_terminal_bytes=123)
        environment = _query_environment("false")
        with self.assertRaisesRegex(ValueError, "fts5_enabled"):
            _probe_report(environment=environment)

    def test_caller_probe_digest_is_not_trusted(self) -> None:
        payload = query_probe_to_payload(_probe_report())
        payload["probe_digest"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "does not match"):
            query_probe_from_payload(payload)


class ArtifactVerificationTests(unittest.TestCase):
    def _migrated_root(self, path: BenchmarkExecutionPath) -> tuple[Path, TMBenchmarkProcessEvidence]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, path)
            return root, evidence

    def test_accepts_real_migrated_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            snapshot = verify_canonical_artifact(
                run_root=root,
                fixture_path=Path(evidence.fixture_path),
                resource_id=evidence.resource_id,
                expected_fixture_digest=evidence.fixture_digest,
            )
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            manifest = Path(evidence.fixture_path + ".localcat-snapshot.json")
            self.assertEqual(
                snapshot.sidecar_digest,
                hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                snapshot.manifest_digest,
                hashlib.sha256(manifest.read_bytes()).hexdigest(),
            )
            self.assertGreater(snapshot.sidecar_identity.inode, 0)

    def test_rejects_missing_sidecar_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            Path(evidence.fixture_path + ".sqlite3").unlink()
            with self.assertRaisesRegex(ValueError, "missing required artifact"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            Path(
                evidence.fixture_path + ".localcat-snapshot.json"
            ).unlink()
            with self.assertRaisesRegex(ValueError, "missing required artifact"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )

    def test_rejects_symlink_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            target = root / "planted.db"
            target.write_bytes(b"foreign")
            sidecar.unlink()
            sidecar.symlink_to(target)
            with self.assertRaisesRegex(
                ValueError,
                "escape the run root|single-link|foreign",
            ):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )

    def test_rejects_multilink_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            os.link(sidecar, root / "fixture.jsonl.sqlite3.hardlink")
            with self.assertRaisesRegex(ValueError, "single-link|foreign"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )

    def test_rejects_foreign_entry_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            (root / "planted.txt").write_text("foreign", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "foreign entries"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            (root / "planted-dir").mkdir()
            with self.assertRaisesRegex(ValueError, "foreign entries"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )

    def test_rejects_fixture_digest_drift_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            with self.assertRaisesRegex(ValueError, "fixture digest"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=Path(evidence.fixture_path),
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=_digest("0"),
                )
            outside = root / "outside-dir" / "outside.jsonl"
            outside.parent.mkdir()
            outside.write_text("x\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "directly in the run root"):
                verify_canonical_artifact(
                    run_root=root,
                    fixture_path=outside,
                    resource_id=evidence.resource_id,
                    expected_fixture_digest=evidence.fixture_digest,
                )

    def test_pre_post_mutation_detected_as_snapshot_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            kwargs: dict[str, Any] = dict(
                run_root=root,
                fixture_path=Path(evidence.fixture_path),
                resource_id=evidence.resource_id,
                expected_fixture_digest=evidence.fixture_digest,
            )
            pre = verify_canonical_artifact(**kwargs)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            with sidecar.open("ab") as stream:
                stream.write(b"tampered")
            post = verify_canonical_artifact(**kwargs)
            # A pre/post mismatch is the same drift rejected by the bridge:
            # the bridge compares its own pre snapshot against the child's
            # reported pre/post and fails closed on any inequality.
            self.assertNotEqual(pre.sidecar_digest, post.sidecar_digest)
            self.assertNotEqual(pre.sidecar_identity, post.sidecar_identity)


class WorkerProtocolTests(unittest.TestCase):
    def _spawn(self, stdin_text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tm_benchmark_query_process", "--worker"],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=_ROOT,
            timeout=120.0,
            check=False,
        )

    def test_rejects_malformed_stdin(self) -> None:
        cases = (
            "",
            "not json",
            "[1, 2]",
            '{"a": 1}',
            '{"protocol": "x", "protocol": "y"}',
            '{"protocol": "tm-benchmark-query-worker-v1", "n": NaN}',
        )
        for case in cases:
            completed = self._spawn(case)
            self.assertEqual(completed.returncode, 1, case)
            self.assertIn("QUERY.REQUEST_INVALID", completed.stderr)

    def test_rejects_non_worker_argv(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "tm_benchmark_query_process"],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=_ROOT,
            timeout=60.0,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)

    def test_latency_boundary_preserves_stable_worker_code(self) -> None:
        specific = tm_benchmark_query_process._WorkerError(
            "QUERY.FUZZY_UNAVAILABLE"
        )
        with patch(
            "tm_benchmark_query_process.measure_path_latency",
            side_effect=specific,
        ):
            with self.assertRaises(
                tm_benchmark_query_process._WorkerError
            ) as ctx:
                tm_benchmark_query_process._measure_latency_evidence(
                    contract=_CONTRACT,
                    requested_path=_FTS5,
                    executor=cast(Any, None),
                    environment=_latency_environment("true"),
                )
        self.assertIs(ctx.exception, specific)

    def test_latency_boundary_maps_unclassified_failure(self) -> None:
        with patch(
            "tm_benchmark_query_process.measure_path_latency",
            side_effect=ValueError("query body must not escape"),
        ):
            with self.assertRaises(
                tm_benchmark_query_process._WorkerError
            ) as ctx:
                tm_benchmark_query_process._measure_latency_evidence(
                    contract=_CONTRACT,
                    requested_path=_FTS5,
                    executor=cast(Any, None),
                    environment=_latency_environment("true"),
                )
        self.assertEqual(ctx.exception.error_code, "QUERY.LATENCY_FAILED")

    def test_worker_rejects_request_fact_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            from tm_benchmark_query_process import _request_payload

            request = _request_payload(
                mode="probe",
                process_evidence=evidence,
                run_root=str(root),
                fixture_path=evidence.fixture_path,
            )
            generation_value = request["generation"]
            if type(generation_value) is not int:
                raise AssertionError("request generation must be an int")
            request["generation"] = generation_value + 1
            completed = self._spawn(
                json.dumps(request, sort_keys=True, separators=(",", ":"))
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("QUERY.FACT_DRIFT", completed.stderr)

    def test_worker_rejects_path_mismatch_request_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FALLBACK)
            from tm_benchmark_query_process import (
                _request_payload,
                query_worker_protocol_digest,
            )

            request = _request_payload(
                mode="probe",
                process_evidence=evidence,
                run_root=str(root),
                fixture_path=evidence.fixture_path,
            )
            request["execution_path"] = "FTS5_TRIGRAM"
            request["actual_index_kind"] = "FTS5_TRIGRAM"
            request["protocol_digest"] = query_worker_protocol_digest(
                mode="probe",
                process_evidence_digest_value=evidence.evidence_digest,
                artifact_baseline_digest=artifact_snapshot_digest(
                    evidence.artifact_snapshot
                ),
                run_root=str(root),
                fixture_path=evidence.fixture_path,
                resource_id=evidence.resource_id,
                canonical_store_id=evidence.canonical_store_id,
                execution_path=_FTS5,
                contract_digest=evidence.contract_digest,
                corpus_digest=evidence.corpus_digest,
                corpus_record_count=evidence.corpus_record_count,
                fixture_digest=evidence.fixture_digest,
                fixture_record_count=evidence.fixture_record_count,
                generation=evidence.generation,
                record_count=evidence.record_count,
                actual_index_kind="FTS5_TRIGRAM",
                path_config_digest=evidence.path_config_digest,
            )
            completed = self._spawn(
                json.dumps(request, sort_keys=True, separators=(",", ":"))
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("QUERY.FACT_DRIFT", completed.stderr)


class ProbeIntegrationTests(unittest.TestCase):
    def test_fast_path_real_subprocess_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            result = run_query_process_probe(evidence, timeout_seconds=120.0)
            probe = result.probe
            self.assertNotEqual(result.query_child_pid, os.getpid())
            self.assertNotEqual(result.query_child_pid, evidence.child_pid)
            self.assertTrue(probe.processes_distinct)
            self.assertFalse(probe.migration_rerun)
            self.assertEqual(probe.exact_actual_path, _FTS5)
            self.assertEqual(probe.fuzzy_actual_path, _FTS5)
            self.assertGreaterEqual(probe.exact_result_count, 1)
            self.assertGreaterEqual(probe.fuzzy_result_count, 0)
            self.assertEqual(probe.fuzzy_calls, 3)
            self.assertEqual(probe.actual_index_kind, "FTS5_TRIGRAM")
            self.assertEqual(probe.reopen_health_index_kind, "FTS5_TRIGRAM")
            self.assertEqual(probe.reopen_health_record_count, 40)
            self.assertEqual(probe.record_count, 40)
            self.assertEqual(
                dict(probe.environment)["fts5_enabled"],
                "true",
            )
            self.assertEqual(probe.query_rss_unit, "bytes")
            self.assertGreater(probe.query_peak_rss_bytes, 0)
            self.assertEqual(
                probe.query_peak_rss_bytes,
                probe.query_rss_terminal_bytes,
            )
            self.assertTrue(probe.artifact_unchanged)
            self.assertEqual(probe.recompute_probe_digest(), probe.probe_digest)
            self.assertEqual(
                probe.process_evidence_digest,
                evidence.evidence_digest,
            )
            self.assertEqual(
                probe.artifact_baseline_digest,
                artifact_snapshot_digest(evidence.artifact_snapshot),
            )
            self.assertEqual(
                probe.artifact_pre.sidecar_digest,
                evidence.artifact_snapshot.sidecar_digest,
            )
            self.assertEqual(
                probe.artifact_pre.manifest_digest,
                evidence.artifact_snapshot.manifest_digest,
            )
            self.assertEqual(
                probe.artifact_pre.family_digest,
                evidence.artifact_snapshot.family_digest,
            )
            self.assertEqual(
                probe.artifact_pre.sidecar_identity.inode,
                evidence.artifact_snapshot.sidecar_identity.inode,
            )
            self.assertEqual(
                probe.artifact_post.manifest_identity.mtime_ns,
                evidence.artifact_snapshot.manifest_identity.mtime_ns,
            )

    def test_fallback_path_real_subprocess_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FALLBACK)
            result = run_query_process_probe(evidence, timeout_seconds=120.0)
            probe = result.probe
            self.assertEqual(probe.exact_actual_path, _FALLBACK)
            self.assertEqual(probe.fuzzy_actual_path, _FALLBACK)
            self.assertEqual(probe.actual_index_kind, "GRAM_FALLBACK")
            self.assertEqual(
                dict(probe.environment)["fts5_enabled"],
                "false",
            )
            self.assertTrue(probe.processes_distinct)

    def test_each_probe_spawns_a_distinct_query_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            first = run_query_process_probe(evidence, timeout_seconds=120.0)
            second = run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertNotEqual(first.query_child_pid, second.query_child_pid)

    def test_evidence_runner_refuses_test_mode_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_evidence(evidence, timeout_seconds=120.0)
            self.assertEqual(raised.exception.error_code, "QUERY.TEST_MODE_MISMATCH")

    def test_artifact_mutation_before_spawn_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fixture = Path(evidence.fixture_path)
            with fixture.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(raised.exception.error_code, "QUERY.ARTIFACT_INVALID")

    def test_post_migration_append_rejected_against_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            with sidecar.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(
                raised.exception.error_code,
                "QUERY.ARTIFACT_BASELINE_DRIFT",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            manifest = Path(evidence.fixture_path + ".localcat-snapshot.json")
            manifest.write_bytes(manifest.read_bytes() + b"tampered")
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(
                raised.exception.error_code,
                "QUERY.ARTIFACT_BASELINE_DRIFT",
            )

    def test_post_migration_optional_family_mutation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            journal = next(
                path
                for path in root.iterdir()
                if "activation-journal" in path.name
            )
            journal.write_bytes(journal.read_bytes() + b"tampered")
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(
                raised.exception.error_code,
                "QUERY.ARTIFACT_BASELINE_DRIFT",
            )

    def test_post_migration_regular_replace_rejected_against_baseline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            sidecar.write_bytes(sidecar.read_bytes() + b"substituted")
            self.assertEqual(sidecar.stat().st_nlink, 1)
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(
                raised.exception.error_code,
                "QUERY.ARTIFACT_BASELINE_DRIFT",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            sidecar.unlink()
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(raised.exception.error_code, "QUERY.ARTIFACT_INVALID")

    def test_symlink_artifact_fails_closed_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            target = root / "planted.db"
            target.write_bytes(b"foreign")
            sidecar.unlink()
            sidecar.symlink_to(target)
            with self.assertRaises(QueryProcessError) as raised:
                run_query_process_probe(evidence, timeout_seconds=120.0)
            self.assertEqual(raised.exception.error_code, "QUERY.ARTIFACT_INVALID")


class ParentRunnerFailureTests(unittest.TestCase):
    def _run(self, evidence: TMBenchmarkProcessEvidence) -> QueryProbeReport:
        return run_query_process_probe(evidence, timeout_seconds=120.0).probe

    def test_runner_rejects_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            with patch(
                "tm_benchmark_query_process.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
            ):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.CHILD_TIMEOUT")

    def test_runner_rejects_child_failure_and_stderr_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fake = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr='{"error_code": "QUERY.X"}\n',
            )
            with patch("tm_benchmark_query_process.subprocess.run", return_value=fake):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.X")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            noise = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="noise",
            )
            with patch(
                "tm_benchmark_query_process.subprocess.run",
                return_value=noise,
            ):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.CHILD_STDERR_NOISE")

    def _valid_response(self, *, query_pid: int = 424242) -> str:
        report = _probe_report()
        envelope = {
            "kind": "probe",
            "payload": query_probe_to_payload(report),
            "protocol": QUERY_WORKER_PROTOCOL_VERSION,
            "query_pid": query_pid,
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    def _fake_response(self, report: QueryProbeReport) -> str:
        envelope = {
            "kind": "probe",
            "payload": query_probe_to_payload(report),
            "protocol": QUERY_WORKER_PROTOCOL_VERSION,
            "query_pid": 424242,
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":"))

    def _evidence_consistent_probe(
        self,
        evidence: TMBenchmarkProcessEvidence,
        *,
        query_child_pid: int = 424242,
        **overrides: Any,
    ) -> QueryProbeReport:
        """Build an internally valid probe matching the live process evidence.

        Every fact is derived from the supplied process evidence and the
        live artifact snapshot, so a single overridden field isolates the
        adjudication failure being tested while the probe digest stays
        internally consistent (the digest is recomputed at construction).
        """
        from tm_benchmark_query_process import (
            _process_pair_digest,
            _request_payload,
        )

        path = evidence.execution_path
        fts5_enabled = "true" if path is _FTS5 else "false"
        snapshot = verify_canonical_artifact(
            run_root=Path(evidence.run_root),
            fixture_path=Path(evidence.fixture_path),
            resource_id=evidence.resource_id,
            expected_fixture_digest=evidence.fixture_digest,
        )
        request_payload = _request_payload(
            mode="probe",
            process_evidence=evidence,
            run_root=evidence.run_root,
            fixture_path=evidence.fixture_path,
        )
        request_protocol_digest = request_payload["protocol_digest"]
        if type(request_protocol_digest) is not str:
            raise AssertionError("request protocol digest must be a string")
        facts: dict[str, Any] = dict(
            process_evidence_digest=evidence.evidence_digest,
            artifact_baseline_digest=artifact_snapshot_digest(
                evidence.artifact_snapshot
            ),
            process_pair_digest=_process_pair_digest(
                migration_child_pid=evidence.child_pid,
                query_child_pid=query_child_pid,
            ),
            query_protocol_digest=request_protocol_digest,
            artifact_pre=snapshot,
            artifact_post=snapshot,
            generation=evidence.generation,
            actual_index_kind=evidence.actual_index_kind,
            reopen_health_index_kind=evidence.actual_index_kind,
            reopen_health_record_count=evidence.record_count,
            record_count=evidence.record_count,
            exact_actual_path=path,
            fuzzy_actual_path=path,
            environment=_query_environment(fts5_enabled),
            query_rss_scope=_SCOPE,
        )
        facts.update(overrides)
        if "environment" in facts and "environment_digest" not in overrides:
            facts["environment_digest"] = benchmark_environment_digest(
                facts["environment"]
            )
        return _probe_report(**facts)

    def _run_fake(
        self,
        report: QueryProbeReport,
        evidence: TMBenchmarkProcessEvidence,
    ) -> QueryProcessError:
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self._fake_response(report),
            stderr="",
        )
        with patch(
            "tm_benchmark_query_process.subprocess.run",
            return_value=fake,
        ):
            with self.assertRaises(QueryProcessError) as raised:
                self._run(evidence)
        return raised.exception

    def test_runner_rejects_fake_child_process_evidence_digest_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                process_evidence_digest=_digest("9"),
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_fake_child_process_pair_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                process_pair_digest=_digest("9"),
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_fake_child_query_protocol_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                query_protocol_digest=_digest("9"),
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_fake_child_generation_and_count_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                generation=evidence.generation + 1,
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                record_count=evidence.record_count + 1,
                reopen_health_record_count=evidence.record_count + 1,
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_fake_child_path_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                actual_index_kind="GRAM_FALLBACK",
                reopen_health_index_kind="GRAM_FALLBACK",
                exact_actual_path=_FALLBACK,
                fuzzy_actual_path=_FALLBACK,
                environment=_query_environment("false"),
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_fake_child_artifact_proof_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            tampered = _artifact_snapshot(sidecar_digest=_digest("9"))
            report = self._evidence_consistent_probe(
                evidence,
                artifact_pre=tampered,
                artifact_post=tampered,
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.ARTIFACT_BASELINE_DRIFT",
            )

    def test_runner_rejects_fake_child_artifact_baseline_digest_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = self._evidence_consistent_probe(
                evidence,
                artifact_baseline_digest=_digest("9"),
            )
            self.assertEqual(
                self._run_fake(report, evidence).error_code,
                "QUERY.FACT_DRIFT",
            )

    def test_runner_rejects_same_pid_and_non_distinct_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fake = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=self._valid_response(query_pid=os.getpid()),
                stderr="",
            )
            with patch("tm_benchmark_query_process.subprocess.run", return_value=fake):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.CHILD_PID_INVALID")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fake = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=self._valid_response(query_pid=evidence.child_pid),
                stderr="",
            )
            with patch("tm_benchmark_query_process.subprocess.run", return_value=fake):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.CHILD_PID_NOT_DISTINCT")

    def test_runner_rejects_protocol_and_kind_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            envelope = json.loads(self._valid_response())
            envelope["protocol"] = "wrong"
            fake = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                stderr="",
            )
            with patch("tm_benchmark_query_process.subprocess.run", return_value=fake):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.RESPONSE_INVALID")

    def test_runner_detects_artifact_mutation_during_child_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            sidecar = Path(evidence.fixture_path + ".sqlite3")

            def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                with sidecar.open("ab") as stream:
                    stream.write(b"tampered")
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=self._valid_response(),
                    stderr="",
                )

            with patch(
                "tm_benchmark_query_process.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.ARTIFACT_MUTATED")

    def test_runner_detects_optional_family_mutation_during_child_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            journal = next(
                path
                for path in root.iterdir()
                if "activation-journal" in path.name
            )

            def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                journal.write_bytes(journal.read_bytes() + b"tampered")
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=self._valid_response(),
                    stderr="",
                )

            with patch(
                "tm_benchmark_query_process.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.ARTIFACT_MUTATED")

    def test_runner_rejects_evidence_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            report = _probe_report()
            envelope = {
                "kind": "probe",
                "payload": query_probe_to_payload(report),
                "protocol": QUERY_WORKER_PROTOCOL_VERSION,
                "query_pid": 424242,
            }
            payload = envelope["payload"]
            assert isinstance(payload, dict)
            payload["probe_digest"] = _digest("0")
            fake = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                stderr="",
            )
            with patch("tm_benchmark_query_process.subprocess.run", return_value=fake):
                with self.assertRaises(QueryProcessError) as raised:
                    self._run(evidence)
            self.assertEqual(raised.exception.error_code, "QUERY.EVIDENCE_INVALID")


class NoBodyLeakageAndBoundaryTests(unittest.TestCase):
    def test_evidence_payloads_never_contain_query_source_or_target_bodies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fixture_bytes = Path(evidence.fixture_path).read_bytes()
            first_row = fixture_bytes.splitlines()[0].decode("utf-8")
            probe = run_query_process_probe(evidence, timeout_seconds=120.0).probe
            payload_text = json.dumps(query_probe_to_payload(probe))
            self.assertNotIn(first_row, payload_text)
            self.assertNotIn("target", payload_text)
            latency_text = json.dumps(latency_evidence_to_payload(_latency_evidence()))
            for sample_payload in json.loads(
                latency_evidence_to_json(_latency_evidence())
            )["exact_samples"]:
                self.assertEqual(
                    set(sample_payload),
                    {
                        "actual_path",
                        "cohort",
                        "elapsed_ns",
                        "minimum_similarity",
                        "query_id",
                        "result_count",
                        "succeeded",
                        "top_k",
                    },
                )

    def test_query_owner_imports_no_capability_report_or_gate_modules(self) -> None:
        source = (_ROOT / "tm_benchmark_query_process.py").read_text(encoding="utf-8")
        imports = _IMPORT_RE.findall(source)
        banned = {
            "tm_benchmark_gate",
            "tm_retrieval_capability",
            "tm_benchmark_oracle",
            "matcher_capability",
            "matcher_validation",
        }
        self.assertTrue(set(imports).isdisjoint(banned))
        self.assertNotIn("BenchmarkReport(", source)
        self.assertNotIn("BenchmarkSuiteReport(", source)

    def test_latency_and_process_owners_never_import_query_owner(self) -> None:
        for module_name in ("tm_benchmark_latency.py", "tm_benchmark_process.py"):
            source = (_ROOT / module_name).read_text(encoding="utf-8")
            self.assertNotIn("tm_benchmark_query_process", source)

    def test_runtime_modules_never_import_query_owner(self) -> None:
        for module_name in (
            "tm_sqlite_store.py",
            "tm_retrieval.py",
            "tm_candidate_index.py",
            "tm_contracts.py",
        ):
            source = (_ROOT / module_name).read_text(encoding="utf-8")
            self.assertNotIn("tm_benchmark_query_process", source)


class ProcessOwnerCodecRegressionTests(unittest.TestCase):
    def test_process_evidence_payload_and_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            payload = process_evidence_to_payload(evidence)
            self.assertEqual(payload["evidence_digest"], evidence.evidence_digest)
            from tm_benchmark_process import evidence_from_payload

            restored = evidence_from_payload(payload)
            self.assertEqual(restored, evidence)
            parsed = json.loads(process_evidence_to_json(evidence))
            self.assertEqual(parsed["evidence_digest"], evidence.evidence_digest)

    def test_process_canonical_artifact_paths_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = _migrate(root, _FTS5)
            fixture = Path(evidence.fixture_path)
            fixture_path, sidecar, manifest = process_canonical_artifact_paths(
                resource_id=evidence.resource_id,
                fixture_path=evidence.fixture_path,
            )
            self.assertEqual(fixture_path, fixture)
            self.assertEqual(sidecar, Path(evidence.fixture_path + ".sqlite3"))
            self.assertEqual(
                manifest,
                Path(evidence.fixture_path + ".localcat-snapshot.json"),
            )

    def test_rss_peak_bytes_facts_normalize_to_bytes(self) -> None:
        import resource as resource_module

        usage = resource_module.getrusage(resource_module.RUSAGE_SELF)
        platform_name, raw_unit, peak_bytes = rss_peak_bytes_facts(usage)
        self.assertIn(raw_unit, ("kib", "bytes"))
        if raw_unit == "kib":
            self.assertEqual(peak_bytes, usage.ru_maxrss * 1024)
        else:
            self.assertEqual(peak_bytes, usage.ru_maxrss)
        self.assertGreater(peak_bytes, 0)
        self.assertEqual(peak_bytes % 1024, 0 if raw_unit == "kib" else peak_bytes % 1024)


if __name__ == "__main__":
    unittest.main()
