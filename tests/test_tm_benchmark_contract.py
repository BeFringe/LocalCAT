from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from typing import cast

from tm_contracts import (
    BENCHMARK_CONTRACT_VERSION,
    BENCHMARK_SUITE_VERSION,
    CANDIDATE_BUDGET_VERSION,
    BenchmarkContract,
    BenchmarkExecutionPath,
    BenchmarkReport,
    BenchmarkSuiteContract,
    BenchmarkSuiteReport,
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    ResourceQueryMetadata,
    benchmark_contract_digest,
    benchmark_environment_digest,
    benchmark_suite_contract_digest,
    candidate_budget_v1,
    contract_from_json,
    contract_to_json,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64
_DIGEST_E = "e" * 64
_DIGEST_F = "f" * 64
_DIGEST_0 = "0" * 64
_DIGEST_1 = "1" * 64


def _stage(
    stage: CandidateStage,
    input_count: int,
    added_unique_count: int,
    output_unique_count: int,
    dropped_count: int = 0,
) -> CandidateStageMetadata:
    return CandidateStageMetadata(
        stage=stage,
        input_count=input_count,
        added_unique_count=added_unique_count,
        output_unique_count=output_unique_count,
        dropped_count=dropped_count,
    )


def _recall(
    *,
    fuzzy_available: bool = True,
    stages: tuple[CandidateStageMetadata, ...] | None = None,
    union_unique_count: int = 3,
    deduplicated_count: int = 2,
    truncated: bool = False,
) -> CandidateRecallMetadata:
    if stages is None:
        stages = (
            _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 3),
            _stage(CandidateStage.UNION, 3, 0, 3),
            _stage(CandidateStage.DEDUPLICATE, 3, 0, 2, 1),
        )
    return CandidateRecallMetadata(
        resource_id="tm.primary",
        index_kind="FTS5_TRIGRAM",
        fuzzy_available=fuzzy_available,
        fuzzy_unavailable_code=(
            None if fuzzy_available else "FUZZY.CAPABILITY_UNAVAILABLE"
        ),
        stages=stages,
        union_unique_count=union_unique_count,
        deduplicated_count=deduplicated_count,
        result_limit=10,
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        candidate_budget=2048,
        truncated=truncated,
    )


def _candidate(
    record_id: int,
    *,
    pretruncate_rank: int | None = None,
) -> CandidateEvidence:
    return CandidateEvidence(
        record_id=record_id,
        recall_stages=(CandidateStage.FTS_TRIGRAM,),
        matched_grams=2,
        query_grams=3,
        overlap_ratio=2 / 3,
        pretruncate_rank=pretruncate_rank,
    )


def _benchmark_contract() -> BenchmarkContract:
    return BenchmarkContract(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        corpus_generator_version="tm-benchmark-corpus-v1",
        corpus_seed=20260729,
        corpus_record_count=100_000,
        corpus_digest=_DIGEST_A,
        corpus_composition_version="tm-corpus-composition-v1",
        corpus_composition_digest=_DIGEST_1,
        exact_cohort_digest=_DIGEST_B,
        exact_min_samples=1_000,
        exact_cohort_count=1_200,
        fuzzy_cohort_digest=_DIGEST_C,
        fuzzy_min_samples=200,
        fuzzy_cohort_count=240,
        oracle_subset_digest=_DIGEST_D,
        oracle_subset_record_count=5_000,
        oracle_query_count=200,
        top_k=10,
        minimum_similarity=0.60,
        warmup_queries_per_cohort=100,
        measured_repeats=1,
        percentile_method="nearest-rank",
        rss_scope="child-process-lifetime-v1",
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        scorer_config_digest=_DIGEST_E,
        fast_path_config_digest=_DIGEST_F,
        fallback_path_config_digest=_DIGEST_0,
        exact_p95_gate_ms=50.0,
        fuzzy_p95_gate_ms=500.0,
        migration_gate_seconds=120.0,
        peak_rss_gate_mib=512.0,
        candidate_recall_gate=1.0,
    )


def _environment(
    path: BenchmarkExecutionPath = BenchmarkExecutionPath.FTS5_TRIGRAM,
) -> tuple[tuple[str, str], ...]:
    return (
        ("cpu", "test-cpu"),
        ("fts5_enabled", "true" if path is BenchmarkExecutionPath.FTS5_TRIGRAM else "false"),
        ("os", "test-os"),
        ("python_version", "3.14.0"),
        ("ram_mib", "16384"),
        ("sqlite_version", "3.51.2"),
        ("unicode_version", "16.0.0"),
    )


