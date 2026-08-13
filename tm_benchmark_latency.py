"""Exact/fuzzy per-query latency evidence owner for benchmark-v1 (Task 8.2).

Ownership
---------
This module owns benchmark-v1 per-query latency evidence for the exact lookup
and fuzzy top-10 execution paths: immutable raw samples plus independently
recomputable nearest-rank statistics.  It is an offline validation/batch owner
only; no production runtime module imports it, and it never constructs a
``BenchmarkReport`` or grants any capability (Task 8.5 owns reports and Gate D).

Boundaries
----------
- Consumes Task 8.1's frozen iterators and strict ``BenchmarkContract``
  (``tm_benchmark`` / ``tm_contracts``).  It never selects, drops, or reorders
  favorable measured samples; contract exact/fuzzy counts are exact required
  counts and ``measured_repeats=1`` with ``warmup_queries_per_cohort=100``.
- Defines a narrow executor seam (``LatencyExecutor``) that Task 8.5 binds to
  real canonical store/retrieval execution.  This module imports no store,
  retrieval, capability, migration, or matcher module.
- Returns frozen path-specific raw latency evidence (``LatencyEvidence``), not
  a report and not a pass/fail capability.
- Evidence and diagnostics never contain query, source, or target text; raw
  samples carry only stable query ids and measured facts.

Measurement contract
--------------------
- ``warmup_queries_per_cohort`` (100) untimed warmup calls per cohort.
- ``measured_repeats`` (1): every cohort query is measured exactly once, in
  frozen order; each call is timed individually with ``perf_counter_ns``
  (never a whole-batch average).
- Integer nearest-rank percentiles: rank = ceil(p * n) (equivalently
  max(1, ceil)), index = rank - 1.  p50/p95/max are recomputed from the raw
  ``elapsed_ns`` samples.
- Fuzzy executor calls are frozen to the contract's ``minimum_similarity`` and
  ``top_k`` and must echo them back on every call.
- Every call must report the requested ``BenchmarkExecutionPath``; a fast-path
  success never stands in for the fallback path.

Failure semantics
-----------------
The runner and evidence construction fail closed: empty samples, bool-as-int,
negative or non-built-in scalar values, scalar/Enum subclasses, duplicate/
missing/out-of-order query ids, wrong cohort/count/path, failed calls, wrong
fuzzy configuration, inconsistent derived statistics, and any contract,
digest, or environment mismatch are rejected before any evidence is returned.

Environment
-----------
``collect_benchmark_environment`` inspects local runtime facts only (CPU, OS,
Python/SQLite/Unicode versions, RAM, FTS5 availability) plus the timing
clock/statistic/cohort configuration, and returns a stable sorted tuple that
``benchmark_environment_digest`` accepts.  FTS5_TRIGRAM evidence requires
``fts5_enabled=true``; GRAM_FALLBACK evidence requires ``fts5_enabled=false``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import json
import math
import os
import platform
import re
import sqlite3
import time
from typing import Protocol
import unicodedata

from tm_benchmark import (
    BenchmarkQuery,
    benchmark_digest,
    iter_exact_queries,
    iter_fuzzy_queries,
)
from tm_contracts import (
    BENCHMARK_PERCENTILE_METHOD,
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    contract_from_json,
    contract_to_json,
)

LATENCY_EVIDENCE_SCHEMA_VERSION = "tm-benchmark-latency-evidence-v1"
DEFAULT_TIMING_CLOCK_NAME = "perf_counter_ns"

_COHORTS = ("exact", "fuzzy")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _require_identity(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_builtin_int(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a built-in int")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _require_builtin_float(value: object, field_name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{field_name} must be a built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class LatencySample:
    """One immutable per-query raw latency sample.

    Carries stable ``query_id``, ``elapsed_ns``, ``cohort``, the actual
    execution path, and a success/result-count fact.  Query, source, and
    target bodies are never stored.
    """

    query_id: int
    elapsed_ns: int
    cohort: str
    actual_path: BenchmarkExecutionPath
    succeeded: bool
    result_count: int
    minimum_similarity: float | None = None
    top_k: int | None = None

    def __post_init__(self) -> None:
        _require_builtin_int(self.query_id, "query id", minimum=1)
        _require_builtin_int(self.elapsed_ns, "elapsed nanoseconds", minimum=0)
        if type(self.cohort) is not str or self.cohort not in _COHORTS:
            raise ValueError(f"unknown sample cohort: {self.cohort!r}")
        if type(self.actual_path) is not BenchmarkExecutionPath:
            raise TypeError("sample actual path must be BenchmarkExecutionPath")
        if type(self.succeeded) is not bool:
            raise TypeError("sample succeeded must be a built-in bool")
        _require_builtin_int(self.result_count, "result count", minimum=0)
        if (self.minimum_similarity is None) != (self.top_k is None):
            raise ValueError(
                "fuzzy configuration must be set or absent together"
            )
        if self.minimum_similarity is not None:
            _require_builtin_float(
                self.minimum_similarity,
                "minimum similarity",
            )
            _require_builtin_int(self.top_k, "top_k", minimum=1)


def nearest_rank_percentile(
    elapsed_ns: tuple[int, ...],
    percentile: float,
) -> int:
    """Integer nearest-rank percentile over raw ``elapsed_ns`` samples.

    rank = ceil(percentile * n), clamped to at least 1; the result is the
    sample at zero-based index ``rank - 1`` of the sorted values.  Never
    interpolates and never averages batches.
    """
    _require_builtin_float(percentile, "percentile")
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be in (0.0, 1.0]")
    ordered = _sorted_elapsed_ns(elapsed_ns)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _sorted_elapsed_ns(elapsed_ns: tuple[int, ...]) -> tuple[int, ...]:
    if not elapsed_ns:
        raise ValueError("cannot compute statistics over empty samples")
    validated = tuple(
        _require_builtin_int(value, "elapsed nanoseconds", minimum=0)
        for value in elapsed_ns
    )
    return tuple(sorted(validated))


def recompute_cohort_statistics(
    elapsed_ns: tuple[int, ...],
) -> tuple[int, int, int]:
    """Independently recompute ``(p50, p95, max)`` from raw samples."""
    ordered = _sorted_elapsed_ns(elapsed_ns)
    p50 = ordered[max(1, math.ceil(0.5 * len(ordered))) - 1]
    p95 = ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]
    return p50, p95, ordered[-1]


def _fts5_enabled() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            row = connection.execute(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == 1


def _ram_mib() -> str:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    if pages is None or page_size is None:
        return "unknown"
    return str((pages * page_size) // (1024 * 1024))


def collect_benchmark_environment(
    *,
    timing_clock: str = DEFAULT_TIMING_CLOCK_NAME,
    percentile_method: str = BENCHMARK_PERCENTILE_METHOD,
    warmup_queries_per_cohort: int = 100,
    measured_repeats: int = 1,
) -> tuple[tuple[str, str], ...]:
    """Collect stable local-runtime facts plus timing/statistic/cohort config.

    Only inspects the local runtime; no network, no telemetry.  The returned
    tuple is sorted by key and accepted by ``benchmark_environment_digest``.
    """
    _require_identity(timing_clock, "timing clock")
    _require_identity(percentile_method, "percentile method")
    _require_builtin_int(
        warmup_queries_per_cohort,
        "warmup queries per cohort",
        minimum=0,
    )
    _require_builtin_int(measured_repeats, "measured repeats", minimum=1)
    environment = {
        "cpu": platform.processor() or platform.machine() or "unknown",
        "fts5_enabled": "true" if _fts5_enabled() else "false",
        "os": platform.system() or "unknown",
        "python_version": platform.python_version(),
        "ram_mib": _ram_mib(),
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_version": unicodedata.unidata_version,
        "timing_clock": timing_clock,
        "percentile_method": percentile_method,
        "warmup_queries_per_cohort": str(warmup_queries_per_cohort),
        "measured_repeats": str(measured_repeats),
    }
    return tuple(sorted(environment.items()))


def validate_environment_for_path(
    environment: tuple[tuple[str, str], ...],
    requested_path: BenchmarkExecutionPath,
) -> None:
    """Fail closed unless the environment matches the requested path."""
    benchmark_environment_digest(environment)
    if not isinstance(requested_path, BenchmarkExecutionPath):
        raise TypeError("requested path must be BenchmarkExecutionPath")
    fts5_enabled = dict(environment).get("fts5_enabled")
    if fts5_enabled not in ("true", "false"):
        raise ValueError("environment fts5_enabled must be 'true' or 'false'")
    if (
        requested_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        and fts5_enabled != "true"
    ):
        raise ValueError(
            "FTS5_TRIGRAM evidence requires environment fts5_enabled=true"
        )
    if (
        requested_path is BenchmarkExecutionPath.GRAM_FALLBACK
        and fts5_enabled != "false"
    ):
        raise ValueError(
            "GRAM_FALLBACK evidence requires environment fts5_enabled=false"
        )


class ExactExecutionOutcome(Protocol):
    """Snapshot-validated facts of one exact lookup call (read-only)."""

    @property
    def actual_path(self) -> BenchmarkExecutionPath: ...

    @property
    def succeeded(self) -> bool: ...

    @property
    def result_count(self) -> int: ...


class FuzzyExecutionOutcome(Protocol):
    """Snapshot-validated facts of one fuzzy top-k call (read-only)."""

    @property
    def actual_path(self) -> BenchmarkExecutionPath: ...

    @property
    def succeeded(self) -> bool: ...

    @property
    def result_count(self) -> int: ...

    @property
    def minimum_similarity(self) -> float: ...

    @property
    def top_k(self) -> int: ...


class LatencyExecutor(Protocol):
    """Narrow callable seam that Task 8.5 binds to real execution.

    Each call executes exactly one query on the requested path and reports the
    actual execution path plus a success/result-count fact.  The runner
    validates every call (warmup and measured); a fast-path success never
    stands in for the fallback path.
    """

    def exact_lookup(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
    ) -> ExactExecutionOutcome: ...

    def fuzzy_top_k(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
        minimum_similarity: float,
        top_k: int,
    ) -> FuzzyExecutionOutcome: ...


def _outcome_attr(outcome: object, name: str) -> object:
    if outcome is None:
        raise TypeError(f"executor outcome must not be None")
    value = getattr(outcome, name, None)
    if value is None:
        raise TypeError(f"executor outcome must expose {name!r}")
    return value


def _snapshot_exact_outcome(
    outcome: object,
) -> tuple[BenchmarkExecutionPath, bool, int]:
    actual_path = _outcome_attr(outcome, "actual_path")
    if not isinstance(actual_path, BenchmarkExecutionPath):
        raise TypeError("exact outcome actual_path must be BenchmarkExecutionPath")
    succeeded = _outcome_attr(outcome, "succeeded")
    if type(succeeded) is not bool:
        raise TypeError("exact outcome succeeded must be a built-in bool")
    result_count = _outcome_attr(outcome, "result_count")
    result_count = _require_builtin_int(
        result_count, "exact outcome result count", minimum=0
    )
    return actual_path, succeeded, result_count


def _snapshot_fuzzy_outcome(
    outcome: object,
) -> tuple[BenchmarkExecutionPath, bool, int, float, int]:
    actual_path = _outcome_attr(outcome, "actual_path")
    if not isinstance(actual_path, BenchmarkExecutionPath):
        raise TypeError("fuzzy outcome actual_path must be BenchmarkExecutionPath")
    succeeded = _outcome_attr(outcome, "succeeded")
    if type(succeeded) is not bool:
        raise TypeError("fuzzy outcome succeeded must be a built-in bool")
    result_count = _outcome_attr(outcome, "result_count")
    result_count = _require_builtin_int(
        result_count, "fuzzy outcome result count", minimum=0
    )
    minimum_similarity = _outcome_attr(outcome, "minimum_similarity")
    minimum_similarity = _require_builtin_float(
        minimum_similarity, "fuzzy outcome minimum similarity"
    )
    top_k = _outcome_attr(outcome, "top_k")
    top_k = _require_builtin_int(top_k, "fuzzy outcome top_k", minimum=1)
    return actual_path, succeeded, result_count, minimum_similarity, top_k


def _read_clock(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int:
        raise TypeError("timing clock must return built-in int nanoseconds")
    if value < 0:
        raise ValueError("timing clock returned a negative value")
    return value


def _cohort_query_payload(query: BenchmarkQuery) -> dict[str, object]:
    """Query payload in Task 8.1's canonical cohort framing."""
    return {
        "query_id": query.query_id,
        "query_raw": query.query_raw,
        "cohort": query.cohort,
        "category": query.category,
        "reference_record_id": query.reference_record_id,
    }


