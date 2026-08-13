"""Full-scan oracle and candidate-recall hard-gate evidence owner (Task 8.4).

Ownership
---------
This module owns benchmark-v1 candidate-recall evidence for the two immutable
index paths (FTS5_TRIGRAM and GRAM_FALLBACK): a brute-force full scan of every
fixed oracle query against every fixed oracle record with the production
``SimilarityScorerV1``, and the real CandidateRetriever/SQLite candidate path
executed separately per path.  It is an offline Task 8.4 evidence owner only:
no production runtime module imports it, and it never constructs the
Task 8.5 report types or publishes any capability
(Task 8.5 owns reports and Gate D).  Corpus, latency, process/RSS and gate
owners stay separate.

Task 8.4 obligations
--------------------
For each of the 200 fixed oracle queries two distinct identity sets are
derived in the original ``BenchmarkRecord.record_id`` namespace:

(a) above-threshold set: every record with ``final_similarity >= 0.60``
    (production retention rule ``final_similarity < minimum_similarity``
    drops);
(b) true full-scan top-10, deterministically ordered by the production fuzzy
    ordering (final similarity descending, record_id descending), taken over
    ALL records without any threshold filter.

They are distinct obligations: the top-10 is never replaced by a
threshold-filtered top-10 and vice versa, and miss queries still carry both
obligations even when all scores are below threshold.

Candidate path
--------------
The actual ``CandidateRetriever``/SQLite candidate path is executed on the
fixed 5000-record subset for FTS5_TRIGRAM and for forced GRAM_FALLBACK
(following the bounded child-local/runtime patch pattern proven by
``tm_benchmark_process.py``: ``patch("tm_sqlite_store._probe_fts5",
return_value=False)`` scoped to the whole build+query run, never touching
global/runtime files).  SQLite assigns dense physical record ids that differ
distinct from the dense physical ids assigned by SQLite: an explicit one-to-one
physical-id<->original-record-id mapping is created, closed, and validated
(duplicates/missing/extras/reordering ambiguity all fail closed), and every
candidate id is translated back to original identity before comparison.
Candidate input uses production fold-v1 and candidate-budget-v1 with
``result_limit=10`` (the contract ``top_k``); the final scorer is never called
in candidate retrieval.  The evidence records the actual per-query index kind,
budget, truncation and availability facts; FTS absence or any per-query index
kind that does not match the requested path is an explicit unavailable/fail
outcome, never silently relabeled.

Evidence contract
-----------------
``OracleRecallEvidence`` is frozen and self-validating: it privately snapshots
the complete ``BenchmarkContract``, binds contract/oracle-subset/scorer/path/
environment digests, carries one strict row per query (query id, category,
reference original id, candidate original identities, above-threshold
original identities, ordered true top-10 original identities, missing sets,
and count/index/budget/truncation facts), and derives ``evidence_digest`` at
construction (``init=False``).  A caller-supplied ``evidence_digest`` is never
trusted, and a boolean ``recall_passed`` cannot self-authorize: it is always
recomputed from the missing sets and per-query availability/index facts and
must match.  Evidence is strict-JSON round-trippable with duplicate-key,
non-finite, unknown, missing and wrong-type rejection; standalone
reconstructed evidence rejects any forged binding.  Only exact 100% for BOTH
obligations over all queries, with every query available on the requested
path, can mark raw recall evidence ``recall_passed``.  Honest hard-gate
failure is acceptable; thresholds, budgets, cohorts and query sets are never
relaxed or curated.  Evidence and diagnostics never contain source, target or
query text; no corpus body and no temporary database are persisted in the
worktree (dedicated disposable run roots only, caller-owned cleanup).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import unicodedata
from typing import Protocol
from unittest.mock import patch

from text_matcher import fold_text_v1
from tm_benchmark import (
    BenchmarkQuery,
    BenchmarkRecord,
    TM_BENCHMARK_SCORER_CONFIG_VERSION,
    _query_payload,
    _record_payload,
    benchmark_digest,
    iter_oracle_queries,
    iter_oracle_subset_records,
    load_benchmark_contract,
    recompute_benchmark_inputs,
)
from tm_candidate_index import CandidateRetriever
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CanonicalResourceIdentity,
    SCORER_VERSION_V1,
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    candidate_budget_v1,
    contract_from_json,
    contract_to_json,
)
from tm_migration import TMMigrationService
from tm_similarity import SimilarityScorerV1
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    SQLiteTMStore,
    _probe_fts5,
)
from tm_stage_sealer import StageSealer

ORACLE_EVIDENCE_SCHEMA_VERSION = "tm-benchmark-oracle-evidence-v1"
ORACLE_EVIDENCE_DIGEST_VERSION = "tm-benchmark-oracle-digest-v1"
ORACLE_DEFAULT_RESOURCE_ID = "tm.benchmark"
ORACLE_DEFAULT_CANONICAL_STORE_ID = "store.benchmark"
ORACLE_FIXTURE_NAME = "oracle.fixture.jsonl"

_ORACLE_CATEGORIES = ("exact", "near-edit", "miss")
_INDEX_KINDS = ("FTS5_TRIGRAM", "GRAM_FALLBACK")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NATIVE_PATH_TYPE = type(Path("."))
_MAX_TEST_RECORD_COUNT = 1_000
_MAX_TEST_QUERY_COUNT = 200


class OraclePathUnavailableError(RuntimeError):
    """Explicit unavailable/fail outcome for a requested execution path."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("unavailable code must be a non-empty string")
        super().__init__(code)
        self.code = code


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


def _require_builtin_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in bool")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


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
    payload: object,
    expected_keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        raise TypeError(f"{label} must be a built-in dict")
    assert isinstance(payload, dict)
    keys = set(payload)
    if keys != expected_keys:
        missing = sorted(expected_keys - keys)
        unknown = sorted(keys - expected_keys)
        raise ValueError(
            f"{label} has missing fields {missing!r} and unknown fields "
            f"{unknown!r}"
        )
    return dict(payload)


def _as_bool(value: object, field_name: str) -> bool:
    return _require_builtin_bool(value, field_name)


def _as_int(value: object, field_name: str, *, minimum: int) -> int:
    return _require_builtin_int(value, field_name, minimum=minimum)


def _as_str(value: object, field_name: str) -> str:
    return _require_identity(value, field_name)


def _as_digest(value: object, field_name: str) -> str:
    return _require_digest(value, field_name)


def _as_optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_builtin_int(value, field_name, minimum=1)


def _as_category(value: object, field_name: str) -> str:
    category = _require_identity(value, field_name)
    if category not in _ORACLE_CATEGORIES:
        raise ValueError(f"{field_name} is an unknown oracle category")
    return category


def _as_index_kind(value: object, field_name: str) -> str:
    index_kind = _require_identity(value, field_name)
    if index_kind not in _INDEX_KINDS:
        raise ValueError(f"{field_name} is an unknown index kind")
    return index_kind


