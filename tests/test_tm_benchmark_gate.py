"""Focused tests for the Task 8.5B gate-combination owner."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, cast
import unittest

from tm_benchmark import load_benchmark_contract
from tm_benchmark_latency import (
    LATENCY_EVIDENCE_SCHEMA_VERSION,
    LatencyEvidence,
    LatencySample,
    recompute_cohort_statistics,
)
from tm_benchmark_oracle import (
    ORACLE_EVIDENCE_SCHEMA_VERSION,
    OracleQueryRow,
    OracleRecallEvidence,
)
from tm_benchmark_process import (
    PROCESS_EVIDENCE_SCHEMA_VERSION,
    TMBenchmarkProcessEvidence,
    ArtifactFileIdentity as ProcessArtifactFileIdentity,
    ArtifactSnapshot as ProcessArtifactSnapshot,
    artifact_snapshot_digest,
    worker_protocol_digest,
)
from tm_benchmark_query_process import (
    QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
    ArtifactFileIdentity,
    ArtifactSnapshot,
    QueryProcessEvidence,
    QueryProcessRunResult,
    _process_pair_digest,
    _request_payload,
)
from tm_benchmark_gate import (
    BENCHMARK_BUNDLE_SCHEMA_VERSION,
    BenchmarkEvidenceBundle,
    BenchmarkPathBundle,
    benchmark_evidence_bundle_digest,
    benchmark_evidence_bundle_from_json,
    benchmark_evidence_bundle_from_payload,
    benchmark_evidence_bundle_to_json,
    benchmark_evidence_bundle_to_payload,
    combine_benchmark_evidence,
    retrieval_benchmark_evidence_by_path,
    retrieval_benchmark_evidence_pair,
)
from tm_contracts import (
    BENCHMARK_PERCENTILE_METHOD,
    BENCHMARK_SUITE_VERSION,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    benchmark_suite_contract_digest,
    candidate_budget_v1,
)
from tm_retrieval_capability import RetrievalBenchmarkEvidence

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")

_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK
_SCOPE = _CONTRACT.rss_scope

_GENERATED_AT = "2026-08-13T00:00:00Z"
_VALID_UNTIL = "2026-08-14T00:00:00Z"

_IMPORT_RE = re.compile(r"^(?:import|from)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)

_PRODUCTION_MODULES = (
    "qt_editor.py",
    "qt_editor_window.py",
    "text_matcher.py",
    "matcher_capability.py",
    "tm_engine.py",
    "tm_retrieval.py",
    "tm_retrieval_capability.py",
    "tm_retrieval_validation.py",
    "tm_sqlite_store.py",
    "tm_migration.py",
    "glossary_engine.py",
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
)


def _digest(prefix: str) -> str:
    return prefix * 64


def _nested(payload: dict[str, object], *keys: str) -> dict[str, object]:
    value: object = payload
    for key in keys:
        value = cast(dict[str, object], value)[key]
    return cast(dict[str, object], value)


def _nested_list(payload: dict[str, object], *keys: str) -> list[object]:
    value: object = payload
    for key in keys:
        value = cast(dict[str, object], value)[key]
    return cast(list[object], value)


def _environment(
    fts5_enabled: str,
    *,
    timing: bool,
    rss: bool,
) -> tuple[tuple[str, str], ...]:
    facts = {
        "cpu": "test-cpu",
        "fts5_enabled": fts5_enabled,
        "os": "test-os",
        "python_version": "test-python",
        "ram_mib": "1024",
        "sqlite_version": "test-sqlite",
        "unicode_version": "test-unicode",
    }
    if timing:
        facts.update(
            {
                "timing_clock": "test-clock-v1",
                "percentile_method": BENCHMARK_PERCENTILE_METHOD,
                "warmup_queries_per_cohort": "100",
                "measured_repeats": "1",
            }
        )
    if rss:
        facts.update(
            {
                "rss_platform": "test-platform",
                "rss_raw_unit": "bytes",
                "rss_scope": _SCOPE,
            }
        )
    return tuple(sorted(facts.items()))


def _process_environment(path: BenchmarkExecutionPath) -> tuple[tuple[str, str], ...]:
    fts5_enabled = "true" if path is _FTS5 else "false"
    return _environment(fts5_enabled, timing=False, rss=True)


def _oracle_environment(path: BenchmarkExecutionPath) -> tuple[tuple[str, str], ...]:
    fts5_enabled = "true" if path is _FTS5 else "false"
    return _environment(fts5_enabled, timing=False, rss=False)


def _path_config_digest(path: BenchmarkExecutionPath) -> str:
    if path is _FTS5:
        return _CONTRACT.fast_path_config_digest
    return _CONTRACT.fallback_path_config_digest


def _index_kind(path: BenchmarkExecutionPath) -> str:
    if path is _FTS5:
        return "FTS5_TRIGRAM"
    return "GRAM_FALLBACK"


def _latency_evidence(
    path: BenchmarkExecutionPath,
    *,
    latency_scale: int = 1,
) -> LatencyEvidence:
    exact_samples = tuple(
        LatencySample(
            query_id=index,
            elapsed_ns=(index * 37 + 3) * latency_scale,
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
            elapsed_ns=(index * 41 + 7) * latency_scale,
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
    environment = _environment(
        "true" if path is _FTS5 else "false",
        timing=True,
        rss=False,
    )
    return LatencyEvidence(
        schema_version=LATENCY_EVIDENCE_SCHEMA_VERSION,
        contract=_CONTRACT,
        contract_digest=benchmark_contract_digest(_CONTRACT),
        exact_cohort_digest=_CONTRACT.exact_cohort_digest,
        fuzzy_cohort_digest=_CONTRACT.fuzzy_cohort_digest,
        execution_path=path,
        path_config_digest=_path_config_digest(path),
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


def _process_artifact_snapshot() -> ProcessArtifactSnapshot:
    identity = ProcessArtifactFileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    return ProcessArtifactSnapshot(
        sidecar_digest=_digest("a"),
        manifest_digest=_digest("b"),
        family_digest=_digest("7"),
        sidecar_identity=identity,
        manifest_identity=identity,
    )


def _query_artifact_snapshot() -> ArtifactSnapshot:
    identity = ArtifactFileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    return ArtifactSnapshot(
        sidecar_digest=_digest("a"),
        manifest_digest=_digest("b"),
        family_digest=_digest("7"),
        sidecar_identity=identity,
        manifest_identity=identity,
    )


def _process_evidence(
    path: BenchmarkExecutionPath,
    *,
    child_pid: int,
    migration_elapsed_ns: int = 5_000_000_000,
    peak_rss_bytes: int = 300_000_000,
) -> TMBenchmarkProcessEvidence:
    contract_digest = benchmark_contract_digest(_CONTRACT)
    environment = _process_environment(path)
    snapshot = _process_artifact_snapshot()
    return TMBenchmarkProcessEvidence(
        schema_version=PROCESS_EVIDENCE_SCHEMA_VERSION,
        test_mode=False,
        contract=_CONTRACT,
        contract_digest=contract_digest,
        corpus_digest=_CONTRACT.corpus_digest,
        corpus_record_count=100_000,
        fixture_digest=_digest("d"),
        fixture_path="/tmp/benchmark-run-root/fixture.jsonl",
        fixture_record_count=100_000,
        run_root="/tmp/benchmark-run-root",
        resource_id="tm.benchmark",
        canonical_store_id="store.benchmark",
        execution_path=path,
        path_config_digest=_path_config_digest(path),
        actual_index_kind=_index_kind(path),
        record_count=100_000,
        generation=0,
        migration_elapsed_ns=migration_elapsed_ns,
        peak_rss_bytes=peak_rss_bytes,
        rss_start_bytes=peak_rss_bytes - 100_000_000,
        rss_terminal_bytes=peak_rss_bytes,
        rss_unit="bytes",
        rss_scope=_SCOPE,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
        worker_protocol_digest=worker_protocol_digest(
            contract_digest=contract_digest,
            corpus_digest=_CONTRACT.corpus_digest,
            corpus_record_count=100_000,
            fixture_digest=_digest("d"),
            fixture_path="/tmp/benchmark-run-root/fixture.jsonl",
            fixture_record_count=100_000,
            run_root="/tmp/benchmark-run-root",
            execution_path=path,
            resource_id="tm.benchmark",
            canonical_store_id="store.benchmark",
            test_mode=False,
        ),
        artifact_snapshot=snapshot,
        child_pid=child_pid,
        child_exit_code=0,
        reopen_phase="GENERATION_PUBLISHED",
        reopen_action="COMPLETED",
        reopen_health_healthy=True,
        reopen_health_index_kind=_index_kind(path),
        reopen_health_record_count=100_000,
        reopen_health_exact_available=True,
        exact_proof_result_count=1,
        exact_proof_winner_matched=True,
        candidate_proof_index_kind=_index_kind(path),
        candidate_proof_count=1,
        candidate_proof_available=True,
        candidate_proof_budget=candidate_budget_v1(_CONTRACT.top_k),
    )


def _query_evidence(
    process_evidence: TMBenchmarkProcessEvidence,
    *,
    query_child_pid: int,
    query_peak_rss_bytes: int = 200_000_000,
    latency_scale: int = 1,
) -> tuple[QueryProcessEvidence, str]:
    path = process_evidence.execution_path
    latency = _latency_evidence(path, latency_scale=latency_scale)
    request = _request_payload(
        mode="evidence",
        process_evidence=process_evidence,
        run_root=process_evidence.run_root,
        fixture_path=process_evidence.fixture_path,
    )
    request_protocol_digest = request["protocol_digest"]
    if type(request_protocol_digest) is not str:
        raise AssertionError("request protocol digest must be a string")
    snapshot = _query_artifact_snapshot()
    evidence = QueryProcessEvidence(
        schema_version=QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
        artifact_key=_digest("c"),
        contract_digest=process_evidence.contract_digest,
        corpus_digest=_CONTRACT.corpus_digest,
        corpus_record_count=100_000,
        fixture_digest=process_evidence.fixture_digest,
        fixture_record_count=100_000,
        resource_id="tm.benchmark",
        canonical_store_id="store.benchmark",
        execution_path=path,
        path_config_digest=process_evidence.path_config_digest,
        actual_index_kind=process_evidence.actual_index_kind,
        record_count=100_000,
        generation=0,
        process_evidence_digest=process_evidence.evidence_digest,
        artifact_baseline_digest=artifact_snapshot_digest(
            process_evidence.artifact_snapshot
        ),
        process_test_mode=False,
        processes_distinct=True,
        process_pair_digest=_process_pair_digest(
            migration_child_pid=process_evidence.child_pid,
            query_child_pid=query_child_pid,
        ),
        query_protocol_digest=request_protocol_digest,
        artifact_pre=snapshot,
        artifact_post=snapshot,
        latency_evidence=latency,
        latency_evidence_digest=latency.evidence_digest,
        query_peak_rss_bytes=query_peak_rss_bytes,
        query_rss_start_bytes=query_peak_rss_bytes - 50_000_000,
        query_rss_terminal_bytes=query_peak_rss_bytes,
        query_rss_unit="bytes",
        query_rss_scope=_SCOPE,
        environment=process_evidence.environment,
        environment_digest=process_evidence.environment_digest,
    )
    return evidence, request_protocol_digest


def _oracle_rows(
    path: BenchmarkExecutionPath,
    *,
    missing_top10_queries: int,
) -> tuple[OracleQueryRow, ...]:
    index_kind = _index_kind(path)
    rows: list[OracleQueryRow] = []
    for query_id in range(1, _CONTRACT.oracle_query_count + 1):
        reference = (query_id * 17) % 100_000 + 1
        category = ("exact", "near-edit", "miss")[query_id % 3]
        top10 = tuple(
            (reference + offset * 3) % 100_000 + 1 for offset in range(10)
        )
        above = tuple(
            sorted(
                (
                    reference,
                    (reference + 500_017) % 100_000 + 1,
                )
            )
        )
        if query_id <= missing_top10_queries:
            candidate_ids = top10[1:]
        else:
            candidate_ids = top10
        candidate_ids = tuple(sorted(set(candidate_ids) | set(above)))
        missing_top10_ids = tuple(
            record_id
            for record_id in top10
            if record_id not in candidate_ids
        )
        missing_above_ids = tuple(
            record_id
            for record_id in above
            if record_id not in candidate_ids
        )
        rows.append(
            OracleQueryRow(
                query_id=query_id,
                category=category,
                reference_record_id=(
                    None if category == "miss" else reference
                ),
                candidate_ids=candidate_ids,
                above_threshold_ids=above,
                top10_ids=top10,
                missing_above_threshold_ids=missing_above_ids,
                missing_top10_ids=missing_top10_ids,
                candidate_count=len(candidate_ids),
                above_count=len(above),
                top10_count=len(top10),
                actual_index_kind=index_kind,
                candidate_available=True,
                unavailable_code=None,
                truncated=False,
            )
        )
    return tuple(rows)


def _oracle_evidence(
    path: BenchmarkExecutionPath,
    *,
    missing_top10_queries: int,
) -> OracleRecallEvidence:
    rows = _oracle_rows(path, missing_top10_queries=missing_top10_queries)
    environment = _oracle_environment(path)
    return OracleRecallEvidence(
        schema_version=ORACLE_EVIDENCE_SCHEMA_VERSION,
        test_mode=False,
        contract=_CONTRACT,
        contract_digest=benchmark_contract_digest(_CONTRACT),
        oracle_subset_digest=_CONTRACT.oracle_subset_digest,
        oracle_subset_record_count=_CONTRACT.oracle_subset_record_count,
        oracle_query_count=_CONTRACT.oracle_query_count,
        scorer_config_digest=_CONTRACT.scorer_config_digest,
        path_config_digest=_path_config_digest(path),
        execution_path=path,
        store_index_kind=_index_kind(path),
        resource_id="tm.benchmark",
        canonical_store_id="store.benchmark",
        fixture_digest=_digest("d"),
        result_limit=_CONTRACT.top_k,
        candidate_budget_version=_CONTRACT.candidate_budget_version,
        candidate_budget=candidate_budget_v1(_CONTRACT.top_k),
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
        rows=rows,
        query_count=_CONTRACT.oracle_query_count,
        missing_above_threshold_total=0,
        missing_top10_total=missing_top10_queries,
        all_queries_available=True,
        index_kind_drift_count=0,
        recall_passed=missing_top10_queries == 0,
    )


def _run_result(
    path: BenchmarkExecutionPath,
    *,
    child_pid: int,
    query_child_pid: int,
    missing_top10_queries: int,
    migration_elapsed_ns: int = 5_000_000_000,
    peak_rss_bytes: int = 300_000_000,
    query_peak_rss_bytes: int = 200_000_000,
    latency_scale: int = 1,
) -> tuple[QueryProcessRunResult, OracleRecallEvidence]:
    process_evidence = _process_evidence(
        path,
        child_pid=child_pid,
        migration_elapsed_ns=migration_elapsed_ns,
        peak_rss_bytes=peak_rss_bytes,
    )
    evidence, request_protocol_digest = _query_evidence(
        process_evidence,
        query_child_pid=query_child_pid,
        query_peak_rss_bytes=query_peak_rss_bytes,
        latency_scale=latency_scale,
    )
    run = QueryProcessRunResult(
        process_evidence=process_evidence,
        evidence=evidence,
        query_child_pid=query_child_pid,
        run_root=process_evidence.run_root,
        fixture_path=process_evidence.fixture_path,
        artifact_pre=evidence.artifact_pre,
        artifact_post=evidence.artifact_post,
        request_protocol_digest=request_protocol_digest,
    )
    oracle = _oracle_evidence(
        path,
        missing_top10_queries=missing_top10_queries,
    )
    return run, oracle


def _fts5_fixture(
    *,
    missing_top10_queries: int = 27,
    **overrides: int,
) -> tuple[QueryProcessRunResult, OracleRecallEvidence]:
    return _run_result(
        _FTS5,
        child_pid=12_345,
        query_child_pid=23_456,
        missing_top10_queries=missing_top10_queries,
        **overrides,
    )


def _fallback_fixture(
    *,
    missing_top10_queries: int = 27,
    **overrides: int,
) -> tuple[QueryProcessRunResult, OracleRecallEvidence]:
    return _run_result(
        _FALLBACK,
        child_pid=34_567,
        query_child_pid=45_678,
        missing_top10_queries=missing_top10_queries,
        **overrides,
    )


def _combined_bundle(
    *,
    fts5_missing: int = 27,
    fallback_missing: int = 27,
    **overrides: int,
) -> BenchmarkEvidenceBundle:
    fts5_run, fts5_oracle = _fts5_fixture(missing_top10_queries=fts5_missing)
    fallback_run, fallback_oracle = _fallback_fixture(
        missing_top10_queries=fallback_missing
    )
    return combine_benchmark_evidence(
        fts5_run,
        fallback_run,
        fts5_oracle,
        fallback_oracle,
    )


class CombineBenchmarkEvidenceTests(unittest.TestCase):
    def test_combine_builds_immutable_bundle_with_both_paths(self) -> None:
        bundle = _combined_bundle()
        self.assertEqual(bundle.schema_version, BENCHMARK_BUNDLE_SCHEMA_VERSION)
        self.assertIsInstance(bundle, BenchmarkEvidenceBundle)
        self.assertEqual(
            bundle.contract_digest,
            benchmark_contract_digest(bundle.contract),
        )
        self.assertEqual(
            bundle.suite_contract.benchmark_contract,
            bundle.contract,
        )
        self.assertEqual(
            bundle.suite_contract_digest,
            benchmark_suite_contract_digest(bundle.suite_contract),
        )
        self.assertEqual(
            bundle.suite_report.path_reports,
            (bundle.fts5.report, bundle.fallback.report),
        )
        self.assertEqual(
            bundle.recompute_bundle_digest(),
            bundle.bundle_digest,
        )
        self.assertEqual(
            benchmark_evidence_bundle_digest(bundle),
            bundle.bundle_digest,
        )

    def test_combine_mirrors_real_truth_both_paths_fail_candidate_recall(
        self,
    ) -> None:
        bundle = _combined_bundle(
            fts5_missing=27,
            fallback_missing=27,
        )
        expected_recall = (200 - 27) / 200
        for path_bundle in (bundle.fts5, bundle.fallback):
            report = path_bundle.report
            self.assertAlmostEqual(report.candidate_recall, expected_recall)
            self.assertFalse(report.passed)
            self.assertEqual(report.failed_gates, ("CANDIDATE_RECALL",))
        self.assertEqual(
            bundle.suite_report.failed_paths,
            (BenchmarkExecutionPath.FTS5_TRIGRAM, BenchmarkExecutionPath.GRAM_FALLBACK),
        )
        self.assertFalse(bundle.suite_report.passed)

    def test_combine_full_pass_path(self) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        for path_bundle in (bundle.fts5, bundle.fallback):
            report = path_bundle.report
            self.assertEqual(report.candidate_recall, 1.0)
            self.assertTrue(report.passed)
            self.assertEqual(report.failed_gates, ())
        self.assertTrue(bundle.suite_report.passed)
        self.assertEqual(bundle.suite_report.failed_paths, ())

    def test_combine_derives_metrics_from_owner_facts(self) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        for path_bundle in (bundle.fts5, bundle.fallback):
            report = path_bundle.report
            latency = path_bundle.latency_evidence
            exact_stats = recompute_cohort_statistics(
                tuple(sample.elapsed_ns for sample in latency.exact_samples)
            )
            fuzzy_stats = recompute_cohort_statistics(
                tuple(sample.elapsed_ns for sample in latency.fuzzy_samples)
            )
            self.assertEqual(
                report.exact_p50_ms,
                exact_stats[0] / 1_000_000,
            )
            self.assertEqual(
                report.exact_p95_ms,
                exact_stats[1] / 1_000_000,
            )
            self.assertEqual(
                report.fuzzy_top10_p95_ms,
                fuzzy_stats[1] / 1_000_000,
            )
            self.assertEqual(report.migration_seconds, 5.0)
            expected_peak_mib = max(
                path_bundle.process_facts.peak_rss_bytes,
                path_bundle.query_facts.query_peak_rss_bytes,
            ) / (1024 * 1024)
            self.assertEqual(report.peak_rss_mib, expected_peak_mib)
            self.assertEqual(
                report.environment_digest,
                benchmark_environment_digest(report.environment),
            )
            self.assertEqual(report.environment, path_bundle.environment)
            self.assertEqual(
                path_bundle.environment_digest,
                benchmark_environment_digest(path_bundle.environment),
            )
            environment = dict(report.environment)
            self.assertEqual(environment["timing_clock"], "test-clock-v1")
            self.assertEqual(environment["rss_scope"], _SCOPE)
            self.assertEqual(
                environment["fts5_enabled"],
                "true" if report.execution_path is _FTS5 else "false",
            )

    def test_combine_derives_hard_gate_failures_independently(self) -> None:
        fts5_run, fts5_oracle = _fts5_fixture(
            missing_top10_queries=0,
            migration_elapsed_ns=130_000_000_000,
        )
        fallback_run, fallback_oracle = _fallback_fixture(
            missing_top10_queries=0,
            query_peak_rss_bytes=600_000_000,
        )
        failing = combine_benchmark_evidence(
            fts5_run,
            fallback_run,
            fts5_oracle,
            fallback_oracle,
        )
        self.assertEqual(
            failing.fts5.report.failed_gates,
            ("MIGRATION",),
        )
        self.assertFalse(failing.fts5.report.passed)
        self.assertEqual(
            failing.fallback.report.failed_gates,
            ("PEAK_RSS",),
        )
        self.assertFalse(failing.fallback.report.passed)
        self.assertEqual(
            failing.suite_report.failed_paths,
            (BenchmarkExecutionPath.FTS5_TRIGRAM, BenchmarkExecutionPath.GRAM_FALLBACK),
        )

    def test_combine_rejects_one_path_only_input(self) -> None:
        fts5_run, fts5_oracle = _fts5_fixture()
        fallback_run, fallback_oracle = _fallback_fixture()
        with self.assertRaisesRegex(TypeError, "must be QueryProcessRunResult"):
            combine_benchmark_evidence(
                cast(Any, None),
                fallback_run,
                fts5_oracle,
                fallback_oracle,
            )
        with self.assertRaisesRegex(TypeError, "must be OracleRecallEvidence"):
            combine_benchmark_evidence(
                fts5_run,
                fallback_run,
                cast(Any, None),
                fallback_oracle,
            )

    def test_combine_rejects_duplicate_or_swapped_paths(self) -> None:
        fts5_run, fts5_oracle = _fts5_fixture()
        fallback_run, fallback_oracle = _fallback_fixture()
        with self.assertRaisesRegex(ValueError, "must execute FTS5_TRIGRAM"):
            combine_benchmark_evidence(
                fallback_run,
                fallback_run,
                fts5_oracle,
                fallback_oracle,
            )
        with self.assertRaisesRegex(ValueError, "must execute GRAM_FALLBACK"):
            combine_benchmark_evidence(
                fts5_run,
                fts5_run,
                fts5_oracle,
                fallback_oracle,
            )
        with self.assertRaisesRegex(ValueError, "must execute FTS5_TRIGRAM"):
            combine_benchmark_evidence(
                fts5_run,
                fallback_run,
                fallback_oracle,
                fallback_oracle,
            )

    def test_combine_requires_mergeable_owner_environments(self) -> None:
        from tm_benchmark_gate import _merge_environment

        fts5_environment = _process_environment(_FTS5)
        conflict = tuple(
            (key, "other" if key == "ram_mib" else value)
            for key, value in fts5_environment
        )
        with self.assertRaisesRegex(ValueError, "conflicting shared environment"):
            _merge_environment(fts5_environment, conflict)
        merged = _merge_environment(
            fts5_environment,
            _oracle_environment(_FTS5),
        )
        self.assertEqual(merged, fts5_environment)


class BundleCodecTests(unittest.TestCase):
    def test_payload_and_json_strict_round_trip(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        self.assertEqual(
            benchmark_evidence_bundle_from_payload(payload),
            bundle,
        )
        self.assertEqual(
            benchmark_evidence_bundle_to_payload(
                benchmark_evidence_bundle_from_payload(payload)
            ),
            payload,
        )
        serialized = benchmark_evidence_bundle_to_json(bundle)
        self.assertEqual(
            benchmark_evidence_bundle_from_json(serialized),
            bundle,
        )
        self.assertEqual(
            benchmark_evidence_bundle_to_json(
                benchmark_evidence_bundle_from_json(serialized)
            ),
            serialized,
        )

    def test_rejects_duplicate_json_keys(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        duplicated = (
            '{"schema_version":'
            + json.dumps(payload["schema_version"])
            + ","
            + benchmark_evidence_bundle_to_json(bundle)[1:]
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON keys|not strict"):
            benchmark_evidence_bundle_from_json(duplicated)

    def test_rejects_non_finite_numbers(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        report = _nested(payload, "fts5", "report")
        report["candidate_recall"] = float("inf")
        with self.assertRaises((TypeError, ValueError)):
            benchmark_evidence_bundle_from_payload(payload)
        serialized = benchmark_evidence_bundle_to_json(bundle)
        forged = serialized.replace(":0.865", ":NaN")
        with self.assertRaisesRegex(ValueError, "not strict|non-finite"):
            benchmark_evidence_bundle_from_json(forged)

    def test_rejects_bool_as_int(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        _nested(payload, "fts5", "report")["exact_sample_count"] = True
        with self.assertRaises(TypeError):
            benchmark_evidence_bundle_from_payload(payload)
        payload = benchmark_evidence_bundle_to_payload(bundle)
        _nested(payload, "fts5", "query_facts")["generation"] = True
        with self.assertRaises(TypeError):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_unknown_and_missing_fields(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        payload["extra"] = 1
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            benchmark_evidence_bundle_from_payload(payload)
        payload = benchmark_evidence_bundle_to_payload(bundle)
        del payload["suite_report"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_bundle_digest_drift(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        payload["bundle_digest"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "bundle digest"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_nested_report_drift(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        _nested(payload, "fts5", "report")["candidate_recall"] = 1.0
        with self.assertRaises(ValueError):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_suite_report_drift(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        _nested(payload, "suite_report")["passed"] = True
        with self.assertRaises(ValueError):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_path_swap(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        fts5_payload = payload["fts5"]
        fallback_payload = payload["fallback"]
        payload["fts5"] = fallback_payload
        payload["fallback"] = fts5_payload
        with self.assertRaisesRegex(ValueError, "FTS5_TRIGRAM"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_one_path_only_payload(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        del payload["fallback"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_discarded_latency_sample(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        samples = _nested_list(
            payload, "fts5", "latency_evidence", "exact_samples"
        )
        samples.pop()
        with self.assertRaises(ValueError):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_forged_process_environment(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        environment = _nested_list(
            payload, "fallback", "process_facts", "environment"
        )
        rebuilt: list[object] = []
        for entry in environment:
            pair = cast(list[object], entry)
            rebuilt.append(
                ["fts5_enabled", "true"]
                if pair[0] == "fts5_enabled"
                else entry
            )
        _nested(payload, "fallback", "process_facts")["environment"] = rebuilt
        with self.assertRaises(ValueError):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_forged_portable_artifact_key(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        query_facts = _nested(payload, "fts5", "query_facts")
        query_facts["portable_artifact_key"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "portable artifact key"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_process_query_stable_fact_drift(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        process_facts = _nested(payload, "fts5", "process_facts")
        process_facts["resource_id"] = "forged.resource"
        with self.assertRaisesRegex(ValueError, "resource id"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_forged_contract_digest(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        payload["contract_digest"] = _digest("0")
        with self.assertRaisesRegex(ValueError, "contract digest"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_rejects_non_dict_payload(self) -> None:
        with self.assertRaises(TypeError):
            benchmark_evidence_bundle_from_payload(cast(Any, "not a payload"))


class GateDEvidenceTests(unittest.TestCase):
    def test_by_path_constructs_exact_report_with_window(self) -> None:
        bundle = _combined_bundle()
        fts5_evidence = retrieval_benchmark_evidence_by_path(
            bundle,
            _FTS5,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
        )
        self.assertIsInstance(fts5_evidence, RetrievalBenchmarkEvidence)
        self.assertIs(fts5_evidence.report, bundle.fts5.report)
        self.assertEqual(fts5_evidence.generated_at_utc, _GENERATED_AT)
        self.assertEqual(fts5_evidence.valid_until_utc, _VALID_UNTIL)
        self.assertFalse(fts5_evidence.report.passed)

    def test_pair_shares_one_validity_window(self) -> None:
        bundle = _combined_bundle()
        fts5_evidence, fallback_evidence = retrieval_benchmark_evidence_pair(
            bundle,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
        )
        self.assertIs(fts5_evidence.report, bundle.fts5.report)
        self.assertIs(fallback_evidence.report, bundle.fallback.report)
        self.assertEqual(
            fts5_evidence.generated_at_utc,
            fallback_evidence.generated_at_utc,
        )
        self.assertEqual(
            fts5_evidence.valid_until_utc,
            fallback_evidence.valid_until_utc,
        )

    def test_rejects_invalid_window_and_path(self) -> None:
        bundle = _combined_bundle()
        with self.assertRaisesRegex(ValueError, "valid_until"):
            retrieval_benchmark_evidence_by_path(
                bundle,
                _FTS5,
                generated_at_utc=_VALID_UNTIL,
                valid_until_utc=_GENERATED_AT,
            )
        with self.assertRaisesRegex(TypeError, "execution_path"):
            retrieval_benchmark_evidence_by_path(
                bundle,
                cast(Any, "FTS5_TRIGRAM"),
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
            )
        with self.assertRaisesRegex(TypeError, "bundle"):
            retrieval_benchmark_evidence_by_path(
                cast(Any, None),
                _FTS5,
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
            )

    def test_failed_fallback_cannot_close_or_replace_fts_report(self) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=27)
        self.assertTrue(bundle.fts5.report.passed)
        self.assertEqual(bundle.fts5.report.failed_gates, ())
        self.assertFalse(bundle.fallback.report.passed)
        self.assertEqual(
            bundle.fallback.report.failed_gates,
            ("CANDIDATE_RECALL",),
        )
        self.assertEqual(
            bundle.suite_report.failed_paths,
            (BenchmarkExecutionPath.GRAM_FALLBACK,),
        )
        fts5_evidence, fallback_evidence = retrieval_benchmark_evidence_pair(
            bundle,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
        )
        self.assertTrue(fts5_evidence.report.passed)
        self.assertFalse(fallback_evidence.report.passed)
        self.assertIs(fts5_evidence.report, bundle.fts5.report)
        self.assertIs(fallback_evidence.report, bundle.fallback.report)

    def test_both_fail_recall_is_represented_independently(self) -> None:
        bundle = _combined_bundle(fts5_missing=27, fallback_missing=27)
        fts5_evidence, fallback_evidence = retrieval_benchmark_evidence_pair(
            bundle,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
        )
        self.assertFalse(fts5_evidence.report.passed)
        self.assertFalse(fallback_evidence.report.passed)
        self.assertEqual(
            fts5_evidence.report.failed_gates,
            ("CANDIDATE_RECALL",),
        )
        self.assertEqual(
            fallback_evidence.report.failed_gates,
            ("CANDIDATE_RECALL",),
        )
        self.assertIsNot(fts5_evidence.report, fallback_evidence.report)


class PortabilityAndBoundaryTests(unittest.TestCase):
    def test_bundle_json_contains_no_locators_pids_or_bodies(self) -> None:
        bundle = _combined_bundle()
        serialized = benchmark_evidence_bundle_to_json(bundle)
        forbidden = (
            "run_root",
            "fixture_path",
            "benchmark-run-root",
            "fixture.jsonl",
            "child_pid",
            "query_child_pid",
            "worker_protocol_digest",
            "query_protocol_digest",
            "process_pair_digest",
            '"artifact_key"',
            "inode",
            "mtime_ns",
            "sidecar_identity",
            "manifest_identity",
            "source_raw",
            "target_raw",
            "query_raw",
        )
        for token in forbidden:
            self.assertNotIn(token, serialized, f"bundle leaks {token!r}")
        self.assertNotIn("/tmp", serialized)

    def test_bundle_payload_uses_owner_internal_projections_only(self) -> None:
        bundle = _combined_bundle()
        payload = benchmark_evidence_bundle_to_payload(bundle)
        for path_key in ("fts5", "fallback"):
            path_payload = _nested(payload, path_key)
            self.assertEqual(
                set(path_payload),
                {
                    "execution_path",
                    "latency_evidence",
                    "oracle_evidence_json",
                    "process_facts",
                    "query_facts",
                    "report",
                },
            )
            self.assertNotIn("process_evidence", path_payload)
            self.assertNotIn("query_evidence", path_payload)

    def test_gate_module_import_boundaries(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "tm_benchmark_gate.py"
        ).read_text(encoding="utf-8")
        imported = set(_IMPORT_RE.findall(source))
        banned_prefixes = (
            "qt_",
            "text_matcher",
            "matcher_capability",
            "tm_engine",
            "tm_retrieval",
            "tm_sqlite_store",
            "tm_migration",
            "glossary",
            "tm_activation",
            "tm_candidate_index",
        )
        for prefix in banned_prefixes:
            self.assertFalse(
                any(name == prefix or name.startswith(prefix + ".")
                    for name in imported),
                f"gate module must not import {prefix!r}",
            )
        self.assertIn("tm_contracts", imported)
        self.assertIn("tm_benchmark_query_process", imported)
        self.assertIn("tm_retrieval_capability", imported)

    def test_gate_module_never_publishes_capability(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "tm_benchmark_gate.py"
        ).read_text(encoding="utf-8")
        for token in (
            "RetrievalCapabilityPublisher(",
            "RetrievalCapabilityEvaluator(",
            "RetrievalCapabilityManifest(",
            "refresh(",
            "publish(",
        ):
            self.assertNotIn(token, source)

    def test_no_production_module_imports_the_gate_module(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for module in _PRODUCTION_MODULES:
            source = (root / module).read_text(encoding="utf-8")
            self.assertNotIn(
                "import tm_benchmark_gate",
                source,
                f"{module} must not import tm_benchmark_gate",
            )

    def test_bundle_keeps_full_latency_raw_samples(self) -> None:
        bundle = _combined_bundle()
        for path_bundle in (bundle.fts5, bundle.fallback):
            latency = path_bundle.latency_evidence
            self.assertEqual(
                latency.exact_sample_count,
                len(latency.exact_samples),
            )
            self.assertEqual(
                latency.exact_sample_count,
                _CONTRACT.exact_cohort_count,
            )
            self.assertEqual(
                latency.fuzzy_sample_count,
                _CONTRACT.fuzzy_cohort_count,
            )
            self.assertEqual(
                latency.recompute_evidence_digest(),
                latency.evidence_digest,
            )
            self.assertEqual(
                latency.recompute_statistics(),
                (
                    latency.exact_p50_ns,
                    latency.exact_p95_ns,
                    latency.exact_max_ns,
                    latency.fuzzy_p50_ns,
                    latency.fuzzy_p95_ns,
                    latency.fuzzy_max_ns,
                ),
            )


if __name__ == "__main__":
    unittest.main()
