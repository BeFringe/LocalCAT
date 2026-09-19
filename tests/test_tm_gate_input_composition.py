"""The common window must compute provisionally without minting provenance."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any
import unittest
from unittest import mock

import matcher_validation as matcher
import tm_benchmark
import tm_benchmark_gate as gate
import tm_gate_a
import tm_gate_inputs as inputs
import tm_retrieval_validation as retrieval


_ROOT = Path(__file__).absolute().parents[1]
_ROOTS_ID = "tests/fixtures/feature5_gate_a_v1.json"
_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _factory(session: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = dict(
        repository_root=_ROOT, generated_at_utc=_NOW,
        valid_until_utc=_NOW + timedelta(hours=1), evaluated_at_utc=_NOW,
        include_full=False, _input_session=session,
    )
    values.update(overrides)
    return matcher.build_validated_matcher_v1(**values)


def _frozen_entries() -> tuple[tuple[str, bytes], ...]:
    # Only tests capture local fixture bytes. The consumer never copies/reopens them.
    paths = {*tm_benchmark.BENCHMARK_IMPLEMENTATION_SOURCE_PATHS}
    paths.update(path.relative_to(_ROOT).as_posix() for path in _ROOT.glob("*.py"))
    paths.update(path.relative_to(_ROOT).as_posix() for path in (_ROOT / "tests/fixtures").glob("*.*") if path.is_file())
    return tuple((name, (_ROOT / name).read_bytes()) for name in sorted(paths))


class CommonInputCompositionTests(unittest.TestCase):
    def test_source_session_reaches_same_formal_factory_without_path_reopen(self) -> None:
        expected = matcher.build_validated_matcher_v1(
            repository_root=_ROOT, generated_at_utc=_NOW,
            valid_until_utc=_NOW + timedelta(hours=1), evaluated_at_utc=_NOW,
            include_full=False,
        )
        with inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as session:
                original = matcher.MatcherCapabilityPublisher

                def publish(*args: Any, **kwargs: Any) -> Any:
                    self.assertIs(session._completed_owner(), owner)
                    return original(*args, **kwargs)

                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("path bytes")), mock.patch.object(Path, "read_text", side_effect=AssertionError("path text")), mock.patch.object(matcher, "recompute_matcher_validation", side_effect=AssertionError("public recompute")), mock.patch.object(matcher, "MatcherCapabilityPublisher", side_effect=publish):
                    observed = _factory(session)
                self.assertEqual(observed.capability(), expected.capability())
                self.assertEqual(observed.capability().state.value, "BASIC_VALIDATED")

    def test_formal_session_factory_rejects_conflicting_roots_and_foreign_locator(self) -> None:
        with inputs._compose_source_input_owner(_ROOT) as owner:
            for overrides in (
                {"approved_roots_path": _ROOT / "tests/fixtures/retrieval_gate_c_roots_v1.json"},
                {"_approved_roots_id": "../roots.json"},
                {"repository_root": _ROOT.parent},
            ):
                with self.subTest(overrides=overrides), owner.open_session() as session:
                    with mock.patch.object(matcher, "MatcherCapabilityPublisher") as publisher:
                        with self.assertRaises((TypeError, ValueError, RuntimeError)):
                            _factory(session, **overrides)
                        publisher.assert_not_called()

    def test_formal_session_factory_terminal_failure_cannot_construct_publisher(self) -> None:
        with inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as session:
                with mock.patch.object(inputs._GateInputSession, "terminal_reproof", side_effect=RuntimeError("terminal failed")), mock.patch.object(matcher, "MatcherCapabilityPublisher") as publisher:
                    with self.assertRaisesRegex(RuntimeError, "terminal failed"):
                        _factory(session)
                    publisher.assert_not_called()

    def test_formal_factory_rejects_borrowed_closed_and_aborted_sessions(self) -> None:
        from platform_source_authority import compose_rooted_source_authority

        with compose_rooted_source_authority(_ROOT) as authority:
            with inputs._open_gate_input_session(authority) as borrowed:
                with self.assertRaises(TypeError):
                    _factory(borrowed)
        with inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as closed:
                pass
            with self.assertRaises(RuntimeError):
                _factory(closed)
            with owner.open_session() as aborted:
                aborted._abort()
                with mock.patch.object(matcher, "MatcherCapabilityPublisher") as publisher:
                    with self.assertRaises(RuntimeError):
                        _factory(aborted)
                    publisher.assert_not_called()

    def test_private_factory_supports_explicit_relative_only_roots(self) -> None:
        with inputs._compose_source_input_owner(_ROOT) as owner:
            with owner.open_session() as session:
                result = _factory(session, repository_root=None, approved_roots_path=None)
                self.assertEqual(result.capability().state.value, "BASIC_VALIDATED")

    def test_test_frozen_runs_real_common_gate_matcher_contract_and_fingerprint(self) -> None:
        entries = _frozen_entries()
        expected = tm_benchmark.benchmark_implementation_fingerprint(_ROOT)
        with inputs._compose_test_frozen_input_owner(entries) as owner:
            with owner.open_session() as session, ExitStack() as stack:
                stack.enter_context(mock.patch.object(Path, "read_bytes", side_effect=AssertionError("path bytes")))
                stack.enter_context(mock.patch.object(Path, "read_text", side_effect=AssertionError("path text")))
                stack.enter_context(mock.patch.object(inputs, "compose_rooted_source_authority", side_effect=AssertionError("source adapter")))
                self.assertEqual(session.provenance, "test-frozen")
                self.assertEqual(len(tm_gate_a._recompute_gate_a_from_session(session).components), 3)
                self.assertIsNotNone(matcher._recompute_matcher_validation_from_session(
                    session, generated_at_utc=_NOW, valid_until_utc=_NOW + timedelta(hours=1), include_full=True,
                ).manifest)
                self.assertIsNotNone(retrieval._recompute_retrieval_validation_from_session(
                    session, generated_at_utc=_NOW, valid_until_utc=_NOW + timedelta(hours=1),
                ).manifest)
                self.assertEqual(tm_benchmark._load_benchmark_contract_from_session(session).corpus_record_count, 100_000)
                self.assertEqual(tm_benchmark._benchmark_implementation_fingerprint_from_session(session), expected)
                self.assertIn("tests/fixtures/unicode-16.0.0-WordBreakTest.txt", session.consumed_ids)
                with self.assertRaises(TypeError):
                    inputs._require_production_session(session)
                with self.assertRaises(TypeError):
                    _factory(session)
            with self.assertRaises((TypeError, RuntimeError)):
                session._completed_owner()

    def test_test_frozen_window_lifetime_terminal_and_outer_abort(self) -> None:
        entries = (("fixture.json", b"exact\r\n"),)
        with inputs._compose_test_frozen_input_owner(entries, terminal_results=(True, False, True)) as owner:
            with owner.open_session() as first:
                reference = first.input("fixture.json")
                self.assertEqual(inputs._read_input_bytes(reference), b"exact\r\n")
                with self.assertRaises(RuntimeError):
                    with owner.open_session():
                        pass
                with self.assertRaises(RuntimeError):
                    inputs._GateInputSession(owner, _key=inputs._FACTORY_KEY)
            with self.assertRaises(RuntimeError):
                inputs._read_input_bytes(reference)
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                with owner.open_session() as second:
                    second.read_bytes("fixture.json")
            third = None
            with self.assertRaisesRegex(ValueError, "outer abort"):
                with owner.open_session() as third:
                    third.terminal_reproof()
                    raise ValueError("outer abort")
            with self.assertRaises((TypeError, RuntimeError)):
                assert third is not None
                third._completed_owner()

    def test_test_frozen_invalid_contract_and_changed_source_are_consumed(self) -> None:
        entries = dict(_frozen_entries())
        expected = tm_benchmark.benchmark_implementation_fingerprint(_ROOT)
        entries["tm_contracts.py"] += b"\n# altered test source bytes\n"
        with inputs._open_test_frozen_input_session(tuple(entries.items())) as session:
            self.assertNotEqual(tm_benchmark._benchmark_implementation_fingerprint_from_session(session), expected)
        entries["benchmark_tm_contract.json"] = b"["
        with inputs._open_test_frozen_input_session(tuple(entries.items())) as session:
            with self.assertRaises(ValueError):
                tm_benchmark._load_benchmark_contract_from_session(session)

    def test_worker_window_accepts_test_consumption_but_rejects_production(self) -> None:
        import tm_benchmark_worker as worker

        with inputs._open_test_frozen_input_session((("fixture.json", b"{}"),)) as session:
            with worker._worker_input_window(session, test_mode=True) as observed:
                self.assertIs(observed, session)
                self.assertEqual(observed.read_bytes("fixture.json"), b"{}")
            with self.assertRaises(TypeError):
                with worker._worker_input_window(session, test_mode=False):
                    self.fail("test inputs upgraded by worker")

    def test_test_frozen_closed_wrong_id_and_foreign_roots_fail(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        entries = (("fixture.json", b"{}"),)
        with inputs._compose_test_frozen_input_owner(entries) as owner, inputs._compose_test_frozen_input_owner(entries) as foreign:
            with self.assertRaisesRegex(RuntimeError, "terminal"), owner.open_session() as session, foreign.open_session() as other:
                with self.assertRaises(ValueError):
                    session._require_bound_input(other.input("fixture.json"))
                with self.assertRaises((ValueError, KeyError)):
                    session.read_bytes("missing.json")
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with self.assertRaises(RuntimeError):
                        executor.submit(session.read_bytes, "fixture.json").result()
                owner.close()
                with self.assertRaises(RuntimeError):
                    session.read_bytes("fixture.json")
                session._abort()

    def test_test_frozen_never_issues_receipt_or_production_owner(self) -> None:
        from tests.test_tm_benchmark_gate import _combined_bundle

        bundle = _combined_bundle()
        artifact = gate.benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        with inputs._open_test_frozen_input_session((("fixture.json", b"{}"),)) as session:
            session.read_bytes("fixture.json")
            session.terminal_reproof()
            for test_mode in (False, True):
                with self.subTest(test_mode=test_mode), self.assertRaises((TypeError, RuntimeError)):
                    gate._issue_benchmark_gate_d_run_result(
                        bundle=bundle, bundle_digest=bundle.bundle_digest,
                        artifact_size=len(artifact), artifact_digest=hashlib.sha256(artifact).hexdigest(),
                        test_mode=test_mode, _input_session=session,
                    )

    def test_frozen_production_admission_cannot_be_forged(self) -> None:
        for value in (object(), {"release_digest": "a" * 64}, lambda name: b"trusted"):
            with self.subTest(value=type(value)), self.assertRaisesRegex(RuntimeError, "unavailable"):
                inputs._compose_frozen_input_owner(value)
                self.fail("production frozen authority was fabricated")


if __name__ == "__main__":
    unittest.main()
