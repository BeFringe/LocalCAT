"""Focused tests for the benchmark-v1 isolated process/RSS evidence owner."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping
import unittest
from unittest.mock import patch

from tm_benchmark import iter_corpus_records, load_benchmark_contract
from tm_benchmark_process import (
    ArtifactFileIdentity,
    ArtifactSnapshot,
    PROCESS_EVIDENCE_SCHEMA_VERSION,
    PROCESS_WORKER_PROTOCOL_VERSION,
    ProcessEvidenceError,
    TMBenchmarkProcessEvidence,
    _canonical_json,
    _evidence_from_stdout,
    _evidence_payload,
    artifact_snapshot_digest,
    artifact_snapshot_to_payload,
    collect_process_environment,
    evidence_from_payload,
    process_evidence_digest,
    run_process_migration_evidence,
    worker_protocol_digest,
)
from tm_contracts import (
    BENCHMARK_RSS_SCOPE,
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    contract_to_json,
)

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK

_RUN_ROOT = "/tmp/benchmark-run-test"
_FIXTURE_PATH = f"{_RUN_ROOT}/fixture.jsonl"
_RESOURCE_ID = "tm.benchmark"
_STORE_ID = "store.benchmark"

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
    "tm_snapshot_artifacts",
    "tm_snapshot_recovery",
    "tm_gate_a",
    "tm_gate_b",
}


def _environment(fts5_enabled: bool) -> tuple[tuple[str, str], ...]:
    return collect_process_environment(
        fts5_enabled=fts5_enabled,
        rss_raw_unit="kib",
        rss_platform="linux",
        rss_scope=BENCHMARK_RSS_SCOPE,
    )


def _artifact_snapshot(
    *,
    sidecar_digest: str = "c" * 64,
    manifest_digest: str = "d" * 64,
    family_digest: str = "e" * 64,
) -> ArtifactSnapshot:
    identity = ArtifactFileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    return ArtifactSnapshot(
        sidecar_digest=sidecar_digest,
        manifest_digest=manifest_digest,
        family_digest=family_digest,
        sidecar_identity=identity,
        manifest_identity=identity,
    )


def _protocol_digest(
    *,
    contract_digest: str,
    corpus_digest: str,
    count: int,
    fixture_digest: str,
    execution_path: BenchmarkExecutionPath,
    test_mode: bool = True,
    fixture_path: str = _FIXTURE_PATH,
    run_root: str = _RUN_ROOT,
) -> str:
    return worker_protocol_digest(
        contract_digest=contract_digest,
        corpus_digest=corpus_digest,
        corpus_record_count=count,
        fixture_digest=fixture_digest,
        fixture_path=fixture_path,
        fixture_record_count=count,
        run_root=run_root,
        execution_path=execution_path,
        resource_id=_RESOURCE_ID,
        canonical_store_id=_STORE_ID,
        test_mode=test_mode,
    )


def _small_evidence(**overrides: Any) -> TMBenchmarkProcessEvidence:
    execution_path: BenchmarkExecutionPath = overrides.pop("execution_path", _FTS5)
    test_mode: bool = overrides.pop("test_mode", True)
    count: int = overrides.pop("count", 40)
    contract_digest = benchmark_contract_digest(_CONTRACT)
    corpus_digest = (
        _CONTRACT.corpus_digest if not test_mode else "a" * 64
    )
    fixture_digest = "b" * 64
    environment = _environment(execution_path is _FTS5)
    kwargs: dict[str, Any] = {
        "schema_version": PROCESS_EVIDENCE_SCHEMA_VERSION,
        "test_mode": test_mode,
        "contract": _CONTRACT,
        "contract_digest": contract_digest,
        "corpus_digest": corpus_digest,
        "corpus_record_count": count,
        "fixture_digest": fixture_digest,
        "fixture_path": _FIXTURE_PATH,
        "fixture_record_count": count,
        "run_root": _RUN_ROOT,
        "resource_id": _RESOURCE_ID,
        "canonical_store_id": _STORE_ID,
        "execution_path": execution_path,
        "path_config_digest": (
            _CONTRACT.fast_path_config_digest
            if execution_path is _FTS5
            else _CONTRACT.fallback_path_config_digest
        ),
        "actual_index_kind": (
            "FTS5_TRIGRAM" if execution_path is _FTS5 else "GRAM_FALLBACK"
        ),
        "record_count": count,
        "generation": 0,
        "migration_elapsed_ns": 123456789,
        "peak_rss_bytes": 40 * 1024 * 1024,
        "rss_start_bytes": 30 * 1024 * 1024,
        "rss_terminal_bytes": 40 * 1024 * 1024,
        "rss_unit": "bytes",
        "rss_scope": BENCHMARK_RSS_SCOPE,
        "environment": environment,
        "environment_digest": benchmark_environment_digest(environment),
        "worker_protocol_digest": _protocol_digest(
            contract_digest=contract_digest,
            corpus_digest=corpus_digest,
            count=count,
            fixture_digest=fixture_digest,
            execution_path=execution_path,
            test_mode=test_mode,
        ),
        "artifact_snapshot": _artifact_snapshot(),
        "child_pid": 424242,
        "child_exit_code": 0,
        "reopen_phase": "GENERATION_PUBLISHED",
        "reopen_action": "COMPLETED",
        "reopen_health_healthy": True,
        "reopen_health_index_kind": (
            "FTS5_TRIGRAM" if execution_path is _FTS5 else "GRAM_FALLBACK"
        ),
        "reopen_health_record_count": count,
        "reopen_health_exact_available": True,
        "exact_proof_result_count": 3,
        "exact_proof_winner_matched": True,
        "candidate_proof_index_kind": (
            "FTS5_TRIGRAM" if execution_path is _FTS5 else "GRAM_FALLBACK"
        ),
        "candidate_proof_count": 5,
        "candidate_proof_available": True,
        "candidate_proof_budget": 2048,
    }
    kwargs.update(overrides)
    return TMBenchmarkProcessEvidence(**kwargs)


class ProcessEvidenceConstructorTests(unittest.TestCase):
    def test_real_mode_refuses_non_100k_test_mode_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "100000"):
            _small_evidence(execution_path=_FTS5, test_mode=False, count=40)
        real = _small_evidence(execution_path=_FTS5, test_mode=False, count=100_000)
        self.assertTrue(real.final_evidence)
        test = _small_evidence(execution_path=_FTS5, test_mode=True, count=40)
        self.assertFalse(test.final_evidence)

    def test_evidence_is_frozen_and_digest_is_derived(self) -> None:
        evidence = _small_evidence()
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )
        self.assertEqual(
            process_evidence_digest(evidence),
            evidence.evidence_digest,
        )
        self.assertEqual(
            evidence.recompute_environment_digest(),
            evidence.environment_digest,
        )
        with self.assertRaisesRegex(TypeError, "init=False"):
            replace(evidence, evidence_digest="f" * 64)

    def test_evidence_privately_snapshots_contract(self) -> None:
        evidence = _small_evidence()
        self.assertEqual(evidence.contract, _CONTRACT)
        self.assertIsNot(evidence.contract, _CONTRACT)

    def test_rejects_contract_and_digest_drift(self) -> None:
        for field_name, value in (
            ("contract_digest", "0" * 64),
            ("corpus_digest", "0" * 64),
            ("fixture_digest", "0" * 64),
            ("path_config_digest", "0" * 64),
            ("environment_digest", "0" * 64),
            ("worker_protocol_digest", "0" * 64),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    _small_evidence(**{field_name: value})

    def test_rejects_path_config_index_kind_and_environment_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(
                execution_path=_FTS5,
                path_config_digest=_CONTRACT.fallback_path_config_digest,
            )
        with self.assertRaises(ValueError):
            _small_evidence(execution_path=_FTS5, actual_index_kind="GRAM_FALLBACK")
        with self.assertRaises(ValueError):
            _small_evidence(
                execution_path=_FALLBACK,
                environment=_environment(True),
                environment_digest=benchmark_environment_digest(
                    _environment(True)
                ),
            )
        with self.assertRaises(ValueError):
            _small_evidence(rss_scope="other-scope")
        with self.assertRaises(ValueError):
            _small_evidence(rss_unit="mebibytes")

    def test_rejects_count_identity_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(corpus_record_count=41)
        with self.assertRaises(ValueError):
            _small_evidence(fixture_record_count=41)
        with self.assertRaises(ValueError):
            _small_evidence(record_count=41)
        with self.assertRaises(ValueError):
            _small_evidence(fixture_path="/tmp/other/fixture.jsonl")

    def test_rejects_bool_as_int_and_negative_or_nonfinite_scalars(self) -> None:
        for field_name in (
            "child_pid",
            "generation",
            "record_count",
            "migration_elapsed_ns",
            "peak_rss_bytes",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises((TypeError, ValueError)):
                    _small_evidence(**{field_name: True})
        with self.assertRaises(ValueError):
            _small_evidence(migration_elapsed_ns=-1)
        with self.assertRaises(ValueError):
            _small_evidence(peak_rss_bytes=-1)
        with self.assertRaises(ValueError):
            _small_evidence(peak_rss_bytes=0)

    def test_rejects_rss_sample_invariants(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(
                rss_start_bytes=50 * 1024 * 1024,
                rss_terminal_bytes=40 * 1024 * 1024,
            )
        with self.assertRaises(ValueError):
            _small_evidence(
                peak_rss_bytes=30 * 1024 * 1024,
                rss_terminal_bytes=40 * 1024 * 1024,
            )

    def test_rejects_child_exit_and_process_proof_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(child_exit_code=1)
        with self.assertRaises(ValueError):
            _small_evidence(child_pid=0)
        with self.assertRaises(ValueError):
            _small_evidence(reopen_phase="PREPARED")
        with self.assertRaises(ValueError):
            _small_evidence(reopen_action="ROLLED_BACK")

    def test_rejects_reopen_health_proof_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(reopen_health_healthy=False)
        with self.assertRaises(ValueError):
            _small_evidence(
                reopen_health_index_kind="GRAM_FALLBACK",
                execution_path=_FTS5,
            )
        with self.assertRaises(ValueError):
            _small_evidence(reopen_health_record_count=41)
        with self.assertRaises(ValueError):
            _small_evidence(reopen_health_exact_available=False)

    def test_rejects_query_proof_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(exact_proof_result_count=0)
        with self.assertRaises(ValueError):
            _small_evidence(exact_proof_winner_matched=False)
        with self.assertRaises(ValueError):
            _small_evidence(candidate_proof_count=0)
        with self.assertRaises(ValueError):
            _small_evidence(candidate_proof_available=False)
        with self.assertRaises(ValueError):
            _small_evidence(candidate_proof_budget=999)
        for field_name in (
            "reopen_health_record_count",
            "exact_proof_result_count",
            "candidate_proof_count",
            "candidate_proof_budget",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(TypeError):
                    _small_evidence(**{field_name: True})
        with self.assertRaises(ValueError):
            _small_evidence(
                candidate_proof_index_kind="GRAM_FALLBACK",
                execution_path=_FTS5,
            )

    def test_real_evidence_requires_contract_corpus_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "corpus digest"):
            _small_evidence(
                execution_path=_FTS5,
                test_mode=False,
                count=100_000,
                corpus_digest="0" * 64,
            )

    def test_evidence_carries_no_query_or_record_bodies(self) -> None:
        evidence = _small_evidence()
        marker = "システムは条目を処理して結果を保存しまし"
        self.assertNotIn(marker, repr(evidence))
        payload = _evidence_payload(evidence)
        self.assertNotIn("source_raw", str(payload))
        self.assertNotIn("target_raw", str(payload))
        self.assertNotIn("query_raw", str(payload))

    def test_rejects_missing_or_wrong_type_artifact_snapshot(self) -> None:
        with self.assertRaises(TypeError):
            _small_evidence(artifact_snapshot=None)
        with self.assertRaisesRegex(TypeError, "artifact snapshot"):
            _small_evidence(artifact_snapshot=_artifact_snapshot().sidecar_digest)
        with self.assertRaisesRegex(ValueError, "sidecar digest"):
            _small_evidence(
                artifact_snapshot=_artifact_snapshot(
                    sidecar_digest="not-a-digest",
                )
            )
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            _small_evidence(
                artifact_snapshot=_artifact_snapshot(
                    manifest_digest="not-a-digest",
                )
            )

    def test_rejects_forged_artifact_identity_scalars(self) -> None:
        for field_name in ("device", "inode", "size", "mtime_ns"):
            with self.subTest(field_name=field_name):
                facts = {
                    "device": 1,
                    "inode": 2,
                    "size": 3,
                    "mtime_ns": 4,
                }
                facts[field_name] = True
                with self.assertRaises(TypeError):
                    ArtifactFileIdentity(**facts)


class ProcessEvidencePayloadTests(unittest.TestCase):
    def _payload(self) -> dict[str, object]:
        return _evidence_payload(_small_evidence())

    def test_payload_round_trips(self) -> None:
        evidence = evidence_from_payload(self._payload())
        self.assertEqual(evidence, _small_evidence())
        self.assertEqual(evidence.evidence_digest, _small_evidence().evidence_digest)

    def test_rejects_unknown_and_missing_fields(self) -> None:
        payload = self._payload()
        payload["bogus"] = 1
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        del payload["child_pid"]
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        del payload["artifact_snapshot"]
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)

    def test_rejects_forged_artifact_snapshot_payload(self) -> None:
        payload = self._payload()
        snapshot = payload["artifact_snapshot"]
        assert isinstance(snapshot, dict)
        snapshot["sidecar_digest"] = "not-a-digest"
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        snapshot = payload["artifact_snapshot"]
        assert isinstance(snapshot, dict)
        identity = snapshot["sidecar_identity"]
        assert isinstance(identity, dict)
        identity["device"] = True
        with self.assertRaises((TypeError, ValueError)):
            evidence_from_payload(payload)

    def test_artifact_snapshot_payload_round_trips(self) -> None:
        snapshot = _artifact_snapshot()
        payload = artifact_snapshot_to_payload(snapshot)
        from tm_benchmark_process import artifact_snapshot_from_payload

        self.assertEqual(artifact_snapshot_from_payload(payload), snapshot)
        self.assertEqual(
            artifact_snapshot_digest(snapshot),
            artifact_snapshot_digest(artifact_snapshot_from_payload(payload)),
        )

    def test_rejects_duplicate_json_keys_in_stdout(self) -> None:
        with self.assertRaises(ValueError):
            _evidence_from_stdout(
                '{"schema_version": "x", "schema_version": "y"}'
            )

    def test_rejects_non_finite_json_number_in_stdout(self) -> None:
        payload_json = _canonical_json(self._payload())
        mutated = payload_json.replace(
            f'"child_pid":{424242}',
            '"child_pid":1e999',
        )
        self.assertNotEqual(mutated, payload_json)
        with self.assertRaises(ValueError):
            _evidence_from_stdout(mutated)

    def test_rejects_bool_for_int_field_in_payload(self) -> None:
        payload = self._payload()
        payload["child_pid"] = True
        with self.assertRaises(TypeError):
            evidence_from_payload(payload)

    def test_rejects_extra_stdout_after_payload(self) -> None:
        with self.assertRaises(ValueError):
            _evidence_from_stdout(
                _canonical_json(self._payload()) + "\n" + '{"extra": true}'
            )


class ProcessRunnerTests(unittest.TestCase):
    def _run(
        self,
        execution_path: BenchmarkExecutionPath,
        *,
        run_root: Path,
        **kwargs: Any,
    ) -> TMBenchmarkProcessEvidence:
        return run_process_migration_evidence(
            contract_path=_ROOT / "benchmark_tm_contract.json",
            execution_path=execution_path,
            run_root=run_root,
            test_mode=True,
            test_record_count=40,
            timeout_seconds=120.0,
            **kwargs,
        )

    def test_fast_path_real_subprocess_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._run(_FTS5, run_root=root)
            self.assertNotEqual(evidence.child_pid, os.getpid())
            self.assertEqual(evidence.actual_index_kind, "FTS5_TRIGRAM")
            self.assertEqual(evidence.reopen_health_index_kind, "FTS5_TRIGRAM")
            self.assertEqual(evidence.candidate_proof_index_kind, "FTS5_TRIGRAM")
            self.assertEqual(evidence.record_count, 40)
            self.assertEqual(evidence.generation, 0)
            self.assertGreaterEqual(evidence.migration_elapsed_ns, 0)
            self.assertGreater(evidence.peak_rss_bytes, 0)
            self.assertEqual(evidence.rss_unit, "bytes")
            self.assertEqual(evidence.rss_scope, BENCHMARK_RSS_SCOPE)
            self.assertGreaterEqual(
                evidence.rss_terminal_bytes,
                evidence.rss_start_bytes,
            )
            self.assertEqual(
                dict(evidence.environment)["fts5_enabled"],
                "true",
            )
            self.assertEqual(
                dict(evidence.environment)["rss_raw_unit"],
                "kib" if sys.platform.startswith("linux") else "bytes",
            )
            self.assertEqual(
                evidence.recompute_evidence_digest(),
                evidence.evidence_digest,
            )
            self.assertFalse(evidence.final_evidence)
            fixture = Path(evidence.fixture_path)
            self.assertTrue(fixture.is_file())
            self.assertEqual(
                evidence.fixture_digest,
                hashlib.sha256(fixture.read_bytes()).hexdigest(),
            )
            names = sorted(path.name for path in root.iterdir())
            self.assertIn("fixture.jsonl", names)
            self.assertIn("fixture.jsonl.sqlite3", names)
            self.assertIn("fixture.jsonl.localcat-snapshot.json", names)
            self.assertTrue(
                any("activation-journal" in name for name in names)
            )
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            manifest = Path(evidence.fixture_path + ".localcat-snapshot.json")
            snapshot = evidence.artifact_snapshot
            self.assertEqual(
                snapshot.sidecar_digest,
                hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                snapshot.manifest_digest,
                hashlib.sha256(manifest.read_bytes()).hexdigest(),
            )
            self.assertRegex(snapshot.family_digest, r"[0-9a-f]{64}\Z")
            sidecar_stat = sidecar.lstat()
            self.assertEqual(snapshot.sidecar_identity.device, sidecar_stat.st_dev)
            self.assertEqual(snapshot.sidecar_identity.inode, sidecar_stat.st_ino)
            self.assertEqual(snapshot.sidecar_identity.size, sidecar_stat.st_size)
            self.assertEqual(
                snapshot.sidecar_identity.mtime_ns,
                sidecar_stat.st_mtime_ns,
            )
            self.assertEqual(snapshot.manifest_identity.inode, manifest.lstat().st_ino)

    def test_run_root_must_be_closed_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "foreign.txt"
            foreign.write_text("foreign", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be empty"):
                self._run(_FTS5, run_root=root)
            self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")

    def test_provided_fixture_must_be_single_link_and_sole_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.jsonl"
            fixture.write_text('{"source":"x","target":"y"}\n', encoding="utf-8")
            extra = root / "extra.txt"
            extra.write_text("foreign", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sole run-root entry"):
                self._run(_FTS5, run_root=root, fixture_path=fixture)
            self.assertEqual(extra.read_text(encoding="utf-8"), "foreign")

    def test_fallback_path_real_subprocess_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._run(_FALLBACK, run_root=root)
            self.assertNotEqual(evidence.child_pid, os.getpid())
            self.assertEqual(evidence.actual_index_kind, "GRAM_FALLBACK")
            self.assertEqual(evidence.reopen_health_index_kind, "GRAM_FALLBACK")
            self.assertEqual(
                evidence.candidate_proof_index_kind,
                "GRAM_FALLBACK",
            )
            self.assertEqual(
                dict(evidence.environment)["fts5_enabled"],
                "false",
            )
            self.assertEqual(evidence.record_count, 40)

    def test_each_run_spawns_a_distinct_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = self._run(_FTS5, run_root=first_root)
            second = self._run(_FTS5, run_root=second_root)
            self.assertNotEqual(first.child_pid, second.child_pid)
            self.assertNotEqual(first.child_pid, os.getpid())
            self.assertNotEqual(second.child_pid, os.getpid())

    def test_fixture_is_pre_generated_before_child_and_never_charged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_path = root / "fixture.jsonl"
            self.assertFalse(fixture_path.exists())
            evidence = self._run(_FTS5, run_root=root)
            fixture_bytes = fixture_path.read_bytes()
            self.assertEqual(
                evidence.fixture_digest,
                hashlib.sha256(fixture_bytes).hexdigest(),
            )
            expected_rows = tuple(
                iter_corpus_records(
                    seed=_CONTRACT.corpus_seed,
                    record_count=40,
                )
            )
            self.assertEqual(len(expected_rows), 40)

    def test_provided_fixture_must_match_requested_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "fixture.jsonl"
            foreign.write_text('{"source":"foreign","target":"x"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                self._run(_FTS5, run_root=root, fixture_path=foreign)

    def test_test_mode_requires_small_explicit_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "test record count"):
                run_process_migration_evidence(
                    contract_path=_ROOT / "benchmark_tm_contract.json",
                    execution_path=_FTS5,
                    run_root=root,
                    test_mode=True,
                )
            with self.assertRaisesRegex(ValueError, "below the real"):
                run_process_migration_evidence(
                    contract_path=_ROOT / "benchmark_tm_contract.json",
                    execution_path=_FTS5,
                    run_root=root,
                    test_mode=True,
                    test_record_count=100_000,
                )

    def test_runner_rejects_child_failure_stderr_noise_and_extra_stdout(
        self,
    ) -> None:
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr='{"error_code": "PROCESS.X"}\n',
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "tm_benchmark_process.subprocess.run",
                return_value=fake,
            ):
                with self.assertRaises(ProcessEvidenceError) as raised:
                    self._run(_FTS5, run_root=root)
            self.assertEqual(raised.exception.error_code, "PROCESS.X")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            noise = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="noise",
            )
            with patch(
                "tm_benchmark_process.subprocess.run",
                return_value=noise,
            ):
                with self.assertRaises(ProcessEvidenceError) as raised:
                    self._run(_FTS5, run_root=root)
            self.assertEqual(
                raised.exception.error_code,
                "PROCESS.CHILD_STDERR_NOISE",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extra = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"x": 1}\n{"y": 2}\n',
                stderr="",
            )
            with patch(
                "tm_benchmark_process.subprocess.run",
                return_value=extra,
            ):
                with self.assertRaises(ProcessEvidenceError) as raised:
                    self._run(_FTS5, run_root=root)
            self.assertEqual(
                raised.exception.error_code,
                "PROCESS.EVIDENCE_INVALID",
            )


class WorkerProtocolTests(unittest.TestCase):
    def _request_json(
        self,
        *,
        fixture_path: Path,
        run_root: Path,
        execution_path: BenchmarkExecutionPath,
        **overrides: Any,
    ) -> str:
        contract_digest = benchmark_contract_digest(_CONTRACT)
        corpus_digest = "a" * 64
        fixture_digest = "b" * 64
        protocol_digest = worker_protocol_digest(
            contract_digest=contract_digest,
            corpus_digest=corpus_digest,
            corpus_record_count=4,
            fixture_digest=fixture_digest,
            fixture_path=str(fixture_path),
            fixture_record_count=4,
            run_root=str(run_root),
            execution_path=execution_path,
            resource_id=_RESOURCE_ID,
            canonical_store_id=_STORE_ID,
            test_mode=True,
        )
        request: dict[str, Any] = {
            "canonical_store_id": _STORE_ID,
            "contract_digest": contract_digest,
            "contract_json": contract_to_json(_CONTRACT),
            "corpus_digest": corpus_digest,
            "corpus_record_count": 4,
            "execution_path": execution_path.value,
            "fixture_digest": fixture_digest,
            "fixture_path": str(fixture_path),
            "fixture_record_count": 4,
            "protocol": PROCESS_WORKER_PROTOCOL_VERSION,
            "protocol_digest": protocol_digest,
            "resource_id": _RESOURCE_ID,
            "run_root": str(run_root),
            "test_mode": True,
        }
        request.update(overrides)
        return _canonical_json(request)

    def _spawn(self, request_json: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tm_benchmark_process", *argv],
            input=request_json,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=_ROOT,
            check=False,
            timeout=120.0,
        )

    def test_worker_rejects_malformed_stdin(self) -> None:
        completed = self._spawn("not json", "--worker")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("error_code", completed.stderr)

    def test_worker_rejects_duplicate_request_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request_json(
                fixture_path=root / "fixture.jsonl",
                run_root=root,
                execution_path=_FTS5,
            )
            duplicated = request.replace(
                '"test_mode":true',
                '"test_mode":true,"test_mode":false',
            )
            completed = self._spawn(duplicated, "--worker")
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")

    def test_worker_rejects_unknown_request_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request_json(
                fixture_path=root / "fixture.jsonl",
                run_root=root,
                execution_path=_FTS5,
            )
            payload = json.loads(request)
            payload["bogus"] = 1
            completed = self._spawn(_canonical_json(payload), "--worker")
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")

    def test_worker_rejects_caller_protocol_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request_json(
                fixture_path=root / "fixture.jsonl",
                run_root=root,
                execution_path=_FTS5,
                protocol_digest="0" * 64,
            )
            completed = self._spawn(request, "--worker")
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("PROTOCOL_DIGEST_MISMATCH", completed.stderr)

    def test_worker_rejects_missing_fixture_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self._request_json(
                fixture_path=root / "missing.jsonl",
                run_root=root,
                execution_path=_FTS5,
            )
            completed = self._spawn(request, "--worker")
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")

    def test_worker_rejects_run_root_that_is_not_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "fixture.jsonl"
            fixture.write_text('{"source":"x","target":"y"}\n', encoding="utf-8")
            (root / "foreign.txt").write_text("foreign", encoding="utf-8")
            request = self._request_json(
                fixture_path=fixture,
                run_root=root,
                execution_path=_FTS5,
            )
            completed = self._spawn(request, "--worker")
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            self.assertIn("RUN_ROOT_NOT_CLOSED", completed.stderr)

    def test_worker_rejects_non_worker_argv(self) -> None:
        completed = self._spawn("", "--other")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")


class ModuleBoundaryTests(unittest.TestCase):
    def test_runtime_modules_never_import_process_owner(self) -> None:
        for module_name in sorted(_BANNED_RUNTIME_MODULES):
            with self.subTest(module_name=module_name):
                source_path = _ROOT / f"{module_name}.py"
                if not source_path.is_file():
                    continue
                source = source_path.read_text(encoding="utf-8")
                for match in _IMPORT_RE.finditer(source):
                    self.assertNotEqual(
                        match.group(1).split(".")[0],
                        "tm_benchmark_process",
                        f"{module_name} imports the benchmark process owner",
                    )

    def test_importing_runtime_modules_loads_no_process_owner(self) -> None:
        banned = ", ".join(repr(name) for name in sorted(_BANNED_RUNTIME_MODULES))
        code = (
            "import sys\n"
            f"modules = [{banned}]\n"
            "for name in modules:\n"
            "    __import__(name)\n"
            "loaded = {m.split('.')[0] for m in sys.modules}\n"
            "assert 'tm_benchmark_process' not in loaded, sorted(loaded)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_process_owner_imports_no_network_qt_or_feature3_modules(self) -> None:
        from tm_benchmark_process import __file__ as owner_file

        source = Path(owner_file).read_text(encoding="utf-8")
        imported = {
            match.group(1).split(".")[0]
            for match in _IMPORT_RE.finditer(source)
        }
        self.assertTrue(imported)
        stdlib = set(sys.stdlib_module_names)
        allowed = {
            "tm_benchmark",
            "tm_benchmark_latency",
            "tm_candidate_index",
            "tm_contracts",
            "tm_migration",
            "tm_sqlite_store",
            "tm_stage_sealer",
            "tm_activation_journal",
            "text_matcher",
        }
        for module in sorted(imported):
            self.assertTrue(
                module in stdlib or module in allowed,
                f"unexpected import: {module}",
            )
        forbidden_prefixes = (
            "qt_",
            "glossary",
            "parser",
            "matcher_capability",
            "matcher_validation",
            "requests",
            "urllib",
            "socket",
            "http",
        )
        for module in sorted(imported):
            self.assertFalse(
                module.startswith(forbidden_prefixes),
                f"forbidden import: {module}",
            )


if __name__ == "__main__":
    unittest.main()