def _run_measurements(
    *,
    contract: BenchmarkContract,
    requested_path: BenchmarkExecutionPath,
    executor: LatencyExecutor,
    exact_queries: tuple[BenchmarkQuery, ...],
    fuzzy_queries: tuple[BenchmarkQuery, ...],
    clock: Callable[[], int],
    minimum_similarity: float,
    top_k: int,
) -> tuple[tuple[LatencySample, ...], tuple[LatencySample, ...], int, int]:
    warmup_count = contract.warmup_queries_per_cohort
    if warmup_count > len(exact_queries) or warmup_count > len(fuzzy_queries):
        raise ValueError("warmup count exceeds cohort query count")

    exact_samples: list[LatencySample] = []
    for query in exact_queries[:warmup_count]:
        _execute_exact(executor, query, requested_path)
    for query in exact_queries:
        before = _read_clock(clock)
        outcome = _execute_exact(executor, query, requested_path)
        after = _read_clock(clock)
        exact_samples.append(
            _sample_from_exact(query, before, after, outcome)
        )

    fuzzy_samples: list[LatencySample] = []
    for query in fuzzy_queries[:warmup_count]:
        _execute_fuzzy(executor, query, requested_path, minimum_similarity, top_k)
    for query in fuzzy_queries:
        before = _read_clock(clock)
        outcome = _execute_fuzzy(
            executor,
            query,
            requested_path,
            minimum_similarity,
            top_k,
        )
        after = _read_clock(clock)
        fuzzy_samples.append(
            _sample_from_fuzzy(query, before, after, outcome)
        )

    return (
        tuple(exact_samples),
        tuple(fuzzy_samples),
        warmup_count,
        warmup_count,
    )


