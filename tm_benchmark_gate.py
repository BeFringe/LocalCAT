"""Task 8.5B gate-combination owner: combine benchmark evidence into Gate D.

Ownership
---------
This module is the benchmark-v1 gate combiner for Task 8.5B.  It consumes
parent-adjudicated migration/query evidence (``QueryProcessRunResult`` for
FTS5_TRIGRAM and GRAM_FALLBACK, in enum order) plus exact-type
``OracleRecallEvidence`` for the same two paths, re-adjudicates each
process/query pair from the retained process evidence, request digest,
artifact snapshots and nested evidence, derives one strict
``BenchmarkReport`` per execution path and one ``BenchmarkSuiteReport``,
and produces an immutable portable ``BenchmarkEvidenceBundle`` with a strict
closed-schema canonical JSON codec.  It is an offline validation/batch owner
only: no production runtime module imports it, and it never publishes or
mutates a capability manifest.  Gate D publishing in this slice is bounded
to constructing ``RetrievalBenchmarkEvidence`` values from derived reports
plus an explicit validity window; ``RetrievalCapabilityPublisher`` and Gate C
fuzzy-core facts are out of scope.

This module is Gate D combination only.  ``tm_benchmark.py``, latency,
process, query-process and oracle owners remain authoritative for their
facts; this module never re-selects cohorts, drops raw samples, recomputes
oracle obligations, or rewrites process/query/oracle digests.

Invariant capsule
-----------------
- Inputs are exact-type parent-adjudicated ``QueryProcessRunResult`` values,
  one FTS5_TRIGRAM and one GRAM_FALLBACK in enum order, plus exact-type
  ``OracleRecallEvidence`` for each path.  Standalone decoded
  ``QueryProcessEvidence``, caller-precomputed metrics, caller booleans,
  synthetic callbacks, selected subsets and one-path-only input are refused.
- Every process/query pair is re-adjudicated from the retained process
  evidence, request protocol digest, actual artifact snapshots and nested
  evidence using the Task 8.5A adjudication protocol.  Literal non-test
  100000 facts, the same frozen contract/digest/corpus/cohort/config,
  exact path/index, complete latency samples and matching process/query
  environments are required; any binding drift fails closed.
- Oracle evidence must be real final evidence on the committed contract.
  Candidate recall is derived from the raw rows' obligations (availability,
  index-kind, above-threshold and top-10 missing sets), never from
  ``recall_passed`` or caller-supplied totals.  The hard recall gate stays
  exactly 1.0; the current real truth (both paths miss 27 true top-10
  identities) must therefore fail ``CANDIDATE_RECALL`` on both paths.
- Latency ms come only from integer ns statistics in the nested
  ``LatencyEvidence`` (recomputed from the retained raw samples);
  migration seconds come from the migration child's ``migration_elapsed_ns``;
  peak RSS MiB is ``max(migration child peak_rss_bytes, query child
  query_peak_rss_bytes)``.  ``BenchmarkReport``/``BenchmarkSuiteReport`` are
  constructed so their tm_contracts validators independently recompute
  failed gates, passed and failed paths.
- The environment is combined only from exact frozen owner facts; conflicting
  shared keys fail closed, and the caller can never inject an environment.
- The portable bundle retains the full latency raw samples, immutable
  process/query/oracle facts and digests, and strict report codec results.
  It never contains absolute run-root/fixture paths, PIDs,
  inode/device/mtime, protocol digests that bind those locators, query/
  source/target bodies, or reusable handles.  Raw ``TMBenchmarkProcessEvidence``
  and ``QueryProcessRunResult`` values are never serialized.
- The bundle codec is closed-schema canonical JSON: duplicate keys,
  NaN/Infinity, bool-as-int, unknown/missing keys, digest drift, nested
  report drift, path swap/duplication, discarded samples, forged
  self-consistent caller fields and one-path-only input all fail closed.
  A strict round trip reproduces the same exact immutable value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import cast

from tm_benchmark import benchmark_digest
from tm_benchmark_latency import (
    LATENCY_EVIDENCE_SCHEMA_VERSION,
    LatencyEvidence,
    latency_evidence_from_payload,
    latency_evidence_to_payload,
)
from tm_benchmark_oracle import (
    ORACLE_EVIDENCE_SCHEMA_VERSION,
    OracleRecallEvidence,
    evidence_from_json as oracle_evidence_from_json,
    evidence_to_json as oracle_evidence_to_json,
)
from tm_benchmark_process import (
    PROCESS_EVIDENCE_SCHEMA_VERSION,
    TMBenchmarkProcessEvidence,
)
from tm_benchmark_query_process import (
    QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
    ArtifactSnapshot,
    QueryProcessError,
    QueryProcessEvidence,
    QueryProcessRunResult,
    _adjudicate_evidence_against_process_evidence,
)
from tm_contracts import (
    BENCHMARK_SUITE_VERSION,
    BenchmarkContract,
    BenchmarkExecutionPath,
    BenchmarkReport,
    BenchmarkSuiteContract,
    BenchmarkSuiteReport,
    benchmark_contract_digest,
    benchmark_environment_digest,
    benchmark_suite_contract_digest,
    contract_from_json,
    contract_to_json,
)
from tm_retrieval_capability import RetrievalBenchmarkEvidence

BENCHMARK_BUNDLE_SCHEMA_VERSION = "tm-benchmark-bundle-v1"
BENCHMARK_BUNDLE_DIGEST_VERSION = "tm-benchmark-bundle-digest-v1"
BENCHMARK_BUNDLE_DIGEST_KIND = "benchmark-bundle"
BENCHMARK_PORTABLE_ARTIFACT_KEY_VERSION = (
    "tm-benchmark-portable-artifact-key-v1"
)

REAL_CORPUS_RECORD_COUNT = 100_000
NANOSECONDS_PER_MILLISECOND = 1_000_000
NANOSECONDS_PER_SECOND = 1_000_000_000
BYTES_PER_MIB = 1024 * 1024

_BENCHMARK_GATE_ORDER = (
    "CANDIDATE_RECALL",
    "EXACT_P95",
    "FUZZY_P95",
    "MIGRATION",
    "PEAK_RSS",
)

_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

_BUNDLE_PAYLOAD_FIELDS = frozenset(
    {
        "bundle_digest",
        "contract_digest",
        "contract_json",
        "fallback",
        "fts5",
        "schema_version",
        "suite_contract_digest",
        "suite_report",
    }
)
_PATH_BUNDLE_PAYLOAD_FIELDS = frozenset(
    {
        "execution_path",
        "latency_evidence",
        "oracle_evidence_json",
        "process_facts",
        "query_facts",
        "report",
    }
)
_REPORT_PAYLOAD_FIELDS = frozenset(
    {
        "candidate_recall",
        "contract_digest",
        "contract_json",
        "corpus_composition_digest",
        "corpus_composition_version",
        "corpus_digest",
        "environment",
        "environment_digest",
        "exact_cohort_digest",
        "exact_max_ms",
        "exact_p50_ms",
        "exact_p95_ms",
        "exact_sample_count",
        "execution_path",
        "failed_gates",
        "fuzzy_cohort_digest",
        "fuzzy_sample_count",
        "fuzzy_top10_max_ms",
        "fuzzy_top10_p50_ms",
        "fuzzy_top10_p95_ms",
        "migration_seconds",
        "oracle_query_count",
        "oracle_subset_digest",
        "passed",
        "path_config_digest",
        "peak_rss_mib",
        "percentile_method",
        "scorer_config_digest",
    }
)
_SUITE_REPORT_PAYLOAD_FIELDS = frozenset(
    {
        "corpus_composition_digest",
        "corpus_composition_version",
        "failed_paths",
        "passed",
        "path_reports",
        "suite_contract_digest",
    }
)
_PROCESS_FACTS_PAYLOAD_FIELDS = frozenset(
    {
        "actual_index_kind",
        "canonical_store_id",
        "contract_digest",
        "corpus_digest",
        "corpus_record_count",
        "environment",
        "environment_digest",
        "evidence_digest",
        "execution_path",
        "fixture_digest",
        "fixture_record_count",
        "generation",
        "migration_elapsed_ns",
        "path_config_digest",
        "peak_rss_bytes",
        "record_count",
        "resource_id",
        "rss_scope",
        "rss_start_bytes",
        "rss_terminal_bytes",
        "rss_unit",
        "schema_version",
    }
)
_QUERY_FACTS_PAYLOAD_FIELDS = frozenset(
    {
        "actual_index_kind",
        "artifact_baseline_digest",
        "canonical_store_id",
        "contract_digest",
        "corpus_digest",
        "corpus_record_count",
        "environment",
        "environment_digest",
        "evidence_digest",
        "execution_path",
        "fixture_digest",
        "fixture_record_count",
        "generation",
        "path_config_digest",
        "portable_artifact_key",
        "process_evidence_digest",
        "processes_distinct",
        "query_peak_rss_bytes",
        "query_rss_scope",
        "query_rss_start_bytes",
        "query_rss_terminal_bytes",
        "query_rss_unit",
        "record_count",
        "resource_id",
        "schema_version",
        "sidecar_digest",
        "manifest_digest",
        "latency_evidence_digest",
    }
)


# --- strict JSON helpers ----------------------------------------------------


def _parse_strict_json(raw: str) -> dict[str, object]:
    """Parse canonical JSON rejecting duplicate keys and non-finite numbers."""
    if type(raw) is not str:
        raise TypeError("payload must be a string")

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite numbers are not allowed")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON keys are not strict")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("payload is not strict JSON") from error
    return cast(dict[str, object], parsed)


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_fields(
    value: object,
    allowed: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields")
    missing = allowed.difference(value)
    if missing:
        raise ValueError(f"{label} is missing required fields")
    return dict(value)


def _as_str(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _as_int(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a built-in int")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _as_float(value: object, field_name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{field_name} must be a built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite float")
    return value


def _as_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in bool")
    return value


def _as_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _as_path(value: object, field_name: str) -> BenchmarkExecutionPath:
    try:
        return BenchmarkExecutionPath(_as_str(value, field_name))
    except ValueError as error:
        raise ValueError(f"{field_name} is an unsupported execution path") from error


def _as_environment(value: object, field_name: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not list:
        raise TypeError(f"{field_name} must be a JSON list of pairs")
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if type(entry) is not list or len(entry) != 2:
            raise TypeError(f"{field_name} entries must be two-item lists")
        key = _as_str(entry[0], f"{field_name} key")
        item_value = _as_str(entry[1], f"{field_name} value")
        pairs.append((key, item_value))
    environment = tuple(pairs)
    benchmark_environment_digest(environment)
    return environment


def _environment_payload(
    environment: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in environment]


# --- portable facts projections --------------------------------------------


@dataclass(frozen=True)
class BenchmarkProcessFacts:
    """Portable Task 8.3 migration-child facts without locators/PIDs.

    A strict projection of the parent-adjudicated
    ``TMBenchmarkProcessEvidence`` retaining only digest, count, path,
    RSS and environment facts.  Absolute run-root/fixture locators, the
    child PID, exit code, worker protocol digest and artifact identities
    are never projected.
    """

    schema_version: str
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    fixture_digest: str
    fixture_record_count: int
    resource_id: str
    canonical_store_id: str
    execution_path: BenchmarkExecutionPath
    path_config_digest: str
    actual_index_kind: str
    record_count: int
    generation: int
    migration_elapsed_ns: int
    peak_rss_bytes: int
    rss_start_bytes: int
    rss_terminal_bytes: int
    rss_unit: str
    rss_scope: str
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_benchmark_process_facts(self)


def _validate_benchmark_process_facts(facts: BenchmarkProcessFacts) -> None:
    if facts.schema_version != PROCESS_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            "process facts schema version must be "
            f"{PROCESS_EVIDENCE_SCHEMA_VERSION}"
        )
    _require_evidence_digest(facts.evidence_digest, "process evidence digest")
    _require_evidence_digest(facts.contract_digest, "process contract digest")
    _require_evidence_digest(facts.corpus_digest, "process corpus digest")
    _require_evidence_digest(facts.fixture_digest, "process fixture digest")
    _require_evidence_digest(facts.path_config_digest, "process path config digest")
    _require_evidence_digest(facts.environment_digest, "process environment digest")
    _require_identity(facts.resource_id, "process resource id")
    _require_identity(facts.canonical_store_id, "process canonical store id")
    if type(facts.execution_path) is not BenchmarkExecutionPath:
        raise TypeError("process facts execution path must be BenchmarkExecutionPath")
    expected_index_kind = _index_kind_for_path(facts.execution_path)
    if facts.actual_index_kind != expected_index_kind:
        raise ValueError("process facts actual index kind must match the path")
    if (
        facts.corpus_record_count != REAL_CORPUS_RECORD_COUNT
        or facts.fixture_record_count != REAL_CORPUS_RECORD_COUNT
        or facts.record_count != REAL_CORPUS_RECORD_COUNT
    ):
        raise ValueError(
            "final process facts require the literal 100000-record corpus"
        )
    _require_builtin_int(facts.generation, "process generation", minimum=0)
    _require_builtin_int(
        facts.migration_elapsed_ns,
        "migration elapsed nanoseconds",
        minimum=0,
    )
    peak_rss = _require_builtin_int(
        facts.peak_rss_bytes,
        "process peak RSS bytes",
        minimum=1,
    )
    rss_start = _require_builtin_int(
        facts.rss_start_bytes,
        "process RSS start bytes",
        minimum=1,
    )
    rss_terminal = _require_builtin_int(
        facts.rss_terminal_bytes,
        "process RSS terminal bytes",
        minimum=1,
    )
    if rss_start > rss_terminal:
        raise ValueError("process RSS terminal sample must not be below start")
    if peak_rss != rss_terminal:
        raise ValueError("process peak RSS must equal the terminal high-water sample")
    _require_identity(facts.rss_unit, "process RSS unit")
    if facts.rss_unit != "bytes":
        raise ValueError("process RSS unit must be 'bytes'")
    _require_identity(facts.rss_scope, "process RSS scope")
    _require_environment(facts.environment, "process environment")
    if facts.environment_digest != benchmark_environment_digest(
        facts.environment
    ):
        raise ValueError("process environment digest must bind the environment")
    environment = dict(facts.environment)
    if environment.get("fts5_enabled") != _fts5_enabled(facts.execution_path):
        raise ValueError("process environment fts5_enabled must match the path")


def _process_facts_payload(facts: BenchmarkProcessFacts) -> dict[str, object]:
    if type(facts) is not BenchmarkProcessFacts:
        raise TypeError("facts must be BenchmarkProcessFacts")
    return {
        "actual_index_kind": facts.actual_index_kind,
        "canonical_store_id": facts.canonical_store_id,
        "contract_digest": facts.contract_digest,
        "corpus_digest": facts.corpus_digest,
        "corpus_record_count": facts.corpus_record_count,
        "environment": _environment_payload(facts.environment),
        "environment_digest": facts.environment_digest,
        "evidence_digest": facts.evidence_digest,
        "execution_path": facts.execution_path.value,
        "fixture_digest": facts.fixture_digest,
        "fixture_record_count": facts.fixture_record_count,
        "generation": facts.generation,
        "migration_elapsed_ns": facts.migration_elapsed_ns,
        "path_config_digest": facts.path_config_digest,
        "peak_rss_bytes": facts.peak_rss_bytes,
        "record_count": facts.record_count,
        "resource_id": facts.resource_id,
        "rss_scope": facts.rss_scope,
        "rss_start_bytes": facts.rss_start_bytes,
        "rss_terminal_bytes": facts.rss_terminal_bytes,
        "rss_unit": facts.rss_unit,
        "schema_version": facts.schema_version,
    }


def _process_facts_from_payload(value: object) -> BenchmarkProcessFacts:
    fields = _strict_fields(
        value,
        _PROCESS_FACTS_PAYLOAD_FIELDS,
        "process facts payload",
    )
    return BenchmarkProcessFacts(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        contract_digest=_as_digest(
            fields["contract_digest"],
            "contract digest",
        ),
        corpus_digest=_as_digest(fields["corpus_digest"], "corpus digest"),
        corpus_record_count=_as_int(
            fields["corpus_record_count"],
            "corpus record count",
            minimum=1,
        ),
        fixture_digest=_as_digest(fields["fixture_digest"], "fixture digest"),
        fixture_record_count=_as_int(
            fields["fixture_record_count"],
            "fixture record count",
            minimum=1,
        ),
        resource_id=_as_str(fields["resource_id"], "resource id"),
        canonical_store_id=_as_str(
            fields["canonical_store_id"],
            "canonical store id",
        ),
        execution_path=_as_path(fields["execution_path"], "execution path"),
        path_config_digest=_as_digest(
            fields["path_config_digest"],
            "path config digest",
        ),
        actual_index_kind=_as_str(
            fields["actual_index_kind"],
            "actual index kind",
        ),
        record_count=_as_int(fields["record_count"], "record count", minimum=1),
        generation=_as_int(fields["generation"], "generation", minimum=0),
        migration_elapsed_ns=_as_int(
            fields["migration_elapsed_ns"],
            "migration elapsed nanoseconds",
            minimum=0,
        ),
        peak_rss_bytes=_as_int(
            fields["peak_rss_bytes"],
            "peak RSS bytes",
            minimum=1,
        ),
        rss_start_bytes=_as_int(
            fields["rss_start_bytes"],
            "RSS start bytes",
            minimum=1,
        ),
        rss_terminal_bytes=_as_int(
            fields["rss_terminal_bytes"],
            "RSS terminal bytes",
            minimum=1,
        ),
        rss_unit=_as_str(fields["rss_unit"], "RSS unit"),
        rss_scope=_as_str(fields["rss_scope"], "RSS scope"),
        environment=_as_environment(fields["environment"], "environment"),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
        evidence_digest=_as_digest(
            fields["evidence_digest"],
            "evidence digest",
        ),
    )


@dataclass(frozen=True)
class BenchmarkQueryFacts:
    """Portable Task 8.5A query-child facts without locators/PIDs.

    A strict projection of the parent-adjudicated ``QueryProcessEvidence``
    retaining digest, count, path, RSS and environment facts.  Artifact
    identity snapshots (inode/device/mtime), the query protocol digest and
    the process-pair digest (which binds PIDs) are never projected.
    """

    schema_version: str
    portable_artifact_key: str
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    fixture_digest: str
    fixture_record_count: int
    resource_id: str
    canonical_store_id: str
    execution_path: BenchmarkExecutionPath
    path_config_digest: str
    actual_index_kind: str
    record_count: int
    generation: int
    process_evidence_digest: str
    artifact_baseline_digest: str
    sidecar_digest: str
    manifest_digest: str
    latency_evidence_digest: str
    processes_distinct: bool
    query_peak_rss_bytes: int
    query_rss_start_bytes: int
    query_rss_terminal_bytes: int
    query_rss_unit: str
    query_rss_scope: str
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_benchmark_query_facts(self)


def _validate_benchmark_query_facts(facts: BenchmarkQueryFacts) -> None:
    if facts.schema_version != QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            "query facts schema version must be "
            f"{QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION}"
        )
    _require_evidence_digest(facts.evidence_digest, "query evidence digest")
    _require_evidence_digest(
        facts.portable_artifact_key,
        "portable artifact key",
    )
    _require_evidence_digest(facts.contract_digest, "query contract digest")
    _require_evidence_digest(facts.corpus_digest, "query corpus digest")
    _require_evidence_digest(facts.fixture_digest, "query fixture digest")
    _require_evidence_digest(facts.path_config_digest, "query path config digest")
    _require_evidence_digest(facts.process_evidence_digest, "process evidence digest")
    _require_evidence_digest(
        facts.artifact_baseline_digest,
        "artifact baseline digest",
    )
    _require_evidence_digest(facts.sidecar_digest, "sidecar digest")
    _require_evidence_digest(facts.manifest_digest, "manifest digest")
    _require_evidence_digest(
        facts.latency_evidence_digest,
        "latency evidence digest",
    )
    _require_evidence_digest(facts.environment_digest, "query environment digest")
    _require_identity(facts.resource_id, "query resource id")
    _require_identity(facts.canonical_store_id, "query canonical store id")
    if type(facts.execution_path) is not BenchmarkExecutionPath:
        raise TypeError("query facts execution path must be BenchmarkExecutionPath")
    expected_index_kind = _index_kind_for_path(facts.execution_path)
    if facts.actual_index_kind != expected_index_kind:
        raise ValueError("query facts actual index kind must match the path")
    if (
        facts.corpus_record_count != REAL_CORPUS_RECORD_COUNT
        or facts.fixture_record_count != REAL_CORPUS_RECORD_COUNT
        or facts.record_count != REAL_CORPUS_RECORD_COUNT
    ):
        raise ValueError(
            "final query facts require the literal 100000-record corpus"
        )
    _require_builtin_int(facts.generation, "query generation", minimum=0)
    if not _as_bool(facts.processes_distinct, "processes distinct"):
        raise ValueError("query child must be a distinct process")
    peak_rss = _require_builtin_int(
        facts.query_peak_rss_bytes,
        "query peak RSS bytes",
        minimum=1,
    )
    rss_start = _require_builtin_int(
        facts.query_rss_start_bytes,
        "query RSS start bytes",
        minimum=1,
    )
    rss_terminal = _require_builtin_int(
        facts.query_rss_terminal_bytes,
        "query RSS terminal bytes",
        minimum=1,
    )
    if rss_start > rss_terminal:
        raise ValueError("query RSS terminal sample must not be below start")
    if peak_rss != rss_terminal:
        raise ValueError("query peak RSS must equal the terminal high-water sample")
    _require_identity(facts.query_rss_unit, "query RSS unit")
    if facts.query_rss_unit != "bytes":
        raise ValueError("query RSS unit must be 'bytes'")
    _require_identity(facts.query_rss_scope, "query RSS scope")
    _require_environment(facts.environment, "query environment")
    if facts.environment_digest != benchmark_environment_digest(
        facts.environment
    ):
        raise ValueError("query environment digest must bind the environment")
    environment = dict(facts.environment)
    if environment.get("fts5_enabled") != _fts5_enabled(facts.execution_path):
        raise ValueError("query environment fts5_enabled must match the path")
    if facts.portable_artifact_key != _portable_artifact_key(
        contract_digest=facts.contract_digest,
        corpus_digest=facts.corpus_digest,
        fixture_digest=facts.fixture_digest,
        resource_id=facts.resource_id,
        canonical_store_id=facts.canonical_store_id,
        execution_path=facts.execution_path,
        path_config_digest=facts.path_config_digest,
        actual_index_kind=facts.actual_index_kind,
        record_count=facts.record_count,
        generation=facts.generation,
        sidecar_digest=facts.sidecar_digest,
        manifest_digest=facts.manifest_digest,
        latency_evidence_digest=facts.latency_evidence_digest,
    ):
        raise ValueError(
            "portable artifact key must derive from stable artifact facts"
        )


def _query_facts_payload(facts: BenchmarkQueryFacts) -> dict[str, object]:
    if type(facts) is not BenchmarkQueryFacts:
        raise TypeError("facts must be BenchmarkQueryFacts")
    return {
        "actual_index_kind": facts.actual_index_kind,
        "artifact_baseline_digest": facts.artifact_baseline_digest,
        "portable_artifact_key": facts.portable_artifact_key,
        "canonical_store_id": facts.canonical_store_id,
        "contract_digest": facts.contract_digest,
        "corpus_digest": facts.corpus_digest,
        "corpus_record_count": facts.corpus_record_count,
        "environment": _environment_payload(facts.environment),
        "environment_digest": facts.environment_digest,
        "evidence_digest": facts.evidence_digest,
        "execution_path": facts.execution_path.value,
        "fixture_digest": facts.fixture_digest,
        "fixture_record_count": facts.fixture_record_count,
        "generation": facts.generation,
        "path_config_digest": facts.path_config_digest,
        "process_evidence_digest": facts.process_evidence_digest,
        "sidecar_digest": facts.sidecar_digest,
        "manifest_digest": facts.manifest_digest,
        "latency_evidence_digest": facts.latency_evidence_digest,
        "processes_distinct": facts.processes_distinct,
        "query_peak_rss_bytes": facts.query_peak_rss_bytes,
        "query_rss_scope": facts.query_rss_scope,
        "query_rss_start_bytes": facts.query_rss_start_bytes,
        "query_rss_terminal_bytes": facts.query_rss_terminal_bytes,
        "query_rss_unit": facts.query_rss_unit,
        "record_count": facts.record_count,
        "resource_id": facts.resource_id,
        "schema_version": facts.schema_version,
    }


def _query_facts_from_payload(value: object) -> BenchmarkQueryFacts:
    fields = _strict_fields(
        value,
        _QUERY_FACTS_PAYLOAD_FIELDS,
        "query facts payload",
    )
    return BenchmarkQueryFacts(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        portable_artifact_key=_as_digest(
            fields["portable_artifact_key"],
            "portable artifact key",
        ),
        contract_digest=_as_digest(
            fields["contract_digest"],
            "contract digest",
        ),
        corpus_digest=_as_digest(fields["corpus_digest"], "corpus digest"),
        corpus_record_count=_as_int(
            fields["corpus_record_count"],
            "corpus record count",
            minimum=1,
        ),
        fixture_digest=_as_digest(fields["fixture_digest"], "fixture digest"),
        fixture_record_count=_as_int(
            fields["fixture_record_count"],
            "fixture record count",
            minimum=1,
        ),
        resource_id=_as_str(fields["resource_id"], "resource id"),
        canonical_store_id=_as_str(
            fields["canonical_store_id"],
            "canonical store id",
        ),
        execution_path=_as_path(fields["execution_path"], "execution path"),
        path_config_digest=_as_digest(
            fields["path_config_digest"],
            "path config digest",
        ),
        actual_index_kind=_as_str(
            fields["actual_index_kind"],
            "actual index kind",
        ),
        record_count=_as_int(fields["record_count"], "record count", minimum=1),
        generation=_as_int(fields["generation"], "generation", minimum=0),
        process_evidence_digest=_as_digest(
            fields["process_evidence_digest"],
            "process evidence digest",
        ),
        artifact_baseline_digest=_as_digest(
            fields["artifact_baseline_digest"],
            "artifact baseline digest",
        ),
        sidecar_digest=_as_digest(fields["sidecar_digest"], "sidecar digest"),
        manifest_digest=_as_digest(
            fields["manifest_digest"],
            "manifest digest",
        ),
        latency_evidence_digest=_as_digest(
            fields["latency_evidence_digest"],
            "latency evidence digest",
        ),
        processes_distinct=_as_bool(
            fields["processes_distinct"],
            "processes distinct",
        ),
        query_peak_rss_bytes=_as_int(
            fields["query_peak_rss_bytes"],
            "query peak RSS bytes",
            minimum=1,
        ),
        query_rss_start_bytes=_as_int(
            fields["query_rss_start_bytes"],
            "query RSS start bytes",
            minimum=1,
        ),
        query_rss_terminal_bytes=_as_int(
            fields["query_rss_terminal_bytes"],
            "query RSS terminal bytes",
            minimum=1,
        ),
        query_rss_unit=_as_str(fields["query_rss_unit"], "query RSS unit"),
        query_rss_scope=_as_str(fields["query_rss_scope"], "query RSS scope"),
        environment=_as_environment(fields["environment"], "environment"),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
        evidence_digest=_as_digest(
            fields["evidence_digest"],
            "evidence digest",
        ),
    )


# --- report codec -----------------------------------------------------------


def _report_payload(report: BenchmarkReport) -> dict[str, object]:
    if type(report) is not BenchmarkReport:
        raise TypeError("report must be BenchmarkReport")
    return {
        "candidate_recall": report.candidate_recall,
        "contract_digest": report.contract_digest,
        "contract_json": contract_to_json(report.contract),
        "corpus_composition_digest": report.corpus_composition_digest,
        "corpus_composition_version": report.corpus_composition_version,
        "corpus_digest": report.corpus_digest,
        "environment": _environment_payload(report.environment),
        "environment_digest": report.environment_digest,
        "exact_cohort_digest": report.exact_cohort_digest,
        "exact_max_ms": report.exact_max_ms,
        "exact_p50_ms": report.exact_p50_ms,
        "exact_p95_ms": report.exact_p95_ms,
        "exact_sample_count": report.exact_sample_count,
        "execution_path": report.execution_path.value,
        "failed_gates": list(report.failed_gates),
        "fuzzy_cohort_digest": report.fuzzy_cohort_digest,
        "fuzzy_sample_count": report.fuzzy_sample_count,
        "fuzzy_top10_max_ms": report.fuzzy_top10_max_ms,
        "fuzzy_top10_p50_ms": report.fuzzy_top10_p50_ms,
        "fuzzy_top10_p95_ms": report.fuzzy_top10_p95_ms,
        "migration_seconds": report.migration_seconds,
        "oracle_query_count": report.oracle_query_count,
        "oracle_subset_digest": report.oracle_subset_digest,
        "passed": report.passed,
        "path_config_digest": report.path_config_digest,
        "peak_rss_mib": report.peak_rss_mib,
        "percentile_method": report.percentile_method,
        "scorer_config_digest": report.scorer_config_digest,
    }


def _report_from_payload(value: object) -> BenchmarkReport:
    fields = _strict_fields(value, _REPORT_PAYLOAD_FIELDS, "benchmark report payload")
    contract_json = _as_str(fields["contract_json"], "contract json")
    contract = contract_from_json(_canonical_json(_parse_strict_json(contract_json)))
    if type(contract) is not BenchmarkContract:
        raise TypeError("report contract must be BenchmarkContract")
    failed_gates_value = fields["failed_gates"]
    if type(failed_gates_value) is not list:
        raise TypeError("failed gates must be a JSON list")
    failed_gates = tuple(
        _as_str(gate, "failed gate") for gate in failed_gates_value
    )
    report = BenchmarkReport(
        contract=contract,
        contract_digest=_as_digest(
            fields["contract_digest"],
            "contract digest",
        ),
        corpus_digest=_as_digest(fields["corpus_digest"], "corpus digest"),
        corpus_composition_version=_as_str(
            fields["corpus_composition_version"],
            "corpus composition version",
        ),
        corpus_composition_digest=_as_digest(
            fields["corpus_composition_digest"],
            "corpus composition digest",
        ),
        exact_cohort_digest=_as_digest(
            fields["exact_cohort_digest"],
            "exact cohort digest",
        ),
        fuzzy_cohort_digest=_as_digest(
            fields["fuzzy_cohort_digest"],
            "fuzzy cohort digest",
        ),
        oracle_subset_digest=_as_digest(
            fields["oracle_subset_digest"],
            "oracle subset digest",
        ),
        scorer_config_digest=_as_digest(
            fields["scorer_config_digest"],
            "scorer config digest",
        ),
        execution_path=_as_path(fields["execution_path"], "execution path"),
        path_config_digest=_as_digest(
            fields["path_config_digest"],
            "path config digest",
        ),
        exact_sample_count=_as_int(
            fields["exact_sample_count"],
            "exact sample count",
            minimum=0,
        ),
        fuzzy_sample_count=_as_int(
            fields["fuzzy_sample_count"],
            "fuzzy sample count",
            minimum=0,
        ),
        oracle_query_count=_as_int(
            fields["oracle_query_count"],
            "oracle query count",
            minimum=0,
        ),
        percentile_method=_as_str(
            fields["percentile_method"],
            "percentile method",
        ),
        candidate_recall=_as_float(
            fields["candidate_recall"],
            "candidate recall",
        ),
        exact_p50_ms=_as_float(fields["exact_p50_ms"], "exact p50"),
        exact_p95_ms=_as_float(fields["exact_p95_ms"], "exact p95"),
        exact_max_ms=_as_float(fields["exact_max_ms"], "exact max"),
        fuzzy_top10_p50_ms=_as_float(
            fields["fuzzy_top10_p50_ms"],
            "fuzzy top-10 p50",
        ),
        fuzzy_top10_p95_ms=_as_float(
            fields["fuzzy_top10_p95_ms"],
            "fuzzy top-10 p95",
        ),
        fuzzy_top10_max_ms=_as_float(
            fields["fuzzy_top10_max_ms"],
            "fuzzy top-10 max",
        ),
        migration_seconds=_as_float(
            fields["migration_seconds"],
            "migration seconds",
        ),
        peak_rss_mib=_as_float(fields["peak_rss_mib"], "peak RSS MiB"),
        passed=_as_bool(fields["passed"], "benchmark passed"),
        failed_gates=failed_gates,
        environment=_as_environment(fields["environment"], "environment"),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
    )
    return report


def _suite_report_payload(report: BenchmarkSuiteReport) -> dict[str, object]:
    if type(report) is not BenchmarkSuiteReport:
        raise TypeError("report must be BenchmarkSuiteReport")
    return {
        "corpus_composition_digest": report.corpus_composition_digest,
        "corpus_composition_version": report.corpus_composition_version,
        "failed_paths": [path.value for path in report.failed_paths],
        "passed": report.passed,
        "path_reports": [_report_payload(path_report) for path_report in report.path_reports],
        "suite_contract_digest": report.suite_contract_digest,
    }


def _suite_report_from_payload(
    value: object,
    suite_contract: BenchmarkSuiteContract,
) -> BenchmarkSuiteReport:
    fields = _strict_fields(
        value,
        _SUITE_REPORT_PAYLOAD_FIELDS,
        "benchmark suite report payload",
    )
    path_reports_value = fields["path_reports"]
    if type(path_reports_value) is not list:
        raise TypeError("suite path reports must be a JSON list")
    path_reports = tuple(
        _report_from_payload(path_report) for path_report in path_reports_value
    )
    failed_paths_value = fields["failed_paths"]
    if type(failed_paths_value) is not list:
        raise TypeError("suite failed paths must be a JSON list")
    failed_paths = tuple(
        _as_path(path, "failed path") for path in failed_paths_value
    )
    return BenchmarkSuiteReport(
        suite_contract=suite_contract,
        suite_contract_digest=_as_digest(
            fields["suite_contract_digest"],
            "suite contract digest",
        ),
        corpus_composition_version=_as_str(
            fields["corpus_composition_version"],
            "suite corpus composition version",
        ),
        corpus_composition_digest=_as_digest(
            fields["corpus_composition_digest"],
            "suite corpus composition digest",
        ),
        path_reports=path_reports,
        passed=_as_bool(fields["passed"], "aggregate passed"),
        failed_paths=failed_paths,
    )


# --- derivation -------------------------------------------------------------


def _index_kind_for_path(path: BenchmarkExecutionPath) -> str:
    if path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        return "FTS5_TRIGRAM"
    if path is BenchmarkExecutionPath.GRAM_FALLBACK:
        return "GRAM_FALLBACK"
    raise ValueError("execution path is unsupported")


def _fts5_enabled(path: BenchmarkExecutionPath) -> str:
    if path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        return "true"
    if path is BenchmarkExecutionPath.GRAM_FALLBACK:
        return "false"
    raise ValueError("execution path is unsupported")


def _portable_artifact_key(
    *,
    contract_digest: str,
    corpus_digest: str,
    fixture_digest: str,
    resource_id: str,
    canonical_store_id: str,
    execution_path: BenchmarkExecutionPath,
    path_config_digest: str,
    actual_index_kind: str,
    record_count: int,
    generation: int,
    sidecar_digest: str,
    manifest_digest: str,
    latency_evidence_digest: str,
) -> str:
    """Derive a locator/PID-independent key for one measured artifact."""
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    return benchmark_digest(
        BENCHMARK_PORTABLE_ARTIFACT_KEY_VERSION,
        "benchmark-portable-artifact",
        [
            {
                "actual_index_kind": _require_identity(
                    actual_index_kind,
                    "actual index kind",
                ),
                "canonical_store_id": _require_identity(
                    canonical_store_id,
                    "canonical store id",
                ),
                "contract_digest": _as_digest(
                    contract_digest,
                    "contract digest",
                ),
                "corpus_digest": _as_digest(
                    corpus_digest,
                    "corpus digest",
                ),
                "execution_path": execution_path.value,
                "fixture_digest": _as_digest(
                    fixture_digest,
                    "fixture digest",
                ),
                "generation": _require_builtin_int(
                    generation,
                    "generation",
                    minimum=0,
                ),
                "latency_evidence_digest": _as_digest(
                    latency_evidence_digest,
                    "latency evidence digest",
                ),
                "manifest_digest": _as_digest(
                    manifest_digest,
                    "manifest digest",
                ),
                "path_config_digest": _as_digest(
                    path_config_digest,
                    "path config digest",
                ),
                "record_count": _require_builtin_int(
                    record_count,
                    "record count",
                    minimum=1,
                ),
                "resource_id": _require_identity(
                    resource_id,
                    "resource id",
                ),
                "sidecar_digest": _as_digest(
                    sidecar_digest,
                    "sidecar digest",
                ),
            }
        ],
    )


def _merge_environment(
    *environments: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Combine frozen owner environments; conflicting shared keys fail."""
    merged: dict[str, str] = {}
    for environment in environments:
        if type(environment) is not tuple:
            raise TypeError("environment must be a built-in tuple")
        for key, value in environment:
            if key in merged and merged[key] != value:
                raise ValueError(
                    f"conflicting shared environment fact for key {key!r}"
                )
            merged[key] = value
    result = tuple(sorted(merged.items()))
    benchmark_environment_digest(result)
    return result