def _as_optional_code(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_identity(value, field_name)


def _id_tuple(value: object, field_name: str) -> tuple[int, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a built-in tuple")
    copied: list[int] = []
    for item in value:
        if type(item) is not int:
            raise TypeError(f"{field_name} must contain built-in ints")
        if item < 1:
            raise ValueError(f"{field_name} must contain positive ints")
        copied.append(item)
    if len(set(copied)) != len(copied):
        raise ValueError(f"{field_name} must contain unique ids")
    return tuple(copied)


def _id_list(value: object, field_name: str) -> tuple[int, ...]:
    if type(value) is not list:
        raise TypeError(f"{field_name} must be a JSON list")
    return _id_tuple(tuple(value), field_name)


def _ram_mib() -> str:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    if pages is None or page_size is None:
        return "unknown"
    return str((pages * page_size) // (1024 * 1024))


def collect_oracle_environment(
    *,
    fts5_enabled: bool,
) -> tuple[tuple[str, str], ...]:
    """Collect stable local-runtime facts accepted by the environment digest.

    Only inspects the local runtime; no network, no telemetry.  The returned
    tuple is sorted by key and accepted by ``benchmark_environment_digest``.
    """

    _require_builtin_bool(fts5_enabled, "fts5 enabled")
    environment = {
        "cpu": platform.processor() or platform.machine() or "unknown",
        "fts5_enabled": "true" if fts5_enabled else "false",
        "os": platform.system() or "unknown",
        "python_version": platform.python_version(),
        "ram_mib": _ram_mib(),
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_version": unicodedata.unidata_version,
    }
    return tuple(sorted(environment.items()))


def validate_oracle_environment(
    environment: tuple[tuple[str, str], ...],
    requested_path: BenchmarkExecutionPath,
) -> None:
    """Fail closed unless the environment matches the requested path."""

    if type(environment) is not tuple:
        raise TypeError("environment must be a built-in tuple")
    benchmark_environment_digest(environment)
    if type(requested_path) is not BenchmarkExecutionPath:
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


def expected_store_index_kind(
    execution_path: BenchmarkExecutionPath,
) -> str:
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        return "FTS5_TRIGRAM"
    if execution_path is BenchmarkExecutionPath.GRAM_FALLBACK:
        return "GRAM_FALLBACK"
    raise ValueError("execution path is unsupported")


def recompute_scorer_config_digest(contract: BenchmarkContract) -> str:
    """Recompute the scorer-config digest and require contract equality."""

    if type(contract) is not BenchmarkContract:
        raise TypeError("contract must be BenchmarkContract")
    digest = benchmark_digest(
        contract.corpus_generator_version,
        "scorer-config",
        [
            {
                "scorer_config_version": TM_BENCHMARK_SCORER_CONFIG_VERSION,
                "scorer_version": SCORER_VERSION_V1,
                "minimum_similarity": contract.minimum_similarity,
                "top_k": contract.top_k,
                "candidate_budget_version": contract.candidate_budget_version,
            }
        ],
    )
    if digest != contract.scorer_config_digest:
        raise ValueError("scorer config digest must match evidence contract")
    return digest


def _oracle_subset_digest(
    generator_version: str,
    oracle_queries: Iterable[BenchmarkQuery],
    oracle_records: Iterable[BenchmarkRecord],
) -> str:
    items: list[dict[str, object]] = [
        {"section": "record", **_record_payload(record)}
        for record in oracle_records
    ]
    items.extend(
        {"section": "query", **_query_payload(query)}
        for query in oracle_queries
    )
    return benchmark_digest(generator_version, "oracle-subset", items)


def _fixture_row(record: BenchmarkRecord) -> dict[str, object]:
    """One migration JSONL row for an oracle record (legacy default provenance)."""

    if type(record) is not BenchmarkRecord:
        raise TypeError("fixture rows require BenchmarkRecord")
    row: dict[str, object] = {
        "source": record.source_raw,
        "target": record.target_raw,
    }
    if record.speaker_raw is not None:
        row["speaker"] = record.speaker_raw
    if record.context_prev_raw is not None:
        row["context_prev"] = record.context_prev_raw
    if record.context_next_raw is not None:
        row["context_next"] = record.context_next_raw
    if record.file_source is not None:
        row["file_source"] = record.file_source
    return row


def _generate_fixture(
    fixture_path: Path,
    records: Iterable[BenchmarkRecord],
) -> tuple[str, int]:
    """Write one immutable JSONL fixture and return (sha256, record count)."""

    if type(fixture_path) is not _NATIVE_PATH_TYPE:
        raise TypeError("fixture path must be a native pathlib.Path")
    hasher = hashlib.sha256()
    count = 0
    try:
        with fixture_path.open("xb") as stream:
            for record in records:
                line = (_canonical_json(_fixture_row(record)) + "\n").encode(
                    "utf-8"
                )
                stream.write(line)
                hasher.update(line)
                count += 1
    except FileExistsError as error:
        raise ValueError(
            "run root is not closed: fixture already exists"
        ) from error
    if count < 1:
        raise ValueError("fixture generation produced no records")
    return hasher.hexdigest(), count


def _validate_records(records: Iterable[BenchmarkRecord]) -> tuple[BenchmarkRecord, ...]:
    if type(records) is not tuple:
        records = tuple(records)
    if not records:
        raise ValueError("oracle records must not be empty")
    previous_id = 0
    for record in records:
        if type(record) is not BenchmarkRecord:
            raise TypeError("oracle records must contain BenchmarkRecord")
        if record.record_id <= previous_id:
            raise ValueError(
                "oracle record ids must be unique and strictly ascending"
            )
        previous_id = record.record_id
    return records


def _validate_queries(
    queries: Iterable[BenchmarkQuery],
) -> tuple[BenchmarkQuery, ...]:
    if type(queries) is not tuple:
        queries = tuple(queries)
    if not queries:
        raise ValueError("oracle queries must not be empty")
    expected_id = 1
    for query in queries:
        if type(query) is not BenchmarkQuery:
            raise TypeError("oracle queries must contain BenchmarkQuery")
        if query.query_id != expected_id:
            raise ValueError(
                "oracle query ids must be exactly 1..N with no gaps"
            )
        expected_id += 1
        if query.category not in _ORACLE_CATEGORIES:
            raise ValueError("oracle query category is invalid")
        if (
            query.category == "miss"
            and query.reference_record_id is not None
        ):
            raise ValueError("miss queries must not carry a reference id")
        if (
            query.category in ("exact", "near-edit")
            and query.reference_record_id is None
        ):
            raise ValueError(
                "exact and near-edit queries require a reference id"
            )
    return queries


@dataclass(frozen=True)
class FullScanQueryOracle:
    """One query's two distinct full-scan obligations in original identity."""

    query_id: int
    category: str
    reference_record_id: int | None
    above_threshold_ids: tuple[int, ...]
    top10_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        query_id = _require_builtin_int(self.query_id, "query id", minimum=1)
        object.__setattr__(self, "query_id", query_id)
        object.__setattr__(
            self,
            "category",
            _as_category(self.category, "query category"),
        )
        reference_record_id = _as_optional_int(
            self.reference_record_id,
            "reference record id",
        )
        object.__setattr__(
            self,
            "reference_record_id",
            reference_record_id,
        )
        if (
            self.category == "miss"
            and self.reference_record_id is not None
        ):
            raise ValueError("miss queries must not carry a reference id")
        if (
            self.category in ("exact", "near-edit")
            and self.reference_record_id is None
        ):
            raise ValueError(
                "exact and near-edit queries require a reference id"
            )
        above = _id_tuple(
            self.above_threshold_ids,
            "above-threshold ids",
        )
        if above != tuple(sorted(above)):
            raise ValueError(
                "above-threshold ids must be unique and ascending"
            )
        object.__setattr__(self, "above_threshold_ids", above)
        top10 = _id_tuple(self.top10_ids, "top-10 ids")
        object.__setattr__(self, "top10_ids", top10)


def compute_full_scan_oracle(
    *,
    contract: BenchmarkContract,
    records: Iterable[BenchmarkRecord],
    queries: Iterable[BenchmarkQuery],
    scorer: SimilarityScorerV1 | None = None,
    minimum_similarity: float | None = None,
    top_k: int | None = None,
) -> tuple[FullScanQueryOracle, ...]:
    """Brute-force every query against every record with the frozen scorer.

    Derives the two distinct obligations in the original record-id namespace:
    (a) every record with ``final_similarity >= minimum_similarity`` (ascending
    record id), and (b) the true full-scan top-``top_k`` ordered by the
    production fuzzy ordering (final similarity descending, record_id
    descending) over ALL records with no threshold filter.  Defaults bind the
    contract's ``minimum_similarity`` and ``top_k``; explicit values are
    rejected unless they equal the contract values.
    """

    if type(contract) is not BenchmarkContract:
        raise TypeError("contract must be BenchmarkContract")
    if scorer is None:
        scorer = SimilarityScorerV1()
    if type(scorer) is not SimilarityScorerV1:
        raise TypeError("scorer must be SimilarityScorerV1")
    if minimum_similarity is None:
        minimum_similarity = contract.minimum_similarity
    if top_k is None:
        top_k = contract.top_k
    if type(minimum_similarity) is not float or type(top_k) is not int:
        raise TypeError("threshold and top_k must use built-in scalar types")
    if minimum_similarity != contract.minimum_similarity:
        raise ValueError("minimum similarity must equal contract value")
    if top_k != contract.top_k:
        raise ValueError("top_k must equal contract value")

    records_tuple = _validate_records(records)
    queries_tuple = _validate_queries(queries)
    if len(records_tuple) < top_k:
        raise ValueError("oracle records must be at least top_k")
    record_ids = tuple(record.record_id for record in records_tuple)
    record_id_set = set(record_ids)

    rows: list[FullScanQueryOracle] = []
    for query in queries_tuple:
        if query.reference_record_id is not None:
            if query.reference_record_id not in record_id_set:
                raise ValueError(
                    "query reference id must belong to the oracle subset"
                )
        scored: list[tuple[int, float]] = []
        for record in records_tuple:
            evidence = scorer.score(query.query_raw, record.source_raw)
            final_similarity = evidence.final_similarity
            if type(final_similarity) is not float or not math.isfinite(
                final_similarity
            ):
                raise RuntimeError("scorer returned an invalid similarity")
            scored.append((record.record_id, final_similarity))
        above_threshold = tuple(
            record_id
            for record_id, final_similarity in scored
            if final_similarity >= minimum_similarity
        )
        ranked = sorted(
            scored,
            key=lambda item: (-item[1], -item[0]),
        )
        top10_ids = tuple(item[0] for item in ranked[:top_k])
        rows.append(
            FullScanQueryOracle(
                query_id=query.query_id,
                category=query.category,
                reference_record_id=query.reference_record_id,
                above_threshold_ids=above_threshold,
                top10_ids=top10_ids,
            )
        )
    return tuple(rows)


class _PhysicalMappingRecord(Protocol):
    """Minimal record shape the physical-mapping validation reads back."""

    @property
    def record_id(self) -> int: ...

    @property
    def origin_ordinal(self) -> int: ...

    @property
    def legacy_line_no(self) -> int | None: ...

    @property
    def source_raw(self) -> str: ...

    @property
    def target_raw(self) -> str: ...

    @property
    def speaker_raw(self) -> str | None: ...

    @property
    def context_prev_raw(self) -> str | None: ...

    @property
    def context_next_raw(self) -> str | None: ...

    @property
    def file_source(self) -> str | None: ...


class _PhysicalMappingStore(Protocol):
    """Duck-typed store seam: closed dense physical-id read-back only."""

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[_PhysicalMappingRecord, ...]: ...


def _validate_physical_mapping(
    store: _PhysicalMappingStore,
    records: tuple[BenchmarkRecord, ...],
) -> dict[int, int]:
    """Create, close, and validate the physical<->original id mapping.

    The fixture is written in ascending original record-id order, so physical
    ids are dense and positional; every one of the checks below must hold or
    the mapping fails closed (duplicates, missing, extras, reordering
    ambiguity, ordinal or body drift are all hard failures).
    """

    expected_count = len(records)
    expected_physical = tuple(range(1, expected_count + 1))
    read_back = store.records_by_id(expected_physical)
    if len(read_back) != expected_count:
        raise ValueError("physical record read-back count mismatch")
    actual_ids = tuple(record.record_id for record in read_back)
    if actual_ids != expected_physical:
        raise ValueError(
            "physical record ids are not the closed dense sequence"
        )
    for physical_id, record, expected in zip(
        expected_physical,
        read_back,
        records,
        strict=True,
    ):
        if record.origin_ordinal != physical_id - 1:
            raise ValueError("physical origin ordinal drift")
        if record.legacy_line_no != physical_id:
            raise ValueError("physical legacy line number drift")
        body = (
            record.source_raw,
            record.target_raw,
            record.speaker_raw,
            record.context_prev_raw,
            record.context_next_raw,
            record.file_source,
        )
        expected_body = (
            expected.source_raw,
            expected.target_raw,
            expected.speaker_raw,
            expected.context_prev_raw,
            expected.context_next_raw,
            expected.file_source,
        )
        if body != expected_body:
            raise ValueError("physical record body drift")
    mapping = {
        physical_id: records[physical_id - 1].record_id
        for physical_id in expected_physical
    }
    reverse = {original_id: physical_id for physical_id, original_id in mapping.items()}
    if len(mapping) != expected_count or len(reverse) != expected_count:
        raise ValueError("physical mapping is not one-to-one")
    return mapping


def _build_oracle_store(
    *,
    fixture_path: Path,
    resource_id: str,
    canonical_store_id: str,
) -> tuple[SQLiteTMStore, str]:
    """Build, seal, activate, publish and rehydrate one oracle store."""

    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        fixture_path.resolve(),
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
    )
    build = service.build_mutable_stage(fixture_path)
    stage = build.mutable_stage
    if stage is None:
        raise RuntimeError("oracle stage build produced no mutable stage")
    if build.reused_completed_revision is not None:
        raise RuntimeError("oracle stage unexpectedly reused a revision")
    if build.preflight.invalid_count != 0:
        raise RuntimeError("oracle stage preflight reported invalid rows")
    if build.preflight.valid_count < 1:
        raise RuntimeError("oracle stage preflight reported no valid rows")
    sealed = StageSealer(
        registry=coordinator.sealed_registry,
        canonical_store_id=canonical_store_id,
    ).seal(stage, expected_prior_generation=None)
    if not sealed.evidence.integrity_ok:
        raise RuntimeError("oracle stage seal integrity failed")
    prepared = coordinator.activate(sealed)
    journal = coordinator.publish_prepared_activation(prepared)
    coordinator.publish_activation(prepared, journal)
    fresh = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    report = fresh.rehydrate_runtime_authority()
    if report is None:
        raise RuntimeError("oracle store rehydration failed")
    store = SQLiteTMStore.from_coordinator(fresh)
    return store, identity.resource_id


def _run_candidate_path(
    *,
    contract: BenchmarkContract,
    execution_path: BenchmarkExecutionPath,
    records: tuple[BenchmarkRecord, ...],
    queries: tuple[BenchmarkQuery, ...],
    run_root: Path,
    resource_id: str,
    canonical_store_id: str,
) -> tuple[
    str,
    str,
    tuple[
        tuple[int, tuple[int, ...], str, bool, str | None, bool],
        ...,
    ],
]:
    """Execute the real CandidateRetriever/SQLite path for one requested path."""

    if type(run_root) is not _NATIVE_PATH_TYPE:
        raise TypeError("run root must be a native pathlib.Path")
    if not run_root.is_dir():
        raise ValueError("run root must be an existing directory")
    try:
        entries = list(run_root.iterdir())
    except OSError as error:
        raise ValueError("run root cannot be inspected") from error
    if entries:
        raise ValueError("run root must be closed before fixture creation")
    fixture_path = run_root / ORACLE_FIXTURE_NAME
    fixture_digest, fixture_count = _generate_fixture(fixture_path, records)
    if fixture_count != len(records):
        raise ValueError("fixture record count mismatch")

    expected_index_kind = expected_store_index_kind(execution_path)
    force_fallback = execution_path is BenchmarkExecutionPath.GRAM_FALLBACK
    if not force_fallback and not _probe_fts5():
        raise OraclePathUnavailableError("ORACLE.FTS5_UNAVAILABLE")

    store: SQLiteTMStore
    health: object
    with ExitStack() as stack:
        if force_fallback:
            stack.enter_context(
                patch("tm_sqlite_store._probe_fts5", return_value=False)
            )
        store, actual_resource_id = _build_oracle_store(
            fixture_path=fixture_path,
            resource_id=resource_id,
            canonical_store_id=canonical_store_id,
        )
        if actual_resource_id != resource_id:
            raise ValueError("oracle store resource id drift")
        health = store.health()
        if not health.healthy:
            raise RuntimeError("oracle store health failed")
        if health.index_kind != expected_index_kind:
            raise RuntimeError(
                "oracle store index kind does not match the requested path"
            )
        if health.record_count != len(records):
            raise RuntimeError("oracle store record count mismatch")
        mapping = _validate_physical_mapping(store, records)
        retriever = CandidateRetriever()
        rows: list[
            tuple[int, tuple[int, ...], str, bool, str | None, bool]
        ] = []
        for query in queries:
            folded_query = fold_text_v1(query.query_raw).folded_text
            if type(folded_query) is not str:
                raise TypeError("folded query must be a built-in string")
            report = retriever.candidates(
                resource_id,
                store,
                folded_query,
                result_limit=contract.top_k,
            )
            metadata = report.metadata
            if metadata.result_limit != contract.top_k:
                raise RuntimeError("candidate result limit drift")
            if metadata.candidate_budget_version != CANDIDATE_BUDGET_VERSION:
                raise RuntimeError("candidate budget version drift")
            candidate_original_ids: list[int] = []
            if metadata.fuzzy_available:
                if metadata.fuzzy_unavailable_code is not None:
                    raise RuntimeError("candidate availability facts conflict")
                for candidate in report.candidates:
                    physical_id = candidate.record_id
                    original_id = mapping.get(physical_id)
                    if original_id is None:
                        raise RuntimeError(
                            "candidate physical id is not in the closed mapping"
                        )
                    candidate_original_ids.append(original_id)
                rows.append(
                    (
                        query.query_id,
                        tuple(candidate_original_ids),
                        metadata.index_kind,
                        True,
                        None,
                        metadata.truncated,
                    )
                )
            else:
                if metadata.fuzzy_unavailable_code is None:
                    raise RuntimeError("candidate unavailable code is missing")
                if report.candidates:
                    raise RuntimeError(
                        "unavailable candidate path must not return candidates"
                    )
                rows.append(
                    (
                        query.query_id,
                        (),
                        metadata.index_kind,
                        False,
                        metadata.fuzzy_unavailable_code,
                        metadata.truncated,
                    )
                )
    if len(rows) != len(queries):
        raise RuntimeError("candidate path row count mismatch")
    return fixture_digest, health.index_kind, tuple(rows)


@dataclass(frozen=True)
class OracleQueryRow:
    """One query's frozen recall facts (original identity namespace)."""

    query_id: int
    category: str
    reference_record_id: int | None
    candidate_ids: tuple[int, ...]
    above_threshold_ids: tuple[int, ...]
    top10_ids: tuple[int, ...]
    missing_above_threshold_ids: tuple[int, ...]
    missing_top10_ids: tuple[int, ...]
    candidate_count: int
    above_count: int
    top10_count: int
    actual_index_kind: str
    candidate_available: bool
    unavailable_code: str | None
    truncated: bool

    def __post_init__(self) -> None:
        query_id = _require_builtin_int(self.query_id, "query id", minimum=1)
        object.__setattr__(self, "query_id", query_id)
        object.__setattr__(
            self,
            "category",
            _as_category(self.category, "query category"),
        )
        reference_record_id = _as_optional_int(
            self.reference_record_id,
            "reference record id",
        )
        object.__setattr__(
            self,
            "reference_record_id",
            reference_record_id,
        )
        if self.category == "miss" and self.reference_record_id is not None:
            raise ValueError("miss queries must not carry a reference id")
        if (
            self.category in ("exact", "near-edit")
            and self.reference_record_id is None
        ):
            raise ValueError(
                "exact and near-edit queries require a reference id"
            )
        candidate_ids = _id_tuple(self.candidate_ids, "candidate ids")
        above = _id_tuple(self.above_threshold_ids, "above-threshold ids")
        if above != tuple(sorted(above)):
            raise ValueError("above-threshold ids must be ascending")
        top10 = _id_tuple(self.top10_ids, "top-10 ids")
        missing_above = _id_tuple(
            self.missing_above_threshold_ids,
            "missing above-threshold ids",
        )
        missing_top10 = _id_tuple(
            self.missing_top10_ids,
            "missing top-10 ids",
        )
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "above_threshold_ids", above)
        object.__setattr__(self, "top10_ids", top10)
        object.__setattr__(
            self,
            "missing_above_threshold_ids",
            missing_above,
        )
        object.__setattr__(self, "missing_top10_ids", missing_top10)
        object.__setattr__(
            self,
            "candidate_count",
            _require_builtin_int(
                self.candidate_count,
                "candidate count",
                minimum=0,
            ),
        )
        object.__setattr__(
            self,
            "above_count",
            _require_builtin_int(self.above_count, "above count", minimum=0),
        )
        object.__setattr__(
            self,
            "top10_count",
            _require_builtin_int(self.top10_count, "top-10 count", minimum=0),
        )
        object.__setattr__(
            self,
            "actual_index_kind",
            _as_index_kind(self.actual_index_kind, "actual index kind"),
        )
        candidate_available = _require_builtin_bool(
            self.candidate_available,
            "candidate available",
        )
        object.__setattr__(self, "candidate_available", candidate_available)
        unavailable_code = _as_optional_code(
            self.unavailable_code,
            "unavailable code",
        )
        object.__setattr__(self, "unavailable_code", unavailable_code)
        object.__setattr__(
            self,
            "truncated",
            _require_builtin_bool(self.truncated, "truncated"),
        )
        if len(candidate_ids) != self.candidate_count:
            raise ValueError("candidate count must match candidate ids")
        if len(above) != self.above_count:
            raise ValueError("above count must match above-threshold ids")
        if len(top10) != self.top10_count:
            raise ValueError("top-10 count must match top-10 ids")
        if self.top10_count < 1:
            raise ValueError("top-10 obligation must never be empty")
        candidate_set = set(candidate_ids)
        if candidate_available:
            if unavailable_code is not None:
                raise ValueError(
                    "available candidates cannot carry an unavailable code"
                )
        else:
            if unavailable_code is None:
                raise ValueError(
                    "unavailable candidates require an unavailable code"
                )
            if candidate_ids:
                raise ValueError(
                    "unavailable candidates must have no candidate ids"
                )
        recomputed_missing_above = tuple(
            record_id
            for record_id in above
            if record_id not in candidate_set
        )
        recomputed_missing_top10 = tuple(
            record_id
            for record_id in top10
            if record_id not in candidate_set
        )
        if recomputed_missing_above != missing_above:
            raise ValueError(
                "missing above-threshold ids are inconsistent"
            )
        if recomputed_missing_top10 != missing_top10:
            raise ValueError("missing top-10 ids are inconsistent")


def _row_payload(row: OracleQueryRow) -> dict[str, object]:
    return {
        "query_id": row.query_id,
        "category": row.category,
        "reference_record_id": row.reference_record_id,
        "candidate_ids": list(row.candidate_ids),
        "above_threshold_ids": list(row.above_threshold_ids),
        "top10_ids": list(row.top10_ids),
        "missing_above_threshold_ids": list(
            row.missing_above_threshold_ids
        ),
        "missing_top10_ids": list(row.missing_top10_ids),
        "candidate_count": row.candidate_count,
        "above_count": row.above_count,
        "top10_count": row.top10_count,
        "actual_index_kind": row.actual_index_kind,
        "candidate_available": row.candidate_available,
        "unavailable_code": row.unavailable_code,
        "truncated": row.truncated,
    }


_ROW_PAYLOAD_FIELDS = frozenset(
    {
        "query_id",
        "category",
        "reference_record_id",
        "candidate_ids",
        "above_threshold_ids",
        "top10_ids",
        "missing_above_threshold_ids",
        "missing_top10_ids",
        "candidate_count",
        "above_count",
        "top10_count",
        "actual_index_kind",
        "candidate_available",
        "unavailable_code",
        "truncated",
    }
)


def _row_from_payload(payload: object) -> OracleQueryRow:
    fields = _strict_fields(payload, _ROW_PAYLOAD_FIELDS, "oracle query row")
    return OracleQueryRow(
        query_id=_as_int(fields["query_id"], "query id", minimum=1),
        category=_as_category(fields["category"], "query category"),
        reference_record_id=_as_optional_int(
            fields["reference_record_id"],
            "reference record id",
        ),
        candidate_ids=_id_list(fields["candidate_ids"], "candidate ids"),
        above_threshold_ids=_id_list(
            fields["above_threshold_ids"],
            "above-threshold ids",
        ),
        top10_ids=_id_list(fields["top10_ids"], "top-10 ids"),
        missing_above_threshold_ids=_id_list(
            fields["missing_above_threshold_ids"],
            "missing above-threshold ids",
        ),
        missing_top10_ids=_id_list(
            fields["missing_top10_ids"],
            "missing top-10 ids",
        ),
        candidate_count=_as_int(
            fields["candidate_count"],
            "candidate count",
            minimum=0,
        ),
        above_count=_as_int(fields["above_count"], "above count", minimum=0),
        top10_count=_as_int(
            fields["top10_count"],
            "top-10 count",
            minimum=0,
        ),
        actual_index_kind=_as_index_kind(
            fields["actual_index_kind"],
            "actual index kind",
        ),
        candidate_available=_as_bool(
            fields["candidate_available"],
            "candidate available",
        ),
        unavailable_code=_as_optional_code(
            fields["unavailable_code"],
            "unavailable code",
        ),
        truncated=_as_bool(fields["truncated"], "truncated"),
    )


@dataclass(frozen=True)
class OracleRecallEvidence:
    """Frozen path-specific raw candidate-recall evidence for one path.

    Never a Task 8.5 report type and never a pass/fail capability.  The
    evidence privately snapshots the complete contract, binds contract/
    oracle-subset/scorer/path/environment digests, keeps one strict row per
    query, and derives ``evidence_digest`` at construction time
    (``init=False``).  ``recall_passed`` is always recomputed from the rows'
    missing sets, availability and index-kind facts and can never be supplied
    by a caller.  ``test_mode`` evidence uses explicit small test-only counts
    and is never ``final_evidence``.
    """

    schema_version: str
    test_mode: bool
    contract: BenchmarkContract
    contract_digest: str
    oracle_subset_digest: str
    oracle_subset_record_count: int
    oracle_query_count: int
    scorer_config_digest: str
    path_config_digest: str
    execution_path: BenchmarkExecutionPath
    store_index_kind: str
    resource_id: str
    canonical_store_id: str
    fixture_digest: str
    result_limit: int
    candidate_budget_version: str
    candidate_budget: int
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    rows: tuple[OracleQueryRow, ...]
    query_count: int
    missing_above_threshold_total: int
    missing_top10_total: int
    all_queries_available: bool
    index_kind_drift_count: int
    recall_passed: bool
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ORACLE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                "evidence schema version must be "
                f"{ORACLE_EVIDENCE_SCHEMA_VERSION}"
            )
        test_mode = _require_builtin_bool(self.test_mode, "test mode")
        object.__setattr__(self, "test_mode", test_mode)
        if type(self.contract) is not BenchmarkContract:
            raise TypeError("evidence contract must be BenchmarkContract")
        contract_snapshot = contract_from_json(contract_to_json(self.contract))
        if type(contract_snapshot) is not BenchmarkContract:
            raise TypeError(
                "evidence contract snapshot must be BenchmarkContract"
            )
        object.__setattr__(self, "contract", contract_snapshot)
        contract_digest = _require_digest(
            self.contract_digest,
            "contract digest",
        )
        if contract_digest != benchmark_contract_digest(contract_snapshot):
            raise ValueError("contract digest must bind evidence contract")
        oracle_subset_digest = _require_digest(
            self.oracle_subset_digest,
            "oracle subset digest",
        )
        oracle_subset_record_count = _require_builtin_int(
            self.oracle_subset_record_count,
            "oracle subset record count",
            minimum=1,
        )
        oracle_query_count = _require_builtin_int(
            self.oracle_query_count,
            "oracle query count",
            minimum=1,
        )
        if not test_mode:
            if (
                oracle_subset_record_count
                != contract_snapshot.oracle_subset_record_count
                or oracle_query_count != contract_snapshot.oracle_query_count
            ):
                raise ValueError(
                    "real evidence must use literal contract counts"
                )
            if oracle_subset_digest != contract_snapshot.oracle_subset_digest:
                raise ValueError(
                    "oracle subset digest must match evidence contract"
                )
        else:
            if (
                oracle_subset_record_count
                >= contract_snapshot.oracle_subset_record_count
                or oracle_query_count
                >= contract_snapshot.oracle_query_count
            ):
                raise ValueError(
                    "test-mode evidence must use explicit miniature counts"
                )
        scorer_config_digest = _require_digest(
            self.scorer_config_digest,
            "scorer config digest",
        )
        if scorer_config_digest != recompute_scorer_config_digest(
            contract_snapshot
        ):
            raise ValueError(
                "scorer config digest must match evidence contract"
            )
        path_config_digest = _require_digest(
            self.path_config_digest,
            "path config digest",
        )
        if type(self.execution_path) is not BenchmarkExecutionPath:
            raise TypeError("execution path must be BenchmarkExecutionPath")
        expected_path_digest = (
            contract_snapshot.fast_path_config_digest
            if self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract_snapshot.fallback_path_config_digest
        )
        if path_config_digest != expected_path_digest:
            raise ValueError("path config digest must match evidence contract")
        expected_index_kind = expected_store_index_kind(self.execution_path)
        if type(self.store_index_kind) is not str:
            raise TypeError("store index kind must be a built-in string")
        if self.store_index_kind != expected_index_kind:
            raise ValueError(
                "store index kind must equal the requested execution path"
            )
        _require_identity(self.resource_id, "resource id")
        _require_identity(self.canonical_store_id, "canonical store id")
        _require_digest(self.fixture_digest, "fixture digest")
        result_limit = _require_builtin_int(
            self.result_limit,
            "result limit",
            minimum=1,
        )
        if result_limit != contract_snapshot.top_k:
            raise ValueError("result limit must equal contract top_k")
        candidate_budget_version = _require_identity(
            self.candidate_budget_version,
            "candidate budget version",
        )
        if candidate_budget_version != CANDIDATE_BUDGET_VERSION:
            raise ValueError(
                "candidate budget version must be candidate-budget-v1"
            )
        if candidate_budget_version != contract_snapshot.candidate_budget_version:
            raise ValueError(
                "candidate budget version must match evidence contract"
            )
        candidate_budget = _require_builtin_int(
            self.candidate_budget,
            "candidate budget",
            minimum=1,
        )
        if candidate_budget != candidate_budget_v1(result_limit):
            raise ValueError(
                "candidate budget must equal candidate-budget-v1 for top_k"
            )
        if type(self.environment) is not tuple:
            raise TypeError("environment must be a built-in tuple")
        environment = tuple(self.environment)
        validate_oracle_environment(environment, self.execution_path)
        object.__setattr__(self, "environment", environment)
        environment_digest = _require_digest(
            self.environment_digest,
            "environment digest",
        )
        if environment_digest != benchmark_environment_digest(environment):
            raise ValueError("environment digest must bind environment facts")
        if type(self.rows) is not tuple:
            raise TypeError("rows must be a built-in tuple")
        rows = tuple(self.rows)
        if len(rows) != oracle_query_count:
            raise ValueError("row count must equal oracle query count")
        expected_id = 1
        for row in rows:
            if type(row) is not OracleQueryRow:
                raise TypeError("rows must contain OracleQueryRow values")
            if row.query_id != expected_id:
                raise ValueError(
                    "row query ids must be exactly 1..N with no gaps"
                )
            if row.top10_count != result_limit:
                raise ValueError(
                    "top-10 obligation must always be the full top_k"
                )
            expected_id += 1
        object.__setattr__(self, "rows", rows)
        if self.query_count != oracle_query_count:
            raise ValueError("query count must equal oracle query count")
        missing_above_threshold_total = _require_builtin_int(
            self.missing_above_threshold_total,
            "missing above-threshold total",
            minimum=0,
        )
        missing_top10_total = _require_builtin_int(
            self.missing_top10_total,
            "missing top-10 total",
            minimum=0,
        )
        recomputed_above = sum(
            len(row.missing_above_threshold_ids) for row in rows
        )
        recomputed_top10 = sum(len(row.missing_top10_ids) for row in rows)
        if (
            missing_above_threshold_total != recomputed_above
            or missing_top10_total != recomputed_top10
        ):
            raise ValueError("missing totals must match the rows")
        all_queries_available = all(
            row.candidate_available for row in rows
        )
        if self.all_queries_available != all_queries_available:
            raise ValueError(
                "all-queries-available fact must match the rows"
            )
        index_kind_drift_count = sum(
            1
            for row in rows
            if row.actual_index_kind != self.store_index_kind
        )
        if self.index_kind_drift_count != index_kind_drift_count:
            raise ValueError(
                "index-kind drift count must match the rows"
            )
        recall_passed = (
            all_queries_available
            and index_kind_drift_count == 0
            and missing_above_threshold_total == 0
            and missing_top10_total == 0
        )
        if self.recall_passed != recall_passed:
            raise ValueError("recall passed must match the derived facts")
        object.__setattr__(self, "recall_passed", recall_passed)
        object.__setattr__(
            self,
            "evidence_digest",
            oracle_evidence_digest(self),
        )

    @property
    def final_evidence(self) -> bool:
        """True only for literal real-mode evidence on the committed contract."""
        return (
            not self.test_mode
            and self.oracle_subset_record_count
            == self.contract.oracle_subset_record_count
            and self.oracle_query_count == self.contract.oracle_query_count
            and self.oracle_subset_digest == self.contract.oracle_subset_digest
        )

    def recompute_environment_digest(self) -> str:
        return benchmark_environment_digest(self.environment)

    def recompute_evidence_digest(self) -> str:
        return oracle_evidence_digest(self)


