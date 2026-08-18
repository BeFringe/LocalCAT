"""Task 8.5C gate owner: combine, run, persist and publish Gate D evidence.

Ownership
---------
This module is the benchmark-v1 gate owner for Task 8.5C.  It consumes
parent-adjudicated migration/query evidence (``QueryProcessRunResult`` for
FTS5_TRIGRAM and GRAM_FALLBACK, in enum order) plus exact-type
``OracleRecallEvidence`` for the same two paths, re-adjudicates each
process/query pair from the retained process evidence, request digest,
artifact snapshots and nested evidence, derives one strict
``BenchmarkReport`` per execution path and one ``BenchmarkSuiteReport``,
and produces an immutable portable ``BenchmarkEvidenceBundle`` with a strict
closed-schema canonical JSON codec.  It is an offline validation/batch owner
only: no production runtime module imports it.

The owner also runs the real Gate D pipeline through one locked entry point
(``run_benchmark_gate_d``): the real oracle-recall-suite port is bound
internally and called first on the two dedicated oracle roots; only when
both FTS5_TRIGRAM and GRAM_FALLBACK oracle obligations are fully clear
(no above-threshold and no top-10 miss) do the real process-migration and
query-process ports run on the two dedicated process roots under the same
exclusive private run directory; the
combined bundle is atomically persisted to an absent final path with a
durable no-follow readback, and only the exact owned temporary tree is
cleaned afterwards.  Test injection exists only behind an explicit private
test seam that always marks its result as test-only and can never produce
final/published evidence.

Gate D capability publication (``publish_retrieval_capability_gate_d``)
accepts an exact ``RetrievalCapabilityManifest`` base, the exact durable
runner result, an exact ``RetrievalCapabilityPublisher`` and explicit UTC
instants;
it composes one new manifest preserving every envelope and Gate C fact
byte-for-byte/value-for-value and replacing only the two fuzzy benchmark
fields from ``retrieval_benchmark_evidence_pair``.  A Core-private publisher
transition verifies the candidate's retained Gate C decisions and per-path
report truth, prepares the immutable publication result, and only then makes
that exact snapshot query-visible.  The owner never constructs a publisher
or evaluator, never imports the evaluator, and never bypasses the publisher
to grant availability.

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
  exactly 1.0; a path whose literal oracle obligations are not fully clear
  (``missing_above_threshold_total`` or ``missing_top10_total`` nonzero)
  must therefore fail ``CANDIDATE_RECALL``, and the runner stops before any
  100000-record migration/query work for that path.
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
  process/query/oracle facts and digests, the current proof-query version,
  one no-follow exact implementation-source fingerprint, and strict report
  codec results.  The fingerprint publishes only a digest, not source paths
  or bytes, and release validation recomputes it from the exact checkout.
  It never contains absolute run-root/fixture paths, PIDs,
  inode/device/mtime, protocol digests that bind those locators, query/
  source/target bodies, or reusable handles.  Raw ``TMBenchmarkProcessEvidence``
  and ``QueryProcessRunResult`` values are never serialized.
- The bundle codec is closed-schema canonical JSON: duplicate keys,
  NaN/Infinity, bool-as-int, unknown/missing keys, digest drift, nested
  report drift, path swap/duplication, discarded samples, forged
  self-consistent caller fields and one-path-only input all fail closed.
  A strict round trip reproduces the same exact immutable value.
- The real runner locks its default ports internally (``test_mode=False``,
  no caller-supplied oracle facts).  It runs the literal oracle suite first
  on the dedicated oracle roots and stops before any migration/query work
  unless both paths show zero above-threshold and zero top-10 misses; only
  then does it run one migration per execution path in enum order and one
  query per retained final process evidence with no migration rerun, then
  one combination.  The private test seam requires injected exact-type ports
  and always returns a ``test_mode=True`` result.
- ``work_root`` must be an existing empty direct native directory; the
  runner creates one exclusive 0700 private child and four exact dedicated
  roots under it, and cleans only that exact created directory after a
  durable readback or a safe failure.  Identity-drifted or ambiguous content
  is never deleted: the owned root is left and a stable cleanup-pending
  failure is returned.  The evidence final is published atomically to an
  absent final only, fsynced, read back no-follow and strictly re-decoded;
  failures never report success and never overwrite or delete foreign files.
- Diagnostics are stable codes only: no absolute paths, PIDs, protocol
  digests or bodies.  The immutable run result carries the strict readback
  bundle, its stable digest and the final artifact's canonical byte size
  and SHA-256 digest only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, TypeVar, cast

from tm_benchmark import (
    BENCHMARK_IMPLEMENTATION_FINGERPRINT_VERSION,
    BENCHMARK_IMPLEMENTATION_SOURCE_PATHS,
    benchmark_digest,
    benchmark_implementation_fingerprint,
    load_benchmark_contract,
)
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
    run_oracle_recall_suite,
)
from tm_benchmark_process import (
    PROCESS_EVIDENCE_SCHEMA_VERSION,
    TMBenchmarkProcessEvidence,
    run_process_migration_evidence,
)
from tm_benchmark_query_process import (
    QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
    ArtifactSnapshot,
    QueryProcessError,
    QueryProcessEvidence,
    QueryProcessRunResult,
    _adjudicate_evidence_against_process_evidence,
    _resolved_absolute_path_string,
    run_query_process_evidence,
)
from tm_contracts import (
    BENCHMARK_SUITE_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION,
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
from tm_retrieval_capability import (
    RetrievalBenchmarkEvidence,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    _RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR,
    _validated_refresh_retrieval_capability,
)

BENCHMARK_BUNDLE_SCHEMA_VERSION = "tm-benchmark-bundle-v2"
BENCHMARK_BUNDLE_DIGEST_VERSION = "tm-benchmark-bundle-digest-v2"
BENCHMARK_BUNDLE_DIGEST_KIND = "benchmark-bundle"
BENCHMARK_PORTABLE_ARTIFACT_KEY_VERSION = (
    "tm-benchmark-portable-artifact-key-v1"
)

_NATIVE_PATH_TYPE = type(Path())
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
        "implementation_fingerprint",
        "proof_query_version",
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
        "implementation_fingerprint",
        "migration_elapsed_ns",
        "path_config_digest",
        "peak_rss_bytes",
        "record_count",
        "resource_id",
        "proof_query_version",
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
        "implementation_fingerprint",
        "path_config_digest",
        "portable_artifact_key",
        "process_evidence_digest",
        "processes_distinct",
        "proof_query_version",
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
    proof_query_version: str
    implementation_fingerprint: str
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
    if facts.proof_query_version != CANDIDATE_PROOF_QUERY_VERSION:
        raise ValueError("process facts proof query version is not current")
    _require_evidence_digest(
        facts.implementation_fingerprint,
        "process implementation fingerprint",
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
        "implementation_fingerprint": facts.implementation_fingerprint,
        "migration_elapsed_ns": facts.migration_elapsed_ns,
        "path_config_digest": facts.path_config_digest,
        "peak_rss_bytes": facts.peak_rss_bytes,
        "record_count": facts.record_count,
        "resource_id": facts.resource_id,
        "proof_query_version": facts.proof_query_version,
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
        proof_query_version=_as_str(
            fields["proof_query_version"],
            "proof query version",
        ),
        implementation_fingerprint=_as_digest(
            fields["implementation_fingerprint"],
            "implementation fingerprint",
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
    proof_query_version: str
    implementation_fingerprint: str
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
    if facts.proof_query_version != CANDIDATE_PROOF_QUERY_VERSION:
        raise ValueError("query facts proof query version is not current")
    _require_evidence_digest(
        facts.implementation_fingerprint,
        "query implementation fingerprint",
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
        "implementation_fingerprint": facts.implementation_fingerprint,
        "path_config_digest": facts.path_config_digest,
        "process_evidence_digest": facts.process_evidence_digest,
        "sidecar_digest": facts.sidecar_digest,
        "manifest_digest": facts.manifest_digest,
        "latency_evidence_digest": facts.latency_evidence_digest,
        "processes_distinct": facts.processes_distinct,
        "proof_query_version": facts.proof_query_version,
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
        proof_query_version=_as_str(
            fields["proof_query_version"],
            "proof query version",
        ),
        implementation_fingerprint=_as_digest(
            fields["implementation_fingerprint"],
            "implementation fingerprint",
        ),
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
    raw samples, immutable process/query/oracle facts and digests, and the
    proof/source implementation identity measured by the owner.  It is
    safe to persist: it never contains absolute run-root/fixture paths, PIDs,
    inode/device/mtime facts, locator-binding protocol digests, query/source/
    target bodies or reusable handles.
    """

    schema_version: str
    proof_query_version: str
    implementation_fingerprint: str
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
    if bundle.proof_query_version != CANDIDATE_PROOF_QUERY_VERSION:
        raise ValueError(
            "bundle proof query version must be "
            f"{CANDIDATE_PROOF_QUERY_VERSION}"
        )
    if (
        type(bundle.implementation_fingerprint) is not str
        or _SHA256_DIGEST.fullmatch(bundle.implementation_fingerprint) is None
    ):
        raise ValueError(
            "bundle implementation fingerprint must be a SHA-256 digest"
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
        if (
            path_bundle.process_facts.proof_query_version
            != bundle.proof_query_version
            or path_bundle.query_facts.proof_query_version
            != bundle.proof_query_version
            or path_bundle.oracle_evidence.proof_query_version
            != bundle.proof_query_version
        ):
            raise ValueError("nested proof query versions must match the bundle")
        if (
            path_bundle.process_facts.implementation_fingerprint
            != bundle.implementation_fingerprint
            or path_bundle.query_facts.implementation_fingerprint
            != bundle.implementation_fingerprint
            or path_bundle.oracle_evidence.implementation_fingerprint
            != bundle.implementation_fingerprint
        ):
            raise ValueError(
                "nested implementation fingerprints must match the bundle"
            )
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
        "implementation_fingerprint": bundle.implementation_fingerprint,
        "proof_query_version": bundle.proof_query_version,
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
    proof_query_version = _as_str(
        fields["proof_query_version"],
        "proof query version",
    )
    if proof_query_version != CANDIDATE_PROOF_QUERY_VERSION:
        raise ValueError("bundle proof query version is unsupported")
    implementation_fingerprint = _as_digest(
        fields["implementation_fingerprint"],
        "implementation fingerprint",
    )
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
        proof_query_version=proof_query_version,
        implementation_fingerprint=implementation_fingerprint,
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
        proof_query_version=evidence.proof_query_version,
        implementation_fingerprint=evidence.implementation_fingerprint,
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
        proof_query_version=evidence.proof_query_version,
        implementation_fingerprint=evidence.implementation_fingerprint,
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
    if _resolved_absolute_path_string(
        process_run.run_root,
        "combined run root",
    ) != _resolved_absolute_path_string(
        process_evidence.run_root,
        "process evidence run root",
    ):
        raise ValueError("run root must match the process evidence")
    if _resolved_absolute_path_string(
        process_run.fixture_path,
        "combined fixture path",
    ) != _resolved_absolute_path_string(
        process_evidence.fixture_path,
        "process evidence fixture path",
    ):
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
    source_bindings = {
        (
            process_evidence.proof_query_version,
            process_evidence.implementation_fingerprint,
        ),
        (
            evidence.proof_query_version,
            evidence.implementation_fingerprint,
        ),
        (
            oracle_evidence.proof_query_version,
            oracle_evidence.implementation_fingerprint,
        ),
    }
    if source_bindings != {
        (CANDIDATE_PROOF_QUERY_VERSION, process_evidence.implementation_fingerprint)
    }:
        raise ValueError(
            "process/query/oracle evidence must bind one current implementation"
        )
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
    if (
        fts5.process_facts.proof_query_version
        != fallback.process_facts.proof_query_version
        or fts5.process_facts.implementation_fingerprint
        != fallback.process_facts.implementation_fingerprint
    ):
        raise ValueError("both paths must bind one proof/source implementation")
    return BenchmarkEvidenceBundle(
        schema_version=BENCHMARK_BUNDLE_SCHEMA_VERSION,
        proof_query_version=fts5.process_facts.proof_query_version,
        implementation_fingerprint=fts5.process_facts.implementation_fingerprint,
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


# --- Gate D owner-driven runner ----------------------------------------------


class BenchmarkGateDError(RuntimeError):
    """Code-only Gate D runner/publication failure; never leaks bodies."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("error code must be a non-empty string")
        self.error_code = error_code
        super().__init__(error_code)


_GATE_D_RUN_RECEIPT_FACTORY_KEY = object()


class _GateDRunReceipt:
    """Module-private issuance proof for one durable runner readback."""

    __slots__ = (
        "artifact_digest",
        "artifact_size",
        "bundle",
        "bundle_digest",
        "_sealed",
        "test_mode",
    )

    artifact_digest: str
    artifact_size: int
    bundle: BenchmarkEvidenceBundle
    bundle_digest: str
    _sealed: bool
    test_mode: bool

    def __init__(
        self,
        *,
        bundle: BenchmarkEvidenceBundle,
        bundle_digest: str,
        artifact_size: int,
        artifact_digest: str,
        test_mode: bool,
        _factory_key: object,
    ) -> None:
        if _factory_key is not _GATE_D_RUN_RECEIPT_FACTORY_KEY:
            raise TypeError("Gate D run receipts are owner-issued")
        self._sealed = False
        self.bundle = bundle
        self.bundle_digest = bundle_digest
        self.artifact_size = artifact_size
        self.artifact_digest = artifact_digest
        self.test_mode = test_mode
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("Gate D run receipts are immutable")
        object.__setattr__(self, name, value)

    def __copy__(self) -> _GateDRunReceipt:
        return self

    def __deepcopy__(self, _memo: object) -> _GateDRunReceipt:
        return self


@dataclass(frozen=True)
class BenchmarkGateDRunResult:
    """Immutable result of one owner-driven Gate D run.

    Carries the strict durable readback bundle, its stable canonical digest
    and the final artifact's canonical byte size and SHA-256 digest.  It
    never exposes temporary run roots, PIDs, device/inode identities or
    paths.
    """

    bundle: BenchmarkEvidenceBundle
    bundle_digest: str
    artifact_size: int
    artifact_digest: str
    test_mode: bool
    _receipt: _GateDRunReceipt = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.bundle) is not BenchmarkEvidenceBundle:
            raise TypeError("run result bundle must be BenchmarkEvidenceBundle")
        if (
            type(self.bundle_digest) is not str
            or _SHA256_DIGEST.fullmatch(self.bundle_digest) is None
        ):
            raise ValueError("run result digest must be a SHA-256 digest")
        if self.bundle_digest != benchmark_evidence_bundle_digest(self.bundle):
            raise ValueError("run result digest must bind the readback bundle")
        if type(self.artifact_size) is not int or self.artifact_size < 0:
            raise ValueError(
                "run result artifact size must be a non-negative int"
            )
        if (
            type(self.artifact_digest) is not str
            or _SHA256_DIGEST.fullmatch(self.artifact_digest) is None
        ):
            raise ValueError(
                "run result artifact digest must be a SHA-256 digest"
            )
        artifact_bytes = benchmark_evidence_bundle_to_json(
            self.bundle
        ).encode("utf-8")
        if (
            len(artifact_bytes) != self.artifact_size
            or hashlib.sha256(artifact_bytes).hexdigest()
            != self.artifact_digest
        ):
            raise ValueError(
                "run result artifact size/digest must bind the artifact bytes"
            )
        if type(self.test_mode) is not bool:
            raise TypeError("run result test mode must be a built-in bool")
        if type(self._receipt) is not _GateDRunReceipt or (
            self._receipt.bundle is not self.bundle
            or self._receipt.bundle_digest != self.bundle_digest
            or self._receipt.artifact_size != self.artifact_size
            or self._receipt.artifact_digest != self.artifact_digest
            or self._receipt.test_mode is not self.test_mode
        ):
            raise TypeError("run result must carry its owner-issued receipt")


def _issue_benchmark_gate_d_run_result(
    *,
    bundle: BenchmarkEvidenceBundle,
    bundle_digest: str,
    artifact_size: int,
    artifact_digest: str,
    test_mode: bool,
) -> BenchmarkGateDRunResult:
    receipt = _GateDRunReceipt(
        bundle=bundle,
        bundle_digest=bundle_digest,
        artifact_size=artifact_size,
        artifact_digest=artifact_digest,
        test_mode=test_mode,
        _factory_key=_GATE_D_RUN_RECEIPT_FACTORY_KEY,
    )
    return BenchmarkGateDRunResult(
        bundle=bundle,
        bundle_digest=bundle_digest,
        artifact_size=artifact_size,
        artifact_digest=artifact_digest,
        test_mode=test_mode,
        _receipt=receipt,
    )


@dataclass(frozen=True)
class RetrievalCapabilityPublicationResult:
    """Immutable result of one Gate D capability publication."""

    manifest: RetrievalCapabilityManifest
    snapshot: RetrievalCapabilitySnapshot

    def __post_init__(self) -> None:
        if type(self.manifest) is not RetrievalCapabilityManifest:
            raise TypeError(
                "publication result manifest must be RetrievalCapabilityManifest"
            )
        if type(self.snapshot) is not RetrievalCapabilitySnapshot:
            raise TypeError(
                "publication result snapshot must be RetrievalCapabilitySnapshot"
            )


@dataclass(frozen=True)
class _GateDRunnerPorts:
    """Private exact-type port set for the owner-driven runner test seam."""

    run_process_migration_evidence: Callable[..., TMBenchmarkProcessEvidence]
    run_query_process_evidence: Callable[..., QueryProcessRunResult]
    run_oracle_recall_suite: Callable[
        ...,
        tuple[OracleRecallEvidence, OracleRecallEvidence],
    ]
    combine_benchmark_evidence: Callable[..., BenchmarkEvidenceBundle]


_DEFAULT_GATE_D_RUNNER_PORTS = _GateDRunnerPorts(
    run_process_migration_evidence=run_process_migration_evidence,
    run_query_process_evidence=run_query_process_evidence,
    run_oracle_recall_suite=run_oracle_recall_suite,
    combine_benchmark_evidence=combine_benchmark_evidence,
)


_PRIVATE_RUN_DIR_PREFIX = ".tm-gate-d-run-"
_EVIDENCE_TEMP_PREFIX = ".tm-gate-d-evidence-"
_EVIDENCE_TEMP_SUFFIX = ".tmp"
_STRICT_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z"
)


def _directory_identity(path: Path, error_code: str) -> tuple[int, int]:
    """Return the no-follow identity of one real direct directory."""
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise BenchmarkGateDError(error_code) from error
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise BenchmarkGateDError(error_code)
    return (observed.st_dev, observed.st_ino)


def _validate_contract_path(contract_path: object) -> None:
    if type(contract_path) is not _NATIVE_PATH_TYPE:
        raise BenchmarkGateDError("GATE_D.CONTRACT_INVALID")


def _load_contract(contract_path: Path) -> BenchmarkContract:
    try:
        return load_benchmark_contract(contract_path)
    except (TypeError, ValueError) as error:
        raise BenchmarkGateDError("GATE_D.CONTRACT_INVALID") from error


def _validate_work_root(work_root: object) -> Path:
    if type(work_root) is not _NATIVE_PATH_TYPE:
        raise BenchmarkGateDError("GATE_D.WORK_ROOT_INVALID")
    _ = _directory_identity(work_root, "GATE_D.WORK_ROOT_INVALID")
    try:
        entries = os.listdir(work_root)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.WORK_ROOT_INVALID") from error
    if entries:
        raise BenchmarkGateDError("GATE_D.WORK_ROOT_INVALID")
    return work_root


def _validate_evidence_path(evidence_path: object) -> Path:
    if (
        type(evidence_path) is not _NATIVE_PATH_TYPE
        or not evidence_path.is_absolute()
    ):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PATH_INVALID")
    _ = _directory_identity(
        evidence_path.parent,
        "GATE_D.EVIDENCE_PATH_INVALID",
    )
    return evidence_path


def _require_outside_run_subtree(
    private_dir: Path,
    evidence_path: Path,
) -> None:
    try:
        private_resolved = private_dir.resolve(strict=True)
        parent_resolved = evidence_path.parent.resolve(strict=True)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PATH_INVALID") from error
    if (
        parent_resolved == private_resolved
        or private_resolved in parent_resolved.parents
    ):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PATH_INVALID")


def _create_private_run_dir(
    work_root: Path,
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    """Create one exclusive 0700 private child; return identities."""
    work_identity = _directory_identity(
        work_root,
        "GATE_D.WORK_ROOT_INVALID",
    )
    try:
        private_dir = Path(
            tempfile.mkdtemp(
                prefix=_PRIVATE_RUN_DIR_PREFIX,
                dir=str(work_root),
            )
        )
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.RUN_ROOT_CREATION_FAILED") from error
    private_identity = _directory_identity(
        private_dir,
        "GATE_D.RUN_ROOT_CREATION_FAILED",
    )
    return private_dir, work_identity, private_identity


def _create_dedicated_roots(
    private_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    roots: list[Path] = []
    try:
        for prefix in (
            "process-fts5-",
            "process-fallback-",
            "oracle-fts5-",
            "oracle-fallback-",
        ):
            roots.append(
                Path(
                    tempfile.mkdtemp(
                        prefix=prefix,
                        dir=str(private_dir),
                    )
                )
            )
        if len({root.resolve() for root in roots}) != 4:
            raise BenchmarkGateDError("GATE_D.RUN_ROOT_CREATION_FAILED")
        process_fts5, process_fallback, oracle_fts5, oracle_fallback = roots
        return process_fts5, process_fallback, oracle_fts5, oracle_fallback
    except BaseException:
        # remove only the empty roots this call created; leave anything
        # that cannot be proven ours for the caller's strict cleanup
        for root in roots:
            try:
                os.rmdir(root)
            except OSError:
                pass
        raise


def _require_test_count(value: object) -> int:
    if (
        type(value) is not int
        or not (1 <= value < REAL_CORPUS_RECORD_COUNT)
    ):
        raise BenchmarkGateDError("GATE_D.TEST_PARAMETERS_INVALID")
    return value


def _require_test_seed(value: object) -> int:
    if type(value) is not int or value < 0:
        raise BenchmarkGateDError("GATE_D.TEST_PARAMETERS_INVALID")
    return value


def _write_all_bytes(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(
    path: Path,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Fsync one no-follow directory, optionally bound to its identity."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISDIR(observed.st_mode) or (
            expected_identity is not None
            and (observed.st_dev, observed.st_ino) != expected_identity
        ):
            raise OSError("directory identity changed")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_owned_file_bytes(
    path: Path,
    expected_identity: tuple[int, int],
) -> bytes:
    """No-follow stable read with descriptor and path revalidation."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != expected_identity
        ):
            raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        terminal = os.fstat(descriptor)
        stable_metadata = (
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(terminal.st_mode)
            or terminal.st_nlink != 1
            or (terminal.st_dev, terminal.st_ino) != expected_identity
            or (
                terminal.st_size,
                terminal.st_mtime_ns,
                terminal.st_ctime_ns,
            )
            != stable_metadata
        ):
            raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    finally:
        os.close(descriptor)
    try:
        final = os.lstat(path)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != expected_identity
        or (final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        != stable_metadata
    ):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")
    return bytes(payload)


def _remove_owned_file_if_ours(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    """Identity-bound removal; fail explicitly when cleanup is incomplete."""
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink not in (1, 2)
        or (observed.st_dev, observed.st_ino) != expected_identity
    ):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    try:
        os.unlink(path)
        _fsync_directory(path.parent)
    except (OSError, BenchmarkGateDError) as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error


def _require_absent_final(evidence_path: Path) -> None:
    try:
        os.lstat(evidence_path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    raise BenchmarkGateDError("GATE_D.EVIDENCE_EXISTS")


def _create_evidence_temp(
    parent: Path,
) -> tuple[Path, int, tuple[int, int]]:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=_EVIDENCE_TEMP_PREFIX,
            suffix=_EVIDENCE_TEMP_SUFFIX,
            dir=str(parent),
        )
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    try:
        observed = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        os.close(descriptor)
        try:
            os.unlink(name)
        except OSError:
            pass
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")
    return Path(name), descriptor, (observed.st_dev, observed.st_ino)


def _write_evidence_temp(
    descriptor: int,
    payload: bytes,
) -> None:
    try:
        _write_all_bytes(descriptor, payload)
        _fsync_descriptor(descriptor)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error


def _revalidate_evidence_temp(
    temp_path: Path,
    temp_identity: tuple[int, int],
    parent_identity: tuple[int, int],
) -> None:
    try:
        observed = os.lstat(temp_path)
        parent_st = os.lstat(temp_path.parent)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != temp_identity
        or (parent_st.st_dev, parent_st.st_ino) != parent_identity
    ):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")


