"""Exercise contract routing through the actual Gate and worker parents."""

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tm_benchmark as benchmark
import tm_benchmark_gate as gate
import tm_benchmark_process as migration
import tm_benchmark_query_process as query
import tm_gate_inputs as inputs
from tm_contracts import BenchmarkExecutionPath, contract_to_json
from tests.benchmark_worker_test_support import worker_temporary_directory


_ROOT = Path(__file__).absolute().parents[1]
_CONTRACT = "benchmark_tm_contract.json"


def _entries() -> tuple[tuple[str, bytes], ...]:
    return tuple((name, (_ROOT / name).read_bytes()) for name in benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS)


class GateContractInputTests(unittest.TestCase):
    def test_actual_gate_contract_reads_bound_frozen_bytes(self) -> None:
        entries = _entries()
        expected = benchmark.load_benchmark_contract(_ROOT / _CONTRACT)
        with inputs._open_test_frozen_input_session(entries) as session:
            reference = session.input(_CONTRACT)
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("source read")), mock.patch.object(inputs, "compose_rooted_source_authority", side_effect=AssertionError("source composition")):
                self.assertEqual(gate._load_contract(None, _input_session=session, _contract_input=reference), expected)
                self.assertEqual(gate._load_contract(None, _input_session=session), expected)
            self.assertEqual(session.consumed_ids, (_CONTRACT,))

    def test_migration_parent_reaches_actual_request_transport_with_frozen_inputs(self) -> None:
        expected_fingerprint = benchmark.benchmark_implementation_fingerprint(_ROOT)
        for execution_path in BenchmarkExecutionPath:
            with self.subTest(path=execution_path), tempfile.TemporaryDirectory() as directory:
                with inputs._open_test_frozen_input_session(_entries()) as session:
                    with mock.patch.object(migration, "_run_worker_child", side_effect=OSError("stop at real child transport")) as child, mock.patch.object(inputs, "compose_rooted_source_authority", side_effect=AssertionError("source composition")):
                        with self.assertRaisesRegex(migration.ProcessEvidenceError, "PROCESS.CHILD_SPAWN_FAILED"):
                            migration.run_process_migration_evidence(
                                contract_path=None, execution_path=execution_path,
                                run_root=Path(directory), test_mode=True, test_record_count=12,
                                _input_session=session, _contract_input=session.input(_CONTRACT),
                            )
                    child.assert_called_once()
                    request = json.loads(child.call_args.args[1])
                    self.assertEqual(request["implementation_fingerprint"], expected_fingerprint)
                    self.assertEqual(request["contract_digest"], gate.benchmark_contract_digest(gate._load_contract(None, _input_session=session)))
                    self.assertEqual(request["fixture_record_count"], 12)
                    self.assertTrue(set(benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS).issubset(session.consumed_ids))

    def test_reference_rejects_foreign_owner_bad_id_missing_and_malformed_bytes(self) -> None:
        with inputs._open_test_frozen_input_session(_entries()) as session, inputs._open_test_frozen_input_session(_entries()) as foreign:
            with self.assertRaises(gate.BenchmarkGateDError):
                gate._load_contract(None, _input_session=session, _contract_input=foreign.input(_CONTRACT))
            with self.assertRaises(ValueError):
                session.input("../benchmark_tm_contract.json")
            with self.assertRaises(gate.BenchmarkGateDError):
                gate._load_contract(None, _input_session=session, _contract_input=session.input("missing.json"))
        for entries in ((), ((_CONTRACT, b"["),)):
            with self.subTest(entries=entries), inputs._open_test_frozen_input_session(entries) as session:
                with self.assertRaises(gate.BenchmarkGateDError):
                    gate._load_contract(None, _input_session=session)

    def test_source_diagnostic_locator_must_match_bound_reference(self) -> None:
        with inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as session:
                reference = session.input(_CONTRACT)
                self.assertEqual(gate._load_contract(_ROOT / _CONTRACT, _input_session=session, _contract_input=reference).corpus_record_count, 100_000)
                for locator in (_ROOT / "tests/fixtures/feature5_gate_a_v1.json", _ROOT.parent / _CONTRACT):
                    with self.subTest(locator=locator), self.assertRaises(gate.BenchmarkGateDError):
                        gate._load_contract(locator, _input_session=session, _contract_input=reference)
        with inputs._open_test_frozen_input_session(_entries()) as session:
            with self.assertRaises(gate.BenchmarkGateDError):
                gate._load_contract(_ROOT / _CONTRACT, _input_session=session, _contract_input=session.input(_CONTRACT))

    def test_frozen_reference_remains_provisional_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, inputs._open_test_frozen_input_session(_entries()) as session:
            reference = session.input(_CONTRACT)
            with mock.patch.object(migration, "_run_worker_child") as child:
                with self.assertRaises(TypeError):
                    migration.run_process_migration_evidence(contract_path=None, execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=Path(directory), _input_session=session, _contract_input=reference)
                child.assert_not_called()
            session._abort()
            with self.assertRaises(gate.BenchmarkGateDError):
                gate._load_contract(None, _input_session=session, _contract_input=reference)
        with inputs._compose_test_frozen_input_owner(_entries(), terminal_results=(False,)) as owner:
            with self.assertRaisesRegex(RuntimeError, "terminal"), owner.open_session() as closed:
                gate._load_contract(None, _input_session=closed)
            with self.assertRaises(gate.BenchmarkGateDError):
                gate._load_contract(None, _input_session=closed)

    def test_actual_gate_runner_passes_reference_to_real_migration_parent(self) -> None:
        from tests.test_tm_benchmark_gate import _runner_ports

        ports, _ = _runner_ports()
        ports = replace(ports, run_process_migration_evidence=migration.run_process_migration_evidence, run_query_process_evidence=query.run_query_process_evidence)
        with worker_temporary_directory() as directory, inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as session:
                with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports), mock.patch.object(migration, "_run_worker_child", side_effect=OSError("stop at actual migration transport")) as child:
                    with self.assertRaises((migration.ProcessEvidenceError, gate.BenchmarkGateDError)) as stopped:
                        gate._run_benchmark_gate_d_core(contract_path=None, work_root=Path(directory), evidence_path=Path(directory)/"evidence.json", ports=ports, test_mode=True, test_record_count=12, test_seed=1, _input_session=session, _contract_input=session.input(_CONTRACT))
                    # A stop before child activation can leave an incomplete
                    # Windows family; the owner correctly elevates cleanup.
                    self.assertIn(str(stopped.exception), ("PROCESS.CHILD_SPAWN_FAILED", "GATE_D.CLEANUP_PENDING"))
                child.assert_called_once()
                self.assertEqual(json.loads(child.call_args.args[1])["fixture_record_count"], 12)

    def test_relative_gate_run_attestation_restore_use_fresh_window_references(self) -> None:
        from tests.test_tm_benchmark_gate import _runner_ports, _base_capability_manifest, _EVALUATED_AT

        with worker_temporary_directory() as directory, inputs._compose_source_input_owner(_ROOT) as owner:
            work = Path(directory)
            ports, _ = _runner_ports()
            with owner.open_session() as run_session:
                old_reference = run_session.input(_CONTRACT)
                with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports):
                    run = gate._run_benchmark_gate_d_from_session(run_session, contract_path=None, work_root=work, evidence_path=work/"evidence.json", _contract_input=old_reference)
            with owner.open_session() as attest:
                with self.assertRaises(gate.BenchmarkGateDError):
                    gate._load_contract(None, _input_session=attest, _contract_input=old_reference)
                gate._persist_gate_d_attestation(contract_path=None, state_root=work/"attestation", base_manifest=_base_capability_manifest(), run_result=run, issued_at_utc=_EVALUATED_AT, _input_session=attest, _contract_input=attest.input(_CONTRACT))
            with owner.open_session() as restore:
                restored = gate._restore_gate_d_attestation(contract_path=None, state_root=work/"attestation", base_manifest=_base_capability_manifest(), _input_session=restore, _contract_input=restore.input(_CONTRACT))
            self.assertIs(gate._gate_d_receipt_owner(restored), owner)
            with inputs._open_test_frozen_input_session(_entries()) as provisional:
                with self.assertRaises(TypeError):
                    gate._restore_gate_d_attestation(contract_path=None, state_root=work/"attestation", base_manifest=_base_capability_manifest(), _input_session=provisional, _contract_input=provisional.input(_CONTRACT))

    def test_external_source_contract_keeps_its_distinct_bytes_and_domain(self) -> None:
        original = benchmark.load_benchmark_contract(_ROOT / _CONTRACT)
        changed = replace(original, corpus_seed=original.corpus_seed + 1)
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory)/"contract.json"
            external.write_text(contract_to_json(changed), encoding="utf-8")
            with inputs._compose_source_input_owner(_ROOT) as owner:
                owner._associate_source_path(external)
                with owner.open_session() as session:
                    reference = session.input_path(external)
                    self.assertEqual(gate._load_contract(external, _input_session=session, _contract_input=reference), changed)
                    self.assertEqual(gate._load_contract(None, _input_session=session, _contract_input=reference), changed)
                    with self.assertRaises(gate.BenchmarkGateDError):
                        gate._load_contract(_ROOT/_CONTRACT, _input_session=session, _contract_input=reference)
                with owner.open_session() as fresh:
                    self.assertEqual(gate._load_contract(external, _input_session=fresh), changed)
            run_root = Path(directory)/"run"
            run_root.mkdir()
            with mock.patch.object(migration, "_run_worker_child", side_effect=OSError("stop")) as child:
                with self.assertRaisesRegex(migration.ProcessEvidenceError, "PROCESS.CHILD_SPAWN_FAILED"):
                    migration.run_process_migration_evidence(contract_path=external, execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=run_root, test_mode=True, test_record_count=12)
            self.assertEqual(json.loads(child.call_args.args[1])["contract_digest"], gate.benchmark_contract_digest(changed))

    def test_query_parent_consumes_frozen_contract_and_fingerprint_before_transport(self) -> None:
        with worker_temporary_directory() as directory:
            evidence = migration.run_process_migration_evidence(contract_path=_ROOT/_CONTRACT, execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=Path(directory), test_mode=True, test_record_count=12)
            with inputs._open_test_frozen_input_session(_entries()) as session:
                with mock.patch.object(query, "_run_worker_child", side_effect=OSError("stop at query transport")) as child, mock.patch.object(inputs, "compose_rooted_source_authority", side_effect=AssertionError("source composition")):
                    with self.assertRaisesRegex(query.QueryProcessError, "QUERY.CHILD_SPAWN_FAILED"):
                        query.run_query_process_probe(evidence, _input_session=session)
                child.assert_called_once()
                self.assertTrue(set(benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS).issubset(session.consumed_ids))

    def test_migration_rejects_bad_or_revoked_binding_before_child(self) -> None:
        for mode in ("missing", "malformed", "foreign", "aborted", "locator"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                entries = () if mode == "missing" else ((_CONTRACT, b"["),) if mode == "malformed" else _entries()
                with inputs._open_test_frozen_input_session(entries) as session, inputs._open_test_frozen_input_session(_entries()) as foreign:
                    reference = (foreign if mode == "foreign" else session).input(_CONTRACT)
                    if mode == "aborted":
                        session._abort()
                    with mock.patch.object(migration, "_run_worker_child") as child:
                        with self.assertRaises((TypeError, ValueError, RuntimeError, KeyError)):
                            migration.run_process_migration_evidence(contract_path=_ROOT/_CONTRACT if mode == "locator" else None, execution_path=BenchmarkExecutionPath.GRAM_FALLBACK, run_root=Path(directory), test_mode=True, test_record_count=12, _input_session=session, _contract_input=reference)
                        child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