def oracle_evidence_digest(evidence: OracleRecallEvidence) -> str:
    """Canonical digest over every evidence fact except the digest itself."""

    if type(evidence) is not OracleRecallEvidence:
        raise TypeError("evidence must be OracleRecallEvidence")
    items: list[dict[str, object]] = [
        {
            "schema_version": evidence.schema_version,
            "test_mode": evidence.test_mode,
            "contract_digest": evidence.contract_digest,
            "oracle_subset_digest": evidence.oracle_subset_digest,
            "oracle_subset_record_count": evidence.oracle_subset_record_count,
            "oracle_query_count": evidence.oracle_query_count,
            "scorer_config_digest": evidence.scorer_config_digest,
            "path_config_digest": evidence.path_config_digest,
            "execution_path": evidence.execution_path.value,
            "store_index_kind": evidence.store_index_kind,
            "resource_id": evidence.resource_id,
            "canonical_store_id": evidence.canonical_store_id,
            "fixture_digest": evidence.fixture_digest,
            "result_limit": evidence.result_limit,
            "candidate_budget_version": evidence.candidate_budget_version,
            "candidate_budget": evidence.candidate_budget,
            "environment_digest": evidence.environment_digest,
            "query_count": evidence.query_count,
            "missing_above_threshold_total": (
                evidence.missing_above_threshold_total
            ),
            "missing_top10_total": evidence.missing_top10_total,
            "all_queries_available": evidence.all_queries_available,
            "index_kind_drift_count": evidence.index_kind_drift_count,
            "recall_passed": evidence.recall_passed,
        }
    ]
    items.extend(_row_payload(row) for row in evidence.rows)
    return benchmark_digest(
        ORACLE_EVIDENCE_DIGEST_VERSION,
        "oracle-recall-evidence",
        items,
    )


