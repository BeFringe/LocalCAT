"""Fresh worker consumer contracts; these are not frozen runtime evidence."""

from __future__ import annotations

import io
from contextlib import ExitStack
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_benchmark_worker as worker
import tm_benchmark_process as migration
import tm_benchmark_query_process as query
from tm_gate_inputs import _GateInputSession, _compose_source_input_owner, _open_source_input_session
from tm_contracts import BenchmarkExecutionPath, contract_to_json
from tm_benchmark import load_benchmark_contract, benchmark_implementation_fingerprint
from tests.benchmark_worker_test_support import worker_temporary_directory

_ROOT = Path(__file__).resolve().parents[1]


class WorkerLaunchContractTests(unittest.TestCase):
    def test_source_keeps_both_python_module_entries(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            for kind, module in (("migration", "tm_benchmark_process"), ("query", "tm_benchmark_query_process")):
                self.assertEqual(worker._worker_command(kind), (sys.executable, "-m", module, "--worker"))

    def test_frozen_migration_selects_same_executable_fixed_mode(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker._worker_command("migration"), (sys.executable, "--localcat-tm-worker", "migration"))

    def test_frozen_query_selects_same_executable_fixed_mode(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker._worker_command("query"), (sys.executable, "--localcat-tm-worker", "query"))

    def test_unknown_extra_duplicate_and_generic_modes_are_rejected(self) -> None:
        for mode in ("", "query query", "migration --skip", "-m", "tm_benchmark_process", ["query"], True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                worker._worker_command(cast(Any, mode))

    def test_frozen_without_producer_never_uses_source_subprocess(self) -> None:
        with patch.object(sys, "frozen", True, create=True), patch.object(subprocess, "run", side_effect=AssertionError("source fallback")):
            with self.assertRaises(OSError):
                worker._run_worker_child("migration", "{}", timeout_seconds=1.0, test_mode=False)

    def test_test_transport_cannot_produce_final_evidence(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(TypeError):
                worker._run_worker_child("query", "{}", timeout_seconds=1.0, test_mode=False, _test_frozen_transport=cast(Any, lambda *_args: None))

    def test_frozen_test_transport_receives_no_authority_or_release_digest(self) -> None:
        calls = []
        def transport(command, payload, timeout):
            calls.append((command, payload, timeout))
            return subprocess.CompletedProcess(command, 0, b"{}\n", b"")
        with patch.object(sys, "frozen", True, create=True):
            result = worker._run_worker_child("query", "{}", timeout_seconds=2.0, test_mode=True, _test_frozen_transport=transport)
        self.assertEqual(calls, [((sys.executable, "--localcat-tm-worker", "query"), b"{}", 2.0)])
        self.assertEqual(result.stdout, "{}\n")

    def test_windowed_result_pipe_error_is_decoded_by_core_exit_status(self) -> None:
        def transport(command, _payload, _timeout):
            return subprocess.CompletedProcess(command, 1, b'{"error_code":"QUERY.ARTIFACT_INVALID"}\n', b"")
        with patch.object(sys, "frozen", True, create=True):
            result = worker._run_worker_child("query", "{}", timeout_seconds=1.0, test_mode=True, _test_frozen_transport=transport)
        self.assertEqual(result.stdout, "")
        self.assertEqual(query._child_stderr_code(result.stderr), "QUERY.ARTIFACT_INVALID")

    def test_frozen_transport_timeout_and_invalid_results_are_fail_closed(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            def timeout(*_args):
                raise subprocess.TimeoutExpired("candidate", 1.0)
            with self.assertRaises(subprocess.TimeoutExpired):
                worker._run_worker_child("migration", "{}", timeout_seconds=1.0, test_mode=True, _test_frozen_transport=timeout)
            for result in (None, subprocess.CompletedProcess([], True, b"{}", b""), subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, b"\xff", b"")):
                with self.subTest(result=result), self.assertRaises(OSError):
                    worker._run_worker_child("query", "{}", timeout_seconds=1.0, test_mode=True, _test_frozen_transport=cast(Any, lambda *_args: result))

    def test_frozen_cli_cannot_adopt_console_streams(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError):
                worker._source_worker_pipes()
            for module in (migration, query):
                self.assertEqual(module._worker_main(["--worker"]), 1)

    def test_worker_argv_rejects_extra_duplicate_and_generic_module_selection(self) -> None:
        for module in (migration, query):
            for argv in ([], ["--worker", "--worker"], ["-m", "other"], ["--worker", "migration"], ["--query"]):
                with self.subTest(module=module.__name__, argv=argv), patch.object(sys, "stderr", io.StringIO()):
                    self.assertEqual(module._worker_main(argv), 2)


class WorkerPipeContracts(unittest.TestCase):
    def test_binary_endpoints_reject_missing_closed_and_aliased_streams(self) -> None:
        for streams in ((None, io.BytesIO(), io.BytesIO()), (io.StringIO(), io.BytesIO(), io.BytesIO())):
            with self.subTest(streams=streams), self.assertRaises((TypeError, ValueError)):
                worker._WorkerPipes(*cast(Any, streams))
        closed = io.BytesIO()
        closed.close()
        with self.assertRaises(ValueError):
            worker._WorkerPipes(closed, io.BytesIO(), io.BytesIO())
        shared = io.BytesIO()
        with self.assertRaises(ValueError):
            worker._WorkerPipes(io.BytesIO(), shared, shared)
        with self.assertRaises(ValueError):
            worker._WorkerPipes(io.BytesIO(), io.BufferedReader(io.BytesIO()))

    def test_two_pipe_windowed_failure_does_not_need_console_stderr(self) -> None:
        for module, code in ((migration, "PROCESS.REQUEST_INVALID"), (query, "QUERY.REQUEST_INVALID")):
            with self.subTest(module=module.__name__), _open_source_input_session(_ROOT) as session:
                result = io.BytesIO()
                with patch.object(sys, "stderr", None):
                    self.assertEqual(module._serve_worker(worker._WorkerPipes(io.BytesIO(b"["), result), session), 1)
                self.assertEqual(json.loads(result.getvalue()), {"error_code": code})

    def test_truncated_write_or_closed_pipe_cannot_succeed(self) -> None:
        class ZeroWriter(io.BytesIO):
            def write(self, data, /):
                return 0
        pipes = worker._WorkerPipes(io.BytesIO(), ZeroWriter())
        with self.assertRaises(OSError):
            pipes.write_result("{}")
        result = io.BytesIO()
        pipes = worker._WorkerPipes(io.BytesIO(), result)
        result.close()
        with self.assertRaises(ValueError):
            pipes.write_result("{}")

    def test_both_worker_cores_use_explicit_pipes_without_console(self) -> None:
        for module, code in ((migration, "PROCESS.REQUEST_INVALID"), (query, "QUERY.REQUEST_INVALID")):
            with self.subTest(module=module.__name__), _open_source_input_session(_ROOT) as session:
                output, errors = io.BytesIO(), io.BytesIO()
                pipes = worker._WorkerPipes(io.BytesIO(b'{"bad":'), output, errors)
                with patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
                    exit_code = module._serve_worker(pipes, session)
                self.assertEqual(exit_code, 1)
                self.assertEqual(output.getvalue(), b"")
                self.assertEqual(json.loads(errors.getvalue()), {"error_code": code})

    def test_closed_and_foreign_sessions_fail_before_request_execution(self) -> None:
        with _open_source_input_session(_ROOT) as session:
            pass
        for invalid in (session, object(), None):
            for module in (migration, query):
                output, errors = io.BytesIO(), io.BytesIO()
                pipes = worker._WorkerPipes(io.BytesIO(b"{}"), output, errors)
                self.assertEqual(module._serve_worker(pipes, invalid), 1)
                self.assertFalse(output.getvalue())


class WorkerTerminalRssTests(unittest.TestCase):
    """Synthetic RSS scheduling proves the cutoff, never benchmark qualification."""

    def _run_scheduled_peak(self, kind: str, phase: str):
        from tests.test_tm_benchmark_process import _small_evidence
        from tests.test_tm_benchmark_query_process import _probe_report, _query_evidence

        module = migration if kind == "migration" else query
        evidence = _small_evidence()
        if kind == "migration":
            initial_payload = migration.process_evidence_to_payload(evidence)
            rss_key = "peak_rss_bytes"
        elif kind == "probe":
            initial_payload = query.query_probe_to_payload(_probe_report())
            rss_key = "query_peak_rss_bytes"
        else:
            initial_payload = query.query_process_evidence_to_payload(_query_evidence())
            rss_key = "query_peak_rss_bytes"
        original_payload = dict(initial_payload)
        high_peak = 512 * 1024 * 1024 + 1
        state = {"peak": initial_payload[rss_key], "terminal": False}
        readings = []
        fingerprint = evidence.implementation_fingerprint
        request = SimpleNamespace(mode=kind, implementation_fingerprint=fingerprint, process_evidence=evidence)
        original_json = module._canonical_json
        original_terminal = _GateInputSession.terminal_reproof

        def encode(payload):
            encoded = original_json(payload)
            if phase == "result":
                state["peak"] = high_peak
            return encoded

        def final_fingerprint(_session):
            self.assertFalse(state["terminal"])
            if phase == "fingerprint":
                state["peak"] = high_peak
            return fingerprint

        def terminal(session):
            result = original_terminal(session)
            state["terminal"] = True
            if phase == "terminal":
                state["peak"] = high_peak
            return result

        def sample(*_args):
            readings.append(state["terminal"])
            return state["peak"] if kind == "migration" else (state["peak"], "bytes")

        with _open_source_input_session(_ROOT) as session, ExitStack() as stack:
            stack.enter_context(patch.object(module, "_validate_worker_request", return_value=request))
            stack.enter_context(patch.object(module, "_canonical_json", side_effect=encode))
            stack.enter_context(patch.object(module, "_input_fingerprint", side_effect=final_fingerprint))
            stack.enter_context(patch.object(_GateInputSession, "terminal_reproof", terminal))
            if kind == "migration":
                stack.enter_context(patch.object(migration, "_run_measured_lifecycle", return_value=object()))
                stack.enter_context(patch.object(migration, "_evidence_from_facts", return_value=evidence))
                stack.enter_context(patch.object(migration, "_rss_bytes", side_effect=sample))
            else:
                stack.enter_context(patch.object(query, "_run_probe" if kind == "probe" else "_run_evidence", return_value=initial_payload))
                stack.enter_context(patch.object(query, "_terminal_rss_facts", side_effect=sample))
            output, errors = io.BytesIO(), io.BytesIO()
            status = module._serve_worker(worker._WorkerPipes(io.BytesIO(b"{}"), output, errors), session)
            self.assertEqual(status, 0, errors.getvalue())
        payload = json.loads(output.getvalue())
        if kind == "migration":
            restored = migration.evidence_from_payload(payload)
            self.assertEqual(restored.migration_elapsed_ns, evidence.migration_elapsed_ns)
            self.assertEqual(restored.rss_start_bytes, evidence.rss_start_bytes)
            self.assertNotEqual(restored.evidence_digest, evidence.evidence_digest)
        else:
            payload = payload["payload"]
            restored = query.query_probe_from_payload(payload) if kind == "probe" else query.query_process_evidence_from_payload(payload)
            self.assertEqual(restored.query_rss_start_bytes, initial_payload["query_rss_start_bytes"])
        self.assertEqual(payload[rss_key], high_peak)
        self.assertEqual(payload["rss_terminal_bytes" if kind == "migration" else "query_rss_terminal_bytes"], high_peak)
        changed_fields = {
            key for key in original_payload if original_payload[key] != payload[key]
        }
        self.assertEqual(changed_fields, {
            rss_key,
            "rss_terminal_bytes" if kind == "migration" else "query_rss_terminal_bytes",
            "probe_digest" if kind == "probe" else "evidence_digest",
        })
        self.assertEqual(readings, [True], "exactly one final sample after terminal")
        return payload[rss_key]

    def test_migration_counts_final_result_fingerprint_and_terminal_peak(self) -> None:
        for phase in ("result", "fingerprint", "terminal"):
            with self.subTest(phase=phase):
                self._run_scheduled_peak("migration", phase)

    def test_probe_counts_final_result_fingerprint_and_terminal_peak(self) -> None:
        for phase in ("result", "fingerprint", "terminal"):
            with self.subTest(phase=phase):
                self._run_scheduled_peak("probe", phase)

    def test_query_evidence_counts_final_result_fingerprint_and_terminal_peak(self) -> None:
        for phase in ("result", "fingerprint", "terminal"):
            with self.subTest(phase=phase):
                self._run_scheduled_peak("evidence", phase)

    def test_only_terminal_peak_crossing_limit_reaches_existing_gate(self) -> None:
        from tm_benchmark_gate import _derive_gate_verdict

        contract = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
        peak = self._run_scheduled_peak("evidence", "terminal")
        self.assertEqual(contract.peak_rss_gate_mib, 512.0)
        self.assertEqual(_derive_gate_verdict(contract=contract, candidate_recall=1.0, exact_p95_ms=0.0, fuzzy_top10_p95_ms=0.0, migration_seconds=0.0, peak_rss_mib=peak / (1024 * 1024)), (("PEAK_RSS",), False))


class SourceSessionIntegrationTests(unittest.TestCase):
    @staticmethod
    def _migrate(root: Path, **kwargs: Any):
        return migration.run_process_migration_evidence(contract_path=_ROOT / "benchmark_tm_contract.json", execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=root, test_mode=True, test_record_count=12, **kwargs)

    def test_retained_activation_lock_is_proved_and_drift_rejected(self) -> None:
        with worker_temporary_directory() as directory:
            root = Path(directory)
            evidence = self._migrate(root)
            sidecar = Path(evidence.fixture_path + ".sqlite3")
            lock_path = root / f".{sidecar.name}.localcat-initial-activation.lock"
            original = lock_path.read_bytes()
            self.assertTrue(original)
            result = query.run_query_process_probe(evidence)
            self.assertEqual(lock_path.read_bytes(), original)
            self.assertEqual(asdict(result.artifact_pre), asdict(evidence.artifact_snapshot))
            self.assertEqual(asdict(result.artifact_post), asdict(evidence.artifact_snapshot))

            lock_path.write_bytes(original + b"tampered")
            with self.assertRaises(query.QueryProcessError) as caught:
                query.run_query_process_probe(evidence)
            self.assertEqual(caught.exception.error_code, "QUERY.ARTIFACT_BASELINE_DRIFT")

    def test_foreign_lock_is_not_accepted_as_activation_artifact(self) -> None:
        with worker_temporary_directory() as directory:
            root = Path(directory)
            evidence = self._migrate(root)
            foreign = root / "foreign.lock"
            foreign.write_bytes(b"unrelated")
            with self.assertRaises(query.QueryProcessError) as caught:
                query.run_query_process_probe(evidence)
            self.assertEqual(caught.exception.error_code, "QUERY.ARTIFACT_INVALID")
            self.assertEqual(foreign.read_bytes(), b"unrelated")

    def test_source_public_runner_closes_owned_window_before_return(self) -> None:
        observed = []
        original = migration._request_payload
        def capture(**kwargs):
            observed.append(kwargs["_input_session"])
            return original(**kwargs)
        with worker_temporary_directory() as directory, patch.object(migration, "_request_payload", side_effect=capture):
            self._migrate(Path(directory))
        self.assertEqual(len(observed), 1)
        with self.assertRaises(RuntimeError):
            observed[0].read_bytes("benchmark_tm_contract.json")

    def test_parent_terminal_failure_prevents_public_return(self) -> None:
        with worker_temporary_directory() as directory, patch.object(_GateInputSession, "terminal_reproof", side_effect=RuntimeError("terminal drift")):
            with self.assertRaisesRegex(RuntimeError, "terminal drift"):
                self._migrate(Path(directory))

    def test_stale_and_foreign_input_domains_are_rejected_before_spawn(self) -> None:
        with _open_source_input_session(_ROOT) as stale:
            pass
        with worker_temporary_directory() as directory, patch.object(migration, "_run_worker_child", side_effect=AssertionError("spawn before proof")):
            with self.assertRaises(RuntimeError):
                self._migrate(Path(directory), _input_session=stale)
            with _open_source_input_session(Path(directory)) as foreign:
                with self.assertRaises(ValueError):
                    self._migrate(Path(directory), _input_session=foreign)

    def test_borrowed_session_cannot_produce_final_worker_evidence(self) -> None:
        from platform_source_authority import compose_rooted_source_authority
        from tm_gate_inputs import _open_gate_input_session
        authority = compose_rooted_source_authority(_ROOT)
        try:
            with worker_temporary_directory() as directory, _open_gate_input_session(authority) as session:
                with self.assertRaises(TypeError):
                    migration.run_process_migration_evidence(contract_path=_ROOT / "benchmark_tm_contract.json", execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=Path(directory), _input_session=session)
        finally:
            authority.close()

    def test_parent_rejects_epoch_drift_after_both_real_children(self) -> None:
        fingerprint = benchmark_implementation_fingerprint(_ROOT)
        with worker_temporary_directory() as directory:
            with patch.object(migration, "_input_fingerprint", side_effect=[fingerprint, "f" * 64]):
                with self.assertRaises(migration.ProcessEvidenceError) as error:
                    self._migrate(Path(directory))
            self.assertEqual(error.exception.error_code, "PROCESS.IMPLEMENTATION_CHANGED")
        with worker_temporary_directory() as directory:
            evidence = self._migrate(Path(directory))
            with patch.object(query, "_input_fingerprint", side_effect=[fingerprint, "f" * 64]):
                with self.assertRaises(query.QueryProcessError) as error:
                    query.run_query_process_probe(evidence)
            self.assertEqual(error.exception.error_code, "QUERY.IMPLEMENTATION_CHANGED")

    def test_frozen_parent_error_transport_keeps_owner_classification(self) -> None:
        with worker_temporary_directory() as directory:
            evidence = self._migrate(Path(directory))
            cases = (
                (1, b'{"error_code":"QUERY.ARTIFACT_INVALID"}', b"", "QUERY.ARTIFACT_INVALID"),
                (0, b"{", b"", "QUERY.RESPONSE_INVALID"),
                (0, b"{} trailing", b"", "QUERY.RESPONSE_INVALID"),
                (0, b"{}", b"noise", "QUERY.CHILD_STDERR_NOISE"),
            )
            for code, result, noise, expected in cases:
                def transport(command, _payload, _timeout):
                    return subprocess.CompletedProcess(command, code, result, noise)
                with self.subTest(expected=expected), _open_source_input_session(_ROOT) as session:
                    with patch.object(sys, "frozen", True, create=True), patch.object(subprocess, "run", side_effect=AssertionError("source fallback")):
                        with self.assertRaises(query.QueryProcessError) as error:
                            query.run_query_process_probe(evidence, _input_session=session, _test_frozen_transport=transport)
                    self.assertEqual(error.exception.error_code, expected)

    def test_query_core_proves_its_own_session_and_closes_before_writing(self) -> None:
        with worker_temporary_directory() as directory:
            evidence = self._migrate(Path(directory))
            request = query._request_payload(mode="probe", process_evidence=evidence, run_root=evidence.run_root, fixture_path=evidence.fixture_path)
            with _open_source_input_session(_ROOT) as session:
                case = self
                class TerminalWriter(io.BytesIO):
                    def write(self, data, /):
                        with case.assertRaises(RuntimeError):
                            session.read_bytes("benchmark_tm_contract.json")
                        return super().write(data)
                result, errors = TerminalWriter(), io.BytesIO()
                pipes = worker._WorkerPipes(io.BytesIO(query._canonical_json(request).encode()), result, errors)
                with patch.object(query, "benchmark_implementation_fingerprint", side_effect=AssertionError("legacy fingerprint")), patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
                    self.assertEqual(query._serve_worker(pipes, session), 0, errors.getvalue())
                self.assertEqual(json.loads(result.getvalue())["query_pid"], os.getpid())

    def test_query_terminal_failure_suppresses_even_constructed_result(self) -> None:
        with worker_temporary_directory() as directory:
            evidence = self._migrate(Path(directory))
            request = query._request_payload(mode="probe", process_evidence=evidence, run_root=evidence.run_root, fixture_path=evidence.fixture_path)
            with _open_source_input_session(_ROOT) as session:
                result, errors = io.BytesIO(), io.BytesIO()
                pipes = worker._WorkerPipes(io.BytesIO(query._canonical_json(request).encode()), result, errors)
                with patch.object(_GateInputSession, "terminal_reproof", side_effect=RuntimeError("terminal drift")):
                    self.assertEqual(query._serve_worker(pipes, session), 1)
                self.assertFalse(result.getvalue())
                self.assertEqual(json.loads(errors.getvalue()), {"error_code": "QUERY.CHILD_FAILED"})

    def test_migration_core_closes_child_window_before_result_write(self) -> None:
        captured = []
        def capture(_kind, request, **_kwargs):
            captured.append(request)
            raise OSError("capture test request")
        with worker_temporary_directory() as directory:
            with patch.object(migration, "_run_worker_child", side_effect=capture), self.assertRaises(migration.ProcessEvidenceError):
                self._migrate(Path(directory))
            with _open_source_input_session(_ROOT) as session:
                case = self
                class TerminalWriter(io.BytesIO):
                    def write(self, data, /):
                        with case.assertRaises(RuntimeError):
                            session.read_bytes("benchmark_tm_contract.json")
                        return super().write(data)
                result, errors = TerminalWriter(), io.BytesIO()
                with patch.object(migration, "benchmark_implementation_fingerprint", side_effect=AssertionError("legacy fingerprint")), patch.object(migration, "load_benchmark_contract", side_effect=AssertionError("legacy contract")):
                    self.assertEqual(migration._serve_worker(worker._WorkerPipes(io.BytesIO(captured[0].encode()), result, errors), session), 0, errors.getvalue())
                self.assertEqual(json.loads(result.getvalue())["child_pid"], os.getpid())
    def test_real_two_path_children_share_parent_session_and_keep_business_assets_outside_it(self) -> None:
        for path in BenchmarkExecutionPath:
            with self.subTest(path=path), worker_temporary_directory() as directory:
                with _compose_source_input_owner(_ROOT) as owner, owner.open_session() as session:
                    with patch.object(migration, "benchmark_implementation_fingerprint", side_effect=AssertionError("Path fingerprint")), patch.object(migration, "load_benchmark_contract", side_effect=AssertionError("Path contract")), patch.object(query, "benchmark_implementation_fingerprint", side_effect=AssertionError("Path fingerprint")):
                        evidence = migration.run_process_migration_evidence(contract_path=_ROOT / "benchmark_tm_contract.json", execution_path=path, run_root=Path(directory), test_mode=True, test_record_count=12, _input_session=session)
                        result = query.run_query_process_probe(evidence, _input_session=session)
                    self.assertNotEqual(evidence.child_pid, os.getpid())
                    self.assertNotEqual(result.query_child_pid, evidence.child_pid)
                    self.assertIn("benchmark_tm_contract.json", session.consumed_ids)
                    self.assertFalse(any("fixture.jsonl" in value for value in session.consumed_ids))
                    self.assertGreaterEqual(evidence.rss_terminal_bytes, evidence.rss_start_bytes)
                    self.assertEqual(evidence.peak_rss_bytes, evidence.rss_terminal_bytes)

    def test_frozen_public_entries_fail_before_source_input_or_spawn(self) -> None:
        with worker_temporary_directory() as directory, patch.object(sys, "frozen", True, create=True), patch.object(subprocess, "run", side_effect=AssertionError("source fallback")):
            with self.assertRaises(RuntimeError):
                migration.run_process_migration_evidence(contract_path=_ROOT / "benchmark_tm_contract.json", execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=Path(directory), test_mode=True, test_record_count=12)

    def test_explicit_external_test_contract_keeps_legacy_seed_variation(self) -> None:
        with worker_temporary_directory() as directory:
            root = Path(directory)
            contract = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
            changed = replace(contract, corpus_seed=contract.corpus_seed + 1)
            external = root / "external-contract.json"
            external.write_text(contract_to_json(changed), encoding="utf-8")
            run_root = root / "run"
            run_root.mkdir()
            evidence = migration.run_process_migration_evidence(contract_path=external, execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=run_root, test_mode=True, test_record_count=12)
            self.assertEqual(evidence.contract, changed)
            self.assertTrue(evidence.test_mode)
            result = query.run_query_process_probe(evidence)
            self.assertNotEqual(result.query_child_pid, evidence.child_pid)
            with self.assertRaises(query.QueryProcessError):
                query.run_query_process_evidence(evidence)


if __name__ == "__main__":
    unittest.main()