def _benchmark_report(
    *,
    path: BenchmarkExecutionPath = BenchmarkExecutionPath.FTS5_TRIGRAM,
    candidate_recall: float = 1.0,
    exact_p95_ms: float = 49.0,
    fuzzy_p95_ms: float = 499.0,
    migration_seconds: float = 119.0,
    peak_rss_mib: float = 511.0,
    passed: bool = True,
    failed_gates: tuple[str, ...] = (),
) -> BenchmarkReport:
    contract = _benchmark_contract()
    environment = _environment(path)
    path_config_digest = (
        contract.fast_path_config_digest
        if path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else contract.fallback_path_config_digest
    )
    return BenchmarkReport(
        contract=contract,
        contract_digest=benchmark_contract_digest(contract),
        corpus_digest=contract.corpus_digest,
        corpus_composition_version=contract.corpus_composition_version,
        corpus_composition_digest=contract.corpus_composition_digest,
        exact_cohort_digest=contract.exact_cohort_digest,
        fuzzy_cohort_digest=contract.fuzzy_cohort_digest,
        oracle_subset_digest=contract.oracle_subset_digest,
        scorer_config_digest=contract.scorer_config_digest,
        execution_path=path,
        path_config_digest=path_config_digest,
        exact_sample_count=contract.exact_cohort_count,
        fuzzy_sample_count=contract.fuzzy_cohort_count,
        oracle_query_count=200,
        percentile_method="nearest-rank",
        candidate_recall=candidate_recall,
        exact_p50_ms=min(25.0, exact_p95_ms),
        exact_p95_ms=exact_p95_ms,
        exact_max_ms=max(75.0, exact_p95_ms),
        fuzzy_top10_p50_ms=min(250.0, fuzzy_p95_ms),
        fuzzy_top10_p95_ms=fuzzy_p95_ms,
        fuzzy_top10_max_ms=max(750.0, fuzzy_p95_ms),
        migration_seconds=migration_seconds,
        peak_rss_mib=peak_rss_mib,
        passed=passed,
        failed_gates=failed_gates,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )


def _benchmark_suite_contract() -> BenchmarkSuiteContract:
    contract = _benchmark_contract()
    return BenchmarkSuiteContract(
        suite_version=BENCHMARK_SUITE_VERSION,
        benchmark_contract=contract,
        benchmark_contract_digest=benchmark_contract_digest(contract),
        required_paths=(
            BenchmarkExecutionPath.FTS5_TRIGRAM,
            BenchmarkExecutionPath.GRAM_FALLBACK,
        ),
    )


def _benchmark_suite_report(
    *,
    fallback_failed: bool = False,
    passed: bool | None = None,
    failed_paths: tuple[BenchmarkExecutionPath, ...] | None = None,
) -> BenchmarkSuiteReport:
    suite_contract = _benchmark_suite_contract()
    contract = suite_contract.benchmark_contract
    fast = _benchmark_report()
    fallback = _benchmark_report(
        path=BenchmarkExecutionPath.GRAM_FALLBACK,
        exact_p95_ms=50.1 if fallback_failed else 49.0,
        passed=not fallback_failed,
        failed_gates=("EXACT_P95",) if fallback_failed else (),
    )
    if passed is None:
        passed = not fallback_failed
    if failed_paths is None:
        failed_paths = (
            (BenchmarkExecutionPath.GRAM_FALLBACK,)
            if fallback_failed
            else ()
        )
    return BenchmarkSuiteReport(
        suite_contract=suite_contract,
        suite_contract_digest=benchmark_suite_contract_digest(
            suite_contract
        ),
        corpus_composition_version=contract.corpus_composition_version,
        corpus_composition_digest=contract.corpus_composition_digest,
        path_reports=(fast, fallback),
        passed=passed,
        failed_paths=failed_paths,
    )


