"""Isolated child-process migration/reopen/query RSS evidence owner (Task 8.3).

Ownership
---------
This module owns benchmark-v1 migration/RSS evidence measured inside a fresh,
isolated interpreter child process for the two immutable index paths
(FTS5_TRIGRAM and GRAM_FALLBACK).  It is an offline validation/batch owner
only: no production runtime module imports it, and it never constructs a
``BenchmarkReport`` or grants any capability (Task 8.5 owns reports and
Gate D).  Task 8.4 owns oracle recall; this module performs no recall scoring.

Process/RSS invariant capsule
-----------------------------
- The parent runner strictly loads ``BenchmarkContract`` and verifies the
  Task 8.1 corpus digest/count.  A pre-generated immutable JSONL fixture
  (100,000 records in a real run; explicit small test-only counts in tests)
  is written and digested before the child is spawned and is excluded from
  measured cost.  Evidence binds the fixture digest/identity and the
  contract corpus digest.
- Every requested path/run spawns a brand-new interpreter via
  ``sys.executable`` with a narrow machine-readable worker mode.  No
  in-process fake is Task 8.3 evidence.  The parent captures the child exit
  status/stdout/stderr safely; diagnostics never contain record/query bodies.
- The child measurement scope starts at child entry before opening/parsing
  and covers source parse, migration stage record insertion and candidate
  index build, full stage/seal/Gate B validation and fsync, durable
  activation publication, a fresh coordinator/runtime reopen health proof,
  and at least one exact plus one fuzzy/candidate query path proof.  Only
  after all of that may the child report success.
- The fast path runs with actual FTS5 available and proves active health
  ``index_kind == FTS5_TRIGRAM``.  The fallback path forces
  ``fts5_available=False`` through a bounded child-local patch of the
  existing ``tm_sqlite_store._probe_fts5`` seam and proves
  ``GRAM_FALLBACK`` from the real schema/index health and query report.
- Migration elapsed uses ``perf_counter_ns`` from before parse/open through
  reopen + health + query proof and reports raw ``elapsed_ns``.  Peak RSS
  covers the whole child lifetime; on Linux ``ru_maxrss`` KiB is normalized
  to bytes with the platform/unit recorded.  Start and terminal samples plus
  the maximum are captured; baseline RSS is never subtracted.
- Evidence is frozen and self-validating: it carries a private complete
  ``BenchmarkContract`` snapshot, contract/corpus/path/fixture/environment/
  worker-protocol digests, the path, actual index kind, record counts,
  generation, ``migration_elapsed_ns``, ``peak_rss_bytes``, RSS unit/scope,
  child pid/exit and proof facts, and a derived evidence digest
  (``init=False``).  Bool/subclass scalars, non-finite or negative values,
  and any contract/count/digest/path/index/environment/process-proof
  mismatch fail closed.
- The parent runner requires the exact child schema and closed keys, rejects
  duplicate JSON keys/non-finite/extra stdout/missing fields, checks
  ``returncode == 0``, and recomputes the child evidence/digests.

Measurement boundaries
----------------------
- Fixture generation/writing happens parent-side before child spawn and is
  never part of the child's measured elapsed or RSS scope.
- The caller supplies a dedicated closed run root: empty when generating the
  fixture, or containing only one regular single-link fixture.  The child
  writes only inside that root and deliberately retains its sidecar/manifest/
  journal/terminal/lineage evidence; cleanup belongs to the caller that owns
  the whole run root and is outside the measured lifecycle.
- No network, credentials, or telemetry; evidence and diagnostics never
  contain query, source, or target text.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import stat
import sqlite3
import subprocess
import sys
import time
import unicodedata
from unittest.mock import patch

from text_matcher import fold_text_v1
from tm_benchmark import (
    BenchmarkRecord,
    _record_payload,
    benchmark_digest,
    iter_corpus_records,
    load_benchmark_contract,
    recompute_benchmark_inputs,
)
from tm_benchmark_latency import validate_environment_for_path
from tm_candidate_index import CandidateRetriever
from tm_contracts import (
    BENCHMARK_RSS_SCOPE,
    BenchmarkContract,
    BenchmarkExecutionPath,
    CanonicalResourceIdentity,
    benchmark_contract_digest,
    benchmark_environment_digest,
    candidate_budget_v1,
    contract_from_json,
    contract_to_json,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    SQLiteTMStore,
    _probe_fts5,
)
from tm_stage_sealer import StageSealer

PROCESS_EVIDENCE_SCHEMA_VERSION = "tm-benchmark-process-evidence-v1"
PROCESS_WORKER_PROTOCOL_VERSION = "tm-benchmark-process-worker-v1"
PROCESS_EVIDENCE_DIGEST_VERSION = "tm-benchmark-process-digest-v1"

REAL_CORPUS_RECORD_COUNT = 100_000
WORKER_MODE_FLAG = "--worker"
WORKER_RSS_UNIT = "bytes"

_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NATIVE_PATH_TYPE = type(Path())

_ENVIRONMENT_RSS_KEYS = frozenset({"rss_platform", "rss_raw_unit", "rss_scope"})


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


def _require_absolute_path_string(value: object, field_name: str) -> str:
    text = _require_identity(value, field_name)
    if not os.path.isabs(text):
        raise ValueError(f"{field_name} must be an absolute path")
    return text


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


def _as_bool(value: object, field_name: str) -> bool:
    return _require_builtin_bool(value, field_name)


def _as_int(value: object, field_name: str, *, minimum: int) -> int:
    return _require_builtin_int(value, field_name, minimum=minimum)


def _as_str(value: object, field_name: str) -> str:
    return _require_identity(value, field_name)


def _as_digest(value: object, field_name: str) -> str:
    return _require_digest(value, field_name)


def _fixture_row(record: BenchmarkRecord) -> dict[str, object]:
    """One migration JSONL row for a corpus record.

    The row intentionally omits ``provenance`` so the migration degrades to
    the legacy default provenance that the stage sealer expects.
    """

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


def _iter_fixture_lines(
    records: Iterable[BenchmarkRecord],
) -> Iterator[bytes]:
    for record in records:
        yield (_canonical_json(_fixture_row(record)) + "\n").encode("utf-8")


def _generate_fixture(
    fixture_path: Path,
    records: Iterable[BenchmarkRecord],
) -> tuple[str, int]:
    """Write one immutable JSONL fixture and return (sha256, record count)."""
    hasher = hashlib.sha256()
    count = 0
    with fixture_path.open("xb") as stream:
        for line in _iter_fixture_lines(records):
            stream.write(line)
            hasher.update(line)
            count += 1
    if count < 1:
        raise ValueError("fixture generation produced no records")
    return hasher.hexdigest(), count


def _require_single_link_regular_file(path: Path, field_name: str) -> os.stat_result:
    if type(path) is not _NATIVE_PATH_TYPE:
        raise TypeError(f"{field_name} must be pathlib.Path")
    try:
        stat_result = path.lstat()
    except OSError as error:
        raise ValueError(f"{field_name} cannot be inspected") from error
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_nlink != 1:
        raise ValueError(f"{field_name} must be a regular single-link file")
    return stat_result


def _expected_fixture_facts(
    records: Iterable[BenchmarkRecord],
) -> tuple[str, int]:
    """Hash/count the canonical fixture rows without writing any file."""
    hasher = hashlib.sha256()
    count = 0
    for line in _iter_fixture_lines(records):
        hasher.update(line)
        count += 1
    if count < 1:
        raise ValueError("fixture generation produced no records")
    return hasher.hexdigest(), count


def _read_fixture_facts(
    fixture_path: Path,
    *,
    expected_digest: str,
    expected_count: int,
) -> tuple[str, int, str]:
    """Hash and count the immutable fixture; return digest, count, source."""
    before = _require_single_link_regular_file(fixture_path, "fixture")
    hasher = hashlib.sha256()
    count = 0
    first_source: str | None = None
    try:
        with fixture_path.open("rb") as stream:
            for raw_line in stream:
                hasher.update(raw_line)
                line = raw_line.strip()
                if not line:
                    continue
                count += 1
                if first_source is None:
                    payload = _parse_strict_json(line.decode("utf-8"))
                    source_raw = payload.get("source")
                    if type(source_raw) is not str or not source_raw:
                        raise ValueError(
                            "fixture first row source is invalid"
                        )
                    first_source = source_raw
    except OSError as error:
        raise ValueError("cannot read fixture file") from error
    after = _require_single_link_regular_file(fixture_path, "fixture")
    if (after.st_dev, after.st_ino, after.st_size) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
    ):
        raise ValueError("fixture identity changed while reading")
    digest = hasher.hexdigest()
    if digest != expected_digest:
        raise ValueError("fixture digest does not match the run request")
    if count != expected_count:
        raise ValueError("fixture record count does not match the run request")
    if first_source is None:
        raise ValueError("fixture is empty")
    return digest, count, first_source


def _ram_mib() -> str:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown"
    if pages is None or page_size is None:
        return "unknown"
    return str((pages * page_size) // (1024 * 1024))


def collect_process_environment(
    *,
    fts5_enabled: bool,
    rss_raw_unit: str,
    rss_platform: str,
    rss_scope: str,
) -> tuple[tuple[str, str], ...]:
    """Collect stable child-runtime facts plus RSS unit/scope facts."""
    _require_identity(rss_raw_unit, "RSS raw unit")
    _require_identity(rss_platform, "RSS platform")
    _require_identity(rss_scope, "RSS scope")
    environment = {
        "cpu": platform.processor() or platform.machine() or "unknown",
        "fts5_enabled": "true" if fts5_enabled else "false",
        "os": platform.system() or "unknown",
        "python_version": platform.python_version(),
        "ram_mib": _ram_mib(),
        "rss_platform": rss_platform,
        "rss_raw_unit": rss_raw_unit,
        "rss_scope": rss_scope,
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_version": unicodedata.unidata_version,
    }
    return tuple(sorted(environment.items()))


def worker_protocol_digest(
    *,
    contract_digest: str,
    corpus_digest: str,
    corpus_record_count: int,
    fixture_digest: str,
    fixture_path: str,
    fixture_record_count: int,
    run_root: str,
    execution_path: BenchmarkExecutionPath,
    resource_id: str,
    canonical_store_id: str,
    test_mode: bool,
) -> str:
    """Canonical digest over every machine-readable worker request fact."""
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    payload: dict[str, object] = {
        "canonical_store_id": _require_identity(
            canonical_store_id,
            "canonical store id",
        ),
        "contract_digest": _require_digest(contract_digest, "contract digest"),
        "corpus_digest": _require_digest(corpus_digest, "corpus digest"),
        "corpus_record_count": _require_builtin_int(
            corpus_record_count,
            "corpus record count",
            minimum=1,
        ),
        "execution_path": execution_path.value,
        "fixture_digest": _require_digest(fixture_digest, "fixture digest"),
        "fixture_path": _require_absolute_path_string(
            fixture_path,
            "fixture path",
        ),
        "fixture_record_count": _require_builtin_int(
            fixture_record_count,
            "fixture record count",
            minimum=1,
        ),
        "resource_id": _require_identity(resource_id, "resource id"),
        "run_root": _require_absolute_path_string(run_root, "run root"),
        "test_mode": _require_builtin_bool(test_mode, "test mode"),
    }
    return benchmark_digest(
        PROCESS_WORKER_PROTOCOL_VERSION,
        "process-worker-request",
        [payload],
    )


@dataclass(frozen=True)
class TMBenchmarkProcessEvidence:
    """Frozen path-specific raw child-process migration/RSS evidence.

    Never a ``BenchmarkReport`` and never a pass/fail capability.  The
    evidence binds a private complete contract snapshot, every input digest,
    the actual child proof facts, and a derived ``evidence_digest`` that
    callers can never supply (``init=False``).  ``test_mode`` evidence uses
    explicit small test-only counts and is never ``final_evidence``.
    """

    schema_version: str
    test_mode: bool
    contract: BenchmarkContract
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    fixture_digest: str
    fixture_path: str
    fixture_record_count: int
    run_root: str
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
    worker_protocol_digest: str
    child_pid: int
    child_exit_code: int
    reopen_phase: str
    reopen_action: str
    reopen_health_healthy: bool
    reopen_health_index_kind: str
    reopen_health_record_count: int
    reopen_health_exact_available: bool
    exact_proof_result_count: int
    exact_proof_winner_matched: bool
    candidate_proof_index_kind: str
    candidate_proof_count: int
    candidate_proof_available: bool
    candidate_proof_budget: int
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PROCESS_EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                "schema version must be " f"{PROCESS_EVIDENCE_SCHEMA_VERSION}"
            )
        test_mode = _require_builtin_bool(self.test_mode, "test mode")
        if type(self.contract) is not BenchmarkContract:
            raise TypeError("evidence contract must be BenchmarkContract")
        contract_snapshot = contract_from_json(contract_to_json(self.contract))
        if type(contract_snapshot) is not BenchmarkContract:
            raise TypeError("evidence contract snapshot must be BenchmarkContract")
        object.__setattr__(self, "contract", contract_snapshot)
        contract_digest = _require_digest(self.contract_digest, "contract digest")
        if contract_digest != benchmark_contract_digest(contract_snapshot):
            raise ValueError("contract digest must bind evidence contract")
        _require_digest(self.corpus_digest, "corpus digest")
        _require_digest(self.fixture_digest, "fixture digest")
        _require_digest(self.path_config_digest, "path config digest")
        _require_digest(self.environment_digest, "environment digest")
        _require_digest(
            self.worker_protocol_digest,
            "worker protocol digest",
        )
        if type(self.execution_path) is not BenchmarkExecutionPath:
            raise TypeError("execution path must be BenchmarkExecutionPath")
        expected_path_digest = (
            contract_snapshot.fast_path_config_digest
            if self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract_snapshot.fallback_path_config_digest
        )
        if self.path_config_digest != expected_path_digest:
            raise ValueError("path config digest must match evidence contract")
        corpus_record_count = _require_builtin_int(
            self.corpus_record_count,
            "corpus record count",
            minimum=1,
        )
        fixture_record_count = _require_builtin_int(
            self.fixture_record_count,
            "fixture record count",
            minimum=1,
        )
        record_count = _require_builtin_int(
            self.record_count,
            "record count",
            minimum=1,
        )
        if (
            corpus_record_count != fixture_record_count
            or fixture_record_count != record_count
        ):
            raise ValueError(
                "corpus/fixture/store record counts must be equal"
            )
        if not test_mode:
            if corpus_record_count != REAL_CORPUS_RECORD_COUNT:
                raise ValueError(
                    "real evidence requires the 100000-record benchmark corpus"
                )
            if corpus_record_count != contract_snapshot.corpus_record_count:
                raise ValueError(
                    "corpus record count must match evidence contract"
                )
            if self.corpus_digest != contract_snapshot.corpus_digest:
                raise ValueError("corpus digest must match evidence contract")
        _require_absolute_path_string(self.fixture_path, "fixture path")
        _require_absolute_path_string(self.run_root, "run root")
        if os.path.dirname(self.fixture_path) != self.run_root:
            raise ValueError("fixture path must live directly in the run root")
        _require_identity(self.resource_id, "resource id")
        _require_identity(self.canonical_store_id, "canonical store id")
        expected_index_kind = (
            "FTS5_TRIGRAM"
            if self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else "GRAM_FALLBACK"
        )
        actual_index_kind = _require_identity(
            self.actual_index_kind,
            "actual index kind",
        )
        if actual_index_kind != expected_index_kind:
            raise ValueError(
                "actual index kind must match the execution path"
            )
        _require_builtin_int(self.generation, "generation", minimum=0)
        _require_builtin_int(
            self.migration_elapsed_ns,
            "migration elapsed nanoseconds",
            minimum=0,
        )
        peak_rss = _require_builtin_int(
            self.peak_rss_bytes,
            "peak RSS bytes",
            minimum=1,
        )
        rss_start = _require_builtin_int(
            self.rss_start_bytes,
            "RSS start bytes",
            minimum=1,
        )
        rss_terminal = _require_builtin_int(
            self.rss_terminal_bytes,
            "RSS terminal bytes",
            minimum=1,
        )
        if rss_start > rss_terminal:
            raise ValueError("RSS terminal sample must not be below start")
        if peak_rss != rss_terminal:
            raise ValueError(
                "peak RSS must equal the terminal high-water sample"
            )
        if self.rss_unit != WORKER_RSS_UNIT:
            raise ValueError(f"RSS unit must be {WORKER_RSS_UNIT!r}")
        if self.rss_scope != contract_snapshot.rss_scope:
            raise ValueError("RSS scope must match evidence contract")
        if self.rss_scope != BENCHMARK_RSS_SCOPE:
            raise ValueError(f"RSS scope must be {BENCHMARK_RSS_SCOPE}")
        validate_environment_for_path(self.environment, self.execution_path)
        environment = dict(self.environment)
        for key in _ENVIRONMENT_RSS_KEYS:
            if key not in environment:
                raise ValueError(f"environment is missing {key!r}")
        if environment["rss_scope"] != self.rss_scope:
            raise ValueError("environment RSS scope must match evidence")
        if environment["rss_raw_unit"] not in ("kib", "bytes"):
            raise ValueError("environment RSS raw unit is invalid")
        _require_identity(environment["rss_platform"], "RSS platform")
        if self.environment_digest != benchmark_environment_digest(
            self.environment
        ):
            raise ValueError("environment digest does not match environment")
        expected_protocol_digest = worker_protocol_digest(
            contract_digest=contract_digest,
            corpus_digest=self.corpus_digest,
            corpus_record_count=corpus_record_count,
            fixture_digest=self.fixture_digest,
            fixture_path=self.fixture_path,
            fixture_record_count=fixture_record_count,
            run_root=self.run_root,
            execution_path=self.execution_path,
            resource_id=self.resource_id,
            canonical_store_id=self.canonical_store_id,
            test_mode=test_mode,
        )
        if self.worker_protocol_digest != expected_protocol_digest:
            raise ValueError(
                "worker protocol digest must match the run request facts"
            )
        _require_builtin_int(self.child_pid, "child pid", minimum=1)
        _require_builtin_int(self.child_exit_code, "child exit code", minimum=0)
        if self.child_exit_code != 0:
            raise ValueError("child exit code must be 0")
        _require_identity(self.reopen_phase, "reopen phase")
        _require_identity(self.reopen_action, "reopen action")
        if self.reopen_phase != "GENERATION_PUBLISHED":
            raise ValueError("reopen phase must be GENERATION_PUBLISHED")
        if self.reopen_action != "COMPLETED":
            raise ValueError("reopen action must be COMPLETED")
        if not _require_builtin_bool(
            self.reopen_health_healthy,
            "reopen health healthy",
        ):
            raise ValueError("reopen health must be healthy")
        reopen_health_index_kind = _require_identity(
            self.reopen_health_index_kind,
            "reopen health index kind",
        )
        if reopen_health_index_kind != actual_index_kind:
            raise ValueError(
                "reopen health index kind must equal actual index kind"
            )
        reopen_record_count = _require_builtin_int(
            self.reopen_health_record_count,
            "reopen health record count",
            minimum=1,
        )
        if reopen_record_count != record_count:
            raise ValueError(
                "reopen health record count must equal store record count"
            )
        if not _require_builtin_bool(
            self.reopen_health_exact_available,
            "reopen health exact available",
        ):
            raise ValueError("reopen health must prove exact availability")
        exact_proof_count = _require_builtin_int(
            self.exact_proof_result_count,
            "exact proof result count",
            minimum=1,
        )
        if exact_proof_count < 1:
            raise ValueError("exact proof must return at least one record")
        if not _require_builtin_bool(
            self.exact_proof_winner_matched,
            "exact proof winner matched",
        ):
            raise ValueError("exact proof winner must match the query")
        candidate_proof_index_kind = _require_identity(
            self.candidate_proof_index_kind,
            "candidate proof index kind",
        )
        if candidate_proof_index_kind != actual_index_kind:
            raise ValueError(
                "candidate proof index kind must equal actual index kind"
            )
        candidate_proof_count = _require_builtin_int(
            self.candidate_proof_count,
            "candidate proof count",
            minimum=1,
        )
        if candidate_proof_count < 1:
            raise ValueError(
                "candidate proof must return at least one candidate"
            )
        if not _require_builtin_bool(
            self.candidate_proof_available,
            "candidate proof available",
        ):
            raise ValueError("candidate proof must be available")
        candidate_proof_budget = _require_builtin_int(
            self.candidate_proof_budget,
            "candidate proof budget",
            minimum=1,
        )
        if candidate_proof_budget != candidate_budget_v1(
            contract_snapshot.top_k
        ):
            raise ValueError(
                "candidate proof budget must match the contract top_k"
            )
        object.__setattr__(
            self,
            "evidence_digest",
            process_evidence_digest(self),
        )

    @property
    def final_evidence(self) -> bool:
        """Test-mode evidence is never final Task 8.5 evidence."""
        return not self.test_mode

    def recompute_environment_digest(self) -> str:
        """Independently recompute the strict environment digest."""
        return benchmark_environment_digest(self.environment)

    def recompute_evidence_digest(self) -> str:
        """Independently recompute the canonical evidence digest."""
        return process_evidence_digest(self)


def process_evidence_digest(evidence: TMBenchmarkProcessEvidence) -> str:
    """Canonical digest over every evidence fact except the digest itself."""
    if type(evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("evidence must be TMBenchmarkProcessEvidence")
    payload: dict[str, object] = {
        "actual_index_kind": evidence.actual_index_kind,
        "candidate_proof_available": evidence.candidate_proof_available,
        "candidate_proof_budget": evidence.candidate_proof_budget,
        "candidate_proof_count": evidence.candidate_proof_count,
        "candidate_proof_index_kind": evidence.candidate_proof_index_kind,
        "canonical_store_id": evidence.canonical_store_id,
        "child_exit_code": evidence.child_exit_code,
        "child_pid": evidence.child_pid,
        "contract_digest": evidence.contract_digest,
        "corpus_digest": evidence.corpus_digest,
        "corpus_record_count": evidence.corpus_record_count,
        "environment_digest": evidence.environment_digest,
        "exact_proof_result_count": evidence.exact_proof_result_count,
        "exact_proof_winner_matched": evidence.exact_proof_winner_matched,
        "execution_path": evidence.execution_path.value,
        "fixture_digest": evidence.fixture_digest,
        "fixture_path": evidence.fixture_path,
        "fixture_record_count": evidence.fixture_record_count,
        "generation": evidence.generation,
        "migration_elapsed_ns": evidence.migration_elapsed_ns,
        "path_config_digest": evidence.path_config_digest,
        "peak_rss_bytes": evidence.peak_rss_bytes,
        "record_count": evidence.record_count,
        "reopen_action": evidence.reopen_action,
        "reopen_health_exact_available": evidence.reopen_health_exact_available,
        "reopen_health_healthy": evidence.reopen_health_healthy,
        "reopen_health_index_kind": evidence.reopen_health_index_kind,
        "reopen_health_record_count": evidence.reopen_health_record_count,
        "reopen_phase": evidence.reopen_phase,
        "resource_id": evidence.resource_id,
        "rss_scope": evidence.rss_scope,
        "rss_start_bytes": evidence.rss_start_bytes,
        "rss_terminal_bytes": evidence.rss_terminal_bytes,
        "rss_unit": evidence.rss_unit,
        "run_root": evidence.run_root,
        "schema_version": evidence.schema_version,
        "test_mode": evidence.test_mode,
        "worker_protocol_digest": evidence.worker_protocol_digest,
    }
    return benchmark_digest(
        PROCESS_EVIDENCE_DIGEST_VERSION,
        "process-evidence",
        [payload],
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


_EVIDENCE_PAYLOAD_FIELDS = frozenset(
    {
        "actual_index_kind",
        "candidate_proof_available",
        "candidate_proof_budget",
        "candidate_proof_count",
        "candidate_proof_index_kind",
        "canonical_store_id",
        "child_exit_code",
        "child_pid",
        "contract_digest",
        "contract_json",
        "corpus_digest",
        "corpus_record_count",
        "environment",
        "environment_digest",
        "evidence_digest",
        "exact_proof_result_count",
        "exact_proof_winner_matched",
        "execution_path",
        "fixture_digest",
        "fixture_path",
        "fixture_record_count",
        "generation",
        "migration_elapsed_ns",
        "path_config_digest",
        "peak_rss_bytes",
        "record_count",
        "reopen_action",
        "reopen_health_exact_available",
        "reopen_health_healthy",
        "reopen_health_index_kind",
        "reopen_health_record_count",
        "reopen_phase",
        "resource_id",
        "rss_scope",
        "rss_start_bytes",
        "rss_terminal_bytes",
        "rss_unit",
        "run_root",
        "schema_version",
        "test_mode",
        "worker_protocol_digest",
    }
)


def _evidence_payload(evidence: TMBenchmarkProcessEvidence) -> dict[str, object]:
    return {
        "actual_index_kind": evidence.actual_index_kind,
        "candidate_proof_available": evidence.candidate_proof_available,
        "candidate_proof_budget": evidence.candidate_proof_budget,
        "candidate_proof_count": evidence.candidate_proof_count,
        "candidate_proof_index_kind": evidence.candidate_proof_index_kind,
        "canonical_store_id": evidence.canonical_store_id,
        "child_exit_code": evidence.child_exit_code,
        "child_pid": evidence.child_pid,
        "contract_digest": evidence.contract_digest,
        "contract_json": contract_to_json(evidence.contract),
        "corpus_digest": evidence.corpus_digest,
        "corpus_record_count": evidence.corpus_record_count,
        "environment": _environment_payload(evidence.environment),
        "environment_digest": evidence.environment_digest,
        "evidence_digest": evidence.evidence_digest,
        "exact_proof_result_count": evidence.exact_proof_result_count,
        "exact_proof_winner_matched": evidence.exact_proof_winner_matched,
        "execution_path": evidence.execution_path.value,
        "fixture_digest": evidence.fixture_digest,
        "fixture_path": evidence.fixture_path,
        "fixture_record_count": evidence.fixture_record_count,
        "generation": evidence.generation,
        "migration_elapsed_ns": evidence.migration_elapsed_ns,
        "path_config_digest": evidence.path_config_digest,
        "peak_rss_bytes": evidence.peak_rss_bytes,
        "record_count": evidence.record_count,
        "reopen_action": evidence.reopen_action,
        "reopen_health_exact_available": evidence.reopen_health_exact_available,
        "reopen_health_healthy": evidence.reopen_health_healthy,
        "reopen_health_index_kind": evidence.reopen_health_index_kind,
        "reopen_health_record_count": evidence.reopen_health_record_count,
        "reopen_phase": evidence.reopen_phase,
        "resource_id": evidence.resource_id,
        "rss_scope": evidence.rss_scope,
        "rss_start_bytes": evidence.rss_start_bytes,
        "rss_terminal_bytes": evidence.rss_terminal_bytes,
        "rss_unit": evidence.rss_unit,
        "run_root": evidence.run_root,
        "schema_version": evidence.schema_version,
        "test_mode": evidence.test_mode,
        "worker_protocol_digest": evidence.worker_protocol_digest,
    }


def evidence_from_payload(
    payload: Mapping[str, object],
) -> TMBenchmarkProcessEvidence:
    """Strictly reconstruct self-validating evidence from a child payload."""
    fields = _strict_fields(payload, _EVIDENCE_PAYLOAD_FIELDS, "evidence payload")
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
    return TMBenchmarkProcessEvidence(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        test_mode=_as_bool(fields["test_mode"], "test mode"),
        contract=contract,
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
        fixture_path=_as_str(fields["fixture_path"], "fixture path"),
        fixture_record_count=_as_int(
            fields["fixture_record_count"],
            "fixture record count",
            minimum=1,
        ),
        run_root=_as_str(fields["run_root"], "run root"),
        resource_id=_as_str(fields["resource_id"], "resource id"),
        canonical_store_id=_as_str(
            fields["canonical_store_id"],
            "canonical store id",
        ),
        execution_path=execution_path,
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
        environment=_environment_from_payload(fields["environment"]),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
        worker_protocol_digest=_as_digest(
            fields["worker_protocol_digest"],
            "worker protocol digest",
        ),
        child_pid=_as_int(fields["child_pid"], "child pid", minimum=1),
        child_exit_code=_as_int(
            fields["child_exit_code"],
            "child exit code",
            minimum=0,
        ),
        reopen_phase=_as_str(fields["reopen_phase"], "reopen phase"),
        reopen_action=_as_str(fields["reopen_action"], "reopen action"),
        reopen_health_healthy=_as_bool(
            fields["reopen_health_healthy"],
            "reopen health healthy",
        ),
        reopen_health_index_kind=_as_str(
            fields["reopen_health_index_kind"],
            "reopen health index kind",
        ),
        reopen_health_record_count=_as_int(
            fields["reopen_health_record_count"],
            "reopen health record count",
            minimum=1,
        ),
        reopen_health_exact_available=_as_bool(
            fields["reopen_health_exact_available"],
            "reopen health exact available",
        ),
        exact_proof_result_count=_as_int(
            fields["exact_proof_result_count"],
            "exact proof result count",
            minimum=1,
        ),
        exact_proof_winner_matched=_as_bool(
            fields["exact_proof_winner_matched"],
            "exact proof winner matched",
        ),
        candidate_proof_index_kind=_as_str(
            fields["candidate_proof_index_kind"],
            "candidate proof index kind",
        ),
        candidate_proof_count=_as_int(
            fields["candidate_proof_count"],
            "candidate proof count",
            minimum=1,
        ),
        candidate_proof_available=_as_bool(
            fields["candidate_proof_available"],
            "candidate proof available",
        ),
        candidate_proof_budget=_as_int(
            fields["candidate_proof_budget"],
            "candidate proof budget",
            minimum=1,
        ),
    )


def _evidence_from_stdout(stdout: str) -> TMBenchmarkProcessEvidence:
    if type(stdout) is not str:
        raise TypeError("child stdout must be text")
    text = stdout.strip()
    if not text:
        raise ValueError("child produced no evidence payload")
    payload = _parse_strict_json(text)
    return evidence_from_payload(payload)


class ProcessEvidenceError(RuntimeError):
    """Code-only parent-side process evidence failure; never leaks bodies."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("error code must be a non-empty string")
        self.error_code = error_code
        super().__init__(error_code)


