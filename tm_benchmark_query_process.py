"""Task 8.5A execution bridge: reopen a Task 8.3 canonical artifact in a
separate query child and measure real exact/fuzzy latency plus query RSS.

Ownership
---------
This module is the benchmark-v1 query-process execution bridge.  It consumes
an exact-type Task 8.3 ``TMBenchmarkProcessEvidence`` (whose migration child
has already exited), reopens the same canonical store artifact in a brand-new
query child, runs the real production exact and ``fold-v1 -> CandidateRetriever
-> records_by_id -> scorer-v1 -> threshold -> stable top-k`` pipeline through
the Task 8.2 latency runner, samples the query child peak RSS, and returns
strict self-validating raw evidence.  It is an offline validation/batch owner
only: no production runtime module imports it, and it never constructs a
``BenchmarkReport``/``BenchmarkSuiteReport``, never publishes Gate D or any
capability, and never writes final evidence artifacts (Task 8.5B owns the
gate combiner and bundle).

Invariant capsule
-----------------
- Process separation: the Task 8.3 migration child remains the sole migration
  elapsed/RSS authority and has exited before this bridge spawns the query
  child.  The query child never re-runs migration; it only rehydrates the
  durable activation authority and reopens the exact canonical generation.
- Artifact authority: before spawn (parent), before reopen (child), and after
  all queries (child) the canonical artifact is re-verified against the
  dedicated run-root namespace, the deterministic sidecar/manifest locators,
  no-follow regular single-link identity, stable dev/inode/size/mtime facts,
  a full SHA-256/identity digest over every retained family entry, and the
  process contract/corpus/fixture/resource/
  store/generation/path/count bindings.  Symlink, multi-link, foreign entry,
  path escape, substitution, drift, and unknown/missing facts fail closed.
  The caller-owned run root and artifact are never cleaned or deleted here.
- Real latency: ``measure_path_latency`` runs exactly once on the process
  evidence execution path with the committed contract/cohorts/warmup/repeats/
  clock.  Exact lookup calls the real canonical exact port; fuzzy executes the
  production retrieval chain on the same reopened generation.  No synthetic
  success callback, no candidate-proof-only shortcut, no caller self-report.
- Evidence: ``QueryProcessEvidence`` is frozen and self-validating; child
  PID and absolute run-root/fixture locators exist only in the local raw
  execution object and child protocol, never in the portable evidence, which
  exposes a stable ``artifact_key`` plus digests and binding facts.  Caller
  booleans/digests never authorize; ``final_evidence`` is derived only from
  literal non-test process+latency facts (the 100000-record corpus) and is
  accepted only through the parent-adjudicated paired run result, never as
  a standalone boolean.  The parent binds every returned fact to the exact
  process evidence and the actual response PID.
- Evidence and diagnostics never persist query, source, or target bodies.
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
import re
import resource
import sqlite3
import stat
import subprocess
import sys
import time
from unittest.mock import patch

from text_matcher import fold_text_v1
from tm_benchmark import benchmark_digest, iter_fuzzy_queries
from tm_benchmark_latency import (
    DEFAULT_TIMING_CLOCK_NAME,
    LatencyEvidence,
    LatencyExecutor,
    collect_benchmark_environment,
    latency_evidence_digest,
    latency_evidence_from_payload,
    latency_evidence_to_payload,
    measure_path_latency,
)
from tm_benchmark_process import (
    ArtifactSnapshot as ProcessArtifactSnapshot,
    PROCESS_ARTIFACT_SNAPSHOT_DIGEST_VERSION,
    REAL_CORPUS_RECORD_COUNT,
    TMBenchmarkProcessEvidence,
    artifact_snapshot_digest,
    artifact_snapshot_to_payload as process_artifact_snapshot_to_payload,
    collect_process_environment,
    evidence_from_payload,
    process_canonical_artifact_paths,
    process_evidence_digest,
    process_evidence_to_payload,
    rss_peak_bytes_facts,
)
from tm_candidate_index import CandidateRetriever
from tm_contracts import (
    BENCHMARK_RSS_SCOPE,
    BenchmarkContract,
    BenchmarkExecutionPath,
    CanonicalResourceIdentity,
    TMQuery,
    benchmark_contract_digest,
    benchmark_environment_digest,
)
from tm_retrieval import score_fuzzy_candidates
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore

QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION = "tm-benchmark-query-process-evidence-v1"
QUERY_PROBE_SCHEMA_VERSION = "tm-benchmark-query-probe-v1"
QUERY_WORKER_PROTOCOL_VERSION = "tm-benchmark-query-worker-v1"
QUERY_EVIDENCE_DIGEST_VERSION = "tm-benchmark-query-digest-v1"
QUERY_PROBE_DIGEST_VERSION = "tm-benchmark-query-probe-digest-v1"
QUERY_ARTIFACT_KEY_VERSION = "tm-benchmark-query-artifact-key-v1"
QUERY_PROCESS_PAIR_VERSION = "tm-benchmark-query-process-pair-v1"
QUERY_WORKER_MODE_FLAG = "--worker"
QUERY_RSS_UNIT = "bytes"
QUERY_PROBE_FUZZY_CALLS = 3

_NATIVE_PATH_TYPE = type(Path())
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
    path = Path(text)
    if not path.is_absolute():
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


def _environment_payload(
    environment: tuple[tuple[str, str], ...],
) -> list[list[str]]:
    return [[key, value] for key, value in environment]


def _environment_from_payload(
    value: object,
) -> tuple[tuple[str, str], ...]:
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


class QueryProcessError(RuntimeError):
    """Code-only query-process failure; never leaks paths or bodies."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("error code must be a non-empty string")
        self.error_code = error_code
        super().__init__(error_code)