def _derive_candidate_recall(evidence: OracleRecallEvidence) -> float:
    """Derive candidate recall from raw rows/obligations only.

    Recall is the fraction of oracle queries whose candidate facts fully
    satisfy every obligation: the candidate path was available, the actual
    index kind matched the store, and neither the above-threshold nor the
    top-10 obligation sets had a missing identity.  ``recall_passed`` and
    the evidence totals are never trusted as inputs; the derived value is
    cross-checked against the owner-derived ``recall_passed`` fact and any
    inconsistency fails closed.
    """
    if type(evidence) is not OracleRecallEvidence:
        raise TypeError("evidence must be OracleRecallEvidence")
    fully_covered = sum(
        1
        for row in evidence.rows
        if row.candidate_available
        and row.actual_index_kind == evidence.store_index_kind
        and not row.missing_above_threshold_ids
        and not row.missing_top10_ids
    )
    recall = fully_covered / evidence.query_count
    if (recall == 1.0) != evidence.recall_passed:
        raise ValueError(
            "derived candidate recall is inconsistent with oracle row facts"
        )
    return recall


def _derive_gate_verdict(
    *,
    contract: BenchmarkContract,
    candidate_recall: float,
    exact_p95_ms: float,
    fuzzy_top10_p95_ms: float,
    migration_seconds: float,
    peak_rss_mib: float,
) -> tuple[tuple[str, ...], bool]:
    failed_gates: list[str] = []
    if candidate_recall < contract.candidate_recall_gate:
        failed_gates.append("CANDIDATE_RECALL")
    if exact_p95_ms > contract.exact_p95_gate_ms:
        failed_gates.append("EXACT_P95")
    if fuzzy_top10_p95_ms > contract.fuzzy_p95_gate_ms:
        failed_gates.append("FUZZY_P95")
    if migration_seconds > contract.migration_gate_seconds:
        failed_gates.append("MIGRATION")
    if peak_rss_mib > contract.peak_rss_gate_mib:
        failed_gates.append("PEAK_RSS")
    failed = tuple(failed_gates)
    if any(gate not in _BENCHMARK_GATE_ORDER for gate in failed):
        raise ValueError("derived failed gates contain unsupported gate")
    return failed, not failed


