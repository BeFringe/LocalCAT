from __future__ import annotations

import dataclasses
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import capability_host as host_module
from capability_host import CapabilityHostComposition
from tm_contracts import TMQuery
from tm_retrieval import TMRetrievalService
from tm_retrieval_capability import (
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalFuzzyPathDecision,
)


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _private_service(snapshot: Any) -> TMRetrievalService:
    port = snapshot.query_port
    field = dataclasses.fields(port)[0]
    service = getattr(port, field.name)
    if type(service) is not TMRetrievalService:
        raise AssertionError("host query port must retain one Core service")
    return service


def _open_paths(
    publisher: RetrievalCapabilityPublisher,
    *,
    evaluated_at_utc: datetime,
    paths: tuple[str, ...] = ("FTS5_TRIGRAM", "GRAM_FALLBACK"),
) -> RetrievalCapabilitySnapshot:
    """Test-only stand-in for a successful Core Gate D publication."""

    current = publisher.snapshot()
    fts5 = (
        RetrievalFuzzyPathDecision(
            path="FTS5_TRIGRAM",
            available=True,
            unavailable_code=None,
        )
        if "FTS5_TRIGRAM" in paths
        else current.fts5_trigram
    )
    fallback = (
        RetrievalFuzzyPathDecision(
            path="GRAM_FALLBACK",
            available=True,
            unavailable_code=None,
        )
        if "GRAM_FALLBACK" in paths
        else current.gram_fallback
    )
    unavailable_codes = tuple(
        sorted(
            {
                code
                for code in (
                    current.context.unavailable_code,
                    current.fuzzy_core.unavailable_code,
                    fts5.unavailable_code,
                    fallback.unavailable_code,
                )
                if code is not None
            }
        )
    )
    next_snapshot = replace(
        current,
        fts5_trigram=fts5,
        gram_fallback=fallback,
        summary=replace(
            current.summary,
            evaluated_at_utc=evaluated_at_utc,
            unavailable_codes=unavailable_codes,
        ),
    )
    setattr(
        publisher,
        "_RetrievalCapabilityPublisher__snapshot",
        next_snapshot,
    )
    return next_snapshot


class _FakeGateDExecution:
    def __init__(
        self,
        *,
        outcome: str = "success",
        safe_code: str | None = None,
        release: Event | None = None,
        open_paths: tuple[str, ...] = (
            "FTS5_TRIGRAM",
            "GRAM_FALLBACK",
        ),
    ) -> None:
        self.outcome = outcome
        self.safe_code = safe_code
        self.release = release
        self.open_paths = open_paths
        self.started = Event()
        self.calls: list[dict[str, object]] = []

    def run_and_publish(
        self,
        *,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        evaluated_at_utc: datetime,
    ) -> object:
        self.calls.append(
            {
                "base_manifest": base_manifest,
                "publisher": publisher,
                "contract_path": contract_path,
                "work_root": work_root,
                "evidence_path": evidence_path,
                "evaluated_at_utc": evaluated_at_utc,
                "mode": os.stat(work_root).st_mode & 0o777,
                "evidence_existed": evidence_path.exists(),
            }
        )
        self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5.0)
        if self.outcome == "error":
            raise cast(Any, host_module)._GateDOperationalError(
                self.safe_code or "GATE_D.FAILED"
            )
        if self.outcome == "preexisting":
            evidence_path.write_text("old receipt", encoding="utf-8")
            raise cast(Any, host_module)._GateDOperationalError(
                "GATE_D.EVIDENCE_PATH_EXISTS"
            )
        evidence_path.write_text("audit", encoding="utf-8")
        snapshot = _open_paths(
            publisher,
            evaluated_at_utc=evaluated_at_utc,
            paths=self.open_paths,
        )
        return cast(Any, host_module)._GateDExecutionResult(
            snapshot=snapshot,
        )


def _composition(
    execution: _FakeGateDExecution,
) -> CapabilityHostComposition:
    composition = cast(Any, host_module).compose_capability_host(
        evaluated_at_utc=_EVALUATED_AT,
    )
    owner = cast(Any, composition).retrieval_gate_d_owner
    object.__setattr__(
        owner,
        "_RetrievalGateDOwner__execute",
        execution.run_and_publish,
    )
    return cast(CapabilityHostComposition, composition)


def _gate_c(composition: CapabilityHostComposition) -> Any:
    owner = cast(Any, composition).retrieval_gate_c_validation_owner
    return owner.validate_gate_c(
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
        evaluated_at_utc=_EVALUATED_AT,
    )