def _execute_exact(
    executor: LatencyExecutor,
    query: BenchmarkQuery,
    requested_path: BenchmarkExecutionPath,
) -> tuple[BenchmarkExecutionPath, bool, int]:
    outcome = executor.exact_lookup(
        query.query_raw,
        requested_path=requested_path,
    )
    actual_path, succeeded, result_count = _snapshot_exact_outcome(outcome)
    if actual_path is not requested_path:
        raise ValueError(
            f"executor path drift on exact query {query.query_id}"
        )
    if not succeeded:
        raise ValueError(f"executor failed exact query {query.query_id}")
    if result_count < 1:
        raise ValueError(
            f"exact query {query.query_id} must return at least one record"
        )
    return actual_path, succeeded, result_count


def _execute_fuzzy(
    executor: LatencyExecutor,
    query: BenchmarkQuery,
    requested_path: BenchmarkExecutionPath,
    minimum_similarity: float,
    top_k: int,
) -> tuple[BenchmarkExecutionPath, bool, int, float, int]:
    outcome = executor.fuzzy_top_k(
        query.query_raw,
        requested_path=requested_path,
        minimum_similarity=minimum_similarity,
        top_k=top_k,
    )
    (
        actual_path,
        succeeded,
        result_count,
        echoed_minimum_similarity,
        echoed_top_k,
    ) = _snapshot_fuzzy_outcome(outcome)
    if actual_path is not requested_path:
        raise ValueError(
            f"executor path drift on fuzzy query {query.query_id}"
        )
    if not succeeded:
        raise ValueError(f"executor failed fuzzy query {query.query_id}")
    if echoed_minimum_similarity != minimum_similarity:
        raise ValueError(
            f"executor echoed wrong minimum similarity on fuzzy query "
            f"{query.query_id}"
        )
    if echoed_top_k != top_k:
        raise ValueError(
            f"executor echoed wrong top_k on fuzzy query {query.query_id}"
        )
    if result_count > top_k:
        raise ValueError(
            f"fuzzy query {query.query_id} result count exceeds top_k"
        )
    return actual_path, succeeded, result_count, echoed_minimum_similarity, echoed_top_k


def _sample_from_exact(
    query: BenchmarkQuery,
    before: int,
    after: int,
    outcome: tuple[BenchmarkExecutionPath, bool, int],
) -> LatencySample:
    actual_path, succeeded, result_count = outcome
    return LatencySample(
        query_id=query.query_id,
        elapsed_ns=after - before,
        cohort="exact",
        actual_path=actual_path,
        succeeded=succeeded,
        result_count=result_count,
    )