def _derive_report(
    *,
    contract: BenchmarkContract,
    contract_digest: str,
    process_facts: BenchmarkProcessFacts,
    query_facts: BenchmarkQueryFacts,
    oracle_evidence: OracleRecallEvidence,
    latency_evidence: LatencyEvidence,
    environment: tuple[tuple[str, str], ...],
) -> BenchmarkReport:
    """Derive one strict path report from the combined frozen facts."""
    recomputed = latency_evidence.recompute_statistics()
    stored = (
        latency_evidence.exact_p50_ns,
        latency_evidence.exact_p95_ns,
        latency_evidence.exact_max_ns,
        latency_evidence.fuzzy_p50_ns,
        latency_evidence.fuzzy_p95_ns,
        latency_evidence.fuzzy_max_ns,
    )
    if recomputed != stored:
        raise ValueError(
            "latency statistics must be recomputable from the raw samples"
        )
    exact_p50_ns, exact_p95_ns, exact_max_ns, fuzzy_p50_ns, fuzzy_p95_ns, fuzzy_max_ns = (
        stored
    )
    candidate_recall = _derive_candidate_recall(oracle_evidence)
    exact_p50_ms = exact_p50_ns / NANOSECONDS_PER_MILLISECOND
    exact_p95_ms = exact_p95_ns / NANOSECONDS_PER_MILLISECOND
    exact_max_ms = exact_max_ns / NANOSECONDS_PER_MILLISECOND
    fuzzy_top10_p50_ms = fuzzy_p50_ns / NANOSECONDS_PER_MILLISECOND
    fuzzy_top10_p95_ms = fuzzy_p95_ns / NANOSECONDS_PER_MILLISECOND
    fuzzy_top10_max_ms = fuzzy_max_ns / NANOSECONDS_PER_MILLISECOND
    migration_seconds = process_facts.migration_elapsed_ns / NANOSECONDS_PER_SECOND
    peak_rss_bytes = max(
        process_facts.peak_rss_bytes,
        query_facts.query_peak_rss_bytes,
    )
    peak_rss_mib = peak_rss_bytes / BYTES_PER_MIB
    failed_gates, passed = _derive_gate_verdict(
        contract=contract,
        candidate_recall=candidate_recall,
        exact_p95_ms=exact_p95_ms,
        fuzzy_top10_p95_ms=fuzzy_top10_p95_ms,
        migration_seconds=migration_seconds,
        peak_rss_mib=peak_rss_mib,
    )
    return BenchmarkReport(
        contract=contract,
        contract_digest=contract_digest,
        corpus_digest=contract.corpus_digest,
        corpus_composition_version=contract.corpus_composition_version,
        corpus_composition_digest=contract.corpus_composition_digest,
        exact_cohort_digest=contract.exact_cohort_digest,
        fuzzy_cohort_digest=contract.fuzzy_cohort_digest,
        oracle_subset_digest=contract.oracle_subset_digest,
        scorer_config_digest=contract.scorer_config_digest,
        execution_path=latency_evidence.execution_path,
        path_config_digest=latency_evidence.path_config_digest,
        exact_sample_count=latency_evidence.exact_sample_count,
        fuzzy_sample_count=latency_evidence.fuzzy_sample_count,
        oracle_query_count=oracle_evidence.oracle_query_count,
        percentile_method=latency_evidence.percentile_method,
        candidate_recall=candidate_recall,
        exact_p50_ms=exact_p50_ms,
        exact_p95_ms=exact_p95_ms,
        exact_max_ms=exact_max_ms,
        fuzzy_top10_p50_ms=fuzzy_top10_p50_ms,
        fuzzy_top10_p95_ms=fuzzy_top10_p95_ms,
        fuzzy_top10_max_ms=fuzzy_top10_max_ms,
        migration_seconds=migration_seconds,
        peak_rss_mib=peak_rss_mib,
        passed=passed,
        failed_gates=failed_gates,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )


def _derive_suite_report(
    suite_contract: BenchmarkSuiteContract,
    fts5_report: BenchmarkReport,
    fallback_report: BenchmarkReport,
) -> BenchmarkSuiteReport:
    contract = suite_contract.benchmark_contract
    failed_paths = tuple(
        path_report.execution_path
        for path_report in (fts5_report, fallback_report)
        if not path_report.passed
    )
    return BenchmarkSuiteReport(
        suite_contract=suite_contract,
        suite_contract_digest=benchmark_suite_contract_digest(suite_contract),
        corpus_composition_version=contract.corpus_composition_version,
        corpus_composition_digest=contract.corpus_composition_digest,
        path_reports=(fts5_report, fallback_report),
        passed=not failed_paths,
        failed_paths=failed_paths,
    )


# --- path bundle ------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkPathBundle:
    """One execution path's combined portable benchmark evidence.

    Holds the owner-internal portable process/query projections, the exact
    retained latency evidence and oracle recall evidence, and the derived
    strict ``BenchmarkReport``.  The merged environment and its digest are
    always derived from the frozen owner facts (never caller-supplied), and
    the report is re-derived from the retained facts at construction so a
    forged report fails closed.
    """

    process_facts: BenchmarkProcessFacts
    query_facts: BenchmarkQueryFacts
    oracle_evidence: OracleRecallEvidence
    latency_evidence: LatencyEvidence
    report: BenchmarkReport
    environment: tuple[tuple[str, str], ...] = field(init=False)
    environment_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_path_bundle(self)
        environment = _merge_environment(
            self.process_facts.environment,
            self.query_facts.environment,
            self.latency_evidence.environment,
            self.oracle_evidence.environment,
        )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "environment_digest",
            benchmark_environment_digest(environment),
        )
        derived_report = _derive_report(
            contract=self.oracle_evidence.contract,
            contract_digest=self.process_facts.contract_digest,
            process_facts=self.process_facts,
            query_facts=self.query_facts,
            oracle_evidence=self.oracle_evidence,
            latency_evidence=self.latency_evidence,
            environment=environment,
        )
        if derived_report != self.report:
            raise ValueError(
                "path report must be derivable from the retained evidence facts"
            )


