from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from typing import Any, Iterator, cast
from unittest.mock import patch

import capability_host as host_module
import tm_retrieval_capability as retrieval_capability_module
from capability_host import CapabilityHostComposition
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    StoreHealth,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    candidate_budget_v1,
)
from tm_retrieval import TMRetrievalService
from tm_retrieval_capability import (
    RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalContextDecision,
    RetrievalFuzzyCoreDecision,
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


class _MetadataQueryView:
    """Small canonical view for observing query-effective capability."""

    resource_id = "tm.race"
    generation = 1

    def health(self) -> StoreHealth:
        return StoreHealth(
            healthy=True,
            schema_version=1,
            generation=self.generation,
            record_count=0,
            index_kind="FTS5_TRIGRAM",
            snapshot_binding_digest=None,
            source_binding_state=None,
            exact_available=True,
            context_available=True,
            fuzzy_available=True,
            diagnostic_codes=(),
        )

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        del source_raw
        return ()

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        del record_ids
        return ()


class _MetadataStore:
    """Expose the production service's required canonical query lease."""

    @contextmanager
    def query_lease(self) -> Iterator[_MetadataQueryView]:
        yield _MetadataQueryView()

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        del source_raw
        raise AssertionError("retrieval must use the query lease")

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        del record_ids
        raise AssertionError("retrieval must use the query lease")

    def append(self, draft: TMRecordDraft) -> TMRecord:
        del draft
        raise AssertionError("retrieval must not append")

    def export_records(self) -> Iterator[TMRecord]:
        return iter(())

    def health(self) -> StoreHealth:
        raise AssertionError("retrieval must use the query lease")


class _BlockingMetadataStore(_MetadataStore):
    """Pause one named query after its immutable capability capture."""

    def __init__(self, *, captured: Event, release: Event) -> None:
        self._captured = captured
        self._release = release

    @contextmanager
    def query_lease(self) -> Iterator[_MetadataQueryView]:
        if current_thread().name == "gate-d-old-query":
            self._captured.set()
            if not self._release.wait(timeout=5.0):
                raise AssertionError("old query release was not signalled")
        yield _MetadataQueryView()


class _ZeroCandidateRetriever:
    """Return an honest open-path zero-hit report through the Core port."""

    def candidates_from_view(
        self,
        resource_id: str,
        view: object,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport:
        del view, folded_query
        stages = (
            CandidateStageMetadata(
                stage=CandidateStage.FTS_TRIGRAM,
                input_count=0,
                added_unique_count=0,
                output_unique_count=0,
                dropped_count=0,
            ),
            CandidateStageMetadata(
                stage=CandidateStage.UNION,
                input_count=0,
                added_unique_count=0,
                output_unique_count=0,
                dropped_count=0,
            ),
            CandidateStageMetadata(
                stage=CandidateStage.DEDUPLICATE,
                input_count=0,
                added_unique_count=0,
                output_unique_count=0,
                dropped_count=0,
            ),
        )
        return CandidateRetrievalReport(
            candidates=(),
            metadata=CandidateRecallMetadata(
                resource_id=resource_id,
                index_kind="FTS5_TRIGRAM",
                fuzzy_available=True,
                fuzzy_unavailable_code=None,
                stages=stages,
                union_unique_count=0,
                deduplicated_count=0,
                result_limit=result_limit,
                candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                candidate_budget=candidate_budget_v1(result_limit),
                truncated=False,
            ),
        )


def _path_candidate(
    publisher: RetrievalCapabilityPublisher,
    *,
    evaluated_at_utc: datetime,
    paths: tuple[str, ...] = ("FTS5_TRIGRAM", "GRAM_FALLBACK"),
) -> RetrievalCapabilitySnapshot:
    """Build a test-only Core candidate without making it query-visible."""

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
    return replace(
        current,
        fts5_trigram=fts5,
        gram_fallback=fallback,
        summary=replace(
            current.summary,
            evaluated_at_utc=evaluated_at_utc,
            unavailable_codes=unavailable_codes,
        ),
    )


def _open_paths(
    publisher: RetrievalCapabilityPublisher,
    *,
    evaluated_at_utc: datetime,
    paths: tuple[str, ...] = ("FTS5_TRIGRAM", "GRAM_FALLBACK"),
) -> RetrievalCapabilitySnapshot:
    """Test-only direct commit used outside prepared-publication probes."""

    next_snapshot = _path_candidate(
        publisher,
        evaluated_at_utc=evaluated_at_utc,
        paths=paths,
    )
    setattr(publisher, "_RetrievalCapabilityPublisher__snapshot", next_snapshot)
    return next_snapshot


class _FakeGateDExecution:
    def __init__(
        self,
        *,
        outcome: str = "success",
        safe_code: str | None = None,
        release: Event | None = None,
        publication_release: Event | None = None,
        publication_failure: str | None = None,
        expire_base_authority: bool = False,
        open_paths: tuple[str, ...] = (
            "FTS5_TRIGRAM",
            "GRAM_FALLBACK",
        ),
    ) -> None:
        self.outcome = outcome
        self.safe_code = safe_code
        self.release = release
        self.publication_release = publication_release
        self.publication_failure = publication_failure
        self.expire_base_authority = expire_base_authority
        self.open_paths = open_paths
        self.started = Event()
        self.publication_started = Event()
        self.calls: list[dict[str, object]] = []
        self.bindings: list[object] = []
        self.receipts: list[object] = []

    def run(
        self,
        *,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> object:
        self.calls.append(
            {
                "contract_path": contract_path,
                "work_root": work_root,
                "evidence_path": evidence_path,
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
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        object.__setattr__(binding, "run_result_type", object)
        self.bindings.append(binding)
        receipt = cast(Any, host_module)._CoreGateDPublication(
            _mint=cast(Any, host_module)._GATE_D_PUBLICATION_MINT,
            binding=binding,
            run_result=object(),
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )
        self.receipts.append(receipt)
        return receipt

    def publish(
        self,
        *,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        evaluated_at_utc: datetime,
        prepare_publication: object,
    ) -> object:
        if not self.calls:
            raise AssertionError("Gate D publication requires a completed run")
        self.publication_started.set()
        if self.publication_release is not None:
            self.publication_release.wait(timeout=5.0)
        self.calls[-1].update(
            {
                "base_manifest": base_manifest,
                "publisher": publisher,
                "evaluated_at_utc": evaluated_at_utc,
            }
        )
        if self.publication_failure == "operational":
            raise cast(Any, host_module)._GateDOperationalError(
                self.safe_code or "GATE_D.CLEANUP_PENDING"
            )
        if self.publication_failure == "programmer":
            raise AssertionError("publication programmer error")
        snapshot = _path_candidate(
            publisher,
            evaluated_at_utc=evaluated_at_utc,
            paths=self.open_paths,
        )
        if self.expire_base_authority:
            expired_codes = tuple(
                sorted(
                    {
                        *snapshot.summary.unavailable_codes,
                        RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
                        RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE,
                    }
                )
            )
            snapshot = replace(
                snapshot,
                context=RetrievalContextDecision(
                    available=False,
                    unavailable_code=(
                        RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE
                    ),
                ),
                fuzzy_core=RetrievalFuzzyCoreDecision(
                    available=False,
                    unavailable_code=(
                        RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE
                    ),
                ),
                summary=replace(
                    snapshot.summary,
                    unavailable_codes=expired_codes,
                ),
            )
        if not callable(prepare_publication):
            raise AssertionError("Gate D projection preparation is required")
        prepared = prepare_publication(snapshot)
        setattr(publisher, "_RetrievalCapabilityPublisher__snapshot", snapshot)
        return prepared


class _StructuralGateDExecution(_FakeGateDExecution):
    """Deliberate method-compatible object without the Core receipt mint."""

    def run(
        self,
        *,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> object:
        super().run(
            contract_path=contract_path,
            work_root=work_root,
            evidence_path=evidence_path,
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )
        return self


def _composition(
    test_case: unittest.TestCase,
    execution: _FakeGateDExecution,
) -> CapabilityHostComposition:
    composition = cast(Any, host_module).compose_capability_host(
        evaluated_at_utc=_EVALUATED_AT,
    )
    owner = cast(Any, composition).retrieval_gate_d_owner
    object.__setattr__(
        owner,
        "_RetrievalGateDOwner__execute",
        execution.run,
    )

    def publish_from_authentic_binding(
        binding: object,
        *,
        run_result: object,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        evaluated_at_utc: datetime,
        prepare_publication: object,
    ) -> object:
        del run_result
        if not any(binding is current for current in execution.bindings):
            raise AssertionError("unexpected Gate D binding")
        return execution.publish(
            base_manifest=base_manifest,
            publisher=publisher,
            evaluated_at_utc=evaluated_at_utc,
            prepare_publication=prepare_publication,
        )

    binding_patch = patch.object(
        cast(Any, host_module)._CoreGateDBinding,
        "publish",
        publish_from_authentic_binding,
    )
    binding_patch.start()
    test_case.addCleanup(binding_patch.stop)
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


def _query_effective_recall(snapshot: object) -> CandidateRecallMetadata:
    service = _private_service(snapshot)
    cast(Any, service)._retriever = _ZeroCandidateRetriever()
    report = cast(Any, snapshot).query_port.query(
        (
            TMResourceHandle(
                resource_id="tm.race",
                store=_MetadataStore(),
                active=True,
                lookup=True,
                update=False,
                order=0,
            ),
        ),
        TMQuery(
            query_source="source",
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=0.60,
            limit=10,
            resource_order=("tm.race",),
        ),
    )
    if report.resource_failures or len(report.resource_metadata) != 1:
        raise AssertionError("query-effective recall metadata is required")
    return cast(CandidateRecallMetadata, report.resource_metadata[0].recall)


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

    def test_binding_rejects_foreign_retrieval_publication_helper(
        self,
    ) -> None:
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        gate_module = binding.gate_module

        def foreign_validated_refresh(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("foreign retrieval helper must not run")

        with patch.object(
            retrieval_capability_module,
            "_validated_refresh_retrieval_capability",
            foreign_validated_refresh,
        ), patch.object(
            gate_module,
            "_validated_refresh_retrieval_capability",
            foreign_validated_refresh,
        ):
            self.assertFalse(binding.is_current())
            with self.assertRaises(RuntimeError):
                cast(Any, host_module)._CoreGateDBinding.capture()
            with tempfile.TemporaryDirectory() as raw_root:
                work_root = Path(raw_root)
                with self.assertRaises(
                    cast(Any, host_module)._GateDOperationalError
                ) as run_raised:
                    binding.run(
                        contract_path=(
                            Path(host_module.__file__).resolve().parent
                            / "benchmark_tm_contract.json"
                        ),
                        work_root=work_root,
                        evidence_path=(
                            work_root / "benchmark_tm_evidence.json"
                        ),
                        publication_owner_identity=object(),
                        publication_graph_nonce=object(),
                    )
            with self.assertRaises(
                cast(Any, host_module)._GateDOperationalError
            ) as raised:
                binding.publish(
                    run_result=object.__new__(binding.run_result_type),
                    base_manifest=cast(Any, object()),
                    publisher=cast(Any, object()),
                    evaluated_at_utc=_EVALUATED_AT,
                    prepare_publication=cast(Any, lambda snapshot: snapshot),
                )

        self.assertEqual(
            run_raised.exception.error_code,
            "GATE_D.IMPLEMENTATION_CHANGED",
        )
        self.assertEqual(
            raised.exception.error_code,
            "GATE_D.IMPLEMENTATION_CHANGED",
        )

    def test_late_foreign_transition_cannot_split_formal_publication(
        self,
    ) -> None:
        composition = cast(Any, host_module).compose_capability_host(
            evaluated_at_utc=_EVALUATED_AT,
        )
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        self.assertFalse(
            _query_effective_recall(gate_c_handoff).fuzzy_available
        )
        host = composition.host
        owner_identity = cast(
            Any,
            host,
        )._CapabilityHost__retrieval_owner_identity
        graph = cast(Any, host)._capture_gate_d_graph(
            owner_identity=owner_identity,
        )
        self.assertIsNotNone(graph)
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        benchmark_tests = cast(
            Any,
            importlib.import_module("tests.test_tm_benchmark_gate"),
        )
        bundle = benchmark_tests._combined_bundle(
            fts5_missing=0,
            fallback_missing=0,
        )
        artifact_bytes = cast(
            Any,
            binding.gate_module,
        ).benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        run_result = cast(
            Any,
            binding.gate_module,
        )._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact_bytes),
            artifact_digest=hashlib.sha256(artifact_bytes).hexdigest(),
            test_mode=False,
        )
        publication = cast(Any, host_module)._CoreGateDPublication(
            _mint=cast(Any, host_module)._GATE_D_PUBLICATION_MINT,
            binding=binding,
            run_result=run_result,
            publication_owner_identity=owner_identity,
            publication_graph_nonce=graph.publication_nonce,
        )
        original_is_current = type(binding).is_current
        original_transition = (
            retrieval_capability_module
            ._validated_refresh_retrieval_capability
        )
        original_gate_alias = (
            binding.gate_module.__dict__[
                "_validated_refresh_retrieval_capability"
            ]
        )
        original_decision_verifier = binding.gate_module.__dict__[
            "_verify_path_decisions_match_reports"
        ]
        original_result_constructor = binding.gate_module.__dict__[
            "RetrievalCapabilityPublicationResult"
        ]
        original_gate_cast = binding.gate_module.__dict__["cast"]
        foreign_calls = {
            "decision_verifier": 0,
            "gate_cast": 0,
            "result_constructor": 0,
            "transition": 0,
        }
        errors: list[BaseException] = []
        result: Any = None
        swapped = False

        def foreign_transition(*args: Any, **kwargs: Any) -> object:
            foreign_calls["transition"] += 1
            original_transition(*args, **kwargs)
            raise AssertionError("late foreign transition ran after commit")

        def foreign_decision_verifier(*_args: Any, **_kwargs: Any) -> None:
            foreign_calls["decision_verifier"] += 1
            raise AssertionError("late foreign decision verifier ran")

        def foreign_result_constructor(*_args: Any, **_kwargs: Any) -> object:
            foreign_calls["result_constructor"] += 1
            raise AssertionError("late foreign result constructor ran")

        def foreign_gate_cast(*_args: Any, **_kwargs: Any) -> object:
            foreign_calls["gate_cast"] += 1
            raise AssertionError("late foreign Gate D cast ran")

        def current_then_replace(observed_binding: object) -> bool:
            nonlocal swapped
            current = original_is_current(observed_binding)
            if current and observed_binding is binding and not swapped:
                setattr(
                    retrieval_capability_module,
                    "_validated_refresh_retrieval_capability",
                    foreign_transition,
                )
                setattr(
                    binding.gate_module,
                    "_validated_refresh_retrieval_capability",
                    foreign_transition,
                )
                setattr(
                    binding.gate_module,
                    "_verify_path_decisions_match_reports",
                    foreign_decision_verifier,
                )
                setattr(
                    binding.gate_module,
                    "RetrievalCapabilityPublicationResult",
                    foreign_result_constructor,
                )
                setattr(binding.gate_module, "cast", foreign_gate_cast)
                swapped = True
            return current

        try:
            with patch.object(
                type(binding),
                "is_current",
                current_then_replace,
            ):
                try:
                    result = cast(Any, host)._publish_gate_d_capability(
                        owner_identity=owner_identity,
                        graph=graph,
                        publication=publication,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                except BaseException as error:
                    errors.append(error)
        finally:
            setattr(
                retrieval_capability_module,
                "_validated_refresh_retrieval_capability",
                original_transition,
            )
            setattr(
                binding.gate_module,
                "_validated_refresh_retrieval_capability",
                original_gate_alias,
            )
            setattr(
                binding.gate_module,
                "_verify_path_decisions_match_reports",
                original_decision_verifier,
            )
            setattr(
                binding.gate_module,
                "RetrievalCapabilityPublicationResult",
                original_result_constructor,
            )
            setattr(binding.gate_module, "cast", original_gate_cast)

        recall = _query_effective_recall(gate_c_handoff)
        current_handoff = host.retrieval_snapshot()
        self.maxDiff = None
        self.assertEqual(
            {
                "errors": len(errors),
                "foreign_calls": foreign_calls,
                "publisher_changed": publisher.snapshot() is not old_capability,
                "query_fuzzy_available": recall.fuzzy_available,
                "returned_generation": (
                    None if result is None else result.generation
                ),
                "host_generation": current_handoff.generation,
            },
            {
                "errors": 0,
                "foreign_calls": {
                    "decision_verifier": 0,
                    "gate_cast": 0,
                    "result_constructor": 0,
                    "transition": 0,
                },
                "publisher_changed": True,
                "query_fuzzy_available": True,
                "returned_generation": 2,
                "host_generation": 2,
            },
        )
        assert result is not None
        self.assertIs(current_handoff, result)
        self.assertIs(host.status_snapshot().retrieval, result.display)
        self.assertEqual(
            host.retrieval_generation_notifications().current(),
            result.generation,
        )

    def test_late_host_cast_cannot_fail_after_formal_commit(self) -> None:
        composition = cast(Any, host_module).compose_capability_host(
            evaluated_at_utc=_EVALUATED_AT,
        )
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        self.assertFalse(
            _query_effective_recall(gate_c_handoff).fuzzy_available
        )
        host = composition.host
        owner_identity = cast(
            Any,
            host,
        )._CapabilityHost__retrieval_owner_identity
        graph = cast(Any, host)._capture_gate_d_graph(
            owner_identity=owner_identity,
        )
        self.assertIsNotNone(graph)
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        benchmark_tests = cast(
            Any,
            importlib.import_module("tests.test_tm_benchmark_gate"),
        )
        bundle = benchmark_tests._combined_bundle(
            fts5_missing=0,
            fallback_missing=0,
        )
        artifact_bytes = cast(
            Any,
            binding.gate_module,
        ).benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        run_result = cast(
            Any,
            binding.gate_module,
        )._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact_bytes),
            artifact_digest=hashlib.sha256(artifact_bytes).hexdigest(),
            test_mode=False,
        )
        publication = cast(Any, host_module)._CoreGateDPublication(
            _mint=cast(Any, host_module)._GATE_D_PUBLICATION_MINT,
            binding=binding,
            run_result=run_result,
            publication_owner_identity=owner_identity,
            publication_graph_nonce=graph.publication_nonce,
        )
        observer = host.retrieval_generation_notifications()
        notification_type = cast(Any, type(observer))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        original_host_cast = host_module.cast
        foreign_calls: list[None] = []
        errors: list[BaseException] = []
        result: Any = None

        def foreign_host_cast(*_args: Any, **_kwargs: Any) -> object:
            foreign_calls.append(None)
            raise AssertionError("late host cast ran after commit")

        def publish_then_replace_cast(
            notification: object,
            generation: int,
        ) -> None:
            original_publish(notification, generation)
            setattr(host_module, "cast", foreign_host_cast)

        try:
            with patch.object(
                notification_type,
                "_publish_prevalidated_locked",
                publish_then_replace_cast,
            ):
                try:
                    result = cast(Any, host)._publish_gate_d_capability(
                        owner_identity=owner_identity,
                        graph=graph,
                        publication=publication,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                except BaseException as error:
                    errors.append(error)
        finally:
            setattr(host_module, "cast", original_host_cast)

        recall = _query_effective_recall(gate_c_handoff)
        current_handoff = host.retrieval_snapshot()
        self.assertEqual(
            {
                "errors": len(errors),
                "foreign_calls": len(foreign_calls),
                "publisher_changed": publisher.snapshot() is not old_capability,
                "query_fuzzy_available": recall.fuzzy_available,
                "returned_generation": (
                    None if result is None else result.generation
                ),
                "host_generation": current_handoff.generation,
            },
            {
                "errors": 0,
                "foreign_calls": 0,
                "publisher_changed": True,
                "query_fuzzy_available": True,
                "returned_generation": 2,
                "host_generation": 2,
            },
        )
        assert result is not None
        self.assertIs(current_handoff, result)
        self.assertIs(host.status_snapshot().retrieval, result.display)
        self.assertEqual(observer.current(), result.generation)

    def test_late_gate_d_status_constructor_cannot_split_owner_from_commit(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        observer = composition.host.retrieval_generation_notifications()
        notification_type = cast(Any, type(observer))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        original_status_constructor = host_module.GateDRunStatus
        foreign_calls: list[None] = []
        thread_errors: list[BaseException] = []

        def foreign_status_constructor(
            *_args: Any,
            **_kwargs: Any,
        ) -> object:
            foreign_calls.append(None)
            raise AssertionError(
                "late Gate D status constructor ran after commit"
            )

        def publish_then_replace_status_constructor(
            notification: object,
            generation: int,
        ) -> None:
            original_publish(notification, generation)
            setattr(
                host_module,
                "GateDRunStatus",
                foreign_status_constructor,
            )

        owner = _gate_d_owner(composition)
        try:
            with (
                patch.object(
                    notification_type,
                    "_publish_prevalidated_locked",
                    publish_then_replace_status_constructor,
                ),
                patch(
                    "threading.excepthook",
                    lambda args: thread_errors.append(args.exc_value),
                ),
            ):
                owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                finished = owner.wait(timeout=1.0)
        finally:
            setattr(
                host_module,
                "GateDRunStatus",
                original_status_constructor,
            )

        recall = _query_effective_recall(gate_c_handoff)
        current_handoff = composition.host.retrieval_snapshot()
        self.assertEqual(
            {
                "foreign_calls": len(foreign_calls),
                "thread_errors": len(thread_errors),
                "owner_state": finished.state.value,
                "publisher_changed": publisher.snapshot() is not old_capability,
                "query_fuzzy_available": recall.fuzzy_available,
                "host_generation": current_handoff.generation,
                "notification_generation": observer.current(),
            },
            {
                "foreign_calls": 0,
                "thread_errors": 0,
                "owner_state": "SUCCEEDED",
                "publisher_changed": True,
                "query_fuzzy_available": True,
                "host_generation": 2,
                "notification_generation": 2,
            },
        )
        self.assertIs(
            composition.host.status_snapshot().retrieval,
            current_handoff.display,
        )

    def test_late_snapshot_descriptor_replacement_cannot_split_precommit(
        self,
    ) -> None:
        composition = cast(Any, host_module).compose_capability_host(
            evaluated_at_utc=_EVALUATED_AT,
        )
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        binding = cast(Any, host_module)._CoreGateDBinding.capture()
        benchmark_tests = cast(
            Any,
            importlib.import_module("tests.test_tm_benchmark_gate"),
        )
        bundle = benchmark_tests._combined_bundle(
            fts5_missing=0,
            fallback_missing=0,
        )
        artifact_bytes = cast(
            Any,
            binding.gate_module,
        ).benchmark_evidence_bundle_to_json(bundle).encode("utf-8")
        run_result = cast(
            Any,
            binding.gate_module,
        )._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact_bytes),
            artifact_digest=hashlib.sha256(artifact_bytes).hexdigest(),
            test_mode=False,
        )
        owner = _gate_d_owner(composition)

        def authentic_execute(
            *,
            contract_path: Path,
            work_root: Path,
            evidence_path: Path,
            publication_owner_identity: object,
            publication_graph_nonce: object,
        ) -> object:
            del contract_path, work_root, evidence_path
            return cast(Any, host_module)._CoreGateDPublication(
                _mint=cast(Any, host_module)._GATE_D_PUBLICATION_MINT,
                binding=binding,
                run_result=run_result,
                publication_owner_identity=publication_owner_identity,
                publication_graph_nonce=publication_graph_nonce,
            )

        object.__setattr__(
            owner,
            "_RetrievalGateDOwner__execute",
            authentic_execute,
        )
        publisher_type = type(publisher)
        slot_name = "_RetrievalCapabilityPublisher__snapshot"
        original_descriptor = publisher_type.__dict__[slot_name]
        observer = composition.host.retrieval_generation_notifications()
        notification_type = cast(Any, type(observer))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        foreign_sets: list[object] = []

        class FailingSnapshotDescriptor:
            def __get__(self, instance: object, owner: object) -> object:
                return original_descriptor.__get__(instance, owner)

            def __set__(self, instance: object, value: object) -> None:
                del instance
                foreign_sets.append(value)
                raise AssertionError(
                    "late snapshot descriptor ran after host precommit"
                )

        def publish_then_replace_snapshot_descriptor(
            notification: object,
            generation: int,
        ) -> None:
            original_publish(notification, generation)
            setattr(
                publisher_type,
                slot_name,
                FailingSnapshotDescriptor(),
            )

        try:
            with patch.object(
                notification_type,
                "_publish_prevalidated_locked",
                publish_then_replace_snapshot_descriptor,
            ):
                owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                finished = owner.wait(timeout=5.0)
        finally:
            setattr(publisher_type, slot_name, original_descriptor)

        recall = _query_effective_recall(gate_c_handoff)
        current_handoff = composition.host.retrieval_snapshot()
        self.assertEqual(
            {
                "foreign_sets": len(foreign_sets),
                "owner_state": finished.state.value,
                "publisher_changed": publisher.snapshot() is not old_capability,
                "query_fuzzy_available": recall.fuzzy_available,
                "host_generation": current_handoff.generation,
                "notification_generation": observer.current(),
            },
            {
                "foreign_sets": 0,
                "owner_state": "SUCCEEDED",
                "publisher_changed": True,
                "query_fuzzy_available": True,
                "host_generation": 2,
                "notification_generation": 2,
            },
        )
        self.assertIs(
            composition.host.status_snapshot().retrieval,
            current_handoff.display,
        )

    def _assert_owner_success_notification_failure_is_atomic(
        self,
        *,
        fail_after_notify: bool,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        display = composition.host.status_snapshot().retrieval
        observer = composition.host.retrieval_generation_notifications()
        owner = _gate_d_owner(composition)
        owner_condition = cast(
            Any,
            owner,
        )._RetrievalGateDOwner__condition
        original_notify = owner_condition.notify_all
        failure = RuntimeError("Gate D owner success notification failed")

        def failing_notify() -> None:
            setattr(owner_condition, "notify_all", original_notify)
            if fail_after_notify:
                original_notify()
            raise failure

        with patch.object(owner_condition, "notify_all", failing_notify):
            owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            with self.assertRaises(RuntimeError) as raised:
                owner.wait(timeout=5.0)

        self.assertIs(raised.exception, failure)
        self.assertEqual(owner.status().state.value, "FAILED")
        self.assertEqual(owner.status().safe_code, "GATE_D.PROGRAMMER_ERROR")
        recall = _query_effective_recall(gate_c_handoff)
        self.assertFalse(recall.fuzzy_available)
        self.assertIs(publisher.snapshot(), old_capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(composition.host.status_snapshot().retrieval, display)
        self.assertEqual(observer.current(), gate_c_handoff.generation)

    def test_owner_success_notification_failure_before_notify_is_atomic(
        self,
    ) -> None:
        self._assert_owner_success_notification_failure_is_atomic(
            fail_after_notify=False
        )

    def test_owner_success_notification_failure_after_notify_is_atomic(
        self,
    ) -> None:
        self._assert_owner_success_notification_failure_is_atomic(
            fail_after_notify=True
        )

    def test_core_publication_rejects_cross_graph_and_replay_after_success(
        self,
    ) -> None:
        composition = cast(Any, host_module).compose_capability_host(
            evaluated_at_utc=_EVALUATED_AT,
        )
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
            snapshot = _path_candidate(
                target_publisher,
                evaluated_at_utc=cast(
                    datetime,
                    kwargs["evaluated_at_utc"],
                ),
            )
            core_result = SimpleNamespace(snapshot=snapshot)
            prepare = kwargs["prepare_publication"]
            if not callable(prepare):
                raise AssertionError("Core prepare callback is required")
            prepared = prepare(core_result)
            setattr(
                target_publisher,
                "_RetrievalCapabilityPublisher__snapshot",
                snapshot,
            )
            return prepared

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
            publication_owner_identity = object()
            publication_graph_nonce = object()
            with patch.object(
                type(binding),
                "is_current",
                side_effect=(True, True, True, False),
            ) as current_check:
                receipt = binding.run(
                    contract_path=(
                        Path(host_module.__file__).resolve().parent
                        / "benchmark_tm_contract.json"
                    ),
                    work_root=work_root,
                    evidence_path=evidence_path,
                    publication_owner_identity=publication_owner_identity,
                    publication_graph_nonce=publication_graph_nonce,
                )
                before_publication = publisher.snapshot()
                with self.assertRaises(PermissionError):
                    receipt.publish(
                        publication_owner_identity=object(),
                        publication_graph_nonce=object(),
                        base_manifest=base_manifest,
                        publisher=publisher,
                        evaluated_at_utc=_EVALUATED_AT,
                        prepare_publication=lambda snapshot: SimpleNamespace(
                            snapshot=snapshot
                        ),
                    )
                self.assertIs(publisher.snapshot(), before_publication)
                result = receipt.publish(
                    publication_owner_identity=publication_owner_identity,
                    publication_graph_nonce=publication_graph_nonce,
                    base_manifest=base_manifest,
                    publisher=publisher,
                    evaluated_at_utc=_EVALUATED_AT,
                    prepare_publication=lambda snapshot: SimpleNamespace(
                        snapshot=snapshot
                    ),
                )
                with self.assertRaises(RuntimeError):
                    receipt.publish(
                        publication_owner_identity=publication_owner_identity,
                        publication_graph_nonce=publication_graph_nonce,
                        base_manifest=base_manifest,
                        publisher=publisher,
                        evaluated_at_utc=_EVALUATED_AT,
                        prepare_publication=lambda snapshot: SimpleNamespace(
                            snapshot=snapshot
                        ),
                    )

        self.assertEqual(current_check.call_count, 3)
        self.assertIs(result.snapshot, publisher.snapshot())
        self.assertTrue(result.snapshot.fts5_trigram.available)
        self.assertTrue(result.snapshot.gram_fallback.available)

    def test_owner_is_narrow_async_and_public_graph_remains_query_only(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
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

    def test_structural_publication_lookalike_is_rejected_before_refresh(
        self,
    ) -> None:
        execution = _StructuralGateDExecution()
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        capability = publisher.snapshot()
        owner = _gate_d_owner(composition)

        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        with self.assertRaises(TypeError):
            owner.wait(timeout=5.0)

        self.assertFalse(execution.publication_started.is_set())
        self.assertIs(publisher.snapshot(), capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(
            composition.host.status_snapshot().retrieval,
            gate_c_handoff.display,
        )
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            gate_c_handoff.generation,
        )

    def test_receipt_rejects_second_host_then_allows_only_one_bound_use(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        first = _composition(self, execution)
        second = _composition(self, execution)
        first_handoff = _gate_c(first)
        second_handoff = _gate_c(second)
        first_host = cast(Any, first.host)
        second_host = cast(Any, second.host)
        first_owner = first_host._CapabilityHost__retrieval_owner_identity
        second_owner = second_host._CapabilityHost__retrieval_owner_identity
        first_graph = first_host._capture_gate_d_graph(
            owner_identity=first_owner
        )
        second_graph = second_host._capture_gate_d_graph(
            owner_identity=second_owner
        )
        if first_graph is None or second_graph is None:
            raise AssertionError("two Gate C graphs are required")

        with tempfile.TemporaryDirectory() as raw_root:
            work_root = Path(raw_root)
            receipt = cast(Any, execution.run(
                contract_path=(
                    Path(host_module.__file__).resolve().parent
                    / "benchmark_tm_contract.json"
                ),
                work_root=work_root,
                evidence_path=work_root / "benchmark_tm_evidence.json",
                publication_owner_identity=first_owner,
                publication_graph_nonce=first_graph.publication_nonce,
            ))

            second_capability = second_graph.publisher.snapshot()
            with self.assertRaises(PermissionError):
                second_host._publish_gate_d_capability(
                    owner_identity=second_owner,
                    graph=second_graph,
                    publication=receipt,
                    evaluated_at_utc=_EVALUATED_AT,
                )
            self.assertIs(
                second_graph.publisher.snapshot(),
                second_capability,
            )
            self.assertIs(second.host.retrieval_snapshot(), second_handoff)

            installed = first_host._publish_gate_d_capability(
                owner_identity=first_owner,
                graph=first_graph,
                publication=receipt,
                evaluated_at_utc=_EVALUATED_AT,
            )
            self.assertIsNotNone(installed)
            with self.assertRaises(RuntimeError):
                receipt.publish(
                    publication_owner_identity=first_owner,
                    publication_graph_nonce=first_graph.publication_nonce,
                    base_manifest=first_graph.base_manifest,
                    publisher=first_graph.publisher,
                    evaluated_at_utc=_EVALUATED_AT,
                    prepare_publication=lambda snapshot: SimpleNamespace(
                        snapshot=snapshot
                    ),
                )

        self.assertEqual(first.host.retrieval_snapshot().generation, 2)
        self.assertEqual(second.host.retrieval_snapshot().generation, 1)
        self.assertIsNot(first.host.retrieval_snapshot(), first_handoff)

    def test_success_uses_gate_c_graph_and_private_absent_workspace(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
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
        composition = _composition(self, execution)
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
        composition = _composition(self, execution)
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

    def test_operational_publication_error_never_opens_old_query_port(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            publication_failure="operational",
            safe_code="GATE_D.CLEANUP_PENDING",
        )
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        publisher = cast(
            Any,
            _private_service(gate_c_handoff),
        )._capability_publisher
        capability = publisher.snapshot()
        owner = _gate_d_owner(composition)

        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        status = owner.wait(timeout=5.0)

        self.assertEqual(status.state.value, "FAILED")
        self.assertEqual(status.safe_code, "GATE_D.CLEANUP_PENDING")
        recall = _query_effective_recall(gate_c_handoff)
        self.assertFalse(recall.fuzzy_available)
        self.assertEqual(
            recall.fuzzy_unavailable_code,
            "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",
        )
        self.assertIs(publisher.snapshot(), capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            gate_c_handoff.generation,
        )

    def test_programmer_publication_error_never_opens_old_query_port(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            publication_failure="programmer",
        )
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        publisher = cast(
            Any,
            _private_service(gate_c_handoff),
        )._capability_publisher
        capability = publisher.snapshot()
        owner = _gate_d_owner(composition)

        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        with self.assertRaises(AssertionError):
            owner.wait(timeout=5.0)

        recall = _query_effective_recall(gate_c_handoff)
        self.assertFalse(recall.fuzzy_available)
        self.assertEqual(
            recall.fuzzy_unavailable_code,
            "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",
        )
        self.assertIs(publisher.snapshot(), capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            gate_c_handoff.generation,
        )

    def test_expired_gate_c_candidate_never_changes_query_or_host_state(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            open_paths=(),
            expire_base_authority=True,
        )
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        publisher = cast(
            Any,
            _private_service(gate_c_handoff),
        )._capability_publisher
        capability = publisher.snapshot()
        display = composition.host.status_snapshot().retrieval
        observer = composition.host.retrieval_generation_notifications()
        owner = _gate_d_owner(composition)

        owner.start_gate_d(
            evaluated_at_utc=datetime(
                2030,
                1,
                3,
                tzinfo=timezone.utc,
            )
        )
        with self.assertRaises(RuntimeError):
            owner.wait(timeout=5.0)

        recall = _query_effective_recall(gate_c_handoff)
        self.assertFalse(recall.fuzzy_available)
        self.assertIs(publisher.snapshot(), capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(composition.host.status_snapshot().retrieval, display)
        self.assertEqual(observer.current(), gate_c_handoff.generation)

    def _assert_notification_failure_is_atomic(
        self,
        *,
        fail_after_generation_assignment: bool,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        publisher = cast(
            Any,
            _private_service(gate_c_handoff),
        )._capability_publisher
        capability = publisher.snapshot()
        display = composition.host.status_snapshot().retrieval
        observer = composition.host.retrieval_generation_notifications()
        notification_type = cast(Any, type(observer))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        failure = RuntimeError("notification publication failed")

        def failing_publish(notification: object, generation: int) -> None:
            if fail_after_generation_assignment:
                original_publish(notification, generation)
            raise failure

        owner = _gate_d_owner(composition)
        with patch.object(
            notification_type,
            "_publish_prevalidated_locked",
            failing_publish,
        ):
            owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            with self.assertRaises(RuntimeError) as raised:
                owner.wait(timeout=5.0)

        self.assertIs(raised.exception, failure)
        self.assertEqual(owner.status().state.value, "FAILED")
        self.assertEqual(owner.status().safe_code, "GATE_D.PROGRAMMER_ERROR")
        recall = _query_effective_recall(gate_c_handoff)
        self.assertFalse(recall.fuzzy_available)
        self.assertIs(publisher.snapshot(), capability)
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(composition.host.status_snapshot().retrieval, display)
        self.assertEqual(observer.current(), gate_c_handoff.generation)

    def test_notification_failure_before_assignment_is_atomic(self) -> None:
        self._assert_notification_failure_is_atomic(
            fail_after_generation_assignment=False
        )

    def test_notification_failure_after_assignment_is_atomic(self) -> None:
        self._assert_notification_failure_is_atomic(
            fail_after_generation_assignment=True
        )

    def test_gate_c_swap_before_gate_d_publication_never_opens_old_graph(
        self,
    ) -> None:
        publication_release = Event()
        execution = _FakeGateDExecution(release=publication_release)
        composition = _composition(self, execution)
        old_handoff = _gate_c(composition)
        old_service = _private_service(old_handoff)
        old_publisher = cast(Any, old_service)._capability_publisher
        old_capability = old_publisher.snapshot()
        observer = composition.host.retrieval_generation_notifications()
        owner = _gate_d_owner(composition)

        started = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        self.assertEqual(started.state.value, "RUNNING")
        self.assertTrue(execution.started.wait(timeout=2.0))

        current_gate_c = _gate_c(composition)
        current_service = _private_service(current_gate_c)
        current_publisher = cast(Any, current_service)._capability_publisher
        current_capability = current_publisher.snapshot()
        current_handoff = current_gate_c
        self.assertIsNot(current_handoff, old_handoff)
        self.assertIsNot(current_publisher, old_publisher)
        self.assertEqual(current_handoff.generation, 2)
        self.assertEqual(observer.current(), 2)

        publication_release.set()
        finished = owner.wait(timeout=5.0)

        self.assertEqual(finished.state.value, "FAILED")
        self.assertEqual(finished.safe_code, "GATE_D.GATE_C_CHANGED")
        self.assertIs(old_publisher.snapshot(), old_capability)
        self.assertFalse(old_publisher.snapshot().fts5_trigram.available)
        self.assertFalse(old_publisher.snapshot().gram_fallback.available)
        old_report = old_handoff.query_port.query(
            (
                TMResourceHandle(
                    resource_id="tm.race",
                    store=_MetadataStore(),
                    active=True,
                    lookup=True,
                    update=False,
                    order=0,
                ),
            ),
            TMQuery(
                query_source="source",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=0.60,
                limit=10,
                resource_order=("tm.race",),
            ),
        )
        self.assertEqual(old_report.resource_failures, ())
        self.assertEqual(len(old_report.resource_metadata), 1)
        self.assertFalse(
            old_report.resource_metadata[0].recall.fuzzy_available
        )
        self.assertEqual(
            old_report.resource_metadata[0].recall.fuzzy_unavailable_code,
            "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",
        )
        self.assertIs(current_publisher.snapshot(), current_capability)
        self.assertFalse(current_publisher.snapshot().fts5_trigram.available)
        self.assertFalse(current_publisher.snapshot().gram_fallback.available)
        self.assertIs(
            composition.host.retrieval_snapshot(),
            current_handoff,
        )
        self.assertIs(
            composition.host.status_snapshot().retrieval,
            current_handoff.display,
        )
        self.assertEqual(observer.current(), 2)

    def test_gate_c_install_waits_for_reserved_gate_d_publication(
        self,
    ) -> None:
        publication_release = Event()
        execution = _FakeGateDExecution(
            publication_release=publication_release,
        )
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        gate_c_service = _private_service(gate_c_handoff)
        gate_c_publisher = cast(Any, gate_c_service)._capability_publisher
        observer = composition.host.retrieval_generation_notifications()
        owner = _gate_d_owner(composition)
        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        self.assertTrue(execution.publication_started.wait(timeout=2.0))

        gate_c_started = Event()
        gate_c_results: list[object] = []
        gate_c_errors: list[BaseException] = []

        def replace_gate_c() -> None:
            gate_c_started.set()
            try:
                gate_c_results.append(_gate_c(composition))
            except BaseException as error:
                gate_c_errors.append(error)

        gate_c_thread = Thread(
            target=replace_gate_c,
            name="gate-c-during-gate-d-publication",
        )
        gate_c_thread.start()
        gate_d_status = None
        try:
            self.assertTrue(gate_c_started.wait(timeout=5.0))
            self.assertTrue(gate_c_thread.is_alive())
        finally:
            publication_release.set()
            try:
                gate_d_status = owner.wait(timeout=5.0)
            finally:
                gate_c_thread.join(timeout=5.0)

        self.assertFalse(gate_c_thread.is_alive())
        self.assertEqual(gate_c_errors, [])
        self.assertIsNotNone(gate_d_status)
        assert gate_d_status is not None
        self.assertEqual(gate_d_status.state.value, "SUCCEEDED")
        self.assertEqual(len(gate_c_results), 1)
        current = gate_c_results[0]
        self.assertIs(composition.host.retrieval_snapshot(), current)
        self.assertEqual(cast(Any, current).generation, 3)
        self.assertEqual(observer.current(), 3)
        self.assertTrue(gate_c_publisher.snapshot().fts5_trigram.available)
        self.assertTrue(gate_c_publisher.snapshot().gram_fallback.available)
        current_service = _private_service(current)
        current_publisher = cast(Any, current_service)._capability_publisher
        self.assertIsNot(current_publisher, gate_c_publisher)
        self.assertFalse(current_publisher.snapshot().fts5_trigram.available)
        self.assertFalse(current_publisher.snapshot().gram_fallback.available)
        self.assertIs(
            composition.host.status_snapshot().retrieval,
            cast(Any, current).display,
        )

    def test_identity_drift_before_publication_preserves_gate_c_graph(
        self,
    ) -> None:
        execution = _FakeGateDExecution(
            outcome="error",
            safe_code="GATE_D.IMPLEMENTATION_CHANGED",
        )
        composition = _composition(self, execution)
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

    def test_publication_programmer_error_propagates_without_handoff_swap(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
        gate_c_handoff = _gate_c(composition)
        service = _private_service(gate_c_handoff)
        publisher = cast(Any, service)._capability_publisher
        gate_c_capability = publisher.snapshot()
        owner = _gate_d_owner(composition)
        programmer_error = AssertionError("publication programmer error")

        with patch.object(
            execution,
            "publish",
            side_effect=programmer_error,
        ):
            owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            with self.assertRaises(AssertionError) as raised:
                owner.wait(timeout=5.0)

        self.assertIs(raised.exception, programmer_error)
        self.assertEqual(owner.status().state.value, "FAILED")
        self.assertEqual(owner.status().safe_code, "GATE_D.PROGRAMMER_ERROR")
        self.assertIs(composition.host.retrieval_snapshot(), gate_c_handoff)
        self.assertIs(publisher.snapshot(), gate_c_capability)
        self.assertEqual(
            composition.host.retrieval_generation_notifications().current(),
            gate_c_handoff.generation,
        )

    def test_failed_benchmark_paths_publish_closed_without_losing_context(
        self,
    ) -> None:
        execution = _FakeGateDExecution(open_paths=())
        composition = _composition(self, execution)
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
        composition = _composition(self, execution)
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
                composition = _composition(self, execution)
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
        composition = _composition(self, execution)
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
        composition = _composition(self, execution)
        handoff = _gate_c(composition)
        service = _private_service(handoff)
        publisher = cast(Any, service)._capability_publisher
        old_capability = publisher.snapshot()
        captured = Event()
        release = Event()
        cast(Any, service)._retriever = _ZeroCandidateRetriever()
        resource = TMResourceHandle(
            resource_id="tm.race",
            store=_BlockingMetadataStore(
                captured=captured,
                release=release,
            ),
            active=True,
            lookup=True,
            update=False,
            order=0,
        )

        query = TMQuery(
            query_source="source",
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=0.60,
            limit=10,
            resource_order=("tm.race",),
        )
        old_reports: list[Any] = []
        old_errors: list[BaseException] = []

        def run_old_query() -> None:
            try:
                old_reports.append(handoff.query_port.query((resource,), query))
            except BaseException as error:
                old_errors.append(error)

        old_query = Thread(
            target=run_old_query,
            name="gate-d-old-query",
        )
        old_query.start()
        try:
            self.assertTrue(captured.wait(timeout=2.0))
            owner = _gate_d_owner(composition)
            owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
            self.assertEqual(owner.wait(timeout=5.0).state.value, "SUCCEEDED")
        finally:
            release.set()
            old_query.join(timeout=2.0)

        self.assertFalse(old_query.is_alive())
        self.assertEqual(old_errors, [])
        self.assertEqual(len(old_reports), 1)
        old_recall = old_reports[0].resource_metadata[0].recall
        self.assertFalse(old_recall.fuzzy_available)
        self.assertEqual(
            old_recall.fuzzy_unavailable_code,
            "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",
        )
        self.assertIsNot(publisher.snapshot(), old_capability)

        next_report = handoff.query_port.query((resource,), query)
        self.assertEqual(next_report.resource_failures, ())
        self.assertEqual(len(next_report.resource_metadata), 1)
        self.assertTrue(
            next_report.resource_metadata[0].recall.fuzzy_available
        )

    def test_gate_d_before_gate_c_stays_closed_without_starting_runner(
        self,
    ) -> None:
        execution = _FakeGateDExecution()
        composition = _composition(self, execution)
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
