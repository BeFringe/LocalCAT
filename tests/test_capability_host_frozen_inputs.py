"""Provisional Host consumers cannot upgrade fixture data to capability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import sys
from types import FrameType
from typing import Any, Iterator
import unittest
from unittest import mock

import capability_host as host
import capability_frozen_inputs as frozen
from tests.test_tm_gate_input_composition import _frozen_entries


ROOT = Path(__file__).absolute().parents[1]
NOW = datetime.now(timezone.utc).replace(microsecond=0)
_active_calls: list[tuple[str, str, dict[str, Any]]] | None = None


def _audit_inputs(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and _active_calls is not None:
        _active_calls.append(("audit", event, {"path": args[0]}))


sys.addaudithook(_audit_inputs)


@contextmanager
def calls() -> Iterator[list[tuple[str, str, dict[str, Any]]]]:
    """Observe real calls without replacing a callable pinned by Host anchors."""
    observed: list[tuple[str, str, dict[str, Any]]] = []
    global _active_calls
    prior_calls = _active_calls
    _active_calls = observed
    previous = sys.getprofile()
    def observe(frame: FrameType, event: str, arg: object) -> None:
        if event == "call":
            values = dict(frame.f_locals)
            if frame.f_globals.get("__name__") == "tm_benchmark_gate" and values.get("_contract_input") is not None:
                session = values.get("_input_session", values.get("session"))
                values["bound_current_window"] = values["_contract_input"]._belongs_to(session)
            if frame.f_code.co_name == "terminal_reproof" and frame.f_globals.get("__name__") == "tm_gate_inputs":
                session = values["self"]
                try:
                    values["consumed_ids_snapshot"] = session.consumed_ids
                except RuntimeError:
                    pass
            observed.append((frame.f_globals.get("__name__", ""), frame.f_code.co_name, values))
    sys.setprofile(observe)
    try:
        yield observed
    finally:
        sys.setprofile(previous)
        _active_calls = prior_calls


def entries(observed: list[tuple[str, str, dict[str, Any]]], module: str, name: str) -> list[dict[str, Any]]:
    return [values for current, function, values in observed if current == module and function == name]


@contextmanager
def changed_attribute(target: Any, name: str, value: object) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


class FrozenHostInputTests(unittest.TestCase):
    def assert_no_input_reopen(self, observed: list[tuple[str, str, dict[str, Any]]]) -> None:
        reopened = [item["path"] for item in entries(observed, "audit", "open") if isinstance(item["path"], str) and Path(item["path"]).suffix.lower() in (".json", ".txt") and Path(item["path"]).is_relative_to(ROOT)]
        self.assertEqual(reopened, [], "frozen fixture/contract was reopened by pathname")

    def test_arbitrary_authority_never_reaches_source_or_host_composition(self) -> None:
        with mock.patch("tm_gate_inputs.compose_rooted_source_authority", side_effect=AssertionError("source fallback")):
            for value in (object(), {}, lambda name: b"proof"):
                with self.subTest(value=type(value)), self.assertRaises(RuntimeError):
                    host.compose_frozen_capability_host(trusted_source_authority=value, evaluated_at_utc=NOW)

    def test_frozen_flag_cannot_adopt_a_real_checkout_authority(self) -> None:
        from platform_source_authority import compose_rooted_source_authority
        source = compose_rooted_source_authority(ROOT)
        with source:
            with mock.patch.object(sys, "frozen", True, create=True), self.assertRaisesRegex(RuntimeError, "trusted source handoff"):
                host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)

    def test_provisional_host_uses_real_matcher_factory_and_cannot_publish(self) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        with calls() as observed:
            result = composition.matcher_validation_owner.validate_text_v1(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
        factory = entries(observed, "matcher_validation", "build_validated_matcher_v1")
        self.assertEqual(len(factory), 1)
        self.assertIsNone(factory[0]["repository_root"])
        self.assertIsNone(factory[0]["approved_roots_path"])
        self.assertEqual(factory[0]["_approved_roots_id"], "tests/fixtures/feature5_gate_a_v1.json")
        self.assertIsNotNone(factory[0]["_input_session"])
        self.assert_no_input_reopen(observed)
        self.assertFalse(entries(observed, "platform_source_authority", "compose_rooted_source_authority"))
        self.assertFalse([values for values in entries(observed, "matcher_capability", "__init__") if type(values.get("self")).__name__ == "MatcherCapabilityPublisher"])
        self.assertIsNone(result.matcher)
        self.assertEqual(result.display.state.value, "UNAVAILABLE")

    def test_provisional_host_gate_c_consumes_session_and_never_installs(self) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        before = composition.host.retrieval_snapshot()
        assert composition.retrieval_gate_c_validation_owner is not None
        with calls() as observed:
            result = composition.retrieval_gate_c_validation_owner.validate_gate_c(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
        invoked = entries(observed, "tm_retrieval_validation", "_recompute_retrieval_validation_from_session")
        self.assertEqual(len(invoked), 1)
        session = invoked[0]["session"]
        terminal = [item for item in entries(observed, "tm_gate_inputs", "terminal_reproof") if item["self"] is session and "consumed_ids_snapshot" in item]
        self.assertTrue(terminal)
        self.assert_no_input_reopen(observed)
        self.assertIn("tests/fixtures/retrieval_gate_c_roots_v1.json", terminal[0]["consumed_ids_snapshot"])
        self.assertFalse(entries(observed, "platform_source_authority", "compose_rooted_source_authority"))
        self.assertFalse(entries(observed, "tm_retrieval_validation", "recompute_retrieval_validation"))
        self.assertFalse([values for values in entries(observed, "tm_retrieval_capability", "__init__") if type(values.get("self")).__name__ == "RetrievalCapabilityPublisher"])
        self.assertEqual(result.generation, before.generation)
        self.assertFalse(result.display.context_available)

    def test_closed_source_rejects_cached_bytes_and_metadata(self) -> None:
        source = frozen._test_frozen_capability_source((("capability_host.py", (ROOT / "capability_host.py").read_bytes()),), ROOT)
        anchor = source.bind_path(ROOT / "capability_host.py")
        self.assertTrue(anchor.is_current())
        source.close()
        self.assertFalse(anchor.is_current())
        with self.assertRaises(RuntimeError):
            anchor.matches_module(host)

    def test_composition_close_revokes_its_actual_input_source(self) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        composition._close_frozen_inputs()
        with self.assertRaises(RuntimeError):
            source.reprove()
        result = composition.matcher_validation_owner.validate_text_v1(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
        self.assertIsNone(result.matcher)

    def test_gate_d_real_execution_uses_relative_contract_and_fresh_same_owner_windows(self) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        owner = composition.retrieval_gate_d_owner
        assert owner is not None
        # The real public owner cannot start with provisional Gate C.
        self.assertEqual(owner.start_gate_d(evaluated_at_utc=NOW).safe_code, "GATE_D.GATE_C_REQUIRED")
        execution = getattr(owner, "_RetrievalGateDOwner__execution")
        work = ROOT / "artifacts/windows/task36a-retry/never-created-gate-d"
        with execution.input_operation(), calls() as observed:
            with self.assertRaisesRegex(host._GateDOperationalError, "GATE_D.INPUT_AUTHORITY_UNAVAILABLE"):
                execution.run(contract_path=execution.contract_path, work_root=work, evidence_path=work / "evidence.json", publication_owner_identity=object(), publication_graph_nonce=object())
            with self.assertRaisesRegex(host._GateDOperationalError, "GATE_D.INPUT_AUTHORITY_UNAVAILABLE"):
                execution.restore(contract_path=execution.contract_path, state_root=work, base_manifest=None, publication_owner_identity=object(), publication_graph_nonce=object())
            binding = execution._capture_binding()
            # An exact but uninitialized result is deliberately malformed. It
            # must be rejected before any receipt/state/publisher is consulted.
            malformed = object.__new__(binding.run_result_type)
            with self.assertRaisesRegex(host._GateDOperationalError, "GATE_D.INPUT_AUTHORITY_UNAVAILABLE"):
                binding.persist_attestation(run_result=malformed, contract_path=execution.contract_path, state_root=work, base_manifest=None, issued_at_utc=NOW)
        invoked = [entries(observed, "tm_benchmark_gate", name)[0] for name in ("_run_benchmark_gate_d_from_session", "_restore_gate_d_attestation", "_persist_gate_d_attestation")]
        sessions = [item.get("_input_session", item.get("session")) for item in invoked]
        self.assertEqual(len({id(session) for session in sessions}), 3)
        self.assertEqual(len({id(getattr(session, "_GateInputSession__owner")) for session in sessions}), 1)
        for item in invoked:
            self.assertIsNone(item["contract_path"])
            self.assertTrue(item["bound_current_window"])
            self.assertTrue(item["_contract_input"]._matches_id("benchmark_tm_contract.json"))
            with self.assertRaises(RuntimeError):
                item["_contract_input"]._read()
        self.assertFalse(entries(observed, "platform_source_authority", "compose_rooted_source_authority"))
        self.assertFalse(entries(observed, "tm_benchmark_gate", "_issue_benchmark_gate_d_run_result"))
        self.assert_no_input_reopen(observed)
        self.assertFalse(work.exists())

    def test_matcher_loader_origin_filename_and_runtime_code_drift_reject_before_factory(self) -> None:
        import matcher_validation as matcher
        from importlib.machinery import SourceFileLoader
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        assert matcher.__spec__ is not None
        cases = (
            mock.patch.object(matcher, "__loader__", SourceFileLoader(matcher.__name__, str(ROOT / "foreign.py"))),
            mock.patch.object(matcher.__spec__, "origin", str(ROOT / "foreign.py")),
            changed_attribute(matcher.build_validated_matcher_v1, "__code__", matcher.build_validated_matcher_v1.__code__.replace(co_filename=str(ROOT / "foreign.py"))),
            mock.patch.object(matcher, "_recompute_matcher_validation_from_session", lambda *a, **kw: None),
        )
        for patcher in cases:
            with self.subTest(patcher=patcher), patcher, calls() as observed:
                result = composition.matcher_validation_owner.validate_text_v1(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
            self.assertIsNone(result.matcher)
            self.assertFalse(entries(observed, "matcher_validation", "build_validated_matcher_v1"))

    def test_relative_input_grammar_and_incomplete_closure_reject(self) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        for path in (ROOT.parent / "foreign.py", ROOT / ".." / "foreign.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                source.bind_path(path)
        incomplete = tuple((name, content) for name, content in _frozen_entries() if name != "tm_gate_inputs.py")
        missing = frozen._test_frozen_capability_source(incomplete, ROOT)
        self.addCleanup(missing.close)
        with self.assertRaises(KeyError):
            host.compose_capability_host(source_authority=missing, evaluated_at_utc=NOW)


if __name__ == "__main__":
    unittest.main()