def _gate_d_owner(composition: CapabilityHostComposition) -> Any:
    return cast(Any, composition).retrieval_gate_d_owner


class CapabilityHostGateDTests(unittest.TestCase):
    def test_ordinary_host_import_does_not_load_offline_gate_d_owner(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, capability_host; "
                    "assert 'tm_benchmark_gate' not in sys.modules"
                ),
            ],
            cwd=Path(host_module.__file__).resolve().parent,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_real_binding_pins_core_runner_publication_and_source_graph(
        self,
    ) -> None:
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        self.assertTrue(binding.is_current())
        gate_module = binding.gate_module

        def replacement(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("replaced runner must never execute")

        with patch.object(
            gate_module,
            "run_benchmark_gate_d",
            replacement,
        ):
            self.assertFalse(binding.is_current())

    def test_successful_core_publication_is_not_reclassified_after_refresh(
        self,
    ) -> None:
        composition = _composition(_FakeGateDExecution())
        handoff = _gate_c(composition)
        service = _private_service(handoff)
        publisher = cast(Any, service)._capability_publisher
        base_manifest = cast(
            Any,
            composition.host,
        )._CapabilityHost__retrieval_base_manifest
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        run_result = object()

        def fake_run(*_args: object) -> object:
            return run_result

        def fake_publish(
            _manifest: object,
            _result: object,
            target_publisher: RetrievalCapabilityPublisher,
            **kwargs: object,
        ) -> object:
            snapshot = _open_paths(
                target_publisher,
                evaluated_at_utc=cast(
                    datetime,
                    kwargs["evaluated_at_utc"],
                ),
            )
            return SimpleNamespace(snapshot=snapshot)

        publication = SimpleNamespace(snapshot=publisher.snapshot())
        object.__setattr__(binding, "run_function", fake_run)
        object.__setattr__(binding, "publish_function", fake_publish)
        object.__setattr__(binding, "run_result_type", object)
        object.__setattr__(
            binding,
            "publication_result_type",
            type(publication),
        )
        with tempfile.TemporaryDirectory() as raw_root:
            work_root = Path(raw_root)
            evidence_path = work_root / "benchmark_tm_evidence.json"
            with patch.object(
                type(binding),
                "is_current",
                side_effect=(True, True, False),
            ):
                result = binding.run_and_publish(
                    base_manifest=base_manifest,
                    publisher=publisher,
                    contract_path=(
                        Path(host_module.__file__).resolve().parent
                        / "benchmark_tm_contract.json"
                    ),
                    work_root=work_root,
                    evidence_path=evidence_path,
                    evaluated_at_utc=_EVALUATED_AT,
                )

        self.assertIs(result.snapshot, publisher.snapshot())
        self.assertTrue(result.snapshot.fts5_trigram.available)
        self.assertTrue(result.snapshot.gram_fallback.available)

    def test_owner_is_narrow_async_and_public_graph_remains_query_only(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(execution)
        owner = _gate_d_owner(composition)

        self.assertEqual(
            tuple(name for name in dir(owner) if not name.startswith("_")),
            ("start_gate_d", "status", "wait"),
        )
        self.assertEqual(
            tuple(inspect.signature(owner.start_gate_d).parameters),
            ("evaluated_at_utc",),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    host_module.compose_capability_host
                ).parameters
            ),
            ("evaluated_at_utc",),
        )
        self.assertFalse(
            hasattr(host_module, "_compose_capability_host_for_gate_d_test")
        )
        for value in (
            composition.host,
            composition.host.retrieval_snapshot(),
            composition.host.retrieval_snapshot().query_port,
        ):
            for forbidden in (
                "runner",
                "result",
                "evidence_path",
                "work_root",
                "publisher",
                "manifest",
                "refresh",
                "start_gate_d",
            ):
                self.assertFalse(hasattr(value, forbidden))

    def test_success_uses_gate_c_graph_and_private_absent_workspace(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        gate_c_snapshot = publisher.snapshot()
        matcher = composition.host.matcher_snapshot()

        started = _gate_d_owner(composition).start_gate_d(
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertEqual(started.state.value, "RUNNING")
        finished = _gate_d_owner(composition).wait(timeout=5.0)

        self.assertEqual(finished.state.value, "SUCCEEDED")
        self.assertIsNone(finished.safe_code)
        self.assertEqual(len(execution.calls), 1)
        call = execution.calls[0]
        self.assertIs(call["publisher"], publisher)
        self.assertIsInstance(
            call["base_manifest"],
            RetrievalCapabilityManifest,
        )
        self.assertEqual(
            call["contract_path"],
            Path(host_module.__file__).resolve().parent
            / "benchmark_tm_contract.json",
        )
        self.assertEqual(call["mode"], 0o700)
        self.assertIs(call["evidence_existed"], False)
        self.assertEqual(
            call["evidence_path"],
            cast(Path, call["work_root"])
            / "benchmark_tm_evidence.json",
        )
        self.assertTrue(cast(Path, call["evidence_path"]).is_file())

        current = composition.host.retrieval_snapshot()
        self.assertEqual(gate_c_handoff.generation, 1)
        self.assertEqual(current.generation, 2)
        self.assertIs(current.query_port, gate_c_handoff.query_port)
        self.assertTrue(current.display.context_available)
        self.assertTrue(current.display.fuzzy_available)
        self.assertTrue(publisher.snapshot().fts5_trigram.available)
        self.assertTrue(publisher.snapshot().gram_fallback.available)
        self.assertIsNot(publisher.snapshot(), gate_c_snapshot)
        self.assertIs(composition.host.matcher_snapshot(), matcher)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            2,
        )

    def test_start_is_nonblocking_and_concurrent_start_runs_once(self) -> None:
        release = Event()
        execution = _FakeGateDExecution(release=release)
        composition = _composition(execution)
        _gate_c(composition)
        owner = _gate_d_owner(composition)

        before = time.monotonic()
        first = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.5)
        self.assertTrue(execution.started.wait(timeout=2.0))
        second = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        self.assertEqual(first.epoch, second.epoch)
        self.assertEqual(second.state.value, "RUNNING")
        self.assertEqual(len(execution.calls), 1)

        release.set()
        self.assertEqual(owner.wait(timeout=5.0).state.value, "SUCCEEDED")
        self.assertEqual(len(execution.calls), 1)

    def test_failure_preserves_gate_c_service_context_and_generation(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            outcome="error",
            safe_code="GATE_D.CLEANUP_PENDING",
        )
        composition = _composition(execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        gate_c_capability = publisher.snapshot()

        _gate_d_owner(composition).start_gate_d(
            evaluated_at_utc=_EVALUATED_AT,
        )
        status = _gate_d_owner(composition).wait(timeout=5.0)

        self.assertEqual(status.state.value, "FAILED")
        self.assertEqual(status.safe_code, "GATE_D.CLEANUP_PENDING")
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(publisher.snapshot(), gate_c_capability)
        self.assertTrue(gate_c_handoff.display.context_available)
        self.assertFalse(gate_c_handoff.display.fuzzy_available)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            1,
        )
        self.assertTrue(cast(Path, execution.calls[0]["work_root"]).exists())

    def test_identity_drift_before_publication_preserves_gate_c_graph(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            outcome="error",
            safe_code="GATE_D.IMPLEMENTATION_CHANGED",
        )
        composition = _composition(execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        gate_c_capability = publisher.snapshot()

        owner = _gate_d_owner(composition)
        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        status = owner.wait(timeout=5.0)

        self.assertEqual(status.state.value, "FAILED")
        self.assertEqual(status.safe_code, "GATE_D.IMPLEMENTATION_CHANGED")
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(publisher.snapshot(), gate_c_capability)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            gate_c_handoff.generation,
        )
        self.assertTrue(gate_c_handoff.display.context_available)
        self.assertFalse(gate_c_handoff.display.fuzzy_available)
        self.assertTrue(cast(Path, execution.calls[0]["work_root"]).exists())

    def test_failed_benchmark_paths_publish_closed_without_losing_context(
        self,
    ) -> None:
        execution = _FakeGateDExecution(open_paths=())
        composition = _composition(execution)
        gate_c_handoff = _gate_c(composition)
        owner = _gate_d_owner(composition)

        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        status = owner.wait(timeout=5.0)

        self.assertEqual(status.state.value, "SUCCEEDED")
        current = composition.host.retrieval_snapshot()
        self.assertEqual(current.generation, gate_c_handoff.generation + 1)
        self.assertIs(current.query_port, gate_c_handoff.query_port)
        self.assertTrue(current.display.context_available)
        self.assertFalse(current.display.fuzzy_available)
        capability = cast(
            Any,
            _private_service(current),
        )._capability_publisher.snapshot()
        self.assertTrue(capability.fuzzy_core.available)
        self.assertFalse(capability.fts5_trigram.available)
        self.assertFalse(capability.gram_fallback.available)

    def test_preexisting_evidence_never_publishes_or_reuses_a_receipt(
        self,
    ) -> None:
        execution = _FakeGateDExecution(outcome="preexisting")
        composition = _composition(execution)
        gate_c_handoff = _gate_c(composition)

        _gate_d_owner(composition).start_gate_d(
            evaluated_at_utc=_EVALUATED_AT,
        )
        status = _gate_d_owner(composition).wait(timeout=5.0)

        self.assertEqual(status.state.value, "FAILED")
        self.assertEqual(
            status.safe_code,
            "GATE_D.EVIDENCE_PATH_EXISTS",
        )
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertEqual(gate_c_handoff.generation, 1)

    def test_each_intended_path_is_published_only_from_core_outcome(self) -> None:
        for path in ("FTS5_TRIGRAM", "GRAM_FALLBACK"):
            with self.subTest(path=path):
                execution = _FakeGateDExecution(open_paths=(path,))
                composition = _composition(execution)
                _gate_c(composition)
                owner = _gate_d_owner(composition)
                owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                self.assertEqual(owner.wait(timeout=5.0).state.value, "SUCCEEDED")

                service = _private_service(
                    composition.host.retrieval_snapshot()
                )
                capability = cast(
                    Any,
                    service,
                )._capability_publisher.snapshot()
                self.assertIs(
                    capability.fts5_trigram.available,
                    path == "FTS5_TRIGRAM",
                )
                self.assertIs(
                    capability.gram_fallback.available,
                    path == "GRAM_FALLBACK",
                )
                self.assertTrue(
                    composition.host.retrieval_snapshot().display.fuzzy_available
                )

    def test_each_epoch_uses_a_new_absent_evidence_path(self) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(execution)
        _gate_c(composition)
        owner = _gate_d_owner(composition)

        for expected_epoch in (1, 2):
            status = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            self.assertEqual(status.epoch, expected_epoch)
            self.assertEqual(owner.wait(timeout=5.0).state.value, "SUCCEEDED")

        first, second = execution.calls
        self.assertNotEqual(first["work_root"], second["work_root"])
        self.assertNotEqual(first["evidence_path"], second["evidence_path"])
        self.assertIs(first["evidence_existed"], False)
        self.assertIs(second["evidence_existed"], False)

    def test_inflight_query_keeps_old_snapshot_and_next_query_uses_new(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(execution)
        handoff = _gate_c(composition)
        service = _private_service(handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        captured = Event()
        release = Event()
        observed: list[RetrievalCapabilitySnapshot] = []
        original_snapshot = RetrievalCapabilityPublisher.snapshot

        def observing_snapshot(
            observed_publisher: RetrievalCapabilityPublisher,
        ) -> RetrievalCapabilitySnapshot:
            snapshot = original_snapshot(observed_publisher)
            if current_thread().name == "gate-d-old-query":
                observed.append(snapshot)
                captured.set()
                release.wait(timeout=5.0)
            elif current_thread().name == "gate-d-next-query":
                observed.append(snapshot)
            return snapshot

        query = TMQuery(
            query_source="source",
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=0.60,
            limit=10,
            resource_order=(),
        )
        old_query = Thread(
            target=handoff.query_port.query,
            args=((), query),
            name="gate-d-old-query",
        )
        with patch.object(
            RetrievalCapabilityPublisher,
            "snapshot",
            observing_snapshot,
        ):
            old_query.start()
            self.assertTrue(captured.wait(timeout=2.0))
            owner = _gate_d_owner(composition)
            owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            self.assertEqual(owner.wait(timeout=5.0).state.value, "SUCCEEDED")
            release.set()
            old_query.join(timeout=2.0)
            next_query = Thread(
                target=handoff.query_port.query,
                args=((), query),
                name="gate-d-next-query",
            )
            next_query.start()
            next_query.join(timeout=2.0)

        self.assertFalse(old_query.is_alive())
        self.assertFalse(next_query.is_alive())
        self.assertIs(observed[0], old_capability)
        self.assertIs(observed[-1], publisher.snapshot())
        self.assertIsNot(observed[-1], old_capability)

    def test_gate_d_before_gate_c_stays_closed_without_starting_runner(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(execution)
        initial = composition.host.retrieval_snapshot()

        returned = _gate_d_owner(composition).start_gate_d(
            evaluated_at_utc=_EVALUATED_AT,
        )

        self.assertEqual(returned.state.value, "FAILED")
        self.assertEqual(returned.safe_code, "GATE_D.GATE_C_REQUIRED")
        self.assertEqual(execution.calls, [])
        self.assertIs(composition.host.retrieval_snapshot(), initial)


if __name__ == "__main__":
    unittest.main()