def _validate_path_bundle(bundle: BenchmarkPathBundle) -> None:
    if type(bundle.process_facts) is not BenchmarkProcessFacts:
        raise TypeError("process facts must be BenchmarkProcessFacts")
    if type(bundle.query_facts) is not BenchmarkQueryFacts:
        raise TypeError("query facts must be BenchmarkQueryFacts")
    if type(bundle.oracle_evidence) is not OracleRecallEvidence:
        raise TypeError("oracle evidence must be OracleRecallEvidence")
    if type(bundle.latency_evidence) is not LatencyEvidence:
        raise TypeError("latency evidence must be LatencyEvidence")
    if type(bundle.report) is not BenchmarkReport:
        raise TypeError("path report must be BenchmarkReport")
    path = bundle.process_facts.execution_path
    if bundle.query_facts.execution_path is not path:
        raise ValueError("query facts path must match process facts path")
    if bundle.oracle_evidence.execution_path is not path:
        raise ValueError("oracle evidence path must match process facts path")
    if bundle.latency_evidence.execution_path is not path:
        raise ValueError("latency evidence path must match process facts path")
    if bundle.report.execution_path is not path:
        raise ValueError("path report execution path must match the evidence")
    if bundle.process_facts.contract_digest != bundle.query_facts.contract_digest:
        raise ValueError("process and query contract digests must match")
    if (
        bundle.process_facts.contract_digest
        != bundle.oracle_evidence.contract_digest
        or bundle.process_facts.contract_digest
        != bundle.latency_evidence.contract_digest
        or bundle.process_facts.contract_digest
        != bundle.report.contract_digest
    ):
        raise ValueError("all path evidence must bind the same contract digest")
    oracle_contract = bundle.oracle_evidence.contract
    latency_contract = bundle.latency_evidence.contract
    if oracle_contract != latency_contract:
        raise ValueError("oracle and latency evidence contracts must match")
    if bundle.process_facts.contract_digest != benchmark_contract_digest(
        oracle_contract
    ):
        raise ValueError(
            "process contract digest must bind the oracle/latency contract"
        )
    if bundle.process_facts.environment != bundle.query_facts.environment:
        raise ValueError("process and query environments must match")
    if (
        bundle.process_facts.environment_digest
        != bundle.query_facts.environment_digest
    ):
        raise ValueError("process and query environment digests must match")
    contract = oracle_contract
    if bundle.process_facts.corpus_digest != contract.corpus_digest:
        raise ValueError("process corpus digest must match the contract")
    if bundle.query_facts.corpus_digest != contract.corpus_digest:
        raise ValueError("query corpus digest must match the contract")
    for field_name, process_value, query_value in (
        (
            "corpus record count",
            bundle.process_facts.corpus_record_count,
            bundle.query_facts.corpus_record_count,
        ),
        (
            "fixture digest",
            bundle.process_facts.fixture_digest,
            bundle.query_facts.fixture_digest,
        ),
        (
            "fixture record count",
            bundle.process_facts.fixture_record_count,
            bundle.query_facts.fixture_record_count,
        ),
        (
            "resource id",
            bundle.process_facts.resource_id,
            bundle.query_facts.resource_id,
        ),
        (
            "canonical store id",
            bundle.process_facts.canonical_store_id,
            bundle.query_facts.canonical_store_id,
        ),
        (
            "record count",
            bundle.process_facts.record_count,
            bundle.query_facts.record_count,
        ),
        (
            "generation",
            bundle.process_facts.generation,
            bundle.query_facts.generation,
        ),
    ):
        if process_value != query_value:
            raise ValueError(f"process/query {field_name} must match")
    if (
        bundle.process_facts.path_config_digest
        != bundle.query_facts.path_config_digest
        or bundle.process_facts.path_config_digest
        != bundle.latency_evidence.path_config_digest
        or bundle.process_facts.path_config_digest
        != bundle.oracle_evidence.path_config_digest
        or bundle.process_facts.path_config_digest
        != bundle.report.path_config_digest
    ):
        raise ValueError("all path evidence must bind the same path config digest")
    expected_path_digest = (
        contract.fast_path_config_digest
        if path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else contract.fallback_path_config_digest
    )
    if bundle.process_facts.path_config_digest != expected_path_digest:
        raise ValueError("path config digest must match the execution path")
    if (
        bundle.latency_evidence.exact_cohort_digest != contract.exact_cohort_digest
        or bundle.latency_evidence.fuzzy_cohort_digest
        != contract.fuzzy_cohort_digest
    ):
        raise ValueError("latency cohort digests must match the contract")
    if (
        bundle.oracle_evidence.oracle_subset_digest
        != contract.oracle_subset_digest
    ):
        raise ValueError("oracle subset digest must match the contract")
    if (
        bundle.oracle_evidence.scorer_config_digest
        != contract.scorer_config_digest
    ):
        raise ValueError("oracle scorer config digest must match the contract")
    if bundle.process_facts.rss_scope != contract.rss_scope:
        raise ValueError("process RSS scope must match the contract")
    if bundle.query_facts.query_rss_scope != contract.rss_scope:
        raise ValueError("query RSS scope must match the contract")
    if (
        bundle.latency_evidence.exact_sample_count != contract.exact_cohort_count
        or bundle.latency_evidence.fuzzy_sample_count
        != contract.fuzzy_cohort_count
    ):
        raise ValueError(
            "latency sample counts must match the contract cohort counts"
        )
    if bundle.oracle_evidence.oracle_query_count != contract.oracle_query_count:
        raise ValueError("oracle query count must match the contract")
    if bundle.process_facts.evidence_digest != bundle.query_facts.process_evidence_digest:
        raise ValueError(
            "query process-evidence digest must bind the process evidence"
        )
    if (
        bundle.query_facts.latency_evidence_digest
        != bundle.latency_evidence.evidence_digest
    ):
        raise ValueError(
            "query latency-evidence digest must bind the latency evidence"
        )
    if bundle.process_facts.actual_index_kind != bundle.query_facts.actual_index_kind:
        raise ValueError("process and query index kinds must match")
    if bundle.process_facts.actual_index_kind != _index_kind_for_path(path):
        raise ValueError("actual index kind must match the execution path")
    if bundle.oracle_evidence.store_index_kind != _index_kind_for_path(path):
        raise ValueError("oracle store index kind must match the execution path")