def _sample_from_fuzzy(
    query: BenchmarkQuery,
    before: int,
    after: int,
    outcome: tuple[BenchmarkExecutionPath, bool, int, float, int],
) -> LatencySample:
    (
        actual_path,
        succeeded,
        result_count,
        minimum_similarity,
        top_k,
    ) = outcome
    return LatencySample(
        query_id=query.query_id,
        elapsed_ns=after - before,
        cohort="fuzzy",
        actual_path=actual_path,
        succeeded=succeeded,
        result_count=result_count,
        minimum_similarity=minimum_similarity,
        top_k=top_k,
    )


@dataclass(frozen=True)
class LatencyEvidence:
    """Frozen path-specific raw latency evidence for one execution path.

    Never a ``BenchmarkReport`` and never a pass/fail capability.  ``p50/p95/
    max`` are validated against recomputation from the raw samples, the
    environment digest is validated against the strict codec, and
    ``evidence_digest`` is always derived from the other fields at
    construction time.
    """

    schema_version: str
    contract: BenchmarkContract
    contract_digest: str
    exact_cohort_digest: str
    fuzzy_cohort_digest: str
    execution_path: BenchmarkExecutionPath
    path_config_digest: str
    warmup_queries_per_cohort: int
    measured_repeats: int
    percentile_method: str
    timing_clock: str
    minimum_similarity: float
    top_k: int
    exact_samples: tuple[LatencySample, ...]
    fuzzy_samples: tuple[LatencySample, ...]
    exact_sample_count: int
    fuzzy_sample_count: int
    exact_p50_ns: int
    exact_p95_ns: int
    exact_max_ns: int
    fuzzy_p50_ns: int
    fuzzy_p95_ns: int
    fuzzy_max_ns: int
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != LATENCY_EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                "evidence schema version must be "
                f"{LATENCY_EVIDENCE_SCHEMA_VERSION}"
            )
        if type(self.contract) is not BenchmarkContract:
            raise TypeError("evidence contract must be BenchmarkContract")
        contract_snapshot = contract_from_json(contract_to_json(self.contract))
        if type(contract_snapshot) is not BenchmarkContract:
            raise TypeError("evidence contract snapshot must be BenchmarkContract")
        object.__setattr__(self, "contract", contract_snapshot)
        _require_digest(self.contract_digest, "contract digest")
        if self.contract_digest != benchmark_contract_digest(contract_snapshot):
            raise ValueError("contract digest must bind evidence contract")
        _require_digest(self.exact_cohort_digest, "exact cohort digest")
        if self.exact_cohort_digest != contract_snapshot.exact_cohort_digest:
            raise ValueError("exact cohort digest must match evidence contract")
        _require_digest(self.fuzzy_cohort_digest, "fuzzy cohort digest")
        if self.fuzzy_cohort_digest != contract_snapshot.fuzzy_cohort_digest:
            raise ValueError("fuzzy cohort digest must match evidence contract")
        if type(self.execution_path) is not BenchmarkExecutionPath:
            raise TypeError("execution path must be BenchmarkExecutionPath")
        _require_digest(self.path_config_digest, "path config digest")
        expected_path_digest = (
            contract_snapshot.fast_path_config_digest
            if self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract_snapshot.fallback_path_config_digest
        )
        if self.path_config_digest != expected_path_digest:
            raise ValueError("path config digest must match evidence contract")
        _require_builtin_int(
            self.warmup_queries_per_cohort,
            "warmup queries per cohort",
            minimum=0,
        )
        _require_builtin_int(self.measured_repeats, "measured repeats", minimum=1)
        if (
            self.warmup_queries_per_cohort
            != contract_snapshot.warmup_queries_per_cohort
        ):
            raise ValueError("warmup count must match evidence contract")
        if self.measured_repeats != contract_snapshot.measured_repeats:
            raise ValueError("measured repeats must match evidence contract")
        if self.percentile_method != BENCHMARK_PERCENTILE_METHOD:
            raise ValueError(
                "percentile method must be " f"{BENCHMARK_PERCENTILE_METHOD}"
            )
        if self.percentile_method != contract_snapshot.percentile_method:
            raise ValueError("percentile method must match evidence contract")
        _require_identity(self.timing_clock, "timing clock")
        _require_builtin_float(self.minimum_similarity, "minimum similarity")
        _require_builtin_int(self.top_k, "top_k", minimum=1)
        if self.minimum_similarity != contract_snapshot.minimum_similarity:
            raise ValueError("minimum similarity must match evidence contract")
        if self.top_k != contract_snapshot.top_k:
            raise ValueError("top_k must match evidence contract")
        if type(self.exact_samples) is not tuple or not self.exact_samples:
            raise ValueError("exact samples must be a non-empty tuple")
        if type(self.fuzzy_samples) is not tuple or not self.fuzzy_samples:
            raise ValueError("fuzzy samples must be a non-empty tuple")
        for index, sample in enumerate(self.exact_samples, start=1):
            if type(sample) is not LatencySample:
                raise TypeError("exact samples must contain LatencySample")
            if sample.cohort != "exact":
                raise ValueError("exact bucket contains a non-exact sample")
            if sample.actual_path is not self.execution_path:
                raise ValueError("exact sample path drift")
            if not sample.succeeded:
                raise ValueError("exact sample records a failed call")
            if (
                sample.minimum_similarity is not None
                or sample.top_k is not None
            ):
                raise ValueError("exact sample must not carry fuzzy config")
            if sample.result_count < 1:
                raise ValueError("exact sample result count must be >= 1")
            if sample.query_id != index:
                raise ValueError(
                    "exact sample query ids must be contiguous and ordered"
                )
        for index, sample in enumerate(self.fuzzy_samples, start=1):
            if type(sample) is not LatencySample:
                raise TypeError("fuzzy samples must contain LatencySample")
            if sample.cohort != "fuzzy":
                raise ValueError("fuzzy bucket contains a non-fuzzy sample")
            if sample.actual_path is not self.execution_path:
                raise ValueError("fuzzy sample path drift")
            if not sample.succeeded:
                raise ValueError("fuzzy sample records a failed call")
            if (
                sample.minimum_similarity != self.minimum_similarity
                or sample.top_k != self.top_k
            ):
                raise ValueError("fuzzy sample config must match evidence")
            if sample.result_count > self.top_k:
                raise ValueError("fuzzy sample result count exceeds top_k")
            if sample.query_id != index:
                raise ValueError(
                    "fuzzy sample query ids must be contiguous and ordered"
                )
        if self.exact_sample_count != len(self.exact_samples):
            raise ValueError("exact sample count must equal len(exact samples)")
        if self.fuzzy_sample_count != len(self.fuzzy_samples):
            raise ValueError("fuzzy sample count must equal len(fuzzy samples)")
        if self.exact_sample_count != contract_snapshot.exact_cohort_count:
            raise ValueError("exact sample count must match evidence contract")
        if self.fuzzy_sample_count != contract_snapshot.fuzzy_cohort_count:
            raise ValueError("fuzzy sample count must match evidence contract")
        expected_exact = recompute_cohort_statistics(
            tuple(sample.elapsed_ns for sample in self.exact_samples)
        )
        if (self.exact_p50_ns, self.exact_p95_ns, self.exact_max_ns) != (
            expected_exact
        ):
            raise ValueError(
                "exact statistics do not match raw exact samples"
            )
        expected_fuzzy = recompute_cohort_statistics(
            tuple(sample.elapsed_ns for sample in self.fuzzy_samples)
        )
        if (self.fuzzy_p50_ns, self.fuzzy_p95_ns, self.fuzzy_max_ns) != (
            expected_fuzzy
        ):
            raise ValueError(
                "fuzzy statistics do not match raw fuzzy samples"
            )
        validate_environment_for_path(self.environment, self.execution_path)
        _validate_environment_config(
            self.environment,
            timing_clock=self.timing_clock,
            percentile_method=self.percentile_method,
            warmup_queries_per_cohort=self.warmup_queries_per_cohort,
            measured_repeats=self.measured_repeats,
        )
        if self.environment_digest != benchmark_environment_digest(
            self.environment
        ):
            raise ValueError("environment digest does not match environment")
        object.__setattr__(self, "evidence_digest", latency_evidence_digest(self))

    def recompute_statistics(self) -> tuple[int, int, int, int, int, int]:
        """Recompute (exact p50, p95, max, fuzzy p50, p95, max) from raw."""
        exact_stats = recompute_cohort_statistics(
            tuple(sample.elapsed_ns for sample in self.exact_samples)
        )
        fuzzy_stats = recompute_cohort_statistics(
            tuple(sample.elapsed_ns for sample in self.fuzzy_samples)
        )
        return (
            exact_stats[0],
            exact_stats[1],
            exact_stats[2],
            fuzzy_stats[0],
            fuzzy_stats[1],
            fuzzy_stats[2],
        )

    def recompute_environment_digest(self) -> str:
        """Independently recompute the strict environment digest."""
        return benchmark_environment_digest(self.environment)

    def recompute_evidence_digest(self) -> str:
        """Independently recompute the canonical evidence digest."""
        return latency_evidence_digest(self)


