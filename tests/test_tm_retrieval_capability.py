"""Task 7.4 retrieval capability evaluator/publisher focused tests.

The suite proves ``tm_retrieval_capability.py`` is the sole decision and
publication boundary: exact frozen value types, a fail-closed matrix
(missing / identity-invalid / failed / expired / open / downgrade),
independent CONTEXT / fuzzy-core / Gate-D path decisions, an opaque and
deterministic evidence summary, tamper-resistant publisher refresh, and
immutable snapshots.  No store, candidate, retrieval or benchmark runner
module is imported here.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any, cast
import unittest

from tm_contracts import (
    BENCHMARK_CONTRACT_VERSION,
    CANDIDATE_BUDGET_VERSION,
    BenchmarkContract,
    BenchmarkExecutionPath,
    BenchmarkReport,
    benchmark_contract_digest,
    benchmark_environment_digest,
)
from tm_retrieval_capability import (
    RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION,
    RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
    RETRIEVAL_SEMANTICS_VERSION,
    RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
    RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
    RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
    RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE,
    RetrievalBenchmarkEvidence,
    RetrievalBenchmarkExpectation,
    RetrievalCapabilityEvaluator,
    RetrievalCapabilityEvidenceSummary,
    RetrievalCapabilityExpectation,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalContextDecision,
    RetrievalCorrectnessCohortEvidence,
    RetrievalCohortExpectation,
    RetrievalFuzzyCoreDecision,
    RetrievalFuzzyPathDecision,
    default_retrieval_capability_publisher,
)


GENERATED_UTC = "2026-08-12T00:00:00Z"
VALID_UNTIL_UTC = "2026-08-13T00:00:00Z"
EVALUATED_AT = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
EXPIRED_AT = datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)
_DEFAULT = object()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


ARTIFACT_DIGEST = _digest("artifact")
BUILD_DIGEST = _digest("build")
FIXTURE_DIGEST = _digest("fixture")
EVALUATOR_DIGEST = _digest("evaluator")
CONTEXT_COHORT_DIGEST = _digest("context.cohort")
FUZZY_CORE_COHORT_DIGEST = _digest("fuzzy-core.cohort")


def _contract() -> BenchmarkContract:
    return BenchmarkContract(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        corpus_generator_version="tm-benchmark-corpus-v1",
        corpus_seed=20260729,
        corpus_record_count=100_000,
        corpus_digest=_digest("corpus"),
        corpus_composition_version="tm-corpus-composition-v1",
        corpus_composition_digest=_digest("composition"),
        exact_cohort_digest=_digest("exact-cohort"),
        exact_min_samples=1_000,
        exact_cohort_count=1_200,
        fuzzy_cohort_digest=_digest("fuzzy-cohort"),
        fuzzy_min_samples=200,
        fuzzy_cohort_count=240,
        oracle_subset_digest=_digest("oracle-subset"),
        oracle_subset_record_count=5_000,
        oracle_query_count=200,
        top_k=10,
        minimum_similarity=0.60,
        warmup_queries_per_cohort=100,
        measured_repeats=1,
        percentile_method="nearest-rank",
        rss_scope="child-process-lifetime-v1",
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        scorer_config_digest=_digest("scorer-config"),
        fast_path_config_digest=_digest("fast-path"),
        fallback_path_config_digest=_digest("fallback-path"),
        exact_p95_gate_ms=50.0,
        fuzzy_p95_gate_ms=500.0,
        migration_gate_seconds=120.0,
        peak_rss_gate_mib=512.0,
        candidate_recall_gate=1.0,
    )


def _report(
    path: BenchmarkExecutionPath,
    *,
    passed: bool = True,
) -> BenchmarkReport:
    contract = _contract()
    exact_p95_ms = 49.0 if passed else 51.0
    environment = (
        ("cpu", "test-cpu"),
        (
            "fts5_enabled",
            "true" if path is BenchmarkExecutionPath.FTS5_TRIGRAM else "false",
        ),
        ("os", "test-os"),
        ("python_version", "3.14.0"),
        ("ram_mib", "16384"),
        ("sqlite_version", "3.51.2"),
        ("unicode_version", "16.0.0"),
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
        path_config_digest=(
            contract.fast_path_config_digest
            if path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract.fallback_path_config_digest
        ),
        exact_sample_count=contract.exact_cohort_count,
        fuzzy_sample_count=contract.fuzzy_cohort_count,
        oracle_query_count=200,
        percentile_method="nearest-rank",
        candidate_recall=1.0,
        exact_p50_ms=25.0,
        exact_p95_ms=exact_p95_ms,
        exact_max_ms=75.0,
        fuzzy_top10_p50_ms=250.0,
        fuzzy_top10_p95_ms=499.0,
        fuzzy_top10_max_ms=750.0,
        migration_seconds=119.0,
        peak_rss_mib=511.0,
        passed=passed,
        failed_gates=() if passed else ("EXACT_P95",),
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )


def _benchmark_evidence(
    path: BenchmarkExecutionPath,
    *,
    passed: bool = True,
) -> RetrievalBenchmarkEvidence:
    return RetrievalBenchmarkEvidence(
        report=_report(path, passed=passed),
        generated_at_utc=GENERATED_UTC,
        valid_until_utc=VALID_UNTIL_UTC,
    )


def _cohort(
    cohort_id: str,
    cohort_digest: str,
    *,
    passed: bool = True,
    generated_at_utc: str = GENERATED_UTC,
    valid_until_utc: str = VALID_UNTIL_UTC,
) -> RetrievalCorrectnessCohortEvidence:
    return RetrievalCorrectnessCohortEvidence(
        cohort_id=cohort_id,
        cohort_digest=cohort_digest,
        passed=passed,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
    )


def _expectation(
    *,
    evaluator_digest: str = EVALUATOR_DIGEST,
) -> RetrievalCapabilityExpectation:
    contract_digest = benchmark_contract_digest(_contract())
    return RetrievalCapabilityExpectation(
        evidence_schema_version=(
            RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ),
        retrieval_artifact_digest=ARTIFACT_DIGEST,
        retrieval_build_digest=BUILD_DIGEST,
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=FIXTURE_DIGEST,
        evaluator_digest=evaluator_digest,
        context_cohorts=(
            RetrievalCohortExpectation(
                cohort_id="context.correctness.cohort.v1",
                cohort_digest=CONTEXT_COHORT_DIGEST,
            ),
        ),
        fuzzy_core_cohorts=(
            RetrievalCohortExpectation(
                cohort_id="fuzzy.core.correctness.cohort.v1",
                cohort_digest=FUZZY_CORE_COHORT_DIGEST,
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


def _manifest(
    *,
    evaluator_digest: str = EVALUATOR_DIGEST,
    context_cohort: RetrievalCorrectnessCohortEvidence | None = None,
    fuzzy_core_cohort: RetrievalCorrectnessCohortEvidence | None = None,
    fts5_benchmark: object = _DEFAULT,
    gram_benchmark: object = _DEFAULT,
    generated_at_utc: str = GENERATED_UTC,
    valid_until_utc: str = VALID_UNTIL_UTC,
) -> RetrievalCapabilityManifest:
    if context_cohort is None:
        context_cohort = _cohort(
            "context.correctness.cohort.v1",
            CONTEXT_COHORT_DIGEST,
        )
    if fuzzy_core_cohort is None:
        fuzzy_core_cohort = _cohort(
            "fuzzy.core.correctness.cohort.v1",
            FUZZY_CORE_COHORT_DIGEST,
        )
    if fts5_benchmark is _DEFAULT:
        fts5_benchmark = _benchmark_evidence(
            BenchmarkExecutionPath.FTS5_TRIGRAM
        )
    if gram_benchmark is _DEFAULT:
        gram_benchmark = _benchmark_evidence(
            BenchmarkExecutionPath.GRAM_FALLBACK
        )
    return RetrievalCapabilityManifest(
        evidence_schema_version=(
            RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ),
        retrieval_artifact_digest=ARTIFACT_DIGEST,
        retrieval_build_digest=BUILD_DIGEST,
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=FIXTURE_DIGEST,
        evaluator_digest=evaluator_digest,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
        context_cohorts=(context_cohort,),
        fuzzy_core_cohorts=(fuzzy_core_cohort,),
        fts5_trigram_benchmark=cast(
            RetrievalBenchmarkEvidence | None,
            fts5_benchmark,
        ),
        gram_fallback_benchmark=cast(
            RetrievalBenchmarkEvidence | None,
            gram_benchmark,
        ),
    )


def _open_snapshot() -> RetrievalCapabilitySnapshot:
    return RetrievalCapabilityEvaluator(_expectation()).evaluate(
        _manifest(),
        evaluated_at_utc=EVALUATED_AT,
    )


class _ContextDecisionSubclass(RetrievalContextDecision):
    pass


class _ManifestSubclass(RetrievalCapabilityManifest):
    pass


class _EvaluatorSubclass(RetrievalCapabilityEvaluator):  # pyright: ignore[reportGeneralTypeIssues]
    pass


class _ExpectationSubclass(RetrievalCapabilityExpectation):
    pass


class ExactTypeClosureTests(unittest.TestCase):
    def test_snapshot_rejects_subclassed_decision_values(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "must be RetrievalContextDecision",
        ):
            RetrievalCapabilitySnapshot(
                semantics_version=RETRIEVAL_SEMANTICS_VERSION,
                context=_ContextDecisionSubclass(
                    available=True,
                    unavailable_code=None,
                ),
                fuzzy_core=RetrievalFuzzyCoreDecision(
                    available=True,
                    unavailable_code=None,
                ),
                fts5_trigram=RetrievalFuzzyPathDecision(
                    path="FTS5_TRIGRAM",
                    available=True,
                    unavailable_code=None,
                ),
                gram_fallback=RetrievalFuzzyPathDecision(
                    path="GRAM_FALLBACK",
                    available=True,
                    unavailable_code=None,
                ),
                summary=RetrievalCapabilityEvidenceSummary(
                    summary_version=RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
                    evidence_digest=_digest("evidence"),
                    evaluated_at_utc=EVALUATED_AT,
                    unavailable_codes=(),
                ),
            )

    def test_publisher_rejects_subclassed_evaluator_and_manifest(self) -> None:
        expectation = _expectation()
        with self.assertRaisesRegex(TypeError, "exact RetrievalCapabilityEvaluator"):
            RetrievalCapabilityPublisher(
                _EvaluatorSubclass(expectation),
                initial_manifest=None,
                evaluated_at_utc=EVALUATED_AT,
            )
        evaluator = RetrievalCapabilityEvaluator(expectation)
        with self.assertRaisesRegex(TypeError, "exact RetrievalCapabilityManifest"):
            evaluator.evaluate(
                _ManifestSubclass(
                    **(cast(Any, _manifest_as_kwargs(_manifest())))
                ),
                evaluated_at_utc=EVALUATED_AT,
            )

    def test_evaluator_rejects_subclassed_expectation(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "exact RetrievalCapabilityExpectation",
        ):
            RetrievalCapabilityEvaluator(
                _ExpectationSubclass(
                    **(cast(Any, _expectation_as_kwargs(_expectation())))
                )
            )

    def test_fuzzy_path_must_be_supported(self) -> None:
        snapshot = _open_snapshot()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            snapshot.fuzzy_available_for("FAST_PATH")

    def test_publisher_clones_the_expectation_private(self) -> None:
        expectation = _expectation()
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(expectation),
            initial_manifest=None,
            evaluated_at_utc=EVALUATED_AT,
        )
        private = cast(
            Any,
            publisher,
        )._RetrievalCapabilityPublisher__expectation_identity
        self.assertIsNot(private, expectation)
        self.assertEqual(private, expectation)


class FailClosedMatrixTests(unittest.TestCase):
    def test_missing_manifest_closes_every_gate_with_missing_codes(self) -> None:
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            None,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
        )
        self.assertFalse(snapshot.fuzzy_core.available)
        self.assertEqual(
            snapshot.fuzzy_core.unavailable_code,
            RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
        )
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (False, RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE),
        )

    def test_identity_mismatch_closes_every_gate_with_identity_codes(self) -> None:
        manifest = _manifest(evaluator_digest=_digest("rogue-evaluator"))
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )
        self.assertEqual(
            snapshot.fuzzy_core.unavailable_code,
            RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE,
        )
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE,
        )
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE,
        )

    def test_self_reported_passed_without_approved_digest_is_failed(self) -> None:
        manifest = _manifest(
            context_cohort=_cohort(
                "context.correctness.cohort.v1",
                _digest("rogue-cohort"),
                passed=True,
            )
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
        )

    def test_failed_cohort_evidence_closes_only_its_own_gate(self) -> None:
        manifest = _manifest(
            context_cohort=_cohort(
                "context.correctness.cohort.v1",
                CONTEXT_COHORT_DIGEST,
                passed=False,
            )
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
        )
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertTrue(snapshot.fts5_trigram.available)
        self.assertTrue(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (True, None),
        )

    def test_failed_benchmark_report_closes_only_its_path(self) -> None:
        manifest = _manifest(
            fts5_benchmark=_benchmark_evidence(
                BenchmarkExecutionPath.FTS5_TRIGRAM,
                passed=False,
            )
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
        )
        self.assertTrue(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (False, RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (True, None),
        )

    def test_evidence_expiry_closes_with_expired_codes(self) -> None:
        manifest = _manifest(valid_until_utc="2026-08-12T11:00:00Z")
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
        )
        self.assertEqual(
            snapshot.fuzzy_core.unavailable_code,
            RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE,
        )
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE,
        )
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE,
        )

    def test_open_snapshot_has_no_unavailable_codes(self) -> None:
        snapshot = _open_snapshot()
        self.assertTrue(snapshot.context.available)
        self.assertIsNone(snapshot.context.unavailable_code)
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertTrue(snapshot.fts5_trigram.available)
        self.assertTrue(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (True, None),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (True, None),
        )
        self.assertEqual(snapshot.summary.unavailable_codes, ())

    def test_refresh_can_downgrade_open_to_closed(self) -> None:
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(_expectation()),
            initial_manifest=_manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertTrue(publisher.snapshot().context.available)
        self.assertEqual(
            publisher.snapshot().fuzzy_available_for("FTS5_TRIGRAM"),
            (True, None),
        )
        refreshed = publisher.refresh(
            _manifest(context_cohort=_cohort(
                "context.correctness.cohort.v1",
                CONTEXT_COHORT_DIGEST,
                passed=False,
            )),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(refreshed.context.available)
        self.assertEqual(
            refreshed.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
        )
        self.assertIs(publisher.snapshot(), refreshed)


class IndependentGateTests(unittest.TestCase):
    def test_closed_context_never_revokes_fuzzy_or_exact_capability(self) -> None:
        manifest = _manifest(
            context_cohort=_cohort(
                "context.correctness.cohort.v1",
                CONTEXT_COHORT_DIGEST,
                passed=False,
            )
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertTrue(snapshot.fts5_trigram.available)
        self.assertTrue(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (True, None),
        )

    def test_closed_fuzzy_core_closes_both_paths_despite_valid_reports(self) -> None:
        manifest = _manifest(
            fuzzy_core_cohort=_cohort(
                "fuzzy.core.correctness.cohort.v1",
                FUZZY_CORE_COHORT_DIGEST,
                passed=False,
            )
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertTrue(snapshot.context.available)
        self.assertFalse(snapshot.fuzzy_core.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (False, RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (False, RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE),
        )

    def test_path_decision_is_independent_per_execution_path(self) -> None:
        manifest = _manifest(gram_benchmark=None)
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (True, None),
        )
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (False, RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE),
        )


class SummaryOpacityAndDeterminismTests(unittest.TestCase):
    def test_summary_contains_only_version_digest_time_and_codes(self) -> None:
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            _manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        summary = snapshot.summary
        self.assertEqual(
            summary.summary_version,
            RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
        )
        self.assertRegex(summary.evidence_digest, r"[0-9a-f]{64}")
        self.assertEqual(summary.evaluated_at_utc, EVALUATED_AT)
        self.assertEqual(summary.unavailable_codes, ())
        body = repr(summary)
        self.assertNotIn("Open the door", body)
        self.assertNotIn("source", body)
        self.assertNotIn("target", body)

    def test_identical_inputs_produce_identical_snapshots(self) -> None:
        evaluator = RetrievalCapabilityEvaluator(_expectation())
        manifest = _manifest()
        first = evaluator.evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        second = evaluator.evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first.summary.evidence_digest,
            second.summary.evidence_digest,
        )

    def test_evidence_digest_is_stable_across_valid_evaluation_instants(self) -> None:
        evaluator = RetrievalCapabilityEvaluator(_expectation())
        manifest = _manifest()
        morning = evaluator.evaluate(
            manifest,
            evaluated_at_utc=datetime(
                2026,
                8,
                12,
                1,
                0,
                0,
                tzinfo=timezone.utc,
            ),
        )
        noon = evaluator.evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertEqual(
            morning.summary.evidence_digest,
            noon.summary.evidence_digest,
        )
        self.assertNotEqual(
            morning.summary.evaluated_at_utc,
            noon.summary.evaluated_at_utc,
        )

    def test_closed_and_open_states_never_share_one_digest(self) -> None:
        evaluator = RetrievalCapabilityEvaluator(_expectation())
        closed = evaluator.evaluate(None, evaluated_at_utc=EVALUATED_AT)
        opened = evaluator.evaluate(
            _manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertNotEqual(
            closed.summary.evidence_digest,
            opened.summary.evidence_digest,
        )


class TamperHandlingTests(unittest.TestCase):
    def test_replacing_private_evaluator_fails_closed(self) -> None:
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(_expectation()),
            initial_manifest=_manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        rogue = RetrievalCapabilityEvaluator(_expectation())
        object.__setattr__(
            publisher,
            "_RetrievalCapabilityPublisher__evaluator",
            rogue,
        )
        snapshot = publisher.refresh(
            _manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )
        self.assertEqual(
            snapshot.fuzzy_core.unavailable_code,
            RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE,
        )

    def test_mutating_private_expectation_fails_closed(self) -> None:
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(_expectation()),
            initial_manifest=_manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        private_expectation = (
            cast(Any, publisher)._RetrievalCapabilityPublisher__expectation_identity
        )
        object.__setattr__(
            private_expectation,
            "evaluator_digest",
            _digest("mutated"),
        )
        snapshot = publisher.refresh(
            _manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )

    def test_manifest_passed_flag_cannot_grant_availability_by_itself(self) -> None:
        manifest = _manifest(
            evaluator_digest=_digest("rogue"),
        )
        snapshot = RetrievalCapabilityEvaluator(_expectation()).evaluate(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )


class SnapshotImmutabilityTests(unittest.TestCase):
    def test_snapshot_and_decisions_are_frozen(self) -> None:
        snapshot = _open_snapshot()
        with self.assertRaises(FrozenInstanceError):
            snapshot.context.available = False  # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaises(FrozenInstanceError):
            snapshot.fuzzy_core.available = False  # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaises(FrozenInstanceError):
            snapshot.summary.evidence_digest = _digest("mutated")  # pyright: ignore[reportAttributeAccessIssue]

    def test_refresh_replaces_the_snapshot_after_consumer_tampering(self) -> None:
        publisher = RetrievalCapabilityPublisher(
            RetrievalCapabilityEvaluator(_expectation()),
            initial_manifest=_manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        captured = publisher.snapshot()
        object.__setattr__(captured.context, "available", False)
        refreshed = publisher.refresh(
            _manifest(),
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertTrue(refreshed.context.available)


class ProductionDefaultTests(unittest.TestCase):
    def test_default_publisher_is_fail_closed_with_stable_missing_codes(self) -> None:
        publisher = default_retrieval_capability_publisher(EVALUATED_AT)
        snapshot = publisher.snapshot()
        self.assertIs(type(snapshot), RetrievalCapabilitySnapshot)
        self.assertEqual(
            snapshot.semantics_version,
            RETRIEVAL_SEMANTICS_VERSION,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (False, RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (False, RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE),
        )
        self.assertEqual(
            snapshot.summary.unavailable_codes,
            (
                RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
                RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
            ),
        )

    def test_default_publisher_refresh_still_requires_approved_identity(self) -> None:
        publisher = default_retrieval_capability_publisher(EVALUATED_AT)
        manifest = _manifest(
            evaluator_digest=_digest("rogue"),
        )
        snapshot = publisher.refresh(
            manifest,
            evaluated_at_utc=EVALUATED_AT,
        )
        self.assertFalse(snapshot.context.available)
        self.assertEqual(
            snapshot.context.unavailable_code,
            RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )


def _manifest_as_kwargs(
    manifest: RetrievalCapabilityManifest,
) -> dict[str, object]:
    return {
        "evidence_schema_version": manifest.evidence_schema_version,
        "retrieval_artifact_digest": manifest.retrieval_artifact_digest,
        "retrieval_build_digest": manifest.retrieval_build_digest,
        "semantics_version": manifest.semantics_version,
        "fixture_digest": manifest.fixture_digest,
        "evaluator_digest": manifest.evaluator_digest,
        "generated_at_utc": manifest.generated_at_utc,
        "valid_until_utc": manifest.valid_until_utc,
        "context_cohorts": manifest.context_cohorts,
        "fuzzy_core_cohorts": manifest.fuzzy_core_cohorts,
        "fts5_trigram_benchmark": manifest.fts5_trigram_benchmark,
        "gram_fallback_benchmark": manifest.gram_fallback_benchmark,
    }


def _expectation_as_kwargs(
    expectation: RetrievalCapabilityExpectation,
) -> dict[str, object]:
    return {
        "evidence_schema_version": expectation.evidence_schema_version,
        "retrieval_artifact_digest": expectation.retrieval_artifact_digest,
        "retrieval_build_digest": expectation.retrieval_build_digest,
        "semantics_version": expectation.semantics_version,
        "fixture_digest": expectation.fixture_digest,
        "evaluator_digest": expectation.evaluator_digest,
        "context_cohorts": expectation.context_cohorts,
        "fuzzy_core_cohorts": expectation.fuzzy_core_cohorts,
        "fts5_trigram": expectation.fts5_trigram,
        "gram_fallback": expectation.gram_fallback,
    }


if __name__ == "__main__":
    unittest.main()