def _path_bundle_payload(bundle: BenchmarkPathBundle) -> dict[str, object]:
    if type(bundle) is not BenchmarkPathBundle:
        raise TypeError("bundle must be BenchmarkPathBundle")
    return {
        "execution_path": bundle.process_facts.execution_path.value,
        "latency_evidence": latency_evidence_to_payload(bundle.latency_evidence),
        "oracle_evidence_json": oracle_evidence_to_json(bundle.oracle_evidence),
        "process_facts": _process_facts_payload(bundle.process_facts),
        "query_facts": _query_facts_payload(bundle.query_facts),
        "report": _report_payload(bundle.report),
    }


def _path_bundle_from_payload(value: object) -> BenchmarkPathBundle:
    fields = _strict_fields(
        value,
        _PATH_BUNDLE_PAYLOAD_FIELDS,
        "path bundle payload",
    )
    execution_path = _as_path(fields["execution_path"], "execution path")
    process_facts = _process_facts_from_payload(fields["process_facts"])
    query_facts = _query_facts_from_payload(fields["query_facts"])
    oracle_evidence = oracle_evidence_from_json(
        _as_str(fields["oracle_evidence_json"], "oracle evidence json")
    )
    latency_evidence = latency_evidence_from_payload(
        cast(Mapping[str, object], fields["latency_evidence"])
    )
    report = _report_from_payload(fields["report"])
    if process_facts.execution_path is not execution_path:
        raise ValueError("process facts path must match the path bundle")
    bundle = BenchmarkPathBundle(
        process_facts=process_facts,
        query_facts=query_facts,
        oracle_evidence=oracle_evidence,
        latency_evidence=latency_evidence,
        report=report,
    )
    return bundle


# --- evidence bundle --------------------------------------------------------


def _suite_contract(
    contract: BenchmarkContract,
    contract_digest: str,
) -> BenchmarkSuiteContract:
    return BenchmarkSuiteContract(
        suite_version=BENCHMARK_SUITE_VERSION,
        benchmark_contract=contract,
        benchmark_contract_digest=contract_digest,
        required_paths=(
            BenchmarkExecutionPath.FTS5_TRIGRAM,
            BenchmarkExecutionPath.GRAM_FALLBACK,
        ),
    )


@dataclass(frozen=True)
class BenchmarkEvidenceBundle:
    """Immutable portable Gate D evidence bundle for both execution paths.

    The bundle retains one strict ``BenchmarkReport`` per execution path
    (in enum order), the aggregate ``BenchmarkSuiteReport``, the full latency
    raw samples and immutable process/query/oracle facts and digests.  It is
    safe to persist: it never contains absolute run-root/fixture paths, PIDs,
    inode/device/mtime facts, locator-binding protocol digests, query/source/
    target bodies or reusable handles.
    """

    schema_version: str
    contract: BenchmarkContract
    contract_digest: str
    suite_contract: BenchmarkSuiteContract
    suite_contract_digest: str
    fts5: BenchmarkPathBundle
    fallback: BenchmarkPathBundle
    suite_report: BenchmarkSuiteReport
    bundle_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_benchmark_evidence_bundle(self)
        object.__setattr__(
            self,
            "bundle_digest",
            benchmark_evidence_bundle_digest(self),
        )

    def recompute_bundle_digest(self) -> str:
        """Independently recompute the canonical bundle digest."""
        return benchmark_evidence_bundle_digest(self)


def _validate_benchmark_evidence_bundle(bundle: BenchmarkEvidenceBundle) -> None:
    if bundle.schema_version != BENCHMARK_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "bundle schema version must be " f"{BENCHMARK_BUNDLE_SCHEMA_VERSION}"
        )
    if type(bundle.contract) is not BenchmarkContract:
        raise TypeError("bundle contract must be BenchmarkContract")
    contract_snapshot = contract_from_json(contract_to_json(bundle.contract))
    if type(contract_snapshot) is not BenchmarkContract:
        raise TypeError("bundle contract snapshot must be BenchmarkContract")
    if contract_snapshot != bundle.contract:
        raise ValueError("bundle contract must be a private immutable snapshot")
    if bundle.contract_digest != benchmark_contract_digest(bundle.contract):
        raise ValueError("bundle contract digest must bind the contract")
    if type(bundle.suite_contract) is not BenchmarkSuiteContract:
        raise TypeError("bundle suite contract must be BenchmarkSuiteContract")
    expected_suite_contract = _suite_contract(
        bundle.contract,
        bundle.contract_digest,
    )
    if bundle.suite_contract != expected_suite_contract:
        raise ValueError("bundle suite contract must derive from the contract")
    if bundle.suite_contract_digest != benchmark_suite_contract_digest(
        bundle.suite_contract
    ):
        raise ValueError("bundle suite contract digest must bind the suite contract")
    if type(bundle.fts5) is not BenchmarkPathBundle:
        raise TypeError("bundle fts5 path must be BenchmarkPathBundle")
    if type(bundle.fallback) is not BenchmarkPathBundle:
        raise TypeError("bundle fallback path must be BenchmarkPathBundle")
    if bundle.fts5.process_facts.execution_path is not BenchmarkExecutionPath.FTS5_TRIGRAM:
        raise ValueError("bundle fts5 path must execute FTS5_TRIGRAM")
    if (
        bundle.fallback.process_facts.execution_path
        is not BenchmarkExecutionPath.GRAM_FALLBACK
    ):
        raise ValueError("bundle fallback path must execute GRAM_FALLBACK")
    for path_bundle in (bundle.fts5, bundle.fallback):
        if path_bundle.oracle_evidence.contract != bundle.contract:
            raise ValueError("path oracle contract must match the bundle contract")
        if path_bundle.latency_evidence.contract != bundle.contract:
            raise ValueError("path latency contract must match the bundle contract")
        if path_bundle.process_facts.contract_digest != bundle.contract_digest:
            raise ValueError(
                "path process contract digest must match the bundle contract"
            )
        if path_bundle.report.contract != bundle.contract:
            raise ValueError("path report contract must match the bundle contract")
    if type(bundle.suite_report) is not BenchmarkSuiteReport:
        raise TypeError("suite report must be BenchmarkSuiteReport")
    if bundle.suite_report.suite_contract != bundle.suite_contract:
        raise ValueError("suite report suite contract must match the bundle")
    if bundle.suite_report.suite_contract_digest != bundle.suite_contract_digest:
        raise ValueError(
            "suite report suite contract digest must match the bundle"
        )
    if bundle.suite_report.path_reports != (
        bundle.fts5.report,
        bundle.fallback.report,
    ):
        raise ValueError(
            "suite report path reports must match the two path reports in order"
        )
    derived_suite_report = _derive_suite_report(
        bundle.suite_contract,
        bundle.fts5.report,
        bundle.fallback.report,
    )
    if derived_suite_report != bundle.suite_report:
        raise ValueError(
            "suite report must be derivable from the two path reports"
        )


def _bundle_payload_fields(bundle: BenchmarkEvidenceBundle) -> dict[str, object]:
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    return {
        "contract_digest": bundle.contract_digest,
        "contract_json": contract_to_json(bundle.contract),
        "fallback": _path_bundle_payload(bundle.fallback),
        "fts5": _path_bundle_payload(bundle.fts5),
        "schema_version": bundle.schema_version,
        "suite_contract_digest": bundle.suite_contract_digest,
        "suite_report": _suite_report_payload(bundle.suite_report),
    }


def benchmark_evidence_bundle_digest(bundle: BenchmarkEvidenceBundle) -> str:
    """Canonical digest over every bundle fact except the digest itself."""
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    _validate_benchmark_evidence_bundle(bundle)
    return benchmark_digest(
        BENCHMARK_BUNDLE_DIGEST_VERSION,
        BENCHMARK_BUNDLE_DIGEST_KIND,
        [_bundle_payload_fields(bundle)],
    )


def benchmark_evidence_bundle_to_payload(
    bundle: BenchmarkEvidenceBundle,
) -> dict[str, object]:
    """Strict public payload snapshot of one evidence bundle."""
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    payload = _bundle_payload_fields(bundle)
    payload["bundle_digest"] = benchmark_evidence_bundle_digest(bundle)
    return payload