_EVIDENCE_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "test_mode",
        "contract_json",
        "contract_digest",
        "oracle_subset_digest",
        "oracle_subset_record_count",
        "oracle_query_count",
        "scorer_config_digest",
        "path_config_digest",
        "execution_path",
        "store_index_kind",
        "resource_id",
        "canonical_store_id",
        "fixture_digest",
        "result_limit",
        "candidate_budget_version",
        "candidate_budget",
        "environment",
        "environment_digest",
        "rows",
        "query_count",
        "missing_above_threshold_total",
        "missing_top10_total",
        "all_queries_available",
        "index_kind_drift_count",
        "recall_passed",
        "evidence_digest",
    }
)


def _environment_payload(
    environment: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in environment]


def _environment_from_payload(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not list:
        raise TypeError("environment must be a JSON list")
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if type(entry) is not list or len(entry) != 2:
            raise TypeError("environment entries must be two-item lists")
        key = _as_str(entry[0], "environment key")
        value_text = _as_str(entry[1], "environment value")
        pairs.append((key, value_text))
    return tuple(pairs)


def _evidence_payload(evidence: OracleRecallEvidence) -> dict[str, object]:
    return {
        "schema_version": evidence.schema_version,
        "test_mode": evidence.test_mode,
        "contract_json": contract_to_json(evidence.contract),
        "contract_digest": evidence.contract_digest,
        "oracle_subset_digest": evidence.oracle_subset_digest,
        "oracle_subset_record_count": evidence.oracle_subset_record_count,
        "oracle_query_count": evidence.oracle_query_count,
        "scorer_config_digest": evidence.scorer_config_digest,
        "path_config_digest": evidence.path_config_digest,
        "execution_path": evidence.execution_path.value,
        "store_index_kind": evidence.store_index_kind,
        "resource_id": evidence.resource_id,
        "canonical_store_id": evidence.canonical_store_id,
        "fixture_digest": evidence.fixture_digest,
        "result_limit": evidence.result_limit,
        "candidate_budget_version": evidence.candidate_budget_version,
        "candidate_budget": evidence.candidate_budget,
        "environment": _environment_payload(evidence.environment),
        "environment_digest": evidence.environment_digest,
        "rows": [_row_payload(row) for row in evidence.rows],
        "query_count": evidence.query_count,
        "missing_above_threshold_total": (
            evidence.missing_above_threshold_total
        ),
        "missing_top10_total": evidence.missing_top10_total,
        "all_queries_available": evidence.all_queries_available,
        "index_kind_drift_count": evidence.index_kind_drift_count,
        "recall_passed": evidence.recall_passed,
        "evidence_digest": evidence.evidence_digest,
    }


def evidence_from_payload(payload: object) -> OracleRecallEvidence:
    """Reconstruct evidence strictly; forged bindings and digests are rejected."""

    fields = _strict_fields(
        payload,
        _EVIDENCE_PAYLOAD_FIELDS,
        "oracle recall evidence",
    )
    contract_json = _as_str(fields["contract_json"], "contract json")
    parsed_contract_json = _parse_strict_json(contract_json)
    contract = contract_from_json(_canonical_json(parsed_contract_json))
    if type(contract) is not BenchmarkContract:
        raise TypeError("evidence contract must be BenchmarkContract")
    try:
        execution_path = BenchmarkExecutionPath(
            _as_str(fields["execution_path"], "execution path")
        )
    except ValueError as error:
        raise ValueError("evidence execution path is invalid") from error
    rows_value = fields["rows"]
    if type(rows_value) is not list:
        raise TypeError("rows must be a JSON list")
    rows = tuple(_row_from_payload(row) for row in rows_value)
    evidence = OracleRecallEvidence(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        test_mode=_as_bool(fields["test_mode"], "test mode"),
        contract=contract,
        contract_digest=_as_digest(
            fields["contract_digest"],
            "contract digest",
        ),
        oracle_subset_digest=_as_digest(
            fields["oracle_subset_digest"],
            "oracle subset digest",
        ),
        oracle_subset_record_count=_as_int(
            fields["oracle_subset_record_count"],
            "oracle subset record count",
            minimum=1,
        ),
        oracle_query_count=_as_int(
            fields["oracle_query_count"],
            "oracle query count",
            minimum=1,
        ),
        scorer_config_digest=_as_digest(
            fields["scorer_config_digest"],
            "scorer config digest",
        ),
        path_config_digest=_as_digest(
            fields["path_config_digest"],
            "path config digest",
        ),
        execution_path=execution_path,
        store_index_kind=_as_str(
            fields["store_index_kind"],
            "store index kind",
        ),
        resource_id=_as_str(fields["resource_id"], "resource id"),
        canonical_store_id=_as_str(
            fields["canonical_store_id"],
            "canonical store id",
        ),
        fixture_digest=_as_digest(fields["fixture_digest"], "fixture digest"),
        result_limit=_as_int(fields["result_limit"], "result limit", minimum=1),
        candidate_budget_version=_as_str(
            fields["candidate_budget_version"],
            "candidate budget version",
        ),
        candidate_budget=_as_int(
            fields["candidate_budget"],
            "candidate budget",
            minimum=1,
        ),
        environment=_environment_from_payload(fields["environment"]),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
        rows=rows,
        query_count=_as_int(fields["query_count"], "query count", minimum=1),
        missing_above_threshold_total=_as_int(
            fields["missing_above_threshold_total"],
            "missing above-threshold total",
            minimum=0,
        ),
        missing_top10_total=_as_int(
            fields["missing_top10_total"],
            "missing top-10 total",
            minimum=0,
        ),
        all_queries_available=_as_bool(
            fields["all_queries_available"],
            "all queries available",
        ),
        index_kind_drift_count=_as_int(
            fields["index_kind_drift_count"],
            "index kind drift count",
            minimum=0,
        ),
        recall_passed=_as_bool(fields["recall_passed"], "recall passed"),
    )
    if evidence.evidence_digest != _as_digest(
        fields["evidence_digest"],
        "evidence digest",
    ):
        raise ValueError("evidence digest must match the reconstructed facts")
    return evidence


def evidence_to_json(evidence: OracleRecallEvidence) -> str:
    if type(evidence) is not OracleRecallEvidence:
        raise TypeError("evidence must be OracleRecallEvidence")
    return _canonical_json(_evidence_payload(evidence))


def evidence_from_json(raw: str) -> OracleRecallEvidence:
    return evidence_from_payload(_parse_strict_json(raw))


def _build_recall_evidence(
    *,
    contract: BenchmarkContract,
    execution_path: BenchmarkExecutionPath,
    run_root: Path,
    test_mode: bool,
    records: tuple[BenchmarkRecord, ...],
    queries: tuple[BenchmarkQuery, ...],
    oracle_rows: tuple[FullScanQueryOracle, ...],
    resource_id: str,
    canonical_store_id: str,
) -> OracleRecallEvidence:
    """Execute one path against owner-derived, already validated oracle rows."""

    (
        fixture_digest,
        store_index_kind,
        candidate_facts,
    ) = _run_candidate_path(
        contract=contract,
        execution_path=execution_path,
        records=records,
        queries=queries,
        run_root=run_root,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
    )
    if len(candidate_facts) != len(oracle_rows):
        raise ValueError("candidate facts must align with oracle rows")

    rows: list[OracleQueryRow] = []
    for oracle_row, query, candidate_fact in zip(
        oracle_rows,
        queries,
        candidate_facts,
        strict=True,
    ):
        (
            query_id,
            candidate_ids,
            actual_index_kind,
            candidate_available,
            unavailable_code,
            truncated,
        ) = candidate_fact
        if query_id != query.query_id:
            raise ValueError("candidate fact query id mismatch")
        candidate_set = set(candidate_ids)
        missing_above = tuple(
            record_id
            for record_id in oracle_row.above_threshold_ids
            if record_id not in candidate_set
        )
        missing_top10 = tuple(
            record_id
            for record_id in oracle_row.top10_ids
            if record_id not in candidate_set
        )
        rows.append(
            OracleQueryRow(
                query_id=query.query_id,
                category=query.category,
                reference_record_id=query.reference_record_id,
                candidate_ids=candidate_ids,
                above_threshold_ids=oracle_row.above_threshold_ids,
                top10_ids=oracle_row.top10_ids,
                missing_above_threshold_ids=missing_above,
                missing_top10_ids=missing_top10,
                candidate_count=len(candidate_ids),
                above_count=len(oracle_row.above_threshold_ids),
                top10_count=len(oracle_row.top10_ids),
                actual_index_kind=actual_index_kind,
                candidate_available=candidate_available,
                unavailable_code=unavailable_code,
                truncated=truncated,
            )
        )

    oracle_subset_digest = _oracle_subset_digest(
        contract.corpus_generator_version,
        queries,
        records,
    )
    if not test_mode and oracle_subset_digest != contract.oracle_subset_digest:
        raise ValueError(
            "oracle subset digest does not match the committed contract"
        )
    fts5_enabled = (
        _probe_fts5()
        if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else False
    )
    environment = collect_oracle_environment(fts5_enabled=fts5_enabled)
    validate_oracle_environment(environment, execution_path)
    environment_digest = benchmark_environment_digest(environment)

    all_queries_available = all(row.candidate_available for row in rows)
    index_kind_drift_count = sum(
        1
        for row in rows
        if row.actual_index_kind != store_index_kind
    )
    missing_above_threshold_total = sum(
        len(row.missing_above_threshold_ids) for row in rows
    )
    missing_top10_total = sum(
        len(row.missing_top10_ids) for row in rows
    )
    return OracleRecallEvidence(
        schema_version=ORACLE_EVIDENCE_SCHEMA_VERSION,
        test_mode=test_mode,
        contract=contract,
        contract_digest=benchmark_contract_digest(contract),
        oracle_subset_digest=oracle_subset_digest,
        oracle_subset_record_count=len(records),
        oracle_query_count=len(queries),
        scorer_config_digest=recompute_scorer_config_digest(contract),
        path_config_digest=(
            contract.fast_path_config_digest
            if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract.fallback_path_config_digest
        ),
        execution_path=execution_path,
        store_index_kind=store_index_kind,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        fixture_digest=fixture_digest,
        result_limit=contract.top_k,
        candidate_budget_version=contract.candidate_budget_version,
        candidate_budget=candidate_budget_v1(contract.top_k),
        environment=environment,
        environment_digest=environment_digest,
        rows=tuple(rows),
        query_count=len(rows),
        missing_above_threshold_total=missing_above_threshold_total,
        missing_top10_total=missing_top10_total,
        all_queries_available=all_queries_available,
        index_kind_drift_count=index_kind_drift_count,
        recall_passed=(
            all_queries_available
            and index_kind_drift_count == 0
            and missing_above_threshold_total == 0
            and missing_top10_total == 0
        ),
    )


def run_oracle_recall_evidence(
    *,
    contract: BenchmarkContract,
    execution_path: BenchmarkExecutionPath,
    run_root: Path,
    test_mode: bool = False,
    test_record_count: int | None = None,
    test_query_count: int | None = None,
    oracle: tuple[FullScanQueryOracle, ...] | None = None,
    scorer: SimilarityScorerV1 | None = None,
    resource_id: str = ORACLE_DEFAULT_RESOURCE_ID,
    canonical_store_id: str = ORACLE_DEFAULT_CANONICAL_STORE_ID,
) -> OracleRecallEvidence:
    """Run one requested path's literal (or miniature test-only) recall evidence.

    Real mode materializes and validates the literal contract counts
    (5000-record subset, 200 queries, threshold 0.60, top_k 10, budgets from
    candidate-budget-v1).  Test mode uses the same production generator and
    thresholds with explicit small test-only counts and marks the evidence
    ``test_mode=True``; it is never ``final_evidence``.
    """

    if type(contract) is not BenchmarkContract:
        raise TypeError("contract must be BenchmarkContract")
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    if type(run_root) is not _NATIVE_PATH_TYPE:
        raise TypeError("run root must be a native pathlib.Path")
    if type(resource_id) is not str or not resource_id:
        raise ValueError("resource id must be a non-empty string")
    if type(canonical_store_id) is not str or not canonical_store_id:
        raise ValueError("canonical store id must be a non-empty string")
    test_mode = _require_builtin_bool(test_mode, "test mode")
    if test_mode:
        if (
            type(test_record_count) is not int
            or type(test_query_count) is not int
        ):
            raise ValueError(
                "test mode requires explicit test record and query counts"
            )
        if not (
            1
            <= test_record_count
            < min(contract.oracle_subset_record_count, _MAX_TEST_RECORD_COUNT + 1)
        ):
            raise ValueError(
                "test record count must be a small explicit test-only value"
            )
        if not (
            1
            <= test_query_count
            <= min(contract.oracle_query_count, _MAX_TEST_QUERY_COUNT)
        ):
            raise ValueError(
                "test query count must be a small explicit test-only value"
            )
        if test_record_count < contract.top_k:
            raise ValueError("test record count must be at least top_k")
        assert isinstance(test_record_count, int)
        assert isinstance(test_query_count, int)
        record_count = test_record_count
        query_count = test_query_count
    else:
        if test_record_count is not None or test_query_count is not None:
            raise ValueError(
                "test counts are only allowed in test mode"
            )
        if oracle is not None:
            raise ValueError(
                "real evidence must derive full-scan oracle inside its owner"
            )
        record_count = contract.oracle_subset_record_count
        query_count = contract.oracle_query_count

    records = tuple(
        iter_oracle_subset_records(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            subset_count=record_count,
        )
    )
    queries = tuple(
        iter_oracle_queries(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            subset_count=record_count,
            query_count=query_count,
        )
    )
    if len(records) != record_count or len(queries) != query_count:
        raise ValueError("oracle iterator counts drifted from request")
    _validate_records(records)
    _validate_queries(queries)

    if oracle is None:
        oracle_rows = compute_full_scan_oracle(
            contract=contract,
            records=records,
            queries=queries,
            scorer=scorer,
        )
    else:
        if type(oracle) is not tuple:
            raise TypeError("oracle must be a built-in tuple")
        if len(oracle) != len(queries):
            raise ValueError("oracle row count must equal query count")
        for oracle_row, query in zip(oracle, queries, strict=True):
            if type(oracle_row) is not FullScanQueryOracle:
                raise TypeError("oracle must contain FullScanQueryOracle rows")
            if oracle_row.query_id != query.query_id:
                raise ValueError("oracle row query id mismatch")
            if oracle_row.category != query.category:
                raise ValueError("oracle row category mismatch")
            if oracle_row.reference_record_id != query.reference_record_id:
                raise ValueError("oracle row reference id mismatch")
        oracle_rows = tuple(oracle)

    return _build_recall_evidence(
        contract=contract,
        execution_path=execution_path,
        run_root=run_root,
        test_mode=test_mode,
        records=records,
        queries=queries,
        oracle_rows=oracle_rows,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
    )


def run_oracle_recall_suite(
    *,
    contract: BenchmarkContract,
    fts5_run_root: Path,
    fallback_run_root: Path,
) -> tuple[OracleRecallEvidence, OracleRecallEvidence]:
    """Run both literal paths from one owner-derived full-scan oracle.

    This is the only real-mode reuse seam: callers cannot provide oracle rows.
    The owner freezes the literal records/queries, computes the full scan once,
    then applies both independent SQLite candidate paths in enum order.
    """

    if type(contract) is not BenchmarkContract:
        raise TypeError("contract must be BenchmarkContract")
    for label, root in (
        ("fts5 run root", fts5_run_root),
        ("fallback run root", fallback_run_root),
    ):
        if type(root) is not _NATIVE_PATH_TYPE:
            raise TypeError(f"{label} must be a native pathlib.Path")
    if fts5_run_root.resolve() == fallback_run_root.resolve():
        raise ValueError("oracle path run roots must be distinct")
    records = tuple(
        iter_oracle_subset_records(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            subset_count=contract.oracle_subset_record_count,
        )
    )
    queries = tuple(
        iter_oracle_queries(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
            subset_count=contract.oracle_subset_record_count,
            query_count=contract.oracle_query_count,
        )
    )
    records = _validate_records(records)
    queries = _validate_queries(queries)
    oracle_rows = compute_full_scan_oracle(
        contract=contract,
        records=records,
        queries=queries,
    )
    fts5 = _build_recall_evidence(
        contract=contract,
        execution_path=BenchmarkExecutionPath.FTS5_TRIGRAM,
        run_root=fts5_run_root,
        test_mode=False,
        records=records,
        queries=queries,
        oracle_rows=oracle_rows,
        resource_id=f"{ORACLE_DEFAULT_RESOURCE_ID}.fts5",
        canonical_store_id=f"{ORACLE_DEFAULT_CANONICAL_STORE_ID}.fts5",
    )
    fallback = _build_recall_evidence(
        contract=contract,
        execution_path=BenchmarkExecutionPath.GRAM_FALLBACK,
        run_root=fallback_run_root,
        test_mode=False,
        records=records,
        queries=queries,
        oracle_rows=oracle_rows,
        resource_id=f"{ORACLE_DEFAULT_RESOURCE_ID}.fallback",
        canonical_store_id=f"{ORACLE_DEFAULT_CANONICAL_STORE_ID}.fallback",
    )
    return fts5, fallback


def run_oracle_recall_evidence_from_path(
    *,
    contract_path: Path,
    execution_path: BenchmarkExecutionPath,
    run_root: Path,
    test_mode: bool = False,
    test_record_count: int | None = None,
    test_query_count: int | None = None,
    oracle: tuple[FullScanQueryOracle, ...] | None = None,
    scorer: SimilarityScorerV1 | None = None,
    resource_id: str = ORACLE_DEFAULT_RESOURCE_ID,
    canonical_store_id: str = ORACLE_DEFAULT_CANONICAL_STORE_ID,
) -> OracleRecallEvidence:
    """Strictly load and revalidate the committed contract, then run evidence."""

    if type(contract_path) is not Path:
        raise TypeError("contract path must be pathlib.Path")
    contract = load_benchmark_contract(contract_path)
    recompute_benchmark_inputs(contract_path)
    return run_oracle_recall_evidence(
        contract=contract,
        execution_path=execution_path,
        run_root=run_root,
        test_mode=test_mode,
        test_record_count=test_record_count,
        test_query_count=test_query_count,
        oracle=oracle,
        scorer=scorer,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
    )


__all__ = [
    "ORACLE_DEFAULT_CANONICAL_STORE_ID",
    "ORACLE_DEFAULT_RESOURCE_ID",
    "ORACLE_EVIDENCE_DIGEST_VERSION",
    "ORACLE_EVIDENCE_SCHEMA_VERSION",
    "FullScanQueryOracle",
    "OraclePathUnavailableError",
    "OracleQueryRow",
    "OracleRecallEvidence",
    "collect_oracle_environment",
    "compute_full_scan_oracle",
    "evidence_from_json",
    "evidence_from_payload",
    "evidence_to_json",
    "expected_store_index_kind",
    "oracle_evidence_digest",
    "run_oracle_recall_evidence",
    "run_oracle_recall_evidence_from_path",
    "run_oracle_recall_suite",
    "validate_oracle_environment",
]