class _WorkerError(RuntimeError):
    """Child-side code-only failure with a stable error code."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _child_stderr_code(stderr: str) -> str:
    if type(stderr) is not str or not stderr.strip():
        return "QUERY.CHILD_CRASH"
    try:
        payload = _parse_strict_json(stderr.strip())
    except ValueError:
        return "QUERY.CHILD_CRASH"
    error_code = payload.get("error_code")
    if type(error_code) is not str or not error_code:
        return "QUERY.CHILD_CRASH"
    return error_code


# --- Artifact identity and verification ------------------------------------


@dataclass(frozen=True)
class ArtifactFileIdentity:
    """No-follow stable identity facts of one canonical artifact file."""

    device: int
    inode: int
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        _require_builtin_int(self.device, "artifact device", minimum=0)
        _require_builtin_int(self.inode, "artifact inode", minimum=0)
        _require_builtin_int(self.size, "artifact size", minimum=0)
        _require_builtin_int(
            self.mtime_ns,
            "artifact mtime nanoseconds",
            minimum=0,
        )


@dataclass(frozen=True)
class ArtifactSnapshot:
    """Pre/post artifact-family digest and identity facts for one query run."""

    sidecar_digest: str
    manifest_digest: str
    family_digest: str
    sidecar_identity: ArtifactFileIdentity
    manifest_identity: ArtifactFileIdentity

    def __post_init__(self) -> None:
        _require_digest(self.sidecar_digest, "sidecar digest")
        _require_digest(self.manifest_digest, "manifest digest")
        _require_digest(self.family_digest, "artifact family digest")
        if type(self.sidecar_identity) is not ArtifactFileIdentity:
            raise TypeError("sidecar identity must be ArtifactFileIdentity")
        if type(self.manifest_identity) is not ArtifactFileIdentity:
            raise TypeError("manifest identity must be ArtifactFileIdentity")


def _identity_payload(
    identity: ArtifactFileIdentity,
) -> dict[str, object]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def _identity_from_payload(value: object) -> ArtifactFileIdentity:
    if type(value) is not dict:
        raise TypeError("artifact identity must be a JSON object")
    fields = _strict_fields(
        value,
        frozenset({"device", "inode", "mtime_ns", "size"}),
        "artifact identity payload",
    )
    return ArtifactFileIdentity(
        device=_as_int(fields["device"], "artifact device", minimum=0),
        inode=_as_int(fields["inode"], "artifact inode", minimum=0),
        size=_as_int(fields["size"], "artifact size", minimum=0),
        mtime_ns=_as_int(
            fields["mtime_ns"],
            "artifact mtime nanoseconds",
            minimum=0,
        ),
    )


def artifact_snapshot_to_payload(
    snapshot: ArtifactSnapshot,
) -> dict[str, object]:
    """Strict public payload snapshot of one artifact snapshot."""
    if type(snapshot) is not ArtifactSnapshot:
        raise TypeError("snapshot must be ArtifactSnapshot")
    return {
        "sidecar_digest": snapshot.sidecar_digest,
        "manifest_digest": snapshot.manifest_digest,
        "family_digest": snapshot.family_digest,
        "sidecar_identity": _identity_payload(snapshot.sidecar_identity),
        "manifest_identity": _identity_payload(snapshot.manifest_identity),
    }


def artifact_snapshot_from_payload(
    value: object,
) -> ArtifactSnapshot:
    """Strictly reconstruct one artifact snapshot from a payload."""
    if type(value) is not dict:
        raise TypeError("artifact snapshot must be a JSON object")
    fields = _strict_fields(
        value,
        frozenset(
            {
                "manifest_digest",
                "manifest_identity",
                "family_digest",
                "sidecar_digest",
                "sidecar_identity",
            }
        ),
        "artifact snapshot payload",
    )
    return ArtifactSnapshot(
        sidecar_digest=_as_digest(fields["sidecar_digest"], "sidecar digest"),
        manifest_digest=_as_digest(
            fields["manifest_digest"],
            "manifest digest",
        ),
        family_digest=_as_digest(
            fields["family_digest"],
            "artifact family digest",
        ),
        sidecar_identity=_identity_from_payload(fields["sidecar_identity"]),
        manifest_identity=_identity_from_payload(fields["manifest_identity"]),
    )


def _expected_run_root_entry_names(
    *,
    fixture_path: Path,
    sidecar_path: Path,
    manifest_path: Path,
) -> frozenset[str]:
    sidecar_name = sidecar_path.name
    return frozenset(
        {
            fixture_path.name,
            sidecar_name,
            manifest_path.name,
            f".{sidecar_name}.localcat-activation-journal.json",
            f".{sidecar_name}.localcat-activation-terminal.json",
            f".{sidecar_name}.localcat-activated-lineage.json",
        }
    )


def verify_canonical_artifact(
    *,
    run_root: Path,
    fixture_path: Path,
    resource_id: str,
    expected_fixture_digest: str,
) -> ArtifactSnapshot:
    """Verify the dedicated-root canonical artifact set, returning a snapshot.

    Checks the dedicated-run-root namespace, deterministic sidecar/manifest
    locators, no-follow regular single-link identity for every entry,
    including optional journal/terminal/lineage siblings, and a deterministic
    SHA-256/identity digest over the complete retained family. Direct
    sidecar/manifest digest and identity facts remain available for portable
    projection and diagnostics.
    Symlinks, hard links, directories, foreign entries, path escape, missing
    facts, or a fixture digest drift fail closed.  The caller-owned run root
    and artifact are never cleaned or deleted.
    """
    if type(run_root) is not _NATIVE_PATH_TYPE:
        raise TypeError("run root must be a Path")
    if type(fixture_path) is not _NATIVE_PATH_TYPE:
        raise TypeError("fixture path must be a Path")
    _require_identity(resource_id, "resource id")
    _require_digest(expected_fixture_digest, "expected fixture digest")
    run_root = run_root.resolve()
    if not run_root.is_absolute() or not run_root.is_dir():
        raise ValueError("run root must be an existing absolute directory")
    fixture_path = fixture_path.resolve()
    if fixture_path.parent != run_root:
        raise ValueError("fixture path must live directly in the run root")
    fixture, sidecar_path, manifest_path = process_canonical_artifact_paths(
        resource_id=resource_id,
        fixture_path=str(fixture_path),
    )
    if fixture != fixture_path:
        raise ValueError("fixture path is not deterministic for the resource")
    for path, label in (
        (sidecar_path, "canonical sidecar"),
        (manifest_path, "snapshot manifest"),
        (fixture_path, "fixture"),
    ):
        if path.parent != run_root:
            raise ValueError(f"{label} must live directly in the run root")
        if path.resolve() != path:
            raise ValueError(f"{label} must not escape the run root")

    try:
        entries = sorted(run_root.iterdir())
    except OSError as error:
        raise ValueError("run root cannot be inspected") from error
    expected_names = _expected_run_root_entry_names(
        fixture_path=fixture_path,
        sidecar_path=sidecar_path,
        manifest_path=manifest_path,
    )
    entry_names = [path.name for path in entries]
    unknown = sorted(set(entry_names) - set(expected_names))
    if unknown:
        raise ValueError(
            "run root contains foreign entries "
            f"{unknown!r}"
        )
    missing = sorted(expected_names - set(entry_names))
    for required in (fixture_path.name, sidecar_path.name, manifest_path.name):
        if required not in entry_names:
            raise ValueError(f"run root is missing required artifact {required!r}")
    sqlite_entries = [
        path for path in entries if path.name.endswith(".sqlite3")
    ]
    if sqlite_entries != [sidecar_path]:
        raise ValueError("canonical sidecar must be the only sqlite entry")

    family_payload: list[dict[str, object]] = []
    family_proofs: dict[str, tuple[str, os.stat_result]] = {}
    for entry in entries:
        digest, stat_result = _stable_file_proof(entry, "run-root entry")
        family_proofs[entry.name] = (digest, stat_result)
        family_payload.append(
            {
                "digest": digest,
                "identity": {
                    "device": stat_result.st_dev,
                    "inode": stat_result.st_ino,
                    "mtime_ns": stat_result.st_mtime_ns,
                    "size": stat_result.st_size,
                },
                "name": entry.name,
            }
        )

    fixture_digest, _fixture_stat = family_proofs[fixture_path.name]
    if fixture_digest != expected_fixture_digest:
        raise ValueError("fixture digest does not match the run request")
    sidecar_digest, sidecar_stat = family_proofs[sidecar_path.name]
    manifest_digest, manifest_stat = family_proofs[manifest_path.name]
    return ArtifactSnapshot(
        sidecar_digest=sidecar_digest,
        manifest_digest=manifest_digest,
        family_digest=benchmark_digest(
            PROCESS_ARTIFACT_SNAPSHOT_DIGEST_VERSION,
            "process-artifact-family",
            family_payload,
        ),
        sidecar_identity=ArtifactFileIdentity(
            device=sidecar_stat.st_dev,
            inode=sidecar_stat.st_ino,
            size=sidecar_stat.st_size,
            mtime_ns=sidecar_stat.st_mtime_ns,
        ),
        manifest_identity=ArtifactFileIdentity(
            device=manifest_stat.st_dev,
            inode=manifest_stat.st_ino,
            size=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ),
    )


def _require_single_link_regular_file(
    path: Path,
    field_name: str,
) -> os.stat_result:
    if type(path) is not _NATIVE_PATH_TYPE:
        raise TypeError(f"{field_name} must be pathlib.Path")
    try:
        stat_result = path.lstat()
    except OSError as error:
        raise ValueError(f"{field_name} cannot be inspected") from error
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_nlink != 1:
        raise ValueError(
            f"{field_name} must be a regular single-link file"
        )
    return stat_result


def _stable_file_proof(
    path: Path,
    field_name: str,
) -> tuple[str, os.stat_result]:
    """Read one no-follow file while proving its path identity stayed fixed."""
    before = _require_single_link_regular_file(path, field_name)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{field_name} cannot be opened") from error

    def identity(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_nlink,
        )

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or identity(opened) != identity(before)
        ):
            raise ValueError(f"{field_name} changed before no-follow open")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        terminal = os.fstat(descriptor)
        if identity(terminal) != identity(opened):
            raise ValueError(f"{field_name} changed while being read")
    except OSError as error:
        raise ValueError(f"{field_name} cannot be read") from error
    finally:
        os.close(descriptor)
    after = _require_single_link_regular_file(path, field_name)
    if identity(after) != identity(terminal):
        raise ValueError(f"{field_name} path changed after read")
    return digest.hexdigest(), after


def _artifact_snapshots_equal(
    pre: ArtifactSnapshot,
    post: ArtifactSnapshot,
) -> bool:
    return pre == post


def _artifact_snapshot_matches_baseline(
    snapshot: ArtifactSnapshot,
    process_evidence: TMBenchmarkProcessEvidence,
) -> bool:
    """True when a live query snapshot equals the process evidence baseline."""
    if type(snapshot) is not ArtifactSnapshot:
        raise TypeError("snapshot must be ArtifactSnapshot")
    if type(process_evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    baseline = process_evidence.artifact_snapshot
    if type(baseline) is not ProcessArtifactSnapshot:
        raise TypeError("process evidence artifact snapshot is invalid")
    return (
        artifact_snapshot_to_payload(snapshot)
        == process_artifact_snapshot_to_payload(baseline)
    )


# --- Query worker protocol --------------------------------------------------


def query_worker_protocol_digest(
    *,
    mode: str,
    process_evidence_digest_value: str,
    artifact_baseline_digest: str,
    run_root: str,
    fixture_path: str,
    resource_id: str,
    canonical_store_id: str,
    execution_path: BenchmarkExecutionPath,
    contract_digest: str,
    corpus_digest: str,
    corpus_record_count: int,
    fixture_digest: str,
    fixture_record_count: int,
    generation: int,
    record_count: int,
    actual_index_kind: str,
    path_config_digest: str,
) -> str:
    """Canonical digest over every machine-readable query request fact."""
    if type(execution_path) is not BenchmarkExecutionPath:
        raise TypeError("execution path must be BenchmarkExecutionPath")
    payload: dict[str, object] = {
        "actual_index_kind": _require_identity(
            actual_index_kind,
            "actual index kind",
        ),
        "artifact_baseline_digest": _require_digest(
            artifact_baseline_digest,
            "artifact baseline digest",
        ),
        "canonical_store_id": _require_identity(
            canonical_store_id,
            "canonical store id",
        ),
        "contract_digest": _require_digest(
            contract_digest,
            "contract digest",
        ),
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
        "generation": _require_builtin_int(
            generation,
            "generation",
            minimum=0,
        ),
        "mode": _require_identity(mode, "mode"),
        "path_config_digest": _require_digest(
            path_config_digest,
            "path config digest",
        ),
        "process_evidence_digest": _require_digest(
            process_evidence_digest_value,
            "process evidence digest",
        ),
        "record_count": _require_builtin_int(
            record_count,
            "record count",
            minimum=1,
        ),
        "resource_id": _require_identity(resource_id, "resource id"),
        "run_root": _require_absolute_path_string(run_root, "run root"),
    }
    return benchmark_digest(
        QUERY_WORKER_PROTOCOL_VERSION,
        "query-worker-request",
        [payload],
    )


_REQUEST_FIELDS = frozenset(
    {
        "actual_index_kind",
        "artifact_baseline_digest",
        "canonical_store_id",
        "contract_digest",
        "corpus_digest",
        "corpus_record_count",
        "execution_path",
        "fixture_digest",
        "fixture_path",
        "fixture_record_count",
        "generation",
        "mode",
        "path_config_digest",
        "process_evidence",
        "process_evidence_digest",
        "protocol",
        "protocol_digest",
        "record_count",
        "resource_id",
        "run_root",
    }
)


@dataclass(frozen=True)
class _WorkerRequest:
    mode: str
    process_evidence: TMBenchmarkProcessEvidence
    process_evidence_digest: str
    artifact_baseline_digest: str
    run_root: str
    fixture_path: str
    resource_id: str
    canonical_store_id: str
    execution_path: BenchmarkExecutionPath
    contract_digest: str
    corpus_digest: str
    corpus_record_count: int
    fixture_digest: str
    fixture_record_count: int
    generation: int
    record_count: int
    actual_index_kind: str
    path_config_digest: str
    protocol_digest: str


def _read_worker_request(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes:
        raise _WorkerError("QUERY.REQUEST_INVALID")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _WorkerError("QUERY.REQUEST_INVALID") from error
    try:
        return _parse_strict_json(text)
    except ValueError as error:
        raise _WorkerError("QUERY.REQUEST_INVALID") from error


def _validate_worker_request(
    payload: Mapping[str, object],
) -> _WorkerRequest:
    try:
        fields = _strict_fields(
            payload,
            _REQUEST_FIELDS,
            "query worker request",
        )
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.REQUEST_INVALID") from error
    if fields["protocol"] != QUERY_WORKER_PROTOCOL_VERSION:
        raise _WorkerError("QUERY.PROTOCOL_MISMATCH")
    mode = _require_identity(fields["mode"], "mode")
    if mode not in ("probe", "evidence"):
        raise _WorkerError("QUERY.MODE_INVALID")
    process_evidence_value = fields["process_evidence"]
    if type(process_evidence_value) is not dict:
        raise _WorkerError("QUERY.PROCESS_EVIDENCE_INVALID")
    try:
        process_evidence = evidence_from_payload(process_evidence_value)
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.PROCESS_EVIDENCE_INVALID") from error
    process_evidence_digest_value = _require_digest(
        fields["process_evidence_digest"],
        "process evidence digest",
    )
    if process_evidence_digest_value != process_evidence.evidence_digest:
        raise _WorkerError("QUERY.PROCESS_EVIDENCE_DIGEST_MISMATCH")
    if mode == "probe" and not process_evidence.test_mode:
        raise _WorkerError("QUERY.TEST_MODE_MISMATCH")
    if mode == "evidence" and process_evidence.test_mode:
        raise _WorkerError("QUERY.TEST_MODE_MISMATCH")

    run_root = _require_absolute_path_string(fields["run_root"], "run root")
    fixture_path = _require_absolute_path_string(
        fields["fixture_path"],
        "fixture path",
    )
    if os.path.dirname(fixture_path) != run_root:
        raise _WorkerError("QUERY.FIXTURE_PATH_INVALID")
    if not os.path.isdir(run_root):
        raise _WorkerError("QUERY.RUN_ROOT_INVALID")
    resource_id = _require_identity(fields["resource_id"], "resource id")
    canonical_store_id = _require_identity(
        fields["canonical_store_id"],
        "canonical store id",
    )
    try:
        execution_path = BenchmarkExecutionPath(
            _require_identity(fields["execution_path"], "execution path")
        )
    except ValueError as error:
        raise _WorkerError("QUERY.PATH_INVALID") from error
    contract_digest = _require_digest(
        fields["contract_digest"],
        "contract digest",
    )
    corpus_digest = _require_digest(fields["corpus_digest"], "corpus digest")
    fixture_digest = _require_digest(fields["fixture_digest"], "fixture digest")
    path_config_digest = _require_digest(
        fields["path_config_digest"],
        "path config digest",
    )
    actual_index_kind = _require_identity(
        fields["actual_index_kind"],
        "actual index kind",
    )
    artifact_baseline_digest = _require_digest(
        fields["artifact_baseline_digest"],
        "artifact baseline digest",
    )
    expected_index_kind = (
        "FTS5_TRIGRAM"
        if execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else "GRAM_FALLBACK"
    )
    if actual_index_kind != expected_index_kind:
        raise _WorkerError("QUERY.INDEX_KIND_MISMATCH")
    facts: list[tuple[str, object, object]] = [
        (
            "artifact baseline digest",
            artifact_baseline_digest,
            artifact_snapshot_digest(process_evidence.artifact_snapshot),
        ),
        ("run root", run_root, process_evidence.run_root),
        ("fixture path", fixture_path, process_evidence.fixture_path),
        ("resource id", resource_id, process_evidence.resource_id),
        (
            "canonical store id",
            canonical_store_id,
            process_evidence.canonical_store_id,
        ),
        ("execution path", execution_path, process_evidence.execution_path),
        ("contract digest", contract_digest, process_evidence.contract_digest),
        ("corpus digest", corpus_digest, process_evidence.corpus_digest),
        (
            "corpus record count",
            fields["corpus_record_count"],
            process_evidence.corpus_record_count,
        ),
        ("fixture digest", fixture_digest, process_evidence.fixture_digest),
        (
            "fixture record count",
            fields["fixture_record_count"],
            process_evidence.fixture_record_count,
        ),
        ("generation", fields["generation"], process_evidence.generation),
        ("record count", fields["record_count"], process_evidence.record_count),
        (
            "actual index kind",
            actual_index_kind,
            process_evidence.actual_index_kind,
        ),
        (
            "path config digest",
            path_config_digest,
            process_evidence.path_config_digest,
        ),
    ]
    for label, request_fact, evidence_fact in facts:
        if request_fact != evidence_fact:
            raise _WorkerError("QUERY.FACT_DRIFT")
    if type(fields["corpus_record_count"]) is not int:
        raise _WorkerError("QUERY.REQUEST_INVALID")
    if type(fields["fixture_record_count"]) is not int:
        raise _WorkerError("QUERY.REQUEST_INVALID")
    if type(fields["generation"]) is not int:
        raise _WorkerError("QUERY.REQUEST_INVALID")
    if type(fields["record_count"]) is not int:
        raise _WorkerError("QUERY.REQUEST_INVALID")
    corpus_record_count = _as_int(
        fields["corpus_record_count"],
        "corpus record count",
        minimum=1,
    )
    fixture_record_count = _as_int(
        fields["fixture_record_count"],
        "fixture record count",
        minimum=1,
    )
    generation = _as_int(fields["generation"], "generation", minimum=0)
    record_count = _as_int(fields["record_count"], "record count", minimum=1)
    expected_protocol_digest = query_worker_protocol_digest(
        mode=mode,
        process_evidence_digest_value=process_evidence_digest_value,
        artifact_baseline_digest=artifact_baseline_digest,
        run_root=run_root,
        fixture_path=fixture_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        execution_path=execution_path,
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_record_count=fixture_record_count,
        generation=generation,
        record_count=record_count,
        actual_index_kind=actual_index_kind,
        path_config_digest=path_config_digest,
    )
    caller_protocol_digest = _require_digest(
        fields["protocol_digest"],
        "protocol digest",
    )
    if caller_protocol_digest != expected_protocol_digest:
        raise _WorkerError("QUERY.PROTOCOL_DIGEST_MISMATCH")
    return _WorkerRequest(
        mode=mode,
        process_evidence=process_evidence,
        process_evidence_digest=process_evidence_digest_value,
        artifact_baseline_digest=artifact_baseline_digest,
        run_root=run_root,
        fixture_path=fixture_path,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        execution_path=execution_path,
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=corpus_record_count,
        fixture_digest=fixture_digest,
        fixture_record_count=fixture_record_count,
        generation=generation,
        record_count=record_count,
        actual_index_kind=actual_index_kind,
        path_config_digest=path_config_digest,
        protocol_digest=caller_protocol_digest,
    )


def _request_payload(
    *,
    mode: str,
    process_evidence: TMBenchmarkProcessEvidence,
    run_root: str,
    fixture_path: str,
) -> dict[str, object]:
    artifact_baseline_digest = artifact_snapshot_digest(
        process_evidence.artifact_snapshot
    )
    protocol_digest = query_worker_protocol_digest(
        mode=mode,
        process_evidence_digest_value=process_evidence.evidence_digest,
        artifact_baseline_digest=artifact_baseline_digest,
        run_root=run_root,
        fixture_path=fixture_path,
        resource_id=process_evidence.resource_id,
        canonical_store_id=process_evidence.canonical_store_id,
        execution_path=process_evidence.execution_path,
        contract_digest=process_evidence.contract_digest,
        corpus_digest=process_evidence.corpus_digest,
        corpus_record_count=process_evidence.corpus_record_count,
        fixture_digest=process_evidence.fixture_digest,
        fixture_record_count=process_evidence.fixture_record_count,
        generation=process_evidence.generation,
        record_count=process_evidence.record_count,
        actual_index_kind=process_evidence.actual_index_kind,
        path_config_digest=process_evidence.path_config_digest,
    )
    return {
        "actual_index_kind": process_evidence.actual_index_kind,
        "artifact_baseline_digest": artifact_baseline_digest,
        "canonical_store_id": process_evidence.canonical_store_id,
        "contract_digest": process_evidence.contract_digest,
        "corpus_digest": process_evidence.corpus_digest,
        "corpus_record_count": process_evidence.corpus_record_count,
        "execution_path": process_evidence.execution_path.value,
        "fixture_digest": process_evidence.fixture_digest,
        "fixture_path": fixture_path,
        "fixture_record_count": process_evidence.fixture_record_count,
        "generation": process_evidence.generation,
        "mode": mode,
        "path_config_digest": process_evidence.path_config_digest,
        "process_evidence": process_evidence_to_payload(process_evidence),
        "process_evidence_digest": process_evidence.evidence_digest,
        "protocol": QUERY_WORKER_PROTOCOL_VERSION,
        "protocol_digest": protocol_digest,
        "record_count": process_evidence.record_count,
        "resource_id": process_evidence.resource_id,
        "run_root": run_root,
    }


# --- Probe report (test-only real-pipeline facts) ---------------------------


def _fts5_available() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE tm_probe USING fts5(x)")
        finally:
            connection.close()
        return True
    except sqlite3.Error:
        return False


def _first_fixture_source(fixture_path: Path) -> str:
    try:
        with fixture_path.open("rb") as stream:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                payload = _parse_strict_json(line.decode("utf-8"))
                source_raw = payload.get("source")
                if type(source_raw) is not str or not source_raw:
                    raise ValueError("fixture first row source is invalid")
                return source_raw
    except OSError as error:
        raise ValueError("cannot read fixture file") from error
    raise ValueError("fixture is empty")


def _probe_fuzzy_query_count(contract: BenchmarkContract) -> int:
    if contract.fuzzy_cohort_count < QUERY_PROBE_FUZZY_CALLS:
        return contract.fuzzy_cohort_count
    return QUERY_PROBE_FUZZY_CALLS


@dataclass(frozen=True)
class QueryProbeReport:
    """Frozen test-only probe facts proving the real query pipeline ran.

    Never final evidence: the probe is only produced from test-mode process
    evidence and never embeds ``LatencyEvidence`` or claims contract-cohort
    binding.  It still proves process separation, artifact identity/digest
    stability, a real exact call and real fuzzy pipeline calls on the
    reopened generation, and normalized query-child peak RSS.
    """

    schema_version: str
    process_evidence_digest: str
    artifact_baseline_digest: str
    processes_distinct: bool
    process_pair_digest: str
    query_protocol_digest: str
    artifact_pre: ArtifactSnapshot
    artifact_post: ArtifactSnapshot
    reopen_phase: str
    reopen_action: str
    reopen_health_healthy: bool
    reopen_health_index_kind: str
    reopen_health_record_count: int
    generation: int
    actual_index_kind: str
    record_count: int
    exact_calls: int
    exact_actual_path: BenchmarkExecutionPath
    exact_result_count: int
    fuzzy_calls: int
    fuzzy_actual_path: BenchmarkExecutionPath
    fuzzy_result_count: int
    migration_rerun: bool
    query_peak_rss_bytes: int
    query_rss_start_bytes: int
    query_rss_terminal_bytes: int
    query_rss_unit: str
    query_rss_scope: str
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    artifact_unchanged: bool = field(init=False)
    probe_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != QUERY_PROBE_SCHEMA_VERSION:
            raise ValueError(
                "schema version must be " f"{QUERY_PROBE_SCHEMA_VERSION}"
            )
        _require_digest(self.process_evidence_digest, "process evidence digest")
        _require_digest(self.artifact_baseline_digest, "artifact baseline digest")
        _require_digest(self.process_pair_digest, "process pair digest")
        _require_digest(self.query_protocol_digest, "query protocol digest")
        if not _require_builtin_bool(
            self.processes_distinct,
            "processes distinct",
        ):
            raise ValueError("query child must be a distinct process")
        if type(self.artifact_pre) is not ArtifactSnapshot:
            raise TypeError("artifact pre snapshot must be ArtifactSnapshot")
        if type(self.artifact_post) is not ArtifactSnapshot:
            raise TypeError("artifact post snapshot must be ArtifactSnapshot")
        unchanged = _artifact_snapshots_equal(
            self.artifact_pre,
            self.artifact_post,
        )
        object.__setattr__(self, "artifact_unchanged", unchanged)
        if not unchanged:
            raise ValueError("pre/post artifact identity or digest drifted")
        if self.reopen_phase != "GENERATION_PUBLISHED":
            raise ValueError("reopen phase must be GENERATION_PUBLISHED")
        if self.reopen_action != "COMPLETED":
            raise ValueError("reopen action must be COMPLETED")
        if not _require_builtin_bool(
            self.reopen_health_healthy,
            "reopen health healthy",
        ):
            raise ValueError("reopen health must be healthy")
        _require_identity(
            self.reopen_health_index_kind,
            "reopen health index kind",
        )
        _require_identity(self.actual_index_kind, "actual index kind")
        if self.reopen_health_index_kind != self.actual_index_kind:
            raise ValueError(
                "reopen health index kind must equal actual index kind"
            )
        reopen_record_count = _require_builtin_int(
            self.reopen_health_record_count,
            "reopen health record count",
            minimum=1,
        )
        record_count = _require_builtin_int(
            self.record_count,
            "record count",
            minimum=1,
        )
        if reopen_record_count != record_count:
            raise ValueError(
                "reopen health record count must equal record count"
            )
        _require_builtin_int(self.generation, "generation", minimum=0)
        expected_index_kind = (
            "FTS5_TRIGRAM"
            if self.exact_actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else "GRAM_FALLBACK"
        )
        if self.actual_index_kind != expected_index_kind:
            raise ValueError(
                "actual index kind must match the execution path"
            )
        if type(self.exact_actual_path) is not BenchmarkExecutionPath:
            raise TypeError("exact actual path must be BenchmarkExecutionPath")
        if type(self.fuzzy_actual_path) is not BenchmarkExecutionPath:
            raise TypeError("fuzzy actual path must be BenchmarkExecutionPath")
        if self.fuzzy_actual_path is not self.exact_actual_path:
            raise ValueError("exact and fuzzy actual paths must agree")
        exact_calls = _require_builtin_int(
            self.exact_calls,
            "exact calls",
            minimum=1,
        )
        if exact_calls != 1:
            raise ValueError("probe must run exactly one exact call")
        _require_builtin_int(
            self.exact_result_count,
            "exact result count",
            minimum=1,
        )
        fuzzy_calls = _require_builtin_int(
            self.fuzzy_calls,
            "fuzzy calls",
            minimum=1,
        )
        if fuzzy_calls != _probe_fuzzy_query_count_of(self):
            raise ValueError("fuzzy probe call count is invalid")
        _require_builtin_int(
            self.fuzzy_result_count,
            "fuzzy result count",
            minimum=0,
        )
        if self.fuzzy_result_count > self.fuzzy_calls * _PROBE_TOP_K_FACTOR:
            raise ValueError("fuzzy probe result count is implausible")
        if _require_builtin_bool(self.migration_rerun, "migration rerun"):
            raise ValueError("query child must never re-run migration")
        query_peak_rss = _require_builtin_int(
            self.query_peak_rss_bytes,
            "query peak RSS bytes",
            minimum=1,
        )
        query_rss_start = _require_builtin_int(
            self.query_rss_start_bytes,
            "query RSS start bytes",
            minimum=1,
        )
        query_rss_terminal = _require_builtin_int(
            self.query_rss_terminal_bytes,
            "query RSS terminal bytes",
            minimum=1,
        )
        if query_rss_start > query_rss_terminal:
            raise ValueError(
                "query RSS terminal sample must not be below start"
            )
        if query_peak_rss != query_rss_terminal:
            raise ValueError(
                "query peak RSS must equal the terminal high-water sample"
            )
        if self.query_rss_unit != QUERY_RSS_UNIT:
            raise ValueError(f"query RSS unit must be {QUERY_RSS_UNIT!r}")
        _require_identity(self.query_rss_scope, "query RSS scope")
        environment_facts = dict(self.environment)
        for key in ("rss_platform", "rss_raw_unit", "rss_scope"):
            if key not in environment_facts:
                raise ValueError(f"environment is missing {key!r}")
        if environment_facts["rss_scope"] != self.query_rss_scope:
            raise ValueError("environment RSS scope must match the evidence")
        if environment_facts["rss_raw_unit"] not in ("kib", "bytes"):
            raise ValueError("environment RSS raw unit is invalid")
        fts5_enabled = environment_facts.get("fts5_enabled")
        if (
            self.exact_actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            and fts5_enabled != "true"
        ):
            raise ValueError(
                "FTS5_TRIGRAM probe requires environment fts5_enabled=true"
            )
        if (
            self.exact_actual_path is BenchmarkExecutionPath.GRAM_FALLBACK
            and fts5_enabled != "false"
        ):
            raise ValueError(
                "GRAM_FALLBACK probe requires environment fts5_enabled=false"
            )
        if self.environment_digest != benchmark_environment_digest(
            self.environment
        ):
            raise ValueError("environment digest does not match environment")
        object.__setattr__(self, "probe_digest", query_probe_digest(self))

    def recompute_probe_digest(self) -> str:
        """Independently recompute the canonical probe digest."""
        return query_probe_digest(self)


def _probe_fuzzy_query_count_of(report: QueryProbeReport) -> int:
    return report.fuzzy_calls


_PROBE_TOP_K_FACTOR = 10


def _probe_payload_facts(
    report: QueryProbeReport,
) -> dict[str, object]:
    return {
        "actual_index_kind": report.actual_index_kind,
        "artifact_baseline_digest": report.artifact_baseline_digest,
        "artifact_post": artifact_snapshot_to_payload(report.artifact_post),
        "artifact_pre": artifact_snapshot_to_payload(report.artifact_pre),
        "exact_actual_path": report.exact_actual_path.value,
        "exact_calls": report.exact_calls,
        "exact_result_count": report.exact_result_count,
        "fuzzy_actual_path": report.fuzzy_actual_path.value,
        "fuzzy_calls": report.fuzzy_calls,
        "fuzzy_result_count": report.fuzzy_result_count,
        "generation": report.generation,
        "migration_rerun": report.migration_rerun,
        "process_evidence_digest": report.process_evidence_digest,
        "process_pair_digest": report.process_pair_digest,
        "processes_distinct": report.processes_distinct,
        "query_protocol_digest": report.query_protocol_digest,
        "query_peak_rss_bytes": report.query_peak_rss_bytes,
        "query_rss_scope": report.query_rss_scope,
        "query_rss_start_bytes": report.query_rss_start_bytes,
        "query_rss_terminal_bytes": report.query_rss_terminal_bytes,
        "query_rss_unit": report.query_rss_unit,
        "record_count": report.record_count,
        "reopen_action": report.reopen_action,
        "reopen_health_healthy": report.reopen_health_healthy,
        "reopen_health_index_kind": report.reopen_health_index_kind,
        "reopen_health_record_count": report.reopen_health_record_count,
        "reopen_phase": report.reopen_phase,
        "schema_version": report.schema_version,
        "environment_digest": report.environment_digest,
    }


def query_probe_digest(report: QueryProbeReport) -> str:
    """Canonical digest over every probe fact except the digest itself."""
    if type(report) is not QueryProbeReport:
        raise TypeError("report must be QueryProbeReport")
    payload = _probe_payload_facts(report)
    payload["environment"] = _environment_payload(report.environment)
    return benchmark_digest(
        QUERY_PROBE_DIGEST_VERSION,
        "query-probe",
        [payload],
    )


_QUERY_PROBE_PAYLOAD_FIELDS = frozenset(
    {
        "actual_index_kind",
        "artifact_baseline_digest",
        "artifact_post",
        "artifact_pre",
        "artifact_unchanged",
        "environment",
        "environment_digest",
        "exact_actual_path",
        "exact_calls",
        "exact_result_count",
        "fuzzy_actual_path",
        "fuzzy_calls",
        "fuzzy_result_count",
        "generation",
        "migration_rerun",
        "probe_digest",
        "process_evidence_digest",
        "process_pair_digest",
        "processes_distinct",
        "query_peak_rss_bytes",
        "query_protocol_digest",
        "query_rss_scope",
        "query_rss_start_bytes",
        "query_rss_terminal_bytes",
        "query_rss_unit",
        "record_count",
        "reopen_action",
        "reopen_health_healthy",
        "reopen_health_index_kind",
        "reopen_health_record_count",
        "reopen_phase",
        "schema_version",
    }
)


def query_probe_to_payload(report: QueryProbeReport) -> dict[str, object]:
    """Strict public payload snapshot of one query probe report."""
    if type(report) is not QueryProbeReport:
        raise TypeError("report must be QueryProbeReport")
    payload = _probe_payload_facts(report)
    payload.update(
        {
            "artifact_unchanged": report.artifact_unchanged,
            "environment": _environment_payload(report.environment),
            "probe_digest": report.probe_digest,
        }
    )
    return payload


def query_probe_from_payload(payload: Mapping[str, object]) -> QueryProbeReport:
    """Strictly reconstruct a self-validating probe report from a payload."""
    fields = _strict_fields(
        payload,
        _QUERY_PROBE_PAYLOAD_FIELDS,
        "query probe payload",
    )
    try:
        exact_actual_path = BenchmarkExecutionPath(
            _as_str(fields["exact_actual_path"], "exact actual path")
        )
        fuzzy_actual_path = BenchmarkExecutionPath(
            _as_str(fields["fuzzy_actual_path"], "fuzzy actual path")
        )
    except ValueError as error:
        raise ValueError("probe actual path is invalid") from error
    report = QueryProbeReport(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        process_evidence_digest=_as_digest(
            fields["process_evidence_digest"],
            "process evidence digest",
        ),
        artifact_baseline_digest=_as_digest(
            fields["artifact_baseline_digest"],
            "artifact baseline digest",
        ),
        processes_distinct=_as_bool(
            fields["processes_distinct"],
            "processes distinct",
        ),
        process_pair_digest=_as_digest(
            fields["process_pair_digest"],
            "process pair digest",
        ),
        query_protocol_digest=_as_digest(
            fields["query_protocol_digest"],
            "query protocol digest",
        ),
        artifact_pre=artifact_snapshot_from_payload(fields["artifact_pre"]),
        artifact_post=artifact_snapshot_from_payload(fields["artifact_post"]),
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
        generation=_as_int(fields["generation"], "generation", minimum=0),
        actual_index_kind=_as_str(
            fields["actual_index_kind"],
            "actual index kind",
        ),
        record_count=_as_int(fields["record_count"], "record count", minimum=1),
        exact_calls=_as_int(fields["exact_calls"], "exact calls", minimum=1),
        exact_actual_path=exact_actual_path,
        exact_result_count=_as_int(
            fields["exact_result_count"],
            "exact result count",
            minimum=1,
        ),
        fuzzy_calls=_as_int(fields["fuzzy_calls"], "fuzzy calls", minimum=1),
        fuzzy_actual_path=fuzzy_actual_path,
        fuzzy_result_count=_as_int(
            fields["fuzzy_result_count"],
            "fuzzy result count",
            minimum=0,
        ),
        migration_rerun=_as_bool(fields["migration_rerun"], "migration rerun"),
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
        environment=_environment_from_payload(fields["environment"]),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
    )
    if fields["artifact_unchanged"] != report.artifact_unchanged:
        raise ValueError(
            "artifact unchanged fact does not match the reconstructed report"
        )
    caller_digest = _as_digest(fields["probe_digest"], "probe digest")
    if caller_digest != report.probe_digest:
        raise ValueError("probe digest does not match the reconstructed report")
    return report


def query_probe_to_json(report: QueryProbeReport) -> str:
    """Strict canonical JSON snapshot of one query probe report."""
    return _canonical_json(query_probe_to_payload(report))


def query_probe_from_json(serialized: str) -> QueryProbeReport:
    """Strictly reconstruct a probe report from one canonical JSON object."""
    if type(serialized) is not str:
        raise TypeError("serialized probe must be a string")
    return query_probe_from_payload(_parse_strict_json(serialized))


# --- QueryProcessEvidence ---------------------------------------------------


def _query_process_evidence_digest_payload(
    evidence: QueryProcessEvidence,
) -> dict[str, object]:
    return {
        "actual_index_kind": evidence.actual_index_kind,
        "artifact_baseline_digest": evidence.artifact_baseline_digest,
        "artifact_key": evidence.artifact_key,
        "artifact_post": artifact_snapshot_to_payload(evidence.artifact_post),
        "artifact_pre": artifact_snapshot_to_payload(evidence.artifact_pre),
        "canonical_store_id": evidence.canonical_store_id,
        "contract_digest": evidence.contract_digest,
        "corpus_digest": evidence.corpus_digest,
        "corpus_record_count": evidence.corpus_record_count,
        "environment_digest": evidence.environment_digest,
        "execution_path": evidence.execution_path.value,
        "final_evidence": evidence.final_evidence,
        "fixture_digest": evidence.fixture_digest,
        "fixture_record_count": evidence.fixture_record_count,
        "generation": evidence.generation,
        "latency_evidence_digest": evidence.latency_evidence_digest,
        "path_config_digest": evidence.path_config_digest,
        "process_evidence_digest": evidence.process_evidence_digest,
        "process_pair_digest": evidence.process_pair_digest,
        "process_test_mode": evidence.process_test_mode,
        "processes_distinct": evidence.processes_distinct,
        "query_protocol_digest": evidence.query_protocol_digest,
        "query_peak_rss_bytes": evidence.query_peak_rss_bytes,
        "query_rss_scope": evidence.query_rss_scope,
        "query_rss_start_bytes": evidence.query_rss_start_bytes,
        "query_rss_terminal_bytes": evidence.query_rss_terminal_bytes,
        "query_rss_unit": evidence.query_rss_unit,
        "record_count": evidence.record_count,
        "resource_id": evidence.resource_id,
        "schema_version": evidence.schema_version,
    }


@dataclass(frozen=True)
class QueryProcessEvidence:
    """Frozen Task 8.5A query-process raw evidence.

    Embeds a private strict snapshot of the process evidence binding facts
    (digests and stable facts only; never the migration child PID or the
    absolute run-root/fixture locators) and the complete path-specific
    ``LatencyEvidence`` plus the process artifact baseline digest.  The
    portable projection is the stable ``artifact_key`` plus the bound
    digests; ``final_evidence`` is derived from literal non-test
    process+latency facts and is never caller-supplied.  It is not
    standalone authorization: final acceptance exists only as the paired
    ``QueryProcessRunResult`` produced by ``run_query_process_evidence``
    after the parent adjudicates every field against the exact process
    evidence and the actual response PID.
    """

    schema_version: str
    artifact_key: str
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
    process_test_mode: bool
    processes_distinct: bool
    process_pair_digest: str
    query_protocol_digest: str
    artifact_pre: ArtifactSnapshot
    artifact_post: ArtifactSnapshot
    latency_evidence: LatencyEvidence
    latency_evidence_digest: str
    query_peak_rss_bytes: int
    query_rss_start_bytes: int
    query_rss_terminal_bytes: int
    query_rss_unit: str
    query_rss_scope: str
    environment: tuple[tuple[str, str], ...]
    environment_digest: str
    artifact_unchanged: bool = field(init=False)
    final_evidence: bool = field(init=False)
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                "schema version must be "
                f"{QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION}"
            )
        _require_digest(self.artifact_key, "artifact key")
        _require_digest(self.contract_digest, "contract digest")
        _require_digest(self.corpus_digest, "corpus digest")
        _require_digest(self.fixture_digest, "fixture digest")
        _require_digest(self.path_config_digest, "path config digest")
        _require_digest(self.process_evidence_digest, "process evidence digest")
        _require_digest(self.artifact_baseline_digest, "artifact baseline digest")
        _require_digest(self.process_pair_digest, "process pair digest")
        _require_digest(self.query_protocol_digest, "query protocol digest")
        _require_digest(self.latency_evidence_digest, "latency evidence digest")
        _require_digest(self.environment_digest, "environment digest")
        _require_identity(self.resource_id, "resource id")
        _require_identity(self.canonical_store_id, "canonical store id")
        _require_identity(self.actual_index_kind, "actual index kind")
        if type(self.execution_path) is not BenchmarkExecutionPath:
            raise TypeError("execution path must be BenchmarkExecutionPath")
        expected_index_kind = (
            "FTS5_TRIGRAM"
            if self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else "GRAM_FALLBACK"
        )
        if self.actual_index_kind != expected_index_kind:
            raise ValueError(
                "actual index kind must match the execution path"
            )
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
            raise ValueError("corpus/fixture/store record counts must be equal")
        _require_builtin_int(self.generation, "generation", minimum=0)
        process_test_mode = _require_builtin_bool(
            self.process_test_mode,
            "process test mode",
        )
        if not process_test_mode:
            if corpus_record_count != REAL_CORPUS_RECORD_COUNT:
                raise ValueError(
                    "final evidence requires the 100000-record corpus"
                )
        if not _require_builtin_bool(
            self.processes_distinct,
            "processes distinct",
        ):
            raise ValueError("query child must be a distinct process")
        if type(self.artifact_pre) is not ArtifactSnapshot:
            raise TypeError("artifact pre snapshot must be ArtifactSnapshot")
        if type(self.artifact_post) is not ArtifactSnapshot:
            raise TypeError("artifact post snapshot must be ArtifactSnapshot")
        unchanged = _artifact_snapshots_equal(
            self.artifact_pre,
            self.artifact_post,
        )
        object.__setattr__(self, "artifact_unchanged", unchanged)
        if not unchanged:
            raise ValueError("pre/post artifact identity or digest drifted")
        if type(self.latency_evidence) is not LatencyEvidence:
            raise TypeError("latency evidence must be LatencyEvidence")
        if self.latency_evidence.execution_path is not self.execution_path:
            raise ValueError("latency evidence path must match query evidence")
        if self.latency_evidence.contract_digest != self.contract_digest:
            raise ValueError(
                "latency evidence contract digest must match query evidence"
            )
        if self.latency_evidence_digest != latency_evidence_digest(
            self.latency_evidence
        ):
            raise ValueError(
                "latency evidence digest must bind the latency evidence"
            )
        if not process_test_mode:
            if (
                self.corpus_digest
                != self.latency_evidence.contract.corpus_digest
            ):
                raise ValueError(
                    "corpus digest must match the latency evidence contract"
                )
        query_peak_rss = _require_builtin_int(
            self.query_peak_rss_bytes,
            "query peak RSS bytes",
            minimum=1,
        )
        query_rss_start = _require_builtin_int(
            self.query_rss_start_bytes,
            "query RSS start bytes",
            minimum=1,
        )
        query_rss_terminal = _require_builtin_int(
            self.query_rss_terminal_bytes,
            "query RSS terminal bytes",
            minimum=1,
        )
        if query_rss_start > query_rss_terminal:
            raise ValueError(
                "query RSS terminal sample must not be below start"
            )
        if query_peak_rss != query_rss_terminal:
            raise ValueError(
                "query peak RSS must equal the terminal high-water sample"
            )
        if self.query_rss_unit != QUERY_RSS_UNIT:
            raise ValueError(f"query RSS unit must be {QUERY_RSS_UNIT!r}")
        _require_identity(self.query_rss_scope, "query RSS scope")
        if (
            self.query_rss_scope
            != self.latency_evidence.contract.rss_scope
        ):
            raise ValueError("query RSS scope must match the contract")
        environment_facts = dict(self.environment)
        for key in ("rss_platform", "rss_raw_unit", "rss_scope"):
            if key not in environment_facts:
                raise ValueError(f"environment is missing {key!r}")
        if environment_facts["rss_scope"] != self.query_rss_scope:
            raise ValueError("environment RSS scope must match the evidence")
        if environment_facts["rss_raw_unit"] not in ("kib", "bytes"):
            raise ValueError("environment RSS raw unit is invalid")
        fts5_enabled = environment_facts.get("fts5_enabled")
        if (
            self.execution_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            and fts5_enabled != "true"
        ):
            raise ValueError(
                "FTS5_TRIGRAM evidence requires environment fts5_enabled=true"
            )
        if (
            self.execution_path is BenchmarkExecutionPath.GRAM_FALLBACK
            and fts5_enabled != "false"
        ):
            raise ValueError(
                "GRAM_FALLBACK evidence requires environment fts5_enabled=false"
            )
        if self.environment_digest != benchmark_environment_digest(
            self.environment
        ):
            raise ValueError("environment digest does not match environment")
        final_evidence = not process_test_mode
        object.__setattr__(self, "final_evidence", final_evidence)
        object.__setattr__(
            self,
            "evidence_digest",
            query_process_evidence_digest(self),
        )

    def recompute_evidence_digest(self) -> str:
        """Independently recompute the canonical evidence digest."""
        return query_process_evidence_digest(self)

    def recompute_environment_digest(self) -> str:
        """Independently recompute the strict environment digest."""
        return benchmark_environment_digest(self.environment)


def query_process_evidence_digest(
    evidence: QueryProcessEvidence,
) -> str:
    """Canonical digest over every evidence fact except the digest itself."""
    if type(evidence) is not QueryProcessEvidence:
        raise TypeError("evidence must be QueryProcessEvidence")
    payload = _query_process_evidence_digest_payload(evidence)
    payload["environment"] = _environment_payload(evidence.environment)
    return benchmark_digest(
        QUERY_EVIDENCE_DIGEST_VERSION,
        "query-process-evidence",
        [payload],
    )


_QUERY_EVIDENCE_PAYLOAD_FIELDS = frozenset(
    {
        "actual_index_kind",
        "artifact_baseline_digest",
        "artifact_key",
        "artifact_post",
        "artifact_pre",
        "artifact_unchanged",
        "canonical_store_id",
        "contract_digest",
        "corpus_digest",
        "corpus_record_count",
        "environment",
        "environment_digest",
        "evidence_digest",
        "execution_path",
        "final_evidence",
        "fixture_digest",
        "fixture_record_count",
        "generation",
        "latency_evidence",
        "latency_evidence_digest",
        "path_config_digest",
        "process_evidence_digest",
        "process_pair_digest",
        "process_test_mode",
        "processes_distinct",
        "query_peak_rss_bytes",
        "query_protocol_digest",
        "query_rss_scope",
        "query_rss_start_bytes",
        "query_rss_terminal_bytes",
        "query_rss_unit",
        "record_count",
        "resource_id",
        "schema_version",
    }
)


def query_process_evidence_to_payload(
    evidence: QueryProcessEvidence,
) -> dict[str, object]:
    """Strict public payload snapshot of one query-process evidence value."""
    if type(evidence) is not QueryProcessEvidence:
        raise TypeError("evidence must be QueryProcessEvidence")
    payload = _query_process_evidence_digest_payload(evidence)
    payload.update(
        {
            "artifact_unchanged": evidence.artifact_unchanged,
            "environment": _environment_payload(evidence.environment),
            "evidence_digest": evidence.evidence_digest,
            "latency_evidence": latency_evidence_to_payload(
                evidence.latency_evidence
            ),
        }
    )
    return payload


def query_process_evidence_from_payload(
    payload: Mapping[str, object],
) -> QueryProcessEvidence:
    """Strictly reconstruct a self-validating query evidence from a payload."""
    fields = _strict_fields(
        payload,
        _QUERY_EVIDENCE_PAYLOAD_FIELDS,
        "query process evidence payload",
    )
    try:
        execution_path = BenchmarkExecutionPath(
            _as_str(fields["execution_path"], "execution path")
        )
    except ValueError as error:
        raise ValueError("execution path is invalid") from error
    latency_evidence_value = fields["latency_evidence"]
    if type(latency_evidence_value) is not dict:
        raise ValueError("latency evidence payload is invalid")
    try:
        latency_evidence = latency_evidence_from_payload(
            latency_evidence_value
        )
    except (TypeError, ValueError) as error:
        raise ValueError("latency evidence payload is invalid") from error
    evidence = QueryProcessEvidence(
        schema_version=_as_str(fields["schema_version"], "schema version"),
        artifact_key=_as_digest(fields["artifact_key"], "artifact key"),
        contract_digest=_as_digest(fields["contract_digest"], "contract digest"),
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
        process_evidence_digest=_as_digest(
            fields["process_evidence_digest"],
            "process evidence digest",
        ),
        artifact_baseline_digest=_as_digest(
            fields["artifact_baseline_digest"],
            "artifact baseline digest",
        ),
        process_test_mode=_as_bool(
            fields["process_test_mode"],
            "process test mode",
        ),
        processes_distinct=_as_bool(
            fields["processes_distinct"],
            "processes distinct",
        ),
        process_pair_digest=_as_digest(
            fields["process_pair_digest"],
            "process pair digest",
        ),
        query_protocol_digest=_as_digest(
            fields["query_protocol_digest"],
            "query protocol digest",
        ),
        artifact_pre=artifact_snapshot_from_payload(fields["artifact_pre"]),
        artifact_post=artifact_snapshot_from_payload(fields["artifact_post"]),
        latency_evidence=latency_evidence,
        latency_evidence_digest=_as_digest(
            fields["latency_evidence_digest"],
            "latency evidence digest",
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
        environment=_environment_from_payload(fields["environment"]),
        environment_digest=_as_digest(
            fields["environment_digest"],
            "environment digest",
        ),
    )
    if fields["artifact_unchanged"] != evidence.artifact_unchanged:
        raise ValueError(
            "artifact unchanged fact does not match the reconstructed evidence"
        )
    if fields["final_evidence"] != evidence.final_evidence:
        raise ValueError(
            "final evidence fact does not match the reconstructed evidence"
        )
    caller_digest = _as_digest(fields["evidence_digest"], "evidence digest")
    if caller_digest != evidence.evidence_digest:
        raise ValueError(
            "evidence digest does not match the reconstructed evidence"
        )
    return evidence


def query_process_evidence_to_json(
    evidence: QueryProcessEvidence,
) -> str:
    """Strict canonical JSON snapshot of one query-process evidence value."""
    return _canonical_json(query_process_evidence_to_payload(evidence))


def query_process_evidence_from_json(
    serialized: str,
) -> QueryProcessEvidence:
    """Strictly reconstruct query evidence from one canonical JSON object."""
    if type(serialized) is not str:
        raise TypeError("serialized query evidence must be a string")
    return query_process_evidence_from_payload(
        _parse_strict_json(serialized)
    )


def _artifact_key(
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
    process_evidence_digest_value: str,
    artifact_baseline_digest_value: str,
    latency_evidence_digest_value: str,
) -> str:
    return benchmark_digest(
        QUERY_ARTIFACT_KEY_VERSION,
        "query-process-artifact",
        [
            {
                "actual_index_kind": actual_index_kind,
                "artifact_baseline_digest": _require_digest(
                    artifact_baseline_digest_value,
                    "artifact baseline digest",
                ),
                "canonical_store_id": canonical_store_id,
                "contract_digest": contract_digest,
                "corpus_digest": corpus_digest,
                "execution_path": execution_path.value,
                "fixture_digest": fixture_digest,
                "generation": generation,
                "latency_evidence_digest": latency_evidence_digest_value,
                "path_config_digest": path_config_digest,
                "process_evidence_digest": process_evidence_digest_value,
                "record_count": record_count,
                "resource_id": resource_id,
            }
        ],
    )


def _process_pair_digest(
    *,
    migration_child_pid: int,
    query_child_pid: int,
) -> str:
    return benchmark_digest(
        QUERY_PROCESS_PAIR_VERSION,
        "query-process-pair",
        [
            {
                "migration_child_pid": _require_builtin_int(
                    migration_child_pid,
                    "migration child pid",
                    minimum=1,
                ),
                "query_child_pid": _require_builtin_int(
                    query_child_pid,
                    "query child pid",
                    minimum=1,
                ),
            }
        ],
    )


# --- Real executor and child lifecycle --------------------------------------


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


class _RealStoreExecutor:
    """LatencyExecutor seam bound to the reopened production store."""

    def __init__(
        self,
        *,
        store: SQLiteTMStore,
        resource_id: str,
        actual_path: BenchmarkExecutionPath,
        actual_index_kind: str,
    ) -> None:
        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be SQLiteTMStore")
        _require_identity(resource_id, "resource id")
        if type(actual_path) is not BenchmarkExecutionPath:
            raise TypeError("actual path must be BenchmarkExecutionPath")
        _require_identity(actual_index_kind, "actual index kind")
        expected_index_kind = (
            "FTS5_TRIGRAM"
            if actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM
            else "GRAM_FALLBACK"
        )
        if actual_index_kind != expected_index_kind:
            raise ValueError("actual index kind must match the actual path")
        self._store = store
        self._resource_id = resource_id
        self._actual_path = actual_path
        self._actual_index_kind = actual_index_kind

    def exact_lookup(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
    ) -> _ExactOutcome:
        records = self._store.exact_records(query_raw)
        return _ExactOutcome(
            actual_path=self._actual_path,
            succeeded=len(records) > 0,
            result_count=len(records),
        )

    def fuzzy_top_k(
        self,
        query_raw: str,
        *,
        requested_path: BenchmarkExecutionPath,
        minimum_similarity: float,
        top_k: int,
    ) -> _FuzzyOutcome:
        folded_query = fold_text_v1(query_raw).folded_text
        report = CandidateRetriever().candidates(
            self._resource_id,
            self._store,
            folded_query,
            result_limit=top_k,
        )
        metadata = report.metadata
        if metadata.index_kind != self._actual_index_kind:
            raise _WorkerError("QUERY.INDEX_KIND_MISMATCH")
        if not metadata.fuzzy_available:
            raise _WorkerError("QUERY.FUZZY_UNAVAILABLE")
        records = self._store.records_by_id(
            tuple(candidate.record_id for candidate in report.candidates)
        )
        query = TMQuery(
            query_source=query_raw,
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=minimum_similarity,
            limit=top_k,
            resource_order=(self._resource_id,),
        )
        result = score_fuzzy_candidates(
            resource_id=self._resource_id,
            resource_order=0,
            query=query,
            report=report,
            records=records,
            scorer=None,
        )
        return _FuzzyOutcome(
            actual_path=self._actual_path,
            succeeded=True,
            result_count=len(result.accepted),
            minimum_similarity=minimum_similarity,
            top_k=top_k,
        )


def _collect_query_environment(
    *,
    fts5_enabled: bool,
    rss_scope: str,
) -> tuple[tuple[str, str], ...]:
    if sys.platform.startswith("linux"):
        rss_platform, rss_raw_unit = "linux", "kib"
    elif sys.platform == "darwin":
        rss_platform, rss_raw_unit = "darwin", "bytes"
    else:
        raise _WorkerError("QUERY.RSS_UNSUPPORTED_PLATFORM")
    return collect_process_environment(
        fts5_enabled=fts5_enabled,
        rss_raw_unit=rss_raw_unit,
        rss_platform=rss_platform,
        rss_scope=rss_scope,
    )


def _latency_environment(
    *,
    fts5_enabled: bool,
    contract: BenchmarkContract,
) -> tuple[tuple[str, str], ...]:
    environment = dict(
        collect_benchmark_environment(
            timing_clock=DEFAULT_TIMING_CLOCK_NAME,
            percentile_method=contract.percentile_method,
            warmup_queries_per_cohort=contract.warmup_queries_per_cohort,
            measured_repeats=contract.measured_repeats,
        )
    )
    environment["fts5_enabled"] = "true" if fts5_enabled else "false"
    return tuple(sorted(environment.items()))


def _reopen_store(request: _WorkerRequest) -> tuple[
    SQLiteTMStore,
    CanonicalResourceIdentity,
    str,
]:
    fixture_path = Path(request.fixture_path)
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        request.resource_id,
        fixture_path.resolve(),
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=request.canonical_store_id,
        resource_identity=identity,
    )
    # A GRAM_FALLBACK artifact records ``candidate_index_kind=GRAM_FALLBACK``
    # in its own schema and ``fts5_available=false`` in meta; the store's
    # fail-closed open validation requires the runtime capability probe to
    # mirror that recorded schema fact (the same bounded seam the Task 8.3
    # fallback child uses).  The patch never chooses or fakes an execution
    # path: the actual index kind below is read back from the reopened
    # store's real health/schema, and any artifact whose schema disagrees
    # with the request still fails closed (CANDIDATE_INDEX_MISMATCH).
    force_fallback = request.actual_index_kind == "GRAM_FALLBACK"
    with ExitStack() as stack:
        if force_fallback:
            stack.enter_context(
                patch("tm_sqlite_store._probe_fts5", return_value=False)
            )
        try:
            report = coordinator.rehydrate_runtime_authority()
        except Exception as error:
            raise _WorkerError("QUERY.REOPEN_FAILED") from error
        if report is None:
            raise _WorkerError("QUERY.REOPEN_FAILED")
        if (
            report.action != "COMPLETED"
            or report.phase != "GENERATION_PUBLISHED"
        ):
            raise _WorkerError("QUERY.REOPEN_FAILED")
        if report.generation != request.generation:
            raise _WorkerError("QUERY.REOPEN_FAILED")
        store = SQLiteTMStore.from_coordinator(coordinator)
        health = store.health()
        if not health.healthy:
            raise _WorkerError("QUERY.HEALTH_FAILED")
        if health.index_kind != request.actual_index_kind:
            raise _WorkerError("QUERY.INDEX_KIND_MISMATCH")
        if health.record_count != request.record_count:
            raise _WorkerError("QUERY.COUNT_MISMATCH")
        if not health.exact_available:
            raise _WorkerError("QUERY.EXACT_UNAVAILABLE")
        if health.generation != request.generation:
            raise _WorkerError("QUERY.REOPEN_FAILED")
        return store, identity, health.index_kind


def _run_probe(
    request: _WorkerRequest,
    *,
    start_usage: resource.struct_rusage,
) -> dict[str, object]:
    fixture_path = Path(request.fixture_path)
    run_root = Path(request.run_root)
    try:
        artifact_pre = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=request.resource_id,
            expected_fixture_digest=request.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.ARTIFACT_INVALID") from error
    if not _artifact_snapshot_matches_baseline(
        artifact_pre,
        request.process_evidence,
    ):
        raise _WorkerError("QUERY.ARTIFACT_BASELINE_DRIFT")
    store, _identity, actual_index_kind = _reopen_store(request)
    try:
        first_source = _first_fixture_source(fixture_path)
        exact_records = store.exact_records(first_source)
        if not exact_records:
            raise _WorkerError("QUERY.EXACT_PROOF_FAILED")
        exact_result_count = len(exact_records)
        fuzzy_calls = _probe_fuzzy_query_count(request.process_evidence.contract)
        fuzzy_queries = tuple(
            iter_fuzzy_queries(
                seed=request.process_evidence.contract.corpus_seed,
                record_count=request.record_count,
                cohort_count=fuzzy_calls,
            )
        )
        if len(fuzzy_queries) != fuzzy_calls:
            raise _WorkerError("QUERY.EXECUTOR_FAILED")
        fuzzy_result_count = 0
        for fuzzy_query in fuzzy_queries:
            folded_query = fold_text_v1(fuzzy_query.query_raw).folded_text
            report = CandidateRetriever().candidates(
                request.resource_id,
                store,
                folded_query,
                result_limit=request.process_evidence.contract.top_k,
            )
            metadata = report.metadata
            if metadata.index_kind != actual_index_kind:
                raise _WorkerError("QUERY.INDEX_KIND_MISMATCH")
            if not metadata.fuzzy_available:
                raise _WorkerError("QUERY.FUZZY_UNAVAILABLE")
            records = store.records_by_id(
                tuple(candidate.record_id for candidate in report.candidates)
            )
            query = TMQuery(
                query_source=fuzzy_query.query_raw,
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=request.process_evidence.contract.minimum_similarity,
                limit=request.process_evidence.contract.top_k,
                resource_order=(request.resource_id,),
            )
            result = score_fuzzy_candidates(
                resource_id=request.resource_id,
                resource_order=0,
                query=query,
                report=report,
                records=records,
                scorer=None,
            )
            fuzzy_result_count += len(result.accepted)
    except _WorkerError:
        raise
    except Exception as error:
        raise _WorkerError("QUERY.EXECUTOR_FAILED") from error
    try:
        artifact_post = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=request.resource_id,
            expected_fixture_digest=request.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.ARTIFACT_MUTATED") from error
    if not _artifact_snapshots_equal(artifact_pre, artifact_post):
        raise _WorkerError("QUERY.ARTIFACT_MUTATED")
    if not _artifact_snapshot_matches_baseline(
        artifact_post,
        request.process_evidence,
    ):
        raise _WorkerError("QUERY.ARTIFACT_BASELINE_DRIFT")
    query_peak_rss_bytes, _raw_unit = _terminal_rss_facts(start_usage)
    actual_path = (
        BenchmarkExecutionPath.FTS5_TRIGRAM
        if actual_index_kind == "FTS5_TRIGRAM"
        else BenchmarkExecutionPath.GRAM_FALLBACK
    )
    fts5_enabled = (
        _fts5_available()
        if actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else False
    )
    environment = _collect_query_environment(
        fts5_enabled=fts5_enabled,
        rss_scope=request.process_evidence.contract.rss_scope,
    )
    report = QueryProbeReport(
        schema_version=QUERY_PROBE_SCHEMA_VERSION,
        process_evidence_digest=request.process_evidence_digest,
        artifact_baseline_digest=artifact_snapshot_digest(
            request.process_evidence.artifact_snapshot
        ),
        processes_distinct=(
            os.getpid() != request.process_evidence.child_pid
        ),
        process_pair_digest=_process_pair_digest(
            migration_child_pid=request.process_evidence.child_pid,
            query_child_pid=os.getpid(),
        ),
        query_protocol_digest=request.protocol_digest,
        artifact_pre=artifact_pre,
        artifact_post=artifact_post,
        reopen_phase="GENERATION_PUBLISHED",
        reopen_action="COMPLETED",
        reopen_health_healthy=True,
        reopen_health_index_kind=actual_index_kind,
        reopen_health_record_count=request.record_count,
        generation=request.generation,
        actual_index_kind=actual_index_kind,
        record_count=request.record_count,
        exact_calls=1,
        exact_actual_path=actual_path,
        exact_result_count=exact_result_count,
        fuzzy_calls=fuzzy_calls,
        fuzzy_actual_path=actual_path,
        fuzzy_result_count=fuzzy_result_count,
        migration_rerun=False,
        query_peak_rss_bytes=query_peak_rss_bytes,
        query_rss_start_bytes=_rss_start_bytes(start_usage),
        query_rss_terminal_bytes=query_peak_rss_bytes,
        query_rss_unit=QUERY_RSS_UNIT,
        query_rss_scope=request.process_evidence.contract.rss_scope,
        environment=environment,
        environment_digest=benchmark_environment_digest(environment),
    )
    return query_probe_to_payload(report)


def _rss_start_bytes(start_usage: resource.struct_rusage) -> int:
    _platform, _raw_unit, start_bytes = rss_peak_bytes_facts(start_usage)
    return start_bytes


def _terminal_rss_facts(
    start_usage: resource.struct_rusage,
) -> tuple[int, str]:
    terminal_usage = resource.getrusage(resource.RUSAGE_SELF)
    _platform, raw_unit, terminal_bytes = rss_peak_bytes_facts(terminal_usage)
    return terminal_bytes, raw_unit


def _run_evidence(
    request: _WorkerRequest,
    *,
    start_usage: resource.struct_rusage,
) -> dict[str, object]:
    fixture_path = Path(request.fixture_path)
    run_root = Path(request.run_root)
    try:
        artifact_pre = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=request.resource_id,
            expected_fixture_digest=request.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.ARTIFACT_INVALID") from error
    if not _artifact_snapshot_matches_baseline(
        artifact_pre,
        request.process_evidence,
    ):
        raise _WorkerError("QUERY.ARTIFACT_BASELINE_DRIFT")
    store, _identity, actual_index_kind = _reopen_store(request)
    actual_path = (
        BenchmarkExecutionPath.FTS5_TRIGRAM
        if actual_index_kind == "FTS5_TRIGRAM"
        else BenchmarkExecutionPath.GRAM_FALLBACK
    )
    executor = _RealStoreExecutor(
        store=store,
        resource_id=request.resource_id,
        actual_path=actual_path,
        actual_index_kind=actual_index_kind,
    )
    fts5_enabled = (
        _fts5_available()
        if actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM
        else False
    )
    if actual_path is BenchmarkExecutionPath.FTS5_TRIGRAM and not fts5_enabled:
        raise _WorkerError("QUERY.FTS5_UNAVAILABLE")
    contract = request.process_evidence.contract
    environment = _latency_environment(
        fts5_enabled=fts5_enabled,
        contract=contract,
    )
    try:
        latency_evidence = measure_path_latency(
            contract=contract,
            requested_path=request.execution_path,
            executor=executor,
            environment=environment,
        )
    except Exception as error:
        raise _WorkerError("QUERY.LATENCY_FAILED") from error
    try:
        artifact_post = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=request.resource_id,
            expected_fixture_digest=request.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise _WorkerError("QUERY.ARTIFACT_MUTATED") from error
    if not _artifact_snapshots_equal(artifact_pre, artifact_post):
        raise _WorkerError("QUERY.ARTIFACT_MUTATED")
    if not _artifact_snapshot_matches_baseline(
        artifact_post,
        request.process_evidence,
    ):
        raise _WorkerError("QUERY.ARTIFACT_BASELINE_DRIFT")
    query_peak_rss_bytes, _raw_unit = _terminal_rss_facts(start_usage)
    query_environment = _collect_query_environment(
        fts5_enabled=fts5_enabled,
        rss_scope=contract.rss_scope,
    )
    evidence = QueryProcessEvidence(
        schema_version=QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION,
        artifact_key=_artifact_key(
            contract_digest=request.contract_digest,
            corpus_digest=request.corpus_digest,
            fixture_digest=request.fixture_digest,
            resource_id=request.resource_id,
            canonical_store_id=request.canonical_store_id,
            execution_path=request.execution_path,
            path_config_digest=request.path_config_digest,
            actual_index_kind=request.actual_index_kind,
            record_count=request.record_count,
            generation=request.generation,
            process_evidence_digest_value=request.process_evidence_digest,
            artifact_baseline_digest_value=artifact_snapshot_digest(
                request.process_evidence.artifact_snapshot
            ),
            latency_evidence_digest_value=latency_evidence.evidence_digest,
        ),
        contract_digest=request.contract_digest,
        corpus_digest=request.corpus_digest,
        corpus_record_count=request.corpus_record_count,
        fixture_digest=request.fixture_digest,
        fixture_record_count=request.fixture_record_count,
        resource_id=request.resource_id,
        canonical_store_id=request.canonical_store_id,
        execution_path=request.execution_path,
        path_config_digest=request.path_config_digest,
        actual_index_kind=request.actual_index_kind,
        record_count=request.record_count,
        generation=request.generation,
        process_evidence_digest=request.process_evidence_digest,
        artifact_baseline_digest=artifact_snapshot_digest(
            request.process_evidence.artifact_snapshot
        ),
        process_test_mode=request.process_evidence.test_mode,
        processes_distinct=(
            os.getpid() != request.process_evidence.child_pid
        ),
        process_pair_digest=_process_pair_digest(
            migration_child_pid=request.process_evidence.child_pid,
            query_child_pid=os.getpid(),
        ),
        query_protocol_digest=request.protocol_digest,
        artifact_pre=artifact_pre,
        artifact_post=artifact_post,
        latency_evidence=latency_evidence,
        latency_evidence_digest=latency_evidence.evidence_digest,
        query_peak_rss_bytes=query_peak_rss_bytes,
        query_rss_start_bytes=_rss_start_bytes(start_usage),
        query_rss_terminal_bytes=query_peak_rss_bytes,
        query_rss_unit=QUERY_RSS_UNIT,
        query_rss_scope=contract.rss_scope,
        environment=query_environment,
        environment_digest=benchmark_environment_digest(query_environment),
    )
    return query_process_evidence_to_payload(evidence)


def _worker_main(argv: list[str]) -> int:
    if argv != [QUERY_WORKER_MODE_FLAG]:
        sys.stderr.write(
            "usage: python -m tm_benchmark_query_process --worker\n"
        )
        return 2
    start_usage = resource.getrusage(resource.RUSAGE_SELF)
    try:
        raw_request = sys.stdin.buffer.read()
        payload = _read_worker_request(raw_request)
        request = _validate_worker_request(payload)
        if request.mode == "probe":
            response_payload = _run_probe(
                request,
                start_usage=start_usage,
            )
        else:
            response_payload = _run_evidence(
                request,
                start_usage=start_usage,
            )
        envelope: dict[str, object] = {
            "kind": request.mode,
            "payload": response_payload,
            "protocol": QUERY_WORKER_PROTOCOL_VERSION,
            "query_pid": os.getpid(),
        }
        sys.stdout.write(_canonical_json(envelope) + "\n")
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
            _canonical_json({"error_code": "QUERY.CHILD_FAILED"}) + "\n"
        )
        sys.stderr.flush()
        return 1


# --- Parent runners ---------------------------------------------------------


@dataclass(frozen=True)
class QueryProbeRunResult:
    """Raw query-child probe run: portable probe plus local run facts."""

    probe: QueryProbeReport
    query_child_pid: int
    run_root: str
    fixture_path: str
    artifact_pre: ArtifactSnapshot
    artifact_post: ArtifactSnapshot


@dataclass(frozen=True)
class QueryProcessRunResult:
    """Parent-adjudicated pair of migration and query-process evidence.

    Unlike the portable nested evidence, this local execution value retains
    the exact process evidence and request digest that the parent adjudicated.
    Gate combination must consume this pair, not a standalone decoded
    ``QueryProcessEvidence``.
    """

    process_evidence: TMBenchmarkProcessEvidence
    evidence: QueryProcessEvidence
    query_child_pid: int
    run_root: str
    fixture_path: str
    artifact_pre: ArtifactSnapshot
    artifact_post: ArtifactSnapshot
    request_protocol_digest: str

    def __post_init__(self) -> None:
        if type(self.process_evidence) is not TMBenchmarkProcessEvidence:
            raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
        if type(self.evidence) is not QueryProcessEvidence:
            raise TypeError("query evidence must be QueryProcessEvidence")
        if not self.evidence.final_evidence:
            raise ValueError("paired query evidence must be final evidence")
        if self.run_root != self.process_evidence.run_root:
            raise ValueError("run root must match process evidence")
        if self.fixture_path != self.process_evidence.fixture_path:
            raise ValueError("fixture path must match process evidence")
        _require_digest(self.request_protocol_digest, "request protocol digest")
        if self.artifact_pre != self.evidence.artifact_pre:
            raise ValueError("parent pre snapshot must match query evidence")
        if self.artifact_post != self.evidence.artifact_post:
            raise ValueError("parent post snapshot must match query evidence")
        _adjudicate_evidence_against_process_evidence(
            evidence=self.evidence,
            process_evidence=self.process_evidence,
            query_child_pid=self.query_child_pid,
            request_protocol_digest=self.request_protocol_digest,
        )


_RESPONSE_FIELDS = frozenset({"kind", "payload", "protocol", "query_pid"})


def _read_child_response(stdout: str) -> dict[str, object]:
    if type(stdout) is not str:
        raise ValueError("child stdout must be text")
    text = stdout.strip()
    if not text:
        raise ValueError("child produced no response payload")
    return _parse_strict_json(text)


def _run_query_child(
    process_evidence: TMBenchmarkProcessEvidence,
    *,
    mode: str,
    timeout_seconds: float,
) -> tuple[
    dict[str, object],
    int,
    str,
    str,
    ArtifactSnapshot,
    ArtifactSnapshot,
    str,
]:
    if type(process_evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    _require_identity(mode, "mode")
    if mode not in ("probe", "evidence"):
        raise ValueError("mode must be 'probe' or 'evidence'")
    if mode == "probe" and not process_evidence.test_mode:
        raise QueryProcessError("QUERY.TEST_MODE_MISMATCH")
    if mode == "evidence" and process_evidence.test_mode:
        raise QueryProcessError("QUERY.TEST_MODE_MISMATCH")
    if type(timeout_seconds) is not float or not math.isfinite(
        timeout_seconds
    ) or timeout_seconds <= 0:
        raise ValueError("timeout seconds must be a positive finite float")
    run_root = Path(process_evidence.run_root).resolve()
    fixture_path = Path(process_evidence.fixture_path).resolve()
    if not run_root.is_dir():
        raise QueryProcessError("QUERY.RUN_ROOT_INVALID")
    if fixture_path.parent != run_root:
        raise QueryProcessError("QUERY.FIXTURE_PATH_INVALID")
    try:
        artifact_pre = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=process_evidence.resource_id,
            expected_fixture_digest=process_evidence.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise QueryProcessError("QUERY.ARTIFACT_INVALID") from error
    if not _artifact_snapshot_matches_baseline(
        artifact_pre,
        process_evidence,
    ):
        raise QueryProcessError("QUERY.ARTIFACT_BASELINE_DRIFT")
    request = _request_payload(
        mode=mode,
        process_evidence=process_evidence,
        run_root=str(run_root),
        fixture_path=str(fixture_path),
    )
    request_protocol_digest = request["protocol_digest"]
    if type(request_protocol_digest) is not str:
        raise QueryProcessError("QUERY.REQUEST_INVALID")
    request_json = _canonical_json(request)
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONWARNINGS"] = "ignore"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tm_benchmark_query_process",
                QUERY_WORKER_MODE_FLAG,
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
        raise QueryProcessError("QUERY.CHILD_SPAWN_FAILED") from error
    except subprocess.TimeoutExpired as error:
        raise QueryProcessError("QUERY.CHILD_TIMEOUT") from error
    if completed.returncode != 0:
        raise QueryProcessError(_child_stderr_code(completed.stderr))
    if completed.stderr.strip():
        raise QueryProcessError("QUERY.CHILD_STDERR_NOISE")
    try:
        response = _read_child_response(completed.stdout)
    except (TypeError, ValueError) as error:
        raise QueryProcessError("QUERY.RESPONSE_INVALID") from error
    fields = _strict_fields(
        response,
        _RESPONSE_FIELDS,
        "query child response",
    )
    if fields["protocol"] != QUERY_WORKER_PROTOCOL_VERSION:
        raise QueryProcessError("QUERY.RESPONSE_INVALID")
    if fields["kind"] != mode:
        raise QueryProcessError("QUERY.RESPONSE_INVALID")
    query_child_pid = _require_builtin_int(
        fields["query_pid"],
        "query child pid",
        minimum=1,
    )
    if query_child_pid == os.getpid():
        raise QueryProcessError("QUERY.CHILD_PID_INVALID")
    if query_child_pid == process_evidence.child_pid:
        raise QueryProcessError("QUERY.CHILD_PID_NOT_DISTINCT")
    payload_value = fields["payload"]
    if type(payload_value) is not dict:
        raise QueryProcessError("QUERY.RESPONSE_INVALID")
    try:
        artifact_post = verify_canonical_artifact(
            run_root=run_root,
            fixture_path=fixture_path,
            resource_id=process_evidence.resource_id,
            expected_fixture_digest=process_evidence.fixture_digest,
        )
    except (TypeError, ValueError) as error:
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED") from error
    if not _artifact_snapshots_equal(artifact_pre, artifact_post):
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED")
    if not _artifact_snapshot_matches_baseline(
        artifact_post,
        process_evidence,
    ):
        raise QueryProcessError("QUERY.ARTIFACT_BASELINE_DRIFT")
    return (
        payload_value,
        query_child_pid,
        str(run_root),
        str(fixture_path),
        artifact_pre,
        artifact_post,
        request_protocol_digest,
    )


def _adjudicate_probe_against_process_evidence(
    *,
    probe: QueryProbeReport,
    process_evidence: TMBenchmarkProcessEvidence,
    query_child_pid: int,
    request_protocol_digest: str,
) -> None:
    """Fresh parent-run adjudication of one returned probe.

    Every material probe fact is compared to the supplied process evidence
    and to the actual request/process facts; a self-consistent returned
    digest is never trusted on its own.  ``artifact_unchanged`` and
    ``processes_distinct`` booleans cannot authorize: distinctness and the
    process-pair digest are recomputed from the real PIDs.
    """
    if type(probe) is not QueryProbeReport:
        raise TypeError("probe must be QueryProbeReport")
    if type(process_evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    if query_child_pid == os.getpid():
        raise QueryProcessError("QUERY.CHILD_PID_INVALID")
    if query_child_pid == process_evidence.child_pid:
        raise QueryProcessError("QUERY.CHILD_PID_NOT_DISTINCT")
    if probe.process_evidence_digest != process_evidence.evidence_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.query_protocol_digest != request_protocol_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if not probe.processes_distinct:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.process_pair_digest != _process_pair_digest(
        migration_child_pid=process_evidence.child_pid,
        query_child_pid=query_child_pid,
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.migration_rerun:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.artifact_baseline_digest != artifact_snapshot_digest(
        process_evidence.artifact_snapshot
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if not _artifact_snapshot_matches_baseline(
        probe.artifact_pre,
        process_evidence,
    ) or not _artifact_snapshot_matches_baseline(
        probe.artifact_post,
        process_evidence,
    ):
        raise QueryProcessError("QUERY.ARTIFACT_BASELINE_DRIFT")
    if probe.generation != process_evidence.generation:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.record_count != process_evidence.record_count:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.actual_index_kind != process_evidence.actual_index_kind:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if (
        probe.exact_actual_path is not process_evidence.execution_path
        or probe.fuzzy_actual_path is not process_evidence.execution_path
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.reopen_health_index_kind != process_evidence.actual_index_kind:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.reopen_health_record_count != process_evidence.record_count:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.query_rss_scope != process_evidence.contract.rss_scope:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if probe.query_rss_unit != QUERY_RSS_UNIT:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    environment_facts = dict(probe.environment)
    if environment_facts.get("rss_scope") != probe.query_rss_scope:
        raise QueryProcessError("QUERY.FACT_DRIFT")


def _adjudicate_evidence_against_process_evidence(
    *,
    evidence: QueryProcessEvidence,
    process_evidence: TMBenchmarkProcessEvidence,
    query_child_pid: int,
    request_protocol_digest: str,
) -> None:
    """Fresh parent-run adjudication of one returned query-process evidence.

    Every material field is compared to the supplied process evidence and
    request facts: process evidence digest, contract/corpus/fixture/resource/
    store/path/path-config/index/count/generation, process test mode, the
    artifact baseline, the returned latency path/contract/cohorts/config,
    actual query-child PID distinctness, the recomputed process-pair digest,
    the just-built query protocol digest, and environment/path/RSS scope.
    Final acceptance is only this paired parent-run result; a standalone
    ``final_evidence`` boolean never authorizes.
    """
    if type(evidence) is not QueryProcessEvidence:
        raise TypeError("evidence must be QueryProcessEvidence")
    if type(process_evidence) is not TMBenchmarkProcessEvidence:
        raise TypeError("process evidence must be TMBenchmarkProcessEvidence")
    if query_child_pid == os.getpid():
        raise QueryProcessError("QUERY.CHILD_PID_INVALID")
    if query_child_pid == process_evidence.child_pid:
        raise QueryProcessError("QUERY.CHILD_PID_NOT_DISTINCT")
    if evidence.process_evidence_digest != process_evidence.evidence_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.query_protocol_digest != request_protocol_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if not evidence.processes_distinct:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.process_pair_digest != _process_pair_digest(
        migration_child_pid=process_evidence.child_pid,
        query_child_pid=query_child_pid,
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.process_test_mode != process_evidence.test_mode:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.artifact_baseline_digest != artifact_snapshot_digest(
        process_evidence.artifact_snapshot
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if not _artifact_snapshot_matches_baseline(
        evidence.artifact_pre,
        process_evidence,
    ) or not _artifact_snapshot_matches_baseline(
        evidence.artifact_post,
        process_evidence,
    ):
        raise QueryProcessError("QUERY.ARTIFACT_BASELINE_DRIFT")
    facts: list[tuple[str, object, object]] = [
        ("contract digest", evidence.contract_digest, process_evidence.contract_digest),
        ("corpus digest", evidence.corpus_digest, process_evidence.corpus_digest),
        (
            "corpus record count",
            evidence.corpus_record_count,
            process_evidence.corpus_record_count,
        ),
        ("fixture digest", evidence.fixture_digest, process_evidence.fixture_digest),
        (
            "fixture record count",
            evidence.fixture_record_count,
            process_evidence.fixture_record_count,
        ),
        ("resource id", evidence.resource_id, process_evidence.resource_id),
        (
            "canonical store id",
            evidence.canonical_store_id,
            process_evidence.canonical_store_id,
        ),
        ("execution path", evidence.execution_path, process_evidence.execution_path),
        (
            "path config digest",
            evidence.path_config_digest,
            process_evidence.path_config_digest,
        ),
        (
            "actual index kind",
            evidence.actual_index_kind,
            process_evidence.actual_index_kind,
        ),
        ("record count", evidence.record_count, process_evidence.record_count),
        ("generation", evidence.generation, process_evidence.generation),
    ]
    for _label, returned, expected in facts:
        if returned != expected:
            raise QueryProcessError("QUERY.FACT_DRIFT")
    latency = evidence.latency_evidence
    contract = process_evidence.contract
    if latency.execution_path is not process_evidence.execution_path:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if latency.contract_digest != process_evidence.contract_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if latency.exact_cohort_digest != contract.exact_cohort_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if latency.fuzzy_cohort_digest != contract.fuzzy_cohort_digest:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if (
        latency.warmup_queries_per_cohort != contract.warmup_queries_per_cohort
        or latency.measured_repeats != contract.measured_repeats
        or latency.percentile_method != contract.percentile_method
        or latency.minimum_similarity != contract.minimum_similarity
        or latency.top_k != contract.top_k
    ):
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.query_rss_scope != contract.rss_scope:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    if evidence.query_rss_unit != QUERY_RSS_UNIT:
        raise QueryProcessError("QUERY.FACT_DRIFT")
    environment_facts = dict(evidence.environment)
    if environment_facts.get("rss_scope") != evidence.query_rss_scope:
        raise QueryProcessError("QUERY.FACT_DRIFT")


def run_query_process_probe(
    process_evidence: TMBenchmarkProcessEvidence,
    *,
    timeout_seconds: float = 120.0,
) -> QueryProbeRunResult:
    """Run one isolated query child in test-only probe mode.

    Requires test-mode process evidence and returns the raw probe facts plus
    the local query-child pid and artifact locators.  Never final evidence.
    """
    payload, query_child_pid, run_root, fixture_path, artifact_pre, artifact_post, request_protocol_digest = (
        _run_query_child(
            process_evidence,
            mode="probe",
            timeout_seconds=timeout_seconds,
        )
    )
    try:
        probe = query_probe_from_payload(payload)
    except (TypeError, ValueError) as error:
        raise QueryProcessError("QUERY.EVIDENCE_INVALID") from error
    _adjudicate_probe_against_process_evidence(
        probe=probe,
        process_evidence=process_evidence,
        query_child_pid=query_child_pid,
        request_protocol_digest=request_protocol_digest,
    )
    if not _artifact_snapshots_equal(artifact_pre, probe.artifact_pre):
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED")
    if not _artifact_snapshots_equal(artifact_pre, probe.artifact_post):
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED")
    return QueryProbeRunResult(
        probe=probe,
        query_child_pid=query_child_pid,
        run_root=run_root,
        fixture_path=fixture_path,
        artifact_pre=artifact_pre,
        artifact_post=artifact_post,
    )


def run_query_process_evidence(
    process_evidence: TMBenchmarkProcessEvidence,
    *,
    timeout_seconds: float = 600.0,
) -> QueryProcessRunResult:
    """Run one isolated query child producing full query-process evidence.

    Consumes exact-type final Task 8.3 process evidence for the literal
    100000-record corpus only; test-mode evidence is refused.  The evidence
    is produced by the real exact/fuzzy latency pipeline on the reopened
    canonical generation.
    """
    payload, query_child_pid, run_root, fixture_path, artifact_pre, artifact_post, request_protocol_digest = (
        _run_query_child(
            process_evidence,
            mode="evidence",
            timeout_seconds=timeout_seconds,
        )
    )
    try:
        evidence = query_process_evidence_from_payload(payload)
    except (TypeError, ValueError) as error:
        raise QueryProcessError("QUERY.EVIDENCE_INVALID") from error
    _adjudicate_evidence_against_process_evidence(
        evidence=evidence,
        process_evidence=process_evidence,
        query_child_pid=query_child_pid,
        request_protocol_digest=request_protocol_digest,
    )
    if not _artifact_snapshots_equal(artifact_pre, evidence.artifact_pre):
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED")
    if not _artifact_snapshots_equal(artifact_pre, evidence.artifact_post):
        raise QueryProcessError("QUERY.ARTIFACT_MUTATED")
    if not evidence.final_evidence:
        raise QueryProcessError("QUERY.EVIDENCE_INVALID")
    return QueryProcessRunResult(
        process_evidence=process_evidence,
        evidence=evidence,
        query_child_pid=query_child_pid,
        run_root=run_root,
        fixture_path=fixture_path,
        artifact_pre=artifact_pre,
        artifact_post=artifact_post,
        request_protocol_digest=request_protocol_digest,
    )


__all__ = [
    "ArtifactFileIdentity",
    "ArtifactSnapshot",
    "QueryProcessError",
    "QueryProcessEvidence",
    "QueryProbeReport",
    "QueryProbeRunResult",
    "QueryProcessRunResult",
    "QUERY_PROCESS_EVIDENCE_SCHEMA_VERSION",
    "QUERY_PROBE_SCHEMA_VERSION",
    "QUERY_WORKER_PROTOCOL_VERSION",
    "artifact_snapshot_from_payload",
    "artifact_snapshot_to_payload",
    "query_process_evidence_digest",
    "query_process_evidence_from_json",
    "query_process_evidence_from_payload",
    "query_process_evidence_to_json",
    "query_process_evidence_to_payload",
    "query_probe_digest",
    "query_probe_from_json",
    "query_probe_from_payload",
    "query_probe_to_json",
    "query_probe_to_payload",
    "query_worker_protocol_digest",
    "run_query_process_evidence",
    "run_query_process_probe",
    "verify_canonical_artifact",
]


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