def _sample_payload(sample: LatencySample) -> dict[str, object]:
    return {
        "query_id": sample.query_id,
        "elapsed_ns": sample.elapsed_ns,
        "cohort": sample.cohort,
        "actual_path": sample.actual_path.value,
        "succeeded": sample.succeeded,
        "result_count": sample.result_count,
        "minimum_similarity": sample.minimum_similarity,
        "top_k": sample.top_k,
    }


def latency_evidence_digest(evidence: LatencyEvidence) -> str:
    """Canonical digest over every evidence fact except the digest itself.

    Uses Task 8.1's versioned line-framed digest so the value is independently
    recomputable from the recorded facts.
    """
    if type(evidence) is not LatencyEvidence:
        raise TypeError("evidence must be LatencyEvidence")
    items: list[dict[str, object]] = [
        {
            "contract_digest": evidence.contract_digest,
            "exact_cohort_digest": evidence.exact_cohort_digest,
            "fuzzy_cohort_digest": evidence.fuzzy_cohort_digest,
            "execution_path": evidence.execution_path.value,
            "path_config_digest": evidence.path_config_digest,
            "warmup_queries_per_cohort": (
                evidence.warmup_queries_per_cohort
            ),
            "measured_repeats": evidence.measured_repeats,
            "percentile_method": evidence.percentile_method,
            "timing_clock": evidence.timing_clock,
            "minimum_similarity": evidence.minimum_similarity,
            "top_k": evidence.top_k,
            "environment_digest": evidence.environment_digest,
            "exact_sample_count": evidence.exact_sample_count,
            "fuzzy_sample_count": evidence.fuzzy_sample_count,
            "exact_p50_ns": evidence.exact_p50_ns,
            "exact_p95_ns": evidence.exact_p95_ns,
            "exact_max_ns": evidence.exact_max_ns,
            "fuzzy_p50_ns": evidence.fuzzy_p50_ns,
            "fuzzy_p95_ns": evidence.fuzzy_p95_ns,
            "fuzzy_max_ns": evidence.fuzzy_max_ns,
        }
    ]
    items.extend(_sample_payload(sample) for sample in evidence.exact_samples)
    items.extend(_sample_payload(sample) for sample in evidence.fuzzy_samples)
    return benchmark_digest(
        LATENCY_EVIDENCE_SCHEMA_VERSION,
        "latency-evidence",
        items,
    )