def benchmark_evidence_bundle_from_payload(
    payload: Mapping[str, object],
) -> BenchmarkEvidenceBundle:
    """Strictly reconstruct a self-validating evidence bundle.

    The serialized reports are never trusted: every fact is reconstructed
    strictly, the reports are re-derived from the retained facts, and the
    bundle digest is recomputed from the canonical payload.
    """
    fields = _strict_fields(
        payload,
        _BUNDLE_PAYLOAD_FIELDS,
        "benchmark evidence bundle payload",
    )
    if fields["schema_version"] != BENCHMARK_BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "bundle schema version must be " f"{BENCHMARK_BUNDLE_SCHEMA_VERSION}"
        )
    contract_json = _as_str(fields["contract_json"], "contract json")
    contract = contract_from_json(_canonical_json(_parse_strict_json(contract_json)))
    if type(contract) is not BenchmarkContract:
        raise TypeError("bundle contract must be BenchmarkContract")
    contract_digest = _as_digest(fields["contract_digest"], "contract digest")
    if contract_digest != benchmark_contract_digest(contract):
        raise ValueError("bundle contract digest must bind the contract")
    suite_contract = _suite_contract(contract, contract_digest)
    suite_contract_digest = _as_digest(
        fields["suite_contract_digest"],
        "suite contract digest",
    )
    if suite_contract_digest != benchmark_suite_contract_digest(suite_contract):
        raise ValueError("bundle suite contract digest must bind the suite contract")
    fts5 = _path_bundle_from_payload(fields["fts5"])
    fallback = _path_bundle_from_payload(fields["fallback"])
    if fts5.process_facts.execution_path is not BenchmarkExecutionPath.FTS5_TRIGRAM:
        raise ValueError("bundle fts5 path must execute FTS5_TRIGRAM")
    if (
        fallback.process_facts.execution_path
        is not BenchmarkExecutionPath.GRAM_FALLBACK
    ):
        raise ValueError("bundle fallback path must execute GRAM_FALLBACK")
    suite_report = _suite_report_from_payload(fields["suite_report"], suite_contract)
    bundle = BenchmarkEvidenceBundle(
        schema_version=BENCHMARK_BUNDLE_SCHEMA_VERSION,
        contract=contract,
        contract_digest=contract_digest,
        suite_contract=suite_contract,
        suite_contract_digest=suite_contract_digest,
        fts5=fts5,
        fallback=fallback,
        suite_report=suite_report,
    )
    expected_digest = benchmark_evidence_bundle_digest(bundle)
    serialized_digest = _as_digest(fields["bundle_digest"], "bundle digest")
    if serialized_digest != expected_digest:
        raise ValueError(
            "bundle digest must bind the reconstructed bundle facts"
        )
    return bundle


def benchmark_evidence_bundle_to_json(bundle: BenchmarkEvidenceBundle) -> str:
    """Strict canonical JSON snapshot of one evidence bundle."""
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    return _canonical_json(benchmark_evidence_bundle_to_payload(bundle))


def benchmark_evidence_bundle_from_json(raw: str) -> BenchmarkEvidenceBundle:
    """Strictly reconstruct a bundle from canonical JSON."""
    return benchmark_evidence_bundle_from_payload(
        _parse_strict_json(raw)
    )


# --- combination ------------------------------------------------------------


