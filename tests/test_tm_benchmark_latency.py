"""Focused tests for the benchmark-v1 exact/fuzzy latency evidence owner."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import inspect
import math
from typing import Any, cast
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

from tm_benchmark import (
    iter_exact_queries,
    iter_fuzzy_queries,
    load_benchmark_contract,
)
from tm_benchmark_latency import (
    LATENCY_EVIDENCE_SCHEMA_VERSION,
    LatencyEvidence,
    LatencySample,
    LatencyExecutor,
    collect_benchmark_environment,
    latency_evidence_digest,
    measure_path_latency,
    nearest_rank_percentile,
    recompute_cohort_statistics,
    validate_environment_for_path,
)
from tm_contracts import (
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
)

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")

_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK

_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+([A-Za-z0-9_\.]+)",
    re.MULTILINE,
)

_BANNED_RUNTIME_MODULES = {
    "tm_sqlite_store",
    "tm_retrieval",
    "tm_retrieval_capability",
    "tm_retrieval_validation",
    "tm_migration",
    "tm_candidate_index",
    "tm_engine",
    "text_matcher",
    "tm_similarity",
    "matcher_capability",
    "matcher_validation",
    "qt_editor",
    "deterministic_workload",
    "tm_snapshot_artifacts",
    "tm_snapshot_recovery",
    "tm_gate_a",
    "tm_gate_b",
}


def _environment(fts5_enabled: str) -> tuple[tuple[str, str], ...]:
    facts = {
        "cpu": "test-cpu",
        "fts5_enabled": fts5_enabled,
        "os": "test-os",
        "python_version": "test-python",
        "ram_mib": "1024",
        "sqlite_version": "test-sqlite",
        "unicode_version": "test-unicode",
        "timing_clock": "test-clock-v1",
        "percentile_method": "nearest-rank",
        "warmup_queries_per_cohort": "100",
        "measured_repeats": "1",
    }
    return tuple(sorted(facts.items()))


def _deltas(count: int) -> tuple[int, ...]:
    return tuple(index * 37 + 3 for index in range(count))


class _RecordingClock:
    """Deterministic clock returning cumulative sums of per-read deltas."""

    def __init__(self, deltas: Sequence[int]) -> None:
        values: list[int] = []
        total = 0
        for delta in deltas:
            total += delta
            values.append(total)
        self._values = tuple(values)
        self._deltas = tuple(deltas)
        self._index = 0
        self.read_count = 0

    def __call__(self) -> int:
        if self._index >= len(self._values):
            raise AssertionError("clock exhausted")
        value = self._values[self._index]
        self._index += 1
        self.read_count += 1
        return value

    def expected_elapsed(self, call_index: int) -> int:
        return self._deltas[2 * call_index + 1]


@dataclass(frozen=True)
class _ExactOutcome:
    actual_path: BenchmarkExecutionPath
    succeeded: bool
    result_count: int


@dataclass(frozen=True)
class _FuzzyOutcome:
    actual_path: BenchmarkExecutionPath
    succeeded: bool
    result_count: int
    minimum_similarity: float
    top_k: int


def _other_path(path: BenchmarkExecutionPath) -> BenchmarkExecutionPath:
    if path is _FTS5:
        return _FALLBACK
    return _FTS5


class _FakeExecutor:
    """Deterministic in-memory executor with failure/drift injection."""

    def __init__(
        self,
        path: BenchmarkExecutionPath,
        *,
        exact_result_count: int = 1,
        fuzzy_result_count: int = 1,
        fail_at_call: int | None = None,
        drift_at_call: int | None = None,
        echo_wrong_minimum_similarity: bool = False,
        echo_wrong_top_k: bool = False,
    ) -> None:
        self.path = path
        self.exact_result_count = exact_result_count
        self.fuzzy_result_count = fuzzy_result_count
        self.fail_at_call = fail_at_call
        self.drift_at_call = drift_at_call
        self.echo_wrong_minimum_similarity = echo_wrong_minimum_similarity
        self.echo_wrong_top_k = echo_wrong_top_k
        self.call_count = 0

    def _next_succeeded(self) -> bool:
        self.call_count += 1
        return self.fail_at_call != self.call_count

    def _actual_path(self) -> BenchmarkExecutionPath:
        if self.drift_at_call == self.call_count:
            return _other_path(self.path)
        return self.path

    def exact_lookup(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
    ) -> _ExactOutcome:
        return _ExactOutcome(
            actual_path=self._actual_path(),
            succeeded=self._next_succeeded(),
            result_count=self.exact_result_count,
        )

    def fuzzy_top_k(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
        minimum_similarity: float,
        top_k: int,
    ) -> _FuzzyOutcome:
        return _FuzzyOutcome(
            actual_path=self._actual_path(),
            succeeded=self._next_succeeded(),
            result_count=self.fuzzy_result_count,
            minimum_similarity=(
                0.5 if self.echo_wrong_minimum_similarity else minimum_similarity
            ),
            top_k=7 if self.echo_wrong_top_k else top_k,
        )


class _ProbeExecutor:
    """Wraps an executor and records the clock read count at each call."""

    def __init__(self, inner: _FakeExecutor, clock: _RecordingClock) -> None:
        self._inner = inner
        self._clock = clock
        self.calls: list[tuple[str, int]] = []

    def exact_lookup(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
    ) -> _ExactOutcome:
        self.calls.append(("exact", self._clock.read_count))
        return self._inner.exact_lookup(query_raw, requested_path=requested_path)

    def fuzzy_top_k(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
        minimum_similarity: float,
        top_k: int,
    ) -> _FuzzyOutcome:
        self.calls.append(("fuzzy", self._clock.read_count))
        return self._inner.fuzzy_top_k(
            query_raw,
            requested_path=requested_path,
            minimum_similarity=minimum_similarity,
            top_k=top_k,
        )


def _small_evidence(
    *,
    exact_elapsed: tuple[int, ...] | None = None,
    fuzzy_elapsed: tuple[int, ...] | None = None,
    path: BenchmarkExecutionPath = _FTS5,
    fts5_enabled: str = "true",
    minimum_similarity: float = 0.6,
    top_k: int = 10,
) -> LatencyEvidence:
    if exact_elapsed is None:
        exact_elapsed = tuple(range(1, _CONTRACT.exact_cohort_count + 1))
    if fuzzy_elapsed is None:
        fuzzy_elapsed = tuple(range(1, _CONTRACT.fuzzy_cohort_count + 1))
    exact_samples = tuple(
        LatencySample(
            query_id=index,
            elapsed_ns=elapsed,
            cohort="exact",
            actual_path=path,
            succeeded=True,
            result_count=1,
        )
        for index, elapsed in enumerate(exact_elapsed, start=1)
    )
    fuzzy_samples = tuple(
        LatencySample(
            query_id=index,
            elapsed_ns=elapsed,
            cohort="fuzzy",
            actual_path=path,
            succeeded=True,
            result_count=1,
            minimum_similarity=minimum_similarity,
            top_k=top_k,
        )
        for index, elapsed in enumerate(fuzzy_elapsed, start=1)
    )
    exact_stats = recompute_cohort_statistics(exact_elapsed)
    fuzzy_stats = recompute_cohort_statistics(fuzzy_elapsed)
    environment = _environment(fts5_enabled)
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
        percentile_method="nearest-rank",
        timing_clock="test-clock-v1",
        minimum_similarity=minimum_similarity,
        top_k=top_k,
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


class NearestRankStatisticTests(unittest.TestCase):
    def test_p50_uses_ceil_rank_not_interpolation(self) -> None:
        samples = (1, 2, 3)
        self.assertEqual(nearest_rank_percentile(samples, 0.5), 2)
        self.assertEqual(nearest_rank_percentile(samples, 0.95), 3)

    def test_single_sample_percentiles_equal_the_sample(self) -> None:
        self.assertEqual(nearest_rank_percentile((42,), 0.5), 42)
        self.assertEqual(nearest_rank_percentile((42,), 0.95), 42)
        self.assertEqual(recompute_cohort_statistics((42,)), (42, 42, 42))

    def test_recomputes_from_unsorted_raw_samples(self) -> None:
        raw = (900, 100, 500, 200, 800, 300, 700, 400, 600)
        ordered = tuple(sorted(raw))
        p50 = ordered[max(1, math.ceil(0.5 * len(ordered))) - 1]
        p95 = ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]
        self.assertEqual(recompute_cohort_statistics(raw), (p50, p95, ordered[-1]))

    def test_rejects_empty_samples(self) -> None:
        with self.assertRaises(ValueError):
            nearest_rank_percentile((), 0.5)
        with self.assertRaises(ValueError):
            recompute_cohort_statistics(())

    def test_rejects_bool_and_subclass_elapsed(self) -> None:
        class _MyInt(int):
            pass

        with_bool: tuple[int, ...] = (1, True)
        with self.assertRaises(TypeError):
            recompute_cohort_statistics(with_bool)
        with self.assertRaises(TypeError):
            recompute_cohort_statistics((1, _MyInt(2)))

    def test_rejects_negative_and_non_int_elapsed(self) -> None:
        with self.assertRaises(ValueError):
            recompute_cohort_statistics((1, -1))
        mixed = cast(tuple[int, ...], (1, 2.5))
        with self.assertRaises(TypeError):
            recompute_cohort_statistics(mixed)

    def test_rejects_bad_percentile(self) -> None:
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                nearest_rank_percentile((1, 2, 3), bad)


class LatencySampleTests(unittest.TestCase):
    def test_valid_exact_and_fuzzy_samples_are_accepted(self) -> None:
        LatencySample(
            query_id=1,
            elapsed_ns=0,
            cohort="exact",
            actual_path=_FTS5,
            succeeded=True,
            result_count=1,
        )
        LatencySample(
            query_id=2,
            elapsed_ns=123,
            cohort="fuzzy",
            actual_path=_FALLBACK,
            succeeded=True,
            result_count=10,
            minimum_similarity=0.6,
            top_k=10,
        )

    def test_rejects_bool_as_int_fields(self) -> None:
        with self.assertRaises(TypeError):
            LatencySample(
                query_id=True,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )
        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=True,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )
        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=True,
            )

    def test_rejects_int_subclass_elapsed(self) -> None:
        class _MyInt(int):
            pass

        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=_MyInt(10),
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )

    def test_rejects_negative_elapsed_and_zero_query_id(self) -> None:
        with self.assertRaises(ValueError):
            LatencySample(
                query_id=1,
                elapsed_ns=-1,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )
        with self.assertRaises(ValueError):
            LatencySample(
                query_id=0,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )

    def test_rejects_unknown_cohort_and_plain_string_path(self) -> None:
        with self.assertRaises(ValueError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="oracle",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            )
        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="exact",
                actual_path=cast(Any, "FTS5_TRIGRAM"),
                succeeded=True,
                result_count=1,
            )

    def test_rejects_non_bool_succeeded(self) -> None:
        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=cast(Any, 1),
                result_count=1,
            )

    def test_rejects_partial_fuzzy_config(self) -> None:
        with self.assertRaises(ValueError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="fuzzy",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
                minimum_similarity=0.6,
            )
        with self.assertRaises(ValueError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="fuzzy",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
                top_k=10,
            )

    def test_rejects_subclassed_float_minimum_similarity(self) -> None:
        class _MyFloat(float):
            pass

        with self.assertRaises(TypeError):
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="fuzzy",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
                minimum_similarity=_MyFloat(0.6),
                top_k=10,
            )


class LatencyEvidenceConstructionTests(unittest.TestCase):
    def test_valid_evidence_recomputes_stats_and_digests(self) -> None:
        evidence = _small_evidence()
        self.assertEqual(
            evidence.recompute_statistics(),
            (
                evidence.exact_p50_ns,
                evidence.exact_p95_ns,
                evidence.exact_max_ns,
                evidence.fuzzy_p50_ns,
                evidence.fuzzy_p95_ns,
                evidence.fuzzy_max_ns,
            ),
        )
        self.assertEqual(
            evidence.recompute_environment_digest(),
            evidence.environment_digest,
        )
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )
        self.assertEqual(
            latency_evidence_digest(evidence),
            evidence.evidence_digest,
        )

    def test_evidence_digest_is_not_a_caller_input(self) -> None:
        evidence = _small_evidence()
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(evidence, evidence_digest="f" * 64)

    def test_standalone_evidence_rejects_contract_binding_drift(self) -> None:
        evidence = _small_evidence()
        for field_name, value in (
            ("contract_digest", "0" * 64),
            ("exact_cohort_digest", "0" * 64),
            ("fuzzy_cohort_digest", "0" * 64),
            ("path_config_digest", "0" * 64),
            ("warmup_queries_per_cohort", 99),
            ("measured_repeats", 2),
            ("minimum_similarity", 0.7),
            ("top_k", 9),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    replace(evidence, **{field_name: value})

    def test_evidence_privately_snapshots_contract(self) -> None:
        evidence = _small_evidence()
        self.assertEqual(evidence.contract, _CONTRACT)
        self.assertIsNot(evidence.contract, _CONTRACT)

    def test_rejects_empty_sample_buckets(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=())
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_samples=())

    def test_rejects_out_of_order_exact_ids(self) -> None:
        evidence = _small_evidence()
        swapped = (evidence.exact_samples[1], evidence.exact_samples[0]) + (
            evidence.exact_samples[2:],
        )
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=swapped)

    def test_rejects_missing_exact_id(self) -> None:
        evidence = _small_evidence()
        gap = (
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
            LatencySample(
                query_id=3,
                elapsed_ns=20,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
            LatencySample(
                query_id=4,
                elapsed_ns=30,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
        )
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=gap)

    def test_rejects_duplicate_exact_id(self) -> None:
        evidence = _small_evidence()
        duplicate = (
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
            LatencySample(
                query_id=1,
                elapsed_ns=20,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
            LatencySample(
                query_id=3,
                elapsed_ns=30,
                cohort="exact",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
            ),
        )
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=duplicate)

    def test_rejects_wrong_cohort_in_bucket(self) -> None:
        evidence = _small_evidence()
        wrong_cohort = (
            LatencySample(
                query_id=1,
                elapsed_ns=10,
                cohort="fuzzy",
                actual_path=_FTS5,
                succeeded=True,
                result_count=1,
                minimum_similarity=0.6,
                top_k=10,
            ),
        ) + evidence.exact_samples[1:]
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=wrong_cohort)

    def test_rejects_path_drift_sample(self) -> None:
        evidence = _small_evidence()
        drifted = tuple(
            replace(sample, actual_path=_FALLBACK)
            for sample in evidence.exact_samples
        )
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=drifted)

    def test_rejects_failed_sample(self) -> None:
        evidence = _small_evidence()
        failed = tuple(
            replace(sample, succeeded=False)
            for sample in evidence.fuzzy_samples
        )
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_samples=failed)

    def test_rejects_inconsistent_derived_statistics(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(evidence, exact_p95_ns=evidence.exact_p95_ns + 1)
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_max_ns=0)

    def test_rejects_wrong_sample_count_field(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(evidence, exact_sample_count=2)
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_sample_count=1)

    def test_rejects_fuzzy_config_mismatch_in_samples(self) -> None:
        evidence = _small_evidence()
        wrong_config = tuple(
            replace(sample, top_k=5)
            for sample in evidence.fuzzy_samples
        )
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_samples=wrong_config)

    def test_rejects_zero_exact_result_and_excessive_fuzzy_result(self) -> None:
        evidence = _small_evidence()
        zero_result = tuple(
            replace(sample, result_count=0)
            for sample in evidence.exact_samples
        )
        with self.assertRaises(ValueError):
            replace(evidence, exact_samples=zero_result)
        excessive = tuple(
            replace(sample, result_count=11)
            for sample in evidence.fuzzy_samples
        )
        with self.assertRaises(ValueError):
            replace(evidence, fuzzy_samples=excessive)

    def test_rejects_tampered_environment_digest(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(evidence, environment_digest="0" * 64)

    def test_rejects_environment_missing_required_field(self) -> None:
        evidence = _small_evidence()
        facts = dict(evidence.environment)
        del facts["cpu"]
        with self.assertRaises(ValueError):
            replace(evidence, environment=tuple(sorted(facts.items())))

    def test_rejects_environment_unsorted(self) -> None:
        evidence = _small_evidence()
        unsorted = tuple(reversed(evidence.environment))
        with self.assertRaises(ValueError):
            replace(evidence, environment=unsorted)

    def test_rejects_path_environment_mismatch(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(
                evidence,
                environment=_environment("false"),
                environment_digest=benchmark_environment_digest(
                    _environment("false")
                ),
            )

    def test_rejects_wrong_schema_version(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises(ValueError):
            replace(evidence, schema_version="not-the-schema")


class LatencyRunnerTests(unittest.TestCase):
    def _run(
        self,
        *,
        path: BenchmarkExecutionPath,
        environment: tuple[tuple[str, str], ...],
        executor: LatencyExecutor,
        clock: Callable[[], int],
        clock_name: str = "test-clock-v1",
    ) -> LatencyEvidence:
        return measure_path_latency(
            contract=_CONTRACT,
            requested_path=path,
            executor=executor,
            clock=clock,
            clock_name=clock_name,
            environment=environment,
        )

    def test_fts5_path_produces_frozen_evidence(self) -> None:
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        executor = _FakeExecutor(_FTS5)
        evidence = self._run(
            path=_FTS5,
            environment=_environment("true"),
            executor=executor,
            clock=clock,
        )
        self.assertEqual(evidence.schema_version, LATENCY_EVIDENCE_SCHEMA_VERSION)
        self.assertIs(evidence.execution_path, _FTS5)
        self.assertEqual(evidence.contract_digest, benchmark_contract_digest(_CONTRACT))
        self.assertEqual(evidence.exact_cohort_digest, _CONTRACT.exact_cohort_digest)
        self.assertEqual(evidence.fuzzy_cohort_digest, _CONTRACT.fuzzy_cohort_digest)
        self.assertEqual(evidence.path_config_digest, _CONTRACT.fast_path_config_digest)
        self.assertEqual(evidence.warmup_queries_per_cohort, 100)
        self.assertEqual(evidence.measured_repeats, 1)
        self.assertEqual(evidence.percentile_method, "nearest-rank")
        self.assertEqual(evidence.timing_clock, "test-clock-v1")
        self.assertEqual(evidence.minimum_similarity, 0.6)
        self.assertEqual(evidence.top_k, 10)
        self.assertEqual(evidence.exact_sample_count, _CONTRACT.exact_cohort_count)
        self.assertEqual(evidence.fuzzy_sample_count, _CONTRACT.fuzzy_cohort_count)
        self.assertEqual(
            evidence.recompute_statistics(),
            (
                evidence.exact_p50_ns,
                evidence.exact_p95_ns,
                evidence.exact_max_ns,
                evidence.fuzzy_p50_ns,
                evidence.fuzzy_p95_ns,
                evidence.fuzzy_max_ns,
            ),
        )
        self.assertEqual(
            evidence.recompute_environment_digest(),
            evidence.environment_digest,
        )
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )
        for sample in evidence.exact_samples:
            self.assertEqual(sample.cohort, "exact")
            self.assertIs(sample.actual_path, _FTS5)
            self.assertTrue(sample.succeeded)
            self.assertIsNone(sample.minimum_similarity)
            self.assertIsNone(sample.top_k)
        for sample in evidence.fuzzy_samples:
            self.assertEqual(sample.cohort, "fuzzy")
            self.assertIs(sample.actual_path, _FTS5)
            self.assertTrue(sample.succeeded)
            self.assertEqual(sample.minimum_similarity, 0.6)
            self.assertEqual(sample.top_k, 10)

    def test_fallback_path_produces_frozen_evidence(self) -> None:
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        executor = _FakeExecutor(_FALLBACK)
        evidence = self._run(
            path=_FALLBACK,
            environment=_environment("false"),
            executor=executor,
            clock=clock,
        )
        self.assertIs(evidence.execution_path, _FALLBACK)
        self.assertEqual(
            evidence.path_config_digest,
            _CONTRACT.fallback_path_config_digest,
        )
        for sample in evidence.exact_samples:
            self.assertIs(sample.actual_path, _FALLBACK)
        for sample in evidence.fuzzy_samples:
            self.assertIs(sample.actual_path, _FALLBACK)

    def test_warmup_exactly_100_per_cohort_and_untimed(self) -> None:
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        inner = _FakeExecutor(_FTS5)
        executor = _ProbeExecutor(inner, clock)
        self._run(
            path=_FTS5,
            environment=_environment("true"),
            executor=executor,
            clock=clock,
        )
        exact_calls = [call for call in executor.calls if call[0] == "exact"]
        fuzzy_calls = [call for call in executor.calls if call[0] == "fuzzy"]
        self.assertEqual(len(exact_calls), 100 + _CONTRACT.exact_cohort_count)
        self.assertEqual(len(fuzzy_calls), 100 + _CONTRACT.fuzzy_cohort_count)
        for index, (_, reads_at_entry) in enumerate(exact_calls[:100]):
            self.assertEqual(reads_at_entry, 0)
        for index, (_, reads_at_entry) in enumerate(exact_calls[100:]):
            self.assertEqual(reads_at_entry, 2 * index + 1)
        for index, (_, reads_at_entry) in enumerate(fuzzy_calls[:100]):
            self.assertEqual(reads_at_entry, 2400)
        for index, (_, reads_at_entry) in enumerate(fuzzy_calls[100:]):
            self.assertEqual(reads_at_entry, 2400 + 2 * index + 1)
        self.assertEqual(clock.read_count, 2 * (
            _CONTRACT.exact_cohort_count + _CONTRACT.fuzzy_cohort_count
        ))

    def test_each_call_timed_individually_not_batch_averaged(self) -> None:
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        executor = _FakeExecutor(_FTS5)
        evidence = self._run(
            path=_FTS5,
            environment=_environment("true"),
            executor=executor,
            clock=clock,
        )
        for index, sample in enumerate(evidence.exact_samples):
            self.assertEqual(sample.elapsed_ns, clock.expected_elapsed(index))
        for index, sample in enumerate(evidence.fuzzy_samples):
            self.assertEqual(
                sample.elapsed_ns,
                clock.expected_elapsed(_CONTRACT.exact_cohort_count + index),
            )

    def test_measured_samples_follow_frozen_order_and_counts(self) -> None:
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        executor = _FakeExecutor(_FTS5)
        evidence = self._run(
            path=_FTS5,
            environment=_environment("true"),
            executor=executor,
            clock=clock,
        )
        self.assertEqual(
            tuple(sample.query_id for sample in evidence.exact_samples),
            tuple(range(1, _CONTRACT.exact_cohort_count + 1)),
        )
        self.assertEqual(
            tuple(sample.query_id for sample in evidence.fuzzy_samples),
            tuple(range(1, _CONTRACT.fuzzy_cohort_count + 1)),
        )
        expected_exact_ids = tuple(
            query.query_id
            for query in iter_exact_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                cohort_count=_CONTRACT.exact_cohort_count,
            )
        )
        expected_fuzzy_ids = tuple(
            query.query_id
            for query in iter_fuzzy_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                cohort_count=_CONTRACT.fuzzy_cohort_count,
            )
        )
        self.assertEqual(
            tuple(sample.query_id for sample in evidence.exact_samples),
            expected_exact_ids,
        )
        self.assertEqual(
            tuple(sample.query_id for sample in evidence.fuzzy_samples),
            expected_fuzzy_ids,
        )

    def test_path_drift_on_any_call_is_rejected(self) -> None:
        executor = _FakeExecutor(_FTS5, drift_at_call=5)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )
        executor = _FakeExecutor(_FTS5, drift_at_call=150)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_failed_call_during_warmup_and_measurement_is_rejected(self) -> None:
        executor = _FakeExecutor(_FTS5, fail_at_call=50)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )
        executor = _FakeExecutor(_FTS5, fail_at_call=300)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_fuzzy_config_drift_is_rejected(self) -> None:
        executor = _FakeExecutor(_FTS5, echo_wrong_minimum_similarity=True)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )
        executor = _FakeExecutor(_FTS5, echo_wrong_top_k=True)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_zero_exact_result_and_excessive_fuzzy_result_rejected(self) -> None:
        executor = _FakeExecutor(_FTS5, exact_result_count=0)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )
        executor = _FakeExecutor(_FTS5, fuzzy_result_count=11)
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=executor,
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_cohort_digest_mismatch_is_rejected(self) -> None:
        queries = tuple(
            iter_exact_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                cohort_count=_CONTRACT.exact_cohort_count,
            )
        )
        modified = (replace(queries[0], query_raw="body-marker"),) + queries[1:]
        with self.assertRaises(ValueError) as caught:
            measure_path_latency(
                contract=_CONTRACT,
                requested_path=_FTS5,
                executor=_FakeExecutor(_FTS5),
                clock=_RecordingClock(_deltas(2880)),
                clock_name="test-clock-v1",
                environment=_environment("true"),
                exact_queries=modified,
            )
        self.assertIn("exact cohort digest", str(caught.exception))
        self.assertNotIn("body-marker", str(caught.exception))

    def test_cohort_count_mismatch_is_rejected(self) -> None:
        fuzzy_queries = tuple(
            iter_fuzzy_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                cohort_count=_CONTRACT.fuzzy_cohort_count,
            )
        )[:-1]
        with self.assertRaises(ValueError) as caught:
            measure_path_latency(
                contract=_CONTRACT,
                requested_path=_FTS5,
                executor=_FakeExecutor(_FTS5),
                clock=_RecordingClock(_deltas(2880)),
                clock_name="test-clock-v1",
                environment=_environment("true"),
                fuzzy_queries=fuzzy_queries,
            )
        self.assertIn("fuzzy cohort count", str(caught.exception))

    def test_environment_path_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=_environment("false"),
                executor=_FakeExecutor(_FTS5),
                clock=_RecordingClock(_deltas(2880)),
            )
        with self.assertRaises(ValueError):
            self._run(
                path=_FALLBACK,
                environment=_environment("true"),
                executor=_FakeExecutor(_FALLBACK),
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_environment_config_mismatch_is_rejected(self) -> None:
        facts = dict(_environment("true"))
        facts["warmup_queries_per_cohort"] = "999"
        with self.assertRaises(ValueError) as caught:
            self._run(
                path=_FTS5,
                environment=tuple(sorted(facts.items())),
                executor=_FakeExecutor(_FTS5),
                clock=_RecordingClock(_deltas(2880)),
            )
        self.assertIn("warmup_queries_per_cohort", str(caught.exception))

    def test_unsorted_environment_is_rejected(self) -> None:
        unsorted_env = tuple(reversed(_environment("true")))
        with self.assertRaises(ValueError):
            self._run(
                path=_FTS5,
                environment=unsorted_env,
                executor=_FakeExecutor(_FTS5),
                clock=_RecordingClock(_deltas(2880)),
            )

    def test_production_default_clock_is_perf_counter_ns(self) -> None:
        parameter = inspect.signature(measure_path_latency).parameters["clock"]
        self.assertIs(parameter.default, time.perf_counter_ns)

    def test_collected_environment_is_stable_sorted_and_complete(self) -> None:
        environment = collect_benchmark_environment()
        keys = tuple(key for key, _ in environment)
        self.assertEqual(keys, tuple(sorted(keys)))
        required = {
            "cpu",
            "fts5_enabled",
            "os",
            "python_version",
            "ram_mib",
            "sqlite_version",
            "unicode_version",
        }
        self.assertTrue(required.issubset(set(keys)))
        facts = dict(environment)
        self.assertIn(facts["fts5_enabled"], ("true", "false"))
        self.assertEqual(facts["timing_clock"], "perf_counter_ns")
        self.assertEqual(facts["percentile_method"], "nearest-rank")
        self.assertEqual(facts["warmup_queries_per_cohort"], "100")
        self.assertEqual(facts["measured_repeats"], "1")
        self.assertEqual(
            benchmark_environment_digest(environment),
            benchmark_environment_digest(environment),
        )

    def test_production_defaults_end_to_end_on_matching_path(self) -> None:
        environment = collect_benchmark_environment()
        facts = dict(environment)
        path = _FTS5 if facts["fts5_enabled"] == "true" else _FALLBACK
        evidence = measure_path_latency(
            contract=_CONTRACT,
            requested_path=path,
            executor=_FakeExecutor(path),
        )
        self.assertIs(evidence.execution_path, path)
        self.assertEqual(evidence.environment_digest, benchmark_environment_digest(environment))
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )
        validate_environment_for_path(evidence.environment, path)

    def test_no_query_bodies_in_evidence_or_diagnostics(self) -> None:
        first_exact = next(
            iter_exact_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                cohort_count=_CONTRACT.exact_cohort_count,
            )
        )
        marker = first_exact.query_raw
        deltas = _deltas(2880)
        clock = _RecordingClock(deltas)
        executor = _FakeExecutor(_FTS5)
        evidence = self._run(
            path=_FTS5,
            environment=_environment("true"),
            executor=executor,
            clock=clock,
        )
        self.assertNotIn(marker, repr(evidence))
        drifting = _FakeExecutor(_FTS5, drift_at_call=1)
        with self.assertRaises(ValueError) as caught:
            self._run(
                path=_FTS5,
                environment=_environment("true"),
                executor=drifting,
                clock=_RecordingClock(_deltas(2880)),
            )
        self.assertIn("path drift", str(caught.exception))
        self.assertNotIn(marker, str(caught.exception))


class LatencyModuleBoundaryTests(unittest.TestCase):
    def test_latency_owner_imports_only_stdlib_and_frozen_owners(self) -> None:
        from tm_benchmark_latency import __file__ as owner_file

        source = Path(owner_file).read_text(encoding="utf-8")
        imported = {
            match.group(1).split(".")[0]
            for match in _IMPORT_RE.finditer(source)
        }
        self.assertTrue(imported)
        stdlib = set(sys.stdlib_module_names)
        for module in sorted(imported):
            self.assertTrue(
                module in stdlib
                or module in ("tm_contracts", "tm_benchmark"),
                f"unexpected import: {module}",
            )

    def test_importing_latency_owner_loads_no_runtime_modules(self) -> None:
        banned = ", ".join(
            repr(name) for name in sorted(_BANNED_RUNTIME_MODULES)
        )
        code = (
            "import sys\n"
            "import tm_benchmark_latency\n"
            f"banned = {{{banned}}}\n"
            "loaded = {m.split('.')[0] for m in sys.modules}\n"
            "assert not (loaded & banned), sorted(loaded & banned)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_no_runtime_reverse_imports_of_latency_owner(self) -> None:
        code = (
            "import sys\n"
            "import tm_contracts\n"
            "import tm_similarity\n"
            "import text_matcher\n"
            "import tm_sqlite_store\n"
            "import tm_retrieval_capability\n"
            "assert 'tm_benchmark_latency' not in sys.modules\n"
            "assert 'tm_benchmark' not in sys.modules\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