def _verify_replaced_final(
    evidence_path: Path,
    temp_identity: tuple[int, int],
    parent_identity: tuple[int, int],
) -> None:
    try:
        observed = os.lstat(evidence_path)
        parent_st = os.lstat(evidence_path.parent)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != temp_identity
        or (parent_st.st_dev, parent_st.st_ino) != parent_identity
    ):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")


def _publish_evidence_bundle(
    bundle: BenchmarkEvidenceBundle,
    evidence_path: Path,
) -> tuple[str, int, str, BenchmarkEvidenceBundle]:
    """Atomically persist one bundle and return its strict durable readback.

    Writes the canonical JSON to one exclusive same-parent temporary,
    fsyncs it, revalidates the temp identity/single-link/parent, atomically
    links it to an absent final only, unlinks the temporary name, fsyncs the
    parent, then reads the final
    no-follow and strictly re-decodes it.  The readback bundle is
    authoritative only when value/digest/bytes all match.  Returns the
    stable bundle digest, the final artifact's canonical byte size and its
    SHA-256 digest alongside the readback bundle.  Foreign files are never
    overwritten or deleted; failures never report success.
    """

    serialized = benchmark_evidence_bundle_to_json(bundle)
    digest = benchmark_evidence_bundle_digest(bundle)
    payload = serialized.encode("utf-8")
    artifact_size = len(payload)
    artifact_digest = hashlib.sha256(payload).hexdigest()
    parent = evidence_path.parent
    try:
        parent_st = os.lstat(parent)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED")
    parent_identity = (parent_st.st_dev, parent_st.st_ino)
    _require_absent_final(evidence_path)
    temp_path, descriptor, temp_identity = _create_evidence_temp(parent)
    published = False
    try:
        try:
            _write_evidence_temp(descriptor, payload)
        finally:
            os.close(descriptor)
        _revalidate_evidence_temp(
            temp_path,
            temp_identity,
            parent_identity,
        )
        _require_absent_final(evidence_path)
        try:
            # Same-directory hard-link publication is atomic and refuses an
            # existing final. Unlike os.replace, it cannot overwrite a file
            # created after the last absence check.
            os.link(temp_path, evidence_path, follow_symlinks=False)
        except FileExistsError as error:
            raise BenchmarkGateDError("GATE_D.EVIDENCE_EXISTS") from error
        except OSError as error:
            raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
        published = True
        try:
            os.unlink(temp_path)
        except OSError as error:
            raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
        _verify_replaced_final(evidence_path, temp_identity, parent_identity)
        _fsync_directory(parent, parent_identity)
        readback_bytes = _read_owned_file_bytes(
            evidence_path,
            temp_identity,
        )
        if readback_bytes != payload:
            raise BenchmarkGateDError("GATE_D.EVIDENCE_READBACK_MISMATCH")
        try:
            readback = benchmark_evidence_bundle_from_json(
                readback_bytes.decode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise BenchmarkGateDError(
                "GATE_D.EVIDENCE_READBACK_MISMATCH"
            ) from error
        if (
            readback != bundle
            or benchmark_evidence_bundle_digest(readback) != digest
        ):
            raise BenchmarkGateDError("GATE_D.EVIDENCE_READBACK_MISMATCH")
        try:
            implementation_after_publish = benchmark_implementation_fingerprint()
        except (TypeError, ValueError) as error:
            raise BenchmarkGateDError(
                "GATE_D.IMPLEMENTATION_INVALID"
            ) from error
        if implementation_after_publish != bundle.implementation_fingerprint:
            raise BenchmarkGateDError("GATE_D.IMPLEMENTATION_CHANGED")
    except BenchmarkGateDError:
        if published:
            _remove_owned_file_if_ours(evidence_path, temp_identity)
        _remove_owned_file_if_ours(temp_path, temp_identity)
        raise
    except OSError as error:
        if published:
            _remove_owned_file_if_ours(evidence_path, temp_identity)
        _remove_owned_file_if_ours(temp_path, temp_identity)
        raise BenchmarkGateDError("GATE_D.EVIDENCE_PUBLISH_FAILED") from error
    return digest, artifact_size, artifact_digest, readback


def _remove_owned_tree(
    path: Path,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Recursively remove one owned tree, refusing any identity drift.

    Every directory level is identity-bound: a level whose identity changed
    or that is not the exact directory observed by its parent is refused
    before any of its content is touched.  Symlinks and multi-link files are
    never followed, deleted or replaced; a single-link regular file is
    unlinked only inside an identity-verified owned directory.  On any
    doubt the owned root is left untouched and ``GATE_D.CLEANUP_PENDING`` is
    raised instead of deleting ambiguous content.
    """
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or (
            expected_identity is not None
            and (observed.st_dev, observed.st_ino) != expected_identity
        )
    ):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    identity = (observed.st_dev, observed.st_ino)
    try:
        names = os.listdir(path)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    for name in names:
        entry = path / name
        try:
            entry_st = os.lstat(entry)
        except OSError as error:
            raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
        if stat.S_ISLNK(entry_st.st_mode):
            raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
        if stat.S_ISDIR(entry_st.st_mode):
            _remove_owned_tree(entry, (entry_st.st_dev, entry_st.st_ino))
            try:
                os.lstat(entry)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
            else:
                # our recursion removed the owned directory; any entry that
                # now occupies the name is foreign and is never touched
                raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
        else:
            if not stat.S_ISREG(entry_st.st_mode) or entry_st.st_nlink != 1:
                raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
            try:
                os.unlink(entry)
            except OSError as error:
                raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    try:
        final_st = os.lstat(path)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if (
        stat.S_ISLNK(final_st.st_mode)
        or not stat.S_ISDIR(final_st.st_mode)
        or (final_st.st_dev, final_st.st_ino) != identity
    ):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    try:
        os.rmdir(path)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error


def _cleanup_private_run_dir(
    private_dir: Path,
    private_identity: tuple[int, int],
    work_identity: tuple[int, int],
    *,
    expected_children: Mapping[str, tuple[int, int]],
) -> None:
    """Remove exactly the created private tree or fail closed.

    The immediate children must be exactly the recorded dedicated roots with
    their exact creation identities; any extra, missing, symlink or replaced
    entry leaves the whole owned tree untouched and raises
    ``GATE_D.CLEANUP_PENDING``.  Content inside an identity-verified root is
    removed by the identity-bound recursive remover only.
    """
    parent = private_dir.parent
    try:
        parent_st = os.lstat(parent)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if (
        stat.S_ISLNK(parent_st.st_mode)
        or not stat.S_ISDIR(parent_st.st_mode)
        or (parent_st.st_dev, parent_st.st_ino) != work_identity
    ):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    try:
        observed = os.lstat(private_dir)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != private_identity
    ):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    try:
        entries = os.listdir(private_dir)
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    if set(entries) != set(expected_children):
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    for name in expected_children:
        entry = private_dir / name
        try:
            entry_st = os.lstat(entry)
        except OSError as error:
            raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
        if (
            stat.S_ISLNK(entry_st.st_mode)
            or not stat.S_ISDIR(entry_st.st_mode)
            or (entry_st.st_dev, entry_st.st_ino)
            != expected_children[name]
        ):
            raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")
    try:
        _remove_owned_tree(private_dir, private_identity)
    except (OSError, BenchmarkGateDError) as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    try:
        os.lstat(private_dir)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
    raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING")


def _require_oracle_clear(evidence: OracleRecallEvidence) -> None:
    """Fail closed unless one path's literal oracle obligations are fully clear.

    The owner-derived literal oracle proof for a path must show no
    above-threshold and no top-10 miss before any real 100000-record
    migration/query work for that path may start.  One path's miss blocks
    the whole run before any expensive migration work; the two paths are
    independent and one clear path never masks the other.
    """

    if type(evidence) is not OracleRecallEvidence:
        raise BenchmarkGateDError("GATE_D.ORACLE_EVIDENCE_INVALID")
    if (
        evidence.missing_above_threshold_total != 0
        or evidence.missing_top10_total != 0
    ):
        path_code = (
            "FTS5"
            if evidence.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else "FALLBACK"
        )
        raise BenchmarkGateDError(
            f"GATE_D.ORACLE_{path_code}_MISS_BLOCKS_100K"
        )


def _run_benchmark_gate_d_core(
    *,
    contract_path: Path,
    work_root: Path,
    evidence_path: Path,
    ports: _GateDRunnerPorts,
    test_mode: bool,
    test_record_count: int | None,
    test_seed: int | None,
) -> BenchmarkGateDRunResult:
    if not test_mode and ports is not _DEFAULT_GATE_D_RUNNER_PORTS:
        raise BenchmarkGateDError("GATE_D.PORT_INJECTION_FORBIDDEN")
    if not test_mode and (
        test_record_count is not None or test_seed is not None
    ):
        raise BenchmarkGateDError("GATE_D.TEST_PARAMETERS_INVALID")
    _validate_contract_path(contract_path)
    _validate_work_root(work_root)
    _validate_evidence_path(evidence_path)
    try:
        implementation_before = benchmark_implementation_fingerprint()
    except (TypeError, ValueError) as error:
        raise BenchmarkGateDError("GATE_D.IMPLEMENTATION_INVALID") from error
    private_dir, work_identity, private_identity = _create_private_run_dir(
        work_root
    )
    root_identities: dict[str, tuple[int, int]] = {}
    try:
        _require_outside_run_subtree(private_dir, evidence_path)
        contract = _load_contract(contract_path)
        (
            process_fts5_root,
            process_fallback_root,
            oracle_fts5_root,
            oracle_fallback_root,
        ) = _create_dedicated_roots(private_dir)
        root_identities = {
            root.name: _directory_identity(
                root,
                "GATE_D.RUN_ROOT_CREATION_FAILED",
            )
            for root in (
                process_fts5_root,
                process_fallback_root,
                oracle_fts5_root,
                oracle_fallback_root,
            )
        }
        fts5_oracle, fallback_oracle = ports.run_oracle_recall_suite(
            contract=contract,
            fts5_run_root=oracle_fts5_root,
            fallback_run_root=oracle_fallback_root,
        )
        _require_oracle_clear(fts5_oracle)
        _require_oracle_clear(fallback_oracle)
        fts5_process = ports.run_process_migration_evidence(
            contract_path=contract_path,
            execution_path=BenchmarkExecutionPath.FTS5_TRIGRAM,
            run_root=process_fts5_root,
            test_mode=test_mode,
            test_record_count=test_record_count,
            test_seed=test_seed,
        )
        fallback_process = ports.run_process_migration_evidence(
            contract_path=contract_path,
            execution_path=BenchmarkExecutionPath.GRAM_FALLBACK,
            run_root=process_fallback_root,
            test_mode=test_mode,
            test_record_count=test_record_count,
            test_seed=test_seed,
        )
        fts5_run = ports.run_query_process_evidence(fts5_process)
        fallback_run = ports.run_query_process_evidence(fallback_process)
        bundle = ports.combine_benchmark_evidence(
            fts5_run,
            fallback_run,
            fts5_oracle,
            fallback_oracle,
        )
        if type(bundle) is not BenchmarkEvidenceBundle:
            raise BenchmarkGateDError("GATE_D.BUNDLE_INVALID")
        try:
            implementation_after = benchmark_implementation_fingerprint()
        except (TypeError, ValueError) as error:
            raise BenchmarkGateDError(
                "GATE_D.IMPLEMENTATION_INVALID"
            ) from error
        if (
            implementation_before != implementation_after
            or bundle.implementation_fingerprint != implementation_after
            or bundle.proof_query_version != CANDIDATE_PROOF_QUERY_VERSION
        ):
            raise BenchmarkGateDError("GATE_D.IMPLEMENTATION_CHANGED")
        bundle_digest, artifact_size, artifact_digest, readback = (
            _publish_evidence_bundle(bundle, evidence_path)
        )
    except BaseException:
        try:
            _cleanup_private_run_dir(
                private_dir,
                private_identity,
                work_identity,
                expected_children=root_identities,
            )
        except BenchmarkGateDError as error:
            raise BenchmarkGateDError("GATE_D.CLEANUP_PENDING") from error
        raise
    _cleanup_private_run_dir(
        private_dir,
        private_identity,
        work_identity,
        expected_children=root_identities,
    )
    return _issue_benchmark_gate_d_run_result(
        bundle=readback,
        bundle_digest=bundle_digest,
        artifact_size=artifact_size,
        artifact_digest=artifact_digest,
        test_mode=test_mode,
    )


def run_benchmark_gate_d(
    contract_path: Path,
    work_root: Path,
    evidence_path: Path,
) -> BenchmarkGateDRunResult:
    """Run the real owner-driven Gate D pipeline and persist the bundle.

    The real default ports are locked internally and run with
    ``test_mode=False``: first the literal owner-derived oracle-recall suite
    on distinct dedicated oracle roots (no caller-supplied or precomputed
    oracle facts); the run stops before any 100000-record work unless both
    FTS5_TRIGRAM and GRAM_FALLBACK oracle obligations are fully clear.  Only
    then does it run one process-migration evidence per
    ``BenchmarkExecutionPath`` in enum order on distinct dedicated process
    roots, one query-process evidence per retained final process evidence
    with no migration rerun, and exactly one ``combine_benchmark_evidence``
    on those exact values.  The combined bundle is atomically persisted to the absent
    ``evidence_path`` with a durable no-follow readback, and only the exact
    private run tree created under ``work_root`` is cleaned afterwards.
    Test-mode runs are only reachable through the private port seam and can
    never produce final evidence.
    """

    return _run_benchmark_gate_d_core(
        contract_path=contract_path,
        work_root=work_root,
        evidence_path=evidence_path,
        ports=_DEFAULT_GATE_D_RUNNER_PORTS,
        test_mode=False,
        test_record_count=None,
        test_seed=None,
    )


def _run_benchmark_gate_d_test(
    contract_path: Path,
    work_root: Path,
    evidence_path: Path,
    *,
    ports: _GateDRunnerPorts,
    test_record_count: int,
    test_seed: int | None = None,
) -> BenchmarkGateDRunResult:
    """Private owner test seam: injected exact-type ports, always test-marked.

    This seam is the only way to inject runner ports.  It refuses the
    default port set, requires explicit small test counts, and always marks
    its result ``test_mode=True`` so it can never yield final or published
    evidence.
    """

    if type(ports) is not _GateDRunnerPorts:
        raise TypeError("ports must be _GateDRunnerPorts")
    if ports is _DEFAULT_GATE_D_RUNNER_PORTS:
        raise BenchmarkGateDError("GATE_D.PORT_INJECTION_REQUIRED")
    _require_test_count(test_record_count)
    if test_seed is not None:
        _require_test_seed(test_seed)
    return _run_benchmark_gate_d_core(
        contract_path=contract_path,
        work_root=work_root,
        evidence_path=evidence_path,
        ports=ports,
        test_mode=True,
        test_record_count=test_record_count,
        test_seed=test_seed,
    )


# --- Gate D capability publication ------------------------------------------


def _validate_evidence_utc_string(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or _STRICT_UTC_TIMESTAMP.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a strict UTC timestamp")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def _require_utc_datetime(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(
            "evaluated_at_utc must be a timezone-aware UTC datetime"
        )
    return value


def _report_path_truth(evidence: RetrievalBenchmarkEvidence) -> bool:
    """True exactly when one path report honestly passes every gate."""
    report = evidence.report
    return report.passed is True and report.failed_gates == ()


def _verify_path_decisions_match_reports(
    snapshot: RetrievalCapabilitySnapshot,
    fts5_evidence: RetrievalBenchmarkEvidence,
    fallback_evidence: RetrievalBenchmarkEvidence,
) -> None:
    """Fail closed unless per-path decisions match each report truth."""
    for decision, evidence in (
        (snapshot.fts5_trigram, fts5_evidence),
        (snapshot.gram_fallback, fallback_evidence),
    ):
        if decision.available != _report_path_truth(evidence):
            raise BenchmarkGateDError(
                "GATE_D.PUBLICATION_DECISION_MISMATCH"
            )


_PreparedPublicationT = TypeVar("_PreparedPublicationT")


_GATE_D_PUBLICATION_BINDINGS_MINT = object()


_GateDPublicationBindings = tuple[
    object,
    type[BenchmarkGateDError],
    Callable[
        [
            RetrievalCapabilitySnapshot,
            RetrievalBenchmarkEvidence,
            RetrievalBenchmarkEvidence,
        ],
        None,
    ],
    Callable[
        ...,
        tuple[RetrievalBenchmarkEvidence, RetrievalBenchmarkEvidence],
    ],
    Callable[..., str],
    type[RetrievalCapabilityManifest],
    str,
    type[RetrievalCapabilityPublicationResult],
    type[RetrievalCapabilityPublisher],
    Callable[[object], datetime],
    type[_GateDRunReceipt],
    type[BenchmarkGateDRunResult],
    Callable[[object, str], str],
    object,
    Callable[..., Any],
]


def _publication_bindings_from_current_globals() -> _GateDPublicationBindings:
    """Mint one private binding set for the public compatibility facade."""

    return (
        _GATE_D_PUBLICATION_BINDINGS_MINT,
        BenchmarkGateDError,
        _verify_path_decisions_match_reports,
        retrieval_benchmark_evidence_pair,
        benchmark_implementation_fingerprint,
        RetrievalCapabilityManifest,
        CANDIDATE_PROOF_QUERY_VERSION,
        RetrievalCapabilityPublicationResult,
        RetrievalCapabilityPublisher,
        _require_utc_datetime,
        _GateDRunReceipt,
        BenchmarkGateDRunResult,
        _validate_evidence_utc_string,
        _RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR,
        _validated_refresh_retrieval_capability,
    )


_GATE_D_PUBLICATION_BINDINGS = _publication_bindings_from_current_globals()


def _publish_retrieval_capability_gate_d_prepared(
    base_manifest: RetrievalCapabilityManifest,
    run_result: BenchmarkGateDRunResult,
    publisher: RetrievalCapabilityPublisher,
    *,
    generated_at_utc: str,
    valid_until_utc: str,
    evaluated_at_utc: datetime,
    prepare_publication: Callable[
        [RetrievalCapabilityPublicationResult],
        _PreparedPublicationT,
    ],
    _publication_bindings: _GateDPublicationBindings,
) -> _PreparedPublicationT:
    """Compose and publish one Gate D manifest through the exact publisher.

    Preserves every envelope and Gate C fact of the base manifest
    byte-for-byte/value-for-value and replaces only the two fuzzy benchmark
    fields with the pair derived from the durable runner readback at the
    explicit validity
    window.  Performs exactly one Core-owned validated publisher transition
    at the explicit evaluated instant: per-path decisions are checked against
    report truth before the candidate can become query-visible.  A failed
    report must stay closed and a passed report must be open.  The owner never
    constructs a publisher/evaluator and never grants availability itself.
    """

    if (
        type(_publication_bindings) is not tuple
        or len(_publication_bindings) != 15
        or _publication_bindings[0]
        is not _GATE_D_PUBLICATION_BINDINGS_MINT
    ):
        raise BenchmarkGateDError("GATE_D.IMPLEMENTATION_INVALID")
    (
        _bindings_mint,
        benchmark_error_type,
        decision_verifier,
        evidence_pair,
        implementation_fingerprint,
        manifest_type,
        proof_query_version,
        publication_result_type,
        publisher_type,
        require_utc_datetime,
        run_receipt_type,
        run_result_type,
        validate_utc_string,
        snapshot_descriptor,
        validated_transition_raw,
    ) = _publication_bindings
    validated_transition = validated_transition_raw

    try:
        if type(base_manifest) is not manifest_type:
            raise benchmark_error_type("GATE_D.MANIFEST_INVALID")
        if type(run_result) is not run_result_type:
            raise benchmark_error_type("GATE_D.RUN_RESULT_INVALID")
        receipt = run_result._receipt
        if type(receipt) is not run_receipt_type or (
            receipt.bundle is not run_result.bundle
            or receipt.bundle_digest != run_result.bundle_digest
            or receipt.artifact_size != run_result.artifact_size
            or receipt.artifact_digest != run_result.artifact_digest
            or receipt.test_mode is not run_result.test_mode
        ):
            raise benchmark_error_type("GATE_D.RUN_RESULT_INVALID")
        if run_result.test_mode:
            raise benchmark_error_type("GATE_D.TEST_EVIDENCE_FORBIDDEN")
        bundle = run_result.bundle
        try:
            current_fingerprint = implementation_fingerprint()
        except (TypeError, ValueError) as error:
            raise benchmark_error_type(
                "GATE_D.IMPLEMENTATION_INVALID"
            ) from error
        if (
            bundle.proof_query_version != proof_query_version
            or bundle.implementation_fingerprint != current_fingerprint
        ):
            raise benchmark_error_type("GATE_D.IMPLEMENTATION_CHANGED")
        if type(publisher) is not publisher_type:
            raise benchmark_error_type("GATE_D.PUBLISHER_INVALID")
        expected_current = publisher.snapshot()
        generated = validate_utc_string(
            generated_at_utc,
            "generated_at_utc",
        )
        valid_until = validate_utc_string(
            valid_until_utc,
            "valid_until_utc",
        )
        if not generated < valid_until:
            raise benchmark_error_type("GATE_D.INSTANTS_INVALID")
        evaluated = require_utc_datetime(evaluated_at_utc)
        fts5_evidence, fallback_evidence = evidence_pair(
            bundle,
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
        )
        new_manifest = manifest_type(
            evidence_schema_version=base_manifest.evidence_schema_version,
            retrieval_artifact_digest=base_manifest.retrieval_artifact_digest,
            retrieval_build_digest=base_manifest.retrieval_build_digest,
            semantics_version=base_manifest.semantics_version,
            fixture_digest=base_manifest.fixture_digest,
            evaluator_digest=base_manifest.evaluator_digest,
            generated_at_utc=base_manifest.generated_at_utc,
            valid_until_utc=base_manifest.valid_until_utc,
            context_cohorts=base_manifest.context_cohorts,
            fuzzy_core_cohorts=base_manifest.fuzzy_core_cohorts,
            fts5_trigram_benchmark=fts5_evidence,
            gram_fallback_benchmark=fallback_evidence,
        )
        try:
            terminal_fingerprint = implementation_fingerprint()
        except (TypeError, ValueError) as error:
            raise benchmark_error_type(
                "GATE_D.IMPLEMENTATION_INVALID"
            ) from error
        if terminal_fingerprint != current_fingerprint:
            raise benchmark_error_type("GATE_D.IMPLEMENTATION_CHANGED")

        def validate_candidate(
            candidate: RetrievalCapabilitySnapshot,
        ) -> RetrievalCapabilityPublicationResult:
            if (
                candidate.context != expected_current.context
                or candidate.fuzzy_core != expected_current.fuzzy_core
            ):
                raise benchmark_error_type(
                    "GATE_D.PUBLICATION_DECISION_MISMATCH"
                )
            decision_verifier(
                candidate,
                fts5_evidence,
                fallback_evidence,
            )
            return publication_result_type(
                manifest=new_manifest,
                snapshot=candidate,
            )

        def prepare_candidate(
            candidate: RetrievalCapabilitySnapshot,
        ) -> _PreparedPublicationT:
            core_result = validate_candidate(candidate)
            return prepare_publication(core_result)

        publication_result = validated_transition(
            publisher,
            new_manifest,
            evaluated_at_utc=evaluated,
            expected_current=expected_current,
            validator=prepare_candidate,
            _snapshot_descriptor=snapshot_descriptor,
        )
    except benchmark_error_type:
        raise
    except (TypeError, ValueError) as error:
        raise benchmark_error_type("GATE_D.PUBLICATION_FAILED") from error
    return publication_result


def publish_retrieval_capability_gate_d(
    base_manifest: RetrievalCapabilityManifest,
    run_result: BenchmarkGateDRunResult,
    publisher: RetrievalCapabilityPublisher,
    *,
    generated_at_utc: str,
    valid_until_utc: str,
    evaluated_at_utc: datetime,
) -> RetrievalCapabilityPublicationResult:
    """Publish one immutable Core result through the prepared private seam."""

    return _publish_retrieval_capability_gate_d_prepared(
        base_manifest,
        run_result,
        publisher,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
        evaluated_at_utc=evaluated_at_utc,
        prepare_publication=lambda result: result,
        _publication_bindings=_publication_bindings_from_current_globals(),
    )


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
    "BENCHMARK_IMPLEMENTATION_FINGERPRINT_VERSION",
    "BENCHMARK_IMPLEMENTATION_SOURCE_PATHS",
    "BenchmarkGateDError",
    "BenchmarkGateDRunResult",
    "BenchmarkEvidenceBundle",
    "BenchmarkPathBundle",
    "BenchmarkProcessFacts",
    "BenchmarkQueryFacts",
    "RetrievalCapabilityPublicationResult",
    "benchmark_evidence_bundle_digest",
    "benchmark_evidence_bundle_from_json",
    "benchmark_evidence_bundle_from_payload",
    "benchmark_evidence_bundle_to_json",
    "benchmark_evidence_bundle_to_payload",
    "benchmark_implementation_fingerprint",
    "combine_benchmark_evidence",
    "publish_retrieval_capability_gate_d",
    "retrieval_benchmark_evidence_by_path",
    "retrieval_benchmark_evidence_pair",
    "run_benchmark_gate_d",
]