# --- Strict public raw-evidence codec (Task 8.5A query-process bridge) ------

_LATENCY_EVIDENCE_PAYLOAD_FIELDS = frozenset(
    {
        "contract_digest",
        "contract_json",
        "environment",
        "environment_digest",
        "evidence_digest",
        "exact_cohort_digest",
        "exact_max_ns",
        "exact_p50_ns",
        "exact_p95_ns",
        "exact_sample_count",
        "exact_samples",
        "execution_path",
        "fuzzy_cohort_digest",
        "fuzzy_max_ns",
        "fuzzy_p50_ns",
        "fuzzy_p95_ns",
        "fuzzy_sample_count",
        "fuzzy_samples",
        "measured_repeats",
        "minimum_similarity",
        "path_config_digest",
        "percentile_method",
        "schema_version",
        "timing_clock",
        "top_k",
        "warmup_queries_per_cohort",
    }
)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_strict_json(raw: str) -> dict[str, object]:
    """Parse one strict JSON object rejecting duplicate keys and non-finite."""

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def parse_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(
                f"non-finite JSON number is not allowed: {value}"
            )
        return number

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_non_finite,
            parse_float=parse_float,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("payload is not strict JSON") from None
    if type(parsed) is not dict:
        raise ValueError("payload must be a JSON object")
    return parsed


