"""Focused tests for the Task 8.5C gate owner.

Covers the strict Gate D combination (Task 8.5B), the owner-driven real
runner with its private test port seam (fixed invocation order, four exact
dedicated roots, atomic durable persistence with strict readback, fail-closed
cleanup and stable-code diagnostics), and the Gate D capability publication
(exact-type manifest/publisher boundaries, value-for-value Gate C
preservation, honest per-path decisions and single refresh).  All runner
tests use injected exact-type owner ports with small synthetic fixtures; the
literal 100k corpus is never executed here.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
import tempfile
from typing import Any, cast
import unittest
from unittest import mock

import tm_benchmark_gate

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
    BENCHMARK_IMPLEMENTATION_SOURCE_PATHS,
    BenchmarkEvidenceBundle,
    BenchmarkGateDError,
    BenchmarkGateDRunResult,
    BenchmarkPathBundle,
    RetrievalCapabilityPublicationResult,
    _DEFAULT_GATE_D_RUNNER_PORTS,
    _GateDRunnerPorts,
    _run_benchmark_gate_d_test,
    benchmark_evidence_bundle_digest,
    benchmark_evidence_bundle_from_json,
    benchmark_evidence_bundle_from_payload,
    benchmark_evidence_bundle_to_json,
    benchmark_evidence_bundle_to_payload,
    benchmark_implementation_fingerprint,
    combine_benchmark_evidence,
    publish_retrieval_capability_gate_d,
    retrieval_benchmark_evidence_by_path,
    retrieval_benchmark_evidence_pair,
    run_benchmark_gate_d,
)
from tm_contracts import (
    BENCHMARK_PERCENTILE_METHOD,
    BENCHMARK_SUITE_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    benchmark_suite_contract_digest,
    candidate_budget_v1,
)
from tm_retrieval_capability import (
    RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
    RETRIEVAL_SEMANTICS_VERSION,
    RetrievalBenchmarkEvidence,
    RetrievalBenchmarkExpectation,
    RetrievalCapabilityEvaluator,
    RetrievalCapabilityExpectation,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalCorrectnessCohortEvidence,
    RetrievalCohortExpectation,
)

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
_IMPLEMENTATION_FINGERPRINT = benchmark_implementation_fingerprint(_ROOT)

_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK
_SCOPE = _CONTRACT.rss_scope

_GENERATED_AT = "2026-08-13T00:00:00Z"
_VALID_UNTIL = "2026-08-14T00:00:00Z"
_EVALUATED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_TEST_RECORD_COUNT = 8

_ARTIFACT_DIGEST = hashlib.sha256(b"gate-d-artifact").hexdigest()
_BUILD_DIGEST = hashlib.sha256(b"gate-d-build").hexdigest()
_FIXTURE_DIGEST = hashlib.sha256(b"gate-d-fixture").hexdigest()
_EVALUATOR_DIGEST = hashlib.sha256(b"gate-d-evaluator").hexdigest()
_CONTEXT_COHORT_DIGEST = hashlib.sha256(b"gate-d-context-cohort").hexdigest()
_FUZZY_CORE_COHORT_DIGEST = hashlib.sha256(b"gate-d-fuzzy-core-cohort").hexdigest()

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


def _runner_ports(
    *,
    fts5_missing: int = 0,
    fallback_missing: int = 0,
    combine_side_effect: Any = None,
) -> tuple[_GateDRunnerPorts, dict[str, list[object]]]:
    """Build exact-type injected runner ports over small synthetic fixtures.

    The ports record invocation order and the exact dedicated roots the
    runner hands them; the combine port delegates to the real strict
    ``combine_benchmark_evidence`` so the produced bundle is final-shaped.
    Oracle fixtures are fully clear by default so the pipeline reaches the
    combine stage; fail-closed oracle tests pass explicit miss counts.
    ``combine_side_effect`` runs after combination to sabotage the owned
    tree for fail-closed cleanup tests.
    """

    record: dict[str, list[object]] = {
        "calls": [],
        "migration_roots": [],
        "oracle_roots": [],
    }

    def migration_port(
        *,
        contract_path: object,
        execution_path: BenchmarkExecutionPath,
        run_root: object,
        test_mode: bool,
        test_record_count: int | None,
        test_seed: int | None,
    ) -> TMBenchmarkProcessEvidence:
        record["calls"].append(("migration", execution_path))
        record["migration_roots"].append(Path(str(run_root)))
        child_pid = 12_345 if execution_path is _FTS5 else 34_567
        return _process_evidence(execution_path, child_pid=child_pid)

    def query_port(
        process_evidence: TMBenchmarkProcessEvidence,
    ) -> QueryProcessRunResult:
        record["calls"].append(("query", process_evidence.execution_path))
        query_child_pid = (
            23_456
            if process_evidence.execution_path is _FTS5
            else 45_678
        )
        evidence, request_protocol_digest = _query_evidence(
            process_evidence,
            query_child_pid=query_child_pid,
        )
        return QueryProcessRunResult(
            process_evidence=process_evidence,
            evidence=evidence,
            query_child_pid=query_child_pid,
            run_root=process_evidence.run_root,
            fixture_path=process_evidence.fixture_path,
            artifact_pre=evidence.artifact_pre,
            artifact_post=evidence.artifact_post,
            request_protocol_digest=request_protocol_digest,
        )

    def oracle_port(
        *,
        contract: object,
        fts5_run_root: object,
        fallback_run_root: object,
    ) -> tuple[OracleRecallEvidence, OracleRecallEvidence]:
        record["calls"].append(("oracle",))
        record["oracle_roots"].append(Path(str(fts5_run_root)))
        record["oracle_roots"].append(Path(str(fallback_run_root)))
        return (
            _oracle_evidence(
                _FTS5,
                missing_top10_queries=fts5_missing,
            ),
            _oracle_evidence(
                _FALLBACK,
                missing_top10_queries=fallback_missing,
            ),
        )

    def combine_port(
        fts5_run: QueryProcessRunResult,
        fallback_run: QueryProcessRunResult,
        fts5_oracle: OracleRecallEvidence,
        fallback_oracle: OracleRecallEvidence,
    ) -> BenchmarkEvidenceBundle:
        record["calls"].append(("combine",))
        bundle = combine_benchmark_evidence(
            fts5_run,
            fallback_run,
            fts5_oracle,
            fallback_oracle,
        )
        if combine_side_effect is not None:
            combine_side_effect(record)
        return bundle

    ports = _GateDRunnerPorts(
        run_process_migration_evidence=migration_port,
        run_query_process_evidence=query_port,
        run_oracle_recall_suite=oracle_port,
        combine_benchmark_evidence=combine_port,
    )
    return ports, record


def _capability_cohort(
    cohort_id: str,
    cohort_digest: str,
    *,
    passed: bool = True,
) -> RetrievalCorrectnessCohortEvidence:
    return RetrievalCorrectnessCohortEvidence(
        cohort_id=cohort_id,
        cohort_digest=cohort_digest,
        passed=passed,
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
    )


def _capability_expectation(
    *,
    evaluator_digest: str = _EVALUATOR_DIGEST,
) -> RetrievalCapabilityExpectation:
    contract_digest = benchmark_contract_digest(_CONTRACT)
    return RetrievalCapabilityExpectation(
        evidence_schema_version=RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION,
        retrieval_artifact_digest=_ARTIFACT_DIGEST,
        retrieval_build_digest=_BUILD_DIGEST,
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=_FIXTURE_DIGEST,
        evaluator_digest=evaluator_digest,
        context_cohorts=(
            RetrievalCohortExpectation(
                cohort_id="context.correctness.cohort.v1",
                cohort_digest=_CONTEXT_COHORT_DIGEST,
            ),
        ),
        fuzzy_core_cohorts=(
            RetrievalCohortExpectation(
                cohort_id="fuzzy.core.correctness.cohort.v1",
                cohort_digest=_FUZZY_CORE_COHORT_DIGEST,
            ),
        ),
        fts5_trigram=RetrievalBenchmarkExpectation(
            path="FTS5_TRIGRAM",
            contract_digest=contract_digest,
        ),
        gram_fallback=RetrievalBenchmarkExpectation(
            path="GRAM_FALLBACK",
            contract_digest=contract_digest,
        ),
    )


def _base_capability_manifest(
    *,
    fts5_benchmark: RetrievalBenchmarkEvidence | None = None,
    gram_benchmark: RetrievalBenchmarkEvidence | None = None,
    evaluator_digest: str = _EVALUATOR_DIGEST,
) -> RetrievalCapabilityManifest:
    return RetrievalCapabilityManifest(
        evidence_schema_version=RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION,
        retrieval_artifact_digest=_ARTIFACT_DIGEST,
        retrieval_build_digest=_BUILD_DIGEST,
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=_FIXTURE_DIGEST,
        evaluator_digest=evaluator_digest,
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
        context_cohorts=(
            _capability_cohort(
                "context.correctness.cohort.v1",
                _CONTEXT_COHORT_DIGEST,
            ),
        ),
        fuzzy_core_cohorts=(
            _capability_cohort(
                "fuzzy.core.correctness.cohort.v1",
                _FUZZY_CORE_COHORT_DIGEST,
            ),
        ),
        fts5_trigram_benchmark=fts5_benchmark,
        gram_fallback_benchmark=gram_benchmark,
    )


def _capability_publisher(
    expectation: RetrievalCapabilityExpectation | None = None,
) -> RetrievalCapabilityPublisher:
    return RetrievalCapabilityPublisher(
        RetrievalCapabilityEvaluator(
            expectation if expectation is not None else _capability_expectation()
        ),
        initial_manifest=_base_capability_manifest(),
        evaluated_at_utc=_EVALUATED_AT,
    )


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
        proof_query_version=CANDIDATE_PROOF_QUERY_VERSION,
        implementation_fingerprint=_IMPLEMENTATION_FINGERPRINT,
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
            proof_query_version=CANDIDATE_PROOF_QUERY_VERSION,
            implementation_fingerprint=_IMPLEMENTATION_FINGERPRINT,
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
        proof_query_version=CANDIDATE_PROOF_QUERY_VERSION,
        implementation_fingerprint=_IMPLEMENTATION_FINGERPRINT,
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
        proof_query_version=CANDIDATE_PROOF_QUERY_VERSION,
        implementation_fingerprint=_IMPLEMENTATION_FINGERPRINT,
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

    def test_combine_accepts_resolved_equivalent_local_paths(self) -> None:
        fts5_run, fts5_oracle = _fts5_fixture(missing_top10_queries=0)
        fallback_run, fallback_oracle = _fallback_fixture(
            missing_top10_queries=0
        )
        resolved_fts5_run = QueryProcessRunResult(
            process_evidence=fts5_run.process_evidence,
            evidence=fts5_run.evidence,
            query_child_pid=fts5_run.query_child_pid,
            run_root=str(Path(fts5_run.run_root).resolve()),
            fixture_path=str(Path(fts5_run.fixture_path).resolve()),
            artifact_pre=fts5_run.artifact_pre,
            artifact_post=fts5_run.artifact_post,
            request_protocol_digest=fts5_run.request_protocol_digest,
        )
        bundle = combine_benchmark_evidence(
            resolved_fts5_run,
            fallback_run,
            fts5_oracle,
            fallback_oracle,
        )
        self.assertTrue(bundle.fts5.report.passed)

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
    def test_bundle_binds_current_proof_and_implementation_without_paths(
        self,
    ) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        self.assertEqual(
            bundle.proof_query_version,
            CANDIDATE_PROOF_QUERY_VERSION,
        )
        self.assertEqual(
            bundle.implementation_fingerprint,
            benchmark_implementation_fingerprint(_ROOT),
        )
        payload = benchmark_evidence_bundle_to_payload(bundle)
        self.assertEqual(
            payload["implementation_fingerprint"],
            bundle.implementation_fingerprint,
        )
        encoded = benchmark_evidence_bundle_to_json(bundle)
        for relative in BENCHMARK_IMPLEMENTATION_SOURCE_PATHS:
            self.assertNotIn(relative, encoded)

    def test_historical_bundle_or_proof_version_is_not_current(self) -> None:
        payload = benchmark_evidence_bundle_to_payload(_combined_bundle())
        payload["schema_version"] = "tm-benchmark-bundle-v1"
        with self.assertRaisesRegex(ValueError, "schema version"):
            benchmark_evidence_bundle_from_payload(payload)
        payload = benchmark_evidence_bundle_to_payload(_combined_bundle())
        payload["proof_query_version"] = "proof-query-v2"
        with self.assertRaisesRegex(ValueError, "proof query version"):
            benchmark_evidence_bundle_from_payload(payload)

    def test_top_level_cannot_rewrap_nested_source_binding_drift(self) -> None:
        for nested_name in ("process_facts", "query_facts"):
            with self.subTest(nested_name=nested_name):
                payload = benchmark_evidence_bundle_to_payload(
                    _combined_bundle(fts5_missing=0, fallback_missing=0)
                )
                nested = _nested(payload, "fts5", nested_name)
                nested["implementation_fingerprint"] = _digest("0")
                with self.assertRaisesRegex(
                    ValueError,
                    "implementation fingerprint",
                ):
                    benchmark_evidence_bundle_from_payload(payload)

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


class GateDRunnerTests(unittest.TestCase):
    def test_runner_rejects_implementation_change_before_publication(
        self,
    ) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        ports, _ = _runner_ports()
        before = _digest("a")
        after = _digest("b")
        with mock.patch.object(
            tm_benchmark_gate,
            "benchmark_implementation_fingerprint",
            side_effect=(before, before, after),
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                _run_benchmark_gate_d_test(
                    _ROOT / "benchmark_tm_contract.json",
                    work_root,
                    evidence_path,
                    ports=ports,
                    test_record_count=_TEST_RECORD_COUNT,
                    test_seed=0,
                )
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.IMPLEMENTATION_CHANGED",
        )
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    """Owner-driven runner: fixed pipeline, durable readback, fail-closed."""

    def _fresh_run_env(
        self,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        work_root = root / "work"
        work_root.mkdir()
        evidence_dir = root / "evidence"
        evidence_dir.mkdir()
        evidence_path = evidence_dir / "bundle.json"
        return temp, work_root, evidence_path

    def _run(
        self,
        work_root: Path,
        evidence_path: Path,
        *,
        fts5_missing: int = 0,
        fallback_missing: int = 0,
        combine_side_effect: Any = None,
    ) -> tuple[BenchmarkGateDRunResult, dict[str, list[object]]]:
        ports, record = _runner_ports(
            fts5_missing=fts5_missing,
            fallback_missing=fallback_missing,
            combine_side_effect=combine_side_effect,
        )
        result = _run_benchmark_gate_d_test(
            _ROOT / "benchmark_tm_contract.json",
            work_root,
            evidence_path,
            ports=ports,
            test_record_count=_TEST_RECORD_COUNT,
            test_seed=0,
        )
        return result, record

    def _assert_clean_work_root(self, work_root: Path) -> None:
        self.assertEqual(os.listdir(work_root), [])

    def test_public_entry_locks_real_defaults(self) -> None:
        parameters = inspect.signature(run_benchmark_gate_d).parameters
        self.assertEqual(
            tuple(parameters),
            ("contract_path", "work_root", "evidence_path"),
        )

    def test_private_seam_refuses_default_ports(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        with self.assertRaises(BenchmarkGateDError) as ctx:
            _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=_DEFAULT_GATE_D_RUNNER_PORTS,
                test_record_count=_TEST_RECORD_COUNT,
            )
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.PORT_INJECTION_REQUIRED",
        )
        self._assert_clean_work_root(work_root)

    def test_runner_invokes_oracle_then_ports_in_fixed_order_once_each(
        self,
    ) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        result, record = self._run(work_root, evidence_path)
        self.assertIsInstance(result, BenchmarkGateDRunResult)
        self.assertTrue(result.test_mode)
        self.assertEqual(
            record["calls"],
            [
                ("oracle",),
                ("migration", _FTS5),
                ("migration", _FALLBACK),
                ("query", _FTS5),
                ("query", _FALLBACK),
                ("combine",),
            ],
        )

    def _assert_runner_blocked_by_oracle(
        self,
        *,
        fts5_missing: int,
        fallback_missing: int,
        error_code: str,
    ) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        ports, record = _runner_ports(
            fts5_missing=fts5_missing,
            fallback_missing=fallback_missing,
        )
        with self.assertRaises(BenchmarkGateDError) as ctx:
            _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=ports,
                test_record_count=_TEST_RECORD_COUNT,
                test_seed=0,
            )
        self.assertEqual(ctx.exception.error_code, error_code)
        self.assertEqual(record["calls"], [("oracle",)])
        self.assertEqual(record["migration_roots"], [])
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    def test_runner_oracle_miss_blocks_100k_before_any_migration(
        self,
    ) -> None:
        self._assert_runner_blocked_by_oracle(
            fts5_missing=1,
            fallback_missing=0,
            error_code="GATE_D.ORACLE_FTS5_MISS_BLOCKS_100K",
        )

    def test_runner_fallback_oracle_miss_blocks_100k_without_masking(
        self,
    ) -> None:
        self._assert_runner_blocked_by_oracle(
            fts5_missing=0,
            fallback_missing=1,
            error_code="GATE_D.ORACLE_FALLBACK_MISS_BLOCKS_100K",
        )

    def test_runner_both_oracle_misses_block_100k_on_first_path(
        self,
    ) -> None:
        self._assert_runner_blocked_by_oracle(
            fts5_missing=1,
            fallback_missing=1,
            error_code="GATE_D.ORACLE_FTS5_MISS_BLOCKS_100K",
        )

    def test_runner_uses_distinct_dedicated_roots_then_cleans_work_root(
        self,
    ) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        result, record = self._run(work_root, evidence_path)
        migration_roots = [Path(str(p)) for p in record["migration_roots"]]
        oracle_roots = [Path(str(p)) for p in record["oracle_roots"]]
        self.assertEqual(len(migration_roots), 2)
        self.assertEqual(len(oracle_roots), 2)
        all_roots = migration_roots + oracle_roots
        self.assertEqual(len({root.resolve() for root in all_roots}), 4)
        for root in all_roots:
            self.assertIn(work_root.resolve(), root.resolve().parents)
        self.assertNotEqual(migration_roots[0], migration_roots[1])
        self.assertNotEqual(oracle_roots[0], oracle_roots[1])
        self.assertTrue(evidence_path.is_file())
        self._assert_clean_work_root(work_root)

    def test_runner_persists_durable_readback_before_cleanup(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        ports, _ = _runner_ports()
        expected_bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        real_cleanup = tm_benchmark_gate._cleanup_private_run_dir
        observed: dict[str, object] = {}

        def observing_cleanup(
            private_dir: Path,
            private_identity: tuple[int, int],
            work_identity: tuple[int, int],
            *,
            expected_children: dict[str, tuple[int, int]],
        ) -> None:
            observed["evidence_bytes"] = evidence_path.read_bytes()
            observed["private_dir_present"] = Path(private_dir).exists()
            real_cleanup(
                private_dir,
                private_identity,
                work_identity,
                expected_children=expected_children,
            )

        with mock.patch.object(
            tm_benchmark_gate,
            "_cleanup_private_run_dir",
            side_effect=observing_cleanup,
        ):
            result = _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=ports,
                test_record_count=_TEST_RECORD_COUNT,
                test_seed=0,
            )
        payload = benchmark_evidence_bundle_to_json(
            expected_bundle
        ).encode("utf-8")
        self.assertEqual(observed["evidence_bytes"], payload)
        self.assertIs(observed["private_dir_present"], True)
        self.assertEqual(evidence_path.read_bytes(), payload)
        self.assertEqual(result.bundle, expected_bundle)
        self.assertEqual(
            result.bundle,
            benchmark_evidence_bundle_from_json(payload.decode("utf-8")),
        )
        self.assertEqual(
            result.bundle_digest,
            benchmark_evidence_bundle_digest(result.bundle),
        )
        self.assertEqual(result.artifact_size, len(payload))
        self.assertEqual(
            result.artifact_digest,
            hashlib.sha256(payload).hexdigest(),
        )
        self._assert_clean_work_root(work_root)

    def test_runner_result_and_artifact_never_leak_identifiers(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        result, _ = self._run(work_root, evidence_path)
        self.assertEqual(
            set(result.__dataclass_fields__),
            {
                "_receipt",
                "bundle",
                "bundle_digest",
                "artifact_size",
                "artifact_digest",
                "test_mode",
            },
        )
        artifact_text = evidence_path.read_text(encoding="utf-8")
        for token in (
            "child_pid",
            "run_root",
            "fixture_path",
            "worker_protocol_digest",
            "query_protocol_digest",
            "process_pair_digest",
            "inode",
            "mtime_ns",
            "sidecar_identity",
            "manifest_identity",
            "source_raw",
            "target_raw",
            "query_raw",
            "/tmp",
            "process-fts5-",
            "oracle-fts5-",
        ):
            self.assertNotIn(token, artifact_text, f"artifact leaks {token!r}")
        self._assert_clean_work_root(work_root)

    def test_runner_refuses_existing_final_and_never_overwrites(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        evidence_path.write_text("foreign-content", encoding="utf-8")
        with self.assertRaises(BenchmarkGateDError) as ctx:
            self._run(work_root, evidence_path)
        self.assertEqual(ctx.exception.error_code, "GATE_D.EVIDENCE_EXISTS")
        self.assertEqual(
            evidence_path.read_text(encoding="utf-8"),
            "foreign-content",
        )
        self._assert_clean_work_root(work_root)

    def test_runner_refuses_symlink_final_and_preserves_target(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        target = evidence_path.parent / "foreign-target.txt"
        target.write_text("target-content", encoding="utf-8")
        evidence_path.symlink_to(target)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            self._run(work_root, evidence_path)
        self.assertEqual(ctx.exception.error_code, "GATE_D.EVIDENCE_EXISTS")
        self.assertTrue(evidence_path.is_symlink())
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "target-content",
        )
        self._assert_clean_work_root(work_root)

    def test_runner_write_failure_fails_closed(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        with mock.patch("os.write", side_effect=OSError("write failed")):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._run(work_root, evidence_path)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.EVIDENCE_PUBLISH_FAILED",
        )
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    def test_runner_link_publication_failure_fails_closed(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        with mock.patch("os.link", side_effect=OSError("link failed")):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._run(work_root, evidence_path)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.EVIDENCE_PUBLISH_FAILED",
        )
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    def test_runner_link_race_never_overwrites_foreign_final(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        real_link = os.link

        def create_foreign_then_link(
            source: Any,
            destination: Any,
            *,
            follow_symlinks: bool = True,
        ) -> None:
            evidence_path.write_text("foreign-race", encoding="utf-8")
            real_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch("os.link", side_effect=create_foreign_then_link):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._run(work_root, evidence_path)
        self.assertEqual(ctx.exception.error_code, "GATE_D.EVIDENCE_EXISTS")
        self.assertEqual(
            evidence_path.read_text(encoding="utf-8"),
            "foreign-race",
        )
        self._assert_clean_work_root(work_root)

    def test_runner_fsync_failure_fails_closed(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        with mock.patch("os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._run(work_root, evidence_path)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.CLEANUP_PENDING",
        )
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    def test_runner_readback_mismatch_fails_closed(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        with mock.patch.object(
            tm_benchmark_gate,
            "_read_owned_file_bytes",
            return_value=b"corrupted-readback",
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._run(work_root, evidence_path)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.EVIDENCE_READBACK_MISMATCH",
        )
        self.assertFalse(evidence_path.exists())
        self._assert_clean_work_root(work_root)

    def test_published_final_cleanup_failure_is_explicit(self) -> None:
        bundle = _combined_bundle()
        with tempfile.TemporaryDirectory() as temporary:
            evidence_path = Path(temporary) / "evidence.json"
            original_unlink = os.unlink

            def fail_final_unlink(path: object, *args: object, **kwargs: object) -> None:
                if Path(cast(Any, path)) == evidence_path:
                    raise OSError("final unlink failed")
                original_unlink(cast(Any, path), *args, **kwargs)

            with (
                mock.patch.object(
                    tm_benchmark_gate,
                    "benchmark_implementation_fingerprint",
                    return_value=_digest("0"),
                ),
                mock.patch("os.unlink", side_effect=fail_final_unlink),
            ):
                with self.assertRaises(BenchmarkGateDError) as ctx:
                    tm_benchmark_gate._publish_evidence_bundle(
                        bundle,
                        evidence_path,
                    )
            self.assertEqual(ctx.exception.error_code, "GATE_D.CLEANUP_PENDING")
            self.assertTrue(evidence_path.is_file())

    def test_runner_cleanup_refuses_foreign_root_replacement(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()

        def sabotage(record: dict[str, list[object]]) -> None:
            root = Path(str(record["migration_roots"][0]))
            os.rmdir(root)
            os.mkdir(root)
            (root / "foreign-marker.txt").write_text(
                "foreign-content",
                encoding="utf-8",
            )

        ports, record = _runner_ports(combine_side_effect=sabotage)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=ports,
                test_record_count=_TEST_RECORD_COUNT,
                test_seed=0,
            )
        self.assertEqual(ctx.exception.error_code, "GATE_D.CLEANUP_PENDING")
        self.assertTrue(evidence_path.is_file())
        replaced = Path(str(record["migration_roots"][0]))
        self.assertTrue(replaced.is_dir())
        self.assertEqual(
            (replaced / "foreign-marker.txt").read_text(encoding="utf-8"),
            "foreign-content",
        )
        self.assertEqual(os.listdir(work_root), [replaced.parent.name])

    def test_runner_cleanup_refuses_symlink_root_replacement(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        target_dir = work_root.parent / "foreign-target-dir"
        target_dir.mkdir()
        (target_dir / "marker.txt").write_text("foreign", encoding="utf-8")

        def sabotage(record: dict[str, list[object]]) -> None:
            root = Path(str(record["oracle_roots"][1]))
            os.rmdir(root)
            os.symlink(target_dir, root)

        ports, record = _runner_ports(combine_side_effect=sabotage)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=ports,
                test_record_count=_TEST_RECORD_COUNT,
                test_seed=0,
            )
        self.assertEqual(ctx.exception.error_code, "GATE_D.CLEANUP_PENDING")
        self.assertTrue(evidence_path.is_file())
        replaced = Path(str(record["oracle_roots"][1]))
        self.assertTrue(replaced.is_symlink())
        self.assertEqual(
            (target_dir / "marker.txt").read_text(encoding="utf-8"),
            "foreign",
        )

    def test_runner_cleanup_refuses_extra_foreign_child(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()

        def sabotage(record: dict[str, list[object]]) -> None:
            private_dir = Path(str(record["migration_roots"][0])).parent
            (private_dir / "foreign-extra.txt").write_text(
                "foreign",
                encoding="utf-8",
            )

        ports, record = _runner_ports(combine_side_effect=sabotage)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            _run_benchmark_gate_d_test(
                _ROOT / "benchmark_tm_contract.json",
                work_root,
                evidence_path,
                ports=ports,
                test_record_count=_TEST_RECORD_COUNT,
                test_seed=0,
            )
        self.assertEqual(ctx.exception.error_code, "GATE_D.CLEANUP_PENDING")
        self.assertTrue(evidence_path.is_file())
        private_dir = Path(str(record["migration_roots"][0])).parent
        extra = private_dir / "foreign-extra.txt"
        self.assertTrue(extra.is_file())
        self.assertEqual(extra.read_text(encoding="utf-8"), "foreign")

    def test_runner_rejects_evidence_inside_run_subtree(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        private_dir = work_root / "private"
        private_dir.mkdir()
        for inside in (
            private_dir / "bundle.json",
            private_dir / "nested" / "bundle.json",
        ):
            inside.parent.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(BenchmarkGateDError) as ctx:
                tm_benchmark_gate._require_outside_run_subtree(
                    private_dir,
                    inside,
                )
            self.assertEqual(
                ctx.exception.error_code,
                "GATE_D.EVIDENCE_PATH_INVALID",
            )
        outside = work_root.parent / "evidence-outside" / "bundle.json"
        outside.parent.mkdir()
        tm_benchmark_gate._require_outside_run_subtree(private_dir, outside)

    def test_runner_allows_evidence_under_caller_work_root(self) -> None:
        temp, work_root, _ = self._fresh_run_env()
        inside = work_root / "bundle.json"
        result, _ = self._run(work_root, inside)
        self.assertTrue(inside.is_file())
        self.assertEqual(
            result.bundle_digest,
            benchmark_evidence_bundle_digest(result.bundle),
        )
        # only the private run tree is cleaned; caller-owned evidence stays
        self.assertEqual(os.listdir(work_root), ["bundle.json"])

    def test_runner_rejects_nonempty_work_root(self) -> None:
        temp, work_root, evidence_path = self._fresh_run_env()
        (work_root / "existing.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(BenchmarkGateDError) as ctx:
            self._run(work_root, evidence_path)
        self.assertEqual(ctx.exception.error_code, "GATE_D.WORK_ROOT_INVALID")
        self.assertEqual(os.listdir(work_root), ["existing.txt"])
        self.assertFalse(evidence_path.exists())


class GateDPublicationTests(unittest.TestCase):
    """Gate D capability publication: compose, refresh once, verify truth."""

    @staticmethod
    def _run_result(
        bundle: BenchmarkEvidenceBundle,
        *,
        test_mode: bool = False,
    ) -> BenchmarkGateDRunResult:
        artifact_bytes = benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        return tm_benchmark_gate._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact_bytes),
            artifact_digest=hashlib.sha256(artifact_bytes).hexdigest(),
            test_mode=test_mode,
        )

    def _publish(
        self,
        manifest: RetrievalCapabilityManifest,
        bundle: BenchmarkEvidenceBundle,
        publisher: RetrievalCapabilityPublisher,
    ) -> RetrievalCapabilityPublicationResult:
        return publish_retrieval_capability_gate_d(
            manifest,
            self._run_result(bundle),
            publisher,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def test_publication_preserves_gate_c_and_closes_both_failed_paths(
        self,
    ) -> None:
        bundle = _combined_bundle()
        self.assertEqual(bundle.fts5.report.failed_gates, ("CANDIDATE_RECALL",))
        self.assertEqual(
            bundle.fallback.report.failed_gates,
            ("CANDIDATE_RECALL",),
        )
        base = _base_capability_manifest()
        publisher = _capability_publisher()
        result = self._publish(base, bundle, publisher)
        self.assertIsInstance(result, RetrievalCapabilityPublicationResult)
        manifest = result.manifest
        for field in (
            "evidence_schema_version",
            "retrieval_artifact_digest",
            "retrieval_build_digest",
            "semantics_version",
            "fixture_digest",
            "evaluator_digest",
            "generated_at_utc",
            "valid_until_utc",
        ):
            self.assertEqual(
                getattr(manifest, field),
                getattr(base, field),
                field,
            )
        self.assertEqual(manifest.context_cohorts, base.context_cohorts)
        self.assertEqual(manifest.fuzzy_core_cohorts, base.fuzzy_core_cohorts)
        self.assertIsNone(base.fts5_trigram_benchmark)
        self.assertIsNone(base.gram_fallback_benchmark)
        fts5_expected, fallback_expected = retrieval_benchmark_evidence_pair(
            bundle,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
        )
        fts5_evidence = manifest.fts5_trigram_benchmark
        fallback_evidence = manifest.gram_fallback_benchmark
        assert fts5_evidence is not None
        assert fallback_evidence is not None
        self.assertEqual(fts5_evidence, fts5_expected)
        self.assertEqual(fallback_evidence, fallback_expected)
        self.assertEqual(fts5_evidence.report.execution_path, _FTS5)
        self.assertEqual(fallback_evidence.report.execution_path, _FALLBACK)
        snapshot = result.snapshot
        self.assertIs(snapshot, publisher.snapshot())
        self.assertTrue(snapshot.context.available)
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
        )
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
        )
        self.assertEqual(
            snapshot.summary.unavailable_codes,
            (RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,),
        )

    def test_publication_isolates_one_path_failure(self) -> None:
        bundle = _combined_bundle(fts5_missing=27, fallback_missing=0)
        result = self._publish(
            _base_capability_manifest(),
            bundle,
            _capability_publisher(),
        )
        self.assertFalse(result.snapshot.fts5_trigram.available)
        self.assertEqual(
            result.snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
        )
        self.assertTrue(result.snapshot.gram_fallback.available)
        self.assertIsNone(result.snapshot.gram_fallback.unavailable_code)
        self.assertTrue(result.snapshot.context.available)
        self.assertTrue(result.snapshot.fuzzy_core.available)

    def test_publication_full_pass_opens_both_paths(self) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        result = self._publish(
            _base_capability_manifest(),
            bundle,
            _capability_publisher(),
        )
        self.assertTrue(result.snapshot.fts5_trigram.available)
        self.assertIsNone(result.snapshot.fts5_trigram.unavailable_code)
        self.assertTrue(result.snapshot.gram_fallback.available)
        self.assertIsNone(result.snapshot.gram_fallback.unavailable_code)

    def test_publication_fails_closed_on_decision_mismatch(self) -> None:
        mismatched = _capability_expectation(
            evaluator_digest=hashlib.sha256(b"other-evaluator").hexdigest()
        )
        publisher = _capability_publisher(mismatched)
        initial = publisher.snapshot()
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            self._publish(_base_capability_manifest(), bundle, publisher)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.PUBLICATION_DECISION_MISMATCH",
        )
        self.assertIs(publisher.snapshot(), initial)

    def test_expired_gate_c_base_never_commits_gate_d_candidate(self) -> None:
        base = _base_capability_manifest()
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(_capability_expectation()),
            initial_manifest=base,
            evaluated_at_utc=_EVALUATED_AT,
        )
        initial = publisher.snapshot()
        self.assertTrue(initial.context.available)
        self.assertTrue(initial.fuzzy_core.available)
        expired = datetime(2026, 8, 15, tzinfo=timezone.utc)

        with self.assertRaises(BenchmarkGateDError):
            publish_retrieval_capability_gate_d(
                base,
                self._run_result(_combined_bundle()),
                publisher,
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=expired,
            )

        self.assertIs(publisher.snapshot(), initial)

    def test_concurrent_publisher_change_aborts_before_gate_d_commit(
        self,
    ) -> None:
        base = _base_capability_manifest()
        publisher = _capability_publisher()
        fingerprint = benchmark_implementation_fingerprint()
        concurrent: list[RetrievalCapabilitySnapshot] = []
        calls: list[None] = []

        def observed_fingerprint() -> str:
            if concurrent:
                raise AssertionError("fingerprint called more than twice")
            if not calls:
                calls.append(None)
                return fingerprint
            concurrent.append(
                publisher.refresh(base, evaluated_at_utc=_EVALUATED_AT)
            )
            return fingerprint

        with mock.patch.object(
            tm_benchmark_gate,
            "benchmark_implementation_fingerprint",
            side_effect=observed_fingerprint,
        ):
            with self.assertRaises(BenchmarkGateDError):
                self._publish(
                    base,
                    _combined_bundle(fts5_missing=0, fallback_missing=0),
                    publisher,
                )

        self.assertEqual(len(concurrent), 1)
        self.assertIs(publisher.snapshot(), concurrent[0])

    def test_publication_validator_failure_never_mutates_publisher(self) -> None:
        publisher = _capability_publisher()
        initial = publisher.snapshot()
        failure = BenchmarkGateDError(
            "GATE_D.PUBLICATION_DECISION_MISMATCH"
        )

        with mock.patch.object(
            tm_benchmark_gate,
            "_verify_path_decisions_match_reports",
            side_effect=failure,
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._publish(
                    _base_capability_manifest(),
                    _combined_bundle(fts5_missing=0, fallback_missing=0),
                    publisher,
                )

        self.assertIs(ctx.exception, failure)
        self.assertIs(publisher.snapshot(), initial)

    def test_publication_result_constructor_failure_never_mutates_publisher(
        self,
    ) -> None:
        publisher = _capability_publisher()
        initial = publisher.snapshot()
        failure = AssertionError("publication result constructor failed")

        with mock.patch.object(
            tm_benchmark_gate,
            "RetrievalCapabilityPublicationResult",
            side_effect=failure,
        ):
            with self.assertRaises(AssertionError) as ctx:
                self._publish(
                    _base_capability_manifest(),
                    _combined_bundle(fts5_missing=0, fallback_missing=0),
                    publisher,
                )

        self.assertIs(ctx.exception, failure)
        self.assertIs(publisher.snapshot(), initial)

    def test_prepared_publication_commits_through_captured_slot_descriptor(
        self,
    ) -> None:
        base = _base_capability_manifest()
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        publisher = _capability_publisher()
        initial = publisher.snapshot()
        publisher_type = type(publisher)
        slot_name = "_RetrievalCapabilityPublisher__snapshot"
        original_descriptor = publisher_type.__dict__[slot_name]
        foreign_sets: list[object] = []

        class FailingSnapshotDescriptor:
            def __get__(self, instance: object, owner: object) -> object:
                return original_descriptor.__get__(instance, owner)

            def __set__(self, instance: object, value: object) -> None:
                del instance
                foreign_sets.append(value)
                raise AssertionError(
                    "late snapshot descriptor ran after Core prepare"
                )

        def prepare_publication(
            result: RetrievalCapabilityPublicationResult,
        ) -> RetrievalCapabilityPublicationResult:
            setattr(
                publisher_type,
                slot_name,
                FailingSnapshotDescriptor(),
            )
            return result

        try:
            result = (
                tm_benchmark_gate
                ._publish_retrieval_capability_gate_d_prepared(
                    base,
                    self._run_result(bundle),
                    publisher,
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                    prepare_publication=prepare_publication,
                    _publication_bindings=(
                        tm_benchmark_gate
                        ._publication_bindings_from_current_globals()
                    ),
                )
            )
        finally:
            setattr(publisher_type, slot_name, original_descriptor)

        self.assertEqual(foreign_sets, [])
        self.assertIs(result.snapshot, publisher.snapshot())
        self.assertIsNot(result.snapshot, initial)
        self.assertTrue(result.snapshot.fts5_trigram.available)
        self.assertTrue(result.snapshot.gram_fallback.available)

    def test_publication_refreshes_exactly_once_and_returns_immutable_result(
        self,
    ) -> None:
        source = (_ROOT / "tm_benchmark_gate.py").read_text(encoding="utf-8")
        publication_source = inspect.getsource(
            tm_benchmark_gate._publish_retrieval_capability_gate_d_prepared
        )
        self.assertEqual(
            publication_source.count("publication_result = validated_transition("),
            1,
        )
        self.assertNotIn(
            "_validated_refresh_retrieval_capability(",
            publication_source,
        )
        self.assertNotIn("publisher.refresh(", publication_source)
        bundle = _combined_bundle()
        publisher = _capability_publisher()
        result = self._publish(_base_capability_manifest(), bundle, publisher)
        self.assertIs(result.snapshot, publisher.snapshot())
        self.assertEqual(
            result.snapshot.summary.evaluated_at_utc,
            _EVALUATED_AT,
        )
        fresh = RetrievalCapabilityEvaluator(_capability_expectation()).evaluate(
            result.manifest,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertEqual(
            result.snapshot.summary.evidence_digest,
            fresh.summary.evidence_digest,
        )
        self.assertEqual(
            result.snapshot.summary.unavailable_codes,
            fresh.summary.unavailable_codes,
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(result.manifest, "semantics_version", "mutated")
        with self.assertRaises(FrozenInstanceError):
            setattr(result.snapshot, "semantics_version", "mutated")

    def test_publication_requires_exact_types_and_valid_instants(self) -> None:
        bundle = _combined_bundle()
        manifest = _base_capability_manifest()
        publisher = _capability_publisher()
        run_result = self._run_result(bundle)
        for base, result, pub, code in (
            (None, run_result, publisher, "GATE_D.MANIFEST_INVALID"),
            (manifest, None, publisher, "GATE_D.RUN_RESULT_INVALID"),
            (manifest, run_result, None, "GATE_D.PUBLISHER_INVALID"),
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                publish_retrieval_capability_gate_d(
                    cast(Any, base),
                    cast(Any, result),
                    cast(Any, pub),
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
            self.assertEqual(ctx.exception.error_code, code)
        with self.assertRaises(BenchmarkGateDError) as ctx:
            publish_retrieval_capability_gate_d(
                manifest,
                run_result,
                publisher,
                generated_at_utc=_VALID_UNTIL,
                valid_until_utc=_GENERATED_AT,
                evaluated_at_utc=_EVALUATED_AT,
            )
        self.assertEqual(ctx.exception.error_code, "GATE_D.INSTANTS_INVALID")
        for generated, valid_until, evaluated in (
            ("not-a-timestamp", _VALID_UNTIL, _EVALUATED_AT),
            (_GENERATED_AT, "not-a-timestamp", _EVALUATED_AT),
            (_GENERATED_AT, _VALID_UNTIL, datetime(2026, 8, 13, 12, 0, 0)),
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                publish_retrieval_capability_gate_d(
                    manifest,
                    run_result,
                    publisher,
                    generated_at_utc=cast(str, generated),
                    valid_until_utc=cast(str, valid_until),
                    evaluated_at_utc=cast(datetime, evaluated),
                )
            self.assertEqual(
                ctx.exception.error_code,
                "GATE_D.PUBLICATION_FAILED",
            )

    def test_publication_rejects_test_runner_result(self) -> None:
        bundle = _combined_bundle()
        with self.assertRaises(BenchmarkGateDError) as ctx:
            publish_retrieval_capability_gate_d(
                _base_capability_manifest(),
                self._run_result(bundle, test_mode=True),
                _capability_publisher(),
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.TEST_EVIDENCE_FORBIDDEN",
        )

    def test_publication_rejects_source_drift_before_refresh(self) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        publisher = _capability_publisher()
        initial = publisher.snapshot()
        with mock.patch.object(
            tm_benchmark_gate,
            "benchmark_implementation_fingerprint",
            return_value=_digest("0"),
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._publish(_base_capability_manifest(), bundle, publisher)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.IMPLEMENTATION_CHANGED",
        )
        self.assertIs(publisher.snapshot(), initial)

    def test_publication_rejects_drift_after_manifest_before_refresh(
        self,
    ) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        publisher = _capability_publisher()
        initial = publisher.snapshot()
        with mock.patch.object(
            tm_benchmark_gate,
            "benchmark_implementation_fingerprint",
            side_effect=(bundle.implementation_fingerprint, _digest("0")),
        ):
            with self.assertRaises(BenchmarkGateDError) as ctx:
                self._publish(_base_capability_manifest(), bundle, publisher)
        self.assertEqual(
            ctx.exception.error_code,
            "GATE_D.IMPLEMENTATION_CHANGED",
        )
        self.assertIs(publisher.snapshot(), initial)

    def test_public_run_result_constructor_cannot_mint_final_receipt(
        self,
    ) -> None:
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        artifact = benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        with self.assertRaises(TypeError):
            cast(Any, BenchmarkGateDRunResult)(
                bundle=bundle,
                bundle_digest=bundle.bundle_digest,
                artifact_size=len(artifact),
                artifact_digest=hashlib.sha256(artifact).hexdigest(),
                test_mode=False,
            )


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
        self.assertNotIn("RetrievalCapabilityEvaluator", source)

    def test_gate_module_publication_boundaries(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent / "tm_benchmark_gate.py"
        ).read_text(encoding="utf-8")
        # Task 8.5 publication requires the owner to import and use the
        # exact Manifest/Publisher/Snapshot types and to compose exactly one
        # new manifest through exactly one validated Core transition.
        self.assertIn("RetrievalCapabilityManifest", source)
        self.assertIn("RetrievalCapabilityPublisher", source)
        self.assertIn("RetrievalCapabilitySnapshot", source)
        self.assertIn("RetrievalCapabilityManifest,", source)
        self.assertIn("new_manifest = manifest_type(", source)
        self.assertEqual(
            source.count("publication_result = validated_transition("),
            1,
        )
        self.assertNotIn("publisher.refresh(", source)
        # The owner must never import or construct the evaluator, create a
        # publisher/evaluator expectation or snapshot itself, or bypass the
        # publisher to grant availability.
        for token in (
            "RetrievalCapabilityEvaluator",
            "RetrievalCapabilityPublisher(",
            "default_retrieval_capability_publisher",
            "RetrievalCapabilityExpectation",
            "RetrievalCapabilitySnapshot(",
            "RetrievalFuzzyPathDecision(",
            "_closed_snapshot(",
            "_snapshot_from_decisions(",
            "evaluate(",
            "bypass(",
        ):
            self.assertNotIn(
                token,
                source,
                f"gate module must not use {token!r}",
            )

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
