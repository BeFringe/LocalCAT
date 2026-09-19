"""Core trusted-input consumption and lifetime regression tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import os
import shutil
import subprocess
import gc
import weakref
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest import mock

from platform_source_authority import compose_rooted_source_authority
from tm_gate_inputs import (
    _GateInputSession,
    _open_gate_input_session,
    _read_input_bytes,
    _relative_input_id,
)


_ROOT = Path(__file__).absolute().parents[1]


class TrustedGateInputSessionTests(unittest.TestCase):
    def test_ids_are_exact_canonical_bundle_relative_ids(self) -> None:
        self.assertEqual(_relative_input_id("tests/fixtures/a.json"), "tests/fixtures/a.json")
        for value in ("", ".", "../a", "a/../b", "a//b", "a/./b", "/a", "C:/a", "a\\b", "a\x00b", "a/", "a:stream", "a ", "CON", "a./b"):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                _relative_input_id(value)

    def test_fake_authority_and_direct_session_construction_are_rejected(self) -> None:
        for authority in (object(), {"x": b"trusted"}, lambda _name: b"trusted"):
            with self.subTest(authority=type(authority)), self.assertRaises(TypeError):
                with _open_gate_input_session(authority):
                    self.fail("foreign authority accepted")
        with self.assertRaises(TypeError):
            cast(Any, _GateInputSession)()

    def test_exact_bytes_lifetime_thread_and_nonclosing_borrow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_bytes(b"exact\r\nbytes")
            authority = compose_rooted_source_authority(root)
            try:
                with _open_gate_input_session(authority) as session:
                    reference = session.input("input.txt")
                    with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("pathname read")):
                        self.assertEqual(_read_input_bytes(reference), b"exact\r\nbytes")
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        with self.assertRaises(RuntimeError):
                            executor.submit(session.read_bytes, "input.txt").result()
                    with self.assertRaises(RuntimeError):
                        with _open_gate_input_session(authority):
                            pass
                with self.assertRaises(RuntimeError):
                    session.read_bytes("input.txt")
                with self.assertRaises(RuntimeError):
                    _read_input_bytes(reference)
                authority.reprove()
            finally:
                authority.close()

    def test_terminal_reproof_failure_prevents_successful_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_bytes(b"exact")
            with compose_rooted_source_authority(root) as authority:
                session: _GateInputSession | None = None
                with self.assertRaises(RuntimeError):
                    with _open_gate_input_session(authority) as session:
                        session.read_bytes("input.txt")
                        with mock.patch.object(type(authority), "_reprove_file_now", return_value=False):
                            session.terminal_reproof()
                assert session is not None
                with self.assertRaises(RuntimeError):
                    session.read_bytes("input.txt")

    def test_closed_authority_does_not_issue_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            authority = compose_rooted_source_authority(Path(temporary))
            authority.close()
            with self.assertRaises(Exception):
                with _open_gate_input_session(authority):
                    pass

    def test_real_source_mutation_is_blocked_or_fails_terminal_reproof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "input.txt"
            path.write_bytes(b"original")
            blocked = False
            with compose_rooted_source_authority(root) as authority:
                try:
                    with _open_gate_input_session(authority) as session:
                        self.assertEqual(session.read_bytes("input.txt"), b"original")
                        try:
                            path.write_bytes(b"changed")
                        except PermissionError:
                            blocked = True
                except RuntimeError:
                    self.assertFalse(blocked)
                else:
                    self.assertTrue(blocked, "changed source escaped terminal reproof")
                    self.assertEqual(path.read_bytes(), b"original")

    def test_fake_foreign_and_wrong_id_source_reads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_bytes(b"same")
            (root / "b.txt").write_bytes(b"same")
            authority = compose_rooted_source_authority(root)
            other = compose_rooted_source_authority(root)
            with authority, other:
                foreign = other.bind_path(root / "a.txt")
                wrong_id = authority.bind_path(root / "b.txt")
                for source in (SimpleNamespace(content=b"same"), foreign, wrong_id):
                    with self.subTest(source=type(source)), _open_gate_input_session(authority) as session:
                        with mock.patch.object(type(authority), "bind_path", return_value=source):
                            with self.assertRaises((TypeError, ValueError)):
                                session.read_bytes("a.txt")

    def test_frozen_route_cannot_fall_back_to_source_path(self) -> None:
        import tm_benchmark

        with mock.patch("sys.frozen", True, create=True):
            with self.assertRaises(RuntimeError):
                _read_input_bytes(_ROOT / "benchmark_tm_contract.json")
            with self.assertRaises(RuntimeError):
                tm_benchmark._stable_benchmark_source_digest(_ROOT / "tm_contracts.py")
            with compose_rooted_source_authority(_ROOT) as authority:
                with self.assertRaises(RuntimeError):
                    with _open_gate_input_session(authority):
                        pass

    def test_exception_revokes_references_and_releases_borrowed_window(self) -> None:
        with compose_rooted_source_authority(_ROOT) as authority:
            reference = None
            with self.assertRaisesRegex(ValueError, "abort"):
                with _open_gate_input_session(authority) as session:
                    reference = session.input("benchmark_tm_contract.json")
                    raise ValueError("abort")
            with self.assertRaises(RuntimeError):
                assert reference is not None
                _read_input_bytes(reference)
            with _open_gate_input_session(authority) as successor:
                self.assertTrue(successor.read_bytes("benchmark_tm_contract.json"))

    def test_foreign_roots_reference_requires_explicit_source_association(self) -> None:
        from tm_gate_inputs import _source_validation_inputs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "roots.json").write_text("{}", encoding="utf-8")
            with compose_rooted_source_authority(_ROOT) as authority, compose_rooted_source_authority(root) as foreign:
                with _open_gate_input_session(authority) as session, _open_gate_input_session(foreign) as other:
                    with self.assertRaises(ValueError):
                        session._require_bound_input(other.input("roots.json"))
            with _source_validation_inputs(_ROOT, root / "roots.json") as (session, roots):
                session._require_bound_input(roots)
                self.assertEqual(_read_input_bytes(roots), b"{}")

    def test_path_locator_rejects_wrong_domain_noncanonical_and_closed_session(self) -> None:
        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                for path in (Path("relative.json"), _ROOT.parent / "foreign.json", _ROOT / ".." / "foreign.json"):
                    with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                        session.input_path(path)
            with self.assertRaises(RuntimeError):
                session.input_path(_ROOT / "benchmark_tm_contract.json")

    def test_source_owner_retains_one_authority_across_sequential_production_windows(self) -> None:
        from tm_gate_inputs import _compose_source_input_owner, _require_production_session

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "input.txt").write_bytes(b"first epoch")
            with _compose_source_input_owner(root) as owner:
                with owner.open_session() as first:
                    _require_production_session(first)
                    self.assertEqual(first.read_bytes("input.txt"), b"first epoch")
                with owner.open_session() as second:
                    _require_production_session(second)
                    self.assertEqual(second.read_bytes("input.txt"), b"first epoch")
                    with self.assertRaises(RuntimeError):
                        first.read_bytes("input.txt")
            with self.assertRaises(RuntimeError):
                with owner.open_session():
                    pass


class TrustedGateConsumptionTests(unittest.TestCase):
    def test_gate_a_and_matcher_consume_retained_json_and_txt(self) -> None:
        import matcher_validation
        import tm_gate_a

        now = datetime.now(timezone.utc).replace(microsecond=0)
        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("pathname text")), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("pathname bytes")):
                    report = tm_gate_a._recompute_gate_a_from_session(session)
                    release = matcher_validation._recompute_matcher_validation_from_session(
                        session, generated_at_utc=now,
                        valid_until_utc=now + timedelta(hours=1), include_full=True,
                    )
                self.assertEqual(len(report.components), 3)
                self.assertIsNotNone(release.manifest)
                self.assertIn("tests/fixtures/unicode-16.0.0-WordBreakTest.txt", session.consumed_ids)

    def test_gate_c_consumes_same_fixture_bytes_for_all_observers(self) -> None:
        import tm_retrieval_validation

        now = datetime.now(timezone.utc).replace(microsecond=0)
        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("pathname text")), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("pathname bytes")):
                    release = tm_retrieval_validation._recompute_retrieval_validation_from_session(
                        session, generated_at_utc=now,
                        valid_until_utc=now + timedelta(hours=1),
                    )
                self.assertIsNotNone(release.manifest)
                self.assertIn("tests/fixtures/retrieval_gate_c_vectors_v1.json", session.consumed_ids)

    def test_benchmark_contract_and_fingerprint_use_same_session(self) -> None:
        import tm_benchmark

        expected = tm_benchmark.benchmark_implementation_fingerprint(_ROOT)
        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                with mock.patch.object(Path, "read_text", side_effect=AssertionError("pathname text")), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("pathname bytes")):
                    contract = tm_benchmark._load_benchmark_contract_from_session(session)
                    observed = tm_benchmark._benchmark_implementation_fingerprint_from_session(session)
                self.assertEqual(contract.corpus_record_count, 100_000)
                self.assertEqual(observed, expected)
                self.assertEqual(set(session.consumed_ids), set(tm_benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS))

    def test_gate_entry_rejects_fake_or_closed_session(self) -> None:
        import tm_gate_a
        import tm_benchmark

        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                pass
            for value in (object(), session, {"read_bytes": lambda _: b"{}"}):
                with self.subTest(value=type(value)), self.assertRaises((TypeError, RuntimeError)):
                    tm_gate_a._recompute_gate_a_from_session(cast(Any, value))
                with self.subTest(benchmark=type(value)), self.assertRaises((TypeError, RuntimeError)):
                    tm_benchmark._load_benchmark_contract_from_session(cast(Any, value))

    def test_gate_d_contract_and_publication_fingerprint_have_explicit_session_route(self) -> None:
        import tm_benchmark_gate
        import tm_benchmark

        expected = tm_benchmark.benchmark_implementation_fingerprint(_ROOT)
        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                with mock.patch.object(tm_benchmark_gate, "load_benchmark_contract", side_effect=AssertionError("legacy path contract")), mock.patch.object(tm_benchmark_gate, "benchmark_implementation_fingerprint", side_effect=AssertionError("legacy path fingerprint")):
                    contract = tm_benchmark_gate._load_contract(
                        _ROOT / "benchmark_tm_contract.json", _input_session=session,
                    )
                    fingerprint = tm_benchmark_gate._gate_d_input_fingerprint(session)
                self.assertEqual(contract.corpus_record_count, 100_000)
                self.assertEqual(fingerprint, expected)

    def test_borrowed_authority_seam_cannot_start_production_or_publish(self) -> None:
        import tm_benchmark_gate as gate

        with compose_rooted_source_authority(_ROOT) as authority:
            with _open_gate_input_session(authority) as session:
                with mock.patch.object(gate, "_validate_contract_path", side_effect=AssertionError("production started")):
                    with self.assertRaisesRegex(TypeError, "composition"):
                        gate._run_benchmark_gate_d_from_session(
                            session, contract_path=_ROOT / "benchmark_tm_contract.json",
                            work_root=_ROOT, evidence_path=_ROOT / "forbidden.json",
                        )
                with mock.patch.object(gate, "benchmark_evidence_bundle_to_json", side_effect=AssertionError("publication started")):
                    with self.assertRaisesRegex(TypeError, "composition"):
                        gate._publish_evidence_bundle(cast(Any, object()), _ROOT / "forbidden.json", _input_session=session)

    def test_source_composed_session_runs_real_gate_d_test_pipeline_without_path_inputs(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _open_source_input_session
        from tests.test_tm_benchmark_gate import _runner_ports, _TEST_RECORD_COUNT

        ports, record = _runner_ports()
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            evidence = work / "evidence.json"
            with _open_source_input_session(_ROOT) as session:
                with mock.patch.object(gate, "load_benchmark_contract", side_effect=AssertionError("legacy contract")), mock.patch.object(gate, "benchmark_implementation_fingerprint", side_effect=AssertionError("legacy fingerprint")):
                    result = gate._run_benchmark_gate_d_test(
                        contract_path=_ROOT / "benchmark_tm_contract.json", work_root=work,
                        evidence_path=evidence, ports=ports, test_record_count=_TEST_RECORD_COUNT,
                        test_seed=1, _input_session=session,
                    )
                self.assertTrue(result.test_mode)
                self.assertTrue(evidence.is_file())
                with self.assertRaises(RuntimeError):
                    session.read_bytes("benchmark_tm_contract.json")
            self.assertTrue(record["calls"])

    def test_attestation_compatibility_consumes_session_contract_and_fingerprint(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _open_source_input_session
        from tests.test_tm_benchmark_gate import _base_capability_manifest, _combined_bundle

        bundle = _combined_bundle()
        with _open_source_input_session(_ROOT) as session:
            with mock.patch.object(gate, "load_benchmark_contract", side_effect=AssertionError("legacy contract")), mock.patch.object(gate, "benchmark_implementation_fingerprint", side_effect=AssertionError("legacy fingerprint")):
                result = gate._gate_d_attestation_compatibility(
                    contract_path=_ROOT / "benchmark_tm_contract.json",
                    base_manifest=_base_capability_manifest(), bundle=bundle,
                    device_key=b"test key", _input_session=session,
                )
            benchmark = result["benchmark"]
            assert isinstance(benchmark, dict)
            self.assertEqual(benchmark["implementation_fingerprint"], bundle.implementation_fingerprint)
            self.assertTrue(session.read_bytes("benchmark_tm_contract.json"))
        with self.assertRaises(RuntimeError):
            session.read_bytes("benchmark_tm_contract.json")

    def test_session_terminal_failure_prevents_gate_d_capability_transition(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _open_source_input_session
        from tests.test_tm_benchmark_gate import (
            _base_capability_manifest, _capability_publisher, _combined_bundle,
            GateDPublicationTests, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT,
        )

        publisher = _capability_publisher()
        before = publisher.snapshot()
        run = GateDPublicationTests._run_result(_combined_bundle(fts5_missing=0, fallback_missing=0))
        with gate._gate_d_receipt_owner(run).open_session() as session:
            with mock.patch.object(_GateInputSession, "terminal_reproof", side_effect=RuntimeError("input drift")):
                with self.assertRaisesRegex(gate.BenchmarkGateDError, "GATE_D.IMPLEMENTATION_INVALID"):
                    gate._publish_retrieval_capability_gate_d_prepared(
                        _base_capability_manifest(), run, publisher,
                        generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT, prepare_publication=lambda result: result,
                        _publication_bindings=gate._GATE_D_PUBLICATION_BINDINGS,
                        _input_session=session,
                    )
            self.assertIs(publisher.snapshot(), before)

    def test_session_terminal_failure_removes_only_its_new_evidence(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _open_source_input_session
        from tests.test_tm_benchmark_gate import _combined_bundle

        bundle = _combined_bundle()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            foreign = Path(temporary) / "unrelated.txt"
            foreign.write_bytes(b"preserved")
            with _open_source_input_session(_ROOT) as session:
                with mock.patch.object(_GateInputSession, "terminal_reproof", side_effect=RuntimeError("input drift")):
                    with self.assertRaises(gate.BenchmarkGateDError):
                        gate._publish_evidence_bundle(bundle, path, _input_session=session)
                self.assertFalse(path.exists())
                self.assertEqual(foreign.read_bytes(), b"preserved")

    def test_capability_owner_rejects_loader_mismatch_despite_current_input_bytes(self) -> None:
        import tm_retrieval_validation as validation
        from tests.test_capability_host_gate_c import (
            _composition, _gate_c_owner, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT,
        )

        composition = _composition()
        owner = _gate_c_owner(composition)
        old = owner.validate_gate_c(
            generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertGreater(old.generation, 0)
        with mock.patch.object(validation, "__loader__", object()):
            observed = owner.validate_gate_c(
                generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        self.assertIs(observed, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)


class SourcePathCompatibilityTests(unittest.TestCase):
    def test_standalone_contract_accepts_cwd_relative_and_parent_locators(self) -> None:
        import tm_benchmark

        expected = tm_benchmark.load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
        with chdir(_ROOT), mock.patch.object(Path, "resolve", side_effect=AssertionError("source locator followed links")):
            for path in (
                Path("benchmark_tm_contract.json"),
                Path("tests/../benchmark_tm_contract.json"),
                _ROOT / "tests/../benchmark_tm_contract.json",
            ):
                with self.subTest(path=path):
                    self.assertEqual(tm_benchmark.load_benchmark_contract(path), expected)

    def test_public_run_accepts_cwd_relative_and_parent_contract_locators(self) -> None:
        from dataclasses import replace
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _runner_ports

        with chdir(_ROOT):
            for path in (
                Path("benchmark_tm_contract.json"),
                Path("tests/../benchmark_tm_contract.json"),
                _ROOT / "tests/../benchmark_tm_contract.json",
            ):
                with self.subTest(path=path), tempfile.TemporaryDirectory() as temporary:
                    work = Path(temporary)
                    ports, record = _runner_ports()
                    migration = mock.Mock(wraps=ports.run_process_migration_evidence)
                    ports = replace(ports, run_process_migration_evidence=migration)
                    with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports):
                        result = gate.run_benchmark_gate_d(path, work, work / "evidence.json")
                    try:
                        self.assertFalse(result.test_mode)
                        self.assertTrue((work / "evidence.json").is_file())
                        self.assertEqual(len(record["calls"]), 6)
                        self.assertEqual(migration.call_count, 2)
                        for call in migration.call_args_list:
                            self.assertEqual(call.kwargs["contract_path"], _ROOT / "benchmark_tm_contract.json")
                    finally:
                        gate._gate_d_receipt_owner(result).close()

    def test_public_validators_accept_parent_root_and_approved_locators(self) -> None:
        import matcher_validation
        import tm_gate_a
        import tm_retrieval_validation

        now = datetime.now(timezone.utc).replace(microsecond=0)
        times = dict(generated_at_utc=now, valid_until_utc=now + timedelta(hours=1))
        with chdir(_ROOT):
            for root, prefix in ((Path("tests/.."), Path("tests/../tests")), (_ROOT / "tests/..", _ROOT / "tests/../tests")):
                with self.subTest(root=root):
                    approved = prefix / "fixtures/feature5_gate_a_v1.json"
                    report = tm_gate_a.recompute_gate_a(repository_root=root, approved_roots_path=approved)
                    self.assertEqual(len(report.components), 3)
                    matcher = matcher_validation.recompute_matcher_validation(
                        repository_root=root, approved_roots_path=approved, include_full=False, **times,
                    )
                    self.assertIsNotNone(matcher.manifest)
                    retrieval = tm_retrieval_validation.recompute_retrieval_validation(
                        repository_root=root, approved_roots_path=prefix / "fixtures/retrieval_gate_c_roots_v1.json", **times,
                    )
                    self.assertIsNotNone(retrieval.manifest)

    def test_source_locator_normalization_keeps_real_links_rejected(self) -> None:
        from platform_source_authority import RootedSourceAuthority
        from tm_gate_inputs import _source_validation_inputs

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "elsewhere/target"
            target.mkdir(parents=True)
            (target / "input.txt").write_bytes(b"retained source")
            (target.parent / "input.txt").write_bytes(b"foreign parent")
            (root / "input.txt").write_bytes(b"lexical target")
            link = root / "linked"
            if os.name == "nt":
                subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                    check=True, capture_output=True,
                )
                self.assertTrue(link.is_junction())
            else:
                link.symlink_to(target, target_is_directory=True)
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                _read_input_bytes(link / "input.txt")
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                with _source_validation_inputs(link, target / "input.txt"):
                    self.fail("symlink root accepted")

            original = RootedSourceAuthority.bind_path
            observed: list[Path] = []

            def record_binding(authority: RootedSourceAuthority, path: Path) -> Any:
                observed.append(path)
                return original(authority, path)

            # Public locators are lexical: an eliminated component is never
            # traversed.  The surviving canonical target still uses no-follow.
            with mock.patch.object(RootedSourceAuthority, "bind_path", new=record_binding), mock.patch.object(Path, "resolve", side_effect=AssertionError("source locator followed links")):
                self.assertEqual(_read_input_bytes(link / "../input.txt"), b"lexical target")
                with _source_validation_inputs(link / "..", link / "../input.txt") as (_session, approved):
                    self.assertEqual(_read_input_bytes(approved), b"lexical target")
            self.assertEqual(observed, [root / "input.txt", root / "input.txt"])


class GateDContinuousInputEpochTests(unittest.TestCase):
    @staticmethod
    def _isolated_source(work: Path) -> Path:
        import tm_benchmark

        source = work / "source"
        source.mkdir()
        for relative in tm_benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS:
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_ROOT / relative, destination)
        return source

    @staticmethod
    def _replace_same_bytes(path: Path) -> None:
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)

    @staticmethod
    def _run_public(source: Path, contract: Path, work: Path) -> Any:
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _runner_ports

        ports, _ = _runner_ports()
        run_root = work / "runs"
        run_root.mkdir()
        with mock.patch.object(gate, "_gate_d_source_root", return_value=source), mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports):
            return gate.run_benchmark_gate_d(contract, run_root, work / "evidence.json")

    def test_public_source_run_supplies_live_session_to_core(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _require_production_session

        def inspect_core(**kwargs: Any) -> object:
            _require_production_session(kwargs.get("_input_session"))
            return object()

        with mock.patch.object(gate, "_run_benchmark_gate_d_core", side_effect=inspect_core):
            gate.run_benchmark_gate_d(_ROOT / "benchmark_tm_contract.json", _ROOT, _ROOT / "unused.json")

    def test_public_source_run_blocks_same_bytes_identity_replacement(self) -> None:
        import tm_benchmark
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _runner_ports

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source"
            source.mkdir()
            run_root = work / "runs"
            run_root.mkdir()
            for relative in tm_benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS:
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_ROOT / relative, destination)
            target = source / "tm_gate_inputs.py"
            identity = target.stat().st_ino
            changed: list[bool] = []

            def replace_source(_record: object) -> None:
                replacement = source / "replacement.tmp"
                replacement.write_bytes(target.read_bytes())
                os.replace(replacement, target)
                changed.append(target.stat().st_ino != identity)

            ports, _ = _runner_ports(combine_side_effect=replace_source)
            with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports), mock.patch.object(gate, "_gate_d_source_root", return_value=source, create=True), mock.patch.object(gate, "benchmark_implementation_fingerprint", side_effect=lambda: tm_benchmark.benchmark_implementation_fingerprint(source)):
                with self.assertRaises((gate.BenchmarkGateDError, PermissionError, RuntimeError)):
                    gate.run_benchmark_gate_d(source / "benchmark_tm_contract.json", run_root, run_root / "evidence.json")
            self.assertFalse((run_root / "evidence.json").exists())
            if os.name == "nt":
                self.assertFalse(changed)
            replacement = source / "after.tmp"
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)  # Failure released the real handles.

    def test_receipt_rejects_foreign_closed_and_omitted_owner_then_same_owner_publishes(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _compose_source_input_owner
        from tests.test_tm_benchmark_gate import _runner_ports, _base_capability_manifest, _capability_publisher, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT

        def publish(run: Any, publisher: Any, session: Any) -> Any:
            return gate._publish_retrieval_capability_gate_d_prepared(
                _base_capability_manifest(), run, publisher,
                generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT, prepare_publication=lambda result: result,
                _publication_bindings=gate._GATE_D_PUBLICATION_BINDINGS,
                _input_session=session,
            )

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            ports, _ = _runner_ports()
            with _compose_source_input_owner(_ROOT) as owner:
                with owner.open_session() as run_session:
                    with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports):
                        run = gate._run_benchmark_gate_d_from_session(run_session, contract_path=_ROOT / "benchmark_tm_contract.json", work_root=work, evidence_path=work / "evidence.json")
                publisher = _capability_publisher()
                before = publisher.snapshot()
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    publish(run, publisher, None)
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    gate._persist_gate_d_attestation(
                        contract_path=_ROOT / "benchmark_tm_contract.json",
                        state_root=work / "attestation", base_manifest=_base_capability_manifest(),
                        run_result=run, issued_at_utc=_EVALUATED_AT,
                    )
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    publish(run, publisher, run_session)
                with _compose_source_input_owner(_ROOT) as foreign:
                    with foreign.open_session() as wrong_session:
                        with self.assertRaises((TypeError, ValueError, RuntimeError)):
                            publish(run, publisher, wrong_session)
                self.assertIs(publisher.snapshot(), before)
                with owner.open_session() as attestation_session:
                    gate._persist_gate_d_attestation(
                        contract_path=_ROOT / "benchmark_tm_contract.json",
                        state_root=work / "attestation", base_manifest=_base_capability_manifest(),
                        run_result=run, issued_at_utc=_EVALUATED_AT, _input_session=attestation_session,
                    )
                with owner.open_session() as valid_session:
                    publish(run, publisher, valid_session)
                self.assertIsNot(publisher.snapshot(), before)
            with _compose_source_input_owner(_ROOT) as replacement:
                with replacement.open_session() as replacement_session:
                    with self.assertRaises((TypeError, ValueError, RuntimeError)):
                        publish(run, _capability_publisher(), replacement_session)
                with replacement.open_session() as restore_session:
                    restored = gate._restore_gate_d_attestation(
                        contract_path=_ROOT / "benchmark_tm_contract.json",
                        state_root=work / "attestation", base_manifest=_base_capability_manifest(),
                        _input_session=restore_session,
                    )
                with replacement.open_session() as restored_publication:
                    publish(restored, _capability_publisher(), restored_publication)

    def test_public_run_attestation_publish_retains_external_contract_and_releases_all_domains(self) -> None:
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _base_capability_manifest, _capability_publisher, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = self._isolated_source(work)
            external = work / "external"
            external.mkdir()
            contract = external / "contract.json"
            shutil.copyfile(_ROOT / "benchmark_tm_contract.json", contract)
            run = self._run_public(source, contract, work)
            owner = gate._gate_d_receipt_owner(run)
            if os.name == "nt":
                for path in (source / "tm_gate_inputs.py", contract):
                    with self.subTest(path=path), self.assertRaises(PermissionError):
                        self._replace_same_bytes(path)
            gate._persist_gate_d_attestation(
                contract_path=contract, state_root=work / "attestation",
                base_manifest=_base_capability_manifest(), run_result=run,
                issued_at_utc=_EVALUATED_AT,
            )
            if os.name == "nt":
                with self.assertRaises(PermissionError):
                    self._replace_same_bytes(contract)
            publisher = _capability_publisher()
            before = publisher.snapshot()
            gate.publish_retrieval_capability_gate_d(
                _base_capability_manifest(), run, publisher,
                generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            self.assertIsNot(publisher.snapshot(), before)
            with self.assertRaises(RuntimeError):
                owner._require_live()
            self._replace_same_bytes(source / "tm_gate_inputs.py")
            self._replace_same_bytes(contract)

    def test_discarded_public_receipt_releases_source_handles_even_on_another_thread(self) -> None:
        import tm_benchmark_gate as gate

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = self._isolated_source(work)
            run = self._run_public(source, source / "benchmark_tm_contract.json", work)
            owner = weakref.ref(gate._gate_d_receipt_owner(run))
            holder = [run]
            del run
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(holder.clear).result()
            gc.collect()
            self.assertIsNone(owner())
            self._replace_same_bytes(source / "tm_gate_inputs.py")

    def test_publication_failure_releases_public_receipt_owner(self) -> None:
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _base_capability_manifest, _capability_publisher, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT

        def fail_prepare(_result: Any) -> Any:
            raise RuntimeError("prepare failed")

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = self._isolated_source(work)
            run = self._run_public(source, source / "benchmark_tm_contract.json", work)
            owner = gate._gate_d_receipt_owner(run)
            publisher = _capability_publisher()
            before = publisher.snapshot()
            with self.assertRaisesRegex(RuntimeError, "prepare failed"):
                gate._publish_retrieval_capability_gate_d_prepared(
                    _base_capability_manifest(), run, publisher,
                    generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT, prepare_publication=fail_prepare,
                    _publication_bindings=gate._GATE_D_PUBLICATION_BINDINGS,
                )
            self.assertIs(publisher.snapshot(), before)
            with self.assertRaises(RuntimeError):
                owner._require_live()
            self._replace_same_bytes(source / "tm_gate_inputs.py")

    def test_failed_fresh_restore_releases_its_external_input_domain(self) -> None:
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _base_capability_manifest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = self._isolated_source(work)
            contract = work / "external-contract.json"
            shutil.copyfile(_ROOT / "benchmark_tm_contract.json", contract)
            with mock.patch.object(gate, "_gate_d_source_root", return_value=source):
                with self.assertRaises(gate.BenchmarkGateDError):
                    gate._restore_gate_d_attestation(
                        contract_path=contract, state_root=work / "missing-state",
                        base_manifest=_base_capability_manifest(),
                    )
            self._replace_same_bytes(contract)

    def test_aborted_input_window_cannot_issue_a_production_receipt(self) -> None:
        import hashlib
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _compose_source_input_owner
        from tests.test_tm_benchmark_gate import _combined_bundle

        bundle = _combined_bundle()
        artifact = gate.benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        session = None
        with _compose_source_input_owner(_ROOT) as owner:
            with self.assertRaisesRegex(ValueError, "consumer failed"):
                with owner.open_session() as session:
                    session.read_bytes("benchmark_tm_contract.json")
                    raise ValueError("consumer failed")
            assert session is not None
            with self.assertRaises(RuntimeError):
                gate._issue_benchmark_gate_d_run_result(
                    bundle=bundle, bundle_digest=bundle.bundle_digest,
                    artifact_size=len(artifact), artifact_digest=hashlib.sha256(artifact).hexdigest(),
                    test_mode=False, _input_session=session,
                )

    def test_outer_abort_revokes_an_already_issued_receipt(self) -> None:
        import tm_benchmark_gate as gate
        from tm_gate_inputs import _compose_source_input_owner
        from tests.test_tm_benchmark_gate import _runner_ports, _base_capability_manifest, _capability_publisher, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            ports, _ = _runner_ports()
            run = None
            with _compose_source_input_owner(_ROOT) as owner:
                with self.assertRaisesRegex(ValueError, "outer consumer failed"):
                    with owner.open_session() as session:
                        with mock.patch.object(gate, "_DEFAULT_GATE_D_RUNNER_PORTS", ports):
                            run = gate._run_benchmark_gate_d_from_session(
                                session, contract_path=_ROOT / "benchmark_tm_contract.json",
                                work_root=work, evidence_path=work / "evidence.json",
                            )
                        raise ValueError("outer consumer failed")
                assert run is not None
                publisher = _capability_publisher()
                before = publisher.snapshot()
                with owner.open_session() as next_session:
                    with self.assertRaises((TypeError, ValueError, RuntimeError)):
                        gate._publish_retrieval_capability_gate_d_prepared(
                            _base_capability_manifest(), run, publisher,
                            generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                            evaluated_at_utc=_EVALUATED_AT, prepare_publication=lambda result: result,
                            _publication_bindings=gate._GATE_D_PUBLICATION_BINDINGS,
                            _input_session=next_session,
                        )
                self.assertIs(publisher.snapshot(), before)

    def test_attestation_reproves_inputs_after_encoding_before_commit(self) -> None:
        import tm_benchmark_gate as gate
        from tests.test_tm_benchmark_gate import _base_capability_manifest, _EVALUATED_AT

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = self._isolated_source(work)
            contract = source / "benchmark_tm_contract.json"
            run = self._run_public(source, contract, work)
            owner = gate._gate_d_receipt_owner(run)
            original = gate._gate_d_attestation_canonical_json

            def close_input_owner(payload: Any) -> str:
                if "compatibility" in payload and "bundle_json" in payload:
                    owner.close()
                return original(payload)

            with mock.patch.object(gate, "_gate_d_attestation_canonical_json", side_effect=close_input_owner):
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    gate._persist_gate_d_attestation(
                        contract_path=contract, state_root=work / "attestation",
                        base_manifest=_base_capability_manifest(), run_result=run,
                        issued_at_utc=_EVALUATED_AT,
                    )
            self.assertFalse((work / "attestation" / "qualification.json").exists())
            self._replace_same_bytes(source / "tm_gate_inputs.py")


if __name__ == "__main__":
    unittest.main()