def _strict_fields(
    payload: Mapping[str, object],
    expected_keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        raise TypeError(f"{label} must be a built-in dict")
    keys = set(payload)
    if keys != expected_keys:
        missing = sorted(expected_keys - keys)
        unknown = sorted(keys - expected_keys)
        raise ValueError(
            f"{label} has missing fields {missing!r} and unknown fields "
            f"{unknown!r}"
        )
    return dict(payload)


def _as_float(value: object, field_name: str) -> float:
    return _require_builtin_float(value, field_name)


def _latency_environment_payload(
    environment: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in environment]


def _latency_environment_from_payload(
    value: object,
) -> tuple[tuple[str, str], ...]:
    if type(value) is not list:
        raise TypeError("environment must be a JSON list")
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if type(entry) is not list or len(entry) != 2:
            raise TypeError("environment entries must be two-item lists")
        key = _require_identity(entry[0], "environment key")
        value_text = _require_identity(entry[1], "environment value")
        pairs.append((key, value_text))
    return tuple(pairs)



def _reject_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in bool")
    return value


def _latency_sample_from_payload(
    value: object,
    label: str,
) -> LatencySample:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    fields = _strict_fields(
        value,
        frozenset(
            {
                "actual_path",
                "cohort",
                "elapsed_ns",
                "minimum_similarity",
                "query_id",
                "result_count",
                "succeeded",
                "top_k",
            }
        ),
        label,
    )
    try:
        actual_path = BenchmarkExecutionPath(
            _require_identity(fields["actual_path"], "sample actual path")
        )
    except ValueError as error:
        raise ValueError("sample actual path is invalid") from error
    minimum_similarity = fields["minimum_similarity"]
    top_k = fields["top_k"]
    if minimum_similarity is None:
        if top_k is not None:
            raise ValueError("fuzzy configuration must be set or absent together")
    else:
        minimum_similarity = _as_float(
            minimum_similarity,
            "sample minimum similarity",
        )
        top_k = _require_builtin_int(top_k, "sample top_k", minimum=1)
    return LatencySample(
        query_id=_require_builtin_int(
            fields["query_id"],
            "sample query id",
            minimum=1,
        ),
        elapsed_ns=_require_builtin_int(
            fields["elapsed_ns"],
            "sample elapsed nanoseconds",
            minimum=0,
        ),
        cohort=_require_identity(fields["cohort"], "sample cohort"),
        actual_path=actual_path,
        succeeded=_reject_bool(fields["succeeded"], "sample succeeded"),
        result_count=_require_builtin_int(
            fields["result_count"],
            "sample result count",
            minimum=0,
        ),
        minimum_similarity=minimum_similarity,
        top_k=top_k,
    )


def latency_evidence_to_payload(
    evidence: LatencyEvidence,
) -> dict[str, object]:
    """Strict public payload snapshot of one latency evidence value."""
    if type(evidence) is not LatencyEvidence:
        raise TypeError("evidence must be LatencyEvidence")
    return {
        "schema_version": evidence.schema_version,
        "contract_json": contract_to_json(evidence.contract),
        "contract_digest": evidence.contract_digest,
        "exact_cohort_digest": evidence.exact_cohort_digest,
        "fuzzy_cohort_digest": evidence.fuzzy_cohort_digest,
        "execution_path": evidence.execution_path.value,
        "path_config_digest": evidence.path_config_digest,
        "warmup_queries_per_cohort": evidence.warmup_queries_per_cohort,
        "measured_repeats": evidence.measured_repeats,
        "percentile_method": evidence.percentile_method,
        "timing_clock": evidence.timing_clock,
        "minimum_similarity": evidence.minimum_similarity,
        "top_k": evidence.top_k,
        "exact_samples": [
            _sample_payload(sample) for sample in evidence.exact_samples
        ],
        "fuzzy_samples": [
            _sample_payload(sample) for sample in evidence.fuzzy_samples
        ],
        "exact_sample_count": evidence.exact_sample_count,
        "fuzzy_sample_count": evidence.fuzzy_sample_count,
        "exact_p50_ns": evidence.exact_p50_ns,
        "exact_p95_ns": evidence.exact_p95_ns,
        "exact_max_ns": evidence.exact_max_ns,
        "fuzzy_p50_ns": evidence.fuzzy_p50_ns,
        "fuzzy_p95_ns": evidence.fuzzy_p95_ns,
        "fuzzy_max_ns": evidence.fuzzy_max_ns,
        "environment": _latency_environment_payload(evidence.environment),
        "environment_digest": evidence.environment_digest,
        "evidence_digest": evidence.evidence_digest,
    }


def latency_evidence_from_payload(
    payload: Mapping[str, object],
) -> LatencyEvidence:
    """Strictly reconstruct a self-validating latency evidence from a payload.

    The payload's ``evidence_digest`` is never trusted: it must equal the
    digest recomputed from the reconstructed evidence.
    """
    fields = _strict_fields(
        payload,
        _LATENCY_EVIDENCE_PAYLOAD_FIELDS,
        "latency evidence payload",
    )
    if _require_identity(
        fields["schema_version"],
        "schema version",
    ) != LATENCY_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            "schema version must be " f"{LATENCY_EVIDENCE_SCHEMA_VERSION}"
        )
    contract_json = _require_identity(
        fields["contract_json"],
        "contract json",
    )
    parsed_contract_json = _parse_strict_json(contract_json)
    contract = contract_from_json(_canonical_json(parsed_contract_json))
    if type(contract) is not BenchmarkContract:
        raise TypeError("latency evidence contract must be BenchmarkContract")
    try:
        execution_path = BenchmarkExecutionPath(
            _require_identity(
                fields["execution_path"],
                "execution path",
            )
        )
    except ValueError as error:
        raise ValueError("execution path is invalid") from error
    exact_samples_value = fields["exact_samples"]
    if type(exact_samples_value) is not list:
        raise TypeError("exact samples must be a JSON list")
    exact_samples = tuple(
        _latency_sample_from_payload(sample, "exact sample")
        for sample in exact_samples_value
    )
    fuzzy_samples_value = fields["fuzzy_samples"]
    if type(fuzzy_samples_value) is not list:
        raise TypeError("fuzzy samples must be a JSON list")
    fuzzy_samples = tuple(
        _latency_sample_from_payload(sample, "fuzzy sample")
        for sample in fuzzy_samples_value
    )
    evidence = LatencyEvidence(
        schema_version=LATENCY_EVIDENCE_SCHEMA_VERSION,
        contract=contract,
        contract_digest=_require_digest(
            fields["contract_digest"],
            "contract digest",
        ),
        exact_cohort_digest=_require_digest(
            fields["exact_cohort_digest"],
            "exact cohort digest",
        ),
        fuzzy_cohort_digest=_require_digest(
            fields["fuzzy_cohort_digest"],
            "fuzzy cohort digest",
        ),
        execution_path=execution_path,
        path_config_digest=_require_digest(
            fields["path_config_digest"],
            "path config digest",
        ),
        warmup_queries_per_cohort=_require_builtin_int(
            fields["warmup_queries_per_cohort"],
            "warmup queries per cohort",
            minimum=0,
        ),
        measured_repeats=_require_builtin_int(
            fields["measured_repeats"],
            "measured repeats",
            minimum=1,
        ),
        percentile_method=_require_identity(
            fields["percentile_method"],
            "percentile method",
        ),
        timing_clock=_require_identity(fields["timing_clock"], "timing clock"),
        minimum_similarity=_as_float(
            fields["minimum_similarity"],
            "minimum similarity",
        ),
        top_k=_require_builtin_int(fields["top_k"], "top_k", minimum=1),
        exact_samples=exact_samples,
        fuzzy_samples=fuzzy_samples,
        exact_sample_count=_require_builtin_int(
            fields["exact_sample_count"],
            "exact sample count",
            minimum=0,
        ),
        fuzzy_sample_count=_require_builtin_int(
            fields["fuzzy_sample_count"],
            "fuzzy sample count",
            minimum=0,
        ),
        exact_p50_ns=_require_builtin_int(
            fields["exact_p50_ns"],
            "exact p50 nanoseconds",
            minimum=0,
        ),
        exact_p95_ns=_require_builtin_int(
            fields["exact_p95_ns"],
            "exact p95 nanoseconds",
            minimum=0,
        ),
        exact_max_ns=_require_builtin_int(
            fields["exact_max_ns"],
            "exact max nanoseconds",
            minimum=0,
        ),
        fuzzy_p50_ns=_require_builtin_int(
            fields["fuzzy_p50_ns"],
            "fuzzy p50 nanoseconds",
            minimum=0,
        ),
        fuzzy_p95_ns=_require_builtin_int(
            fields["fuzzy_p95_ns"],
            "fuzzy p95 nanoseconds",
            minimum=0,
        ),
        fuzzy_max_ns=_require_builtin_int(
            fields["fuzzy_max_ns"],
            "fuzzy max nanoseconds",
            minimum=0,
        ),
        environment=_latency_environment_from_payload(fields["environment"]),
        environment_digest=_require_digest(
            fields["environment_digest"],
            "environment digest",
        ),
    )
    caller_digest = _require_digest(
        fields["evidence_digest"],
        "evidence digest",
    )
    if caller_digest != evidence.evidence_digest:
        raise ValueError(
            "evidence digest does not match the reconstructed evidence"
        )
    return evidence


def latency_evidence_to_json(evidence: LatencyEvidence) -> str:
    """Strict canonical JSON snapshot of one latency evidence value."""
    return _canonical_json(latency_evidence_to_payload(evidence))


def latency_evidence_from_json(serialized: str) -> LatencyEvidence:
    """Strictly reconstruct latency evidence from one canonical JSON object."""
    if type(serialized) is not str:
        raise TypeError("serialized latency evidence must be a string")
    return latency_evidence_from_payload(_parse_strict_json(serialized))