class _WorkerError(RuntimeError):
    """Code-only failure inside the isolated worker."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("error code must be a non-empty string")
        self.error_code = error_code
        super().__init__(error_code)


def _rss_platform_facts() -> tuple[str, str]:
    """Return (platform name, raw ru_maxrss unit) or fail closed."""
    if sys.platform.startswith("linux"):
        return "linux", "kib"
    if sys.platform == "darwin":
        return "darwin", "bytes"
    raise _WorkerError("PROCESS.RSS_UNSUPPORTED_PLATFORM")


def _rss_bytes(usage: resource.struct_rusage, raw_unit: str) -> int:
    value = usage.ru_maxrss
    if type(value) is not int or isinstance(value, bool):
        raise _WorkerError("PROCESS.RSS_INVALID")
    if raw_unit == "kib":
        return value * 1024
    if raw_unit == "bytes":
        return value
    raise _WorkerError("PROCESS.RSS_INVALID")


def _read_worker_request(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes:
        raise _WorkerError("PROCESS.REQUEST_INVALID")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _WorkerError("PROCESS.REQUEST_INVALID") from error
    try:
        return _parse_strict_json(text)
    except ValueError as error:
        raise _WorkerError("PROCESS.REQUEST_INVALID") from error


_WORKER_REQUEST_FIELDS = frozenset(
    {
        "canonical_store_id",
        "contract_digest",
        "contract_json",
        "corpus_digest",
        "corpus_record_count",
        "execution_path",
        "fixture_digest",
        "fixture_path",
        "fixture_record_count",
        "protocol",
        "protocol_digest",
        "resource_id",
        "run_root",
        "test_mode",
    }
)


@dataclass(frozen=True)
class _WorkerRequest:
    contract: BenchmarkContract
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    fixture_digest: str
    fixture_path: str
    fixture_record_count: int
    run_root: str
    execution_path: BenchmarkExecutionPath
    resource_id: str
    canonical_store_id: str
    test_mode: bool
    protocol_digest: str


def _validate_worker_request(
    payload: Mapping[str, object],
) -> _WorkerRequest:
    fields = _strict_fields(
        payload,
        _WORKER_REQUEST_FIELDS,
        "worker request",
    )
    if fields["protocol"] != PROCESS_WORKER_PROTOCOL_VERSION:
        raise _WorkerError("PROCESS.PROTOCOL_MISMATCH")
    contract_digest = _require_digest(
        fields["contract_digest"],
        "contract digest",
    )
    corpus_digest = _require_digest(fields["corpus_digest"], "corpus digest")
    fixture_digest = _require_digest(
        fields["fixture_digest"],
        "fixture digest",
    )
    corpus_record_count = _require_builtin_int(
        fields["corpus_record_count"],
        "corpus record count",
        minimum=1,
    )
    fixture_record_count = _require_builtin_int(
        fields["fixture_record_count"],
        "fixture record count",
        minimum=1,
    )
    if corpus_record_count != fixture_record_count:
        raise _WorkerError("PROCESS.COUNT_MISMATCH")
    contract_json = _require_identity(fields["contract_json"], "contract json")
    parsed_contract_json = _parse_strict_json(contract_json)
    contract = contract_from_json(_canonical_json(parsed_contract_json))
    if type(contract) is not BenchmarkContract:
        raise _WorkerError("PROCESS.CONTRACT_INVALID")
    if benchmark_contract_digest(contract) != contract_digest:
        raise _WorkerError("PROCESS.CONTRACT_DIGEST_MISMATCH")
    fixture_path = _require_absolute_path_string(
        fields["fixture_path"],
        "fixture path",
    )
    run_root = _require_absolute_path_string(fields["run_root"], "run root")
    if os.path.dirname(fixture_path) != run_root:
        raise _WorkerError("PROCESS.FIXTURE_PATH_INVALID")
    if not os.path.isdir(run_root):
        raise _WorkerError("PROCESS.RUN_ROOT_INVALID")
    resource_id = _require_identity(fields["resource_id"], "resource id")
    canonical_store_id = _require_identity(
        fields["canonical_store_id"],
        "canonical store id",
    )
    test_mode = _require_builtin_bool(fields["test_mode"], "test mode")
    if not test_mode:
        if corpus_digest != contract.corpus_digest:
            raise _WorkerError("PROCESS.CORPUS_DIGEST_MISMATCH")
        if corpus_record_count != contract.corpus_record_count:
            raise _WorkerError("PROCESS.COUNT_MISMATCH")
    execution_path_text = _require_identity(
        fields["execution_path"],
        "execution path",
    )
    try:
        execution_path = BenchmarkExecutionPath(execution_path_text)
    except ValueError as error:
        raise _WorkerError("PROCESS.PATH_INVALID") from error
    expected_protocol_digest = worker_protocol_digest(
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_path=fixture_path,
        fixture_record_count=fixture_record_count,
        run_root=run_root,
        execution_path=execution_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        test_mode=test_mode,
    )
    caller_protocol_digest = _require_digest(
        fields["protocol_digest"],
        "protocol digest",
    )
    if caller_protocol_digest != expected_protocol_digest:
        raise _WorkerError("PROCESS.PROTOCOL_DIGEST_MISMATCH")
    try:
        entries = tuple(
            os.path.join(run_root, name) for name in os.listdir(run_root)
        )
    except OSError as error:
        raise _WorkerError("PROCESS.RUN_ROOT_INVALID") from error
    if entries != (fixture_path,):
        raise _WorkerError("PROCESS.RUN_ROOT_NOT_CLOSED")
    try:
        _require_single_link_regular_file(Path(fixture_path), "fixture")
    except (TypeError, ValueError) as error:
        raise _WorkerError("PROCESS.FIXTURE_INVALID") from error
    return _WorkerRequest(
        contract=contract,
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_path=fixture_path,
        fixture_record_count=fixture_record_count,
        run_root=run_root,
        execution_path=execution_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        test_mode=test_mode,
        protocol_digest=caller_protocol_digest,
    )


def _request_payload(
    *,
    contract: BenchmarkContract,
    contract_digest: str,
    corpus_digest: str,
    corpus_record_count: int,
    fixture_digest: str,
    fixture_path: str,
    fixture_record_count: int,
    run_root: str,
    execution_path: BenchmarkExecutionPath,
    resource_id: str,
    canonical_store_id: str,
    test_mode: bool,
) -> dict[str, object]:
    protocol_digest = worker_protocol_digest(
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_path=fixture_path,
        fixture_record_count=fixture_record_count,
        run_root=run_root,
        execution_path=execution_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        test_mode=test_mode,
    )
    return {
        "canonical_store_id": canonical_store_id,
        "contract_digest": contract_digest,
        "contract_json": contract_to_json(contract),
        "corpus_digest": corpus_digest,
        "corpus_record_count": corpus_record_count,
        "execution_path": execution_path.value,
        "fixture_digest": fixture_digest,
        "fixture_path": fixture_path,
        "fixture_record_count": fixture_record_count,
        "protocol": PROCESS_WORKER_PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "resource_id": resource_id,
        "run_root": run_root,
        "test_mode": test_mode,
    }


@dataclass(frozen=True)
class _MeasuredFacts:
    actual_index_kind: str
    candidate_proof_available: bool
    candidate_proof_budget: int
    candidate_proof_count: int
    candidate_proof_index_kind: str
    canonical_store_id: str
    child_exit_code: int
    child_pid: int
    contract: BenchmarkContract
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    exact_proof_result_count: int
    exact_proof_winner_matched: bool
    execution_path: BenchmarkExecutionPath
    fixture_digest: str
    fixture_path: str
    fixture_record_count: int
    generation: int
    migration_elapsed_ns: int
    path_config_digest: str
    peak_rss_bytes: int
    record_count: int
    reopen_action: str
    reopen_health_exact_available: bool
    reopen_health_healthy: bool
    reopen_health_index_kind: str
    reopen_health_record_count: int
    reopen_phase: str
    resource_id: str
    rss_scope: str
    rss_start_bytes: int
    rss_terminal_bytes: int
    rss_unit: str
    run_root: str
    schema_version: str
    test_mode: bool
    worker_protocol_digest: str


def _run_measured_lifecycle(
    request: _WorkerRequest,
    *,
    started_ns: int,
    start_usage: resource.struct_rusage,
) -> _MeasuredFacts:
    contract = request.contract
    execution_path = request.execution_path
    fixture_path = Path(request.fixture_path)
    run_root = Path(request.run_root)
    resource_id = request.resource_id
    canonical_store_id = request.canonical_store_id

    try:
        _fixture_digest, fixture_count, first_source = _read_fixture_facts(
            fixture_path,
            expected_digest=request.fixture_digest,
            expected_count=request.fixture_record_count,
        )
    except (OSError, ValueError) as error:
        raise _WorkerError("PROCESS.FIXTURE_INVALID") from error
    expected_index_kind = (
        "FTS5_TRIGRAM"
        if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else "GRAM_FALLBACK"
    )
    fts5_enabled = False
    if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM:
        if not _probe_fts5():
            raise _WorkerError("PROCESS.FTS5_UNAVAILABLE")
        fts5_enabled = True

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
    force_fallback = (
        execution_path is BenchmarkExecutionPath.GRAM_FALLBACK
    )
    with ExitStack() as stack:
        if force_fallback:
            stack.enter_context(
                patch("tm_sqlite_store._probe_fts5", return_value=False)
            )
        build = service.build_mutable_stage(fixture_path)
        stage = build.mutable_stage
        if stage is None:
            raise _WorkerError("PROCESS.STAGE_UNREUSED")
        if build.reused_completed_revision is not None:
            raise _WorkerError("PROCESS.STAGE_REUSED")
        if build.preflight.valid_count != fixture_count:
            raise _WorkerError("PROCESS.COUNT_MISMATCH")
        if build.preflight.invalid_count != 0:
            raise _WorkerError("PROCESS.FIXTURE_INVALID")
        sealed = StageSealer(
            registry=coordinator.sealed_registry,
            canonical_store_id=canonical_store_id,
        ).seal(stage, expected_prior_generation=None)
        seal_evidence = sealed.evidence
        if not seal_evidence.integrity_ok:
            raise _WorkerError("PROCESS.GATE_B_FAILED")
        if seal_evidence.record_count != fixture_count:
            raise _WorkerError("PROCESS.COUNT_MISMATCH")
        prepared = coordinator.activate(sealed)
        journal = coordinator.publish_prepared_activation(prepared)
        coordinator.publish_activation(prepared, journal)
        generation = coordinator.current_generation
        if generation is None:
            raise _WorkerError("PROCESS.ACTIVATION_FAILED")
        fresh = ResourceStoreCoordinator(
            canonical_store_id=canonical_store_id,
            resource_identity=identity,
        )
        report = fresh.rehydrate_runtime_authority()
        if report is None:
            raise _WorkerError("PROCESS.REOPEN_FAILED")
        if (
            report.action != "COMPLETED"
            or report.phase != "GENERATION_PUBLISHED"
        ):
            raise _WorkerError("PROCESS.REOPEN_FAILED")
        if report.generation != generation:
            raise _WorkerError("PROCESS.REOPEN_FAILED")
        store = SQLiteTMStore.from_coordinator(fresh)
        health = store.health()
        if not health.healthy:
            raise _WorkerError("PROCESS.HEALTH_FAILED")
        if health.index_kind != expected_index_kind:
            raise _WorkerError("PROCESS.INDEX_KIND_MISMATCH")
        if health.record_count != fixture_count:
            raise _WorkerError("PROCESS.COUNT_MISMATCH")
        if not health.exact_available:
            raise _WorkerError("PROCESS.HEALTH_FAILED")
        exact = store.exact_records(first_source)
        if not exact:
            raise _WorkerError("PROCESS.EXACT_PROOF_FAILED")
        if exact[0].source_raw != first_source:
            raise _WorkerError("PROCESS.EXACT_PROOF_FAILED")
        folded_query = fold_text_v1(first_source).folded_text
        candidate_report = CandidateRetriever().candidates(
            resource_id,
            store,
            folded_query,
            result_limit=contract.top_k,
        )
        metadata = candidate_report.metadata
        if metadata.index_kind != expected_index_kind:
            raise _WorkerError("PROCESS.INDEX_KIND_MISMATCH")
        if not candidate_report.candidates:
            raise _WorkerError("PROCESS.CANDIDATE_PROOF_FAILED")
        if not metadata.fuzzy_available:
            raise _WorkerError("PROCESS.CANDIDATE_PROOF_FAILED")
        captured_health = health
        captured_metadata = metadata
        captured_exact = exact
        captured_candidate_report = candidate_report
        captured_generation = generation
        captured_report = report

    terminal_ns = time.perf_counter_ns()
    terminal_usage = resource.getrusage(resource.RUSAGE_SELF)
    elapsed_ns = terminal_ns - started_ns
    if elapsed_ns < 0:
        raise _WorkerError("PROCESS.ELAPSED_INVALID")
    platform_name, raw_unit = _rss_platform_facts()
    start_rss = _rss_bytes(start_usage, raw_unit)
    terminal_rss = _rss_bytes(terminal_usage, raw_unit)
    if start_rss > terminal_rss:
        raise _WorkerError("PROCESS.RSS_MONOTONIC_VIOLATION")

    environment = collect_process_environment(
        fts5_enabled=fts5_enabled,
        rss_raw_unit=raw_unit,
        rss_platform=platform_name,
        rss_scope=contract.rss_scope,
    )
    environment_digest = benchmark_environment_digest(environment)
    worker_protocol = worker_protocol_digest(
        contract_digest=request.contract_digest,
        corpus_digest=request.corpus_digest,
        corpus_record_count=request.corpus_record_count,
        fixture_digest=request.fixture_digest,
        fixture_path=request.fixture_path,
        fixture_record_count=request.fixture_record_count,
        run_root=request.run_root,
        execution_path=execution_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        test_mode=request.test_mode,
    )
    if worker_protocol != request.protocol_digest:
        raise _WorkerError("PROCESS.PROTOCOL_DIGEST_MISMATCH")
    return _MeasuredFacts(
        actual_index_kind=captured_health.index_kind,
        candidate_proof_available=captured_metadata.fuzzy_available,
        candidate_proof_budget=captured_metadata.candidate_budget,
        candidate_proof_count=len(captured_candidate_report.candidates),
        candidate_proof_index_kind=captured_metadata.index_kind,
        canonical_store_id=canonical_store_id,
        child_exit_code=0,
        child_pid=os.getpid(),
        contract=contract,
        contract_digest=request.contract_digest,
        corpus_digest=request.corpus_digest,
        corpus_record_count=request.corpus_record_count,
        environment=environment,
        environment_digest=environment_digest,
        exact_proof_result_count=len(captured_exact),
        exact_proof_winner_matched=(
            captured_exact[0].source_raw == first_source
        ),
        execution_path=execution_path,
        fixture_digest=request.fixture_digest,
        fixture_path=request.fixture_path,
        fixture_record_count=request.fixture_record_count,
        generation=captured_generation,
        migration_elapsed_ns=elapsed_ns,
        path_config_digest=(
            contract.fast_path_config_digest
            if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else contract.fallback_path_config_digest
        ),
        peak_rss_bytes=terminal_rss,
        record_count=captured_health.record_count,
        reopen_action=captured_report.action,
        reopen_health_exact_available=captured_health.exact_available,
        reopen_health_healthy=captured_health.healthy,
        reopen_health_index_kind=captured_health.index_kind,
        reopen_health_record_count=captured_health.record_count,
        reopen_phase=captured_report.phase,
        resource_id=resource_id,
        rss_scope=contract.rss_scope,
        rss_start_bytes=start_rss,
        rss_terminal_bytes=terminal_rss,
        rss_unit=WORKER_RSS_UNIT,
        run_root=request.run_root,
        schema_version=PROCESS_EVIDENCE_SCHEMA_VERSION,
        test_mode=request.test_mode,
        worker_protocol_digest=worker_protocol,
    )


def _evidence_from_facts(facts: _MeasuredFacts) -> TMBenchmarkProcessEvidence:
    return TMBenchmarkProcessEvidence(
        schema_version=facts.schema_version,
        test_mode=facts.test_mode,
        contract=facts.contract,
        contract_digest=facts.contract_digest,
        corpus_digest=facts.corpus_digest,
        corpus_record_count=facts.corpus_record_count,
        fixture_digest=facts.fixture_digest,
        fixture_path=facts.fixture_path,
        fixture_record_count=facts.fixture_record_count,
        run_root=facts.run_root,
        resource_id=facts.resource_id,
        canonical_store_id=facts.canonical_store_id,
        execution_path=facts.execution_path,
        path_config_digest=facts.path_config_digest,
        actual_index_kind=facts.actual_index_kind,
        record_count=facts.record_count,
        generation=facts.generation,
        migration_elapsed_ns=facts.migration_elapsed_ns,
        peak_rss_bytes=facts.peak_rss_bytes,
        rss_start_bytes=facts.rss_start_bytes,
        rss_terminal_bytes=facts.rss_terminal_bytes,
        rss_unit=facts.rss_unit,
        rss_scope=facts.rss_scope,
        environment=facts.environment,
        environment_digest=facts.environment_digest,
        worker_protocol_digest=facts.worker_protocol_digest,
        child_pid=facts.child_pid,
        child_exit_code=facts.child_exit_code,
        reopen_phase=facts.reopen_phase,
        reopen_action=facts.reopen_action,
        reopen_health_healthy=facts.reopen_health_healthy,
        reopen_health_index_kind=facts.reopen_health_index_kind,
        reopen_health_record_count=facts.reopen_health_record_count,
        reopen_health_exact_available=facts.reopen_health_exact_available,
        exact_proof_result_count=facts.exact_proof_result_count,
        exact_proof_winner_matched=facts.exact_proof_winner_matched,
        candidate_proof_index_kind=facts.candidate_proof_index_kind,
        candidate_proof_count=facts.candidate_proof_count,
        candidate_proof_available=facts.candidate_proof_available,
        candidate_proof_budget=facts.candidate_proof_budget,
    )


def _worker_main(argv: list[str]) -> int:
    if argv != [WORKER_MODE_FLAG]:
        sys.stderr.write(
            "usage: python -m tm_benchmark_process --worker\n"
        )
        return 2
    started_ns = time.perf_counter_ns()
    start_usage = resource.getrusage(resource.RUSAGE_SELF)
    try:
        raw_request = sys.stdin.buffer.read()
        payload = _read_worker_request(raw_request)
        request = _validate_worker_request(payload)
        facts = _run_measured_lifecycle(
            request,
            started_ns=started_ns,
            start_usage=start_usage,
        )
        evidence = _evidence_from_facts(facts)
        payload_out = _evidence_payload(evidence)
        sys.stdout.write(_canonical_json(payload_out) + "\n")
        sys.stdout.flush()
        return 0
    except _WorkerError as error:
        sys.stderr.write(
            _canonical_json({"error_code": error.error_code}) + "\n"
        )
        sys.stderr.flush()
        return 1
    except Exception:
        sys.stderr.write(
            _canonical_json({"error_code": "PROCESS.CHILD_FAILED"}) + "\n"
        )
        sys.stderr.flush()
        return 1


def _child_stderr_code(stderr: str) -> str:
    if type(stderr) is not str or not stderr.strip():
        return "PROCESS.CHILD_CRASH"
    try:
        payload = _parse_strict_json(stderr.strip())
    except ValueError:
        return "PROCESS.CHILD_CRASH"
    error_code = payload.get("error_code")
    if type(error_code) is not str or not error_code:
        return "PROCESS.CHILD_CRASH"
    return error_code


def run_process_migration_evidence(
    *,
    contract_path: Path,
    execution_path: BenchmarkExecutionPath,
    run_root: Path,
    fixture_path: Path | None = None,
    resource_id: str = "tm.benchmark",
    canonical_store_id: str = "store.benchmark",
    test_mode: bool = False,
    test_record_count: int | None = None,
    test_seed: int | None = None,
    timeout_seconds: float = 600.0,
) -> TMBenchmarkProcessEvidence:
    """Parent runner: verify inputs, spawn one isolated child, return evidence.

    Real mode verifies the full Task 8.1 corpus digest/count via
    ``recompute_benchmark_inputs`` and uses the 100,000-record corpus.  Test
    mode uses explicit small test-only counts and never produces final
    evidence.  Fixture generation/writing happens here, before the child is
    spawned, and is excluded from measured child cost.
    """

    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    if type(run_root) is not _NATIVE_PATH_TYPE:
        raise TypeError("run root must be a Path")
    run_root = run_root.resolve()
    if not run_root.is_dir():
        raise ValueError("run root must be an existing directory")
    if fixture_path is not None and type(fixture_path) is not _NATIVE_PATH_TYPE:
        raise TypeError("fixture path must be a Path or None")
    _require_identity(resource_id, "resource id")
    _require_identity(canonical_store_id, "canonical store id")
    if type(test_mode) is not bool:
        raise TypeError("test mode must be a built-in bool")
    if type(timeout_seconds) is not float or not math.isfinite(
        timeout_seconds
    ) or timeout_seconds <= 0:
        raise ValueError("timeout seconds must be a positive finite float")

    contract = load_benchmark_contract(contract_path)
    contract_digest = benchmark_contract_digest(contract)
    if test_mode:
        if test_record_count is None:
            raise ValueError(
                "test mode requires an explicit small test record count"
            )
        test_record_count = _require_builtin_int(
            test_record_count,
            "test record count",
            minimum=1,
        )
        if test_record_count >= REAL_CORPUS_RECORD_COUNT:
            raise ValueError(
                "test record count must be below the real 100000 corpus"
            )
        if test_seed is None:
            test_seed = contract.corpus_seed
        test_seed = _require_builtin_int(test_seed, "test seed", minimum=0)
        records = tuple(
            iter_corpus_records(
                seed=test_seed,
                record_count=test_record_count,
            )
        )
        corpus_digest = benchmark_digest(
            contract.corpus_generator_version,
            "corpus",
            [_record_payload(record) for record in records],
        )
        corpus_record_count = test_record_count
    else:
        recompute_benchmark_inputs(contract_path)
        corpus_digest = contract.corpus_digest
        corpus_record_count = contract.corpus_record_count
        records = iter_corpus_records(
            seed=contract.corpus_seed,
            record_count=contract.corpus_record_count,
        )

    initial_entries = tuple(run_root.iterdir())
    if fixture_path is None:
        if initial_entries:
            raise ValueError("run root must be empty before fixture generation")
        fixture_path = run_root / "fixture.jsonl"
        if fixture_path.exists():
            raise ValueError("fixture path must not pre-exist for generation")
        fixture_digest, fixture_record_count = _generate_fixture(
            fixture_path,
            records,
        )
    else:
        fixture_path = fixture_path.resolve()
        if fixture_path.parent != run_root:
            raise ValueError("fixture path must live directly in the run root")
        if initial_entries != (fixture_path,):
            raise ValueError("provided fixture must be the sole run-root entry")
        _require_single_link_regular_file(fixture_path, "fixture")
        fixture_digest, fixture_record_count = _expected_fixture_facts(records)
        observed_digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        if observed_digest != fixture_digest:
            raise ValueError(
                "provided fixture does not match the requested corpus"
            )
    if fixture_record_count != corpus_record_count:
        raise ValueError("fixture record count must equal corpus count")

    request = _request_payload(
        contract=contract,
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_path=str(fixture_path),
        fixture_record_count=fixture_record_count,
        run_root=str(run_root),
        execution_path=execution_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        test_mode=test_mode,
    )
    request_json = _canonical_json(request)
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONWARNINGS"] = "ignore"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tm_benchmark_process",
                WORKER_MODE_FLAG,
            ],
            input=request_json,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=Path(__file__).resolve().parent,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
    except OSError as error:
        raise ProcessEvidenceError("PROCESS.CHILD_SPAWN_FAILED") from error
    except subprocess.TimeoutExpired as error:
        raise ProcessEvidenceError("PROCESS.CHILD_TIMEOUT") from error
    if completed.returncode != 0:
        raise ProcessEvidenceError(_child_stderr_code(completed.stderr))
    if completed.stderr.strip():
        raise ProcessEvidenceError("PROCESS.CHILD_STDERR_NOISE")
    try:
        evidence = _evidence_from_stdout(completed.stdout)
    except (TypeError, ValueError) as error:
        raise ProcessEvidenceError("PROCESS.EVIDENCE_INVALID") from error
    if evidence.child_pid == os.getpid():
        raise ProcessEvidenceError("PROCESS.CHILD_PID_INVALID")
    if evidence.worker_protocol_digest != request["protocol_digest"]:
        raise ProcessEvidenceError("PROCESS.PROTOCOL_DIGEST_MISMATCH")
    if evidence.contract_digest != contract_digest:
        raise ProcessEvidenceError("PROCESS.CONTRACT_DIGEST_MISMATCH")
    if evidence.fixture_digest != fixture_digest:
        raise ProcessEvidenceError("PROCESS.FIXTURE_DIGEST_MISMATCH")
    if evidence.fixture_record_count != fixture_record_count:
        raise ProcessEvidenceError("PROCESS.COUNT_MISMATCH")
    if not test_mode:
        if evidence.corpus_digest != contract.corpus_digest:
            raise ProcessEvidenceError("PROCESS.CORPUS_DIGEST_MISMATCH")
        if evidence.corpus_record_count != contract.corpus_record_count:
            raise ProcessEvidenceError("PROCESS.COUNT_MISMATCH")
    return evidence


__all__ = [
    "PROCESS_EVIDENCE_DIGEST_VERSION",
    "PROCESS_EVIDENCE_SCHEMA_VERSION",
    "PROCESS_WORKER_PROTOCOL_VERSION",
    "ProcessEvidenceError",
    "TMBenchmarkProcessEvidence",
    "collect_process_environment",
    "evidence_from_payload",
    "process_evidence_digest",
    "run_process_migration_evidence",
    "worker_protocol_digest",
]


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