class CandidateMetadataContractTests(unittest.TestCase):
    def test_candidate_budget_v1_is_fixed_and_bounded(self) -> None:
        self.assertEqual(candidate_budget_v1(1), 2048)
        self.assertEqual(candidate_budget_v1(16), 2048)
        self.assertEqual(candidate_budget_v1(17), 2176)
        self.assertEqual(candidate_budget_v1(64), 8192)
        self.assertEqual(candidate_budget_v1(10_000), 8192)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            candidate_budget_v1(0)
        with self.assertRaisesRegex(TypeError, "integer"):
            candidate_budget_v1(cast(int, cast(object, True)))

    def test_candidate_and_resource_metadata_round_trip_strictly(self) -> None:
        recall = _recall()
        retrieval = CandidateRetrievalReport(
            candidates=(_candidate(7), _candidate(11)),
            metadata=recall,
        )
        resource = ResourceQueryMetadata(
            resource_id="tm.primary",
            context_available=False,
            context_unavailable_code="CONTEXT.NOT_INDEXED",
            recall=recall,
            scored_count=2,
            returned_count=1,
        )

        for contract in (
            recall.stages[0],
            recall,
            retrieval.candidates[0],
            retrieval,
            resource,
        ):
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                decoded = contract_from_json(encoded)
                self.assertEqual(decoded, contract)
                self.assertEqual(contract_to_json(decoded), encoded)
                envelope = json.loads(encoded)
                envelope["payload"]["caller_validated"] = True
                with self.assertRaisesRegex(ValueError, "unexpected fields"):
                    contract_from_json(
                        json.dumps(envelope, separators=(",", ":"), sort_keys=True)
                    )

        with self.assertRaises(FrozenInstanceError):
            setattr(recall, "candidate_budget", 1)

    def test_stage_order_and_all_counts_must_conserve_continuously(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 0"):
            _stage(CandidateStage.FTS_TRIGRAM, -1, 1, 0)
        with self.assertRaisesRegex(ValueError, "conserve"):
            _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 2)
        with self.assertRaisesRegex(ValueError, "source stage"):
            _recall(
                stages=(
                    _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 3),
                    _stage(CandidateStage.GRAM_2, 3, 0, 2, 1),
                    _stage(CandidateStage.UNION, 2, 0, 2),
                    _stage(CandidateStage.DEDUPLICATE, 2, 0, 2),
                ),
                union_unique_count=2,
                deduplicated_count=2,
            )
        with self.assertRaisesRegex(ValueError, "UNION"):
            _recall(
                stages=(
                    _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 3),
                    _stage(CandidateStage.UNION, 3, 0, 2, 1),
                    _stage(CandidateStage.DEDUPLICATE, 2, 0, 2),
                ),
                union_unique_count=2,
                deduplicated_count=2,
            )

        with self.assertRaisesRegex(ValueError, "continuous"):
            _recall(
                stages=(
                    _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 3),
                    _stage(CandidateStage.UNION, 2, 0, 2),
                    _stage(CandidateStage.DEDUPLICATE, 2, 0, 2),
                ),
                union_unique_count=2,
                deduplicated_count=2,
            )
        with self.assertRaisesRegex(ValueError, "stage order"):
            _recall(
                stages=(
                    _stage(CandidateStage.UNION, 0, 0, 0),
                    _stage(CandidateStage.FTS_TRIGRAM, 0, 3, 3),
                    _stage(CandidateStage.DEDUPLICATE, 3, 0, 3),
                ),
                union_unique_count=0,
                deduplicated_count=3,
            )

    def test_union_deduplicate_truncate_and_budget_must_close(self) -> None:
        with self.assertRaisesRegex(ValueError, "UNION"):
            _recall(union_unique_count=4)
        with self.assertRaisesRegex(ValueError, "DEDUPLICATE"):
            _recall(deduplicated_count=3)
        with self.assertRaisesRegex(ValueError, "candidate budget"):
            replace(_recall(), candidate_budget=1024)
        with self.assertRaisesRegex(ValueError, "TRUNCATE"):
            _recall(truncated=True)
        with self.assertRaisesRegex(ValueError, "truncated"):
            _recall(
                stages=(
                    _stage(CandidateStage.FTS_TRIGRAM, 0, 2050, 2050),
                    _stage(CandidateStage.UNION, 2050, 0, 2050),
                    _stage(CandidateStage.DEDUPLICATE, 2050, 0, 2050),
                    _stage(CandidateStage.TRUNCATE, 2050, 0, 2048, 2),
                ),
                union_unique_count=2050,
                deduplicated_count=2050,
                truncated=False,
            )

    def test_candidate_evidence_and_final_output_must_reconcile(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            replace(_candidate(7), overlap_ratio=0.5)
        with self.assertRaisesRegex(ValueError, "recall stages"):
            replace(_candidate(7), recall_stages=(CandidateStage.UNION,))
        with self.assertRaisesRegex(ValueError, "candidate count"):
            CandidateRetrievalReport(
                candidates=(_candidate(7),),
                metadata=_recall(),
            )
        with self.assertRaisesRegex(ValueError, "unique record"):
            CandidateRetrievalReport(
                candidates=(_candidate(7), _candidate(7)),
                metadata=_recall(),
            )

    def test_fuzzy_unavailable_requires_empty_recall_and_safe_code(self) -> None:
        unavailable = _recall(
            fuzzy_available=False,
            stages=(),
            union_unique_count=0,
            deduplicated_count=0,
        )
        CandidateRetrievalReport(candidates=(), metadata=unavailable)

        with self.assertRaisesRegex(ValueError, "unavailable code"):
            replace(unavailable, fuzzy_unavailable_code=None)
        with self.assertRaisesRegex(ValueError, "empty stages"):
            replace(unavailable, stages=_recall().stages)
        with self.assertRaisesRegex(ValueError, "empty candidates"):
            CandidateRetrievalReport(
                candidates=(_candidate(7),),
                metadata=unavailable,
            )
        with self.assertRaisesRegex(ValueError, "must be empty"):
            replace(_recall(), fuzzy_unavailable_code="FUZZY.DISABLED")

    def test_scored_and_returned_counts_cannot_claim_unrecalled_work(self) -> None:
        recall = _recall()
        with self.assertRaisesRegex(ValueError, "scored count"):
            ResourceQueryMetadata(
                "tm.primary",
                True,
                None,
                recall,
                3,
                1,
            )
        with self.assertRaisesRegex(ValueError, "resource id"):
            ResourceQueryMetadata(
                "tm.other",
                True,
                None,
                recall,
                2,
                1,
            )
        with self.assertRaisesRegex(ValueError, "context unavailable code"):
            ResourceQueryMetadata(
                "tm.primary",
                True,
                "CONTEXT.DISABLED",
                recall,
                2,
                1,
            )

    def test_codec_revalidates_mutated_nested_candidate_values(self) -> None:
        retrieval = CandidateRetrievalReport(
            candidates=(_candidate(7), _candidate(11)),
            metadata=_recall(),
        )
        object.__setattr__(retrieval.metadata.stages[1], "input_count", 2)
        object.__setattr__(retrieval.metadata.stages[1], "output_unique_count", 2)
        with self.assertRaisesRegex(ValueError, "continuous"):
            contract_to_json(retrieval)


class BenchmarkContractTests(unittest.TestCase):
    def test_benchmark_v1_contract_is_frozen_and_strictly_round_trips(self) -> None:
        contract = _benchmark_contract()
        encoded = contract_to_json(contract)
        self.assertEqual(contract_from_json(encoded), contract)
        self.assertEqual(contract_to_json(contract_from_json(encoded)), encoded)
        with self.assertRaises(FrozenInstanceError):
            setattr(contract, "corpus_record_count", 1)

        envelope = json.loads(encoded)
        envelope["payload"]["top_k"] = 9
        with self.assertRaisesRegex(ValueError, "top_k"):
            contract_from_json(
                json.dumps(envelope, separators=(",", ":"), sort_keys=True)
            )

    def test_benchmark_v1_rejects_missing_or_tampered_fixed_parameters(self) -> None:
        mutations = (
            ("contract version", {"contract_version": "benchmark-v2"}),
            ("corpus record count", {"corpus_record_count": 99_999}),
            ("exact minimum samples", {"exact_min_samples": 999}),
            ("exact minimum samples", {"exact_min_samples": 1_001}),
            ("fuzzy minimum samples", {"fuzzy_min_samples": 199}),
            ("fuzzy minimum samples", {"fuzzy_min_samples": 201}),
            ("exact cohort count", {"exact_cohort_count": 999}),
            ("fuzzy cohort count", {"fuzzy_cohort_count": 199}),
            ("oracle record count", {"oracle_subset_record_count": 4_999}),
            ("oracle query count", {"oracle_query_count": 199}),
            ("top_k", {"top_k": 9}),
            ("minimum similarity", {"minimum_similarity": 0.59}),
            ("warmup", {"warmup_queries_per_cohort": 99}),
            ("measured repeats", {"measured_repeats": 2}),
            ("percentile method", {"percentile_method": "linear"}),
            ("RSS scope", {"rss_scope": "parent-only"}),
            ("candidate budget version", {"candidate_budget_version": "v2"}),
            ("exact p95 gate", {"exact_p95_gate_ms": 50.1}),
            ("fuzzy p95 gate", {"fuzzy_p95_gate_ms": 500.1}),
            ("migration gate", {"migration_gate_seconds": 120.1}),
            ("peak RSS gate", {"peak_rss_gate_mib": 512.1}),
            ("candidate recall gate", {"candidate_recall_gate": 0.99}),
        )
        for expected, changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, expected):
                    replace(_benchmark_contract(), **changes)

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_benchmark_contract(), corpus_digest="not-a-digest")

    def test_path_specific_reports_bind_contract_cohorts_config_and_environment(
        self,
    ) -> None:
        for path in BenchmarkExecutionPath:
            with self.subTest(path=path):
                report = _benchmark_report(path=path)
                encoded = contract_to_json(report)
                self.assertEqual(contract_from_json(encoded), report)
                self.assertEqual(
                    contract_to_json(contract_from_json(encoded)),
                    encoded,
                )

        report = _benchmark_report()
        mismatches = (
            ("contract digest", {"contract_digest": _DIGEST_1}),
            ("corpus digest", {"corpus_digest": _DIGEST_1}),
            ("exact cohort digest", {"exact_cohort_digest": _DIGEST_1}),
            ("fuzzy cohort digest", {"fuzzy_cohort_digest": _DIGEST_1}),
            ("oracle subset digest", {"oracle_subset_digest": _DIGEST_1}),
            ("scorer config digest", {"scorer_config_digest": _DIGEST_1}),
            ("path config digest", {"path_config_digest": _DIGEST_1}),
            ("percentile method", {"percentile_method": "linear"}),
            ("environment digest", {"environment_digest": _DIGEST_1}),
            (
                "corpus composition version",
                {"corpus_composition_version": "other-v1"},
            ),
            (
                "corpus composition digest",
                {"corpus_composition_digest": _DIGEST_0},
            ),
        )
        for expected, changes in mismatches:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, expected):
                    replace(report, **changes)

        with self.assertRaisesRegex(ValueError, "contract digest"):
            replace(
                report,
                contract=replace(
                    report.contract,
                    corpus_seed=report.contract.corpus_seed + 1,
                ),
            )

    def test_fast_and_fallback_path_capability_cannot_be_mixed(self) -> None:
        fast = _benchmark_report()
        fallback = _benchmark_report(path=BenchmarkExecutionPath.GRAM_FALLBACK)
        self.assertNotEqual(fast.path_config_digest, fallback.path_config_digest)

        fallback_environment = _environment(
            BenchmarkExecutionPath.GRAM_FALLBACK
        )
        with self.assertRaisesRegex(ValueError, "FTS5 environment"):
            replace(
                fast,
                environment=fallback_environment,
                environment_digest=benchmark_environment_digest(
                    fallback_environment
                ),
            )
        fast_environment = _environment(
            BenchmarkExecutionPath.FTS5_TRIGRAM
        )
        with self.assertRaisesRegex(ValueError, "fallback environment"):
            replace(
                fallback,
                environment=fast_environment,
                environment_digest=benchmark_environment_digest(
                    fast_environment
                ),
            )
        with self.assertRaisesRegex(ValueError, "path config digest"):
            replace(fast, path_config_digest=fallback.path_config_digest)

    def test_passed_and_failed_gates_are_derived_from_measured_facts(self) -> None:
        exact_failure = _benchmark_report(
            exact_p95_ms=50.1,
            passed=False,
            failed_gates=("EXACT_P95",),
        )
        self.assertFalse(exact_failure.passed)

        with self.assertRaisesRegex(ValueError, "passed"):
            _benchmark_report(
                exact_p95_ms=50.1,
                failed_gates=("EXACT_P95",),
            )
        with self.assertRaisesRegex(ValueError, "failed gates"):
            _benchmark_report(
                exact_p95_ms=50.1,
                passed=False,
                failed_gates=("FUZZY_P95",),
            )
        with self.assertRaisesRegex(ValueError, "failed gates"):
            _benchmark_report(
                exact_p95_ms=50.1,
                fuzzy_p95_ms=500.1,
                passed=False,
                failed_gates=("FUZZY_P95", "EXACT_P95"),
            )

    def test_report_rejects_insufficient_samples_and_non_finite_metrics(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact sample count"):
            replace(_benchmark_report(), exact_sample_count=999)
        with self.assertRaisesRegex(ValueError, "exact sample count"):
            replace(_benchmark_report(), exact_sample_count=1_201)
        with self.assertRaisesRegex(ValueError, "fuzzy sample count"):
            replace(_benchmark_report(), fuzzy_sample_count=199)
        with self.assertRaisesRegex(ValueError, "fuzzy sample count"):
            replace(_benchmark_report(), fuzzy_sample_count=241)
        with self.assertRaisesRegex(ValueError, "oracle query count"):
            replace(_benchmark_report(), oracle_query_count=199)
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(_benchmark_report(), exact_p95_ms=float("nan"))
        with self.assertRaisesRegex(ValueError, "monotonic"):
            replace(_benchmark_report(), exact_p50_ms=50.0)

    def test_suite_report_requires_both_paths_and_derives_aggregate_pass(
        self,
    ) -> None:
        passing = _benchmark_suite_report()
        failing = _benchmark_suite_report(fallback_failed=True)
        self.assertTrue(passing.passed)
        self.assertFalse(failing.passed)
        self.assertEqual(
            failing.failed_paths,
            (BenchmarkExecutionPath.GRAM_FALLBACK,),
        )

        for contract in (
            passing.suite_contract,
            passing,
            failing,
        ):
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                decoded = contract_from_json(encoded)
                self.assertEqual(decoded, contract)
                self.assertEqual(contract_to_json(decoded), encoded)

        with self.assertRaisesRegex(ValueError, "both benchmark paths"):
            replace(
                passing.suite_contract,
                required_paths=(BenchmarkExecutionPath.FTS5_TRIGRAM,),
            )
        with self.assertRaisesRegex(ValueError, "both benchmark paths"):
            replace(passing, path_reports=passing.path_reports[:1])
        with self.assertRaisesRegex(ValueError, "path order"):
            replace(
                passing,
                path_reports=tuple(reversed(passing.path_reports)),
            )
        with self.assertRaisesRegex(ValueError, "both benchmark paths"):
            replace(
                passing,
                path_reports=(
                    passing.path_reports[0],
                    passing.path_reports[0],
                ),
            )
        with self.assertRaisesRegex(ValueError, "aggregate passed"):
            _benchmark_suite_report(
                fallback_failed=True,
                passed=True,
            )
        with self.assertRaisesRegex(ValueError, "failed paths"):
            _benchmark_suite_report(
                fallback_failed=True,
                failed_paths=(),
            )

        envelope = json.loads(contract_to_json(passing))
        envelope["payload"]["aggregate_ready"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            contract_from_json(
                json.dumps(envelope, separators=(",", ":"), sort_keys=True)
            )

    def test_suite_rejects_contract_and_corpus_composition_tampering(
        self,
    ) -> None:
        suite = _benchmark_suite_report()
        with self.assertRaisesRegex(ValueError, "suite contract digest"):
            replace(suite, suite_contract_digest=_DIGEST_0)
        with self.assertRaisesRegex(ValueError, "corpus composition version"):
            replace(suite, corpus_composition_version="other-v1")
        with self.assertRaisesRegex(ValueError, "corpus composition digest"):
            replace(suite, corpus_composition_digest=_DIGEST_0)

        object.__setattr__(
            suite.path_reports[1].contract,
            "corpus_composition_digest",
            _DIGEST_0,
        )
        with self.assertRaisesRegex(ValueError, "contract digest"):
            contract_to_json(suite)

    def test_codec_revalidates_mutated_nested_benchmark_contract(self) -> None:
        report = _benchmark_report()
        object.__setattr__(report.contract, "candidate_recall_gate", 0.99)
        with self.assertRaisesRegex(ValueError, "candidate recall gate"):
            contract_to_json(report)


if __name__ == "__main__":
    unittest.main()