def _project_process_facts(evidence: TMBenchmarkProcessEvidence) -> BenchmarkProcessFacts:
    if type(evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    return BenchmarkProcessFacts(
        schema_version=evidence.schema_version,
        contract_digest=evidence.contract_digest,
        corpus_digest=evidence.corpus_digest,
        corpus_record_count=evidence.corpus_record_count,
        fixture_digest=evidence.fixture_digest,
        fixture_record_count=evidence.fixture_record_count,
        resource_id=evidence.resource_id,
        canonical_store_id=evidence.canonical_store_id,
        execution_path=evidence.execution_path,
        path_config_digest=evidence.path_config_digest,
        actual_index_kind=evidence.actual_index_kind,
        record_count=evidence.record_count,
        generation=evidence.generation,
        migration_elapsed_ns=evidence.migration_elapsed_ns,
        peak_rss_bytes=evidence.peak_rss_bytes,
        rss_start_bytes=evidence.rss_start_bytes,
        rss_terminal_bytes=evidence.rss_terminal_bytes,
        rss_unit=evidence.rss_unit,
        rss_scope=evidence.rss_scope,
        environment=evidence.environment,
        environment_digest=evidence.environment_digest,
        evidence_digest=evidence.evidence_digest,
    )


def _project_query_facts(evidence: QueryProcessEvidence) -> BenchmarkQueryFacts:
    if type(evidence) is not QueryProcessEvidence:
        raise TypeError("query evidence must be QueryProcessEvidence")
    return BenchmarkQueryFacts(
        schema_version=evidence.schema_version,
        portable_artifact_key=_portable_artifact_key(
            contract_digest=evidence.contract_digest,
            corpus_digest=evidence.corpus_digest,
            fixture_digest=evidence.fixture_digest,
            resource_id=evidence.resource_id,
            canonical_store_id=evidence.canonical_store_id,
            execution_path=evidence.execution_path,
            path_config_digest=evidence.path_config_digest,
            actual_index_kind=evidence.actual_index_kind,
            record_count=evidence.record_count,
            generation=evidence.generation,
            sidecar_digest=evidence.artifact_pre.sidecar_digest,
            manifest_digest=evidence.artifact_pre.manifest_digest,
            latency_evidence_digest=evidence.latency_evidence_digest,
        ),
        contract_digest=evidence.contract_digest,
        corpus_digest=evidence.corpus_digest,
        corpus_record_count=evidence.corpus_record_count,
        fixture_digest=evidence.fixture_digest,
        fixture_record_count=evidence.fixture_record_count,
        resource_id=evidence.resource_id,
        canonical_store_id=evidence.canonical_store_id,
        execution_path=evidence.execution_path,
        path_config_digest=evidence.path_config_digest,
        actual_index_kind=evidence.actual_index_kind,
        record_count=evidence.record_count,
        generation=evidence.generation,
        process_evidence_digest=evidence.process_evidence_digest,
        artifact_baseline_digest=evidence.artifact_baseline_digest,
        sidecar_digest=evidence.artifact_pre.sidecar_digest,
        manifest_digest=evidence.artifact_pre.manifest_digest,
        latency_evidence_digest=evidence.latency_evidence_digest,
        processes_distinct=evidence.processes_distinct,
        query_peak_rss_bytes=evidence.query_peak_rss_bytes,
        query_rss_start_bytes=evidence.query_rss_start_bytes,
        query_rss_terminal_bytes=evidence.query_rss_terminal_bytes,
        query_rss_unit=evidence.query_rss_unit,
        query_rss_scope=evidence.query_rss_scope,
        environment=evidence.environment,
        environment_digest=evidence.environment_digest,
        evidence_digest=evidence.evidence_digest,
    )


def _build_path_bundle(
    process_run: QueryProcessRunResult,
    oracle_evidence: OracleRecallEvidence,
    *,
    contract: BenchmarkContract,
    contract_digest: str,
) -> BenchmarkPathBundle:
    """Re-adjudicate one parent-run pair and derive its path bundle."""
    if type(process_run) is not QueryProcessRunResult:
        raise TypeError("process run must be QueryProcessRunResult")
    if type(oracle_evidence) is not OracleRecallEvidence:
        raise TypeError("oracle evidence must be OracleRecallEvidence")
    process_evidence = process_run.process_evidence
    evidence = process_run.evidence
    try:
        _adjudicate_evidence_against_process_evidence(
            evidence=evidence,
            process_evidence=process_evidence,
            query_child_pid=process_run.query_child_pid,
            request_protocol_digest=process_run.request_protocol_digest,
        )
    except QueryProcessError as error:
        raise ValueError(
            "combined benchmark evidence failed process/query re-adjudication"
        ) from error
    if type(process_evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    if type(evidence) is not QueryProcessEvidence:
        raise TypeError("query evidence must be QueryProcessEvidence")
    if process_run.run_root != process_evidence.run_root:
        raise ValueError("run root must match the process evidence")
    if process_run.fixture_path != process_evidence.fixture_path:
        raise ValueError("fixture path must match the process evidence")
    if process_run.artifact_pre != evidence.artifact_pre:
        raise ValueError("parent pre snapshot must match the query evidence")
    if process_run.artifact_post != evidence.artifact_post:
        raise ValueError("parent post snapshot must match the query evidence")
    if type(process_run.artifact_pre) is not ArtifactSnapshot:
        raise TypeError("parent pre snapshot must be ArtifactSnapshot")
    if type(process_run.artifact_post) is not ArtifactSnapshot:
        raise TypeError("parent post snapshot must be ArtifactSnapshot")
    if process_evidence.test_mode or not process_evidence.final_evidence:
        raise ValueError("combined evidence requires final process evidence")
    if evidence.process_test_mode or not evidence.final_evidence:
        raise ValueError("combined evidence requires final query evidence")
    if oracle_evidence.test_mode or not oracle_evidence.final_evidence:
        raise ValueError("combined evidence requires final oracle evidence")
    if (
        process_evidence.corpus_record_count != REAL_CORPUS_RECORD_COUNT
        or process_evidence.fixture_record_count != REAL_CORPUS_RECORD_COUNT
        or process_evidence.record_count != REAL_CORPUS_RECORD_COUNT
        or evidence.corpus_record_count != REAL_CORPUS_RECORD_COUNT
        or evidence.fixture_record_count != REAL_CORPUS_RECORD_COUNT
        or evidence.record_count != REAL_CORPUS_RECORD_COUNT
    ):
        raise ValueError(
            "combined evidence requires the literal 100000-record corpus"
        )
    path = process_evidence.execution_path
    if evidence.execution_path is not path:
        raise ValueError("query evidence path must match the process evidence")
    if oracle_evidence.execution_path is not path:
        raise ValueError("oracle evidence path must match the process evidence")
    latency = evidence.latency_evidence
    if type(latency) is not LatencyEvidence:
        raise TypeError("latency evidence must be LatencyEvidence")
    if latency.execution_path is not path:
        raise ValueError("latency evidence path must match the process evidence")
    if (
        process_evidence.contract != contract
        or evidence.latency_evidence.contract != contract
        or oracle_evidence.contract != contract
    ):
        raise ValueError("all combined evidence must bind the same frozen contract")
    if (
        process_evidence.contract_digest != contract_digest
        or evidence.contract_digest != contract_digest
        or latency.contract_digest != contract_digest
        or oracle_evidence.contract_digest != contract_digest
    ):
        raise ValueError(
            "all combined evidence must bind the same frozen contract digest"
        )
    expected_path_digest = (
        contract.fast_path_config_digest
        if path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else contract.fallback_path_config_digest
    )
    if (
        process_evidence.path_config_digest != expected_path_digest
        or evidence.path_config_digest != expected_path_digest
        or latency.path_config_digest != expected_path_digest
        or oracle_evidence.path_config_digest != expected_path_digest
    ):
        raise ValueError(
            "all combined evidence must bind the same path config digest"
        )
    expected_index_kind = _index_kind_for_path(path)
    if (
        process_evidence.actual_index_kind != expected_index_kind
        or evidence.actual_index_kind != expected_index_kind
        or oracle_evidence.store_index_kind != expected_index_kind
    ):
        raise ValueError(
            "all combined evidence must execute the same actual index kind"
        )
    if latency.exact_cohort_digest != contract.exact_cohort_digest:
        raise ValueError("exact cohort digest must match the frozen contract")
    if latency.fuzzy_cohort_digest != contract.fuzzy_cohort_digest:
        raise ValueError("fuzzy cohort digest must match the frozen contract")
    if oracle_evidence.oracle_subset_digest != contract.oracle_subset_digest:
        raise ValueError("oracle subset digest must match the frozen contract")
    if oracle_evidence.scorer_config_digest != contract.scorer_config_digest:
        raise ValueError("scorer config digest must match the frozen contract")
    if (
        latency.exact_sample_count != contract.exact_cohort_count
        or latency.fuzzy_sample_count != contract.fuzzy_cohort_count
    ):
        raise ValueError(
            "combined evidence requires the complete frozen latency samples"
        )
    if oracle_evidence.oracle_query_count != contract.oracle_query_count:
        raise ValueError("oracle query count must match the frozen contract")
    if evidence.environment != process_evidence.environment:
        raise ValueError("process and query environments must match")
    if evidence.environment_digest != process_evidence.environment_digest:
        raise ValueError("process and query environment digests must match")
    process_facts = _project_process_facts(process_evidence)
    query_facts = _project_query_facts(evidence)
    return BenchmarkPathBundle(
        process_facts=process_facts,
        query_facts=query_facts,
        oracle_evidence=oracle_evidence,
        latency_evidence=latency,
        report=_derive_report(
            contract=contract,
            contract_digest=contract_digest,
            process_facts=process_facts,
            query_facts=query_facts,
            oracle_evidence=oracle_evidence,
            latency_evidence=latency,
            environment=_merge_environment(
                process_facts.environment,
                query_facts.environment,
                latency.environment,
                oracle_evidence.environment,
            ),
        ),
    )


def combine_benchmark_evidence(
    process_run_fts5: QueryProcessRunResult,
    process_run_fallback: QueryProcessRunResult,
    oracle_evidence_fts5: OracleRecallEvidence,
    oracle_evidence_fallback: OracleRecallEvidence,
) -> BenchmarkEvidenceBundle:
    """Combine parent-adjudicated evidence into one strict Gate D bundle.

    Accepts exactly two parent-adjudicated ``QueryProcessRunResult`` values
    (FTS5_TRIGRAM then GRAM_FALLBACK, in enum order) plus exact-type final
    ``OracleRecallEvidence`` for the same two paths.  Returns an immutable
    portable bundle with one strict ``BenchmarkReport`` per path and one
    ``BenchmarkSuiteReport``.  Never publishes or mutates a capability
    manifest.
    """
    if type(process_run_fts5) is not QueryProcessRunResult:
        raise TypeError("fts5 process run must be QueryProcessRunResult")
    if type(process_run_fallback) is not QueryProcessRunResult:
        raise TypeError("fallback process run must be QueryProcessRunResult")
    if type(oracle_evidence_fts5) is not OracleRecallEvidence:
        raise TypeError("fts5 oracle evidence must be OracleRecallEvidence")
    if type(oracle_evidence_fallback) is not OracleRecallEvidence:
        raise TypeError(
            "fallback oracle evidence must be OracleRecallEvidence"
        )
    if (
        process_run_fts5.process_evidence.execution_path
        is not BenchmarkExecutionPath.FTS5_TRIGRAM
    ):
        raise ValueError("fts5 process run must execute FTS5_TRIGRAM")
    if (
        process_run_fallback.process_evidence.execution_path
        is not BenchmarkExecutionPath.GRAM_FALLBACK
    ):
        raise ValueError("fallback process run must execute GRAM_FALLBACK")
    if (
        oracle_evidence_fts5.execution_path
        is not BenchmarkExecutionPath.FTS5_TRIGRAM
    ):
        raise ValueError("fts5 oracle evidence must execute FTS5_TRIGRAM")
    if (
        oracle_evidence_fallback.execution_path
        is not BenchmarkExecutionPath.GRAM_FALLBACK
    ):
        raise ValueError("fallback oracle evidence must execute GRAM_FALLBACK")
    contract = process_run_fts5.process_evidence.contract
    if type(contract) is not BenchmarkContract:
        raise TypeError("fts5 process contract must be BenchmarkContract")
    contract_snapshot = contract_from_json(contract_to_json(contract))
    if type(contract_snapshot) is not BenchmarkContract:
        raise TypeError("fts5 process contract snapshot must be BenchmarkContract")
    contract = contract_snapshot
    contract_digest = process_run_fts5.process_evidence.contract_digest
    if contract_digest != benchmark_contract_digest(contract):
        raise ValueError("fts5 process contract digest must bind the contract")
    if (
        process_run_fallback.process_evidence.contract_digest != contract_digest
        or process_run_fallback.process_evidence.contract != contract
    ):
        raise ValueError(
            "both process runs must bind the same frozen benchmark contract"
        )
    fts5 = _build_path_bundle(
        process_run_fts5,
        oracle_evidence_fts5,
        contract=contract,
        contract_digest=contract_digest,
    )
    fallback = _build_path_bundle(
        process_run_fallback,
        oracle_evidence_fallback,
        contract=contract,
        contract_digest=contract_digest,
    )
    suite_contract = _suite_contract(contract, contract_digest)
    suite_report = _derive_suite_report(
        suite_contract,
        fts5.report,
        fallback.report,
    )
    return BenchmarkEvidenceBundle(
        schema_version=BENCHMARK_BUNDLE_SCHEMA_VERSION,
        contract=contract,
        contract_digest=contract_digest,
        suite_contract=suite_contract,
        suite_contract_digest=benchmark_suite_contract_digest(suite_contract),
        fts5=fts5,
        fallback=fallback,
        suite_report=suite_report,
    )


# --- Gate D boundary --------------------------------------------------------


def retrieval_benchmark_evidence_by_path(
    bundle: BenchmarkEvidenceBundle,
    execution_path: BenchmarkExecutionPath,
    *,
    generated_at_utc: str,
    valid_until_utc: str,
) -> RetrievalBenchmarkEvidence:
    """Construct one Gate D evidence value for the requested path.

    The report is the strict derived report for that execution path and the
    validity window is validated by the ``RetrievalBenchmarkEvidence``
    constructor.  This slice never refreshes or publishes a capability
    manifest.
    """
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution_path must be BenchmarkExecutionPath")
    if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        path_bundle = bundle.fts5
    elif execution_path is BenchmarkExecutionPath.GRAM_FALLBACK:
        path_bundle = bundle.fallback
    else:
        raise ValueError("execution path is unsupported")
    if path_bundle.report.execution_path is not execution_path:
        raise ValueError(
            "benchmark report path must match the requested execution path"
        )
    return RetrievalBenchmarkEvidence(
        report=path_bundle.report,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
    )


def retrieval_benchmark_evidence_pair(
    bundle: BenchmarkEvidenceBundle,
    *,
    generated_at_utc: str,
    valid_until_utc: str,
) -> tuple[RetrievalBenchmarkEvidence, RetrievalBenchmarkEvidence]:
    """Construct both Gate D evidence values sharing one validity window."""
    if type(bundle) is not BenchmarkEvidenceBundle:
        raise TypeError("bundle must be BenchmarkEvidenceBundle")
    fts5_evidence = retrieval_benchmark_evidence_by_path(
        bundle,
        BenchmarkExecutionPath.FTS5_TRIGRAM,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
    )
    fallback_evidence = retrieval_benchmark_evidence_by_path(
        bundle,
        BenchmarkExecutionPath.GRAM_FALLBACK,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
    )
    return fts5_evidence, fallback_evidence


# --- shared strict helpers --------------------------------------------------


def _require_identity(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_evidence_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_builtin_int(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a built-in int")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _require_environment(
    value: object,
    field_name: str,
) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a built-in tuple")
    environment = value
    benchmark_environment_digest(environment)
    return environment


__all__ = [
    "BENCHMARK_BUNDLE_DIGEST_KIND",
    "BENCHMARK_BUNDLE_DIGEST_VERSION",
    "BENCHMARK_BUNDLE_SCHEMA_VERSION",
    "BenchmarkEvidenceBundle",
    "BenchmarkPathBundle",
    "BenchmarkProcessFacts",
    "BenchmarkQueryFacts",
    "benchmark_evidence_bundle_digest",
    "benchmark_evidence_bundle_from_json",
    "benchmark_evidence_bundle_from_payload",
    "benchmark_evidence_bundle_to_json",
    "benchmark_evidence_bundle_to_payload",
    "combine_benchmark_evidence",
    "retrieval_benchmark_evidence_by_path",
    "retrieval_benchmark_evidence_pair",
]