def measure_path_latency(
    *,
    contract: BenchmarkContract,
    requested_path: BenchmarkExecutionPath,
    executor: LatencyExecutor,
    clock: Callable[[], int] = time.perf_counter_ns,
    clock_name: str = DEFAULT_TIMING_CLOCK_NAME,
    environment: tuple[tuple[str, str], ...] | None = None,
    exact_queries: Iterable[BenchmarkQuery] | None = None,
    fuzzy_queries: Iterable[BenchmarkQuery] | None = None,
) -> LatencyEvidence:
    """Measure one execution path and return frozen raw latency evidence.

    Defaults bind Task 8.1's frozen cohort iterators and ``perf_counter_ns``.
    Every fuzzy call is frozen to ``contract.minimum_similarity``/``top_k``;
    every call's actual path is verified against ``requested_path``; any
    drift, failure, count, digest, or environment mismatch fails closed.
    """
    if not isinstance(contract, BenchmarkContract):
        raise TypeError("contract must be BenchmarkContract")
    if not isinstance(requested_path, BenchmarkExecutionPath):
        raise TypeError("requested path must be BenchmarkExecutionPath")
    if not callable(clock):
        raise TypeError("clock must be callable")
    _require_identity(clock_name, "clock name")
    contract_digest = benchmark_contract_digest(contract)
    if environment is None:
        environment = collect_benchmark_environment(
            timing_clock=clock_name,
            percentile_method=contract.percentile_method,
            warmup_queries_per_cohort=contract.warmup_queries_per_cohort,
            measured_repeats=contract.measured_repeats,
        )
    validate_environment_for_path(environment, requested_path)
    _validate_environment_config(
        environment,
        timing_clock=clock_name,
        percentile_method=contract.percentile_method,
        warmup_queries_per_cohort=contract.warmup_queries_per_cohort,
        measured_repeats=contract.measured_repeats,
    )
    environment_digest = benchmark_environment_digest(environment)

    if exact_queries is None:
        exact_queries = iter_exact_queries(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            cohort_count=contract.exact_cohort_count,
        )
    if fuzzy_queries is None:
        fuzzy_queries = iter_fuzzy_queries(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            cohort_count=contract.fuzzy_cohort_count,
        )
    exact_queries_tuple = tuple(exact_queries)
    fuzzy_queries_tuple = tuple(fuzzy_queries)
    if len(exact_queries_tuple) != contract.exact_cohort_count:
        raise ValueError("exact cohort count must equal contract count")
    if len(fuzzy_queries_tuple) != contract.fuzzy_cohort_count:
        raise ValueError("fuzzy cohort count must equal contract count")
    exact_cohort_digest = benchmark_digest(
        contract.corpus_generator_version,
        "exact-cohort",
        [_cohort_query_payload(query) for query in exact_queries_tuple],
    )
    fuzzy_cohort_digest = benchmark_digest(
        contract.corpus_generator_version,
        "fuzzy-cohort",
        [_cohort_query_payload(query) for query in fuzzy_queries_tuple],
    )
    if exact_cohort_digest != contract.exact_cohort_digest:
        raise ValueError("exact cohort digest does not match contract")
    if fuzzy_cohort_digest != contract.fuzzy_cohort_digest:
        raise ValueError("fuzzy cohort digest does not match contract")

    (
        exact_samples,
        fuzzy_samples,
        exact_warmup_calls,
        fuzzy_warmup_calls,
    ) = _run_measurements(
        contract=contract,
        requested_path=requested_path,
        executor=executor,
        exact_queries=exact_queries_tuple,
        fuzzy_queries=fuzzy_queries_tuple,
        clock=clock,
        minimum_similarity=contract.minimum_similarity,
        top_k=contract.top_k,
    )
    if exact_warmup_calls != contract.warmup_queries_per_cohort:
        raise ValueError("exact warmup count must equal contract warmup count")
    if fuzzy_warmup_calls != contract.warmup_queries_per_cohort:
        raise ValueError("fuzzy warmup count must equal contract warmup count")

    path_config_digest = (
        contract.fast_path_config_digest
        if requested_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else contract.fallback_path_config_digest
    )
    exact_stats = recompute_cohort_statistics(
        tuple(sample.elapsed_ns for sample in exact_samples)
    )
    fuzzy_stats = recompute_cohort_statistics(
        tuple(sample.elapsed_ns for sample in fuzzy_samples)
    )
    return LatencyEvidence(
        schema_version=LATENCY_EVIDENCE_SCHEMA_VERSION,
        contract=contract,
        contract_digest=contract_digest,
        exact_cohort_digest=exact_cohort_digest,
        fuzzy_cohort_digest=fuzzy_cohort_digest,
        execution_path=requested_path,
        path_config_digest=path_config_digest,
        warmup_queries_per_cohort=contract.warmup_queries_per_cohort,
        measured_repeats=contract.measured_repeats,
        percentile_method=contract.percentile_method,
        timing_clock=clock_name,
        minimum_similarity=contract.minimum_similarity,
        top_k=contract.top_k,
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
        environment_digest=environment_digest,
    )


def _validate_environment_config(
    environment: tuple[tuple[str, str], ...],
    *,
    timing_clock: str,
    percentile_method: str,
    warmup_queries_per_cohort: int,
    measured_repeats: int,
) -> None:
    facts = dict(environment)
    expected: tuple[tuple[str, str], ...] = (
        ("timing_clock", timing_clock),
        ("percentile_method", percentile_method),
        ("warmup_queries_per_cohort", str(warmup_queries_per_cohort)),
        ("measured_repeats", str(measured_repeats)),
    )
    for key, value in expected:
        if facts.get(key) != value:
            raise ValueError(f"environment {key} does not match configuration")
